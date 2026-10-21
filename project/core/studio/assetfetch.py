"""
Studio asset auto-fetch — web images/fonts the design uses land in the
studio assets automatically (offline-safe, export-safe, canvas-safe).

Two paths, same saver:
- explicit: studio_page action=fetch {url, query} — the model saves a found
  file deliberately; the search words travel as the asset's remembered
  description (meta query) so the deck knows what the file is.
- folder: studio_page action=fetch {file, query} — copy a working-folder
  file (e.g. an image_search result from images/) into the studio
  assets; same meta, no re-download, magic-checked again.
- automatic: deliver/patch scans the code for leftover hotlinked http(s)
  image/font URLs and saves them the same way (query unknown then).

Caps (bounded per Master Rules): max 3 files per call, 2MB each, 15s per
download, images (png/jpg/webp/gif) + fonts (ttf/otf/woff/woff2) only.
Failures keep the hotlink (degraded, never fatal) and are reported.
Stdlib only.
"""
from __future__ import annotations

import re
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

MAX_FILES_PER_CALL = 3
MAX_FILE_BYTES = 2 * 1024 * 1024
TIMEOUT_SEC = 15

_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
_FONT_EXTS = {"ttf", "otf", "woff", "woff2"}
_SAFE_RE = None


def _bootstrap():
    proj = Path(__file__).resolve().parents[1]
    if str(proj) not in sys.path:
        sys.path.insert(0, str(proj))


def _safe_re():
    global _SAFE_RE
    if _SAFE_RE is None:
        import re as _re
        _SAFE_RE = _re.compile(r"[^A-Za-z0-9_.-]")
    return _SAFE_RE


def _validate_url(url: object) -> str:
    if not isinstance(url, str):
        raise TypeError("url must be string")
    s = url.strip()
    if not s or len(s) > 2000:
        raise ValueError("url must be 1..2000 chars")
    try:
        parts = urlparse(s)
    except Exception:
        raise ValueError("Invalid url") from None
    if parts.scheme not in ("http", "https"):
        raise ValueError("Blocked: url must start with http:// or https://")
    if not parts.netloc:
        raise ValueError("Invalid url: missing host")
    if parts.username or parts.password:
        raise ValueError("Blocked: url must not contain credentials")
    return s


def _kind_of(url: str) -> str | None:
    """image|font by URL extension (query strings ignored), else None."""
    try:
        tail = urlparse(url).path.rsplit("/", 1)[-1].lower()
        ext = tail.rsplit(".", 1)[-1] if "." in tail else ""
        if ext in _IMAGE_EXTS:
            return "image"
        if ext in _FONT_EXTS:
            return "font"
    except Exception:
        pass
    return None


def _slug_name(url: str, hint: str = "") -> str:
    try:
        tail = urlparse(url).path.rsplit("/", 1)[-1]
        tail = _safe_re().sub("_", tail.strip())[:60].strip("._") or "webfile"
    except Exception:
        tail = "webfile"
    if "." not in tail:
        tail += ".png" if _kind_of(url) != "font" else ".woff2"
    head = _safe_re().sub("_", str(hint or "").strip().lower())[:24].strip("._")
    if head:
        tail = f"{head}_{tail}"[:80]
    return tail


def _magic_ok(blob: bytes, ext: str) -> bool:
    try:
        if ext == "png":
            return blob[:8] == b"\x89PNG\r\n\x1a\n"
        if ext in ("jpg", "jpeg"):
            return blob[:2] == b"\xff\xd8"
        if ext == "gif":
            return blob[:6] in (b"GIF87a", b"GIF89a")
        if ext == "webp":
            return blob[:4] == b"RIFF" and blob[8:12] == b"WEBP"
        # Fonts: trust extension (woff2/ttf have varied magic); size cap applies.
        return True
    except Exception:
        return False


def save_external(sid: str, url: str, query: str = "") -> dict:
    """Download one web file into the studio assets + remember it in meta.

    Returns {rel, src, bytes, file}. Raises on invalid URL, wrong type,
    oversize, timeout or network failure (caller keeps the hotlink then).
    """
    _bootstrap()
    from core.studio import store as _store
    clean = _validate_url(url)
    kind = _kind_of(clean)
    if kind is None:
        raise ValueError("Only image (png/jpg/webp/gif) and font (ttf/otf/woff/woff2) URLs are saved")
    req = urllib.request.Request(clean, headers={"User-Agent": "Mozilla/5.0"}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            blob = resp.read(MAX_FILE_BYTES + 1)
    except Exception as e:
        raise RuntimeError(f"Download failed: {type(e).__name__}: {str(e)[:120]}") from None
    if not blob or len(blob) > MAX_FILE_BYTES:
        raise ValueError(f"File must be 1..{MAX_FILE_BYTES // (1024 * 1024)}MB")
    name = _slug_name(clean)
    ext = name.rsplit(".", 1)[-1].lower()
    if not _magic_ok(blob, ext):
        raise ValueError("File content does not match its image type")
    folder = "images" if kind == "image" else "fonts"
    target = _store.studios_root() / _store.validate_studio_id(sid) / "assets" / folder / name
    target.parent.mkdir(parents=True, exist_ok=True)
    stem, suffix = name.rsplit(".", 1)[0][:60] or "webfile", name.rsplit(".", 1)[1]
    i = 1
    while target.exists():
        i += 1
        if i > 50:
            raise ValueError("Too many files with this name")
        target = target.parent / f"{stem}_{i}.{suffix}"
    target.write_bytes(blob)
    rel = f"{folder}/{target.name}"
    try:
        _store.record_asset_meta(sid, rel, {
            "source_url": clean[:500],
            "query": str(query or "").strip()[:200],
            "bytes": len(blob),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    except Exception:
        pass
    return {"rel": rel, "src": _store.asset_url(sid, rel),
            "bytes": len(blob), "file": target.name}


_FOLDER_IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}


def save_folder_file(sid: str, rel_path: str, query: str = "") -> dict:
    """Copy a working-folder file (e.g. an image_search result) into assets.

    The path is resolved strictly inside the working folder (traversal-proof);
    the bytes are magic-checked again and the search words are remembered as
    the asset's description, exactly like a web save. Raster images only
    (png/jpg/webp/gif — the only image types the studio shell serves).
    Returns {rel, src, bytes, file}. Raises on any problem.
    """
    _bootstrap()
    from core.studio import store as _store
    from core.workfolder import resolve as _resolve_folder
    clean_sid = _store.validate_studio_id(sid)
    if not isinstance(rel_path, str) or not rel_path.strip() or len(rel_path) > 500:
        raise ValueError("file must be a path inside the working folder (e.g. images/cat_1.jpg)")
    src = _resolve_folder(rel_path.strip().replace("\\", "/"))
    if not src.is_file():
        raise FileNotFoundError(f"File not found in the working folder: {rel_path[:120]}")
    try:
        size = src.stat().st_size
    except Exception:
        raise ValueError(f"Cannot read file: {rel_path[:120]}")
    if size < 1 or size > MAX_FILE_BYTES:
        raise ValueError(f"File must be 1..{MAX_FILE_BYTES // (1024 * 1024)}MB")
    ext = src.suffix.lower().lstrip(".")
    if ext not in _FOLDER_IMAGE_EXTS:
        raise ValueError("Only raster images (png/jpg/webp/gif) can be studio assets")
    try:
        blob = src.read_bytes()
    except Exception:
        raise ValueError(f"Cannot read file: {rel_path[:120]}")
    if not _magic_ok(blob, "jpeg" if ext == "jpg" else ext):
        raise ValueError("File content does not match its image type")
    stem = _safe_re().sub("_", src.stem.strip())[:40].strip("._") or "webfile"
    return _commit_asset(clean_sid, "images", stem, ext, blob, query,
                         f"folder:{rel_path.strip()[:400]}")


def _commit_asset(sid: str, folder: str, stem: str, ext: str,
                 blob: bytes, query: str, source_url: str) -> dict:
    """Write verified bytes into the studio assets + remember them in meta.

    Single owner of asset writes (web saves, folder copies and direct
    byte saves all land here). Collision-safe names, bounded meta.
    Returns {rel, src, bytes, file}. Raises on any problem.
    """
    _bootstrap()
    from core.studio import store as _store
    clean_sid = _store.validate_studio_id(sid)
    safe_stem = _safe_re().sub("_", (stem or "").strip())[:60].strip("._") or "webfile"
    target = _store.studios_root() / clean_sid / "assets" / folder / f"{safe_stem}.{ext}"
    target.parent.mkdir(parents=True, exist_ok=True)
    i = 1
    while target.exists():
        i += 1
        if i > 50:
            raise ValueError("Too many files with this name")
        target = target.parent / f"{safe_stem}_{i}.{ext}"
    target.write_bytes(blob)
    rel = f"{folder}/{target.name}"
    try:
        _store.record_asset_meta(clean_sid, rel, {
            "source_url": str(source_url or "")[:500],
            "query": str(query or "").strip()[:200],
            "bytes": len(blob),
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
    except Exception:
        pass
    return {"rel": rel, "src": _store.asset_url(clean_sid, rel),
            "bytes": len(blob), "file": target.name}


_SAVE_BYTES_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}


def save_bytes(sid: str, blob: bytes, ext: str, query: str = "", source_label: str = "") -> dict:
    """Store already-downloaded image bytes straight into studio assets.

    For callers that fetched + verified the file themselves (e.g. the
    image_search tool inside a studio turn): no re-download, magic-checked
    again, query remembered as the asset description. Raster images only.
    Returns {rel, src, bytes, file}. Raises on any problem.
    """
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        raise ValueError("image bytes are required")
    blob = bytes(blob)
    if len(blob) > MAX_FILE_BYTES:
        raise ValueError(f"File must be 1..{MAX_FILE_BYTES // (1024 * 1024)}MB")
    e = str(ext or "").lower().lstrip(".")
    if e not in _SAVE_BYTES_EXTS:
        raise ValueError("Only raster images (png/jpg/webp/gif) can be studio assets")
    if not _magic_ok(blob, "jpeg" if e == "jpg" else e):
        raise ValueError("File content does not match its image type")
    stem = _safe_re().sub("_", str(query or "").strip().lower())[:24].strip("._") or "webfile"
    return _commit_asset(sid, "images", stem, e, blob, query, source_label)


_IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc\s*=\s*[\"'](https?://[^\"']+)[\"']", re.IGNORECASE)
_CSS_URL_RE = re.compile(r"url\(\s*[\"']?(https?://[^\"')]+)[\"']?\s*\)", re.IGNORECASE)


def collect_external_urls(code: str) -> list[dict]:
    """Hotlinked web image/font URLs in page code, in order, deduplicated."""
    out: list[dict] = []
    seen: set[str] = set()
    try:
        text = str(code or "")
        for m in _IMG_SRC_RE.finditer(text):
            u = m.group(1)
            if u not in seen and _kind_of(u) == "image":
                seen.add(u)
                out.append({"url": u, "kind": "image"})
        for m in _CSS_URL_RE.finditer(text):
            u = m.group(1)
            if u in seen:
                continue
            k = _kind_of(u)
            if k is None:
                continue
            seen.add(u)
            out.append({"url": u, "kind": k})
    except Exception:
        pass
    return out


def absorb_external(sid: str, code: str) -> tuple[str, list[dict]]:
    """Save leftover hotlinks into assets + rewrite code to asset srcs.

    Returns (new_code, saved[]) with saved[] = [{url, rel, src, file}].
    At most MAX_FILES_PER_CALL are saved; failures keep their hotlink.
    Never raises (worst case: code unchanged, saved empty).
    """
    try:
        body = str(code or "")
        saved: list[dict] = []
        for item in collect_external_urls(body)[:MAX_FILES_PER_CALL]:
            try:
                got = save_external(sid, item["url"])
            except Exception:
                continue
            body = body.replace(item["url"], got["src"])
            saved.append({"url": item["url"], **got})
        return body, saved
    except Exception:
        try:
            return str(code or ""), []
        except Exception:
            return "", []
