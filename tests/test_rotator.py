from datetime import timedelta

import pytest

from credrot.audit import AuditLog
from credrot.planner import build_plan
from credrot.rotator import UnknownBatch, rotate_batch
from credrot.store import (
    get_batch,
    list_rotation_items,
    load_credentials,
    save_plan,
)


def _prepare(db, registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    return plan


def test_rotate_executes_batch_and_bumps_versions(db, registry, policy, clock):
    plan = _prepare(db, registry, policy, clock)
    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "completed"
    assert result.executed == 3 and result.failed == 0
    credentials = load_credentials(db)
    assert credentials["CRED-ALPHA-READER-01"].version == 4
    assert credentials["CRED-ALPHA-READER-01"].status == "active"
    assert credentials["CRED-ALPHA-READER-01"].expires_at == (
        clock.now() + timedelta(days=policy.rotation_period_days)
    )
    assert credentials["CRED-GAMMA-SPARE-01"].status == "active"
    items = list_rotation_items(db, plan.batch_id)
    assert len(items) == 3 and all(row["ok"] == 1 for row in items)
    assert get_batch(db, plan.batch_id)["status"] == "completed"
    events = AuditLog(db, clock).list_events(batch_id=plan.batch_id)
    assert any(event["action"] == "rotation.completed" for event in events)


def test_rotate_is_idempotent(db, registry, policy, clock):
    plan = _prepare(db, registry, policy, clock)
    rotate_batch(db, plan.batch_id, clock, policy)
    again = rotate_batch(db, plan.batch_id, clock, policy)
    assert again.status == "completed"
    assert again.executed == 0
    assert again.skipped == len(plan.items)
    assert all(item["result"] == "skipped" and item["ok"] for item in again.items)


def test_unknown_batch_is_rejected(db, policy, clock):
    with pytest.raises(UnknownBatch):
        rotate_batch(db, "BATCH-MISSING", clock, policy)


def test_failure_rolls_back_all_credentials_and_keeps_reason(
    db, registry, policy, clock
):
    plan = _prepare(db, registry, policy, clock)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'revoked' WHERE id = ?",
            ("CRED-ALPHA-READER-01",),
        )
    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "failed"
    assert result.failed == 1
    assert result.error and "immutable" in result.error
    credentials = load_credentials(db)
    assert credentials["CRED-BETA-READER-01"].version == 5
    batch = get_batch(db, plan.batch_id)
    assert batch["status"] == "failed"
    assert "immutable" in batch["failure_reason"]
    items = list_rotation_items(db, plan.batch_id)
    assert items and items[0]["result"] == "failed" and items[0]["ok"] == 0

    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'active' WHERE id = ?",
            ("CRED-ALPHA-READER-01",),
        )
    retry = rotate_batch(db, plan.batch_id, clock, policy)
    assert retry.status == "completed" and retry.executed == 3


def test_empty_plan_completes_without_items(db, policy, clock):
    plan = build_plan({}, {}, policy, clock.now())
    save_plan(db, plan)
    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "completed"
    assert result.executed == 0 and result.skipped == 0
    assert get_batch(db, plan.batch_id)["status"] == "completed"
