"""Loopback-only credential verification and aggregate health conclusions."""

from __future__ import annotations

import time
from typing import Any, Mapping
from urllib.parse import urlsplit

import requests

from .audit import AuditLog
from .clock import Clock, format_time
from .config import Policy
from .db import Database
from .model import Credential, USABLE_STATES, Verification
from .store import load_credentials
from .verification_store import insert_verification


LOOPBACK = "127.0.0.1"
DEFAULT_CODES = {"valid": "VALID", "invalid": "INVALID", "unknown": "UNKNOWN"}


class LoopbackViolation(ValueError):
    """Raised before a request if a URL does not point at 127.0.0.1."""


def validate_loopback_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "http":
        raise LoopbackViolation("verification URL must use http")
    if parsed.hostname != LOOPBACK:
        raise LoopbackViolation("verification URL must target 127.0.0.1")
    if parsed.username or parsed.password:
        raise LoopbackViolation("verification URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise LoopbackViolation("verification URL must not contain query or fragment")
    return base_url.rstrip("/")


class VerificationClient:
    def __init__(
        self, base_url: str, timeout_seconds: float, session: Any = None
    ) -> None:
        self.base_url = validate_loopback_url(base_url)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout must be positive")
        if session is None:
            session = requests.Session()
            session.trust_env = False
        self.session = session

    def check(
        self, credential: Credential, checked_at: Any, batch_id: str | None = None
    ) -> Verification:
        started = time.monotonic()
        try:
            response = self.session.post(
                f"{self.base_url}/verify",
                json={
                    "credential_id": credential.id,
                    "fingerprint": credential.fingerprint,
                },
                timeout=self.timeout_seconds,
            )
        except requests.ConnectTimeout:
            return self._error(
                credential, "STUB_UNREACHABLE", checked_at, batch_id, started
            )
        except requests.Timeout:
            return self._error(credential, "TIMEOUT", checked_at, batch_id, started)
        except requests.ConnectionError:
            return self._error(
                credential, "STUB_UNREACHABLE", checked_at, batch_id, started
            )
        except requests.RequestException:
            return self._error(
                credential, "STUB_REQUEST_ERROR", checked_at, batch_id, started
            )

        if response.status_code != 200:
            return self._error(
                credential,
                f"STUB_HTTP_{response.status_code}",
                checked_at,
                batch_id,
                started,
            )
        try:
            payload = response.json()
        except ValueError:
            return self._error(
                credential, "STUB_BAD_JSON", checked_at, batch_id, started
            )
        outcome = payload.get("outcome") if isinstance(payload, Mapping) else None
        if outcome not in DEFAULT_CODES:
            return self._error(
                credential, "STUB_BAD_OUTCOME", checked_at, batch_id, started
            )
        code = payload.get("code", DEFAULT_CODES[outcome])
        latency = int((time.monotonic() - started) * 1000)
        return Verification(
            credential_id=credential.id,
            outcome=outcome,
            code=str(code),
            checked_at=checked_at,
            batch_id=batch_id,
            latency_ms=latency,
        )

    def _error(
        self, credential: Credential, code: str, checked_at: Any,
        batch_id: str | None, started: float,
    ) -> Verification:
        latency = int((time.monotonic() - started) * 1000)
        return Verification(
            credential_id=credential.id,
            outcome="error",
            code=code,
            checked_at=checked_at,
            batch_id=batch_id,
            latency_ms=latency,
        )


def conclude(outcomes: list[str]) -> str:
    if not outcomes:
        return "degraded"
    valid = sum(1 for outcome in outcomes if outcome == "valid")
    if valid == len(outcomes):
        return "healthy"
    if valid == 0:
        return "failing"
    return "degraded"


def verify_all(
    db: Database,
    clock: Clock,
    policy: Policy,
    batch_id: str | None = None,
    base_url: str | None = None,
    client: VerificationClient | None = None,
) -> dict[str, Any]:
    """Check active and standby credentials and persist deterministic results."""
    now = clock.now()
    url = base_url or f"http://{policy.stub_host}:{policy.stub_port}"
    verifier = client or VerificationClient(url, policy.timeout_seconds)
    credentials = [
        credential
        for credential in load_credentials(db).values()
        if credential.effective_state(now) in USABLE_STATES
    ]
    audit = AuditLog(db, clock)
    results: list[Verification] = []
    with db.transaction() as conn:
        for credential in sorted(credentials, key=lambda item: item.id):
            result = verifier.check(credential, now, batch_id=batch_id)
            insert_verification(conn, result)
            audit.record(
                actor="verifier",
                action="verification.checked",
                subject=credential.id,
                before={"outcome": "unverified"},
                after={"outcome": result.outcome, "code": result.code},
                batch_id=batch_id,
                reason="loopback stub verdict",
                at=now,
                conn=conn,
            )
            results.append(result)

    outcomes = [result.outcome for result in results]
    conclusion = conclude(outcomes)
    counts = {
        outcome: outcomes.count(outcome)
        for outcome in ("valid", "invalid", "unknown", "error")
    }
    audit.record(
        actor="verifier",
        action="verification.batch",
        subject=batch_id or "ALL",
        before={},
        after={"conclusion": conclusion, "counts": counts},
        batch_id=batch_id,
        reason="verification batch completed",
        at=now,
    )
    return {
        "batch_id": batch_id,
        "checked_at": format_time(now),
        "conclusion": conclusion,
        "counts": counts,
        "total": len(results),
        "results": [result.to_dict() for result in results],
    }
