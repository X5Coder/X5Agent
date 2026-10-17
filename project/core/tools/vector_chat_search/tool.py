"""
vector_chat_search - semantic search over conversation history.
Uses embedding model if available, otherwise fallback lexical.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .events import format_event


def _validate_query(q: Any) -> str:
    if not isinstance(q, str):
        raise TypeError("query must be string")
    s = q.strip()
    if not s:
        raise ValueError("query must be non-empty")
    if len(s) > 2000:
        raise ValueError("query too long (max 2000)")
    return s


def _validate_k(k: Any) -> int:
    if k is None:
        return 5
    if not isinstance(k, int):
        raise TypeError("k must be integer")
    if k < 1 or k > 20:
        raise ValueError("k must be between 1 and 20")
    return k


def _load_history() -> list[dict]:
    # Conversation history: Data first (unified), legacy fallback.
    project_root = Path(__file__).resolve().parents[3]
    candidates = []
    try:
        import sys as _sys
        if str(project_root) not in _sys.path:
            _sys.path.insert(0, str(project_root))
        from core.paths import data_db_dir as _dd
        candidates.append(_dd() / "conversation.json")
    except Exception:
        pass
    candidates.append(project_root / "storage" / "database" / "conversation.json")
    for hist_path in candidates:
        if not hist_path.exists():
            continue
        try:
            data = json.loads(hist_path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception:
            continue
    return []


def execute(query: str, k: int | None = None) -> dict:
    q = _validate_query(query)
    limit = _validate_k(k)
    event = format_event()
    # Use persistent per-message vector store (auto-handles delete/edit)
    try:
        from core.agent.memory.chat_vector_store import search as vector_search, ensure_synced
        ensure_synced()
        results = vector_search(q, k=limit)
        if results:
            return {"event": event, "query": q, "k": limit, "results": results}
    except Exception:
        pass

    # Fallback: lexical over history if vector store empty or embedding not ready
    history = _load_history()
    if not history:
        return {"event": event, "query": q, "k": limit, "results": [], "note": "no history yet"}
    q_low = q.lower()
    scored = []
    for idx, m in enumerate(history):
        content = m.get("content", "") if isinstance(m, dict) else str(m)
        if not isinstance(content, str):
            continue
        c_low = content.lower()
        score = c_low.count(q_low)
        if q_low in c_low:
            score += 1
        scored.append((score, idx))
    scored.sort(reverse=True, key=lambda x: x[0])
    results = [{"score": s, "message": history[i]} for s, i in scored[:limit] if s > 0]
    return {"event": event, "query": q, "k": limit, "results": results}
