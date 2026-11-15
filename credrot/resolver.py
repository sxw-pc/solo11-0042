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
                 "reason": "NO_MATCHING_ROLE", "verification_age_minutes": None}
            )
            continue

        viable: list[tuple[tuple[int, int, int, str], Credential, dict[str, Any]]] = []
        role_evals: list[tuple[Credential, dict[str, Any]]] = []
        for credential in sorted(candidates, key=lambda item: item.id):
            rank = task.bindings.index(credential.id)
            attempt, usable = _evaluate(
                credential, rank, now, verifications.get(credential.id), policy
            )
            attempt["role"] = role
            attempts.append(attempt)
            role_evals.append((credential, attempt))
            if usable:
                viable.append((_sort_key(credential, attempt), credential, attempt))

        if not viable:
            softened = _soften_stale(role, role_evals, policy)
            if softened is not None:
                credential, attempt = softened
                viable.append((_sort_key(credential, attempt), credential, attempt))

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
        if attempt.get("softened"):
            selected[role]["softened"] = True
        if (attempt["state"] != "active" or attempt.get("unverified")
                or attempt.get("softened")):
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
        "verification_age_minutes": None,
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
    attempt["verification_age_minutes"] = round(age_seconds / 60, 3)
    max_age = policy.role_max_age_minutes.get(
        credential.role, policy.max_verification_age_minutes
    )
    if age_seconds > max_age * 60:
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


def _sort_key(
    credential: Credential, attempt: dict[str, Any]
) -> tuple[int, int, int, str]:
    return (
        0 if attempt["state"] == "active" else 1,
        attempt["binding_rank"],
        -credential.version,
        credential.id,
    )


def _soften_stale(
    role: str,
    role_evals: list[tuple[Credential, dict[str, Any]]],
    policy: Policy,
) -> tuple[Credential, dict[str, Any]] | None:
    """Pick a stale-but-valid candidate when a listed role has nothing fresh.

    Allowed only when every state-usable candidate was rejected solely for
    verification age; the pick's latest outcome must be ``valid``. Runs only
    after the normal pass found nothing, so fresh candidates always win.
    """
    if role not in policy.soften_stale_for_roles:
        return None
    evaluated = [
        (credential, attempt)
        for credential, attempt in role_evals
        if attempt["state"] in {"active", "standby"}
    ]
    if not evaluated or any(
        attempt["reason"] != "STALE_VERIFICATION" for _, attempt in evaluated
    ):
        return None
    softenable = [
        (credential, attempt)
        for credential, attempt in evaluated
        if (attempt["verification"] or {}).get("outcome") == "valid"
    ]
    if not softenable:
        return None
    credential, attempt = min(softenable, key=lambda pair: _sort_key(*pair))
    attempt.update({"usable": True, "softened": True, "reason": "STALE_SOFTENED"})
    return credential, attempt


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
