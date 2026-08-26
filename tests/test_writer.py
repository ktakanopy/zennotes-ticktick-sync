import pytest

from ztsync.writer import atomic_create, atomic_replace


def test_atomic_replace_creates_backup_and_preserves_content(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("old\n", encoding="utf-8")
    backup_dir = tmp_path / "backups"

    backup = atomic_replace(path, "new\n", backup_dir=backup_dir)

    assert path.read_text(encoding="utf-8") == "new\n"
    assert backup.read_text(encoding="utf-8") == "old\n"
    assert not list(tmp_path.glob(".note.md.*.tmp"))


def test_atomic_replace_keeps_original_if_validation_fails(tmp_path):
    path = tmp_path / "note.md"
    path.write_text("old\n", encoding="utf-8")

    with pytest.raises(ValueError):
        atomic_replace(
            path,
            "new\n",
            backup_dir=tmp_path / "backups",
            validator=lambda _: (_ for _ in ()).throw(ValueError("invalid")),
        )

    assert path.read_text(encoding="utf-8") == "old\n"


def test_atomic_create_is_exclusive(tmp_path):
    path = tmp_path / "new.md"
    atomic_create(path, "content\n")
    assert path.read_text(encoding="utf-8") == "content\n"
    with pytest.raises(FileExistsError):
        atomic_create(path, "other\n")
