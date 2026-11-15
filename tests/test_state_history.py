from credrot.archive import archive_batch
from credrot.clock import FixedClock
from credrot.planner import build_plan
from credrot.report import build_report
from credrot.rotator import rotate_batch
from credrot.state_history import build_state_history
from credrot.store import list_rotation_items, save_plan


def _completed_plan(db, registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "completed"
    return plan


def _chain(history, batch_id, credential_id):
    return [
        entry
        for entry in history
        if (entry["batch_id"], entry["credential_id"]) == (batch_id, credential_id)
    ]


def _states(chain):
    return [(entry["from_state"], entry["to_state"]) for entry in chain]


def test_history_matches_audit_trail_for_completed_batch(
    db, registry, policy, clock
):
    plan = _completed_plan(db, registry, policy, clock)
    history = build_state_history(db, clock, plan.batch_id)
    assert {item.credential_id for item in plan.items} == {
        entry["credential_id"] for entry in history
    }
    beta = _chain(history, plan.batch_id, "CRED-BETA-READER-01")
    assert _states(beta) == [
        ("active", "rotating"), ("rotating", "rotating"), ("rotating", "active")
    ]
    assert beta[1]["event"] == "credential.renewed"
    assert all(entry["reason_code"] == "CRITICAL_EXPIRY" for entry in beta)
    assert all(entry["at"] == "2025-01-01T00:00:00Z" for entry in beta)
    gamma = _chain(history, plan.batch_id, "CRED-GAMMA-SPARE-01")
    assert _states(gamma) == [("standby", "active")]
    assert gamma[0]["reason_code"] == "REDUNDANCY_LOW"


def test_history_records_clock_expiry_step_missing_from_rotation_items(
    db, registry, policy
):
    clock = FixedClock("2025-01-03T00:00:00Z")
    plan = _completed_plan(db, registry, policy, clock)
    history = build_state_history(db, clock, plan.batch_id)
    beta = _chain(history, plan.batch_id, "CRED-BETA-READER-01")
    assert _states(beta) == [
        ("active", "expired"), ("expired", "rotating"),
        ("rotating", "rotating"), ("rotating", "active"),
    ]
    assert beta[0]["reason"] == "expires_at reached"
    # rotation_items stores only the planned from/to, not this clock step.
    row = next(
        row
        for row in list_rotation_items(db, plan.batch_id)
        if row["credential_id"] == "CRED-BETA-READER-01"
    )
    assert (row["from_state"], row["to_state"]) == ("expired", "active")


def test_rerun_completed_batch_adds_no_fabricated_entries(
    db, registry, policy, clock
):
    plan = _completed_plan(db, registry, policy, clock)
    before = build_state_history(db, clock, plan.batch_id)
    again = rotate_batch(db, plan.batch_id, clock, policy)
    assert again.skipped == len(plan.items) and again.executed == 0
    assert build_state_history(db, clock, plan.batch_id) == before


def test_skipped_item_never_enters_state_history(db, registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'active' WHERE id = ?",
            ("CRED-GAMMA-SPARE-01",),
        )
    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "completed" and result.skipped == 1
    rows = {
        row["credential_id"]: row
        for row in list_rotation_items(db, plan.batch_id)
    }
    assert rows["CRED-GAMMA-SPARE-01"]["result"] == "skipped"
    history = build_state_history(db, clock, plan.batch_id)
    assert _chain(history, plan.batch_id, "CRED-GAMMA-SPARE-01") == []
    others = set(rows) - {"CRED-GAMMA-SPARE-01"}
    assert {entry["credential_id"] for entry in history} == others


def test_failed_batch_leaves_no_state_history(db, registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE credentials SET status = 'revoked' WHERE id = ?",
            ("CRED-ALPHA-READER-01",),
        )
    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "failed"
    assert build_state_history(db, clock, plan.batch_id) == []


def test_report_and_archive_share_one_state_history(
    db, registry, policy, clock
):
    assert build_state_history(db, clock) == []
    assert build_report(db, clock, policy)["state_history"] == []
    plan = _completed_plan(db, registry, policy, clock)
    report = build_report(db, clock, policy, plan.batch_id)
    snapshot = archive_batch(db, clock, plan.batch_id)
    history = report["state_history"]
    assert history == snapshot["state_history"]
    # The global view (no batch filter) covers the same audit events.
    assert build_state_history(db, clock) == history
    for item in report["batch"]["items"]:
        chain = _chain(history, plan.batch_id, item["credential_id"])
        assert (item["result"] == "executed") == bool(chain)
        if chain:
            assert chain[-1]["to_state"] == item["to_state"]
            planned = [chain[0]["from_state"]] + [e["to_state"] for e in chain]
            assert item["from_state"] in planned
