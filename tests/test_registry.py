from pathlib import Path

import yaml

from credrot.registry import compute_fingerprint, load_registry


def _documents(fixtures_dir: Path):
    return (
        yaml.safe_load((fixtures_dir / "credentials.yaml").read_text()),
        yaml.safe_load((fixtures_dir / "tasks.yaml").read_text()),
        yaml.safe_load((fixtures_dir / "policy.yaml").read_text()),
    )


def _load(tmp_path: Path, credentials, tasks, policy):
    for name, data in (
        ("credentials.yaml", credentials),
        ("tasks.yaml", tasks),
        ("policy.yaml", policy),
    ):
        (tmp_path / name).write_text(yaml.safe_dump(data), encoding="utf-8")
    return load_registry(
        tmp_path / "credentials.yaml",
        tmp_path / "tasks.yaml",
        tmp_path / "policy.yaml",
    )


def test_demo_registry_is_valid(registry):
    assert registry.valid
    assert len(registry.credentials) == 10
    assert len(registry.tasks) == 5
    assert compute_fingerprint("SYNTH-DS-ALPHA-reader-01").startswith("sha256:")


def test_duplicate_credential_id_is_collected(fixtures_dir, tmp_path):
    credentials, tasks, policy = _documents(fixtures_dir)
    credentials["credentials"].append(dict(credentials["credentials"][0]))
    result = _load(tmp_path, credentials, tasks, policy)
    assert any(issue.code == "DUPLICATE_CREDENTIAL" for issue in result.issues)


def test_fingerprint_mismatch_invalidates_registry(fixtures_dir, tmp_path):
    credentials, tasks, policy = _documents(fixtures_dir)
    credentials["credentials"][0]["fingerprint"] = "sha256:" + "0" * 64
    result = _load(tmp_path, credentials, tasks, policy)
    assert not result.valid
    assert any(issue.code == "FINGERPRINT_MISMATCH" for issue in result.issues)


def test_unknown_binding_and_role_mismatch(fixtures_dir, tmp_path):
    credentials, tasks, policy = _documents(fixtures_dir)
    tasks["tasks"][0]["bindings"].append("CRED-DOES-NOT-EXIST")
    tasks["tasks"][1]["bindings"].append("CRED-ALPHA-READER-01")
    result = _load(tmp_path, credentials, tasks, policy)
    codes = {issue.code for issue in result.issues}
    assert "UNKNOWN_BINDING" in codes
    assert "BINDING_SOURCE" in codes or "BINDING_ROLE" in codes


def test_bad_yaml_returns_issues_instead_of_raising(fixtures_dir, tmp_path):
    (tmp_path / "credentials.yaml").write_text("credentials: [unclosed", encoding="utf-8")
    (tmp_path / "tasks.yaml").write_text(
        (fixtures_dir / "tasks.yaml").read_text(), encoding="utf-8"
    )
    (tmp_path / "policy.yaml").write_text(
        (fixtures_dir / "policy.yaml").read_text(), encoding="utf-8"
    )
    result = load_registry(
        tmp_path / "credentials.yaml", tmp_path / "tasks.yaml", tmp_path / "policy.yaml"
    )
    assert result.credentials == {}
    assert any(issue.code == "YAML_LOAD" for issue in result.issues)
