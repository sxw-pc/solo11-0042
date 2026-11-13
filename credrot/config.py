"""YAML loading and validation for credrot policy and fixture locations."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    """Raised for an unreadable or structurally invalid YAML document."""


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str

    def render(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


def load_yaml(path: Path) -> Any:
    """Load a YAML file without ever fetching anything from the network."""
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            return yaml.safe_load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc


def default_fixtures_dir() -> Path:
    return PROJECT_ROOT / "fixtures"


def default_db_path() -> Path:
    return PROJECT_ROOT / ".credrot" / "credrot.db"


@dataclass(frozen=True)
class Policy:
    horizon_days: int = 14
    critical_horizon_days: int = 3
    rotation_period_days: int = 90
    max_items_per_plan: int = 20
    timeout_seconds: float = 1.0
    stub_host: str = "127.0.0.1"
    stub_port: int = 8777
    max_verification_age_minutes: int = 1440
    allow_unverified: bool = False
    default_backup_margin: int = 1

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> tuple["Policy", list[Issue]]:
        issues: list[Issue] = []
        if not isinstance(data, Mapping):
            return cls(), [Issue("POLICY_SHAPE", "policy", "policy root must be a mapping")]

        rotation = _section(data, "rotation", issues)
        verification = _section(data, "verification", issues)
        stub = _section(data, "stub", issues)
        resolver = _section(data, "resolver", issues)
        tasks = _section(data, "tasks", issues)

        horizon = _as_int(rotation, "horizon_days", 14, "policy.rotation", issues)
        critical = _as_int(
            rotation, "critical_horizon_days", 3, "policy.rotation", issues
        )
        period = _as_int(
            rotation, "period_days", 90, "policy.rotation", issues
        )
        max_items = _as_int(
            rotation, "max_items_per_plan", 20, "policy.rotation", issues
        )
        timeout = _as_float(
            verification, "timeout_seconds", 1.0, "policy.verification", issues
        )
        host = str(stub.get("host", "127.0.0.1"))
        port = _as_int(stub, "port", 8777, "policy.stub", issues)
        max_age = _as_int(
            resolver,
            "max_verification_age_minutes",
            1440,
            "policy.resolver",
            issues,
        )
        allow_unverified = _as_bool(
            resolver, "allow_unverified", False, "policy.resolver", issues
        )
        backup_margin = _as_int(
            tasks, "default_backup_margin", 1, "policy.tasks", issues
        )

        if horizon < 0:
            issues.append(Issue("POLICY_RANGE", "policy.rotation.horizon_days", "must be >= 0"))
        if critical < 0 or critical > horizon:
            issues.append(
                Issue(
                    "POLICY_RANGE",
                    "policy.rotation.critical_horizon_days",
                    "must be between 0 and horizon_days",
                )
            )
        if period <= 0:
            issues.append(Issue("POLICY_RANGE", "policy.rotation.period_days", "must be > 0"))
        if not 1 <= max_items <= 1000:
            issues.append(
                Issue("POLICY_RANGE", "policy.rotation.max_items_per_plan", "must be 1..1000")
            )
        if timeout <= 0:
            issues.append(
                Issue("POLICY_RANGE", "policy.verification.timeout_seconds", "must be > 0")
            )
        if host != "127.0.0.1":
            issues.append(
                Issue("STUB_HOST", "policy.stub.host", "stub must bind 127.0.0.1 only")
            )
            host = "127.0.0.1"
        if not 1 <= port <= 65535:
            issues.append(Issue("POLICY_RANGE", "policy.stub.port", "must be 1..65535"))
        if max_age <= 0:
            issues.append(
                Issue(
                    "POLICY_RANGE",
                    "policy.resolver.max_verification_age_minutes",
                    "must be > 0",
                )
            )
        if backup_margin < 0:
            issues.append(
                Issue("POLICY_RANGE", "policy.tasks.default_backup_margin", "must be >= 0")
            )

        policy = cls(
            horizon_days=horizon,
            critical_horizon_days=critical,
            rotation_period_days=period,
            max_items_per_plan=max_items,
            timeout_seconds=timeout,
            stub_host=host,
            stub_port=port,
            max_verification_age_minutes=max_age,
            allow_unverified=allow_unverified,
            default_backup_margin=backup_margin,
        )
        return policy, issues


def load_policy(path: Path) -> tuple[Policy, list[Issue]]:
    try:
        data = load_yaml(path)
    except ConfigError as exc:
        return Policy(), [Issue("POLICY_IO", str(path), str(exc))]
    if data is None:
        return Policy(), [Issue("POLICY_SHAPE", str(path), "policy file is empty")]
    return Policy.from_mapping(data)


def _section(data: Mapping[str, Any], key: str, issues: list[Issue]) -> Mapping[str, Any]:
    value = data.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        issues.append(Issue("POLICY_SHAPE", f"policy.{key}", "must be a mapping"))
        return {}
    return value


def _as_int(
    data: Mapping[str, Any], key: str, default: int, path: str, issues: list[Issue]
) -> int:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(Issue("POLICY_TYPE", f"{path}.{key}", "must be an integer"))
        return default
    return value


def _as_float(
    data: Mapping[str, Any], key: str, default: float, path: str, issues: list[Issue]
) -> float:
    value = data.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(Issue("POLICY_TYPE", f"{path}.{key}", "must be a number"))
        return default
    return float(value)


def _as_bool(
    data: Mapping[str, Any], key: str, default: bool, path: str, issues: list[Issue]
) -> bool:
    value = data.get(key, default)
    if not isinstance(value, bool):
        issues.append(Issue("POLICY_TYPE", f"{path}.{key}", "must be a boolean"))
        return default
    return value
