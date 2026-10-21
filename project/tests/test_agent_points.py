from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.agent_guard import points


def _official(monkeypatch):
    monkeypatch.setattr(points, "_require_official", lambda: None)


def test_guard_refusal_result_is_copyable() -> None:
    refusal = points.GuardRefusal("license_blocked", "blocked")

    assert refusal.as_result() == {
        "ok": False,
        "code": "license_blocked",
        "error": "blocked",
    }


def test_require_tool_rejects_missing_name() -> None:
    with pytest.raises(points.GuardRefusal) as caught:
        points.require_tool("  ")

    assert caught.value.code == "invalid_tool"


def test_require_tool_rejects_overlong_name() -> None:
    with pytest.raises(points.GuardRefusal) as caught:
        points.require_tool("x" * (points.MAX_TOOL_NAME + 1))

    assert caught.value.code == "invalid_tool"


def test_require_tool_passes_when_official_and_licensed(monkeypatch) -> None:
    _official(monkeypatch)
    monkeypatch.setattr(points, "_license_gate", lambda: (True, ""))

    points.require_tool("terminal")


def test_require_tool_fails_closed_when_license_blocks(monkeypatch) -> None:
    _official(monkeypatch)
    monkeypatch.setattr(points, "_license_gate", lambda: (False, "blocked"))

    with pytest.raises(points.GuardRefusal) as caught:
        points.require_tool("terminal")

    assert caught.value.code == "license_blocked"
    assert caught.value.message == "blocked"


def test_legacy_launch_refuses_before_license(monkeypatch) -> None:
    monkeypatch.setattr(
        points,
        "_require_official",
        lambda: (_ for _ in ()).throw(
            points.GuardRefusal("unsupported_launch", "legacy")
        ),
    )
    monkeypatch.setattr(points, "_license_gate", lambda: (True, ""))

    with pytest.raises(points.GuardRefusal) as caught:
        points.loop_guard()

    assert caught.value.code == "unsupported_launch"


def test_action_guard_rejects_unknown_action(monkeypatch) -> None:
    _official(monkeypatch)

    with pytest.raises(points.GuardRefusal) as caught:
        points.action_guard("unknown")

    assert caught.value.code == "invalid_action"


def test_action_guard_accepts_known_action(monkeypatch) -> None:
    _official(monkeypatch)
    monkeypatch.setattr(points, "_license_gate", lambda: (True, ""))

    points.action_guard("gateway")


def test_local_guard_rejects_missing_module() -> None:
    with pytest.raises(points.GuardRefusal) as caught:
        points.local_guard("")

    assert caught.value.code == "invalid_module"

def test_engine_guard_gate_is_fail_closed(monkeypatch) -> None:
    from core.agent.loop import engine

    monkeypatch.setattr(
        points,
        "loop_guard",
        lambda: (_ for _ in ()).throw(points.GuardRefusal("unsupported_launch", "legacy")),
    )

    ok, message, code = engine._guard_gate()

    assert ok is False
    assert message == "legacy"
    assert code == "unsupported_launch"


def test_dispatcher_refuses_before_loading_a_tool(monkeypatch) -> None:
    from core.agent.loop import dispatcher

    monkeypatch.setattr(
        points,
        "require_tool",
        lambda name: (_ for _ in ()).throw(points.GuardRefusal("license_blocked", "blocked")),
    )

    result = dispatcher.execute_tool("terminal", {"command": "echo hi"})

    assert result["success"] is False
    assert result["code"] == "license_blocked"
    assert result["error"] == "blocked"


def _blocked_action(monkeypatch) -> None:
    monkeypatch.setattr(
        points,
        "action_guard",
        lambda action: (_ for _ in ()).throw(
            points.GuardRefusal("unsupported_launch", "legacy")
        ),
    )


def test_gateway_refuses_before_agent_run(monkeypatch) -> None:
    from gateway import dispatch as gateway_dispatch

    sent: list[str] = []
    monkeypatch.setattr(gateway_dispatch, "_convert_part", lambda part: ("hi", ""))
    monkeypatch.setattr(
        gateway_dispatch, "_tell", lambda channel, chat, text: sent.append(text)
    )
    _blocked_action(monkeypatch)

    gateway_dispatch._run_parts_inner(
        "telegram", "1", [{"type": "text", "text": "hi"}], None
    )

    assert sent == ["legacy"]


def test_scheduler_refuses_before_loading_jobs(monkeypatch) -> None:
    from core.scheduler import engine as scheduler_engine

    def _must_not_load():
        raise AssertionError("jobs were loaded before the guard")

    monkeypatch.setattr(scheduler_engine, "_load_jobs", _must_not_load)
    _blocked_action(monkeypatch)

    assert scheduler_engine.check_and_execute_due_jobs() == []


def test_trigger_refuses_before_loading_skill(monkeypatch) -> None:
    from core.trigger import engine as trigger_engine

    def _must_not_load(ref):
        raise AssertionError("skill was loaded before the guard")

    monkeypatch.setattr(trigger_engine, "_load_skill_content", _must_not_load)
    _blocked_action(monkeypatch)

    trigger_engine._execute_trigger_skill(
        {"trigger_id": "t1", "skill_ref": "skill", "task": "task"}
    )


def test_studio_refuses_before_building_context(monkeypatch) -> None:
    from core.studio import api as studio_api

    _blocked_action(monkeypatch)

    status, body = studio_api._run_studio_turn("pdf_1", 1, "hi", None)

    assert status == 403
    assert body["code"] == "unsupported_launch"
    assert body["error"] == "legacy"
