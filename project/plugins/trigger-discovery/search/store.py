"""
Trigger embedding store - per-plugin triggers, searchable via trigger_search.

Stored in ChromaDB (X5Agent/Data/vector/chroma). Uses the bundled embedding
model via core/embedding/service.py (offline). Ships with a prebuilt seed
(trigger_embeddings.seed.json) imported on a fresh copy when specs match.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path

_COLLECTION = "triggers"
_PROJECT = Path(__file__).resolve().parents[3]


def _vb():
    import sys as _sys
    if str(_PROJECT) not in _sys.path:
        _sys.path.insert(0, str(_PROJECT))
    from core.embedding import vector_db
    return vector_db


_SEED_CANDIDATES = (
    _PROJECT / "storage" / "vector" / "trigger_embeddings.seed.json",
    Path(__file__).resolve().parent / "trigger_embeddings.seed.json",
)


def _legacy_json_candidates() -> tuple[Path, ...]:
    out = [Path(__file__).resolve().parents[3] / "storage" / "vector" / "trigger_embeddings.json"]
    try:
        import sys as _sys
        if str(_PROJECT) not in _sys.path:
            _sys.path.insert(0, str(_PROJECT))
        from core.paths import data_vector_dir
        out.insert(0, data_vector_dir() / "trigger_embeddings.json")
    except Exception:
        pass
    return tuple(out)


def _project_root() -> Path:
    return _PROJECT


def _collect_trigger_specs() -> list[dict]:
    root = _project_root()
    triggers = []
    plugins_root = root / "plugins"
    if plugins_root.exists():
        for category in plugins_root.iterdir():
            if not category.is_dir():
                continue
            triggers_dir = category / "triggers"
            if not triggers_dir.exists():
                continue
            for trig_dir in triggers_dir.iterdir():
                if not trig_dir.is_dir():
                    continue
                spec_path = trig_dir / "spec.json"
                if spec_path.exists():
                    try:
                        spec = json.loads(spec_path.read_text(encoding="utf-8"))
                        if isinstance(spec.get("name"), str) and isinstance(spec.get("description"), str):
                            triggers.append(spec)
                    except Exception:
                        continue
    # Browser page-monitor trigger (project/X5Browser, single versioned folder).
    try:
        import sys as _sys2
        _root2 = _project_root()
        if str(_root2) not in _sys2.path:
            _sys2.path.insert(0, str(_root2))
        from X5Browser.catalog import SPECS as _BROWSER_SPECS
        _seen = {t.get("name") for t in triggers}
        for _spec in _BROWSER_SPECS:
            try:
                if not isinstance(_spec, dict):
                    continue
                if _spec.get("name") != "manage_triggers":
                    continue
                if not isinstance(_spec.get("description"), str):
                    continue
                if _spec.get("name") in _seen:
                    continue
                triggers.append({"name": _spec["name"], "description": _spec["description"],
                                 "inputs": _spec.get("inputs", {}),
                                 "source": "X5Browser"})
                _seen.add(_spec.get("name"))
            except Exception:
                continue
    except Exception:
        pass
    triggers.sort(key=lambda x: x.get("name", ""))
    return triggers


def _compute_hash(triggers: list[dict]) -> str:
    blob = ""
    for t in triggers:
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


_REBUILD_GUARD_NAME = "triggers.rebuilding"
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
    triggers = _collect_trigger_specs()
    if not triggers:
        return []
    current_hash = _compute_hash(triggers)
    vb = _vb()

    if vb.count(_COLLECTION) > 0 and vb.get_meta(_COLLECTION).get("hash") == current_hash:
        return _read_records()

    if vb.count(_COLLECTION) == 0:
        records = (_load_json_store(_legacy_json_candidates(), "triggers", current_hash)
                   or _load_json_store(_SEED_CANDIDATES, "triggers", current_hash))
        if records is not None:
            try:
                _write_all(current_hash, records)
                print(f"[trigger-discovery] Loaded prebuilt trigger embeddings "
                      f"({len(records)} triggers, hash {current_hash[:8]})")
            except Exception:
                pass
            return _read_records()

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
            print(f"[trigger-discovery] Triggers changed "
                  f"({vb.get_meta(_COLLECTION).get('hash', '')[:8]} -> {current_hash[:8]}), rebuilding...")

        texts = [f"{t.get('name','')} : {t.get('description','')}"[:2000] for t in triggers]
        fingerprints = [_spec_fingerprint(t.get("name", ""), t.get("description", "")) for t in triggers]

        old_by_name = {r["name"]: r for r in _read_records() if r.get("embedding")}
        final_emb: list = [None] * len(triggers)
        reused = 0
        for i, t in enumerate(triggers):
            old = old_by_name.get(t.get("name", ""))
            if (old is not None and old.get("fingerprint") == fingerprints[i]
                    and isinstance(old.get("embedding"), list) and old["embedding"]):
                final_emb[i] = old["embedding"]
                reused += 1

        def _embed_some(indexes: list) -> None:
            from core.embedding.service import embed_texts, is_ready
            if not is_ready():
                raise FileNotFoundError(
                    "Embedding model folder is missing (project/embedding_model/). "
                    "Copy it with the project and restart.")
            got = embed_texts([texts[i] for i in indexes], normalize=True,
                              progress_label="trigger-discovery")
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
            raise RuntimeError(f"Embedding rebuild left {len(bad)} triggers without vectors")

        records = [{
            "name": spec["name"],
            "description": spec["description"],
            "spec": spec,
            "embedding": emb,
            "fingerprint": fp,
        } for spec, emb, fp in zip(triggers, final_emb, fingerprints)]

        _write_all(current_hash, records)
        print(f"[trigger-discovery] Incremental update: reused {reused}, "
              f"embedded {len(records) - reused} of {len(records)} triggers, hash {current_hash[:8]}")
        return records
    finally:
        if won:
            _release_rebuild()


def search(query: str, top_k: int = 5) -> list[dict]:
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be non-empty")
    if len(query) > 2000:
        raise ValueError("query too long")
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
    """Seed dict (committable JSON) for the current runtime store, or None."""
    triggers = _collect_trigger_specs()
    if not triggers:
        return None
    current_hash = _compute_hash(triggers)
    vb = _vb()
    if vb.count(_COLLECTION) == 0 or vb.get_meta(_COLLECTION).get("hash") != current_hash:
        return None
    records = _read_records()
    return {
        "hash": current_hash,
        "model_id": "embedding-model",
        "model_path": "embedding_model",
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "triggers": records,
        "count": len(records),
    }
