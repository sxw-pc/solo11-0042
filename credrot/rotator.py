"""Transactional, idempotent execution of an approved rotation plan."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from .audit import AuditLog
from .clock import Clock, format_time
from .config import Policy
from .db import Database
from .state_machine import StateMachine
from .store import (
    canonical_json,
    get_batch,
    get_credential,
    insert_rotation_item,
    set_batch_result,
    update_credential,
)


class UnknownBatch(ValueError):
    pass


class RotationAbort(Exception):
    pass


@dataclass(frozen=True)
class RotationResult:
    batch_id: str
    status: str
    items: list[dict[str, Any]]
    executed: int
    skipped: int
    failed: int
    summary: dict[str, Any]
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "status": self.status,
            "items": self.items,
            "executed": self.executed,
            "skipped": self.skipped,
            "failed": self.failed,
            "summary": self.summary,
            "error": self.error,
        }


def rotate_batch(
    db: Database, batch_id: str, clock: Clock, policy: Policy
) -> RotationResult:
    batch = get_batch(db, batch_id)
    if batch is None:
        raise UnknownBatch(f"unknown rotation batch: {batch_id}")
    plan = batch["plan"]
    plan_items: list[dict[str, Any]] = list(plan.get("items", []))
    now = clock.now()
    if batch["status"] == "completed":
        return _already_completed(batch_id, plan_items, batch["summary"])

    audit = AuditLog(db, clock)
    machine = StateMachine(audit=audit, clock=clock)
    results: list[dict[str, Any]] = []
    executed = skipped = 0
    failed_item: dict[str, Any] | None = None
    try:
        with db.transaction() as conn:
            for item in plan_items:
                failed_item = item
                outcome = _apply_item(conn, audit, machine, item, batch_id, now, policy)
                if outcome["result"] == "skipped":
                    skipped += 1
                else:
                    executed += 1
                results.append(outcome)
                insert_rotation_item(
                    conn, batch_id, item, outcome["result"],
                    outcome.get("failure_reason"), now,
                )
            failed_item = None
            summary = dict(batch["summary"])
            summary["execution"] = {
                "executed": executed,
                "skipped": skipped,
                "failed": 0,
                "total": len(plan_items),
            }
            conn.execute(
                """UPDATE rotation_batches
                   SET status = 'completed', executed_at = ?, failure_reason = NULL,
                       summary_json = ?
                   WHERE batch_id = ?""",
                (format_time(now), canonical_json(summary), batch_id),
            )
            audit.record(
                actor="rotator",
                action="rotation.completed",
                subject=batch_id,
                before={"status": batch["status"]},
                after={"status": "completed", "execution": summary["execution"]},
                batch_id=batch_id,
                reason="rotation transaction committed",
                at=now,
                conn=conn,
            )
        return RotationResult(
            batch_id, "completed", results, executed, skipped, 0, summary
        )
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        failed_row = (
            _result(failed_item, "failed") if failed_item is not None else None
        )
        if failed_row is not None:
            failed_row["failure_reason"] = reason
        _record_failure(
            db, audit, batch, batch_id, failed_item, reason, now
        )
        return RotationResult(
            batch_id, "failed", [failed_row] if failed_row else [], 0, 0, 1,
            {"executed": 0, "skipped": 0, "failed": 1, "total": len(plan_items)},
            reason,
        )


def _apply_item(
    conn: Any,
    audit: AuditLog,
    machine: StateMachine,
    item: dict[str, Any],
    batch_id: str,
    now: datetime,
    policy: Policy,
) -> dict[str, Any]:
    credential_id = item["credential_id"]
    action = item["action"]
    target = item["target_state"]
    reason = item.get("reason", item.get("reason_code", "rotation"))
    before = _result(item, "executed")

    credential = get_credential(conn, credential_id)
    if credential is None:
        raise RotationAbort(f"{credential_id}: credential no longer exists")
    if credential.status == "revoked":
        raise RotationAbort(f"{credential_id}: revoked credentials are immutable")
    if target not in {"active", "standby"}:
        raise RotationAbort(f"{credential_id}: unsupported target state {target}")

    if action == "promote":
        if credential.effective_state(now) == "active":
            before["result"] = "skipped"
            before["failure_reason"] = "target state already reached"
            return before
        if credential.effective_state(now) != "standby":
            raise RotationAbort(
                f"{credential_id}: cannot promote from "
                f"{credential.effective_state(now)}"
            )
        updated = machine.transition(
            credential, "active", actor="rotator", reason=reason,
            at=now, conn=conn, batch_id=batch_id,
        )
        update_credential(conn, updated, now)
        return before

    if action != "rotate":
        raise RotationAbort(f"{credential_id}: unsupported action {action}")

    working, _ = machine.expire_if_due(
        credential, now, conn=conn, batch_id=batch_id
    )
    if working.status == "rotating":
        before["result"] = "skipped"
        before["failure_reason"] = "rotation already in progress"
        return before
    if working.status == "revoked":
        raise RotationAbort(f"{credential_id}: revoked credentials are immutable")
    if working.status not in {"active", "standby", "expired"}:
        raise RotationAbort(f"{credential_id}: cannot rotate from {working.status}")

    rotating = machine.transition(
        working, "rotating", actor="rotator", reason=reason,
        at=now, conn=conn, batch_id=batch_id,
    )
    renewed = rotating.changed(
        version=rotating.version + 1,
        issued_at=now,
        expires_at=now + timedelta(days=policy.rotation_period_days),
    )
    audit.record(
        actor="rotator",
        action="credential.renewed",
        subject=credential_id,
        before={"version": rotating.version, "expires_at": format_time(rotating.expires_at)},
        after={"version": renewed.version, "expires_at": format_time(renewed.expires_at)},
        batch_id=batch_id,
        reason=reason,
        at=now,
        conn=conn,
    )
    final = machine.transition(
        renewed, target, actor="rotator", reason=reason,
        at=now, conn=conn, batch_id=batch_id,
    )
    update_credential(conn, final, now)
    return before


def _result(item: dict[str, Any], result: str) -> dict[str, Any]:
    return {
        "credential_id": item["credential_id"],
        "action": item.get("action", "rotate"),
        "from_state": item.get("from_state", "unknown"),
        "to_state": item.get("target_state", "unknown"),
        "reason_code": item.get("reason_code", "UNKNOWN"),
        "result": result,
        "ok": result in {"executed", "skipped"},
        "failure_reason": None,
    }


def _already_completed(
    batch_id: str, items: list[dict[str, Any]], summary: dict[str, Any]
) -> RotationResult:
    results = []
    for item in items:
        row = _result(item, "skipped")
        row["failure_reason"] = "batch already completed; no state change"
        results.append(row)
    return RotationResult(
        batch_id, "completed", results, 0, len(results), 0,
        dict(summary), None,
    )


def _record_failure(
    db: Database, audit: AuditLog, batch: dict[str, Any], batch_id: str,
    failed_item: dict[str, Any] | None, reason: str, now: datetime,
) -> None:
    """Persist failure metadata after the credential transaction rolled back."""
    set_batch_result(
        db, batch_id, "failed", now, summary=dict(batch["summary"]),
        failure_reason=reason,
    )
    failed_credential = (
        failed_item.get("credential_id") if failed_item is not None else None
    )
    if failed_credential is not None:
        item = failed_item
        conn = db.connect()
        try:
            conn.execute(
                """INSERT OR REPLACE INTO rotation_items
                (batch_id, credential_id, action, from_state, to_state,
                 reason_code, result, ok, failure_reason, at)
                VALUES (?, ?, ?, ?, ?, ?, 'failed', 0, ?, ?)""",
                (
                    batch_id, failed_credential, item.get("action", "rotate"),
                    item.get("from_state", "unknown"),
                    item.get("target_state", "unknown"),
                    item.get("reason_code", "UNKNOWN"), reason, format_time(now),
                ),
            )
            conn.commit()
        finally:
            conn.close()
    audit.record(
        actor="rotator",
        action="rotation.failed",
        subject=batch_id,
        before={"status": batch["status"]},
        after={"status": "failed"},
        batch_id=batch_id,
        reason=reason,
        at=now,
    )
