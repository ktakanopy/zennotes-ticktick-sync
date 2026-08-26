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

Install uv and run:

    uv sync
    uv run pytest
    uv run ruff check .
    uv run ruff format --check .

The server runtime state must stay outside the vault and repository.

## Server setup

The current server has a Python virtual environment at `.venv`. If rebuilding
it, install the project and development tools with:

    python3 -m venv .venv
    .venv/bin/pip install -e '.[dev]'

Copy `.env.example` to `.env`, set the absolute vault path, and keep the file at
mode 600. Never place TickTick secrets in Git, the vault or logs.

Run the checks and safe scan:

    .venv/bin/pytest
    .venv/bin/ruff check .
    .venv/bin/ruff format --check .
    .venv/bin/ztsync doctor
    .venv/bin/ztsync sync --dry-run

## TickTick authentication

Register an OAuth application in the official TickTick developer portal and set
the client ID and secret in `.env`. The redirect URI must match exactly.

For the recommended loopback flow, create an SSH tunnel from the Mac while the
server-side login command is waiting for the callback:

    ssh -N -L 8765:127.0.0.1:8765 takano@192.168.15.14

In another server shell:

    .venv/bin/ztsync auth login

Open the printed URL in the Mac browser, then paste the resulting callback URL
back into the server shell. After authentication, select the dedicated project:

    .venv/bin/ztsync project ensure

## Running

The first real run should be supervised and limited to the default five new
tasks:

    .venv/bin/ztsync sync --max-new 5

Do not install or enable the systemd unit before the staged test in PLAN.md has
passed. Manual installation for later use:

    mkdir -p ~/.config/systemd/user
    cp systemd/zennotes-ticktick-sync.service ~/.config/systemd/user/
    systemctl --user daemon-reload
    systemctl --user start zennotes-ticktick-sync.service

The unit is not enabled by the repository setup. Inspect logs with
`journalctl --user -u zennotes-ticktick-sync.service` and stop it with
`systemctl --user stop zennotes-ticktick-sync.service`.

No delete operation is implemented. To roll back, stop the service and restore
the relevant backup from
`/home/takano/.local/state/zennotes-ticktick-sync/backups/` after review.
