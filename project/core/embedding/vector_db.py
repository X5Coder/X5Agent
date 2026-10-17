"""ChromaDB vector store - THE single owner of every embedding on disk.

Every vector in the project (tool discovery, trigger discovery, chat memory,
document chunks, embedding_memory) is stored in ChromaDB instead of JSON
files. Same location rule as before: everything lives under
X5Agent/Data/vector (now the ``chroma/`` subfolder), so a Data zip backup
still contains it all.

Design rules:
  * Embeddings are ALWAYS supplied by the caller (the bundled embedding
    model via core.embedding.service) - no Chroma embedding function is ever
    installed, so nothing downloads and the space is exactly the project's
    model. Queries use the same vectors, hence cosine == dot product.
  * Collections use cosine space. The metric is fixed at creation.
  * ``X5_VECTOR_DB_DIR`` overrides the folder (tests use it for isolation);
    the client is cached per resolved folder, so switching folders is safe.
"""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

# Batch size for writes (Chroma rejects very large single adds).
_BATCH = 1000
# Collection names (Chroma requires 3-512 name chars).
TOOLS = "tools"
TRIGGERS = "triggers"
CHAT = "chat_messages"
DOCUMENT_CHUNKS = "document_chunks"
DOCUMENT_FILES = "document_files"
EMBEDDING_MEMORY = "embedding_memory"

_lock = threading.RLock()
_clients: dict[str, Any] = {}
_collections: dict[tuple[str, str], Any] = {}


def store_dir() -> Path:
    """Resolved Chroma persist folder (env override wins, for tests)."""
    override = os.environ.get("X5_VECTOR_DB_DIR")
    if override and override.strip():
        return Path(override.strip()).expanduser()
    import sys as _sys
    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    try:
        from core.paths import data_vector_dir
        return data_vector_dir() / "chroma"
    except Exception:
        return proj / "storage" / "vector" / "chroma"


def _client():
    """Process-cached PersistentClient for the resolved folder (thread-safe)."""
    key = str(store_dir())
    with _lock:
        client = _clients.get(key)
        if client is not None:
            return client
        import chromadb
        from chromadb.config import Settings

        path = store_dir()
        path.mkdir(parents=True, exist_ok=True)
        client = chromadb.PersistentClient(
            path=str(path), settings=Settings(anonymized_telemetry=False))
        _clients[key] = client
        return client


def collection(name: str):
    """Get-or-create a cosine collection (cached per folder + name)."""
    key = (str(store_dir()), name)
    with _lock:
        col = _collections.get(key)
        if col is not None:
            return col
        col = _client().get_or_create_collection(
            name=name, metadata={"hnsw:space": "cosine"})
        _collections[key] = col
        return col


def _as_list(value) -> list:
    """Normalize a Chroma result field to a plain list.

    Chroma returns its result fields as numpy arrays in some versions and
    plain lists in others; both iterate the same way, and an ambiguous
    truth-value on an array is what we are avoiding here.
    """
    if value is None:
        return []
    try:
        return list(value)
    except TypeError:
        return []


def _rows(value) -> list[list]:
    """Normalize an embeddings field to a list of plain, JSON-safe float lists."""
    out: list[list] = []
    for row in _as_list(value):
        if not isinstance(row, (list, tuple)):
            try:
                row = list(row)
            except TypeError:
                out.append(row)
                continue
        conv: list = []
        for x in row:
            try:
                conv.append(float(x))
            except (TypeError, ValueError):
                conv.append(x)
        out.append(conv)
    return out


def upsert(name: str, ids: list, embeddings: list, documents: list | None = None,
           metadatas: list | None = None) -> int:
    """Insert/overwrite records in batches. Idempotent (safe to retry)."""
    if not ids:
        return 0
    col = collection(name)
    total = len(ids)
    for start in range(0, total, _BATCH):
        end = start + _BATCH
        col.upsert(
            ids=list(ids[start:end]),
            embeddings=[list(e) for e in embeddings[start:end]],
            documents=list(documents[start:end]) if documents is not None else None,
            metadatas=list(metadatas[start:end]) if metadatas is not None else None,
        )
    return total


def all_records(name: str) -> dict:
    """Every record: {ids, embeddings, documents, metadatas} (empty when none)."""
    col = collection(name)
    if col.count() == 0:
        return {"ids": [], "embeddings": [], "documents": [], "metadatas": []}
    got = col.get(include=["embeddings", "documents", "metadatas"])
    return {
        "ids": [str(i) for i in _as_list(got.get("ids"))],
        "embeddings": _rows(got.get("embeddings")),
        "documents": _as_list(got.get("documents")),
        "metadatas": [m if isinstance(m, dict) else {} for m in _as_list(got.get("metadatas"))],
    }


def get_ids(name: str, ids: list) -> dict:
    """Fetch specific ids: {metadatas, documents, embeddings} (missing dropped)."""
    col = collection(name)
    if not ids:
        return {"ids": [], "metadatas": [], "documents": [], "embeddings": []}
    got = col.get(ids=list(ids), include=["embeddings", "documents", "metadatas"])
    return {
        "ids": [str(i) for i in _as_list(got.get("ids"))],
        "metadatas": [m if isinstance(m, dict) else {} for m in _as_list(got.get("metadatas"))],
        "documents": _as_list(got.get("documents")),
        "embeddings": _rows(got.get("embeddings")),
    }


def query(name: str, embedding: list, n_results: int,
          where: dict | None = None) -> list[dict]:
    """Top-n by cosine similarity -> [{id, score, document, metadata}].

    score = 1 - cosine distance, so it equals the normalized dot product
    (the exact metric the old JSON stores used).
    """
    col = collection(name)
    if col.count() == 0:
        return []
    res = col.query(
        query_embeddings=[list(embedding)],
        n_results=max(1, int(n_results)),
        where=where,
        include=["distances", "documents", "metadatas"],
    )
    ids = _as_list(res.get("ids"))
    dists = _as_list(res.get("distances"))
    docs = _as_list(res.get("documents"))
    metas = _as_list(res.get("metadatas"))
    ids = ids[0] if ids else []
    dists = dists[0] if dists else []
    docs = docs[0] if docs else []
    metas = metas[0] if metas else []
    out: list[dict] = []
    for i, rid in enumerate(ids):
        try:
            score = round(1.0 - float(dists[i]), 4)
        except Exception:
            score = 0.0
        meta = metas[i] if i < len(metas) else {}
        out.append({
            "id": str(rid),
            "score": score,
            "document": docs[i] if i < len(docs) else None,
            "metadata": meta if isinstance(meta, dict) else {},
        })
    return out


def delete(name: str, ids: list | None = None, where: dict | None = None) -> None:
    """Delete by ids or by metadata filter (no-op when nothing matches)."""
    if not ids and not where:
        return
    collection(name).delete(ids=list(ids) if ids else None, where=where)


def count(name: str) -> int:
    try:
        return int(collection(name).count())
    except Exception:
        return 0


def get_meta(name: str) -> dict:
    md = collection(name).metadata
    return dict(md) if isinstance(md, dict) else {}


def update_meta(name: str, meta: dict) -> None:
    """Merge keys into collection metadata (hnsw:* keys are never re-sent)."""
    col = collection(name)
    merged = {k: v for k, v in get_meta(name).items() if not str(k).startswith("hnsw:")}
    merged.update(meta or {})
    merged = {k: v for k, v in merged.items()
              if v is None or isinstance(v, (str, int, float, bool))}
    try:
        col.modify(metadata=merged)
    except Exception:
        pass


def drop(name: str) -> None:
    """Remove a collection entirely (and its cache entry)."""
    key = (str(store_dir()), name)
    with _lock:
        try:
            _client().delete_collection(name)
        except Exception:
            pass
        _collections.pop(key, None)
