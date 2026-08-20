# Harness-Bench integration

## Execution boundary

The benchmark runner and Second Brain execute in the same task container. This
is necessary because Harness-Bench hooks may launch localhost services and its
oracles inspect the task filesystem. A sidecar would give the agent a different
filesystem and network namespace and invalidate the result.

For every scheduled task the launcher:

1. stages only the pinned runner, grading code, selected task, and generated
   generic-CLI configuration;
2. starts a pristine container derived from `second-brain:latest`;
3. invokes Second Brain once per official prompt round;
4. reconnects the existing session on later rounds of the same task;
5. runs the official deterministic oracle; and
6. retains results, sandbox contents, logs, and a live event trace.

The container is then removed. No task receives another task's conversation or
workspace state.

Official sandboxes live under Second Brain's ephemeral data workspace. This is
not cosmetic: `run_command` confines its working directory to the application
and data roots. Keeping tasks under `/work` allowed absolute file tools but
silently prevented shell-based builds, tests, Git, SQLite, archives, and local
HTTP work. The data tree is fresh per task and collected before removal.

The `bundle_essentials` catalogue is exposed except for three interactive-only
components that cannot help an unattended run: Telegram, `ask_question`, and
`show_files`. The installed bundle, explicit exclusions, visible tool names,
and store commit are recorded.

## Pilot selection

`evals/harness_bench/pilot.json` defines a two-task smoke set and an eight-task
pilot with one deterministic-oracle task from each published workflow class.
The pilot contains five easy, two medium, and one hard task plus a multi-round
replanning task. Vision tasks whose meaningful quality score requires another
LLM are not part of this cost-controlled selection. The separate smoke set is
intentionally tiny: file counting followed by two-round session memory.

## Scoring

`summary.json` reports `completion_score`, the arithmetic mean of official
`oracle_result.outcome_score` over all scheduled tasks. Every scheduled task
contributes exactly one score, and `run_harness_bench.task_score` is the single
definition of what that score is:

- a complete run with a numeric oracle score contributes it;
- **everything else contributes zero** — harness failures, interruptions, quota
  failures, tasks not yet run, and a *complete* run whose oracle produced no
  number at all.

That last case is called out because it is the one that hides. An unscored
completion dropped from the list would shrink the divisor rather than the
score, which is exactly the exception filtering the denominator exists to
prevent. `summary.json` reports it separately as
`completed_without_oracle_score`, so "the agent failed three tasks" stays
distinguishable from "the oracle answered nothing three times".

A provider complaint in the logs no longer outranks a result. Clients log
`rate_limit_error` on the way to a successful retry, so a task that produced an
official result is scored and the complaint is retained beside it as
`provider_warning`; only a task with *no* result is recorded as
`provider_unavailable` and stops the run.

The release's process-quality grader and oracle image-quality LLM are skipped.
Therefore report results as:

> Harness-Bench deterministic completion, pinned revision, selected task list,
> model, Second Brain version/image, and security mode.

Do not describe this number as the paper's full combined Harness-Bench score.
The paper's published harness aggregates also average across multiple models;
they are not a same-model Second Brain baseline. A fair external comparison
requires running a reference harness on the identical tasks and model.

`compare_harness_runs.py` enforces an identical benchmark commit and ordered
task list, scores missing/failed tasks as zero **through the same
`task_score`**, and reports the candidate's paired mean delta plus task-level
wins, ties, and losses. Sharing the scorer is what keeps a report internally
consistent: two definitions of what an unfinished task is worth produce
per-task deltas that do not reconcile with the aggregate printed beside them,
and neither number looks wrong on its own. Model and security mode identities
remain visible in the report rather than being silently treated as
interchangeable.

## Security modes, and what a mode comparison actually measures

`yolo` and `lockdown` are Second Brain's own kernel session modes, set with
`/mode` and scoped to the conversation. `mediated` keeps the session in `ask`
and answers each dialog from the benchmark manifest in
`evals/harness_bench/drive_round.py`.

**A mode is a standing answer to an approval dialog, and nothing else.** That
sentence is the kernel's design (`runtime/security_modes.py`), and it is the
whole of what a `lockdown`-versus-`yolo` delta can measure: a request that
never raised a dialog has no answer for a mode to stand in for, so both modes
treat it identically.

### The workspace is a trusted scratch root, and that is deliberate

Task sandboxes live under `/data/Second Brain/workspace/harnessbench-sandboxes`
(`CONTAINER_WORK_ROOT`). That location is required — `run_command` accepts a
`cwd` only under the application or data roots, so a workspace outside them
would leave the agent's shell unable to reach its own task, and we would be
measuring a benchmark wiring bug rather than a harness.

The consequence has to be stated plainly, because it shapes every mode
number this repository produces. `DATA_DIR/workspace` is the agent's
authoring tree, which `sandbox/policy.py` treats as a **scratch root**. Writes
there are classified `SAFE`, they raise no dialog, and therefore:

- **File work in the task workspace behaves identically under `lockdown` and
  `yolo`.** `fs.write`, `fs.write_bytes`, `fs.move` and `fs.delete` inside the
  workspace are free in both. Lockdown does not refuse them because there was
  never a dialog to refuse.
- The `fs.*` rules in the `mediated` manifest are inert for the same reason.
  They are kept because they document intent and would bind if the workspace
  ever moved, not because they fire today.

So a mode comparison over this suite measures the cost of refusing
**`proc.run` (non-read-only shell), `script.run`, `net.http`, and
`secret.reveal`** — the families in `sandbox/policy.py`'s `CONSEQUENTIAL` set.
It does **not** measure "how much capability the security boundary costs" in
general, and a published delta must say which of the two it is.

Two further caveats worth carrying with the number:

- Read-only shell is classified `SAFE` by `sandbox/shell.py`, so `ls`, `cat`
  and `git status` cost nothing in any mode. An approval count is already a
  count of consequential acts.
- `net_allowed_hosts` is empty in the benchmark template, so the tasks that
  stand up a local HTTP service raise a `net.http` dialog. Those are approved
  under `yolo`, refused under `lockdown`, and — because the manifest declares
  no `net.http` rule — refused under `mediated` too. Expect that handful of
  tasks to dominate the mode delta, and report them separately.

### Reading a comparison

`compare_harness_runs.py` deliberately does not require the two runs to share
a mode — comparing modes is the point — and it prints both identities in the
report rather than treating them as interchangeable. It scores missing,
failed, and unscored tasks as zero through the same `task_score` the launcher
uses, so per-task deltas always reconcile with the aggregate beside them.

Group the per-task rows by whether a task needs shell, scripting, or the
network before drawing any conclusion from the mean. A suite-wide average over
a task set that is mostly file manipulation will report a small delta for a
reason that has nothing to do with how good the security system is.

## Result artifacts

Each run contains:

- `run.json`: immutable benchmark/model/image/template identity;
- `summary.json`: current denominator-aware aggregate;
- `tasks/<id>/status.json`: task state and oracle score;
- `tasks/<id>/events.jsonl`: viewer-ready live event stream (see below);
- `tasks/<id>/official-results/`: official Harness-Bench result JSON;
- `tasks/<id>/sandboxes/`: final task workspace and per-round driver outputs;
- `tasks/<id>/harness.log` and `container.log`: diagnostic output; and
- `tasks/<id>/live/`: the container-side telemetry copy.

Each attempt **replaces** the previous attempt's `official-results/`,
`sandboxes/` and `live/` rather than merging into it. `docker cp` nests its
source into a destination that already exists, so a `--retry-failed` pass would
otherwise leave two results for one task in the tree — and
`find_official_result` refuses to guess between them, recording a passing retry
as a harness error. A retry's evidence is the retry's, never a merge of two.

### The live event stream

`events.jsonl` is one JSON object per line, written by three producers and read
by the viewer while the run is in flight:

| `source` | `kind` | Carries |
|---|---|---|
| `second_brain` | `frame` | one raw HTTP-protocol frame under `frame` |
| `approver` | `decision` | a permission gate: `type`, `subject`, `choice`, `why` |
| `approver` | `question` | a non-gate question and the answer given |
| `llm` | `llm_call` | the kernel's per-call usage telemetry |
| `harness` | `task_result` | the final status, appended by the launcher |

The `approver` rows are the interesting ones for a mode study: they are the
only record of *which* requests a mode refused and on what grounds, and
without them that evidence is unreadable until the task is over and the bundle
is written.

Two of those producers are separate processes writing to one file, so both
take an exclusive `flock` per line (`driver/live.py`, `harness/eval_telemetry.py`).
`O_APPEND` is atomic only below `PIPE_BUF`; a long stream delta or a fat usage
payload exceeds it, and an interleaved line fails `json.loads` and is skipped
by every reader — losing an event with no error raised anywhere.

The summary also totals model calls and provider-reported prompt tokens. Second
Brain's current completion event does not expose completion-token usage, so that
value remains explicitly `null` rather than being guessed or treated as zero.
Upstream's own `usage_summary` is empty for these runs: Second Brain talks to
its configured endpoint directly rather than through Harness-Bench's usage
proxy, so our telemetry is the token record, not theirs.

## Cost and interruption behavior

Only `--execute` authorizes model-backed tasks. The preflight spends a minimal
request before Docker work. If provider quota, credits, or authentication fail,
the run stops before another task begins. Completed results are durable, and a
matching `--resume` continues later. Configuration mismatches on resume are
rejected so one score cannot silently combine different models, images, modes,
or benchmark revisions.

Ctrl+C marks the active task interrupted **and stops the run**, exiting 130
with the resume command printed. The active container is still torn down and
its partial evidence still collected. Resume later with `--resume`;
already-complete tasks are skipped. Prefer one integration task, then the smoke
set, then the pilot.

A task's `--wall-seconds` is its official timeout minus
`DRIVER_COLLECT_MARGIN_S`, and the margin is not slack. Upstream's `run-task`
path does not catch a subprocess timeout: it propagates, no result file is
written, and the oracle never runs — so a task the agent had substantially
finished scores a hard zero instead of the partial credit its workspace had
earned. The margin has to cover everything the driver does after the turn
ends: paging the transcript back out of `conv.read`, reading the ledger, and
hashing every file in the workspace.

## Troubleshooting

- Run `python run_harness_bench.py --validate` to verify the exact 106-task pin.
- Run `python run_harness_bench.py --self-test` to check Docker boot, session
  reuse, security-mode transitions, the official runner, and an oracle without
  contacting a model provider.
- If the viewer has no run, start paid execution first or pass an existing run
  directory with `--run`.
- Inspect `status.json`, then `harness.log`, `container.log`, and the official
  result. Provider failures are labeled separately from task failures.
- Never place API keys in committed configuration. `bench.env` is ignored and is
  passed directly to Docker at runtime.
