"""
Chat Vector Store - per-message embeddings for the conversation.

Each message has an id, role, content and embedding. Deleting/editing a
message auto-manages its embedding. Uses the bundled embedding model via
core/embedding/service.py (offline). Every user message + assistant reply is
embedded automatically on persist (core/agent/loop/engine.py).

Storage: X5Agent/Data/vector/chroma (collection "chat_messages") +
X5Agent/Data/database/conversation.json (source of truth). Embeds are never
capped: the store grows with the conversation.
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

_COLLECTION = "chat_messages"
_PROJECT = Path(__file__).resolve().parents[3]
_MAX_TEXT = 4000


def _vb():
    import sys as _sys
    if str(_PROJECT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT))
    from core.embedding import vector_db
    return vector_db


def _resolve_conversation_candidates() -> list[Path]:
    # Data first (unified), legacy fallback.
    legacy = Path(__file__).resolve().parents[3] / "storage" / "database" / "conversation.json"
    try:
        import sys as _sys2
        if str(_PROJECT) not in _sys2.path:
            _sys2.path.insert(0, str(_PROJECT))
        from core.paths import data_db_dir as _dd
        return [_dd() / "conversation.json", legacy]
    except Exception:
        return [legacy]


_CONVERSATION_CANDIDATES = _resolve_conversation_candidates()
CONVERSATION_PATH = _CONVERSATION_CANDIDATES[0]


def _load_conversation() -> list[dict]:
    for cand in _CONVERSATION_CANDIDATES:
        if not cand.exists():
            continue
        try:
            data = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def _save_conversation(data: list[dict]) -> None:
    CONVERSATION_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONVERSATION_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _embed_single(text: str) -> list[float]:
    from core.embedding.service import embed_texts
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be non-empty")
    if len(text) > _MAX_TEXT:
        text = text[:_MAX_TEXT]
    return embed_texts([text])[0]


def _entry(mid: str, role: str, content: str, created_at: str) -> dict:
    return {"id": mid, "role": role, "content": content[:_MAX_TEXT], "created_at": created_at}


def add_message(message_id: str, content: str, role: str = "user") -> dict:
    """Add a message embedding. Called when a new message is persisted."""
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("message_id required")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("content required")
    mid = message_id.strip()
    vb = _vb()
    existing = vb.get_ids(_COLLECTION, [mid])
    if existing["ids"]:
        md = existing["metadatas"][0] or {}
        return _entry(mid, md.get("role", role), md.get("content", content), md.get("created_at", ""))
    emb = _embed_single(content)
    created_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    body = content[:_MAX_TEXT]
    vb.upsert(_COLLECTION, [mid], [emb], [body],
              [{"role": str(role), "content": body, "created_at": created_at}])
    return _entry(mid, role, content, created_at)


def delete_message(message_id: str) -> bool:
    """Delete a message and its embedding. Returns True if it existed."""
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("message_id required")
    mid = message_id.strip()
    conv = _load_conversation()
    new_conv = [m for m in conv if str(m.get("id") or m.get("message_id") or "") != mid]
    found_in_conv = len(new_conv) < len(conv)
    if found_in_conv:
        _save_conversation(new_conv)
    vb = _vb()
    found_in_vec = vb.count(_COLLECTION) > 0 and bool(vb.get_ids(_COLLECTION, [mid])["ids"])
    if found_in_vec:
        vb.delete(_COLLECTION, ids=[mid])
    return found_in_conv or found_in_vec


def prune_below(anchor_id: str) -> int:
    """Delete every message below the anchor in ONE atomic read-modify-write.

    Edit-resend / regenerate: the anchor bubble stays, all newer messages go
    (conversation + their embeddings). No partial state, no N round-trips.
    Returns the removed count. Raises FileNotFoundError when the anchor is
    gone, ValueError when the id is bad.
    """
    if not isinstance(anchor_id, str) or not anchor_id.strip():
        raise ValueError("anchor id required")
    mid = anchor_id.strip()
    conv = _load_conversation()
    idx = next((i for i, m in enumerate(conv)
                if str(m.get("id") or m.get("message_id") or "") == mid), None)
    if idx is None:
        raise FileNotFoundError("anchor message not found")
    doomed = conv[idx + 1:]
    if not doomed:
        return 0
    doomed_ids = {str(m.get("id") or m.get("message_id") or "").strip() for m in doomed}
    doomed_ids.discard("")
    _save_conversation(conv[:idx + 1])
    if doomed_ids:
        try:
            _vb().delete(_COLLECTION, ids=list(doomed_ids))
        except Exception:
            pass
    return len(doomed)


def update_message(message_id: str, new_content: str) -> dict:
    """Edit a message: update the conversation + re-create its embedding."""
    if not isinstance(message_id, str) or not message_id.strip():
        raise ValueError("message_id required")
    if not isinstance(new_content, str) or not new_content.strip():
        raise ValueError("new_content required")
    mid = message_id.strip()
    conv = _load_conversation()
    role = "user"
    updated = False
    for m in conv:
        if str(m.get("id") or m.get("message_id") or "") == mid:
            role = str(m.get("role") or "user")
            m["content"] = new_content[:_MAX_TEXT]
            m["edited_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            updated = True
            break
    if updated:
        _save_conversation(conv)
    vb = _vb()
    if vb.count(_COLLECTION) > 0:
        vb.delete(_COLLECTION, ids=[mid])
    return add_message(mid, new_content, role=role)


def ensure_synced() -> dict:
    """Sync conversation.json with the vector store: add missing, delete
    orphaned, update changed. Called on startup and before search."""
    conv = _load_conversation()
    changed = False
    for m in conv:
        if "id" not in m and "message_id" not in m:
            m["id"] = str(uuid.uuid4())[:8]
            changed = True
        if "message_id" in m and "id" not in m:
            m["id"] = m.pop("message_id")
        elif "message_id" in m:
            m.pop("message_id", None)
    if changed:
        _save_conversation(conv)
        conv = _load_conversation()

    vb = _vb()
    got = vb.all_records(_COLLECTION)
    stored = {rid: (meta or {}) for rid, meta in zip(got["ids"], got["metadatas"])}

    for m in conv:
        mid = str(m.get("id", "")).strip()
        if not mid:
            continue
        content = str(m.get("content", "")).strip()
        if not content:
            continue
        meta = stored.get(mid)
        if meta is None:
            try:
                add_message(mid, content, role=m.get("role", "user"))
            except Exception:
                continue
        elif str(meta.get("content", "")) != content[:_MAX_TEXT]:
            try:
                if vb.count(_COLLECTION) > 0:
                    vb.delete(_COLLECTION, ids=[mid])
                add_message(mid, content, role=m.get("role", "user"))
            except Exception:
                continue

    conv_ids = {str(m.get("id", "")).strip() for m in conv if m.get("id")}
    orphaned = [rid for rid in stored if rid not in conv_ids]
    if orphaned:
        try:
            vb.delete(_COLLECTION, ids=orphaned)
        except Exception:
            pass
    return {"conversation_count": len(conv), "vector_count": vb.count(_COLLECTION),
            "orphaned_removed": len(orphaned)}


def search(query: str, k: int = 5) -> list[dict]:
    """Semantic search over chat messages using stored embeddings."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    if not isinstance(k, int) or k < 1 or k > 20:
        k = 5
    ensure_synced()
    vb = _vb()
    if vb.count(_COLLECTION) == 0:
        return []
    from core.embedding.service import embed_query
    rows = vb.query(_COLLECTION, embed_query(query), k)
    conv = _load_conversation()
    by_id = {str(m.get("id")): m for m in conv}
    results = []
    for r in rows:
        mid = r.get("id")
        md = r.get("metadata") or {}
        msg = by_id.get(mid) or {"id": mid, "content": md.get("content", ""),
                                 "role": md.get("role", "user")}
        results.append({"score": r.get("score", 0.0), "message": msg, "id": mid})
    return results


def clear_all() -> int:
    """Drop every chat embedding (used by Delete-chat-history)."""
    vb = _vb()
    n = vb.count(_COLLECTION)
    vb.drop(_COLLECTION)
    return n
