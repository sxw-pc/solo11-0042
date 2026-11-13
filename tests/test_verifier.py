import socket

import pytest

from credrot.audit import AuditLog
from credrot.stubs.verify_stub import StubServer
from credrot.verification_store import latest_verifications
from credrot.verifier import (
    LoopbackViolation,
    VerificationClient,
    conclude,
    validate_loopback_url,
    verify_all,
)


def test_verification_url_is_loopback_only():
    assert validate_loopback_url("http://127.0.0.1:9999") == "http://127.0.0.1:9999"
    with pytest.raises(LoopbackViolation):
        VerificationClient("http://example.org:8777", 1)
    with pytest.raises(LoopbackViolation):
        VerificationClient("http://localhost:8777", 1)
    with pytest.raises(LoopbackViolation):
        VerificationClient("https://127.0.0.1:8777", 1)


def test_stub_returns_valid_invalid_and_unknown(stub, registry):
    client = VerificationClient(stub.base_url, 1.0)
    valid = client.check(registry.credentials["CRED-ALPHA-READER-01"], None)
    invalid = client.check(registry.credentials["CRED-GAMMA-SPARE-01"], None)
    unknown_credential = registry.credentials["CRED-ALPHA-READER-02"].changed(
        fingerprint="sha256:" + "1" * 64
    )
    unknown = client.check(unknown_credential, None)
    assert (valid.outcome, valid.code) == ("valid", "VALID")
    assert (invalid.outcome, invalid.code) == ("invalid", "INVALID")
    assert (unknown.outcome, unknown.code) == ("unknown", "UNKNOWN")


def test_timeout_is_mapped_to_stable_code(registry):
    credential = registry.credentials["CRED-ALPHA-READER-01"]
    rules = {
        "default_outcome": "unknown",
        "default_delay_ms": 0,
        "rules": {
            credential.fingerprint: {"outcome": "valid", "delay_ms": 300}
        },
    }
    with StubServer(rules) as server:
        client = VerificationClient(server.base_url, 0.05)
        result = client.check(credential, None)
    assert result.outcome == "error"
    assert result.code == "TIMEOUT"


def test_connection_refused_is_mapped_to_stable_code(registry):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    client = VerificationClient(f"http://127.0.0.1:{port}", 0.2)
    result = client.check(registry.credentials["CRED-ALPHA-READER-01"], None)
    assert result.outcome == "error"
    assert result.code == "STUB_UNREACHABLE"


@pytest.mark.parametrize(
    "outcomes, expected",
    [
        (["valid", "valid"], "healthy"),
        (["valid", "invalid"], "degraded"),
        (["invalid", "error"], "failing"),
        ([], "degraded"),
    ],
)
def test_health_conclusions(outcomes, expected):
    assert conclude(outcomes) == expected


def test_verify_all_persists_and_audits(db, policy, clock, stub):
    result = verify_all(db, clock, policy, batch_id="BATCH-TEST", base_url=stub.base_url)
    assert result["total"] == 9
    assert result["counts"] == {
        "valid": 8,
        "invalid": 1,
        "unknown": 0,
        "error": 0,
    }
    assert result["conclusion"] == "degraded"
    observations = latest_verifications(db)
    assert observations["CRED-GAMMA-SPARE-01"]["outcome"] == "invalid"
    assert len(observations) == 9
    events = AuditLog(db, clock).list_events(action="verification.batch")
    assert events and events[-1]["after"]["conclusion"] == "degraded"
