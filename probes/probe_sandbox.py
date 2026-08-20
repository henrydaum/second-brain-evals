"""Exercise the sandbox itself through the wire: boxes, scripts, writes.

``proc.run`` proves the handler path. This proves the *box* path — a script
runs in a real subprocess with ``sandbox/`` as its cwd, which is the machinery
a container is most likely to break.
"""
import json
import threading
import time
import urllib.error
import urllib.request

BASE, TOKEN, THREAD = "http://127.0.0.1:8787", "benchtoken", "main"
frames: list = []


def post(kind, args=None, timeout=120):
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


def stream():
    req = urllib.request.Request(
        f"{BASE}/events?thread={THREAD}&token={TOKEN}",
        headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        r = urllib.request.urlopen(req, timeout=300)
        while True:
            raw = r.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            if line.startswith("data:"):
                try:
                    frames.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass
    except Exception:  # noqa: BLE001
        pass


def show(label, kind, args=None):
    status, answer = post(kind, args)
    print(f"   {label}: {status} {json.dumps(answer)[:260]}")
    return status, answer


def approve_in_background(decide):
    """Answer dialogs as a manifest approver would, on its own thread."""
    seen = set()

    def loop():
        while True:
            for f in list(frames):
                if f.get("kind") != "approval":
                    continue
                payload = f.get("payload") or {}
                rid = payload.get("id")
                if rid in seen:
                    continue
                seen.add(rid)
                detail = payload.get("detail") or {}
                choice = decide(detail)
                print(f"   [approver] {detail.get('type')} "
                      f"{json.dumps(detail)[:110]} -> {choice}")
                post("frontend.resolve", {"value": choice, "request_id": rid})
            time.sleep(0.15)

    threading.Thread(target=loop, daemon=True).start()


for _ in range(60):
    try:
        if post("session.get")[0]:
            break
    except OSError:
        time.sleep(1)

threading.Thread(target=stream, daemon=True).start()
time.sleep(1.0)
post("frontend.submit", {"input_kind": "text", "text": "/locations"})
time.sleep(2.5)
print("== session established ==")

# Deny nothing here; the point is to see what is even asked.
approve_in_background(lambda detail: "allow")

print("== paths the guest can ask for ==")
status, answer = show("paths.get scripts", "paths.get", {"name": "scripts"})
scripts_dir = (answer.get("data") or {})
scripts_dir = scripts_dir if isinstance(scripts_dir, str) else (
    answer.get("data") or "")
print(f"   scripts dir = {scripts_dir!r}")

print("== write a script into the workspace (free-write grant) ==")
source = (
    "def main(sdk):\n"
    "    sdk.log('script running inside a real box')\n"
    "    return {'ok': True, 'cwd_is_sandbox': True,\n"
    "            'budget': sdk.budget()}\n")
path = f"{scripts_dir}/bench_probe.py"
show("fs.write", "fs.write", {"path": path, "data": source})

print("== validate it the way the loader would ==")
show("plugin.validate", "plugin.validate", {"path": path})

print("== run it: a real subprocess box ==")
show("script.run", "script.run", {"path": path})

print("== a write OUTSIDE any writable dir ==")
show("fs.write /work", "fs.write", {"path": "/work/deliverable.txt",
                                    "data": "hello"})

print(f"== kinds seen: {sorted({f.get('kind') for f in frames})} ==")
