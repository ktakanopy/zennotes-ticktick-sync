from __future__ import annotations

from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .markdown import marker, parse_vault, render_task
from .models import (
    Conflict,
    MarkdownTask,
    SyncAction,
    TaskSnapshot,
    TickTickTask,
    priority_name_to_ticktick,
)
from .state import StateStore, fields_hash
from .ticktick import TickTickClient, TickTickError
from .writer import atomic_create, atomic_replace


class ReconcileError(RuntimeError):
    pass


def _api_datetime(value: date, due_time: time | None, time_zone: str) -> str:
    local = datetime.combine(value, due_time or time.min, tzinfo=ZoneInfo(time_zone))
    return local.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S%z")


def create_payload(task: MarkdownTask, project_id: str, time_zone: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "projectId": project_id,
        "title": task.title,
        "isAllDay": task.due_time is None,
        "timeZone": time_zone,
        "priority": priority_name_to_ticktick(task.priority),
        "tags": task.tags,
    }
    if task.due:
        payload["dueDate"] = _api_datetime(task.due, task.due_time, time_zone)
    return payload


def update_payload(
    remote: TickTickTask,
    local_fields: dict[str, Any],
    time_zone: str,
) -> dict[str, Any]:
    payload = dict(remote.raw)
    payload.pop("id", None)
    payload["projectId"] = remote.project_id
    payload["title"] = local_fields["title"]
    payload["priority"] = priority_name_to_ticktick(local_fields["priority"])
    payload["tags"] = local_fields["tags"]
    payload["status"] = 0 if local_fields["status"] == "open" else remote.status
    due_time = (
        time.fromisoformat(local_fields["due_time"]) if local_fields.get("due_time") else None
    )
    payload["isAllDay"] = due_time is None
    payload["timeZone"] = time_zone
    due = local_fields["due"]
    payload["dueDate"] = (
        _api_datetime(date.fromisoformat(due), due_time, time_zone) if due else None
    )
    return payload


def changed_fields(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> set[str]:
    names = {"status", "title", "due", "due_time", "priority", "tags"}
    return {name for name in names if previous.get(name) != current.get(name)}


def create_fingerprint(task: MarkdownTask) -> str:
    return fields_hash({"path": task.path, "raw_line": task.raw_line})


def _replace_task_line(
    task: MarkdownTask,
    replacement: str,
    *,
    settings: Settings,
) -> None:
    path = Path(task.path)
    original = path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)
    index = task.line_number - 1
    if index < 0 or index >= len(lines) or lines[index] != task.raw_line:
        raise ReconcileError(f"stale Markdown task at {task.path}:{task.line_number}")
    lines[index] = replacement
    updated = "".join(lines)
    atomic_replace(
        path,
        updated,
        backup_dir=settings.state_dir / "backups",
    )


class Reconciler:
    def __init__(
        self,
        settings: Settings,
        store: StateStore,
        client: TickTickClient,
        *,
        project_id: str,
        dry_run: bool = False,
    ):
        self.settings = settings
        self.store = store
        self.client = client
        self.project_id = project_id
        self.dry_run = dry_run

    def _snapshot(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
        local_fields: dict[str, Any] | None = None,
        remote_fields: dict[str, Any] | None = None,
    ) -> TaskSnapshot:
        local_values = local_fields or task.normalized_fields
        remote_values = remote_fields or remote.normalized_fields
        return TaskSnapshot(
            task_id=remote.id,
            project_id=self.project_id,
            path=task.path,
            line_number=task.line_number,
            local_fields=local_values,
            remote_fields=remote_values,
            local_hash=fields_hash(local_values),
            remote_hash=fields_hash(remote_values),
            synced_at=datetime.now(UTC),
        )

    def _create_remote(
        self,
        task: MarkdownTask,
        remote_by_id: dict[str, TickTickTask],
    ) -> SyncAction:
        if self.dry_run:
            return SyncAction(
                kind="create_remote",
                path=task.path,
                line_number=task.line_number,
                reason="unmapped Markdown task",
            )
        fingerprint = create_fingerprint(task)
        pending = self.store.get_pending(fingerprint)
        if pending:
            remote = remote_by_id.get(pending["task_id"])
            if remote is None:
                return SyncAction(
                    kind="create_remote",
                    path=task.path,
                    line_number=task.line_number,
                    reason="pending remote creation is not visible yet",
                )
            updated_line = render_task(
                task,
                task_marker=marker(remote.id, self.project_id),
            )
            _replace_task_line(task, updated_line, settings=self.settings)
            self.store.upsert_snapshot(self._snapshot(task, remote))
            self.store.resolve_pending(fingerprint)
            return SyncAction(
                kind="create_remote",
                task_id=remote.id,
                path=task.path,
                line_number=task.line_number,
                reason="recovered pending remote creation",
            )
        payload = create_payload(task, self.project_id, self.settings.ticktick_time_zone)
        remote = self.client.create_task(payload)
        if task.completed:
            self.client.complete_task(self.project_id, remote.id)
            remote = remote.model_copy(update={"status": 2})
        self.store.add_pending(
            fingerprint=fingerprint,
            operation="create_remote",
            task_id=remote.id,
            project_id=self.project_id,
            path=task.path,
            line_number=task.line_number,
            payload=payload,
        )
        updated_line = render_task(
            task,
            task_marker=marker(remote.id, self.project_id),
        )
        try:
            _replace_task_line(task, updated_line, settings=self.settings)
        except BaseException:
            raise
        self.store.resolve_pending(fingerprint)
        self.store.upsert_snapshot(self._snapshot(task, remote))
        return SyncAction(
            kind="create_remote",
            task_id=remote.id,
            path=task.path,
            line_number=task.line_number,
        )

    def _update_remote(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
        local_fields: dict[str, Any],
        fields: set[str],
    ) -> SyncAction:
        if self.dry_run:
            return SyncAction(
                kind="update_remote",
                task_id=remote.id,
                path=task.path,
                line_number=task.line_number,
                fields=sorted(fields),
                reason="Markdown changed since last snapshot",
            )
        updated = self.client.update_task(
            remote.id,
            update_payload(remote, local_fields, self.settings.ticktick_time_zone),
        )
        if local_fields["status"] == "completed" and not remote.completed:
            self.client.complete_task(self.project_id, remote.id)
        self.store.upsert_snapshot(
            self._snapshot(
                task,
                updated,
                local_fields=local_fields,
                remote_fields=local_fields,
            )
        )
        return SyncAction(
            kind="update_remote",
            task_id=remote.id,
            path=task.path,
            line_number=task.line_number,
            fields=sorted(fields),
        )

    def _update_local(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
        fields: set[str],
    ) -> SyncAction:
        if self.dry_run:
            return SyncAction(
                kind="update_local",
                task_id=remote.id,
                path=task.path,
                line_number=task.line_number,
                fields=sorted(fields),
                reason="TickTick changed since last snapshot",
            )
        values = remote.normalized_fields
        replacement = render_task(
            task,
            checked=remote.completed,
            title=values["title"],
            due=date.fromisoformat(values["due"]) if values["due"] else None,
            due_time=time.fromisoformat(values["due_time"]) if values["due_time"] else None,
            priority=values["priority"],
            tags=values["tags"],
            task_marker=marker(remote.id, self.project_id),
        )
        _replace_task_line(task, replacement, settings=self.settings)
        self.store.upsert_snapshot(
            self._snapshot(
                task,
                remote,
                local_fields=values,
                remote_fields=values,
            )
        )
        return SyncAction(
            kind="update_local",
            task_id=remote.id,
            path=task.path,
            line_number=task.line_number,
            fields=sorted(fields),
        )

    def _conflict(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
        fields: set[str],
        local_fields: dict[str, Any],
        remote_fields: dict[str, Any],
    ) -> SyncAction:
        fingerprint = fields_hash(
            {
                "task_id": remote.id,
                "fields": sorted(fields),
                "local": local_fields,
                "remote": remote_fields,
            }
        )
        conflict = Conflict(
            task_id=remote.id,
            project_id=self.project_id,
            path=task.path,
            line_number=task.line_number,
            fields=sorted(fields),
            local_values=local_fields,
            remote_values=remote_fields,
            fingerprint=fingerprint,
            created_at=datetime.now(UTC),
        )
        if not self.dry_run:
            self.store.record_conflict(conflict)
        return SyncAction(
            kind="conflict",
            task_id=remote.id,
            path=task.path,
            line_number=task.line_number,
            fields=sorted(fields),
            reason="both sides changed the same fields",
        )

    def _reconcile_mapped(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
    ) -> SyncAction | None:
        local_fields = task.normalized_fields
        remote_fields = remote.normalized_fields
        snapshot = self.store.get_snapshot(remote.id)
        if snapshot is None:
            if not self.dry_run:
                self.store.upsert_snapshot(self._snapshot(task, remote))
            return None

        local_changes = changed_fields(snapshot.local_fields, local_fields)
        remote_changes = changed_fields(snapshot.remote_fields, remote_fields)
        conflicts = {
            field
            for field in local_changes & remote_changes
            if local_fields.get(field) != remote_fields.get(field)
        }
        if conflicts:
            return self._conflict(task, remote, conflicts, local_fields, remote_fields)
        if local_changes:
            return self._update_remote(task, remote, local_fields, local_changes)
        if remote_changes:
            return self._update_local(task, remote, remote_changes)
        if not self.dry_run and local_fields != snapshot.local_fields:
            self.store.upsert_snapshot(self._snapshot(task, remote))
        return None

    def _import_remote(self, remote: TickTickTask) -> SyncAction:
        target = self.settings.vault_path / "inbox/ticktick.md"
        if self.dry_run:
            return SyncAction(kind="import_local", task_id=remote.id, path=target.as_posix())
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            content = target.read_text(encoding="utf-8")
            prefix = "" if not content or content.endswith("\n") else "\n"
        else:
            content = ""
            prefix = ""
        values = remote.normalized_fields
        checkbox = "x" if remote.completed else " "
        due_value = values["due"] or ""
        if due_value and values["due_time"]:
            due_value += f"T{values['due_time']}"
        due = f" due:{due_value}" if due_value else ""
        priority = f" !{values['priority']}" if values["priority"] else ""
        tags = "".join(f" #{tag}" for tag in values["tags"])
        line = (
            f"- [{checkbox}] {values['title']}{due}{priority}{tags} "
            f"{marker(remote.id, self.project_id)}\n"
        )
        if target.exists():
            atomic_replace(
                target,
                content + prefix + line,
                backup_dir=self.settings.state_dir / "backups",
            )
        else:
            atomic_create(target, line)
        imported = MarkdownTask(
            path=target.as_posix(),
            line_number=len((content + prefix).splitlines()) + 1,
            raw_line=line,
            checkbox=checkbox,
            title=values["title"],
            due=date.fromisoformat(values["due"]) if values["due"] else None,
            due_time=time.fromisoformat(values["due_time"]) if values["due_time"] else None,
            priority=values["priority"],
            tags=values["tags"],
            task_id=remote.id,
            project_id=self.project_id,
            newline="\n",
        )
        self.store.upsert_snapshot(self._snapshot(imported, remote))
        return SyncAction(kind="import_local", task_id=remote.id, path=target.as_posix())

    def run(self, *, max_new_tasks: int = 5) -> list[SyncAction]:
        local_tasks = parse_vault(
            self.settings.vault_path,
            self.settings.task_paths,
            reference_date=datetime.now(self.settings.timezone).date(),
        )
        remote_tasks = self.client.list_project_tasks(self.project_id)
        remote_by_id = {task.id: task for task in remote_tasks}
        actions: list[SyncAction] = []
        mapped_ids: set[str] = set()
        remaining_new = max_new_tasks

        for local in local_tasks:
            if local.task_id is None:
                if not local.title:
                    continue
                if remaining_new <= 0:
                    continue
                actions.append(self._create_remote(local, remote_by_id))
                remaining_new -= 1
                continue
            if local.project_id != self.project_id:
                continue
            mapped_ids.add(local.task_id)
            remote = remote_by_id.get(local.task_id)
            if remote is None:
                try:
                    remote = self.client.get_project_task(self.project_id, local.task_id)
                except TickTickError as exc:
                    if exc.status_code != 404:
                        raise
            if remote is None:
                actions.append(
                    SyncAction(
                        kind="remote_task_missing",
                        task_id=local.task_id,
                        path=local.path,
                        line_number=local.line_number,
                    )
                )
                continue
            action = self._reconcile_mapped(local, remote)
            if action:
                actions.append(action)

        for remote in remote_tasks:
            if remote.id not in mapped_ids:
                if remaining_new <= 0:
                    continue
                actions.append(self._import_remote(remote))
                remaining_new -= 1
        return actions
