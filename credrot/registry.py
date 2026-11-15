"""Load and validate synthetic credential/task fixtures into domain objects."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .clock import ClockError, parse_time
from .config import ConfigError, Issue, Policy, load_policy, load_yaml
from .model import Credential, ROLES, STATUSES, Task


ID_PATTERN = re.compile(r"^CRED-[A-Z0-9][A-Z0-9-]*$")
TASK_PATTERN = re.compile(r"^TASK-[A-Z0-9][A-Z0-9-]*$")
SOURCE_PATTERN = re.compile(r"^DS-[A-Z0-9][A-Z0-9-]*$")
FP_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True)
class Registry:
    credentials: dict[str, Credential]
    tasks: dict[str, Task]
    policy: Policy
    issues: list[Issue]

    @property
    def valid(self) -> bool:
        return not self.issues


def compute_fingerprint(synthetic_ref: str) -> str:
    return "sha256:" + hashlib.sha256(synthetic_ref.encode("utf-8")).hexdigest()


def load_registry(
    credentials_path: Path,
    tasks_path: Path,
    policy_path: Path,
) -> Registry:
    """Load all three fixture files, accumulating errors instead of raising."""
    issues: list[Issue] = []
    policy, policy_issues = load_policy(policy_path)
    issues.extend(policy_issues)

    credentials: dict[str, Credential] = {}
    for index, raw in enumerate(_list_root(credentials_path, "credentials", issues)):
        credential = _parse_credential(raw, index, issues)
        if credential is None:
            continue
        if credential.id in credentials:
            issues.append(
                Issue("DUPLICATE_CREDENTIAL", f"credentials[{index}].id", credential.id)
            )
            continue
        credentials[credential.id] = credential

    tasks: dict[str, Task] = {}
    for index, raw in enumerate(_list_root(tasks_path, "tasks", issues)):
        task = _parse_task(raw, index, policy, credentials, issues)
        if task is None:
            continue
        if task.id in tasks:
            issues.append(Issue("DUPLICATE_TASK", f"tasks[{index}].id", task.id))
            continue
        tasks[task.id] = task

    return Registry(credentials, tasks, policy, issues)


def load_demo_registry(fixtures_dir: Path) -> Registry:
    base = Path(fixtures_dir)
    return load_registry(
        base / "credentials.yaml",
        base / "tasks.yaml",
        base / "policy.yaml",
    )


def _list_root(path: Path, key: str, issues: list[Issue]) -> list[Mapping[str, Any]]:
    try:
        data = load_yaml(path)
    except ConfigError as exc:
        issues.append(Issue("YAML_LOAD", str(path), str(exc)))
        return []
    if not isinstance(data, Mapping) or not isinstance(data.get(key), list):
        issues.append(
            Issue("YAML_SHAPE", str(path), f"expected a mapping with a '{key}' list")
        )
        return []
    rows = data[key]
    valid: list[Mapping[str, Any]] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            issues.append(Issue("YAML_SHAPE", f"{key}[{index}]", "must be a mapping"))
            continue
        valid.append(row)
    return valid


def _parse_credential(
    raw: Mapping[str, Any], index: int, issues: list[Issue]
) -> Credential | None:
    path = f"credentials[{index}]"
    required = (
        "id",
        "source",
        "role",
        "version",
        "seq",
        "synthetic_ref",
        "fingerprint",
        "issued_at",
        "expires_at",
        "status",
    )
    missing = [key for key in required if key not in raw]
    if missing:
        issues.append(Issue("MISSING_FIELD", path, "missing " + ", ".join(missing)))
        return None

    credential_id = str(raw["id"])
    source = str(raw["source"])
    role = str(raw["role"])
    status = str(raw["status"])
    version = raw["version"]
    seq = raw["seq"]
    synthetic_ref = str(raw["synthetic_ref"])
    fingerprint = str(raw["fingerprint"])

    if not ID_PATTERN.match(credential_id):
        issues.append(Issue("BAD_ID", f"{path}.id", "expected CRED-* uppercase id"))
    if not SOURCE_PATTERN.match(source):
        issues.append(Issue("BAD_SOURCE", f"{path}.source", "expected DS-* source id"))
    if role not in ROLES:
        issues.append(Issue("BAD_ROLE", f"{path}.role", f"expected one of {ROLES}"))
    if status not in STATUSES:
        issues.append(Issue("BAD_STATUS", f"{path}.status", f"expected one of {STATUSES}"))
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        issues.append(Issue("BAD_VERSION", f"{path}.version", "must be an integer >= 1"))
        version = 1
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        issues.append(Issue("BAD_SEQ", f"{path}.seq", "must be an integer >= 1"))
        seq = 1

    expected_ref = f"SYNTH-{source}-{role}-{seq:02d}"
    if synthetic_ref != expected_ref:
        issues.append(
            Issue(
                "SYNTHETIC_REF_MISMATCH",
                f"{path}.synthetic_ref",
                f"expected {expected_ref}",
            )
        )
    if not FP_PATTERN.match(fingerprint):
        issues.append(
            Issue("BAD_FINGERPRINT", f"{path}.fingerprint", "expected sha256:<64 hex>")
        )
    elif fingerprint != compute_fingerprint(synthetic_ref):
        issues.append(
            Issue(
                "FINGERPRINT_MISMATCH",
                f"{path}.fingerprint",
                "does not match sha256(synthetic_ref)",
            )
        )

    try:
        issued_at = parse_time(raw["issued_at"])
        expires_at = parse_time(raw["expires_at"])
    except ClockError as exc:
        issues.append(Issue("BAD_TIMESTAMP", path, str(exc)))
        return None
    if issued_at >= expires_at:
        issues.append(
            Issue("BAD_VALIDITY", path, "issued_at must be earlier than expires_at")
        )

    return Credential(
        id=credential_id,
        source=source,
        role=role,
        version=version,
        synthetic_ref=synthetic_ref,
        fingerprint=fingerprint,
        issued_at=issued_at,
        expires_at=expires_at,
        status=status,
    )


def _parse_task(
    raw: Mapping[str, Any],
    index: int,
    policy: Policy,
    credentials: Mapping[str, Credential],
    issues: list[Issue],
) -> Task | None:
    path = f"tasks[{index}]"
    required = ("id", "source", "priority", "required_roles", "min_active", "bindings")
    missing = [key for key in required if key not in raw]
    if missing:
        issues.append(Issue("MISSING_FIELD", path, "missing " + ", ".join(missing)))
        return None

    task_id = str(raw["id"])
    source = str(raw["source"])
    priority = raw["priority"]
    min_active = raw["min_active"]
    backup_margin = raw.get("backup_margin", policy.default_backup_margin)
    roles_raw = raw["required_roles"]
    bindings_raw = raw["bindings"]

    if not TASK_PATTERN.match(task_id):
        issues.append(Issue("BAD_ID", f"{path}.id", "expected TASK-* uppercase id"))
    if not SOURCE_PATTERN.match(source):
        issues.append(Issue("BAD_SOURCE", f"{path}.source", "expected DS-* source id"))
    if isinstance(priority, bool) or not isinstance(priority, int) or priority < 0:
        issues.append(Issue("BAD_PRIORITY", f"{path}.priority", "must be integer >= 0"))
        priority = 0
    if isinstance(min_active, bool) or not isinstance(min_active, int) or min_active < 0:
        issues.append(Issue("BAD_MIN_ACTIVE", f"{path}.min_active", "must be integer >= 0"))
        min_active = 0
    if (
        isinstance(backup_margin, bool)
        or not isinstance(backup_margin, int)
        or backup_margin < 0
    ):
        issues.append(
            Issue("BAD_BACKUP_MARGIN", f"{path}.backup_margin", "must be integer >= 0")
        )
        backup_margin = 0

    roles: list[str] = []
    if not isinstance(roles_raw, list) or not roles_raw:
        issues.append(
            Issue("BAD_ROLES", f"{path}.required_roles", "must be a non-empty list")
        )
    else:
        for role in roles_raw:
            if role not in ROLES:
                issues.append(
                    Issue("BAD_ROLE", f"{path}.required_roles", f"unknown role {role!r}")
                )
            elif role in roles:
                issues.append(
                    Issue("DUPLICATE_ROLE", f"{path}.required_roles", f"duplicate {role}")
                )
            else:
                roles.append(role)

    bindings: list[str] = []
    if not isinstance(bindings_raw, list):
        issues.append(Issue("BAD_BINDINGS", f"{path}.bindings", "must be a list"))
    else:
        for credential_id in bindings_raw:
            credential_id = str(credential_id)
            if credential_id in bindings:
                issues.append(
                    Issue("DUPLICATE_BINDING", f"{path}.bindings", credential_id)
                )
                continue
            credential = credentials.get(credential_id)
            if credential is None:
                issues.append(
                    Issue("UNKNOWN_BINDING", f"{path}.bindings", credential_id)
                )
                continue
            if credential.source != source:
                issues.append(
                    Issue(
                        "BINDING_SOURCE",
                        f"{path}.bindings",
                        f"{credential_id} belongs to {credential.source}",
                    )
                )
            if roles and credential.role not in roles:
                issues.append(
                    Issue(
                        "BINDING_ROLE",
                        f"{path}.bindings",
                        f"{credential_id} role {credential.role} is not required",
                    )
                )
            bindings.append(credential_id)

    return Task(
        id=task_id,
        source=source,
        priority=priority,
        required_roles=tuple(roles),
        min_active=min_active,
        backup_margin=backup_margin,
        bindings=tuple(bindings),
    )
