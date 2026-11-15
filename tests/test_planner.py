import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from credrot.clock import parse_time
from credrot.model import (
    ITEM_ORIGIN_COMPENSATED,
    ITEM_ORIGIN_SCHEDULED,
    Credential,
    Task,
)
from credrot.planner import build_plan


def test_plan_is_byte_deterministic(registry, policy, clock):
    first = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    second = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    assert first.to_dict() == second.to_dict()
    assert first.batch_id == second.batch_id
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )


def test_plan_orders_by_urgency_then_priority(registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    codes = [item.reason_code for item in plan.items]
    assert codes == ["CRITICAL_EXPIRY", "EXPIRING_SOON", "REDUNDANCY_LOW"]
    assert plan.items[0].credential_id == "CRED-BETA-READER-01"
    assert plan.items[1].credential_id == "CRED-ALPHA-READER-01"


def test_plan_targets_preserve_primary_or_promote_backup(registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    by_id = {item.credential_id: item for item in plan.items}
    assert by_id["CRED-BETA-READER-01"].target_state == "active"
    assert by_id["CRED-ALPHA-READER-01"].action == "rotate"
    assert by_id["CRED-GAMMA-SPARE-01"].action == "promote"
    assert by_id["CRED-GAMMA-SPARE-01"].affected_tasks == ("TASK-GAMMA-AUDIT",)
    assert plan.summary["total_items"] == 3


def test_expired_credentials_are_recovered(registry, policy):
    later = datetime(2025, 1, 20, tzinfo=timezone.utc)
    plan = build_plan(registry.credentials, registry.tasks, policy, later)
    expired = [item for item in plan.items if item.reason_code == "EXPIRED"]
    assert {item.credential_id for item in expired} == {
        "CRED-ALPHA-READER-01",
        "CRED-BETA-READER-01",
    }


def test_empty_plan_is_valid(registry, policy, clock):
    plan = build_plan({}, {}, policy, clock.now())
    assert plan.items == ()
    assert plan.summary["total_items"] == 0
    assert plan.batch_id.startswith("BATCH-20250101T000000Z-")


def test_plan_cap_and_backup_deficit(registry, policy, clock):
    tasks = dict(registry.tasks)
    tasks["TASK-ALPHA-EXPORT"] = replace(tasks["TASK-ALPHA-EXPORT"], backup_margin=3)
    capped = replace(policy, max_items_per_plan=1)
    plan = build_plan(registry.credentials, tasks, capped, clock.now())
    assert len(plan.items) == 1
    assert plan.summary["truncated"] == 2
    assert plan.summary["backup_deficits"]["TASK-ALPHA-EXPORT"] == 3


def test_plan_rejects_revoked_and_uses_task_roles(registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    ids = {item.credential_id for item in plan.items}
    assert "CRED-GAMMA-OLD-01" not in ids
    assert all(item.role in {"reader", "writer", "admin"} for item in plan.items)


def _cred(cid, source, role, status, now, days):
    return Credential(
        id=cid, source=source, role=role, version=1,
        synthetic_ref=f"SYNTH-{source}-{role}-{cid[-2:]}",
        fingerprint="sha256:" + "0" * 64,
        issued_at=now - timedelta(days=30), expires_at=now + timedelta(days=days),
        status=status,
    )


def _redistribution_fixture(policy, now):
    """4 tasks / 5 standby bindings; five candidate items before the cap of 3."""
    spec = {
        "CRED-A-ACTIVE-01": ("DS-A", "active", 2), "CRED-A-STANDBY-01": ("DS-A", "standby", 200),
        "CRED-B-ACTIVE-01": ("DS-B", "active", 2), "CRED-B-ACTIVE-02": ("DS-B", "active", 10),
        "CRED-B-STANDBY-01": ("DS-B", "standby", 10), "CRED-B-STANDBY-02": ("DS-B", "standby", 200),
        "CRED-C-ACTIVE-01": ("DS-C", "active", 200), "CRED-C-STANDBY-01": ("DS-C", "standby", 200),
        "CRED-D-REVOKED-01": ("DS-D", "revoked", 200), "CRED-D-STANDBY-01": ("DS-D", "standby", 200),
    }
    credentials = {cid: _cred(cid, s, "reader", st, now, d) for cid, (s, st, d) in spec.items()}
    tasks = {
        "TASK-A": Task("TASK-A", "DS-A", 1, ("reader",), 1, 1,
                       ("CRED-A-ACTIVE-01", "CRED-A-STANDBY-01")),
        "TASK-B": Task("TASK-B", "DS-B", 1, ("reader",), 1, 1,
                       ("CRED-B-ACTIVE-01", "CRED-B-ACTIVE-02",
                        "CRED-B-STANDBY-01", "CRED-B-STANDBY-02")),
        "TASK-C": Task("TASK-C", "DS-C", 1, ("reader",), 1, 1,
                       ("CRED-C-ACTIVE-01", "CRED-C-STANDBY-01")),
        "TASK-D": Task("TASK-D", "DS-D", 1, ("reader",), 1, 1,
                       ("CRED-D-REVOKED-01", "CRED-D-STANDBY-01")),
    }
    return credentials, tasks, replace(policy, max_items_per_plan=3)


def test_cap_redistribution_reclaims_low_urgency_rotate_for_promote(policy):
    now = parse_time("2025-01-01T00:00:00Z")
    credentials, tasks, capped = _redistribution_fixture(policy, now)
    plan = build_plan(credentials, tasks, capped, now)
    by_id = {item.credential_id: item for item in plan.items}

    assert len(plan.items) <= capped.max_items_per_plan
    assert plan.summary["total_items"] == 3
    assert plan.summary["active_deficits"] == []
    assert {"CRED-A-ACTIVE-01", "CRED-B-ACTIVE-01"} <= set(by_id)
    assert "CRED-B-ACTIVE-02" not in by_id
    promoted = by_id["CRED-D-STANDBY-01"]
    assert (promoted.action, promoted.target_state) == ("promote", "active")
    assert promoted.origin == ITEM_ORIGIN_COMPENSATED
    assert promoted.slot_source == "CRED-B-ACTIVE-02"
    assert all(
        item.origin == ITEM_ORIGIN_SCHEDULED and item.slot_source is None
        for item in plan.items if item.credential_id != "CRED-D-STANDBY-01"
    )
    assert plan.summary["displaced"] == [{
        "credential_id": "CRED-B-ACTIVE-02", "action": "rotate",
        "reason_code": "EXPIRING_SOON", "displaced_by": "CRED-D-STANDBY-01",
    }]
    # truncated counts only original items never selected; no double counting.
    original = {
        "CRED-A-ACTIVE-01", "CRED-B-ACTIVE-01", "CRED-B-ACTIVE-02",
        "CRED-B-STANDBY-01", "CRED-D-STANDBY-01",
    }
    selected = {item.credential_id for item in plan.items}
    assert plan.summary["truncated"] == len(original - selected) == 2
    assert plan.summary["backup_deficits"] == {"TASK-D": 1}


def test_cap_redistribution_is_byte_deterministic(policy):
    now = parse_time("2025-01-01T00:00:00Z")
    credentials, tasks, capped = _redistribution_fixture(policy, now)
    first = build_plan(credentials, tasks, capped, now)
    second = build_plan(credentials, tasks, capped, now)
    assert json.dumps(first.to_dict(), sort_keys=True) == json.dumps(
        second.to_dict(), sort_keys=True
    )
    assert first.batch_id == second.batch_id


def test_one_compensated_promote_is_not_reused_to_claim_a_second_slot(policy):
    now = parse_time("2025-01-01T00:00:00Z")
    credentials = {
        cid: _cred(cid, source, "reader", status, now, days)
        for cid, (source, status, days) in {
            "CRED-A1": ("DS-A", "active", 2),
            "CRED-A2": ("DS-A", "active", 10),
            "CRED-A3": ("DS-A", "active", 11),
            "CRED-D1": ("DS-D", "standby", 200),
        }.items()
    }
    tasks = {
        "TASK-A": Task("TASK-A", "DS-A", 1, ("reader",), 1, 0,
                       ("CRED-A1", "CRED-A2", "CRED-A3")),
        "TASK-D": Task("TASK-D", "DS-D", 1, ("reader",), 1, 0, ("CRED-D1",)),
        # TASK-E can only ever use the same credential as TASK-D.
        "TASK-E": Task("TASK-E", "DS-E", 1, ("reader",), 1, 0, ("CRED-D1",)),
    }
    plan = build_plan(credentials, tasks, replace(policy, max_items_per_plan=3), now)
    assert len(plan.items) == 3
    assert len(plan.summary["displaced"]) == 1
    assert plan.summary["displaced"][0]["displaced_by"] == "CRED-D1"
    promoted = next(item for item in plan.items if item.credential_id == "CRED-D1")
    assert promoted.origin == ITEM_ORIGIN_COMPENSATED
    assert plan.summary["active_deficits"] == ["TASK-E:reader"]
    assert plan.summary["truncated"] == 1


def test_cap_reports_real_deficit_when_no_safe_rotate_can_yield(policy):
    now = parse_time("2025-01-01T00:00:00Z")
    credentials, tasks, _ = _redistribution_fixture(policy, now)
    # TASK-A's sole active credential is expired: cutting its rotate breaks
    # min_active, so the slot cannot be reclaimed for the TASK-D promotion.
    credentials["CRED-A-ACTIVE-01"] = credentials["CRED-A-ACTIVE-01"].changed(
        expires_at=now - timedelta(days=1)
    )
    plan = build_plan(credentials, tasks, replace(policy, max_items_per_plan=1), now)
    assert len(plan.items) == 1
    assert plan.summary["active_deficits"] == ["TASK-D:reader"]
    assert plan.summary["displaced"] == []
    assert plan.summary["truncated"] == 4
    assert all(item.origin == ITEM_ORIGIN_SCHEDULED for item in plan.items)
