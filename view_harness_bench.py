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
:root{color-scheme:dark;--bg:#0d1117;--card:#161b22;--line:#30363d;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149;--amber:#d29922}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:#e6edf3;font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace}
header{padding:18px 24px;border-bottom:1px solid var(--line);display:flex;gap:24px;align-items:center}h1{font:600 20px system-ui;margin:0}.muted{color:var(--muted)}
main{display:grid;grid-template-columns:340px 1fr;min-height:calc(100vh - 65px)}aside{border-right:1px solid var(--line);padding:14px;overflow:auto}.task{padding:10px 12px;margin:5px 0;border:1px solid var(--line);border-radius:7px;cursor:pointer}.task:hover,.task.active{border-color:var(--blue);background:#1c2430}.score{float:right;color:var(--green)}
section{padding:18px;min-width:0}.cards{display:flex;gap:10px;flex-wrap:wrap}.card{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:10px 14px;min-width:150px}.value{font-size:20px;color:#fff}.panel{margin-top:14px;background:var(--card);border:1px solid var(--line);border-radius:7px}.panel h2{font:600 14px system-ui;margin:0;padding:10px 13px;border-bottom:1px solid var(--line)}
#text{padding:14px;white-space:pre-wrap;overflow-wrap:anywhere;min-height:180px;max-height:48vh;overflow:auto;font-family:system-ui;line-height:1.55}.event{padding:6px 12px;border-top:1px solid #21262d}.tool{color:var(--blue)}.approval{color:var(--amber)}.error{color:var(--red)}.ok{color:var(--green)}
@media(max-width:800px){main{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--line);max-height:260px}}
</style></head><body>
<header><h1>Second Brain &middot; Harness-Bench</h1><span id="run" class="muted"></span><span id="updated" class="muted"></span></header>
<main><aside><div id="tasks"></div></aside><section>
<div class="cards"><div class="card"><div class="muted">Completion</div><div id="score" class="value">&mdash;</div></div><div class="card"><div class="muted">Model calls</div><div id="calls" class="value">0</div></div><div class="card"><div class="muted">Prompt tokens</div><div id="tokens" class="value">&mdash;</div></div><div class="card"><div class="muted">Selected task</div><div id="selected" class="value" style="font-size:13px">&mdash;</div></div></div>
<div class="panel"><h2>Model output</h2><div id="text"></div></div><div class="panel"><h2>Tools, approvals, and errors</h2><div id="events"></div></div>
</section></main>
<script>
let selected=null;
const esc=s=>String(s??"").replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
function choose(id){selected=id;refresh()}
function render(data){document.querySelector('#run').textContent=data.run.run_id+' / '+data.run.mode+' / '+data.run.model;document.querySelector('#updated').textContent='updated '+new Date().toLocaleTimeString();
 const rows=data.tasks||[];if(!selected){selected=(rows.find(x=>x.state==='running')||rows[0]||{}).task_id||null}
 document.querySelector('#tasks').innerHTML=rows.map(x=>`<div class="task ${x.task_id===selected?'active':''}" onclick="choose('${esc(x.task_id)}')"><span class="score">${x.outcome_score??''}</span>${esc(x.task_id)}<br><span>${esc(x.title||'')}</span><br><span class="muted">${esc(x.state||'pending')} / ${esc(x.difficulty||'unspecified')}</span></div>`).join('');
 document.querySelector('#score').textContent=data.summary?.completion_score??'--';document.querySelector('#selected').textContent=selected||'--';
 const ev=data.events||[];let output='',calls=0,tokens=0,known=false,other=[];
 for(const e of ev){if(e.source==='llm'&&e.kind==='llm_call'){calls++;let n=e.payload?.prompt_tokens;if(typeof n==='number'){tokens+=n;known=true}continue}const f=e.frame||{},p=f.payload||{};if(f.kind==='stream_delta'){output+=p.delta||'';continue}if(f.kind==='tool_status'){other.push(`<div class="event tool">[${esc(p.status)}] ${esc(p.tool_name||p.command_name||p.kind||'tool')}</div>`)}else if(f.kind==='approval'){other.push(`<div class="event approval">[approval] ${esc(p.title)}</div>`)}else if(f.kind==='error'){other.push(`<div class="event error">[error] ${esc(JSON.stringify(p))}</div>`)}else if(e.kind==='task_result'){other.push(`<div class="event ok">[oracle] score ${esc(e.status?.outcome_score)} / ${esc(e.status?.state)}</div>`)}}
 document.querySelector('#text').textContent=output;document.querySelector('#events').innerHTML=other.slice(-200).join('')||'<div class="event muted">Waiting for tool activity...</div>';document.querySelector('#calls').textContent=calls;document.querySelector('#tokens').textContent=known?tokens.toLocaleString():'--';}
async function refresh(){try{const r=await fetch('/api/state?task='+encodeURIComponent(selected||''),{cache:'no-store'});render(await r.json())}catch(e){document.querySelector('#updated').textContent='viewer error: '+e}setTimeout(refresh,1000)}refresh();
</script></body></html>"""


class ViewerHandler(BaseHTTPRequestHandler):
    run_dir: Path

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/":
            return self._send(200, HTML.encode(), "text/html; charset=utf-8")
        if parsed.path == "/api/state":
            requested = (urllib.parse.parse_qs(parsed.query).get("task") or [""])[0]
            return self._json(load_state(self.run_dir, requested))
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
    handler = type("BoundViewerHandler", (ViewerHandler,), {"run_dir": run_dir})
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


def load_state(run_dir: Path, requested: str = "") -> dict[str, Any]:
    """Load a consistent-enough snapshot while a run is writing files."""
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
    selected = requested if requested in ids else next(
        (row["task_id"] for row in tasks if row.get("state") == "running"),
        ids[0] if ids else "",
    )
    events = read_jsonl(run_dir / "tasks" / selected / "events.jsonl", limit=5000) if selected else []
    return {"run": run, "summary": summary, "tasks": tasks, "selected": selected, "events": events}


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def read_jsonl(path: Path, limit: int) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
