"""Append-only audit trail stored alongside the credential ledger."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Mapping

from .clock import Clock, format_time


def _json(value: Mapping[str, Any] | None) -> str:
    return json.dumps(
        dict(value or {}), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


class AuditLog:
    """Write and read audit events.

    A connection can be supplied so audit writes participate in the caller's
    SQLite transaction. Without one, the event is committed immediately.
    """

    def __init__(self, db: Any, clock: Clock):
        self.db = db
        self.clock = clock

    def record(
        self,
        *,
        actor: str,
        action: str,
        subject: str,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        batch_id: str | None = None,
        reason: str = "",
        at: datetime | None = None,
        conn: Any = None,
    ) -> dict[str, Any]:
        event_at = format_time(at or self.clock.now())
        values = (
            event_at,
            str(actor),
            str(action),
            str(subject),
            _json(before),
            _json(after),
            batch_id,
            str(reason),
        )
        if conn is not None:
            self._insert(conn, values)
        else:
            connection = self.db.connect()
            try:
                self._insert(connection, values)
                connection.commit()
            finally:
                connection.close()
        return {
            "at": event_at,
            "actor": actor,
            "action": action,
            "subject": subject,
            "before": dict(before or {}),
            "after": dict(after or {}),
            "batch_id": batch_id,
            "reason": reason,
        }

    @staticmethod
    def _insert(conn: Any, values: tuple[Any, ...]) -> None:
        conn.execute(
            """
            INSERT INTO audit_events
                (at, actor, action, subject, before_json, after_json,
                 batch_id, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )

    def list_events(
        self,
        *,
        batch_id: str | None = None,
        subject: str | None = None,
        action: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("batch_id", batch_id),
            ("subject", subject),
            ("action", action),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(int(limit), 5000)))
        connection = self.db.connect()
        try:
            rows = connection.execute(
                f"""
                SELECT at, actor, action, subject, before_json, after_json,
                       batch_id, reason
                FROM audit_events{where}
                ORDER BY id ASC
                LIMIT ?
                """,
                params,
            ).fetchall()
        finally:
            connection.close()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                {
                    "at": row["at"],
                    "actor": row["actor"],
                    "action": row["action"],
                    "subject": row["subject"],
                    "before": json.loads(row["before_json"] or "{}"),
                    "after": json.loads(row["after_json"] or "{}"),
                    "batch_id": row["batch_id"],
                    "reason": row["reason"],
                }
            )
        return result
