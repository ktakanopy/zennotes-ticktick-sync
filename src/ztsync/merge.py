from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import Settings
from .markdown import marker, parse_vault, render_task
from .matching import markdown_match_key
from .models import MarkdownTask, TickTickTask
from .reconcile import changed_fields, update_payload
from .ticktick import TickTickClient, TickTickError
from .writer import atomic_replace


def _inbox_path(settings: Settings) -> Path:
    return (settings.vault_path.resolve() / "inbox/ticktick.md").resolve()


def _is_inbox(task: MarkdownTask, settings: Settings) -> bool:
    return Path(task.path).resolve() == _inbox_path(settings)


def _summary(task: MarkdownTask, root: Path) -> dict[str, Any]:
    return {
        "path": str(Path(task.path).resolve().relative_to(root)),
        "line_number": task.line_number,
        "task_id": task.task_id,
        "completed": task.completed,
    }


def _assert_current_task(task: MarkdownTask) -> None:
    path = Path(task.path)
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    index = task.line_number - 1
    if index < 0 or index >= len(lines) or lines[index] != task.raw_line:
        raise RuntimeError(f"stale Markdown task at {task.path}:{task.line_number}")


def find_duplicate_groups(
    settings: Settings,
    project_id: str,
) -> list[dict[str, Any]]:
    root = settings.vault_path.resolve()
    tasks = parse_vault(
        settings.vault_path,
        settings.task_paths,
    )
    groups: defaultdict[tuple[Any, ...], list[MarkdownTask]] = defaultdict(list)
    for task in tasks:
        if task.title and not task.is_subtask and task.project_id in {None, project_id}:
            groups[markdown_match_key(task)].append(task)

    result: list[dict[str, Any]] = []
    for candidates in groups.values():
        inbox_tasks = [task for task in candidates if _is_inbox(task, settings)]
        canonical_tasks = [task for task in candidates if not _is_inbox(task, settings)]
        if not inbox_tasks or len(canonical_tasks) != 1:
            continue
        if not any(task.task_id for task in candidates):
            continue
        canonical = canonical_tasks[0]
        result.append(
            {
                "title": canonical.title,
                "canonical": _summary(canonical, root),
                "duplicates": [_summary(task, root) for task in inbox_tasks],
            }
        )
    return result


def _task_by_id(tasks: list[MarkdownTask], task_id: str) -> MarkdownTask | None:
    return next((task for task in tasks if task.task_id == task_id), None)


def apply_duplicate_groups(
    settings: Settings,
    client: TickTickClient,
    project_id: str,
    groups: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    tasks = parse_vault(settings.vault_path, settings.task_paths)
    task_by_location = {(task.path, task.line_number): task for task in tasks}
    prepared: list[dict[str, Any]] = []

    for group in groups:
        canonical_info = group["canonical"]
        canonical = task_by_location.get(
            (
                str(settings.vault_path.resolve() / canonical_info["path"]),
                canonical_info["line_number"],
            )
        )
        if canonical is None:
            canonical = _task_by_id(tasks, canonical_info["task_id"])
        duplicate_tasks = [
            task_by_location.get(
                (str(settings.vault_path.resolve() / duplicate["path"]), duplicate["line_number"])
            )
            for duplicate in group["duplicates"]
        ]
        duplicates = [task for task in duplicate_tasks if task is not None]
        if canonical is None or not duplicates:
            continue

        ids = {task.task_id for task in duplicates if task.task_id}
        canonical_id = canonical.task_id or (next(iter(ids)) if len(ids) == 1 else None)
        if not canonical_id or canonical.project_id not in {None, project_id}:
            continue
        _assert_current_task(canonical)
        for duplicate in duplicates:
            _assert_current_task(duplicate)
        duplicate_ids = sorted(task_id for task_id in ids if task_id != canonical_id)
        if not duplicate_ids:
            prepared.append(
                {
                    "canonical": canonical,
                    "canonical_id": canonical_id,
                    "duplicates": duplicates,
                    "duplicate_ids": [],
                    "remote": None,
                }
            )
            continue

        try:
            canonical_remote = client.get_project_task(project_id, canonical_id)
            duplicate_remotes = [
                client.get_project_task(project_id, task_id) for task_id in duplicate_ids
            ]
        except TickTickError:
            raise
        prepared.append(
            {
                "canonical": canonical,
                "canonical_id": canonical_id,
                "duplicates": duplicates,
                "duplicate_ids": duplicate_ids,
                "remote": canonical_remote,
                "duplicate_remotes": duplicate_remotes,
            }
        )

    outcomes: list[dict[str, Any]] = []
    removals_by_path: defaultdict[str, list[MarkdownTask]] = defaultdict(list)
    for item in prepared:
        canonical = item["canonical"]
        canonical_id = item["canonical_id"]
        duplicate_ids = item["duplicate_ids"]
        canonical_remote: TickTickTask | None = item.get("remote")
        duplicate_remotes: list[TickTickTask] = item.get("duplicate_remotes", [])
        merged_completed = canonical.completed or any(
            remote.completed for remote in duplicate_remotes
        )

        if canonical_remote:
            desired_fields = dict(canonical.normalized_fields)
            desired_fields["status"] = "completed" if merged_completed else "open"
            fields = changed_fields(canonical_remote.normalized_fields, desired_fields)
            if fields:
                canonical_remote = client.update_task(
                    canonical_id,
                    update_payload(
                        canonical_remote,
                        desired_fields,
                        settings.ticktick_time_zone,
                    ),
                )
                if desired_fields["status"] == "completed" and not canonical_remote.completed:
                    client.complete_task(project_id, canonical_id)
                    canonical_remote = canonical_remote.model_copy(update={"status": 2})

        for duplicate_id in duplicate_ids:
            client.delete_task(project_id, duplicate_id)

        canonical_line = render_task(
            canonical,
            checked=merged_completed,
            task_marker=marker(canonical_id, project_id),
        )
        if canonical.raw_line != canonical_line:
            replacement_path = Path(canonical.path)
            content = replacement_path.read_text(encoding="utf-8")
            lines = content.splitlines(keepends=True)
            index = canonical.line_number - 1
            if index < 0 or index >= len(lines) or lines[index] != canonical.raw_line:
                raise RuntimeError(
                    f"stale Markdown task at {canonical.path}:{canonical.line_number}"
                )
            lines[index] = canonical_line
            atomic_replace(
                replacement_path,
                "".join(lines),
                backup_dir=settings.state_dir / "backups",
            )

        for duplicate in item["duplicates"]:
            removals_by_path[duplicate.path].append(duplicate)

        outcomes.append(
            {
                "title": canonical.title,
                "canonical_task_id": canonical_id,
                "deleted_duplicate_task_ids": duplicate_ids,
                "removed_duplicate_lines": [
                    _summary(task, settings.vault_path.resolve()) for task in item["duplicates"]
                ],
            }
        )

    for path_text, remove_tasks in removals_by_path.items():
        path = Path(path_text)
        content = path.read_text(encoding="utf-8")
        lines = content.splitlines(keepends=True)
        for duplicate in sorted(remove_tasks, key=lambda task: task.line_number, reverse=True):
            index = duplicate.line_number - 1
            if index < 0 or index >= len(lines) or lines[index] != duplicate.raw_line:
                raise RuntimeError(f"stale Markdown task at {path}:{duplicate.line_number}")
            del lines[index]
        atomic_replace(
            path,
            "".join(lines),
            backup_dir=settings.state_dir / "backups",
        )
    return outcomes
