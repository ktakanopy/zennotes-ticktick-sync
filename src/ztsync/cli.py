from __future__ import annotations

import argparse
import json
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .config import Settings
from .markdown import MarkdownParseError, parse_vault
from .reconcile import Reconciler
from .service import run_forever
from .state import StateStore
from .ticktick import TickTickClient, TickTickError, TokenStore


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    server: _OAuthCallbackServer

    def do_GET(self) -> None:
        callback = urlparse(self.path)
        if callback.path != self.server.callback_path:
            self._respond(404, "OAuth callback path not found.")
            return
        query = parse_qs(callback.query)
        if query.get("state", [None])[0] != self.server.expected_state:
            self._respond(400, "OAuth state mismatch. Return to the terminal and try again.")
            return
        self.server.callback_query = query
        self._respond(200, "Authorization received. You can close this window.")
        self.server.shutdown()

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _respond(self, status: int, message: str) -> None:
        body = (f"<!doctype html><html><body><p>{message}</p></body></html>").encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _OAuthCallbackServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        bind_address: tuple[str, int],
        expected_state: str,
        callback_path: str,
    ):
        self.expected_state = expected_state
        self.callback_path = callback_path
        self.callback_query: dict[str, list[str]] | None = None
        super().__init__(bind_address, _OAuthCallbackHandler)


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
    redirect = urlparse(settings.ticktick_redirect_uri)
    try:
        redirect_port = redirect.port
    except ValueError:
        print("TICKTICK_REDIRECT_URI has an invalid port", file=sys.stderr)
        return 1
    if (
        redirect.scheme != "http"
        or not redirect.hostname
        or redirect.query
        or redirect.fragment
        or redirect_port != settings.ticktick_oauth_port
    ):
        print(
            "TICKTICK_REDIRECT_URI must be an HTTP URL with the configured OAuth port "
            "and no query or fragment",
            file=sys.stderr,
        )
        return 1
    state = secrets.token_urlsafe(24)
    url = TickTickClient.authorization_url(
        settings.ticktick_client_id,
        settings.ticktick_redirect_uri,
        state,
    )
    try:
        server = _OAuthCallbackServer(
            (settings.ticktick_oauth_bind_host, settings.ticktick_oauth_port),
            state,
            redirect.path or "/",
        )
    except OSError as exc:
        print(f"could not start OAuth callback listener: {exc}", file=sys.stderr)
        return 1
    print("Open this URL in a browser and authorize the app:")
    print(url)
    print(f"Waiting for callback at {settings.ticktick_redirect_uri}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("OAuth login cancelled", file=sys.stderr)
        return 1
    finally:
        server.server_close()
    query = server.callback_query or {}
    if query.get("state", [None])[0] != state:
        print("OAuth state mismatch; authorization cancelled", file=sys.stderr)
        return 1
    if query.get("error", [None])[0]:
        print(f"OAuth authorization failed: {query['error'][0]}", file=sys.stderr)
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
