from datetime import date, time
from pathlib import Path

import pytest

from ztsync.markdown import MarkdownParseError, append_marker, parse_text, render_task


def test_parser_extracts_fields_and_ignores_fence() -> None:
    path = Path("daily.md")
    text = (
        "# Daily\n"
        "- [ ] Check SMS Hamming problem due:2026-08-26 !high #work #ai\n"
        "\n"
        "\x60\x60\x60md\n"
        "- [ ] this is an example\n"
        "\x60\x60\x60\n"
        "- [x] Finished research #work\n"
    )
    tasks = parse_text(path, text)

    assert len(tasks) == 2
    assert tasks[0].title == "Check SMS Hamming problem"
    assert tasks[0].due == date(2026, 8, 26)
    assert tasks[0].priority == "high"
    assert tasks[0].tags == ["work", "ai"]
    assert tasks[1].completed


def test_parser_extracts_natural_due_time_and_tags() -> None:
    task = parse_text(
        Path("daily.md"),
        "- [ ] Send report tomorrow 18am #work #ai\n",
        reference_date=date(2026, 8, 26),
    )[0]

    assert task.title == "Send report"
    assert task.due == date(2026, 8, 27)
    assert task.due_time == time(18, 0)
    assert task.tags == ["work", "ai"]


def test_parser_accepts_canonical_due_time_and_renders_it() -> None:
    task = parse_text(Path("daily.md"), "- [ ] Meeting due:2026-08-27T09:30\n")[0]

    assert task.due == date(2026, 8, 27)
    assert task.due_time == time(9, 30)
    assert "due:2026-08-27T09:30" in render_task(task)


def test_marker_and_render_preserve_identity() -> None:
    task = parse_text(Path("daily.md"), "- [ ] Existing task due:2026-08-26\n")[0]
    marked = append_marker(task, "task-1", "project-1")
    mapped = parse_text(Path("daily.md"), marked)[0]

    assert mapped.task_id == "task-1"
    assert mapped.project_id == "project-1"
    assert render_task(mapped, checked=True).startswith("- [x] Existing task")


def test_parser_tracks_nested_task_and_item_marker() -> None:
    tasks = parse_text(
        Path("daily.md"),
        "- [ ] Parent\n  - [ ] Child <!-- zt:v1 task=parent-1 item=item-1 project=project-1 -->\n",
    )

    assert tasks[0].parent_line_number is None
    assert tasks[1].parent_line_number == 1
    assert tasks[1].task_id == "parent-1"
    assert tasks[1].item_id == "item-1"
    assert tasks[1].is_subtask


def test_invalid_due_date_is_rejected() -> None:
    with pytest.raises(MarkdownParseError):
        parse_text(Path("daily.md"), "- [ ] Bad due:2026-02-31\n")


def test_crlf_is_preserved() -> None:
    task = parse_text(Path("daily.md"), "- [ ] CRLF\r\n")[0]
    assert task.newline == "\r\n"
    assert render_task(task, checked=True).endswith("\r\n")


def test_partial_or_duplicate_marker_is_rejected() -> None:
    with pytest.raises(MarkdownParseError):
        parse_text(Path("daily.md"), "- [ ] Bad <!-- zt:v1 task=task-1 -->\n")
    with pytest.raises(MarkdownParseError):
        parse_text(
            Path("daily.md"),
            "- [ ] Bad <!-- zt:v1 task=task-1 project=project-1 --> "
            "<!-- zt:v1 task=task-2 project=project-1 -->\n",
        )
