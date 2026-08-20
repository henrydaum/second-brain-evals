# Second Brain Evals

This repository measures Second Brain as an agent framework. Its first supported
benchmark is the pinned 106-task Harness-Bench release. Each task runs in a fresh
container, but every round of a multi-round task reconnects to the same Second
Brain session. This prevents memory leakage between trials without discarding the
state a task deliberately asks the agent to retain.

The default metric is **deterministic completion**: the mean official oracle
score across every scheduled task, with failed or missing trials counted as zero.
The costly LLM process grader and image-quality judge are deliberately disabled.
This makes the pilot affordable and repeatable, but it is not the paper's full
combined score and must be labeled accordingly.

## Setup

Requirements are Docker Desktop using Linux containers, Python 3.12+, the
general-purpose `second-brain:latest` image, and a Second Brain checkout next to
this repository (or `SECOND_BRAIN_REPO` set to its location).

```powershell
py -3.13 -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
python build_template.py --profile bench
python run_harness_bench.py --fetch-benchmark --validate
python run_harness_bench.py --build-image --self-test
```

The upstream benchmark is checked out under the ignored `build` directory and
must be exactly the revision recorded in
`evals/harness_bench/benchmark.lock.json`. That revision has no repository
license, so its source is not vendored here.

Create an ignored `bench.env`:

```dotenv
SB_LLM_API_KEY=...
SB_LLM_MODEL=minimax/MiniMax-M3
SB_LLM_ENDPOINT=https://api.minimax.io/v1
SB_LLM_BACKEND=LiteLLMService
SB_HTTP_TOKEN=a-local-random-token
```

## Run safely

Validation and self-tests make no model calls:

```powershell
python run_harness_bench.py --validate
python run_harness_bench.py --self-test
python audit_harness_bench.py --output results\harness-bench\corpus-audit.json
```

Paid execution always requires the explicit `--execute` switch. Begin with one
task, not the smoke set:

```powershell
python run_harness_bench.py --task 001-file --mode yolo --execute --run-id minimax-integration
```

After that integration task is reliable, run the two-task smoke or the
category-stratified eight-task pilot:

```powershell
python run_harness_bench.py --smoke --mode yolo --execute --run-id minimax-smoke
python run_harness_bench.py --pilot --mode yolo --execute --run-id minimax-pilot
```

The launcher performs a one-token provider preflight, stops before scheduling
another task when it detects quota or credit exhaustion, and writes each task as
it finishes. Resume the same configuration later without rerunning successes:

```powershell
python run_harness_bench.py --smoke --mode yolo --execute `
  --resume results\harness-bench\minimax-smoke
```

Add `--retry-failed` only when failed tasks should be attempted again. A run may
span multiple quota windows; it does not need to complete in one sitting.

## Watch the agent

Start the viewer in a second terminal after a run directory has been created:

```powershell
python view_harness_bench.py --run latest
```

The local UI at <http://127.0.0.1:8765> polls once per second and shows:

- streaming model output, appended rather than redrawn, so it does not scroll
  back to the top under you while the agent is talking;
- **approval decisions as they are made** — request type, subject, allow/deny,
  and the manifest's reason — which is the panel a lockdown-versus-yolo
  comparison is actually about;
- tool activity and errors, model calls, known prompt tokens, tool-call count,
  and a running allowed/denied tally;
- the tail of `harness.log` and `container.log`, so a task that died says why
  instead of sitting frozen at "running";
- a liveness badge driven by the age of the newest event, because a dead
  container otherwise looks identical to a thinking one.

It follows the running task as the run advances; clicking a task pins it. Each
poll transfers only the events since the last one, so cost stays proportional
to what happened rather than to how long the task has been going.

The underlying JSONL trace, official result, sandbox, and logs remain in
`results/harness-bench/<run-id>/tasks/<task-id>/`.

## Security modes

- `yolo` asks Second Brain to auto-approve tool operations.
- `lockdown` denies tool operations at the kernel security boundary.
- `mediated` uses an explicit benchmark policy that permits workspace file work
  and Second Brain's scripting tool while retaining mediation elsewhere.

The benchmark keeps the Essentials bundle except for the interactive-only
Telegram frontend, `ask_question`, and `show_files`. All other Essentials tools
remain available. The exact profile and store commit are recorded in the
template manifest and each run.

A mode is a standing answer to an approval dialog and nothing else, so a
`lockdown`-versus-`yolo` delta measures the cost of refusing shell, scripting,
and network — **not** file work, which is free in both because the task
workspace sits in the agent's own scratch tree. See
[docs/HARNESS_BENCH.md](docs/HARNESS_BENCH.md#security-modes-and-what-a-mode-comparison-actually-measures)
before reporting one; the caveat changes how the number should be read.

Use identical task selections, model configuration, timeouts, and benchmark
revision when comparing modes or frameworks. MiniMax M3 is useful for cheap
harness engineering, but a publishable framework comparison also needs the same
established model run through Second Brain and a reference harness.

Compare two completed or interrupted runs with compatibility checks and paired
task deltas:

```powershell
python compare_harness_runs.py minimax-yolo minimax-lockdown `
  --output results\harness-bench\yolo-vs-lockdown.json
```

See [docs/HARNESS_BENCH.md](docs/HARNESS_BENCH.md) for architecture, artifacts,
scoring caveats, and troubleshooting.

## The console

One page for the whole loop:

```powershell
python bench_console.py
```

Opens `http://127.0.0.1:8765` with three tabs:

- **Jobs** — a form to configure a job (model, plugin profile, permission
  mode, repeats, task selection) and a list of the ones already planned.
  *Preview selection* resolves the task list before committing to anything;
  planning spends nothing. Unfinished jobs get a **resume** button.
- **Live** — the existing viewer, scoped to the replicate in flight, with a
  job-level status line. It returns to Jobs a few seconds after the job ends.
- **Data** — the exported database, with saved queries for `config_scores`,
  `task_reliability`, per-profile cost, failed checks and transcripts, plus a
  free SQL box. Read-only: the file is opened `mode=ro`.

The dataset is re-exported automatically when a job finishes, so Data is
current the moment you land back on it.

**One job at a time.** Each task starts a container and spends quota, so a
second concurrent job would contend for both and make the timings meaningless.
The runner refuses rather than queues — a queued job waits invisibly, and the
point of the page is seeing what is happening now.

Binds to 127.0.0.1 only. Everything it does is also available from the CLI
below.

## Jobs: configuration in, data out

A **job** is a configuration — model, plugin profile, permission mode, task
selection, repeat count. Running one produces **trials**, one per task per
replicate, each joined back to the configuration that produced it.

```python
from harness_bench_api import HarnessBenchAPI, JobSpec

api = HarnessBenchAPI()
job = api.plan(JobSpec(model="minimax/MiniMax-M3",
                       tasks={"difficulty": ["easy"]},
                       profile="bench", mode="yolo", repeats=3))
api.run(job.job_id)      # resumable; a usage limit pauses rather than fails
api.dataset()            # every run on disk -> SQLite
```

The same surface as a CLI, JSON on stdout:

```powershell
python harness_bench_api.py new --model minimax/MiniMax-M3 --difficulty easy --repeats 3
python harness_bench_api.py run JOB_ID
python harness_bench_api.py status JOB_ID
python harness_bench_api.py export
```

`plan` spends nothing — it resolves the task list and writes `job.json`.
Replicate *n* is the ordinary run directory `JOB_ID-r{n}`, so `--resume`, the
viewer and `compare_harness_runs.py` all keep working on it. Calling `run`
again after a pause skips finished replicates and resumes the rest; the
launcher's conflict guard refuses to resume under a changed configuration.

### Varying the plugin set

`profiles.json` names the store packages a job installs. `seed` profiles are
baked into the image by `build_template.py`; `runtime` profiles are deltas the
entrypoint applies at container start, so comparing plugin sets costs a
container start rather than two image builds.

```powershell
python run_harness_bench.py --profile no-script --difficulty easy --execute
```

Shipped runtime profiles: `bench` (the seed as built), `no-script` (drops
`run_script` and `validate`), `no-validate`, `no-subagents`, and `lean`. A
failed install or removal kills the container rather than running the wrong
configuration under the right label, and `live/profile.json` records the tools
that were actually present.

### Tokens, cost, and the database

Token counts are the provider's own, from the `usage` block of its response —
nothing here tokenises anything. `input_tokens_billed` sums each call's whole
prompt, which is what you are charged for and **not** the context size
(`input_tokens_largest_call` answers that). `cached_input_tokens` is the
discounted share *of* the input, never an addition to it. A count the provider
withheld stays `NULL`, and a missing price yields a `NULL` cost rather than a
free-looking run.

Prices live in `models.json` and cost is computed at export time, so correcting
a price and re-exporting fixes every trial already on disk.

`results/harness-bench/harness_bench.sqlite` holds `jobs`, `runs`, `trials`,
`messages` (the transcript), `driver_rounds`, `model_calls`, `tool_calls`,
`approvals` and `oracle_checks`, plus the `task_reliability` and
`config_scores` views. `config_scores` averages per task before averaging
across tasks, so unevenly repeated tasks cannot skew the headline number.

Run directories are the source of truth and the database is derived, so
re-exporting is always safe — unchanged runs are skipped by fingerprint:

```powershell
python export_harness_bench_data.py                    # refresh everything
python export_harness_bench_data.py --run RUN_ID       # just one run
python export_harness_bench_data.py --with-events      # include raw events
```
