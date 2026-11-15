"""Rotation must persist registry-consistent identities for renewed credentials."""

import shutil
from datetime import timedelta

import pytest
import yaml

from credrot import model
from credrot.__main__ import main as cli_main
from credrot.audit import AuditLog
from credrot.clock import format_time, parse_time
from credrot.db import Database
from credrot.model import Credential, next_available_seq, synthetic_ref_for
from credrot.planner import build_plan
from credrot.registry import compute_fingerprint, load_registry
from credrot.rotator import rotate_batch
from credrot.store import (
    get_batch,
    list_batches,
    list_rotation_items,
    load_credentials,
    save_plan,
    seed_registry,
)


NOW = "2025-01-01T00:00:00Z"


def _cred(cid, source, role, seq, status, expires, version=1):
    ref = synthetic_ref_for(source, role, seq)
    return Credential(
        id=cid, source=source, role=role, version=version, synthetic_ref=ref,
        fingerprint=compute_fingerprint(ref), issued_at=parse_time("2024-01-01T00:00:00Z"),
        expires_at=parse_time(expires), status=status,
    )


def _prepare(db, credentials, tasks, policy, clock):
    plan = build_plan(credentials, tasks, policy, clock.now())
    save_plan(db, plan)
    return plan


def _reload_ledger(db, target_dir, fixtures_dir):
    """Dump the ledger as credentials.yaml and run full registry validation (回灌)."""
    target_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for cred in load_credentials(db).values():
        assert cred.seq is not None, f"{cred.id}: unparseable {cred.synthetic_ref}"
        rows.append({
            "id": cred.id, "source": cred.source, "role": cred.role,
            "version": cred.version, "seq": cred.seq,
            "synthetic_ref": cred.synthetic_ref, "fingerprint": cred.fingerprint,
            "issued_at": format_time(cred.issued_at),
            "expires_at": format_time(cred.expires_at), "status": cred.status,
        })
    (target_dir / "credentials.yaml").write_text(
        yaml.safe_dump({"credentials": rows}), encoding="utf-8")
    for name in ("tasks.yaml", "policy.yaml"):
        shutil.copy(fixtures_dir / name, target_dir / name)
    return load_registry(*(target_dir / name for name in
                           ("credentials.yaml", "tasks.yaml", "policy.yaml")))


def test_rotation_writes_registry_consistent_identity(db, registry, policy, clock,
                                                      tmp_path, fixtures_dir):
    plan = _prepare(db, registry.credentials, registry.tasks, policy, clock)
    assert rotate_batch(db, plan.batch_id, clock, policy).status == "completed"

    now = clock.now()
    creds = load_credentials(db)
    alpha, beta, spare = (creds[c] for c in (
        "CRED-ALPHA-READER-01", "CRED-BETA-READER-01", "CRED-GAMMA-SPARE-01"))
    assert alpha.version == 4 and alpha.seq == 3
    assert alpha.synthetic_ref == "SYNTH-DS-ALPHA-reader-03"
    assert alpha.fingerprint == compute_fingerprint(alpha.synthetic_ref)
    assert alpha.issued_at == now
    assert alpha.expires_at == now + timedelta(days=policy.rotation_period_days)
    assert (beta.version, beta.synthetic_ref) == (6, "SYNTH-DS-BETA-reader-03")
    assert beta.fingerprint == compute_fingerprint(beta.synthetic_ref)
    # A promote is a state change only; the identity stays put.
    assert (spare.version, spare.synthetic_ref, spare.status) == (
        1, "SYNTH-DS-GAMMA-admin-01", "active")

    events = AuditLog(db, clock).list_events(
        batch_id=plan.batch_id, action="credential.renewed")
    renewed = {event["subject"]: event for event in events}
    assert renewed["CRED-ALPHA-READER-01"]["before"]["synthetic_ref"] == (
        "SYNTH-DS-ALPHA-reader-01")
    assert renewed["CRED-ALPHA-READER-01"]["after"]["synthetic_ref"] == (
        "SYNTH-DS-ALPHA-reader-03")
    assert renewed["CRED-ALPHA-READER-01"]["after"]["fingerprint"] == (
        compute_fingerprint("SYNTH-DS-ALPHA-reader-03"))

    # The rotated ledger survives a full registry reload (回灌).
    reloaded = _reload_ledger(db, tmp_path / "reload", fixtures_dir)
    assert reloaded.valid, [issue.render() for issue in reloaded.issues]
    assert reloaded.credentials["CRED-ALPHA-READER-01"].synthetic_ref == (
        "SYNTH-DS-ALPHA-reader-03")


def test_next_available_seq_rules():
    creds = [
        _cred("CRED-A", "DS-X", "reader", 1, "active", "2030-01-01T00:00:00Z"),
        _cred("CRED-B", "DS-X", "reader", 2, "revoked", "2024-01-01T00:00:00Z"),
        _cred("CRED-C", "DS-X", "writer", 1, "active", "2030-01-01T00:00:00Z"),
    ]
    assert next_available_seq([], "DS-X", "reader") == 1
    assert next_available_seq(creds, "DS-X", "reader") == 3  # revoked keeps 2 burned
    assert next_available_seq(creds, "DS-X", "writer") == 2  # other role unaffected
    pair = [creds[0], _cred("CRED-D", "DS-X", "reader", 3, "active", "2030-01-01T00:00:00Z")]
    assert next_available_seq(pair, "DS-X", "reader") == 2  # gaps fill first
    dirty = creds[0].changed(synthetic_ref="SYNTH-not-canonical")
    assert next_available_seq([dirty], "DS-X", "reader") == 1  # malformed occupies none


def test_seq_allocation_counts_revoked_and_stays_distinct(tmp_path, policy, clock):
    creds = {
        "CRED-X-A": _cred("CRED-X-A", "DS-X", "reader", 1, "active", "2025-01-05T00:00:00Z"),
        "CRED-X-B": _cred("CRED-X-B", "DS-X", "reader", 2, "active", "2025-01-05T00:00:00Z"),
        "CRED-X-OLD": _cred("CRED-X-OLD", "DS-X", "reader", 3, "revoked", "2024-06-01T00:00:00Z"),
    }
    db = Database(tmp_path / "seq.db")
    db.initialize()
    seed_registry(db, creds, {}, clock.now())
    plan = _prepare(db, creds, {}, policy, clock)
    assert len(plan.items) == 2  # revoked records are never planned

    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "completed" and result.executed == 2
    rotated = load_credentials(db)
    first, second = rotated["CRED-X-A"], rotated["CRED-X-B"]
    # Seq 3 stays burned by the revoked record; the first rotation takes 4.
    assert first.synthetic_ref == "SYNTH-DS-X-reader-04"
    # The second then finds seq 1 free — the ref CRED-X-A just vacated.
    assert second.synthetic_ref == "SYNTH-DS-X-reader-01"
    assert first.synthetic_ref != second.synthetic_ref
    for cred in (first, second):
        assert cred.fingerprint == compute_fingerprint(cred.synthetic_ref)


DEFECTS = {
    "FINGERPRINT_MISMATCH": (
        lambda r: r.changed(fingerprint="sha256:" + "0" * 64), "FINGERPRINT_MISMATCH"),
    "VALIDITY_EXPIRES": (
        lambda r: r.changed(expires_at=r.expires_at + timedelta(days=1)),
        "VALIDITY_MISMATCH"),
    "VALIDITY_ISSUED_AT": (
        lambda r: r.changed(issued_at=r.issued_at - timedelta(seconds=1)),
        "VALIDITY_MISMATCH"),
    "SYNTHETIC_REF_MISMATCH": (
        lambda r: r.changed(synthetic_ref=r.synthetic_ref + "X"),
        "SYNTHETIC_REF_MISMATCH"),
}


@pytest.mark.parametrize("case", sorted(DEFECTS))
def test_identity_defect_aborts_and_rolls_back(db, registry, policy, clock,
                                               monkeypatch, case):
    defect, code = DEFECTS[case]
    plan = _prepare(db, registry.credentials, registry.tasks, policy, clock)
    original = model.renewed_identity
    monkeypatch.setattr(
        model, "renewed_identity", lambda *args: defect(original(*args)))

    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "failed"
    assert result.error_code == code
    assert code in (result.error or "")
    # Nothing was persisted: every credential keeps its pre-batch identity.
    beta = load_credentials(db)["CRED-BETA-READER-01"]
    assert (beta.version, beta.synthetic_ref) == (5, "SYNTH-DS-BETA-reader-01")
    assert load_credentials(db)["CRED-ALPHA-READER-01"].version == 3
    batch = get_batch(db, plan.batch_id)
    assert batch["status"] == "failed" and code in batch["failure_reason"]
    items = list_rotation_items(db, plan.batch_id)
    assert items[0]["result"] == "failed" and code in items[0]["failure_reason"]


def test_identity_conflict_aborts_and_rolls_back(db, registry, policy, clock):
    # Dirty record: its ref doesn't match its own source/role, so allocation skips it.
    intruder = _cred("CRED-INTRUDER", "DS-GAMMA", "reader", 9, "active",
                     "2030-01-01T00:00:00Z")
    intruder = intruder.changed(
        synthetic_ref="SYNTH-DS-BETA-reader-03",
        fingerprint=compute_fingerprint("SYNTH-DS-BETA-reader-03"))
    creds = {**registry.credentials, "CRED-INTRUDER": intruder}
    seed_registry(db, creds, registry.tasks, clock.now())
    plan = _prepare(db, creds, registry.tasks, policy, clock)

    result = rotate_batch(db, plan.batch_id, clock, policy)
    assert result.status == "failed"
    assert result.error_code == "IDENTITY_CONFLICT"
    assert "IDENTITY_CONFLICT" in (result.error or "")
    assert "SYNTH-DS-BETA-reader-03" in (result.error or "")
    beta = load_credentials(db)["CRED-BETA-READER-01"]
    assert (beta.version, beta.synthetic_ref) == (5, "SYNTH-DS-BETA-reader-01")
    assert get_batch(db, plan.batch_id)["status"] == "failed"


def test_completed_batch_reentry_keeps_identities(db, registry, policy, clock):
    plan = _prepare(db, registry.credentials, registry.tasks, policy, clock)
    assert rotate_batch(db, plan.batch_id, clock, policy).status == "completed"
    snapshot = {c.id: (c.version, c.synthetic_ref, c.fingerprint)
                for c in load_credentials(db).values()}

    again = rotate_batch(db, plan.batch_id, clock, policy)
    assert again.status == "completed" and again.executed == 0
    assert again.skipped == len(plan.items)
    assert all(item["result"] == "skipped" and item["ok"] for item in again.items)
    assert {c.id: (c.version, c.synthetic_ref, c.fingerprint)
            for c in load_credentials(db).values()} == snapshot


def test_cli_fixed_clock_replay_has_no_identity_issues(tmp_path, fixtures_dir):
    db_path = tmp_path / "credrot.db"
    base = ["--db", str(db_path), "--fixtures", str(fixtures_dir), "--now", NOW]
    assert cli_main(["init-demo", *base]) == 0
    assert cli_main(["plan", *base]) == 0
    batch_id = list_batches(Database(str(db_path)))[0]["batch_id"]
    assert cli_main(["rotate", "--batch", batch_id, *base]) == 0

    # The rotated ledger reloads through the registry without identity issues.
    reloaded = _reload_ledger(Database(str(db_path)), tmp_path / "reload", fixtures_dir)
    assert reloaded.valid, [issue.render() for issue in reloaded.issues]

    # Re-running init-demo on the same fixed clock stays clean.
    assert cli_main(["init-demo", *base]) == 0
