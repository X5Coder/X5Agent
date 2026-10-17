"""Workspace file ids + upload mirror + @ injection + vision/studio resolution.

Deterministic: every file id is the Workspace-relative path, shown in the
context map, carried by @ mentions, resolvable by vision_analyze and by the
studio asset store. Run: pytest project/tests/test_workspace_media_logic.py
"""
import base64
import shutil
import sys
import uuid as _uuid
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import workspace_root  # noqa: E402
from web.server.server import DashboardHandler  # noqa: E402

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def _unique_tag() -> str:
    return f"UT_WM_{_uuid.uuid4().hex[:6]}"


def _handler():
    return object.__new__(DashboardHandler)


def test_workspace_api_entries_carry_stable_ids():
    h = _handler()
    tag = _unique_tag()
    target = workspace_root() / tag
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / "pic.png").write_bytes(_PNG)
        out = DashboardHandler._workspace_entries(h, target)
        by_name = {e["name"]: e for e in out["entries"]}
        assert by_name["pic.png"]["type"] == "file"
        assert by_name["pic.png"]["id"] == f"{tag}/pic.png"
    finally:
        shutil.rmtree(str(target), ignore_errors=True)


def test_upload_mirror_keeps_original_name():
    h = _handler()
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    name = f"{_unique_tag()}_logo.png"
    saved, mirrors = [], []
    try:
        kind, saved_path, ref, iid = DashboardHandler._save_chat_attachment(
            h, {"type": "image", "name": name, "data": data_url})
        assert kind == "image"
        saved.append(Path(str(saved_path)))
        assert f'file_id="{name}"' in ref, ref
        assert iid == name, ref
        mirrors.append(workspace_root() / name)
        assert (workspace_root() / name).is_file()
        # Same name again -> same content, suffixed stable id (never overwrite).
        kind2, saved2, ref2, iid2 = DashboardHandler._save_chat_attachment(
            h, {"type": "image", "name": name, "data": data_url})
        saved.append(Path(str(saved2)))
        assert f'file_id="{name}"' not in ref2, ref2
        import re as _re2
        fid2 = _re2.search(r'file_id="([^"]+)"', ref2).group(1)
        assert fid2.startswith(name.split(".")[0] + "_") and fid2.endswith(".png"), ref2
        assert iid2 == fid2, ref2
        mirrors.append(workspace_root() / fid2)
        assert (workspace_root() / fid2).is_file()
    finally:
        for p in saved:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        for p in mirrors:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass


def test_at_mention_injects_real_file_id():
    h = _handler()
    tag = _unique_tag()
    target = workspace_root() / tag
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / "notes.txt").write_text("hello", encoding="utf-8")
        rel = f"{tag}/notes.txt"
        loop_input, _ = DashboardHandler._build_loop_input(
            h, {"message": f"read @{rel.split('/')[-1]} please",
                "workspace_refs": [{"name": "notes.txt", "path": rel}]})
        # @ picks send the file id + full path, in place, never metadata.
        assert ("@" + rel + ' "') in loop_input, loop_input
        assert "[Workspace file:" not in loop_input, loop_input
    finally:
        shutil.rmtree(str(target), ignore_errors=True)


def test_upload_sent_as_workspace_id_plus_message_metadata():
    h = _handler()
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    name = f"{_unique_tag()}_shot.png"
    saved, fid = [], None
    try:
        loop_input, saved_paths = DashboardHandler._build_loop_input(
            h, {"message": "what is in this?",
                "attachments": [{"type": "image", "name": name, "data": data_url}]})
        saved += [Path(str(p)) for p in saved_paths]
        import re as _re
        m = _re.search(r'file_id="([^"]+)"', loop_input)
        assert m, loop_input
        fid = m.group(1)
        assert (workspace_root() / fid).is_file()
        # Message metadata carries size + type + the Workspace file id.
        assert "file_id=" in loop_input
        assert "what is in this?" in loop_input
        # Priority block points at the same Workspace id.
        assert f'file_id="{fid}"' in loop_input
    finally:
        for p in saved:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        if fid:
            try:
                (workspace_root() / fid).unlink(missing_ok=True)
            except Exception:
                pass


def test_vision_resolves_workspace_ids():
    from core.ai_models.media import resolve_image

    tag = _unique_tag()
    target = workspace_root() / tag
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / "img.png").write_bytes(_PNG)
        rel = f"{tag}/img.png"
        assert resolve_image(image_id=rel).is_file()
        assert resolve_image(image_path=rel).is_file()
    finally:
        shutil.rmtree(str(target), ignore_errors=True)
    try:
        resolve_image(image_id="../outside.png")
        raise AssertionError("traversal must fail")
    except ValueError:
        pass
    try:
        resolve_image(image_id=f"UT_missing_{_uuid.uuid4().hex[:6]}.png")
        raise AssertionError("missing must fail")
    except FileNotFoundError:
        pass


def test_studio_assets_dirs_exist_per_studio():
    from core.studio import store as _store

    created = _store.create_studio(title="UT Assets", pages=1)
    try:
        base = _store.studios_root() / created["studio_id"]
        assert (base / "assets" / "fonts").is_dir()
        assert (base / "assets" / "images").is_dir()
        assert (base / "pages" / "Page01.jsx").is_file()
    finally:
        shutil.rmtree(str(_store.studios_root() / created["studio_id"]), ignore_errors=True)


def test_workspace_map_uses_full_metadata_shape():
    from core.agent.context.builder import _workspace_block

    tag = _unique_tag()
    target = workspace_root() / f"{tag}.txt"
    try:
        target.write_text("x", encoding="utf-8")
        block = _workspace_block()
        assert "id + path + type + size" in block
        assert ("id: " + '"' + tag + '.txt"') in block
        assert "path: " in block and "type: txt" in block and "size: 1 bytes" in block
        # Path-link instructions travel with the map (main + pdf sessions).
        assert "as-is" in block and "one folder" in block
    finally:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass


def test_at_mention_injects_id_plus_path_in_place():
    h = _handler()
    tag = _unique_tag()
    target = workspace_root() / tag
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / "pic.png").write_bytes(_PNG)
        rel = f"{tag}/pic.png"
        loop_input, _ = DashboardHandler._build_loop_input(
            h, {"message": "use @pic here please",
                "workspace_refs": [{"name": "pic", "path": rel}]})
        # The token becomes @<file-id> + full path exactly where it stood.
        assert "use @" + rel + ' "' in loop_input, loop_input
        assert "[Workspace file:" not in loop_input, loop_input
        import re as _re
        m = _re.search(r"@" + _re.escape(rel) + r' "([^"]+)"', loop_input)
        assert m and Path(m.group(1)).is_file(), loop_input
        # An unused pick appends as an @<file-id> + path line, still no metadata.
        loop_input2, _ = DashboardHandler._build_loop_input(
            h, {"message": "hello",
                "workspace_refs": [{"name": "pic", "path": rel}]})
        assert ("@" + rel + ' "') in loop_input2, loop_input2
        assert "[Workspace file:" not in loop_input2, loop_input2
    finally:
        shutil.rmtree(str(target), ignore_errors=True)


def test_upload_lines_use_full_metadata_shape():
    h = _handler()
    data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
    name = f"{_unique_tag()}_full.png"
    saved, fid = [], None
    try:
        loop_input, saved_paths = DashboardHandler._build_loop_input(
            h, {"message": "here",
                "attachments": [{"type": "image", "name": name, "data": data_url}]})
        saved += [Path(str(p)) for p in saved_paths]
        import re as _re
        m = _re.search(r"file_id=", loop_input)
        assert m, loop_input
        m = _re.search(r'file_id="([^"]+)"', loop_input)
        fid = m.group(1)
        assert "path: " in loop_input and "type: png" in loop_input
        assert ("size: %d bytes" % len(_PNG)) in loop_input
    finally:
        for p in saved:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        if fid:
            try:
                (workspace_root() / fid).unlink(missing_ok=True)
            except Exception:
                pass


def test_folder_listing_entries_carry_path():
    from core.tools.workspace.tool import execute

    tag = _unique_tag()
    target = workspace_root() / tag
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / "doc.txt").write_text("y", encoding="utf-8")
        out = execute("list", path=tag)
        assert out.get("success")
        by_name = {e["name"]: e for e in out["entries"]}
        assert by_name["doc.txt"]["id"] == f"{tag}/doc.txt"
        assert by_name["doc.txt"]["path"].replace("\\\\", "/").endswith(f"{tag}/doc.txt")
    finally:
        shutil.rmtree(str(target), ignore_errors=True)


def test_studio_id_validation_rejects_escape():
    from core.studio import store as _store

    assert _store.validate_studio_id("pdf_1") == "pdf_1"
    assert _store.validate_page_no(3) == 3
    for bad in ("../x", "/x", "", "pdf_", "deck/1"):
        try:
            _store.validate_studio_id(bad)
            raise AssertionError("must reject: %r" % (bad,))
        except (ValueError, TypeError):
            pass


def test_at_arabic_name_injects_verbatim_id_resolving_to_real_file():
    import re as _re
    from core.paths import resolve_inside_workspace

    h = _handler()
    tag = _unique_tag()
    target = workspace_root() / tag
    target.mkdir(parents=True, exist_ok=True)
    try:
        (target / "صورة تجربة.png").write_bytes(_PNG)
        rel = f"{tag}/صورة تجربة.png"
        loop_input, _ = DashboardHandler._build_loop_input(
            h, {"message": "حلل @صورة تجربة لو سمحت",
                "workspace_refs": [{"name": "صورة تجربة", "path": rel}]})
        # Verbatim id + full path, no mojibake, no metadata block.
        assert ("@" + rel + ' "') in loop_input, loop_input
        assert "[Workspace file:" not in loop_input, loop_input
        m = _re.search(r"@" + _re.escape(rel) + r' "([^"]+)"', loop_input)
        assert m and Path(m.group(1)).is_file(), loop_input
        # The id resolves through the Workspace jail to the real file.
        resolved = resolve_inside_workspace(rel)
        assert resolved.is_file() and resolved.read_bytes() == _PNG
        # Entering the folder shows the same file with id + full path.
        from core.tools.workspace.tool import execute
        out = execute("list", path=tag)
        assert out.get("success")
        entry = {e["name"]: e for e in out["entries"]}["صورة تجربة.png"]
        assert entry["id"] == rel
        assert "\\" not in entry["path"] and Path(entry["path"]).is_file()
    finally:
        shutil.rmtree(str(target), ignore_errors=True)


def test_emitted_paths_are_directly_usable_in_html():
    import re as _re
    from core.paths import resolve_inside_workspace
    from core.tools.workspace.tool import execute

    tag = _unique_tag()
    sub = workspace_root() / tag
    sub.mkdir(parents=True, exist_ok=True)
    try:
        (sub / "art.png").write_bytes(_PNG)
        rel = f"{tag}/art.png"
        # Outer map shows the folder; entering it shows the file id+path.
        from core.agent.context.builder import _workspace_block
        assert (f"folder: {tag}/") in _workspace_block()
        entry = {e["name"]: e for e in execute("list", path=tag)["entries"]}["art.png"]
        assert entry["id"] == rel
        full = entry["path"]
        # Full path: absolute, no backslashes, points at the real bytes.
        assert "\\" not in full and (":/" in full or full.startswith("/")), full
        assert Path(full).is_file() and Path(full).read_bytes() == _PNG
        # An HTML snippet using the emitted path / id resolves to real files.
        html = "<html><body><img src=\"%s\"><img src=\"%s\"></body></html>" % (full, rel)
        for src in _re.findall(r"<img[^>]*src=\"([^\"]+)\"", html):
            cand = Path(src)
            if not cand.is_absolute():
                cand = resolve_inside_workspace(src)
            assert cand.is_file(), src
    finally:
        shutil.rmtree(str(sub), ignore_errors=True)


def test_studio_system_prompt_is_english_and_mandates_approve():
    from pathlib import Path as _Path
    text = (_Path(__file__).resolve().parents[1] / "prompts" / "pdf_studio_system.md").read_text(encoding="utf-8")
    assert "studio_page" in text
    assert "MUST" in text
    assert "استخدم" not in text


def test_gateway_mirror_keeps_original_name():
    from gateway.dispatch import _mirror_workspace

    name = f"{_unique_tag()}_gw.png"
    try:
        first = _mirror_workspace(_PNG, name)
        assert first == name
        assert (workspace_root() / name).is_file()
        second = _mirror_workspace(_PNG, name)
        assert second != name and second.startswith(name.split(".")[0] + "_")
        assert (workspace_root() / second).is_file()
    finally:
        for fid in (name,):
            try:
                (workspace_root() / fid).unlink(missing_ok=True)
            except Exception:
                pass
        try:
            for p in workspace_root().glob(name.split(".")[0] + "_*.png"):
                p.unlink(missing_ok=True)
        except Exception:
            pass


def test_gateway_image_hint_full_shape():
    from gateway.dispatch import _convert_part

    name = f"{_unique_tag()}_gwlook.png"
    try:
        text, hint = _convert_part({"kind": "image", "text": "شوف دي",
                                    "blob": _PNG, "name": name, "mime": "image/png"})
        assert text == "شوف دي"
        assert ('file_id="' + name + '"') in hint
        assert "path: " in hint and "type: png" in hint
        assert ("size: %d bytes" % len(_PNG)) in hint
        assert "analyze it first" in hint
        assert (workspace_root() / name).is_file()
    finally:
        try:
            (workspace_root() / name).unlink(missing_ok=True)
        except Exception:
            pass


def test_gateway_document_hint_full_shape():
    from gateway.dispatch import _convert_part

    blob = b"%PDF-1.4 fake-body"
    name = f"{_unique_tag()}_sheet.pdf"
    try:
        text, hint = _convert_part({"kind": "file", "text": "",
                                    "blob": blob, "name": name, "mime": "application/pdf"})
        assert ('file_id="' + name + '"') in hint
        assert "path: " in hint and "type: pdf" in hint
        assert ("size: %d bytes" % len(blob)) in hint
        assert "document_search" in hint
        assert (workspace_root() / name).is_file()
    finally:
        try:
            (workspace_root() / name).unlink(missing_ok=True)
        except Exception:
            pass


def test_gateway_video_hint_points_at_video_flow():
    from gateway.dispatch import _convert_part

    blob = b"\x00\x00\x00fake-mp4"
    name = f"{_unique_tag()}_clip.mp4"
    try:
        text, hint = _convert_part({"kind": "file", "text": "",
                                    "blob": blob, "name": name, "mime": "video/mp4"})
        assert hint.startswith("- video:") and ('file_id="' + name + '"') in hint
        assert "path: " in hint and "type: mp4" in hint
        assert "process it first" in hint
        assert (workspace_root() / name).is_file()
    finally:
        try:
            (workspace_root() / name).unlink(missing_ok=True)
        except Exception:
            pass


def test_documents_block_uses_full_metadata_shape():
    from core.agent.context.builder import _documents_block

    tag = _unique_tag()
    target = workspace_root() / f"{tag}_note.txt"
    try:
        target.write_text("doc body", encoding="utf-8")
        block = _documents_block()
        assert ("id: " + '"' + target.name + '"') in block, block
        assert "path: " in block and "type: txt" in block and "size: " in block
    finally:
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass


def test_memories_block_sends_user_then_facts(monkeypatch):
    import core.agent.context.builder as b
    from core.memory import service

    monkeypatch.setattr(b, "_memory_text",
                        lambda kind: {"user": "- name: TESTUSER",
                                      "facts": "- likes: tea"}.get(kind, ""))
    monkeypatch.setattr(b, "_embedding_memory_block", lambda: "")
    block = b._memories_block()
    assert "TESTUSER" in block and "likes: tea" in block, block
    # User profile first, facts second.
    assert block.index("User memory") < block.index("Facts memory"), block
    assert service.USER in ("user", "agent", "facts", "notes")


def test_all_emitted_paths_are_full_html_usable_links():
    import re as _re
    from core.agent.context.builder import _workspace_block

    h = _handler()
    tag = _unique_tag()
    sub = workspace_root() / tag
    sub.mkdir(parents=True, exist_ok=True)
    saved, fid = [], None
    try:
        (sub / "art.png").write_bytes(_PNG)
        rel = f"{tag}/art.png"
        data_url = "data:image/png;base64," + base64.b64encode(_PNG).decode("ascii")
        up_name = f"{tag}_up.png"
        loop_input, saved_paths = DashboardHandler._build_loop_input(
            h, {"message": "use @art now",
                "attachments": [{"type": "image", "name": up_name, "data": data_url}],
                "workspace_refs": [{"name": "art", "path": rel}]})
        saved += [Path(str(p)) for p in saved_paths]
        m = _re.search(r'file_id="([^"]+)"', loop_input)
        fid = m.group(1) if m else None
        corps = _workspace_block() + "\n" + loop_input
        paths = _re.findall(r'path: "([^"]+)"', corps)
        assert len(paths) >= 3, corps
        for p in paths:
            assert "\\" not in p, p
            assert ":/" in p or p.startswith("/"), p
    finally:
        for p in saved:
            try:
                p.unlink(missing_ok=True)
            except Exception:
                pass
        if fid:
            try:
                (workspace_root() / fid).unlink(missing_ok=True)
            except Exception:
                pass
        shutil.rmtree(str(sub), ignore_errors=True)
