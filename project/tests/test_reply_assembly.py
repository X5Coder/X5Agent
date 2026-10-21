"""reply_assembly (long replies in live parts): validation + assembly contract.

No LLM, no network, no disk writes (the loop's persistence/context are stubbed).
"""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.core_plugins.reply_assembly import events as ra_events
from plugins.core_plugins.reply_assembly import tool as ra_tool


def test_spec_loads_and_names_tool():
    spec = json.loads((PROJECT_ROOT / "plugins" / "core_plugins" / "reply_assembly"
                       / "spec.json").read_text(encoding="utf-8"))
    assert spec["name"] == "reply_assembly"
    assert spec["inputs"]["part"]["required"] is True
    assert spec["inputs"]["action"]["enum"] == ["next", "stop"]
    assert "long" in spec["description"].lower()


def test_validate_part_and_action():
    assert ra_tool.execute(part="hello", action="next")["accepted"] is True
    # action missing -> next (weak-model friendly, never a dead call)
    assert ra_tool.execute(part="hello")["action"] == "next"
    # case/space tolerant
    assert ra_tool.execute(part="bye", action=" STOP ")["action"] == "stop"
    with pytest.raises(ValueError):
        ra_tool.execute(part="   ", action="next")
    with pytest.raises(TypeError):
        ra_tool.execute(part=123, action="next")
    with pytest.raises(ValueError):
        ra_tool.execute(part="x" * (ra_tool.MAX_PART_CHARS + 1), action="next")
    with pytest.raises(ValueError):
        ra_tool.execute(part="x", action="later")


def test_result_messages_and_bounds():
    next_out = ra_tool.execute(part="a" * 1234, action="next", part_index=3)
    assert next_out["part_index"] == 3
    assert next_out["chars"] == 1234
    assert "Continue NOW with part 4" in next_out["message"]
    assert next_out["event"] == "Writing reply · part 3"
    stop_out = ra_tool.execute(part="tail", action="stop", part_index=4)
    assert stop_out["final"] is True
    assert "end your turn now with no text" in stop_out["message"]
    assert stop_out["event"] == "Writing reply · final part 4"
    # a forged/odd index never breaks the label
    assert ra_tool.execute(part="x", action="next", part_index=999)["part_index"] == 1


def test_events_labels():
    assert ra_events.SHOW_ARROW is False
    assert ra_events.format_event(action="stop", part_index=5) == "Writing reply · final part 5"
    assert ra_events.format_event(action="next", part_index=2) == "Writing reply · part 2"
    assert ra_events.format_event(action="next") == "Writing reply"


def test_dispatcher_registered_and_executes():
    from core.agent.loop.dispatcher import _DISCOVERABLE_MAP, execute_tool

    assert _DISCOVERABLE_MAP.get("reply_assembly") == "plugins.core_plugins.reply_assembly.tool"
    # A 25k-char part passes the 10k generic cap (long replies are legit),
    # and the loop's part number is forwarded to the tool.
    res = execute_tool("reply_assembly", {"part": "x" * 25000, "action": "next"},
                       part_index=7)
    assert res["success"] is True
    assert res["output"]["part_index"] == 7
    assert res["output"]["chars"] == 25000


def test_join_parts_preserves_formatting():
    from core.agent.loop.engine import _join_reply_parts

    assert _join_reply_parts(["one", "two"]) == "one\n\ntwo"
    assert _join_reply_parts(["ends\n", "next"]) == "ends\nnext"
    assert _join_reply_parts(["ends", "\nnext"]) == "ends\nnext"
    assert _join_reply_parts([]) == ""
    assert _join_reply_parts(["a", None, "", "b"]) == "a\n\nb"


def test_server_frame_and_replay_accept_reply_part():
    from web.server import server as S

    frame = S._build_stream_frame("reply_part", "hello", (2, "next"))
    assert frame == {"type": "reply_part", "text": "hello", "index": 2, "action": "next"}
    assert S._build_stream_frame("reply_part", "", (1, "next")) is None
    S._run_frames_clear()
    S._run_frame_remember({"type": "reply_part", "text": "x", "index": 1, "action": "stop"})
    with S._RUN_FRAMES_LOCK:
        assert [f.get("type") for f in S._RUN_FRAMES] == ["reply_part"]
    S._run_frames_clear()


def test_loop_stop_ends_turn_and_assembles(monkeypatch):
    """next -> model called again; stop -> no extra call; reply = joined parts."""
    from core.agent.loop import engine as E
    from core.ai_models import aip as AIP
    from core.ai_models import store as STORE
    from core.embedding import bootstrap as BOOT

    monkeypatch.setattr(E, "_license_gate", lambda: (True, ""))
    monkeypatch.setattr(E, "build_context", lambda *a, **k: ("system", "context", []))
    monkeypatch.setattr(E, "_persist_conversation", lambda *a, **k: ("u1", "a1"))
    monkeypatch.setattr(E, "_clear_temp_images_if_any", lambda: None)
    monkeypatch.setattr(BOOT, "require_ready", lambda: None)
    monkeypatch.setattr(STORE, "get_text_model", lambda: {"model_name": "m", "server_url": "http://x",
                                                          "parser_definition": {"language": "AIP/1"}})
    monkeypatch.setattr(AIP, "get_compiled", lambda record, kind: {"kind": kind})
    monkeypatch.setattr(AIP, "canonical_from_loop", lambda msgs, tools: {"messages": msgs, "tools": tools})
    calls = []
    scripted = [
        {"text": "", "tool_calls": [{"id": "c1", "name": "reply_assembly",
                                     "arguments": {"part": "Part one.", "action": "next"}}]},
        {"text": "", "tool_calls": [{"id": "c2", "name": "reply_assembly",
                                     "arguments": {"part": "Part two.", "action": "stop"}}]},
        {"text": "MUST NOT RUN", "tool_calls": []},
    ]

    def fake_exec(compiled, canonical, cancel_event=None):
        calls.append(1)
        return scripted[min(len(calls) - 1, len(scripted) - 1)]

    monkeypatch.setattr(AIP, "execute_compiled", fake_exec)
    import core.memory.auto_memory as AM
    monkeypatch.setattr(AM, "maybe_trigger_memory", lambda: None, raising=False)
    monkeypatch.setattr(AM, "maybe_trigger_auto_memory", lambda: None, raising=False)

    parts = []
    res = E.run_loop("write a long answer", max_steps=10, verbose=False,
                     on_reply_part=lambda text, idx, action: parts.append((text, idx, action)))
    assert len(calls) == 2, "stop must end the turn without another model call"
    assert parts == [("Part one.", 1, "next"), ("Part two.", 2, "stop")]
    assert res["final_answer"] == "Part one.\n\nPart two."
    assert res["success"] is True
