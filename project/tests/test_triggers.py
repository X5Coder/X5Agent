"""Trigger logic tests: 8 approved configs, inbox/outbox guards, one-time death.

Covers the final design:
1 document_search new doc | 2 vision new image | 3 video group (analyze+transcribe+extract)
4 tts new txt | 5 read_file modified | 6 write_file modified | 7 video_download urls.txt | 8 upload ready
No LLM, no watchdog, no network. Deterministic and isolated.
"""
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.tools.trigger.tool import _validate_filesystem_config
from core.trigger import engine as eng
from core.paths import ensure_x5agent
from core.workfolder import get_path as folder_path

APPROVED = [
    ("filesystem_new_file", {"watch_path": "inbox/docs", "pattern": "*.pdf"}),
    ("filesystem_new_file", {"watch_path": "inbox/media", "pattern": "*.jpg"}),
    ("filesystem_new_file", {"watch_path": "inbox/media", "pattern": "*.mp4"}),
    ("filesystem_new_file", {"watch_path": "inbox/tts", "pattern": "*.txt"}),
    ("filesystem_file_modified", {"watch_path": "inbox/docs", "pattern": "*"}),
    ("filesystem_file_modified", {"watch_path": "inbox/docs", "pattern": "*.md"}),
    ("filesystem_file_modified", {"watch_path": "inbox", "pattern": "urls.txt"}),
    ("filesystem_new_file", {"watch_path": "outbox/ready_upload", "pattern": "*"}),
]

# 1. All 8 approved configs validate
for i, (tname, cfg) in enumerate(APPROVED, 1):
    out = _validate_filesystem_config(tname, dict(cfg))
    assert out["watch_path"] == cfg["watch_path"] and out["pattern"] == cfg["pattern"], (i, out)
print("8 approved configs OK")

# 2. Forbidden guards fail clearly (no silent ignore)
for bad in [
    {"watch_path": "outbox/done", "pattern": "*"},
    {"watch_path": "outbox/done/sub", "pattern": "*"},
    {"watch_path": "../Data", "pattern": "*"},
    {"watch_path": "inbox/docs", "pattern": ""},
    {"watch_path": "", "pattern": "*"},
]:
    try:
        _validate_filesystem_config("filesystem_new_file", bad)
        raise AssertionError(f"not rejected: {bad}")
    except (ValueError, TypeError):
        pass
print("forbidden guards OK")

# 3. Resolver stays inside the working folder and blocks outbox/done
ensure_x5agent()
ws = folder_path().resolve()
resolved = eng._resolve_watch_dir("inbox/docs")
assert str(resolved).startswith(str(ws)), resolved
try:
    eng._resolve_watch_dir("outbox/done")
    raise AssertionError("outbox/done resolved, must raise")
except ValueError:
    pass
print("resolver OK")

# 4. One-time death: _delete_skill_and_trigger removes trigger + skill dir
import json
import tempfile
import core.trigger.engine as E

tmp_triggers = Path(tempfile.mktemp(prefix="trig-", suffix=".json"))
tmp_skill_base = Path(tempfile.mkdtemp(prefix="trigskills-"))
tid = "deadbeef"
skill_ref = "trigger_deadbeef_demo"
(target_skill := tmp_skill_base / skill_ref).mkdir(parents=True, exist_ok=True)
(tmp_triggers).write_text(json.dumps([{
    "trigger_id": tid, "trigger_name": "filesystem_new_file",
    "task": "demo", "config": {"watch_path": "inbox/docs"},
    "skill_ref": skill_ref, "status": "active",
}], ensure_ascii=False), encoding="utf-8")

orig_load, orig_save = E._load_triggers, E._save_triggers
orig_skill_dir = E.TRIGGER_SKILLS_DIR
try:
    E._load_triggers = lambda: json.loads(tmp_triggers.read_text(encoding="utf-8"))
    E._save_triggers = lambda tr: tmp_triggers.write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
    E.TRIGGER_SKILLS_DIR = tmp_skill_base
    E._delete_skill_and_trigger(tid)
    remaining = json.loads(tmp_triggers.read_text(encoding="utf-8"))
    assert remaining == [], remaining
    assert not (tmp_skill_base / skill_ref).exists(), "skill dir not deleted"
finally:
    E._load_triggers, E._save_triggers = orig_load, orig_save
    E.TRIGGER_SKILLS_DIR = orig_skill_dir
print("one-time death OK")

# 5. txt separation: docs vs tts watch different dirs
docs_dir = eng._resolve_watch_dir("inbox/docs")
tts_dir = eng._resolve_watch_dir("inbox/tts")
assert docs_dir != tts_dir, (docs_dir, tts_dir)
print("docs/tts separation OK")

print("ALL TRIGGER TESTS PASSED")


# ---------------------------------------------------------------------------
# pytest-style tests for the unified trigger flow (no import-time execution).
# Guarded: real triggers.json is snapshotted/restored, created skills removed,
# watchers stopped. LLM skill generation is mocked (no network).
# ---------------------------------------------------------------------------
import pytest as _pytest


@_pytest.fixture
def _tguard():
    import core.tools.trigger.tool as _T
    from core.trigger import engine as _ENG
    from core.paths import data_db_dir as _dd
    db = _dd()
    trig = db / "triggers.json"
    before = trig.read_bytes() if trig.is_file() else None
    proj = Path(__file__).resolve().parents[1]
    base = proj / "skills" / "trigger_skills"
    base.mkdir(parents=True, exist_ok=True)
    skills_before = set(p.name for p in base.iterdir())
    yield _T, _ENG, base
    import shutil as _sh
    for tid in list(_ENG._WATCHERS):
        try:
            _ENG._stop_watcher(tid)
        except Exception:
            pass
    for p in base.iterdir():
        if p.name not in skills_before:
            _sh.rmtree(str(p)) if p.is_dir() else p.unlink()
    if before is None:
        if trig.is_file():
            trig.unlink()
    else:
        trig.write_bytes(before)


@_pytest.fixture
def _mock_gen(monkeypatch):
    import core.tools.trigger.tool as _T
    n = {"i": 0}

    def fake(name, task, config, todo=None, cancel_flag=None):
        n["i"] += 1
        return f"# Mock Skill v{n['i']} for {task[:30]}"

    monkeypatch.setattr(_T, "_generate_trigger_skill", fake)
    monkeypatch.setattr(_T, "_find_recent_todo_for_task", lambda task: None)
    return n


def test_create_rejects_unwatchable_path(_tguard, _mock_gen):
    import tempfile
    T, ENG, base = _tguard
    skills_before = set(p.name for p in base.iterdir())
    outside = str(Path(tempfile.gettempdir()) / "trigger_outside_xyz")
    try:
        T.execute(action="create", trigger_name="filesystem_new_file",
                  task="TEST outside", config={"watch_path": outside})
        raise AssertionError("outside watch_path accepted")
    except ValueError:
        pass
    assert set(p.name for p in base.iterdir()) == skills_before
    assert all(t.get("task") != "TEST outside" for t in T.execute(action="list")["triggers"])


def test_update_restarts_watcher_on_new_path(_tguard, _mock_gen):
    T, ENG, base = _tguard
    r = T.execute(action="create", trigger_name="filesystem_new_file",
                  task="TEST restart watch", config={"watch_path": "inbox/docs", "pattern": "RESTART_A_*.tmp"})
    tid = r["trigger"]["trigger_id"]
    try:
        assert tid in ENG._WATCHERS
        u = T.execute(action="update", trigger_id=tid,
                      config={"watch_path": "inbox/tts", "pattern": "RESTART_B_*.tmp"})
        assert u["skill_regenerated"] is True
        assert tid in ENG._WATCHERS, "watcher not restarted after path change"
        assert u["trigger"]["config"]["watch_path"] == "inbox/tts"
    finally:
        try:
            T.execute(action="delete", trigger_id=tid)
        except Exception:
            pass


def test_create_cap_matches_engine_cap():
    import core.tools.trigger.tool as T
    from core.trigger import engine as ENG
    assert T.MAX_TRIGGERS == ENG.MAX_ACTIVE_TRIGGERS


def test_start_all_rearms_after_restart_loss(_tguard, _mock_gen):
    T, ENG, base = _tguard
    r = T.execute(action="create", trigger_name="filesystem_new_file",
                  task="TEST rearm", config={"watch_path": "inbox/docs", "pattern": "REARM_*.tmp"})
    tid = r["trigger"]["trigger_id"]
    try:
        assert tid in ENG._WATCHERS
        ENG._stop_watcher(tid)  # simulate process restart (watchers are in-memory)
        assert tid not in ENG._WATCHERS
        ENG.start_all_triggers()  # what the server calls at startup
        assert tid in ENG._WATCHERS, "restart did not re-arm the trigger"
    finally:
        try:
            T.execute(action="delete", trigger_id=tid)
        except Exception:
            pass


def _sess_guard():
    """Snapshot trigger/scheduler session dirs + main conversation; restore after."""
    from core.paths import data_db_dir as _dd
    db = _dd()
    conv = db / "conversation.json"
    conv_before = conv.read_bytes() if conv.is_file() else None
    sess_dirs = {}
    for name in ("trigger_sessions", "scheduler_sessions"):
        d = db / name
        sess_dirs[name] = (d, set(p.name for p in d.iterdir()) if d.is_dir() else None)
    return db, conv, conv_before, sess_dirs


def _sess_restore(db, conv, conv_before, sess_dirs):
    import shutil as _sh
    if conv_before is None:
        if conv.is_file():
            conv.unlink()
    else:
        conv.write_bytes(conv_before)
    for name, (d, before) in sess_dirs.items():
        if before is None:
            if d.is_dir():
                _sh.rmtree(str(d))
        else:
            if d.is_dir():
                for p in d.iterdir():
                    if p.name not in before:
                        p.unlink()


def test_trigger_session_crud_and_bounds():
    import json as _js
    from core.trigger import session as _S
    db, conv, conv_before, sess_dirs = _sess_guard()
    tid = "TESTSESSunit1"
    try:
        assert _S.load_session(tid) == []
        assert _S.history_block(tid) == ""
        assert _S.append_session(tid, "user", "TEST hello") is not None
        assert _S.append_session(tid, "assistant", "TEST hi", {"steps": 1}) is not None
        msgs = _S.load_session(tid)
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert "TEST hello" in _S.history_block(tid)
        # bad ids fail clearly / safely
        try:
            _S.session_path("../../x")
            raise AssertionError("path escape accepted")
        except ValueError:
            pass
        assert _S.append_session(tid, "system", "nope") is None
        assert _S.delete_session(tid) is True
        assert _S.load_session(tid) == []
    finally:
        _sess_restore(db, conv, conv_before, sess_dirs)


def test_loop_persist_routes_trigger_keys_to_trigger_sessions():
    from core.agent.loop.engine import _persist_conversation
    db, conv, conv_before, sess_dirs = _sess_guard()
    try:
        out = _persist_conversation("TEST trig input", "TEST trig output", [], None, 0, None,
                                    session_key="trigger:TESTROUTEx")
        assert out is not None
        tfile = db / "trigger_sessions" / "TESTROUTEx.json"
        assert tfile.is_file(), "trigger session file missing"
        import json as _js
        msgs = _js.loads(tfile.read_text(encoding="utf-8"))
        assert [m["role"] for m in msgs] == ["user", "assistant"]
        assert not (db / "scheduler_sessions" / "TESTROUTEx.json").exists(), "leaked into scheduler sessions"
        cur = conv.read_bytes() if conv.is_file() else None
        assert cur == conv_before, "main conversation touched!"
    finally:
        _sess_restore(db, conv, conv_before, sess_dirs)


def test_execute_uses_hidden_branch_session(_tguard, monkeypatch):
    import core.agent.loop.engine as _loop_mod
    T, ENG, base = _tguard
    from core.paths import data_db_dir as _dd
    db = _dd()
    conv = db / "conversation.json"
    conv_before = conv.read_bytes() if conv.is_file() else None
    calls = []

    def fake_run_loop(**kwargs):
        calls.append(kwargs)
        return {"final_answer": "TEST done", "steps": 1, "success": True}

    monkeypatch.setattr(_loop_mod, "run_loop", fake_run_loop)
    # direct engine call with a task-only trigger (no skill file needed)
    ENG._execute_trigger_skill({"trigger_id": "TESTENGbranch1", "task": "TEST branch task"})
    assert calls, "run_loop never invoked"
    assert calls[0].get("isolated") is True
    assert calls[0].get("session_key") == "trigger:TESTENGbranch1", calls[0].get("session_key")
    cur = conv.read_bytes() if conv.is_file() else None
    assert cur == conv_before, "main conversation touched by direct execute!"


def test_library_dispatch_binds_fires_and_dies(_tguard, _mock_gen, monkeypatch):
    """Library binding without a browser: only the browser edge is stood in.

    Proves check_library_triggers() maps the fired lib id to OUR entry
    (unknown ids ignored), auto-executes the bound skill in the hidden
    session, then one-time death (entry+skill+session) with silent re-poll.
    No network: skill-gen and run_loop are mocked; everything else is real.
    """
    import core.tools.trigger.tool as T
    import core.agent.loop.engine as loop_mod
    from core.trigger import engine as ENG
    monkeypatch.setattr(T, "_library_start", lambda cleaned: "TESTLIBx")
    monkeypatch.setattr(T, "_library_fired", lambda: [
        {"trigger_id": "someone-else", "kind": "text_present", "value": "x",
         "status": "fired", "matched": "zzz", "detail": {},
         "url": "https://other.example", "title": "Other",
         "reason": "TEST", "ts": 1.0},
        {"trigger_id": "TESTLIBx", "kind": "text_present", "value": "Example",
         "status": "fired", "matched": "Example",
         "detail": {}, "url": "https://example.com", "title": "Example",
         "reason": "TEST", "ts": 2.0},
    ])
    calls = []

    def fake_run_loop(**kwargs):
        calls.append(kwargs)
        return {"final_answer": "TEST done", "steps": 1, "success": True}

    monkeypatch.setattr(loop_mod, "run_loop", fake_run_loop)
    r = T.execute(action="create", trigger_name="manage_triggers",
                  task="TEST lib dispatch", config={"url": "https://example.com",
                  "kind": "text_present", "value": "Example", "match": "contains",
                  "check_interval_s": 2, "timeout_s": 120})
    tid = r["trigger"]["trigger_id"]
    try:
        assert r["trigger"].get("library_trigger_id") == "TESTLIBx"
        assert ENG.check_library_triggers() == [tid]
        assert calls, "bound skill never executed"
        assert calls[0].get("isolated") is True
        assert calls[0].get("session_key") == "trigger:" + tid
        assert ENG.check_library_triggers() == [], "re-poll must be silent after death"
        remaining = [t for t in T.execute(action="list")["triggers"] if t.get("trigger_id") == tid]
        assert not remaining, "entry not deleted"
    finally:
        try:
            T.execute(action="delete", trigger_id=tid)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Library layer (manage_triggers): validation + mocked lifecycle.
# No browser, no LLM, no network. Deterministic and isolated.
# ---------------------------------------------------------------------------

def test_library_config_validation():
    import core.tools.trigger.tool as T
    good_single = T._validate_library_config(
        {"url": "https://example.com", "kind": "text_present", "value": "price"})
    assert good_single["url"] == "https://example.com"
    assert good_single["kind"] == "text_present"
    assert good_single["match"] == "contains"
    good_multi = T._validate_library_config({
        "url": "https://example.com", "timeout_s": 0,
        "conditions": [{"kind": "text_present", "value": "a"},
                       {"kind": "url_contains", "value": "b", "match": "exact"}]})
    assert len(good_multi["conditions"]) == 2
    assert good_multi["timeout_s"] == 0
    for bad in [
        {},  # missing url
        {"url": "ftp://example.com", "kind": "text_present"},  # bad scheme
        {"url": "https://example.com"},  # neither kind nor conditions
        {"url": "https://example.com", "kind": "", "conditions": None},
        {"url": "https://example.com", "kind": "text_present", "value": "x",
         "match": "fuzzy"},  # bad match
        {"url": "https://example.com", "kind": "text_present",
         "check_interval_s": 60},  # out of 1..30
        {"url": "https://example.com", "kind": "text_present",
         "timeout_s": -5},  # negative
        {"url": "https://example.com", "conditions": []},  # empty conditions
        {"url": "https://example.com", "conditions": [{"value": "x"}]},  # no kind
    ]:
        try:
            T._validate_library_config(bad)
            raise AssertionError(f"not rejected: {bad}")
        except (ValueError, TypeError):
            pass


def test_library_create_delete_lifecycle(_tguard, _mock_gen, monkeypatch):
    import core.tools.trigger.tool as T
    from core.trigger import engine as ENG
    monkeypatch.setattr(ENG, "_rearm_library_entry", lambda entry: None)
    started = {}

    def fake_start(cleaned):
        assert cleaned["url"] == "https://example.com/page"
        started["n"] = started.get("n", 0) + 1
        return f"t{started['n']}"

    stopped = []
    monkeypatch.setattr(T, "_library_start", fake_start)
    monkeypatch.setattr(T, "_library_stop", lambda lib_id: stopped.append(lib_id) or True)
    r = T.execute(action="create", trigger_name="manage_triggers",
                  task="TEST library watch",
                  config={"url": "https://example.com/page",
                          "kind": "text_present", "value": "order"})
    trig = r["trigger"]
    assert trig["engine"] == "library"
    assert trig["library_trigger_id"] == "t1"
    assert trig["library_url"] == "https://example.com/page"
    tid = trig["trigger_id"]
    try:
        d = T.execute(action="delete", trigger_id=tid)
        assert d["skill_deleted"] is True
        assert stopped == ["t1"], stopped
    finally:
        try:
            T.execute(action="delete", trigger_id=tid)
        except Exception:
            pass


def test_library_create_failure_leaves_no_orphan(_tguard, _mock_gen, monkeypatch):
    import core.tools.trigger.tool as T
    monkeypatch.setattr(T, "_library_start",
                        lambda cleaned: (_ for _ in ()).throw(
                            RuntimeError("browser stopped. Start it first.")))
    skills_before = {p.name for p in _tguard[2].iterdir()}
    try:
        T.execute(action="create", trigger_name="manage_triggers",
                  task="TEST library down",
                  config={"url": "https://example.com/x", "kind": "text_present"})
        raise AssertionError("browser-down create accepted")
    except RuntimeError:
        pass
    assert {p.name for p in _tguard[2].iterdir()} == skills_before
    assert all(t.get("task") != "TEST library down"
               for t in T.execute(action="list")["triggers"])


def test_library_update_restarts_on_identity_change(_tguard, _mock_gen, monkeypatch):
    import core.tools.trigger.tool as T
    from core.trigger import engine as ENG
    monkeypatch.setattr(ENG, "_rearm_library_entry", lambda entry: None)
    calls = {"start": 0, "stop": []}

    def fake_start(cleaned):
        calls["start"] += 1
        return f"t{calls['start']}"

    monkeypatch.setattr(T, "_library_start", fake_start)
    monkeypatch.setattr(T, "_library_stop", lambda lib_id: calls["stop"].append(lib_id) or True)
    r = T.execute(action="create", trigger_name="manage_triggers",
                  task="TEST library update",
                  config={"url": "https://example.com/a", "kind": "text_present"})
    tid = r["trigger"]["trigger_id"]
    try:
        assert r["trigger"]["library_trigger_id"] == "t1"
        u = T.execute(action="update", trigger_id=tid,
                      config={"url": "https://example.com/b", "kind": "text_present"})
        assert u["skill_regenerated"] is True
        assert calls["stop"] == ["t1"], calls
        assert u["trigger"]["library_trigger_id"] == "t2"
        assert u["trigger"]["library_url"] == "https://example.com/b"
    finally:
        try:
            T.execute(action="delete", trigger_id=tid)
        except Exception:
            pass


def test_library_poll_runs_bound_skill(_tguard, _mock_gen, monkeypatch):
    import core.tools.trigger.tool as T
    from core.trigger import engine as ENG
    monkeypatch.setattr(T, "_library_start", lambda cleaned: "t9")
    monkeypatch.setattr(T, "_library_stop", lambda lib_id: True)
    monkeypatch.setattr(T, "_library_pending_ids", lambda: {"t9"})
    monkeypatch.setattr(T, "_library_fired",
                        lambda: [{"trigger_id": "t9", "status": "fired",
                                  "matched": "order now"}])
    ran = []
    monkeypatch.setattr(ENG, "_execute_trigger_skill",
                        lambda trig: (ran.append(trig.get("trigger_id")),
                                      ENG._delete_skill_and_trigger(trig.get("trigger_id"))))
    monkeypatch.setattr(ENG, "_rearm_library_entry", lambda entry: None)
    r = T.execute(action="create", trigger_name="manage_triggers",
                  task="TEST library poll",
                  config={"url": "https://example.com/p", "kind": "text_present"})
    tid = r["trigger"]["trigger_id"]
    try:
        done = ENG.check_library_triggers()
        assert done == [tid], done
        assert ran == [tid], ran
        assert all(t.get("trigger_id") != tid
                   for t in T.execute(action="list")["triggers"])
    finally:
        try:
            T.execute(action="delete", trigger_id=tid)
        except Exception:
            pass
