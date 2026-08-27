from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .config import Settings
from .markdown import marker, parse_vault, render_task
from .matching import markdown_match_key, ticktick_match_key
from .models import (
    Conflict,
    MarkdownTask,
    SyncAction,
    TaskSnapshot,
    TickTickItem,
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


def create_payload(
    task: MarkdownTask,
    project_id: str,
    time_zone: str,
    items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
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
    if items is not None:
        payload["items"] = items
    return payload


def update_payload(
    remote: TickTickTask,
    local_fields: dict[str, Any],
    time_zone: str,
    items: list[dict[str, Any]] | None = None,
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
    if items is not None:
        payload["items"] = items
    return payload


def changed_fields(
    previous: dict[str, Any],
    current: dict[str, Any],
) -> set[str]:
    names = {"status", "title", "due", "due_time", "priority", "tags", "items"}
    return {name for name in names if previous.get(name) != current.get(name)}


def _local_item_fields(task: MarkdownTask) -> dict[str, str | None]:
    return {
        "title": task.title,
        "status": "completed" if task.completed else "open",
        "due": task.due.isoformat() if task.due else None,
        "due_time": task.due_time.strftime("%H:%M") if task.due_time else None,
    }


def _remote_item_fields(item: TickTickItem) -> dict[str, str | None]:
    return item.normalized_fields


def _local_tree_fields(task: MarkdownTask, children: list[MarkdownTask]) -> dict[str, Any]:
    fields = dict(task.normalized_fields)
    fields["items"] = [_local_item_fields(child) for child in children]
    return fields


def _remote_tree_fields(remote: TickTickTask) -> dict[str, Any]:
    fields = dict(remote.normalized_fields)
    fields["items"] = [_remote_item_fields(item) for item in remote.items]
    return fields


def _item_title_key(value: str) -> str:
    return " ".join(value.casefold().split())


def _match_items(
    children: list[MarkdownTask],
    remote_items: list[TickTickItem],
) -> list[TickTickItem | None]:
    used: set[str] = set()
    matches: list[TickTickItem | None] = []
    for index, child in enumerate(children):
        match = None
        if child.item_id:
            match = next((item for item in remote_items if item.id == child.item_id), None)
        elif len(children) == len(remote_items):
            candidate = remote_items[index]
            if candidate.id not in used:
                match = candidate
        else:
            candidates = [
                item
                for item in remote_items
                if item.id not in used
                and _item_title_key(item.title) == _item_title_key(child.title)
            ]
            if len(candidates) == 1:
                match = candidates[0]
        if match:
            used.add(match.id)
        matches.append(match)
    return matches


def _items_payload(
    children: list[MarkdownTask],
    remote_items: list[TickTickItem],
    time_zone: str,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for index, (child, match) in enumerate(
        zip(children, _match_items(children, remote_items), strict=True)
    ):
        payload: dict[str, Any] = {
            "title": child.title,
            "status": 1 if child.completed else 0,
            "sortOrder": index,
            "isAllDay": child.due_time is None,
            "timeZone": time_zone,
        }
        if child.due:
            payload["startDate"] = _api_datetime(child.due, child.due_time, time_zone)
        if match:
            payload["id"] = match.id
        payloads.append(payload)
    return payloads


def _render_item_line(
    parent: MarkdownTask,
    item: TickTickItem,
    parent_id: str,
    project_id: str,
    indent: str,
) -> str:
    checkbox = "x" if item.completed else " "
    values = item.normalized_fields
    due_value = values["due"] or ""
    if due_value and values["due_time"]:
        due_value += f"T{values['due_time']}"
    due = f" due:{due_value}" if due_value else ""
    return (
        f"{indent}- [{checkbox}] {item.title.strip()}{due} "
        f"{marker(parent_id, project_id, item.id)}{parent.newline}"
    )


def _replace_task_tree_from_remote(
    parent: MarkdownTask,
    children: list[MarkdownTask],
    remote: TickTickTask,
    project_id: str,
    *,
    settings: Settings,
) -> None:
    path = Path(parent.path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    parent_index = parent.line_number - 1
    if parent_index < 0 or parent_index >= len(lines) or lines[parent_index] != parent.raw_line:
        raise ReconcileError(f"stale Markdown task at {parent.path}:{parent.line_number}")

    child_indexes = sorted((child.line_number - 1 for child in children), reverse=True)
    for index, child in zip(
        child_indexes,
        sorted(children, key=lambda item: item.line_number, reverse=True),
        strict=True,
    ):
        if index < 0 or index >= len(lines) or lines[index] != child.raw_line:
            raise ReconcileError(f"stale Markdown task at {child.path}:{child.line_number}")

    newline = parent.newline or "\n"
    parent_for_render = parent.model_copy(update={"newline": newline})
    replacement = render_task(
        parent_for_render,
        checked=remote.completed,
        title=remote.title,
        due=remote.due_date,
        due_time=remote.due_time,
        priority=remote.normalized_fields["priority"],
        tags=remote.tags,
        task_marker=marker(remote.id, project_id),
    )
    lines[parent_index] = replacement
    insertion_index = parent_index + 1
    for index in child_indexes:
        if index < insertion_index:
            insertion_index -= 1
        del lines[index]

    indent = children[0].indent if children else f"{parent.indent}  "
    item_lines = [
        _render_item_line(parent_for_render, item, remote.id, project_id, indent)
        for item in remote.items
    ]
    lines[insertion_index:insertion_index] = item_lines
    atomic_replace(
        path,
        "".join(lines),
        backup_dir=settings.state_dir / "backups",
    )


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

    def _children(
        self, task: MarkdownTask, children_by_parent: dict[tuple[str, int], list[MarkdownTask]]
    ) -> list[MarkdownTask]:
        return [
            child
            for child in children_by_parent.get((task.path, task.line_number), [])
            if child.title
        ]

    def _refresh_remote_items(
        self,
        remote: TickTickTask,
        children: list[MarkdownTask],
    ) -> TickTickTask:
        if not children:
            return remote
        if len(remote.items) == len(children) and all(item.id for item in remote.items):
            return remote
        refreshed = self.client.get_project_task(self.project_id, remote.id)
        if len(refreshed.items) != len(children) or not all(item.id for item in refreshed.items):
            raise ReconcileError(f"TickTick did not return checklist items for task {remote.id}")
        return refreshed

    def _write_item_markers(
        self,
        parent: MarkdownTask,
        children: list[MarkdownTask],
        remote: TickTickTask,
    ) -> None:
        matches = _match_items(children, remote.items)
        for child, item in zip(children, matches, strict=True):
            if item is None:
                raise ReconcileError(
                    f"could not map ZenNotes subtask at {child.path}:{child.line_number}"
                )
            replacement = render_task(
                child,
                task_marker=marker(remote.id, self.project_id, item.id),
            )
            if replacement != child.raw_line:
                _replace_task_line(child, replacement, settings=self.settings)

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
        children: list[MarkdownTask],
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
            remote = self._refresh_remote_items(remote, children)
            updated_line = render_task(
                task,
                task_marker=marker(remote.id, self.project_id),
            )
            _replace_task_line(task, updated_line, settings=self.settings)
            self._write_item_markers(task, children, remote)
            linked_task = task.model_copy(
                update={
                    "raw_line": updated_line,
                    "task_id": remote.id,
                    "project_id": self.project_id,
                }
            )
            self.store.upsert_snapshot(
                self._snapshot(
                    linked_task,
                    remote,
                    local_fields=_local_tree_fields(task, children),
                    remote_fields=_remote_tree_fields(remote),
                )
            )
            self.store.resolve_pending(fingerprint)
            return SyncAction(
                kind="create_remote",
                task_id=remote.id,
                path=task.path,
                line_number=task.line_number,
                reason="recovered pending remote creation",
            )
        payload = create_payload(
            task,
            self.project_id,
            self.settings.ticktick_time_zone,
            items=_items_payload(children, [], self.settings.ticktick_time_zone) or None,
        )
        remote = self.client.create_task(payload)
        if task.completed:
            self.client.complete_task(self.project_id, remote.id)
            remote = remote.model_copy(update={"status": 2})
        remote = self._refresh_remote_items(remote, children)
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
        self._write_item_markers(task, children, remote)
        self.store.resolve_pending(fingerprint)
        linked_task = task.model_copy(
            update={"raw_line": updated_line, "task_id": remote.id, "project_id": self.project_id}
        )
        self.store.upsert_snapshot(
            self._snapshot(
                linked_task,
                remote,
                local_fields=_local_tree_fields(task, children),
                remote_fields=_remote_tree_fields(remote),
            )
        )
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
        children: list[MarkdownTask],
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
        items = (
            _items_payload(children, remote.items, self.settings.ticktick_time_zone)
            if "items" in fields
            else None
        )
        updated = self.client.update_task(
            remote.id,
            update_payload(remote, local_fields, self.settings.ticktick_time_zone, items=items),
        )
        if local_fields["status"] == "completed" and not remote.completed:
            self.client.complete_task(self.project_id, remote.id)
            updated = updated.model_copy(update={"status": 2})
        updated = self._refresh_remote_items(updated, children)
        if children:
            self._write_item_markers(task, children, updated)
        self.store.upsert_snapshot(
            self._snapshot(
                task,
                updated,
                local_fields=local_fields,
                remote_fields=_remote_tree_fields(updated),
            )
        )
        return SyncAction(
            kind="update_remote",
            task_id=remote.id,
            path=task.path,
            line_number=task.line_number,
            fields=sorted(fields),
        )

    def _link_local(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
        children: list[MarkdownTask],
    ) -> SyncAction:
        local_fields = _local_tree_fields(task, children)
        remote_fields = _remote_tree_fields(remote)
        fields = changed_fields(remote_fields, local_fields)
        if self.dry_run:
            return SyncAction(
                kind="link_local",
                task_id=remote.id,
                path=task.path,
                line_number=task.line_number,
                fields=sorted(fields),
                reason="matched an existing TickTick task by content",
            )

        updated_remote = remote
        if not children and remote.items:
            parent_fields = {name: value for name, value in local_fields.items() if name != "items"}
            parent_changes = changed_fields(remote.normalized_fields, parent_fields)
            if parent_changes:
                updated_remote = self.client.update_task(
                    remote.id,
                    update_payload(remote, parent_fields, self.settings.ticktick_time_zone),
                )
                if parent_fields["status"] == "completed" and not remote.completed:
                    self.client.complete_task(self.project_id, remote.id)
                    updated_remote = updated_remote.model_copy(update={"status": 2})
                if remote.items and not updated_remote.items:
                    updated_remote = self.client.get_project_task(self.project_id, remote.id)
            _replace_task_tree_from_remote(
                task,
                children,
                updated_remote,
                self.project_id,
                settings=self.settings,
            )
            local_fields = _remote_tree_fields(updated_remote)
        elif fields:
            items = (
                _items_payload(children, remote.items, self.settings.ticktick_time_zone)
                if "items" in fields
                else None
            )
            updated_remote = self.client.update_task(
                remote.id,
                update_payload(
                    remote,
                    local_fields,
                    self.settings.ticktick_time_zone,
                    items=items,
                ),
            )
            if local_fields["status"] == "completed" and not remote.completed:
                self.client.complete_task(self.project_id, remote.id)
                updated_remote = updated_remote.model_copy(update={"status": 2})
            updated_remote = self._refresh_remote_items(updated_remote, children)
            local_fields = _local_tree_fields(task, children)
            updated_line = render_task(
                task,
                task_marker=marker(remote.id, self.project_id),
            )
            _replace_task_line(task, updated_line, settings=self.settings)
            self._write_item_markers(task, children, updated_remote)
        else:
            updated_line = render_task(
                task,
                task_marker=marker(remote.id, self.project_id),
            )
            _replace_task_line(task, updated_line, settings=self.settings)
            if children:
                updated_remote = self._refresh_remote_items(updated_remote, children)
                self._write_item_markers(task, children, updated_remote)
            local_fields = _local_tree_fields(task, children)

        linked_task = task.model_copy(
            update={
                "raw_line": render_task(task, task_marker=marker(remote.id, self.project_id)),
                "task_id": remote.id,
                "project_id": self.project_id,
            }
        )
        self.store.upsert_snapshot(
            self._snapshot(
                linked_task,
                updated_remote,
                local_fields=local_fields,
                remote_fields=_remote_tree_fields(updated_remote),
            )
        )
        return SyncAction(
            kind="link_local",
            task_id=remote.id,
            path=task.path,
            line_number=task.line_number,
            fields=sorted(fields),
            reason="matched an existing TickTick task by content",
        )

    def _update_local(
        self,
        task: MarkdownTask,
        remote: TickTickTask,
        fields: set[str],
        children: list[MarkdownTask],
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
        if remote.items or children:
            _replace_task_tree_from_remote(
                task,
                children,
                remote,
                self.project_id,
                settings=self.settings,
            )
        else:
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
                local_fields=_remote_tree_fields(remote),
                remote_fields=_remote_tree_fields(remote),
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
        children: list[MarkdownTask],
    ) -> SyncAction | None:
        local_fields = _local_tree_fields(task, children)
        remote_fields = _remote_tree_fields(remote)
        snapshot = self.store.get_snapshot(remote.id)
        if snapshot is None:
            if children:
                return self._update_remote(task, remote, local_fields, {"items"}, children)
            if remote.items:
                return self._update_local(task, remote, {"items"}, children)
            if not self.dry_run:
                self.store.upsert_snapshot(
                    self._snapshot(
                        task,
                        remote,
                        local_fields=local_fields,
                        remote_fields=remote_fields,
                    )
                )
            return None

        local_changes = changed_fields(snapshot.local_fields, local_fields)
        remote_changes = changed_fields(snapshot.remote_fields, remote_fields)
        if "items" not in snapshot.local_fields:
            local_changes.discard("items")
            remote_changes.discard("items")
            if children:
                local_changes.add("items")
            elif remote.items:
                remote_changes.add("items")
        conflicts = {
            field
            for field in local_changes & remote_changes
            if local_fields.get(field) != remote_fields.get(field)
        }
        if conflicts:
            return self._conflict(task, remote, conflicts, local_fields, remote_fields)
        if local_changes:
            return self._update_remote(task, remote, local_fields, local_changes, children)
        if remote_changes:
            return self._update_local(task, remote, remote_changes, children)
        if not self.dry_run and local_fields != snapshot.local_fields:
            self.store.upsert_snapshot(
                self._snapshot(
                    task,
                    remote,
                    local_fields=local_fields,
                    remote_fields=remote_fields,
                )
            )
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
        item_lines = [
            _render_item_line(imported, item, remote.id, self.project_id, "  ")
            for item in remote.items
        ]
        updated_content = content + prefix + line + "".join(item_lines)
        if target.exists():
            atomic_replace(
                target,
                updated_content,
                backup_dir=self.settings.state_dir / "backups",
            )
        else:
            atomic_create(target, updated_content)
        tree_fields = _remote_tree_fields(remote)
        self.store.upsert_snapshot(
            self._snapshot(
                imported,
                remote,
                local_fields=tree_fields,
                remote_fields=tree_fields,
            )
        )
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
        children_by_parent: defaultdict[tuple[str, int], list[MarkdownTask]] = defaultdict(list)
        local_by_location = {(local.path, local.line_number): local for local in local_tasks}
        for local in local_tasks:
            if local.parent_line_number is not None:
                parent = local_by_location.get((local.path, local.parent_line_number))
                if parent and parent.parent_line_number is not None:
                    raise ReconcileError("TickTick checklist items support one nesting level")
                children_by_parent[(local.path, local.parent_line_number)].append(local)

        mapped_ids = {
            local.task_id
            for local in local_tasks
            if local.task_id and local.project_id == self.project_id and not local.is_subtask
        }
        daily_mapped_by_key: defaultdict[tuple[Any, ...], list[MarkdownTask]] = defaultdict(list)
        unmarked_by_key: defaultdict[tuple[Any, ...], list[MarkdownTask]] = defaultdict(list)
        for local in local_tasks:
            if local.is_subtask:
                continue
            if not local.title:
                continue
            key = markdown_match_key(local)
            if local.task_id and local.project_id == self.project_id:
                if (
                    Path(local.path).resolve()
                    != (self.settings.vault_path.resolve() / "inbox/ticktick.md").resolve()
                ):
                    daily_mapped_by_key[key].append(local)
            elif local.task_id is None:
                unmarked_by_key[key].append(local)

        remote_by_key: defaultdict[tuple[Any, ...], list[TickTickTask]] = defaultdict(list)
        for remote in remote_tasks:
            if remote.id not in mapped_ids:
                remote_by_key[ticktick_match_key(remote)].append(remote)

        linked_by_location: dict[tuple[str, int], TickTickTask] = {}
        ambiguous_keys: set[tuple[Any, ...]] = set()
        for key, candidates in unmarked_by_key.items():
            available_remote = remote_by_key.get(key, [])
            if len(candidates) == 1 and len(available_remote) == 1:
                linked_by_location[(candidates[0].path, candidates[0].line_number)] = (
                    available_remote[0]
                )
            elif available_remote:
                ambiguous_keys.add(key)

        remaining_new = max_new_tasks

        for local in local_tasks:
            if local.is_subtask:
                continue
            children = self._children(local, children_by_parent)
            if local.task_id is None:
                if not local.title:
                    continue
                key = markdown_match_key(local)
                location = (local.path, local.line_number)
                if key in ambiguous_keys:
                    actions.append(
                        SyncAction(
                            kind="ambiguous_match",
                            path=local.path,
                            line_number=local.line_number,
                            reason="multiple local or TickTick tasks match by content",
                        )
                    )
                    continue
                linked_remote = linked_by_location.get(location)
                if linked_remote:
                    actions.append(self._link_local(local, linked_remote, children))
                    mapped_ids.add(linked_remote.id)
                    continue
                if remaining_new <= 0:
                    continue
                actions.append(self._create_remote(local, children, remote_by_id))
                remaining_new -= 1
                continue
            if local.project_id != self.project_id:
                continue
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
            action = self._reconcile_mapped(local, remote, children)
            if action:
                actions.append(action)

        for remote in remote_tasks:
            if remote.id not in mapped_ids:
                matching_local = daily_mapped_by_key.get(ticktick_match_key(remote), [])
                if matching_local:
                    actions.append(
                        SyncAction(
                            kind="ambiguous_match",
                            task_id=remote.id,
                            reason=(
                                "remote task matches a daily-notes task; do not import it to inbox"
                            ),
                        )
                    )
                    continue
                if remaining_new <= 0:
                    continue
                actions.append(self._import_remote(remote))
                remaining_new -= 1
        return actions
