"""Thin local HTTP handlers for the credrot application."""

from __future__ import annotations

from typing import Any

from flask import Blueprint, Response, jsonify, request

from .clock import Clock, ClockError, format_time, parse_time
from .config import Policy
from .db import Database
from .planner import build_plan
from .report import ReportError, build_report, render_json
from .report_markdown import render_markdown
from .rotator import UnknownBatch, rotate_batch
from .store import (
    get_batch,
    list_batches,
    load_credentials,
    load_tasks,
    save_plan,
    table_counts,
)
from .verification_store import latest_verifications
from .verifier import conclude


def create_api_blueprint(db: Database, clock: Clock, policy: Policy) -> Blueprint:
    bp = Blueprint("api", __name__, url_prefix="/api")

    def _now() -> Any:
        raw = request.args.get("now")
        return parse_time(raw) if raw else clock.now()

    @bp.get("/credentials")
    def credentials() -> Any:
        now = _now()
        observations = latest_verifications(db)
        items = []
        for credential in sorted(
            load_credentials(db).values(), key=lambda item: item.id
        ):
            payload = credential.to_dict(now)
            observation = observations.get(credential.id)
            payload["verification"] = (
                {
                    "outcome": observation["outcome"],
                    "code": observation["code"],
                    "checked_at": observation["checked_at"],
                }
                if observation
                else None
            )
            items.append(payload)
        return jsonify({"generated_at": format_time(now), "credentials": items})

    @bp.get("/plan")
    def plan() -> Any:
        try:
            now = _now()
        except ClockError as exc:
            return _error(str(exc), 400)
        plan_value = build_plan(
            load_credentials(db), load_tasks(db), policy, now
        )
        inserted = save_plan(db, plan_value)
        payload = plan_value.to_dict()
        payload["stored"] = inserted or get_batch(db, plan_value.batch_id) is not None
        return Response(
            render_json(payload),
            mimetype="application/json",
        )

    @bp.get("/health")
    def health() -> Any:
        now = _now()
        credentials = list(load_credentials(db).values())
        observations = latest_verifications(db)
        outcomes = [
            observations[item.id]["outcome"]
            for item in credentials
            if item.effective_state(now) in {"active", "standby"}
            and item.id in observations
        ]
        latest_batch = list_batches(db, limit=1)
        return jsonify(
            {
                "status": "ok",
                "generated_at": format_time(now),
                "counts": table_counts(db),
                "verification": {
                    "conclusion": conclude(outcomes),
                    "observed": len(outcomes),
                },
                "latest_batch": latest_batch[0] if latest_batch else None,
            }
        )

    @bp.get("/report")
    def report() -> Any:
        batch_id = request.args.get("batch_id") or request.args.get("batch")
        try:
            now = _now()
            clock_value = _clock_at(now)
            payload = build_report(db, clock_value, policy, batch_id)
        except (ClockError, ReportError) as exc:
            return _error(str(exc), 400)
        output_format = request.args.get("format", "json").lower()
        if output_format in {"md", "markdown"}:
            return Response(render_markdown(payload), mimetype="text/markdown")
        if output_format != "json":
            return _error("format must be json or markdown", 400)
        return Response(render_json(payload), mimetype="application/json")

    @bp.post("/rotate")
    def rotate() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("JSON object required", 400)
        batch_id = payload.get("batch_id")
        if not isinstance(batch_id, str) or not batch_id:
            return _error("batch_id is required", 400)
        try:
            result = rotate_batch(db, batch_id, clock, policy)
        except UnknownBatch as exc:
            return _error(str(exc), 404)
        status = 200 if result.status == "completed" else 409
        return jsonify(result.to_dict()), status

    @bp.errorhandler(ClockError)
    def clock_error(exc: ClockError) -> Any:
        return _error(str(exc), 400)

    return bp


def _clock_at(now: Any) -> Any:
    from .clock import FixedClock

    return FixedClock(now)


def _error(message: str, status: int) -> tuple[Any, int]:
    return jsonify({"error": message}), status
