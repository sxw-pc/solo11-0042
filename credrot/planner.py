"""Pure, deterministic rotation-plan construction."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Iterable, Mapping

from .clock import format_time, parse_time
from .config import Policy
from .model import Credential, PlanItem, Precondition, RotationPlan, Task


URGENCY = {"EXPIRED": 0, "CRITICAL_EXPIRY": 1, "EXPIRING_SOON": 2, "REDUNDANCY_LOW": 3}


def snapshot_precondition(credential: Credential, now: datetime) -> Precondition:
    """Capture the credential identity a plan item assumes at execution."""
    return Precondition(
        version=credential.version,
        status=credential.status,
        effective_state=credential.effective_state(now),
        expires_at=format_time(credential.expires_at),
    )


def build_plan(
    credentials: Mapping[str, Credential] | Iterable[Credential],
    tasks: Mapping[str, Task] | Iterable[Task],
    policy: Policy,
    now: datetime,
) -> RotationPlan:
    """Build a plan without reading a clock, database or network."""
    now = parse_time(now)
    creds = (
        dict(credentials)
        if isinstance(credentials, Mapping)
        else {item.id: item for item in credentials}
    )
    task_map = (
        dict(tasks) if isinstance(tasks, Mapping) else {item.id: item for item in tasks}
    )
    ordered_tasks = sorted(task_map.values(), key=lambda task: (task.priority, task.id))

    entries: dict[str, PlanItem] = {}
    urgency: dict[str, int] = {}
    final_state: dict[str, str] = {
        cred_id: cred.effective_state(now) for cred_id, cred in creds.items()
    }

    for credential in sorted(creds.values(), key=lambda cred: cred.id):
        effective = credential.effective_state(now)
        if effective == "revoked" or credential.status == "rotating":
            continue
        reason_code, reason = _expiry_reason(credential, effective, now, policy)
        if reason_code is None:
            continue
        affected = _affected_tasks(credential, ordered_tasks)
        target = "active" if credential.status in {"active", "rotating"} else "standby"
        item = PlanItem(
            credential_id=credential.id,
            source=credential.source,
            role=credential.role,
            action="rotate",
            from_state=effective,
            target_state=target,
            reason_code=reason_code,
            reason=reason,
            days_to_expiry=credential.days_left(now),
            affected_tasks=tuple(task.id for task in affected),
            task_priority=min((task.priority for task in affected), default=999),
            precondition=snapshot_precondition(credential, now),
        )
        entries[credential.id] = item
        urgency[credential.id] = URGENCY[reason_code]
        final_state[credential.id] = target

    active_deficits: list[str] = []
    for task in ordered_tasks:
        for role in task.required_roles:
            active_count = _count_final(task, role, "active", creds, final_state)
            if active_count >= task.min_active:
                continue
            candidates = _promotion_candidates(task, role, creds, final_state)
            for credential in candidates:
                if active_count >= task.min_active:
                    break
                current = entries.get(credential.id)
                if current is not None and current.target_state == "standby":
                    entries[credential.id] = replace(current, target_state="active")
                elif current is None:
                    affected = _affected_tasks(credential, ordered_tasks)
                    entries[credential.id] = PlanItem(
                        credential_id=credential.id,
                        source=credential.source,
                        role=credential.role,
                        action="promote",
                        from_state="standby",
                        target_state="active",
                        reason_code="REDUNDANCY_LOW",
                        reason=f"active coverage below min_active for {task.id}",
                        days_to_expiry=credential.days_left(now),
                        affected_tasks=tuple(item.id for item in affected),
                        task_priority=min(
                            (item.priority for item in affected), default=task.priority
                        ),
                        precondition=snapshot_precondition(credential, now),
                    )
                    urgency[credential.id] = URGENCY["REDUNDANCY_LOW"]
                final_state[credential.id] = "active"
                active_count += 1
            if active_count < task.min_active:
                active_deficits.append(f"{task.id}:{role}")

    backup_deficits: dict[str, int] = {}
    for task in ordered_tasks:
        standby = sum(
            1
            for credential_id in task.bindings
            if credential_id in creds
            and creds[credential_id].role in task.required_roles
            and final_state.get(credential_id) == "standby"
        )
        deficit = max(0, task.backup_margin - standby)
        if deficit:
            backup_deficits[task.id] = deficit

    ordered_items = sorted(
        entries.values(),
        key=lambda item: (urgency[item.credential_id], item.task_priority, item.credential_id),
    )
    truncated = max(0, len(ordered_items) - policy.max_items_per_plan)
    if truncated:
        ordered_items = ordered_items[: policy.max_items_per_plan]
    items = tuple(ordered_items)
    summary = _summary(items, creds, ordered_tasks, backup_deficits,
                       active_deficits, truncated)
    batch_id = _batch_id(now, items)
    return RotationPlan(batch_id=batch_id, generated_at=now, items=items, summary=summary)


def _expiry_reason(
    credential: Credential, effective: str, now: datetime, policy: Policy
) -> tuple[str | None, str]:
    if effective == "expired":
        return "EXPIRED", "expires_at has passed; simulated renewal required"
    if effective == "rotating":
        return None, ""
    days = credential.days_left(now)
    if 0 <= days <= policy.critical_horizon_days:
        return "CRITICAL_EXPIRY", f"{days} day(s) remain; inside critical horizon"
    if 0 <= days <= policy.horizon_days:
        return "EXPIRING_SOON", f"{days} day(s) remain; inside rotation horizon"
    return None, ""


def _affected_tasks(credential: Credential, tasks: list[Task]) -> list[Task]:
    return [
        task
        for task in tasks
        if credential.id in task.bindings
        and task.source == credential.source
        and credential.role in task.required_roles
    ]


def _count_final(
    task: Task, role: str, target: str,
    credentials: Mapping[str, Credential], final_state: Mapping[str, str],
) -> int:
    return sum(
        1
        for credential_id in task.bindings
        if credential_id in credentials
        and credentials[credential_id].source == task.source
        and credentials[credential_id].role == role
        and final_state.get(credential_id) == target
    )


def _promotion_candidates(
    task: Task, role: str,
    credentials: Mapping[str, Credential], final_state: Mapping[str, str],
) -> list[Credential]:
    candidates = [
        credentials[credential_id]
        for credential_id in task.bindings
        if credential_id in credentials
        and credentials[credential_id].role == role
        and final_state.get(credential_id) == "standby"
    ]
    return sorted(candidates, key=lambda cred: cred.id)


def _summary(
    items: tuple[PlanItem, ...],
    credentials: Mapping[str, Credential],
    tasks: list[Task],
    backup_deficits: dict[str, int],
    active_deficits: list[str],
    truncated: int,
) -> dict[str, object]:
    reasons = Counter(item.reason_code for item in items)
    actions = Counter(item.action for item in items)
    affected = sorted({task for item in items for task in item.affected_tasks})
    return {
        "total_items": len(items),
        "counts_by_reason": dict(sorted(reasons.items())),
        "counts_by_action": dict(sorted(actions.items())),
        "affected_tasks": affected,
        "backup_deficits": dict(sorted(backup_deficits.items())),
        "active_deficits": sorted(active_deficits),
        "truncated": truncated,
        "credential_count": len(credentials),
        "task_count": len(tasks),
    }


def _batch_id(now: datetime, items: tuple[PlanItem, ...]) -> str:
    payload = format_time(now) + "|" + "|".join(
        "|".join(
            (
                item.credential_id,
                item.action,
                item.target_state,
                item.reason_code,
                str(item.precondition.version),
                item.precondition.status,
                item.precondition.effective_state,
                item.precondition.expires_at,
            )
        )
        for item in items
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"BATCH-{stamp}-{digest}"
