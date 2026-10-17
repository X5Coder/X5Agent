"""
Media helpers for universal model tools (vision / speech).

Small, single-responsibility file I/O: resolve an image or audio file from an
id or a path, validate size/type, and hand back bytes or base64 for the
engine variables ({{image_base64}}, {{audio_base64}}). No model code here.
"""
from __future__ import annotations

import base64
from pathlib import Path

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".ogg", ".flac", ".webm")
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 25 * 1024 * 1024

_MIME_BY_EXT = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp",
    ".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".flac": "audio/flac", ".webm": "audio/webm",
}


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _image_dirs() -> list[Path]:
    dirs: list[Path] = []
    try:
        import sys as _sys
        if str(_project_root()) not in _sys.path:
            _sys.path.insert(0, str(_project_root()))
        from core.paths import data_subdir as _ds
        dirs.append((_ds("files", "images")).resolve())
    except Exception:
        pass
    dirs.append((_project_root() / "storage" / "files" / "images").resolve())
    return dirs


def resolve_image(image_id: str | None = None, image_path: str | None = None) -> Path:
    """Resolve an image file. Raises ValueError/FileNotFoundError. Never trusts input."""
    if image_path and isinstance(image_path, str) and image_path.strip().lower().startswith("studio:"):
        # Studio asset ref (studio:pdf_2/images/hero.png) — validated exactly
        # like the asset route (no traversal, images only). Lets studio turns
        # inspect their own assets through vision_analyze.
        try:
            import sys as _sys0
            if str(_project_root()) not in _sys0.path:
                _sys0.path.insert(0, str(_project_root()))
            from core.studio import store as _sstore
            rest = image_path.strip()[len("studio:"):].strip().replace("\\", "/")
            sid, _, rel = rest.partition("/")
            cand, mime = _sstore.resolve_asset_file(sid, rel)
            if not str(mime).startswith("image/"):
                raise ValueError("Studio asset is not an image")
            return _checked(cand, IMAGE_EXTS, MAX_IMAGE_BYTES, "Image")
        except (FileNotFoundError, ValueError):
            raise
        except Exception as e:
            raise ValueError(f"Invalid studio image ref: {e}") from None
    if image_path:
        if not isinstance(image_path, str) or not image_path.strip():
            raise ValueError("image_path must be a non-empty string")
        p = Path(image_path)
        if not p.exists():
            alt = (_project_root() / image_path).resolve()
            if alt.exists():
                p = alt
            else:
                # Workspace-relative id (the stable file_id the context shows).
                try:
                    import sys as _sys2
                    if str(_project_root()) not in _sys2.path:
                        _sys2.path.insert(0, str(_project_root()))
                    from core.paths import resolve_inside_workspace as _ws_resolve
                    _w = _ws_resolve(image_path)
                    if _w.exists():
                        p = _w
                    else:
                        raise FileNotFoundError(f"Image not found: {image_path}")
                except (FileNotFoundError, ValueError, TypeError):
                    raise FileNotFoundError(f"Image not found: {image_path}") from None
        return _checked(p, IMAGE_EXTS, MAX_IMAGE_BYTES, "Image")
    if not image_id or not isinstance(image_id, str):
        raise ValueError("At least one of image_id or image_path is required")
    if "\\" in image_id or ".." in image_id:
        raise ValueError("Invalid image_id")
    # A Workspace file_id (Workspace-relative path, e.g. "logo.png" or
    # "deck/logo.png") resolves straight inside the Workspace jail.
    if "/" in image_id:
        try:
            import sys as _sys3
            if str(_project_root()) not in _sys3.path:
                _sys3.path.insert(0, str(_project_root()))
            from core.paths import resolve_inside_workspace as _ws_resolve2
            _w2 = _ws_resolve2(image_id)
            if _w2.is_file():
                return _checked(_w2, IMAGE_EXTS, MAX_IMAGE_BYTES, "Image")
        except (ValueError, TypeError, OSError):
            pass
    else:
        # Bare file name: also try the Workspace root before the data dirs.
        try:
            import sys as _sys4
            if str(_project_root()) not in _sys4.path:
                _sys4.path.insert(0, str(_project_root()))
            from core.paths import resolve_inside_workspace as _ws_resolve3
            _w3 = _ws_resolve3(image_id)
            if _w3.is_file():
                return _checked(_w3, IMAGE_EXTS, MAX_IMAGE_BYTES, "Image")
        except (ValueError, TypeError, OSError):
            pass
        for allowed in _image_dirs():
            try:
                cand = (allowed / image_id).resolve()
                try:
                    cand.relative_to(allowed)
                except ValueError:
                    continue
                if cand.exists():
                    return _checked(cand, IMAGE_EXTS, MAX_IMAGE_BYTES, "Image")
            except (ValueError, OSError):
                continue
    raise FileNotFoundError(f"Image id not found: {image_id}")


def _checked(p: Path, exts: tuple, cap: int, label: str) -> Path:
    if p.stat().st_size > cap:
        raise ValueError(f"{label} too large (max {cap // (1024 * 1024)}MB)")
    if p.suffix.lower() not in exts:
        raise ValueError(f"Unsupported {label.lower()} type: {p.suffix}")
    return p


def image_to_base64(p: Path) -> tuple[str, str]:
    """Returns (base64_text, mime)."""
    raw = p.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Image too large (max 20MB)")
    mime = _MIME_BY_EXT.get(p.suffix.lower(), "image/png")
    return base64.b64encode(raw).decode("ascii"), mime


def read_audio(audio_path: str | Path) -> tuple[bytes, str]:
    """Read an audio file (cap 25MB). Returns (bytes, mime)."""
    p = Path(audio_path)
    if not p.exists():
        alt = (_project_root() / str(audio_path)).resolve()
        if alt.exists():
            p = alt
        else:
            raise FileNotFoundError(f"Audio not found: {audio_path}")
    if p.stat().st_size > MAX_AUDIO_BYTES:
        raise ValueError("Audio too large (max 25MB)")
    return p.read_bytes(), _MIME_BY_EXT.get(p.suffix.lower(), "audio/wav")
