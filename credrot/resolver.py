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
            task_id,
            now,
            "NO_BINDINGS",
            "task has no credential bindings",
            required_roles=task.required_roles,
            role_integrity=[
                _integrity_entry(role, "repair", "NO_BOUND_CREDENTIAL", [], [], None)
                for role in task.required_roles
            ],
        )
        _audit_failure(db, clock, result, write_audit)
        return result

    verifications = latest_verifications(db)
    attempts: list[dict[str, Any]] = []
    selected: dict[str, dict[str, Any]] = {}
    role_integrity: list[dict[str, Any]] = []
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
            role_integrity.append(
                _integrity_entry(role, "repair", "NO_BOUND_CREDENTIAL", [], [], None)
            )
            continue

        bound_ids = [credential.id for credential in candidates]
        revoked_ids = [
            credential.id
            for credential in candidates
            if credential.effective_state(now) == "revoked"
        ]

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
            if len(revoked_ids) == len(bound_ids):
                role_integrity.append(
                    _integrity_entry(
                        role, "repair", "ALL_BINDINGS_REVOKED",
                        bound_ids, revoked_ids, None,
                    )
                )
            else:
                role_integrity.append(
                    _integrity_entry(
                        role, "degraded", "NO_USABLE_CANDIDATE",
                        bound_ids, revoked_ids, None,
                    )
                )
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
            reason = (
                "STANDBY_SELECTED"
                if attempt["state"] != "active"
                else "UNVERIFIED_SELECTED"
            )
            role_integrity.append(
                _integrity_entry(
                    role, "degraded", reason, bound_ids, revoked_ids, chosen.id
                )
            )
        else:
            role_integrity.append(
                _integrity_entry(
                    role, "ok", "RESOLVED_ACTIVE", bound_ids, revoked_ids, chosen.id
                )
            )

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
        "role_integrity": role_integrity,
    }
    if not ok:
        _audit_failure(db, clock, result, write_audit)
    return result


def _integrity_entry(
    role: str,
    status: str,
    reason: str,
    bound: list[str],
    revoked: list[str],
    selected: str | None,
) -> dict[str, Any]:
    """Per-role reference-integrity verdict.

    ``repair`` means a structural break (no binding, or every binding revoked)
    that only manual re-binding can fix; ``degraded`` is a runtime condition
    (expired, unverified, failed check, standby fallback) that rotation or
    verification can heal; ``ok`` means an active verified selection.
    """
    return {
        "role": role,
        "status": status,
        "reason": reason,
        "bound": list(bound),
        "revoked": list(revoked),
        "selected": selected,
    }


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


def _failure(
    task_id: str,
    now: datetime,
    code: str,
    message: str,
    *,
    required_roles: tuple[str, ...] = (),
    role_integrity: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "task_id": task_id,
        "source": None,
        "task_priority": None,
        "required_roles": list(required_roles),
        "checked_at": format_time(now),
        "code": code,
        "message": message,
        "degraded": False,
        "credentials": {},
        "attempts": [],
        "role_integrity": list(role_integrity or []),
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
