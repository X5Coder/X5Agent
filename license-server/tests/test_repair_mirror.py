from __future__ import annotations

import urllib.error

import pytest
from fastapi.testclient import TestClient

from app import repair_mirror

COMMIT = "a" * 40


def _get(client: TestClient, file_path: str, commit: str = COMMIT):
    return client.get(f"/mirror/{commit}/{file_path}")


def test_serves_a_protected_file(client: TestClient, monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    def fake_download(commit: str, relative: str) -> bytes:
        seen.append((commit, relative))
        return b"# protected\n"

    monkeypatch.setattr(repair_mirror, "_download", fake_download)
    response = _get(client, "project/core/agent_guard/seal.py")
    assert response.status_code == 200
    assert response.content == b"# protected\n"
    assert response.headers["content-type"] == "application/octet-stream"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "immutable" in response.headers["cache-control"]
    assert seen == [(COMMIT, "project/core/agent_guard/seal.py")]


def test_serves_the_launcher(client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(repair_mirror, "_download", lambda *_: b"launcher")
    response = _get(client, "agent_boot.py")
    assert response.status_code == 200
    assert response.content == b"launcher"


@pytest.mark.parametrize("commit", ["abc", "A" * 40, "g" * 40])
def test_rejects_a_bad_commit(client: TestClient, commit: str) -> None:
    response = _get(client, "project/core/agent_guard/seal.py", commit=commit)
    assert response.status_code in {400, 404}
    assert response.json()["detail"] in {"bad_commit", "not_found"}


@pytest.mark.parametrize(
    "file_path",
    [
        "README.md",
        "license-server/app/main.py",
        "project/.env",
        "project/core/.env",
        "project/agent/release_public_key.pem",
        "project/tests/__pycache__/x.pyc",
        "project/core/agent_guard/../../../etc/passwd",
        "project\\core\\agent_guard\\seal.py",
        "project/core/agent_guard/.hidden.py",
    ],
)
def test_rejects_paths_outside_the_mirror(
    client: TestClient, monkeypatch, file_path: str
) -> None:
    called = False

    def fake_download(*_args) -> bytes:
        nonlocal called
        called = True
        return b"should not run"

    monkeypatch.setattr(repair_mirror, "_download", fake_download)
    response = _get(client, file_path)
    assert response.status_code in {400, 404}
    assert called is False


def test_upstream_404_is_a_404(client: TestClient, monkeypatch) -> None:
    def missing(*_args) -> bytes:
        raise urllib.error.HTTPError(
            url="https://example.invalid", code=404, msg="no", hdrs=None, fp=None
        )

    monkeypatch.setattr(repair_mirror, "_download", missing)
    response = _get(client, "project/core/agent_guard/seal.py")
    assert response.status_code == 404
    assert response.json()["detail"] == "not_found"


def test_upstream_failure_is_a_502(client: TestClient, monkeypatch) -> None:
    def down(*_args) -> bytes:
        raise urllib.error.URLError("offline")

    monkeypatch.setattr(repair_mirror, "_download", down)
    response = _get(client, "project/core/agent_guard/seal.py")
    assert response.status_code == 502
    assert response.json()["detail"] == "mirror_unavailable"


def test_oversized_file_is_refused(client: TestClient, monkeypatch) -> None:
    def huge(*_args) -> bytes:
        raise repair_mirror.RepairMirrorError(413, "file_too_large")

    monkeypatch.setattr(repair_mirror, "_download", huge)
    response = _get(client, "project/core/agent_guard/seal.py")
    assert response.status_code == 413
    assert response.json()["detail"] == "file_too_large"
