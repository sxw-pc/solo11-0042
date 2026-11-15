from pathlib import Path

import pytest

from credrot.db import Database
from credrot.store import load_credentials, load_tasks, seed_registry, table_counts
from credrot.verification_store import latest_verifications


def test_schema_user_version_and_wal(tmp_path: Path, registry, clock):
    db = Database(tmp_path / "ledger.db")
    db.initialize()
    conn = db.connect()
    try:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        conn.close()
    assert version == 2
    assert journal.lower() == "wal"
    assert {"credentials", "tasks", "audit_events", "rotation_batches"} <= tables


def test_transaction_rolls_back(tmp_path: Path):
    db = Database(tmp_path / "tx.db")
    db.initialize()
    with pytest.raises(RuntimeError):
        with db.transaction() as conn:
            conn.execute(
                """INSERT INTO credentials
                (id, source, role, version, synthetic_ref, fingerprint,
                 issued_at, expires_at, status, created_at, updated_at)
                VALUES ('CRED-X', 'DS-X', 'reader', 1, 'SYNTH-DS-X-reader-01',
                        'sha256:x', '2025-01-01T00:00:00Z',
                        '2025-02-01T00:00:00Z', 'active',
                        '2025-01-01T00:00:00Z', '2025-01-01T00:00:00Z')"""
            )
            raise RuntimeError("abort")
    assert table_counts(db)["credentials"] == 0


def test_seed_and_load_round_trip(db, registry):
    credentials = load_credentials(db)
    tasks = load_tasks(db)
    assert len(credentials) == 10
    assert len(tasks) == 5
    assert tasks["TASK-ALPHA-DAILY"].bindings == (
        "CRED-ALPHA-READER-01",
        "CRED-ALPHA-READER-02",
    )
    assert credentials["CRED-BETA-READER-01"].version == 5
    assert latest_verifications(db) == {}
    assert table_counts(db)["tasks"] == 5


def test_seed_is_destructive_and_repeatable(db, registry, clock):
    seed_registry(db, registry.credentials, registry.tasks, clock.now())
    assert table_counts(db)["credentials"] == 10
    assert table_counts(db)["tasks"] == 5
