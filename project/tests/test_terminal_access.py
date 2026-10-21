"""Terminal is always full-access: any command, any existing working directory.

The old scope switch (limited/full) is gone, so these tests guard that nothing
reintroduces a jail and that the tool still validates its real inputs.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.tools.terminal import tool as term  # noqa: E402


def test_no_scope_api_remains():
    src = (PROJECT_ROOT / "web" / "server" / "server.py").read_text(encoding="utf-8")
    assert "/api/terminal_access" not in src
    assert "terminal_access.json" not in src
    assert not (PROJECT_ROOT / "core" / "tools" / "terminal" / "scope.py").exists()


def test_command_validation():
    assert term._validate_command("  echo hi  ") == "echo hi"
    with pytest.raises(ValueError):
        term._validate_command("   ")
    with pytest.raises(TypeError):
        term._validate_command(7)


def test_workdir_accepts_any_existing_dir(tmp_path):
    assert term._validate_workdir(None) == term._working_folder()
    assert term._validate_workdir(str(tmp_path)) == tmp_path.resolve()
    # an absolute path outside the project is a normal choice now
    assert term._validate_workdir(str(PROJECT_ROOT)) == PROJECT_ROOT.resolve()
    with pytest.raises(FileNotFoundError):
        term._validate_workdir(str(tmp_path / "nope"))


def test_timeout_bounds():
    assert term._validate_timeout(None) == term.DEFAULT_TIMEOUT
    assert term._validate_timeout(term.MAX_TIMEOUT) == term.MAX_TIMEOUT
    with pytest.raises(ValueError):
        term._validate_timeout(term.MAX_TIMEOUT + 1)
    with pytest.raises(ValueError):
        term._validate_timeout(0)
    with pytest.raises(TypeError):
        term._validate_timeout("30")


def test_spec_declares_full_access_open():
    import json
    spec = json.loads((PROJECT_ROOT / "core" / "tools" / "terminal" / "spec.json").read_text(encoding="utf-8"))
    blob = json.dumps(spec)
    assert "Workspace" not in blob
    assert "working folder" in blob.lower()
