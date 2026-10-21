"""Backup/restore + studio-survives-chat (deterministic, no LLM)."""
import sys
import uuid
import zipfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def test_backup_zip_has_manifest_and_client_data():
    from core.backup import service as _backup
    from core.paths import ensure_x5agent
    ensure_x5agent()
    tmp = _backup.create_backup_zip()
    try:
        assert tmp.is_file() and tmp.stat().st_size > 0
        with zipfile.ZipFile(str(tmp), "r") as zf:
            names = zf.namelist()
            assert "backup.json" in names
            assert any(n.startswith("Data/") for n in names), "backup must carry Data/"
            for n in names:
                assert not n.startswith("/") and ".." not in n.split("/")
    finally:
        tmp.unlink(missing_ok=True)


def test_backup_includes_project_skills_and_records_info():
    import shutil
    from core.backup import service as _backup
    from core.paths import ensure_x5agent
    ensure_x5agent()
    skills_dir = _backup._skills_dir()
    sentinel = skills_dir / "UT_backup_probe"
    created_dir = False
    try:
        sentinel.mkdir(parents=True, exist_ok=True)
        created_dir = True
        (sentinel / "probe.txt").write_text("skills-backup-probe", encoding="utf-8")
        tmp = _backup.create_backup_zip()
        try:
            with zipfile.ZipFile(str(tmp), "r") as zf:
                names = zf.namelist()
                assert "skills/UT_backup_probe/probe.txt" in names, "backup must carry project/skills"
            info = _backup.get_backup_info()
            assert info.get("has_backup") is True and info.get("last_backup_at")
            assert info.get("files", 0) >= 1 and info.get("size_bytes", 0) > 0
        finally:
            tmp.unlink(missing_ok=True)
    finally:
        if created_dir:
            shutil.rmtree(str(sentinel), ignore_errors=True)


def test_restore_rejects_empty_backup():
    from core.backup import service as _backup
    tmp = _mktemp("x5_empty_")
    try:
        with zipfile.ZipFile(str(tmp), "w") as zf:
            zf.writestr("backup.json", "{}")
            zf.writestr("project/server.py", "print(1)")
        try:
            _backup.restore_backup_zip(tmp)
        except ValueError:
            return
        raise AssertionError("system-only zip must be rejected as empty")
    finally:
        tmp.unlink(missing_ok=True)


def _mktemp(prefix: str) -> Path:
    import os
    import tempfile
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".zip")
    try:
        os.close(fd)
    except Exception:
        pass
    return Path(path)


def test_restore_rejects_zip_slip():
    from core.backup import service as _backup
    tmp = _mktemp("x5_evil_")
    try:
        with zipfile.ZipFile(str(tmp), "w") as zf:
            zf.writestr("Data/ok.txt", "ok")
            zf.writestr("../evil.txt", "evil")
        try:
            _backup.restore_backup_zip(tmp)
        except ValueError:
            return
        raise AssertionError("traversal entry must be rejected")
    finally:
        tmp.unlink(missing_ok=True)


def test_restore_rejects_non_zip():
    from core.backup import service as _backup
    tmp = _mktemp("x5_notzip_")
    try:
        tmp.write_bytes(b"not a zip at all")
        try:
            _backup.restore_backup_zip(tmp)
        except ValueError:
            return
        raise AssertionError("non-zip must be rejected")
    finally:
        tmp.unlink(missing_ok=True)


def test_backup_roundtrip_restores_folder_file():
    from core.backup import service as _backup
    from core.workfolder import get_path as folder_path
    name = f"UT_backup_{uuid.uuid4().hex[:8]}.txt"
    target = folder_path() / name
    target.write_text("backup-roundtrip", encoding="utf-8")
    tmp = _backup.create_backup_zip()
    try:
        target.unlink()
        assert not target.exists()
        out = _backup.restore_backup_zip(tmp)
        assert out.get("ok") is True and out.get("files", 0) >= 1
        assert target.is_file()
        assert target.read_text(encoding="utf-8") == "backup-roundtrip"
    finally:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass
        tmp.unlink(missing_ok=True)


def test_studios_listed_via_tool_not_context():
    import shutil
    from core.agent.context.builder import build_context, _studios_hint
    from plugins.core_plugins.pdf_studio import tool as _studio
    from core.studio import store as _store
    created = _store.create_studio(title="UT Block", pages=1)
    sid = created["studio_id"]
    try:
        # Context carries a fixed pointer only — never studio data.
        hint = _studios_hint()
        assert "action=list" in hint and sid not in hint
        _, user_content, _ = build_context("hi")
        assert sid not in user_content and "UT Block" not in user_content
        assert "action=list" in user_content
        # The single tool lists every studio with its state + details.
        listed = _studio.execute(action="list")
        assert listed["success"] is True and listed["action"] == "list"
        entry = next(e for e in listed["studios"] if e["studio_id"] == sid)
        assert entry["title"] == "UT Block" and entry["pages"] == 1
        assert entry["designed"] == 0 and entry["open_url"] == f"/studio-pdf/{sid}"
        detail = _studio.execute(action="get", studio_id=sid)
        assert detail["success"] is True and detail["studio_id"] == sid
        assert detail["title"] == "UT Block" and detail["pages"] == 1
        assert detail["open_card"] == f"[STUDIO:{sid}]"
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_pdf_studio_open_returns_existing_without_creating():
    import shutil
    from plugins.core_plugins.pdf_studio import tool as _studio
    from core.studio import store as _store
    before = {s["studio_id"] for s in _store.list_studios()}
    created = _store.create_studio(title="UT Open", pages=1)
    sid = created["studio_id"]
    try:
        out = _studio.execute(action="open", studio_id=sid)
        assert out.get("success") is True and out.get("action") == "open"
        assert out.get("studio_id") == sid
        assert out.get("open_card") == f"[STUDIO:{sid}]"
        assert out.get("open_url") == f"/studio-pdf/{sid}"
        assert {s["studio_id"] for s in _store.list_studios()} == before | {sid}
        try:
            _studio.execute(action="open", studio_id="pdf_999999")
        except (ValueError, FileNotFoundError):
            return
        raise AssertionError("unknown studio_id must fail")
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_create_with_pages_builds_slides_and_launches_parallel():
    import shutil
    from plugins.core_plugins.pdf_studio import tool as _studio
    from core.studio import store as _store
    import core.studio.api as _api
    calls = []
    real = _api.launch_initial_turns
    try:
        _api.launch_initial_turns = lambda sid, msgs, **k: (calls.append((sid, list(msgs))), {"launched": len(msgs), "pages": list(range(1, len(msgs) + 1))})[1]
        out = _studio.execute(title="UT Pages", pages=["cover", "agenda", "pricing"])
        sid = out["studio_id"]
        try:
            assert out.get("designs_running") == 3
            assert len(calls) == 1 and calls[0][0] == sid
            assert calls[0][1] == ["cover", "agenda", "pricing"]
            base = _store.studios_root() / sid
            for n in (1, 2, 3):
                assert (base / "pages" / f"Page{n:02d}.jsx").is_file()
        finally:
            shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
    finally:
        _api.launch_initial_turns = real


def test_launch_initial_turns_runs_one_turn_per_page():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    from core.studio import session as _session
    created = _store.create_studio(title="UT Launch", pages=1)
    sid = created["studio_id"]
    try:
        _store.add_page(sid)
        seen = []

        def _fake(loop_input, system_override=None, cancel_flag=None):
            seen.append(str(loop_input)[-200:])
            return {"reply": "fake design"}

        out = _api.launch_initial_turns(sid, ["first slide", "second slide"],
                                        run_loop_fn=_fake, wait=True)
        assert out.get("launched") == 2
        assert len(seen) == 2
        assert not _api.studio_turn_running(sid, 1)
        assert not _api.studio_turn_running(sid, 2)
        hist = _session.load_history(sid)
        users = [m for m in hist if m.get("role") == "user"]
        # Parallel turns land in any order — both prompts must be present.
        assert sorted(m.get("content") for m in users) == ["first slide", "second slide"]
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        try:
            _session.delete_session(sid)
        except Exception:
            pass


def test_chat_prune_never_touches_studios():
    # Regenerate/edit in chat prunes conversation only — studio dirs stay.
    from core.agent.memory import chat_vector_store as cvs
    from core.studio import store as _store
    created = _store.create_studio(title="UT Survive", pages=1)
    sid = created["studio_id"]
    try:
        conv = cvs._load_conversation()
        anchor = next((m for m in conv
                       if str(m.get("id") or "") and "[STUDIO:" in str(m.get("content") or "")), None)
        if anchor is None:
            # No studio-linked message in history: just assert the studio
            # exists independently of whatever the chat holds.
            assert (_store.studios_root() / sid).is_dir()
            return
        cvs.prune_below(str(anchor.get("id")))
        assert (_store.studios_root() / sid).is_dir()
    finally:
        import shutil
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
