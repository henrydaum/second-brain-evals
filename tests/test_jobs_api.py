"""What a job promises: the same configuration in, the same trials out.

These pin the properties that are invisible when they break. A job that
silently ran the wrong task set, resumed under a changed configuration, or
reported a cost built from a missing price all produce output that looks
entirely reasonable next to output that is correct.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from export_harness_bench_data import costs, export_runs, split_run_id  # noqa: E402
from harness_bench_api import HarnessBenchAPI, JobSpec  # noqa: E402
from run_harness_bench import DEFAULT_BENCHMARK, choose_tasks, validate_benchmark  # noqa: E402


def _selector(**kwargs) -> argparse.Namespace:
    base = dict(task=None, all=False, difficulty=None, task_class=None,
                exclude=None, pilot=False, smoke=False)
    base.update(kwargs)
    return argparse.Namespace(**base)


# -- task selection ---------------------------------------------------

def test_difficulty_is_not_a_three_bucket_scheme() -> None:
    """``medium-hard`` and ``unspecified`` are real values, and get forgotten.

    A filter written as "easy, medium, hard" silently covers 79 of 106 tasks
    while looking exhaustive. The suite is pinned, so these counts are facts
    about the benchmark rather than a snapshot -- if they ever change, the
    revision moved and the lock file should have stopped it.
    """
    tasks = validate_benchmark(DEFAULT_BENCHMARK)["tasks"]
    counts: dict[str, int] = {}
    for row in tasks.values():
        counts[row["difficulty"]] = counts.get(row["difficulty"], 0) + 1

    assert counts == {"hard": 42, "medium": 30, "unspecified": 24,
                      "easy": 7, "medium-hard": 3}
    assert sum(counts.values()) == 106
    named = choose_tasks(_selector(difficulty=["easy", "medium", "hard"]), tasks)
    assert len(named) == 79                       # the 27 it quietly misses


def test_a_mistyped_filter_raises_instead_of_running_nothing() -> None:
    """An empty selection and a finished run are indistinguishable afterwards."""
    tasks = validate_benchmark(DEFAULT_BENCHMARK)["tasks"]
    with pytest.raises(RuntimeError, match="unknown difficulty: eezy"):
        choose_tasks(_selector(difficulty=["eezy"]), tasks)
    with pytest.raises(RuntimeError, match="unknown class"):
        choose_tasks(_selector(task_class=["Nonexistent"]), tasks)


def test_selectors_compose_and_exclusion_applies_last() -> None:
    tasks = validate_benchmark(DEFAULT_BENCHMARK)["tasks"]
    easy = choose_tasks(_selector(difficulty=["easy"]), tasks)
    trimmed = choose_tasks(
        _selector(difficulty=["easy"], exclude=[easy[0]]), tasks)
    assert trimmed == easy[1:]
    # An explicit id survives alongside a filter, and is never duplicated.
    both = choose_tasks(_selector(task=[easy[0]], difficulty=["easy"]), tasks)
    assert both == easy


# -- planning ---------------------------------------------------------

def test_planning_resolves_trials_without_spending_anything(tmp_path: Path) -> None:
    api = HarnessBenchAPI(results_root=tmp_path)
    job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                           tasks={"difficulty": ["easy"]}, repeats=3))

    assert len(job.tasks) == 7
    assert job.trial_count == 21
    assert job.runs == [f"{job.job_id}-r1", f"{job.job_id}-r2", f"{job.job_id}-r3"]
    assert job.state == "planned"
    # A replicate is an ordinary run directory, which is what lets --resume,
    # the viewer and the exporter keep working on it unchanged.
    assert all(split_run_id(run_id) == (job.job_id, index)
               for index, run_id in enumerate(job.runs, 1))
    # Nothing was executed: no run directory exists yet.
    assert not (tmp_path / job.runs[0]).exists()


def test_a_selector_that_chooses_nothing_is_refused(tmp_path: Path) -> None:
    """Falling back to the smoke set would make a two-task run look like 106."""
    api = HarnessBenchAPI(results_root=tmp_path)
    with pytest.raises(ValueError, match="chooses nothing"):
        api.plan(JobSpec(model="minimax/MiniMax-M3", tasks={}))
    with pytest.raises(ValueError, match="unknown model"):
        api.plan(JobSpec(model="gpt-imaginary", tasks={"pilot": True}))
    with pytest.raises(ValueError, match="unknown profile"):
        api.plan(JobSpec(model="minimax/MiniMax-M3", tasks={"pilot": True},
                         profile="does-not-exist"))


def test_the_job_and_the_launcher_resolve_the_same_task_list(tmp_path: Path) -> None:
    """One resolver, deliberately.

    A job whose planned tasks differ from the tasks the launcher then runs
    would report a trial count that never matches the trials on disk, and
    nothing anywhere would flag the disagreement.
    """
    api = HarnessBenchAPI(results_root=tmp_path)
    selector = {"difficulty": ["medium-hard"], "exclude": ["082-compose-config-repair"]}
    job = api.plan(JobSpec(model="minimax/MiniMax-M3", tasks=selector))
    direct = choose_tasks(
        _selector(difficulty=["medium-hard"], exclude=["082-compose-config-repair"]),
        validate_benchmark(DEFAULT_BENCHMARK)["tasks"])
    assert job.tasks == direct


# -- execution and resumption -----------------------------------------

def _fake_run(tmp_path: Path, run_id: str, tasks: list[str], state: str) -> None:
    run_dir = tmp_path / run_id
    (run_dir / "tasks").mkdir(parents=True, exist_ok=True)
    (run_dir / "run.json").write_text(json.dumps(
        {"run_id": run_id, "tasks": tasks, "model": "minimax/MiniMax-M3",
         "mode": "yolo", "profile": "bench"}), encoding="utf-8")
    for task_id in tasks:
        folder = run_dir / "tasks" / task_id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "status.json").write_text(json.dumps(
            {"task_id": task_id, "state": state, "outcome_score": 1.0}),
            encoding="utf-8")


def test_a_finished_replicate_is_never_paid_for_twice(tmp_path: Path, monkeypatch) -> None:
    """Resuming after a usage limit must not re-run what already completed."""
    api = HarnessBenchAPI(results_root=tmp_path)
    job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                           tasks={"ids": ["001-file"]}, repeats=2))
    _fake_run(tmp_path, job.runs[0], job.tasks, "complete")

    commands = []

    def fake(command, **kwargs):
        commands.append(command)
        _fake_run(tmp_path, job.runs[1], job.tasks, "complete")
        return argparse.Namespace(returncode=0)

    monkeypatch.setattr("harness_bench_api.subprocess.run", fake)
    result = api.run(job.job_id)

    assert len(commands) == 1                       # only the unfinished one
    assert "--run-id" in commands[0]
    assert job.runs[1] in commands[0]
    assert result.state == "complete"


def test_a_usage_limit_pauses_the_job_rather_than_failing_it(tmp_path: Path, monkeypatch) -> None:
    """Exit 75 is the expected way a long benchmark proceeds, not an error.

    The job has to stay resumable and has to remember *why* it stopped, or the
    next session cannot tell a quota pause from a half-finished experiment.
    """
    api = HarnessBenchAPI(results_root=tmp_path)
    job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                           tasks={"ids": ["001-file"]}, repeats=3))

    calls = []

    def fake(command, **kwargs):
        calls.append(command)
        return argparse.Namespace(returncode=75)

    monkeypatch.setattr("harness_bench_api.subprocess.run", fake)
    result = api.run(job.job_id)

    assert result.state == "paused"
    assert result.detail["paused_reason"] == "provider_unavailable"
    assert len(calls) == 1                          # stopped, did not push on
    assert api.status(job.job_id)["remaining"] == 3


def test_an_existing_run_directory_is_resumed_not_recreated(tmp_path: Path, monkeypatch) -> None:
    """``--resume`` carries the launcher's conflict guard, which is the point.

    Recreating would either collide or, worse, start a second run under a
    changed configuration and average the two together.
    """
    api = HarnessBenchAPI(results_root=tmp_path)
    job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                           tasks={"ids": ["001-file"]}, repeats=1))
    _fake_run(tmp_path, job.runs[0], job.tasks, "harness_error")

    commands = []
    monkeypatch.setattr("harness_bench_api.subprocess.run",
                        lambda command, **kwargs: (commands.append(command),
                                                   argparse.Namespace(returncode=0))[1])
    api.run(job.job_id)

    assert "--resume" in commands[0]
    assert "--run-id" not in commands[0]


def test_a_dry_run_schedules_without_authorizing_model_calls(tmp_path: Path, monkeypatch) -> None:
    api = HarnessBenchAPI(results_root=tmp_path)
    job = api.plan(JobSpec(model="minimax/MiniMax-M3", tasks={"ids": ["001-file"]}))
    commands = []
    monkeypatch.setattr("harness_bench_api.subprocess.run",
                        lambda command, **kwargs: (commands.append(command),
                                                   argparse.Namespace(returncode=0))[1])
    api.run(job.job_id, execute=False)
    assert "--execute" not in commands[0]


# -- cost -------------------------------------------------------------

def test_a_missing_price_yields_no_cost_rather_than_a_free_run() -> None:
    """Zero is a claim. ``None`` is the truth when a price is unpublished."""
    prices = {"m": {"pricing": {"input_usd_per_mtok": 0.30,
                                "output_usd_per_mtok": None,
                                "cached_input_usd_per_mtok": None}}}
    result = costs("m", billed=1_000_000, cached=None, output=500_000, prices=prices)
    assert result["cost_input_usd"] == 0.30
    assert result["cost_output_usd"] is None
    # A partial total reads as a total, so it is withheld entirely.
    assert result["cost_total_usd"] is None
    # An unknown model is not a free model.
    assert costs("unknown", 1000, None, 1000, prices)["cost_input_usd"] is None


def test_cached_tokens_split_the_input_bill_instead_of_adding_to_it() -> None:
    """Cached tokens are a discounted *share* of the prompt, not an extra charge.

    Adding them would double-count the cached portion -- and would do so
    invisibly, since the total would still look like a plausible cost.
    """
    prices = {"m": {"pricing": {"input_usd_per_mtok": 1.00,
                                "cached_input_usd_per_mtok": 0.10,
                                "output_usd_per_mtok": 2.00}}}
    result = costs("m", billed=1_000_000, cached=900_000, output=0, prices=prices)
    # 100k at full rate + 900k at the cached rate = 0.10 + 0.09
    assert result["cost_input_usd"] == 0.19
    assert result["cost_output_usd"] == 0.0
    assert result["cost_total_usd"] == 0.19

    # With no published cached rate the whole prompt bills at full price: an
    # upper bound, never an invented discount.
    plain = {"m": {"pricing": {"input_usd_per_mtok": 1.00,
                               "cached_input_usd_per_mtok": None,
                               "output_usd_per_mtok": 2.00}}}
    assert costs("m", 1_000_000, 900_000, 0, plain)["cost_input_usd"] == 1.0


# -- the database -----------------------------------------------------

def _trial_run(root: Path, run_id: str, task_id: str, score: float,
               calls: list[dict]) -> Path:
    run = root / run_id
    task = run / "tasks" / task_id
    task.mkdir(parents=True, exist_ok=True)
    (run / "run.json").write_text(json.dumps({
        "tasks": [task_id], "model": "minimax/MiniMax-M3", "mode": "yolo",
        "profile": "bench", "template": {"kernel_commit": "abc123"},
        "task_metadata": {task_id: {"title": "T", "difficulty": "easy", "class": "C"}},
    }), encoding="utf-8")
    (task / "status.json").write_text(json.dumps({
        "task_id": task_id, "state": "complete", "outcome_score": score,
        "model": "minimax/MiniMax-M3", "mode": "yolo", "profile": "bench",
    }), encoding="utf-8")
    (task / "events.jsonl").write_text("\n".join(
        json.dumps({"at": 1.0, "source": "llm", "kind": "llm_call", "payload": call})
        for call in calls) + "\n", encoding="utf-8")
    return run


def test_re_exporting_replaces_a_run_instead_of_duplicating_it(tmp_path: Path) -> None:
    """The database is derived, so a rebuild must be safe to repeat.

    Without delete-then-insert a corpus re-exported across sessions doubles
    silently, and every mean stays plausible while resting on duplicate rows.
    """
    run = _trial_run(tmp_path / "runs", "job-r1", "001-file", 1.0,
                     [{"prompt_tokens": 100, "completion_tokens": 10, "ok": True}])
    output = tmp_path / "db"

    first = export_runs([run], output)
    second = export_runs([run], output, rebuild=True)

    assert first["table_counts"]["trials"] == 1
    assert second["table_counts"]["trials"] == 1
    assert second["table_counts"]["model_calls"] == 1


def test_an_unchanged_run_is_skipped_but_still_counted(tmp_path: Path) -> None:
    run = _trial_run(tmp_path / "runs", "job-r1", "001-file", 1.0,
                     [{"prompt_tokens": 100, "ok": True}])
    output = tmp_path / "db"
    export_runs([run], output)
    again = export_runs([run], output)

    assert again["runs_refreshed"] == []
    assert again["runs_skipped_unchanged"] == ["job-r1"]
    assert again["table_counts"]["trials"] == 1     # still there, just not rewritten


def test_the_headline_score_averages_per_task_before_across_tasks(tmp_path: Path) -> None:
    """Otherwise the score drifts with the scheduling history.

    Task A is run three times and task B once. A flat mean over the four
    trials weights A at three quarters, so re-running one task -- something
    done merely because a session had budget left -- would move the published
    number. Averaging per task first makes the score depend on the harness
    instead.
    """
    root = tmp_path / "runs"
    runs = [
        _trial_run(root, "job-r1", "001-file", 0.0, [{"prompt_tokens": 10, "ok": True}]),
        _trial_run(root, "job-r2", "001-file", 0.0, [{"prompt_tokens": 10, "ok": True}]),
        _trial_run(root, "job-r3", "001-file", 0.0, [{"prompt_tokens": 10, "ok": True}]),
        _trial_run(root, "other-r1", "007-session-memory", 1.0,
                   [{"prompt_tokens": 10, "ok": True}]),
    ]
    output = tmp_path / "db"
    export_runs(runs, output)

    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        tasks, trials, score = connection.execute(
            "SELECT tasks, trials, completion_score FROM config_scores").fetchone()
    finally:
        connection.close()

    assert (tasks, trials) == (2, 4)
    assert score == 0.5          # not 0.25, which a flat mean over trials gives


def test_a_multi_round_transcript_is_indexed_per_round(tmp_path: Path) -> None:
    """A long-running-autonomy task drives the conversation more than once.

    Taking only the first bundle would drop most of what such a task did, and
    the trial would look short rather than look wrong.
    """
    run = _trial_run(tmp_path / "runs", "job-r1", "001-file", 1.0,
                     [{"prompt_tokens": 10, "ok": True}])
    sandbox = run / "tasks" / "001-file" / "sandboxes" / "second-brain" / "m" / "s"
    for round_number, said in ((1, "first"), (2, "second")):
        folder = sandbox / f"round-0{round_number}"
        folder.mkdir(parents=True)
        (folder / "result.json").write_text(json.dumps({
            "outcome": {"ok": True, "reason": "typing_false"}, "wall_s": 1.0,
            "security_mode": "yolo", "session": {"mode": "yolo"},
            "metrics": {"tool_calls": 1, "approvals": 0},
        }), encoding="utf-8")
        (folder / "transcript.json").write_text(json.dumps({"messages": [
            {"role": "user", "content": said},
            # The kernel stores an assistant turn as a JSON blob carrying both
            # the text and the calls; the exporter has to split them.
            {"role": "assistant", "content": json.dumps(
                {"content": "thinking", "tool_calls": [{"id": "c1", "name": "grep"}]})},
        ]}), encoding="utf-8")

    output = tmp_path / "db"
    export_runs([run], output)
    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        rounds = connection.execute(
            "SELECT DISTINCT round FROM messages ORDER BY round").fetchall()
        assistant = connection.execute(
            "SELECT content, tool_calls_json FROM messages"
            " WHERE role = 'assistant' ORDER BY round").fetchall()
        counts = connection.execute(
            "SELECT message_count, round_count FROM trials").fetchone()
    finally:
        connection.close()

    assert rounds == [(1,), (2,)]
    assert counts == (4, 2)
    assert assistant[0][0] == "thinking"          # text, not the raw blob
    assert json.loads(assistant[0][1])[0]["name"] == "grep"


# -- plugin profiles --------------------------------------------------

def test_the_reported_tool_list_follows_the_profile_instead_of_a_constant(
        tmp_path: Path, monkeypatch) -> None:
    """A hardcoded tool list is a lie the moment plugins become a variable.

    ``visible_tools`` used to be a module constant. That was harmless only
    while every run had the same plugin set -- which is exactly the assumption
    ``--profile`` exists to break. A run under ``no-script`` legitimately has
    fewer tools, and a constant would report the full set beside it while the
    score reflected the reduced one.
    """
    import run_harness_bench as launcher

    monkeypatch.setattr(launcher, "RESULTS", tmp_path)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "template").mkdir()
    (tmp_path / "template" / "template_manifest.json").write_text(json.dumps({
        "tool_names": ["edit_file", "read_file", "run_command", "run_script",
                       "validate", "web_search"],
        "kernel_commit": "abc", "store_commit": "def",
    }), encoding="utf-8")

    def options(profile: str) -> argparse.Namespace:
        return argparse.Namespace(
            resume=None, run_id=f"run-{profile}", mode="yolo", profile=profile,
            image="img")

    metadata = {"commit": "c", "repository": "r", "license_file_present": False,
                "tasks": {"001-file": {"title": "T", "class": "C",
                                       "difficulty": "easy", "timeout_sec": 60,
                                       "rounds": 1}}}
    _, full = launcher.open_run(options("bench"), ["001-file"], "m", metadata, "id")
    _, lean = launcher.open_run(options("no-script"), ["001-file"], "m", metadata, "id")

    assert "run_script" in full["visible_tools"]
    assert "validate" in full["visible_tools"]
    # The profile actually subtracts, and the record says so.
    assert "run_script" not in lean["visible_tools"]
    assert "validate" not in lean["visible_tools"]
    assert lean["profile"] == "no-script"
    assert set(full["visible_tools"]) - set(lean["visible_tools"]) == {
        "run_script", "validate"}


def test_a_template_without_tool_names_reports_nothing_rather_than_guessing(
        tmp_path: Path, monkeypatch) -> None:
    """An older template predates the recorded file list. Absent beats stale."""
    import run_harness_bench as launcher

    monkeypatch.setattr(launcher, "RESULTS", tmp_path)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    metadata = {"commit": "c", "repository": "r", "license_file_present": False,
                "tasks": {"001-file": {"title": "T", "class": "C",
                                       "difficulty": "easy", "timeout_sec": 60,
                                       "rounds": 1}}}
    _, run = launcher.open_run(
        argparse.Namespace(resume=None, run_id="r", mode="yolo",
                           profile="bench", image="img"),
        ["001-file"], "m", metadata, "id")

    assert run["visible_tools"] is None


def test_resuming_refuses_to_mix_plugin_profiles(tmp_path: Path, monkeypatch) -> None:
    """Two profiles averaged into one run would be a silent category error."""
    import run_harness_bench as launcher

    monkeypatch.setattr(launcher, "RESULTS", tmp_path)
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    run_dir = tmp_path / "existing"
    run_dir.mkdir()
    (run_dir / "run.json").write_text(json.dumps({
        "run_id": "existing", "mode": "yolo", "model": "m", "profile": "bench",
        "benchmark_commit": "c", "image_id": "id", "tasks": ["001-file"],
    }), encoding="utf-8")

    metadata = {"commit": "c", "repository": "r", "license_file_present": False,
                "tasks": {}}
    with pytest.raises(RuntimeError, match="refusing to mix configurations"):
        launcher.open_run(
            argparse.Namespace(resume=str(run_dir), run_id=None, mode="yolo",
                               profile="no-script", image="img"),
            ["001-file"], "m", metadata, "id")


def _stub_package_manager(monkeypatch, failures=()):
    """Stand in for the in-container package manager the entrypoint drives."""
    import types

    calls = {"install": [], "uninstall": []}

    def result(ok):
        return types.SimpleNamespace(ok=ok, lines=["boom"] if not ok else [])

    module = types.ModuleType("package_manager")
    module.install_package = lambda root, stem, **kw: (
        calls["install"].append(stem), result(stem not in failures))[1]
    module.uninstall_package = lambda stem, **kw: (
        calls["uninstall"].append(stem), result(stem not in failures))[1]

    helpers = types.ModuleType("bundled.commands.helpers")
    helpers.package_manager = module
    for name, value in (("bundled", types.ModuleType("bundled")),
                        ("bundled.commands", types.ModuleType("bundled.commands")),
                        ("bundled.commands.helpers", helpers),
                        ("bundled.commands.helpers.package_manager", module)):
        monkeypatch.setitem(sys.modules, name, value)
    return calls


def test_the_container_records_the_plugin_set_it_actually_ran(
        tmp_path: Path, monkeypatch) -> None:
    """The manifest is read off the filesystem, not echoed back from the request.

    A removal naming a stem the seed never had would otherwise be reported as
    applied. The point of the record is to catch exactly that: a trial whose
    profile column disagrees with the tools the agent actually had.
    """
    import entrypoint

    calls = _stub_package_manager(monkeypatch)
    installed = tmp_path / "installed" / "tools"
    installed.mkdir(parents=True)
    for name in ("tool_read_file.py", "tool_run_command.py", "__init__.py"):
        (installed / name).write_text("", encoding="utf-8")

    record_path = tmp_path / "profile.json"
    monkeypatch.setattr(entrypoint, "PROFILE_RECORD", record_path)
    monkeypatch.setattr(entrypoint, "Path", Path)
    monkeypatch.setenv("SB_PROFILE", "no-script")
    monkeypatch.setenv("SB_REMOVE_PACKAGES", "tool_run_script,tool_validate")
    monkeypatch.setenv("SB_ADD_PACKAGES", "")
    monkeypatch.setattr(entrypoint, "_installed_root", lambda: tmp_path / "installed")

    record = entrypoint.apply_profile()

    assert calls["uninstall"] == ["tool_run_script", "tool_validate"]
    assert record["profile"] == "no-script"
    # Derived from disk: dunder files are not tools, and the two removed stems
    # are simply absent rather than listed as "removed and therefore gone".
    assert record["tool_names"] == ["read_file", "run_command"]
    assert json.loads(record_path.read_text(encoding="utf-8"))["tool_names"] == [
        "read_file", "run_command"]


def test_a_failed_profile_change_kills_the_container(tmp_path: Path, monkeypatch) -> None:
    """Carrying on would produce a trial whose configuration column is a lie.

    A crash costs one task. A silent mismatch corrupts every comparison drawn
    from the run afterwards, and nothing would ever surface it.
    """
    import entrypoint

    _stub_package_manager(monkeypatch, failures={"tool_validate"})
    monkeypatch.setattr(entrypoint, "PROFILE_RECORD", tmp_path / "profile.json")
    monkeypatch.setattr(entrypoint, "_installed_root", lambda: tmp_path / "installed")
    monkeypatch.setenv("SB_ADD_PACKAGES", "")
    monkeypatch.setenv("SB_REMOVE_PACKAGES", "tool_validate")

    with pytest.raises(RuntimeError, match="profile removal failed"):
        entrypoint.apply_profile()


def test_a_deleted_run_stops_counting_toward_the_score(tmp_path: Path) -> None:
    """The database claims to be derived from disk, so it has to follow disk.

    A stale trial keeps contributing to every mean while the run it came from
    is gone -- and the row looks exactly like a live one.
    """
    root = tmp_path / "runs"
    keep = _trial_run(root, "keep-r1", "001-file", 1.0,
                      [{"prompt_tokens": 10, "ok": True}])
    drop = _trial_run(root, "drop-r1", "007-session-memory", 0.0,
                      [{"prompt_tokens": 10, "ok": True}])
    output = tmp_path / "db"
    export_runs([keep, drop], output, prune=True)

    import shutil
    shutil.rmtree(drop)
    export_runs([keep], output, prune=True)

    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        assert connection.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM model_calls").fetchone()[0] == 1
    finally:
        connection.close()


def test_a_single_run_export_never_prunes_the_rest(tmp_path: Path) -> None:
    """Pruning during a narrowed export would delete the whole corpus."""
    root = tmp_path / "runs"
    first = _trial_run(root, "a-r1", "001-file", 1.0, [{"prompt_tokens": 10, "ok": True}])
    second = _trial_run(root, "b-r1", "007-session-memory", 1.0,
                        [{"prompt_tokens": 10, "ok": True}])
    output = tmp_path / "db"
    export_runs([first, second], output, prune=True)
    export_runs([first], output, rebuild=True)          # prune defaults to False

    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        assert connection.execute("SELECT COUNT(*) FROM trials").fetchone()[0] == 2
    finally:
        connection.close()


# -- prices reaching the viewer ---------------------------------------

def test_the_viewer_is_served_prices_rather_than_carrying_its_own() -> None:
    """A rate duplicated in the page is a rate that drifts from the invoice.

    The viewer's number is the one somebody reads while deciding whether to
    let an expensive run continue, so it has to come from the same file the
    exporter bills by.
    """
    import view_harness_bench as viewer

    rates = viewer._pricing("minimax/MiniMax-M3")
    assert rates["input_usd_per_mtok"] == 0.30
    assert rates["cached_input_usd_per_mtok"] == 0.06
    assert rates["output_usd_per_mtok"] == 1.20
    # An unknown model gets nulls, which the page shows as "no price".
    unknown = viewer._pricing("not-a-model")
    assert unknown["input_usd_per_mtok"] is None
    # And no literal rate is baked into the page script.
    assert "inputUsdPerMillion" not in viewer.HTML
    assert "0.30" not in viewer.HTML


def test_the_viewer_and_the_exporter_agree_on_one_worked_example() -> None:
    """Two implementations of the same bill, in two languages.

    The page computes cost in JavaScript from streamed events; the exporter
    computes it in Python from the same events at rest. They can only be kept
    honest by checking them against one number that is worked out by hand.

    1,000,000 billed input of which 800,000 cached, 100,000 output:
        uncached  200,000 x $0.30/M = $0.060
        cached    800,000 x $0.06/M = $0.048
        output    100,000 x $1.20/M = $0.120
                                      -------
                                      $0.228
    """
    prices, _ = __import__("export_harness_bench_data").pricing_table()
    result = costs("minimax/MiniMax-M3", billed=1_000_000, cached=800_000,
                   output=100_000, prices=prices)
    assert result["cost_input_usd"] == 0.108
    assert result["cost_output_usd"] == 0.12
    assert result["cost_total_usd"] == 0.228


def test_load_state_carries_pricing_for_the_run_model(tmp_path: Path) -> None:
    import view_harness_bench as viewer

    run = tmp_path / "run"
    (run / "tasks" / "a").mkdir(parents=True)
    (run / "run.json").write_text(json.dumps(
        {"run_id": "r", "model": "minimax/MiniMax-M3", "tasks": ["a"]}), encoding="utf-8")
    (run / "tasks" / "a" / "status.json").write_text(
        json.dumps({"task_id": "a", "state": "running"}), encoding="utf-8")

    state = viewer.load_state(run, "a", 0)
    assert state["pricing"]["model"] == "minimax/MiniMax-M3"
    assert state["pricing"]["cached_input_usd_per_mtok"] == 0.06


# -- stale images -----------------------------------------------------

def _template(tmp_path: Path, monkeypatch, **manifest) -> None:
    import run_harness_bench as launcher
    monkeypatch.setattr(launcher, "ROOT", tmp_path)
    (tmp_path / "template").mkdir(exist_ok=True)
    (tmp_path / "template" / "template_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(launcher, "kernel_repo", lambda: tmp_path / "no-repo")


def test_a_profile_the_image_cannot_apply_is_fatal(tmp_path: Path, monkeypatch) -> None:
    """The one failure mode that produces confident, mislabelled data.

    An image built before runtime profiles ignores SB_REMOVE_PACKAGES
    silently. The run then records profile="no-script" while the agent had
    run_script the whole time -- and every downstream comparison inherits the
    wrong label with nothing anywhere reporting a problem.
    """
    from run_harness_bench import image_freshness

    _template(tmp_path, monkeypatch, kernel_commit="a" * 40, store_commit="b" * 40)
    verdict = image_freshness("no-script")
    assert verdict["fatal"] and "no-script" in verdict["fatal"][0]

    # The default profile asks for no delta, so the same image is fine for it.
    assert image_freshness("bench")["fatal"] == []


def test_a_moved_kernel_warns_but_does_not_block(tmp_path: Path, monkeypatch) -> None:
    """Missing telemetry is honest, just less useful; a wrong label is not.

    A run against an older kernel records that kernel's commit and reports the
    counts it actually had, so the data is correct -- merely thinner. That is
    a warning, not a refusal, because benchmarking an older build on purpose
    is a legitimate thing to do.
    """
    import run_harness_bench as launcher
    from run_harness_bench import image_freshness

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _template(tmp_path, monkeypatch, tool_names=["read_file"],
              kernel_commit="a" * 40, store_commit="b" * 40)
    monkeypatch.setattr(launcher, "kernel_repo", lambda: repo)
    monkeypatch.setattr(launcher, "git", lambda root, *args: "c" * 40)

    verdict = image_freshness("bench")
    assert verdict["fatal"] == []
    assert any("kernel moved" in line for line in verdict["warn"])
    assert any("store moved" in line for line in verdict["warn"])


def test_a_current_image_reports_nothing(tmp_path: Path, monkeypatch) -> None:
    import run_harness_bench as launcher
    from run_harness_bench import image_freshness

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    _template(tmp_path, monkeypatch, tool_names=["read_file", "run_script"],
              kernel_commit="a" * 40, store_commit="a" * 40)
    monkeypatch.setattr(launcher, "kernel_repo", lambda: repo)
    monkeypatch.setattr(launcher, "git", lambda root, *args: "a" * 40)

    verdict = image_freshness("no-script")
    assert verdict == {"fatal": [], "warn": []}


def test_a_missing_profile_record_is_not_agreement(tmp_path: Path) -> None:
    """Absence of evidence was being read as evidence of absence.

    The mismatch check only fired when live/profile.json existed and
    disagreed. An image that ignores profiles writes no record at all, so
    there was nothing to disagree with and a mislabelled run passed clean.
    """
    run = _trial_run(tmp_path / "runs", "job-r1", "001-file", 1.0,
                     [{"prompt_tokens": 10, "ok": True}])
    status = run / "tasks" / "001-file" / "status.json"
    payload = json.loads(status.read_text(encoding="utf-8"))
    payload["profile"] = "no-script"
    status.write_text(json.dumps(payload), encoding="utf-8")

    output = tmp_path / "db"
    export_runs([run], output)
    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        flags = connection.execute("SELECT validity_flags FROM trials").fetchone()[0]
    finally:
        connection.close()
    assert "profile_unverified" in flags

    # The default profile needs no record, so it must not be flagged.
    payload["profile"] = "bench"
    status.write_text(json.dumps(payload), encoding="utf-8")
    export_runs([run], output, rebuild=True)
    connection = sqlite3.connect(output / "harness_bench.sqlite")
    try:
        assert "profile_unverified" not in (
            connection.execute("SELECT validity_flags FROM trials").fetchone()[0] or "")
    finally:
        connection.close()
