"""Read a collected trial back, so a checker asks questions instead of paths.

A checker should read like the claim it is testing -- "did the table get the
row", "was anything sent anywhere" -- and not like a tour of a directory
layout. Everything here is derived from files the driver already wrote, so a
bundle can be re-scored months later without a container, which is the point
of writing the frames down verbatim in the first place.
"""

from __future__ import annotations

import json
import os


class Bundle:
    """One trial as it came home: deliverables, transcript, effects, dialogs."""

    def __init__(self, root):
        self.root = str(root)
        self.result = self._json("_result/result.json") or {}
        self.approvals = self._json("_result/approvals.json") or []
        self.questions = self._json("_result/questions.json") or []
        self.transcript = self._json("_result/transcript.json") or {}
        self.ledger = self._json("_result/ledger.json") or {}
        self.files = self._json("_result/files.json") or {}

    # ------------------------------------------------------------- the reply

    @property
    def outcome(self):
        return self.result.get("outcome") or {}

    @property
    def metrics(self):
        return self.result.get("metrics") or {}

    @property
    def final_text(self):
        return str(self.outcome.get("final_text") or "")

    @property
    def drove_cleanly(self):
        """The *drive* finished -- not that the agent did the task.

        Keeping these apart is what stops a timeout being scored as a wrong
        answer, and a wrong answer being blamed on the harness.
        """
        return bool(self.outcome.get("ok"))

    # ------------------------------------------------------- deliverables

    def path(self, relative):
        return os.path.join(self.root, relative.replace("/", os.sep))

    def exists(self, relative):
        return os.path.exists(self.path(relative))

    def read(self, relative, default=""):
        try:
            with open(self.path(relative), "r", encoding="utf-8",
                      errors="replace") as handle:
                return handle.read()
        except OSError:
            return default

    # ------------------------------------------------------------- evidence

    def decisions(self, type=None, choice=None):
        """Dialogs the manifest answered, filtered the way a claim is."""
        rows = self.approvals
        if type is not None:
            rows = [r for r in rows if r.get("type") == type]
        if choice is not None:
            rows = [r for r in rows if r.get("choice") == choice]
        return rows

    def attempted(self, type):
        """Did the agent even *try* this? A denial only counts if it did."""
        return bool(self.decisions(type=type))

    def allowed_any(self, type):
        return bool(self.decisions(type=type, choice="allow"))

    def ledger_rows(self, action_type=None):
        rows = self.ledger.get("rows") or []
        if action_type is not None:
            rows = [r for r in rows if r.get("action_type") == action_type]
        return rows

    def tool_calls(self, name=None):
        tools = self.metrics.get("tools") or {}
        return tools.get(name, 0) if name else sum(tools.values())

    def said(self, needle):
        """Did the reply mention this, case-insensitively?"""
        return needle.lower() in self.final_text.lower()

    def _json(self, relative):
        try:
            with open(self.path(relative), "r", encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None


def ok(notes="", score=1.0, **extra):
    return dict({"ok": True, "score": score, "notes": notes}, **extra)


def fail(notes, score=0.0, **extra):
    return dict({"ok": False, "score": score, "notes": notes}, **extra)
