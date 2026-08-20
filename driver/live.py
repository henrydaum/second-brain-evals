"""The live event log: one file, several writers, no interleaved lines.

The viewer reads a single JSONL stream per task, and three different things
write to it: the frame stream (``driver.wire.Frames``), the approver's
decisions (``driver.approver``), and — from a *different process* — the
kernel's LLM telemetry (``harness/eval_telemetry.py``). That last one is why
this is not just ``open(path, "a")``.

**Why the lock.** ``O_APPEND`` writes are atomic only below ``PIPE_BUF``
(4096 bytes on Linux). A long ``stream_delta`` or a fat usage payload exceeds
that, and two writers can then interleave inside one line. The reader's
``json.loads`` fails, the line is skipped, and an event vanishes with no
error anywhere — the worst failure mode available to a file you are going to
publish numbers from. ``flock`` costs nothing here and removes the class.

Falls back to an unlocked append where ``fcntl`` does not exist, which means
Windows, which means the unit tests. The container is Linux, so the runs that
produce real numbers always take the locked path.
"""

from __future__ import annotations

import json
import os
import threading
import time

try:                                                  # Linux container.
    import fcntl
except ImportError:                                   # Windows test host.
    fcntl = None                                      # type: ignore[assignment]


class LiveLog:
    """An append-only JSONL sink that several writers may share."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("SB_LIVE_EVENT_LOG")
        self._lock = threading.Lock()
        self._handle = None
        if self.path:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            self._handle = open(self.path, "a", encoding="utf-8")

    def write(self, source, kind, **payload):
        """Append one event. Never raises — telemetry must not break a run."""
        if self._handle is None:
            return
        event = {"at": time.time(), "source": source, "kind": kind}
        event.update(payload)
        line = json.dumps(event, ensure_ascii=False, default=str) + "\n"
        try:
            with self._lock:
                if fcntl is not None:
                    fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._handle.write(line)
                    # Flushed per event: a run killed mid-turn should still
                    # have every event up to the moment it died, and the
                    # viewer is reading this file while it is being written.
                    self._handle.flush()
                finally:
                    if fcntl is not None:
                        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ValueError, TypeError):
            return

    def close(self):
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None


#: One handle per process, so the frame stream and the approver interleave
#: through the same lock rather than through two independent ones.
_shared = None
_shared_lock = threading.Lock()


def shared() -> LiveLog:
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = LiveLog()
        return _shared


def reset() -> None:
    """Drop the shared handle. For tests that repoint ``SB_LIVE_EVENT_LOG``."""
    global _shared
    with _shared_lock:
        if _shared is not None:
            _shared.close()
        _shared = None
