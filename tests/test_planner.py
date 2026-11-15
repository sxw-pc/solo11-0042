import json
from dataclasses import replace
from datetime import datetime, timezone

from credrot.clock import parse_time
from credrot.db import Database
from credrot.model import Credential, Task
from credrot.planner import build_plan
from credrot.rotator import rotate_batch
from credrot.store import (
    load_credentials,
    load_tasks,
    save_plan,
    seed_registry,
)


def _cred(cid, role="reader", status="standby", source="DS-X",
          expires="2025-06-01T00:00:00Z"):
    return Credential(
        id=cid,
        source=source,
        role=role,
        version=1,
        synthetic_ref=f"SYNTH-{source}-{role}-01",
        fingerprint="sha256:" + "0" * 64,
        issued_at=parse_time("2024-01-01T00:00:00Z"),
        expires_at=parse_time(expires),
        status=status,
    )


def _task(tid, roles=("reader",), min_active=1, margin=0, bindings=(),
          priority=1, source="DS-X"):
    return Task(
        id=tid,
        source=source,
        priority=priority,
        required_roles=tuple(roles),
        min_active=min_active,
        backup_margin=margin,
        bindings=tuple(bindings),
    )


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


def test_shared_credential_promotion_vetoed_to_protect_peer_margin(policy, clock):
    # CRED-SHARED-01 is TASK-MAIN's only promotion candidate, but it is also
    # TASK-PEER's last standby: promoting it would zero the peer's margin.
    creds = {
        "CRED-SHARED-01": _cred("CRED-SHARED-01"),
        "CRED-PEER-ACTIVE-01": _cred("CRED-PEER-ACTIVE-01", status="active"),
    }
    tasks = {
        "TASK-MAIN": _task("TASK-MAIN", min_active=1, margin=0,
                           bindings=("CRED-SHARED-01",), priority=1),
        "TASK-PEER": _task("TASK-PEER", min_active=1, margin=1,
                           bindings=("CRED-PEER-ACTIVE-01", "CRED-SHARED-01"),
                           priority=2),
    }
    plan = build_plan(creds, tasks, policy, clock.now())
    assert plan.items == ()
    assert plan.summary["active_deficits"] == ["TASK-MAIN:reader"]
    assert plan.summary["backup_deficits"] == {}
    degraded = plan.summary["degraded_tasks"]
    assert len(degraded) == 1
    entry = degraded[0]
    assert entry["task_id"] == "TASK-MAIN"
    assert entry["role"] == "reader"
    assert entry["shortfall"] == 1
    assert "peer backup margins" in entry["reason"]
    assert [c["credential_id"] for c in entry["candidates"]] == ["CRED-SHARED-01"]
    assert entry["candidates"][0]["action"] == "promote"
    assert entry["candidates"][0]["blocked_by"] == ["TASK-PEER"]
    assert "TASK-PEER" in entry["candidates"][0]["reason"]


def test_single_task_backfill_behavior_unchanged(policy, clock):
    # A credential bound to exactly one task has no peers, so the joint
    # coverage check never vetoes it: the legacy promotion still fires.
    creds = {"CRED-SOLO-01": _cred("CRED-SOLO-01")}
    tasks = {
        "TASK-MAIN": _task("TASK-MAIN", min_active=1, margin=1,
                           bindings=("CRED-SOLO-01",)),
    }
    plan = build_plan(creds, tasks, policy, clock.now())
    assert len(plan.items) == 1
    item = plan.items[0]
    assert item.credential_id == "CRED-SOLO-01"
    assert item.action == "promote"
    assert item.target_state == "active"
    assert plan.summary["degraded_tasks"] == []
    assert plan.summary["active_deficits"] == []


def test_degraded_candidates_ordered_by_executability_across_batches(policy, clock):
    # TASK-MAIN needs two actives; both candidates are shared and blocked.
    # CRED-ZULU-01 is blocked by one peer, CRED-ALPHA-02 by two, so the
    # backfill order must put ZULU first despite its later credential id.
    creds = {
        "CRED-ZULU-01": _cred("CRED-ZULU-01"),
        "CRED-ALPHA-02": _cred("CRED-ALPHA-02"),
        "CRED-PA-ACTIVE": _cred("CRED-PA-ACTIVE", status="active"),
        "CRED-PB-ACTIVE": _cred("CRED-PB-ACTIVE", status="active"),
        "CRED-PC-ACTIVE": _cred("CRED-PC-ACTIVE", status="active"),
        "CRED-PC-SPARE": _cred("CRED-PC-SPARE"),
    }
    tasks = {
        "TASK-MAIN": _task("TASK-MAIN", min_active=2, margin=0,
                           bindings=("CRED-ZULU-01", "CRED-ALPHA-02"),
                           priority=1),
        "TASK-PEER-A": _task("TASK-PEER-A", min_active=1, margin=1,
                             bindings=("CRED-PA-ACTIVE", "CRED-ZULU-01"),
                             priority=2),
        "TASK-PEER-B": _task("TASK-PEER-B", min_active=1, margin=1,
                             bindings=("CRED-PB-ACTIVE", "CRED-ALPHA-02"),
                             priority=3),
        "TASK-PEER-C": _task("TASK-PEER-C", min_active=1, margin=2,
                             bindings=("CRED-PC-ACTIVE", "CRED-ALPHA-02",
                                       "CRED-PC-SPARE"),
                             priority=4),
    }
    plan = build_plan(creds, tasks, policy, clock.now())
    assert plan.items == ()
    degraded = plan.summary["degraded_tasks"]
    assert len(degraded) == 1
    entry = degraded[0]
    assert entry["task_id"] == "TASK-MAIN"
    assert entry["shortfall"] == 2
    order = [c["credential_id"] for c in entry["candidates"]]
    assert order == ["CRED-ZULU-01", "CRED-ALPHA-02"]
    assert entry["candidates"][0]["blocked_by"] == ["TASK-PEER-A"]
    assert entry["candidates"][1]["blocked_by"] == ["TASK-PEER-B", "TASK-PEER-C"]

    # A later batch: TASK-PEER-A gained a spare, so the first candidate is
    # now promotable while CRED-ALPHA-02 stays blocked for the next round.
    creds2 = {**creds, "CRED-PA-SPARE": _cred("CRED-PA-SPARE")}
    tasks2 = {
        **tasks,
        "TASK-PEER-A": _task("TASK-PEER-A", min_active=1, margin=1,
                             bindings=("CRED-PA-ACTIVE", "CRED-ZULU-01",
                                       "CRED-PA-SPARE"),
                             priority=2),
    }
    plan2 = build_plan(creds2, tasks2, policy, clock.now())
    assert [item.credential_id for item in plan2.items] == ["CRED-ZULU-01"]
    assert plan2.items[0].action == "promote"
    degraded2 = plan2.summary["degraded_tasks"]
    assert len(degraded2) == 1
    assert degraded2[0]["shortfall"] == 1
    assert [c["credential_id"] for c in degraded2[0]["candidates"]] == [
        "CRED-ALPHA-02"
    ]


def test_plan_replay_is_idempotent(tmp_path, policy, clock):
    creds = {
        "CRED-ROT-01": _cred("CRED-ROT-01", expires="2025-01-05T00:00:00Z"),
        "CRED-PRO-01": _cred("CRED-PRO-01"),
    }
    tasks = {
        "TASK-T1": _task("TASK-T1", min_active=1, margin=0,
                         bindings=("CRED-PRO-01", "CRED-ROT-01")),
    }
    db = Database(tmp_path / "credrot.db")
    db.initialize()
    seed_registry(db, creds, tasks, clock.now())

    plan = build_plan(creds, tasks, policy, clock.now())
    assert {item.action for item in plan.items} == {"rotate", "promote"}
    save_plan(db, plan)
    first = rotate_batch(db, plan.batch_id, clock, policy)
    assert first.status == "completed"
    assert first.executed == len(plan.items)

    # Replaying the same batch skips every item instead of re-changing state.
    replay = rotate_batch(db, plan.batch_id, clock, policy)
    assert replay.status == "completed"
    assert replay.executed == 0
    assert replay.skipped == len(plan.items)
    assert all(item["result"] == "skipped" for item in replay.items)

    # Recomputing the plan at the same clock finds every target state
    # already reached, so it schedules no further state changes.
    recomputed = build_plan(
        load_credentials(db), load_tasks(db), policy, clock.now()
    )
    assert recomputed.items == ()
    assert recomputed.summary["degraded_tasks"] == []
    assert recomputed.summary["active_deficits"] == []
