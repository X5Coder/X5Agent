from __future__ import annotations

import importlib.util
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from core.agent_guard import heal, manifest as manifest_module


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _agent_boot_module():
    path = _repo_root() / "agent_boot.py"
    spec = importlib.util.spec_from_file_location("agent_boot_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _policy() -> dict:
    return {
        "repo": "X5Coder/X5Agent",
        "update_hosts": ["raw.githubusercontent.com"],
        "license_servers": ["https://license.example.test"],
    }


def _repo(tmp_path: Path) -> tuple[Path, dict]:
    root = tmp_path / "repo"
    (root / "project" / "core").mkdir(parents=True)
    (root / "project" / "core" / "alpha.py").write_bytes(b"alpha\n")
    (root / "project" / "launch.py").write_bytes(b"launch\n")
    (root / "updates.json").write_bytes(b'{"version":"0.83.0"}')
    (root / "X5Agent" / "Data").mkdir(parents=True)
    (root / "X5Agent" / "Data" / "user.txt").write_bytes(b"keep\n")
    generated = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    manifest = manifest_module.build_manifest(
        version="0.83.0",
        commit="abc123",
        release_key_id="release-test",
        policy_hash="policy-test",
        generated_at=generated,
        root=root,
    )
    return root, manifest


def _repair(root: Path, manifest: dict, fetcher=None, **kwargs) -> heal.SilentRepair:
    return heal.SilentRepair(
        root,
        manifest,
        _policy(),
        agent_root=root / "X5Agent" / "Data" / "agent",
        fetcher=fetcher,
        **kwargs,
    )


def test_snapshot_roundtrip_and_verify(tmp_path):
    root, manifest = _repo(tmp_path)
    snapshot = root / "X5Agent" / "Data" / "agent" / "snapshot"

    assert heal.create_snapshot(root, snapshot, manifest) == len(manifest["files"])
    assert heal.verify_snapshot(snapshot, manifest)["ok"] is True

    (snapshot / "project" / "core" / "alpha.py").write_bytes(b"tampered\n")
    assert heal.verify_snapshot(snapshot, manifest)["ok"] is False


def test_repair_restores_changed_file_from_snapshot_without_touching_user_data(tmp_path):
    root, manifest = _repo(tmp_path)
    snapshot = root / "X5Agent" / "Data" / "agent" / "snapshot"
    heal.create_snapshot(root, snapshot, manifest)
    (root / "project" / "core" / "alpha.py").write_bytes(b"tampered\n")

    result = _repair(root, manifest).run()

    assert result.ok is True
    assert "project/core/alpha.py" in result.repaired
    assert (root / "project" / "core" / "alpha.py").read_text(encoding="utf-8") == "alpha\n"
    assert (root / "X5Agent" / "Data" / "user.txt").read_text(encoding="utf-8") == "keep\n"
    assert heal.safe_mode_status(root / "X5Agent" / "Data" / "agent") == ""


def test_repair_restores_missing_file_from_snapshot(tmp_path):
    root, manifest = _repo(tmp_path)
    snapshot = root / "X5Agent" / "Data" / "agent" / "snapshot"
    heal.create_snapshot(root, snapshot, manifest)
    (root / "project" / "core" / "alpha.py").unlink()

    result = _repair(root, manifest).run()

    assert result.ok is True
    assert (root / "project" / "core" / "alpha.py").read_text(encoding="utf-8") == "alpha\n"


def test_extra_protected_file_is_quarantined_not_deleted(tmp_path):
    root, manifest = _repo(tmp_path)
    snapshot = root / "X5Agent" / "Data" / "agent" / "snapshot"
    heal.create_snapshot(root, snapshot, manifest)
    extra = root / "project" / "core" / "extra.py"
    extra.write_bytes(b"unknown\n")

    result = _repair(root, manifest).run()

    assert result.ok is True
    assert result.quarantined == ("project/core/extra.py",)
    assert not extra.exists()
    quarantined = list((root / "X5Agent" / "Data" / "agent" / "quarantine").rglob("extra.py"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "unknown\n"


def test_remote_repair_fetches_only_allowed_https_urls(tmp_path):
    root, manifest = _repo(tmp_path)
    (root / "project" / "core" / "alpha.py").write_bytes(b"tampered\n")
    seen: list[str] = []

    def fetcher(url: str, timeout: int) -> bytes:
        seen.append(url)
        return b"alpha\n"

    result = _repair(root, manifest, fetcher=fetcher).run()

    assert result.ok is True
    assert seen == [
        "https://raw.githubusercontent.com/X5Coder/X5Agent/abc123/project/core/alpha.py"
    ]
    assert (root / "project" / "core" / "alpha.py").read_bytes() == b"alpha\n"


def test_bad_remote_content_never_overwrites_and_enters_safe_mode(tmp_path):
    root, manifest = _repo(tmp_path)
    target = root / "project" / "core" / "alpha.py"
    target.write_bytes(b"tampered\n")

    result = _repair(root, manifest, fetcher=lambda url, timeout: b"evil\n").run()

    assert result.ok is False
    assert result.safe_mode is True
    assert target.read_text(encoding="utf-8") == "tampered\n"
    assert heal.safe_mode_status(root / "X5Agent" / "Data" / "agent") == "Checking installation"
    assert heal.read_safe_mode(root / "X5Agent" / "Data" / "agent")["safe_mode"] is True


def test_repair_queue_holds_until_success(tmp_path):
    root, manifest = _repo(tmp_path)
    target = root / "project" / "core" / "alpha.py"
    snapshot = root / "X5Agent" / "Data" / "agent" / "snapshot"
    heal.create_snapshot(root, snapshot, manifest)
    good = (snapshot / "project" / "core" / "alpha.py").read_bytes()
    (snapshot / "project" / "core" / "alpha.py").write_bytes(b"evil\n")
    target.write_bytes(b"tampered\n")
    queue = heal.RepairQueue(max_items=2)

    failed = _repair(
        root,
        manifest,
        fetcher=lambda url, timeout: b"evil\n",
        queue=queue,
    ).run()
    assert failed.safe_mode is True
    assert queue.held is True
    assert queue.enqueue("network") is True
    assert queue.enqueue("settings") is True
    assert queue.enqueue("extra") is True

    (snapshot / "project" / "core" / "alpha.py").write_bytes(good)
    healed = _repair(root, manifest, queue=queue).run()
    assert healed.ok is True
    assert queue.held is False
    assert queue.drain() == ("settings", "extra")


def test_repair_refuses_oversized_remote_file(tmp_path):
    root, manifest = _repo(tmp_path)
    (root / "project" / "core" / "alpha.py").write_bytes(b"tampered\n")

    result = _repair(
        root,
        manifest,
        fetcher=lambda url, timeout: b"alpha\n",
        max_file_bytes=2,
    ).run()

    assert result.ok is False
    assert (root / "project" / "core" / "alpha.py").read_text(encoding="utf-8") == "tampered\n"


def test_bootstrap_restores_missing_guard_file_from_snapshot(tmp_path, monkeypatch):
    source = _repo_root()
    target = tmp_path / "repo"
    shutil.copytree(source / "project" / "agent", target / "project" / "agent")
    shutil.copytree(
        source / "project" / "core" / "agent_guard",
        target / "project" / "core" / "agent_guard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    snapshot = target / "X5Agent" / "Data" / "agent" / "snapshot" / "project" / "core" / "agent_guard"
    shutil.copytree(target / "project" / "core" / "agent_guard", snapshot)
    missing = target / "project" / "core" / "agent_guard" / "trust.py"
    missing.unlink()

    module = _agent_boot_module()
    monkeypatch.setattr(module, "ROOT", target)

    assert module._restore_guard_files() is True
    assert missing.read_bytes() == (
        source / "project" / "core" / "agent_guard" / "trust.py"
    ).read_bytes()


def test_bootstrap_refuses_a_tampered_manifest(tmp_path, monkeypatch):
    source = _repo_root()
    target = tmp_path / "repo"
    shutil.copytree(source / "project" / "agent", target / "project" / "agent")
    shutil.copytree(
        source / "project" / "core" / "agent_guard",
        target / "project" / "core" / "agent_guard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    manifest_path = target / "project" / "agent" / "manifest.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    data["root"] = "0" * 64
    manifest_path.write_text(json.dumps(data), encoding="utf-8")
    missing = target / "project" / "core" / "agent_guard" / "trust.py"
    missing.unlink()

    module = _agent_boot_module()
    monkeypatch.setattr(module, "ROOT", target)

    assert module._restore_guard_files() is False
    assert not missing.exists()
