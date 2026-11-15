"""Persistence helpers for verification observations."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from .clock import format_time, parse_time
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


def latest_verification(
    conn: sqlite3.Connection, credential_id: str
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM verifications WHERE credential_id = ? ORDER BY id DESC LIMIT 1",
        (credential_id,),
    ).fetchone()
    return _verification_dict(row) if row else None


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


def latest_valid_verifications(
    db: Database, as_of: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Most recent ``valid`` observation per credential, ordered by time.

    ``MAX(id)`` is not usable here: observations for one credential can be
    inserted after an earlier check with a later ``checked_at``. A row only
    counts when its outcome is ``valid`` *and* its batch context agrees with
    the timestamp: an unattributed check (no batch) is standalone evidence,
    while a batch-scoped check must belong to a batch that committed, and
    every batch-scoped observation of that credential written at the same
    timestamp must itself be ``valid``. That rejects e.g. a ``valid`` row left
    behind for a batch whose other observations were rolled back.
    """
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT v.credential_id, v.batch_id, v.outcome, v.code,
                      v.checked_at, v.latency_ms, rb.status AS batch_status
               FROM verifications v
               LEFT JOIN rotation_batches rb ON rb.batch_id = v.batch_id"""
        ).fetchall()
    finally:
        conn.close()

    cutoff = parse_time(as_of) if as_of is not None else None
    by_stamp: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        if row["batch_id"] is not None and row["batch_status"] != "completed":
            continue
        checked_at = parse_time(row["checked_at"])
        if cutoff is not None and checked_at > cutoff:
            continue
        by_stamp.setdefault((row["credential_id"], format_time(checked_at)), []).append(
            row
        )

    latest: dict[str, dict[str, Any]] = {}
    for (credential_id, stamp), group in by_stamp.items():
        if any(member["outcome"] != "valid" for member in group):
            continue
        representative = min(group, key=lambda member: member["batch_id"] or "")
        candidate = {
            "credential_id": credential_id,
            "outcome": representative["outcome"],
            "code": representative["code"],
            "checked_at": stamp,
            "batch_id": representative["batch_id"],
            "latency_ms": representative["latency_ms"],
        }
        current = latest.get(credential_id)
        if current is None or (candidate["checked_at"], candidate["batch_id"] or "") > (
            current["checked_at"],
            current["batch_id"] or "",
        ):
            latest[credential_id] = candidate
    return latest
