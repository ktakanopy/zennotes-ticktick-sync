from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _read_dotenv(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key.strip()] = value
    return values


def _env(dotenv: dict[str, str], name: str, default: str = "") -> str:
    return os.environ.get(name, dotenv.get(name, default)).strip()


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value:
        raise ValueError(f"task path must be a safe relative path: {value!r}")
    return value


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True)

    vault_path: Path
    task_paths: list[str] = Field(min_length=1)
    ticktick_project_name: str = "ZenNotes"
    ticktick_time_zone: str = "America/Sao_Paulo"
    ticktick_client_id: str | None = None
    ticktick_client_secret: str | None = None
    ticktick_redirect_uri: str = "http://192.168.15.14:8765/oauth/callback"
    ticktick_oauth_bind_host: str = Field(default="0.0.0.0", min_length=1)
    ticktick_oauth_port: int = Field(default=8765, ge=1, le=65535)
    state_dir: Path
    vault_poll_seconds: int = Field(default=30, ge=5)
    ticktick_poll_seconds: int = Field(default=60, ge=10)

    @field_validator("vault_path", "state_dir")
    @classmethod
    def require_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("path must be absolute")
        return value

    @field_validator("task_paths")
    @classmethod
    def validate_task_paths(cls, value: list[str]) -> list[str]:
        return [_safe_relative_path(item.strip()) for item in value if item.strip()]

    @field_validator("ticktick_time_zone")
    @classmethod
    def validate_time_zone(cls, value: str) -> str:
        ZoneInfo(value)
        return value

    @classmethod
    def from_env(cls, directory: Path | None = None) -> Settings:
        base = directory or Path.cwd()
        dotenv = _read_dotenv(base / ".env")
        vault_path = Path(_env(dotenv, "ZENNOTES_VAULT_PATH", str(base / "vault")))
        task_paths = _env(
            dotenv,
            "ZENNOTES_TASK_PATHS",
            "projects/luma-health/daily-notes,inbox/ticktick.md",
        ).split(",")
        client_id = _env(dotenv, "TICKTICK_CLIENT_ID") or None
        client_secret = _env(dotenv, "TICKTICK_CLIENT_SECRET") or None
        return cls(
            vault_path=vault_path,
            task_paths=task_paths,
            ticktick_project_name=_env(dotenv, "TICKTICK_PROJECT_NAME", "ZenNotes"),
            ticktick_time_zone=_env(dotenv, "TICKTICK_TIME_ZONE", "America/Sao_Paulo"),
            ticktick_client_id=client_id,
            ticktick_client_secret=client_secret,
            ticktick_redirect_uri=_env(
                dotenv,
                "TICKTICK_REDIRECT_URI",
                "http://192.168.15.14:8765/oauth/callback",
            ),
            ticktick_oauth_bind_host=_env(
                dotenv,
                "TICKTICK_OAUTH_BIND_HOST",
                "0.0.0.0",
            ),
            ticktick_oauth_port=int(_env(dotenv, "TICKTICK_OAUTH_PORT", "8765")),
            state_dir=Path(
                _env(
                    dotenv,
                    "SYNC_STATE_DIR",
                    str(Path.home() / ".local/state/zennotes-ticktick-sync"),
                )
            ),
            vault_poll_seconds=int(_env(dotenv, "VAULT_POLL_SECONDS", "30")),
            ticktick_poll_seconds=int(_env(dotenv, "TICKTICK_POLL_SECONDS", "60")),
        )

    @property
    def ticktick_credentials_configured(self) -> bool:
        return bool(self.ticktick_client_id and self.ticktick_client_secret)

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.ticktick_time_zone)
