# What Harness-Bench requires from Second Brain

The pinned release is a broad workspace benchmark, not a large-corpus retrieval
benchmark. `audit_harness_bench.py` measures 106 tasks, 508 fixture files, and
388,123 fixture bytes in total. Once images are excluded, the largest task has
less than 13 KB of fixture data. Installing an embedding model and asynchronously
indexing every fresh task would add startup cost without solving a scale problem
present in this dataset.

The workload instead stresses:

- exact file discovery, reading, editing, and output contracts;
- shell-based tests, Git, archives, SQLite, and code repair;
- CSV/JSON/Markdown synthesis and evidence attribution;
- seven task-local HTTP services supplied by official hooks;
- state retention across six two-round, one three-round, and one five-round
  task; and
- a small multimodal/document edge: three PNGs, two JPEGs, one PDF, and one
  DOCX fixture.

## Current readiness

Second Brain is strong on the dominant path. Its direct file tools, real shell,
contained SDK scripts, persistent conversation session, subagents, audit ledger,
and fresh per-task data tree map naturally onto the benchmark. The integration
now places official workspaces under the ephemeral Second Brain data root so
`run_command` can legitimately use them. A deterministic local fake model has
completed the real model/tool/filesystem loop and received official oracle score
1.0 on task 001 without an external call.

The Essentials catalogue remains available except for Telegram, `ask_question`,
and `show_files`, which require an attending user. File tools, shell and SDK
scripting, web search, validation, database access, and subagents remain. The
benchmark records the installed bundle, exclusions, and store commit.

## Knowledge base recommendation

Do not install `bundle_knowledgebase` for the smoke set or eight-task pilot.
Those selections contain only tiny text, HTML, SQL, CSV, JSON, and Markdown
fixtures. Direct read/grep/glob plus scripts are faster and more deterministic.

For a future complete 106-task run, add a separately labeled document-capable
image or profile before tasks 008, 010, and 013. MiniMax M3 supports native
multimodal input, but the current eval profile conservatively declares image
input disabled. Task 010 also needs dependable PDF/DOCX parsing and DOCX
creation. Installing the entire knowledge-base bundle would provide that, but
also downloads OCR, transcription, Torch, embedding models, and indexing tasks
that this corpus does not need. A smaller parser profile (`parse_pdf`,
`parse_office`, and their dependencies) is the better full-suite design.

Hybrid search should be evaluated later on a purpose-built large-corpus suite,
where every task receives a pre-indexed corpus and index readiness is part of
the protocol. Enabling it only for Second Brain on these tiny fixtures would
not measure the capability it was designed for and would complicate comparisons
with reference harnesses.

## Remaining risks to measure

- MiniMax's real tool-call formatting and error recovery, which the provider
  quota currently prevents testing.
- YOLO versus Lockdown. Lockdown can still read and perform freely writable
  file work and run validated contained SDK scripts; it denies actions that
  would require approval, including arbitrary shell/network activity.
- Exact prompt-token and completion-token accounting. The kernel currently
  reports prompt tokens; completion tokens remain unavailable in its finished
  call event.
- Document and native-image routing before any full-suite claim.
