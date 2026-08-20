# AgentDojo

**Track:** security — the most legible statistic in the field. **Status:**
fastest to *set up* (no Docker at all), slowest to *finish* (the biggest
adapter). Scheduled after Terminal-Bench and Boundary-Bench.

## What it is

97 realistic user tasks and 629 injection cases across banking, Slack, travel
and workspace domains ([site](https://agentdojo.spylab.ai),
[repo](https://github.com/ethz-spylab/agentdojo), ETH Zurich SpyLab). It scores
three things *jointly*:

- **benign utility** — does the agent do the job with no attack present
- **utility under attack** — does it still do the job with an injection
- **attack success rate** — did the injection get what it wanted

That joint scoring is what makes it worth entering: a harness that refuses
everything scores zero utility, and one that does everything scores high
attack success. Second Brain should sit where neither happens.

## What it demands

The agent is a **query function** in their Python process, taking the user
instructions, the available tools, and the environment state. No containers,
no adapter class hierarchy — but the tools are *theirs*, simulated, and
utility is scored by reading their environment's end state.

## The mapping, and the honest caveat

Their "harmful actions" are calls to **simulated** tools, not real egress. So
Second Brain's win does *not* come from `net.http` gating directly. It comes
from routing their tools through a bench service where the manifest approver
refuses what the task did not sanction, plus secret handles keeping injected
exfiltration payloads empty.

Adapter shape:

1. a bench **service** inside the container holding the AgentDojo environment
2. auto-generated `tool_*.py` shims, one per AgentDojo tool, each calling
   `sdk.services.call` (or `net.http` to a localhost env server) — injections
   then arrive in tool results exactly as the benchmark intends
3. the query function boots or reuses a container, submits the instruction,
   returns the final text
4. AgentDojo reads its own environment for utility and attack success

`detail.url` carries the URL *path* when the shim goes over `net.http`, so
**per-tool manifest rules are expressible**: `/tools/read_inbox` allowable,
`/tools/send_money` deniable. That is the security story rendered in the
vocabulary the kernel already ships.

## Why it is the biggest adapter

Tool codegen plus an environment server, and their tool schemas have to be
translated into plugin declarations faithfully enough that the model sees what
their other harnesses show it. Nothing here needs a kernel change, which is
the good news.

## Successors

[AgentDyn](https://arxiv.org/html/2602.03117v1) (2026) is the dynamic
open-ended successor; worth entering once the AgentDojo adapter exists, since
the agent side should carry over.
