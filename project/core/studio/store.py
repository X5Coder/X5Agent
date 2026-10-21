"""
PDF Studio store — single owner of studio data (backup-ready).

Layout (ALL inside X5Agent/Data so zipping Data backs up every studio):
  X5Agent/Data/studios/pdf_<n>/
    studio.json            # {studio_id, title, created_at, page_size, pages:[{page_no, has_code}]}
    pages/Page01.jsx ...   # ONE React component per slide (exactly one code file each)
    assets/fonts/          # uploaded fonts for this studio only
    assets/images/         # uploaded images for this studio only

The React shell itself lives ONCE in project/web/studio-pdf/ (never copied).
Each pdf_studio tool call creates a NEW numbered studio here.
"""
from __future__ import annotations

import functools as _functools
import json
import re
from datetime import datetime, timezone
from pathlib import Path

STUDIO_ID_RE = re.compile(r"^pdf_\d{1,6}$")
MAX_TITLE_CHARS = 120
MAX_PAGES = 50
MAX_CODE_BYTES = 200_000
DEFAULT_PAGES = 1
MAX_ERRORS = 20
MAX_HISTORY = 20
# A turn older than this is treated as dead (server crashed mid-turn):
# readers ignore its designing flag instead of showing loading forever.
DESIGNING_TIMEOUT_SEC = 1800
# A new studio starts with ONE page only; scrolling down inside the studio
# creates each next page (as a real empty file) one by one.
# Unified page size for EVERY page in EVERY studio (single environment).
UNIFIED_PAGE = {"label": "A4 portrait", "w": 794, "h": 1123, "mm": "210 × 297 mm"}
# The ONE size table (pdf-motion preset method): a studio is exactly one of
# these for its whole life. The shell preview, the model context, and the
# export all read the SAME studio orientation — mismatch is impossible.
ORIENTATION_DIMS = {
    "portrait": {"label": "A4 portrait", "w": 794, "h": 1123, "mm": "210 × 297 mm"},
    "landscape": {"label": "A4 landscape", "w": 1123, "h": 794, "mm": "297 × 210 mm"},
}
# Page lifecycle: empty -> ready (approved) -> needs_fix (error reported).
STATUS_EMPTY = "empty"
STATUS_READY = "ready"
STATUS_NEEDS_FIX = "needs_fix"


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def studios_root() -> Path:
    try:
        import sys as _sys
        proj = _project_root()
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        from core.paths import data_root, ensure_x5agent
        ensure_x5agent()
        root = data_root() / "studios"
        root.mkdir(parents=True, exist_ok=True)
        return root
    except Exception:
        root = _project_root().parents[0] / "X5Agent" / "Data" / "studios"
        root.mkdir(parents=True, exist_ok=True)
        return root


def validate_studio_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("studio_id is required (e.g. pdf_1)")
    sid = value.strip()
    if not STUDIO_ID_RE.match(sid):
        raise ValueError(f"Invalid studio_id: {sid[:40]}")
    return sid


def validate_page_no(value: object) -> int:
    try:
        n = int(value)  # type: ignore[arg-type]
    except Exception:
        raise ValueError("page_no must be an integer") from None
    if n < 1 or n > MAX_PAGES:
        raise ValueError(f"page_no out of range 1..{MAX_PAGES}")
    return n


def validate_code(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("code is required (full React component)")
    if len(value.encode("utf-8", "ignore")) > MAX_CODE_BYTES:
        raise ValueError(f"code too large (max {MAX_CODE_BYTES} bytes)")
    return value


_MUTEX = None


def studio_lock(studio_id: str = ""):
    """Process-wide studio mutation lock (re-entrant).

    Every read-modify-write cycle (approve/history/error/rename/add_page)
    and every session append runs under it, so concurrent turns on one
    studio never corrupt studio.json or the session file. One lock is
    enough at this scale (local single-user server); critical sections
    are tiny (file writes only, never model calls).
    """
    global _MUTEX
    if _MUTEX is None:
        import threading as _th
        _MUTEX = _th.RLock()
    return _MUTEX


def _mutual(fn):
    """Run a store mutation under the studio lock (concurrent turns safe)."""
    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with studio_lock():
            return fn(*args, **kwargs)
    return wrapper


def _studio_dir(studio_id: str) -> Path:
    return studios_root() / validate_studio_id(studio_id)


def _meta_path(studio_id: str) -> Path:
    return _studio_dir(studio_id) / "studio.json"


def _page_path(studio_id: str, page_no: int) -> Path:
    return _studio_dir(studio_id) / "pages" / f"Page{page_no:02d}.jsx"


def _default_page_code(page_no: int, title: str) -> str:
    safe = title.replace('"', "'")[:80] if isinstance(title, str) else "Untitled"
    return (
        "// Studio page %d — empty. The studio agent delivers the full design here.\n"
        "export default function Page%02d() {\n"
        "  return (\n"
        '    <section className="pdf-page" data-page="%d">\n'
        '      <h1>%s — page %d</h1>\n'
        "      <p>Empty — waiting for the studio agent.</p>\n"
        "    </section>\n"
        "  );\n"
        "}\n" % (page_no, page_no, page_no, safe, page_no)
    )


def _next_id() -> str:
    root = studios_root()
    best = 0
    try:
        for child in root.iterdir():
            m = re.fullmatch(r"pdf_(\d+)", child.name)
            if m and child.is_dir():
                best = max(best, int(m.group(1)))
    except Exception:
        pass
    return f"pdf_{best + 1}"


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def title_taken(title: str, exclude: str = "") -> str:
    """Return the studio_id already using this title (case-insensitive), else ''."""
    want = str(title or "").strip().lower()
    if not want:
        return ""
    try:
        for child in studios_root().iterdir():
            if not child.is_dir() or child.name == exclude:
                continue
            try:
                meta = json.loads((child / "studio.json").read_text(encoding="utf-8"))
                if str(meta.get("title", "")).strip().lower() == want:
                    return child.name
            except Exception:
                continue
    except Exception:
        pass
    return ""


def used_titles() -> list[str]:
    out: list[str] = []
    try:
        for child in sorted(studios_root().iterdir()):
            if child.is_dir() and STUDIO_ID_RE.match(child.name):
                try:
                    meta = json.loads((child / "studio.json").read_text(encoding="utf-8"))
                    t = str(meta.get("title", "")).strip()
                    if t:
                        out.append(t)
                except Exception:
                    continue
    except Exception:
        pass
    return out[-50:]


@_mutual
def create_studio(title: str = "Untitled", pages: int = DEFAULT_PAGES) -> dict:
    name = str(title or "Untitled").strip()[:MAX_TITLE_CHARS] or "Untitled"
    clash = title_taken(name)
    if clash:
        raise ValueError(f"Studio name '{name}' is already used by {clash}. Pick an unused name.")
    try:
        count = int(pages)
    except Exception:
        count = DEFAULT_PAGES
    count = max(1, min(MAX_PAGES, count))
    sid = _next_id()
    base = studios_root() / sid
    (base / "pages").mkdir(parents=True, exist_ok=True)
    (base / "assets" / "fonts").mkdir(parents=True, exist_ok=True)
    (base / "assets" / "images").mkdir(parents=True, exist_ok=True)
    meta = {
        "studio_id": sid,
        "title": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "page_size": "A4",
        "orientation": "portrait",
        "size_locked": False,
        "pages": [{"page_no": i, "has_code": False, "status": STATUS_EMPTY} for i in range(1, count + 1)],
        "errors": [],
        "designing": {},
    }
    for i in range(1, count + 1):
        p = base / "pages" / f"Page{i:02d}.jsx"
        if not p.exists():
            p.write_text(_default_page_code(i, name), encoding="utf-8")
    _write_json_atomic(base / "studio.json", meta)
    return {"studio_id": sid, "title": name, "pages": count,
            "open_url": f"/studio-pdf/{sid}", "meta": meta}


def _read_meta(studio_id: str) -> dict:
    """Parse one studio's studio.json exactly once.

    Every status derivation (designing / orientation / size lock / history
    counts) needs this same file, and on an edited studio it is a large one —
    the undo/redo stack holds a full copy of every design version — so a caller
    that needs several of them reads it through here instead of paying one
    file read plus one JSON parse of the whole history per helper.
    """
    sid = validate_studio_id(studio_id)
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError(f"Corrupt studio.json for {sid}: {e}") from None
    return meta if isinstance(meta, dict) else {}


def _asset_names(sid: str) -> tuple:
    """Saved studio asset file names (fonts, images), bounded for reads."""
    base = _studio_dir(sid)
    fonts_dir = base / "assets" / "fonts"
    images_dir = base / "assets" / "images"
    fonts = sorted([p.name for p in fonts_dir.iterdir() if p.is_file()]) if fonts_dir.is_dir() else []
    images = sorted([p.name for p in images_dir.iterdir() if p.is_file()]) if images_dir.is_dir() else []
    return fonts[:100], images[:200]


def get_studio(studio_id: str) -> dict:
    sid = validate_studio_id(studio_id)
    meta = _read_meta(sid)
    fonts, images = _asset_names(sid)
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    return {"studio_id": sid, "meta": meta, "pages": pages,
            "fonts": fonts, "images": images, "open_url": f"/studio-pdf/{sid}",
            "history": _history_counts_from(meta)}


def studio_view(studio_id: str) -> dict:
    """The status view the console and the shell's poll read: everything from
    ONE read of studio.json, and WITHOUT the undo/redo version stack.

    That stack is a full copy of every design version (~100KB+ on an edited
    studio, growing with every edit) and nothing here uses it — callers get the
    small per-page COUNTS and read the stack only when the user really undoes.
    It used to ride along on every open, every shell poll and every console
    card sweep, and each derived field re-read and re-parsed the whole file.
    """
    sid = validate_studio_id(studio_id)
    meta = _read_meta(sid)
    counts = _history_counts_from(meta)  # counted BEFORE the stack is dropped
    meta.pop("history", None)
    fonts, images = _asset_names(sid)
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    return {"studio_id": sid, "meta": meta, "pages": pages,
            "fonts": fonts, "images": images, "open_url": f"/studio-pdf/{sid}",
            "history": counts,
            "designing": sorted(int(k) for k in _designing_fresh_from(meta)),
            "orientation": _orientation_from(meta),
            "size_locked": _size_locked_from(meta),
            "page": _dims_from(meta)}


def list_studios() -> list[dict]:
    out: list[dict] = []
    try:
        for child in sorted(studios_root().iterdir()):
            if child.is_dir() and STUDIO_ID_RE.match(child.name):
                try:
                    meta = json.loads((child / "studio.json").read_text(encoding="utf-8"))
                    out.append({"studio_id": child.name, "title": str(meta.get("title", ""))[:80],
                                "pages": len(meta.get("pages", [])) if isinstance(meta.get("pages"), list) else 0})
                except Exception:
                    out.append({"studio_id": child.name, "title": "", "pages": 0})
    except Exception:
        pass
    return out[-50:]


@_mutual
def rename_studio(studio_id: str, title: str) -> dict:
    """Rename a studio via the same tool. Titles stay unique per studio."""
    sid = validate_studio_id(studio_id)
    name = str(title or "").strip()[:MAX_TITLE_CHARS]
    if not name:
        raise ValueError("title is required (unused studio name)")
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    clash = title_taken(name, exclude=sid)
    if clash:
        raise ValueError(f"Studio name '{name}' is already used by {clash}. Pick an unused name.")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["title"] = name
    _write_json_atomic(meta_path, meta)
    return {"studio_id": sid, "title": name}


@_mutual
def add_page(studio_id: str) -> dict:
    sid = validate_studio_id(studio_id)
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    nxt = (max([int(p.get("page_no", 0)) for p in pages if isinstance(p, dict)] or [0]) + 1)
    if nxt > MAX_PAGES:
        raise ValueError(f"Studio is full (max {MAX_PAGES} pages)")
    _page_path(sid, nxt).write_text(_default_page_code(nxt, str(meta.get("title", ""))), encoding="utf-8")
    pages.append({"page_no": nxt, "has_code": False, "status": STATUS_EMPTY})
    meta["pages"] = pages
    _write_json_atomic(meta_path, meta)
    return {"studio_id": sid, "page_no": nxt}


def _history_bins(meta: dict) -> dict:
    hist = meta.get("history")
    if not isinstance(hist, dict):
        hist = {"undo": {}, "redo": {}}
        meta["history"] = hist
    for key in ("undo", "redo"):
        if not isinstance(hist.get(key), dict):
            hist[key] = {}
    return hist


@_mutual
def write_page_code(studio_id: str, page_no: int, code: str) -> dict:
    sid = validate_studio_id(studio_id)
    n = validate_page_no(page_no)
    body = validate_code(code)
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    target = _page_path(sid, n)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Save-state: every approve pushes the previous code, so undo/redo steps
    # through each delivered state. Bounded (last MAX_HISTORY per page).
    try:
        meta_pre = json.loads(meta_path.read_text(encoding="utf-8"))
        hist = _history_bins(meta_pre)
        if target.is_file():
            prev = target.read_text(encoding="utf-8")
            if prev != body:
                stack = hist["undo"].get(str(n))
                if not isinstance(stack, list):
                    stack = []
                    hist["undo"][str(n)] = stack
                stack.append({"code": prev[:MAX_CODE_BYTES],
                              "ts": datetime.now(timezone.utc).isoformat()})
                del stack[:-MAX_HISTORY]
                hist["redo"][str(n)] = []
                _write_json_atomic(meta_path, meta_pre)
    except Exception:
        pass
    target.write_text(body, encoding="utf-8")
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
        found = False
        for p in pages:
            if isinstance(p, dict) and int(p.get("page_no", -1)) == n:
                # ONE code per slide file: approve overwrites, never appends.
                p["has_code"] = True
                p["status"] = STATUS_READY
                found = True
        if not found:
            pages.append({"page_no": n, "has_code": True, "status": STATUS_READY})
        meta["pages"] = sorted(pages, key=lambda x: int(x.get("page_no", 0)))
        # The designing flag stays until the TURN ends (cleared by the chat
        # runner): deliver-then-patch inside one turn must keep working.
        _write_json_atomic(meta_path, meta)
    except Exception:
        pass
    return {"studio_id": sid, "page_no": n, "path": f"pages/Page{n:02d}.jsx",
            "bytes": len(body.encode("utf-8", "ignore"))}


def read_page_code(studio_id: str, page_no: int) -> dict:
    sid = validate_studio_id(studio_id)
    n = validate_page_no(page_no)
    target = _page_path(sid, n)
    if not target.is_file():
        return {"studio_id": sid, "page_no": n, "has_code": False, "code": ""}
    try:
        text = target.read_text(encoding="utf-8")
        # Placeholder pages created by create_studio/add_page count as empty
        # until the studio agent delivers a real design via studio_page.
        if text.startswith("// Studio page"):
            return {"studio_id": sid, "page_no": n, "has_code": False, "code": ""}
        return {"studio_id": sid, "page_no": n, "has_code": True,
                "code": text[:MAX_CODE_BYTES]}
    except Exception as e:
        raise ValueError(f"Cannot read page {n}: {e}") from None


# Studio asset files the page code may actually show: every entry carries
# the exact src path to use inside the design (<img src> / @font-face).
# Served at GET /api/studio/asset?studio_id=<sid>&file=<dir>/<name>
# (same-origin, owner-gated, real MIME — canvas/export safe).
ASSET_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "webp": "image/webp", "gif": "image/gif",
    "ttf": "font/ttf", "otf": "font/otf",
    "woff": "font/woff", "woff2": "font/woff2",
}
_ASSET_DIRS = ("images", "fonts")
_ASSET_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
MAX_ASSET_BYTES = 8 * 1024 * 1024


def asset_url(studio_id: str, rel: str) -> str:
    """Usable src path for a studio asset inside page code. Never raises."""
    try:
        from urllib.parse import quote as _quote
        return ("/api/studio/asset?studio_id=" + _quote(validate_studio_id(studio_id), safe="") +
                "&file=" + _quote(str(rel or ""), safe="/"))
    except Exception:
        return ""


def resolve_asset_file(studio_id: str, rel: str) -> tuple[Path, str]:
    """Validate + resolve a studio asset for serving. Raises on any abuse.

    rel must be images/<name> or fonts/<name> with an allowlisted
    extension; the resolved path must stay inside the studio dir, be a
    real file and fit the size cap. Uploads are magic-checked already,
    so only shape + location + size are enforced here.
    """
    sid = validate_studio_id(studio_id)
    rel = str(rel or "").strip().replace("\\", "/")
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if len(parts) != 2 or parts[0] not in _ASSET_DIRS:
        raise ValueError("file must be images/<name> or fonts/<name>")
    name = parts[-1]
    if not _ASSET_NAME_RE.match(name) or "." not in name:
        raise ValueError("unsafe file name")
    ext = name.rsplit(".", 1)[-1].lower()
    mime = ASSET_MIME.get(ext)
    if mime is None:
        raise ValueError(f"extension .{ext[:10]} is not servable")
    base = _studio_dir(sid)
    if not (base / "studio.json").is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    cand = (base / "assets" / parts[0] / name).resolve()
    try:
        cand.relative_to(base.resolve())
    except ValueError:
        raise ValueError("file escapes the studio") from None
    if not cand.is_file():
        raise FileNotFoundError(f"Unknown asset file: {rel[:80]}")
    try:
        if cand.stat().st_size > MAX_ASSET_BYTES or cand.stat().st_size < 1:
            raise ValueError("asset file size out of range")
    except ValueError:
        raise
    except Exception as e:
        raise ValueError(f"Cannot stat asset: {e}") from None
    return cand, mime


MAX_ASSET_META = 300


def _asset_meta_path(studio_id: str) -> Path:
    return _studio_dir(validate_studio_id(studio_id)) / "assets" / "meta.json"


def read_asset_meta(studio_id: str) -> dict:
    """Remembered per-asset facts {rel: {source_url, query, bytes, ts}}. Never raises."""
    try:
        p = _asset_meta_path(studio_id)
        if not p.is_file():
            return {}
        data = json.loads(p.read_text(encoding="utf-8"))
        return dict(data) if isinstance(data, dict) else {}
    except Exception:
        return {}


def record_asset_meta(studio_id: str, rel: str, info: dict) -> None:
    """Remember what an asset is (web source URL + search words). Never raises.

    Bounded (last MAX_ASSET_META kept, oldest dropped). rel must be
    images/<name> or fonts/<name>.
    """
    try:
        sid = validate_studio_id(studio_id)
        rel = str(rel or "").strip().replace("\\", "/")
        parts = [p for p in rel.split("/") if p not in ("", ".")]
        if len(parts) != 2 or parts[0] not in _ASSET_DIRS or not isinstance(info, dict):
            return
        p = _asset_meta_path(sid)
        meta = read_asset_meta(sid)
        if rel in meta:
            del meta[rel]
        meta[rel] = {
            "source_url": str(info.get("source_url", ""))[:500],
            "query": str(info.get("query", ""))[:200],
            "bytes": int(info.get("bytes", 0) or 0),
            "ts": str(info.get("ts", ""))[:32],
        }
        while len(meta) > MAX_ASSET_META:
            meta.pop(next(iter(meta)))
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


def asset_entries(studio_id: str) -> list[dict]:
    """Every servable asset: {dir, name, kind, size, src, query, source}. Never raises."""
    out: list[dict] = []
    try:
        sid = validate_studio_id(studio_id)
        meta = read_asset_meta(sid)
        base = _studio_dir(sid)
        for d in _ASSET_DIRS:
            folder = base / "assets" / d
            if not folder.is_dir():
                continue
            try:
                names = sorted(p.name for p in folder.iterdir() if p.is_file())
            except Exception:
                continue
            for name in names[:200]:
                if name == "meta.json":
                    continue
                ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
                if ext not in ASSET_MIME:
                    continue
                try:
                    size = (folder / name).stat().st_size
                except Exception:
                    continue
                m = meta.get(f"{d}/{name}") if isinstance(meta, dict) else None
                m = m if isinstance(m, dict) else {}
                out.append({"dir": d, "name": name,
                            "kind": "image" if d == "images" else "font",
                            "size": int(size),
                            "src": asset_url(sid, f"{d}/{name}"),
                            "query": str(m.get("query", ""))[:200],
                            "source": str(m.get("source_url", ""))[:200]})
    except Exception:
        pass
    return out


def _orientation_from(meta: dict) -> str:
    o = str(meta.get("orientation", "portrait")).strip().lower()
    return o if o in ORIENTATION_DIMS else "portrait"


def _dims_from(meta: dict) -> dict:
    return dict(ORIENTATION_DIMS[_orientation_from(meta)])


def _size_locked_from(meta: dict) -> bool:
    """A page is designed, or the first request already froze the size."""
    if meta.get("size_locked") is True:
        return True
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    return any(isinstance(p, dict) and p.get("has_code") for p in pages)


def studio_orientation(studio_id: str) -> str:
    """This studio's orientation: portrait|landscape (default portrait). Never raises."""
    try:
        return _orientation_from(_read_meta(studio_id))
    except Exception:
        return "portrait"


def studio_dims(studio_id: str) -> dict:
    """Exact page box for this studio (single source of truth). Never raises."""
    try:
        return _dims_from(_read_meta(studio_id))
    except Exception:
        return dict(ORIENTATION_DIMS["portrait"])


def studio_size_locked(studio_id: str) -> bool:
    """True once the first request was sent (orientation frozen). Never raises."""
    try:
        return _size_locked_from(_read_meta(studio_id))
    except Exception:
        return False


@_mutual
def set_orientation(studio_id: str, orientation: str) -> dict:
    """Switch portrait|landscape BEFORE the first request. Rejects once locked."""
    sid = validate_studio_id(studio_id)
    want = str(orientation or "").strip().lower()
    if want not in ORIENTATION_DIMS:
        raise ValueError("orientation must be portrait|landscape")
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if meta.get("size_locked") is True:
        raise ValueError("Size locked after the first request — orientation is frozen.")
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    if any(isinstance(p, dict) and p.get("has_code") for p in pages):
        raise ValueError("Size locked — a page is already designed.")
    meta["orientation"] = want
    _write_json_atomic(meta_path, meta)
    return {"studio_id": sid, "orientation": want, "page": dict(ORIENTATION_DIMS[want])}


def _designing_raw(meta: dict) -> dict:
    d = meta.get("designing") if isinstance(meta, dict) else None
    return dict(d) if isinstance(d, dict) else {}


def _designing_fresh(raw: dict) -> dict:
    """Keep only live turns ({str(page_no): iso_ts}); stale ones are dead."""
    out: dict = {}
    try:
        now = datetime.now(timezone.utc)
        for k, v in raw.items():
            try:
                n = int(k)
                if not (1 <= n <= MAX_PAGES):
                    continue
                ts = datetime.fromisoformat(str(v))
                if (now - ts).total_seconds() < DESIGNING_TIMEOUT_SEC:
                    out[str(n)] = str(v)
            except Exception:
                continue
    except Exception:
        pass
    return out


def _designing_fresh_from(meta: dict) -> dict:
    """Live turns only ({str(page_no): iso_ts}) from an already parsed meta."""
    return _designing_fresh(_designing_raw(meta))


def designing_pages(studio_id: str) -> dict:
    """Pages with a LIVE turn running: {page_no: started_iso}. Never raises."""
    try:
        fresh = _designing_fresh_from(_read_meta(studio_id))
        return {int(k): v for k, v in fresh.items()}
    except Exception:
        return {}


@_mutual
def mark_designing(studio_id: str, page_no: int) -> None:
    """Flag a page while its turn executes (single source of truth for the
    black loading template; survives reloads, unlike client timers). Never raises."""
    try:
        sid = validate_studio_id(studio_id)
        n = validate_page_no(page_no)
        meta_path = _meta_path(sid)
        if not meta_path.is_file():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        fresh = _designing_fresh(_designing_raw(meta))
        fresh[str(n)] = datetime.now(timezone.utc).isoformat()
        meta["designing"] = fresh
        # First request sent: the page size freezes for the studio's whole life.
        meta["size_locked"] = True
        _write_json_atomic(meta_path, meta)
    except Exception:
        pass


@_mutual
def clear_designing(studio_id: str, page_no: int) -> None:
    """Drop the flag when the turn ends (delivered, empty-handed, or failed). Never raises."""
    try:
        sid = validate_studio_id(studio_id)
        n = validate_page_no(page_no)
        meta_path = _meta_path(sid)
        if not meta_path.is_file():
            return
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        raw = _designing_raw(meta)
        if str(n) in raw:
            del raw[str(n)]
            meta["designing"] = raw
            _write_json_atomic(meta_path, meta)
    except Exception:
        pass


def _history_counts_from(meta: dict) -> dict:
    """Per-page undo/redo depths, derived from an already parsed meta."""
    hist = meta.get("history") if isinstance(meta.get("history"), dict) else {}
    out: dict = {}
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    for p in pages:
        if isinstance(p, dict):
            k = str(int(p.get("page_no", 0)))
            u = hist.get("undo", {}).get(k) if isinstance(hist.get("undo"), dict) else []
            r = hist.get("redo", {}).get(k) if isinstance(hist.get("redo"), dict) else []
            out[k] = {"undo": len(u) if isinstance(u, list) else 0,
                      "redo": len(r) if isinstance(r, list) else 0}
    return out


def history_counts(studio_id: str) -> dict:
    """Per-page undo/redo depths for toolbar button states. Never raises."""
    try:
        return _history_counts_from(_read_meta(studio_id))
    except Exception:
        return {}


@_mutual
def step_page(studio_id: str, page_no: int, direction: str) -> dict:
    """Undo/redo one delivered state on a page (toolbar step buttons).

    Pops the target stack, pushes the current code to the other stack, writes
    the file (one code per slide, overwrite) and marks it ready. Bounded.
    """
    sid = validate_studio_id(studio_id)
    n = validate_page_no(page_no)
    d = str(direction or "").strip().lower()
    if d not in ("undo", "redo"):
        raise ValueError("direction must be undo|redo")
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    hist = _history_bins(meta)
    src = hist["undo" if d == "undo" else "redo"].get(str(n))
    if not isinstance(src, list) or not src:
        raise ValueError(f"Nothing to {d} on slide {n}")
    target = _page_path(sid, n)
    current = target.read_text(encoding="utf-8") if target.is_file() else ""
    entry = src.pop()
    code = str(entry.get("code", "")) if isinstance(entry, dict) else ""
    if not code.strip():
        raise ValueError(f"Nothing to {d} on slide {n}")
    dst = hist["redo" if d == "undo" else "undo"].get(str(n))
    if not isinstance(dst, list):
        dst = []
        hist["redo" if d == "undo" else "undo"][str(n)] = dst
    if current.strip():
        dst.append({"code": current[:MAX_CODE_BYTES],
                    "ts": datetime.now(timezone.utc).isoformat()})
        del dst[:-MAX_HISTORY:]
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(validate_code(code), encoding="utf-8")
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    for p in pages:
        if isinstance(p, dict) and int(p.get("page_no", -1)) == n:
            p["has_code"] = True
            p["status"] = STATUS_READY
    _write_json_atomic(meta_path, meta)
    return {"studio_id": sid, "page_no": n, "direction": d,
            "history": history_counts(sid)}


@_mutual
def delete_page(studio_id: str, page_no: int) -> dict:
    """Delete ONE slide entirely; later slides shift up to take its number.

    Removes the PageNN.jsx file (later files renamed down), drops the page
    from the meta list, remaps undo/redo stacks + designing flags + recorded
    errors past it. Refuses the last remaining page (a studio always keeps
    at least one). History of the deleted slide is gone with it.
    """
    sid = validate_studio_id(studio_id)
    n = validate_page_no(page_no)
    meta_path = _meta_path(sid)
    if not meta_path.is_file():
        raise FileNotFoundError(f"Unknown studio: {sid}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
    nums = sorted({int(p.get("page_no", 0)) for p in pages if isinstance(p, dict)})
    if n not in nums:
        raise ValueError(f"Slide {n} does not exist")
    if len(nums) <= 1:
        raise ValueError("Cannot delete the only page of a studio")
    top = max(nums)
    try:
        _page_path(sid, n).unlink(missing_ok=True)
    except Exception:
        pass
    for k in range(n + 1, top + 1):
        try:
            src = _page_path(sid, k)
            if src.is_file():
                src.replace(_page_path(sid, k - 1))
        except Exception:
            continue
    meta["pages"] = sorted(
        ({"page_no": int(p.get("page_no", 0)) - 1 if int(p.get("page_no", 0)) > n
          else int(p.get("page_no", 0)),
          "has_code": bool(p.get("has_code")),
          "status": str(p.get("status", STATUS_EMPTY))}
         for p in pages if isinstance(p, dict) and int(p.get("page_no", 0)) != n),
        key=lambda x: x["page_no"])
    try:
        hist = _history_bins(meta)
        for key in ("undo", "redo"):
            bins = hist.get(key)
            if not isinstance(bins, dict):
                continue
            moved: dict = {}
            for k, v in bins.items():
                try:
                    kn = int(k)
                except Exception:
                    continue
                if kn == n:
                    continue
                moved[str(kn - 1 if kn > n else kn)] = v
            hist[key] = moved
    except Exception:
        pass
    try:
        raw = _designing_raw(meta)
        meta["designing"] = {str(int(k) - 1 if int(k) > n else int(k)): v
                             for k, v in raw.items() if int(k) != n}
    except Exception:
        pass
    try:
        errs = meta.get("errors") if isinstance(meta.get("errors"), list) else []
        kept = []
        for e in errs:
            if not isinstance(e, dict):
                continue
            try:
                en = int(e.get("page_no", 0))
            except Exception:
                continue
            if en == n:
                continue
            e2 = dict(e)
            e2["page_no"] = en - 1 if en > n else en
            kept.append(e2)
        meta["errors"] = kept[-MAX_ERRORS:]
    except Exception:
        pass
    _write_json_atomic(meta_path, meta)
    return {"studio_id": sid, "deleted": n, "pages": len(meta["pages"]),
            "history": history_counts(sid)}


def slide_states_text(studio_id: str) -> str:
    """Compact slide states for the studio context: 'Slide N: empty|has code'."""
    try:
        info = get_studio(studio_id)
        lines = []
        for p in info.get("pages", []):
            if isinstance(p, dict):
                n = int(p.get("page_no", 0))
                state = "has code" if p.get("has_code") else "empty"
                lines.append(f"Slide {n}: {state}")
        return "\n".join(lines[:MAX_PAGES]) or "(no slides)"
    except Exception as e:
        return f"(studio states unavailable: {e})"


@_mutual
def append_page_error(studio_id: str, page_no: int, error: str) -> dict:
    """Record a silent render error (user never sees it; the agent fixes it).

    Marks the page needs_fix. Bounded (last MAX_ERRORS kept). Never raises.
    """
    sid = validate_studio_id(studio_id)
    n = validate_page_no(page_no)
    msg = str(error or "").strip()[:500] or "unknown render error"
    try:
        meta_path = _meta_path(sid)
        if not meta_path.is_file():
            raise FileNotFoundError(f"Unknown studio: {sid}")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        pages = meta.get("pages") if isinstance(meta.get("pages"), list) else []
        for p in pages:
            if isinstance(p, dict) and int(p.get("page_no", -1)) == n:
                p["status"] = STATUS_NEEDS_FIX
        errs = meta.get("errors") if isinstance(meta.get("errors"), list) else []
        errs.append({"page_no": n, "error": msg,
                     "ts": datetime.now(timezone.utc).isoformat()})
        meta["errors"] = errs[-MAX_ERRORS:]
        _write_json_atomic(meta_path, meta)
        try:
            from core.studio import session as _session
            _session.append_messages(sid, [{"role": "system",
                "content": f"Silent render error on slide {n}: {msg} (fix via studio_page, never show it to the user)."}])
        except Exception:
            pass
    except (FileNotFoundError, ValueError):
        raise
    except Exception as e:
        raise ValueError(f"Cannot record page error: {e}") from None
    return {"studio_id": sid, "page_no": n}


def recent_errors(studio_id: str, limit: int = 5) -> list[dict]:
    try:
        meta = json.loads(_meta_path(validate_studio_id(studio_id)).read_text(encoding="utf-8"))
        errs = meta.get("errors") if isinstance(meta.get("errors"), list) else []
        return [e for e in errs if isinstance(e, dict)][-max(1, min(10, int(limit))):]
    except Exception:
        return []


def slide_states_block(studio_id: str, current_page: int = 0) -> str:
    """Full per-slide context block: number + state (+place) for EVERY slide.

    The current page carries its task micro-prompt: execute the user request,
    then MUST finish through studio_page. All pages travel together
    so the agent always sees the whole deck state.
    """
    try:
        sid = validate_studio_id(studio_id)
        info = get_studio(sid)
        pages = info.get("pages", [])
        try:
            cur = int(current_page)
        except Exception:
            cur = 0
        errs = {int(e.get("page_no", 0)): str(e.get("error", ""))[:200]
                for e in recent_errors(studio_id) if isinstance(e, dict)}
        try:
            designing = set(designing_pages(sid).keys())
        except Exception:
            designing = set()
        lines = []
        for p in pages:
            if not isinstance(p, dict):
                continue
            n = int(p.get("page_no", 0))
            status = str(p.get("status", STATUS_EMPTY))
            if status == STATUS_READY:
                state = "has code"
            elif status == STATUS_NEEDS_FIX:
                state = "code under construction"
            else:
                state = "empty, no code"
            if n == cur and cur >= 1:
                lines.append(
                    f"Slide {n}: {state} — YOUR TASK HERE. The slide's CURRENT code travels "
                    f"in the task block below (re-read it with studio_page action=\"read\" "
                    f"only if it arrived truncated). Execute the user request below, "
                    f"then MUST finish with studio_page(studio_id=\"{sid}\", action=\"deliver\", "
                    f"page_no={n}, code=<full React component>) for a new design, or "
                    f"studio_page(studio_id=\"{sid}\", action=\"patch\", "
                    f"page_no={n}, old=<exact block>, new=<replacement>) for a section edit "
                    f"on an approved page (verify with action=\"test\"). Other slides: view "
                    f"with studio_read, links with http_request (GET/POST).")
            elif n in designing:
                lines.append(f"Slide {n}: {state} — designing (a turn is already running "
                             f"here, do not start another one on it; other slides stay sendable)")
            elif status == STATUS_NEEDS_FIX and n in errs and errs[n]:
                lines.append(f"Slide {n}: {state} — last error: {errs[n]}")
            else:
                lines.append(f"Slide {n}: {state}")
        return "\n".join(lines[:MAX_PAGES]) or "(no slides)"
    except Exception as e:
        return f"(slide states unavailable: {e})"


@_mutual
def purge_studios() -> dict:
    """Delete EVERY studio + its branch session (explicit-only helper).

    Removes Data/studios/pdf_* dirs and Data/database/studio_sessions/*.json.
    Memories, the working folder and all other Data are never touched. Never raises.
    NOTE: nothing calls this in normal operation — studios are never deleted:
    not by chat regenerate/edit/prune, not by Delete-chat-history. Kept only
    as an explicit helper (plus its test).
    """
    removed_studios = 0
    removed_sessions = 0
    try:
        root = studios_root()
        for child in list(root.iterdir()):
            try:
                if child.is_dir() and STUDIO_ID_RE.match(child.name):
                    import shutil as _shutil
                    _shutil.rmtree(str(child), ignore_errors=True)
                    if not child.exists():
                        removed_studios += 1
            except Exception:
                continue
    except Exception:
        pass
    try:
        import sys as _sys
        proj = _project_root()
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        from core.paths import data_root, ensure_x5agent
        ensure_x5agent()
        sdir = data_root() / "database" / "studio_sessions"
        if sdir.is_dir():
            for f in list(sdir.iterdir()):
                try:
                    if f.is_file() and f.suffix == ".json":
                        f.unlink()
                        removed_sessions += 1
                except Exception:
                    continue
    except Exception:
        pass
    return {"studios": removed_studios, "sessions": removed_sessions}
