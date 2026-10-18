"""liveviewer.py — the project's ONE public live-viewer port (token-gated).

Why this module exists
----------------------
The library serves its viewer page on the loopback relay
(``127.0.0.1:6080``): that relay is websockify wired straight onto x11vnc with
no authentication of any kind, so it can never be the public face of the live
browser. This module publishes exactly one port (default 8790, env
``X5_LIVE_VIEWER_PORT``) that serves the library's own viewer page and proxies
its stream, gated by the very tokens the library already mints per session:

  GET /view?token=...  the viewer page, only for a token the current session
                       really minted (control or preview) and with its mode
                       forced FROM that token — a preview link can never send
                       input even if the query string is edited by hand.
                       Anything else answers the library's own contract:
                       410 ``{"status": "Not Connect"}``.
  GET /websockify      the WebSocket upgrade, proxied to the loopback relay
                       and only for a viewer that already passed /view (the
                       HttpOnly gate cookie it received there). The raw relay
                       is never exposed, so an unauthenticated client cannot
                       reach the stream even by guessing this port.
  GET /<path>          the noVNC assets the page imports, proxied from the
                       loopback relay (which only serves its own web root).

Ownership: this module owns the listening socket and nothing else. The browser
itself (boot/adopt/close/heal) stays with ``X5Browser.service`` — this module
only reads the tokens that service already reports and stops when the browser
closes. Every failure (aiohttp missing, port taken, relay down) is reported,
never fatal: ``start()`` returns False and the project keeps the library's raw
loopback links instead of breaking the browser.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from pathlib import Path
from typing import Any

DEFAULT_PORT = 8790
COOKIE_NAME = "x5_live_gate"
VIEWER_FILE = "x5-live.html"
MODE_MARKER = "rfb.resizeSession = false;"
MAX_ASSET_BYTES = 8 * 1024 * 1024
NOT_CONNECT = {"status": "Not Connect"}

_runner: Any = None
_port: int = 0
_relay_cache: int = 0


def viewer_port() -> int:
    """The public live-viewer port (env ``X5_LIVE_VIEWER_PORT``, default 8790)."""
    raw = ""
    try:
        raw = str(os.environ.get("X5_LIVE_VIEWER_PORT", "") or "").strip()
    except Exception:
        raw = ""
    try:
        port = int(raw)
    except Exception:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


def gate_base() -> str:
    """The base URL the library must mint its session links on."""
    return f"http://127.0.0.1:{viewer_port()}/view"


def publish_base() -> bool:
    """Point the library's links at this port (never overrides an operator).

    The library reads ``X5_LIVE_PUBLIC_URL`` when it builds a session's links,
    so this is the single switch that keeps its tokens and this gate together.
    An operator who set the variable themselves stays in charge.
    """
    try:
        os.environ.setdefault("X5_LIVE_PUBLIC_URL", gate_base())
        return True
    except Exception:
        return False


def links_owner() -> Any:
    """The library's token owner, minting its links on the gated port.

    The agent-side library API falls back to a plain ``LiveLinks()`` whose base
    is the raw loopback relay, so the base has to be handed over explicitly:
    that path reads no environment variable (only the library's live-stack
    owner does, which is why ``publish_base`` exists too). Returns None when the
    library is unavailable, so callers keep the library's own default.
    """
    try:
        from x5browser.live.links import LiveLinks

        return LiveLinks(base_url=gate_base())
    except Exception:
        return None


def is_started() -> bool:
    """True while the gate owns its port."""
    return _runner is not None


def started_port() -> int:
    """The port the running gate bound (0 when it is not started)."""
    return _port if _runner is not None else 0


def relay_port() -> int:
    """The library's loopback relay port (websockify), cached once."""
    global _relay_cache
    if _relay_cache:
        return _relay_cache
    port = 6080
    try:
        from x5browser import LiveStack

        candidate = int(getattr(LiveStack(), "novnc_port", port))
        if 1 <= candidate <= 65535:
            port = candidate
    except Exception:
        port = 6080
    _relay_cache = port
    return port


def tokens(links: dict | None = None) -> dict[str, str]:
    """The session's ``{"control": token, "preview": token}`` (may be empty).

    Tokens are read out of the URLs the library reports, so this module never
    needs (or keeps) the library's internal token store.
    """
    if links is None:
        try:
            from .service import live_links

            links = live_links()
        except Exception:
            links = {}
    found: dict[str, str] = {}
    if not isinstance(links, dict):
        return found
    for key in ("control", "preview"):
        url = links.get(key)
        if not isinstance(url, str) or "token=" not in url:
            continue
        query = url.split("?", 1)[-1]
        for chunk in query.split("&"):
            if chunk.startswith("token=") and len(chunk) > len("token="):
                found[key] = chunk[len("token=") :]
                break
    return found


def authorize(token: Any, links: dict | None = None) -> str:
    """The mode a token unlocks: ``"control"``, ``"preview"``, or ``""``."""
    if not isinstance(token, str) or not token:
        return ""
    known = tokens(links)
    for mode in ("control", "preview"):
        candidate = known.get(mode, "")
        if candidate and len(candidate) == len(token) and secrets.compare_digest(
            candidate, token
        ):
            return mode
    return ""


def inject_mode(html: str, mode: str) -> str:
    """Force the page's mode from the token (the library's own patch).

    The query string never decides: only the token does, so a preview link
    cannot be turned into an interactive one by editing the URL.
    """
    if not isinstance(html, str) or MODE_MARKER not in html:
        return html
    if mode not in ("control", "preview"):
        return html
    interactive = "false" if mode == "control" else "true"
    return html.replace(
        MODE_MARKER, f"{MODE_MARKER}\n        rfb.viewOnly = {interactive};", 1
    )


def viewer_html() -> str:
    """The library's own viewer page (the only page this port serves)."""
    import x5browser

    path = Path(x5browser.__file__).resolve().parent / "live" / "web" / VIEWER_FILE
    return path.read_text(encoding="utf-8")


async def _serve(port: int) -> Any:
    """Bind the gate on every interface and return its runner."""
    from aiohttp import web

    runner = web.AppRunner(_app(), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host="0.0.0.0", port=port).start()
    return runner


async def start_async(port: int = 0) -> bool:
    """Publish the gate once, from the browser loop.

    Must be awaited on the loop that owns the browser (the gate lives there
    with the session it serves). Idempotent: an already-started gate returns
    True without binding a second port. False = not published at all; callers
    then keep the library's raw loopback links.
    """
    global _runner, _port
    if is_started():
        return True
    try:
        import aiohttp  # noqa: F401 - the gate needs the aiohttp server
    except Exception:
        return False
    target = port or viewer_port()
    try:
        runner = await _serve(target)
    except Exception:
        _runner = None
        _port = 0
        return False
    _runner = runner
    _port = target
    return True


async def stop_async() -> bool:
    """Close the gate. Safe to await at any time, also when never started."""
    global _runner, _port
    runner, _runner = _runner, None
    _port = 0
    if runner is None:
        return True
    try:
        await runner.cleanup()
        return True
    except Exception:
        return False


# -- HTTP surface -----------------------------------------------------------


def _not_connect(status: int = 410) -> Any:
    from aiohttp import web

    return web.json_response(
        dict(NOT_CONNECT), status=status, headers={"Access-Control-Allow-Origin": "*"}
    )


def _content_type(raw: Any) -> str:
    """A bare media type: aiohttp rejects parameters in ``content_type``."""
    text = str(raw or "").split(";", 1)[0].strip()
    return text or "application/octet-stream"


async def handle_view(request: Any) -> Any:
    """The token gate: the viewer page while the token is live, else Not Connect."""
    from aiohttp import web

    mode = authorize(request.query.get("token", ""))
    if not mode:
        return _not_connect()
    try:
        body = inject_mode(viewer_html(), mode).encode("utf-8")
    except Exception:
        return web.Response(status=503, text="viewer page unavailable")
    response = web.Response(body=body, content_type="text/html", charset="utf-8")
    # The page carries the mode and the gate cookie: a cached copy could be
    # replayed for a later token (or without re-setting the cookie, leaving the
    # socket unauthorized), so it is never stored.
    response.headers["Cache-Control"] = "no-store"
    response.set_cookie(
        COOKIE_NAME,
        request.query.get("token", ""),
        httponly=True,
        samesite="Lax",
        path="/",
    )
    return response


def _offered_protocols(request: Any) -> tuple[str, ...]:
    """The subprotocols the viewer asked for (noVNC always asks for ``binary``).

    A browser fails any socket whose offered subprotocol was not echoed back
    ("Sent non-empty 'Sec-WebSocket-Protocol' header but no response was
    received") and the relay answers the handshake without selecting one, so
    the gate has to select one itself or the viewer never connects.
    """
    raw = request.headers.get("Sec-WebSocket-Protocol", "")
    return tuple(part.strip() for part in raw.split(",") if part.strip())


async def handle_websockify(request: Any) -> Any:
    """Proxy the VNC stream for a viewer that already passed /view."""
    from aiohttp import ClientSession, web

    if not authorize(request.cookies.get(COOKIE_NAME, "")):
        return _not_connect()
    server = web.WebSocketResponse(
        heartbeat=30.0, protocols=_offered_protocols(request)
    )
    await server.prepare(request)
    try:
        async with ClientSession() as session:
            async with session.ws_connect(
                f"ws://127.0.0.1:{relay_port()}/websockify"
            ) as client:
                await asyncio.gather(
                    _pump(server, client), _pump(client, server)
                )
    except Exception:
        pass  # a dead relay or a closing viewer is a normal end of stream
    try:
        await server.close()
    except Exception:
        pass
    return server


async def _pump(source: Any, target: Any) -> None:
    """Forward text/binary frames one way until either side closes."""
    from aiohttp import WSMsgType

    try:
        async for message in source:
            if message.type == WSMsgType.TEXT:
                await target.send_str(message.data)
            elif message.type == WSMsgType.BINARY:
                await target.send_bytes(message.data)
            elif message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
            elif message.type == WSMsgType.ERROR:
                break
    except Exception:
        pass
    try:
        await target.close()
    except Exception:
        pass


async def handle_asset(request: Any) -> Any:
    """Proxy the noVNC files the viewer page imports (relay web root only).

    The body is drained chunk by chunk on purpose: `response.content.read(n)`
    returns only what is buffered at that moment, so a real module (rfb.js is
    several hundred KB) reached the browser truncated and the viewer died with
    "Unexpected end of input" instead of ever drawing the stream.
    """
    from aiohttp import ClientError, ClientSession, web

    relative = str(request.match_info.get("tail", "") or "")
    if not relative or ".." in relative:
        raise web.HTTPNotFound(text="not a viewer asset")
    url = f"http://127.0.0.1:{relay_port()}/{relative}"
    try:
        async with ClientSession() as session:
            async with session.get(url) as upstream:
                if upstream.status != 200:
                    raise web.HTTPNotFound(text="viewer asset unavailable")
                chunks: list[bytes] = []
                received = 0
                async for chunk in upstream.content.iter_chunked(64 * 1024):
                    received += len(chunk)
                    if received > MAX_ASSET_BYTES:
                        raise web.HTTPRequestEntityTooLarge(
                            MAX_ASSET_BYTES,
                            text="viewer asset exceeds the size cap",
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                ctype = _content_type(upstream.headers.get("Content-Type"))
    except ClientError:
        # The relay is down (a unit restarting): retryable, not a 404.
        raise web.HTTPServiceUnavailable(
            text="viewer upstream is down", headers={"Retry-After": "2"}
        )
    return web.Response(
        body=body,
        content_type=ctype,
        # Never cache: a browser that kept a previously truncated/older module
        # would keep failing long after the proxy was fixed, and these files are
        # small and local (the whole point of the gate is one correct copy).
        headers={"Cache-Control": "no-store"},
    )


def _app() -> Any:
    """The gate's whole public surface (three routes, nothing else)."""
    from aiohttp import web

    application = web.Application(client_max_size=64 * 1024)
    application.add_routes(
        [
            web.get("/view", handle_view),
            web.get("/websockify", handle_websockify),
            web.get("/{tail:.*}", handle_asset),
        ]
    )
    return application
