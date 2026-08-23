"""Notice when the agent rewrote its own inputs, and say so while it can still care.

The first full 106-task run failed 23 of the 31 integrity checks it was
offered -- a 74% violation rate -- and the oracle detail is never ambiguous
about what happened: ``cartState.test.js must remain unchanged``,
``ordercalc/pricing.py must not change``, ``test_file_hash_unchanged:
expected 52e242ab..., actual bac01bef...``. The agent edits the artifact it is
being graded against, usually to make a failing test stop failing.

Three things are worth being precise about, because each rules out an easier
fix that does not work:

**It is not a permissions problem, so a deny list is the wrong shape.** The
tasks *say* which files are fixed -- "the schema and policy files must not be
edited" is in the prompt the agent read. And the inverse case is just as
common: 011-code-debug requires editing ``in/buggy_code.py`` in place, 043
requires editing ``in/db/migration.sql``. A blanket read-only rule over the
input tree would break as many tasks as it saved. What is missing is not
authority but *notice*.

**It cannot live in the tool, and in YOLO it cannot live in the manifest
either.** ``tool_edit_file`` deliberately holds no policy -- the kernel
classifies every effect on the way out -- and in YOLO the kernel raises no
dialog, so ``approver.Manifest`` never sees an ``fs.write`` to rule on. The
one layer that observes every mode equally is the driver, after the turn.

**Created files are not drift.** Deliverables are the point of the task. Only
a path that existed at baseline and then changed or vanished is evidence that
something the task supplied was overwritten.

So this module compares two hashes of the workspace and reports the
difference. ``run_task`` hands that report back to the agent as one more turn,
which is the harness doing the thing the benchmark's own analysis says
separates good harnesses from bad: keeping execution state legible, so a
failure at the boundary between intention and completion is caught before it
becomes an oracle failure rather than after.
"""

from __future__ import annotations

from driver import collect

#: Written by the agent's own runtime rather than supplied by the task, so a
#: change here is bookkeeping and not a rewritten input. Matched against any
#: segment of the relative path.
NOISE_SEGMENTS = frozenset({
    "__pycache__", ".pytest_cache", ".git", "node_modules", ".ruff_cache",
    ".mypy_cache", ".ipynb_checkpoints", ".venv", "venv", ".tox",
})

#: Same reasoning, by suffix.
NOISE_SUFFIXES = (".pyc", ".pyo", ".log", ".sqlite-journal", ".sqlite-wal",
                  ".sqlite-shm")

#: A correction turn quoting two hundred paths teaches nothing and costs a
#: large prompt. The count is always reported in full; only the listing is cut.
LIST_CAP = 40


def _is_noise(rel: str) -> bool:
    if rel.endswith(NOISE_SUFFIXES):
        return True
    return any(part in NOISE_SEGMENTS for part in rel.split("/"))


def baseline(root):
    """``{relative path: sha256}`` for everything the task supplied.

    Built from :func:`collect.workdir` rather than a second walk, so the
    baseline and the post-run comparison cannot disagree about what counts as
    a file or how it is hashed. A file too large to hash records ``None`` and
    is compared on size instead, which is weaker but still catches truncation
    and wholesale replacement.
    """
    snapshot = collect.workdir(root)
    out = {}
    for entry in snapshot.get("files") or []:
        rel = entry.get("path")
        if not rel or _is_noise(rel):
            continue
        out[rel] = entry.get("sha256") or ("size:%s" % entry.get("bytes"))
    return out


def drift(before, root):
    """What happened to the baseline files, as ``{modified, deleted, created}``.

    ``created`` is returned for the record -- it is what the task asked for,
    and a run that created nothing is its own kind of failure worth seeing --
    but only ``modified`` and ``deleted`` are drift.
    """
    after = baseline(root)
    modified, deleted = [], []
    for rel, digest in before.items():
        if rel not in after:
            deleted.append(rel)
        elif after[rel] != digest:
            modified.append(rel)
    created = [rel for rel in after if rel not in before]
    return {"modified": sorted(modified), "deleted": sorted(deleted),
            "created": sorted(created)}


def violated(report) -> bool:
    """Did anything the task supplied get rewritten or removed?"""
    return bool(report.get("modified") or report.get("deleted"))


def summarize(report) -> dict:
    """The countable form, for ``result.json`` and the telemetry export."""
    return {
        "modified_count": len(report.get("modified") or []),
        "deleted_count": len(report.get("deleted") or []),
        "created_count": len(report.get("created") or []),
        "modified": (report.get("modified") or [])[:LIST_CAP],
        "deleted": (report.get("deleted") or [])[:LIST_CAP],
        "clean": not violated(report),
    }


def committed_nothing(report) -> bool:
    """Did the turn end without a single new file anywhere in the workspace?

    The benchmark's own failure taxonomy calls this *artifact commitment* --
    "plausible reasoning without committing required outputs" -- and puts it
    at 11.1% of failed trajectories. In the first full run it is the most
    expensive thing the agent does: the 13 trials that finished with an empty
    ``out/`` averaged 0.446 against 0.812 for those with two files or more.

    The trigger is deliberately the narrowest one that still catches it.
    *Empty* ``out/`` is far too broad -- 043-db-migration-safety and
    045-dependency-upgrade-compat both scored 0.93+ with nothing in ``out/``,
    because their deliverable is an edit to a supplied file. **No new file
    anywhere** is the honest signal, and in the first full run it separates
    090 and 052 -- which made zero ``edit_file`` calls, wrote a long analysis
    as their last message, and scored 0.05 and 0.06 -- from every trial that
    actually produced something. A directory is not a file: both of those
    ended on ``mkdir``, which is exactly the shape of stopping one step early.
    """
    return not (report.get("created") or [])


#: Said once, to an agent that reasoned its way to the end of a turn without
#: writing anything down. Phrased so that a task genuinely needing no file can
#: cost one cheap turn and say so, rather than being pushed into inventing an
#: artifact it was never asked for.
COMMITMENT_PROMPT = (
    "Your turn ended without creating a single new file in the workspace.\n\n"
    "If this task asked for deliverables -- a report, a CSV, a JSON summary, "
    "anything under `out/` -- they do not exist yet, and analysis in your "
    "reply does not count: the grader reads the workspace, not the "
    "conversation. Re-read what the task asked you to produce, and write it "
    "now, at exactly the paths it named.\n\n"
    "If the task genuinely required no new file, say so in one line and stop."
)


def correction_prompt(report) -> str:
    """What to say to an agent that has just overwritten its own inputs.

    Deliberately *not* an instruction to revert. Editing a supplied file in
    place is the stated deliverable of several tasks, so a harness that
    ordered a blanket restore would break them. The turn states the fact and
    hands back the judgment, which is the only version of this that is honest
    about not knowing which of the two cases it is looking at.

    It also names the failure mode the benchmark punishes most quietly --
    editing a test so it stops failing -- because that is the one the agent
    reliably does not recognise as a violation while it is doing it.
    """
    changed = list(report.get("modified") or [])
    gone = list(report.get("deleted") or [])
    lines = ["Workspace integrity check, before your work is graded.",
             "",
             "These files were supplied with the task and are no longer as "
             "they were given to you:"]
    for rel in changed[:LIST_CAP]:
        lines.append(f"  modified: {rel}")
    for rel in gone[:LIST_CAP]:
        lines.append(f"  deleted:  {rel}")
    extra = (len(changed) + len(gone)) - len(changed[:LIST_CAP]) - len(gone[:LIST_CAP])
    if extra > 0:
        lines.append(f"  ... and {extra} more")
    lines += [
        "",
        "Some tasks ask you to edit a supplied file in place; if that is what "
        "the task asked for, this is correct and you should leave it alone.",
        "",
        "But if the task said any of these must remain unchanged -- fixtures, "
        "input data, existing tests, source files you were told not to touch "
        "-- restore them to their original contents now. Rewriting a test so "
        "that it passes does not count as fixing the code, and is scored as a "
        "failure even when everything else is right.",
        "",
        "Restore what should not have changed, leave the rest, then confirm "
        "in one line which files you restored and which you kept.",
    ]
    return "\n".join(lines)
