"""Persistence helpers for verification observations."""

from __future__ import annotations

import sqlite3
from typing import Any

from .db import Database
from .model import Verification


def insert_verification(conn: sqlite3.Connection, verification: Verification) -> None:
    conn.execute(
        """INSERT INTO verifications
        (batch_id, credential_id, outcome, code, latency_ms, checked_at)
        VALUES (?, ?, ?, ?, ?, ?)""",
        (
            verification.batch_id, verification.credential_id,
            verification.outcome, verification.code, verification.latency_ms,
            verification.checked_at.isoformat().replace("+00:00", "Z"),
        ),
    )


def _verification_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "credential_id": row["credential_id"], "outcome": row["outcome"],
        "code": row["code"], "checked_at": row["checked_at"],
        "batch_id": row["batch_id"], "latency_ms": row["latency_ms"],
    }


def latest_verifications(db: Database) -> dict[str, dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT v.* FROM verifications v
               JOIN (SELECT credential_id, MAX(id) AS max_id FROM verifications
                     GROUP BY credential_id) latest
               ON v.id = latest.max_id
               ORDER BY v.credential_id"""
        ).fetchall()
    finally:
        conn.close()
    return {row["credential_id"]: _verification_dict(row) for row in rows}


def list_verifications(
    db: Database, batch_id: str | None = None
) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        if batch_id is None:
            rows = conn.execute(
                "SELECT * FROM verifications ORDER BY id ASC"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM verifications WHERE batch_id = ? ORDER BY id ASC",
                (batch_id,),
            ).fetchall()
    finally:
        conn.close()
    return [_verification_dict(row) for row in rows]
