import json
from dataclasses import replace
from datetime import datetime, timezone

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
