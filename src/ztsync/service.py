from __future__ import annotations

import fcntl
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from uuid import uuid4

from .config import Settings
from .markdown import task_files
from .reconcile import Reconciler
from .state import StateStore
from .ticktick import TickTickClient, TickTickError, TokenStore


class SingleProcessLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self) -> SingleProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self.handle.close()
            self.handle = None
            raise RuntimeError("another ztsync process is already running") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self.handle:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
            self.handle.close()
            self.handle = None


def vault_signature(settings: Settings) -> tuple[tuple[str, int, int], ...]:
    signature: list[tuple[str, int, int]] = []
    vault_path = settings.vault_path.resolve()
    for path in task_files(settings.vault_path, settings.task_paths):
        relative = path.relative_to(vault_path).as_posix()
        stat = path.stat()
        signature.append((relative, stat.st_mtime_ns, stat.st_size))
    return tuple(sorted(signature))


def run_once(settings: Settings, *, dry_run: bool = False, max_new_tasks: int = 5) -> int:
    if not settings.ticktick_credentials_configured:
        raise TickTickError("TickTick OAuth client credentials are not configured")
    run_id = uuid4().hex
    started = datetime.now(UTC)
    actions = []
    with StateStore(settings.state_dir) as store:
        try:
            project_id = store.get_metadata("ticktick_project_id")
            if not project_id:
                raise TickTickError("run 'ztsync project ensure' before starting sync")
            with TickTickClient(
                client_id=settings.ticktick_client_id,
                client_secret=settings.ticktick_client_secret,
                token_store=TokenStore(settings.state_dir / "oauth.json"),
            ) as client:
                actions = Reconciler(
                    settings,
                    store,
                    client,
                    project_id=project_id,
                    dry_run=dry_run,
                ).run(max_new_tasks=max_new_tasks)
            store.record_run(
                run_id,
                started,
                "dry_run" if dry_run else "success",
                [action.model_dump() for action in actions],
            )
        except Exception as exc:
            try:
                store.record_run(
                    run_id,
                    started,
                    "failed",
                    [
                        {
                            "kind": "sync_cycle_failed",
                            "error": type(exc).__name__,
                            "reason": str(exc),
                        }
                    ],
                )
            except Exception:
                pass
            raise
    for action in actions:
        print(json.dumps(action.model_dump(), ensure_ascii=False, sort_keys=True))
    return 0


def run_forever(settings: Settings) -> None:
    with SingleProcessLock(settings.state_dir / "service.lock"):
        previous_signature: tuple[tuple[str, int, int], ...] | None = None
        last_remote_sync = 0.0
        while True:
            current_signature = vault_signature(settings)
            now = monotonic()
            vault_changed = current_signature != previous_signature
            remote_due = now - last_remote_sync >= settings.ticktick_poll_seconds
            if vault_changed or remote_due:
                try:
                    run_once(settings)
                    previous_signature = vault_signature(settings)
                    last_remote_sync = monotonic()
                except Exception as exc:
                    print(
                        json.dumps(
                            {
                                "event": "sync_cycle_failed",
                                "error": type(exc).__name__,
                                "message": str(exc),
                            },
                            ensure_ascii=False,
                        )
                    )
                    previous_signature = current_signature
            time.sleep(settings.vault_poll_seconds)
