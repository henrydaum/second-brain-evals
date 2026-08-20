"""The wire: SSE stream in, Requests out, every frame kept.

Lifted from the five probes, which each carried their own copy of ``post`` and
``stream``. The copies had already drifted in timeouts and error handling,
which is the usual way a benchmark harness starts lying: two runs differ
because two clients differ.

Three rules from ``docs/HTTP_PROTOCOL.md`` are structural here rather than
advisory, because each one cost a run to discover:

* **The stream is the attendance signal.** Opening ``/events`` declares that
  somebody is watching; attendance decides whether an unsafe Request raises a
  dialog or is refused outright. No stream, no dialogs. So the stream opens
  first and stays open for the whole run, and it *reconnects itself* -- a
  dropped stream is a silently unattended session, and the agent would lose
  the ability to ask for anything without any error surfacing.
* **A POST may legitimately block on a person.** That is the design, not a
  hang: the kernel holds the request open while the dialog is unanswered, for
  up to its own 300-second deadline. Timeouts here are generous on purpose.
* **Frames are events, not state.** Nothing is re-sent because you asked, so
  every frame is written to disk as it arrives. Everything else in a result
  bundle is derived from that file, which means a scoring bug can be fixed and
  re-run without spending another model call.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request

#: The kernel binds loopback only, so a driver runs inside the container.
DEFAULT_BASE = os.environ.get("SB_BASE_URL", "http://127.0.0.1:8787")
DEFAULT_TOKEN = os.environ.get("SB_HTTP_TOKEN", "")
DEFAULT_THREAD = os.environ.get("SB_THREAD", "main")

#: A dialog expires after 300s in the kernel, so a POST blocked on one cannot
#: outlive that by much. Anything shorter risks abandoning a live approval.
BLOCKING_TIMEOUT = 360.0


class Frames:
    """Every frame the stream delivered, in order, with a cursor.

    A plain list would do until two threads read it -- the approver polls for
    ``approval`` while the turn watches for ``typing`` -- so the lock is not
    ceremony. ``since`` is the cursor that makes end-of-turn detection
    correct: a previous command's typing cycle must not end the turn that
    follows it, and the only way to say that is "frames after this index".
    """

    def __init__(self, path=None):
        self._frames = []
        self._lock = threading.Lock()
        self._sink = open(path, "a", encoding="utf-8") if path else None

    def append(self, frame):
        with self._lock:
            self._frames.append(frame)
            if self._sink is not None:
                # Flushed per frame: a run that is killed mid-turn should
                # still have every frame up to the moment it died.
                self._sink.write(json.dumps(frame, ensure_ascii=False) + "\n")
                self._sink.flush()

    def snapshot(self, since=0):
        with self._lock:
            return list(self._frames[since:])

    def mark(self):
        """The cursor to scope a later read to. See the class docstring."""
        with self._lock:
            return len(self._frames)

    def kinds(self, since=0):
        return {f.get("kind") for f in self.snapshot(since)}

    def close(self):
        with self._lock:
            if self._sink is not None:
                self._sink.close()
                self._sink = None

    def __len__(self):
        with self._lock:
            return len(self._frames)


class Client:
    """One session on the wire: a stream that stays open, and Requests.

    ``thread`` selects the session, keyed ``http:<thread>``. The client never
    names a session any other way -- a ``session_key`` in a body is stripped
    and replaced, because identity belongs to the server to state.
    """

    def __init__(self, base=DEFAULT_BASE, token=DEFAULT_TOKEN,
                 thread=DEFAULT_THREAD, record=None):
        self.base = base.rstrip("/")
        self.token = token
        self.thread = thread
        self.frames = Frames(record)
        self.stream_state = {"open": False, "reconnects": 0,
                             "last_event_id": None, "error": None}
        self._stop = threading.Event()
        self._thread = None

    # ---------------------------------------------------------------- posts

    def post(self, kind, args=None, timeout=BLOCKING_TIMEOUT):
        """One Request. Answers ``(status, body)``; never raises for HTTP.

        A ``403 approval_declined`` is the single most interesting answer this
        call can give -- it is the security layer working -- so it arrives as
        data rather than as an exception somebody has to remember to catch.
        """
        body = json.dumps(args or {}).encode()
        request = urllib.request.Request(
            self.base + "/sdk/" + kind + "?thread=" + self.thread, data=body,
            method="POST",
            headers={"Authorization": "Bearer " + self.token,
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                return answer.status, _json(answer.read())
        except urllib.error.HTTPError as e:
            return e.code, _json(e.read())
        except Exception as e:                                  # noqa: BLE001
            return 0, {"error": type(e).__name__ + ": " + str(e)}

    def wait_until_up(self, seconds=120.0):
        """Poll until the server answers at all. Boot is not instant."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.post("session.get", timeout=10)[0]:
                return True
            time.sleep(0.5)
        return False

    # --------------------------------------------------------------- stream

    def open_stream(self, settle=1.0):
        """Open ``/events`` and keep it open, reconnecting on its own.

        The reconnect is the point. A stream that dies takes attendance with
        it, and the failure is silent: the agent simply stops being able to
        ask, and every unsafe Request comes back refused with nobody having
        been asked. ``Last-Event-ID`` resumes from where we stopped, which the
        kernel honours out of a 500-frame buffer.
        """
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._stream_forever,
                                        daemon=True)
        self._thread.start()
        time.sleep(settle)

    def _stream_forever(self):
        while not self._stop.is_set():
            try:
                self._stream_once()
            except Exception as e:                              # noqa: BLE001
                self.stream_state["error"] = type(e).__name__ + ": " + str(e)
            self.stream_state["open"] = False
            if self._stop.is_set():
                return
            self.stream_state["reconnects"] += 1
            time.sleep(0.5)

    def _stream_once(self):
        headers = {"Authorization": "Bearer " + self.token,
                   "Accept": "text/event-stream"}
        if self.stream_state["last_event_id"] is not None:
            headers["Last-Event-ID"] = str(self.stream_state["last_event_id"])
        request = urllib.request.Request(
            self.base + "/events?thread=" + self.thread
            + "&token=" + self.token, headers=headers)
        # No read timeout: an idle stream is a working stream, and the agent
        # may legitimately think for minutes between frames.
        answer = urllib.request.urlopen(request)
        self.stream_state["open"] = True
        self.stream_state["error"] = None
        while not self._stop.is_set():
            raw = answer.readline()
            if not raw:
                return                       # server closed; reconnect above
            line = raw.decode("utf-8", errors="replace").strip()
            if line.startswith("id:"):
                self.stream_state["last_event_id"] = line[3:].strip()
            elif line.startswith("data:"):
                try:
                    self.frames.append(json.loads(line[5:].strip()))
                except json.JSONDecodeError:
                    pass                     # a keep-alive or a partial line

    def close(self):
        self._stop.set()
        self.frames.close()

    # -------------------------------------------------------------- waiting

    def wait_for(self, predicate, seconds=60.0, since=0):
        """The first frame after ``since`` that satisfies ``predicate``."""
        deadline = time.time() + seconds
        while time.time() < deadline:
            for frame in self.frames.snapshot(since):
                if predicate(frame):
                    return frame
            time.sleep(0.1)
        return None


def _json(raw):
    try:
        return json.loads(raw or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"error": "unparseable answer",
                "raw": (raw or b"")[:400].decode("utf-8", errors="replace")}
