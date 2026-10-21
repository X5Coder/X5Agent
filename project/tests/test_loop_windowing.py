"""Loop context-accounting + missing-model skip tests (no LLM, deterministic).

Contract under test: one MESSAGE is one TURN (the reply plus every tool step
it produced). Steps still travel in the normal OpenAI wire format, but they
never consume the 30-message chat-history window, so a 30-step turn sends all
30 steps and still leaves the 30 real user/agent messages intact.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import core.agent.loop.engine as engine
from core.agent.loop.engine import (
    _MESSAGE_LIMIT,
    _clip_resume_units,
    _model_skipped_result,
    _outgoing_messages,
)


def _unit(i: int, tools: int = 1) -> list[dict]:
    assistant = {"role": "assistant", "content": f"reply {i}",
                 "tool_calls": [{"id": f"c{i}-{k}", "name": "t"} for k in range(tools)]}
    unit = [assistant]
    for k in range(tools):
        unit.append({"role": "tool", "tool_call_id": f"c{i}-{k}", "content": f"out {i}-{k}"})
    return unit


def _big_unit(i: int, size: int = 400) -> list[dict]:
    assistant = {"role": "assistant", "content": f"reply {i}",
                 "tool_calls": [{"id": f"big{i}", "name": "t"}]}
    return [assistant,
            {"role": "tool", "tool_call_id": f"big{i}", "content": "x" * size}]


def test_turn_keeps_every_step_with_call_output_linkage():
    seed = [{"role": "system", "content": "sys"}, {"role": "user", "content": "ctx"}]
    units = [_unit(i) for i in range(35)]
    out = _outgoing_messages(seed, units)
    assert out[:2] == seed
    # All 35 steps are sent: a turn is one message, so no step is windowed out.
    assert len(out) == 2 + 35 * 2
    assert out[2]["content"] == "reply 0"  # oldest step still present
    assert out[3]["tool_call_id"] == "c0-0"  # call/output stay together
    assert out[-2]["content"] == "reply 34"


def test_thirty_step_turn_is_one_message_and_sends_all_steps():
    seed = [{"role": "system", "content": "s"}]
    units = [_unit(i) for i in range(30)]
    out = _outgoing_messages(seed, units)
    assert len(out) == 1 + 30 * 2
    assert _MESSAGE_LIMIT == 30


def test_short_history_passes_through_untouched():
    seed = [{"role": "system", "content": "s"}]
    units = [_unit(0), _unit(1)]
    out = _outgoing_messages(seed, units)
    assert len(out) == 1 + 4


def test_ceiling_drops_only_whole_oldest_steps_and_says_so(monkeypatch):
    monkeypatch.setattr(engine, "_TURN_CHARS_CEILING", 1700)
    seed = [{"role": "system", "content": "s"}]
    units = [_big_unit(i) for i in range(5)]
    out = _outgoing_messages(seed, units)
    # 2 newest steps fit (~807 chars each); the 3 oldest are left out.
    assert out[0] == seed[0]
    note = out[1]
    assert note["role"] == "assistant" and "[Context note]" in note["content"]
    assert "3 earlier step(s)" in note["content"]
    # Unit alignment: the oldest kept step starts with its assistant call and
    # carries the matching tool output (never a dangling tool_call_id).
    first = out[2]
    assert first["role"] == "assistant"
    assert first["content"] == "reply 3"
    assert out[3]["tool_call_id"] == first["tool_calls"][0]["id"]
    assert out[-2]["content"] == "reply 4"


def test_outgoing_never_raises_on_garbage():
    assert isinstance(_outgoing_messages(None, None), list)  # type: ignore[arg-type]
    assert _outgoing_messages([{"role": "system"}], "nope") == [{"role": "system"}]  # type: ignore[arg-type]


def test_resume_units_keep_every_step_whole(monkeypatch):
    monkeypatch.setattr(engine, "_TURN_CHARS_CEILING", 1700)
    units = [_big_unit(i) for i in range(5)] + ["junk"]  # junk entries are skipped
    out = _clip_resume_units(units)  # type: ignore[arg-type]
    assert [u[0]["content"] for u in out] == ["reply 3", "reply 4"]
    assert [u[1]["tool_call_id"] for u in out] == ["big3", "big4"]
    assert _clip_resume_units(None) == []
    assert _clip_resume_units("bad") == []  # type: ignore[arg-type]


def test_skip_result_never_stops_loop_and_links_model_page():
    res = _model_skipped_result("vision_analyze", {"prompt": "x"}, "vision")
    assert res["success"] is True
    out = res["output"]
    assert out["skipped"] is True
    assert out["needs_model_kind"] == "vision"
    assert "/ai-models" in out["message"]
    assert "vision_analyze" in out["event"]


def test_skip_result_never_raises():
    res = _model_skipped_result("", None, "stt")
    assert res["success"] is True
    assert res["output"]["needs_model_kind"] == "stt"
