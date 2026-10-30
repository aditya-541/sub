"""Small SQLite task journal for resumable local and validator workflows."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .protocol import BuildTask


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BuildQueueFullError(RuntimeError):
    pass


class TaskStore:
    def __init__(self, database: str | Path | None = None) -> None:
        self.database = Path(database or os.getenv("FACTORY_STATE_DB", "./data/factory.sqlite3"))

    def _connect(self) -> sqlite3.Connection:
        self.database.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=10)
        os.chmod(self.database, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS factory_tasks (
                task_id TEXT PRIMARY KEY,
                kind TEXT NOT NULL,
                prompt TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                outcome_json TEXT,
                error TEXT
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS factory_task_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_json TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE TABLE IF NOT EXISTS factory_build_jobs (
                task_id TEXT PRIMARY KEY,
                project_id TEXT NOT NULL,
                task_json TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS factory_build_jobs_state_created "
            "ON factory_build_jobs(state, created_at)"
        )
        return connection

    def enqueue_build(self, task: BuildTask, project_id: str) -> None:
        now = _now()
        with closing(self._connect()) as connection, connection:
            limit = int(os.getenv("FACTORY_BUILD_QUEUE_LIMIT", "16"))
            if limit < 1:
                raise ValueError("FACTORY_BUILD_QUEUE_LIMIT must be positive.")
            active = connection.execute(
                "SELECT COUNT(*) FROM factory_build_jobs WHERE state IN ('queued', 'running')"
            ).fetchone()[0]
            if active >= limit:
                raise BuildQueueFullError("The Factory build queue is full; retry later.")
            connection.execute(
                """INSERT INTO factory_tasks
                   (task_id, kind, prompt, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (task.task_id, task.kind.value, task.prompt, now, now),
            )
            connection.execute(
                """INSERT INTO factory_build_jobs
                   (task_id, project_id, task_json, state, created_at, updated_at)
                   VALUES (?, ?, ?, 'queued', ?, ?)""",
                (task.task_id, project_id, task.model_dump_json(), now, now),
            )
            connection.execute(
                """INSERT INTO factory_task_events
                   (task_id, created_at, event_type, event_json) VALUES (?, ?, ?, ?)""",
                (task.task_id, now, "queued", json.dumps({"project_id": project_id})),
            )

    def claim_build_job(self) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT task_id, project_id, task_json FROM factory_build_jobs
                   WHERE state = 'queued' ORDER BY created_at, task_id LIMIT 1"""
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                "UPDATE factory_build_jobs SET state='running', updated_at=? WHERE task_id=?",
                (_now(), row["task_id"]),
            )
            connection.commit()
        return {
            "task": BuildTask.model_validate_json(row["task_json"]),
            "project_id": row["project_id"],
        }

    def recover_running_build_jobs(self) -> int:
        """Requeue interrupted single-host jobs when the worker starts again."""
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT task_id FROM factory_build_jobs WHERE state='running'"
            ).fetchall()
            now = _now()
            connection.execute(
                """UPDATE factory_build_jobs SET state='queued', updated_at=?
                   WHERE state='running'""",
                (now,),
            )
            connection.execute(
                """UPDATE factory_tasks SET status='queued', updated_at=?, error=NULL
                   WHERE task_id IN (SELECT task_id FROM factory_build_jobs WHERE state='queued')
                     AND status='running'""",
                (now,),
            )
            connection.executemany(
                """INSERT INTO factory_task_events
                   (task_id, created_at, event_type, event_json) VALUES (?, ?, 'requeued', ?)""",
                [(row["task_id"], now, json.dumps({"reason": "worker-restart"})) for row in rows],
            )
        return len(rows)

    def release_build_job(self, task_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            task = connection.execute(
                "SELECT status FROM factory_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            if task is None:
                return
            state = "done" if task["status"] in {"completed", "partial", "failed"} else "queued"
            connection.execute(
                "UPDATE factory_build_jobs SET state=?, updated_at=? WHERE task_id=?",
                (state, _now(), task_id),
            )

    def save(
        self,
        task: BuildTask,
        *,
        status: str,
        outcome: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        if status not in {"queued", "running", "completed", "partial", "failed"}:
            raise ValueError("Unknown Factory task status.")
        now = _now()
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO factory_tasks
                       (task_id, kind, prompt, status, created_at, updated_at, outcome_json, error)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(task_id) DO UPDATE SET
                         status=excluded.status,
                         updated_at=excluded.updated_at,
                         outcome_json=excluded.outcome_json,
                         error=excluded.error""",
                (
                    task.task_id,
                    task.kind.value,
                    task.prompt,
                    status,
                    now,
                    now,
                    json.dumps(outcome, ensure_ascii=False) if outcome is not None else None,
                    error[:2000] if error else None,
                ),
            )

    def get(self, task_id: str) -> dict[str, Any] | None:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM factory_tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["outcome"] = (
            json.loads(result.pop("outcome_json")) if result["outcome_json"] else None
        )
        return result

    def record_event(self, task_id: str, event_type: str, data: dict[str, Any]) -> int:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                """INSERT INTO factory_task_events
                   (task_id, created_at, event_type, event_json) VALUES (?, ?, ?, ?)""",
                (task_id, _now(), event_type, json.dumps(data, ensure_ascii=False)),
            )
            return int(cursor.lastrowid)

    def events_after(self, task_id: str, event_id: int = 0) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT event_id, created_at, event_type, event_json
                   FROM factory_task_events WHERE task_id = ? AND event_id > ?
                   ORDER BY event_id""",
                (task_id, event_id),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "created_at": row["created_at"],
                "event_type": row["event_type"],
                "data": json.loads(row["event_json"]),
            }
            for row in rows
        ]
