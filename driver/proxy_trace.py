"""Re-express one run's transcript as the proxy capture the grader reads.

Harness-Bench's process grader does not read our bundle. It reads
``<sandbox>/usage-proxy/``: one JSON file per model call holding the request
body that call was sent and the response it got back, plus a ``requests.jsonl``
index carrying usage. That layout exists because the upstream harnesses are
graded through a recording HTTP proxy, and the grader rebuilds a conversation
from what the proxy saw by diffing each request against the previous one.

We have no such proxy -- the driver talks to the kernel, not to an endpoint --
but we do keep the same information in a better form: an ordered, deduplicated
transcript. So this module runs the reconstruction backwards. It re-expands the
transcript into the per-call request/response records the differ expects, and
the differ then recovers the deltas it would have computed from a real capture.

**The transcript is the source of truth and this is a projection of it.** If the
two ever disagree the transcript wins, because the process score is a reading of
what happened and not its own evidence.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: What the record's ``provider`` field says. Two values are reserved by the
#: reader -- ``compaction_summarizer`` rounds are folded into history without
#: emitting output, and ``router`` rounds skip the history diff entirely -- so
#: an ordinary round must not claim either.
PROVIDER = "second-brain"


def _assistant_message(content: str) -> dict[str, Any]:
    """Turn one stored assistant row back into a chat-completions message.

    The kernel stores an assistant turn as a JSON string holding the text and
    any tool calls, because that is what it received. A row that is not JSON is
    plain prose, which is the ordinary shape for the final answer.
    """
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {"role": "assistant", "content": content or ""}
    if not isinstance(payload, dict):
        return {"role": "assistant", "content": content or ""}
    message: dict[str, Any] = {"role": "assistant",
                               "content": payload.get("content") or ""}
    calls = payload.get("tool_calls")
    if isinstance(calls, list) and calls:
        message["tool_calls"] = calls
    return message


def _chat_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The transcript in the shape a chat request carries it."""
    out: list[dict[str, Any]] = []
    for row in rows:
        role = str(row.get("role") or "").strip()
        content = row.get("content")
        content = "" if content is None else str(content)
        if role == "assistant":
            out.append(_assistant_message(content))
        elif role == "tool":
            message: dict[str, Any] = {"role": "tool", "content": content}
            if row.get("tool_call_id"):
                message["tool_call_id"] = row["tool_call_id"]
            if row.get("tool_name"):
                message["name"] = row["tool_name"]
            out.append(message)
        elif role in ("user", "system"):
            out.append({"role": role, "content": content})
    return out


def build_records(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One record per assistant turn: what was asked, and what came back.

    An assistant row ends a round, so the request that produced it carried
    everything before it. Rebuilding the request cumulatively is what lets the
    reader's diff recover each round's new messages -- the same answer it would
    reach from a real capture, by the same route.
    """
    chat = _chat_messages(messages)
    records: list[dict[str, Any]] = []
    for index, message in enumerate(chat):
        if message.get("role") != "assistant":
            continue
        records.append({
            "provider": PROVIDER,
            "request_body": json.dumps({"messages": chat[:index]},
                                       ensure_ascii=False),
            "response_json": {"choices": [{"index": 0,
                                           "message": message,
                                           "finish_reason": "stop"}]},
        })
    return records


def write(sandbox: Path, messages: list[dict[str, Any]],
          usage: list[dict[str, Any]] | None = None) -> int:
    """Write the capture for one run and return how many rounds it holds.

    Appends rather than replaces: a multi-round task calls this once per round
    against the same sandbox, and the reader takes the files in name order, so
    later rounds have to keep sorting after earlier ones.
    """
    proxy = Path(sandbox) / "usage-proxy"
    responses = proxy / "responses"
    responses.mkdir(parents=True, exist_ok=True)
    start = len(list(responses.glob("*.json")))

    records = build_records(messages)
    index_lines: list[str] = []
    for offset, record in enumerate(records):
        name = f"{start + offset:04d}.json"
        (responses / name).write_text(
            json.dumps(record, ensure_ascii=False), encoding="utf-8")
        row: dict[str, Any] = {"raw_response_file": name, "provider": PROVIDER}
        # Usage is per model call and the reader indexes it by file name. A
        # round with no matching usage row is still a round; the totals simply
        # do not account for it, which beats dropping the round.
        if usage and offset < len(usage):
            row["usage"] = usage[offset]
        index_lines.append(json.dumps(row, ensure_ascii=False))

    with open(proxy / "requests.jsonl", "a", encoding="utf-8") as handle:
        for line in index_lines:
            handle.write(line + "\n")
    return len(records)
