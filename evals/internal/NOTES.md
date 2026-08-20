# Internal suite

**Track:** the gate. **Status:** build first. Nothing gets published before
this looks good.

## What it is

Roughly a dozen hard tasks — the ones Henry finds himself hand-guiding the
agent through — driven through the same container and the same driver as every
public benchmark. Programmatic checks where possible (did the file exist, did
the table get the row, did the plugin validate), an LLM judge where not.

## Why it exists

Second Brain has 109 test files pinning kernel correctness and, until this,
nothing measuring agent competence. So every usefulness lever — prompt edits,
model choice, a goal doorman, script-first guidance — was being tuned blind: a
change that helps the task you tried it on and hurts five others is invisible.

It also converts "DeepSeek V4 Pro feels better than MiniMax M3" into a number,
which is the cost question and the model-selection question at once.

## Why it comes before the public benchmarks

An unattended benchmark run will expose hand-guidance dependency mercilessly.
The usefulness work — goal doorman, script-first prompting, failure-text audit,
recipe distillation — should land before any number is published, and this is
the instrument that says when it has.

Leaderboard first impressions do not rerun.

## Task shape

Each task is a directory:

```
task.json      prompt, manifest, budgets, done-condition
fixtures/      files the container starts with
check.py       programmatic assertions against the result bundle
```

The result bundle is what the shared driver collects: final text, transcript
(`conv.read`), effects (`ledger.read`), approvals with their decisions, and
the task directory's end state.

## Two things worth measuring beyond pass/fail

- **Approval count per task.** A task solved with fewer dialogs is a task the
  agent understood; a spike in dialogs is usually the agent flailing at shell
  when a script would have done.
- **Tool-call count against script use.** The script-first hypothesis says
  hard tasks should collapse into one `script.run` rather than fifteen tool
  round-trips. That is a measurable claim, and this is where it gets measured.
