"""Read-only credential state-history view.

The per-credential migration sequence is derived from ``credential.transition``
and ``credential.renewed`` audit events, cross-referenced with executed
``rotation_items``. Audit events are the only evidence of a real migration, so
skipped items never appear, idempotent re-runs add nothing, and clock-driven
expiry (``expire_if_due``) shows up even though it is never written to
``rotation_items``. Reports and archives share this view.
"""

from __future__ import annotations

from typing import Any

from .audit import AuditLog
from .clock import Clock
from .db import Database
from .store import list_rotation_items

HISTORY_ACTIONS = ("credential.transition", "credential.renewed")


def build_state_history(
    db: Database, clock: Clock, batch_id: str | None = None
) -> list[dict[str, Any]]:
    """Return migration entries in audit order, optionally scoped to a batch."""
    audit = AuditLog(db, clock)
    recorded = audit.list_events(batch_id=batch_id, limit=5000)
    events = [e for e in recorded if e["action"] in HISTORY_ACTIONS]
    reason_codes = _executed_reason_codes(db, batch_id, events)
    history: list[dict[str, Any]] = []
    current: dict[str, str] = {}
    for event in events:
        credential_id = event["subject"]
        if event["action"] == "credential.transition":
            from_state = event["before"].get("state")
            to_state = event["after"].get("state")
        else:
            # credential.renewed bumps the version without changing state.
            from_state = to_state = current.get(credential_id)
        if to_state is not None:
            current[credential_id] = to_state
        history.append(
            {
                "credential_id": credential_id, "from_state": from_state,
                "to_state": to_state, "reason": event["reason"],
                "reason_code": reason_codes.get(
                    (event["batch_id"], credential_id)
                ),
                "batch_id": event["batch_id"], "at": event["at"],
                "event": event["action"],
            }
        )
    return history


def _executed_reason_codes(
    db: Database, batch_id: str | None, events: list[dict[str, Any]]
) -> dict[tuple[str, str], str]:
    """Map ``(batch_id, credential_id)`` to executed items' reason codes."""
    batches = [batch_id] if batch_id is not None else sorted(
        {event["batch_id"] for event in events if event["batch_id"]}
    )
    codes: dict[tuple[str, str], str] = {}
    for batch in batches:
        for item in list_rotation_items(db, batch):
            if item["result"] == "executed":
                codes[(batch, item["credential_id"])] = item["reason_code"]
    return codes
