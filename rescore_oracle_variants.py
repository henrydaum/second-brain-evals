"""Dual-score tasks 012/013/014 under both oracle variants, from preserved workspaces.

Tasks 012, 013 and 014 all resolve ``ground_truth.json`` as
``task_dir = w.parent.parent``, which is the api-slug directory in every
configuration -- never the task directory. The read misses, ``gt`` silently
becomes ``{}``, and each oracle then re-weights itself:

* **012** scores a fixed ``0*0.25 + 1*0.35 + 1*0.40 = 0.75`` whatever the agent
  wrote, because one branch divides by an empty ``expected`` and falls to 0.0
  while the other two divide by empty lists and default to a full 1.0.
* **014** silently skips topic coverage entirely (``total_topics: 0``).
* **013** is unaffected -- its ``gt.get(...)`` defaults happen to duplicate
  ``ground_truth.json`` exactly. It is scored here anyway, to prove that.

Upstream is unfixed (Qihoo360/harness-bench PR #7 is open), so **the published
paper was produced with these bugs present**. A direct comparison therefore has
to be scored the same broken way. Our runs patched 012 only, so the headline
number needs 012 put *back*.

This writes both variants and never picks one:

``upstream``  -- the pinned oracle exactly as published. Paper-comparable.
``pr7``       -- ``task_dir`` rewritten to ``Path(__file__).resolve().parent``.
                 Research interest only; not comparable to published figures.

The oracle is a pure function of the final workspace, so this costs no agent
re-run and no API spend. Reads the read-only snapshot.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
BENCH = ROOT / "build" / "harness-bench-src"
SNAPSHOT = ROOT / "results" / "study-2026-08" / "raw-snapshot"
OUTDIR = ROOT / "results" / "study-2026-08" / "derived"

TASKS = ("012-doc-synthesis", "013-image-edit", "014-task-decomposition")

# Exact source text, so an upstream revision that fixes or moves the line fails
# loudly here instead of being silently "fixed" a second time.
REWRITE = ("task_dir = w.parent.parent", "task_dir = Path(__file__).resolve().parent")


def load_oracle(task_dir: Path, module_tag: str):
    path = task_dir / "oracle_grade.py"
    name = f"oracle_{module_tag}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load oracle at {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def staged_pr7(task_id: str, tmp: Path) -> Path:
    """Copy the task and apply the PR #7 fix to the copy."""
    dest = tmp / task_id
    shutil.copytree(BENCH / "tasks" / task_id, dest)
    oracle = dest / "oracle_grade.py"
    src = oracle.read_text(encoding="utf-8")
    before, after = REWRITE
    if before not in src:
        raise RuntimeError(
            f"{task_id}: expected {before!r} in oracle_grade.py but it is absent. "
            "The pinned benchmark revision changed; re-check this shim before trusting it.")
    oracle.write_text(src.replace(before, after), encoding="utf-8")
    if not (dest / "ground_truth.json").is_file():
        raise RuntimeError(f"{task_id}: staged copy has no ground_truth.json to find")
    return dest


def find_workspace(task_dir: Path) -> Path | None:
    for d in sorted(task_dir.glob("sandboxes/*/*/oc-bench-v2-*/workspace")):
        if d.is_dir():
            return d
    return None


def stored_outcome(task_dir: Path) -> Any:
    hits = sorted(task_dir.glob("official-results/*/*/*.json"))
    if not hits:
        return None
    blob = json.loads(hits[0].read_text(encoding="utf-8", errors="replace"))
    return ((blob.get("oracle_result") or {}).get("outcome_score"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--snapshot", default=str(SNAPSHOT))
    ap.add_argument("--out", default=str(OUTDIR))
    opts = ap.parse_args(argv)

    snap = Path(opts.snapshot).resolve()
    out = Path(opts.out).resolve()
    out.mkdir(parents=True, exist_ok=True)

    rows: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        upstream = {t: load_oracle(BENCH / "tasks" / t, f"up_{t.replace('-', '_')}") for t in TASKS}
        pr7 = {t: load_oracle(staged_pr7(t, tmp), f"pr7_{t.replace('-', '_')}") for t in TASKS}

        for rd in sorted(d for d in snap.iterdir() if d.is_dir()):
            for task_id in TASKS:
                tdir = rd / "tasks" / task_id
                row: dict[str, Any] = {"run_id": rd.name, "task_id": task_id,
                                       "stored_outcome_score": stored_outcome(tdir)}
                ws = find_workspace(tdir)
                if ws is None:
                    row["error"] = "no workspace"
                    rows.append(row)
                    continue
                for label, mods in (("upstream", upstream), ("pr7", pr7)):
                    try:
                        res = mods[task_id].score_workspace(ws)
                        row[f"{label}_score"] = res.get("outcome_score")
                        row[f"{label}_summary"] = res.get("summary")
                    except Exception as e:  # noqa: BLE001
                        row[f"{label}_score"] = None
                        row[f"{label}_error"] = f"{type(e).__name__}: {e}"
                rows.append(row)
                print(f"{rd.name[9:30]:22} {task_id:24} "
                      f"stored={row.get('stored_outcome_score')!s:>7} "
                      f"upstream={row.get('upstream_score')!s:>7} "
                      f"pr7={row.get('pr7_score')!s:>7}"
                      + (f"  ERR {row.get('upstream_error') or row.get('pr7_error')}"
                         if row.get("upstream_error") or row.get("pr7_error") else ""))

    dest = out / "oracle_variants.json"
    dest.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {dest}")

    # The two predictions this whole approach rests on. If either fails, the
    # re-score path is wrong and the numbers built on it cannot be trusted.
    u012 = [r.get("upstream_score") for r in rows if r["task_id"] == "012-doc-synthesis"]
    ok012 = all(s == 0.75 for s in u012 if s is not None)
    same013 = all(r.get("upstream_score") == r.get("pr7_score")
                  for r in rows if r["task_id"] == "013-image-edit")
    print(f"\nCHECK upstream 012 == 0.75 everywhere : {'PASS' if ok012 else 'FAIL'}  {u012}")
    print(f"CHECK 013 identical across variants   : {'PASS' if same013 else 'FAIL'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
