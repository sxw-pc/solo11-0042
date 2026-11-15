"""Temporal contract between committed rotations and valid verifications."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from credrot.archive import archive_batch
from credrot.clock import FixedClock
from credrot.gating import GateDecision, RotationEvidence, gates_for, load_gates
from credrot.model import Credential, Task, Verification
from credrot.planner import build_plan
from credrot.plan_export import plan_to_markdown
from credrot.report import build_report, render_json, render_markdown
from credrot.rotator import rotate_batch
from credrot.store import (
    get_batch,
    list_rotation_items,
    load_credentials,
    save_plan,
)
from credrot.verification_store import latest_valid_verifications
from credrot.verifier import verify_all


LATER = datetime(2025, 1, 1, 0, 10, tzinfo=timezone.utc)
MUCH_LATER = datetime(2025, 1, 2, tzinfo=timezone.utc)
GATE = "POST_ROTATION_UNVERIFIED"
# The demo fixtures declare this credential's fingerprint as invalid at the stub.
INVALID = "CRED-GAMMA-SPARE-01"


class FakeClient:
    """Deterministic stand-in for VerificationClient; no socket involved."""

    def __init__(self, outcomes: dict[str, str] | None = None):
        self.outcomes = outcomes or {}

    def check(self, credential, checked_at, batch_id=None):
        outcome = self.outcomes.get(
            credential.id, "invalid" if credential.id == INVALID else "valid"
        )
        return Verification(
            credential_id=credential.id,
            outcome=outcome,
            code={"valid": "VALID", "invalid": "INVALID"}.get(outcome, "UNKNOWN"),
            checked_at=checked_at,
            batch_id=batch_id,
            latency_ms=0,
        )


def _plan(db, registry, policy, now):
    if not isinstance(now, datetime):
        now = now.now()
    credentials = load_credentials(db)
    return build_plan(
        credentials,
        registry.tasks,
        policy,
        now,
        gates=load_gates(db, credentials, now),
    )


def _rotate_and_verify(
    db, registry, policy, clock, outcomes: dict[str, str] | None = None,
    verify_at: datetime | None = None,
):
    plan = _plan(db, registry, policy, clock.now())
    save_plan(db, plan)
    rotation = rotate_batch(db, plan.batch_id, clock, policy)
    assert rotation.status == "completed"
    verification = verify_all(
        db,
        FixedClock(verify_at or clock.now()),
        policy,
        batch_id=plan.batch_id,
        client=FakeClient(outcomes),
    )
    return plan, verification


def _by_id(plan):
    return {item.credential_id: item for item in plan.items}


def test_never_rotated_credentials_are_not_gated(db, registry, policy, clock):
    plan = _plan(db, registry, policy, clock)
    assert plan.summary["post_rotation_gated"] == []
    assert GATE not in plan.summary["counts_by_reason"]
    assert [item.reason_code for item in plan.items] == [
        "CRITICAL_EXPIRY", "EXPIRING_SOON", "REDUNDANCY_LOW",
    ]


def test_rotation_then_same_instant_verification_is_gated(
    db, registry, policy, clock
):
    """A check stamped at the rotation instant is not strictly later."""
    rotated, verification = _rotate_and_verify(db, registry, policy, clock)
    assert verification["counts"]["valid"] == 8

    plan = _plan(db, registry, policy, LATER)
    items = _by_id(plan)
    assert items["CRED-ALPHA-READER-01"].action == "hold"
    assert items["CRED-BETA-READER-01"].action == "hold"
    assert items["CRED-ALPHA-READER-01"].reason_code == GATE
    assert all(
        item.action not in {"rotate", "promote"} for item in plan.items
    )
    assert plan.summary["post_rotation_gated"] == [
        "CRED-ALPHA-READER-01", "CRED-BETA-READER-01",
    ]
    evidence = plan.summary["gate_evidence"]["CRED-ALPHA-READER-01"]
    assert evidence == {
        "rotation_batch_id": rotated.batch_id,
        "rotated_at": "2025-01-01T00:00:00Z",
        "last_valid_verification_at": "2025-01-01T00:00:00Z",
    }


def test_same_clock_repeat_is_byte_identical(db, registry, policy, clock):
    _rotate_and_verify(db, registry, policy, clock)
    first = _plan(db, registry, policy, LATER)
    second = _plan(db, registry, policy, LATER)
    assert first.batch_id == second.batch_id
    assert json.dumps(
        first.to_dict(), sort_keys=True, separators=(",", ":")
    ) == json.dumps(second.to_dict(), sort_keys=True, separators=(",", ":"))
    assert save_plan(db, first) is True
    assert save_plan(db, second) is False  # idempotent insert, same content


def test_later_valid_verification_clears_the_gate(db, registry, policy, clock):
    _rotate_and_verify(db, registry, policy, clock)
    verify_all(
        db,
        FixedClock(datetime(2025, 1, 1, 1, tzinfo=timezone.utc)),
        policy,
        client=FakeClient(),
    )
    plan = _plan(db, registry, policy, MUCH_LATER)
    assert plan.summary["post_rotation_gated"] == []
    assert all(item.reason_code != GATE for item in plan.items)


def test_invalid_verification_does_not_clear_the_gate(db, registry, policy, clock):
    _rotate_and_verify(
        db, registry, policy, clock,
        outcomes={
            "CRED-ALPHA-READER-01": "invalid",
            "CRED-BETA-READER-01": "invalid",
        },
        verify_at=datetime(2025, 1, 1, 1, tzinfo=timezone.utc),
    )
    plan = _plan(db, registry, policy, MUCH_LATER)
    assert "CRED-ALPHA-READER-01" in plan.summary["post_rotation_gated"]
    assert "CRED-BETA-READER-01" in plan.summary["post_rotation_gated"]


def test_failed_batch_rows_are_not_rotation_evidence(
    db, registry, policy, clock
):
    plan = _plan(db, registry, policy, clock)
    save_plan(db, plan)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'revoked' WHERE id = ?",
            ("CRED-ALPHA-READER-01",),
        )
    failed = rotate_batch(db, plan.batch_id, clock, policy)
    assert failed.status == "failed"
    assert any(row["result"] == "failed" for row in list_rotation_items(
        db, plan.batch_id
    ))
    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'active' WHERE id = ?",
            ("CRED-ALPHA-READER-01",),
        )

    retry_plan = _plan(db, registry, policy, clock)
    assert retry_plan.summary["post_rotation_gated"] == []
    assert retry_plan.batch_id == plan.batch_id


def test_revoked_credential_is_exempt_from_the_gate(db, registry, policy, clock):
    _rotate_and_verify(db, registry, policy, clock)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'revoked' WHERE id = ?",
            ("CRED-ALPHA-READER-01",),
        )
    plan = _plan(db, registry, policy, LATER)
    assert "CRED-ALPHA-READER-01" not in plan.summary["post_rotation_gated"]
    assert "CRED-BETA-READER-01" in plan.summary["post_rotation_gated"]


def test_verification_from_uncommitted_batch_is_not_evidence(
    db, registry, policy, clock
):
    _rotate_and_verify(db, registry, policy, clock)
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO rotation_batches
            (batch_id, generated_at, executed_at, status, plan_json, summary_json)
            VALUES ('BATCH-GHOST', '2025-01-01T00:00:00Z',
                    '2025-01-01T00:00:00Z', 'failed', '{}', '{}')"""
        )
        conn.execute(
            """INSERT INTO verifications
            (batch_id, credential_id, outcome, code, latency_ms, checked_at)
            VALUES ('BATCH-GHOST', 'CRED-ALPHA-READER-01', 'valid', 'VALID', 0,
                    '2025-01-02T00:00:00Z')"""
        )
    plan = _plan(db, registry, policy, MUCH_LATER)
    assert "CRED-ALPHA-READER-01" in plan.summary["post_rotation_gated"]


def test_latest_valid_verification_orders_by_time_not_row_id(
    db, registry, policy, clock
):
    """MAX(id) would report the earlier timestamp; ordering must not."""
    with db.transaction() as conn:
        conn.execute(
            """INSERT INTO verifications
            (credential_id, outcome, code, latency_ms, checked_at)
            VALUES ('CRED-ALPHA-READER-01', 'valid', 'VALID', 0,
                    '2025-01-05T00:00:00Z')"""
        )
        conn.execute(
            """INSERT INTO verifications
            (credential_id, outcome, code, latency_ms, checked_at)
            VALUES ('CRED-ALPHA-READER-01', 'invalid', 'INVALID', 0,
                    '2025-01-01T00:00:00Z')"""
        )
    latest = latest_valid_verifications(db)
    assert latest["CRED-ALPHA-READER-01"]["checked_at"] == (
        "2025-01-05T00:00:00Z"
    )


def test_gate_blocks_promotion_and_reports_deficit(policy, clock):
    """Pure planner check: a gated standby is never promoted."""
    standby = Credential(
        id="CRED-X-SPARE-01", source="DS-X", role="reader", version=1,
        synthetic_ref="SYNTH-DS-X-reader-01", fingerprint="sha256:" + "0" * 64,
        issued_at=clock.now(), expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="standby",
    )
    active = standby.changed(
        id="CRED-X-ACTIVE-01", synthetic_ref="SYNTH-DS-X-reader-02",
        status="active", expires_at=datetime(2025, 1, 5, tzinfo=timezone.utc),
    )
    task = Task(
        id="TASK-X", source="DS-X", priority=1, required_roles=("reader",),
        min_active=1, backup_margin=0, bindings=(active.id, standby.id),
    )
    # No rotation evidence at all -> no gate.
    assert gates_for({standby.id: standby, active.id: active}, {}, {}, clock.now()) == {}

    gates = {
        standby.id: GateDecision(
            credential_id=standby.id,
            rotation_batch_id="BATCH-OLD",
            rotated_at=standby.issued_at,
            last_valid_verification_at=None,
        )
    }
    plan = build_plan(
        {standby.id: standby, active.id: active},
        {task.id: task},
        policy,
        clock.now(),
        gates=gates,
    )
    items = _by_id(plan)
    assert items[standby.id].action == "hold"
    assert items[standby.id].reason_code == GATE
    assert standby.id not in {
        item.credential_id for item in plan.items if item.action == "promote"
    }


def test_gate_evidence_requires_a_strictly_later_verification(clock):
    from datetime import timedelta

    credential = Credential(
        id="CRED-X-1", source="DS-X", role="reader", version=1,
        synthetic_ref="SYNTH-DS-X-reader-01", fingerprint="sha256:" + "0" * 64,
        issued_at=clock.now(), expires_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        status="active",
    )
    rotated_at = clock.now()
    rotations = {
        credential.id: RotationEvidence(credential.id, "BATCH-A", rotated_at)
    }
    from credrot.gating import VerificationEvidence

    for offset, expected_gate in ((timedelta(0), True), (timedelta(seconds=1), False)):
        verifications = {
            credential.id: VerificationEvidence(
                credential.id, rotated_at + offset, "BATCH-B"
            )
        }
        decisions = gates_for(
            {credential.id: credential}, rotations, verifications, clock.now()
        )
        assert (credential.id in decisions) is expected_gate


def test_gate_entries_are_visible_in_plan_outputs(db, registry, policy, clock):
    _rotate_and_verify(db, registry, policy, clock)
    plan = _plan(db, registry, policy, LATER)
    save_plan(db, plan)

    payload = plan.to_dict()
    assert payload["summary"]["counts_by_reason"][GATE] == 2
    assert {
        item["reason_code"] for item in payload["items"]
    } == {GATE}
    markdown = plan_to_markdown(plan)
    assert GATE in markdown and "hold" in markdown
    stored = get_batch(db, plan.batch_id)
    assert stored["plan"]["summary"]["counts_by_reason"][GATE] == 2


def test_gate_reason_is_visible_in_report_and_archive(db, registry, policy, clock):
    _rotate_and_verify(db, registry, policy, clock)
    plan = _plan(db, registry, policy, LATER)
    save_plan(db, plan)
    executed = rotate_batch(db, plan.batch_id, FixedClock(LATER), policy)
    assert executed.status == "completed" and executed.executed == 0
    assert executed.skipped == 2

    report_clock = FixedClock(LATER)
    report = build_report(db, report_clock, policy, plan.batch_id)
    codes = {item["reason_code"] for item in report["batch"]["items"]}
    assert GATE in codes
    assert GATE in render_markdown(report)
    assert GATE in render_json(report)
    assert render_json(build_report(db, report_clock, policy, plan.batch_id)) == (
        render_json(report)
    )

    snapshot = archive_batch(db, report_clock, plan.batch_id)
    assert {
        row["reason_code"] for row in snapshot["rotation_items"]
    } == {GATE}


def test_executing_a_stale_actionable_plan_is_refused(
    db, registry, policy, clock
):
    stale = _plan(db, registry, policy, datetime(2024, 12, 31, tzinfo=timezone.utc))
    save_plan(db, stale)
    fresh = _plan(db, registry, policy, clock)
    save_plan(db, fresh)
    assert stale.batch_id != fresh.batch_id
    assert rotate_batch(db, fresh.batch_id, clock, policy).status == "completed"

    refused = rotate_batch(db, stale.batch_id, clock, policy)
    assert refused.status == "failed"
    assert "gate" in (refused.error or "")
    assert load_credentials(db)["CRED-ALPHA-READER-01"].version == 4


def test_gate_only_withholds_actions(db, registry, policy, clock):
    """Planning is not blocked, and a gated plan executes as a no-op."""
    _rotate_and_verify(db, registry, policy, clock)
    plan = _plan(db, registry, policy, LATER)
    save_plan(db, plan)
    before = {
        credential.id: (credential.version, credential.status)
        for credential in load_credentials(db).values()
    }
    result = rotate_batch(db, plan.batch_id, FixedClock(LATER), policy)
    assert result.status == "completed"
    after = {
        credential.id: (credential.version, credential.status)
        for credential in load_credentials(db).values()
    }
    assert before == after
