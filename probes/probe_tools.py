"""What actually registered, and what each installed file needs to load.

Declared ``dependencies_pip`` is only half the question: a plugin can also
want a system binary (ripgrep) or import something nobody declared. The
kernel's own answer is what registered, so ask it.
"""
import json
import urllib.error
import urllib.request

BASE, TOKEN, THREAD = "http://127.0.0.1:8787", "benchtoken", "main"


def post(kind, args=None):
    req = urllib.request.Request(
        f"{BASE}/sdk/{kind}?thread={THREAD}",
        data=json.dumps(args or {}).encode(), method="POST",
        headers={"Authorization": f"Bearer {TOKEN}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return json.loads(e.read() or b"{}")


def names(entries):
    out = []
    for entry in entries or []:
        out.append(entry.get("name") if isinstance(entry, dict) else entry)
    return sorted(str(n) for n in out if n)


tools = names((post("tool.list") or {}).get("data"))
print(f"REGISTERED TOOLS ({len(tools)}):")
for name in tools:
    print(f"   {name}")

for source in ("services", "tasks", "frontends", "commands"):
    got = (post("plugin.list", {"source": source}) or {}).get("data")
    print(f"\n{source.upper()}: {json.dumps(got)[:300]}")

print("\nINSTALLED FILES — does each one load?")
listed = (post("fs.list", {"path": "/data/Second Brain/installed",
                           "recursive": True, "glob": "*.py"}) or {}).get("data")
paths = listed if isinstance(listed, list) else (listed or {}).get("entries", [])
for item in paths:
    path = item if isinstance(item, str) else item.get("path", "")
    if not path.endswith(".py"):
        continue
    verdict = (post("plugin.validate", {"path": path}) or {}).get("data") or {}
    stem = path.rsplit("/", 1)[-1]
    flag = "OK " if verdict.get("ok") else "FAIL"
    extra = ""
    if verdict.get("unmediated"):
        extra = f" needs: {sorted({u.get('module') for u in verdict['unmediated'] if isinstance(u, dict)})}"
    elif verdict.get("disclaimed"):
        extra = " (disclaimed)"
    print(f"   {flag} {stem}{extra}")
