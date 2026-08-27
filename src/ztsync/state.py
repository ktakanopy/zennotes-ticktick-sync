from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import Conflict, TaskSnapshot


def fields_hash(fields: dict[str, Any]) -> str:
    encoded = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class StateStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        self.database_path = directory / "state.db"
        self.connection = sqlite3.connect(self.database_path)
        os.chmod(self.database_path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._migrate()

    def _migrate(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_version (
                version INTEGER NOT NULL
            );
            INSERT INTO schema_version(version)
                SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

            CREATE TABLE IF NOT EXISTS snapshots (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                local_json TEXT NOT NULL,
                remote_json TEXT NOT NULL,
                local_hash TEXT NOT NULL,
                remote_hash TEXT NOT NULL,
                synced_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conflicts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                path TEXT,
                line_number INTEGER,
                fields_json TEXT NOT NULL,
                local_json TEXT NOT NULL,
                remote_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS unresolved_conflict_fingerprint
                ON conflicts(fingerprint) WHERE resolved_at IS NULL;

            CREATE TABLE IF NOT EXISTS sync_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                actions_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pending_operations (
                fingerprint TEXT PRIMARY KEY,
                operation TEXT NOT NULL,
                task_id TEXT NOT NULL,
                project_id TEXT NOT NULL,
                path TEXT NOT NULL,
                line_number INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                resolved_at TEXT
            );
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> StateStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def get_snapshot(self, task_id: str) -> TaskSnapshot | None:
        row = self.connection.execute(
            "SELECT * FROM snapshots WHERE task_id = ?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return TaskSnapshot(
            task_id=row["task_id"],
            project_id=row["project_id"],
            path=row["path"],
            line_number=row["line_number"],
            local_fields=json.loads(row["local_json"]),
            remote_fields=json.loads(row["remote_json"]),
            local_hash=row["local_hash"],
            remote_hash=row["remote_hash"],
            synced_at=datetime.fromisoformat(row["synced_at"]),
        )

    def upsert_snapshot(self, snapshot: TaskSnapshot) -> None:
        self.connection.execute(
            """
            INSERT INTO snapshots(
                task_id, project_id, path, line_number, local_json, remote_json,
                local_hash, remote_hash, synced_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                project_id=excluded.project_id,
                path=excluded.path,
                line_number=excluded.line_number,
                local_json=excluded.local_json,
                remote_json=excluded.remote_json,
                local_hash=excluded.local_hash,
                remote_hash=excluded.remote_hash,
                synced_at=excluded.synced_at
            """,
            (
                snapshot.task_id,
                snapshot.project_id,
                snapshot.path,
                snapshot.line_number,
                json.dumps(snapshot.local_fields, ensure_ascii=False, sort_keys=True),
                json.dumps(snapshot.remote_fields, ensure_ascii=False, sort_keys=True),
                snapshot.local_hash,
                snapshot.remote_hash,
                snapshot.synced_at.isoformat(),
            ),
        )
        self.connection.commit()

    def record_conflict(self, conflict: Conflict) -> bool:
        cursor = self.connection.execute(
            """
            INSERT OR IGNORE INTO conflicts(
                task_id, project_id, path, line_number, fields_json,
                local_json, remote_json, fingerprint, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict.task_id,
                conflict.project_id,
                conflict.path,
                conflict.line_number,
                json.dumps(conflict.fields, ensure_ascii=False, sort_keys=True),
                json.dumps(conflict.local_values, ensure_ascii=False, sort_keys=True),
                json.dumps(conflict.remote_values, ensure_ascii=False, sort_keys=True),
                conflict.fingerprint,
                conflict.created_at.isoformat(),
            ),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def list_conflicts(self, unresolved_only: bool = True) -> list[dict[str, Any]]:
        where = "WHERE resolved_at IS NULL" if unresolved_only else ""
        rows = self.connection.execute(
            f"SELECT * FROM conflicts {where} ORDER BY created_at DESC"
        ).fetchall()
        return [dict(row) for row in rows]

    def counts(self) -> dict[str, int]:
        snapshots = self.connection.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
        conflicts = self.connection.execute(
            "SELECT COUNT(*) FROM conflicts WHERE resolved_at IS NULL"
        ).fetchone()[0]
        return {"snapshots": snapshots, "unresolved_conflicts": conflicts}

    def set_metadata(self, key: str, value: str) -> None:
        self.connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (key, value),
        )
        self.connection.commit()

    def get_metadata(self, key: str) -> str | None:
        row = self.connection.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def add_pending(
        self,
        *,
        fingerprint: str,
        operation: str,
        task_id: str,
        project_id: str,
        path: str,
        line_number: int,
        payload: dict[str, Any],
    ) -> None:
        self.connection.execute(
            """
            INSERT INTO pending_operations(
                fingerprint, operation, task_id, project_id, path, line_number,
                payload_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(fingerprint) DO UPDATE SET
                task_id=excluded.task_id,
                payload_json=excluded.payload_json,
                resolved_at=NULL
            """,
            (
                fingerprint,
                operation,
                task_id,
                project_id,
                path,
                line_number,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                datetime.now(UTC).isoformat(),
            ),
        )
        self.connection.commit()

    def get_pending(self, fingerprint: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """
            SELECT * FROM pending_operations
            WHERE fingerprint = ? AND resolved_at IS NULL
            """,
            (fingerprint,),
        ).fetchone()
        return dict(row) if row else None

    def resolve_pending(self, fingerprint: str) -> None:
        self.connection.execute(
            """
            UPDATE pending_operations
            SET resolved_at = ?
            WHERE fingerprint = ? AND resolved_at IS NULL
            """,
            (datetime.now(UTC).isoformat(), fingerprint),
        )
        self.connection.commit()

    def record_run(
        self,
        run_id: str,
        started_at: datetime,
        status: str,
        actions: list[dict[str, Any]],
    ) -> None:
        finished_at = datetime.now(UTC).isoformat()
        self.connection.execute(
            """
            INSERT INTO sync_runs(run_id, started_at, finished_at, status, actions_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                started_at.astimezone(UTC).isoformat(),
                finished_at,
                status,
                json.dumps(actions, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.connection.commit()
