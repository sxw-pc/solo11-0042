import pytest

from dataclasses import replace
from datetime import timedelta

from credrot.archive import archive_batch
from credrot.model import Verification
from credrot.planner import build_plan
from credrot.plan_export import plan_to_markdown
from credrot.report import ReportError, build_report, render_json, render_markdown
from credrot.rotator import rotate_batch
from credrot.store import save_plan
from credrot.verification_store import insert_verification
from credrot.verifier import verify_all


def _worked_batch(db, registry, policy, clock, stub):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    rotate_batch(db, plan.batch_id, clock, policy)
    verify_all(db, clock, policy, batch_id=plan.batch_id, base_url=stub.base_url)
    return plan


def test_report_json_and_markdown_are_deterministic(
    db, registry, policy, clock, stub
):
    plan = _worked_batch(db, registry, policy, clock, stub)
    first = build_report(db, clock, policy, plan.batch_id)
    second = build_report(db, clock, policy, plan.batch_id)
    assert render_json(first) == render_json(second)
    assert render_markdown(first) == render_markdown(second)
    assert first["batch"]["batch_id"] == plan.batch_id
    assert first["health"]["conclusion"] == "degraded"


def test_report_contains_required_sections(db, registry, policy, clock, stub):
    plan = _worked_batch(db, registry, policy, clock, stub)
    markdown = render_markdown(build_report(db, clock, policy, plan.batch_id))
    assert "# credrot report" in markdown
    assert "## Credential overview" in markdown
    assert "## Rotation batch" in markdown
    assert "## Health matrix" in markdown
    assert "## Task resolvability" in markdown
    assert "## Failures" in markdown
    assert "CRED-GAMMA-SPARE-01" in markdown


def test_unknown_batch_raises_report_error(db, policy, clock):
    with pytest.raises(ReportError):
        build_report(db, clock, policy, "BATCH-NOPE")


def test_archive_snapshot_contains_batch_items_and_audit(
    db, registry, policy, clock, stub
):
    plan = _worked_batch(db, registry, policy, clock, stub)
    snapshot = archive_batch(db, clock, plan.batch_id)
    assert snapshot["batch"]["batch_id"] == plan.batch_id
    assert len(snapshot["rotation_items"]) == len(plan.items)
    assert len(snapshot["verifications"]) == 9
    assert any(event["action"] == "rotation.completed" for event in snapshot["audit_events"])


def test_plan_markdown_export(registry, policy, clock):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    markdown = plan_to_markdown(plan)
    assert plan.batch_id in markdown
    assert "CRITICAL_EXPIRY" in markdown
    assert "| credential |" in markdown


def test_report_task_summary_reflects_stale_softening(db, policy, clock):
    softening = replace(
        policy, max_verification_age_minutes=60, soften_stale_for_roles=("reader",)
    )
    stale_at = clock.now() - timedelta(hours=2)
    with db.transaction() as conn:
        for credential_id in ("CRED-ALPHA-READER-01", "CRED-ALPHA-READER-02"):
            insert_verification(
                conn,
                Verification(
                    credential_id=credential_id, outcome="valid", code="VALID",
                    checked_at=stale_at,
                ),
            )
    report = build_report(db, clock, softening)
    summary = {t["task_id"]: t for t in report["tasks"]}["TASK-ALPHA-DAILY"]
    assert summary["ok"] is True and summary["degraded"] is True
    assert summary["resolved"]["reader"]["softened"] is True
    detail = {t["task_id"]: t for t in report["task_details"]}["TASK-ALPHA-DAILY"]
    assert "STALE_SOFTENED" in {row["reason"] for row in detail["attempts"]}
    assert all(row["verification_age_minutes"] == 120.0 for row in detail["attempts"])
    assert "CRED-ALPHA-READER-01 (softened)" in render_markdown(report)
    assert render_json(report) == render_json(build_report(db, clock, softening))
