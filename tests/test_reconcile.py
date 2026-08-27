from datetime import date, time

from ztsync.config import Settings
from ztsync.markdown import parse_text
from ztsync.models import TickTickTask
from ztsync.reconcile import Reconciler, create_payload
from ztsync.state import StateStore


class FakeClient:
    def __init__(self, tasks=None):
        self.tasks = list(tasks or [])
        self.created = []
        self.updated = []
        self.completed = []

    def list_project_tasks(self, project_id):
        return [task for task in self.tasks if task.project_id == project_id]

    def create_task(self, payload):
        task = TickTickTask(
            id=f"task-{len(self.created) + 1}",
            project_id=payload["projectId"],
            title=payload["title"],
            due_date=date.fromisoformat(payload["dueDate"][:10])
            if payload.get("dueDate")
            else None,
            priority=payload["priority"],
            tags=payload["tags"],
            raw=payload,
        )
        self.created.append(payload)
        self.tasks.append(task)
        return task

    def update_task(self, task_id, payload):
        current = next(task for task in self.tasks if task.id == task_id)
        updated = TickTickTask(
            id=current.id,
            project_id=current.project_id,
            title=payload["title"],
            status=current.status,
            due_date=date.fromisoformat(payload["dueDate"][:10])
            if payload.get("dueDate")
            else None,
            priority=payload["priority"],
            tags=payload["tags"],
            raw=payload,
        )
        self.tasks = [updated if task.id == task_id else task for task in self.tasks]
        self.updated.append(payload)
        return updated

    def complete_task(self, project_id, task_id):
        self.completed.append(task_id)

    def get_project_task(self, project_id, task_id):
        return next(task for task in self.tasks if task.id == task_id)


def settings_for(vault_path):
    return Settings(
        vault_path=vault_path,
        task_paths=["daily-notes", "inbox/ticktick.md"],
        state_dir=vault_path / ".sync-state",
    )


def test_create_payload_sends_tags_and_clock_time(tmp_path):
    task = parse_text(
        tmp_path / "daily.md",
        "- [ ] Send report tomorrow 18am #work #release\n",
        reference_date=date(2026, 8, 26),
    )[0]

    payload = create_payload(task, "project-1", "America/Sao_Paulo")

    assert payload["title"] == "Send report"
    assert payload["tags"] == ["work", "release"]
    assert payload["isAllDay"] is False
    assert payload["dueDate"] == "2026-08-27T21:00:00+0000"


def test_unmarked_markdown_task_is_created_once(tmp_path):
    daily = tmp_path / "daily-notes"
    daily.mkdir()
    note = daily / "2026-08-26.md"
    note.write_text("- [ ] New task due:2026-08-26 !high #work\n", encoding="utf-8")
    client = FakeClient()
    settings = settings_for(tmp_path)

    with StateStore(settings.state_dir) as store:
        first = Reconciler(settings, store, client, project_id="project-1").run()
        second = Reconciler(settings, store, client, project_id="project-1").run()

    assert [action.kind for action in first] == ["create_remote"]
    assert second == []
    assert len(client.created) == 1
    assert "zt:v1 task=task-1 project=project-1" in note.read_text(encoding="utf-8")


def test_empty_markdown_checkbox_is_ignored_without_blocking_other_tasks(tmp_path):
    daily = tmp_path / "daily-notes"
    daily.mkdir()
    note = daily / "2026-08-26.md"
    note.write_text("- [x]\n- [ ] Real task\n", encoding="utf-8")
    client = FakeClient()
    settings = settings_for(tmp_path)

    with StateStore(settings.state_dir) as store:
        actions = Reconciler(settings, store, client, project_id="project-1").run()

    assert [action.kind for action in actions] == ["create_remote"]
    assert actions[0].line_number == 2
    assert len(client.created) == 1
    lines = note.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "- [x]"
    assert "zt:v1 task=task-1 project=project-1" in lines[1]


def test_completed_markdown_task_is_completed_remotely(tmp_path):
    daily = tmp_path / "daily-notes"
    daily.mkdir()
    note = daily / "2026-08-26.md"
    note.write_text("- [x] Already done\n", encoding="utf-8")
    client = FakeClient()
    settings = settings_for(tmp_path)

    with StateStore(settings.state_dir) as store:
        Reconciler(settings, store, client, project_id="project-1").run()

    assert client.completed == ["task-1"]


def test_mapped_task_missing_from_project_list_is_fetched_directly(tmp_path):
    class HiddenTaskClient(FakeClient):
        def list_project_tasks(self, project_id):
            return []

    daily = tmp_path / "daily-notes"
    daily.mkdir()
    note = daily / "2026-08-26.md"
    note.write_text(
        "- [ ] Original due:2026-08-27T09:30 #work <!-- zt:v1 task=task-1 project=project-1 -->\n",
        encoding="utf-8",
    )
    remote = TickTickTask(
        id="task-1",
        project_id="project-1",
        title="Original",
        due_date=date(2026, 8, 27),
        due_time=time(9, 30),
        tags=["work"],
        raw={"id": "task-1", "projectId": "project-1", "title": "Original"},
    )
    client = HiddenTaskClient([remote])
    settings = settings_for(tmp_path)

    with StateStore(settings.state_dir) as store:
        Reconciler(settings, store, client, project_id="project-1").run()
        note.write_text(
            "- [ ] Changed due:2026-08-27T10:00 #work "
            "<!-- zt:v1 task=task-1 project=project-1 -->\n",
            encoding="utf-8",
        )
        actions = Reconciler(settings, store, client, project_id="project-1").run()

    assert [action.kind for action in actions] == ["update_remote"]
    assert actions[0].fields == ["due_time", "title"]
    assert client.updated[0]["isAllDay"] is False


def test_remote_task_is_imported_to_inbox(tmp_path):
    remote = TickTickTask(
        id="remote-1",
        project_id="project-1",
        title="Imported task",
        due_date=date(2026, 8, 26),
        priority=3,
        tags=["work"],
    )
    settings = settings_for(tmp_path)
    client = FakeClient([remote])

    with StateStore(settings.state_dir) as store:
        actions = Reconciler(settings, store, client, project_id="project-1").run()

    inbox = tmp_path / "inbox/ticktick.md"
    assert [action.kind for action in actions] == ["import_local"]
    assert "Imported task due:2026-08-26 !medium #work" in inbox.read_text(encoding="utf-8")


def test_timed_remote_task_is_imported_with_clock_time(tmp_path):
    remote = TickTickTask(
        id="remote-1",
        project_id="project-1",
        title="Timed imported task",
        due_date=date(2026, 8, 26),
        due_time=time(18, 30),
        tags=["work"],
    )
    settings = settings_for(tmp_path)
    client = FakeClient([remote])

    with StateStore(settings.state_dir) as store:
        actions = Reconciler(settings, store, client, project_id="project-1").run()

    inbox = tmp_path / "inbox/ticktick.md"
    assert [action.kind for action in actions] == ["import_local"]
    assert "Timed imported task due:2026-08-26T18:30 #work" in inbox.read_text(encoding="utf-8")


def test_same_field_changed_on_both_sides_creates_conflict(tmp_path):
    daily = tmp_path / "daily-notes"
    daily.mkdir()
    note = daily / "2026-08-26.md"
    note.write_text("- [ ] Original\n", encoding="utf-8")
    client = FakeClient()
    settings = settings_for(tmp_path)

    with StateStore(settings.state_dir) as store:
        Reconciler(settings, store, client, project_id="project-1").run()
        note.write_text(
            "- [ ] Local edit <!-- zt:v1 task=task-1 project=project-1 -->\n",
            encoding="utf-8",
        )
        client.tasks[0] = client.tasks[0].model_copy(update={"title": "Remote edit"})
        actions = Reconciler(settings, store, client, project_id="project-1").run()
        conflicts = store.list_conflicts()

    assert [action.kind for action in actions] == ["conflict"]
    assert conflicts[0]["task_id"] == "task-1"
    assert "Local edit" in note.read_text(encoding="utf-8")
    assert client.updated == []
