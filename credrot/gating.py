"""Post-rotation verification gate: the temporal contract between tables.

A rotation is only *established* when the batch that performed it committed.
The executor writes a ``failed`` row for a rolled-back batch, so the mere
presence of a ``rotation_items`` row for a credential proves nothing. Likewise
a verification only counts as evidence when its verdict is ``valid`` and its
``checked_at`` is consistent with the batch that recorded it. Both facts are
therefore derived from committed state, never from ``MAX(id)`` ordering.

The resulting ``GateDecision`` objects are pure data. ``planner`` turns them
into visible ``POST_ROTATION_UNVERIFIED`` entries; the plan JSON is persisted
in ``rotation_batches.plan_json`` and, once executed, in ``rotation_items``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Mapping, Protocol

from .clock import format_time, parse_time
from .db import Database
from .store import latest_successful_rotations
from .verification_store import latest_valid_verifications


POST_ROTATION_UNVERIFIED = "POST_ROTATION_UNVERIFIED"


class _CredentialLike(Protocol):
    id: str

    def effective_state(self, now: datetime) -> str: ...


@dataclass(frozen=True)
class RotationEvidence:
    """A rotate item from a batch whose transaction committed."""

    credential_id: str
    batch_id: str
    executed_at: datetime


@dataclass(frozen=True)
class VerificationEvidence:
    """A ``valid`` observation consistent with its recording batch."""

    credential_id: str
    checked_at: datetime
    batch_id: str | None = None


@dataclass(frozen=True)
class GateDecision:
    credential_id: str
    rotation_batch_id: str
    rotated_at: datetime
    last_valid_verification_at: datetime | None

    def reason(self) -> str:
        return (
            f"rotation {self.rotation_batch_id} committed at "
            f"{format_time(self.rotated_at)} but no valid verification was "
            "recorded after it; rotate/promote withheld until re-verified"
        )


def rotation_evidence(record: Mapping[str, object]) -> RotationEvidence:
    return RotationEvidence(
        credential_id=str(record["credential_id"]),
        batch_id=str(record["batch_id"]),
        executed_at=parse_time(str(record["executed_at"])),
    )


def verification_evidence(record: Mapping[str, object]) -> VerificationEvidence:
    return VerificationEvidence(
        credential_id=str(record["credential_id"]),
        checked_at=parse_time(str(record["checked_at"])),
        batch_id=record.get("batch_id"),  # type: ignore[arg-type]
    )


def gates_for(
    credentials: Mapping[str, _CredentialLike] | object,
    rotations: Mapping[str, RotationEvidence],
    verifications: Mapping[str, VerificationEvidence],
    now: datetime,
) -> dict[str, GateDecision]:
    """Decide which credentials must not be scheduled this planning round.

    The gate fires when a committed rotation exists at or before ``now`` and
    the most recent valid verification is not strictly later than it. A
    verification stamped at the same instant is *not* later: with a fixed
    clock, ordering is ambiguous and the conservative branch wins.

    Revoked credentials are immutable and exempt. Credentials that never
    committed a rotation are never gated, so a failed batch cannot create one.
    """
    now = parse_time(now)
    cred_map = (
        dict(credentials)
        if isinstance(credentials, Mapping)
        else {item.id: item for item in credentials}  # type: ignore[union-attr]
    )
    decisions: dict[str, GateDecision] = {}
    for credential_id in sorted(cred_map):
        credential = cred_map[credential_id]
        if credential.effective_state(now) == "revoked":
            continue
        rotation = rotations.get(credential_id)
        if rotation is None or rotation.executed_at > now:
            continue
        verification = verifications.get(credential_id)
        if verification is not None and verification.checked_at > rotation.executed_at:
            continue
        decisions[credential_id] = GateDecision(
            credential_id=credential_id,
            rotation_batch_id=rotation.batch_id,
            rotated_at=rotation.executed_at,
            last_valid_verification_at=(
                verification.checked_at if verification is not None else None
            ),
        )
    return decisions


def load_gates(
    db: Database,
    credentials: Mapping[str, _CredentialLike] | object,
    now: datetime,
) -> dict[str, GateDecision]:
    """Read committed evidence from SQLite and evaluate the gate.

    Both loaders apply the ``now`` cutoff, so a plan at an earlier injected
    clock cannot borrow evidence from a later one.
    """
    rotations = {
        credential_id: rotation_evidence(record)
        for credential_id, record in latest_successful_rotations(
            db, as_of=now
        ).items()
    }
    verifications = {
        credential_id: verification_evidence(record)
        for credential_id, record in latest_valid_verifications(
            db, as_of=now
        ).items()
    }
    return gates_for(credentials, rotations, verifications, now)
