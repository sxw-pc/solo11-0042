"""Credential state machine with an explicit transition whitelist."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .clock import Clock, is_due
from .model import Credential, InvalidTransition, STATUSES


TRANSITIONS: dict[str, frozenset[str]] = {
    "active": frozenset({"standby", "rotating", "expired", "revoked"}),
    "standby": frozenset({"active", "rotating", "expired", "revoked"}),
    "rotating": frozenset({"active", "standby", "expired", "revoked"}),
    "expired": frozenset({"rotating", "revoked"}),
    "revoked": frozenset(),
}


class StateMachine:
    """Mutates credentials through validated transitions and emits audits.

    Mutating a frozen ``Credential`` returns a replacement object. Persistence
    is deliberately the caller's responsibility so this class stays testable
    outside SQLite.
    """

    def __init__(self, audit: Any = None, clock: Clock | None = None):
        self.audit = audit
        self.clock = clock

    @staticmethod
    def can_transition(from_state: str, to_state: str) -> bool:
        if from_state not in TRANSITIONS or to_state not in STATUSES:
            return False
        return to_state in TRANSITIONS[from_state]

    def transition(
        self,
        credential: Credential,
        target_state: str,
        *,
        actor: str,
        reason: str,
        at: datetime | None = None,
        conn: Any = None,
        batch_id: str | None = None,
    ) -> Credential:
        if at is None:
            if self.clock is None:
                raise ValueError("state transition requires 'at' or an injected clock")
            at = self.clock.now()
        if target_state not in STATUSES:
            self._reject(
                credential,
                target_state,
                actor,
                reason or "unknown target state",
                at,
                conn,
                batch_id,
            )
        if target_state == "expired" and actor not in {"clock", "system"}:
            self._reject(
                credential,
                target_state,
                actor,
                "expired is a clock-driven state",
                at,
                conn,
                batch_id,
            )
        if not self.can_transition(credential.status, target_state):
            self._reject(
                credential,
                target_state,
                actor,
                reason,
                at,
                conn,
                batch_id,
            )

        before = {
            "state": credential.status,
            "version": credential.version,
            "expires_at": credential.expires_at.isoformat(),
        }
        updated = credential.changed(status=target_state)
        after = {
            "state": updated.status,
            "version": updated.version,
            "expires_at": updated.expires_at.isoformat(),
        }
        if self.audit is not None:
            self.audit.record(
                actor=actor,
                action="credential.transition",
                subject=credential.id,
                before=before,
                after=after,
                batch_id=batch_id,
                reason=reason,
                at=at,
                conn=conn,
            )
        return updated

    def expire_if_due(
        self,
        credential: Credential,
        now: datetime,
        *,
        conn: Any = None,
        batch_id: str | None = None,
    ) -> tuple[Credential, bool]:
        if credential.status in {"revoked", "expired"}:
            return credential, False
        if not is_due(credential.expires_at, now):
            return credential, False
        updated = self.transition(
            credential,
            "expired",
            actor="clock",
            reason="expires_at reached",
            at=now,
            conn=conn,
            batch_id=batch_id,
        )
        return updated, True

    def _reject(
        self,
        credential: Credential,
        target_state: str,
        actor: str,
        reason: str,
        at: datetime,
        conn: Any,
        batch_id: str | None,
    ) -> None:
        if self.audit is not None:
            self.audit.record(
                actor=actor,
                action="credential.transition_rejected",
                subject=credential.id,
                before={"state": credential.status},
                after={"state": target_state},
                batch_id=batch_id,
                reason=reason or "illegal transition",
                at=at,
                conn=conn,
            )
        raise InvalidTransition(credential.id, credential.status, target_state)
