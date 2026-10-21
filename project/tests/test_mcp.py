"""MCP client tests: store, URL safety, transport, OAuth, service and the
mcp_servers agent tool. Everything runs against a local fake MCP server +
authorization server on 127.0.0.1 (no real network, no keys)."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from core.mcp import client as mcp_client
from core.mcp import oauth as mcp_oauth
from core.mcp import service as mcp_service_module
from core.mcp import store as mcp_store

mcp_service = mcp_service_module.mcp_service

REDIRECT_LOCAL = "http://localhost:7777/api/mcp/oauth/callback"


class _State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.port = 0
        self.require_token = False
        self.authorization: list[str] = []
        self.deleted_sessions: list[str] = []
        self.registered: dict = {}
        self.token_form: dict = {}
        self.calls = 0

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"


def _handler(state: _State):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # silence test noise
            return

        def _send(self, status: int, payload=None, headers=None, raw=None, ctype="application/json"):
            body = raw if raw is not None else (
                json.dumps(payload).encode("utf-8") if payload is not None else b""
            )
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _read_body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = 0
            return self.rfile.read(min(length, 2 * 1024 * 1024)) if length else b""

        def do_DELETE(self) -> None:
            state.deleted_sessions.append(str(self.headers.get("Mcp-Session-Id") or ""))
            self._send(202)

        def do_GET(self) -> None:
            path = urlsplit(self.path).path
            base = state.base
            if path.startswith("/.well-known/oauth-protected-resource"):
                self._send(200, {
                    "resource": base + "/mcp",
                    "authorization_servers": [base],
                    "scopes_supported": ["mcp.read"],
                })
                return
            if path == "/.well-known/oauth-authorization-server":
                self._send(200, {
                    "issuer": base,
                    "authorization_endpoint": base + "/authorize",
                    "token_endpoint": base + "/token",
                    "registration_endpoint": base + "/register",
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "scopes_supported": ["mcp.read"],
                })
                return
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            raw = self._read_body()
            base = state.base

            if path == "/register":
                try:
                    data = json.loads(raw.decode("utf-8") or "{}")
                except Exception:
                    data = {}
                state.registered = data if isinstance(data, dict) else {}
                self._send(201, {"client_id": "client-1", "token_endpoint_auth_method": "none"})
                return

            if path == "/token":
                form = {key: values[0] for key, values in parse_qs(raw.decode("utf-8")).items()}
                state.token_form = form
                if form.get("grant_type") == "refresh_token":
                    self._send(200, {
                        "access_token": "good", "token_type": "Bearer", "expires_in": 3600,
                    })
                    return
                if form.get("code") != "code-ok":
                    self._send(400, {"error": "invalid_grant"})
                    return
                self._send(200, {
                    "access_token": "good",
                    "refresh_token": "refresh-1",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                    "scope": "mcp.read",
                })
                return

            if state.require_token:
                auth = str(self.headers.get("Authorization") or "")
                with state.lock:
                    state.authorization.append(auth)
                if auth != "Bearer good":
                    self._send(
                        401,
                        {"error": "unauthorized"},
                        headers={
                            "WWW-Authenticate": (
                                f'Bearer resource_metadata="{base}'
                                '/.well-known/oauth-protected-resource/mcp"'
                            )
                        },
                    )
                    return

            try:
                message = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                message = {}
            method = str(message.get("method") or "")
            request_id = message.get("id")

            if method == "initialize":
                self._send(
                    200,
                    {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {
                            "protocolVersion": "2025-06-18",
                            "serverInfo": {"name": "Fake MCP", "version": "1.2"},
                        },
                    },
                    headers={"Mcp-Session-Id": "sess-1"},
                )
                return
            if method == "notifications/initialized":
                self._send(202)
                return
            if method == "tools/list":
                cursor = (message.get("params") or {}).get("cursor")
                if not cursor:
                    payload = {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {
                            "tools": [{
                                "name": "echo",
                                "description": "Echo the text back",
                                "inputSchema": {
                                    "type": "object",
                                    "properties": {"text": {"type": "string"}},
                                    "required": ["text"],
                                },
                            }],
                            "nextCursor": "p2",
                        },
                    }
                    # First page as SSE: both decoders must work.
                    body = ("event: message\ndata: " + json.dumps(payload) + "\n\n").encode()
                    self._send(200, raw=body, ctype="text/event-stream")
                    return
                self._send(200, {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {"tools": [
                        {
                            "name": "add",
                            "description": "Add two numbers",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                            },
                        },
                        {"name": "fail", "description": "Always fails"},
                    ]},
                })
                return
            if method == "tools/call":
                params = message.get("params") or {}
                name = str(params.get("name") or "")
                arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
                with state.lock:
                    state.calls += 1
                if name == "fail":
                    self._send(200, {
                        "jsonrpc": "2.0", "id": request_id,
                        "result": {"content": [{"type": "text", "text": "boom"}], "isError": True},
                    })
                    return
                self._send(200, {
                    "jsonrpc": "2.0", "id": request_id,
                    "result": {
                        "content": [{
                            "type": "text",
                            "text": json.dumps({"tool": name, "arguments": arguments}),
                        }],
                        "structuredContent": {"tool": name, "ok": True},
                    },
                })
                return

            self._send(200, {
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": "unknown method"},
            })

    return Handler


@pytest.fixture
def fake():
    state = _State()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    state.port = int(server.server_address[1])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, state.base
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "mcp_servers.json"
    monkeypatch.setattr(mcp_store, "_path", lambda: path)
    mcp_service._pending.clear()
    mcp_service._flow_locks.clear()
    return path


# --------------------------------------------------------------------- URLs
def test_validate_server_url_allows_localhost_and_lan():
    for value in (
        "http://localhost:3000/mcp",
        "http://127.0.0.1:3000/mcp",
        "http://192.168.1.5:8080/mcp",
        "http://10.0.0.4/mcp",
        "http://[::1]:3000/mcp",
        "https://mcp.example.com/mcp",
        "https://192.168.1.5/mcp",
    ):
        assert mcp_store.validate_server_url(value)
    assert mcp_store.validate_server_url("http://LOCALHOST:3000/mcp") == "http://localhost:3000/mcp"
    assert mcp_store.validate_server_url("https://mcp.example.com/mcp#frag") == "https://mcp.example.com/mcp"


def test_validate_server_url_refuses_public_http_and_metadata():
    for value in (
        "http://example.com/mcp",
        "ftp://localhost/mcp",
        "http://user:pass@localhost/mcp",
        "http://169.254.169.254/mcp",
        "http://metadata.google.internal/mcp",
        "http://localhost:3000",
    ):
        with pytest.raises(ValueError):
            mcp_store.validate_server_url(value)


def test_redirect_uri_accepts_localhost_and_lan_console_addresses():
    for value in (
        "http://localhost:7777/api/mcp/oauth/callback",
        "http://127.0.0.1:7777/api/mcp/oauth/callback",
        "http://192.168.1.5:7777/api/mcp/oauth/callback",
        "http://[::1]:7777/api/mcp/oauth/callback",
        "https://dex.example.com/api/mcp/oauth/callback",
    ):
        assert mcp_oauth.validate_redirect_uri(value).endswith("/api/mcp/oauth/callback")
    assert mcp_oauth.validate_redirect_uri(
        "http://192.168.1.5:7777/api/mcp/oauth/callback/"
    ) == "http://192.168.1.5:7777/api/mcp/oauth/callback"


def test_redirect_uri_refuses_public_http_and_wrong_path():
    for value in (
        "http://8.8.8.8:7777/api/mcp/oauth/callback",
        "http://localhost:7777/other/callback",
        "http://localhost:7777/api/mcp/oauth/callback?next=evil",
        "ftp://localhost:7777/api/mcp/oauth/callback",
        "http://user:pass@localhost:7777/api/mcp/oauth/callback",
    ):
        with pytest.raises(mcp_oauth.OAuthError):
            mcp_oauth.validate_redirect_uri(value)


# -------------------------------------------------------------------- store
def test_store_roundtrip_keeps_schema_and_hides_tokens(store):
    record = mcp_store.new_record("Local", "http://localhost:3000/mcp")
    record["status"] = "connected"
    record["oauth"] = {"access_token": "secret-token", "refresh_token": "secret-refresh"}
    record["tools"] = [{
        "name": "echo",
        "description": "Echo",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}},
    }]
    mcp_store.save_records([record])

    loaded = mcp_store.load_records()
    assert len(loaded) == 1
    assert loaded[0]["tools"][0]["input_schema"]["properties"]["text"]["type"] == "string"

    public = mcp_store.public_record(loaded[0])
    assert "oauth" not in public and "access_token" not in json.dumps(public)
    assert "input_schema" not in public["tools"][0]
    full = mcp_store.public_record(loaded[0], full=True)
    assert full["tools"][0]["input_schema"]["type"] == "object"


def test_store_rejects_bad_records_and_duplicates(store):
    record = mcp_store.new_record("A", "http://localhost:3000/mcp")
    bad = dict(record, id="not-an-id")
    assert mcp_store.normalize_record(bad) is None
    assert mcp_store.normalize_record(dict(record, name="")) is None
    assert mcp_store.normalize_record(dict(record, url="http://example.com/mcp")) is None
    # An oversized tool schema is dropped, the tool row stays usable.
    record["tools"] = [{"name": "big", "input_schema": {"x": "y" * (mcp_store.MAX_TOOL_SCHEMA + 10)}}]
    cleaned = mcp_store.normalize_record(record)
    assert cleaned["tools"][0]["input_schema"] == {}


# ------------------------------------------------------------------- client
def test_probe_collects_tools_schemas_and_paginates(fake):
    state, base = fake
    probe = mcp_client.probe(base + "/mcp")
    assert probe["protocol_version"] == "2025-06-18"
    assert probe["server_name"] == "Fake MCP"
    names = [tool["name"] for tool in probe["tools"]]
    assert names == ["echo", "add", "fail"]
    assert probe["tools"][0]["input_schema"]["required"] == ["text"]
    assert probe["session_id"] == "sess-1"


def test_probe_401_exposes_resource_metadata(fake):
    state, base = fake
    state.require_token = True
    with pytest.raises(mcp_client.McpAuthRequired) as caught:
        mcp_client.probe(base + "/mcp")
    assert caught.value.resource_metadata.endswith("/.well-known/oauth-protected-resource/mcp")
    assert "secret" not in str(caught.value)


def test_call_tool_returns_text_and_closes_session(fake):
    state, base = fake
    result = mcp_client.call_tool(base + "/mcp", "", "echo", {"text": "hi"})
    assert result["is_error"] is False
    assert json.loads(result["text"])["arguments"] == {"text": "hi"}
    assert result["structured"] == {"tool": "echo", "ok": True}
    assert state.deleted_sessions == ["sess-1"]
    failure = mcp_client.call_tool(base + "/mcp", "", "fail", {})
    assert failure["is_error"] is True and "boom" in failure["text"]


def test_oauth_discovery_registration_and_exchange(fake):
    state, base = fake
    found = mcp_oauth.discover(base + "/mcp")
    assert found["issuer"] == base
    assert found["scopes"] == ["mcp.read"]
    client = mcp_oauth.register_client(found["metadata"], REDIRECT_LOCAL, found["scopes"])
    assert state.registered["redirect_uris"] == [REDIRECT_LOCAL]
    verifier = mcp_oauth.new_verifier()
    state_name = mcp_oauth.new_state()
    url = mcp_oauth.authorization_url(
        found["metadata"], client, state_name, verifier, found["resource"], found["scopes"]
    )
    query = parse_qs(urlsplit(url).query)
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [mcp_oauth.challenge(verifier)]
    assert query["resource"] == [base + "/mcp"]
    tokens = mcp_oauth.exchange_code(found["metadata"], client, "code-ok", verifier, found["resource"])
    assert tokens["access_token"] == "good"
    assert tokens["expires_at"] > 0


# ------------------------------------------------------------------ service
def test_service_add_local_server_connects_and_calls(fake, store):
    _state, base = fake
    result = mcp_service.add_server("Local", base + "/mcp", REDIRECT_LOCAL)
    assert result["auth_url"] == ""
    assert result["server"]["status"] == "connected"
    assert result["server"]["tool_count"] == 3
    assert "input_schema" not in result["server"]["tools"][0]

    info = mcp_service.describe_tools("local")
    assert info["tools"][0]["input_schema"]["type"] == "object"

    out = mcp_service.call_tool("Local", "echo", {"text": "hi"})
    assert out["is_error"] is False
    assert json.loads(out["text"])["arguments"] == {"text": "hi"}

    with pytest.raises(mcp_service_module.McpServiceError):
        mcp_service.call_tool("Local", "missing", {})


def test_service_full_oauth_flow(fake, store):
    state, base = fake
    state.require_token = True
    result = mcp_service.add_server("Remote", base + "/mcp", REDIRECT_LOCAL)
    assert result["server"]["status"] == "auth_required"
    assert result["auth_url"].startswith(base + "/authorize?")
    assert state.registered["redirect_uris"] == [REDIRECT_LOCAL]

    query = parse_qs(urlsplit(result["auth_url"]).query)
    done = mcp_service.complete_oauth(query["state"][0], "code-ok")
    assert done["ok"] is True
    verifier = state.token_form["code_verifier"]
    assert query["code_challenge"] == [mcp_oauth.challenge(verifier)]
    assert mcp_service.describe_servers()[0]["status"] == "connected"

    out = mcp_service.call_tool("Remote", "echo", {"text": "authorized"})
    assert out["is_error"] is False
    assert "authorized" in out["text"]

    # Disconnect really forgets the authorization: connecting again starts a
    # fresh OAuth flow (and the new state can finish it).
    server_id = mcp_service.describe_servers()[0]["id"]
    mcp_service.disconnect(server_id)
    again = mcp_service.connect(server_id, REDIRECT_LOCAL)
    assert again["auth_url"]
    assert again["server"]["status"] == "auth_required"
    second = parse_qs(urlsplit(again["auth_url"]).query)
    assert mcp_service.complete_oauth(second["state"][0], "code-ok")["ok"] is True
    assert mcp_service.describe_servers()[0]["status"] == "connected"


def test_service_refuses_lan_redirect_and_helper_names(fake, store):
    state, base = fake
    state.require_token = True
    # A wrong path is refused without ever reaching the network.
    with pytest.raises(mcp_service_module.McpServiceError):
        mcp_service.add_server("Bad", base + "/mcp", "http://192.168.1.5:7777/wrong")
    mcp_service.add_server("Local", base + "/mcp", REDIRECT_LOCAL)
    with pytest.raises(mcp_service_module.McpServiceError):
        mcp_service.describe_tools("")
    with pytest.raises(mcp_service_module.McpServiceError):
        mcp_service.call_tool("Local", "echo", "not-a-dict")


# ------------------------------------------------------------------- plugin
def test_plugin_list_tools_call(fake, store):
    from plugins.core_plugins.mcp_servers import tool as plugin

    state, base = fake
    mcp_service.add_server("Local", base + "/mcp", REDIRECT_LOCAL)

    listing = plugin.execute(action="list")
    assert listing["servers"][0]["name"] == "Local"
    assert listing["connected"] == 1

    info = plugin.execute(action="tools", server="local")
    assert info["tool_count"] == 3
    assert info["tools"][0]["input_schema"]["type"] == "object"

    call = plugin.execute(action="call", server="Local", tool="echo", arguments='{"text":"hey"}')
    assert call["is_error"] is False and "hey" in call["content"]
    assert call["structured"]["ok"] is True

    failure = plugin.execute(action="call", server="Local", tool="fail", arguments="{}")
    assert failure["is_error"] is True and failure["content"] == "boom"
    assert state.calls == 2


def test_plugin_validation(fake, store):
    from plugins.core_plugins.mcp_servers import tool as plugin

    _state, base = fake
    with pytest.raises(ValueError):
        plugin.execute()
    with pytest.raises(ValueError):
        plugin.execute(action="nope")
    with pytest.raises(ValueError):
        plugin.execute(action="tools")
    mcp_service.add_server("Local", base + "/mcp", REDIRECT_LOCAL)
    with pytest.raises(ValueError):
        plugin.execute(action="call", server="Local", tool="echo", arguments="not-json")
    with pytest.raises(ValueError):
        plugin.execute(action="call", server="Local", tool="nope", arguments="{}")
    with pytest.raises(ValueError):
        plugin.execute(action="call", server="Ghost", tool="echo", arguments="{}")


def test_plugin_is_registered_as_a_core_tool():
    from core.agent.loop.dispatcher import _CORE_MAP
    from core.tools.registry import CORE_TOOLS, list_core_specs

    assert "mcp_servers" in CORE_TOOLS
    assert _CORE_MAP["mcp_servers"] == "plugins.core_plugins.mcp_servers.tool"
    specs = list_core_specs()
    spec = next(item for item in specs if item.get("name") == "mcp_servers")
    assert spec["inputs"]["action"]["enum"] == ["list", "tools", "call"]
