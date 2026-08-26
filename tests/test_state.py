from datetime import UTC, datetime

from ztsync.models import TaskSnapshot
from ztsync.state import StateStore, fields_hash


def test_snapshot_round_trip(tmp_path) -> None:
    local = {"status": "open", "title": "Task", "due": None, "priority": None, "tags": []}
    remote = dict(local)
    snapshot = TaskSnapshot(
        task_id="task-1",
        project_id="project-1",
        path="daily.md",
        line_number=2,
        local_fields=local,
        remote_fields=remote,
        local_hash=fields_hash(local),
        remote_hash=fields_hash(remote),
        synced_at=datetime.now(UTC),
    )

    with StateStore(tmp_path / "state") as store:
        store.upsert_snapshot(snapshot)
        assert store.get_snapshot("task-1") == snapshot
        assert store.counts() == {"snapshots": 1, "unresolved_conflicts": 0}


def test_state_migration_is_idempotent(tmp_path) -> None:
    state_path = tmp_path / "state"
    with StateStore(state_path):
        pass
    with StateStore(state_path) as store:
        assert store.get_metadata("missing") is None


def test_pending_operation_round_trip(tmp_path) -> None:
    with StateStore(tmp_path / "state") as store:
        store.add_pending(
            fingerprint="fp-1",
            operation="create_remote",
            task_id="task-1",
            project_id="project-1",
            path="daily.md",
            line_number=1,
            payload={"title": "Task"},
        )
        assert store.get_pending("fp-1")["task_id"] == "task-1"
        store.resolve_pending("fp-1")
        assert store.get_pending("fp-1") is None
