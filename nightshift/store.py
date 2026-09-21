"""SQLite state: tasks, runs and key/value state. One Store (connection) per thread."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

from nightshift.tasks import Task

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id    TEXT PRIMARY KEY,
    order_idx  INTEGER NOT NULL,
    plan       TEXT NOT NULL,
    title      TEXT NOT NULL,
    tier       TEXT NOT NULL,
    review     TEXT NOT NULL,
    paths      TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    no_tests   INTEGER NOT NULL,
    body       TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'ready',
    step       TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    last_run   TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id     TEXT PRIMARY KEY,
    task_id    TEXT NOT NULL REFERENCES tasks(task_id),
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    outcome    TEXT,
    detail     TEXT
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)

    # tasks -----------------------------------------------------------------

    def sync_tasks(self, plan: str, tasks: list[Task]) -> None:
        """Insert new tasks as ready; refresh definitions of known tasks without touching progress."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for idx, t in enumerate(tasks):
                self.conn.execute(
                    """
                    INSERT INTO tasks (task_id, order_idx, plan, title, tier, review, paths,
                                       depends_on, no_tests, body, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        order_idx = excluded.order_idx, plan = excluded.plan, title = excluded.title,
                        tier = excluded.tier, review = excluded.review, paths = excluded.paths,
                        depends_on = excluded.depends_on, no_tests = excluded.no_tests, body = excluded.body
                    """,
                    (t.id, idx, plan, t.title, t.tier, t.review, json.dumps(t.paths),
                     json.dumps(t.depends_on), int(t.no_tests), t.body, now()),
                )

    def list_tasks(self) -> list[dict]:
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY order_idx").fetchall()
        return [self._row(r) for r in rows]

    def get_task(self, task_id: str) -> dict | None:
        row = self.conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        return self._row(row) if row else None

    def next_eligible(self, tiers: set[str]) -> Task | None:
        merged = {r["task_id"] for r in self.conn.execute("SELECT task_id FROM tasks WHERE status = 'merged'")}
        for row in self.list_tasks():
            if row["status"] == "ready" and row["tier"] in tiers and set(row["depends_on"]) <= merged:
                return Task(
                    id=row["task_id"], title=row["title"], tier=row["tier"], review=row["review"],
                    paths=row["paths"], depends_on=row["depends_on"], no_tests=row["no_tests"],
                    body=row["body"],
                )
        return None

    def set_status(self, task_id: str, status: str, step: str | None = None) -> None:
        self.conn.execute(
            "UPDATE tasks SET status = ?, step = ?, updated_at = ? WHERE task_id = ?",
            (status, step, now(), task_id),
        )

    def set_step(self, task_id: str, step: str | None) -> None:
        self.conn.execute("UPDATE tasks SET step = ?, updated_at = ? WHERE task_id = ?", (step, now(), task_id))

    def increment_attempts(self, task_id: str) -> int:
        self.conn.execute("UPDATE tasks SET attempts = attempts + 1 WHERE task_id = ?", (task_id,))
        return self.get_task(task_id)["attempts"]

    def recover_interrupted(self) -> list[str]:
        """After a crash: in-progress tasks go back to ready and open runs are closed as interrupted."""
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            ids = [r["task_id"] for r in self.conn.execute("SELECT task_id FROM tasks WHERE status = 'in_progress'")]
            self.conn.execute("UPDATE tasks SET status = 'ready', step = NULL, updated_at = ? "
                              "WHERE status = 'in_progress'", (now(),))
            self.conn.execute("UPDATE runs SET ended_at = ?, outcome = 'interrupted', "
                              "detail = 'process stopped during the cycle' WHERE ended_at IS NULL", (now(),))
        return ids

    def requeue_would_merge(self) -> None:
        self.conn.execute("UPDATE tasks SET status = 'ready', attempts = 0, updated_at = ? "
                          "WHERE status = 'would_merge'", (now(),))

    def reset_attempts(self, task_id: str) -> None:
        self.conn.execute("UPDATE tasks SET attempts = 0 WHERE task_id = ?", (task_id,))

    @staticmethod
    def _row(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["paths"] = json.loads(d["paths"])
        d["depends_on"] = json.loads(d["depends_on"])
        d["no_tests"] = bool(d["no_tests"])
        return d

    # runs ------------------------------------------------------------------

    def start_run(self, task_id: str) -> str:
        run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO runs (run_id, task_id, started_at) VALUES (?, ?, ?)", (run_id, task_id, now())
            )
            self.conn.execute("UPDATE tasks SET last_run = ? WHERE task_id = ?", (run_id, task_id))
        return run_id

    def finish_run(self, run_id: str, outcome: str, detail: str) -> None:
        self.conn.execute(
            "UPDATE runs SET ended_at = ?, outcome = ?, detail = ? WHERE run_id = ?",
            (now(), outcome, detail, run_id),
        )

    def recent_runs(self, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # state -----------------------------------------------------------------

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str | None) -> None:
        self.conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def usage_limits(self) -> dict[str, dict[str, str]]:
        """Providers that are out of quota, from the `limit:<provider>:<field>` state keys. Cleared ones are gone."""
        rows = self.conn.execute("SELECT key, value FROM state WHERE key LIKE 'limit:%'").fetchall()
        limits: dict[str, dict[str, str]] = {}
        for row in rows:
            _, provider, field_name = row["key"].split(":", 2)
            if row["value"] is not None:
                limits.setdefault(provider, {})[field_name] = row["value"]
        return {p: fields for p, fields in limits.items() if "resume_after" in fields}
