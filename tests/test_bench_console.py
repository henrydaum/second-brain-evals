"""The console is a front door, not a second implementation.

Its job is to reach the same code the CLI reaches. These pin the places where
it could quietly stop doing that -- a page that silently loses its way back, a
query box that turns out to be writable, a second job started on top of a
running one.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import types
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bench_console  # noqa: E402
from bench_console import ConsoleHandler, JobRunner, active_run, live_page  # noqa: E402
from harness_bench_api import HarnessBenchAPI, JobSpec  # noqa: E402


@pytest.fixture()
def console(tmp_path):
    """A running console bound to an empty results root."""
    api = HarnessBenchAPI(results_root=tmp_path)
    api.dataset = lambda *args, **kwargs: {}          # never export in tests
    handler = type("Bound", (ConsoleHandler,), {"api": api, "runner": JobRunner(api)})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    class Client:
        base = f"http://127.0.0.1:{port}"
        api = None

        def get(self, path):
            with urllib.request.urlopen(self.base + path) as response:
                return response.status, response.read()

        def json(self, path):
            return json.loads(self.get(path)[1])

        def post(self, path, payload):
            request = urllib.request.Request(
                self.base + path, data=json.dumps(payload).encode(), method="POST")
            with urllib.request.urlopen(request) as response:
                return json.loads(response.read())

    client = Client()
    client.api = api
    client.runner = handler.runner
    try:
        yield client
    finally:
        server.shutdown()
        server.server_close()


def test_every_page_renders(console) -> None:
    for path in ("/", "/run", "/data"):
        status, body = console.get(path)
        assert status == 200
        assert b"<html" in body.lower()


def test_the_live_page_keeps_a_way_back_and_watches_the_job() -> None:
    """The viewer is reused by string-injection, so a markup change breaks it.

    Silently: the page would still render, still poll, and simply never offer
    a way back or notice the job finishing.
    """
    page = live_page("some-job").decode("utf-8")
    # The same three tabs as every other page. A viewer you can only leave by
    # editing the URL is what makes the console feel like two separate tools.
    assert 'href="/"' in page and 'href="/data"' in page and 'href="/run"' in page
    assert 'id="jobstate"' in page                 # job-level status line
    assert "watchJob" in page                      # redirect-when-done poller
    # And the viewer's own machinery survived the injection.
    assert "/api/state" in page and "stream_delta" in page


def test_the_query_box_refuses_to_write(console) -> None:
    """SQLite is opened read-only, so this guard is the courteous half.

    It exists to give a clear message instead of a driver error, and to stop a
    second statement smuggled in behind a SELECT.
    """
    assert "only SELECT" in console.post("/api/query", {"sql": "DROP TABLE trials"})["error"]
    assert "only SELECT" in console.post(
        "/api/query", {"sql": "UPDATE trials SET score = 1"})["error"]
    assert "one statement" in console.post(
        "/api/query", {"sql": "SELECT 1; DROP TABLE trials"})["error"]
    # A missing database is explained rather than raised.
    assert "no database yet" in console.post("/api/query", {"sql": "SELECT 1"})["error"]


def test_the_catalogue_offers_the_real_configuration_choices(console) -> None:
    catalog = console.json("/api/catalog")
    assert catalog["task_count"] == 106
    assert "minimax/MiniMax-M3" in catalog["models"]
    assert {"bench", "no-script"} <= {row["name"] for row in catalog["profiles"]}
    # Including the two difficulty values a hand-written form would forget.
    assert "medium-hard" in catalog["difficulties"]
    assert "unspecified" in catalog["difficulties"]


def test_preview_resolves_through_the_same_selector_as_a_real_job(console) -> None:
    """A preview that disagreed with the job would be worse than none."""
    preview = console.post("/api/resolve", {"difficulty": ["easy"]})["tasks"]
    job = console.api.plan(JobSpec(model="minimax/MiniMax-M3",
                                   tasks={"difficulty": ["easy"]}))
    assert preview == job.tasks


def test_a_bad_selector_is_explained_on_the_page(console) -> None:
    """Errors here are user input, not server faults; a 500 renders as nothing."""
    assert "unknown difficulty" in console.post(
        "/api/resolve", {"difficulty": ["eezy"]})["error"]
    assert "chooses nothing" in console.post(
        "/api/jobs", {"model": "minimax/MiniMax-M3", "tasks": {}})["error"]


def test_a_second_job_is_refused_while_one_is_running(console, monkeypatch) -> None:
    """Two jobs would contend for Docker and for provider quota.

    Refusing beats queueing: a queued job waits invisibly, and the whole point
    of this page is seeing what is happening now.
    """
    import subprocess

    real = subprocess.run
    started, release = threading.Event(), threading.Event()

    def dispatch(command, **kwargs):
        if not (isinstance(command, list)
                and any("run_harness_bench" in str(part) for part in command)):
            return real(command, **kwargs)
        run_id = command[command.index("--run-id") + 1]
        folder = console.api.results_root / run_id / "tasks"
        folder.mkdir(parents=True, exist_ok=True)
        (folder.parent / "run.json").write_text(
            json.dumps({"run_id": run_id, "tasks": ["001-file"]}), encoding="utf-8")
        started.set()
        release.wait(timeout=10)
        task = folder / "001-file"
        task.mkdir(parents=True, exist_ok=True)
        (task / "status.json").write_text(json.dumps(
            {"task_id": "001-file", "state": "complete", "outcome_score": 1.0}),
            encoding="utf-8")
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(subprocess, "run", dispatch)
    first = console.post("/api/jobs", {"model": "minimax/MiniMax-M3",
                                       "tasks": {"ids": ["001-file"]}})
    assert "job_id" in first
    assert started.wait(timeout=10)

    second = console.post("/api/jobs", {"model": "minimax/MiniMax-M3",
                                        "tasks": {"ids": ["001-file"]}})
    assert "still running" in second["error"]

    # The live view finds the replicate that is actually in flight.
    assert console.json("/api/state?job=" + first["job_id"])["run"]["run_id"] \
        == first["job_id"] + "-r1"

    release.set()
    for _ in range(100):
        if not console.runner.busy:
            break
        time.sleep(0.05)
    assert console.runner.busy is False
    assert console.json("/api/job")["finished_reason"] == "complete"


def test_the_active_replicate_is_the_newest_one_on_disk(tmp_path) -> None:
    """Replicates run in order, so the newest existing run is the live one."""
    api = HarnessBenchAPI(results_root=tmp_path)
    job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                           tasks={"ids": ["001-file"]}, repeats=3))
    assert active_run(api, job.job_id) is None            # nothing started yet

    for index in (1, 2):
        folder = tmp_path / f"{job.job_id}-r{index}"
        folder.mkdir(parents=True)
        (folder / "run.json").write_text("{}", encoding="utf-8")
    assert active_run(api, job.job_id).name == f"{job.job_id}-r2"
    assert active_run(api, "no-such-job") is None


def test_a_background_failure_reaches_the_page(tmp_path, monkeypatch) -> None:
    """A job that died in a thread otherwise looks like a slow one."""
    api = HarnessBenchAPI(results_root=tmp_path)
    api.dataset = lambda *args, **kwargs: {}
    runner = JobRunner(api)
    monkeypatch.setattr(api, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("docker is not running")))

    runner.start(JobSpec(model="minimax/MiniMax-M3", tasks={"ids": ["001-file"]}))
    for _ in range(100):
        if not runner.busy:
            break
        time.sleep(0.05)

    snapshot = runner.snapshot()
    assert snapshot["finished_reason"] == "error"
    assert "docker is not running" in snapshot["error"]


def test_the_console_javascript_is_syntactically_whole() -> None:
    """No Python test would catch a broken brace in a page script."""
    for script in (bench_console.DASHBOARD_SCRIPT, bench_console.DATA_SCRIPT):
        assert script.count("{") == script.count("}")
        assert script.count("(") == script.count(")")


def test_every_saved_query_is_valid_against_the_real_schema(tmp_path) -> None:
    """A broken analysis chip returns nothing, which reads as "no problem here".

    These queries are how the corpus gets interrogated, so a column renamed in
    the exporter has to break a test rather than quietly empty a panel.
    """
    import json
    import re
    import sqlite3

    from export_harness_bench_data import SCHEMA, VIEWS, connect

    # A database with the real schema and no rows: enough to prove the SQL is
    # answerable, without depending on anyone's run history.
    database = tmp_path / "empty.sqlite"
    connect(database).close()

    block = bench_console.DATA_SCRIPT
    block = block[block.index("const SAVED = {"):]
    block = block[:block.index("\n};") + 3]

    # Names are the stable part; pull each key and its string literal.
    names = re.findall(r"^  '([^']+)':", block, re.M)
    assert len(names) >= 15, "saved queries disappeared"
    assert {"cost split", "systematic vs stochastic", "repeated actions",
            "tool error rates", "integrity failures"} <= set(names)

    connection = sqlite3.connect(database)
    try:
        known = set(SCHEMA) | set(VIEWS)
        # Every table or view the queries name must exist in the schema.
        for referenced in re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)", block):
            if referenced not in {"trials", "tool_calls", "messages",
                                  "driver_rounds", "sqlite_master"} | known:
                raise AssertionError(f"saved query references unknown table {referenced!r}")
    finally:
        connection.close()
