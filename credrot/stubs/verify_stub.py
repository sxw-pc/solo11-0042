"""A deliberately dumb loopback credential-verification stub."""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Mapping

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

from ..config import ConfigError, load_yaml


OUTCOMES = {"valid", "invalid", "unknown"}
CODES = {"valid": "VALID", "invalid": "INVALID", "unknown": "UNKNOWN"}
LOOPBACK = "127.0.0.1"


def load_stub_rules(path: Path) -> dict[str, Any]:
    try:
        data = load_yaml(Path(path))
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    if not isinstance(data, Mapping):
        raise ValueError("stub rules root must be a mapping")
    default = data.get("default_outcome", "unknown")
    if default not in OUTCOMES:
        raise ValueError("default_outcome must be valid, invalid or unknown")
    delay = data.get("default_delay_ms", 0)
    if isinstance(delay, bool) or not isinstance(delay, int) or delay < 0:
        raise ValueError("default_delay_ms must be a non-negative integer")
    rules: dict[str, dict[str, Any]] = {}
    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise ValueError("rules must be a list")
    for index, raw in enumerate(raw_rules):
        if not isinstance(raw, Mapping):
            raise ValueError(f"rules[{index}] must be a mapping")
        fingerprint = raw.get("fingerprint")
        outcome = raw.get("outcome", default)
        if not isinstance(fingerprint, str) or not fingerprint.startswith("sha256:"):
            raise ValueError(f"rules[{index}].fingerprint must be a sha256 string")
        if outcome not in OUTCOMES:
            raise ValueError(f"rules[{index}].outcome is invalid")
        rule_delay = raw.get("delay_ms", delay)
        if (
            isinstance(rule_delay, bool)
            or not isinstance(rule_delay, int)
            or rule_delay < 0
        ):
            raise ValueError(f"rules[{index}].delay_ms must be non-negative")
        rules[fingerprint] = {"outcome": outcome, "delay_ms": rule_delay}
    return {
        "default_outcome": default,
        "default_delay_ms": delay,
        "rules": rules,
    }


def create_stub_app(rules: Mapping[str, Any]) -> Flask:
    app = Flask(__name__)
    app.config["JSON_SORT_KEYS"] = False

    @app.get("/healthz")
    def healthz() -> Any:
        return jsonify({"ok": True, "service": "credrot-verify-stub"})

    @app.post("/verify")
    def verify() -> Any:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"error": "JSON object required"}), 400
        fingerprint = payload.get("fingerprint")
        if not isinstance(fingerprint, str):
            return jsonify({"error": "fingerprint is required"}), 400
        rule = rules.get("rules", {}).get(fingerprint)
        if rule is None:
            outcome = rules.get("default_outcome", "unknown")
            delay_ms = rules.get("default_delay_ms", 0)
        else:
            outcome = rule["outcome"]
            delay_ms = rule["delay_ms"]
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        return jsonify(
            {
                "outcome": outcome,
                "code": CODES[outcome],
                "stub": "loopback",
            }
        )

    return app


class StubServer:
    """Run the stub in a background thread on an ephemeral loopback port."""

    def __init__(self, rules: Mapping[str, Any], port: int = 0):
        self.rules = rules
        self.port = port
        self._server: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> "StubServer":
        app = create_stub_app(self.rules)
        self._server = make_server(LOOPBACK, self.port, app, threaded=True)
        self.port = self._server.server_port
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="credrot-stub", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://{LOOPBACK}:{self.port}"

    def __enter__(self) -> "StubServer":
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()


def run_stub(port: int, rules_path: Path) -> None:
    if port < 1 or port > 65535:
        raise ValueError("port must be in 1..65535")
    rules = load_stub_rules(rules_path)
    app = create_stub_app(rules)
    server = make_server(LOOPBACK, port, app, threaded=True)
    print(f"verify stub listening on http://{LOOPBACK}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
