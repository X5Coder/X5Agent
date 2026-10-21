"""Persistent license session state (X5Agent/Data/database/license.json).

Data integrity rules:
- bounded file size, tolerant read (a corrupt file never crashes the app)
- atomic write (temp file + os.replace)
- owner-only permissions on POSIX
- the account password is NEVER stored; the refresh token is, because it is
  the only way to renew a 15-minute entitlement without the password.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from . import config

_LOG = logging.getLogger("x5agent.license")

SCHEMA = 1
_MAX_FILE_BYTES = 64 * 1024
_MAX_NOTICES = 20
_MAX_REFRESH_TOKEN = 4096
_MAX_ENTITLEMENT_TOKEN = 8192

_STATES = frozenset(
    {
        # server subscription states
        "trial",
        "active",
        "warning",
        "suspended",
        "expired",
        "none",
        # local client states
        "signed_out",
        "device_superseded",
        "auth_required",
        "account_disabled",
        "invalid_entitlement",
        "network",
        "server_error",
    }
)
_STR_LIMITS: dict[str, int] = {
    "server_url": 2048,
    "email": 320,
    "account_id": 64,
    "device_id": 64,
    "refresh_token": _MAX_REFRESH_TOKEN,
    "entitlement_token": _MAX_ENTITLEMENT_TOKEN,
    "state": 32,
    "plan": 64,
    "starts_at": 64,
    "expires_at": 64,
    "subscription_id": 64,
    "last_error": 400,
}
_DEFAULT_STATE: dict[str, Any] = {
    "schema": SCHEMA,
    "server_url": "",
    "email": "",
    "account_id": "",
    "device_id": "",
    "refresh_token": "",
    "entitlement_token": "",
    "state": "signed_out",
    "plan": "",
    "starts_at": "",
    "expires_at": "",
    "subscription_id": "",
    "remaining_seconds": 0,
    "can_work": False,
    "plan_chosen": False,
    "trial_used": False,
    "last_heartbeat_at": 0.0,
    "last_error": "",
    "notices": [],
    "server_time_offset": 0.0,
    "updated_at": 0.0,
}


def _clean_str(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _clean_int(value: Any, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return low
    return max(low, min(high, number))


def _clean_float(value: Any, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:  # NaN
        return low
    return max(low, min(high, number))


def clean_notices(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []
    clean: list[dict[str, str]] = []
    for item in value[:_MAX_NOTICES]:
        if not isinstance(item, dict):
            continue
        kind = _clean_str(item.get("kind"), 32)
        if not kind:
            continue
        clean.append(
            {
                "kind": kind,
                "created_at": _clean_str(item.get("created_at"), 64),
            }
        )
    return clean


def normalize_state(raw: dict[str, Any] | None) -> dict[str, Any]:
    """Return a fully typed state dict; unknown fields are dropped."""
    source = raw if isinstance(raw, dict) else {}
    state = dict(_DEFAULT_STATE)
    for key, limit in _STR_LIMITS.items():
        state[key] = _clean_str(source.get(key), limit)
    if state["state"] not in _STATES:
        state["state"] = "signed_out"
    state["remaining_seconds"] = _clean_int(
        source.get("remaining_seconds"), 0, 315_360_000
    )
    state["can_work"] = bool(source.get("can_work"))
    state["plan_chosen"] = bool(source.get("plan_chosen"))
    state["trial_used"] = bool(source.get("trial_used"))
    state["last_heartbeat_at"] = _clean_float(
        source.get("last_heartbeat_at"), 0.0, time.time() + 86_400
    )
    state["server_time_offset"] = _clean_float(
        source.get("server_time_offset"), -86_400.0, 86_400.0
    )
    state["updated_at"] = _clean_float(
        source.get("updated_at"), 0.0, time.time() + 86_400
    )
    state["notices"] = clean_notices(source.get("notices"))
    state["schema"] = SCHEMA
    return state


def default_state() -> dict[str, Any]:
    return normalize_state({})


def load_state(path: Path | None = None) -> dict[str, Any]:
    target = Path(path) if path is not None else config.state_path()
    try:
        if not target.is_file():
            return default_state()
        if target.stat().st_size > _MAX_FILE_BYTES:
            _LOG.warning("license state file is too large; starting signed out")
            return default_state()
        with target.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _LOG.warning("license state unreadable (%s); starting signed out", type(exc).__name__)
        return default_state()
    if not isinstance(raw, dict):
        _LOG.warning("license state has an invalid shape; starting signed out")
        return default_state()
    return normalize_state(raw)


def save_state(state: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Atomically persist the state and return the normalized copy.

    Raises OSError on write failure so callers can surface it; never logs the
    token values.
    """
    target = Path(path) if path is not None else config.state_path()
    clean = normalize_state(state)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(clean, ensure_ascii=False, indent=2).encode("utf-8")
    temp = target.with_name(target.name + ".tmp")
    try:
        with temp.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    try:
        os.chmod(target, 0o600)
    except (OSError, NotImplementedError):
        # Windows has no POSIX mode bits; the file lives in the user's Data
        # folder and is never served over HTTP.
        pass
    return clean


def clear_session(state: dict[str, Any], *, next_state: str = "signed_out") -> dict[str, Any]:
    """Drop every server credential but keep the email for convenience."""
    clean = normalize_state(state)
    clean.update(
        {
            "account_id": "",
            "device_id": "",
            "refresh_token": "",
            "entitlement_token": "",
            "plan": "",
            "starts_at": "",
    "expires_at": "",
            "subscription_id": "",
            "remaining_seconds": 0,
            "can_work": False,
            "plan_chosen": False,
            "trial_used": False,
            "last_error": "",
            "notices": [],
        }
    )
    clean["state"] = next_state if next_state in _STATES else "signed_out"
    return clean
