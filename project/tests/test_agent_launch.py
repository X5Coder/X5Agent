from __future__ import annotations

import sys
import types

import pytest

from core.agent_guard import import_guard, launch_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    # sanitize_sys_path mutates sys.path in place (by design at startup), so a
    # test that runs it would otherwise strip this interpreter's user
    # site-packages for every later test in the process.
    saved_path = list(sys.path)
    try:
        yield
    finally:
        sys.path[:] = saved_path
        import_guard.uninstall_import_guard()


def test_sanitize_sys_path_removes_user_roots(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    data = tmp_path / "X5Agent" / "Data"
    data.mkdir(parents=True)

    monkeypatch.setattr(
        launch_guard,
        "forbidden_roots",
        lambda project_dir=None: (data,),
    )
    monkeypatch.setattr(
        sys,
        "path",
        ["", str(data), str(project), "C:\\Python312\\Lib"],
    )

    removed = launch_guard.sanitize_sys_path(project)

    assert "" in removed
    assert str(data) in removed
    assert str(project) in sys.path
    assert not any(launch_guard.is_forbidden_path(entry) for entry in sys.path if entry)


def test_sitecustomize_is_detected(monkeypatch):
    module = types.ModuleType("sitecustomize")
    module.__file__ = "C:\\temp\\sitecustomize.py"
    monkeypatch.setitem(sys.modules, "sitecustomize", module)

    found = launch_guard.detect_sitecustomize()

    assert any(item.startswith("sitecustomize:") for item in found)


def test_begin_launch_marks_legacy(monkeypatch):
    monkeypatch.delenv("X5_AGENT_BOOT", raising=False)
    monkeypatch.setattr(sys, "dont_write_bytecode", False)

    state = launch_guard.begin_launch("test")

    assert state["mode"] == "legacy"
    assert state["trusted"] is False
    assert state["reason"] == "legacy_entrypoint"
    assert state["import_guard"] is True


def test_begin_launch_marks_official(monkeypatch):
    monkeypatch.setenv("X5_AGENT_BOOT", "1")
    monkeypatch.setattr(sys, "dont_write_bytecode", False)
    monkeypatch.setattr(launch_guard, "detect_sitecustomize", lambda: [])
    monkeypatch.setattr(launch_guard, "_path_is_safe", lambda: True)

    state = launch_guard.begin_launch("test")

    assert state["mode"] == "official"
    assert state["trusted"] is True
    assert state["reason"] == "official"
    assert sys.dont_write_bytecode is True


def test_assert_official_rejects_legacy(monkeypatch):
    monkeypatch.delenv("X5_AGENT_BOOT", raising=False)
    launch_guard.begin_launch("test")

    with pytest.raises(RuntimeError):
        launch_guard.assert_official()


def test_strict_mode_rejects_legacy_main(monkeypatch):
    import main as project_main

    monkeypatch.delenv("X5_AGENT_BOOT", raising=False)
    monkeypatch.setenv("X5_AGENT_STRICT", "1")

    assert project_main.main() == 2
