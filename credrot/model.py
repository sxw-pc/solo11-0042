"""Domain objects shared by the registry, planner, executor and reporters."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping

from .clock import days_until, format_time, is_due, parse_time
from .config import Issue


STATUSES = ("active", "standby", "rotating", "expired", "revoked")
ROLES = ("reader", "writer", "admin")
USABLE_STATES = ("active", "standby")

SYNTHETIC_REF_PATTERN = re.compile(
    r"^SYNTH-(?P<source>DS-[A-Z0-9][A-Z0-9-]*)-(?P<role>" + "|".join(ROLES) + r")-(?P<seq>[0-9]{2,})$")


def synthetic_ref_for(source: str, role: str, seq: int) -> str:
    """Canonical synthetic identity for a source/role/sequence triple."""
    return f"SYNTH-{source}-{role}-{seq:02d}"


def parse_synthetic_ref(ref: str) -> tuple[str, str, int] | None:
    """Split a synthetic_ref into (source, role, seq); None when malformed."""
    match = SYNTHETIC_REF_PATTERN.match(ref)
    if match is None:
        return None
    return match.group("source"), match.group("role"), int(match.group("seq"))


def compute_fingerprint(synthetic_ref: str) -> str:
    return "sha256:" + hashlib.sha256(synthetic_ref.encode("utf-8")).hexdigest()


def next_available_seq(
    credentials: Iterable["Credential"], source: str, role: str
) -> int:
    """Smallest unoccupied positive seq among same source/role credentials.

    Revoked records keep their seq burned; malformed refs occupy no number.
    """
    used: set[int] = set()
    for credential in credentials:
        if credential.source != source or credential.role != role:
            continue
        parsed = parse_synthetic_ref(credential.synthetic_ref)
        if parsed is not None and parsed[:2] == (source, role):
            used.add(parsed[2])
    seq = 1
    while seq in used:
        seq += 1
    return seq


def renewed_identity(
    credential: "Credential", seq: int, now: datetime, rotation_period_days: int
) -> "Credential":
    """Next version whose identity satisfies the registry rules."""
    ref = synthetic_ref_for(credential.source, credential.role, seq)
    return credential.changed(
        version=credential.version + 1, synthetic_ref=ref,
        fingerprint=compute_fingerprint(ref), issued_at=now,
        expires_at=now + timedelta(days=rotation_period_days))


def validate_rotation_identity(
    credential: "Credential", *, seq: int, now: datetime,
    rotation_period_days: int, holders: Mapping[str, str],
) -> list[Issue]:
    """Check a renewed identity against the registry rules.

    ``holders`` maps every *other* credential's synthetic_ref to its id, so
    a collision with a live or revoked record fails as IDENTITY_CONFLICT.
    """
    issues: list[Issue] = []
    path = f"rotation.{credential.id}"
    expected_ref = synthetic_ref_for(credential.source, credential.role, seq)
    if credential.synthetic_ref != expected_ref:
        issues.append(Issue("SYNTHETIC_REF_MISMATCH", f"{path}.synthetic_ref",
                            f"expected {expected_ref}"))
    holder = holders.get(credential.synthetic_ref)
    if holder is not None:
        issues.append(Issue("IDENTITY_CONFLICT", f"{path}.synthetic_ref",
                            f"{credential.synthetic_ref} already held by {holder}"))
    if credential.fingerprint != compute_fingerprint(credential.synthetic_ref):
        issues.append(Issue("FINGERPRINT_MISMATCH", f"{path}.fingerprint",
                            "does not match sha256(synthetic_ref)"))
    expected_expires = parse_time(now) + timedelta(days=rotation_period_days)
    if (parse_time(credential.issued_at) != parse_time(now)
            or parse_time(credential.expires_at) != expected_expires):
        issues.append(Issue(
            "VALIDITY_MISMATCH", path,
            f"expected issued_at={format_time(now)} and expires_at={format_time(expected_expires)}"))
    return issues


def rotation_identity(
    credential: "Credential", siblings: Iterable["Credential"],
    now: datetime, rotation_period_days: int,
) -> tuple["Credential", list[Issue]]:
    """Allocate, build and validate a renewed identity for one rotation."""
    siblings = list(siblings)
    seq = next_available_seq(siblings, credential.source, credential.role)
    renewed = renewed_identity(credential, seq, now, rotation_period_days)
    holders = {c.synthetic_ref: c.id for c in siblings if c.id != credential.id}
    issues = validate_rotation_identity(
        renewed, seq=seq, now=now, holders=holders,
        rotation_period_days=rotation_period_days)
    return renewed, issues


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

    @property
    def seq(self) -> int | None:
        """Sequence number encoded in ``synthetic_ref``; None when non-canonical."""
        parsed = parse_synthetic_ref(self.synthetic_ref)
        if parsed is None or parsed[:2] != (self.source, self.role):
            return None
        return parsed[2]

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
