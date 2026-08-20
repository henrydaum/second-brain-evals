"""Benchmark-only observer for Second Brain LLM call events.

This module is copied beside ``main.py`` in the generated payload.  It listens
to the kernel's existing public event channel and appends JSONL; it does not
alter requests, responses, prompts, tools, or control flow.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

_lock = threading.Lock()
_unsubscribe = None


def install() -> None:
    """Install the observer when ``SB_LLM_USAGE_LOG`` names an output file."""
    global _unsubscribe
    target = os.environ.get("SB_LLM_USAGE_LOG")
    if not target or _unsubscribe is not None:
        return

    from events.event_bus import bus
    from events.event_channels import AGENT_LLM_CALL_FINISHED

    path = Path(target)

    def record(payload) -> None:
        row = dict(payload or {})
        row["recorded_at"] = time.time()
        rendered = json.dumps(row, ensure_ascii=False, default=str)
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(rendered + "\n")

    _unsubscribe = bus.subscribe(AGENT_LLM_CALL_FINISHED, record)
