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

## Jobs: configuration in, data out

A **job** is a configuration — model, plugin profile, permission mode, task
selection, and a repeat count. Running one produces **trials**, one per task
per replicate, each joined back to the configuration that produced it.

```python
from harness_bench_api import HarnessBenchAPI, JobSpec

api = HarnessBenchAPI()
job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                       tasks={"difficulty": ["easy"]},
                       profile="bench", mode="yolo", repeats=3))
api.run(job.job_id)      # resumable
api.dataset()            # every run on disk -> SQLite
```

The same surface as a CLI, JSON on stdout:

```
python harness_bench_api.py new --model minimax/MiniMax-M3 --difficulty easy --repeats 3
python harness_bench_api.py run <job-id>
python harness_bench_api.py status <job-id>
python harness_bench_api.py export
```

`plan` spends nothing: it resolves the selection, writes
`results/harness-bench/jobs/<job_id>/job.json`, and stops. Only `run` starts
containers, and only with `--execute` (the default; `--dry-run` withholds it).

### A replicate is an ordinary run

Replicate *n* of job `J` is the run directory `J-r{n}`, in the layout that
already existed. That is why `--resume`, `view_harness_bench.py`,
`compare_harness_runs.py` and the exporter all work on it unchanged, and why
`trial_id` stays `run_id/task_id`.

The job file records which runs belong together. It never becomes a second
account of what happened: **trial state is always derived from the run
directories**, so `status` reads the same files the launcher wrote and there is
nothing to keep in sync.

### Pausing is the normal path

The 106-task suite will not fit in one sitting against a rate-limited
provider, so stopping is designed for rather than recovered from. The launcher
exits 75 on provider exhaustion and 130 on Ctrl+C; both leave the run
directory resumable. `run()` catches them, marks the job `paused` with the
reason, and returns — no exception, no partial-state cleanup. Calling `run()`
again resumes: finished replicates are skipped entirely, and an existing run
directory is continued with `--resume`, which carries the launcher's conflict
guard so a resumed job can never silently mix two configurations.

### Task selection

Difficulty is taken verbatim from each `task.yaml`, and it is **not** a tidy
three-bucket scheme. At the pinned revision:

| Difficulty | Tasks |
|---|---|
| `hard` | 42 |
| `medium` | 30 |
| `unspecified` | 24 |
| `easy` | 7 |
| `medium-hard` | 3 |

A selection written as "easy, medium, hard" covers 79 of 106 while looking
exhaustive. Selectors accept `ids`, `difficulty`, `class`, `all`, `pilot`,
`smoke`, and `exclude`; an unknown value raises rather than matching nothing,
because a typo that selects zero tasks produces a run that looks finished.

## Plugin profiles

Which store packages the agent has is a variable, not a constant, and
`profiles.json` holds it in two layers:

- **`seed`** — what `build_template.py` bakes into the image, by really
  installing from `origin/store` so the template carries its own provenance.
- **`runtime`** — the delta `entrypoint.py` applies when a container starts.
  This is what a job names, and it needs no rebuild.

```
--profile bench         the seed as built
--profile no-script     drops run_script and validate
--profile no-subagents  drops spawn/schedule_subagent
--profile lean          file and shell work only
```

Comparing plugin sets therefore costs a container start rather than two image
builds, which is what makes the comparison worth running.

**A failed install or removal kills the container**, and the task is recorded
as a harness error. The alternative — continuing with the seed's plugin set
while the job's manifest claims otherwise — produces a trial whose
configuration column is wrong, and that quietly corrupts every comparison
drawn from it afterwards.

After applying the delta the entrypoint writes `live/profile.json`: the tools
actually on disk, not the ones requested. That distinction is the point. A
removal naming a stem the seed never had would otherwise be reported as
applied, and the exporter raises `profile_mismatch` in `validity_flags` when
the effective profile disagrees with the requested one.

`run.json`'s `visible_tools` is now **derived** from the template manifest and
the profile delta. It used to be a module constant, which was harmless only
while every run had the same plugin set — precisely the assumption `--profile`
exists to break.

## Tokens and cost

All three token counts are the **provider's own**, lifted from the `usage`
block of its response. Nothing is tokenised anywhere in this stack: only the
provider knows how it serialised the chat template and the tool schemas, so its
number is the billable one and a local estimate would be a second opinion
nobody charges by. On the streaming path
`stream_options={"include_usage": True}` asks for a final chunk carrying the
same block; a provider that ignores it leaves the counts `None`.

| Column | Meaning |
|---|---|
| `input_tokens_billed` | Σ of each call's whole prompt. **Billed input, not context size** — each call re-sends the conversation, so this climbs across a turn. |
| `input_tokens_largest_call` | The biggest single prompt. *This* is the context-size question. |
| `cached_input_tokens` | The discounted **share of** billed input, never an addition to it. |
| `output_tokens` | Completion tokens. |
| `tokens_complete` | Whether every call reported both input and output. |

**Unknown is never zero.** A count the provider withheld stays `NULL`, and a
`NULL` price yields a `NULL` cost rather than a free-looking run. Prices live
in `models.json` and cost is computed at *export* time, so correcting a price
and re-exporting fixes every trial already on disk.

Output and cached counts require kernel commit `f72435fe` (with store commit
`95afb0bc`) or later. Before those, the kernel published only
`prompt_tokens`, so trials recorded earlier carry `output_tokens = NULL` and
`tokens_complete = 0`. `kernel_commit` is on every trial
row, so old and new trials are separable rather than silently averaged.

## The database

`results/harness-bench/harness_bench.sqlite`, rebuilt from run directories:

| Table | Holds |
|---|---|
| `jobs` | one row per job configuration |
| `runs` | one row per replicate, with kernel/store/benchmark commits |
| `trials` | one row per task per replicate: score, tokens, cost, timings |
| `messages` | the conversation transcript, per round |
| `driver_rounds` | per-round driver metrics and the *granted* security mode |
| `model_calls`, `tool_calls`, `approvals`, `oracle_checks` | the detail |
| `events` | raw event rows, only with `--with-events` |
| `source_runs` | fingerprints, so unchanged runs are skipped |

Run directories are the source of truth and the database is derived, so
re-exporting is always safe: a changed run has its rows deleted and rewritten,
never appended beside the old ones. A whole-corpus export also prunes runs and
jobs that are no longer on disk; a narrowed `--run` export never prunes.

Two views carry the headline numbers:

- **`task_reliability`** — per configuration per task: `trials`, `mean_score`,
  `min`/`max`, `pass_rate`. This is what repeats exist to produce.
- **`config_scores`** — the benchmark number, computed as
  **mean over tasks( mean over replicates )**. Averaging trials flat would let
  a task that happened to be repeated three times outweigh one run once, so the
  score would drift with the scheduling history rather than with the harness.
  Since the suite is run in pieces as usage allows, that history is arbitrary.

## Result artifacts

Each run contains:

- `run.json`: immutable benchmark/model/image/template identity;
- `summary.json`: current denominator-aware aggregate;
- `tasks/<id>/status.json`: task state and oracle score;
- `tasks/<id>/events.jsonl`: viewer-ready live event stream (see below);
- `tasks/<id>/official-results/`: official Harness-Bench result JSON;
- `tasks/<id>/sandboxes/`: final task workspace and per-round driver outputs;
- `tasks/<id>/harness.log` and `container.log`: diagnostic output;
- `tasks/<id>/live/llm_usage.jsonl`: one row per model call, with the
  provider's own token counts; and
- `tasks/<id>/live/profile.json`: the plugin set the container **actually**
  ran, written by the entrypoint after applying the job's profile delta.

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
