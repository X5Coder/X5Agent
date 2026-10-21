import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.documents.store import (
    SUPPORTED_EXTENSIONS,
    VISION_PAGE_PROMPT,
    chunk_markdown,
    convert_to_markdown,
    delete_file_embeddings,
    ensure_indexed,
    is_supported,
    list_documents,
    normalize_file_id,
    purge_missing,
    search,
)
from core.documents import store as _store
from core.workfolder import get_path as folder_path
from core.tools.registry import list_core_specs
from core.agent.loop.dispatcher import execute_tool

TAG = "doc_test_tmp"
TAG_DIR = (folder_path() / TAG)

# Deterministic fake embeddings (no model download in tests).
import math as _math


def _fake_vec(text, dim=8):
    import hashlib as _hl
    vals = []
    for j in range(dim):
        h = _hl.sha256(f"{text}#{j}".encode()).hexdigest()
        vals.append((int(h[:4], 16) % 100) / 100.0 + 0.01)
    n = _math.sqrt(sum(v * v for v in vals))
    return [v / n for v in vals]


def _patch_embeddings():
    import core.embedding.service as _emb
    _emb.embed_texts = lambda texts, normalize=True: [_fake_vec(t) for t in texts]
    _emb.embed_query = lambda q: _fake_vec(q)


def cleanup():
    import shutil
    try:
        if TAG_DIR.exists():
            shutil.rmtree(TAG_DIR, ignore_errors=True)
        for fid in [f"{TAG}/sample.txt", f"{TAG}/notes.md"]:
            try:
                delete_file_embeddings(fid)
            except Exception:
                pass
    except Exception:
        pass


try:
    cleanup()
    TAG_DIR.mkdir(parents=True, exist_ok=True)
    _patch_embeddings()

    # 1. chunking: 650 words -> 300/300/50, last word always complete
    words = [f"w{i}" for i in range(650)]
    chunks = chunk_markdown(" ".join(words))
    assert len(chunks) == 3, chunks
    assert len(chunks[0].split()) == 300 and len(chunks[1].split()) == 300
    assert len(chunks[2].split()) == 50, len(chunks[2].split())
    assert chunks[0].split()[-1] == "w299" and chunks[2].split()[-1] == "w649"
    assert chunk_markdown("   ") == []
    print("chunk 300 whole-word OK")

    # 2. supported types incl. pdf/word/excel/ppt
    for ext in (".pdf", ".docx", ".xlsx", ".pptx", ".csv", ".txt", ".md"):
        assert is_supported(f"a{ext}"), ext
    assert ".pdf" in SUPPORTED_EXTENSIONS
    for bad in ("a.exe", "a.png", "a.mp4", "", "noext"):
        assert not is_supported(bad), bad
    print("supported-types OK")

    # 3. file_id validation: unsupported + traversal rejected
    for bad_id in ("evil.exe", "../x.pdf", "/abs.pdf", "", "a" * 501):
        try:
            normalize_file_id(bad_id)
            raise AssertionError(f"accepted: {bad_id}")
        except (ValueError, TypeError):
            pass
    assert normalize_file_id(f"{TAG}/a.pdf") == f"{TAG}/a.pdf"
    print("file_id validation OK")

    # 4. markitdown converts txt + md for real
    (TAG_DIR / "sample.txt").write_text(
        "# Budget Report\n\nThe project budget is 50000 and timeline is Q1.\n", encoding="utf-8")
    md = convert_to_markdown(TAG_DIR / "sample.txt")
    assert "50000" in md, md
    (TAG_DIR / "notes.md").write_text("# Notes\n\napple orange banana\n", encoding="utf-8")
    assert "apple" in convert_to_markdown(TAG_DIR / "notes.md")
    print("markitdown convert OK")

    # 5. index + search (top_k min 1 enforced)
    meta = ensure_indexed(f"{TAG}/sample.txt")
    assert meta["chunk_count"] >= 1, meta
    res = search(f"{TAG}/sample.txt", "project budget timeline", top_k=1)
    assert len(res["results"]) == 1 and res["results"][0]["text"], res
    for bad_k in (0, 21, -1):
        try:
            search(f"{TAG}/sample.txt", "q", top_k=bad_k)
            raise AssertionError(f"top_k accepted: {bad_k}")
        except ValueError:
            pass
    try:
        search(f"{TAG}/evil.exe", "q")
        raise AssertionError("unsupported file_id searched")
    except ValueError:
        pass
    print("index + search OK")

    # 6. deleting a file purges its embeddings (the same call the folder API makes)
    (TAG_DIR / "sample.txt").unlink()
    assert delete_file_embeddings(f"{TAG}/sample.txt") is True
    assert not (TAG_DIR / "sample.txt").exists()
    try:
        search(f"{TAG}/sample.txt", "budget")
        raise AssertionError("search worked after delete")
    except FileNotFoundError:
        pass
    print("delete purges embeddings OK")

    # 7. direct unlink is purged too (terminal/manual deletes)
    (TAG_DIR / "ghost.txt").write_text("ghost content here", encoding="utf-8")
    ensure_indexed(f"{TAG}/ghost.txt")
    (TAG_DIR / "ghost.txt").unlink()
    assert purge_missing() >= 1
    print("purge_missing OK")

    # 8. thin PDF page goes to vision (patched, concurrent path)
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(200, 200)
    blank_pdf = TAG_DIR / "blank.pdf"
    with open(str(blank_pdf), "wb") as f:
        w.write(f)
    from PIL import Image as _Img
    tiny = TAG_DIR / "tiny.png"
    _Img.new("RGB", (8, 8), "white").save(str(tiny))
    _store._render_pdf_page = lambda path, index: tiny
    _store._vision_transcribe = lambda img: "hello vision page"
    out_md = convert_to_markdown(blank_pdf)
    assert "hello vision page" in out_md, out_md[:200]
    assert "vision" in VISION_PAGE_PROMPT.lower() or "Extract everything" in VISION_PAGE_PROMPT
    print("vision thin-page fallback OK")

    # 9. wiring: registry (discoverable behind tool_search) + dispatcher + metadata
    assert not any(s.get("name") == "document_search" for s in list_core_specs())
    from core.tools.registry import list_discoverable_specs as _list_disc
    assert any(s.get("name") == "document_search" for s in _list_disc())
    d = execute_tool("document_search", {
        "file_id": f"{TAG}/notes.md", "query": "apple fruit", "top_k": 1})
    assert d.get("success"), d
    assert d["output"]["results"] and "apple" in d["output"]["results"][0]["text"]
    d2 = execute_tool("document_search", {"file_id": f"{TAG}/x.exe", "query": "q"})
    assert not d2.get("success"), d2
    from core.agent.context.builder import build_context, _documents_block
    block = _documents_block()
    assert "document_search" in block and f"{TAG}/notes.md" in block, block
    _, user_ctx, _ = build_context("hi")
    assert "document_search" in user_ctx and "[Documents metadata" in user_ctx
    print("wiring OK")

    print("ALL DOCUMENT TESTS PASSED")
finally:
    cleanup()
