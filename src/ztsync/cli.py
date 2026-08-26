from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .markdown import MarkdownParseError, parse_vault
from .reconcile import Reconciler
from .service import run_forever
from .state import StateStore
from .ticktick import TickTickClient, TickTickError, TokenStore


def _settings() -> Settings:
    return Settings.from_env(Path.cwd())


def _doctor(settings: Settings) -> int:
    errors: list[str] = []
    if not settings.vault_path.is_dir():
        errors.append(f"vault does not exist or is not a directory: {settings.vault_path}")
    if not settings.state_dir.is_absolute():
        errors.append(f"state directory is not absolute: {settings.state_dir}")
    if not settings.ticktick_credentials_configured:
        errors.append("TickTick OAuth client credentials are not configured")
    try:
        tasks = parse_vault(settings.vault_path, settings.task_paths)
    except (OSError, MarkdownParseError, ValueError) as exc:
        errors.append(f"vault task scan failed: {exc}")
        tasks = []
    print(f"vault: {settings.vault_path}")
    print(f"eligible tasks: {len(tasks)}")
    print(f"state: {settings.state_dir}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("doctor: OK")
    return 0


def _status(settings: Settings) -> int:
    with StateStore(settings.state_dir) as store:
        result = store.counts()
    print(json.dumps(result, sort_keys=True))
    return 0


def _conflicts(settings: Settings) -> int:
    with StateStore(settings.state_dir) as store:
        conflicts = store.list_conflicts()
    for conflict in conflicts:
        print(json.dumps(conflict, ensure_ascii=False, sort_keys=True))
    if not conflicts:
        print("no unresolved conflicts")
    return 0


def _client(settings: Settings) -> TickTickClient:
    if not settings.ticktick_credentials_configured:
        raise TickTickError("TickTick OAuth client credentials are not configured")
    return TickTickClient(
        client_id=settings.ticktick_client_id,
        client_secret=settings.ticktick_client_secret,
        token_store=TokenStore(settings.state_dir / "oauth.json"),
    )


def _auth_login(settings: Settings) -> int:
    if not settings.ticktick_credentials_configured:
        print("TickTick OAuth client credentials are not configured", file=sys.stderr)
        return 1
    state = secrets.token_urlsafe(24)
    url = TickTickClient.authorization_url(
        settings.ticktick_client_id,
        settings.ticktick_redirect_uri,
        state,
    )
    print("Open this URL in a browser, authorize the app, then paste the full callback URL:")
    print(url)
    try:
        callback = input().strip()
    except EOFError:
        print("OAuth callback was not provided", file=sys.stderr)
        return 1
    query = parse_qs(urlparse(callback).query)
    if query.get("state", [None])[0] != state:
        print("OAuth state mismatch; authorization cancelled", file=sys.stderr)
        return 1
    code = query.get("code", [None])[0]
    if not code:
        print("OAuth callback did not contain an authorization code", file=sys.stderr)
        return 1
    try:
        with _client(settings) as client:
            client.exchange_code(code, settings.ticktick_redirect_uri)
    except TickTickError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("TickTick authentication saved")
    return 0


def _project_ensure(settings: Settings) -> int:
    try:
        with StateStore(settings.state_dir) as store:
            with _client(settings) as client:
                projects = [
                    project
                    for project in client.list_projects()
                    if project.get("name") == settings.ticktick_project_name
                ]
                if len(projects) > 1:
                    print(
                        f"multiple TickTick projects named {settings.ticktick_project_name!r}",
                        file=sys.stderr,
                    )
                    return 1
                project = (
                    projects[0]
                    if projects
                    else client.create_project(settings.ticktick_project_name)
                )
                project_id = str(project["id"])
                store.set_metadata("ticktick_project_id", project_id)
        print(f"TickTick project: {settings.ticktick_project_name} ({project_id})")
        return 0
    except TickTickError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _sync(settings: Settings, dry_run: bool, max_new: int) -> int:
    try:
        tasks = parse_vault(settings.vault_path, settings.task_paths)
    except (OSError, MarkdownParseError, ValueError) as exc:
        print(f"sync scan failed: {exc}", file=sys.stderr)
        return 1
    unmarked = sum(1 for task in tasks if not task.task_id)
    marked = len(tasks) - unmarked
    mode = "dry-run" if dry_run else "scan-only"
    print(f"{mode}: {len(tasks)} eligible tasks ({marked} mapped, {unmarked} new)")
    if dry_run:
        return 0
    try:
        with StateStore(settings.state_dir) as store:
            project_id = store.get_metadata("ticktick_project_id")
            if not project_id:
                print("run 'ztsync project ensure' before sync", file=sys.stderr)
                return 1
            with _client(settings) as client:
                actions = Reconciler(
                    settings,
                    store,
                    client,
                    project_id=project_id,
                ).run(max_new_tasks=max_new)
                for action in actions:
                    print(action.model_dump_json())
    except (TickTickError, OSError, MarkdownParseError, ValueError) as exc:
        print(f"sync failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ztsync")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("doctor")
    subparsers.add_parser("status")
    subparsers.add_parser("conflicts")

    auth = subparsers.add_parser("auth")
    auth_subparsers = auth.add_subparsers(dest="auth_command", required=True)
    auth_subparsers.add_parser("login")

    project = subparsers.add_parser("project")
    project_subparsers = project.add_subparsers(dest="project_command", required=True)
    project_subparsers.add_parser("ensure")

    sync = subparsers.add_parser("sync")
    sync.add_argument("--dry-run", action="store_true")
    sync.add_argument("--max-new", type=int, default=5)
    subparsers.add_parser("service")

    args = parser.parse_args(argv)
    settings = _settings()
    if args.command == "doctor":
        return _doctor(settings)
    if args.command == "status":
        return _status(settings)
    if args.command == "conflicts":
        return _conflicts(settings)
    if args.command == "auth":
        if args.auth_command == "login":
            return _auth_login(settings)
    if args.command == "project":
        if args.project_command == "ensure":
            return _project_ensure(settings)
    if args.command == "sync":
        if args.max_new < 0:
            parser.error("--max-new must be non-negative")
        return _sync(settings, args.dry_run, args.max_new)
    if args.command == "service":
        run_forever(settings)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
