from credrot.audit import AuditLog


def test_audit_event_round_trip(db, clock):
    audit = AuditLog(db, clock)
    audit.record(
        actor="tester",
        action="credential.transition",
        subject="CRED-ALPHA-READER-01",
        before={"state": "active"},
        after={"state": "standby"},
        batch_id="BATCH-1",
        reason="test",
    )
    event = audit.list_events(subject="CRED-ALPHA-READER-01")[0]
    assert event["actor"] == "tester"
    assert event["before"] == {"state": "active"}
    assert event["after"] == {"state": "standby"}
    assert event["batch_id"] == "BATCH-1"
    assert event["at"] == "2025-01-01T00:00:00Z"


def test_audit_filters_and_order(db, clock):
    audit = AuditLog(db, clock)
    for index in range(3):
        audit.record(
            actor="tester",
            action="verification.checked",
            subject=f"CRED-{index}",
            batch_id="BATCH-A" if index < 2 else "BATCH-B",
            reason=str(index),
        )
    assert len(audit.list_events(batch_id="BATCH-A")) == 2
    assert len(audit.list_events(action="verification.checked")) == 3
    assert [event["reason"] for event in audit.list_events()] == ["0", "1", "2"]


def test_audit_limit_is_bounded(db, clock):
    audit = AuditLog(db, clock)
    for index in range(5):
        audit.record(
            actor="tester", action="x", subject=str(index), reason="r"
        )
    assert len(audit.list_events(limit=2)) == 2
    assert len(audit.list_events(limit=0)) == 1
