"""MCP server records — Data/database/mcp_servers.json is authoritative.

Owns: the list of registered MCP servers, their last connection status, the
tool catalogue they reported, and the OAuth material needed to reconnect.
OAuth tokens are secrets: they live in this file only (Data is gitignored,
file mode 600 where the OS supports it) and are never returned to the browser.

Every read is validated and every write is atomic (temp file + os.replace).
Never raises from reads; writes raise ``McpStoreError`` with a safe message.
"""
from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.paths import data_db_file  # noqa: E402

_FILENAME = "mcp_servers.json"
_FILE_VERSION = 1
_MAX_FILE_BYTES = 8 * 1024 * 1024

MAX_SERVERS = 20
MAX_NAME = 60
MAX_URL = 2048
MAX_ERROR = 300
MAX_TOOLS = 300
MAX_TOOL_NAME = 128
MAX_TOOL_TITLE = 120
MAX_TOOL_DESC = 400
MAX_TOOL_SCHEMA = 6000
MAX_TOKEN = 8192
MAX_CLIENT_SECRET = 512
MAX_SCOPE = 1000

STATUSES = frozenset({"connected", "auth_required", "error", "disconnected"})

_BLOCKED_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})


class McpStoreError(RuntimeError):
    """A safe, user-facing store failure (never contains secrets)."""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = " ".join(value.split())
    return clean[:limit]


def _raw_text(value: object, limit: int) -> str:
    """Token/URL text: no whitespace folding, still bounded."""
    if not isinstance(value, str):
        return ""
    return value.strip()[:limit]


def _is_local_host(host: str) -> bool:
    """Loopback, LAN (RFC1918/ULA), link-local, CGNAT and *.local names."""
    name = (host or "").strip().lower().strip("[]")
    if not name:
        return False
    if name == "localhost" or name.endswith(".localhost") or name.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return bool(address.is_loopback or address.is_private or address.is_link_local)


def validate_server_url(raw: object) -> str:
    """Validate and normalize an MCP endpoint URL.

    HTTPS everywhere; HTTP is accepted on this machine and on a local network
    (localhost, 127.0.0.1, 192.168.x.x, 10.x.x.x, *.local), which is where a
    self-hosted MCP server usually runs. Credentials, fragments and cloud
    metadata hosts are refused.
    """
    value = _raw_text(raw, MAX_URL)
    if not value:
        raise ValueError("Server URL is required")
    if len(str(raw).strip()) > MAX_URL:
        raise ValueError("Server URL is too long")
    try:
        parts = urlsplit(value)
    except ValueError:
        raise ValueError("Server URL is not valid") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise ValueError("Server URL must start with http:// or https://")
    if parts.username or parts.password:
        raise ValueError("Server URL must not contain credentials")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise ValueError("Server URL has no host")
    if host in _BLOCKED_HOSTS:
        raise ValueError("This host is not allowed")
    try:
        port = parts.port
    except ValueError:
        raise ValueError("Server URL has an invalid port") from None
    if parts.scheme.lower() == "http" and not _is_local_host(host):
        raise ValueError(
            "Use HTTPS for remote servers (HTTP is allowed on localhost and local networks)"
        )
    if not parts.path:
        raise ValueError("Server URL must include the MCP endpoint path")
    netloc = host
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]"
    if port:
        netloc = f"{netloc}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, parts.query, ""))


def _schema(value: object, limit: int = MAX_TOOL_SCHEMA) -> dict:
    """Keep a bounded JSON Schema object; anything else is dropped safely."""
    if not isinstance(value, dict):
        return {}
    try:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        return {}
    if len(text) > limit:
        return {}
    return value


def normalize_tools(raw: object) -> list[dict]:
    """Bound and validate the tool catalogue reported by a server."""
    out: list[dict] = []
    if not isinstance(raw, list):
        return out
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("name"), MAX_TOOL_NAME)
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({
            "name": name,
            "title": _text(item.get("title"), MAX_TOOL_TITLE),
            "description": _text(item.get("description"), MAX_TOOL_DESC),
            "input_schema": _schema(item.get("input_schema") or item.get("inputSchema")),
        })
        if len(out) >= MAX_TOOLS:
            break
    return out


def _normalize_oauth(raw: object) -> dict:
    data = raw if isinstance(raw, dict) else {}
    out: dict[str, str] = {}
    for key, limit in (
        ("issuer", MAX_URL),
        ("authorization_server", MAX_URL),
        ("token_endpoint", MAX_URL),
        ("client_id", 512),
        ("client_secret", MAX_CLIENT_SECRET),
        ("access_token", MAX_TOKEN),
        ("refresh_token", MAX_TOKEN),
        ("token_type", 40),
        ("scope", MAX_SCOPE),
        ("redirect_uri", MAX_URL),
        ("resource", MAX_URL),
        ("client_auth", 32),
    ):
        value = _raw_text(data.get(key), limit)
        if value:
            out[key] = value
    expires = data.get("expires_at")
    if isinstance(expires, str) and expires.strip().isdigit():
        out["expires_at"] = expires.strip()[:20]
    elif isinstance(expires, (int, float)) and expires > 0:
        out["expires_at"] = str(int(expires))
    return out


def normalize_record(raw: object) -> dict | None:
    """Tolerant normalizer for a record read back from disk."""
    if not isinstance(raw, dict):
        return None
    server_id = _text(raw.get("id"), 64)
    if not re.fullmatch(r"mcp_[0-9a-f]{8,32}", server_id or ""):
        return None
    name = _text(raw.get("name"), MAX_NAME)
    try:
        url = validate_server_url(raw.get("url"))
    except ValueError:
        return None
    if not name:
        return None
    status = str(raw.get("status") or "disconnected").strip().lower()
    if status not in STATUSES:
        status = "disconnected"
    created = _raw_text(raw.get("created_at"), 40) or now_iso()
    return {
        "id": server_id,
        "name": name,
        "url": url,
        "status": status,
        "error": _text(raw.get("error"), MAX_ERROR),
        "tools": normalize_tools(raw.get("tools")),
        "protocol_version": _text(raw.get("protocol_version"), 32),
        "server_name": _text(raw.get("server_name"), MAX_NAME),
        "server_version": _text(raw.get("server_version"), 40),
        "oauth": _normalize_oauth(raw.get("oauth")),
        "created_at": created,
        "updated_at": _raw_text(raw.get("updated_at"), 40) or created,
        "connected_at": _raw_text(raw.get("connected_at"), 40),
    }


def public_record(record: dict, full: bool = False) -> dict:
    """Browser-safe view: no tokens, no client secret, no endpoints.

    ``full`` also carries each tool's input JSON Schema (used by the agent
    tool, not by the dashboard list, so the page payload stays small).
    """
    if not isinstance(record, dict):
        return {}
    oauth = record.get("oauth") if isinstance(record.get("oauth"), dict) else {}
    tools = normalize_tools(record.get("tools"))
    if not full:
        tools = [
            {"name": tool["name"], "title": tool["title"], "description": tool["description"]}
            for tool in tools
        ]
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "url": str(record.get("url") or ""),
        "status": str(record.get("status") or "disconnected"),
        "error": str(record.get("error") or ""),
        "tools": tools,
        "tool_count": len(tools),
        "protocol_version": str(record.get("protocol_version") or ""),
        "server_name": str(record.get("server_name") or ""),
        "server_version": str(record.get("server_version") or ""),
        "has_oauth": bool(oauth.get("access_token") or oauth.get("refresh_token")),
        "can_reconnect": bool(oauth.get("refresh_token") or oauth.get("access_token")),
        "created_at": str(record.get("created_at") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "connected_at": str(record.get("connected_at") or ""),
    }


def _path() -> Path:
    return data_db_file(_FILENAME)


def load_records() -> list[dict]:
    """Read every valid record; a corrupt or oversized file reads as empty."""
    path = _path()
    try:
        if not path.is_file() or path.stat().st_size > _MAX_FILE_BYTES:
            return []
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    raw_servers = data.get("servers") if isinstance(data, dict) else None
    if not isinstance(raw_servers, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw_servers:
        record = normalize_record(item)
        if record is None or record["id"] in seen:
            continue
        seen.add(record["id"])
        out.append(record)
        if len(out) >= MAX_SERVERS:
            break
    return out


def save_records(records: list[dict]) -> None:
    """Atomic write; bounded to MAX_SERVERS; 600 where the OS supports it."""
    clean: list[dict] = []
    for item in (records or [])[:MAX_SERVERS]:
        record = normalize_record(item)
        if record is not None:
            clean.append(record)
    path = _path()
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"version": _FILE_VERSION, "servers": clean}, handle,
                      ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        raise McpStoreError("Could not save MCP servers") from exc
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def new_record(name: object, url: object) -> dict:
    clean_name = _text(name, MAX_NAME)
    if len(clean_name) < 1:
        raise ValueError("Server name is required")
    try:
        clean_url = validate_server_url(url)
    except ValueError:
        raise
    stamp = now_iso()
    return {
        "id": f"mcp_{secrets.token_hex(6)}",
        "name": clean_name,
        "url": clean_url,
        "status": "disconnected",
        "error": "",
        "tools": [],
        "protocol_version": "",
        "server_name": "",
        "server_version": "",
        "oauth": {},
        "created_at": stamp,
        "updated_at": stamp,
        "connected_at": "",
    }


def get_record(server_id: str) -> dict | None:
    target = str(server_id or "").strip()
    for record in load_records():
        if record["id"] == target:
            return record
    return None


def upsert_record(record: dict) -> dict:
    clean = normalize_record(record)
    if clean is None:
        raise McpStoreError("Invalid MCP server record")
    records = [r for r in load_records() if r["id"] != clean["id"]]
    records.insert(0, clean)
    save_records(records)
    return clean


def delete_record(server_id: str) -> bool:
    target = str(server_id or "").strip()
    records = load_records()
    kept = [r for r in records if r["id"] != target]
    if len(kept) == len(records):
        return False
    save_records(kept)
    return True
