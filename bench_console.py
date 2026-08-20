"""One local page for the whole benchmarking loop.

    python bench_console.py

Submit a job, watch it run, read the results — without leaving the browser.

Three pages, and nothing behind them that the CLI cannot also do:

* ``/`` submits a job and lists the ones already planned.
* ``/run`` is the existing live viewer, scoped to the job that is running and
  wired to return here when it finishes.
* ``/data`` queries the exported database read-only.

**One job at a time, deliberately.** Every task starts a Docker container and
spends provider quota, so a second concurrent job would contend for both and
make the timings meaningless. The runner refuses rather than queues: a queue
would invite firing off work that silently waits, and the point of this page
is to see what is happening now.

The server binds to 127.0.0.1. It runs benchmark jobs and reads a local
database, so it has no business being reachable from anywhere else.
"""

from __future__ import annotations

import argparse
import html
import json
import sqlite3
import threading
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import view_harness_bench
from export_harness_bench_data import DATABASE_NAME, RESULTS, read_json
from harness_bench_api import HarnessBenchAPI, JobSpec, MODES
from run_harness_bench import MODELS, PROFILES

ROOT = Path(__file__).resolve().parent


class JobRunner:
    """Runs one job on a background thread and reports what it is doing.

    Job *state* is never cached here -- it is read back from disk through
    :meth:`HarnessBenchAPI.status` on every request, the same as the CLI. This
    object only holds what disk cannot say: whether a thread is currently
    alive, and how it died if it did.
    """

    def __init__(self, api: HarnessBenchAPI) -> None:
        self.api = api
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.job_id: str | None = None
        self.error: str | None = None
        self.finished_reason: str | None = None

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, spec: JobSpec) -> str:
        with self._lock:
            if self.busy:
                raise RuntimeError(
                    f"job {self.job_id} is still running; wait for it or stop the server")
            job = self.api.plan(spec)
            self.job_id = job.job_id
            self.error = None
            self.finished_reason = None
            self._thread = threading.Thread(
                target=self._drive, args=(job.job_id,), daemon=True)
            self._thread.start()
            return job.job_id

    def resume(self, job_id: str) -> str:
        with self._lock:
            if self.busy:
                raise RuntimeError(f"job {self.job_id} is still running")
            self.job_id = job_id
            self.error = None
            self.finished_reason = None
            self._thread = threading.Thread(
                target=self._drive, args=(job_id,), daemon=True)
            self._thread.start()
            return job_id

    def _drive(self, job_id: str) -> None:
        try:
            job = self.api.run(job_id)
            self.finished_reason = job.detail.get("paused_reason") or job.state
        except Exception:                                   # noqa: BLE001
            # Surfaced on the page rather than only on the console: a job that
            # died in a background thread otherwise looks like one that is
            # simply taking a long time.
            self.error = traceback.format_exc()
            self.finished_reason = "error"
        try:
            # Refresh the dataset so /data is current the moment the run ends.
            self.api.dataset()
        except Exception:                                   # noqa: BLE001
            pass

    def snapshot(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id, "busy": self.busy,
            "error": self.error, "finished_reason": self.finished_reason,
        }
        if self.job_id:
            try:
                payload["status"] = self.api.status(self.job_id)
            except FileNotFoundError:
                payload["status"] = None
        return payload


def active_run(api: HarnessBenchAPI, job_id: str | None) -> Path | None:
    """The replicate of ``job_id`` worth watching: the newest one on disk.

    A job's replicates run in order, so the newest existing run directory is
    the one in flight -- and once the job ends it is the last one, which is
    also what somebody arriving late wants to see.
    """
    if not job_id:
        return None
    try:
        payload = api._read_job(job_id)
    except FileNotFoundError:
        return None
    existing = [api.results_root / run_id for run_id in payload.get("runs") or []]
    existing = [path for path in existing if (path / "run.json").exists()]
    return existing[-1] if existing else None


# -- pages ------------------------------------------------------------

STYLE = """
:root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--line:#30363d;--muted:#8b949e;
--blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e6edf3;
font:14px/1.5 ui-monospace,SFMono-Regular,Consolas,monospace}
header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;gap:18px;
align-items:center;flex-wrap:wrap}
h1{font:600 18px system-ui;margin:0}a{color:var(--blue)}
nav a{margin-right:14px;text-decoration:none}nav a.on{color:#fff;font-weight:600}
main{padding:20px 24px;max-width:1200px}
.card{background:var(--card);border:1px solid var(--line);border-radius:8px;
padding:16px;margin-bottom:16px}
.card h2{font:600 14px system-ui;margin:0 0 12px}
label{display:block;color:var(--muted);font-size:12px;margin:10px 0 4px}
select,input[type=number],input[type=text],textarea{width:100%;background:#0d1117;
color:#e6edf3;border:1px solid var(--line);border-radius:6px;padding:7px 9px;
font:13px ui-monospace,Consolas,monospace}
textarea{min-height:64px;resize:vertical}
button{background:var(--blue);color:#04101f;border:0;border-radius:6px;
padding:9px 18px;font:600 13px system-ui;cursor:pointer}
button:disabled{opacity:.5;cursor:not-allowed}
button.ghost{background:transparent;color:var(--blue);border:1px solid var(--line)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin-top:4px}
.chip{border:1px solid var(--line);border-radius:14px;padding:3px 11px;font-size:12px;
cursor:pointer;user-select:none}
.chip.on{border-color:var(--blue);background:#1c2430;color:#fff}
table{border-collapse:collapse;width:100%;font-size:12px}
th,td{border-bottom:1px solid var(--line);padding:6px 9px;text-align:left;
white-space:nowrap}
th{color:var(--muted);font-weight:600}
.scroll{overflow-x:auto}
.muted{color:var(--muted)}.green{color:var(--green)}.red{color:var(--red)}
.amber{color:var(--amber)}
.err{white-space:pre-wrap;color:var(--red);font-size:12px}
"""


def page(title: str, active: str, body: str, script: str = "") -> bytes:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>{STYLE}</style></head><body>
<header><h1>Second Brain &middot; Harness-Bench</h1>
<nav>
 <a href="/" class="{'on' if active == 'jobs' else ''}">Jobs</a>
 <a href="/run" class="{'on' if active == 'run' else ''}">Live</a>
 <a href="/data" class="{'on' if active == 'data' else ''}">Data</a>
</nav></header>
<main>{body}</main>
<script>{script}</script></body></html>""".encode("utf-8")


DASHBOARD_BODY = """
<div class="card">
 <h2>New job</h2>
 <div class="grid">
  <div><label>Model</label><select id="model"></select></div>
  <div><label>Plugin profile</label><select id="profile"></select></div>
  <div><label>Permission mode</label><select id="mode"></select></div>
  <div><label>Repeats</label><input type="number" id="repeats" value="1" min="1" max="20"></div>
 </div>
 <label>Tasks &mdash; pick difficulties, classes, or a preset</label>
 <div class="chips" id="difficulty"></div>
 <div class="chips" id="class"></div>
 <div class="chips" id="preset"></div>
 <label>Or exact task ids (comma or newline separated; overrides nothing, adds to the above)</label>
 <textarea id="ids" placeholder="032-customer-followup-draft, 067-canary-release-check"></textarea>
 <label>Exclude</label>
 <input type="text" id="exclude" placeholder="task ids to drop">
 <label>Notes (recorded with the job)</label>
 <input type="text" id="notes" placeholder="why this run exists">
 <p id="resolved" class="muted"></p>
 <button id="go">Plan and run</button>
 <button id="preview" class="ghost">Preview selection</button>
 <div id="formError" class="err"></div>
</div>

<div class="card">
 <h2>Jobs</h2>
 <div class="scroll"><table id="jobs"><thead><tr>
  <th>Job</th><th>State</th><th>Model</th><th>Profile</th><th>Mode</th>
  <th>Repeats</th><th>Trials</th><th>Done</th><th>Notes</th><th></th>
 </tr></thead><tbody></tbody></table></div>
</div>
"""

DASHBOARD_SCRIPT = r"""
let catalog = null, chosen = {difficulty:new Set(), class:new Set(), preset:new Set()};
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $ = id => document.getElementById(id);

function chips(box, values, group){
  $(box).innerHTML = values.map(v =>
    `<span class="chip" data-g="${group}" data-v="${esc(v)}">${esc(v)}</span>`).join('');
}
function wireChips(){
  document.querySelectorAll('.chip').forEach(el => el.onclick = () => {
    const g = el.dataset.g, v = el.dataset.v;
    chosen[g].has(v) ? chosen[g].delete(v) : chosen[g].add(v);
    el.classList.toggle('on');
    preview();
  });
}
function selector(){
  const ids = $('ids').value.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  const exclude = $('exclude').value.split(/[,\n]/).map(s => s.trim()).filter(Boolean);
  const s = {};
  if (ids.length) s.ids = ids;
  if (chosen.difficulty.size) s.difficulty = [...chosen.difficulty];
  if (chosen.class.size) s['class'] = [...chosen.class];
  for (const p of chosen.preset) s[p] = true;
  if (exclude.length) s.exclude = exclude;
  return s;
}
async function preview(){
  const s = selector();
  if (!Object.keys(s).filter(k => k !== 'exclude').length){
    $('resolved').textContent = 'Nothing selected yet.';
    return;
  }
  const r = await fetch('/api/resolve', {method:'POST', body: JSON.stringify(s)});
  const d = await r.json();
  const reps = Math.max(1, parseInt($('repeats').value) || 1);
  $('resolved').textContent = d.error
    ? d.error
    : `${d.tasks.length} tasks x ${reps} repeats = ${d.tasks.length * reps} trials`;
}
async function load(){
  catalog = await (await fetch('/api/catalog')).json();
  $('model').innerHTML = catalog.models.map(m => `<option>${esc(m)}</option>`).join('');
  $('profile').innerHTML = catalog.profiles.map(p =>
    `<option value="${esc(p.name)}">${esc(p.name)} — ${esc(p.description)}</option>`).join('');
  $('mode').innerHTML = catalog.modes.map(m => `<option>${esc(m)}</option>`).join('');
  chips('difficulty', catalog.difficulties, 'difficulty');
  chips('class', catalog.classes, 'class');
  chips('preset', ['all','pilot','smoke'], 'preset');
  wireChips();
  ['ids','exclude','repeats'].forEach(id => $(id).oninput = preview);
  refresh();
}
async function refresh(){
  const jobs = await (await fetch('/api/jobs')).json();
  $('jobs').tBodies[0].innerHTML = jobs.rows.map(j => {
    const cls = j.state === 'complete' ? 'green'
              : j.state === 'paused' ? 'amber'
              : j.state === 'running' ? 'blue' : 'muted';
    const action = j.state === 'running'
      ? `<a href="/run?job=${encodeURIComponent(j.job_id)}">watch</a>`
      : (j.completed < j.trial_count
          ? `<button class="ghost" data-resume="${esc(j.job_id)}">resume</button>`
          : `<a href="/data">results</a>`);
    return `<tr><td>${esc(j.job_id)}</td><td class="${cls}">${esc(j.state)}` +
      (j.paused_reason ? ` <span class="muted">(${esc(j.paused_reason)})</span>` : '') +
      `</td><td>${esc(j.model)}</td><td>${esc(j.profile)}</td><td>${esc(j.mode)}</td>` +
      `<td>${esc(j.repeats)}</td><td>${esc(j.trial_count)}</td>` +
      `<td>${esc(j.completed)}</td><td class="muted">${esc(j.notes || '')}</td>` +
      `<td>${action}</td></tr>`;
  }).join('') || '<tr><td colspan="10" class="muted">No jobs yet.</td></tr>';
  document.querySelectorAll('[data-resume]').forEach(el => el.onclick = async () => {
    el.disabled = true;
    const r = await fetch('/api/resume', {method:'POST',
      body: JSON.stringify({job_id: el.dataset.resume})});
    const d = await r.json();
    if (d.job_id) location.href = '/run?job=' + encodeURIComponent(d.job_id);
    else { $('formError').textContent = d.error; el.disabled = false; }
  });
  if (jobs.busy) setTimeout(refresh, 3000);
}
$('preview').onclick = e => { e.preventDefault(); preview(); };
$('go').onclick = async () => {
  $('formError').textContent = '';
  $('go').disabled = true;
  const body = {
    model: $('model').value, profile: $('profile').value, mode: $('mode').value,
    repeats: parseInt($('repeats').value) || 1, notes: $('notes').value,
    tasks: selector(),
  };
  const r = await fetch('/api/jobs', {method:'POST', body: JSON.stringify(body)});
  const d = await r.json();
  if (d.job_id) location.href = '/run?job=' + encodeURIComponent(d.job_id);
  else { $('formError').textContent = d.error || 'could not start'; $('go').disabled = false; }
};
load();
"""

DATA_BODY = """
<div class="card">
 <h2>Query</h2>
 <label>Saved views</label>
 <div class="chips" id="saved"></div>
 <label>SQL (read-only; SELECT only)</label>
 <textarea id="sql">SELECT * FROM config_scores</textarea>
 <p><button id="run">Run</button>
    <button id="refresh" class="ghost">Re-export from run directories</button></p>
 <div id="err" class="err"></div>
</div>
<div class="card">
 <h2>Result <span class="muted" id="count"></span></h2>
 <div class="scroll"><table id="out"><thead></thead><tbody></tbody></table></div>
</div>
"""

DATA_SCRIPT = r"""
const esc = s => String(s ?? '').replace(/[&<>"']/g, c =>
  ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $ = id => document.getElementById(id);
const SAVED = {
  'config_scores': 'SELECT * FROM config_scores',
  'task_reliability': 'SELECT * FROM task_reliability ORDER BY mean_score',
  'trials': 'SELECT trial_id, task_id, difficulty, profile, mode, score, elapsed_sec,\n       input_tokens_billed, output_tokens, cost_total_usd, validity_flags\nFROM trials ORDER BY trial_id',
  'cost by profile': 'SELECT profile, COUNT(*) AS trials, ROUND(AVG(score),4) AS score,\n       SUM(input_tokens_billed) AS input_tokens, SUM(output_tokens) AS output_tokens,\n       ROUND(SUM(cost_total_usd),4) AS cost_usd\nFROM trials GROUP BY profile',
  'failed checks': 'SELECT * FROM failed_checks LIMIT 200',
  'transcript': "SELECT trial_id, round, message_index, role, substr(content,1,200) AS content\nFROM messages ORDER BY trial_id, round, message_index LIMIT 300",
  'tables': "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') ORDER BY type, name",
};
$('saved').innerHTML = Object.keys(SAVED).map(k =>
  `<span class="chip" data-k="${esc(k)}">${esc(k)}</span>`).join('');
document.querySelectorAll('.chip').forEach(el => el.onclick = () => {
  $('sql').value = SAVED[el.dataset.k];
  run();
});
async function run(){
  $('err').textContent = '';
  const r = await fetch('/api/query', {method:'POST', body: JSON.stringify({sql: $('sql').value})});
  const d = await r.json();
  if (d.error){ $('err').textContent = d.error; return; }
  $('count').textContent = d.rows.length + ' rows';
  $('out').tHead.innerHTML = '<tr>' + d.columns.map(c => `<th>${esc(c)}</th>`).join('') + '</tr>';
  $('out').tBodies[0].innerHTML = d.rows.map(row =>
    '<tr>' + row.map(v => `<td>${esc(v)}</td>`).join('') + '</tr>').join('');
}
$('run').onclick = run;
$('refresh').onclick = async () => {
  $('refresh').disabled = true;
  $('err').textContent = 'Re-exporting…';
  const r = await fetch('/api/export', {method:'POST', body:'{}'});
  const d = await r.json();
  $('err').textContent = d.error || ('Refreshed: ' + JSON.stringify(d.table_counts));
  $('refresh').disabled = false;
  run();
};
run();
"""


def live_page(job_id: str) -> bytes:
    """The existing viewer, with a way back and a nudge when the job ends.

    Reused rather than rewritten: it already handles byte-cursor polling,
    sticky scrolling and the approval feed, and a second implementation would
    drift from it.
    """
    body = view_harness_bench.HTML
    # Give the viewer the console's navigation without forking its markup.
    body = body.replace(
        '<header><h1>Second Brain &middot; Harness-Bench</h1>',
        '<header><h1><a href="/" style="color:inherit;text-decoration:none">'
        '&larr; Second Brain &middot; Harness-Bench</a></h1>'
        '<span id="jobstate" class="muted"></span>')
    watcher = """
<script>
// The job, as opposed to the task: the viewer polls one run directory, and
// only the console knows when every replicate has finished.
const JOB = new URLSearchParams(location.search).get('job') || '';
async function watchJob(){
  try{
    const d = await (await fetch('/api/job?job=' + encodeURIComponent(JOB),
                                 {cache:'no-store'})).json();
    const s = d.status || {};
    document.querySelector('#jobstate').textContent =
      JOB ? `job ${JOB} — ${d.busy ? 'running' : (d.finished_reason || s.state || '')}` +
            ` — ${s.completed ?? 0}/${s.trial_count ?? 0} trials` : '';
    if(!d.busy && d.started){
      // Finished: land back on the dashboard, where the results are.
      document.querySelector('#jobstate').textContent += ' — returning…';
      setTimeout(() => location.href = '/', 2500);
      return;
    }
  }catch(e){}
  setTimeout(watchJob, 3000);
}
watchJob();
</script>
"""
    return body.replace("</body>", watcher + "</body>").encode("utf-8")


# -- server -----------------------------------------------------------

class ConsoleHandler(BaseHTTPRequestHandler):
    api: HarnessBenchAPI
    runner: JobRunner

    def do_GET(self) -> None:                                # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        route = parsed.path

        if route == "/":
            return self._send(200, page("Harness-Bench jobs", "jobs",
                                        DASHBOARD_BODY, DASHBOARD_SCRIPT),
                              "text/html; charset=utf-8")
        if route == "/run":
            job_id = (query.get("job") or [self.runner.job_id or ""])[0]
            return self._send(200, live_page(job_id), "text/html; charset=utf-8")
        if route == "/data":
            return self._send(200, page("Harness-Bench data", "data",
                                        DATA_BODY, DATA_SCRIPT),
                              "text/html; charset=utf-8")
        if route == "/api/catalog":
            return self._json(self._catalog())
        if route == "/api/jobs":
            return self._json(self._jobs())
        if route == "/api/job":
            payload = self.runner.snapshot()
            payload["started"] = bool(self.runner.job_id)
            return self._json(payload)
        if route == "/api/state":
            return self._json(self._state(query))
        return self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:                               # noqa: N802
        route = urllib.parse.urlparse(self.path).path
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
        except (TypeError, ValueError):
            return self._json({"error": "malformed request body"})

        try:
            if route == "/api/jobs":
                spec = JobSpec(
                    model=body.get("model") or "",
                    tasks=body.get("tasks") or {},
                    profile=body.get("profile") or "bench",
                    mode=body.get("mode") or "yolo",
                    repeats=int(body.get("repeats") or 1),
                    notes=body.get("notes") or "",
                )
                return self._json({"job_id": self.runner.start(spec)})
            if route == "/api/resume":
                return self._json({"job_id": self.runner.resume(body["job_id"])})
            if route == "/api/resolve":
                return self._json(self._resolve(body))
            if route == "/api/query":
                return self._json(self._query(body.get("sql") or ""))
            if route == "/api/export":
                return self._json(self.api.dataset())
        except Exception as exc:                             # noqa: BLE001
            # Every failure here is a user-facing one -- a bad selector, a busy
            # runner, a malformed query -- so it belongs on the page rather
            # than as a 500 the browser renders as nothing.
            return self._json({"error": f"{type(exc).__name__}: {exc}"})
        return self._send(404, b"not found", "text/plain")

    # -- data for the pages -------------------------------------------

    def _catalog(self) -> dict[str, Any]:
        rows = self.api.list_tasks()
        return {
            "models": sorted(MODELS),
            "profiles": [{"name": name, "description": spec.get("description") or ""}
                         for name, spec in sorted(PROFILES.items())],
            "modes": list(MODES),
            "difficulties": sorted({row["difficulty"] for row in rows}),
            "classes": sorted({row["class"] for row in rows}),
            "task_count": len(rows),
        }

    def _resolve(self, selector: dict[str, Any]) -> dict[str, Any]:
        """Answer "what would this select" without writing a job file."""
        from run_harness_bench import choose_tasks, validate_benchmark
        namespace = HarnessBenchAPI._selector(selector)
        tasks = choose_tasks(namespace, validate_benchmark(self.api.benchmark_root)["tasks"])
        return {"tasks": tasks}

    def _jobs(self) -> dict[str, Any]:
        rows = []
        for row in self.api.list_jobs():
            try:
                status = self.api.status(row["job_id"])
            except FileNotFoundError:
                continue
            rows.append({**row, "completed": status["completed"],
                         "state": status["state"] or row.get("state")})
        rows.sort(key=lambda item: item.get("created_at") or 0, reverse=True)
        return {"rows": rows, "busy": self.runner.busy}

    def _state(self, query: dict[str, list[str]]) -> dict[str, Any]:
        """Proxy the viewer's own state loader at this job's active replicate."""
        job_id = (query.get("job") or [self.runner.job_id or ""])[0]
        run_dir = active_run(self.api, job_id)
        if run_dir is None:
            try:                                  # nothing from this job yet
                run_dir = view_harness_bench.resolve_run("latest")
            except (FileNotFoundError, OSError):
                return {"run": {}, "tasks": [], "events": [], "cursor": 0,
                        "logs": {}, "summary": {}, "selected": "",
                        "waiting": "No run has started yet."}
        try:
            cursor = int((query.get("cursor") or ["0"])[0])
        except ValueError:
            cursor = 0
        return view_harness_bench.load_state(
            run_dir, (query.get("task") or [""])[0], cursor=max(0, cursor),
            follow=(query.get("follow") or ["1"])[0] != "0")

    def _query(self, sql: str) -> dict[str, Any]:
        """Run one read-only statement against the exported database.

        Opened through a ``mode=ro`` URI, so the guard below is a courtesy to
        the user rather than the thing standing between them and a dropped
        table: SQLite itself refuses the write.
        """
        # The statement is validated before anything else is consulted: whether
        # a query is allowed is a fact about the query, not about whether a
        # database happens to exist yet. Checking the file first would report
        # "no database" for a DROP and leave the real objection unsaid.
        statement = sql.strip().rstrip(";")
        if not statement:
            return {"error": "empty query"}
        lowered = statement.lower()
        if not (lowered.startswith("select") or lowered.startswith("with")):
            return {"error": "only SELECT (or WITH ... SELECT) queries are allowed"}
        if ";" in statement:
            return {"error": "one statement at a time"}
        database = self.api.results_root / DATABASE_NAME
        if not database.exists():
            return {"error": f"no database yet at {database}. "
                             "Run a job, or press 'Re-export from run directories'."}
        uri = "file:" + urllib.parse.quote(str(database)) + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            cursor = connection.execute(statement)
            rows = cursor.fetchmany(2000)
            columns = [column[0] for column in cursor.description or []]
        except sqlite3.Error as exc:
            return {"error": f"SQL error: {exc}"}
        finally:
            connection.close()
        return {"columns": columns, "rows": [list(row) for row in rows]}

    # -- plumbing ------------------------------------------------------

    def _json(self, payload: Any) -> None:
        self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    options = parser.parse_args(argv)

    api = HarnessBenchAPI()
    handler = type("BoundConsoleHandler", (ConsoleHandler,),
                   {"api": api, "runner": JobRunner(api)})
    server = ThreadingHTTPServer(("127.0.0.1", options.port), handler)
    url = f"http://127.0.0.1:{options.port}"
    print(f"Harness-Bench console: {url}\nResults: {RESULTS}\nCtrl+C to stop.")
    if not options.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
