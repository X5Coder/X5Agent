"""MCP server service — the one owner of add / connect / OAuth / disconnect.

Flow for a new server:
  add -> probe (initialize + tools/list) -> connected
                          \\-> 401 -> OAuth (discovery, registration, PKCE)
                                     -> callback -> tokens -> probe -> connected

Pending OAuth flows live in memory only (single-use state, 10 minute TTL,
bounded to 20) so a console restart simply asks the user to press Connect
again. Network calls happen outside the store lock; one server never connects
twice at the same time.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.mcp import client as mcp_client  # noqa: E402
from core.mcp import oauth as mcp_oauth  # noqa: E402
from core.mcp import store as mcp_store  # noqa: E402

_FLOW_TTL = 600.0
_MAX_FLOWS = 20
_REFRESH_SKEW = 30


class McpServiceError(RuntimeError):
    """User-facing MCP action failure with an HTTP status hint."""

    def __init__(self, message: str, status: int = 502) -> None:
        self.status = int(status)
        super().__init__(str(message))


class McpService:
    def __init__(self) -> None:
        self._flow_lock = threading.RLock()
        self._flow_locks: dict[str, threading.Lock] = {}
        self._pending: dict[str, dict] = {}

    # ------------------------------------------------------------------ reads
    def list_servers(self) -> list[dict]:
        return [mcp_store.public_record(record) for record in mcp_store.load_records()]

    def get_server(self, server_id: str) -> dict:
        record = mcp_store.get_record(server_id)
        if record is None:
            raise McpServiceError("This MCP server was not found", 404)
        return mcp_store.public_record(record, full=True)

    def describe_servers(self) -> list[dict]:
        """Compact catalogue for the agent tool (name + status + tool count)."""
        return [
            {
                "id": record["id"],
                "name": record["name"],
                "status": record["status"],
                "tool_count": len(record["tools"]),
            }
            for record in mcp_store.load_records()
        ]

    def describe_tools(self, server_ref: object) -> dict:
        """One server's tools with their input JSON Schema."""
        record = self._resolve_record(server_ref)
        return {
            "id": record["id"],
            "name": record["name"],
            "status": record["status"],
            "error": record["error"],
            "tools": [
                {
                    "name": tool["name"],
                    "title": tool["title"],
                    "description": tool["description"],
                    "input_schema": tool["input_schema"],
                }
                for tool in record["tools"]
            ],
        }

    def call_tool(self, server_ref: object, tool_name: object, arguments: object) -> dict:
        """Run one tool on a connected server (never touches the UI state)."""
        record = self._resolve_record(server_ref)
        name = " ".join(str(tool_name or "").split())
        if not name:
            raise McpServiceError("tool is required: use a name from action=tools", 400)
        if len(name) > 128:
            raise McpServiceError("The MCP tool name is not valid", 400)
        known = [tool["name"] for tool in record["tools"]]
        if name not in known:
            listing = ", ".join(known[:15]) or "(none yet)"
            raise McpServiceError(
                f"'{name[:80]}' is not a tool of {record['name']}. Available: {listing}", 400
            )
        if record["status"] != "connected":
            raise McpServiceError(
                f"{record['name']} is not connected. Press Connect on the MCP Servers page.", 409
            )
        payload = arguments if isinstance(arguments, dict) else {}
        if not isinstance(arguments, dict) and arguments not in (None, ""):
            raise McpServiceError("arguments must be a JSON object", 400)
        lock = self._lock_for(record["id"])
        if not lock.acquire(timeout=60):
            raise McpServiceError("The MCP server is busy. Try again.", 409)
        try:
            record = mcp_store.get_record(record["id"]) or record
            token = self._call_token(record)
            try:
                result = mcp_client.call_tool(record["url"], token, name, payload)
            except mcp_client.McpAuthRequired:
                refresh = str((record.get("oauth") or {}).get("refresh_token") or "")
                refreshed = False
                if refresh:
                    try:
                        self._refresh(record)
                        refreshed = True
                    except Exception:
                        refreshed = False
                if not refreshed:
                    self._require_auth(record)
                    raise McpServiceError(
                        f"{record['name']} needs authorization. Press Authorize on the MCP Servers page.",
                        409,
                    ) from None
                try:
                    result = mcp_client.call_tool(
                        record["url"],
                        str((record.get("oauth") or {}).get("access_token") or ""),
                        name,
                        payload,
                    )
                except mcp_client.McpError as exc:
                    raise McpServiceError(str(exc) or "The MCP tool call failed") from None
            except mcp_client.McpUnreachable:
                raise McpServiceError("Could not reach the MCP server") from None
            except mcp_client.McpError as exc:
                raise McpServiceError(str(exc) or "The MCP tool call failed") from None
            return {
                "server": record["name"],
                "server_id": record["id"],
                "tool": name,
                **result,
            }
        finally:
            lock.release()

    # ------------------------------------------------------------------ writes
    def add_server(self, name: object, url: object, redirect_uri: str) -> dict:
        try:
            redirect_uri = mcp_oauth.validate_redirect_uri(redirect_uri)
        except mcp_oauth.OAuthError as exc:
            raise McpServiceError(str(exc) or "The console redirect address is not usable", 400) from None
        try:
            record = mcp_store.new_record(name, url)
        except ValueError as exc:
            raise McpServiceError(str(exc), 400) from None
        with self._flow_lock:
            records = mcp_store.load_records()
            if len(records) >= mcp_store.MAX_SERVERS:
                raise McpServiceError(
                    f"You can add up to {mcp_store.MAX_SERVERS} MCP servers", 400
                )
            if any(existing["url"] == record["url"] for existing in records):
                raise McpServiceError("This MCP server is already added", 400)
            records.insert(0, record)
            try:
                mcp_store.save_records(records)
            except mcp_store.McpStoreError as exc:
                raise McpServiceError(str(exc), 500) from None
        return self.connect(record["id"], redirect_uri)

    def connect(self, server_id: str, redirect_uri: str) -> dict:
        """Probe (or authorize) one server. Returns {server, auth_url}."""
        lock = self._lock_for(server_id)
        if not lock.acquire(blocking=False):
            raise McpServiceError("This server is already connecting", 409)
        try:
            record = mcp_store.get_record(server_id)
            if record is None:
                raise McpServiceError("This MCP server was not found", 404)
            return self._connect_locked(record, redirect_uri)
        finally:
            lock.release()

    def _connect_locked(self, record: dict, redirect_uri: str) -> dict:
        hint = ""
        try:
            probe = self._probe(record)
        except mcp_client.McpAuthRequired as exc:
            hint = exc.resource_metadata
        except mcp_client.McpUnreachable:
            self._mark_error(record, "Could not reach the MCP server")
            raise McpServiceError("Could not reach the MCP server") from None
        except mcp_client.McpError as exc:
            message = str(exc) or "The MCP server refused the connection"
            self._mark_error(record, message)
            raise McpServiceError(message) from None
        else:
            return {"server": self._apply_probe(record, probe), "auth_url": ""}

        # The token (if any) was refused: try one refresh before asking the user.
        if record["oauth"].get("refresh_token"):
            try:
                self._refresh(record)
                probe = self._probe(record)
            except mcp_client.McpAuthRequired:
                return self.begin_oauth(record, redirect_uri, hint)
            except Exception:
                pass
            else:
                return {"server": self._apply_probe(record, probe), "auth_url": ""}
        return self.begin_oauth(record, redirect_uri, hint)

    def begin_oauth(self, record: dict, redirect_uri: str, hint: str = "") -> dict:
        """Discover the authorization server, register the client and build the URL."""
        try:
            redirect_uri = mcp_oauth.validate_redirect_uri(redirect_uri)
        except mcp_oauth.OAuthError as exc:
            raise McpServiceError(
                str(exc) or "Open the console on this machine or its local network address",
                400,
            ) from None
        try:
            discovered = mcp_oauth.discover(record["url"], hint)
            metadata = discovered["metadata"]
            scopes = discovered["scopes"]
            stored = record.get("oauth") or {}
            reuse = (
                stored.get("client_id")
                and stored.get("redirect_uri") == redirect_uri
                and stored.get("issuer") == discovered["issuer"]
            )
            if reuse:
                client = {
                    "client_id": stored["client_id"],
                    "client_secret": stored.get("client_secret", ""),
                    "client_auth": stored.get("client_auth", "none"),
                    "redirect_uri": redirect_uri,
                }
            else:
                client = mcp_oauth.register_client(metadata, redirect_uri, scopes)
        except mcp_oauth.OAuthError as exc:
            message = str(exc) or "OAuth discovery failed"
            self._mark_error(record, message)
            raise McpServiceError(message, 400) from None

        verifier = mcp_oauth.new_verifier()
        state = mcp_oauth.new_state()
        resource = discovered["resource"]
        try:
            auth_url = mcp_oauth.authorization_url(
                metadata, client, state, verifier, resource, scopes
            )
        except mcp_oauth.OAuthError as exc:
            self._mark_error(record, str(exc))
            raise McpServiceError(str(exc), 400) from None

        with self._flow_lock:
            self._prune_pending()
            if len(self._pending) >= _MAX_FLOWS:
                oldest = min(self._pending, key=lambda key: self._pending[key]["created_at"])
                self._pending.pop(oldest, None)
            self._pending[state] = {
                "server_id": record["id"],
                "verifier": verifier,
                "redirect_uri": redirect_uri,
                "metadata": {
                    "authorization_endpoint": metadata.get("authorization_endpoint"),
                    "token_endpoint": metadata.get("token_endpoint"),
                },
                "client": client,
                "resource": resource,
                "scope": " ".join(scopes),
                "created_at": time.time(),
            }

        record["oauth"] = {
            **{key: value for key, value in (record.get("oauth") or {}).items()
               if key in ("access_token", "refresh_token", "expires_at", "token_type", "scope")},
            "issuer": discovered["issuer"],
            "authorization_server": discovered["issuer"],
            "token_endpoint": str(metadata.get("token_endpoint") or ""),
            "client_id": client["client_id"],
            "client_secret": client.get("client_secret", ""),
            "client_auth": client.get("client_auth", "none"),
            "redirect_uri": redirect_uri,
            "resource": resource,
        }
        record["status"] = "auth_required"
        record["error"] = ""
        record["updated_at"] = mcp_store.now_iso()
        try:
            saved = mcp_store.upsert_record(record)
        except mcp_store.McpStoreError as exc:
            raise McpServiceError(str(exc), 500) from None
        return {"server": mcp_store.public_record(saved), "auth_url": auth_url}

    def complete_oauth(self, state: str, code: str, error: str = "") -> dict:
        """Finish a pending flow. Returns {ok, server_name, error}."""
        clean_state = str(state or "").strip()
        with self._flow_lock:
            self._prune_pending()
            flow = self._pending.pop(clean_state, None) if clean_state else None
        if flow is None:
            return {"ok": False, "server_name": "", "error": "This authorization link is no longer valid. Press Connect again."}

        lock = self._lock_for(flow["server_id"])
        if not lock.acquire(timeout=30):
            return {"ok": False, "server_name": "", "error": "This server is busy. Press Connect again."}
        try:
            return self._finish_flow(flow, code, error)
        finally:
            lock.release()

    def _finish_flow(self, flow: dict, code: str, error: str) -> dict:
        record = mcp_store.get_record(flow["server_id"])
        if record is None:
            return {"ok": False, "server_name": "", "error": "That MCP server was removed."}
        name = record["name"]
        if error:
            record["status"] = "auth_required"
            record["error"] = "Authorization was refused"
            record["updated_at"] = mcp_store.now_iso()
            self._save_quiet(record)
            return {"ok": False, "server_name": name, "error": "Authorization was refused."}
        if not str(code or "").strip():
            return {"ok": False, "server_name": name, "error": "The authorization server returned no code."}

        try:
            tokens = mcp_oauth.exchange_code(
                flow["metadata"], flow["client"], code, flow["verifier"], flow["resource"]
            )
        except mcp_oauth.OAuthError as exc:
            record["status"] = "auth_required"
            record["error"] = "Token exchange failed"
            record["updated_at"] = mcp_store.now_iso()
            self._save_quiet(record)
            return {"ok": False, "server_name": name, "error": str(exc) or "Token exchange failed."}

        self._store_tokens(record, tokens, flow)
        record["status"] = "auth_required"
        record["error"] = ""
        record["updated_at"] = mcp_store.now_iso()
        try:
            mcp_store.upsert_record(record)
        except mcp_store.McpStoreError:
            return {"ok": False, "server_name": name, "error": "Could not save the MCP server."}

        try:
            probe = self._probe(record)
        except mcp_client.McpError as exc:
            self._mark_error(record, str(exc) or "The MCP server refused the connection")
            return {"ok": False, "server_name": name, "error": str(exc) or "The MCP server refused the connection."}
        view = self._apply_probe(record, probe)
        return {"ok": True, "server_name": view["name"], "error": ""}

    def disconnect(self, server_id: str) -> dict:
        record = mcp_store.get_record(server_id)
        if record is None:
            raise McpServiceError("This MCP server was not found", 404)
        oauth = record.get("oauth") or {}
        token = str(oauth.get("access_token") or "")
        for key in ("access_token", "refresh_token", "expires_at", "token_type", "scope"):
            oauth.pop(key, None)
        record["oauth"] = oauth
        record["status"] = "disconnected"
        record["error"] = ""
        record["tools"] = []
        record["protocol_version"] = ""
        record["server_name"] = ""
        record["server_version"] = ""
        record["connected_at"] = ""
        record["updated_at"] = mcp_store.now_iso()
        saved = mcp_store.upsert_record(record)
        if token:
            mcp_client.close_session(record["url"], token, "")
        return {"server": mcp_store.public_record(saved)}

    def remove(self, server_id: str) -> dict:
        record = mcp_store.get_record(server_id)
        if record is None:
            raise McpServiceError("This MCP server was not found", 404)
        if not mcp_store.delete_record(server_id):
            raise McpServiceError("This MCP server was not found", 404)
        with self._flow_lock:
            for state in [key for key, flow in self._pending.items() if flow["server_id"] == server_id]:
                self._pending.pop(state, None)
        return {"ok": True}

    # ---------------------------------------------------------------- helpers
    def _resolve_record(self, ref: object) -> dict:
        """Find one server by id, exact name (any case) or unique name prefix."""
        value = " ".join(str(ref or "").split())[:120]
        if not value:
            raise McpServiceError("server is required: use a name from action=list", 400)
        records = mcp_store.load_records()
        for record in records:
            if record["id"] == value:
                return record
        lowered = value.casefold()
        exact = [record for record in records if record["name"].casefold() == lowered]
        if len(exact) == 1:
            return exact[0]
        prefix = [record for record in records if record["name"].casefold().startswith(lowered)]
        if len(prefix) == 1:
            return prefix[0]
        if not records:
            raise McpServiceError(
                "No MCP server is added yet. Add one on the MCP Servers page.", 404
            )
        names = ", ".join(record["name"] for record in (exact or prefix or records)[:10])
        raise McpServiceError(
            f"No single MCP server matches '{value[:60]}'. Available: {names}", 404
        )

    def _call_token(self, record: dict) -> str:
        """A usable access token, refreshing once when it has expired."""
        oauth = record.get("oauth") or {}
        token = str(oauth.get("access_token") or "")
        expires = int(oauth.get("expires_at") or 0)
        if token and (not expires or expires > int(time.time()) + _REFRESH_SKEW):
            return token
        if oauth.get("refresh_token"):
            try:
                self._refresh(record)
            except Exception:
                return ""
            return str((record.get("oauth") or {}).get("access_token") or "")
        return ""

    def _require_auth(self, record: dict) -> None:
        record["status"] = "auth_required"
        record["error"] = "Authorization is required"
        record["updated_at"] = mcp_store.now_iso()
        self._save_quiet(record)

    def _lock_for(self, server_id: str) -> threading.Lock:
        with self._flow_lock:
            lock = self._flow_locks.get(server_id)
            if lock is None:
                lock = threading.Lock()
                self._flow_locks[server_id] = lock
            return lock

    def _prune_pending(self) -> None:
        cutoff = time.time() - _FLOW_TTL
        for state in [key for key, flow in self._pending.items() if flow["created_at"] < cutoff]:
            self._pending.pop(state, None)

    def _probe(self, record: dict) -> dict:
        return mcp_client.probe(record["url"], self._call_token(record))

    def _refresh(self, record: dict) -> None:
        oauth = record.get("oauth") or {}
        refresh = str(oauth.get("refresh_token") or "")
        if not refresh:
            raise McpServiceError("Authorization is required", 401)
        endpoint = str(oauth.get("token_endpoint") or "")
        if not endpoint:
            discovered = mcp_oauth.discover(record["url"])
            endpoint = str(discovered["metadata"].get("token_endpoint") or "")
            oauth["token_endpoint"] = endpoint
            oauth["resource"] = discovered["resource"]
        client = {
            "client_id": str(oauth.get("client_id") or ""),
            "client_secret": str(oauth.get("client_secret") or ""),
            "client_auth": str(oauth.get("client_auth") or "none"),
            "redirect_uri": str(oauth.get("redirect_uri") or ""),
        }
        tokens = mcp_oauth.refresh_access_token(
            {"token_endpoint": endpoint}, client, refresh, str(oauth.get("resource") or "")
        )
        flow = {"scope": oauth.get("scope", "")}
        self._store_tokens(record, tokens, flow, keep_refresh=refresh)
        record["updated_at"] = mcp_store.now_iso()
        save = mcp_store.upsert_record(record)
        record["oauth"] = save["oauth"]

    def _store_tokens(self, record: dict, tokens: dict, flow: dict, keep_refresh: str = "") -> None:
        oauth = record.get("oauth") or {}
        oauth["access_token"] = tokens["access_token"]
        oauth["token_type"] = tokens.get("token_type") or "Bearer"
        oauth["expires_at"] = str(tokens.get("expires_at") or 0)
        refresh = tokens.get("refresh_token") or keep_refresh
        if refresh:
            oauth["refresh_token"] = refresh
        scope = tokens.get("scope") or flow.get("scope") or oauth.get("scope") or ""
        if scope:
            oauth["scope"] = scope
        client = (flow.get("client") or {}) if isinstance(flow.get("client"), dict) else {}
        for key in ("client_id", "client_secret", "client_auth"):
            if client.get(key):
                oauth[key] = client[key]
        if flow.get("redirect_uri"):
            oauth["redirect_uri"] = flow["redirect_uri"]
        if flow.get("resource"):
            oauth["resource"] = flow["resource"]
        metadata = flow.get("metadata") if isinstance(flow.get("metadata"), dict) else {}
        if metadata.get("token_endpoint"):
            oauth["token_endpoint"] = metadata["token_endpoint"]
        record["oauth"] = oauth

    def _apply_probe(self, record: dict, probe: dict) -> dict:
        stamp = mcp_store.now_iso()
        record["status"] = "connected"
        record["error"] = ""
        record["tools"] = mcp_store.normalize_tools(probe.get("tools"))
        record["protocol_version"] = str(probe.get("protocol_version") or "")[:32]
        record["server_name"] = str(probe.get("server_name") or "")[:60]
        record["server_version"] = str(probe.get("server_version") or "")[:40]
        record["connected_at"] = stamp
        record["updated_at"] = stamp
        try:
            saved = mcp_store.upsert_record(record)
        except mcp_store.McpStoreError as exc:
            raise McpServiceError(str(exc), 500) from None
        try:
            token = str((record.get("oauth") or {}).get("access_token") or "")
            mcp_client.close_session(
                record["url"], token, str(probe.get("session_id") or ""),
                str(probe.get("protocol_version") or ""),
            )
        except Exception:
            pass
        return mcp_store.public_record(saved)

    def _mark_error(self, record: dict, message: str) -> None:
        record["status"] = "error"
        record["error"] = " ".join(str(message or "Connection failed").split())[:300]
        record["updated_at"] = mcp_store.now_iso()
        self._save_quiet(record)

    def _save_quiet(self, record: dict) -> None:
        try:
            mcp_store.upsert_record(record)
        except mcp_store.McpStoreError:
            pass


mcp_service = McpService()
