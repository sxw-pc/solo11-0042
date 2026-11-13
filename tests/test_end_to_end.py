from pathlib import Path

from credrot.db import Database
from credrot.planner import build_plan
from credrot.report import build_report, render_json, render_markdown
from credrot.resolver import resolve
from credrot.rotator import rotate_batch
from credrot.store import load_credentials, save_plan, seed_registry
from credrot.verifier import verify_all


def _prepare_database(path, registry, policy, clock):
    db = Database(path)
    db.initialize()
    seed_registry(db, registry.credentials, registry.tasks, clock.now())
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    rotation = rotate_batch(db, plan.batch_id, clock, policy)
    return db, plan, rotation


def test_full_offline_lifecycle(db, registry, policy, clock, stub):
    plan = build_plan(registry.credentials, registry.tasks, policy, clock.now())
    save_plan(db, plan)
    rotation = rotate_batch(db, plan.batch_id, clock, policy)
    assert rotation.status == "completed" and rotation.executed == 3
    verification = verify_all(
        db, clock, policy, batch_id=plan.batch_id, base_url=stub.base_url
    )
    assert verification["counts"]["valid"] == 8
    assert verification["counts"]["invalid"] == 1

    alpha = resolve(db, "TASK-ALPHA-DAILY", clock, policy)
    beta = resolve(db, "TASK-BETA-DAILY", clock, policy)
    gamma = resolve(db, "TASK-GAMMA-AUDIT", clock, policy)
    assert alpha["ok"] and beta["ok"]
    assert gamma["ok"] is False and gamma["code"] == "NO_HEALTHY_CANDIDATE"

    report = build_report(db, clock, policy, plan.batch_id)
    assert report["batch"]["batch_id"] == plan.batch_id
    assert report["batch"]["status"] == "completed"
    assert report["health"]["conclusion"] == "degraded"
    assert report["credentials"][0]["version"] == 4
    task_map = {task["task_id"]: task for task in report["tasks"]}
    assert task_map["TASK-ALPHA-DAILY"]["ok"] is True
    assert task_map["TASK-GAMMA-AUDIT"]["ok"] is False
    assert report["failures"]

    json_text = render_json(report)
    markdown = render_markdown(report)
    assert render_json(build_report(db, clock, policy, plan.batch_id)) == json_text
    assert render_markdown(build_report(db, clock, policy, plan.batch_id)) == markdown
    assert plan.batch_id in markdown


def test_two_identical_inputs_produce_identical_reports(
    tmp_path: Path, registry, policy, clock, stub
):
    db_a, plan_a, _ = _prepare_database(
        tmp_path / "a.db", registry, policy, clock
    )
    db_b, plan_b, _ = _prepare_database(
        tmp_path / "b.db", registry, policy, clock
    )
    assert plan_a.batch_id == plan_b.batch_id
    verify_all(db_a, clock, policy, batch_id=plan_a.batch_id, base_url=stub.base_url)
    verify_all(db_b, clock, policy, batch_id=plan_b.batch_id, base_url=stub.base_url)
    report_a = build_report(db_a, clock, policy, plan_a.batch_id)
    report_b = build_report(db_b, clock, policy, plan_b.batch_id)
    assert render_json(report_a) == render_json(report_b)
    assert render_markdown(report_a) == render_markdown(report_b)
    assert load_credentials(db_a)["CRED-ALPHA-READER-01"].version == 4
