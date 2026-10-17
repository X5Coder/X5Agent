"""
embedding_memory - semantic text memory behind tool_search.
Stores one text as one local embedding chunk in X5Agent/Data/vector/chroma
and retrieves the most similar chunks for a query sentence via cosine similarity.
New schema: reason + text (store) + query (search-only) + top_k (count).
Persistent across restarts, unbounded (grows with stored texts).
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Any

from .events import format_event

ALLOWED = {"store", "search", "list", "get", "delete", "clear"}
MAX_TEXT_LEN = 2000
DEFAULT_TOP_K = 5
MAX_TOP_K = 10

_COLLECTION = "embedding_memory"
_PROJECT = Path(__file__).resolve().parents[3]

# Old JSON store (pre-Chroma). Entries are merged once on first use: unlike
# chat/document vectors these texts cannot be re-derived from anything else.
_LEGACY_CANDIDATES = (
    _PROJECT / "storage" / "vector" / "embedding_memory.json",
)


def _legacy_candidates() -> tuple[Path, ...]:
    out = list(_LEGACY_CANDIDATES)
    try:
        import sys as _sys
        if str(_PROJECT) not in _sys.path:
            _sys.path.insert(0, str(_PROJECT))
        from core.paths import data_vector_dir
        out.insert(0, data_vector_dir() / "embedding_memory.json")
    except Exception:
        pass
    return tuple(out)


def _vb():
    import sys as _sys
    if str(_PROJECT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT))
    from core.embedding import vector_db
    return vector_db


def _read_legacy_entries() -> list[dict]:
    for cand in _legacy_candidates():
        try:
            if not cand.exists():
                continue
            data = json.loads(cand.read_text(encoding="utf-8"))
            entries = data.get("entries") if isinstance(data, dict) else data
            if not isinstance(entries, list):
                continue
            clean = [e for e in entries
                     if isinstance(e, dict) and isinstance(e.get("text"), str)
                     and e["text"].strip() and isinstance(e.get("embedding"), list)
                     and e["embedding"]]
            if clean:
                return clean
        except Exception:
            continue
    return []


_IMPORT_CHECKED = False


def _ensure_imported() -> None:
    """One-time merge of the legacy JSON store into Chroma. Never raises."""
    global _IMPORT_CHECKED
    if _IMPORT_CHECKED:
        return
    _IMPORT_CHECKED = True
    try:
        vb = _vb()
        if vb.count(_COLLECTION) > 0:
            return
        entries = _read_legacy_entries()
        if not entries:
            return
        ids = [str(e.get("id") or uuid.uuid4().hex[:8]) for e in entries]
        embs = [[float(x) for x in e["embedding"]] for e in entries]
        texts = [e["text"] for e in entries]
        metas = [{"text": e["text"], "created_at": str(e.get("created_at") or "")}
                 for e in entries]
        vb.upsert(_COLLECTION, ids, embs, texts, metas)
    except Exception:
        pass


def count_stored() -> int:
    """Number of stored embedding chunks. Never raises (0 on any failure)."""
    try:
        _ensure_imported()
        return _vb().count(_COLLECTION)
    except Exception:
        return 0


def _clean_text(v: Any, name: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} must be non-empty string")
    t = " ".join(v.split())
    if len(t) > MAX_TEXT_LEN:
        raise ValueError(f"{name} too long (max {MAX_TEXT_LEN} chars)")
    return t


def _validate_top_k(v: Any) -> int:
    k = DEFAULT_TOP_K if v is None else v
    if isinstance(k, bool) or not isinstance(k, int) or k < 1 or k > MAX_TOP_K:
        raise ValueError(f"top_k must be integer 1-{MAX_TOP_K}")
    return k


def _do_store(chunk: str) -> dict:
    from core.embedding.service import embed_texts
    _ensure_imported()
    vectors = embed_texts([chunk], normalize=True)
    entry_id = uuid.uuid4().hex[:8]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _vb().upsert(_COLLECTION, [entry_id], [vectors[0]], [chunk],
                 [{"text": chunk, "created_at": now}])
    return {"event": format_event(text=chunk), "action": "store",
            "stored": {"id": entry_id, "text": chunk},
            "total": count_stored()}


def _do_search(q: str, k: int) -> dict:
    from core.embedding.service import embed_query
    _ensure_imported()
    vb = _vb()
    if vb.count(_COLLECTION) == 0:
        return {"event": format_event(query=q), "action": "search",
                "query": q, "results": [],
                "note": "memory is empty, store a text first"}
    rows = vb.query(_COLLECTION, embed_query(q), k)
    results = [{"id": r.get("id", ""),
                "text": (r.get("metadata") or {}).get("text", r.get("document", "")),
                "score": r.get("score", 0.0)} for r in rows]
    return {"event": format_event(query=q), "action": "search",
            "query": q, "results": results}


def execute(reason: str | None = None, text: str | None = None,
            query: str | None = None, top_k: int | None = None,
            **_legacy: Any) -> dict:
    # reason is informational only (shown in UI); never affects logic.
    # Legacy kwargs (action/texts/entry_id) are accepted silently so old
    # model calls fail clearly instead of crashing with TypeError.
    legacy_texts = _legacy.get("texts")
    legacy_action = _legacy.get("action")
    if text is None and isinstance(legacy_texts, list) and legacy_texts:
        first = legacy_texts[0] if isinstance(legacy_texts[0], str) else None
        text = first
    has_text = isinstance(text, str) and bool(text.strip())
    has_query = isinstance(query, str) and bool(query.strip())
    if has_text and has_query:
        raise ValueError("provide either text (to store) or query (to search), not both")
    if not has_text and not has_query:
        if isinstance(legacy_action, str) and legacy_action.strip().lower() in ALLOWED:
            raise ValueError("embedding_memory now uses text to store and query to search (top_k 1-10)")
        raise ValueError("text or query required: provide text to store, or query to search")
    if has_query:
        q = _clean_text(query, "query")
        k = _validate_top_k(top_k)
        return _do_search(q, k)
    chunk = _clean_text(text, "text")
    return _do_store(chunk)
