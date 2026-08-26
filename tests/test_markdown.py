from datetime import date
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


def test_marker_and_render_preserve_identity() -> None:
    task = parse_text(Path("daily.md"), "- [ ] Existing task due:2026-08-26\n")[0]
    marked = append_marker(task, "task-1", "project-1")
    mapped = parse_text(Path("daily.md"), marked)[0]

    assert mapped.task_id == "task-1"
    assert mapped.project_id == "project-1"
    assert render_task(mapped, checked=True).startswith("- [x] Existing task")


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
