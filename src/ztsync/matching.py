from __future__ import annotations

import re
from typing import Any

from .models import MarkdownTask, TickTickTask

URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _normalized_title(value: str) -> str:
    without_urls = URL_RE.sub(" ", value)
    return " ".join(without_urls.casefold().split())


def match_key(fields: dict[str, Any]) -> tuple[Any, ...]:
    return (
        _normalized_title(str(fields.get("title") or "")),
        fields.get("due"),
        fields.get("due_time"),
        fields.get("priority"),
        tuple(sorted(fields.get("tags") or [])),
    )


def markdown_match_key(task: MarkdownTask) -> tuple[Any, ...]:
    return match_key(task.normalized_fields)


def ticktick_match_key(task: TickTickTask) -> tuple[Any, ...]:
    return match_key(task.normalized_fields)
