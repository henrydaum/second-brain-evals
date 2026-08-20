"""Drive one real agent turn over the wire and wait for the documented end.

This is the whole driver in miniature: open the stream, establish the session,
submit, answer any dialog from a manifest, and stop on ``typing: false`` —
which the protocol calls the one reliable "the agent is finished" signal.
"""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

BASE, TOKEN, THREAD = "http://127.0.0.1:8787", "benchtoken", "main"
frames: list = []
PROMPT = sys.argv[1] if len(sys.argv) > 1 else (
    "In one short sentence: what is 17 times 23? Just answer.")


def post(kind, args=None, timeout=300):
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
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def stream():
    req = urllib.request.Request(
        f"{BASE}/events?thread={THREAD}&token={TOKEN}",
        headers={"Authorization": f"Bearer {TOKEN}"})
    try:
        r = urllib.request.urlopen(req, timeout=600)
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


def approver():
    """Allow once, always — a manifest would decide; this only proves flow."""
    seen = set()
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
            print(f"   [approver] {json.dumps(detail)[:150]} -> allow",
                  flush=True)
            post("frontend.resolve", {"value": "allow", "request_id": rid})
        time.sleep(0.15)


for _ in range(90):
    if post("session.get")[0]:
        break
    time.sleep(1)

threading.Thread(target=stream, daemon=True).start()
time.sleep(1.0)
post("frontend.submit", {"input_kind": "text", "text": "/new"})
time.sleep(2.0)
threading.Thread(target=approver, daemon=True).start()

print(f"== model check ==", flush=True)
status, answer = post("llm.list")
print(f"   llm.list: {status} {json.dumps(answer)[:300]}", flush=True)

print(f"== submitting: {PROMPT!r} ==", flush=True)
mark = len(frames)          # only what this turn produced
start = time.time()
status, answer = post("frontend.submit",
                      {"input_kind": "text", "text": PROMPT})
print(f"   submit -> {status} {json.dumps(answer)[:120]}", flush=True)

# The documented completion signal: typing flips false when the *logical*
# turn ends, including after a doorman holds it open or on a crash.
deadline = time.time() + 240
saw_typing = False
while time.time() < deadline:
    for f in list(frames[mark:]):
        if f.get("kind") == "typing":
            if f.get("payload") is True:
                saw_typing = True
            elif saw_typing:
                deadline = 0
                break
    if deadline == 0:
        break
    time.sleep(0.2)

elapsed = time.time() - start
turn = frames[mark:]
kinds = [f.get("kind") for f in turn]
print(f"== turn ended after {elapsed:.1f}s "
      f"(typing seen: {saw_typing}, ended: {deadline == 0}) ==", flush=True)
print(f"   kinds: {sorted(set(kinds))}")
print(f"   stream_delta frames: {kinds.count('stream_delta')}")

# The reply arrives as deltas and nothing else: a frontend declaring
# supports_streaming is deduped against the messages channel, so a client
# that waits for a `messages` frame waits forever. `final_text` on the
# closing frame is the authoritative whole.
deltas = "".join(str((f.get("payload") or {}).get("delta") or "")
                 for f in turn if f.get("kind") == "stream_delta")
final = next((str((f.get("payload") or {}).get("final_text") or "")
              for f in reversed(turn)
              if f.get("kind") == "stream_delta"
              and (f.get("payload") or {}).get("done")), "")
print(f"   STREAMED : {deltas[:400]!r}")
print(f"   FINAL    : {final[:400]!r}")
for f in turn:
    if f.get("kind") == "messages":
        print(f"   MESSAGE: {json.dumps(f.get('payload'))[:400]}")
    if f.get("kind") == "error":
        print(f"   ERROR: {json.dumps(f.get('payload'))[:300]}")

# conv.read needs the conversation named; the session's own id comes from
# session.get, and conv.list is the fallback for a fresh container.
cid = ((post("session.get", {"details": True})[1].get("data")) or {}
       ).get("conversation_id")
if cid is None:
    listed = post("conv.list", {"limit": 1})[1].get("data") or []
    cid = listed[0].get("id") if listed else None
status, answer = post("conv.read", {"id": cid, "limit": 10})
rows = (answer.get("data") or {}).get("messages") or []
print(f"== transcript ({len(rows)} rows) ==")
for row in rows[-4:]:
    content = (row.get("content") or "")[:200].replace("\n", " ")
    print(f"   {row.get('role')}: {content}")
