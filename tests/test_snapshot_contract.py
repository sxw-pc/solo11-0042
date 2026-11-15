"""Tests for the planning-time snapshot contract and execution enforcement.

Each plan item snapshots the credential's version/state/validity at planning
time. The execution transaction rejects a mismatch as SUPERSEDED_BY_BATCH
(another batch wrote the credential) or SNAPSHOT_STALE (natural expiry). Codes
derive from the snapshot itself, never from re-running the planner.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from credrot.archive import archive_batch
from credrot.clock import FixedClock, format_time
from credrot.planner import build_plan, snapshot_precondition
from credrot.report import build_report, render_json, render_markdown
from credrot.rotator import rotate_batch
from credrot.snapshot import (
    SNAPSHOT_STALE,
    SUPERSEDED_BY_BATCH,
    SnapshotConflict,
    classify_snapshot_conflict,
    ensure_precondition,
)
from credrot.store import (
    get_batch,
    list_rotation_items,
    load_credentials,
    load_tasks,
    save_plan,
)

PLAN_NOW = "2025-01-01T00:00:00Z"
BETA, GAMMA = "CRED-BETA-READER-01", "CRED-GAMMA-SPARE-01"


def _plan(db, registry, policy, at):
    plan = build_plan(registry.credentials, registry.tasks, policy,
                      FixedClock(at).now())
    save_plan(db, plan)
    return plan


def _plan_from_db(db, policy, at):
    plan = build_plan(load_credentials(db), load_tasks(db), policy,
                      FixedClock(at).now())
    save_plan(db, plan)
    return plan


def _set_credential(db, credential_id, **changes):
    with db.transaction() as conn:
        row = dict(conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (credential_id,)).fetchone())
        row.update(changes)
        conn.execute(
            "UPDATE credentials SET version = ?, status = ?, expires_at = ?, "
            "issued_at = ? WHERE id = ?",
            (row["version"], row["status"], row["expires_at"],
             row["issued_at"], credential_id))


def _precondition(registry, credential_id, at=PLAN_NOW):
    return snapshot_precondition(
        registry.credentials[credential_id], FixedClock(at).now()).to_dict()


def _identity_snapshot(db):
    return {cid: (c.version, c.status) for cid, c in load_credentials(db).items()}


# --- planning-time contract
def test_plan_items_carry_precondition_snapshot(registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    item = {i.credential_id: i for i in plan.items}[BETA]
    assert item.precondition == snapshot_precondition(
        registry.credentials[BETA], clock.now())
    raw = json.loads(json.dumps(plan.to_dict(), sort_keys=True))
    assert raw["items"][0]["precondition"] == item.precondition.to_dict()


def test_snapshot_precondition_matches_credential_identity(registry, clock):
    beta = registry.credentials[BETA]
    assert snapshot_precondition(beta, clock.now()).to_dict() == {
        "version": beta.version, "status": beta.status,
        "effective_state": beta.effective_state(clock.now()),
        "expires_at": format_time(beta.expires_at)}


def test_batch_id_changes_when_snapshot_identity_changes(registry, policy, clock):
    first = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    advanced = dict(registry.credentials)
    beta = registry.credentials[BETA]
    advanced[BETA] = beta.changed(version=beta.version + 1)
    second = build_plan(advanced, registry.tasks, policy, clock.now())
    assert first.batch_id != second.batch_id  # same clock, different version


# --- pure classifier: reasons derive from the snapshot alone


def test_classifier_accepts_matching_snapshot(registry):
    assert classify_snapshot_conflict(
        _precondition(registry, BETA), registry.credentials[BETA],
        FixedClock(PLAN_NOW).now()) is None


@pytest.mark.parametrize("changes", [{"version": 99}, {"status": "standby"}])
def test_classifier_flags_writes_as_superseded(registry, changes):
    current = registry.credentials[BETA].changed(**changes)
    assert classify_snapshot_conflict(
        _precondition(registry, BETA), current, FixedClock(PLAN_NOW).now()
    ) == SUPERSEDED_BY_BATCH


def test_classifier_flags_expires_at_change_as_superseded(registry):
    renewed = registry.credentials[BETA].changed(
        expires_at=registry.credentials[BETA].expires_at + timedelta(days=30))
    assert classify_snapshot_conflict(
        _precondition(registry, BETA), renewed, FixedClock(PLAN_NOW).now()
    ) == SUPERSEDED_BY_BATCH


def test_classifier_flags_clock_expiry_as_stale(registry):
    # Same row (version/status/expires_at untouched); only the clock moved.
    assert classify_snapshot_conflict(
        _precondition(registry, BETA), registry.credentials[BETA],
        FixedClock("2025-01-03T00:00:00Z").now()) == SNAPSHOT_STALE


def test_classifier_without_snapshot_does_not_enforce(registry):
    assert classify_snapshot_conflict(
        None, registry.credentials[BETA],
        FixedClock("2025-01-03T00:00:00Z").now()) is None


def test_effective_state_change_without_expiry_is_superseded(registry):
    # GAMMA is valid until 2025-03-01; a promotion changes stored state with
    # no expiry explanation, so clock drift cannot be the cause.
    promoted = registry.credentials[GAMMA].changed(status="active")
    assert classify_snapshot_conflict(
        _precondition(registry, GAMMA), promoted,
        FixedClock("2025-02-01T00:00:00Z").now()) == SUPERSEDED_BY_BATCH


# --- transactional enforcement


def test_superseded_batch_rolls_back_and_keeps_ledger_untouched(
        db, registry, policy):
    plan_a = _plan(db, registry, policy, PLAN_NOW)
    before = _identity_snapshot(db)
    plan_b = _plan_from_db(db, policy, "2025-01-01T00:01:00Z")
    assert rotate_batch(db, plan_b.batch_id,
                        FixedClock("2025-01-01T00:01:00Z"),
                        policy).status == "completed"

    result = rotate_batch(db, plan_a.batch_id,
                          FixedClock("2025-01-01T00:02:00Z"), policy)
    assert (result.status, result.failure_code) == ("failed", SUPERSEDED_BY_BATCH)
    assert result.failed == 1 and result.executed == 0
    assert SUPERSEDED_BY_BATCH in (result.error or "")
    assert result.items[0]["failure_code"] == SUPERSEDED_BY_BATCH

    # A's failed transaction changed nothing; B's decisions survive.
    assert _identity_snapshot(db) != before
    assert load_credentials(db)[BETA].version == \
        registry.credentials[BETA].version + 1
    batch = get_batch(db, plan_a.batch_id)
    assert (batch["status"], batch["failure_code"]) == (
        "failed", SUPERSEDED_BY_BATCH)
    items = list_rotation_items(db, plan_a.batch_id)
    assert len(items) == 1 and items[0]["ok"] == 0
    assert items[0]["failure_code"] == SUPERSEDED_BY_BATCH


def test_superseded_failure_is_idempotent_on_retry(db, registry, policy):
    plan_a = _plan(db, registry, policy, PLAN_NOW)
    plan_b = _plan_from_db(db, policy, "2025-01-01T00:01:00Z")
    rotate_batch(db, plan_b.batch_id, FixedClock("2025-01-01T00:01:00Z"), policy)
    first = rotate_batch(db, plan_a.batch_id,
                         FixedClock("2025-01-01T00:02:00Z"), policy)
    second = rotate_batch(db, plan_a.batch_id,
                          FixedClock("2025-01-01T00:03:00Z"), policy)
    assert first.status == second.status == "failed"
    assert first.failure_code == second.failure_code == SUPERSEDED_BY_BATCH
    assert len(list_rotation_items(db, plan_a.batch_id)) == 1
    assert load_credentials(db)[BETA].version == \
        registry.credentials[BETA].version + 1


def test_status_only_change_is_superseded_without_version_bump(
        db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    _set_credential(db, BETA, status="standby")  # demotion, no renewal
    result = rotate_batch(db, plan.batch_id, FixedClock(PLAN_NOW), policy)
    assert (result.status, result.failure_code) == ("failed", SUPERSEDED_BY_BATCH)
    assert load_credentials(db)[BETA].status == "standby"  # rollback preserved


def test_natural_expiry_after_planning_is_snapshot_stale(db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    before = load_credentials(db)[BETA]
    result = rotate_batch(db, plan.batch_id,
                          FixedClock("2025-01-03T00:00:00Z"), policy)
    assert (result.status, result.failure_code) == ("failed", SNAPSHOT_STALE)
    assert SNAPSHOT_STALE in (result.error or "")
    after = load_credentials(db)[BETA]
    assert (after.version, after.status) == (before.version, before.status)
    assert get_batch(db, plan.batch_id)["failure_code"] == SNAPSHOT_STALE
    items = list_rotation_items(db, plan.batch_id)
    assert items[0]["failure_code"] == SNAPSHOT_STALE and items[0]["ok"] == 0


def test_snapshot_stale_failure_is_idempotent_and_retryable(
        db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    clock = FixedClock("2025-01-03T00:00:00Z")
    first = rotate_batch(db, plan.batch_id, clock, policy)
    second = rotate_batch(db, plan.batch_id, clock, policy)
    assert first.failure_code == second.failure_code == SNAPSHOT_STALE
    assert len(list_rotation_items(db, plan.batch_id)) == 1
    # A fresh, current plan can still execute against the real state.
    fresh = _plan_from_db(db, policy, "2025-01-03T00:01:00Z")
    assert rotate_batch(db, fresh.batch_id,
                        FixedClock("2025-01-03T00:01:00Z"),
                        policy).status == "completed"


def test_promote_item_is_guarded_by_snapshot(db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    _set_credential(db, GAMMA, status="active")  # another batch promoted it
    result = rotate_batch(db, plan.batch_id, FixedClock(PLAN_NOW), policy)
    assert (result.status, result.failure_code) == ("failed", SUPERSEDED_BY_BATCH)
    # Whole batch rolled back: no renewal happened either.
    assert load_credentials(db)[BETA].version == registry.credentials[BETA].version


def test_conflict_raises_structured_exception(db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    _set_credential(db, BETA, version=42)
    item = get_batch(db, plan.batch_id)["plan"]["items"][0]
    credential = load_credentials(db)[item["credential_id"]]
    with pytest.raises(SnapshotConflict) as excinfo:
        ensure_precondition(item, credential, FixedClock(PLAN_NOW).now())
    assert excinfo.value.code == SUPERSEDED_BY_BATCH
    assert excinfo.value.credential_id == item["credential_id"]


def test_legacy_plan_without_precondition_still_executes(db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT plan_json FROM rotation_batches WHERE batch_id = ?",
            (plan.batch_id,)).fetchone()
        payload = json.loads(row["plan_json"])
        for item in payload["items"]:
            item.pop("precondition", None)
        conn.execute(
            "UPDATE rotation_batches SET plan_json = ? WHERE batch_id = ?",
            (json.dumps(payload, sort_keys=True), plan.batch_id))
    result = rotate_batch(db, plan.batch_id, FixedClock(PLAN_NOW), policy)
    assert result.status == "completed" and result.failure_code is None


# --- report and archive surface the reason code deterministically


def test_report_and_archive_expose_snapshot_failure_codes(db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    rotate_batch(db, plan.batch_id, FixedClock("2025-01-03T00:00:00Z"), policy)
    clock = FixedClock("2025-01-03T00:00:00Z")
    report = build_report(db, clock, policy, plan.batch_id)
    assert report["batch"]["failure_code"] == SNAPSHOT_STALE
    assert report["batch"]["items"][0]["failure_code"] == SNAPSHOT_STALE
    codes = {f.get("code") for f in report["failures"]
             if f["subject"] == plan.batch_id}
    assert SNAPSHOT_STALE in codes

    snapshot = archive_batch(db, clock, plan.batch_id)
    assert snapshot["batch"]["failure_code"] == SNAPSHOT_STALE
    assert snapshot["rotation_items"][0]["failure_code"] == SNAPSHOT_STALE
    assert any(
        event["action"] == "rotation.failed"
        and (event.get("after") or {}).get("failure_code") == SNAPSHOT_STALE
        for event in snapshot["audit_events"])


def test_failed_batch_report_is_byte_deterministic(db, registry, policy):
    plan = _plan(db, registry, policy, PLAN_NOW)
    rotate_batch(db, plan.batch_id, FixedClock("2025-01-03T00:00:00Z"), policy)
    clock = FixedClock("2025-01-03T00:00:00Z")
    first = build_report(db, clock, policy, plan.batch_id)
    second = build_report(db, clock, policy, plan.batch_id)
    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)
    assert SNAPSHOT_STALE in render_markdown(first)
