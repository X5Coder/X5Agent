"""
Embedding store for discoverable tools.

Responsibility: maintain embeddings for tool descriptions in ChromaDB
(X5Agent/Data/vector/chroma), auto-create on startup, and rebuild only the
specs that changed. Uses the bundled embedding model via
core/embedding/service.py (offline).

A prebuilt seed (tool_embeddings.seed.json, committed JSON) is imported on a
fresh copy when specs are unchanged, so no embedding call is needed the first
time. The runtime store itself is Chroma, never JSON.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

_COLLECTION = "tools"
_PROJECT = Path(__file__).resolve().parents[3]


def _vb():
    import sys as _sys
    if str(_PROJECT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT))
    from core.embedding import vector_db
    return vector_db


# Prebuilt seed shipped with the project (committed, small JSON). Used when
# the runtime Chroma collection is empty and specs are unchanged - instant, offline.
_SEED_CANDIDATES = (
    _PROJECT / "storage" / "vector" / "tool_embeddings.seed.json",
    Path(__file__).resolve().parent / "tool_embeddings.seed.json",
)


def _legacy_json_candidates() -> tuple[Path, ...]:
    """Old JSON runtime store (migrated into Chroma on first run)."""
    out = [Path(__file__).resolve().parents[3] / "storage" / "vector" / "tool_embeddings.json"]
    try:
        import sys as _sys
        if str(_PROJECT) not in _sys.path:
            _sys.path.insert(0, str(_PROJECT))
        from core.paths import data_vector_dir
        out.insert(0, data_vector_dir() / "tool_embeddings.json")
    except Exception:
        pass
    return tuple(out)


def _project_root() -> Path:
    return _PROJECT


def _collect_discoverable_specs() -> list[dict]:
    """
    Collect specs for all discoverable tools.
    Scans plugins/*/ */spec.json dynamically + ensures the 5 required tools are present.
    """
    root = _project_root()
    plugins_root = root / "plugins"
    tools: list[dict] = []
    # Session-only tools: they live in studio branch sessions alone (the
    # studio loop pre-seeds them directly) and must never surface in the
    # main agent via tool_search.
    _SESSION_ONLY = {"studio_page", "studio_read"}

    if plugins_root.exists():
        for category in plugins_root.iterdir():
            if not category.is_dir():
                continue
            if category.name == "tool-discovery":
                continue
            for tool_dir in category.iterdir():
                if not tool_dir.is_dir():
                    continue
                if tool_dir.name == "skill_create":
                    continue
                if tool_dir.name in _SESSION_ONLY:
                    continue
                spec_path = tool_dir / "spec.json"
                if spec_path.exists():
                    try:
                        spec = json.loads(spec_path.read_text(encoding="utf-8"))
                        if not isinstance(spec.get("name"), str) or not isinstance(spec.get("description"), str):
                            continue
                        tools.append(spec)
                    except Exception:
                        continue
    # document_search lives in core/tools on disk but is classified
    # discoverable (behind tool_search); the folder stays for import compat.
    for _disc_name in ("document_search",):
        try:
            _spec_path = root / "core" / "tools" / _disc_name / "spec.json"
            if _spec_path.exists():
                _spec = json.loads(_spec_path.read_text(encoding="utf-8"))
                if (isinstance(_spec.get("name"), str) and isinstance(_spec.get("description"), str)
                        and _spec.get("name") not in {t.get("name") for t in tools}):
                    tools.append(_spec)
        except Exception:
            pass
    # Browser discoverable tools (project/X5Browser, single versioned folder).
    try:
        import sys as _sys2
        _root2 = _project_root()
        if str(_root2) not in _sys2.path:
            _sys2.path.insert(0, str(_root2))
        from X5Browser.catalog import SPECS as _BROWSER_SPECS
        _seen = {t.get("name") for t in tools}
        for _spec in _BROWSER_SPECS:
            try:
                if not isinstance(_spec, dict) or _spec.get("core"):
                    continue
                if not isinstance(_spec.get("name"), str) or not isinstance(_spec.get("description"), str):
                    continue
                if _spec.get("name") in _seen:
                    continue
                tools.append({"name": _spec["name"], "description": _spec["description"],
                              "inputs": _spec.get("inputs", {})})
                _seen.add(_spec["name"])
            except Exception:
                continue
    except Exception:
        pass
    # Sort by name for deterministic hash
    tools.sort(key=lambda x: x.get("name", ""))
    return tools


def _compute_hash(tools: list[dict]) -> str:
    # Hash of concatenated name + description exactly as in spec (verbatim)
    blob = ""
    for t in tools:
        blob += f"{t.get('name','')}:{t.get('description','')}\n"
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _spec_fingerprint(name: str, description: str) -> str:
    """Per-spec fingerprint (stored on every entry).

    Incremental-update contract: a stored embedding is reused ONLY when
    its fingerprint still matches the current spec. New specs, edited
    descriptions, and legacy entries (no fingerprint) are re-embedded;
    removed specs simply disappear. Never raises.
    """
    try:
        blob = f"{name}:{description}"
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()
    except Exception:
        return ""


def _read_records() -> list[dict]:
    """Every stored tool record (with embeddings) back from Chroma."""
    got = _vb().all_records(_COLLECTION)
    out: list[dict] = []
    for rid, emb, meta in zip(got["ids"], got["embeddings"], got["metadatas"]):
        meta = meta or {}
        spec: dict = {}
        try:
            spec = json.loads(meta.get("spec_json") or "{}")
            if not isinstance(spec, dict):
                spec = {}
        except Exception:
            spec = {}
        out.append({
            "name": meta.get("name") or rid,
            "description": meta.get("description") or "",
            "spec": spec,
            "embedding": emb,
            "fingerprint": meta.get("fingerprint") or "",
        })
    return out


def _write_all(hash_val: str, records: list[dict]) -> None:
    """Replace the whole collection with these records (clears removed specs)."""
    vb = _vb()
    vb.drop(_COLLECTION)
    ids = [str(r.get("name", "")) for r in records]
    embs = [r.get("embedding") or [] for r in records]
    docs = [f"{r.get('name','')} : {r.get('description','')}"[:2000] for r in records]
    metas = [{
        "name": str(r.get("name", "")),
        "description": str(r.get("description", ""))[:8000],
        "spec_json": json.dumps(r.get("spec") or {}, ensure_ascii=False),
        "fingerprint": str(r.get("fingerprint", "")),
    } for r in records]
    vb.upsert(_COLLECTION, ids, embs, docs, metas)
    vb.update_meta(_COLLECTION, {
        "hash": hash_val,
        "model_id": "embedding-model",
        "model_path": "embedding_model",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "count": len(records),
    })


def _load_json_store(candidates, key: str, current_hash: str) -> list[dict] | None:
    """Read one JSON store that matches the current hash. Never raises."""
    for cand in candidates:
        try:
            if not cand.exists():
                continue
            data = json.loads(cand.read_text(encoding="utf-8"))
            if (isinstance(data, dict) and data.get("hash") == current_hash
                    and isinstance(data.get(key), list) and data[key]):
                first = data[key][0]
                if isinstance(first.get("embedding"), list) and first["embedding"]:
                    return data[key]
        except Exception:
            continue
    return None


# Single-flight rebuild guard (file-based, NOT a memory lock).
# tool_search re-execs this file on every call, so each call gets fresh
# module globals: a threading.Lock here would never coordinate across calls.
# The guard file serializes concurrent rebuilds across threads, processes,
# and fresh module loads.
_REBUILD_GUARD_NAME = "tools.rebuilding"
_REBUILD_GUARD_STALE_SECONDS = 300
_REBUILD_WAIT_SECONDS = 180


def _rebuild_guard_path() -> Path:
    d = _vb().store_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d / _REBUILD_GUARD_NAME


def _stored_is_fresh(current_hash: str) -> bool:
    vb = _vb()
    try:
        if vb.count(_COLLECTION) == 0:
            return False
        return vb.get_meta(_COLLECTION).get("hash") == current_hash
    except Exception:
        return False


def _rebuild_claimed_by_other() -> bool:
    try:
        guard = _rebuild_guard_path()
        if not guard.exists():
            return False
        try:
            age = time.time() - guard.stat().st_mtime
        except Exception:
            return False
        if age > _REBUILD_GUARD_STALE_SECONDS:
            try:
                guard.unlink(missing_ok=True)
            except Exception:
                pass
            return False
        return True
    except Exception:
        return False


def _claim_rebuild() -> bool:
    try:
        path = _rebuild_guard_path()
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        try:
            os.write(fd, str(os.getpid()).encode("utf-8"))
        except Exception:
            pass
        try:
            os.close(fd)
        except Exception:
            pass
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _release_rebuild() -> None:
    try:
        _rebuild_guard_path().unlink(missing_ok=True)
    except Exception:
        pass


def ensure_embeddings(auto_create: bool = True) -> list[dict]:
    """
    Ensure the Chroma tool collection exists and matches current specs.
    Uses the shipped seed / legacy JSON first (no model), otherwise embeds
    only new/changed specs via the bundled model. Returns the records.
    """
    tools = _collect_discoverable_specs()
    if not tools:
        return []

    current_hash = _compute_hash(tools)
    vb = _vb()

    if vb.count(_COLLECTION) > 0 and vb.get_meta(_COLLECTION).get("hash") == current_hash:
        return _read_records()

    if vb.count(_COLLECTION) == 0:
        records = (_load_json_store(_legacy_json_candidates(), "tools", current_hash)
                   or _load_json_store(_SEED_CANDIDATES, "tools", current_hash))
        if records is not None:
            try:
                _write_all(current_hash, records)
                print(f"[tool-discovery] Loaded prebuilt tool embeddings "
                      f"({len(records)} tools, hash {current_hash[:8]})")
            except Exception:
                pass
            return _read_records()

    # Slow rebuild path is single-flight (file guard): the fast hash check
    # stays unblocked, losers wait bounded for the winner's fresh store.
    if _rebuild_claimed_by_other():
        deadline = time.time() + _REBUILD_WAIT_SECONDS
        while time.time() < deadline:
            time.sleep(2)
            if _stored_is_fresh(current_hash):
                return _read_records()
    won = _claim_rebuild()
    if not won and _stored_is_fresh(current_hash):
        return _read_records()
    try:
        if _stored_is_fresh(current_hash):
            return _read_records()
        if not auto_create:
            return []
        if vb.count(_COLLECTION) > 0:
            print(f"[tool-discovery] Descriptions changed "
                  f"({vb.get_meta(_COLLECTION).get('hash', '')[:8]} -> {current_hash[:8]}), rebuilding...")

        texts = [f"{t.get('name','')} : {t.get('description','')}"[:2000] for t in tools]
        fingerprints = [_spec_fingerprint(t.get("name", ""), t.get("description", "")) for t in tools]

        old_by_name = {r["name"]: r for r in _read_records() if r.get("embedding")}
        final_emb: list = [None] * len(tools)
        reused = 0
        for i, t in enumerate(tools):
            old = old_by_name.get(t.get("name", ""))
            if (old is not None and old.get("fingerprint") == fingerprints[i]
                    and isinstance(old.get("embedding"), list) and old["embedding"]):
                final_emb[i] = old["embedding"]
                reused += 1

        def _embed_some(indexes: list[int]) -> None:
            try:
                from core.embedding.service import embed_texts, is_ready
            except Exception as e:
                raise RuntimeError(f"Embedding service not available: {e}") from e
            if not is_ready():
                raise FileNotFoundError(
                    "Embedding model folder is missing (project/embedding_model/). "
                    "Copy it with the project and restart.")
            got = embed_texts([texts[i] for i in indexes], normalize=True,
                              progress_label="tool-discovery")
            if len(got) != len(indexes):
                raise RuntimeError(f"Embedding batch incomplete ({len(got)}/{len(indexes)})")
            for i, emb in zip(indexes, got):
                final_emb[i] = emb

        need = [i for i, e in enumerate(final_emb) if e is None]
        if need:
            _embed_some(need)
        dim = next((len(e) for e in final_emb if isinstance(e, list) and e), 0)
        bad = [i for i, e in enumerate(final_emb)
               if not isinstance(e, list) or len(e) != dim or dim == 0]
        if bad:
            _embed_some(bad)
            dim = next((len(e) for e in final_emb if isinstance(e, list) and e), 0)
            bad = [i for i, e in enumerate(final_emb)
                   if not isinstance(e, list) or len(e) != dim or dim == 0]
        if bad:
            raise RuntimeError(f"Embedding rebuild left {len(bad)} tools without vectors")

        records = [{
            "name": spec["name"],
            "description": spec["description"],
            "spec": spec,
            "embedding": emb,
            "fingerprint": fp,
        } for spec, emb, fp in zip(tools, final_emb, fingerprints)]

        _write_all(current_hash, records)
        print(f"[tool-discovery] Incremental update: reused {reused}, "
              f"embedded {len(records) - reused} of {len(records)} tools, hash {current_hash[:8]}")
        return records
    finally:
        if won:
            _release_rebuild()


def get_embeddings() -> list[dict]:
    """Return current embeddings, ensuring they are up-to-date (auto-create)."""
    return ensure_embeddings(auto_create=True)


def search(query: str, top_k: int = 5) -> list[dict]:
    """Semantic search over discoverable tools using Chroma cosine similarity."""
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty string")
    if len(query) > 2000:
        raise ValueError("query too long")
    if not isinstance(top_k, int) or top_k < 1 or top_k > 20:
        top_k = 5

    ensure_embeddings(auto_create=True)
    vb = _vb()
    if vb.count(_COLLECTION) == 0:
        return []

    from core.embedding.service import embed_query
    rows = vb.query(_COLLECTION, embed_query(query), top_k)
    results = []
    for r in rows:
        md = r.get("metadata") or {}
        try:
            spec = json.loads(md.get("spec_json") or "{}")
            if not isinstance(spec, dict):
                spec = {}
        except Exception:
            spec = {}
        results.append({
            "name": md.get("name", ""),
            "description": md.get("description", ""),
            "spec": spec,
            "score": r.get("score", 0.0),
        })
    return results


def snapshot_seed() -> dict | None:
    """Seed dict (committable JSON) for the current runtime store, or None if
    the runtime collection is empty / out of date (run ensure first)."""
    tools = _collect_discoverable_specs()
    if not tools:
        return None
    current_hash = _compute_hash(tools)
    vb = _vb()
    if vb.count(_COLLECTION) == 0 or vb.get_meta(_COLLECTION).get("hash") != current_hash:
        return None
    records = _read_records()
    return {
        "hash": current_hash,
        "model_id": "embedding-model",
        "model_path": "embedding_model",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tools": records,
        "count": len(records),
    }


def force_rebuild() -> list[dict]:
    """Force delete and rebuild embeddings (used when descriptions change manually)."""
    _vb().drop(_COLLECTION)
    return ensure_embeddings(auto_create=True)
