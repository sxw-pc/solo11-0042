from dataclasses import replace
from datetime import timedelta

import pytest

from credrot.audit import AuditLog
from credrot.clock import FixedClock
from credrot.model import Verification
from credrot.resolver import resolve
from credrot.verification_store import insert_verification
from credrot.verifier import verify_all


def _observe(db, policy, clock, stub):
    verify_all(db, clock, policy, base_url=stub.base_url)


def _resolve_aged(db, policy, clock, task_id, records):
    """Insert verifications aged by minutes, then resolve the task."""
    for credential_id, outcome, minutes in records:
        with db.transaction() as conn:
            insert_verification(
                conn,
                Verification(
                    credential_id=credential_id,
                    outcome=outcome,
                    code=outcome.upper(),
                    checked_at=clock.now() - timedelta(minutes=minutes),
                ),
            )
    return resolve(db, task_id, clock, policy)


def _softening(policy):
    return replace(
        policy, max_verification_age_minutes=60, soften_stale_for_roles=("reader",)
    )


def test_resolves_verified_active_credential(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    result = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    assert result["ok"] is True
    assert result["degraded"] is False
    assert result["credentials"]["reader"]["credential_id"] == "CRED-ALPHA-READER-01"
    assert all(row["verification_age_minutes"] == 0.0 for row in result["attempts"])


def test_falls_back_to_standby_when_active_is_invalid(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    with db.transaction() as conn:
        insert_verification(
            conn,
            Verification(
                credential_id="CRED-ALPHA-READER-01",
                outcome="invalid",
                code="INVALID",
                checked_at=clock.now(),
            ),
        )
    result = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    assert result["ok"] is True
    assert result["degraded"] is True
    assert result["credentials"]["reader"]["credential_id"] == "CRED-ALPHA-READER-02"
    reasons = {row["candidate_id"]: row["reason"] for row in result["attempts"]}
    assert reasons["CRED-ALPHA-READER-01"] == "VERIFICATION_INVALID"


def test_all_candidates_failing_returns_structured_failure(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    result = resolve(db, "TASK-GAMMA-AUDIT", clock, policy)
    assert result["ok"] is False
    assert result["code"] == "NO_HEALTHY_CANDIDATE"
    assert result["credentials"] == {"admin": None}
    assert result["attempts"][0]["reason"] == "VERIFICATION_INVALID"
    events = AuditLog(db, clock).list_events(action="resolution.failed")
    assert events[-1]["subject"] == "TASK-GAMMA-AUDIT"


def test_unverified_candidate_is_not_usable_by_default(db, policy, clock):
    result = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    assert result["ok"] is False
    assert result["attempts"][0]["reason"] == "UNVERIFIED"
    assert result["attempts"][0]["verification_age_minutes"] is None


def test_stale_verification_is_rejected(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    later = FixedClock(clock.now() + timedelta(hours=2))
    strict = replace(policy, max_verification_age_minutes=60)
    result = resolve(db, "TASK-ALPHA-DAILY", later, strict)
    assert result["ok"] is False
    assert result["attempts"][0]["reason"] == "STALE_VERIFICATION"


def test_missing_task_and_no_bindings_are_structured(db, policy, clock):
    missing = resolve(db, "TASK-UNKNOWN", clock, policy)
    assert missing["code"] == "TASK_NOT_FOUND"
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM task_bindings WHERE task_id = ?", ("TASK-ALPHA-EXPORT",)
        )
    empty = resolve(db, "TASK-ALPHA-EXPORT", clock, policy)
    assert empty["ok"] is False
    assert empty["code"] == "NO_BINDINGS"


def test_multi_role_task_resolves_each_role(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    result = resolve(db, "TASK-BETA-DAILY", clock, policy)
    assert result["ok"] is True
    assert set(result["credentials"]) == {"reader", "writer"}
    assert result["credentials"]["writer"]["credential_id"] == "CRED-BETA-WRITER-01"


def test_role_max_age_overrides_global_only_for_listed_role(db, policy, clock):
    custom = replace(policy, role_max_age_minutes={"reader": 60})
    result = _resolve_aged(db, custom, clock, "TASK-ALPHA-DAILY", [
        ("CRED-ALPHA-READER-01", "valid", 120),
        ("CRED-ALPHA-READER-02", "valid", 30),
    ])
    assert result["credentials"]["reader"]["credential_id"] == "CRED-ALPHA-READER-02"
    attempts = {row["candidate_id"]: row for row in result["attempts"]}
    assert attempts["CRED-ALPHA-READER-01"]["reason"] == "STALE_VERIFICATION"
    assert attempts["CRED-ALPHA-READER-01"]["verification_age_minutes"] == 120.0
    admin_only = replace(policy, role_max_age_minutes={"admin": 30})
    result = resolve(db, "TASK-ALPHA-DAILY", clock, admin_only)
    assert result["credentials"]["reader"]["credential_id"] == "CRED-ALPHA-READER-01"


def test_stale_softening_selects_stale_valid_candidate(db, policy, clock):
    result = _resolve_aged(db, _softening(policy), clock, "TASK-ALPHA-DAILY", [
        ("CRED-ALPHA-READER-01", "valid", 120),
        ("CRED-ALPHA-READER-02", "valid", 120),
    ])
    assert result["ok"] is True and result["degraded"] is True
    selected = result["credentials"]["reader"]
    assert selected["credential_id"] == "CRED-ALPHA-READER-01"
    assert selected["softened"] is True
    attempt = {row["candidate_id"]: row for row in result["attempts"]}
    attempt = attempt["CRED-ALPHA-READER-01"]
    assert attempt["reason"] == "STALE_SOFTENED"
    assert attempt["usable"] is True and attempt["softened"] is True
    assert attempt["verification_age_minutes"] == 120.0


@pytest.mark.parametrize("outcome", ["invalid", "unknown"])
def test_softening_never_applies_to_invalid_or_unknown(db, policy, clock, outcome):
    result = _resolve_aged(db, _softening(policy), clock, "TASK-ALPHA-DAILY", [
        ("CRED-ALPHA-READER-01", outcome, 120),
        ("CRED-ALPHA-READER-02", outcome, 120),
    ])
    assert result["ok"] is False and result["code"] == "NO_HEALTHY_CANDIDATE"
    assert result["credentials"] == {"reader": None}
    assert all(row["reason"] != "STALE_SOFTENED" for row in result["attempts"])


def test_fresh_standby_beats_stale_softened_active(db, policy, clock):
    result = _resolve_aged(db, _softening(policy), clock, "TASK-ALPHA-DAILY", [
        ("CRED-ALPHA-READER-01", "valid", 120),
        ("CRED-ALPHA-READER-02", "valid", 30),
    ])
    assert result["credentials"]["reader"]["credential_id"] == "CRED-ALPHA-READER-02"
    assert "softened" not in result["credentials"]["reader"]
    reasons = {row["candidate_id"]: row["reason"] for row in result["attempts"]}
    assert reasons["CRED-ALPHA-READER-01"] == "STALE_VERIFICATION"
    assert reasons["CRED-ALPHA-READER-02"] == "ACCEPTED_STANDBY"


def test_softening_boundary_conditions(db, policy, clock):
    unlisted = replace(_softening(policy), soften_stale_for_roles=("admin",))
    result = _resolve_aged(db, unlisted, clock, "TASK-ALPHA-DAILY", [
        ("CRED-ALPHA-READER-01", "valid", 120),
        ("CRED-ALPHA-READER-02", "valid", 120),
    ])
    assert result["ok"] is False and result["code"] == "NO_HEALTHY_CANDIDATE"
    # A candidate rejected for anything but staleness blocks softening.
    result = _resolve_aged(db, _softening(policy), clock, "TASK-ALPHA-DAILY", [
        ("CRED-ALPHA-READER-02", "invalid", 30),
    ])
    assert result["ok"] is False and result["code"] == "NO_HEALTHY_CANDIDATE"
    reasons = {row["candidate_id"]: row["reason"] for row in result["attempts"]}
    assert reasons["CRED-ALPHA-READER-01"] == "STALE_VERIFICATION"
    assert reasons["CRED-ALPHA-READER-02"] == "VERIFICATION_INVALID"
