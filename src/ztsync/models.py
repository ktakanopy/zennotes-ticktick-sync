from __future__ import annotations

from datetime import date, datetime, time
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

Priority = Literal["low", "medium", "high"]
TaskStatus = Literal["open", "in_progress", "completed"]


class MarkdownTask(BaseModel):
    model_config = ConfigDict(frozen=True)

    path: str
    line_number: int = Field(ge=1)
    raw_line: str
    indent: str = ""
    checkbox: Literal[" ", "x", "X", "/"]
    title: str
    due: date | None = None
    due_time: time | None = None
    priority: Priority | None = None
    tags: list[str] = Field(default_factory=list)
    task_id: str | None = None
    project_id: str | None = None
    newline: Literal["\n", "\r\n", ""] = "\n"

    @property
    def completed(self) -> bool:
        return self.checkbox in {"x", "X"}

    @property
    def status(self) -> TaskStatus:
        if self.completed:
            return "completed"
        if self.checkbox == "/":
            return "in_progress"
        return "open"

    @property
    def normalized_fields(self) -> dict[str, Any]:
        return {
            "status": "completed" if self.completed else "open",
            "title": self.title,
            "due": self.due.isoformat() if self.due else None,
            "due_time": self.due_time.strftime("%H:%M") if self.due_time else None,
            "priority": self.priority,
            "tags": sorted(set(self.tags)),
        }


class TickTickTask(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    project_id: str
    title: str
    status: int = 0
    due_date: date | None = None
    due_time: time | None = None
    priority: int = 0
    tags: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def completed(self) -> bool:
        return self.status == 2

    @property
    def normalized_fields(self) -> dict[str, Any]:
        return {
            "status": "completed" if self.completed else "open",
            "title": self.title.strip(),
            "due": self.due_date.isoformat() if self.due_date else None,
            "due_time": self.due_time.strftime("%H:%M") if self.due_time else None,
            "priority": ticktick_priority_to_name(self.priority),
            "tags": sorted(set(self.tags)),
        }

    @classmethod
    def from_api(cls, payload: dict[str, Any]) -> TickTickTask:
        due_date = payload.get("dueDate") or payload.get("due_date")
        parsed_due = date.fromisoformat(str(due_date)[:10]) if due_date else None
        parsed_due_time = None
        if due_date and "T" in str(due_date) and not payload.get("isAllDay", False):
            parsed_datetime = datetime.fromisoformat(str(due_date).replace("Z", "+00:00"))
            time_zone = payload.get("timeZone")
            if time_zone and parsed_datetime.tzinfo:
                parsed_datetime = parsed_datetime.astimezone(ZoneInfo(str(time_zone)))
            parsed_due = parsed_datetime.date()
            parsed_due_time = parsed_datetime.time().replace(second=0, microsecond=0)
        raw_tags = payload.get("tags") or []
        tags = sorted(str(tag) for tag in raw_tags)
        return cls(
            id=str(payload["id"]),
            project_id=str(payload.get("projectId") or payload.get("project_id") or ""),
            title=str(payload.get("title") or ""),
            status=int(payload.get("status") or 0),
            due_date=parsed_due,
            due_time=parsed_due_time,
            priority=int(payload.get("priority") or 0),
            tags=tags,
            raw=dict(payload),
        )


class TaskSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    task_id: str
    project_id: str
    path: str
    line_number: int = Field(ge=1)
    local_fields: dict[str, Any]
    remote_fields: dict[str, Any]
    local_hash: str
    remote_hash: str
    synced_at: datetime


class Conflict(BaseModel):
    task_id: str
    project_id: str
    path: str | None = None
    line_number: int | None = None
    fields: list[str]
    local_values: dict[str, Any]
    remote_values: dict[str, Any]
    fingerprint: str
    created_at: datetime


class SyncAction(BaseModel):
    kind: Literal[
        "create_remote",
        "update_remote",
        "complete_remote",
        "update_local",
        "import_local",
        "conflict",
        "local_task_missing",
        "remote_task_missing",
    ]
    task_id: str | None = None
    path: str | None = None
    line_number: int | None = None
    fields: list[str] = Field(default_factory=list)
    reason: str | None = None


def ticktick_priority_to_name(priority: int) -> Priority | None:
    if priority >= 5:
        return "high"
    if priority >= 3:
        return "medium"
    if priority >= 1:
        return "low"
    return None


def priority_name_to_ticktick(priority: Priority | None) -> int:
    return {"low": 1, "medium": 3, "high": 5}.get(priority, 0)
