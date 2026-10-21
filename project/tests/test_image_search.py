"""image_search tests (no network, deterministic, isolated)."""
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from plugins.core_plugins.image_search import tool as img_tool
from plugins.core_plugins.image_search import events as img_events


def test_spec_loads_and_names_tool():
    spec = json.loads((PROJECT_ROOT / "plugins" / "core_plugins" / "image_search" / "spec.json").read_text(encoding="utf-8"))
    assert spec["name"] == "image_search"
    assert spec["inputs"]["query"]["required"] is True
    assert "metadata" in spec["description"].lower() or "file" in spec["description"].lower()


def test_rejects_bad_query_count_kind():
    with pytest.raises((TypeError, ValueError)):
        img_tool.execute(query="   ")
    with pytest.raises(TypeError):
        img_tool.execute(query=123)
    with pytest.raises(ValueError):
        img_tool.execute(query="cat", count=0)
    with pytest.raises(ValueError):
        img_tool.execute(query="cat", count=11)
    with pytest.raises(ValueError):
        img_tool.execute(query="cat", kind="video")


def test_plan_routes_topical_first_and_caps_sources():
    plan = [f.__name__ for f in img_tool._plan("black cat", "auto")]
    assert plan[0] == "_src_pets"
    assert "_src_openverse" in plan and "_src_wikimedia" in plan
    assert len(plan) <= img_tool.MAX_SOURCES_PER_CALL
    plan_icon = [f.__name__ for f in img_tool._plan("cat icon", "auto")]
    assert plan_icon[0] == "_src_iconify"
    plan_photo = [f.__name__ for f in img_tool._plan("nasa moon", "photo")]
    assert "_src_nasa" not in plan_photo  # explicit photo = general base only
    plan_space = [f.__name__ for f in img_tool._plan("nasa moon landing", "auto")]
    assert "_src_nasa" in plan_space


def test_sniff_detects_formats_and_rejects_text():
    assert img_tool._sniff(b"\xff\xd8\xff\xe0junk", "image/jpeg") == "jpg"
    assert img_tool._sniff(b"\x89PNG\r\n\x1a\njunk", "image/png") == "png"
    assert img_tool._sniff(b"GIF89ajunk", "image/gif") == "gif"
    assert img_tool._sniff(b"RIFF....WEBPjunk", "image/webp") == "webp"
    assert img_tool._sniff(b'<svg xmlns="http://www.w3.org/2000/svg"></svg>', "image/svg+xml") == "svg"
    assert img_tool._sniff(b"<html><body>nope</body></html>", "text/html") is None


def test_openverse_parser_extracts_url_and_credit():
    sample = {"results": [{"url": "https://live.staticflickr.com/1/2_b.jpg",
                           "title": "A cat", "foreign_landing_url": "https://flickr.com/x"}]}
    real = img_tool._get_json
    img_tool._get_json = lambda url: sample
    try:
        out = img_tool._src_openverse("cat", 3)
    finally:
        img_tool._get_json = real
    assert out and out[0]["url"] == "https://live.staticflickr.com/1/2_b.jpg"
    assert out[0]["source"] == "openverse" and "flickr.com" in out[0]["page"]


def test_wikimedia_parser_strips_tracking_params():
    sample = {"query": {"pages": {"1": {"title": "File:Cat.jpg", "imageinfo": [
        {"url": "https://upload.wikimedia.org/x.jpg?utm_source=a", "thumburl": "https://thumb.wikimedia.org/t.jpg?utm_x=1",
         "mime": "image/jpeg", "width": 100, "height": 80, "thumbwidth": 50, "thumbheight": 40,
         "descriptionurl": "https://commons.wikimedia.org/d"}]}}}}
    real = img_tool._get_json
    img_tool._get_json = lambda url: sample
    try:
        out = img_tool._src_wikimedia("cat", 3)
    finally:
        img_tool._get_json = real
    assert out and out[0]["url"] == "https://thumb.wikimedia.org/t.jpg"
    assert "?" not in out[0]["url"] and out[0]["mime"] == "image/jpeg"


def test_flickr_parser_reads_media_m():
    sample = {"items": [{"media": {"m": "https://live.staticflickr.com/1/2_m.jpg"},
                         "title": "t", "link": "https://flickr.com/p"}]}
    real = img_tool._get_json
    img_tool._get_json = lambda url: sample
    try:
        out = img_tool._src_flickr("cat", 3)
    finally:
        img_tool._get_json = real
    assert out and out[0]["url"].endswith("_m.jpg") and out[0]["source"] == "flickr"


def test_execute_saves_verified_copy_with_metadata(monkeypatch, tmp_path):
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    monkeypatch.setattr(img_tool, "_src_openverse",
                        lambda q, n: [{"url": "https://example.com/a.png", "title": "A cat",
                                       "page": "https://example.com", "width": 8, "height": 8,
                                       "mime": "image/png", "source": "openverse"}])
    monkeypatch.setattr(img_tool, "_plan", lambda q, k: [img_tool._src_openverse])
    monkeypatch.setattr(img_tool, "_download_verified", lambda url, hint: (png, "png", "image/png"))
    monkeypatch.setattr(img_tool, "_save",
                        lambda q, s, i, e, d: _save_tmp(tmp_path, q, s, i, e, d))
    out = img_tool.execute(query="black cat", count=2)
    assert out["query"] == "black cat" and len(out["images"]) == 1
    meta = out["images"][0]
    assert meta["mime"] == "image/png" and meta["bytes"] == len(png)
    assert meta["file"].startswith("images/") and meta["source"] == "openverse"


def _save_tmp(tmp_path, query, source, index, ext, data):
    target = tmp_path / f"{source}_{index}.{ext}"
    target.write_bytes(data)
    return f"images/{source}_{index}.{ext}"


def test_execute_fails_clearly_when_nothing_usable(monkeypatch):
    monkeypatch.setattr(img_tool, "_plan", lambda q, k: [])
    monkeypatch.setattr(img_tool, "_src_picsum", lambda q, n: [])
    with pytest.raises(RuntimeError, match="no usable picture"):
        img_tool.execute(query="cat")


def test_events_and_dispatcher_wiring():
    assert img_events.format_event(query="black cat") == "Searching images · black cat"
    from core.agent.loop import dispatcher
    assert dispatcher._DISCOVERABLE_MAP["image_search"] == "plugins.core_plugins.image_search.tool"
    from core.tools import registry
    assert registry.is_core_plugin("image_search") is True


def test_studio_spec_is_self_contained_and_requires_studio():
    import json as _json
    spec = img_tool.studio_spec()
    assert spec["name"] == "image_search"
    assert spec["inputs"]["studio_id"]["required"] is True
    assert spec["inputs"]["query"]["required"] is True
    blob = _json.dumps(spec, ensure_ascii=False).lower()
    assert "workspace" not in blob


def test_studio_mode_saves_into_studio_assets_with_src(monkeypatch):
    import shutil
    from core.studio import store as _store
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
    monkeypatch.setattr(img_tool, "_plan", lambda q, k: [img_tool._src_openverse])
    monkeypatch.setattr(img_tool, "_src_openverse",
                        lambda q, n: [{"url": "https://example.com/a.png", "title": "A cat",
                                       "page": "https://example.com", "width": 8, "height": 8,
                                       "mime": "image/png", "source": "openverse"}])
    monkeypatch.setattr(img_tool, "_download_verified", lambda url, hint: (png, "png", "image/png"))
    created = _store.create_studio(title="UT ImgStudio", pages=1)
    sid = created["studio_id"]
    try:
        out = img_tool.execute(query="black cat", count=2, studio_id=sid)
        assert out["studio_id"] == sid and len(out["images"]) == 1
        meta = out["images"][0]
        assert "file" not in meta  # zero Workspace in studio outputs
        assert meta["src"].startswith("/api/studio/asset?") and meta["asset"].startswith("images/")
        assert (_store.studios_root() / sid / "assets" / "images" / meta["asset"].split("/", 1)[1]).is_file()
        assert "workspace" not in json.dumps(out, ensure_ascii=False).lower()
    finally:
        shutil.rmtree(str(_store.studios_root() / sid), ignore_errors=True)


def test_studio_mode_rejects_unknown_studio():
    with pytest.raises(ValueError, match="[Uu]nknown studio"):
        img_tool.execute(query="cat", studio_id="pdf_999999")
    with pytest.raises(ValueError):
        img_tool.execute(query="cat", studio_id="not-a-studio")


def test_engine_strips_studio_id_outside_studio():
    from core.agent.loop.engine import _sanitize_tool_args
    args = {"query": "cat", "studio_id": "pdf_1"}
    assert _sanitize_tool_args("image_search", dict(args), False) == {"query": "cat"}
    assert _sanitize_tool_args("image_search", dict(args), True) == args
    assert _sanitize_tool_args("http_request", dict(args), False) == args


def test_collect_final_images_picks_workspace_card_files():
    from core.agent.loop.engine import _collect_final_images
    log = [
        {"tool": "image_search",
         "result": {"success": True,
                    "output": {"images": [
                        {"file": "images/cat_1.jpg", "source": "openverse"},
                        {"file": "../evil.jpg", "source": "x"},
                        {"src": "/api/studio/asset?studio_id=pdf_1&file=images/a.jpg"},
                    ]}}},
        {"tool": "image_search",
         "result": {"success": False, "output": {"images": [{"file": "images/no.jpg"}]}}},
    ]
    got = _collect_final_images(log)
    assert got == [{"folder": "images/cat_1.jpg"}]
