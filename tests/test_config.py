from pathlib import Path

import pytest

from credrot.clock import ClockError, FixedClock, days_until, parse_time
from credrot.config import ConfigError, Policy, load_policy, load_yaml


def test_policy_mapping_and_nested_values():
    policy, issues = Policy.from_mapping(
        {
            "rotation": {
                "horizon_days": 20,
                "critical_horizon_days": 2,
                "period_days": 30,
                "max_items_per_plan": 5,
            },
            "verification": {"timeout_seconds": 0.25},
            "stub": {"host": "127.0.0.1", "port": 9000},
            "resolver": {
                "max_verification_age_minutes": 60,
                "allow_unverified": True,
            },
            "tasks": {"default_backup_margin": 2},
        }
    )
    assert not issues
    assert policy.horizon_days == 20
    assert policy.timeout_seconds == 0.25
    assert policy.allow_unverified is True
    assert policy.default_backup_margin == 2


def test_policy_rejects_external_stub_and_bad_ranges():
    policy, issues = Policy.from_mapping(
        {
            "rotation": {"horizon_days": 2, "critical_horizon_days": 5},
            "verification": {"timeout_seconds": "fast"},
            "stub": {"host": "example.org"},
        }
    )
    codes = {issue.code for issue in issues}
    assert "STUB_HOST" in codes
    assert "POLICY_TYPE" in codes
    assert "POLICY_RANGE" in codes
    assert policy.stub_host == "127.0.0.1"


def test_missing_policy_is_reported_not_raised(tmp_path: Path):
    policy, issues = load_policy(tmp_path / "absent.yaml")
    assert policy.horizon_days == 14
    assert issues and issues[0].code == "POLICY_IO"


def test_invalid_yaml_raises_config_error(tmp_path: Path):
    path = tmp_path / "broken.yaml"
    path.write_text("rotation: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_yaml(path)


def test_fixed_clock_and_day_boundary_math():
    now = FixedClock("2025-01-01T00:00:00Z").now()
    assert parse_time("2025-01-01T00:00:00Z") == now
    assert days_until(parse_time("2025-01-02T00:00:00Z"), now) == 1
    assert days_until(parse_time("2024-12-31T23:59:59Z"), now) == -1
    with pytest.raises(ClockError):
        parse_time("not-a-time")
