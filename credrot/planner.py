"""Pure, deterministic rotation-plan construction."""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import replace
from datetime import datetime
from typing import Iterable, Mapping

from .clock import format_time, parse_time
from .config import Policy
from .model import Credential, ITEM_ORIGIN_COMPENSATED, PlanItem, RotationPlan, Task


URGENCY = {"EXPIRED": 0, "CRITICAL_EXPIRY": 1, "EXPIRING_SOON": 2, "REDUNDANCY_LOW": 3}


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
    final_state = {cid: cred.effective_state(now) for cid, cred in creds.items()}
    for credential in sorted(creds.values(), key=lambda cred: cred.id):
        effective = credential.effective_state(now)
        if effective == "revoked" or credential.status == "rotating":
            continue
        reason_code, reason = _expiry_reason(credential, effective, now, policy)
        if reason_code is None:
            continue
        affected = _affected_tasks(credential, ordered_tasks)
        target = "active" if credential.status in {"active", "rotating"} else "standby"
        entries[credential.id] = PlanItem(
            credential_id=credential.id, source=credential.source, role=credential.role,
            action="rotate", from_state=effective, target_state=target,
            reason_code=reason_code, reason=reason,
            days_to_expiry=credential.days_left(now),
            affected_tasks=tuple(task.id for task in affected),
            task_priority=min((task.priority for task in affected), default=999),
        )
        urgency[credential.id] = URGENCY[reason_code]
        final_state[credential.id] = target
    for task in ordered_tasks:
        for role in task.required_roles:
            active_count = _count_state(task, role, "active", creds, final_state)
            if active_count >= task.min_active:
                continue
            for credential in _promotion_candidates(task, role, creds, final_state):
                if active_count >= task.min_active:
                    break
                current = entries.get(credential.id)
                if current is not None and current.target_state == "standby":
                    entries[credential.id] = replace(current, target_state="active")
                elif current is None:
                    affected = _affected_tasks(credential, ordered_tasks)
                    entries[credential.id] = PlanItem(
                        credential_id=credential.id, source=credential.source,
                        role=credential.role, action="promote",
                        from_state="standby", target_state="active",
                        reason_code="REDUNDANCY_LOW",
                        reason=f"active coverage below min_active for {task.id}",
                        days_to_expiry=credential.days_left(now),
                        affected_tasks=tuple(item.id for item in affected),
                        task_priority=min((i.priority for i in affected), default=task.priority),
                    )
                    urgency[credential.id] = URGENCY["REDUNDANCY_LOW"]
                final_state[credential.id] = "active"
                active_count += 1
    ordered_items = sorted(entries.values(), key=lambda item: (
        urgency[item.credential_id], item.task_priority, item.credential_id))
    items, displaced, truncated = _redistribute_under_capacity(
        ordered_items, creds, ordered_tasks, policy.max_items_per_plan, now, urgency)
    state = _plan_state(items, creds, now)
    active_deficits = [
        f"{task.id}:{role}" for task in ordered_tasks for role in task.required_roles
        if _count_state(task, role, "active", creds, state) < task.min_active]
    backup_deficits: dict[str, int] = {}
    for task in ordered_tasks:
        standby = sum(1 for cid in task.bindings if cid in creds
                      and creds[cid].role in task.required_roles
                      and state.get(cid) == "standby")
        deficit = max(0, task.backup_margin - standby)
        if deficit:
            backup_deficits[task.id] = deficit
    summary = _summary(items, creds, ordered_tasks, backup_deficits,
                       active_deficits, truncated, displaced)
    return RotationPlan(batch_id=_batch_id(now, items), generated_at=now,
                        items=items, summary=summary)


def _redistribute_under_capacity(ordered_items, credentials, tasks, capacity, now, urgency):
    """Cap via constrained redistribution, not a hard cut.
    Excluded credentials revert to their effective state. A role whose cut
    promote leaves it below min_active may reclaim the slot of a later,
    lower-urgency rotate whose tasks keep every affected role at its minimum;
    rotates strictly more urgent than the least-urgent retained tier are kept.
    """
    selected = {item.credential_id: item for item in ordered_items[:capacity]}
    dropped = {item.credential_id: item for item in ordered_items[capacity:]}
    live_state = _plan_state(selected.values(), credentials, now)
    displaced: list[dict[str, str]] = []
    for task, role in [(t, r) for t in tasks for r in t.required_roles]:
        if _count_state(task, role, "active", credentials, live_state) >= task.min_active:
            continue
        candidate = next((item for item in sorted(dropped.values(),
                          key=lambda i: i.credential_id)
                          if item.action == "promote"
                          and item.credential_id in task.bindings
                          and credentials[item.credential_id].role == role), None)
        if candidate is None:
            continue
        victim = _reclaimable_rotate(selected, credentials, tasks, live_state, now)
        if victim is None:
            continue
        del selected[victim.credential_id]
        live_state[victim.credential_id] = credentials[victim.credential_id].effective_state(now)
        selected[candidate.credential_id] = replace(
            candidate, origin=ITEM_ORIGIN_COMPENSATED, slot_source=victim.credential_id)
        # A committed promote cannot reclaim a second slot through another task.
        del dropped[candidate.credential_id]
        live_state[candidate.credential_id] = "active"
        displaced.append({"credential_id": victim.credential_id, "action": victim.action,
                          "reason_code": victim.reason_code,
                          "displaced_by": candidate.credential_id})
    truncated = len({i.credential_id for i in ordered_items} - set(selected))
    items = tuple(sorted(selected.values(), key=lambda item: (
        urgency[item.credential_id], item.task_priority, item.credential_id)))
    displaced.sort(key=lambda row: (row["credential_id"], row["displaced_by"]))
    return items, displaced, truncated


def _plan_state(items, credentials, now) -> dict[str, str]:
    """Credential states if only the given items were executed."""
    state = {cid: cred.effective_state(now) for cid, cred in credentials.items()}
    for item in items:
        state[item.credential_id] = item.target_state
    return state

def _count_state(task, role, target, credentials, state) -> int:
    return sum(1 for cid in task.bindings if cid in credentials
               and credentials[cid].source == task.source
               and credentials[cid].role == role and state.get(cid) == target)


def _reclaimable_rotate(selected, credentials, tasks, state, now):
    """Lowest-urgency rotate reclaimable without breaking any role's min_active."""
    rotates = [item for item in selected.values() if item.action == "rotate"]
    if not rotates:
        return None
    worst = max(URGENCY[item.reason_code] for item in rotates)
    for victim in sorted(
        (item for item in rotates if URGENCY[item.reason_code] == worst),
        key=lambda item: (item.task_priority, item.credential_id)):
        reverted = credentials[victim.credential_id].effective_state(now)
        safe = True
        for task in tasks:
            if task.id not in victim.affected_tasks:
                continue
            for role in task.required_roles:
                active = _count_state(task, role, "active", credentials, state)
                if state.get(victim.credential_id) == "active" and reverted != "active":
                    active -= 1
                safe = safe and active >= task.min_active
        if safe:
            return victim
    return None


def _expiry_reason(credential, effective, now, policy) -> tuple[str | None, str]:
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
    return [task for task in tasks if credential.id in task.bindings
            and task.source == credential.source
            and credential.role in task.required_roles]


def _promotion_candidates(task, role, credentials, final_state) -> list[Credential]:
    return sorted((credentials[cid] for cid in task.bindings if cid in credentials
                   and credentials[cid].role == role
                   and final_state.get(cid) == "standby"), key=lambda c: c.id)


def _summary(items, credentials, tasks, backup_deficits,
             active_deficits, truncated, displaced) -> dict[str, object]:
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
        "truncated": truncated, "displaced": displaced,
        "credential_count": len(credentials), "task_count": len(tasks),
    }


def _batch_id(now: datetime, items: tuple[PlanItem, ...]) -> str:
    payload = format_time(now) + "|" + "|".join(
        f"{item.credential_id}:{item.action}:{item.target_state}:{item.reason_code}"
        for item in items)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:10]
    return f"BATCH-{now.strftime('%Y%m%dT%H%M%SZ')}-{digest}"
