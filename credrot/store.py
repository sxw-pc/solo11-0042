"""Repositories for credentials, tasks, batches and rotation items."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

from .clock import format_time, parse_time
from .db import Database
from .model import Credential, RotationPlan, Task


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def seed_registry(
    db: Database,
    credentials: dict[str, Credential],
    tasks: dict[str, Task],
    at: datetime,
) -> None:
    stamp = format_time(at)
    with db.transaction() as conn:
        for table in (
            "audit_events", "verifications", "rotation_items", "rotation_batches",
            "task_bindings", "tasks", "credentials",
        ):
            conn.execute(f"DELETE FROM {table}")
        for cred in sorted(credentials.values(), key=lambda c: c.id):
            conn.execute(
                """INSERT INTO credentials
                (id, source, role, version, synthetic_ref, fingerprint,
                 issued_at, expires_at, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cred.id, cred.source, cred.role, cred.version,
                    cred.synthetic_ref, cred.fingerprint, format_time(cred.issued_at),
                    format_time(cred.expires_at), cred.status, stamp, stamp,
                ),
            )
        for task in sorted(tasks.values(), key=lambda t: t.id):
            conn.execute(
                """INSERT INTO tasks
                (id, source, priority, required_roles, min_active, backup_margin)
                VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    task.id, task.source, task.priority,
                    canonical_json(list(task.required_roles)), task.min_active,
                    task.backup_margin,
                ),
            )
            for rank, credential_id in enumerate(task.bindings, start=1):
                conn.execute(
                    """INSERT INTO task_bindings
                    (task_id, credential_id, rank) VALUES (?, ?, ?)""",
                    (task.id, credential_id, rank),
                )


def _credential(row: sqlite3.Row) -> Credential:
    return Credential(
        id=row["id"], source=row["source"], role=row["role"],
        version=row["version"], synthetic_ref=row["synthetic_ref"],
        fingerprint=row["fingerprint"], issued_at=parse_time(row["issued_at"]),
        expires_at=parse_time(row["expires_at"]), status=row["status"],
    )


def load_credentials(db: Database) -> dict[str, Credential]:
    conn = db.connect()
    try:
        return {credential.id: credential for credential in list_credentials(conn)}
    finally:
        conn.close()


def list_credentials(conn: sqlite3.Connection) -> list[Credential]:
    """All credentials, read through an existing (possibly transactional) conn."""
    rows = conn.execute("SELECT * FROM credentials ORDER BY id").fetchall()
    return [_credential(row) for row in rows]


def load_tasks(db: Database) -> dict[str, Task]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM tasks ORDER BY priority, id").fetchall()
        bindings = conn.execute(
            "SELECT task_id, credential_id FROM task_bindings ORDER BY task_id, rank"
        ).fetchall()
    finally:
        conn.close()
    by_task: dict[str, list[str]] = {}
    for binding in bindings:
        by_task.setdefault(binding["task_id"], []).append(binding["credential_id"])
    return {
        row["id"]: Task(
            id=row["id"], source=row["source"], priority=row["priority"],
            required_roles=tuple(json.loads(row["required_roles"])),
            min_active=row["min_active"], backup_margin=row["backup_margin"],
            bindings=tuple(by_task.get(row["id"], [])),
        )
        for row in rows
    }


def get_credential(conn: sqlite3.Connection, credential_id: str) -> Credential | None:
    row = conn.execute(
        "SELECT * FROM credentials WHERE id = ?", (credential_id,)
    ).fetchone()
    return _credential(row) if row else None


def update_credential(
    conn: sqlite3.Connection, credential: Credential, at: datetime
) -> None:
    cursor = conn.execute(
        """UPDATE credentials SET version = ?, synthetic_ref = ?, fingerprint = ?,
           issued_at = ?, expires_at = ?, status = ?, updated_at = ? WHERE id = ?""",
        (
            credential.version, credential.synthetic_ref, credential.fingerprint,
            format_time(credential.issued_at), format_time(credential.expires_at),
            credential.status, format_time(at), credential.id,
        ),
    )
    if cursor.rowcount != 1:
        raise KeyError(f"credential disappeared: {credential.id}")


def save_plan(db: Database, plan: RotationPlan, status: str = "planned") -> bool:
    conn = db.connect()
    try:
        cursor = conn.execute(
            """INSERT OR IGNORE INTO rotation_batches
            (batch_id, generated_at, status, plan_json, summary_json)
            VALUES (?, ?, ?, ?, ?)""",
            (
                plan.batch_id, format_time(plan.generated_at), status,
                canonical_json(plan.to_dict()), canonical_json(dict(plan.summary)),
            ),
        )
        conn.commit()
        return cursor.rowcount == 1
    finally:
        conn.close()


def get_batch(db: Database, batch_id: str) -> dict[str, Any] | None:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM rotation_batches WHERE batch_id = ?", (batch_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    return {
        "batch_id": row["batch_id"], "generated_at": row["generated_at"],
        "executed_at": row["executed_at"], "status": row["status"],
        "plan": json.loads(row["plan_json"]),
        "summary": json.loads(row["summary_json"]),
        "failure_reason": row["failure_reason"],
    }


def list_batches(db: Database, limit: int = 20) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT batch_id, generated_at, executed_at, status, summary_json,
                      failure_reason
               FROM rotation_batches
               ORDER BY generated_at DESC, batch_id DESC LIMIT ?""",
            (max(1, int(limit)),),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "batch_id": row["batch_id"], "generated_at": row["generated_at"],
            "executed_at": row["executed_at"], "status": row["status"],
            "summary": json.loads(row["summary_json"]),
            "failure_reason": row["failure_reason"],
        }
        for row in rows
    ]


def insert_rotation_item(
    conn: sqlite3.Connection, batch_id: str, item: dict[str, Any],
    result: str, failure_reason: str | None, at: datetime,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO rotation_items
        (batch_id, credential_id, action, from_state, to_state, reason_code,
         result, ok, failure_reason, at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            batch_id, item["credential_id"], item.get("action", "rotate"),
            item.get("from_state", "unknown"), item.get("target_state", "unknown"),
            item.get("reason_code", "UNKNOWN"), result,
            1 if result in {"executed", "skipped"} else 0,
            failure_reason, format_time(at),
        ),
    )


def list_rotation_items(db: Database, batch_id: str) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM rotation_items WHERE batch_id = ? ORDER BY credential_id",
            (batch_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def set_batch_result(
    db: Database, batch_id: str, status: str, at: datetime,
    summary: dict[str, Any] | None = None, failure_reason: str | None = None,
) -> None:
    fields = ["status = ?", "executed_at = ?", "failure_reason = ?"]
    values: list[Any] = [status, format_time(at), failure_reason]
    if summary is not None:
        fields.append("summary_json = ?")
        values.append(canonical_json(summary))
    values.append(batch_id)
    conn = db.connect()
    try:
        conn.execute(
            f"UPDATE rotation_batches SET {', '.join(fields)} WHERE batch_id = ?",
            values,
        )
        conn.commit()
    finally:
        conn.close()


def table_counts(db: Database) -> dict[str, int]:
    conn = db.connect()
    try:
        return {
            table: conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
            for table in (
                "credentials", "tasks", "rotation_batches", "rotation_items",
                "verifications", "audit_events",
            )
        }
    finally:
        conn.close()
