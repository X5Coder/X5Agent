"""PDF Studio validation tests (no LLM, no network, deterministic)."""
import shutil
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(autouse=True)
def _clean_studio_data():
    """Never leak test studios/sessions into the real Data sandbox."""
    from core.studio import store as _store
    from core.studio.session import session_path as _session_path
    before_dirs = {p.name for p in _store.studios_root().iterdir()} if _store.studios_root().is_dir() else set()
    sdir = _session_path("pdf_1").parent
    before_sessions = {p.name for p in sdir.iterdir()} if sdir.is_dir() else set()
    yield
    for child in list(_store.studios_root().iterdir()):
        if child.name not in before_dirs and child.is_dir() and child.name.startswith("pdf_"):
            shutil.rmtree(str(child), ignore_errors=True)
    if sdir.is_dir():
        for f in list(sdir.iterdir()):
            if f.name not in before_sessions and f.suffix == ".json":
                try:
                    f.unlink()
                except Exception:
                    pass


def test_specs_load_with_correct_names():
    import json
    for name in ("pdf_studio", "studio_page"):
        spec = json.loads((PROJECT_ROOT / "plugins" / "core_plugins" / name / "spec.json").read_text(encoding="utf-8"))
        assert spec["name"] == name


def test_system_prompt_is_english_and_mandates_approve():
    text = (PROJECT_ROOT / "prompts" / "pdf_studio_system.md").read_text(encoding="utf-8")
    assert "studio_page" in text and "MUST" in text
    assert "Never ask questions" in text
    assert "استخدم" not in text
    # Constrained turn: budget, image discipline, section edits, must-deliver.
    assert "action=\"deliver\"" in text and "action=\"patch\"" in text
    assert "12 tool calls" in text and "2 image_search calls" in text
    # Export-safe images + exact pdf-motion box method.
    assert "crossOrigin" in text and "<bdi>" in text
    assert "minHeight: 0" in text
    # Exactly 4 studio tools: approve + read-other + image_search + http.
    # No tool_search. Photos ONLY via image_search (no manual URL catalog).
    assert "studio_read" in text and "http_request" in text
    assert "image_search" in text
    assert "tool_search" not in text
    assert 'action="test"' in text
    # Fonts-only SOURCES (photos come from the image_search tool, never URLs).
    assert "SOURCES" in text and "openverse" not in text and "IMG-1" not in text
    assert "Professional bar" in text and "Color law" in text
    # Web-asset auto-save + deliberate fetch. Vision stays OUT of the prompt
    # (it is advertised per-turn in context only when a model is behind it).
    assert 'action="fetch"' in text and "vision_analyze" not in text
    # Self-contained studio: zero Workspace words anywhere in the prompt.
    assert "Workspace" not in text and "workspace" not in text


def _data_url(blob: bytes, mime: str) -> str:
    import base64
    return f"data:{mime};base64," + base64.b64encode(blob).decode("ascii")


def test_upload_accepts_image_and_font_rejects_junk():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    created = _store.create_studio(title="UT Upload", pages=1)
    sid = created["studio_id"]
    try:
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        code, out = _api.handle_upload({"studio_id": sid, "kind": "image",
                                        "name": "hero.png", "data": _data_url(png, "image/png")})
        assert code == 200 and out["file"] == "hero.png"
        assert (_store.studios_root() / sid / "assets" / "images" / "hero.png").is_file()
        ttf = b"\x00\x01\x00\x00" + b"\x00" * 64
        code, out = _api.handle_upload({"studio_id": sid, "kind": "font",
                                        "name": "Cairo.ttf", "data": _data_url(ttf, "font/ttf")})
        assert code == 200 and out["file"] == "Cairo.ttf"
        # Re-upload same name collides safely.
        code, out = _api.handle_upload({"studio_id": sid, "kind": "image",
                                        "name": "hero.png", "data": _data_url(png, "image/png")})
        assert code == 200 and out["file"] == "hero_2.png"
        # Traversal neutralized to a safe basename inside assets (never escapes).
        code, out = _api.handle_upload({"studio_id": sid, "kind": "image", "name": "../evil.png",
                                        "data": _data_url(png, "image/png")})
        assert code == 200 and out["file"] == "evil.png"
        assert (_store.studios_root() / sid / "assets" / "images" / "evil.png").is_file()
        assert not (_store.studios_root() / "evil.png").exists()
        # Junk rejected: bad ext, wrong kind, bad magic, unknown studio.
        for bad in ({"studio_id": sid, "kind": "image", "name": "a.exe", "data": _data_url(png, "image/png")},
                    {"studio_id": sid, "kind": "video", "name": "a.mp4", "data": _data_url(png, "video/mp4")},
                    {"studio_id": sid, "kind": "image", "name": "fake.png", "data": _data_url(b"nope", "image/png")},
                    {"studio_id": "pdf_999999", "kind": "image", "name": "a.png", "data": _data_url(png, "image/png")}):
            code, _ = _api.handle_upload(bad)
            assert code in (400, 404), bad
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_main_prompts_route_pdf_to_studio():
    mission = (PROJECT_ROOT / "prompts" / "general_mission.md").read_text(encoding="utf-8")
    system = (PROJECT_ROOT / "prompts" / "system.md").read_text(encoding="utf-8")
    assert "pdf_studio" in mission and "pdf_create" not in mission
    assert "pdf_studio" in system
    # Main agent asks ONLY for the PDF name, then passes it as title.
    assert "ONLY for the PDF name" in mission
    assert "as title" in mission
    assert "ONLY for the PDF name" in system


def test_dispatcher_registers_new_tools_and_drops_old():
    from core.agent.loop.dispatcher import _DISCOVERABLE_MAP
    assert _DISCOVERABLE_MAP.get("pdf_studio") == "plugins.core_plugins.pdf_studio.tool"
    assert _DISCOVERABLE_MAP.get("studio_page") == "plugins.core_plugins.studio_page.tool"
    assert _DISCOVERABLE_MAP.get("studio_read") == "plugins.core_plugins.studio_read.tool"
    assert "pdf_create" not in _DISCOVERABLE_MAP
    assert "studio_approve" not in _DISCOVERABLE_MAP
    assert "studio_read_slide" not in _DISCOVERABLE_MAP
    assert "studio_patch" not in _DISCOVERABLE_MAP


def test_store_creates_reads_and_validates():
    import shutil
    from core.studio import store as _store
    created = _store.create_studio(title="UT", pages=2)
    try:
        info = _store.get_studio(created["studio_id"])
        assert len(info["pages"]) == 2
        assert "Slide 1: empty" in _store.slide_states_text(created["studio_id"])
        try:
            _store.validate_studio_id("../evil")
            raise AssertionError("must reject")
        except ValueError:
            pass
    finally:
        shutil.rmtree(str(_store.studios_root() / created["studio_id"]), ignore_errors=True)


def test_approve_rejects_bad_code_accepts_good():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    created = _store.create_studio(title="UT Approve", pages=1)
    sid = created["studio_id"]
    try:
        # Writes need a live turn on the tasked slide.
        assert _page.execute(studio_id=sid, action="deliver", page_no=1,
                             code="export default function Page01() { return 1; }")["success"] is False
        _store.mark_designing(sid, 1)
        bad = _page.execute(studio_id=sid, action="deliver", page_no=1, code="hi")
        assert bad["success"] is False and bad["error"]
        good_code = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        good = _page.execute(studio_id=sid, action="deliver", page_no=1, code=good_code)
        assert good["success"] is True and good["error"] == ""
        assert "has code" in _store.slide_states_text(sid)
        # A large full-page design passes the dispatcher (no 10k args cap).
        from core.agent.loop.dispatcher import execute_tool
        big = "export default function Page01() { return (<section>" + ("<p>x</p>" * 3000) + "</section>); }"
        assert len(big) > 20000
        out = execute_tool("studio_page", {"studio_id": sid, "action": "deliver",
                                           "page_no": 1, "code": big})
        assert out["success"] is True and out["output"]["success"] is True
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_read_slide_returns_empty_then_code():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from plugins.core_plugins.studio_read import tool as _read
    from core.studio import store as _store
    created = _store.create_studio(title="UT Read", pages=1)
    sid = created["studio_id"]
    try:
        # Read-only view works with no live turn (blank at first).
        first = _read.execute(studio_id=sid, page_no=1)
        assert first["success"] is True and first["has_code"] is False
        # Approval reads need the tasked slide's live turn.
        refused = _page.execute(studio_id=sid, action="read", page_no=1)
        assert refused["success"] is False and "studio_read" in refused["error"]
        listed = _page.execute(studio_id=sid, action="list")
        assert listed["success"] is True and listed["slides"][0]["state"] == "empty, no code"
        code = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _store.mark_designing(sid, 1)
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=code)
        second = _page.execute(studio_id=sid, action="read", page_no=1)
        assert second["has_code"] is True and "Page01" in second["code"]
        listed2 = _page.execute(studio_id=sid, action="list")
        assert listed2["slides"][0]["state"] == "has code"
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_events_format():
    from plugins.core_plugins.pdf_studio import events as _e1
    from plugins.core_plugins.studio_page import events as _e2
    assert "Studio" in _e1.format_event(studio_id="pdf_1")
    assert "Renaming" in _e1.format_event(action="rename", studio_id="pdf_1")
    assert "1" in _e2.format_event(action="deliver", studio_id="pdf_1", page_no=1)
    assert "2" in _e2.format_event(action="read", studio_id="pdf_1", page_no=2)
    assert "1" in _e2.format_event(action="patch", studio_id="pdf_1", page_no=1)
    assert "Listing" in _e2.format_event(action="list")


def test_create_rejects_duplicate_title_and_rename_via_same_tool():
    import shutil
    from plugins.core_plugins.pdf_studio import tool as _studio
    from core.studio import store as _store
    first = _studio.execute(action="create", title="UT Dup Name")
    try:
        try:
            _studio.execute(action="create", title="ut dup name")
            raise AssertionError("duplicate must fail")
        except ValueError as e:
            assert "already used" in str(e)
        renamed = _studio.execute(action="rename", studio_id=first["studio_id"], title="UT New Name")
        assert renamed["success"] is True and renamed["title"] == "UT New Name"
        assert renamed["open_card"] == f"[STUDIO:{first['studio_id']}]"
        info = _store.get_studio(first["studio_id"])
        assert info["meta"]["title"] == "UT New Name"
        try:
            _studio.execute(action="bad_action_xyz", title="x")
            raise AssertionError("bad action must fail")
        except ValueError:
            pass
    finally:
        shutil.rmtree(str(_store.studios_root() / first["studio_id"]), ignore_errors=True)


def test_one_code_per_slide_file_overwrites():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    created = _store.create_studio(title="UT Overwrite", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        good = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=good)
        second = good.replace("Hi", "Second")
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=second)
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "Second" in text and "Hi</h1>" not in text
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_approve_rejects_non_react_imports():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    created = _store.create_studio(title="UT Imports", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        bad = ("import { Award } from 'lucide-react';\n"
               "export default function Page01() { return (<section><h1>Hi</h1></section>); }")
        res = _page.execute(studio_id=sid, action="deliver", page_no=1, code=bad)
        assert res["success"] is False and "lucide-react" in res["error"]
        ok = ("import React from 'react';\n"
              "export default function Page01() { return (<section><h1>Hi</h1></section>); }")
        res2 = _page.execute(studio_id=sid, action="deliver", page_no=1, code=ok)
        assert res2["success"] is True
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_status_lifecycle_and_states_block():
    import shutil
    from core.studio import store as _store
    created = _store.create_studio(title="UT States", pages=3)
    sid = created["studio_id"]
    try:
        block = _store.slide_states_block(sid, 2)
        assert "Slide 1: empty, no code" in block
        assert "YOUR TASK HERE" in block and "studio_page" in block
        assert f'studio_id="{sid}"' in block and "page_no=2" in block
        from plugins.core_plugins.studio_page import tool as _page
        _store.mark_designing(sid, 1)
        good = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=good)
        _store.append_page_error(sid, 3, "boom")
        block2 = _store.slide_states_block(sid, 2)
        assert "Slide 1: has code" in block2
        assert "Slide 3: code under construction" in block2 and "boom" in block2
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_purge_studios_removes_data_and_sessions():
    # purge_studios() wipes EVERYTHING by design, so this test backs up the
    # live Data first and restores it afterwards — it must NEVER destroy
    # real user studios/sessions.
    import shutil
    import tempfile
    from core.studio import store as _store
    from core.studio import session as _session
    from core.studio.session import session_path as _session_path
    tmp = Path(tempfile.mkdtemp(prefix="x5_purge_ut_"))
    studios_bak = tmp / "studios"
    sessions_bak = tmp / "sessions"
    try:
        sroot = _store.studios_root()
        if sroot.is_dir():
            shutil.copytree(str(sroot), str(studios_bak))
        sdir = _session_path("pdf_1").parent
        if sdir.is_dir():
            sessions_bak.mkdir(parents=True, exist_ok=True)
            for f in sdir.iterdir():
                if f.is_file() and f.suffix == ".json":
                    shutil.copy2(str(f), str(sessions_bak / f.name))
        created = _store.create_studio(title="UT Purge", pages=1)
        _session.append_messages(created["studio_id"], [{"role": "user", "content": "hi"}])
        assert (_store.studios_root() / created["studio_id"]).is_dir()
        purged = _store.purge_studios()
        assert purged["studios"] >= 1
        assert not (_store.studios_root() / created["studio_id"]).exists()
    finally:
        try:
            sroot = _store.studios_root()
            if sroot.is_dir():
                shutil.rmtree(str(sroot), ignore_errors=True)
            if studios_bak.is_dir():
                shutil.copytree(str(studios_bak), str(sroot))
            sdir = _session_path("pdf_1").parent
            sdir.mkdir(parents=True, exist_ok=True)
            if sessions_bak.is_dir():
                for f in sessions_bak.iterdir():
                    if f.is_file():
                        shutil.copy2(str(f), str(sdir / f.name))
        finally:
            shutil.rmtree(str(tmp), ignore_errors=True)


def test_context_carries_tools_first_size_and_attachments():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    created = _store.create_studio(title="UT Ctx", pages=2)
    try:
        ctx = _api.build_studio_context(created["studio_id"], 2, "Make it green",
                                        [{"name": "logo.png", "type": "png", "size": "70 bytes"}])
        assert "studio_page" in ctx.split("All other tools")[0]
        assert "794x1123" in ctx
        assert "Slide 1: empty, no code" in ctx and "YOUR TASK HERE" in ctx
        assert "logo.png" in ctx
    finally:
        shutil.rmtree(str(_store.studios_root() / created["studio_id"]), ignore_errors=True)


def test_page_error_and_page_get_handlers():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    from plugins.core_plugins.studio_page import tool as _page
    created = _store.create_studio(title="UT PageIO", pages=1)
    sid = created["studio_id"]
    try:
        code, out = _api.handle_page_get(f"studio_id={sid}&page_no=1")
        assert code == 200 and out["has_code"] is False
        _store.mark_designing(sid, 1)
        good = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=good)
        code, out = _api.handle_page_get(f"studio_id={sid}&page_no=1")
        assert code == 200 and out["has_code"] is True and "Page01" in out["code"]
        code, out = _api.handle_page_error({"studio_id": sid, "page_no": 1, "error": "kaboom"})
        assert code == 200 and out["success"] is True
        assert "under construction" in _store.slide_states_block(sid, 1)
        code, out = _api.handle_page_error({"studio_id": sid, "page_no": 99, "error": "x"})
        assert code == 400
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_history_undo_redo_steps_through_states():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    from plugins.core_plugins.studio_page import tool as _page
    created = _store.create_studio(title="UT Hist", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        v1 = "export default function Page01() { return (<section><h1>One</h1></section>); }"
        v2 = "export default function Page01() { return (<section><h1>Two</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=v1)
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=v2)
        code, out = _api.handle_history({"studio_id": sid, "page_no": 1, "direction": "undo"})
        assert code == 200 and out["success"] is True
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "One" in text
        code, out = _api.handle_history({"studio_id": sid, "page_no": 1, "direction": "redo"})
        assert code == 200
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "Two" in text
        code, out = _api.handle_history({"studio_id": sid, "page_no": 1, "direction": "sideways"})
        assert code == 400
        code, out = _api.handle_history({"studio_id": "pdf_999999", "page_no": 1, "direction": "undo"})
        assert code == 404
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_create_starts_with_single_page_and_returns_card_for_agent():
    import shutil
    from plugins.core_plugins.pdf_studio import tool as _studio
    from core.studio import store as _store
    created = _studio.execute(title="UT Card")
    try:
        # One page only; scrolling inside the studio adds the rest one by one.
        assert created["pages"] == 1
        assert created["open_card"] == f"[STUDIO:{created['studio_id']}]"
        assert created["open_url"] == f"/studio-pdf/{created['studio_id']}"
        base = _store.studios_root() / created["studio_id"]
        assert (base / "pages" / "Page01.jsx").is_file()
        assert not (base / "pages" / "Page02.jsx").exists()
        added = _store.add_page(created["studio_id"])
        assert added["page_no"] == 2
        assert (base / "pages" / "Page02.jsx").is_file()
    finally:
        shutil.rmtree(str(_store.studios_root() / created["studio_id"]), ignore_errors=True)


def test_main_spec_is_title_and_reason_only_without_session_leak():
    import json
    spec = json.loads((PROJECT_ROOT / "plugins" / "core_plugins" / "pdf_studio" / "spec.json").read_text(encoding="utf-8"))
    assert spec["name"] == "pdf_studio"
    assert set((spec.get("inputs", {}).get("properties", {})).keys()) == {"title", "reason", "action", "studio_id", "pages"}
    assert spec["inputs"].get("required") == []
    # The main agent knows nothing about pages, code, or delivery tools.
    blob = (spec["description"] + json.dumps(spec["inputs"])).lower()
    assert "studio_page" not in blob and "studio_read_slide" not in blob
    assert "react" not in blob
    # Single-tool contract: list/get/open/create via one tool; studios never
    # auto-listed in context (the model calls action=list first).
    assert "action=create|open" in spec["description"] and "studio_id" in spec["description"]
    assert "action=list" in spec["description"] and "action=get" in spec["description"]


def test_session_tools_hidden_from_main_discovery():
    import importlib.util
    store_path = PROJECT_ROOT / "plugins" / "tool-discovery" / "search" / "store.py"
    spec = importlib.util.spec_from_file_location("tool_discovery_store_ut", str(store_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    names = {t.get("name") for t in mod._collect_discoverable_specs()}
    assert "pdf_studio" in names
    assert "studio_page" not in names
    assert "studio_read" not in names


def test_main_loop_rejects_session_tools_but_studio_allows_them():
    from core.agent.loop.engine import (
        is_studio_call_allowed, _studio_tool_specs, STUDIO_ONLY_TOOLS, STUDIO_SESSION_TOOLS)
    assert STUDIO_ONLY_TOOLS == frozenset({"studio_page", "studio_read"})
    assert STUDIO_SESSION_TOOLS == frozenset({"studio_page", "studio_read", "http_request", "image_search"})
    assert is_studio_call_allowed("studio_page") is False
    assert is_studio_call_allowed("studio_read") is False
    assert is_studio_call_allowed("studio_page", studio_session=True) is True
    assert is_studio_call_allowed("studio_read", studio_session=True) is True
    assert is_studio_call_allowed("http_request", studio_session=True) is True
    assert is_studio_call_allowed("image_search", studio_session=True) is True
    # Studio turns run only their 4 tools — nothing else.
    assert is_studio_call_allowed("tool_search", studio_session=True) is False
    assert is_studio_call_allowed("pdf_studio") is True
    seeded = {name for name, _, _ in _studio_tool_specs()}
    assert {"studio_page", "studio_read", "http_request", "image_search"} <= seeded
    assert "tool_search" not in seeded


def test_create_stores_reason_in_branch_session():
    import shutil
    from plugins.core_plugins.pdf_studio import tool as _studio
    from core.studio import store as _store
    from core.studio import session as _session
    created = _studio.execute(title="UT Reason", reason="investor pitch")
    try:
        hist = _session.load_history(created["studio_id"])
        assert any("investor pitch" in str(m.get("content", "")) for m in hist)
    finally:
        shutil.rmtree(str(_store.studios_root() / created["studio_id"]), ignore_errors=True)
        _session.delete_session(created["studio_id"])


def test_patch_edits_one_section_and_rejects_bad():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    created = _store.create_studio(title="UT Patch", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        # Blank page: patch refused, deliver-first guidance.
        refused = _page.execute(studio_id=sid, action="patch", page_no=1, old="<h1>", new="<h2>")
        assert refused["success"] is False and "deliver" in refused["error"]
        code = ("export default function Page01() { return (<section>"
                "<h1>Title</h1><p>Body</p><p>Body</p></section>); }")
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=code)
        # Missing block refused.
        miss = _page.execute(studio_id=sid, action="patch", page_no=1, old="<h9>Nope</h9>", new="x")
        assert miss["success"] is False and "not found" in miss["error"]
        # Ambiguous block (matches twice) refused.
        dup = _page.execute(studio_id=sid, action="patch", page_no=1, old="<p>Body</p>", new="<p>New</p>")
        assert dup["success"] is False and "2 places" in dup["error"]
        # Unique block patched in place; rest of the file untouched.
        ok = _page.execute(studio_id=sid, action="patch", page_no=1, old="<h1>Title</h1>", new="<h1>Changed</h1>")
        assert ok["success"] is True and ok["error"] == ""
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "<h1>Changed</h1>" in text and "<p>Body</p>" in text
        # Undo still steps back through the patched state.
        stepped = _store.step_page(sid, 1, "undo")
        assert stepped["direction"] == "undo"
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "<h1>Title</h1>" in text
        # Unknown action returns its error as output.
        weird = _page.execute(studio_id=sid, action="explode", page_no=1)
        assert weird["success"] is False and "Unknown action" in weird["error"]
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_designing_flag_lifecycle_and_expiry():
    import json
    import shutil
    from datetime import datetime, timedelta, timezone
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import api as _api
    from core.studio import store as _store
    created = _store.create_studio(title="UT Designing", pages=1)
    sid = created["studio_id"]
    try:
        assert _store.designing_pages(sid) == {}
        _store.mark_designing(sid, 1)
        assert 1 in _store.designing_pages(sid)
        # Slide block reports the running turn; GET exposes it to the shell.
        assert "designing" in _store.slide_states_block(sid, 0)
        code, out = _api.handle_get(f"studio_id={sid}")
        assert code == 200 and 1 in [int(x) for x in out["designing"]]
        # Delivery does NOT clear the flag: deliver-then-patch in one turn
        # must keep working. The turn end clears it instead.
        good = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=good)
        assert 1 in _store.designing_pages(sid)
        listed = _page.execute(studio_id=sid, action="list")
        assert listed["slides"][0]["designing"] is True
        _store.clear_designing(sid, 1)
        assert _store.designing_pages(sid) == {}
        # Stale flags (older than the timeout) are treated as dead.
        _store.mark_designing(sid, 1)
        meta_path = _store.studios_root() / sid / "studio.json"
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["designing"]["1"] = (datetime.now(timezone.utc)
                                 - timedelta(seconds=_store.DESIGNING_TIMEOUT_SEC + 60)).isoformat()
        meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        assert _store.designing_pages(sid) == {}
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_chat_marks_designing_and_clears_when_turn_ends():
    import shutil
    from core.studio import api as _api
    from core.studio import session as _session
    from core.studio import store as _store
    created = _store.create_studio(title="UT ChatFlag", pages=1)
    sid = created["studio_id"]
    try:
        seen = {}

        def _fake_loop(loop_input, system_override=""):
            # While the turn executes, the flag must be live.
            seen["during"] = _store.designing_pages(sid)
            return {"success": True, "reply": "done with no delivery"}

        code, out = _api.handle_chat({"studio_id": sid, "page_no": 1, "message": "hi"},
                                     run_loop_fn=_fake_loop)
        assert code == 200 and out["success"] is True
        assert seen.get("during", {}).get(1)
        # Turn ended (no delivery): flag cleared, assistant reply persisted.
        assert _store.designing_pages(sid) == {}
        hist = _session.load_history(sid)
        assert hist and hist[-1].get("role") == "assistant"
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        _session.delete_session(sid)


def test_orientation_single_size_truth_and_lock():
    import shutil
    from core.studio import api as _api
    from core.studio import session as _session
    from core.studio import store as _store
    from plugins.core_plugins.studio_page import tool as _page
    created = _store.create_studio(title="UT Orient", pages=1)
    sid = created["studio_id"]
    try:
        assert _store.studio_orientation(sid) == "portrait"
        assert _store.studio_size_locked(sid) is False
        dims = _store.studio_dims(sid)
        assert (dims["w"], dims["h"]) == (794, 1123)
        saved = _store.set_orientation(sid, "landscape")
        assert saved["orientation"] == "landscape"
        assert (saved["page"]["w"], saved["page"]["h"]) == (1123, 794)
        try:
            _store.set_orientation(sid, "diagonal")
            raise AssertionError("bad orientation must fail")
        except ValueError:
            pass
        # First request freezes the box for the studio's whole life.
        _store.mark_designing(sid, 1)
        assert _store.studio_size_locked(sid) is True
        try:
            _store.set_orientation(sid, "portrait")
            raise AssertionError("locked switch must fail")
        except ValueError as e:
            assert "locked" in str(e).lower()
        # Context + GET follow the same box (preview == model == export).
        ctx = _api.build_studio_context(sid, 1, "hi")
        assert "1123x794" in ctx and "A4 landscape" in ctx
        code, out = _api.handle_get(f"studio_id={sid}")
        assert code == 200 and out["orientation"] == "landscape"
        assert out["size_locked"] is True and out["page"]["w"] == 1123
        code, out = _api.handle_orientation({"studio_id": sid, "orientation": "portrait"})
        assert code == 400
        code, out = _api.handle_orientation({"studio_id": "pdf_999999", "orientation": "portrait"})
        assert code == 404
        # A designed page locks too, even without a live turn.
        _store.clear_designing(sid, 1)
        good = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _store.mark_designing(sid, 1)
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=good)
        _store.clear_designing(sid, 1)
        assert _store.studio_size_locked(sid) is True
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        _session.delete_session(sid)


def test_pending_cancel_halts_turn_started_after_stop():
    import shutil
    from core.studio import api as _api
    from core.studio import session as _session
    from core.studio import store as _store
    created = _store.create_studio(title="UT PreStop", pages=1)
    sid = created["studio_id"]
    try:
        # STOP with no live turn records a pending cancel (returns False).
        assert _api.request_studio_cancel(sid) is False

        def _instant_loop(loop_input, system_override="", cancel_flag=None):
            assert cancel_flag is not None and cancel_flag.is_set()
            return {"success": False, "cancelled": True, "final_answer": ""}

        code, out = _api.handle_chat({"studio_id": sid, "page_no": 1, "message": "go"},
                                     run_loop_fn=_instant_loop)
        assert code == 200 and out.get("stopped") is True
        assert _store.designing_pages(sid) == {}
        assert _api.studio_turn_running(sid) is False
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        _session.delete_session(sid)


def test_cancel_stops_turn_and_continue_resumes_request():
    import shutil
    import threading
    from core.studio import api as _api
    from core.studio import session as _session
    from core.studio import store as _store
    created = _store.create_studio(title="UT StopGo", pages=1)
    sid = created["studio_id"]
    try:
        release = threading.Event()

        def _slow_loop(loop_input, system_override="", cancel_flag=None):
            assert cancel_flag is not None
            cancel_flag.wait(timeout=10)
            return {"success": False, "cancelled": bool(cancel_flag.is_set()),
                    "final_answer": ""}

        import concurrent.futures as _fut
        with _fut.ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_api.handle_chat,
                              {"studio_id": sid, "page_no": 1, "message": "design red"},
                              _slow_loop)
            import time as _t
            for _ in range(100):
                _t.sleep(0.05)
                if _api.studio_turn_running(sid):
                    break
            assert _api.studio_turn_running(sid) is True
            code, out = _api.handle_cancel({"studio_id": sid})
            assert code == 200 and out["cancelled"] is True
            code, out = fut.result(timeout=15)
            assert code == 200 and out.get("stopped") is True
        assert _api.studio_turn_running(sid) is False
        assert _store.designing_pages(sid) == {}
        # Nothing to continue on an empty studio, but the request persists.
        code, out = _api.handle_cancel({"studio_id": sid})
        assert code == 200 and out["cancelled"] is False
        code, out = _api.handle_continue({"studio_id": sid, "page_no": 1},
                                         run_loop_fn=lambda i, system_override="": {
                                             "success": True, "reply": "continued"})
        assert code == 200 and out.get("stopped") is not True
        hist = _session.load_history(sid)
        assert any(m.get("role") == "user" and "design red" in str(m.get("content", ""))
                   for m in hist)
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        _session.delete_session(sid)


def test_studio_turn_never_persists_to_main_chat():
    import json
    import shutil
    from core.studio import api as _api
    from core.studio import session as _session
    from core.studio import store as _store
    from core.paths import data_root as _data_root
    conv = _data_root() / "database" / "conversation.json"
    before = conv.read_text(encoding="utf-8") if conv.is_file() else ""
    created = _store.create_studio(title="UT NoLeak", pages=1)
    sid = created["studio_id"]
    try:
        code, out = _api.handle_chat({"studio_id": sid, "page_no": 1, "message": "leak check"},
                                     run_loop_fn=lambda i, system_override="": {
                                         "success": True, "reply": "ok"})
        assert code == 200
        after = conv.read_text(encoding="utf-8") if conv.is_file() else ""
        assert after == before
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        _session.delete_session(sid)


def test_studio_minimal_context_is_small_and_codeless():
    from core.agent.context.builder import build_context
    sp, uc, tools = build_context("hi", isolated=True, studio_mode=True)
    assert len(uc) < 3000
    names = [t.get("function", {}).get("name") for t in tools]
    assert names[:4] == ["studio_page", "studio_read", "image_search", "http_request"]
    assert set(names) <= {"studio_page", "studio_read", "image_search",
                          "http_request", "vision_analyze"}
    assert "Workspace map" not in uc and "User Memory" not in uc
    # Self-contained studio context: no Workspace file may be named here.
    assert "Workspace" not in uc


def test_studio_read_views_any_slide_without_writing():
    import shutil
    from plugins.core_plugins.studio_read import tool as _read
    from core.studio import store as _store
    created = _store.create_studio(title="UT StudioRead", pages=2)
    sid = created["studio_id"]
    try:
        blank = _read.execute(studio_id=sid, page_no=2)
        assert blank["success"] is True and blank["has_code"] is False
        # No live turn needed: read-only, even while another turn runs.
        _store.mark_designing(sid, 1)
        still = _read.execute(studio_id=sid, page_no=2)
        assert still["success"] is True
        bad = _read.execute(studio_id="pdf_999999", page_no=1)
        assert bad["success"] is False
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_approval_read_and_test_stay_on_tasked_slide():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    created = _store.create_studio(title="UT TaskedOnly", pages=2)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        good = "export default function Page01() { return (<section><h1>Hi</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=good)
        # Approval read of the tasked slide works.
        own = _page.execute(studio_id=sid, action="read", page_no=1)
        assert own["success"] is True and own["has_code"] is True
        # Approval read of another slide is refused (use studio_read).
        other = _page.execute(studio_id=sid, action="read", page_no=2)
        assert other["success"] is False and "studio_read" in other["error"]
        # Standalone test of the tasked slide passes; other slide refused.
        ok = _page.execute(studio_id=sid, action="test", page_no=1)
        assert ok["success"] is True
        nope = _page.execute(studio_id=sid, action="test", page_no=2)
        assert nope["success"] is False
        bad = _page.execute(studio_id=sid, action="test", page_no=1,
                            code="import { X } from 'nope-lib'; export default 1;")
        assert bad["success"] is False and "nope-lib" in bad["error"]
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_assets_resolve_with_src_and_reject_abuse():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    created = _store.create_studio(title="UT Assets", pages=1)
    sid = created["studio_id"]
    try:
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        code, out = _api.handle_upload({"studio_id": sid, "kind": "image",
                                        "name": "hero.png", "data": _data_url(png, "image/png")})
        assert code == 200
        cand, mime = _store.resolve_asset_file(sid, "images/hero.png")
        assert cand.is_file() and mime == "image/png"
        entries = _store.asset_entries(sid)
        assert len(entries) == 1 and entries[0]["src"].startswith("/api/studio/asset?studio_id=")
        assert "file=images/hero.png" in entries[0]["src"]
        # Abuse rejected: traversal, wrong dir, bad ext, unknown studio/file.
        for bad_sid, bad_rel, exc in ((sid, "../studio.json", ValueError),
                                      (sid, "pages/Page01.jsx", ValueError),
                                      (sid, "images/evil.exe", ValueError),
                                      (sid, "images/missing.png", FileNotFoundError),
                                      ("pdf_999999", "images/hero.png", FileNotFoundError)):
            try:
                _store.resolve_asset_file(bad_sid, bad_rel)
                raise AssertionError(f"must reject {bad_rel}")
            except exc:
                pass
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_context_lists_assets_with_src_and_upload_twice():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    created = _store.create_studio(title="UT CtxAssets", pages=1)
    sid = created["studio_id"]
    try:
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        _api.handle_upload({"studio_id": sid, "kind": "image",
                            "name": "logo.png", "data": _data_url(png, "image/png")})
        ctx = _api.build_studio_context(
            sid, 1, "Make it green",
            [{"name": "logo.png", "type": "image/png", "size": "70 bytes"}])
        # Asset block carries the usable src path for code ...
        assert "src: /api/studio/asset?studio_id=" in ctx
        assert "images/logo.png" in ctx
        # ... and the upload appears a second time in its request section.
        assert ctx.count("logo.png") >= 2
        assert "Uploaded with this request" in ctx
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_edit_after_undo_applies_to_reverted_version():
    """Undo really rewrites the file: a later edit lands on the version on screen."""
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from plugins.core_plugins.studio_read import tool as _read
    from core.studio import store as _store
    created = _store.create_studio(title="UT UndoEdit", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        v1 = "export default function Page01() { return (<section><h1>One</h1></section>); }"
        v2 = "export default function Page01() { return (<section><h1>Two</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=v1)
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=v2)
        stepped = _store.step_page(sid, 1, "undo")
        assert stepped["direction"] == "undo"
        # Reads (what the next turn's context is built on) see the reverted code.
        seen = _read.execute(studio_id=sid, page_no=1)
        assert seen["success"] is True and "<h1>One</h1>" in seen["code"]
        seen2 = _page.execute(studio_id=sid, action="read", page_no=1)
        assert seen2["success"] is True and "<h1>One</h1>" in seen2["code"]
        # A patch with the reverted block applies on the reverted version.
        ok = _page.execute(studio_id=sid, action="patch", page_no=1,
                           old="<h1>One</h1>", new="<h1>One edited</h1>")
        assert ok["success"] is True
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "<h1>One edited</h1>" in text and "Two" not in text
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_edit_context_carries_current_code_not_old_versions():
    """An edit turn sees the CURRENT code + the request only.

    The tasked slide's live code travels upfront (no read round-trip); old
    versions of the same slide live in undo stacks on disk — never sent.
    Other slides travel as states only.
    """
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    from plugins.core_plugins.studio_page import tool as _page
    created = _store.create_studio(title="UT TaskedCode", pages=3)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        v1 = "export default function Page01() { return (<section><h1>MARKER_ALPHA</h1></section>); }"
        v2 = "export default function Page01() { return (<section><h1>MARKER_BETA</h1></section>); }"
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=v1)
        _page.execute(studio_id=sid, action="deliver", page_no=1, code=v2)
        _store.clear_designing(sid, 1)
        _store.mark_designing(sid, 2)
        _page.execute(studio_id=sid, action="deliver", page_no=2,
                      code="export default function Page02() { return (<section><h1>MARKER_GAMMA</h1></section>); }")
        _store.clear_designing(sid, 2)
        ctx = _api.build_studio_context(sid, 2, "change the title color", None)
        # Tasked slide: CURRENT code travels, old version does not.
        assert "MARKER_GAMMA" in ctx and "Tasked slide 2 code" in ctx
        assert "MARKER_ALPHA" not in ctx and "MARKER_BETA" not in ctx
        # Other slides: states only.
        assert "Slide 1: has code" in ctx and "Slide 3: empty" in ctx
        # Blank tasked slide: explicit blank note instead of code.
        ctx_blank = _api.build_studio_context(sid, 3, "design it", None)
        assert "Tasked slide 3 code: (blank" in ctx_blank
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def _local_web():
    """Tiny local HTTP server (images + a font). Returns (base_url, close)."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 200
    woff = b"wOF2" + b"\x00" * 200

    class _H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0].endswith(".png"):
                body, ctype = png, "image/png"
            elif self.path.split("?")[0].endswith(".woff2"):
                body, ctype = woff, "font/woff2"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return f"http://127.0.0.1:{srv.server_port}", lambda: (srv.shutdown(), srv.server_close())


def test_fetch_action_saves_with_query_and_deliver_absorbs_hotlinks():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    base, close = _local_web()
    created = _store.create_studio(title="UT Fetch", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        # Explicit fetch: search words remembered as the asset description.
        got = _page.execute(studio_id=sid, action="fetch", page_no=1,
                            url=f"{base}/cat.png", query="صورة قطة")
        assert got["success"] is True and got["src"].startswith("/api/studio/asset?")
        assert "images/" in got["src"]
        meta = _store.read_asset_meta(sid)
        assert meta.get("images/" + got["file"], {}).get("query") == "صورة قطة"
        entries = _store.asset_entries(sid)
        assert entries and entries[0]["query"] == "صورة قطة"
        # Auto-absorb on deliver: hotlink saved + code rewritten to asset src.
        code = ("export default function Page01() { return (<section>"
                f"<img src=\"{base}/dog.png\" crossOrigin=\"anonymous\" /></section>); }}")
        out = _page.execute(studio_id=sid, action="deliver", page_no=1, code=code)
        assert out["success"] is True and "saved to assets" in out["message"]
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert "/api/studio/asset?studio_id=" in text and f"{base}/dog.png" not in text
        # Bad URLs rejected, never saved.
        bad = _page.execute(studio_id=sid, action="fetch", page_no=1,
                            url=f"{base}/nope.txt", query="x")
        assert bad["success"] is False
        bad2 = _page.execute(studio_id=sid, action="fetch", page_no=1,
                             url="ftp://x/y.png", query="x")
        assert bad2["success"] is False
    finally:
        try:
            close()
        except Exception:
            pass
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_fetch_accepts_workspace_file_from_image_search():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    from core.paths import resolve_inside_workspace
    created = _store.create_studio(title="UT Bridge", pages=1)
    sid = created["studio_id"]
    staged = resolve_inside_workspace("images/ut_bridge_cat.png")
    try:
        _store.mark_designing(sid, 1)
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64)
        # file= wins over url and is copied (not re-downloaded) with meta.
        got = _page.execute(studio_id=sid, action="fetch", page_no=1,
                            file="images/ut_bridge_cat.png",
                            url="http://127.0.0.1:9/never.png", query="cat photo")
        assert got["success"] is True and got["src"].startswith("/api/studio/asset?")
        assert (_store.studios_root() / sid / "assets" / "images" / got["file"]).is_file()
        meta = _store.read_asset_meta(sid)
        assert meta.get("images/" + got["file"], {}).get("query") == "cat photo"
        # Escape blocked, non-images rejected, missing files fail clearly.
        bad_outside = _page.execute(studio_id=sid, action="fetch", page_no=1,
                                    file="../outside.png", query="x")
        assert bad_outside["success"] is False
        bad_missing = _page.execute(studio_id=sid, action="fetch", page_no=1,
                                    file="images/ut_nope.png", query="x")
        assert bad_missing["success"] is False
    finally:
        try:
            staged.unlink(missing_ok=True)
        except Exception:
            pass
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_absorb_is_capped_and_keeps_failed_hotlinks():
    import shutil
    from plugins.core_plugins.studio_page import tool as _page
    from core.studio import store as _store
    base, close = _local_web()
    created = _store.create_studio(title="UT Cap", pages=1)
    sid = created["studio_id"]
    try:
        _store.mark_designing(sid, 1)
        imgs = "".join(f"<img src=\"{base}/p{i}.png\" />" for i in range(4))
        code = ("export default function Page01() { return (<section>" + imgs + "</section>); }")
        out = _page.execute(studio_id=sid, action="deliver", page_no=1, code=code)
        assert out["success"] is True
        text = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        assert text.count("/api/studio/asset?") == 3
        assert f"{base}/p3.png" in text  # 4th kept as hotlink (cap = 3)
    finally:
        try:
            close()
        except Exception:
            pass
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_delete_page_shifts_later_slides():
    import shutil
    from core.studio import api as _api
    from core.studio import store as _store
    from plugins.core_plugins.studio_page import tool as _page
    created = _store.create_studio(title="UT Delete", pages=3)
    sid = created["studio_id"]
    try:
        for n, word in ((1, "One"), (2, "Two"), (3, "Three")):
            _store.mark_designing(sid, n)
            _page.execute(studio_id=sid, action="deliver", page_no=n,
                          code=f"export default function Page0{n}() {{ return (<section><h1>{word}</h1></section>); }}")
            _store.clear_designing(sid, n)
        code, out = _api.handle_delete_page({"studio_id": sid, "page_no": 2})
        assert code == 200 and out["success"] is True and out["pages"] == 2
        t1 = (_store.studios_root() / sid / "pages" / "Page01.jsx").read_text(encoding="utf-8")
        t2 = (_store.studios_root() / sid / "pages" / "Page02.jsx").read_text(encoding="utf-8")
        assert "One" in t1 and "Three" in t2 and "Two" not in t1 + t2
        assert not (_store.studios_root() / sid / "pages" / "Page03.jsx").exists()
        block = _store.slide_states_block(sid, 2)
        assert "Slide 2: has code" in block
        # Live turn on the slide blocks the delete.
        _store.mark_designing(sid, 1)
        code, out = _api.handle_delete_page({"studio_id": sid, "page_no": 1})
        assert code == 400
        _store.clear_designing(sid, 1)
        # Last remaining page can never go.
        code, out = _api.handle_delete_page({"studio_id": sid, "page_no": 2})
        assert code == 200
        code, out = _api.handle_delete_page({"studio_id": sid, "page_no": 1})
        assert code == 400
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_vision_tool_hidden_without_model_shown_with(monkeypatch):
    from core.agent.context import builder as _b
    from core.agent.loop import engine as _e
    monkeypatch.setattr(_b, "_studio_vision_available", lambda: False)
    assert _b._studio_tool_names() == ["studio_page", "studio_read", "image_search", "http_request"]
    assert _e.is_studio_call_allowed("vision_analyze", studio_session=True) is False
    assert _e.is_studio_call_allowed("vision_analyze") is True  # main agent unaffected
    monkeypatch.setattr(_b, "_studio_vision_available", lambda: True)
    assert _b._studio_tool_names() == ["studio_page", "studio_read", "image_search",
                                       "http_request", "vision_analyze"]
    assert _e.is_studio_call_allowed("vision_analyze", studio_session=True) is True
    seeded = {name for name, _, _ in _e._studio_tool_specs()}
    assert "vision_analyze" in seeded


def test_resolve_studio_image_ref_for_vision():
    import shutil
    from core.ai_models.media import resolve_image
    from core.studio import api as _api
    from core.studio import store as _store
    created = _store.create_studio(title="UT VisionRef", pages=1)
    sid = created["studio_id"]
    try:
        png = bytes.fromhex("89504e470d0a1a0a") + b"\x00" * 64
        _api.handle_upload({"studio_id": sid, "kind": "image",
                            "name": "see.png", "data": _data_url(png, "image/png")})
        p = resolve_image(image_path=f"studio:{sid}/images/see.png")
        assert p.is_file() and p.suffix == ".png"
        for bad in (f"studio:{sid}/../studio.json", f"studio:{sid}/pages/Page01.jsx",
                    "studio:pdf_999999/images/see.png"):
            try:
                resolve_image(image_path=bad)
                raise AssertionError(f"must reject {bad}")
            except (FileNotFoundError, ValueError):
                pass
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_parallel_turns_per_page_same_slide_rejected():
    import shutil
    import threading
    from core.studio import api as _api
    from core.studio import session as _session
    from core.studio import store as _store
    created = _store.create_studio(title="UT Parallel", pages=3)
    sid = created["studio_id"]
    try:
        release = threading.Event()

        def _slow_loop(loop_input, system_override="", cancel_flag=None):
            cancel_flag.wait(timeout=10)
            return {"success": False, "cancelled": bool(cancel_flag.is_set()),
                    "final_answer": ""}

        import concurrent.futures as _fut
        with _fut.ThreadPoolExecutor(max_workers=3) as pool:
            fut1 = pool.submit(_api.handle_chat,
                              {"studio_id": sid, "page_no": 1, "message": "design one"},
                              _slow_loop)
            import time as _t
            for _ in range(100):
                _t.sleep(0.05)
                if _api.studio_turn_running(sid, 1):
                    break
            assert _api.studio_turn_running(sid, 1) is True
            # Same slide: second request refused (one slide = one turn).
            code, out = _api.handle_chat({"studio_id": sid, "page_no": 1, "message": "again"},
                                         run_loop_fn=lambda i, system_override="": {
                                             "success": True, "reply": "x"})
            assert code == 400 and "already has a running turn" in out["error"]
            # Different slide: parallel turn starts fine on the same session.
            fut2 = pool.submit(_api.handle_chat,
                              {"studio_id": sid, "page_no": 2, "message": "design two"},
                              _slow_loop)
            for _ in range(100):
                _t.sleep(0.05)
                if _api.studio_turn_running(sid, 2):
                    break
            assert _api.studio_turn_running(sid, 2) is True
            assert _api.studio_turn_running(sid) is True
            # Per-page STOP halts slide 1 only; slide 2 keeps running.
            code, out = _api.handle_cancel({"studio_id": sid, "page_no": 1})
            assert code == 200 and out["cancelled"] is True
            code, out = fut1.result(timeout=15)
            assert code == 200 and out.get("stopped") is True
            assert _api.studio_turn_running(sid, 1) is False
            assert _api.studio_turn_running(sid, 2) is True
            # Studio-wide STOP halts the rest.
            code, out = _api.handle_cancel({"studio_id": sid})
            assert code == 200 and out["cancelled"] is True
            code, out = fut2.result(timeout=15)
            assert code == 200 and out.get("stopped") is True
        assert _api.studio_turn_running(sid) is False
        assert _store.designing_pages(sid) == {}
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)
        _session.delete_session(sid)


# ---- shell + asset transport ------------------------------------------------
# The iframe loads /studio-pdf/shell/<id> (small HTML, always fresh) and the
# content-hashed bundles under /studio-pdf/assets/* (2.8MB, cacheable forever).
# That split is what makes the studio open instantly on every visit instead of
# re-downloading the whole bundle again — and what keeps a new build's renamed
# bundles from being served out of a stale cache.

import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _raw_get(port: int, path: str) -> bytes:
    head = f"GET {path} HTTP/1.1\r\nHost: t\r\nCookie: x5sess=ok\r\nConnection: close\r\n\r\n".encode()
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(head)
        buf = b""
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                chunk = sock.recv(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
        return buf
    finally:
        sock.close()


def test_studio_shell_is_fresh_and_assets_are_cacheable(monkeypatch):
    from core.studio import api as studio_api
    from web.server import console_auth as ca
    from web.server import server as S

    assets = studio_api.studio_dist_dir() / "assets"
    if not assets.is_dir():
        pytest.skip("studio dist is not built")
    names = sorted(p.name for p in assets.iterdir() if p.is_file())
    if not names:
        pytest.skip("studio dist has no assets")

    monkeypatch.setattr(ca, "is_setup", lambda: True)
    monkeypatch.setattr(ca, "session_valid", lambda token: bool(token))
    monkeypatch.setattr(S, "_events_watchers", lambda: None)

    httpd = S.ThreadedServer(("127.0.0.1", 0), S.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # The shell must stay no-store: a new build renames its bundles, so the
        # HTML that points at them may never be replayed from a cache.
        shell = _raw_get(port, "/studio-pdf/shell/pdf_1")
        head = shell.split(b"\r\n\r\n", 1)[0]
        assert b"200" in head.split(b"\r\n")[0], head[:200]
        assert b"Cache-Control: no-store" in head, head[:400]

        # A content-hashed asset is immutable: the browser keeps it forever and
        # the next studio open loads it from disk with zero network traffic.
        asset = _raw_get(port, "/studio-pdf/assets/" + names[0])
        ahead = asset.split(b"\r\n\r\n", 1)[0]
        assert b"200" in ahead.split(b"\r\n")[0], ahead[:200]
        assert b"Cache-Control: public, max-age=31536000, immutable" in ahead, ahead[:400]

        # An unknown asset name still 404s (never an HTML fallback).
        miss = _raw_get(port, "/studio-pdf/assets/nope-000000.js")
        mhead = miss.split(b"\r\n\r\n", 1)[0]
        assert b"404" in mhead.split(b"\r\n")[0], mhead[:200]
    finally:
        httpd.shutdown()
        httpd.server_close()
