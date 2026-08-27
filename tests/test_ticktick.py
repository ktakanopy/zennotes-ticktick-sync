from datetime import UTC, datetime, time, timedelta

import httpx

from ztsync.models import TickTickTask
from ztsync.ticktick import API_BASE_URL, OAuthToken, TickTickClient, TokenStore


def test_client_reads_project_and_creates_task(tmp_path) -> None:
    token_store = TokenStore(tmp_path / "oauth.json")
    token_store.save(OAuthToken(access_token="secret"))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/open/v1/project":
            return httpx.Response(200, json=[{"id": "project-1", "name": "ZenNotes"}])
        if request.url.path == "/open/v1/task":
            return httpx.Response(
                200,
                json={"id": "task-1", "projectId": "project-1", "title": "New task", "status": 0},
            )
        raise AssertionError(request.url)

    http_client = httpx.Client(
        base_url=API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with TickTickClient(
        client_id="client",
        client_secret="secret",
        token_store=token_store,
        http_client=http_client,
    ) as client:
        assert client.list_projects()[0]["id"] == "project-1"
        task = client.create_task({"projectId": "project-1", "title": "New task"})
    assert task.id == "task-1"
    assert requests[1].headers["Authorization"] == "Bearer secret"


def test_client_retries_rate_limit(tmp_path) -> None:
    token_store = TokenStore(tmp_path / "oauth.json")
    token_store.save(OAuthToken(access_token="secret"))
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"id": "user-1"})

    http_client = httpx.Client(base_url=API_BASE_URL, transport=httpx.MockTransport(handler))
    with TickTickClient(
        client_id="client",
        client_secret="secret",
        token_store=token_store,
        http_client=http_client,
        sleeper=lambda _: None,
    ) as client:
        assert client.get_user()["id"] == "user-1"
    assert attempts == 2


def test_client_reads_mapped_task_directly(tmp_path) -> None:
    token_store = TokenStore(tmp_path / "oauth.json")
    token_store.save(OAuthToken(access_token="secret"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/open/v1/project/project-1/task/task-1"
        return httpx.Response(
            200,
            json={
                "id": "task-1",
                "projectId": "project-1",
                "title": "Timed task",
                "status": 2,
                "isAllDay": False,
                "timeZone": "America/Sao_Paulo",
                "dueDate": "2026-08-27T21:30:00+00:00",
                "tags": ["work"],
            },
        )

    http_client = httpx.Client(
        base_url=API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with TickTickClient(
        client_id="client",
        client_secret="secret",
        token_store=token_store,
        http_client=http_client,
    ) as client:
        task = client.get_project_task("project-1", "task-1")

    assert isinstance(task, TickTickTask)
    assert task.completed
    assert task.due_time == time(18, 30)
    assert task.tags == ["work"]


def test_client_deletes_task(tmp_path) -> None:
    token_store = TokenStore(tmp_path / "oauth.json")
    token_store.save(OAuthToken(access_token="secret"))

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/open/v1/project/project-1/task/task-1"
        return httpx.Response(204)

    http_client = httpx.Client(
        base_url=API_BASE_URL,
        transport=httpx.MockTransport(handler),
    )
    with TickTickClient(
        client_id="client",
        client_secret="secret",
        token_store=token_store,
        http_client=http_client,
    ) as client:
        client.delete_task("project-1", "task-1")


def test_expired_token_requires_refresh_token(tmp_path) -> None:
    store = TokenStore(tmp_path / "oauth.json")
    store.save(
        OAuthToken(
            access_token="secret",
            expires_at=datetime.now(UTC) - timedelta(seconds=1),
        )
    )
    client = TickTickClient(
        client_id="client",
        client_secret="secret",
        token_store=store,
        http_client=httpx.Client(
            base_url=API_BASE_URL,
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={})),
        ),
    )
    try:
        client.get_user()
    except Exception as exc:
        assert "refresh token" in str(exc)
    else:
        raise AssertionError("expired token unexpectedly accepted")
