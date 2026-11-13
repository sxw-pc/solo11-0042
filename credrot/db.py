"""SQLite schema, connections and explicit transaction boundaries."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS credentials (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    role TEXT NOT NULL,
    version INTEGER NOT NULL,
    synthetic_ref TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    priority INTEGER NOT NULL,
    required_roles TEXT NOT NULL,
    min_active INTEGER NOT NULL,
    backup_margin INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_bindings (
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL REFERENCES credentials(id),
    rank INTEGER NOT NULL,
    PRIMARY KEY (task_id, credential_id)
);
CREATE TABLE IF NOT EXISTS rotation_batches (
    batch_id TEXT PRIMARY KEY,
    generated_at TEXT NOT NULL,
    executed_at TEXT,
    status TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    summary_json TEXT NOT NULL,
    failure_reason TEXT
);
CREATE TABLE IF NOT EXISTS rotation_items (
    batch_id TEXT NOT NULL REFERENCES rotation_batches(batch_id) ON DELETE CASCADE,
    credential_id TEXT NOT NULL,
    action TEXT NOT NULL,
    from_state TEXT NOT NULL,
    to_state TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    result TEXT NOT NULL,
    ok INTEGER NOT NULL,
    failure_reason TEXT,
    at TEXT NOT NULL,
    PRIMARY KEY (batch_id, credential_id)
);
CREATE TABLE IF NOT EXISTS verifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT,
    credential_id TEXT NOT NULL,
    outcome TEXT NOT NULL,
    code TEXT NOT NULL,
    latency_ms INTEGER,
    checked_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    subject TEXT NOT NULL,
    before_json TEXT NOT NULL,
    after_json TEXT NOT NULL,
    batch_id TEXT,
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_credential
    ON verifications(credential_id, id);
CREATE INDEX IF NOT EXISTS idx_audit_batch ON audit_events(batch_id);
"""


class Database:
    """A small path wrapper; every unit of work gets its own connection."""

    def __init__(self, path: str | Path):
        self.path = str(path)

    def connect(self) -> sqlite3.Connection:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            self.path, timeout=10, isolation_level=None, check_same_thread=False
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def initialize(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(SCHEMA)
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(rotation_items)")
            }
            if "ok" not in columns:
                conn.execute(
                    "ALTER TABLE rotation_items ADD COLUMN ok INTEGER NOT NULL DEFAULT 1"
                )
            conn.execute("PRAGMA user_version = 1")
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()
