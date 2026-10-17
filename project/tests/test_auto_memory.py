"""Auto-memory tests: 30-message cadence, extraction, summarization, notes tool.

Safe: no network (the single LLM entry is mocked), no import-time side effects,
real Data files are snapshotted and restored.
"""
import json
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import pytest

GUARD_DB_FILES = ("conversation.json", "user_memory.json", "agent_memory.json",
                  "facts_memory.json", "agent_notes.json", ".auto_memory_last_trigger")


@pytest.fixture
def guard():
    from core.paths import data_db_dir
    from core.memory import service
    db = data_db_dir()
    # Snapshot every guarded file by ABSOLUTE path (plain names would resolve
    # against the current working directory on restore).
    targets = [db / name for name in GUARD_DB_FILES]
    targets.extend(service._storage_path(kind) for kind in service.KINDS)
    before = {str(p): (p.read_bytes() if p.is_file() else None) for p in targets}
    yield
    for path, content in before.items():
        p = Path(path)
        if content is None:
            if p.is_file():
                p.unlink()
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)


def _write_conversation(n):
    from core.paths import data_db_dir
    conv = [{"id": f"t{i}", "role": "user" if i % 2 == 0 else "assistant",
             "content": f"TEST message {i}", "ts": "2026-01-01T00:00:00Z"}
            for i in range(n)]
    (data_db_dir() / "conversation.json").write_text(
        json.dumps(conv, ensure_ascii=False), encoding="utf-8")
    (data_db_dir() / ".auto_memory_last_trigger").write_text("0", encoding="utf-8")


@pytest.fixture
def mock_llm(monkeypatch):
    """Mock the single LLM entry of auto_memory. No network."""
    from core.memory import auto_memory as am

    def fake_call_llm(system_prompt, user_content):
        if "compressor" in system_prompt.lower():
            return "- name: TESTNAME\n- city: TESTCITY"
        return "- name: TESTNAME\n- city: TESTCITY"

    monkeypatch.setattr(am, "_call_llm", fake_call_llm)


def test_should_trigger_every_30(guard):
    from core.memory.auto_memory import _should_trigger
    _write_conversation(29)
    assert _should_trigger() is False
    _write_conversation(30)
    assert _should_trigger() is True


def test_extract_stores_bullets(guard, mock_llm):
    from core.memory import auto_memory as am
    from core.memory import service
    _write_conversation(30)
    out = am.trigger_now_for_test()
    assert out["user_extracted"] is True
    assert out["facts_extracted"] is True
    assert out["agent_extracted"] is True
    text = service.get_text(service.USER)
    assert "TESTNAME" in text and "TESTCITY" in text
    assert text.startswith("- ")


def test_no_retrigger_without_30_new(guard, mock_llm):
    from core.memory import auto_memory as am
    from core.memory.auto_memory import _should_trigger
    _write_conversation(30)
    am.trigger_now_for_test()
    assert _should_trigger() is False


def test_shrink_guard_keeps_existing(guard, monkeypatch):
    from core.memory import auto_memory as am
    from core.memory import service
    # Existing memory is big; a "failed" LLM returns a tiny memory.
    service.set_text(service.USER, "\n".join(f"- fact number {i} with some words here" for i in range(20)))
    before = service.get_text(service.USER)
    monkeypatch.setattr(am, "_call_llm", lambda s, u: "- x")
    _write_conversation(30)
    assert am._extract(service.USER) is False
    assert service.get_text(service.USER) == before


def test_summarize_compresses_over_limit(guard, monkeypatch):
    from core.memory import auto_memory as am
    from core.memory import service
    big = "\n".join("- " + ("x" * 200) for _ in range(20))  # ~1.3k tokens, over the cap
    service.set_text(service.AGENT, big)
    assert service.is_over_limit(service.AGENT) is True
    monkeypatch.setattr(am, "_call_llm", lambda s, u: "- name: TESTNAME")
    assert am.summarize_if_needed(service.AGENT) is True
    assert service.get_text(service.AGENT).strip() == "- name: TESTNAME"


def test_summarize_skips_under_limit(guard, monkeypatch):
    from core.memory import auto_memory as am
    from core.memory import service
    service.set_text(service.FACTS, "- likes: tea")
    assert am.summarize_if_needed(service.FACTS) is False


def test_background_trigger_marks_done(guard, mock_llm):
    from core.memory import auto_memory as am
    from core.paths import data_db_dir
    _write_conversation(60)
    res = am.maybe_trigger_auto_memory()
    assert res is not None and res["triggered_at"] == 60
    marker = data_db_dir() / ".auto_memory_last_trigger"
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            if int(marker.read_text(encoding="utf-8").strip()) == 60:
                break
        except Exception:
            pass
        time.sleep(0.2)
    else:
        raise AssertionError("background auto-memory did not mark 60 in time")


def test_memory_tool_actions(guard, monkeypatch):
    from core.memory import service
    import plugins.core_plugins.memory.tool as mem_tool
    # Avoid a real summarizer call on write/edit.
    import core.memory.auto_memory as am_mod
    monkeypatch.setattr(am_mod, "summarize_if_needed", lambda kind: False)

    service.clear(service.NOTES)
    assert mem_tool.execute("read")["empty"] is True
    w = mem_tool.execute("write", text="first note")
    assert w["action"] == "write"
    assert "first note" in service.get_text(service.NOTES)
    mem_tool.execute("write", text="second note")
    assert "second note" in service.get_text(service.NOTES)
    e = mem_tool.execute("edit", text="only note")
    assert e["action"] == "edit"
    assert service.get_text(service.NOTES).strip() == "- only note"
    d = mem_tool.execute("delete")
    assert d["cleared_lines"] >= 1
    assert service.get_text(service.NOTES) == ""


def test_memory_tool_rejects_bad_args(guard):
    import plugins.core_plugins.memory.tool as mem_tool
    with pytest.raises(ValueError):
        mem_tool.execute("read_and_write")
    with pytest.raises(ValueError):
        mem_tool.execute("write")  # text required
    with pytest.raises(ValueError):
        mem_tool.execute(None)
