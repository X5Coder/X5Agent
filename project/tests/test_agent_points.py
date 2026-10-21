from __future__ import annotations

import pytest

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
