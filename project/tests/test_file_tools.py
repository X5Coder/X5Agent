import base64
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.agent.loop.dispatcher import execute_tool
from core.workfolder import get_path as folder_path
from core.tools.registry import list_discoverable_specs, is_core_plugin

TAG = "x5t_filetools"
created = []


def _ws():
    ws = folder_path()
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def cleanup():
    import shutil
    try:
        ws = _ws()
        for p in ws.glob(TAG + "*"):
            if p.is_file():
                p.unlink(missing_ok=True)
            else:
                shutil.rmtree(p, ignore_errors=True)
    except Exception:
        pass


try:
    cleanup()
    ws = _ws()

    # 1. specs registered + discoverable + core (always on)
    names = {s.get("name") for s in list_discoverable_specs()}
    assert "file_to_base64" in names and "file_metadata" in names, names
    assert is_core_plugin("file_to_base64") and is_core_plugin("file_metadata")
    print("specs registered OK")

    # 2. base64: binary blob roundtrip, context-safe (no full text in result)
    blob = (b"\x00\x01\x02hello-world" * 5000)
    src = ws / (TAG + "_blob.bin")
    src.write_bytes(blob)
    created.append(src.name)
    r = execute_tool("file_to_base64", {"path": src.name})
    assert r.get("success"), r
    out = r["output"]
    assert out["source_size"] == len(blob), out
    assert out["base64_size"] == len(base64.b64encode(blob)), out
    assert len(out.get("preview", "")) <= 64, out
    assert "message" in out and out["message"], out
    saved = ws / out["output_path"]
    assert saved.exists(), out
    created.append(out["output_path"])
    assert saved.read_text(encoding="utf-8") == base64.b64encode(blob).decode("ascii")
    print("file_to_base64 roundtrip OK")

    # 3. base64 rejects escape + missing
    bad = execute_tool("file_to_base64", {"path": "../outside.bin"})
    assert not bad.get("success"), bad
    missing = execute_tool("file_to_base64", {"path": TAG + "_nope.bin"})
    assert not missing.get("success"), missing
    print("file_to_base64 guards OK")

    # 4. metadata read: txt (base layer) + png/jpg (image layer)
    from PIL import Image
    png = ws / (TAG + "_img.png")
    Image.new("RGB", (32, 16), (10, 20, 30)).save(png)
    created.append(png.name)
    jpg = ws / (TAG + "_img.jpg")
    Image.new("RGB", (40, 20), (200, 10, 10)).save(jpg, quality=90)
    created.append(jpg.name)
    m = execute_tool("file_metadata", {"path": png.name})
    assert m.get("success"), m
    meta = m["output"]["metadata"]
    assert meta["width"] == 32 and meta["height"] == 16 and meta["format"] == "PNG", meta
    assert len(meta.get("sha256", "")) == 64, meta
    t = execute_tool("file_metadata", {"path": src.name})
    assert t.get("success") and t["output"]["metadata"]["size"] == len(blob), t
    print("file_metadata read OK")

    # 5. metadata set on JPEG -> .meta copy, original untouched, verified
    s = execute_tool("file_metadata", {"path": jpg.name, "action": "set",
                                       "tags": {"Artist": "X5Test", "Copyright": "2026"}})
    assert s.get("success"), s
    sout = s["output"]
    assert set(sout["applied"]) == {"Artist", "Copyright"}, sout
    assert sout["verified"].get("Artist") == "X5Test", sout
    created.append(sout["output_path"])
    orig = execute_tool("file_metadata", {"path": jpg.name})
    assert "Artist" not in (orig["output"]["metadata"].get("exif") or {}), orig
    print("file_metadata set JPEG OK")

    # 6. metadata set on PNG (text chunk) + reject txt
    p = execute_tool("file_metadata", {"path": png.name, "action": "set",
                                       "tags": {"Author": "X5Test"}})
    assert p.get("success") and p["output"]["verified"].get("Author") == "X5Test", p
    created.append(p["output"]["output_path"])
    rej = execute_tool("file_metadata", {"path": src.name, "action": "set",
                                         "tags": {"K": "V"}})
    assert not rej.get("success"), rej
    print("file_metadata set PNG/reject OK")

    print("ALL FILE TOOLS TESTS PASSED")
finally:
    cleanup()
