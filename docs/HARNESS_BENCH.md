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
`oracle_result.outcome_score` over all scheduled tasks. Harness failures,
interruptions, quota failures, and tasks not yet run contribute zero until they
are completed. This prevents exception filtering from inflating the score.

The release's process-quality grader and oracle image-quality LLM are skipped.
Therefore report results as:

> Harness-Bench deterministic completion, pinned revision, selected task list,
> model, Second Brain version/image, and security mode.

Do not describe this number as the paper's full combined Harness-Bench score.
The paper's published harness aggregates also average across multiple models;
they are not a same-model Second Brain baseline. A fair external comparison
requires running a reference harness on the identical tasks and model.

`compare_harness_runs.py` enforces an identical benchmark commit and ordered
task list, scores missing/failed tasks as zero, and reports the candidate's
paired mean delta plus task-level wins, ties, and losses. Model and security
mode identities remain visible in the report rather than being silently treated
as interchangeable.

## Result artifacts

Each run contains:

- `run.json`: immutable benchmark/model/image/template identity;
- `summary.json`: current denominator-aware aggregate;
- `tasks/<id>/status.json`: task state and oracle score;
- `tasks/<id>/events.jsonl`: viewer-ready live Second Brain and LLM events;
- `tasks/<id>/official-results/`: official Harness-Bench result JSON;
- `tasks/<id>/sandboxes/`: final task workspace and per-round driver outputs;
- `tasks/<id>/harness.log` and `container.log`: diagnostic output; and
- `tasks/<id>/live/`: the container-side telemetry copy.

The summary also totals model calls and provider-reported prompt tokens. Second
Brain's current completion event does not expose completion-token usage, so that
value remains explicitly `null` rather than being guessed or treated as zero.

## Cost and interruption behavior

Only `--execute` authorizes model-backed tasks. The preflight spends a minimal
request before Docker work. If provider quota, credits, or authentication fail,
the run stops before another task begins. Completed results are durable, and a
matching `--resume` continues later. Configuration mismatches on resume are
rejected so one score cannot silently combine different models, images, modes,
or benchmark revisions.

Ctrl+C marks the active task interrupted. Resume it later; already-complete tasks
are skipped. Prefer one integration task, then the smoke set, then the pilot.

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
