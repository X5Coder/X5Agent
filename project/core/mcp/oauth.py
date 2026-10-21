"""MCP OAuth 2.0 client pieces: discovery, dynamic registration, PKCE.

Implements the client half of the MCP authorization spec:
protected-resource metadata (RFC 9728) -> authorization-server metadata
(RFC 8414 / OIDC) -> dynamic client registration (RFC 7591) -> authorization
code + PKCE (S256) -> token endpoint (RFC 8707 ``resource`` included).

Every request is bounded, redirects are validated hop by hop, and an error
message never carries a token, secret or full endpoint query string.
"""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import secrets
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_MAX_METADATA = 256 * 1024
_MAX_TOKEN_BODY = 256 * 1024
_TIMEOUT = 15.0
_MAX_REDIRECTS = 3
_MAX_SCOPES = 8

_S256 = "S256"
_ALLOWED_AUTH_METHODS = frozenset({"none", "client_secret_basic", "client_secret_post"})
_BLOCKED_HOSTS = frozenset({"169.254.169.254", "metadata.google.internal"})
_REDIRECT_PATH = "/api/mcp/oauth/callback"


class OAuthError(RuntimeError):
    """Safe, user-facing OAuth failure."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


def _private_host(host: str) -> bool:
    """Loopback, LAN (RFC1918/ULA), link-local, CGNAT and *.local names.

    The console runs on the user's own machine: localhost, 127.0.0.1 and the
    machine's LAN address (with its port) must all be able to receive the
    OAuth redirect. Plain HTTP outside the local network is never accepted.
    """
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


def validate_https_url(raw: object, what: str = "URL") -> str:
    """https everywhere, http only on this machine or a local network."""
    value = str(raw or "").strip()
    if not value or len(value) > 2048:
        raise OAuthError(f"{what} is not valid")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise OAuthError(f"{what} is not valid") from None
    if parts.scheme.lower() not in ("http", "https"):
        raise OAuthError(f"{what} must use https")
    if parts.username or parts.password:
        raise OAuthError(f"{what} must not contain credentials")
    host = (parts.hostname or "").strip().lower()
    if not host or host in _BLOCKED_HOSTS:
        raise OAuthError(f"{what} is not allowed")
    if parts.scheme.lower() == "http" and not _private_host(host):
        raise OAuthError(f"{what} must use https")
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((parts.scheme.lower(), netloc, parts.path or "/", parts.query, ""))


def validate_redirect_uri(raw: object) -> str:
    """The console callback: any local host/port the user opened the console on.

    ``http://localhost:7777``, ``http://127.0.0.1:7777`` and the machine's LAN
    address (``http://192.168.1.5:7777``) are all valid, because the popup and
    the callback are served by this same console. The path is fixed.
    """
    value = str(raw or "").strip()
    if not value or len(value) > 2048:
        raise OAuthError("Console redirect address is not valid")
    try:
        parts = urllib.parse.urlsplit(value)
        port = parts.port
    except ValueError:
        raise OAuthError("Console redirect address is not valid") from None
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise OAuthError("Console redirect address must use http or https")
    if parts.username or parts.password:
        raise OAuthError("Console redirect address must not contain credentials")
    host = (parts.hostname or "").strip().lower()
    if not host or host in _BLOCKED_HOSTS:
        raise OAuthError("Console redirect address is not allowed")
    if scheme == "http" and not _private_host(host):
        raise OAuthError(
            "Open the console over HTTPS (HTTP works on localhost and local networks)"
        )
    if parts.path.rstrip("/") != _REDIRECT_PATH or parts.query:
        raise OAuthError("Console redirect address is not valid")
    netloc = f"[{host}]" if ":" in host and not host.startswith("[") else host
    if port:
        netloc = f"{netloc}:{port}"
    return urllib.parse.urlunsplit((scheme, netloc, _REDIRECT_PATH, "", ""))


def _origin(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))



def _request_json(url: str, *, data: dict | None = None, timeout: float = _TIMEOUT,
                  cap: int = _MAX_METADATA) -> dict:
    """GET or POST JSON with validated redirects and a bounded body."""
    target = validate_https_url(url)
    headers = {"Accept": "application/json", "User-Agent": "X5Agent"}
    body = None
    method = "GET"
    if data is not None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    opener = urllib.request.build_opener(_NoRedirect())
    for _hop in range(_MAX_REDIRECTS + 1):
        request = urllib.request.Request(target, data=body, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(cap + 1)
                if len(raw) > cap:
                    raise OAuthError("The authorization server answer is too large")
                try:
                    parsed = json.loads(raw.decode("utf-8", "replace") or "{}")
                except Exception:
                    raise OAuthError("The authorization server sent an invalid answer") from None
                if not isinstance(parsed, dict):
                    raise OAuthError("The authorization server sent an invalid answer")
                return parsed
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            if status in (301, 302, 303, 307, 308):
                location = exc.headers.get("Location", "") if exc.headers else ""
                if not location:
                    raise OAuthError("The authorization server redirected without a target") from None
                target = validate_https_url(urllib.parse.urljoin(target, location))
                if status == 303:
                    method, body = "GET", None
                continue
            detail = ""
            try:
                payload = json.loads(exc.read(8192).decode("utf-8", "replace") or "{}")
                if isinstance(payload, dict):
                    detail = " ".join(str(payload.get("error_description") or payload.get("error") or "").split())[:200]
            except Exception:
                detail = ""
            message = f"The authorization server answered HTTP {status}"
            if detail:
                message = f"{message}: {detail}"
            raise OAuthError(message) from None
        except (socket.timeout, TimeoutError):
            raise OAuthError("The authorization server did not answer in time") from None
        except urllib.error.URLError:
            raise OAuthError("Could not reach the authorization server") from None
    raise OAuthError("The authorization server redirected too many times")


def _pick_prm_urls(resource_url: str) -> list[str]:
    parts = urllib.parse.urlsplit(resource_url)
    base = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")
    urls = []
    if path:
        urls.append(f"{base}/.well-known/oauth-protected-resource{path}")
    urls.append(f"{base}/.well-known/oauth-protected-resource")
    return urls


def _authorization_metadata(issuer: str) -> dict | None:
    issuer = validate_https_url(issuer, "Authorization server")
    parts = urllib.parse.urlsplit(issuer)
    path = parts.path.rstrip("/")
    candidates = [
        f"{_origin(issuer)}/.well-known/oauth-authorization-server{path}",
        f"{_origin(issuer)}/.well-known/oauth-authorization-server",
        f"{_origin(issuer)}/.well-known/openid-configuration{path}",
        f"{_origin(issuer)}/.well-known/openid-configuration",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            meta = _request_json(candidate)
        except OAuthError:
            continue
        if isinstance(meta.get("authorization_endpoint"), str) and isinstance(meta.get("token_endpoint"), str):
            try:
                meta["authorization_endpoint"] = validate_https_url(meta["authorization_endpoint"], "Authorization endpoint")
                meta["token_endpoint"] = validate_https_url(meta["token_endpoint"], "Token endpoint")
                if isinstance(meta.get("registration_endpoint"), str) and meta["registration_endpoint"]:
                    meta["registration_endpoint"] = validate_https_url(meta["registration_endpoint"], "Registration endpoint")
            except OAuthError:
                continue
            meta["issuer"] = str(meta.get("issuer") or issuer)
            return meta
    return None


def discover(resource_url: str, resource_metadata_hint: str = "") -> dict:
    """Resolve the authorization server for one MCP endpoint."""
    resource = validate_https_url(resource_url, "MCP server URL")
    metadata: dict | None = None
    candidates: list[str] = []
    if resource_metadata_hint:
        try:
            candidates.append(validate_https_url(resource_metadata_hint, "Protected-resource metadata URL"))
        except OAuthError:
            pass
    candidates.extend(_pick_prm_urls(resource))

    for candidate in candidates:
        try:
            data = _request_json(candidate)
        except OAuthError:
            continue
        if isinstance(data.get("authorization_servers"), list):
            metadata = data
            break

    servers: list[str] = []
    scopes: list[str] = []
    advertised_resource = resource
    if metadata:
        for item in metadata.get("authorization_servers") or []:
            try:
                servers.append(validate_https_url(item, "Authorization server"))
            except OAuthError:
                continue
            if len(servers) >= 3:
                break
        for item in metadata.get("scopes_supported") or []:
            text = " ".join(str(item).split())[:100]
            if text and text not in scopes:
                scopes.append(text)
            if len(scopes) >= _MAX_SCOPES:
                break
        clean_resource = metadata.get("resource")
        if isinstance(clean_resource, str) and clean_resource.strip():
            try:
                advertised_resource = validate_https_url(clean_resource, "Protected resource")
            except OAuthError:
                advertised_resource = resource
    if not servers:
        servers = [_origin(resource)]

    for issuer in servers:
        meta = _authorization_metadata(issuer)
        if meta is None:
            continue
        methods = meta.get("code_challenge_methods_supported")
        if isinstance(methods, list) and methods and _S256 not in methods:
            raise OAuthError("This server does not support the required PKCE method (S256)")
        if not scopes and isinstance(meta.get("scopes_supported"), list):
            for item in meta["scopes_supported"]:
                text = " ".join(str(item).split())[:100]
                if text and text not in scopes:
                    scopes.append(text)
                if len(scopes) >= _MAX_SCOPES:
                    break
        return {
            "metadata": meta,
            "resource": advertised_resource,
            "scopes": scopes,
            "issuer": str(meta.get("issuer") or issuer),
        }
    raise OAuthError(
        "This MCP server did not advertise an OAuth authorization server that supports dynamic registration"
    )


def register_client(metadata: dict, redirect_uri: str, scopes: list[str] | None = None, client_name: str = "X5Agent") -> dict:
    """Dynamic client registration (RFC 7591). No retries: registration is not idempotent."""
    endpoint = metadata.get("registration_endpoint")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise OAuthError("This MCP server requires a pre-registered client and does not support dynamic registration")
    redirect = validate_redirect_uri(redirect_uri)
    body = {
        "client_name": client_name[:64],
        "client_uri": "https://x5agent.com",
        "redirect_uris": [redirect],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scopes:
        body["scope"] = " ".join(scopes)[:1000]
    answer = _request_json(endpoint, data=body)
    client_id = answer.get("client_id")
    if not isinstance(client_id, str) or not client_id.strip():
        raise OAuthError("The authorization server did not return a client id")
    method = answer.get("token_endpoint_auth_method")
    if not isinstance(method, str) or method not in _ALLOWED_AUTH_METHODS:
        method = "none"
    secret = answer.get("client_secret")
    return {
        "client_id": client_id.strip()[:512],
        "client_secret": secret.strip()[:512] if isinstance(secret, str) else "",
        "client_auth": method,
        "redirect_uri": redirect,
    }


def new_verifier() -> str:
    return secrets.token_urlsafe(64)[:96]


def challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(metadata: dict, client: dict, state: str, verifier: str,
                      resource: str, scopes: list[str]) -> str:
    endpoint = validate_https_url(metadata.get("authorization_endpoint"), "Authorization endpoint")
    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": client["redirect_uri"],
        "state": state,
        "code_challenge": challenge(verifier),
        "code_challenge_method": _S256,
    }
    if resource:
        params["resource"] = resource
    if scopes:
        params["scope"] = " ".join(scopes)
    separator = "&" if urllib.parse.urlsplit(endpoint).query else "?"
    return f"{endpoint}{separator}{urllib.parse.urlencode(params)}"


def _form_post(url: str, form: dict, client: dict) -> dict:
    endpoint = validate_https_url(url, "Token endpoint")
    body = dict(form)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "X5Agent",
    }
    auth = client.get("client_auth") or "none"
    if auth == "client_secret_basic" and client.get("client_secret"):
        raw = f"{urllib.parse.quote(client['client_id'], safe='')}:{urllib.parse.quote(client['client_secret'], safe='')}"
        headers["Authorization"] = "Basic " + base64.b64encode(raw.encode("ascii")).decode("ascii")
    elif auth == "client_secret_post" and client.get("client_secret"):
        body["client_secret"] = client["client_secret"]
    encoded = urllib.parse.urlencode(body).encode("ascii")
    request = urllib.request.Request(endpoint, data=encoded, headers=headers, method="POST")
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=_TIMEOUT) as response:
            raw = response.read(_MAX_TOKEN_BODY + 1)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            payload = json.loads(exc.read(8192).decode("utf-8", "replace") or "{}")
            if isinstance(payload, dict):
                detail = " ".join(str(payload.get("error_description") or payload.get("error") or "").split())[:200]
        except Exception:
            detail = ""
        message = f"The token endpoint answered HTTP {int(exc.code)}"
        if detail:
            message = f"{message}: {detail}"
        raise OAuthError(message) from None
    except (socket.timeout, TimeoutError):
        raise OAuthError("The token endpoint did not answer in time") from None
    except urllib.error.URLError:
        raise OAuthError("Could not reach the token endpoint") from None
    if len(raw) > _MAX_TOKEN_BODY:
        raise OAuthError("The token answer is too large")
    try:
        answer = json.loads(raw.decode("utf-8", "replace") or "{}")
    except Exception:
        raise OAuthError("The token endpoint sent an invalid answer") from None
    if not isinstance(answer, dict):
        raise OAuthError("The token endpoint sent an invalid answer")
    access = answer.get("access_token")
    if not isinstance(access, str) or not access.strip() or len(access) > 8192:
        raise OAuthError("The token endpoint did not return an access token")
    token_type = answer.get("token_type")
    if isinstance(token_type, str) and token_type.strip().lower() != "bearer":
        raise OAuthError("The token endpoint returned an unsupported token type")
    try:
        expires_in = int(answer.get("expires_in") or 3600)
    except (TypeError, ValueError):
        expires_in = 3600
    expires_in = max(60, min(expires_in, 31_536_000))
    refresh = answer.get("refresh_token")
    scope = answer.get("scope")
    return {
        "access_token": access.strip(),
        "refresh_token": refresh.strip()[:8192] if isinstance(refresh, str) else "",
        "token_type": "Bearer",
        "scope": " ".join(str(scope or "").split())[:1000],
        "expires_at": int(time.time()) + expires_in,
    }


def exchange_code(metadata: dict, client: dict, code: str, verifier: str,
                  resource: str) -> dict:
    form = {
        "grant_type": "authorization_code",
        "code": str(code).strip()[:4096],
        "redirect_uri": client["redirect_uri"],
        "client_id": client["client_id"],
        "code_verifier": verifier,
    }
    if resource:
        form["resource"] = resource
    return _form_post(str(metadata.get("token_endpoint") or ""), form, client)


def refresh_access_token(metadata: dict, client: dict, refresh_token: str,
                         resource: str = "") -> dict:
    form = {
        "grant_type": "refresh_token",
        "refresh_token": str(refresh_token).strip()[:8192],
        "client_id": client["client_id"],
    }
    if resource:
        form["resource"] = resource
    return _form_post(str(metadata.get("token_endpoint") or ""), form, client)
