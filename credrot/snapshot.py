"""Planning-time snapshot contract and its execution-time classification.

A plan item records the credential's identity (version, stored state,
effective state and validity deadline) at planning time. Before an item is
applied inside the rotation transaction, that snapshot is compared with the
current ledger row. Classification is derived solely from the stored
snapshot — the planner is never re-run to reconstruct why a plan went stale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .clock import format_time, is_due, parse_time
from .model import Credential


#: A different batch advanced the credential between planning and execution.
SUPERSEDED_BY_BATCH = "SUPERSEDED_BY_BATCH"
#: The credential drifted only because the planned validity window elapsed.
SNAPSHOT_STALE = "SNAPSHOT_STALE"
SNAPSHOT_FAILURE_CODES = frozenset({SUPERSEDED_BY_BATCH, SNAPSHOT_STALE})


class SnapshotConflict(Exception):
    """Raised when an item's planning-time precondition no longer holds."""

    def __init__(self, credential_id: str, code: str, detail: str):
        self.credential_id = credential_id
        self.code = code
        self.detail = detail
        super().__init__(f"{credential_id}: {code}: {detail}")


def classify_snapshot_conflict(
    precondition: dict[str, Any] | None,
    credential: Credential,
    now: datetime,
) -> str | None:
    """Return a failure code for a mismatch, or ``None`` when it still holds.

    A changed version, stored status or validity deadline proves another
    batch wrote the credential (``SUPERSEDED_BY_BATCH``). A drift that is
    fully explained by the clock crossing the snapshot's ``expires_at`` is
    natural expiry (``SNAPSHOT_STALE``).
    """
    if not precondition:
        # Plans persisted before snapshots existed cannot be contract-checked.
        return None
    if (
        credential.version != precondition.get("version")
        or credential.status != precondition.get("status")
        or format_time(credential.expires_at) != precondition.get("expires_at")
    ):
        return SUPERSEDED_BY_BATCH
    current_effective = credential.effective_state(now)
    if current_effective == precondition.get("effective_state"):
        return None
    snapshot_deadline = precondition.get("expires_at")
    try:
        expired_naturally = (
            current_effective == "expired"
            and snapshot_deadline is not None
            and is_due(parse_time(snapshot_deadline), now)
        )
    except ValueError:
        expired_naturally = False
    return SNAPSHOT_STALE if expired_naturally else SUPERSEDED_BY_BATCH


def ensure_precondition(
    item: dict[str, Any], credential: Credential, now: datetime
) -> None:
    """Raise :class:`SnapshotConflict` when the item's snapshot is stale."""
    code = classify_snapshot_conflict(item.get("precondition"), credential, now)
    if code is None:
        return
    precondition = item.get("precondition") or {}
    detail = (
        f"snapshot v{precondition.get('version')} "
        f"{precondition.get('status')}/{precondition.get('effective_state')} "
        f"does not match current v{credential.version} "
        f"{credential.status}/{credential.effective_state(now)}"
    )
    raise SnapshotConflict(item["credential_id"], code, detail)
