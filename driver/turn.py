"""Establish a session, submit a task, and know when the agent is finished.

The order here is not stylistic. Each step below cost a run to discover, and
two of them fail *silently* when skipped -- the run still produces a number,
and the number is wrong.
"""

from __future__ import annotations

import time

#: A slash command creates a session without needing a model, which is the
#: cheapest way to satisfy the invariant below.
#:
#: **It must be a command that takes no arguments**, and that rules out the
#: obvious choice. ``/locations`` asks which map to show, so it leaves the
#: session in ``filling_command_form`` -- a phase where **plain text is
#: coerced into the form answer**. A driver that bootstraps with it then
#: submits the task prompt, the prompt is eaten as the answer to "Choose
#: which location map to show", and the run reports an agent that ignored
#: its instructions. ``/new`` takes nothing, ends at ``awaiting_input``, and
#: starts a fresh conversation, which is what a trial wants anyway.
BOOTSTRAP_COMMAND = "/new"

#: Phases in which the session is blocked on somebody and will not take a
#: turn. Text submitted here means something other than "do this task".
BLOCKED_PHASES = ("filling_command_form", "approving_request")


def establish_session(client, command=BOOTSTRAP_COMMAND, seconds=90.0,
                      require_ask_mode=True, fresh=True):
    """Make a session exist and leave it ready to take a turn.

    **Not optional, and the single most expensive omission available.** A
    session has to *exist* before anything can be attended: opening the stream
    does not create one, ``session.get`` answers ``{"data": null}``, and every
    unsafe Request comes back ``403 approval_declined`` with nobody having
    been asked. The run then looks like an agent that refuses to use its
    tools.

    Readiness is ``phase == "awaiting_input"``, not the presence of a
    conversation id: the conversation row is created lazily by the first real
    message, so ``conversation_id`` is still ``null`` here even though
    everything works. Waiting on it would hang forever, and treating a
    blocked phase as ready is the trap described above.

    Waiting for the bootstrap command's *own* ``typing: false`` is the other
    half. Scoping end-of-turn detection to frames after the task submit is
    necessary but not sufficient -- if this command's cycle is still in
    flight, its closing frame lands after that mark and ends the task turn
    instantly, which reads as a model that answered in 1.8 seconds.
    """
    mark = client.frames.mark()
    existing = (client.post("session.get", {"details": True})[1].get("data")) or {}
    status, answer = 200, {"data": existing}
    if fresh or not existing:
        status, answer = client.post(
            "frontend.submit", {"input_kind": "text", "text": command})
    deadline = time.time() + seconds
    data = {}
    while time.time() < deadline:
        data = (client.post("session.get", {"details": True})[1]
                .get("data")) or {}
        phase = data.get("phase")
        if phase in BLOCKED_PHASES:
            # Something is holding the session open. Clear it rather than
            # submit a task prompt into a form and measure the result.
            client.post("frontend.submit",
                        {"input_kind": "text", "text": "/cancel"})
            time.sleep(0.5)
            continue
        if phase == "awaiting_input" and not _still_typing(client, mark):
            break
        time.sleep(0.25)

    mode = data.get("mode")
    ready = data.get("phase") == "awaiting_input"
    # Callers may require ask mode for a mediated run or deliberately request
    # a kernel-enforced YOLO/Lockdown ablation after session establishment.
    honest = (mode == "ask") or not require_ask_mode
    return {"ok": bool(ready and honest),
            "phase": data.get("phase"),
            "mode": mode,
            "conversation_id": data.get("conversation_id"),
            "submit_status": status, "submit_answer": answer,
            "attended": bool(data.get("attended")),
            "note": None if honest else "refusing to run in mode " + str(mode)}


def set_security_mode(client, requested="ask", seconds=90.0):
    """Move the attended session into the explicitly requested kernel mode."""
    if requested not in ("ask", "yolo", "lockdown"):
        return {"ok": False, "mode": None, "note": "unknown mode " + repr(requested)}
    data = (client.post("session.get", {"details": True})[1].get("data")) or {}
    if data.get("mode") == requested and data.get("phase") == "awaiting_input":
        return {"ok": True, "mode": requested, "phase": data.get("phase"),
                "conversation_id": data.get("conversation_id"), "changed": False}

    mark = client.frames.mark()
    status, answer = client.post(
        "frontend.submit", {"input_kind": "text", "text": "/mode " + requested})
    deadline = time.time() + seconds
    while time.time() < deadline:
        data = (client.post("session.get", {"details": True})[1].get("data")) or {}
        if (data.get("phase") == "awaiting_input"
                and data.get("mode") == requested
                and not _still_typing(client, mark)):
            return {"ok": True, "mode": requested, "phase": data.get("phase"),
                    "conversation_id": data.get("conversation_id"), "changed": True,
                    "submit_status": status, "submit_answer": answer}
        time.sleep(0.25)
    return {"ok": False, "mode": data.get("mode"), "phase": data.get("phase"),
            "conversation_id": data.get("conversation_id"), "changed": True,
            "submit_status": status, "submit_answer": answer,
            "note": "mode transition did not settle"}


def run_turn(client, prompt, wall_s=900.0, stall_s=300.0, approver=None,
             log=print):
    """Submit one prompt and wait for the documented end of the turn.

    ``typing: false`` is the only reliable completion signal. It means the
    *logical* turn ended -- including after a doorman held it open, after a
    re-drive, and after a crash -- and there is no "done" event to wait for
    instead.

    Two budgets guard it. ``wall_s`` is the honest cap on the whole turn.
    ``stall_s`` is a silence cap, and it is **paused while a dialog is
    outstanding**: a POST blocked on an approval is the design rather than a
    hang, and a naive stall timer would kill precisely the runs the security
    story is about.
    """
    mark = client.frames.mark()
    started = time.time()
    status, answer = client.post(
        "frontend.submit", {"input_kind": "text", "text": prompt})
    if status and status >= 400:
        return _result(client, mark, started, "submit_failed", status, answer)

    seen = mark
    last_activity = time.time()
    reason = "wall_timeout"
    while True:
        now = time.time()
        frames = client.frames.snapshot(mark)
        if mark + len(frames) > seen:
            seen = mark + len(frames)
            last_activity = now
        if _turn_ended(frames):
            reason = "typing_false"
            break
        if now - started > wall_s:
            reason = "wall_timeout"
            break
        waiting = approver.outstanding() if approver is not None else 0
        if waiting:
            last_activity = now          # blocked on a person, not stalled
        elif now - last_activity > stall_s:
            reason = "stall"
            break
        time.sleep(0.2)
    return _result(client, mark, started, reason, status, answer)


def _turn_ended(frames):
    """A ``typing: false`` that follows a ``typing: true`` in this window."""
    started = False
    for frame in frames:
        if frame.get("kind") != "typing":
            continue
        payload = frame.get("payload")
        if payload is True:
            started = True
        elif payload is False and started:
            return True
    return False


def _still_typing(client, since):
    """True when the most recent typing frame since ``since`` said true."""
    state = None
    for frame in client.frames.snapshot(since):
        if frame.get("kind") == "typing":
            state = frame.get("payload")
    return state is True


def _result(client, mark, started, reason, submit_status, submit_answer):
    frames = client.frames.snapshot(mark)
    text, aborted = reply_text(frames)
    return {"ok": reason == "typing_false" and not aborted,
            "reason": reason,
            "final_text": text,
            "aborted": aborted,
            "elapsed_s": round(time.time() - started, 3),
            "submit_status": submit_status,
            "submit_answer": submit_answer,
            "mark": mark,
            "frame_count": len(frames),
            "kinds": sorted({str(f.get("kind")) for f in frames}),
            "errors": [f.get("payload") for f in frames
                       if f.get("kind") == "error"]}


def reply_text(frames):
    """The agent's reply, and whether the stream was cut off.

    The reply arrives as ``stream_delta`` frames **and nothing else**: a
    frontend declaring ``supports_streaming`` is deduped against the
    ``messages`` channel, so a client waiting for a ``messages`` frame waits
    forever. On the closing frame, ``final_text`` is the cleaned whole and
    replaces what was accumulated -- except when ``aborted``, where there is
    no ``final_text`` and the partial should be discarded rather than
    reported as an answer.
    """
    deltas = []
    final = None
    aborted = False
    for frame in frames:
        if frame.get("kind") != "stream_delta":
            continue
        payload = frame.get("payload") or {}
        deltas.append(str(payload.get("delta") or ""))
        if payload.get("done"):
            if payload.get("aborted"):
                aborted = True
            elif payload.get("final_text") is not None:
                final = str(payload.get("final_text"))
    if aborted:
        return "", True
    return (final if final is not None else "".join(deltas)), False
