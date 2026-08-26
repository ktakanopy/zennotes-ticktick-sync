# ZenNotes ↔ TickTick Sync — implementation plan

## Goal

Run one service on `192.168.15.14` that keeps a deliberately small, explicit
set of Markdown tasks in the ZenNotes vault aligned with a single TickTick
project. It must work in both directions without an LLM.

The service is the only automated writer of the vault. ZenNotes clients on the
Mac and Android continue to edit the same Google Drive-synchronised Markdown
files normally.

## Decisions fixed for v1

| Concern | Decision |
| --- | --- |
| Language/runtime | Python 3.12, managed with `uv` |
| Transport | TickTick Open API v1 over HTTPS and OAuth 2.0 |
| Direction | Bidirectional for mapped tasks only |
| TickTick scope | One dedicated project/list: `ZenNotes` |
| Markdown scope | `projects/luma-health/daily-notes/**/*.md` and `inbox/ticktick.md` only |
| TickTick-created task destination | `inbox/ticktick.md`; never guess a daily note |
| Polling | Check vault every 30 seconds; fetch TickTick project data every 60 seconds |
| State | Local SQLite database, outside the Google Drive vault |
| LLM | Never used by sync code |
| Deletes | Never propagate automatically in v1 |
| Conflicts | Do not overwrite; record and surface them |

Do not expand the scope to historical notes, templates, `prompts/`, files named
`*.sync-conflict-*`, or tasks in fenced code blocks.

## Required task format

Only standard checkbox bullets are eligible:

```md
- [ ] Check SMS Hamming problem due:2026-08-26 !high #work
- [x] Finished research due:2026-08-25 #work
```

After the first successful synchronization, append an invisible identity
marker to the same line:

```md
- [ ] Check SMS Hamming problem due:2026-08-26 !high #work <!-- zt:v1 task=TT_TASK_ID project=TT_PROJECT_ID -->
```

The marker is not user-facing Markdown content. Preserve it exactly when users
edit task text. A task that has no marker is a new ZenNotes task. A marker with
an invalid/missing TickTick task is an error to report, never an instruction to
create a duplicate.

Task fields mapped in v1:

| Markdown | TickTick |
| --- | --- |
| checkbox | `status` / completion |
| text before recognised suffixes | `title` |
| `due:YYYY-MM-DD` | all-day `dueDate` in `America/Sao_Paulo` |
| `!high`, `!medium`, `!low`, absent | priority |
| `#tag` | tags |
| marker | task and project identity |

Do not map subtasks, recurring rules, reminders, attachments, comments,
locations, start times or rich descriptions in v1. Retain these TickTick fields
when updating a task; do not clear them.

## Safety invariants

1. Never call a destructive TickTick endpoint in v1.
2. Never rewrite an entire note to change one task. Replace only the precisely
   identified task line, preserving newline style and unrelated content.
3. Never parse checkbox-looking text inside fenced Markdown code blocks.
4. Never overwrite a field changed on both sides since the prior successful
   sync. Create a conflict instead.
5. Never process duplicate markers. Report both file/line locations.
6. Never store OAuth secrets or access tokens in the repository, vault or logs.
7. One process at a time: an OS lock prevents overlapping service runs.
8. Every managed write has an atomic replace, a timestamped backup and an audit
   event.

## Conflict and deletion policy

For every task and every mapped field, SQLite records the last successfully
synchronised normalized value plus its hash. On each cycle:

- changed only in Markdown → write to TickTick;
- changed only in TickTick → write the task line in Markdown;
- unchanged on both sides → no operation;
- changed differently on both sides → create a conflict; do not modify either.

Completion vs reopening is a normal status-field conflict if both changed.
It must not silently pick a winner.

If a mapped Markdown line disappears, leave the TickTick task untouched and
emit `local_task_missing`. If a TickTick task disappears or is deleted, leave
the Markdown line untouched and emit `remote_task_missing`. Resolution will be
manual in v1. Future delete propagation requires an explicit separate design.

Write conflicts to `state/conflicts.jsonl` and expose them with
`uv run ztsync conflicts`. Do not create a second `*.sync-conflict-*` note.

## Repository layout

```
zennotes-ticktick-sync/
  README.md
  pyproject.toml
  .gitignore
  .env.example
  src/ztsync/
    __init__.py
    cli.py
    config.py
    models.py
    markdown.py
    ticktick.py
    state.py
    reconcile.py
    writer.py
    service.py
  tests/
    fixtures/
    test_markdown.py
    test_reconcile.py
    test_state.py
    test_writer.py
  systemd/zennotes-ticktick-sync.service
```

Runtime state is intentionally outside the checkout:
`/home/takano/.local/state/zennotes-ticktick-sync/`. It contains `state.db`,
`conflicts.jsonl`, `service.log`, `backups/`, and an exclusive lock file.
Add only the project-local `.env` and runtime artefacts to `.gitignore`.

## Configuration contract

Create `.env.example` only; `.env` is created manually on the server with
mode `0600`.

```dotenv
ZENNOTES_VAULT_PATH=/absolute/path/to/google-drive/notes
ZENNOTES_TASK_PATHS=projects/luma-health/daily-notes,inbox/ticktick.md
TICKTICK_PROJECT_NAME=ZenNotes
TICKTICK_TIME_ZONE=America/Sao_Paulo
TICKTICK_CLIENT_ID=
TICKTICK_CLIENT_SECRET=
TICKTICK_REDIRECT_URI=http://127.0.0.1:8765/oauth/callback
SYNC_STATE_DIR=/home/takano/.local/state/zennotes-ticktick-sync
VAULT_POLL_SECONDS=30
TICKTICK_POLL_SECONDS=60
```

Do not hard-code the Google Drive mount path. The setup task must discover and
validate it on the server before the service is enabled.

## Implementation tasks

Each task below is independently executable and must be committed separately
only if Kevin asks for commits. Do not start a later task until its checks pass.

### 1. Bootstrap the Python project

Create the repository structure, `pyproject.toml`, `uv` configuration,
formatter/linter configuration, `.gitignore`, `.env.example` and a brief
README. Use `httpx` for HTTP, `pydantic` for data validation and `pytest`
for tests. Configure Python 3.12. Add no framework, database server or queue.

Acceptance:

```sh
uv sync
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

### 2. Define configuration and domain models

Implement validated config loading from `.env`/environment. Validate absolute
vault and state paths, poll intervals, timezone and safe relative task paths.
Implement typed models for `MarkdownTask`, `TickTickTask`, `TaskSnapshot`,
`Conflict` and `SyncAction`. Model normalized fields explicitly.

Acceptance: unit tests reject missing credentials, paths outside the vault,
invalid due dates and invalid marker values.

### 3. Implement a lossless Markdown task parser

Parse only configured files. Identify task line, byte/line location, checkbox,
title, due marker, priority, tags and `zt:v1` identity marker. Ignore code
fences. Preserve the raw original line and newline convention. Provide a
renderer that changes only mapped fields and preserves unknown suffixes.

Acceptance: fixture tests cover regular tasks, completed tasks, Unicode, links,
empty task text, image embeds, nested bullets, code fences, CRLF, bad dates,
duplicate markers and unchanged round trips.

### 4. Implement SQLite state and migrations

Create the state directory securely and use SQLite WAL mode. Create tables for
task mappings/snapshots, sync runs and conflicts. Store normalized last-synced
field values and hashes, not credentials. Include a schema-version migration
mechanism and a read-only `status` query.

Acceptance: migration is idempotent; reopening DB preserves state; query tests
show the correct last snapshot and conflict history.

### 5. Implement TickTick OAuth and API client

Implement the documented authorization-code flow and secure token storage in
the state directory. Implement only: get current user, list projects, create
project, fetch one project with tasks, create task, update task and complete
task. Set Authorization bearer headers, explicit timeouts and bounded retries
for transient 429/5xx errors. Never log token values.

Before coding, verify exact endpoint payloads against the current official
TickTick Open API docs. Do not rely on undocumented `/api/v2` endpoints.

Acceptance: mocked HTTP tests cover OAuth errors, 401/403, rate limit retry,
network timeout and task CRUD payload parsing. A manual command prints only
the TickTick account display name and selected project ID.

### 6. Add project bootstrap and explicit CLI

Implement commands:

```sh
uv run ztsync auth login
uv run ztsync doctor
uv run ztsync project ensure
uv run ztsync status
uv run ztsync conflicts
uv run ztsync sync --dry-run
uv run ztsync sync
```

`doctor` verifies mount access, state directory permissions, OAuth state and
task path configuration without changing tasks. `project ensure` finds or
creates exactly one project named `ZenNotes` and persists its ID. `dry-run`
prints planned actions without writing Markdown or calling mutation endpoints.

Acceptance: CLI exit codes and dry-run behaviour are fully unit tested.

### 7. Implement reconciliation: Markdown → TickTick

For each unmarked eligible Markdown task, create a TickTick task in the
dedicated project, then atomically append its marker. For marked tasks, compare
normalized local fields with the state snapshot and update only when Markdown
alone changed. Use TickTick's completion endpoint for completion changes if
the API requires it.

If the remote write succeeds but Markdown marker write fails, record a
recoverable pending operation keyed by the returned task ID; do not make a
second remote create on the next run.

Acceptance: fixture + mocked API tests prove exactly-once creation across a
crash between remote creation and local marker insertion.

### 8. Implement reconciliation: TickTick → Markdown

Fetch all tasks in the managed TickTick project. For a task already mapped,
compare each field with the snapshot and update only when TickTick alone
changed. For a remote task with no mapping, append it as an unchecked or
checked task to `inbox/ticktick.md` under a dated `## Imported from TickTick`
heading, then append its marker and save the mapping.

Never choose a daily note based on a due date. Never import a task from another
TickTick project.

Acceptance: tests cover import, status/title/due/tag/priority updates and
non-managed-project exclusion.

### 9. Implement conflict detection and reporting

Compare local/remote normalized field values against the last successful
snapshot. If the same field changed differently, create a single deduplicated
conflict event containing task ID, source file, line, fields, local value and
remote value. Continue synchronizing unrelated tasks and fields.

Acceptance: a test changes title locally and remotely and verifies neither side
is overwritten; `ztsync conflicts` shows one actionable conflict.

### 10. Implement safe Markdown writes and backups

Write via temporary file in the same directory, `fsync`, then atomic rename.
Keep timestamped copies of affected notes under the state directory for 30
days; retention must be explicit and tested. Validate the resulting Markdown
can be parsed before replacing the source. Do not alter file permissions.

Acceptance: simulated write failure leaves original source intact; backup and
restore test passes; concurrent invocation is refused by the lock.

### 11. Build service loop and observability

Implement a simple polling loop, not filesystem watches. Run a single sync
cycle at startup, then poll using configured intervals. Emit compact structured
JSON logs: run ID, action counts, duration, error class — never full note
content or credentials. A failed cycle must not stop later cycles.

Acceptance: fake clock tests prove scheduling; one failed remote request is
logged and the subsequent cycle succeeds.

### 12. Add systemd deployment artefact

Create a user systemd unit that runs the installed CLI/service under `takano`.
Use `EnvironmentFile` pointing to the mode-0600 `.env`, restart on failure,
and restrictive filesystem permissions compatible with the Google Drive mount
and state directory. Do not enable the unit automatically.

Acceptance: `systemd-analyze verify systemd/zennotes-ticktick-sync.service`
passes. README documents manual install, start, stop, logs and rollback.

### 13. End-to-end staged rollout

On a copied test vault and a dedicated test TickTick project, execute:

1. Markdown create → TickTick create + marker;
2. Markdown complete → TickTick complete;
3. TickTick create → `inbox/ticktick.md` import;
4. TickTick title/date/priority/tag update → Markdown update;
5. simultaneous title edit → conflict, no overwrite;
6. missing local/remote task → report, no deletion;
7. restart mid-cycle → no duplicate task.

Only after all pass, point `.env` to the real vault and production
`ZenNotes` project. First production run is `sync --dry-run`, reviewed
manually. The first live run is supervised and starts with at most five
explicitly chosen tasks.

## Model handoff rules

The implementation model must not:

- use an LLM, browser automation, unofficial TickTick APIs, or scraped session
  cookies;
- read or modify anything outside the configured vault paths and state path;
- propagate delete actions;
- auto-resolve conflicts;
- put tokens in source control, test fixtures, output or logs;
- expand the supported Markdown syntax without a separate design decision.

For every task it must return: changed files, tests/checks run, exact result,
and any unresolved issue. It must stop and ask for direction if the TickTick
API cannot support an assumed field or OAuth token refresh is required but
undocumented.

## Setup inputs required from Kevin before Task 5 / Task 13

1. Exact Google Drive vault mount path on `192.168.15.14`.
2. TickTick developer app client ID and client secret, created in the official
   developer portal.
3. Confirmation that a dedicated `ZenNotes` TickTick list may be created.
4. Approval of the OAuth redirect approach (recommended: SSH tunnel from the
   Mac to the server for the loopback callback).
5. Choice of a disposable TickTick test account/list or approval to create a
   dedicated test list in the current account.

## Rollback

Stop the systemd unit, then restore a timestamped backup from
`$SYNC_STATE_DIR/backups/`. Because v1 never deletes tasks and records mapping
state locally, stopping the service halts synchronization without damaging the
vault. TickTick tasks created during testing can be manually archived or
deleted only after review.
