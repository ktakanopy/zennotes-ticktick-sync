# ZenNotes - TickTick Sync

Safe, scoped synchronization between Markdown tasks in ZenNotes and one dedicated
TickTick project. The v1 implementation uses deterministic parsing and matching;
it does not use an LLM.

The detailed implementation plan is in PLAN.md. The server project is
`/home/takano/projects/zennotes-ticktick-sync`; the synchronized vault currently
resolves to `/home/takano/personal_projects/zennotes`.

The service is intentionally not connected to TickTick until OAuth credentials,
the real vault path and a test project have been verified.

## Local development

Install `uv` and run:

    uv sync
    uv run pytest
    uv run ruff check .
    uv run ruff format --check .

`uv` manages the project environment and lockfile; do not create or invoke a
virtual environment manually.

The server runtime state must stay outside the vault and repository.

## Task syntax

Tasks use the same hashtag convention as TickTick. Tags are removed from the
title and sent as TickTick tags:

    - [ ] Review the release #work #release

Date-only tasks remain all-day tasks. The sync also accepts deterministic
natural dates with an optional time:

    - [ ] Send the report tomorrow 18:00 #work
    - [ ] Call the team tomorrow at 6pm #work
    - [ ] Prepare notes today 9h

The relative date is resolved using `TICKTICK_TIME_ZONE` when the task is
first synchronized. The Markdown line is then canonicalized to
`due:YYYY-MM-DDTHH:MM`, so it does not move again at midnight. Existing
date-only syntax such as `due:2026-08-27` remains supported.

## Server setup

Install `uv`, then synchronize the project from `pyproject.toml` and
`uv.lock`:

    uv sync

Copy `.env.example` to `.env`, set the absolute vault path, and keep the file at
mode 600. Never place TickTick secrets in Git, the vault or logs.

Run the checks and safe scan:

    uv run pytest
    uv run ruff check .
    uv run ruff format --check .
    uv run ztsync doctor
    uv run ztsync sync --dry-run

## TickTick authentication

Register an OAuth application in the official TickTick developer portal and set
the client ID and secret in `.env`. The redirect URI must match exactly.

The login listener binds to `0.0.0.0:8765` by default so a browser on the local
network can reach the server directly. Register this exact redirect URI in the
TickTick developer portal:

    http://192.168.15.14:8765/oauth/callback

Run the login command in a server shell:

    uv run ztsync auth login

Open the printed URL in the Mac browser. The server will receive the callback
automatically and show a confirmation page. Keep port 8765 restricted to the
local network; `0.0.0.0` is a bind address, not a browser redirect address.
After authentication, select the dedicated project:

    uv run ztsync project ensure

## Running

The first real run should be supervised and limited to the default five new
tasks:

    uv run ztsync sync --max-new 5

For continuous operation on the server, install the persistent user service:

    mkdir -p ~/.config/systemd/user
    cp systemd/zennotes-ticktick-sync.service ~/.config/systemd/user/
    uv sync --no-dev
    systemctl --user daemon-reload
    systemctl --user enable --now zennotes-ticktick-sync.service
    systemctl --user is-active zennotes-ticktick-sync.service

Inspect logs with `journalctl --user -u zennotes-ticktick-sync.service -f`.
If the server must run while `takano` is not logged in, enable user-manager
lingering once with `sudo loginctl enable-linger takano`.

If systemd is unavailable, `./run_in_backgroundl.sh` remains a manual fallback.
It uses `nohup`, prints the PID and writes output to `~/ztsync.log`.

Tasks already represented in a configured daily-notes path are not imported to
`inbox/ticktick.md`. Before the first run after an existing migration, inspect
possible duplicate pairs with:

    uv run ztsync merge

The command keeps the daily-notes task as canonical. Apply only after reviewing
the output; it removes the duplicate inbox line and deletes the matching
duplicate TickTick task:

    systemctl --user stop zennotes-ticktick-sync.service
    uv run ztsync merge --apply
    systemctl --user start zennotes-ticktick-sync.service

The normal sync never deletes tasks. To roll back local Markdown changes, stop
the service and restore the relevant backup from
`/home/takano/.local/state/zennotes-ticktick-sync/backups/` after review.
