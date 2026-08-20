import json
from pathlib import Path

from driver.wire import Frames
from compare_harness_runs import compare_runs
from audit_harness_bench import audit
from evals.harness_bench.drive_round import _manifest, _round_number
from run_harness_bench import (
    BENCHMARK_TOOLS,
    CONTAINER_WORK_ROOT,
    DEFAULT_BENCHMARK,
    ProviderUnavailableError,
    SELECTIONS,
    fetch_benchmark,
    provider_preflight,
    stage_benchmark,
    summarize,
    validate_benchmark,
)


def test_benchmark_profile_only_removes_interactive_tools() -> None:
    assert "ask_question" not in BENCHMARK_TOOLS
    assert "show_files" not in BENCHMARK_TOOLS
    assert {"run_command", "run_script", "web_search", "spawn_subagent"} <= BENCHMARK_TOOLS
from view_harness_bench import HTML, load_state


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
    assert float(model["args"][-1]) == 570.0
    app = json.loads((stage / "config" / "app.json").read_text(encoding="utf-8"))
    assert app["work_root"] == CONTAINER_WORK_ROOT
    assert app["work_root"].startswith("/data/Second Brain/workspace/")
    assert (stage / "tasks" / "033-offline-knowledge-qa" / "oracle_grade.py").is_file()


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
    assert summary["llm_usage"]["prompt_tokens_total_known"] == 123


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

    state = load_state(run)

    assert state["selected"] == "a"
    assert state["tasks"][0]["title"] == "Task A"
    assert state["tasks"][1]["state"] == "pending"
    assert state["events"][0]["frame"]["payload"]["delta"] == "hello"
    assert "Â" not in HTML and "â€" not in HTML


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
