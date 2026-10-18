"""
X5Agent path resolver - single source of truth for sandbox layout.

Layout (created on first load):
  <repo>/X5Agent/
    Workspace/   # ONLY place the model may read/write/execute (its device)
    Data/        # ALL user data (zip backup): database, vector, files, skills text, plugin configs, .env
      database/ vector/ files/images files/audio files/videos skills/ configs/plugin_configs/

Model weights live in project/models/<type>/<name> (NOT in X5Agent).
System Core (immutable engine + plugins + tool code) lives in project/
and is logically X5Agent/System Core - never writable via model tools.

System Core (immutable engine + plugins + tool code) lives in project/
and is logically X5Agent/System Core - never writable via model tools.

All model-facing filesystem/terminal tools MUST resolve against Workspace.
Internal services (memories, skills_view, embeddings) read Data directly.
"""
from __future__ import annotations

from pathlib import Path


def _repo_root() -> Path:
    # core/paths.py -> parents[0]=core, [1]=project, [2]=repo root
    return Path(__file__).resolve().parents[2]


def x5agent_root() -> Path:
    return _repo_root() / "X5Agent"


def workspace_root() -> Path:
    return x5agent_root() / "Workspace"


def data_root() -> Path:
    """X5Agent/Data — unless the container entrypoint pinned a persistent store.

    The entrypoint writes ALL live data into the manager's bind
    (/app/data/x5store) which survives single-room rebuilds while bundled
    paths do not — so the store wins whenever its marker file exists.
    Local dev (no /app/data): falls back to the normal X5Agent/Data.
    """
    try:
        import os as _os

        store = Path("/app/data/x5store")
        marker = store / ".x5store"
        if store.is_dir() and marker.is_file():
            return store
        # First touch: claim the store layout (marker lives on the bind, so it
        # survives rebuilds; local dev without binds never reaches here).
        if Path("/app/data").is_dir():
            try:
                store.mkdir(parents=True, exist_ok=True)
                if store.is_dir():
                    marker.write_text("x5agent persistent store", encoding="utf-8")
                    return store
            except Exception:
                pass
    except Exception:
        pass
    return x5agent_root() / "Data"


def models_root() -> Path:
    # Model weights live in project/models/<type>/<name> (first download lands here).
    return _repo_root() / "project" / "models"


def system_core_root() -> Path:
    # Logical System Core = physical project/ (immutable engine)
    return _repo_root() / "project"


def data_subdir(*parts: str) -> Path:
    return data_root().joinpath(*parts)


def _layout_dirs() -> list[Path]:
    """The X5Agent layout this module owns, in creation order."""
    ws = workspace_root()
    dr = data_root()
    return [
        ws,
        ws / "inbox" / "docs",
        ws / "inbox" / "tts",
        ws / "inbox" / "media",
        ws / "outbox" / "done",
        ws / "outbox" / "ready_upload",
        dr,
        dr / "database",
        dr / "database" / "studio_sessions",
        dr / "studios",
        dr / "vector",
        dr / "files" / "images",
        dr / "files" / "audio",
        dr / "files" / "videos",
        dr / "skills",
        dr / "configs" / "plugin_configs",
    ]


def ensure_x5agent() -> dict[str, str]:
    """Create X5Agent/{Workspace,Data} + Data subdirs. Idempotent. Never raises.

    The stores resolve every path through here, so this runs on each path build
    and the steady state has to be cheap: when the layout is already complete it
    only checks existence (a few stats) instead of re-issuing all sixteen mkdir
    calls, which cost milliseconds per call. A directory that is missing — or a
    tree that was deleted underneath us — still triggers the full pass, so the
    layout exists whenever this returns.
    """
    created: dict[str, str] = {}
    try:
        dirs = _layout_dirs()
        if all(d.is_dir() for d in dirs):
            return created
        for d in dirs:
            try:
                d.mkdir(parents=True, exist_ok=True)
                created[d.name] = str(d)
            except Exception:
                continue
    except Exception:
        pass
    return created


def is_inside_workspace(candidate: Path) -> bool:
    """True if resolved candidate is inside Workspace (or is Workspace itself)."""
    try:
        ws = workspace_root().resolve()
        cand = candidate.resolve() if candidate.is_absolute() else (ws / str(candidate)).resolve()
        try:
            cand.relative_to(ws)
            return True
        except ValueError:
            return False
    except Exception:
        return False


def resolve_inside_workspace(user_path: str) -> Path:
    """
    Resolve a model-supplied path strictly inside Workspace.
    Raises ValueError if escape is attempted. Never trusts input.
    """
    if not isinstance(user_path, str):
        raise TypeError("path must be string")
    s = user_path.strip()
    if not s:
        raise ValueError("path must be non-empty")
    if len(s) > 500:
        raise ValueError("path too long")
    ws = workspace_root().resolve()
    cand = Path(s)
    if not cand.is_absolute():
        cand = (ws / s).resolve()
    else:
        cand = cand.resolve()
    try:
        cand.relative_to(ws)
    except ValueError:
        raise ValueError(f"Blocked: outside Workspace. Workspace is {ws}. Got: {user_path[:120]}") from None
    return cand


def legacy_project_root() -> Path:
    return system_core_root()


# ---------------------------------------------------------------------------
# Unified Data layer (final unification): ALL runtime user data lives in
# X5Agent/Data. Writes ALWAYS go to Data. Reads prefer Data, fallback legacy
# project/storage (kept read-only for backward compatibility).
# ---------------------------------------------------------------------------

def legacy_storage_db_dir() -> Path:
    return system_core_root() / "storage" / "database"


def legacy_storage_vector_dir() -> Path:
    return system_core_root() / "storage" / "vector"


def data_db_dir() -> Path:
    return data_root() / "database"


def data_vector_dir() -> Path:
    return data_root() / "vector"


def data_db_file(filename: str) -> Path:
    """Authoritative write path for a database file (ensures parent dir)."""
    p = data_db_dir() / filename
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def data_vector_file(filename: str) -> Path:
    """Authoritative write path for a vector file (ensures parent dir)."""
    p = data_vector_dir() / filename
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    return p


def read_db_json(filename: str, default: object) -> object:
    """Read Data first, fallback legacy storage. Never raises."""
    import json as _json
    for cand in (data_db_dir() / filename, legacy_storage_db_dir() / filename):
        try:
            if cand.exists():
                data = _json.loads(cand.read_text(encoding="utf-8"))
                return data if data is not None else default
        except Exception:
            continue
    return default


def write_db_json(filename: str, data: object) -> Path:
    """Write to Data only (single source of truth). Ensures dir."""
    import json as _json
    p = data_db_file(filename)
    p.write_text(_json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


def read_vector_json(filename: str, default: object) -> object:
    """Read Data/vector first, fallback legacy storage/vector. Never raises."""
    import json as _json
    for cand in (data_vector_dir() / filename, legacy_storage_vector_dir() / filename):
        try:
            if cand.exists():
                data = _json.loads(cand.read_text(encoding="utf-8"))
                return data if data is not None else default
        except Exception:
            continue
    return default


# Files migrated one-time from legacy storage into Data (never overwrites Data).
_MIGRATE_DB_FILES = [
    "conversation.json", "todos.json", "scheduler.json", "triggers.json",
    "user_memory.json", "agent_memory.json", "facts_memory.json",
    "agent_notes.json", "plugin_state.json", "model_providers.json",
    "video_analyze_jobs.json", "video_transcribe_jobs.json",
]
_MIGRATE_VECTOR_FILES = [
    "tool_embeddings.json", "chat_embeddings.json", "trigger_embeddings.json",
]


def migrate_legacy_to_data_once() -> dict[str, int]:
    """
    One-time copy legacy project/storage -> X5Agent/Data.
    Copies a file ONLY when Data copy is missing and legacy exists.
    Never overwrites Data. Idempotent. Never raises.
    Returns {"db": n, "vector": m, "env": k}.
    """
    result = {"db": 0, "vector": 0, "env": 0}
    try:
        ensure_x5agent()
        import shutil as _shutil
        for name in _MIGRATE_DB_FILES:
            try:
                dst = data_db_dir() / name
                src = legacy_storage_db_dir() / name
                if not dst.exists() and src.exists() and src.is_file():
                    _shutil.copy2(str(src), str(dst))
                    result["db"] += 1
            except Exception:
                continue
        for name in _MIGRATE_VECTOR_FILES:
            try:
                dst = data_vector_dir() / name
                src = legacy_storage_vector_dir() / name
                if not dst.exists() and src.exists() and src.is_file():
                    _shutil.copy2(str(src), str(dst))
                    result["vector"] += 1
            except Exception:
                continue
        # .env into backup (server also handles load/save, this is belt-and-suspenders)
        try:
            dst = data_root() / ".env"
            src = system_core_root() / ".env"
            if not dst.exists() and src.exists() and src.is_file():
                _shutil.copy2(str(src), str(dst))
                result["env"] += 1
        except Exception:
            pass
    except Exception:
        pass
    return result
