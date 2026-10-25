"""SQLite task archive: what was asked, what was normalised, what happened.

Every stored string is redacted first; the raw panel reads these rows, so a key
that ever reached the harness output must never reach the database.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .redact import clip, redact, redact_obj

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    source TEXT NOT NULL,
    raw_input TEXT NOT NULL,
    normalized TEXT NOT NULL DEFAULT '',
    constraints TEXT NOT NULL DEFAULT '[]',
    requires_confirmation INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    stop_reason TEXT NOT NULL DEFAULT '',
    summary_ja TEXT NOT NULL DEFAULT '',
    summary_zh TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    harness_session TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    permission_denials INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, seq);
CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC);
"""


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            # a hard crash leaves rows in a non-terminal state; they are not running now
            self._conn.execute(
                "UPDATE tasks SET status = 'interrupted', stop_reason = 'interrupted',"
                " updated_at = ? WHERE status IN ('running', 'pending')",
                (time.time(),),
            )
            self._conn.commit()
        self._seq: dict[str, int] = {}

    # ------------------------------------------------------------------ tasks
    def create_task(self, *, source: str, raw_input: str, normalized: str = "",
                    constraints: list[str] | None = None, requires_confirmation: bool = False,
                    harness_session: str = "") -> str:
        task_id = uuid.uuid4().hex
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks (id, created_at, updated_at, source, raw_input, normalized,"
                " constraints, requires_confirmation, status, harness_session)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)",
                (task_id, now, now, source, redact(raw_input), redact(normalized),
                 json.dumps([redact(c) for c in (constraints or [])], ensure_ascii=False),
                 1 if requires_confirmation else 0, "pending", harness_session),
            )
            self._conn.commit()
        self._seq[task_id] = 0
        return task_id

    def update_task(self, task_id: str, **fields: Any) -> None:
        allowed = {
            "normalized", "status", "stop_reason", "summary_ja", "summary_zh", "error",
            "duration_ms", "permission_denials", "harness_session", "requires_confirmation",
        }
        assignments = []
        values: list[Any] = []
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key in ("summary_ja", "summary_zh", "error", "normalized", "stop_reason", "status"):
                value = redact(value)
            assignments.append(f"{key} = ?")
            values.append(value)
        if not assignments:
            return
        assignments.append("updated_at = ?")
        values.append(time.time())
        values.append(task_id)
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?", values)
            self._conn.commit()

    def add_event(self, task_id: str, kind: str, payload: Any) -> None:
        with self._lock:
            seq = self._seq.get(task_id, 0)
            self._seq[task_id] = seq + 1
            text = json.dumps(redact_obj(payload), ensure_ascii=False)[:20000]
            self._conn.execute(
                "INSERT INTO task_events (task_id, seq, ts, kind, payload) VALUES (?,?,?,?,?)",
                (task_id, seq, time.time(), kind, text),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ reads
    def list_tasks(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._task_row(row) for row in rows]

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task_row(row) if row else None

    def get_events(self, task_id: str, limit: int = 1000) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq, ts, kind, payload FROM task_events WHERE task_id = ?"
                " ORDER BY seq ASC LIMIT ?", (task_id, limit),
            ).fetchall()
        events = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
            except json.JSONDecodeError:
                payload = {"raw": row["payload"]}
            events.append({"seq": row["seq"], "ts": row["ts"], "kind": row["kind"],
                           "payload": payload})
        return events

    @staticmethod
    def _task_row(row: sqlite3.Row) -> dict[str, Any]:
        data = dict(row)
        try:
            data["constraints"] = json.loads(data.get("constraints") or "[]")
        except json.JSONDecodeError:
            data["constraints"] = []
        data["raw_input"] = clip(redact(data.get("raw_input", "")), 4000)
        data["normalized"] = clip(redact(data.get("normalized", "")), 4000)
        data["summary_ja"] = redact(data.get("summary_ja", ""))
        data["summary_zh"] = redact(data.get("summary_zh", ""))
        data["error"] = clip(redact(data.get("error", "")), 4000)
        return data

    def close(self) -> None:
        with self._lock:
            self._conn.close()
