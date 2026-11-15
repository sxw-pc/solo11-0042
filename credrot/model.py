"""Domain objects shared by the registry, planner, executor and reporters."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from .clock import days_until, format_time, is_due


STATUSES = ("active", "standby", "rotating", "expired", "revoked")
ROLES = ("reader", "writer", "admin")
USABLE_STATES = ("active", "standby")


class DomainError(Exception):
    """Base class for expected credential-domain failures."""


class InvalidTransition(DomainError):
    def __init__(self, credential_id: str, from_state: str, to_state: str):
        self.credential_id = credential_id
        self.from_state = from_state
        self.to_state = to_state
        super().__init__(
            f"credential {credential_id}: illegal transition {from_state} -> {to_state}"
        )


@dataclass(frozen=True)
class Credential:
    id: str
    source: str
    role: str
    version: int
    synthetic_ref: str
    fingerprint: str
    issued_at: datetime
    expires_at: datetime
    status: str

    def effective_state(self, now: datetime) -> str:
        if self.status == "revoked":
            return "revoked"
        if self.status == "expired":
            return "expired"
        if is_due(self.expires_at, now):
            return "expired"
        return self.status

    def days_left(self, now: datetime) -> int:
        return days_until(self.expires_at, now)

    def changed(self, **kwargs: Any) -> "Credential":
        return replace(self, **kwargs)

    def to_dict(self, now: datetime) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "role": self.role,
            "version": self.version,
            "synthetic_ref": self.synthetic_ref,
            "fingerprint": self.fingerprint,
            "issued_at": format_time(self.issued_at),
            "expires_at": format_time(self.expires_at),
            "state": self.status,
            "effective_state": self.effective_state(now),
            "days_to_expiry": self.days_left(now),
        }

    def brief(self, now: datetime) -> dict[str, Any]:
        return {
            "credential_id": self.id,
            "version": self.version,
            "role": self.role,
            "state": self.effective_state(now),
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class Task:
    id: str
    source: str
    priority: int
    required_roles: tuple[str, ...]
    min_active: int
    backup_margin: int
    bindings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source": self.source,
            "priority": self.priority,
            "required_roles": list(self.required_roles),
            "min_active": self.min_active,
            "backup_margin": self.backup_margin,
            "bindings": list(self.bindings),
        }


@dataclass(frozen=True)
class Precondition:
    """Execution precondition captured for a credential at planning time.

    The snapshot is the consistency contract for a plan item: rotation only
    proceeds when the credential's current identity fields still match it.
    ``effective_state`` is the state computed at planning time; ``status`` is
    the stored state and ``expires_at`` the validity deadline the snapshot was
    based on, so a later natural expiry can be told apart from a write made by
    another batch without re-running the planner.
    """

    version: int
    status: str
    effective_state: str
    expires_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "status": self.status,
            "effective_state": self.effective_state,
            "expires_at": self.expires_at,
        }


@dataclass(frozen=True)
class PlanItem:
    credential_id: str
    source: str
    role: str
    action: str
    from_state: str
    target_state: str
    reason_code: str
    reason: str
    days_to_expiry: int
    affected_tasks: tuple[str, ...]
    task_priority: int
    precondition: Precondition

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "source": self.source,
            "role": self.role,
            "action": self.action,
            "from_state": self.from_state,
            "target_state": self.target_state,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "days_to_expiry": self.days_to_expiry,
            "affected_tasks": list(self.affected_tasks),
            "task_priority": self.task_priority,
            "precondition": self.precondition.to_dict(),
        }


@dataclass(frozen=True)
class RotationPlan:
    batch_id: str
    generated_at: datetime
    items: tuple[PlanItem, ...] = ()
    summary: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "generated_at": format_time(self.generated_at),
            "items": [item.to_dict() for item in self.items],
            "summary": dict(self.summary),
        }


@dataclass(frozen=True)
class AuditEvent:
    actor: str
    action: str
    subject: str
    before: Mapping[str, Any]
    after: Mapping[str, Any]
    batch_id: str | None
    reason: str
    at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "actor": self.actor,
            "action": self.action,
            "subject": self.subject,
            "before": dict(self.before),
            "after": dict(self.after),
            "batch_id": self.batch_id,
            "reason": self.reason,
            "at": format_time(self.at),
        }


@dataclass(frozen=True)
class Verification:
    credential_id: str
    outcome: str
    code: str
    checked_at: datetime
    batch_id: str | None = None
    latency_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "credential_id": self.credential_id,
            "outcome": self.outcome,
            "code": self.code,
            "checked_at": format_time(self.checked_at),
            "batch_id": self.batch_id,
        }
