from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path

from .models import MarkdownTask

TASK_RE = re.compile(
    r"^(?P<indent>[ \t]*)-[ \t]+\[(?P<checkbox>[ xX/])\]"
    r"(?:[ \t]+(?P<body>.*?))?(?P<newline>\r?\n)?$"
)
MARKER_RE = re.compile(
    r"<!--\s*zt:v1\s+task=(?P<task>[^\s>]+)\s+project=(?P<project>[^\s>]+)\s*-->"
)
DUE_RE = re.compile(r"(?<!\S)due:(?P<due>\d{4}-\d{2}-\d{2})(?=\s|$)")
PRIORITY_RE = re.compile(r"(?<!\S)!(?P<priority>high|medium|low)(?=\s|$)", re.IGNORECASE)
TAG_RE = re.compile(r"(?<!\S)#(?P<tag>[A-Za-z0-9][A-Za-z0-9_/-]*)(?=\s|$)")
FENCE_RE = re.compile(r"^[ \t]*(?:\x60{3,}|~{3,})")


class MarkdownParseError(ValueError):
    pass


def _parse_task(path: Path, line_number: int, raw_line: str) -> MarkdownTask | None:
    match = TASK_RE.match(raw_line)
    if not match:
        return None

    newline = match.group("newline") or ""
    body = match.group("body") or ""
    marker_matches = list(MARKER_RE.finditer(body))
    if len(marker_matches) > 1 or ("zt:v1" in body and not marker_matches):
        raise MarkdownParseError(f"{path}:{line_number}: invalid or duplicate task marker")
    marker_match = marker_matches[0] if marker_matches else None
    task_id = marker_match.group("task") if marker_match else None
    project_id = marker_match.group("project") if marker_match else None

    clean_body = MARKER_RE.sub("", body).strip()
    due_match = DUE_RE.search(clean_body)
    due = None
    if due_match:
        try:
            due = date.fromisoformat(due_match.group("due"))
        except ValueError as exc:
            raise MarkdownParseError(
                f"{path}:{line_number}: invalid due date {due_match.group('due')!r}"
            ) from exc

    priority_match = PRIORITY_RE.search(clean_body)
    priority = priority_match.group("priority").lower() if priority_match else None
    tags = [match.group("tag") for match in TAG_RE.finditer(clean_body)]

    title = clean_body
    for pattern in (DUE_RE, PRIORITY_RE, TAG_RE):
        title = pattern.sub("", title)
    title = re.sub(r"[ \t]{2,}", " ", title).strip()

    return MarkdownTask(
        path=path.as_posix(),
        line_number=line_number,
        raw_line=raw_line,
        indent=match.group("indent") or "",
        checkbox=match.group("checkbox"),
        title=title,
        due=due,
        priority=priority,
        tags=tags,
        task_id=task_id,
        project_id=project_id,
        newline=newline,
    )


def parse_text(path: Path, text: str) -> list[MarkdownTask]:
    tasks: list[MarkdownTask] = []
    in_fence = False
    for line_number, raw_line in enumerate(text.splitlines(keepends=True), start=1):
        fence = FENCE_RE.match(raw_line)
        if fence:
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        task = _parse_task(path, line_number, raw_line)
        if task:
            tasks.append(task)
    return tasks


def parse_file(path: Path) -> list[MarkdownTask]:
    return parse_text(path, path.read_text(encoding="utf-8"))


def task_files(vault_path: Path, configured_paths: Iterable[str]) -> list[Path]:
    vault = vault_path.resolve()
    result: set[Path] = set()
    for relative in configured_paths:
        candidate = (vault / relative).resolve()
        try:
            candidate.relative_to(vault)
        except ValueError as exc:
            raise ValueError(f"configured path escapes vault: {relative}") from exc
        if candidate.is_dir():
            result.update(
                path for path in candidate.rglob("*.md") if ".sync-conflict-" not in path.name
            )
        elif candidate.is_file() and ".sync-conflict-" not in candidate.name:
            result.add(candidate)
    return sorted(result)


def parse_vault(vault_path: Path, configured_paths: Iterable[str]) -> list[MarkdownTask]:
    tasks: list[MarkdownTask] = []
    seen_markers: dict[tuple[str, str], MarkdownTask] = {}
    for path in task_files(vault_path, configured_paths):
        for task in parse_file(path):
            if task.task_id and task.project_id:
                key = (task.task_id, task.project_id)
                if key in seen_markers:
                    previous = seen_markers[key]
                    raise MarkdownParseError(
                        f"duplicate task marker at {previous.path}:{previous.line_number} "
                        f"and {task.path}:{task.line_number}"
                    )
                seen_markers[key] = task
            tasks.append(task)
    return tasks


def marker(task_id: str, project_id: str) -> str:
    return f"<!-- zt:v1 task={task_id} project={project_id} -->"


def render_task(
    task: MarkdownTask,
    *,
    checked: bool | None = None,
    title: str | None = None,
    due: date | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    task_marker: str | None = None,
) -> str:
    checkbox = "x" if (task.completed if checked is None else checked) else " "
    chosen_title = task.title if title is None else title
    chosen_due = task.due if due is None else due
    chosen_priority = task.priority if priority is None else priority
    chosen_tags = task.tags if tags is None else tags
    parts: list[str] = [chosen_title]
    if chosen_due:
        parts.append(f"due:{chosen_due.isoformat()}")
    if chosen_priority:
        parts.append(f"!{chosen_priority}")
    parts.extend(f"#{tag}" for tag in chosen_tags)
    if task_marker:
        parts.append(task_marker)
    body = " ".join(part for part in parts if part)
    suffix = f" {body}" if body else ""
    return f"{task.indent}- [{checkbox}]{suffix}{task.newline}"


def append_marker(task: MarkdownTask, task_id: str, project_id: str) -> str:
    if task.task_id and task.project_id:
        return task.raw_line
    newline = task.newline
    content = task.raw_line[: -len(newline)] if newline else task.raw_line
    return f"{content} {marker(task_id, project_id)}{newline}"
