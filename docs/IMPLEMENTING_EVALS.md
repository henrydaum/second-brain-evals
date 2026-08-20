# Implementing an eval against the Second Brain container

This is the how-to for adding a benchmark to the battery. It assumes nothing
about which benchmark: every one of them reduces to *drive the agent, answer
its dialogs, read what it left behind*, and the differences live entirely in
who supplies the task and who scores the result.

Everything here has been run. Where a rule cost a wasted run to discover, it
says so — those are the parts to read twice.

---

## 1. What the container is

One image is one reproducible trial. It carries three things:

| Half | Where it comes from | Why it is separate |
|---|---|---|
| The kernel | `secondbrain:dev`, built from the Second Brain repo | changes every time you touch the kernel |
| The pip half | `RUN pip install litellm Pillow` in `Dockerfile.bench` | store packages declare pip deps that land in site-packages, **not** in DATA_DIR |
| The DATA_DIR half | `build_template.py` → `template/` → baked at `/opt/sb-template` | config plus the store packages, installed for real so the manifest carries provenance |

**The template is two halves in different places, and that is the one thing
that surprises people.** Installing a store package writes its *files* into
DATA_DIR and its *dependencies* into site-packages. A template built by
installing packages is therefore only half a template: copy the DATA_DIR
somewhere new and the plugins are there but their libraries are not. Hence a
pip layer in the image that has to be kept in step with the package list by
hand.

The template is baked at `/opt/sb-template`, not at `/data`, for two reasons:
a volume mounted at DATA_DIR would shadow anything baked there, and every
trial wants its own pristine writable copy without a rebuild. The entrypoint
copies `/opt/sb-template` into `/data/Second Brain` on first start.

### Building it

```bash
python build_template.py --profile bench          # declare, install, manifest
docker build -f Dockerfile.bench -t secondbrain:bench .
```

`build_template.py` runs the install *inside Linux*, with the kernel repo's
git directory mounted read-only — that is the only thing that knows what
`origin/store` means, and a DATA_DIR assembled on Windows would carry Windows
paths. It writes `template_manifest.json` beside the tree:

```json
{"packages": ["bundle_essentials", "frontend_http"],
 "autoload_services": ["web_search_provider"],
 "store_commit": "5347a5e...", "kernel_commit": "ec315e9..."}
```

**That manifest is what a published score has to report alongside it.** A
benchmark number without the harness configuration that produced it is not a
claim anyone can check.

### Running it

Nothing secret is in any layer — a layer is immutable and distributable, so a
key committed to one outlives any later deletion of it. The entrypoint reads
the environment instead:

| Variable | Meaning |
|---|---|
| `SB_LLM_API_KEY`, `SB_LLM_MODEL`, `SB_LLM_ENDPOINT`, `SB_LLM_BACKEND` | the model profile to run |
| `SB_HTTP_TOKEN`, `SB_HTTP_PORT` | the wire the driver talks to |
| `SB_AUTOLOAD_SERVICES` | **registered** service names, not file stems |
| `SB_WRITABLE_DIRS` | where the agent may write deliverables |

```bash
docker run --rm --user 1000:1000 --env-file bench.env secondbrain:bench
```

Verified from the image alone, with no volumes and unprivileged: the template
seeds, the service autoloads, all thirteen tools register, and a real model
turn answers.

---

## 2. How the driver talks to it

The driver is an ordinary HTTP client. It is deliberately **not** a plugin:
the wire is documented (`docs/HTTP_PROTOCOL.md` in the kernel repo), a client
is debuggable from outside the sandbox, and every benchmark run then dogfoods
the same surface a real web client uses.

**The kernel binds loopback only.** A published port will not reach it, so the
driver runs *inside* the container — which is what Harbor and Boundary-Bench
expect anyway, since they install the agent into the task container.

### The sequence, in the order it has to happen

```
1. GET  /events?thread=main&token=...    open the stream, and keep it open
2. POST /sdk/frontend.submit             {"input_kind":"text","text":"/locations"}
3. POST /sdk/frontend.submit             the actual task prompt
4.      ... answer every approval frame with /sdk/frontend.resolve ...
5.      stop when a typing:false frame arrives
6. POST /sdk/conv.read, /sdk/ledger.read     collect the evidence
```

**Step 2 is not optional, and leaving it out is the single most expensive
mistake available.** A session has to *exist* before anything can be attended:
opening the stream does not create one, `session.get` answers `{"data": null}`,
and every unsafe Request comes back `403 approval_declined` with nobody having
been asked. Any slash command creates it, and `/locations` needs no model.

**Step 1 stays open for the whole run.** The stream *is* the attendance signal
— "no stream, no dialogs". Close it and the agent silently loses the ability to
ask for anything.

**Step 5 is the only reliable completion signal.** `typing: false` means the
*logical* turn ended, including after a doorman held it open, after a re-drive,
and after a crash. Scope the detection to frames that arrived *after* your
submit: a previous command's typing cycle will otherwise end your turn
instantly, and the run will look like a model that answered in 1.8 seconds.

### Reading the reply

The reply arrives as `stream_delta` frames **and nothing else**. A frontend
declaring `supports_streaming` is deduped against the `messages` channel, so a
client waiting for a `messages` frame waits forever. Accumulate `delta`, and
take `final_text` from the closing frame (`done: true`) as authoritative.

```json
{"kind":"stream_delta","payload":{"seq":1,"delta":"READY","done":false}}
{"kind":"stream_delta","payload":{"seq":2,"delta":"","done":true,"final_text":"READY"}}
{"kind":"typing","payload":false}
```

---

## 3. Answering dialogs: the manifest approver

This is the part that makes Second Brain worth benchmarking, so it is worth
implementing carefully rather than switching off.

**Never run `yolo`.** It auto-approves attended unsafe Requests, which is
precisely the layer being measured. Run `ask` mode with a driver that answers
from a per-task manifest — allow what the task legitimately needs, refuse the
rest. Yolo is worth exactly one run, as a published ablation: "full mediation
costs Second Brain X points".

Every approval frame carries a machine-readable `detail`. **Match on that,
never on `title` or `body`** — those are prose renderings of the same facts,
and a reworded dialog would silently change policy.

```json
{"kind":"approval","payload":{
  "id":"approve_...","title":"Run shell commands",
  "detail":{"type":"proc.run","asker":"frontend:http",
            "command":"echo hello","prefixes":["echo hello"]},
  "enum":["allow","always:echo hello","deny"],
  "enum_labels":["Allow once","Always allow: echo hello","Deny"]}}
```

Per family: `proc.*` carries `command`/`cwd`/`prefixes`, `net.http` carries
`method`/`url`, `fs.*` carries `path`/`dst`, everything else carries `subject`.
A `detail` of `null` means the question is not a permission gate at all (a
`ui.ask`, a tool asking the person something) — do not apply grant policy to it.

Three rules for the matcher:

- **Answer `allow`, never an `always:` option.** A standing grant escapes the
  manifest into config and silently widens every later task in that trial.
- **`prefixes` are `(program, subcommand)` pairs** — `echo hello`, not `echo`.
  They come from the same `shell.command_prefix` vocabulary the kernel's own
  grant store uses, which is why matching in that unit is exact rather than a
  guess. A line the lexer refuses (a glob, a redirect, a subshell) carries no
  `prefixes` at all; match on `command` or refuse.
- **No match means deny, and log it.** A benchmark result where the harness
  quietly allowed something is not a result.

Worked example, which is also the whole security story in two lines:

```
[manifest] net.http https://html.duckduckgo.com/html/ -> allow   -> 3 results
[manifest] net.http https://example.com/exfiltrate    -> deny    -> 403 approval_declined
```

---

## 4. Collecting evidence

Three sources, all over the same wire:

- `conv.read` — the transcript. **Needs the conversation id** (from
  `session.get`'s `conversation_id`, or `conv.list`); with no id it answers
  zero rows. Page it: the answer is capped by *bytes*, and asking for too much
  fails `413` rather than truncating.
- `ledger.read` — every effect the system performed, with provenance. This is
  the audit trail a submission wants; it records the driver's own calls too.
- The filesystem — whatever the task said to produce. Set `SB_WRITABLE_DIRS` to
  the task directory or the agent cannot write there without a dialog.

---

## 5. Adding a new eval

Each benchmark gets a directory under `evals/`, and supplies three things:

```python
class Adapter:
    def tasks(self): ...                 # yield task_id, prompt, fixtures, manifest
    def setup(self, task): ...           # place fixtures where the container sees them
    def score(self, task, result): ...   # their verifier, or ours
```

Everything else — the container, the wire sequence, the approver, the
collection — is shared and lives in `driver/`. If an adapter needs to reach
into the driver, that is a sign the driver is missing an argument rather than
that the adapter is special.

The four targets and what each one demands are in `evals/*/NOTES.md`.

---

## 6. Iterating on the kernel

Measured on a Windows host with Docker Desktop:

| Loop | Cost | Use when |
|---|---|---|
| `docker build -t secondbrain:dev .` | **~2s** | you changed kernel source |
| then `docker build -f Dockerfile.bench ...` | **~20s** | you need the bench image too |
| Bind-mount `-v "<repo>:/app"` | **0s**, no rebuild | running the kernel suite on Linux |

The 20s is the pip layer: `FROM secondbrain:dev` changing invalidates
everything after it, so litellm reinstalls. Live with it, or reorder the image
so the kernel source is the last layer.

Bind-mounting is the fastest way to run the kernel's own test suite on Linux
during development — no rebuild at all — but expect it to be about 4x slower
(48s against 12s) because Windows bind-mounts are slow, and expect timing-
sensitive tests to flake under that I/O. Trust the baked image for a verdict.

Two mechanical traps:

- Bind-mounting for tests writes `.pytest_tmp/` into the repo as root, which
  then **breaks the next `docker build`** with `invalid file request`. It is in
  `.dockerignore` now; if you add another such directory, add it there too.
- `secondbrain:bench` has an `ENTRYPOINT`, so `docker run secondbrain:bench ls`
  does not run `ls` — it passes `ls` to the entrypoint and boots the app. Use
  `secondbrain:dev` for shell work, or `--entrypoint sh`.

---

## 7. Things that each cost a run to find

1. A session must exist before anything is attended (§2).
2. The reply is `stream_delta` only; `messages` never arrives (§2).
3. `conv.read` needs the conversation id (§4).
4. `fs.write` takes `data`, not `content`.
5. `service.call` takes `kwargs` for named arguments — `args` is positional,
   and a dict there splats its **keys**. The kernel refuses this now rather
   than running the callee on the string `"query"`.
6. A service is not loaded because it is installed — set `autoload_services` —
   and its registered name is the class's `name`, not the file stem
   (`service_web_search.py` becomes `web_search_provider`).
7. From Git Bash: `MSYS_NO_PATHCONV=1` for `docker exec` paths, and prefix host
   paths with `//` for `-v`.
8. Scope end-of-turn detection to frames after your submit (§2).
