from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

from credrot.clock import FixedClock
from credrot.config import Policy, load_policy
from credrot.db import Database
from credrot.registry import Registry, load_demo_registry
from credrot.store import seed_registry
from credrot.stubs.verify_stub import StubServer, load_stub_rules


FIXTURES = ROOT / "fixtures"
NOW = "2025-01-01T00:00:00Z"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def policy() -> Policy:
    value, issues = load_policy(FIXTURES / "policy.yaml")
    assert not issues
    return value


@pytest.fixture
def registry() -> Registry:
    value = load_demo_registry(FIXTURES)
    assert value.valid, [issue.render() for issue in value.issues]
    return value


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(NOW)


@pytest.fixture
def db(tmp_path: Path, registry: Registry, clock: FixedClock) -> Database:
    database = Database(tmp_path / "credrot.db")
    database.initialize()
    seed_registry(database, registry.credentials, registry.tasks, clock.now())
    return database


@pytest.fixture
def stub_rules():
    return load_stub_rules(FIXTURES / "verify_stub_rules.yaml")


@pytest.fixture
def stub(stub_rules):
    server = StubServer(stub_rules).start()
    try:
        yield server
    finally:
        server.stop()
