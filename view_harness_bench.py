"""Local live viewer for Second Brain Harness-Bench runs."""

from __future__ import annotations

import argparse
import json
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_RESULTS = ROOT / "results" / "harness-bench"


HTML = r"""<!doctype html>
<html><head><meta charset="utf-8"><title>Second Brain &middot; Harness-Bench</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--line:#30363d;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922;--purple:#bc8cff}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e6edf3;font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}
header{padding:14px 24px;border-bottom:1px solid var(--line);display:flex;gap:18px;align-items:center;flex-wrap:wrap}h1{font:600 19px system-ui;margin:0}.muted{color:var(--muted)}
#live{font-size:12px;padding:2px 8px;border-radius:10px;border:1px solid var(--line)}
#live.on{color:var(--green);border-color:var(--green)}#live.stale{color:var(--amber);border-color:var(--amber)}#live.off{color:var(--red);border-color:var(--red)}
main{display:grid;grid-template-columns:330px 1fr;min-height:calc(100vh - 60px)}aside{border-right:1px solid var(--line);padding:12px;overflow:auto}
.task{padding:9px 11px;margin:5px 0;border:1px solid var(--line);border-radius:7px;cursor:pointer}.task:hover,.task.active{border-color:var(--blue);background:#1c2430}.task.run{border-left:3px solid var(--green)}
.score{float:right;color:var(--green)}
section{padding:16px;min-width:0}.cards{display:flex;gap:9px;flex-wrap:wrap}.card{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:9px 13px;min-width:112px}.value{font-size:19px;color:#fff}
.panel{margin-top:13px;background:var(--card);border:1px solid var(--line);border-radius:7px}.panel h2{font:600 13px system-ui;margin:0;padding:9px 13px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between}
.panel h2 label{font-weight:400;color:var(--muted);font-size:12px;cursor:pointer}
#text{padding:13px;white-space:pre-wrap;overflow-wrap:anywhere;min-height:150px;max-height:42vh;overflow:auto;font-family:system-ui;line-height:1.55}
#events,#decisions{max-height:30vh;overflow:auto}
.event{padding:5px 12px;border-top:1px solid #21262d}.tool{color:var(--blue)}.approval{color:var(--amber)}.error{color:var(--red)}.ok{color:var(--green)}.allow{color:var(--green)}.deny{color:var(--red)}.ask{color:var(--purple)}
#logs{padding:11px 13px;white-space:pre-wrap;overflow:auto;max-height:26vh;font-size:12px;color:var(--muted)}
@media(max-width:800px){main{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line);max-height:230px}}
</style></head><body>
<header><h1>Second Brain &middot; Harness-Bench</h1><span id="run" class="muted"></span><span id="live" class="off">no data</span><span id="updated" class="muted"></span></header>
<main><aside><div id="tasks"></div></aside><section>
<div class="cards">
 <div class="card"><div class="muted">Completion</div><div id="score" class="value">&mdash;</div></div>
 <div class="card"><div class="muted">Model calls</div><div id="calls" class="value">0</div></div>
 <div class="card"><div class="muted">Prompt tokens</div><div id="tokens" class="value">&mdash;</div></div>
 <div class="card"><div class="muted">Tool calls</div><div id="tools" class="value">0</div></div>
 <div class="card"><div class="muted">Allowed / denied</div><div id="gates" class="value">0 / 0</div></div>
 <div class="card"><div class="muted">Task</div><div id="selected" class="value" style="font-size:13px">&mdash;</div></div>
</div>
<div class="panel"><h2><span>Model output</span><label><input type="checkbox" id="stick" checked> follow</label></h2><div id="text"></div></div>
<div class="panel"><h2><span>Approval decisions</span><span class="muted" id="mode"></span></h2><div id="decisions"></div></div>
<div class="panel"><h2>Tools and errors</h2><div id="events"></div></div>
<div class="panel"><h2>Diagnostics</h2><div id="logs"></div></div>
</section></main>
<script>
// State the page accumulates rather than refetches. The server sends only
// events past `cursor`, so everything below is appended to, never rebuilt --
// which is also what keeps the panels from scrolling back to the top on
// every poll. Replacing textContent resets scrollTop, and at 1 Hz that made
// streaming output impossible to actually read.
let selected=null, follow=true, cursor=0, seenTask=null;
let out='', calls=0, tokens=0, tokensKnown=false, toolCalls=0, allowed=0, denied=0;
let lastEventAt=0, lastPoll=0;
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const atBottom=el=>el.scrollHeight-el.scrollTop-el.clientHeight<40;

function choose(id){ // an explicit click pins the task and stops following
  selected=id; follow=false; resetTask(); refresh();
}
function resetTask(){
  cursor=0; out=''; calls=0; tokens=0; tokensKnown=false; toolCalls=0; allowed=0; denied=0;
  lastEventAt=0;
  document.querySelector('#text').textContent='';
  document.querySelector('#events').innerHTML='';
  document.querySelector('#decisions').innerHTML='';
}
function append(id,html){
  const el=document.querySelector(id), stuck=atBottom(el);
  el.insertAdjacentHTML('beforeend',html);
  while(el.childElementCount>400) el.removeChild(el.firstElementChild);
  if(stuck) el.scrollTop=el.scrollHeight;
}
function render(data){
  const r=data.run||{};
  document.querySelector('#run').textContent=[r.run_id,r.mode,r.model].filter(Boolean).join(' / ');
  document.querySelector('#updated').textContent='updated '+new Date().toLocaleTimeString();
  document.querySelector('#mode').textContent=r.mode?('mode: '+r.mode):'';

  if(data.selected!==seenTask){seenTask=data.selected;selected=data.selected;resetTask()}
  const rows=data.tasks||[];
  document.querySelector('#tasks').innerHTML=rows.map(x=>
    `<div class="task ${x.task_id===data.selected?'active':''} ${x.state==='running'?'run':''}" data-id="${esc(x.task_id)}">`+
    `<span class="score">${x.outcome_score??''}</span>${esc(x.task_id)}<br><span>${esc(x.title||'')}</span><br>`+
    `<span class="muted">${esc(x.state||'pending')} / ${esc(x.difficulty||'unspecified')}</span></div>`).join('');
  document.querySelectorAll('.task').forEach(el=>el.onclick=()=>choose(el.dataset.id));
  document.querySelector('#score').textContent=data.summary?.completion_score??'--';
  document.querySelector('#selected').textContent=data.selected||'--';

  const textEl=document.querySelector('#text'), stick=document.querySelector('#stick').checked;
  const stuck=stick&&atBottom(textEl);
  let added='';
  for(const e of (data.events||[])){
    if(e.at&&e.at>lastEventAt) lastEventAt=e.at;
    if(e.source==='llm'&&e.kind==='llm_call'){
      calls++; const n=e.payload?.prompt_tokens;
      if(typeof n==='number'){tokens+=n;tokensKnown=true}
      continue;
    }
    if(e.source==='approver'){
      const p=e.payload||{};
      if(e.kind==='decision'){
        p.choice==='allow'?allowed++:denied++;
        append('#decisions',`<div class="event ${p.choice==='allow'?'allow':'deny'}">`+
          `[${esc(p.choice)}] ${esc(p.type)} ${esc(p.subject||'')}<br>`+
          `<span class="muted">${esc(p.why||'')}</span></div>`);
      }else if(e.kind==='question'){
        append('#decisions',`<div class="event ask">[asked] ${esc(p.title||'')}`+
          ` &rarr; ${esc(JSON.stringify(p.answer))}</div>`);
      }
      continue;
    }
    if(e.kind==='task_result'){
      append('#events',`<div class="event ok">[oracle] score ${esc(e.status?.outcome_score)} / ${esc(e.status?.state)}</div>`);
      continue;
    }
    const f=e.frame||{}, p=f.payload||{};
    if(f.kind==='stream_delta'){added+=p.delta||'';continue}
    if(f.kind==='tool_status'){
      if(p.status==='started') toolCalls++;
      append('#events',`<div class="event tool">[${esc(p.status)}] ${esc(p.tool_name||p.command_name||p.kind||'tool')}`+
        (p.ok===false?` <span class="error">${esc(p.error||'failed')}</span>`:'')+`</div>`);
    }else if(f.kind==='approval'){
      append('#events',`<div class="event approval">[dialog] ${esc(p.title)}</div>`);
    }else if(f.kind==='error'){
      append('#events',`<div class="event error">[error] ${esc(JSON.stringify(p))}</div>`);
    }
  }
  if(added){out+=added;textEl.textContent=out;if(stuck)textEl.scrollTop=textEl.scrollHeight}

  document.querySelector('#calls').textContent=calls;
  document.querySelector('#tokens').textContent=tokensKnown?tokens.toLocaleString():'--';
  document.querySelector('#tools').textContent=toolCalls;
  document.querySelector('#gates').textContent=allowed+' / '+denied;

  const logs=data.logs||{};
  document.querySelector('#logs').textContent=
    Object.keys(logs).map(k=>'== '+k+' ==\n'+logs[k]).join('\n\n')||'No diagnostic output yet.';

  // Liveness: a container that died leaves the clock ticking and everything
  // else unchanged, which reads as healthy. Age of the newest event says
  // otherwise.
  const badge=document.querySelector('#live');
  const state=(rows.find(x=>x.task_id===data.selected)||{}).state;
  if(state!=='running'){badge.className='off';badge.textContent=state||'idle'}
  else{
    const age=lastEventAt?(Date.now()/1000-lastEventAt):Infinity;
    if(age<20){badge.className='on';badge.textContent='live'}
    else if(age<120){badge.className='stale';badge.textContent='quiet '+Math.round(age)+'s'}
    else{badge.className='off';badge.textContent='no events '+Math.round(age)+'s'}
  }
  cursor=data.cursor||0;
}
async function refresh(){
  try{
    const q=new URLSearchParams({task:selected||'',cursor:String(cursor),follow:follow?'1':'0'});
    const r=await fetch('/api/state?'+q,{cache:'no-store'});
    render(await r.json());
  }catch(e){
    document.querySelector('#updated').textContent='viewer error: '+e;
  }
  setTimeout(refresh,1000);
}
refresh();
</script></body></html>"""


class ViewerHandler(BaseHTTPRequestHandler):
    run_dir: Path
    run_spec: str = ""

    def _current_run(self) -> Path:
        """Re-resolve ``latest`` per request, keeping the last good answer.

        ``--run latest`` used to be resolved once at start-up, so a viewer
        opened between two runs stayed pinned to the older one forever and
        showed nothing while the new run filled up beside it.
        """
        if self.run_spec != "latest":
            return self.run_dir
        try:
            return resolve_run("latest")
        except (FileNotFoundError, OSError):
            return self.run_dir

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            return self._send(200, HTML.encode(), "text/html; charset=utf-8")
        if parsed.path == "/api/state":
            query = urllib.parse.parse_qs(parsed.query)
            requested = (query.get("task") or [""])[0]
            try:
                cursor = int((query.get("cursor") or ["0"])[0])
            except ValueError:
                cursor = 0
            follow = (query.get("follow") or ["1"])[0] != "0"
            return self._json(load_state(self._current_run(), requested,
                                         cursor=max(0, cursor), follow=follow))
        self._send(404, b"not found", "text/plain")

    def _json(self, payload: Any) -> None:
        self._send(200, json.dumps(payload, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default="latest", help="run directory, run id, or 'latest'")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-open", action="store_true")
    options = parser.parse_args(argv)
    run_dir = resolve_run(options.run)
    if not (run_dir / "run.json").exists():
        parser.error(f"not a Harness-Bench run: {run_dir}")
    handler = type("BoundViewerHandler", (ViewerHandler,),
                   {"run_dir": run_dir, "run_spec": options.run})
    server = ThreadingHTTPServer(("127.0.0.1", options.port), handler)
    url = f"http://127.0.0.1:{options.port}"
    print(f"Viewing {run_dir}\n{url}")
    if not options.no_open:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def resolve_run(value: str) -> Path:
    if value == "latest":
        candidates = [path for path in DEFAULT_RESULTS.iterdir() if (path / "run.json").exists()] if DEFAULT_RESULTS.exists() else []
        if not candidates:
            raise FileNotFoundError("no Harness-Bench runs found")
        return max(candidates, key=lambda path: path.stat().st_mtime)
    path = Path(value)
    if path.exists():
        return path.resolve()
    return (DEFAULT_RESULTS / value).resolve()


def load_state(run_dir: Path, requested: str = "", cursor: int = 0,
               follow: bool = True) -> dict[str, Any]:
    """A snapshot of the run, plus only the events the client has not seen.

    ``cursor`` is a **byte offset** into the selected task's event log, and it
    is what makes the viewer usable on a long task. Re-reading the whole file
    every second was not merely wasteful: an event log is one line per stream
    delta, so a task that talks for ten minutes produces a file measured in
    megabytes, and the viewer was re-reading, re-parsing, re-serializing and
    re-rendering all of it at 1 Hz. The cost lands exactly when the run gets
    interesting.

    The offset is returned as ``cursor`` so the client can hand it back. A
    client that sends ``0`` gets the whole file, which is what a fresh page
    load and a task switch both want.
    """
    run = read_json(run_dir / "run.json") or {}
    summary = read_json(run_dir / "summary.json") or {}
    ids = [str(item) for item in (run.get("tasks") or [])]
    metadata = run.get("task_metadata") or {}
    tasks = [
        {
            **(metadata.get(task_id) or {}),
            **(read_json(run_dir / "tasks" / task_id / "status.json") or {"task_id": task_id, "state": "pending"}),
        }
        for task_id in ids
    ]
    running = next((row["task_id"] for row in tasks if row.get("state") == "running"), "")
    if requested in ids and not (follow and running and requested != running
                                 and _finished(tasks, requested)):
        selected = requested
    else:
        # Follow the active task by default. The viewer used to latch onto
        # whatever was running when the page opened and stay there, so once
        # task 1 finished you sat watching a corpse while task 2 ran.
        selected = running or (requested if requested in ids else "") or (ids[0] if ids else "")
    if selected != requested:
        cursor = 0

    events, cursor = read_jsonl_from(
        run_dir / "tasks" / selected / "events.jsonl", cursor) if selected else ([], 0)
    return {
        "run": run,
        "summary": summary,
        "tasks": tasks,
        "selected": selected,
        "running": running,
        "events": events,
        "cursor": cursor,
        "logs": _log_tails(run_dir / "tasks" / selected) if selected else {},
    }


def _finished(tasks: list[dict[str, Any]], task_id: str) -> bool:
    row = next((item for item in tasks if item.get("task_id") == task_id), {})
    return row.get("state") not in ("running", "pending", None)


def _log_tails(task_dir: Path, limit: int = 4000) -> dict[str, str]:
    """The last of each diagnostic log, so a dead task explains itself.

    Without this a container that died showed as a task frozen in ``running``
    with the clock still ticking, and the only way to learn why was to go and
    read files -- which is the thing a live viewer exists to save you.
    """
    tails = {}
    for name in ("harness.log", "container.log"):
        try:
            text = (task_dir / name).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.strip():
            tails[name] = text[-limit:]
    return tails


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl_from(path: Path, offset: int = 0) -> tuple[list[Any], int]:
    """Rows after ``offset``, and the offset to resume from next time.

    Opened in binary and seeked rather than read whole, so the cost is in the
    new bytes rather than in the file. A trailing partial line -- the writer
    is appending while we read -- is left behind by rewinding the returned
    offset to the last newline, so the next poll picks it up complete rather
    than dropping it as unparseable.
    """
    try:
        size = path.stat().st_size
    except OSError:
        return [], 0
    if offset > size:
        offset = 0                      # file replaced (a retry): start over
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            chunk = handle.read()
    except OSError:
        return [], offset
    end = chunk.rfind(b"\n")
    if end == -1:
        return [], offset               # no complete line yet
    complete, consumed = chunk[:end + 1], end + 1
    rows = []
    for line in complete.decode("utf-8", errors="replace").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows, offset + consumed


if __name__ == "__main__":
    raise SystemExit(main())
