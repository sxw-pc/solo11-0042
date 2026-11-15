"""Pure, deterministic rotation-plan construction."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Iterable, Mapping

from .clock import format_time, parse_time
from .config import Policy
from .gating import POST_ROTATION_UNVERIFIED, GateDecision
from .model import Credential, PlanItem, RotationPlan, Task


URGENCY = {
    "EXPIRED": 0,
    "CRITICAL_EXPIRY": 1,
    "EXPIRING_SOON": 2,
    "REDUNDANCY_LOW": 3,
    POST_ROTATION_UNVERIFIED: 4,
}
# Gate entries observe a fact rather than schedule work, so they sort after
# every actionable item regardless of task priority.
GATE_PRIORITY = 9999


def build_plan(
    credentials: Mapping[str, Credential] | Iterable[Credential],
    tasks: Mapping[str, Task] | Iterable[Task],
    policy: Policy,
    now: datetime,
    gates: Mapping[str, GateDecision] | None = None,
) -> RotationPlan:
    """Build a plan without reading a clock, database or network.

    ``gates`` is precomputed by :func:`credrot.gating.load_gates`; an absent
    mapping means no gate applies. Passing it in keeps this function pure.
    """
    now = parse_time(now)
    creds: dict[str, Credential] = _index(credentials)  # type: ignore[assignment]
    ordered_tasks = sorted(
        _index(tasks).values(), key=lambda task: (task.priority, task.id)
    )  # type: ignore[union-attr, arg-type]
    decisions = {
        credential_id: decision
        for credential_id, decision in (gates or {}).items()
        if credential_id in creds
    }

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
        entries[credential.id] = _rotate_item(credential, effective, reason_code,
                                              reason, ordered_tasks, now)
        urgency[credential.id] = URGENCY[reason_code]
        final_state[credential.id] = entries[credential.id].target_state

    active_deficits: list[str] = []
    for task in ordered_tasks:
        for role in task.required_roles:
            active_count = _count_final(task, role, "active", creds, final_state)
            if active_count >= task.min_active:
                continue
            for credential in _promotion_candidates(task, role, creds, final_state):
                if active_count >= task.min_active:
                    break
                if credential.id in decisions:
                    continue  # gated: no promote, surfaced as a deficit below
                current = entries.get(credential.id)
                if current is not None and current.target_state == "standby":
                    entries[credential.id] = replace(current, target_state="active")
                elif current is None:
                    entries[credential.id] = _promote_item(
                        credential, ordered_tasks, task, now
                    )
                    urgency[credential.id] = URGENCY["REDUNDANCY_LOW"]
                final_state[credential.id] = "active"
                active_count += 1
            if active_count < task.min_active:
                active_deficits.append(f"{task.id}:{role}")

    backup_deficits: dict[str, int] = {}
    for task in ordered_tasks:
        standby = sum(
            1 for credential_id in task.bindings
            if credential_id in creds
            and creds[credential_id].role in task.required_roles
            and final_state.get(credential_id) == "standby"
        )
        if task.backup_margin > standby:
            backup_deficits[task.id] = task.backup_margin - standby

    # Hold entries are the durable record that a committed rotation still
    # awaits verification; they replace any rotate/promote entry above.
    withheld = sorted(
        credential_id for credential_id in decisions
        if creds[credential_id].status != "rotating"
    )
    for credential_id in withheld:
        entries[credential_id] = _gate_item(
            creds[credential_id], decisions[credential_id], ordered_tasks, now
        )
        urgency[credential_id] = URGENCY[POST_ROTATION_UNVERIFIED]

    pool = sorted(
        entries.values(),
        key=lambda item: (urgency[item.credential_id], item.task_priority, item.credential_id),
    )
    # Only actionable items count against the per-plan cap.
    actionable = [item for item in pool if item.reason_code != POST_ROTATION_UNVERIFIED]
    gate_items = [item for item in pool if item.reason_code == POST_ROTATION_UNVERIFIED]
    truncated = max(0, len(actionable) - policy.max_items_per_plan)
    items = tuple(actionable[: policy.max_items_per_plan] + gate_items)
    summary = _summary(items, creds, ordered_tasks, backup_deficits,
                       active_deficits, truncated, withheld, decisions)
    return RotationPlan(
        batch_id=_batch_id(now, items), generated_at=now, items=items,
        summary=summary,
    )


def _index(items: Mapping[str, object] | Iterable[object]) -> dict[str, object]:
    if isinstance(items, Mapping):
        return dict(items)
    return {item.id: item for item in items}  # type: ignore[attr-defined]


def _item(
    credential: Credential, action: str, from_state: str, target_state: str,
    reason_code: str, reason: str, tasks: list[Task], now: datetime,
    task_priority: int | None = None,
) -> PlanItem:
    affected = _affected_tasks(credential, tasks)
    return PlanItem(
        credential_id=credential.id,
        source=credential.source,
        role=credential.role,
        action=action,
        from_state=from_state,
        target_state=target_state,
        reason_code=reason_code,
        reason=reason,
        days_to_expiry=credential.days_left(now),
        affected_tasks=tuple(task.id for task in affected),
        task_priority=(
            min((task.priority for task in affected), default=999)
            if task_priority is None else task_priority
        ),
    )


def _rotate_item(
    credential: Credential, effective: str, reason_code: str, reason: str,
    tasks: list[Task], now: datetime,
) -> PlanItem:
    target = "active" if credential.status in {"active", "rotating"} else "standby"
    return _item(credential, "rotate", effective, target, reason_code, reason,
                 tasks, now)


def _promote_item(
    credential: Credential, tasks: list[Task], task: Task, now: datetime
) -> PlanItem:
    return _item(
        credential, "promote", "standby", "active", "REDUNDANCY_LOW",
        f"active coverage below min_active for {task.id}", tasks, now,
    )


def _gate_item(
    credential: Credential, decision: GateDecision, tasks: list[Task], now: datetime
) -> PlanItem:
    state = credential.effective_state(now)
    return _item(
        credential, "hold", state, state, POST_ROTATION_UNVERIFIED,
        decision.reason(), tasks, now, task_priority=GATE_PRIORITY,
    )


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
    return sorted(
        (
            credentials[credential_id]
            for credential_id in task.bindings
            if credential_id in credentials
            and credentials[credential_id].role == role
            and final_state.get(credential_id) == "standby"
        ),
        key=lambda cred: cred.id,
    )


def _summary(
    items: tuple[PlanItem, ...],
    credentials: Mapping[str, Credential],
    tasks: list[Task],
    backup_deficits: dict[str, int],
    active_deficits: list[str],
    truncated: int,
    withheld: list[str],
    decisions: Mapping[str, GateDecision],
) -> dict[str, object]:
    return {
        "total_items": len(items),
        "counts_by_reason": _counts(items, "reason_code"),
        "counts_by_action": _counts(items, "action"),
        "affected_tasks": sorted({t for item in items for t in item.affected_tasks}),
        "backup_deficits": dict(sorted(backup_deficits.items())),
        "active_deficits": sorted(active_deficits),
        "truncated": truncated,
        "post_rotation_gated": sorted(withheld),
        "gate_evidence": {
            credential_id: _evidence(decisions[credential_id])
            for credential_id in sorted(withheld)
        },
        "credential_count": len(credentials),
        "task_count": len(tasks),
    }


def _counts(items: tuple[PlanItem, ...], field: str) -> dict[str, int]:
    return dict(sorted(Counter(getattr(item, field) for item in items).items()))


def _evidence(decision: GateDecision) -> dict[str, object]:
    checked = decision.last_valid_verification_at
    return {
        "rotation_batch_id": decision.rotation_batch_id,
        "rotated_at": format_time(decision.rotated_at),
        "last_valid_verification_at": format_time(checked) if checked else None,
    }


def _batch_id(now: datetime, items: tuple[PlanItem, ...]) -> str:
    payload = format_time(now) + "|" + "|".join(
        f"{item.credential_id}:{item.action}:{item.target_state}:{item.reason_code}"
        for item in items
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    return f"BATCH-{stamp}-{digest}"
