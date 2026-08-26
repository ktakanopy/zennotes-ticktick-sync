from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx
from pydantic import BaseModel, ConfigDict

from .models import TickTickTask

API_BASE_URL = "https://api.ticktick.com/open/v1"
AUTHORIZE_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"


class TickTickError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class OAuthToken(BaseModel):
    model_config = ConfigDict(extra="allow")

    access_token: str
    token_type: str = "Bearer"
    expires_at: datetime | None = None
    refresh_token: str | None = None

    @classmethod
    def from_response(cls, payload: dict[str, Any]) -> OAuthToken:
        expires_in = payload.get("expires_in")
        expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
        return cls(
            access_token=str(payload["access_token"]),
            token_type=str(payload.get("token_type") or "Bearer"),
            expires_at=expires_at,
            refresh_token=payload.get("refresh_token"),
            **{
                key: value
                for key, value in payload.items()
                if key not in {"access_token", "token_type", "expires_in", "refresh_token"}
            },
        )

    @property
    def expired(self) -> bool:
        return bool(self.expires_at and datetime.now(UTC) >= self.expires_at)


class TokenStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> OAuthToken | None:
        if not self.path.is_file():
            return None
        return OAuthToken.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, token: OAuthToken) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
        temporary.write_text(token.model_dump_json(), encoding="utf-8")
        os.chmod(temporary, 0o600)
        os.replace(temporary, self.path)
        os.chmod(self.path, 0o600)


class TickTickClient:
    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        token_store: TokenStore,
        http_client: httpx.Client | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_store = token_store
        self.http = http_client or httpx.Client(base_url=API_BASE_URL, timeout=15.0)
        self._owns_http = http_client is None
        self.sleeper = sleeper

    def close(self) -> None:
        if self._owns_http:
            self.http.close()

    def __enter__(self) -> TickTickClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
        query = urlencode(
            {
                "scope": "tasks:read tasks:write",
                "client_id": client_id,
                "state": state,
                "redirect_uri": redirect_uri,
                "response_type": "code",
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    def exchange_code(self, code: str, redirect_uri: str) -> OAuthToken:
        response = httpx.post(
            TOKEN_URL,
            data={
                "code": code,
                "grant_type": "authorization_code",
                "scope": "tasks:read tasks:write",
                "redirect_uri": redirect_uri,
            },
            auth=(self.client_id, self.client_secret),
            timeout=15.0,
        )
        if response.is_error:
            raise self._error_from_response(response, "OAuth token exchange failed")
        token = OAuthToken.from_response(response.json())
        self.token_store.save(token)
        return token

    def refresh_token(self) -> OAuthToken:
        current = self.token_store.load()
        if not current or not current.refresh_token:
            raise TickTickError("no refresh token available")
        response = httpx.post(
            TOKEN_URL,
            data={
                "refresh_token": current.refresh_token,
                "grant_type": "refresh_token",
                "scope": "tasks:read tasks:write",
            },
            auth=(self.client_id, self.client_secret),
            timeout=15.0,
        )
        if response.is_error:
            raise self._error_from_response(response, "OAuth token refresh failed")
        payload = response.json()
        token = OAuthToken.from_response(
            {**payload, "refresh_token": payload.get("refresh_token") or current.refresh_token}
        )
        self.token_store.save(token)
        return token

    def _token(self) -> OAuthToken:
        token = self.token_store.load()
        if not token:
            raise TickTickError("TickTick is not authenticated; run ztsync auth login")
        if token.expired:
            token = self.refresh_token()
        return token

    @staticmethod
    def _error_from_response(response: httpx.Response, prefix: str) -> TickTickError:
        detail = ""
        try:
            payload = response.json()
            if isinstance(payload, dict):
                detail = str(payload.get("error") or payload.get("message") or "")
        except (ValueError, json.JSONDecodeError):
            pass
        suffix = f": {detail}" if detail else ""
        return TickTickError(f"{prefix} ({response.status_code}){suffix}", response.status_code)

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        for attempt in range(3):
            token = self._token()
            headers = dict(kwargs.pop("headers", {}))
            headers["Authorization"] = f"{token.token_type} {token.access_token}"
            try:
                response = self.http.request(method, path, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    raise TickTickError(f"TickTick request failed: {type(exc).__name__}") from exc
                self.sleeper(2**attempt)
                continue
            if response.status_code == 401 and token.refresh_token and attempt == 0:
                self.refresh_token()
                continue
            if response.status_code == 429 or response.status_code >= 500:
                if attempt == 2:
                    raise self._error_from_response(response, "TickTick request failed")
                retry_after = response.headers.get("Retry-After", "1")
                try:
                    delay = min(float(retry_after), 5.0)
                except ValueError:
                    delay = 1.0
                self.sleeper(delay)
                continue
            if response.is_error:
                raise self._error_from_response(response, "TickTick request failed")
            if response.status_code == 204 or not response.content:
                return None
            return response.json()
        raise AssertionError("unreachable")

    def get_user(self) -> dict[str, Any]:
        return self._request("GET", "/user")

    def list_projects(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/project")
        return list(payload or [])

    def get_project_data(self, project_id: str) -> dict[str, Any]:
        return dict(self._request("GET", f"/project/{project_id}/data"))

    def list_project_tasks(self, project_id: str) -> list[TickTickTask]:
        payload = self.get_project_data(project_id)
        tasks = []
        for item in payload.get("tasks", []):
            normalized = dict(item)
            normalized.setdefault("projectId", project_id)
            tasks.append(TickTickTask.from_api(normalized))
        return tasks

    def create_project(self, name: str) -> dict[str, Any]:
        return dict(self._request("POST", "/project", json={"name": name}))

    def create_task(self, payload: dict[str, Any]) -> TickTickTask:
        response = self._request("POST", "/task", json=payload)
        return TickTickTask.from_api(response)

    def update_task(self, task_id: str, payload: dict[str, Any]) -> TickTickTask:
        response = self._request("POST", f"/task/{task_id}", json=payload)
        return TickTickTask.from_api(response)

    def complete_task(self, project_id: str, task_id: str) -> None:
        self._request("POST", f"/project/{project_id}/task/{task_id}/complete")
