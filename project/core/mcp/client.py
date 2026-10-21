"""MCP JSON-RPC client over Streamable HTTP (stdlib only).

Only the request/response half of the transport is used: each JSON-RPC call is
a POST, and the answer may be plain JSON or an SSE stream carrying the message.
Every call is bounded (timeout, body size, retries) and redirects are refused
so a Bearer token can never be forwarded to another host.

The client never logs a URL with a token and never puts a token in an error
message; callers receive one safe sentence plus a stable code.
"""
from __future__ import annotations

import json
import re
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

PROTOCOL_VERSION = "2025-06-18"
_MAX_RESPONSE = 2 * 1024 * 1024
_MAX_TOOLS = 300
_MAX_PAGES = 5
_DEFAULT_TIMEOUT = 20.0
_MAX_RETRIES = 2
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_SESSION_RE = re.compile(r"^[A-Za-z0-9._~-]{1,256}$")


class McpError(RuntimeError):
    """Safe transport/protocol failure."""

    def __init__(self, message: str, code: str = "mcp_error") -> None:
        self.code = str(code)
        super().__init__(str(message))


class McpUnreachable(McpError):
    """The endpoint could not be reached (network, DNS, TLS, timeout)."""


class McpAuthRequired(McpError):
    """The endpoint answered 401 and wants a Bearer token."""

    def __init__(self, message: str, resource_metadata: str = "") -> None:
        super().__init__(message, code="auth_required")
        self.resource_metadata = str(resource_metadata or "")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise McpError("The MCP endpoint redirected to another address", "redirected")


def _client_version() -> str:
    try:
        data = json.loads(
            (Path(__file__).resolve().parents[3] / "updates.json").read_text(encoding="utf-8")
        )
        version = data.get("version")
        if isinstance(version, str) and version.strip():
            return version.strip()[:32]
    except Exception:
        pass
    return "0"


def _user_agent() -> str:
    return f"X5Agent/{_client_version()}"


def _resource_metadata(header: str) -> str:
    value = str(header or "")
    if not value:
        return ""
    match = re.search(r'resource_metadata\s*=\s*"([^"]{1,2048})"', value, re.I)
    if not match:
        match = re.search(r"resource_metadata\s*=\s*([^\s,]{1,2048})", value, re.I)
    return match.group(1).strip() if match else ""


def _open(url: str, data: bytes | None, headers: dict[str, str], timeout: float):
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    opener = urllib.request.build_opener(_NoRedirect())
    return opener.open(request, timeout=timeout)


def _read_capped(response: Any, cap: int = _MAX_RESPONSE) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > cap:
            raise McpError("The MCP server response is too large", "response_too_large")
        chunks.append(chunk)
    return b"".join(chunks)


def _sse_messages(text: str) -> list[dict]:
    """Extract JSON-RPC objects out of an SSE body (bounded by the caller)."""
    messages: list[dict] = []
    data_lines: list[str] = []

    def flush() -> None:
        if not data_lines:
            return
        payload = "\n".join(data_lines)
        data_lines.clear()
        try:
            parsed = json.loads(payload)
        except Exception:
            return
        if isinstance(parsed, dict):
            messages.append(parsed)
        elif isinstance(parsed, list):
            messages.extend(item for item in parsed if isinstance(item, dict))

    for line in text.splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data_lines.append(value[1:] if value.startswith(" ") else value)
    flush()
    return messages


def _decode_messages(body: bytes) -> list[dict]:
    text = body.decode("utf-8", "replace")
    stripped = text.lstrip()
    if not stripped:
        return []
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            parsed = json.loads(stripped)
        except Exception:
            return []
        if isinstance(parsed, dict):
            return [parsed]
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        return []
    return _sse_messages(text)


def _match_result(messages: list[dict], request_id: int) -> dict:
    chosen: dict | None = None
    fallback: dict | None = None
    for message in messages:
        if "result" in message or "error" in message:
            fallback = message
            if message.get("id") == request_id:
                chosen = message
                break
    message = chosen or fallback
    if message is None:
        raise McpError("The MCP server sent no answer", "empty_answer")
    error = message.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        text = error.get("message") if isinstance(error.get("message"), str) else "request failed"
        safe = " ".join(str(text).split())[:200]
        raise McpError(f"The MCP server refused the request: {safe}", f"rpc_{code}")
    result = message.get("result")
    if not isinstance(result, dict):
        raise McpError("The MCP server sent an invalid answer", "invalid_answer")
    return result


def _request(
    url: str,
    payload: dict | None,
    token: str,
    session_id: str,
    protocol_version: str,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    """POST one JSON-RPC message (or a notification when payload is None)."""
    headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "User-Agent": _user_agent(),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b"{}"

    last_error: Exception | None = None
    for attempt in range(_MAX_RETRIES + 1):
        try:
            with _open(url, body, headers, timeout) as response:
                return int(getattr(response, "status", 200)), dict(response.headers), _read_capped(response)
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            try:
                raw = _read_capped(exc)
            except Exception:
                raw = b""
            if status == 401:
                raise McpAuthRequired(
                    "This MCP server needs authorization",
                    _resource_metadata(exc.headers.get("WWW-Authenticate", "") if exc.headers else ""),
                ) from None
            if status in _RETRY_STATUSES and attempt < _MAX_RETRIES:
                time.sleep(0.4 * (attempt + 1))
                continue
            raise McpError(f"The MCP server answered HTTP {status}", f"http_{status}") from None
        except (socket.timeout, TimeoutError) as exc:
            last_error = exc
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                last_error = exc
            else:
                last_error = exc
        except McpError:
            raise
        except Exception as exc:
            last_error = exc
        if attempt < _MAX_RETRIES:
            time.sleep(0.4 * (attempt + 1))
    raise McpUnreachable("Could not reach the MCP server") from last_error


def _rpc(
    url: str,
    method: str,
    params: dict | None,
    request_id: int,
    token: str,
    session_id: str,
    protocol_version: str,
    timeout: float,
) -> tuple[dict, str]:
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    _, headers, body = _request(url, payload, token, session_id, protocol_version, timeout)
    answered_session = str(headers.get("Mcp-Session-Id") or headers.get("mcp-session-id") or "")
    if answered_session and not _SESSION_RE.match(answered_session):
        answered_session = ""
    if not body.strip():
        return {}, answered_session
    return _match_result(_decode_messages(body), request_id), answered_session


def _notify(url: str, method: str, token: str, session_id: str, protocol_version: str, timeout: float) -> None:
    payload = {"jsonrpc": "2.0", "method": method}
    try:
        _request(url, payload, token, session_id, protocol_version, timeout)
    except McpError:
        # Notifications do not change the probe result; the request that
        # follows is the real verification.
        pass


def probe(url: str, token: str = "", timeout: float = _DEFAULT_TIMEOUT) -> dict:
    """Initialize the session and return the server info plus its tools."""
    result, session_id = _rpc(
        url, "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "X5Agent", "version": _client_version()},
        },
        1, token, "", "", timeout,
    )
    version = result.get("protocolVersion")
    if not isinstance(version, str) or not version.strip():
        raise McpError("The MCP server did not report a protocol version", "bad_initialize")
    version = version.strip()[:32]
    server_info = result.get("serverInfo") if isinstance(result.get("serverInfo"), dict) else {}
    server_name = " ".join(str(server_info.get("name") or "").split())[:60]
    server_version = " ".join(str(server_info.get("version") or "").split())[:40]

    _notify(url, "notifications/initialized", token, session_id, version, timeout)

    tools: list[dict] = []
    cursor = ""
    for page in range(_MAX_PAGES):
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        listed, answered_session = _rpc(
            url, "tools/list", params or None, 2 + page, token, session_id, version, timeout,
        )
        if not session_id and answered_session:
            session_id = answered_session
        for item in listed.get("tools") or []:
            if not isinstance(item, dict):
                continue
            name = " ".join(str(item.get("name") or "").split())[:128]
            if not name:
                continue
            tools.append({
                "name": name,
                "title": " ".join(str(item.get("title") or "").split())[:120],
                "description": " ".join(str(item.get("description") or "").split())[:400],
                "input_schema": item.get("inputSchema") if isinstance(item.get("inputSchema"), dict) else {},
            })
            if len(tools) >= _MAX_TOOLS:
                break
        next_cursor = listed.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor.strip() or len(tools) >= _MAX_TOOLS:
            break
        cursor = next_cursor.strip()[:512]

    return {
        "protocol_version": version,
        "server_name": server_name,
        "server_version": server_version,
        "session_id": session_id,
        "tools": tools,
    }


_MAX_CALL_CHARS = 20000
_MAX_STRUCTURED_CHARS = 20000


def _call_content(result: dict) -> tuple[str, bool]:
    """Flatten an MCP tool result into bounded text (never raw base64 blobs)."""
    is_error = bool(result.get("isError"))
    parts: list[str] = []
    total = 0
    content = result.get("content")
    if isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "")
            if kind == "text":
                text = str(item.get("text") or "")
                parts.append(text)
                total += len(text)
            elif kind in ("image", "audio", "resource", "resource_link"):
                parts.append(f"[{kind} content omitted]")
            if total >= _MAX_CALL_CHARS:
                break
    text = "\n".join(parts)
    if len(text) > _MAX_CALL_CHARS:
        text = text[:_MAX_CALL_CHARS] + "\n… truncated"
    return text, is_error


def call_tool(
    url: str,
    token: str,
    tool_name: str,
    arguments: dict,
    timeout: float = _DEFAULT_TIMEOUT,
) -> dict:
    """Run one tool on the server: initialize -> tools/call -> close session.

    Returns ``{text, is_error, structured?}``. The session is closed in a
    ``finally`` block so a failed call never leaves a dangling server session.
    """
    name = str(tool_name or "").strip()
    if not name or len(name) > 128:
        raise McpError("The MCP tool name is not valid", "bad_tool_name")
    if not isinstance(arguments, dict):
        raise McpError("The MCP tool arguments must be a JSON object", "bad_arguments")

    result, session_id = _rpc(
        url, "initialize",
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "X5Agent", "version": _client_version()},
        },
        1, token, "", "", timeout,
    )
    version = result.get("protocolVersion")
    if not isinstance(version, str) or not version.strip():
        raise McpError("The MCP server did not report a protocol version", "bad_initialize")
    version = version.strip()[:32]
    _notify(url, "notifications/initialized", token, session_id, version, timeout)
    try:
        answer, _ = _rpc(
            url, "tools/call",
            {"name": name, "arguments": arguments},
            2, token, session_id, version, timeout,
        )
    finally:
        close_session(url, token, session_id, version)

    text, is_error = _call_content(answer)
    out: dict[str, Any] = {"text": text, "is_error": is_error}
    structured = answer.get("structuredContent")
    if isinstance(structured, (dict, list)):
        try:
            encoded = json.dumps(structured, ensure_ascii=False)
            out["structured"] = (
                structured if len(encoded) <= _MAX_STRUCTURED_CHARS
                else {"truncated": True, "preview": encoded[:_MAX_STRUCTURED_CHARS]}
            )
        except Exception:
            pass
    return out


def close_session(url: str, token: str, session_id: str, protocol_version: str = "", timeout: float = 5.0) -> None:
    """Best-effort session termination (never raises)."""
    if not session_id:
        return
    headers = {"User-Agent": _user_agent(), "Mcp-Session-Id": session_id}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if protocol_version:
        headers["MCP-Protocol-Version"] = protocol_version
    try:
        request = urllib.request.Request(url, headers=headers, method="DELETE")
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=timeout) as response:
            _read_capped(response, 65536)
    except Exception:
        pass
