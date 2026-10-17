"""Execution-context + Active-Triggers block tests.

Proves the unified logic: scheduled/trigger runs execute with the normal
system prompt + all memories + environment + the skill as a normal L8
message + the same tools as the main session (no chat history), while the
MAIN context always lists every armed trigger + its awaited task so the
agent can manage them via trigger_search. No network, no watchers started
(triggers.json is written directly and restored).
"""
import json
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import pytest


@pytest.fixture
def guard_triggers():
    from core.paths import data_db_dir
    p = data_db_dir() / "triggers.json"
    before = p.read_bytes() if p.is_file() else None
    yield p
    if before is None:
        if p.is_file():
            p.unlink()
    else:
        p.write_bytes(before)


def _write_triggers(p, entries):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8")


def test_execution_context_isolated_composition():
    from core.agent.context.builder import build_context
    skill = "TEST-SKILL-EXECUTE-THIS-TASK"
    system_prompt, user_content, core_tools = build_context(
        skill, isolated=True, session_history="[branch] TEST prior run")
    assert system_prompt and len(system_prompt) > 100  # normal L1-L3 system prompt
    assert "[System Info - L4]" in user_content  # environment
    assert ("[User memory" in user_content or "[Facts memory" in user_content
            or "[Memories L5: empty]" in user_content)
    assert "[Available CORE tools L7:" in user_content  # same tools layer
    assert "[Current User Input - L8]" in user_content
    assert "TEST-SKILL-EXECUTE-THIS-TASK" in user_content.split("[Current User Input - L8]")[1]
    assert "[Mode: ISOLATED SCHEDULED EXECUTION" in user_content
    assert "[branch] TEST prior run" in user_content  # hidden branch history as L6
    assert "[Last " not in user_content and "messages - L6]" not in user_content  # no main chat


def test_execution_tools_match_main_session():
    from core.agent.context.builder import build_context
    _, _, isolated_tools = build_context("x", isolated=True)
    _, _, normal_tools = build_context("x", isolated=False)

    def names(tools):
        return sorted({t.get("function", {}).get("name") for t in tools} - {""})

    assert names(isolated_tools) == names(normal_tools) and names(normal_tools)


def test_main_context_lists_active_triggers(guard_triggers):
    from core.agent.context.builder import build_context
    _write_triggers(guard_triggers, [
        {"trigger_id": "TA1", "trigger_name": "filesystem_new_file",
         "task": "TEST await new pdf", "status": "active",
         "skill_ref": "s1", "config": {"watch_path": "inbox/docs"}},
        {"trigger_id": "TA2", "trigger_name": "manage_triggers",
         "task": "TEST watch example", "status": "active",
         "skill_ref": "s2", "config": {"url": "https://example.com"}},
        {"trigger_id": "TP9", "trigger_name": "filesystem_new_file",
         "task": "TEST paused one", "status": "paused", "config": {}},
    ])
    _, user_content, _ = build_context("hello", isolated=False)
    assert "[Active Triggers (2 armed" in user_content
    assert "TA1" in user_content and "TEST await new pdf" in user_content
    assert "TA2" in user_content and "TEST watch example" in user_content
    assert "TP9" not in user_content  # paused stays hidden
    assert "trigger_search" in user_content  # management hint


def test_no_triggers_no_block(guard_triggers):
    from core.agent.context.builder import build_context
    _write_triggers(guard_triggers, [])
    _, user_content, _ = build_context("hello", isolated=False)
    assert "[Active Triggers" not in user_content


def test_triggers_block_shows_count_over_bound(guard_triggers):
    from core.agent.context.builder import build_context
    _write_triggers(guard_triggers, [
        {"trigger_id": f"TX{i:02d}", "trigger_name": "filesystem_new_file",
         "task": f"TEST task {i}", "status": "active", "config": {}}
        for i in range(12)
    ])
    _, user_content, _ = build_context("hello", isolated=False)
    assert "[Active Triggers (12 armed" in user_content
    assert "showing last 10" in user_content
