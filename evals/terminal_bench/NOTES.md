# Terminal-Bench 2.x (Harbor)

**Track:** popularity. **Status:** first target. **Why first:** the expensive
part — a working Linux container — is done, and Harbor supplies the task
orchestration, containerisation and verification. The adapter is thin.

## What it is

89 containerised terminal tasks, run by the [Harbor](https://www.harborframework.com)
framework. The agent runs *inside* the task container, is handed an
`instruction.md`, works the shell, and a verifier checks the end state
afterwards. The leaderboard is the one everyone quotes (Claude Code 92.1,
Codex CLI 77.3).

## What it demands

- A Python adapter class, passed as `--agent-import-path "module:Class"`.
- The agent installed into the task container; it may spawn background
  processes, which is what lets us run the app and drive it over loopback.
- API keys via environment variables.
- **Minimum 5 trials per task** (`-k 5`) for a leaderboard submission.
- A PR to the harbor-framework submissions dataset with `metadata.yaml`
  (agent name, URL, org, model info) plus the trial logs. A bot validates it.
- Two rules: the agent may **not** access the tbench site or repo during a run
  (anti-reward-hacking), and you may **not** modify timeouts or resources.

## Adapter shape

The adapter installs Second Brain into the task container, starts it with the
HTTP frontend, and runs the shared driver against loopback:

1. install the image contents (or run the task inside our image if Harbor
   allows a custom base — check this first, it removes an install step)
2. `entrypoint.py`-equivalent: write config from env, start `main.py`
3. driver: open stream, submit `/locations`, submit the instruction
4. manifest: `proc.run` **any** — the container is the boundary here, and that
   is the configuration every competitor runs, so it is the fair one
5. stop on `typing: false`, let Harbor's verifier read the container

Reference implementation to crib from:
[badlogic/pi-terminal-bench](https://github.com/badlogic/pi-terminal-bench) —
Pi's own Harbor adapter.

## Expectations

This is Second Brain's weakest domain: coding-native harnesses will beat it.
A respectable mid-table score is the goal, and its purpose is to make the
security claims from Boundary-Bench and AgentDojo read as "capable system that
is also safe" rather than "safe because it cannot do anything".

## Open questions

- Can Harbor use a custom agent base image, or must the agent install itself
  into their task image? Decides whether the adapter installs or just runs.
- Is `pip install litellm` inside a task container acceptable per-trial cost,
  or should the model call go out through a pre-baked layer?
