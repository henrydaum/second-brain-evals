"""The internal suite: the gate everything else waits behind.

Roughly a dozen hard tasks -- the ones that get hand-guided -- driven through
the same container and the same driver as every public benchmark. Nothing gets
published before this looks good, because an unattended benchmark run exposes
hand-guidance dependency mercilessly and leaderboard first impressions do not
rerun.

A task is a directory, and that is the whole format::

    tasks/<id>/task.json      prompt, manifest, ui policy, budgets
    tasks/<id>/fixtures/      files the container starts with
    tasks/<id>/check.py       ``check(bundle) -> {ok, score, notes}``

``check.py`` is loaded from its own directory and handed a
:class:`harness.bundle.Bundle`, so it asks about deliverables and evidence
rather than about paths. It runs on the host, after the container is gone,
against files on disk -- which means a scoring bug is fixed and re-run for
free instead of costing another sweep.
"""

from __future__ import annotations

import importlib.util
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
TASKS = os.path.join(HERE, "tasks")


class Adapter:
    """Reads the task directories and scores what comes back."""

    def __init__(self, root=TASKS):
        self.root = root

    def tasks(self):
        for task_id in sorted(os.listdir(self.root)):
            directory = os.path.join(self.root, task_id)
            spec_path = os.path.join(directory, "task.json")
            if not os.path.isfile(spec_path):
                continue
            with open(spec_path, "r", encoding="utf-8") as handle:
                spec = json.load(handle)
            spec.setdefault("id", task_id)
            spec["directory"] = directory
            fixtures = os.path.join(directory, "fixtures")
            spec["fixtures"] = fixtures if os.path.isdir(fixtures) else None
            yield spec

    def score(self, task, dest):
        """Run the task's own checker against the collected trial."""
        from harness.bundle import Bundle, fail

        checker = os.path.join(task["directory"], "check.py")
        if not os.path.isfile(checker):
            return fail("no check.py for " + str(task["id"]))
        bundle = Bundle(dest)
        if not bundle.result:
            return fail("no result.json -- the trial produced no bundle")
        return _load(checker, task["id"]).check(bundle)


def _load(path, task_id):
    """Import one check.py under a name that cannot collide with another."""
    spec = importlib.util.spec_from_file_location(
        "sbcheck_" + str(task_id), path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
