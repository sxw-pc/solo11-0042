"""Deterministic Markdown/JSON reports and batch archive snapshots."""

from __future__ import annotations

import json
from typing import Any

from .clock import Clock, format_time
from .config import Policy
from .db import Database
from .resolver import resolve
from .store import (
    get_batch,
    list_batches,
    list_rotation_items,
    load_credentials,
    load_tasks,
)
from .verification_store import latest_verifications
from .verifier import conclude
from .report_markdown import render_markdown as render_markdown


class ReportError(ValueError):
    pass


__all__ = ["ReportError", "build_report", "render_json", "render_markdown"]


FAILURE_ACTIONS = {
    "credential.transition_rejected",
    "rotation.failed",
    "resolution.failed",
}


def build_report(
    db: Database,
    clock: Clock,
    policy: Policy,
    batch_id: str | None = None,
) -> dict[str, Any]:
    now = clock.now()
    credentials = load_credentials(db)
    tasks = load_tasks(db)
    latest = latest_verifications(db)
    batch = _select_batch(db, batch_id)
    if batch_id is not None and batch is None:
        raise ReportError(f"unknown batch: {batch_id}")

    matrix = []
    for credential in sorted(credentials.values(), key=lambda item: item.id):
        observation = latest.get(credential.id)
        matrix.append(
            {
                "credential_id": credential.id,
                "source": credential.source,
                "role": credential.role,
                "state": credential.effective_state(now),
                "outcome": observation["outcome"] if observation else "unverified",
                "code": observation["code"] if observation else "NOT_CHECKED",
                "checked_at": observation["checked_at"] if observation else None,
            }
        )
    checked_states = [
        row["outcome"]
        for row in matrix
        if row["state"] in {"active", "standby"} and row["outcome"] != "unverified"
    ]
    health = {
        "conclusion": conclude(checked_states),
        "counts": {
            outcome: sum(1 for row in matrix if row["outcome"] == outcome)
            for outcome in ("valid", "invalid", "unknown", "error", "unverified")
        },
        "matrix": matrix,
    }

    task_results = [
        resolve(db, task.id, clock, policy, write_audit=False)
        for task in sorted(tasks.values(), key=lambda item: (item.priority, item.id))
    ]
    failures = _failures(batch, task_results, db)
    report = {
        "generated_at": format_time(now),
        "credentials": [
            credential.to_dict(now)
            for credential in sorted(credentials.values(), key=lambda item: item.id)
        ],
        "batches": list_batches(db, limit=20),
        "batch": _batch_detail(batch, db),
        "health": health,
        "tasks": [_task_summary(result) for result in task_results],
        "task_details": task_results,
        "failures": failures,
    }
    return report


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(
        report, ensure_ascii=False, sort_keys=True, indent=2
    ) + "\n"


def _select_batch(db: Database, batch_id: str | None) -> dict[str, Any] | None:
    if batch_id is not None:
        return get_batch(db, batch_id)
    batches = list_batches(db, limit=1)
    if not batches:
        return None
    return get_batch(db, batches[0]["batch_id"])


def _batch_detail(
    batch: dict[str, Any] | None, db: Database
) -> dict[str, Any] | None:
    if batch is None:
        return None
    stored_items = list_rotation_items(db, batch["batch_id"])
    if stored_items:
        items = [
            {
                "credential_id": row["credential_id"],
                "action": row["action"],
                "from_state": row["from_state"],
                "to_state": row["to_state"],
                "reason_code": row["reason_code"],
                "result": row["result"],
                "failure_reason": row["failure_reason"],
                "failure_code": row["failure_code"],
                "at": row["at"],
            }
            for row in stored_items
        ]
    else:
        items = [
            {
                "credential_id": item["credential_id"],
                "action": item["action"],
                "from_state": item["from_state"],
                "to_state": item["target_state"],
                "reason_code": item["reason_code"],
                "result": "planned",
                "failure_reason": None,
                "failure_code": None,
                "at": None,
            }
            for item in batch["plan"].get("items", [])
        ]
    return {
        "batch_id": batch["batch_id"],
        "status": batch["status"],
        "generated_at": batch["generated_at"],
        "executed_at": batch["executed_at"],
        "failure_reason": batch["failure_reason"],
        "failure_code": batch.get("failure_code"),
        "summary": batch["summary"],
        "items": items,
    }


def _task_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "task_id": result["task_id"],
        "ok": result["ok"],
        "code": result["code"],
        "degraded": result["degraded"],
        "resolved": result["credentials"],
    }


def _failures(
    batch: dict[str, Any] | None, tasks: list[dict[str, Any]], db: Database
) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    if batch is not None and batch.get("failure_reason"):
        failures.append(
            {
                "kind": "rotation",
                "subject": batch["batch_id"],
                "message": batch["failure_reason"],
                "code": batch.get("failure_code"),
            }
        )
    for task in tasks:
        if not task["ok"]:
            failures.append(
                {
                    "kind": "resolution",
                    "subject": task["task_id"],
                    "message": f"{task['code']}: {task['message']}",
                    "code": task["code"],
                }
            )
    for event in _failure_events(db):
        failures.append(
            {
                "kind": event["action"],
                "subject": event["subject"],
                "message": event["reason"],
                "code": (event.get("after") or {}).get("failure_code"),
            }
        )
    deduped = {
        (item["kind"], item["subject"], item["message"], item["code"]): item
        for item in failures
    }
    return [deduped[key] for key in sorted(deduped)]


def _failure_events(db: Database) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            """SELECT at, actor, action, subject, before_json, after_json,
                      batch_id, reason
               FROM audit_events ORDER BY at, subject, action, id"""
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "at": row["at"], "actor": row["actor"], "action": row["action"],
            "subject": row["subject"], "batch_id": row["batch_id"],
            "reason": row["reason"],
            "after": json.loads(row["after_json"] or "{}"),
        }
        for row in rows
        if row["action"] in FAILURE_ACTIONS
    ]
