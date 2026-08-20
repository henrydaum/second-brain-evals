"""Read back what the run left behind: transcript, effects, files, metrics.

Three sources, all of them evidence a submission has to be able to show:

* ``conv.read`` -- what was said. Needs the conversation id, and has to be
  paged, because the answer is capped by *bytes* and asking for too much
  fails ``413`` rather than truncating.
* ``ledger.read`` -- every effect the system performed, with provenance. This
  is the audit trail, and it records the driver's own calls too.
* The filesystem -- whatever the task said to produce.

The driver is an ordinary process inside the container, not sandboxed code,
so the filesystem half is a plain walk rather than a Request. That matters
for honesty as well as speed: reading a deliverable through the same policy
the agent was subject to would let a policy bug hide the evidence of itself.
"""

from __future__ import annotations

import hashlib
import os
import time

#: The kernel caps a database answer at 500 rows (``DB_MAX_ROWS``).
LEDGER_PAGE = 500
#: Rows per transcript page. Dropped on a 413, which is a byte cap rather
#: than a row cap -- one row can be a 100 KB file edit.
CONV_PAGE = 100


def conversation_id(client):
    """The session's own conversation, with ``conv.list`` as the fallback."""
    data = (client.post("session.get", {"details": True})[1].get("data")) or {}
    found = data.get("conversation_id")
    if found is not None:
        return found
    listed = client.post("conv.list", {"limit": 1})[1].get("data") or []
    if isinstance(listed, dict):
        listed = listed.get("items") or []
    return listed[0].get("id") if listed else None


def transcript(client, cid, page=CONV_PAGE):
    """The whole conversation, oldest first, paged forwards.

    ``since_id: 0`` is how to ask for the very beginning; each page then
    resumes from the newest id it returned. Paging forwards rather than
    scrolling back is what makes "read all of it" terminate cleanly on an
    empty page instead of on a row count nobody knows in advance.
    """
    if cid is None:
        return {"messages": [], "complete": False,
                "note": "no conversation id"}
    rows = []
    since = 0
    limit = page
    while True:
        status, answer = client.post(
            "conv.read", {"id": cid, "since_id": since, "limit": limit,
                          "details": True})
        if status == 413:
            # A byte cap, not a row cap. Ask for less rather than give up.
            limit = max(1, limit // 2)
            if limit == 1:
                return {"messages": rows, "complete": False,
                        "note": "one row exceeded the wire cap"}
            continue
        if status != 200:
            return {"messages": rows, "complete": False,
                    "note": "conv.read " + str(status)}
        data = answer.get("data") or {}
        page_rows = data.get("messages") or []
        if not page_rows:
            return {"messages": rows, "complete": True,
                    "conversation": data.get("conversation")}
        rows.extend(page_rows)
        newest = data.get("newest_id")
        if newest is None or newest == since:
            return {"messages": rows, "complete": True,
                    "conversation": data.get("conversation")}
        since = newest


def ledger(client, cid=None, since_id=None):
    """Every effect, newest first.

    Scoped to the conversation when there is one -- the ledger is
    write-optimised filler by volume and is meant to be read targeted, never
    linearly. ``complete`` is reported rather than assumed: rows come back
    newest first with no ``before_id`` to page downwards with, so a trial
    that fills a whole page has older rows this cannot reach, and saying so
    is better than a silent truncation.
    """
    args = {"limit": LEDGER_PAGE}
    if cid is not None:
        args["conversation_id"] = cid
    if since_id is not None:
        args["since_id"] = since_id
    status, answer = client.post("ledger.read", args)
    rows = answer.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("rows") or []
    return {"rows": rows, "status": status,
            "complete": len(rows) < LEDGER_PAGE}


def ledger_high_water(client):
    """The newest ledger id before a turn, so the run can be bracketed."""
    rows = (client.post("ledger.read", {"limit": 1})[1].get("data")) or []
    if isinstance(rows, dict):
        rows = rows.get("items") or rows.get("rows") or []
    return rows[0].get("id") if rows else None


def workdir(root, max_bytes=8 * 1024 * 1024):
    """What the task directory holds now, with a hash per file.

    The hash is the point: a checker that compares against a fixture needs to
    say *changed*, and a size is not an answer. Oversized files are recorded
    with their size and no hash rather than being skipped, so a deliverable
    that is too big to hash still shows up as having been produced.
    """
    if not root or not os.path.isdir(root):
        return {"root": root, "files": [], "note": "no such directory"}
    files = []
    for base, _dirs, names in os.walk(root):
        for name in sorted(names):
            full = os.path.join(base, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                size = os.path.getsize(full)
                entry = {"path": rel, "bytes": size,
                         "mtime": round(os.path.getmtime(full), 3)}
                if size <= max_bytes:
                    entry["sha256"] = _sha256(full)
                files.append(entry)
            except OSError as e:
                files.append({"path": rel, "error": str(e)})
    return {"root": root, "files": files, "count": len(files),
            "collected_at": time.time()}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def metrics(frames, decisions, questions, ledger_rows):
    """The numbers the internal suite exists to produce.

    Two of these are hypotheses rather than bookkeeping, and both are stated
    in ``evals/internal/NOTES.md``:

    * **approvals per task** -- a task solved with fewer dialogs is a task the
      agent understood; a spike is usually flailing at shell where a script
      would have done.
    * **tool calls against script use** -- the script-first claim says a hard
      task should collapse into one ``run_script`` rather than fifteen tool
      round-trips. Recording both is what turns that into a measurement.
    """
    tools = {}
    commands = {}
    for frame in frames:
        if frame.get("kind") != "tool_status":
            continue
        payload = frame.get("payload") or {}
        if payload.get("status") != "started":
            continue
        if payload.get("kind") == "command":
            name = str(payload.get("command_name") or "?")
            commands[name] = commands.get(name, 0) + 1
        else:
            name = str(payload.get("tool_name") or "?")
            tools[name] = tools.get(name, 0) + 1

    by_type = {}
    denied = 0
    for decision in decisions:
        key = str(decision.get("type"))
        bucket = by_type.setdefault(key, {"allow": 0, "deny": 0})
        bucket[decision.get("choice", "deny")] = \
            bucket.get(decision.get("choice", "deny"), 0) + 1
        if decision.get("choice") == "deny":
            denied += 1

    actions = {}
    failed = 0
    refused = 0
    for row in ledger_rows:
        key = str(row.get("action_type"))
        actions[key] = actions.get(key, 0) + 1
        if not row.get("ok"):
            failed += 1
            if str(row.get("error_code") or "") == "approval_declined":
                refused += 1

    return {
        "tool_calls": sum(tools.values()),
        "tools": tools,
        "commands": commands,
        "script_runs": tools.get("run_script", 0),
        "shell_runs": tools.get("run_command", 0),
        "approvals": len(decisions),
        "approvals_denied": denied,
        "approvals_by_type": by_type,
        "questions_asked": len(questions),
        "ledger_actions": actions,
        "ledger_failed": failed,
        "ledger_refused": refused,
        "stream_deltas": sum(1 for f in frames
                             if f.get("kind") == "stream_delta"),
        "notifications": sum(1 for f in frames
                             if f.get("kind") == "notification"),
        "errors": sum(1 for f in frames if f.get("kind") == "error"),
    }
