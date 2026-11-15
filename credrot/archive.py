"""Batch-scoped archive snapshot export."""

from __future__ import annotations

from typing import Any

from .audit import AuditLog
from .clock import Clock, format_time
from .db import Database
from .store import get_batch, list_rotation_items, load_credentials
from .verification_store import list_verifications

from .report import ReportError


def archive_batch(
    db: Database, clock: Clock, batch_id: str
) -> dict[str, Any]:
    batch = get_batch(db, batch_id)
    if batch is None:
        raise ReportError(f"unknown batch: {batch_id}")
    now = clock.now()
    audit = AuditLog(db, clock)
    return {
        "archived_at": format_time(now),
        "batch": batch,
        "rotation_items": list_rotation_items(db, batch_id),
        "verifications": list_verifications(db, batch_id),
        "audit_events": audit.list_events(batch_id=batch_id, limit=5000),
        "credentials": [
            credential.to_dict(now)
            for credential in sorted(
                load_credentials(db).values(), key=lambda item: item.id
            )
        ],
    }


