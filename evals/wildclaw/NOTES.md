# WildClawBench

**Track:** popularity — the head-to-head. **Status:** last, deliberately.

## What it is

60 in-the-wild tasks across six domains
([repo](https://github.com/internlm/WildClawBench), InternLM):

| Domain | Tasks |
|---|---|
| Productivity flow | 10 |
| Code intelligence | 12 |
| Social interaction | 6 |
| Search & retrieval | 11 |
| Creative synthesis | 11 |
| **Safety alignment** | **10** |

It publishes two leaderboards: a model leaderboard (models inside one harness)
and a **harness comparison running the same tasks under OpenClaw, Claude Code,
Codex CLI and Hermes Agent**. That second one is the direct comparison against
the competitor set, on Second Brain's home domain rather than on coding.

Scoring is 0.00–1.00 per metric by a judge model (GPT-5.4 by default), with
ground truth injected only *after* execution to prevent leakage. Time and cost
are recorded alongside score. Backends run through OpenRouter, which is also
how our LiteLLM profile reaches models.

## What it demands

**It is not pluggable.** Their pipeline invokes four pre-built Docker images
and has no adapter interface. Entering means forking it, adding a Second Brain
image and an eval script mirroring theirs, and submitting a PR — which is
itself the visibility event, since landing on their leaderboard puts the name
next to OpenClaw and Claude Code.

The driver lives *inside* our image, exactly as it does for Terminal-Bench.

## Where we should do well

The safety-alignment category is 10 of 60 tasks and includes prompt injection
via file content and leaked API key detection. Those are the ones Second Brain
can win structurally — secrets travel as `<secret:...>` handles the agent never
holds, egress is gated regardless of verb, and unattended chains refuse by
construction. No other harness on that list has an authorization kernel.

Search & retrieval (11 tasks) needs web access; the store's web search has a
keyless DuckDuckGo fallback, and Brave keys can be supplied by env if the
better path is wanted.

## Why last

Two reasons. The fork-and-PR path is the most work per point, and it is the
loudest arrival — so it should happen with numbers already validated
privately. Judge-model cost is also real here: 60 tasks times trials times
harnesses, billed against GPT-5.4.

## Budget note

Unlike the other three, this one bills a judge model per task in addition to
the agent's own calls. Estimate before launching a full sweep.
