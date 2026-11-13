"""Task-to-credential resolution with explicit degraded fallbacks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .audit import AuditLog
from .clock import Clock, format_time, parse_time
from .config import Policy
from .db import Database
from .model import Credential
from .store import load_credentials, load_tasks
from .verification_store import latest_verifications


def resolve(
    db: Database,
    task_id: str,
    clock: Clock,
    policy: Policy,
    *,
    write_audit: bool = True,
) -> dict[str, Any]:
    """Resolve every role needed by a task; failures are structured data."""
    now = clock.now()
    tasks = load_tasks(db)
    credentials = load_credentials(db)
    task = tasks.get(task_id)
    if task is None:
        result = _failure(
            task_id, now, "TASK_NOT_FOUND", f"task {task_id} is not registered"
        )
        _audit_failure(db, clock, result, write_audit)
        return result
    if not task.bindings:
        result = _failure(
            task_id, now, "NO_BINDINGS", "task has no credential bindings"
        )
        _audit_failure(db, clock, result, write_audit)
        return result

    verifications = latest_verifications(db)
    attempts: list[dict[str, Any]] = []
    selected: dict[str, dict[str, Any]] = {}
    failure_code = "OK"
    failure_message = "all required roles resolved"
    degraded = False

    for role in task.required_roles:
        candidates = [
            credentials[credential_id]
            for credential_id in task.bindings
            if credential_id in credentials
            and credentials[credential_id].role == role
        ]
        if not candidates:
            failure_code = "NO_MATCHING_ROLE"
            failure_message = f"no bound credential supplies role {role}"
            attempts.append(
                {"role": role, "candidate_id": None, "usable": False,
                 "reason": "NO_MATCHING_ROLE"}
            )
            continue

        viable: list[tuple[tuple[int, int, int, str], Credential, dict[str, Any]]] = []
        for credential in sorted(candidates, key=lambda item: item.id):
            rank = task.bindings.index(credential.id)
            attempt, usable = _evaluate(
                credential, rank, now, verifications.get(credential.id), policy
            )
            attempt["role"] = role
            attempts.append(attempt)
            if usable:
                key = (
                    0 if attempt["state"] == "active" else 1,
                    rank,
                    -credential.version,
                    credential.id,
                )
                viable.append((key, credential, attempt))

        if not viable:
            if failure_code == "OK":
                failure_code = "NO_HEALTHY_CANDIDATE"
                failure_message = f"no usable credential for role {role}"
            continue
        viable.sort(key=lambda row: row[0])
        _, chosen, attempt = viable[0]
        selected[role] = {
            "credential_id": chosen.id,
            "version": chosen.version,
            "role": chosen.role,
            "state": attempt["state"],
            "fingerprint": chosen.fingerprint,
            "verification": attempt["verification"],
            "binding_rank": task.bindings.index(chosen.id),
        }
        if attempt["state"] != "active" or attempt.get("unverified"):
            degraded = True

    ok = len(selected) == len(task.required_roles) and failure_code == "OK"
    result = {
        "ok": ok,
        "task_id": task.id,
        "source": task.source,
        "task_priority": task.priority,
        "required_roles": list(task.required_roles),
        "checked_at": format_time(now),
        "code": "OK" if ok else failure_code,
        "message": failure_message,
        "degraded": degraded,
        "credentials": {role: selected.get(role) for role in task.required_roles},
        "attempts": attempts,
    }
    if not ok:
        _audit_failure(db, clock, result, write_audit)
    return result


def _evaluate(
    credential: Credential,
    rank: int,
    now: datetime,
    verification: dict[str, Any] | None,
    policy: Policy,
) -> tuple[dict[str, Any], bool]:
    state = credential.effective_state(now)
    attempt: dict[str, Any] = {
        "candidate_id": credential.id,
        "binding_rank": rank,
        "state": state,
        "verification": None,
        "usable": False,
        "reason": "",
    }
    if state not in {"active", "standby"}:
        attempt["reason"] = f"STATE_{state.upper()}"
        return attempt, False
    if verification is None:
        if policy.allow_unverified:
            attempt.update(
                {"usable": True, "unverified": True, "reason": "ACCEPTED_UNVERIFIED"}
            )
            return attempt, True
        attempt["reason"] = "UNVERIFIED"
        return attempt, False

    summary = {
        "outcome": verification["outcome"],
        "code": verification["code"],
        "checked_at": verification["checked_at"],
    }
    attempt["verification"] = summary
    age_seconds = (now - parse_time(verification["checked_at"])).total_seconds()
    if age_seconds > policy.max_verification_age_minutes * 60:
        attempt["reason"] = "STALE_VERIFICATION"
        return attempt, False
    if verification["outcome"] != "valid":
        attempt["reason"] = f"VERIFICATION_{verification['outcome'].upper()}"
        return attempt, False
    attempt["usable"] = True
    attempt["reason"] = (
        "ACCEPTED_ACTIVE" if state == "active" else "ACCEPTED_STANDBY"
    )
    return attempt, True


def _failure(task_id: str, now: datetime, code: str, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "task_id": task_id,
        "source": None,
        "task_priority": None,
        "required_roles": [],
        "checked_at": format_time(now),
        "code": code,
        "message": message,
        "degraded": False,
        "credentials": {},
        "attempts": [],
    }


def _audit_failure(
    db: Database, clock: Clock, result: dict[str, Any], write_audit: bool
) -> None:
    if not write_audit:
        return
    audit = AuditLog(db, clock)
    audit.record(
        actor="resolver",
        action="resolution.failed",
        subject=result["task_id"],
        before={},
        after={"code": result["code"], "attempts": len(result["attempts"])},
        batch_id=None,
        reason=result["message"],
        at=clock.now(),
    )
