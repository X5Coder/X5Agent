"""Console owner auth — single owner of the email/password gate + sessions.

File format matches install.py ask_credentials() exactly, so an owner created
by `python install.py` is honored here immediately (and vice versa):
  Data/database/console_auth.json = {email, salt, hash, updated_at}
  hash = pbkdf2_hmac(sha256, password, salt, 200_000).hex()

Sessions are random tokens (30 days) held in memory and persisted to
Data/database/console_sessions.json (bounded 1000, expired pruned on write),
so a server restart does not log you out — only expiry or clearing that file
does. Cookie: HttpOnly + SameSite=Lax,
no Secure (the console is plain HTTP, so a Secure cookie would never be
stored by the browser; the first-party Lax session is what keeps the login
alive — see server._session_cookie).
Secrets are never logged and never returned (reads expose email only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from pathlib import Path

COOKIE_NAME = "x5sess"
SESSION_DAYS = 30
PBKDF2_ROUNDS = 200_000
# English Gmail addresses only (the console has a single owner).
EMAIL_RE = re.compile(r"^[a-z0-9][a-z0-9._%+-]*@gmail\.com$")
MIN_PASSWORD = 8
_ASCII_PRINTABLE_RE = re.compile(r"^[\x21-\x7e]+$")

_sessions: dict[str, float] = {}
_sessions_lock = threading.Lock()


def email_problem(email: str) -> str | None:
    """Why the email is unacceptable, else None. English Gmail addresses only.

    Single owner for the rule: the installer prompt, set_owner.py, the server
    startup prompt and the setup page all call this instead of keeping their
    own idea of a valid owner email.
    """
    email = (email or "").strip()
    if not email:
        return "email is required"
    if not EMAIL_RE.match(email.lower()):
        return "email must be an English Gmail address (example@gmail.com)"
    return None


def password_problem(password: str) -> str | None:
    """Why the password is unacceptable, else None. English (ASCII) only."""
    if not password:
        return "password is required"
    if len(password) < MIN_PASSWORD:
        return f"password too short (min {MIN_PASSWORD})"
    if not _ASCII_PRINTABLE_RE.match(password):
        return ("password must be English characters only "
                "(letters, numbers, symbols)")
    return None


def owner_path(path: Path | None = None) -> Path:
    """Owner record location. Tests inject tmp paths; runtime uses Data."""
    if path is not None:
        return path
    import sys as _sys

    proj = Path(__file__).resolve().parents[2]
    if str(proj) not in _sys.path:
        _sys.path.insert(0, str(proj))
    from core.paths import data_db_file

    return data_db_file("console_auth.json")


def is_setup(path: Path | None = None) -> bool:
    """True when a valid owner record exists (email + salt + hash)."""
    try:
        record = json.loads(owner_path(path).read_text(encoding="utf-8"))
        return bool(isinstance(record, dict) and record.get("email")
                    and record.get("salt") and record.get("hash"))
    except Exception:
        return False


def _hash(password: str, salt_hex: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"),
        bytes.fromhex(salt_hex), PBKDF2_ROUNDS).hex()


def setup_owner(email: str, password: str, path: Path | None = None) -> str:
    """First-run owner creation. Refuses when an owner already exists."""
    email = (email or "").strip().lower()
    if is_setup(path):
        raise RuntimeError("owner already set")
    problem = email_problem(email) or password_problem(password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_hex(16)
    record = {"email": email, "salt": salt, "hash": _hash(password, salt),
              "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    target = owner_path(path)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return email


def verify_owner(email: str, password: str, path: Path | None = None) -> bool:
    """True only when email matches and the password hash matches."""
    try:
        record = json.loads(owner_path(path).read_text(encoding="utf-8"))
    except Exception:
        return False
    if not isinstance(record, dict):
        return False
    if (record.get("email") or "") != (email or "").strip().lower():
        return False
    try:
        expected = str(record.get("hash") or "")
        actual = _hash(password or "", str(record.get("salt") or ""))
        return bool(expected and hmac.compare_digest(expected, actual))
    except Exception:
        return False


def change_password(email: str, old_password: str, new_password: str,
                    path: Path | None = None) -> None:
    """Password change: verifies the old password first, then re-hashes."""
    if not verify_owner(email, old_password, path):
        raise ValueError("current password is wrong")
    problem = password_problem(new_password)
    if problem:
        raise ValueError(problem)
    try:
        record = json.loads(owner_path(path).read_text(encoding="utf-8"))
    except Exception as e:
        raise ValueError("owner record unreadable") from e
    salt = secrets.token_hex(16)
    record["salt"] = salt
    record["hash"] = _hash(new_password, salt)
    record["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    owner_path(path).write_text(json.dumps(record, ensure_ascii=False, indent=2),
                                encoding="utf-8")


def owner_email(path: Path | None = None) -> str | None:
    """Email only (safe to expose to the UI)."""
    try:
        record = json.loads(owner_path(path).read_text(encoding="utf-8"))
        email = str((record or {}).get("email") or "")
        return email or None
    except Exception:
        return None


def _sessions_path() -> Path | None:
    """Persistent session file (survives server restarts). None on failure."""
    try:
        import sys as _sys
        proj = Path(__file__).resolve().parents[2]
        if str(proj) not in _sys.path:
            _sys.path.insert(0, str(proj))
        from core.paths import data_db_file
        return data_db_file("console_sessions.json")
    except Exception:
        return None


def _load_sessions() -> dict:
    """Read persisted sessions {token: exp}. Never raises."""
    try:
        p = _sessions_path()
        if p is None or not p.exists():
            return {}
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_sessions(data: dict) -> None:
    """Persist sessions (bounded 1000, expired pruned). Never raises."""
    try:
        p = _sessions_path()
        if p is None:
            return
        now = time.time()
        clean = {str(k)[:128]: float(v) for k, v in (data or {}).items()
                 if isinstance(v, (int, float)) and float(v) > now}
        if len(clean) > 1000:
            ordered = sorted(clean.items(), key=lambda kv: kv[1], reverse=True)[:1000]
            clean = dict(ordered)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(clean), encoding="utf-8")
    except Exception:
        pass


def create_session() -> str:
    """Mint a random session token (30 days). Persisted to disk (survives restarts)."""
    token = secrets.token_urlsafe(32)
    exp = time.time() + SESSION_DAYS * 86400
    with _sessions_lock:
        _sessions[token] = exp
        try:
            stored = _load_sessions()
            stored[token] = exp
            _save_sessions(stored)
        except Exception:
            pass
    return token


def session_valid(token: str | None) -> bool:
    """True when the token exists and has not expired (memory + disk, prunes lazily)."""
    if not token:
        return False
    now = time.time()
    with _sessions_lock:
        exp = _sessions.get(token)
        if exp is None:
            # Restarted server: reload from disk once.
            try:
                stored = _load_sessions()
                exp = stored.get(token)
                if exp is not None:
                    _sessions[token] = float(exp)
            except Exception:
                exp = None
        if exp is None:
            return False
        try:
            exp = float(exp)
        except Exception:
            _sessions.pop(token, None)
            return False
        if exp < now:
            _sessions.pop(token, None)
            try:
                stored = _load_sessions()
                stored.pop(token, None)
                _save_sessions(stored)
            except Exception:
                pass
            return False
        if len(_sessions) > 1000:  # bounded: drop expired first
            for tok, when in list(_sessions.items()):
                try:
                    if float(when) < now:
                        _sessions.pop(tok, None)
                except Exception:
                    _sessions.pop(tok, None)
        return True


def drop_session(token: str | None) -> None:
    """Logout: forget one token (memory + disk, never raises)."""
    try:
        if token:
            with _sessions_lock:
                _sessions.pop(token, None)
                try:
                    stored = _load_sessions()
                    stored.pop(token, None)
                    _save_sessions(stored)
                except Exception:
                    pass
    except Exception:
        pass


def session_from_cookie(cookie_header: str | None) -> str | None:
    """Extract the session token from a Cookie header (name x5sess)."""
    try:
        for part in (cookie_header or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name.strip() == COOKIE_NAME and value.strip():
                return value.strip()[:128]
    except Exception:
        pass
    return None
