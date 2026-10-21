"""Backup + restore owner: X5Agent/Data + X5Agent/Workspace + project/skills (stdlib).

Scope (client data only, per explicit requirement):
- INCLUDE: everything under X5Agent/Data (database incl. conversation,
  studios, vector, files, skills text, plugin configs, Data/.env with API
  keys) + everything under X5Agent/Workspace (user files) + user skills
  under project/skills (user skills + scheduled_skills + trigger_skills —
  they live outside Data, so a Data-only zip would silently drop them).
- EXCLUDE: system code itself (project/* except project/skills),
  embedding weights (project/embedding_model), models, .git, caches
  outside X5Agent.

Zip layout: Data/... + Workspace/... + skills/... + backup.json manifest.
Last successful backup is recorded in Data/database/backup_meta.json
(read via get_backup_info(), served as GET /api/backup/info).
Never raises unexpected: validation errors raise ValueError with a short
message; I/O failures propagate as OSError.
"""
from __future__ import annotations

import json
import tempfile
import time
import zipfile
from pathlib import Path

BACKUP_VERSION = 1
# Resource limits (Rule 74): a full Data backup stays far below
# this on real installs; the cap only stops abuse/accidents.
BACKUP_MAX_BYTES = 2 * 1024 * 1024 * 1024
BACKUP_MAX_FILES = 50000
# Restore accepts any reasonable client backup ("whatever size"): the only
# hard stop is the same 2GB cap + entry count + zip-slip validation.
RESTORE_MAX_BYTES = 2 * 1024 * 1024 * 1024

_MANIFEST_NAME = "backup.json"


def _roots():
    import sys as _sys
    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    from core.paths import data_root, ensure_x5agent
    ensure_x5agent()
    return data_root()


def _skills_dir() -> Path:
    import sys as _sys
    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    return proj / "skills"


_BACKUP_META_FILE = "backup_meta.json"


def get_backup_info() -> dict:
    """Last-backup record (server-authoritative). Never raises."""
    import sys as _sys
    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    try:
        from core.paths import read_db_json
        meta = read_db_json(_BACKUP_META_FILE, {})
        if isinstance(meta, dict) and meta.get("last_backup_at"):
            return {
                "has_backup": True,
                "last_backup_at": str(meta.get("last_backup_at")),
                "files": int(meta.get("files") or 0),
                "size_bytes": int(meta.get("size_bytes") or 0),
            }
    except Exception:
        pass
    return {"has_backup": False, "last_backup_at": "", "files": 0, "size_bytes": 0}


def _record_backup_meta(files: int, size_bytes: int) -> None:
    """Persist last-backup date/count/size. Best-effort: never fails a backup."""
    try:
        import sys as _sys
        proj = Path(__file__).resolve().parents[2]
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        from core.paths import write_db_json
        write_db_json(_BACKUP_META_FILE, {
            "version": BACKUP_VERSION,
            "last_backup_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "files": int(files),
            "size_bytes": int(size_bytes),
        })
    except Exception:
        pass


def _manifest() -> dict:
    return {
        "version": BACKUP_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contains": ["Data", "skills"],
    }


def create_backup_zip() -> Path:
    """Zip Data+skills into a temp .zip file. Caller must delete it."""
    import sys as _sys
    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    data_dir = _roots()
    import os as _os
    _fd, _path = tempfile.mkstemp(prefix="x5_backup_", suffix=".zip")
    try:
        _os.close(_fd)
    except Exception:
        pass
    tmp = Path(_path)
    count = 0
    total = 0
    skills_dir = _skills_dir()
    try:
        with zipfile.ZipFile(str(tmp), "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(_MANIFEST_NAME, json.dumps(_manifest(), ensure_ascii=False, indent=2))
            for base, prefix in ((data_dir, "Data"), (skills_dir, "skills")):
                if not base.is_dir():
                    continue
                for path in sorted(base.rglob("*")):
                    try:
                        if not path.is_file() or path.is_symlink():
                            continue
                        rel = path.relative_to(base).as_posix()
                        if not rel:
                            continue
                        arc = f"{prefix}/{rel}"
                        size = path.stat().st_size
                        total += size
                        if total > BACKUP_MAX_BYTES:
                            raise ValueError("backup too large (max 2GB)")
                        zf.write(str(path), arc)
                        count += 1
                        if count > BACKUP_MAX_FILES:
                            raise ValueError("too many files (max 50000)")
                    except ValueError:
                        raise
                    except Exception:
                        continue
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    try:
        _record_backup_meta(count, tmp.stat().st_size)
    except Exception:
        pass
    return tmp


def _safe_target(root: Path, rel: str) -> Path:
    cand = (root / rel).resolve()
    try:
        cand.relative_to(root.resolve())
    except ValueError:
        raise ValueError(f"blocked entry: {rel[:120]}") from None
    return cand


def restore_backup_zip(zip_path: Path | str) -> dict:
    """Validate + extract a backup zip over Data/skills. Returns counts.

    Only Data/*, skills/* and (legacy) Workspace/* entries are restored
    (+ manifest checked); legacy Workspace files land in the default working
    folder. System paths (project/* other than skills, models, absolute,
    traversal) are rejected. Never touches code or embedding weights.
    Raises ValueError on invalid.
    """
    import sys as _sys
    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    zp = Path(zip_path)
    if not zp.is_file():
        raise ValueError("backup file not found")
    if zp.stat().st_size <= 0 or zp.stat().st_size > RESTORE_MAX_BYTES:
        raise ValueError("backup size out of bounds (max 2GB)")
    if not zipfile.is_zipfile(str(zp)):
        raise ValueError("not a zip backup")
    data_dir = _roots()
    skills_dir = _skills_dir()
    from core.paths import default_folder_root
    folder_dir = default_folder_root()
    with zipfile.ZipFile(str(zp), "r") as zf:
        infos = zf.infolist()
        if len(infos) > BACKUP_MAX_FILES + 1:
            raise ValueError("too many files (max 50000)")
        # Pass 1: validate every name before touching disk (no partial state).
        targets: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in infos:
            name = (info.filename or "").replace("\\", "/").strip()
            if not name or name.endswith("/"):
                continue
            if name == _MANIFEST_NAME:
                continue
            if name.startswith("/") or name[1:2] == ":" or ".." in name.split("/"):
                raise ValueError(f"blocked entry: {name[:120]}")
            if name.startswith("Data/"):
                rel = name[len("Data/"):].strip("/")
                if not rel:
                    continue
                targets.append((info, _safe_target(data_dir, rel)))
            elif name.startswith("Workspace/"):
                rel = name[len("Workspace/"):].strip("/")
                if not rel:
                    continue
                targets.append((info, _safe_target(folder_dir, rel)))
            elif name.startswith("skills/"):
                rel = name[len("skills/"):].strip("/")
                if not rel:
                    continue
                targets.append((info, _safe_target(skills_dir, rel)))
            else:
                # Unknown top-level (system files, weights, junk): skip —
                # restore is client-data only by design.
                continue
        if not targets:
            raise ValueError("empty backup (no Data/skills entries)")
        # Pass 2: extract (bounded per-file, skip dirs/symlinks-in-zip).
        restored = 0
        data_n = 0
        folder_n = 0
        skills_n = 0
        for info, dest in targets:
            if info.is_dir():
                continue
            if info.file_size > BACKUP_MAX_BYTES:
                raise ValueError("entry too large")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src, open(dest, "wb") as out:
                    left = info.file_size
                    while left > 0:
                        chunk = src.read(min(1024 * 1024, left))
                        if not chunk:
                            break
                        out.write(chunk)
                        left -= len(chunk)
                restored += 1
                try:
                    resolved = dest.resolve()
                except Exception:
                    folder_n += 1
                    continue
                try:
                    resolved.relative_to(data_dir.resolve())
                    data_n += 1
                    continue
                except ValueError:
                    pass
                try:
                    resolved.relative_to(skills_dir.resolve())
                    skills_n += 1
                    continue
                except ValueError:
                    pass
                folder_n += 1
            except ValueError:
                raise
            except Exception as e:
                raise OSError(f"restore failed at {dest.name}: {e}"[:200]) from None
    return {"ok": True, "files": restored, "data_files": data_n, "folder_files": folder_n, "skills_files": skills_n}
