from datetime import timedelta

import pytest

from credrot.audit import AuditLog
from credrot.model import InvalidTransition
from credrot.state_machine import StateMachine


def test_transition_whitelist():
    machine = StateMachine()
    assert machine.can_transition("standby", "active")
    assert machine.can_transition("active", "rotating")
    assert machine.can_transition("rotating", "standby")
    assert machine.can_transition("expired", "rotating")
    assert machine.can_transition("active", "revoked")
    assert not machine.can_transition("revoked", "active")
    assert not machine.can_transition("active", "active")
    assert machine.can_transition("standby", "expired")


def test_valid_transition_is_audited(db, registry, clock):
    audit = AuditLog(db, clock)
    machine = StateMachine(audit=audit, clock=clock)
    credential = registry.credentials["CRED-ALPHA-READER-02"]
    updated = machine.transition(
        credential, "active", actor="tester", reason="promote", conn=None
    )
    assert updated.status == "active"
    assert credential.status == "standby"
    events = audit.list_events(subject=credential.id)
    assert events[-1]["action"] == "credential.transition"
    assert events[-1]["before"]["state"] == "standby"
    assert events[-1]["after"]["state"] == "active"


def test_illegal_transition_raises_and_is_audited(db, registry, clock):
    audit = AuditLog(db, clock)
    machine = StateMachine(audit=audit, clock=clock)
    revoked = registry.credentials["CRED-GAMMA-OLD-01"]
    with pytest.raises(InvalidTransition):
        machine.transition(revoked, "active", actor="tester", reason="try")
    events = audit.list_events(subject=revoked.id)
    assert events[-1]["action"] == "credential.transition_rejected"
    assert events[-1]["after"]["state"] == "active"


def test_expiry_is_clock_driven_and_exact_boundary(db, registry, clock):
    audit = AuditLog(db, clock)
    machine = StateMachine(audit=audit, clock=clock)
    credential = registry.credentials["CRED-ALPHA-READER-01"]
    before, changed = machine.expire_if_due(
        credential, credential.expires_at - timedelta(seconds=1)
    )
    assert changed is False and before.status == "active"
    after, changed = machine.expire_if_due(credential, credential.expires_at)
    assert changed is True and after.status == "expired"
    assert after.effective_state(clock.now()) == "expired"


def test_human_cannot_force_expired_state(db, registry, clock):
    audit = AuditLog(db, clock)
    machine = StateMachine(audit=audit, clock=clock)
    credential = registry.credentials["CRED-ALPHA-READER-01"]
    with pytest.raises(InvalidTransition):
        machine.transition(credential, "expired", actor="operator", reason="manual")
    events = audit.list_events(subject=credential.id)
    assert "clock-driven" in events[-1]["reason"]
