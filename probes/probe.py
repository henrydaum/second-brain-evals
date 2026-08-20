"""Drive Second Brain over the HTTP wire and prove the contract holds.

No LLM required. ``proc.run`` is UNSAFE, so it raises a real approval — which
is the whole driver contract in one round trip: the stream is the attendance
signal, the dialog arrives as an ``approval`` frame carrying the machine-
readable ``detail``, and answering it by id lets the POST complete.
"""
import json
import threading
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8787"
TOKEN = "benchtoken"
THREAD = "main"
frames: list = []


def post(kind, args=None, timeout=60):
    body = json.dumps(args or {}).encode()
    req = urllib.request.Request(
        f"{BASE}/sdk/{kind}?thread={THREAD}", data=body, method="POST",
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


stream_state: dict = {"status": None, "error": None, "lines": 0}


def stream():
    """The attendance signal: no stream, no dialogs."""
    req = urllib.request.Request(
        f"{BASE}/events?thread={THREAD}&token={TOKEN}",
        headers={"Authorization": f"Bearer {TOKEN}", "Accept": "text/event-stream"})
    try:
        r = urllib.request.urlopen(req, timeout=120)
        stream_state["status"] = r.status
        while True:
            raw = r.readline()
            if not raw:
                stream_state["error"] = "stream ended"
                return
            stream_state["lines"] += 1
            line = raw.decode(errors="replace").strip()
            if line.startswith("data:"):
                try:
                    frames.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
    except Exception as exc:  # noqa: BLE001 - diagnostics
        stream_state["error"] = f"{type(exc).__name__}: {exc}"


def wait_for(predicate, seconds=30):
    end = time.time() + seconds
    while time.time() < end:
        for f in list(frames):
            if predicate(f):
                return f
        time.sleep(0.1)
    return None


# 1. Wait for the server to answer at all.
for _ in range(60):
    try:
        if post("session.get", {"details": True})[0]:
            break
    except OSError:
        time.sleep(1)
else:
    raise SystemExit("server never answered")

def show(label, kind, args=None):
    status, answer = post(kind, args)
    body = json.dumps(answer)
    print(f"   {label}: {status} {body[:220]}")
    return status, answer


print("== 1. server is up ==")
show("session.get", "session.get", {"details": True})

# 2. Open the stream. Attendance follows from this.
threading.Thread(target=stream, daemon=True).start()
time.sleep(1.5)
print(f"== 2. stream: {stream_state} ==")
show("session.get", "session.get", {"details": True})

# A session has to exist before anything can be attended: the stream alone
# does not create one. A slash command makes it without needing an LLM.
print("== 2b. submit a command to establish the session ==")
show("submit /locations", "frontend.submit",
     {"input_kind": "text", "text": "/locations"})
time.sleep(3)
show("session.get", "session.get", {"details": True})
print(f"   frames so far: {sorted({f.get('kind') for f in frames})}")

print("== 3. read-only Requests over the wire ==")
show("conv.list", "conv.list", {"limit": 5})
show("ledger.read", "ledger.read", {"limit": 3})
show("command.list", "command.list", {})

# 4. The real test: an UNSAFE Request raises a dialog mid-POST.
print("== 4. proc.run (UNSAFE) should raise a dialog ==")
result: dict = {}
threading.Thread(
    target=lambda: result.update(
        zip(("status", "answer"),
            post("proc.run", {"argv": ["echo", "hello from the box"],
                              "shell": "default"}))),
    daemon=True).start()

frame = wait_for(lambda f: f.get("kind") == "approval")
if not frame:
    print("   NO APPROVAL FRAME (kinds seen: "
          f"{sorted({f.get('kind') for f in frames})})")
    print(f"   stream: {stream_state}")
    for _ in range(60):
        if result:
            break
        time.sleep(0.1)
    print(f"   proc.run -> {result.get('status')} "
          f"{json.dumps(result.get('answer'))[:300]}")
else:
    payload = frame.get("payload") or {}
    print(f"   title  : {payload.get('title')!r}")
    print(f"   detail : {json.dumps(payload.get('detail'), sort_keys=True)}")
    options = list(zip(payload.get("enum") or [],
                       payload.get("enum_labels") or []))
    print(f"   options: {options}")
    allow = next((v for v, _ in options if v == "allow"), None)
    print(f"== 5. resolving with {allow!r} (allow once) ==")
    post("frontend.resolve", {"value": allow, "request_id": payload.get("id")})
    for _ in range(150):
        if result:
            break
        time.sleep(0.1)
    print(f"   proc.run -> {result.get('status')} "
          f"{json.dumps(result.get('answer'))[:220]}")

print(f"== kinds seen: {sorted({f.get('kind') for f in frames})} ==")
