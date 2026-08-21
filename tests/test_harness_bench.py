import importlib.util
import json
import shutil
import threading
from pathlib import Path

import pytest

from driver import live
from driver.approver import Approver, Manifest
from driver.wire import Frames
from compare_harness_runs import compare_runs
from audit_harness_bench import audit
from export_harness_bench_data import export_runs
from evals.harness_bench.drive_round import _manifest, _round_number
from run_harness_bench import (
    BENCHMARK_TOOLS,
    CONTAINER_WORK_ROOT,
    DEFAULT_BENCHMARK,
    DRIVER_COLLECT_MARGIN_S,
    ProviderUnavailableError,
    SELECTIONS,
    assert_ground_truth_reachable,
    detect_provider_failure,
    fetch_benchmark,
    find_official_result,
    normalize_oracle_ground_truth,
    provider_preflight,
    snapshot_problem,
    stage_benchmark,
    summarize,
    task_score,
    validate_benchmark,
)


def test_benchmark_profile_only_removes_interactive_tools() -> None:
    assert "ask_question" not in BENCHMARK_TOOLS
    assert "show_files" not in BENCHMARK_TOOLS
    assert {"run_command", "run_script", "web_search", "spawn_subagent"} <= BENCHMARK_TOOLS
from view_harness_bench import HTML, load_state


def test_analysis_export_builds_joinable_sqlite_dataset(tmp_path: Path) -> None:
    import sqlite3

    run = tmp_path / "run-1"
    task = run / "tasks" / "task-1"
    official = task / "official.json"
    official.parent.mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({
        "tasks": ["task-1"], "model": "minimax/MiniMax-M3", "mode": "yolo",
        "task_metadata": {"task-1": {"title": "T", "difficulty": "easy", "class": "C"}},
    }), encoding="utf-8")
    (run / "summary.json").write_text(json.dumps({"tasks": []}), encoding="utf-8")
    (task / "status.json").write_text(json.dumps({
        "task_id": "task-1", "state": "complete", "model": "minimax/MiniMax-M3",
        "outcome_score": 1.0, "elapsed_sec": 2.0, "official_result": "official.json",
    }), encoding="utf-8")
    official.write_text(json.dumps({"oracle_result": {"checks": [
        {"id": "answer", "pass": True, "weight": 1.0, "detail": "ok"},
    ]}}), encoding="utf-8")
    (task / "events.jsonl").write_text("\n".join(json.dumps(row) for row in (
        {"at": 1.0, "source": "llm", "kind": "llm_call",
         "payload": {"model": "minimax/MiniMax-M3", "ok": True, "duration_s": 1.0,
                     "prompt_tokens": 100, "cached_prompt_tokens": 40,
                     "completion_tokens": 10}},
        {"at": 2.0, "source": "llm", "kind": "llm_call",
         "payload": {"model": "minimax/MiniMax-M3", "ok": True, "duration_s": 1.0,
                     "prompt_tokens": 200, "cached_prompt_tokens": 80,
                     "completion_tokens": 20}},
    )) + "\n", encoding="utf-8")

    output = tmp_path / "dataset"
    manifest = export_runs([run], output)
    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        row = connection.execute(
            "SELECT outcome_score, input_tokens_billed, input_tokens_largest_call,"
            " cached_input_tokens, input_tokens_uncached, output_tokens,"
            " tokens_complete, cost_input_usd, cost_output_usd, cost_total_usd"
            " FROM trials").fetchone()
        # Billed input is the sum of both whole prompts (300); the largest
        # single call (200) is the separate question of how big it got.
        assert row[:6] == (1.0, 300, 200, 120, 180, 30)
        assert row[6] == 1                       # both counts on every call
        # MiniMax direct-API rates: $0.30/Mtok input, $0.06 cached, $1.20 out.
        # 120 of the 300 billed input tokens were cache reads, so the input
        # bill is split rather than charged twice:
        #     180 * 0.30/1e6  +  120 * 0.06/1e6  =  0.000061
        assert row[7] == 6.1e-05
        assert row[8] == 3.6e-05                 # 30 * 1.20/1e6
        assert row[9] == 9.7e-05                 # and they reconcile
        assert round(row[7] + row[8], 6) == row[9]
        assert connection.execute("SELECT count(*) FROM oracle_checks").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM model_calls").fetchone()[0] == 2
    finally:
        connection.close()
    assert manifest["rows_written"]["trials"] == 1


def test_pinned_release_has_all_tasks_and_pilot_covers_categories() -> None:
    metadata = validate_benchmark(DEFAULT_BENCHMARK)

    assert metadata["task_count"] == 106
    assert metadata["license_file_present"] is False
    categories = {metadata["tasks"][task_id]["class"] for task_id in SELECTIONS["pilot"]}
    difficulties = [metadata["tasks"][task_id]["difficulty"] for task_id in SELECTIONS["pilot"]]
    assert len(categories) == 8
    assert len(SELECTIONS["pilot"]) == 8
    assert difficulties.count("easy") == 5
    assert difficulties.count("medium") == 2
    assert difficulties.count("hard") == 1
    assert metadata["tasks"][SELECTIONS["smoke"][1]]["rounds"] == 2


def test_corpus_audit_proves_this_is_not_a_large_knowledgebase_benchmark() -> None:
    report = audit(DEFAULT_BENCHMARK)

    assert report["tasks"] == 106
    assert report["fixture_files"] == 508
    assert report["fixture_bytes"] == 388123
    assert report["largest_non_media_task"]["non_media_bytes"] < 13_000
    assert report["tasks_with_hooks"] == 28
    assert len(report["local_http_tasks"]) == 7


def test_fetch_is_idempotent_for_pinned_checkout() -> None:
    fetch_benchmark(DEFAULT_BENCHMARK)


def test_provider_preflight_normalizes_provider_exception(monkeypatch) -> None:
    import litellm

    monkeypatch.setattr(litellm, "completion", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("quota 2056")))

    try:
        provider_preflight("minimax/fake", {"SB_LLM_API_KEY": "fake", "SB_LLM_ENDPOINT": "https://example.invalid"})
    except ProviderUnavailableError as exc:
        assert str(exc) == "quota 2056"
    else:
        raise AssertionError("provider exception was not normalized")


def test_stage_uses_generic_cli_and_requested_mode(tmp_path: Path) -> None:
    stage = tmp_path / "stage"

    stage_benchmark(DEFAULT_BENCHMARK, stage, "033-offline-knowledge-qa", "lockdown", "minimax/MiniMax-M3")

    config = json.loads((stage / "config" / "harness.json").read_text(encoding="utf-8"))
    model = config["models"]["second-brain"]
    assert model["adapter"] == "generic_cli"
    assert model["model"] == "minimax/MiniMax-M3"
    assert model["args"][model["args"].index("--security-mode") + 1] == "lockdown"
    assert model["args"][-2] == "--wall-seconds"
    # The driver must return *and write its bundle* inside the official
    # timeout, because upstream's run-task path does not catch a subprocess
    # timeout: it propagates, no result is written, and the oracle never runs.
    assert float(model["args"][-1]) == 600.0 - DRIVER_COLLECT_MARGIN_S
    app = json.loads((stage / "config" / "app.json").read_text(encoding="utf-8"))
    assert app["work_root"] == CONTAINER_WORK_ROOT
    assert app["work_root"].startswith("/data/Second Brain/workspace/")
    assert (stage / "tasks" / "033-offline-knowledge-qa" / "oracle_grade.py").is_file()


def test_staged_oracle_resolves_ground_truth_beside_itself(tmp_path: Path) -> None:
    """012's oracle walked up from the workspace and never reached its own task
    directory, so every trial scored a fixed 0.75: a forced zero on the branch
    that divides by the expectation count, and free full marks on the two that
    divide by empty lists."""
    stage = tmp_path / "harnessbench"
    stage_benchmark(DEFAULT_BENCHMARK, stage, "012-doc-synthesis", "yolo", "openai/fake")
    task_dir = stage / "tasks" / "012-doc-synthesis"
    source = (task_dir / "oracle_grade.py").read_text(encoding="utf-8")

    assert "w.parent.parent" not in source
    assert "task_dir = Path(__file__).resolve().parent" in source
    assert (task_dir / "ground_truth.json").is_file()

    truth = json.loads((task_dir / "ground_truth.json").read_text(encoding="utf-8"))
    workspace = tmp_path / "sandbox" / "run" / "workspace"
    out = workspace / "out"
    out.mkdir(parents=True)
    # Exactly the expected trust scores, and contradictions whose claims match
    # nothing. Under a loaded ground truth that is accuracy 1.0 and coverage
    # 0.0; under the empty one it was the reverse -- a forced 0.0 and a free
    # 1.0 -- so this workspace tells the two apart.
    (out / "trustworthiness.json").write_text(json.dumps(
        {doc: {"score": score, "reason": "r"}
         for doc, score in truth["expected_trust_scores"].items()}), encoding="utf-8")
    (out / "contradictions.json").write_text(json.dumps(
        {"contradictions": [{"claim": "nothing the ground truth asks about",
                             "documents": [], "quotes": [], "resolution": ""}]}), encoding="utf-8")
    (out / "final_report.md").write_text("prose. " * 400, encoding="utf-8")

    spec = importlib.util.spec_from_file_location("oracle_012", task_dir / "oracle_grade.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checks = {check["id"]: check for check in module.score_workspace(workspace)["checks"]}

    assert checks["trust_assessment"]["detail"]["accuracy"] == 1.0
    assert checks["trust_assessment"]["pass"] is True
    assert checks["contradiction_detection"]["detail"]["coverage"] == 0.0


def test_stage_refuses_a_task_whose_ground_truth_cannot_be_read(tmp_path: Path) -> None:
    stage = tmp_path / "harnessbench"
    stage_benchmark(DEFAULT_BENCHMARK, stage, "033-offline-knowledge-qa", "yolo", "openai/fake")
    task_dir = stage / "tasks" / "033-offline-knowledge-qa"
    (task_dir / "ground_truth.json").unlink()

    with pytest.raises(RuntimeError, match="ground_truth.json"):
        assert_ground_truth_reachable(task_dir, "033-offline-knowledge-qa")


def test_ground_truth_rewrite_fails_loudly_if_upstream_moves_the_line(tmp_path: Path) -> None:
    """A scoring shim that silently no-ops is worse than no shim: the run looks
    graded and is not."""
    task_dir = tmp_path / "012-doc-synthesis"
    task_dir.mkdir()
    (task_dir / "oracle_grade.py").write_text("task_dir = somewhere_else\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="ORACLE_GROUND_TRUTH_REWRITES"):
        normalize_oracle_ground_truth(task_dir, "012-doc-synthesis")


def test_round_detection_and_lockdown_manifest(tmp_path: Path) -> None:
    assert _round_number(Path("prompt-round2.txt")) == 2
    assert _round_number(Path("prompt.txt")) == 1
    assert _manifest("lockdown", tmp_path) == {"default": "deny"}
    mediated = _manifest("mediated", tmp_path)
    assert mediated["script.run"] == "allow"
    assert str(tmp_path) in mediated["fs.write"]["under"]


def test_live_frames_are_flushed_for_viewer(tmp_path: Path, monkeypatch) -> None:
    live = tmp_path / "events.jsonl"
    monkeypatch.setenv("SB_LIVE_EVENT_LOG", str(live))
    frames = Frames()

    frames.append({"kind": "stream_delta", "payload": {"delta": "hello"}})
    frames.close()

    event = json.loads(live.read_text(encoding="utf-8"))
    assert event["source"] == "second_brain"
    assert event["frame"]["payload"]["delta"] == "hello"


def test_summary_keeps_scheduled_failures_in_denominator(tmp_path: Path) -> None:
    run = tmp_path / "run"
    first = run / "tasks" / "a" / "status.json"
    first.parent.mkdir(parents=True)
    first.write_text(json.dumps({"task_id": "a", "state": "complete", "outcome_score": 1.0}), encoding="utf-8")
    (first.parent / "events.jsonl").write_text(
        json.dumps({"source": "llm", "kind": "llm_call", "payload": {"prompt_tokens": 123}}) + "\n",
        encoding="utf-8",
    )

    summary = summarize(run, ["a", "b"])

    assert summary["completion_score"] == 0.5
    assert summary["score_denominator"] == 2
    assert summary["completed"] == 1
    assert summary["llm_usage"]["calls"] == 1
    assert summary["llm_usage"]["input_tokens_billed"] == 123
    # The call reported no completion count, so the total is absent rather
    # than zero and ``output_complete`` says why.
    assert summary["llm_usage"]["output_tokens"] is None
    assert summary["llm_usage"]["output_complete"] is False
    assert summary["llm_usage"]["input_complete"] is True


def test_viewer_loads_running_task_and_live_events(tmp_path: Path) -> None:
    run = tmp_path / "run"
    (run / "tasks" / "a").mkdir(parents=True)
    (run / "run.json").write_text(
        json.dumps({"run_id": "test", "mode": "yolo", "model": "fake", "tasks": ["a", "b"],
                    "task_metadata": {"a": {"title": "Task A", "difficulty": "easy"}}}),
        encoding="utf-8",
    )
    (run / "tasks" / "a" / "status.json").write_text(
        json.dumps({"task_id": "a", "state": "running"}), encoding="utf-8"
    )
    (run / "tasks" / "a" / "events.jsonl").write_text(
        json.dumps({"source": "second_brain", "frame": {"kind": "stream_delta", "payload": {"delta": "hello"}}}) + "\n",
        encoding="utf-8",
    )
    (run / "tasks" / "a" / "problem.json").write_text(
        json.dumps({"rounds": [{"round": 1, "file": "prompt.txt",
                                "text": "Do the thing."}]}),
        encoding="utf-8",
    )

    state = load_state(run)

    assert state["selected"] == "a"
    assert state["tasks"][0]["title"] == "Task A"
    assert state["tasks"][1]["state"] == "pending"
    assert state["events"][0]["frame"]["payload"]["delta"] == "hello"
    assert state["problem"]["rounds"][0]["text"] == "Do the thing."
    assert 'id="timeline"' in HTML
    assert 'id="decisions"' not in HTML and 'id="events"' not in HTML
    assert "Â" not in HTML and "â€" not in HTML


def test_problem_snapshot_preserves_all_official_rounds() -> None:
    problem = snapshot_problem(DEFAULT_BENCHMARK, "007-session-memory")
    assert len(problem["rounds"]) == 2
    assert "passphrase" in problem["rounds"][0]["text"].lower()


def test_paired_comparison_requires_identical_tasks_and_counts_failures_as_zero(tmp_path: Path) -> None:
    def make_run(name: str, tasks: list[str], scores: dict[str, float]) -> Path:
        root = tmp_path / name
        root.mkdir()
        (root / "run.json").write_text(
            json.dumps({"run_id": name, "benchmark_commit": "abc", "tasks": tasks, "model": "m", "mode": name}),
            encoding="utf-8",
        )
        (root / "summary.json").write_text(json.dumps({"completion_score": 0}), encoding="utf-8")
        for task_id, score in scores.items():
            folder = root / "tasks" / task_id
            folder.mkdir(parents=True)
            (folder / "status.json").write_text(
                json.dumps({"state": "complete", "outcome_score": score}), encoding="utf-8"
            )
        return root

    baseline = make_run("ask", ["a", "b"], {"a": 1, "b": 1})
    candidate = make_run("lockdown", ["a", "b"], {"a": 1})

    report = compare_runs(baseline, candidate)

    assert report["candidate_delta"] == -0.5
    assert (report["wins"], report["ties"], report["losses"]) == (0, 1, 1)


def test_an_unscored_completion_is_a_zero_not_a_smaller_denominator(tmp_path: Path) -> None:
    """The bug this pins was invisible in exactly the way that matters.

    A task that ran to completion but whose oracle produced no number used to
    be dropped from the score list entirely, so the mean divided by a smaller
    denominator while ``score_denominator`` still reported the full one. The
    benchmark's headline claim is that failures stay in the denominator; this
    was the one case where they did not.
    """
    run = tmp_path / "run"
    for task_id, status in (
        ("a", {"state": "complete", "outcome_score": 1.0}),
        ("b", {"state": "complete", "outcome_score": None}),
        ("c", {"state": "complete"}),
        ("d", {"state": "harness_error"}),
    ):
        folder = run / "tasks" / task_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(json.dumps(status), encoding="utf-8")

    summary = summarize(run, ["a", "b", "c", "d"])

    assert summary["completion_score"] == 0.25
    assert summary["score_denominator"] == 4
    assert summary["completed_without_oracle_score"] == 2


def test_one_scorer_serves_the_launcher_and_the_comparison() -> None:
    """Two definitions of "what is an unfinished task worth" is one too many.

    A comparison whose per-task deltas do not reconcile with the aggregate
    printed beside them is worse than no comparison, because both numbers
    look reasonable alone.
    """
    from compare_harness_runs import _task_score as compare_scorer

    assert compare_scorer.__module__ == "compare_harness_runs"
    assert task_score({"state": "complete", "outcome_score": 0.5}) == 0.5
    assert task_score({"state": "complete", "outcome_score": None}) == 0.0
    assert task_score({"state": "harness_error", "outcome_score": 1.0}) == 0.0
    assert task_score(None) == 0.0


def test_a_retried_rate_limit_does_not_discard_a_finished_task(tmp_path: Path) -> None:
    """A provider complaint in the log is not a provider failure.

    LiteLLM logs ``rate_limit_error`` on its way to a *successful* retry, so
    checking the log before checking for a result threw away real oracle
    scores and stopped the run -- on rate-limited providers, which is to say
    on the runs where it happens.
    """
    task_dir = tmp_path / "tasks" / "x"
    (task_dir / "official-results").mkdir(parents=True)
    (task_dir / "harness.log").write_text(
        "litellm.RateLimitError: rate_limit_error, retrying in 2s\nok\n", encoding="utf-8")

    assert detect_provider_failure(task_dir) is not None      # still detected
    result = task_dir / "official-results" / "x.json"
    result.write_text(json.dumps({"oracle_result": {"outcome_score": 1.0}}), encoding="utf-8")
    assert find_official_result(task_dir, "x") == result       # and outranked


def test_a_second_attempt_replaces_the_first_bundle_rather_than_nesting(tmp_path: Path) -> None:
    """``docker cp`` nests into an existing destination, and that broke retries.

    The first attempt creates ``official-results/``; without removing it the
    second copy lands at ``official-results/results/...`` and the tree holds
    two results for one task. ``find_official_result`` then refuses to guess
    and a passing retry is recorded as a harness error.
    """
    task_dir = tmp_path / "tasks" / "x"
    first = task_dir / "official-results" / "second-brain" / "unknown-api"
    first.mkdir(parents=True)
    (first / "x.json").write_text("{}", encoding="utf-8")

    nested = task_dir / "official-results" / "results" / "second-brain" / "unknown-api"
    nested.mkdir(parents=True)
    (nested / "x.json").write_text("{}", encoding="utf-8")
    assert find_official_result(task_dir, "x") is None          # the failure

    shutil.rmtree(task_dir / "official-results", ignore_errors=True)
    fresh = task_dir / "official-results" / "second-brain" / "unknown-api"
    fresh.mkdir(parents=True)
    (fresh / "x.json").write_text("{}", encoding="utf-8")
    assert find_official_result(task_dir, "x") == fresh / "x.json"


def test_approval_decisions_reach_the_live_log(tmp_path: Path, monkeypatch) -> None:
    """Which requests a mode refused, and why, is the content of a mode study.

    ``decisions`` is only readable once the bundle is written, which is after
    the task is over. A lockdown run is worth watching while it happens.
    """
    monkeypatch.setenv("SB_LIVE_EVENT_LOG", str(tmp_path / "events.jsonl"))
    live.reset()

    class _Client:
        def __init__(self) -> None:
            self.frames = Frames()

        def post(self, kind, args=None, timeout=None):
            return 200, {"ok": True}

    approver = Approver(_Client(), Manifest({"default": "deny"}), log=None)
    approver._handle({"id": "r1", "detail": {"type": "proc.run", "command": "rm -rf /tmp/x"},
                      "enum": ["allow", "deny"]})
    approver._answer_question({"id": "r2", "title": "Approve mode?", "type": "boolean"}, 0.0)
    live.shared().close()

    rows = [json.loads(line) for line
            in (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    decision = next(r for r in rows if r.get("kind") == "decision")
    assert decision["source"] == "approver"
    assert decision["payload"]["choice"] == "deny"
    assert decision["payload"]["type"] == "proc.run"
    assert "no rule for proc.run" in decision["payload"]["why"]
    question = next(r for r in rows if r.get("kind") == "question")
    assert question["payload"]["answer"] is True


def test_the_mode_grant_is_recorded_even_though_it_is_not_a_permission_gate() -> None:
    """``/mode yolo`` is gated, and its dialog carries no ``detail``.

    So it arrives as a *question* rather than a permission gate and is
    answered by the ``canned`` UI policy rather than by the manifest -- which
    is why a ``{"default": "deny"}`` manifest does not refuse the very mode
    the run is trying to measure. Pinned because a kernel change that gave
    that dialog a ``detail`` would silently fail every YOLO run: the manifest
    would deny it, the mode would stay ``ask``, and the run would quietly
    measure the wrong configuration.
    """
    approver = Approver.__new__(Approver)
    approver.ui = {"policy": "canned", "text": "Proceed."}

    assert approver._question_answer({"type": "boolean"}) is True
    assert Manifest({"default": "deny"}).decide({"type": "proc.run"})[0] == "deny"


def test_the_viewer_returns_only_new_events_and_survives_a_partial_line(tmp_path: Path) -> None:
    """Re-reading a multi-megabyte event log at 1 Hz is the viewer's whole cost.

    The partial-line case is not hypothetical: the file is being appended to
    while it is read, so the last line is routinely half-written.
    """
    run = tmp_path / "run"
    (run / "tasks" / "a").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "r", "tasks": ["a"]}), encoding="utf-8")
    (run / "tasks" / "a" / "status.json").write_text(
        json.dumps({"task_id": "a", "state": "running"}), encoding="utf-8")
    events = run / "tasks" / "a" / "events.jsonl"
    events.write_text(json.dumps({"at": 1, "frame": {"kind": "typing", "payload": True}}) + "\n",
                      encoding="utf-8")

    first = load_state(run, "a", 0)
    assert len(first["events"]) == 1 and first["cursor"] > 0
    assert load_state(run, "a", first["cursor"])["events"] == []

    with events.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": 2, "frame": {"kind": "typing", "payload": False}}) + "\n")
        handle.write('{"at": 3, "frame": {"kind": "str')      # writer mid-flush
    second = load_state(run, "a", first["cursor"])
    assert len(second["events"]) == 1                          # partial withheld
    with events.open("a", encoding="utf-8") as handle:
        handle.write('eam_delta"}}\n')
    assert len(load_state(run, "a", second["cursor"])["events"]) == 1   # then whole


def test_the_viewer_follows_the_running_task_until_a_task_is_clicked(tmp_path: Path) -> None:
    """It used to latch onto whatever ran when the page opened and stay there."""
    run = tmp_path / "run"
    for task_id, state in (("a", "complete"), ("b", "running")):
        folder = run / "tasks" / task_id
        folder.mkdir(parents=True)
        (folder / "status.json").write_text(
            json.dumps({"task_id": task_id, "state": state, "outcome_score": 1.0}), encoding="utf-8")
    (run / "run.json").write_text(json.dumps({"run_id": "r", "tasks": ["a", "b"]}), encoding="utf-8")

    assert load_state(run, "a", 0, follow=True)["selected"] == "b"
    assert load_state(run, "a", 0, follow=False)["selected"] == "a"
    assert load_state(run, "a", 99, follow=True)["cursor"] == 0      # reset on switch


def test_a_dead_task_explains_itself_in_the_viewer(tmp_path: Path) -> None:
    """A frozen task with a ticking clock reads as healthy. The log says otherwise."""
    run = tmp_path / "run"
    (run / "tasks" / "a").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps({"run_id": "r", "tasks": ["a"]}), encoding="utf-8")
    (run / "tasks" / "a" / "harness.log").write_text("container exited 137\n", encoding="utf-8")

    assert "container exited 137" in load_state(run, "a", 0)["logs"]["harness.log"]


def test_several_writers_share_the_live_log_without_shredding_a_line(tmp_path: Path, monkeypatch) -> None:
    """O_APPEND is atomic only below PIPE_BUF, and payloads exceed it.

    An interleaved line fails ``json.loads`` and is skipped by every reader,
    so the event disappears with no error anywhere.
    """
    monkeypatch.setenv("SB_LIVE_EVENT_LOG", str(tmp_path / "events.jsonl"))
    live.reset()
    log = live.shared()

    def spam(tag: str) -> None:
        for index in range(120):
            log.write(tag, "decision", payload={"blob": tag * 400, "index": index})

    threads = [threading.Thread(target=spam, args=(tag,)) for tag in ("approver", "llm")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    log.close()

    lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 240
    assert all(json.loads(line)["kind"] == "decision" for line in lines)


# -- writing while somebody is watching -------------------------------

def test_an_atomic_write_survives_a_concurrent_reader(tmp_path: Path) -> None:
    """Windows cannot replace a file another process holds open.

    Python's open() does not request FILE_SHARE_DELETE, so os.replace fails
    with PermissionError while any reader has the target open -- and
    summary.json is read once per second by the viewer's load_state whenever
    the Live tab is showing. Watching a run was therefore enough to crash it.

    The retry is on the swap only, never on the content, so a reader still
    never observes a partial file.
    """
    import threading
    import time

    from run_harness_bench import write_json

    target = tmp_path / "summary.json"
    write_json(target, {"n": -1})

    stop = threading.Event()
    seen = []

    def poll():
        while not stop.is_set():
            try:
                seen.append(json.loads(target.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                seen.append(None)
            time.sleep(0.002)

    readers = [threading.Thread(target=poll, daemon=True) for _ in range(3)]
    for reader in readers:
        reader.start()
    try:
        for index in range(40):
            write_json(target, {"n": index})
    finally:
        stop.set()
        for reader in readers:
            reader.join(timeout=2)

    assert json.loads(target.read_text(encoding="utf-8")) == {"n": 39}
    # Every observation was a whole document: the swap is still atomic.
    assert all(row is None or "n" in row for row in seen)


def test_an_unwritable_summary_does_not_end_a_paid_run(tmp_path: Path, monkeypatch) -> None:
    """The asymmetry that matters.

    summary.json is derived -- every number in it is recomputed from the
    per-task status.json files. Losing the write costs a stale file until the
    next task finishes. Losing the run costs whatever the provider was
    already paid, and that is what used to happen.
    """
    import run_harness_bench as launcher

    run = tmp_path / "run"
    task = run / "tasks" / "a"
    task.mkdir(parents=True)
    (task / "status.json").write_text(json.dumps(
        {"task_id": "a", "state": "complete", "outcome_score": 1.0}), encoding="utf-8")

    def refuse(path, payload, **kwargs):
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(launcher, "write_json", refuse)

    # Returns the summary it could not persist, rather than raising.
    summary = launcher.write_summary(run, ["a"])
    assert summary["completion_score"] == 1.0
    assert not (run / "summary.json").exists()
