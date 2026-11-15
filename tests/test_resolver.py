from dataclasses import replace
from datetime import timedelta

from credrot.audit import AuditLog
from credrot.clock import FixedClock
from credrot.model import Verification
from credrot.resolver import resolve
from credrot.verification_store import insert_verification
from credrot.verifier import verify_all


def _observe(db, policy, clock, stub):
    verify_all(db, clock, policy, base_url=stub.base_url)


def test_resolves_verified_active_credential(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    result = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    assert result["ok"] is True
    assert result["degraded"] is False
    assert result["credentials"]["reader"]["credential_id"] == "CRED-ALPHA-READER-01"


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


def _revoke(db, *credential_ids):
    with db.transaction() as conn:
        for credential_id in credential_ids:
            conn.execute(
                "UPDATE credentials SET status = 'revoked' WHERE id = ?",
                (credential_id,),
            )


def _invalidate(db, clock, credential_id):
    with db.transaction() as conn:
        insert_verification(
            conn,
            Verification(
                credential_id=credential_id,
                outcome="invalid",
                code="INVALID",
                checked_at=clock.now(),
            ),
        )


def test_all_bindings_revoked_marks_role_for_repair(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    _revoke(db, "CRED-ALPHA-READER-01", "CRED-ALPHA-READER-02")
    result = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    assert result["ok"] is False
    assert result["code"] == "NO_HEALTHY_CANDIDATE"
    [reader] = result["role_integrity"]
    assert reader["role"] == "reader"
    assert reader["status"] == "repair"
    assert reader["reason"] == "ALL_BINDINGS_REVOKED"
    assert reader["bound"] == ["CRED-ALPHA-READER-01", "CRED-ALPHA-READER-02"]
    assert reader["revoked"] == ["CRED-ALPHA-READER-01", "CRED-ALPHA-READER-02"]
    assert reader["selected"] is None
    events = AuditLog(db, clock).list_events(action="resolution.failed")
    assert events[-1]["subject"] == "TASK-ALPHA-DAILY"


def test_unusable_non_revoked_standby_is_degraded_not_repair(db, policy, clock):
    # Active binding revoked, standby binding survives but is unverified:
    # a runtime gap, not a structural break.
    _revoke(db, "CRED-ALPHA-READER-01")
    result = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    assert result["ok"] is False
    [reader] = result["role_integrity"]
    assert reader["status"] == "degraded"
    assert reader["reason"] == "NO_USABLE_CANDIDATE"
    assert reader["revoked"] == ["CRED-ALPHA-READER-01"]
    assert reader["selected"] is None


def test_repair_and_degraded_roles_stay_separate(db, policy, clock, stub):
    _observe(db, policy, clock, stub)
    _revoke(db, "CRED-BETA-READER-01", "CRED-BETA-READER-02")
    _invalidate(db, clock, "CRED-BETA-WRITER-01")
    result = resolve(db, "TASK-BETA-DAILY", clock, policy)
    assert result["ok"] is False
    assert result["degraded"] is True
    by_role = {entry["role"]: entry for entry in result["role_integrity"]}
    assert by_role["reader"]["status"] == "repair"
    assert by_role["reader"]["reason"] == "ALL_BINDINGS_REVOKED"
    assert by_role["reader"]["revoked"] == [
        "CRED-BETA-READER-01", "CRED-BETA-READER-02",
    ]
    assert by_role["writer"]["status"] == "degraded"
    assert by_role["writer"]["reason"] == "STANDBY_SELECTED"
    assert by_role["writer"]["selected"] == "CRED-BETA-WRITER-02"


def test_task_without_bindings_marks_every_role_for_repair(db, policy, clock):
    with db.transaction() as conn:
        conn.execute(
            "DELETE FROM task_bindings WHERE task_id = ?", ("TASK-ALPHA-EXPORT",)
        )
    result = resolve(db, "TASK-ALPHA-EXPORT", clock, policy)
    assert result["code"] == "NO_BINDINGS"
    assert result["role_integrity"] == [
        {
            "role": "writer",
            "status": "repair",
            "reason": "NO_BOUND_CREDENTIAL",
            "bound": [],
            "revoked": [],
            "selected": None,
        }
    ]
