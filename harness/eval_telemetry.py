"""Benchmark-only observer for Second Brain LLM call events.

This module is copied beside ``main.py`` in the generated payload.  It listens
to the kernel's existing public event channel and appends JSONL; it does not
alter requests, responses, prompts, tools, or control flow.

The ``flock`` here duplicates ``driver/live.py`` rather than importing it, and
the duplication is deliberate: this runs inside the *server* process, which
has ``/opt/sb-evals`` on its path, while the driver runs in a separate process
with ``/opt/sb-driver``.  The two write to the same live event log, which is
exactly why the lock has to exist in both -- ``O_APPEND`` is atomic only below
``PIPE_BUF`` (4096 bytes), and a usage payload can exceed that.  Without it two
processes interleave inside one line, the reader's ``json.loads`` fails, and
the event disappears with no error anywhere.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

try:                                                  # Linux container.
    import fcntl
except ImportError:                                   # Windows test host.
    fcntl = None                                      # type: ignore[assignment]

_lock = threading.Lock()
_unsubscribe = None


def _append_locked(path: Path, line: str) -> None:
    """One line, whole, even with another process appending to the same file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(line)
            handle.flush()
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def install() -> None:
    """Install the observer when ``SB_LLM_USAGE_LOG`` names an output file."""
    global _unsubscribe
    target = os.environ.get("SB_LLM_USAGE_LOG")
    if not target or _unsubscribe is not None:
        return

    from events.event_bus import bus
    from events.event_channels import AGENT_LLM_CALL_FINISHED

    path = Path(target)
    live_target = os.environ.get("SB_LIVE_EVENT_LOG")

    def record(payload) -> None:
        row = dict(payload or {})
        row["recorded_at"] = time.time()
        rendered = json.dumps(row, ensure_ascii=False, default=str)
        with _lock:
            # The usage log has one writer, so it needs no file lock; the
            # live log has several, so it does.
            _append_locked(path, rendered + "\n")
            if live_target:
                event = {"at": time.time(), "source": "llm",
                         "kind": "llm_call", "payload": row}
                _append_locked(
                    Path(live_target),
                    json.dumps(event, ensure_ascii=False, default=str) + "\n")

    _unsubscribe = bus.subscribe(AGENT_LLM_CALL_FINISHED, record)
