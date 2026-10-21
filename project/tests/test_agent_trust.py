from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.agent_guard import manifest as manifest_module
from core.agent_guard import seal, trust


def _keypair(tmp_path: Path):
    key = Ed25519PrivateKey.generate()
    private_path = tmp_path / "release.pem"
    public_path = tmp_path / "release.pub.pem"
    private_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    return private_path, public_path


def _policy() -> dict:
    return {
        "schema": "x5-agent-policy/1",
        "license_servers": ["https://license.example.test"],
        "license_public_key_id": "license-test",
        "repo": "X5Coder/X5Agent",
        "release_channel": "stable",
        "update_hosts": ["raw.githubusercontent.com"],
        "min_version": "0.82.0",
        "max_manifest_age_hours": 168,
        "offline_lease_hours": 72,
        "key_id": "release-test",
        "release_key_id": "release-test",
        "allowed_builds": [],
        "allowed_mroots": [],
        "capability_ttl_seconds": 300,
        "clock_rollback_tolerance_seconds": 300,
        "safe_launch": True,
        "allow_user_code_paths": False,
        "allow_pickle_models": False,
        "next_key": None,
        "dev_overrides": False,
    }


def test_policy_signature_roundtrip(tmp_path):
    private_path, public_path = _keypair(tmp_path)
    policy_path = tmp_path / "policy.json"
    signature_path = tmp_path / "policy.sig"
    policy = _policy()
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    signature_path.write_text(
        trust.sign_payload(seal.canonical_json_bytes(policy), private_path),
        encoding="ascii",
    )

    loaded = trust.load_policy(
        policy_path,
        signature_path,
        public_path,
        expected_fingerprint=None,
    )

    assert loaded["key_id"] == "release-test"
    assert trust.policy_hash(loaded) == trust.policy_hash(policy)


def test_policy_tamper_is_rejected(tmp_path):
    private_path, public_path = _keypair(tmp_path)
    policy_path = tmp_path / "policy.json"
    signature_path = tmp_path / "policy.sig"
    policy = _policy()
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    signature_path.write_text(
        trust.sign_payload(seal.canonical_json_bytes(policy), private_path),
        encoding="ascii",
    )
    policy["license_servers"] = ["https://evil.example.test"]
    policy_path.write_text(json.dumps(policy), encoding="utf-8")

    with pytest.raises(trust.TrustError):
        trust.load_policy(
            policy_path,
            signature_path,
            public_path,
            expected_fingerprint=None,
        )


def test_policy_rejects_unsafe_flags(tmp_path):
    policy = _policy()
    policy["allow_pickle_models"] = True

    with pytest.raises(trust.TrustError):
        trust.validate_policy_shape(policy)


def test_pinned_key_rejects_wrong_key(tmp_path):
    _, public_path = _keypair(tmp_path)

    with pytest.raises(trust.TrustError):
        trust.load_public_key(public_path, expected_fingerprint="0" * 64)


def test_policy_allowlist_rejects_other_build():
    policy = _policy()
    policy["allowed_builds"] = ["allowed-build"]
    manifest = {
        "policy_hash": trust.policy_hash(policy),
        "release_key_id": "release-test",
        "commit": "other-build",
        "root": "a" * 64,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    with pytest.raises(trust.TrustError):
        trust.validate_manifest_against_policy(manifest, policy)


def test_manifest_mode_is_path_based():
    assert seal._mode_for("project/scripts/x5-local") == "100755"
    assert seal._mode_for("project/scripts/setup.sh") == "100755"
    assert seal._mode_for("project/core/agent_guard/seal.py") == "100644"


def test_manifest_roundtrip_and_tamper(tmp_path):
    private_path, public_path = _keypair(tmp_path)
    repo = tmp_path / "repo"
    (repo / "project" / "core").mkdir(parents=True)
    (repo / "project" / "scripts").mkdir(parents=True)
    (repo / "project" / "prompts").mkdir(parents=True)
    (repo / "project" / "core" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "project" / "scripts" / "s.py").write_text("VALUE = 2\n", encoding="utf-8")
    (repo / "project" / "prompts" / "p.md").write_text("# Prompt\n", encoding="utf-8")
    (repo / "project" / "launch.py").write_text("print('launch')\n", encoding="utf-8")
    (repo / "project" / "main.py").write_text("print('main')\n", encoding="utf-8")
    (repo / "project" / "install.py").write_text("print('install')\n", encoding="utf-8")
    (repo / "project" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (repo / "updates.json").write_text('{"version": "0.82.0"}\n', encoding="utf-8")

    built = manifest_module.build_manifest(
        version="0.82.0",
        commit="abc123",
        release_key_id="release-test",
        policy_hash="a" * 64,
        generated_at="2026-10-21T00:00:00Z",
        root=repo,
    )
    manifest_path = repo / "project" / "agent" / "manifest.json"
    manifest_module.write_manifest(built, manifest_path)
    signature_path = repo / "project" / "agent" / "manifest.sig"
    signature_path.write_text(
        trust.sign_payload(str(built["root"]).encode("ascii"), private_path),
        encoding="ascii",
    )

    loaded = trust.load_signed_manifest(
        manifest_path,
        signature_path,
        public_path,
        expected_fingerprint=None,
    )
    manifest_module.assert_manifest(loaded, root=repo, policy_hash="a" * 64)

    (repo / "project" / "core" / "a.py").write_text("VALUE = 99\n", encoding="utf-8")
    result = manifest_module.verify_manifest(loaded, root=repo, policy_hash="a" * 64)
    assert result["ok"] is False
    assert any("mismatch" in problem for problem in result["problems"])


def test_manifest_detects_extra_protected_file(tmp_path):
    repo = tmp_path / "repo"
    (repo / "project" / "core").mkdir(parents=True)
    (repo / "project" / "core" / "a.py").write_text("VALUE = 1\n", encoding="utf-8")
    built = manifest_module.build_manifest(
        version="0.82.0",
        commit="abc123",
        release_key_id="release-test",
        policy_hash="a" * 64,
        generated_at="2026-10-21T00:00:00Z",
        root=repo,
    )
    (repo / "project" / "core" / "b.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = manifest_module.verify_manifest(built, root=repo)

    assert result["ok"] is False
    assert any("not in the manifest" in problem for problem in result["problems"])
