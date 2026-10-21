from __future__ import annotations

import importlib
import sys

import pytest

from core.agent_guard import import_guard, launch_guard


@pytest.fixture(autouse=True)
def _clean_guard():
    yield
    import_guard.uninstall_import_guard()


def test_forbidden_path_detection(monkeypatch, tmp_path):
    data = tmp_path / "X5Agent" / "Data"
    data.mkdir(parents=True)
    module_file = data / "evil.py"
    module_file.write_text("VALUE = 1\n", encoding="utf-8")

    monkeypatch.setattr(
        launch_guard,
        "forbidden_roots",
        lambda project_dir=None: (data,),
    )

    assert launch_guard.is_forbidden_path(module_file) is True
    assert launch_guard.is_forbidden_path(tmp_path / "project" / "safe.py") is False


def test_import_guard_blocks_data_module(monkeypatch, tmp_path):
    skills = tmp_path / "skills"
    skills.mkdir()
    module_name = "agent_forbidden_sample"
    (skills / f"{module_name}.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        launch_guard,
        "forbidden_roots",
        lambda project_dir=None: (skills,),
    )
    monkeypatch.syspath_prepend(str(skills))
    importlib.invalidate_caches()

    import_guard.install_import_guard()
    sys.modules.pop(module_name, None)

    with pytest.raises(ImportError):
        importlib.import_module(module_name)

    assert module_name not in sys.modules


def test_assert_no_user_code_on_path(monkeypatch, tmp_path):
    data = tmp_path / "X5Agent" / "Workspace"
    data.mkdir(parents=True)
    monkeypatch.setattr(
        launch_guard,
        "forbidden_roots",
        lambda project_dir=None: (data,),
    )
    monkeypatch.setattr(sys, "path", [str(data), str(tmp_path / "project")])

    with pytest.raises(RuntimeError):
        import_guard.assert_no_user_code_on_path()
