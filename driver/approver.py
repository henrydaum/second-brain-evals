"""Answer the agent's dialogs from a manifest, and write down every answer.

This is the layer measured by the mediated configuration. YOLO and Lockdown
are separate, explicitly labeled ablations enforced by Second Brain's kernel;
the approver remains attached to handle setup UI and to retain an audit trail.

Four rules, each of which the kernel's own vocabulary makes exact:

1. **Match on ``detail``, never on ``title`` or ``body``.** Those are prose
   renderings of the same facts, and a reworded dialog would silently change
   policy without a single test failing.
2. **Answer ``allow``, never an ``always:`` option.** A standing grant escapes
   the manifest into config and silently widens every later task in that
   trial -- and the widening is invisible, because nothing asks again.
3. **``prefixes`` are ``(program, subcommand)`` pairs**, the same unit
   ``shell_allowed_prefixes`` grants are stored in
   (``sandbox/shell.py::command_prefix``). Matching in that unit is exact
   rather than a guess. A line the lexer refuses -- a glob, a redirect, a
   subshell -- carries no ``prefixes`` at all, and is matched on ``command``
   or refused.
4. **No match means deny, and log it.** A benchmark result where the harness
   quietly allowed something is not a result.

Worth knowing while writing a manifest: a read-only shell command never
reaches here at all. ``sandbox/shell.py::_read_only_command`` classifies it
SAFE, so ``ls``, ``cat`` and ``git status`` raise no dialog and cost no
approval. An approval count is therefore already a count of *consequential*
acts, which is what makes it worth reporting per task.
"""

from __future__ import annotations

import posixpath
import threading
import time
import urllib.parse

from driver import live

#: What a family carries in ``detail``, per ``docs/HTTP_PROTOCOL.md``.
#: ``proc.*``: command / cwd / prefixes. ``net.http``: method / url.
#: ``fs.*``: path / dst. Everything else: subject.
DENY = "deny"
ALLOW = "allow"


class Manifest:
    """A task's standing answers, expressed in the kernel's own units.

    ::

        {"proc.run": {"prefixes": ["python", "sqlite3"]},
         "fs.write": {"under": ["/work/task"]},
         "net.http": {"hosts": ["html.duckduckgo.com"]},
         "default": "deny"}

    A whole family can also be answered outright with the string ``"allow"``
    or ``"deny"``. Harness-Bench's YOLO and Lockdown ablations use the kernel's
    explicit session mode; this manifest supplies the narrower mediated mode.
    """

    def __init__(self, spec=None):
        spec = dict(spec or {})
        self.default = spec.pop("default", DENY)
        self.rules = spec

    def decide(self, detail):
        """``(choice, why)`` for one permission gate. Never raises.

        ``why`` is recorded beside the decision because a manifest that
        allowed something for a reason nobody wrote down is the same problem
        as a harness that allowed it silently.
        """
        kind = (detail or {}).get("type")
        if not kind:
            return self.default, "no request type in detail"
        rule = self.rules.get(kind)
        if rule is None:
            return self.default, "no rule for " + str(kind)
        if isinstance(rule, str):
            return (rule, "family rule: " + rule) if rule in (ALLOW, DENY) \
                else (DENY, "unreadable rule " + repr(rule))
        if kind.startswith("proc."):
            return _decide_proc(rule, detail)
        if kind == "net.http":
            return _decide_net(rule, detail)
        if kind.startswith("fs."):
            return _decide_fs(rule, detail)
        return _decide_subject(rule, detail)


def _decide_proc(rule, detail):
    """Every segment must be granted, or the whole line is refused.

    That is the kernel's own rule (``shell._remembered_prefix``): granting
    ``git push`` must not run ``git push && rm -rf /``, because ``rm`` is a
    segment of its own and nobody granted it.

    **Two shapes can never be granted by prefix, and both show up in real
    runs.** A line the lexer refuses -- a redirect, a subshell, a
    substitution, anything with a metacharacter -- carries no ``prefixes`` at
    all. And a program named by absolute path (``"/usr/local/bin/python"
    build.py``) is deliberately not the ``python`` this vocabulary is talking
    about, because resolving which one it is means trusting PATH. For both,
    the only honest answers are an exact ``commands`` match or a refusal --
    **not** a string-prefix test, which would make ``git push`` also match
    ``git push && rm -rf /``. If a task keeps hitting this, the fix is
    granting ``script.run`` so the agent has a clean route, not loosening the
    matcher until it stops noticing.
    """
    command = detail.get("command") or ""
    exact = set(rule.get("commands") or [])
    if command and command in exact:
        return ALLOW, "exact command"
    prefixes = detail.get("prefixes") or []
    if not prefixes:
        # The lexer refused to decompose this line, so there is nothing to
        # match in the grant vocabulary. An exact command match was the only
        # honest route and it did not hit.
        return DENY, "no prefixes (undecomposable line) and no exact match"
    allowed = {str(p).strip().casefold() for p in (rule.get("prefixes") or [])}
    missing = [p for p in prefixes if str(p).strip().casefold() not in allowed]
    if missing:
        return DENY, "ungranted segment(s): " + ", ".join(map(str, missing))
    return ALLOW, "prefixes granted: " + ", ".join(map(str, prefixes))


def _decide_net(rule, detail):
    """Host allowlist, matched the way the kernel matches ``net_allowed_hosts``."""
    url = detail.get("url") or ""
    host = (urllib.parse.urlparse(url).hostname or "").casefold()
    if not host:
        return DENY, "no host in url " + repr(url[:80])
    methods = [str(m).upper() for m in (rule.get("methods") or [])]
    method = str(detail.get("method") or "GET").upper()
    if methods and method not in methods:
        return DENY, "method " + method + " not allowed"
    for allowed in rule.get("hosts") or []:
        allowed = str(allowed).casefold().lstrip(".")
        if host == allowed or host.endswith("." + allowed):
            return ALLOW, "host " + host + " matches " + allowed
    return DENY, "host " + host + " not in allowlist"


def _decide_fs(rule, detail):
    """Both ends of a move have to be inside the task's own tree."""
    roots = [_norm(r) for r in (rule.get("under") or [])]
    if not roots:
        return DENY, "no writable roots declared"
    for field in ("path", "dst"):
        target = detail.get(field)
        if not target:
            continue
        if not any(_within(_norm(target), root) for root in roots):
            return DENY, field + " " + str(target) + " is outside the task tree"
    return ALLOW, "inside " + ", ".join(roots)


def _decide_subject(rule, detail):
    """Everything else: one ``subject``, matched exactly or by containment.

    ``script.run`` is the family that matters here, and it is worth allowing
    rather than refusing on principle. A script is not a bypass: it runs in a
    real sandbox box, and **its own Requests come back through this same
    approver one at a time** -- an ``fs.write`` inside a script is gated
    exactly as an ``fs.write`` from a tool is. So granting ``script.run``
    hands over the *route*, not the authority, which is also the route the
    script-first hypothesis wants the agent to take.
    """
    subject = str(detail.get("subject") or "")
    if subject and subject in {str(s) for s in (rule.get("subjects") or [])}:
        return ALLOW, "subject " + subject
    roots = [_norm(r) for r in (rule.get("under") or [])]
    if subject and any(_within(_norm(subject), root) for root in roots):
        return ALLOW, "subject under " + ", ".join(roots)
    return DENY, "subject " + repr(subject) + " not allowed"


def _norm(path):
    """One spelling for a path, so containment is a string test again."""
    return posixpath.normpath(str(path).replace("\\", "/")).rstrip("/") or "/"


def _within(target, root):
    return target == root or target.startswith(root.rstrip("/") + "/")


class Approver:
    """Watches the stream, answers what it can justify, records everything.

    Runs on its own thread because the POST that raised the dialog is still
    open: the kernel is blocking that request until somebody answers, and the
    thread that submitted the turn cannot be the thread that answers it.
    """

    def __init__(self, client, manifest, ui=None, log=print, since=None):
        self.client = client
        self.manifest = manifest if isinstance(manifest, Manifest) \
            else Manifest(manifest)
        self.ui = dict(ui or {"policy": "canned",
                              "text": "Proceed with your best judgment."})
        self.log = log or (lambda *a, **k: None)
        self.decisions = []
        self.questions = []
        # Where this approver starts reading. Frames are history as well as
        # news, so an approver constructed mid-run would otherwise re-answer
        # every dialog the *bootstrap* raised -- posting resolves for
        # request ids that were settled minutes ago, and logging decisions it
        # never actually made.
        self._since = client.frames.mark() if since is None else since
        # Decisions go to the live log as well as to ``decisions``. The
        # in-memory list is only readable once the bundle is written, which
        # is after the task is over -- and *which* requests a mode refused,
        # and why, is the whole content of a lockdown-versus-yolo comparison.
        # Watching that happen is worth more than reading it afterwards.
        self._live = live.shared()
        self._seen = set()
        self._open = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------- lifecycle

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def stop(self, timeout=1.0):
        """Stop scanning, and wait for the last decision to be written down."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def outstanding(self):
        """How many dialogs are still waiting on us.

        A turn is not stalled while this is non-zero -- it is blocked on a
        person by design, which is exactly the case a naive stall timer would
        kill.
        """
        with self._lock:
            return len(self._open)

    # ------------------------------------------------------------------ loop

    def _loop(self):
        while not self._stop.is_set():
            for frame in self.client.frames.snapshot(self._since):
                kind = frame.get("kind")
                if kind == "approval":
                    self._handle(frame.get("payload") or {})
                elif kind == "approval_settled":
                    settled = (frame.get("payload") or {}).get("request_id")
                    with self._lock:
                        self._open.discard(settled)
            time.sleep(0.15)

    def _handle(self, payload):
        request_id = payload.get("id")
        if not request_id:
            return
        with self._lock:
            if request_id in self._seen:
                return
            self._seen.add(request_id)
            self._open.add(request_id)

        seen_at = time.time()
        detail = payload.get("detail")
        if detail is None:
            self._answer_question(payload, seen_at)
            return

        choice, why = self.manifest.decide(detail)
        choice = self._legal(choice, payload)

        # Written down *before* it is acted on, and mutated in place after.
        # A record appended after the resolve returns is a record that does
        # not exist while the kernel is already acting on the answer -- which
        # is both a race a reader can lose and an audit trail that quietly
        # omits any decision whose resolve failed or whose process died.
        record = {"request_id": request_id, "type": detail.get("type"),
                  "asker": detail.get("asker"), "detail": detail,
                  "choice": choice, "why": why, "seen_at": seen_at,
                  "resolve_status": None, "resolve_answer": None,
                  "latency_s": None}
        self.decisions.append(record)
        self.log("[manifest] " + str(detail.get("type")) + " "
                 + _subject_of(detail) + " -> " + choice + "  (" + why + ")")

        status, answer = self.client.post(
            "frontend.resolve", {"value": choice, "request_id": request_id})
        record["resolve_status"] = status
        record["resolve_answer"] = answer
        record["latency_s"] = round(time.time() - seen_at, 3)
        self._live.write("approver", "decision",
                         payload={"request_id": request_id,
                                  "type": detail.get("type"),
                                  "asker": detail.get("asker"),
                                  "subject": _subject_of(detail),
                                  "choice": choice, "why": why,
                                  "resolve_status": status,
                                  "latency_s": record["latency_s"]})
        with self._lock:
            self._open.discard(request_id)

    def _legal(self, choice, payload):
        """Never answer an option the dialog did not offer.

        And never answer an ``always:`` option even when it is offered: the
        grant it writes outlives the task that asked for it.
        """
        options = [str(v) for v in (payload.get("enum") or [])]
        if not options:
            return choice
        if choice in options:
            return choice
        for fallback in (DENY, "no", "false"):
            if fallback in options:
                return fallback
        return options[-1]

    def _answer_question(self, payload, seen_at):
        """A ``detail`` of ``None`` is not a permission gate.

        It is ``ui.ask``, or a tool asking the person something. Grant policy
        does not apply to it -- but ignoring it stalls the turn for the
        kernel's full 300 seconds, so it gets answered and *counted*. An
        unattended task that had to ask is a task the agent did not do on its
        own, which is a result worth seeing rather than an inconvenience to
        paper over.
        """
        request_id = payload.get("id")
        value = self._question_answer(payload)
        record = {"request_id": request_id, "title": payload.get("title"),
                  "body": payload.get("body"), "type": payload.get("type"),
                  "enum": payload.get("enum"), "answer": value,
                  "resolve_status": None, "resolve_answer": None,
                  "seen_at": seen_at, "latency_s": None}
        self.questions.append(record)          # recorded before acting
        self.log("[question] " + str(payload.get("title"))[:70]
                 + " -> " + repr(value))
        status, answer = self.client.post(
            "frontend.resolve", {"value": value, "request_id": request_id})
        record["resolve_status"] = status
        record["resolve_answer"] = answer
        record["latency_s"] = round(time.time() - seen_at, 3)
        self._live.write("approver", "question",
                         payload={"request_id": request_id,
                                  "title": payload.get("title"),
                                  "type": payload.get("type"),
                                  "answer": value,
                                  "resolve_status": status,
                                  "latency_s": record["latency_s"]})
        with self._lock:
            self._open.discard(request_id)

    def _question_answer(self, payload):
        policy = self.ui.get("policy", "canned")
        options = payload.get("enum") or []
        kind = payload.get("type") or "string"
        if options:
            default = payload.get("default")
            if policy == "decline":
                return default if default in options else options[-1]
            return default if default in options else options[0]
        if kind == "boolean":
            return policy != "decline"
        if kind in ("integer", "number"):
            return payload.get("default") if payload.get("default") is not None else 0
        if kind == "array":
            return []
        if kind == "object":
            return {}
        return "" if policy == "decline" else self.ui.get("text", "")


def _subject_of(detail):
    """The one field worth putting in a log line, per family."""
    for field in ("command", "url", "path", "subject"):
        if detail.get(field):
            return str(detail[field])[:90]
    return ""
