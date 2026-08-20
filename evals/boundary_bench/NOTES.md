# Boundary-Bench

**Track:** security — the flagship claim. **Status:** rides the Terminal-Bench
adapter almost free, so it follows immediately after it.

## What it is

A hardening layer ([paper](https://arxiv.org/html/2608.02670),
[repo](https://github.com/boundary-bench/boundary-bench)) that attaches
OS-enforced policy to an existing benchmark — Terminal-Bench 2.1 — and
measures how success and cost change against the unrestricted baseline.

Three axes, all native OS mechanisms rather than a policy shim, so the agent
meets ordinary `EROFS` and `EPERM` errors:

- **Network egress:** default-deny proxy, 205 allowlisted domains, no per-task
  additions. Cloud metadata and private ranges always blocked. Egress rules
  never block the model endpoint.
- **Filesystem:** OS directories read-only via bind mounts; only the
  workspace, `/tmp`, `/dev/shm` and harness caches stay writable.
- **Privilege:** unprivileged user, no sudoers, empty capability bounding set,
  `no_new_privs`.

## Why this is the flagship

Published result: under the strict policy, **Claude Code, Codex, Grok Build
and Terminus-2 lose 7.1–18.3 points of success and inflate cost 16–167%** —
they meet hardening as surprise errors and flail. Second Brain is built to
know its own bounds and route around them deliberately. If our delta under
policy is near zero, "born inside the boundary" is the headline of the whole
campaign.

MiniMax M3 is already among their tested backends, which gives a direct
same-model cross-reference for free.

## What we already know works

Simulated locally and passed clean, with zero errors: `--user 1000:1000`,
`--read-only` rootfs, `--tmpfs /tmp`. See §6 of the implementation guide.

## What still needs deciding

- **DATA_DIR placement.** Second Brain needs a writable DATA_DIR (SQLite,
  config, plugin trees). Under this policy that must be inside the workspace
  or `/tmp` — set `XDG_DATA_HOME` accordingly rather than using `/data`.
- **Loopback under the egress proxy.** Inbound is unrestricted because the
  sandbox exposes no services, and the driver talks to 127.0.0.1, so this
  should be fine — but confirm it before a full run.
- **The model endpoint** must be reachable; they state egress rules never
  block it, but our endpoint is MiniMax/OpenRouter rather than theirs.

## Entry

A harness adapter module in `src/boundarybench/harness/` plus an in-sandbox
install script, mirroring their four existing ones, submitted as a PR. Their
paper used 3 valid trials per bundle-task-policy cell.

## The number to report

Not the absolute score — the **delta**. Ours under policy against ours
unrestricted, set beside their published 7.1–18.3 point losses.
