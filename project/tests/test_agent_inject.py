"""XAP/1 guard injection: idempotent points + notices, no broken functions."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import agent_inject, agent_notice

SAMPLE = '''"""sample tool."""
from __future__ import annotations

from pathlib import Path


def execute(path: str) -> dict:
    """Run it."""
    return {"path": path}


def helper() -> int:
    return 1


def _private() -> int:
    return 2
'''


def _inject(tmp_path: Path, text: str = SAMPLE) -> tuple[Path, str, int]:
    path = tmp_path / "tool.py"
    path.write_text(text, encoding="utf-8", newline="")
    used: set[tuple[int, int]] = set()
    updated, count = agent_inject.inject_text(path, text, used)
    return path, updated, count


def test_injects_module_and_public_function_guards(tmp_path):
    path, updated, count = _inject(tmp_path)

    assert count == 3
    assert agent_inject.GUARD_IMPORT in updated
    assert updated.count(agent_inject.GUARD_CALL) == 3
    assert updated.index(agent_inject.GUARD_CALL) < updated.index("def execute")
    execute_body = updated.split("def execute", 1)[1].split("def helper", 1)[0]
    assert agent_inject.GUARD_CALL in execute_body
    assert execute_body.index(agent_inject.GUARD_CALL) < execute_body.index("return")
    helper_body = updated.split("def helper", 1)[1].split("def _private", 1)[0]
    assert agent_inject.GUARD_CALL in helper_body
    assert agent_inject.check_file(path, updated) == []


def test_injection_is_idempotent(tmp_path):
    path, updated, _count = _inject(tmp_path)
    used: set[tuple[int, int]] = set()
    again, count = agent_inject.inject_text(path, updated, used)

    assert count == 0
    assert again == updated


def test_every_guard_has_a_unique_notice(tmp_path):
    path, updated, _count = _inject(tmp_path)
    path.write_text(updated, encoding="utf-8", newline="")

    pairs = agent_notice._notice_pairs(path)
    assert len(pairs) == 3
    assert len(set(pairs)) == 3
    assert agent_notice.NOTICE_TEXT in updated


def test_crlf_is_preserved(tmp_path):
    text = SAMPLE.replace("\n", "\r\n")
    _path, updated, _count = _inject(tmp_path, text)

    assert "\r\n" in updated
    assert "\n" not in updated.replace("\r\n", "")


def test_check_reports_missing_points(tmp_path):
    path = tmp_path / "tool.py"
    path.write_text(SAMPLE, encoding="utf-8", newline="")

    missing = agent_inject.check_file(path, SAMPLE)

    assert any("module guard" in item for item in missing)
    assert any("execute" in item for item in missing)
    assert any("helper" in item for item in missing)


def test_real_tool_tree_has_every_guard_point():
    missing: list[str] = []
    for path in agent_inject._targets():
        missing.extend(
            agent_inject.check_file(path, path.read_text(encoding="utf-8"))
        )

    assert missing == []
