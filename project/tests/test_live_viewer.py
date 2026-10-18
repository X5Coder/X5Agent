"""Gated live-viewer port (X5Browser.liveviewer): token gate + mode forcing.

Safe: no browser, no relay, no listening socket. Handlers are called directly
with aiohttp's mocked request, so the whole gate contract is checked without
binding a port or touching a live session.
"""
import asyncio
import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import pytest

pytest.importorskip("aiohttp", reason="the gate is an aiohttp server")

from aiohttp.test_utils import make_mocked_request  # noqa: E402

from X5Browser import liveviewer as lv  # noqa: E402

CONTROL = "C" * 43
PREVIEW = "P" * 43


def _request(path: str, cookies: dict | None = None, match_info: dict | None = None):
    headers = {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if match_info is None:
        return make_mocked_request("GET", path, headers=headers)
    return make_mocked_request("GET", path, headers=headers, match_info=match_info)


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def live_tokens(monkeypatch):
    """A live session: both tokens known (the library links are not touched)."""
    monkeypatch.setattr(
        lv, "tokens", lambda links=None: {"control": CONTROL, "preview": PREVIEW}
    )


# -- configuration ----------------------------------------------------------


def test_viewer_port_default_and_env(monkeypatch):
    monkeypatch.delenv("X5_LIVE_VIEWER_PORT", raising=False)
    assert lv.DEFAULT_PORT == 8790
    assert lv.viewer_port() == lv.DEFAULT_PORT
    monkeypatch.setenv("X5_LIVE_VIEWER_PORT", "8123")
    assert lv.viewer_port() == 8123


def test_viewer_port_rejects_nonsense(monkeypatch):
    for bad in ("", "abc", "0", "-1", "70000", "6080.5"):
        monkeypatch.setenv("X5_LIVE_VIEWER_PORT", bad)
        assert lv.viewer_port() == lv.DEFAULT_PORT, bad


def test_gate_base_is_loopback_on_its_own_port(monkeypatch):
    monkeypatch.setenv("X5_LIVE_VIEWER_PORT", "8791")
    assert lv.gate_base() == "http://127.0.0.1:8791/view"


def test_publish_base_sets_the_switch_once(monkeypatch):
    monkeypatch.delenv("X5_LIVE_PUBLIC_URL", raising=False)
    assert lv.publish_base() is True
    assert os.environ["X5_LIVE_PUBLIC_URL"] == lv.gate_base()
    # An operator who set the variable stays in charge.
    monkeypatch.setenv("X5_LIVE_PUBLIC_URL", "https://live.example.com/view")
    assert lv.publish_base() is True
    assert os.environ["X5_LIVE_PUBLIC_URL"] == "https://live.example.com/view"


def test_relay_port_is_the_library_default():
    # The gate proxies the library's own loopback relay, never a public port.
    assert lv.relay_port() == 6080


# -- tokens and authorization ----------------------------------------------


def test_tokens_read_out_of_the_library_links():
    links = {
        "running": True,
        "control": f"http://127.0.0.1:8790/view?scale=true&token={CONTROL}",
        "preview": f"http://127.0.0.1:8790/view?scale=true&token={PREVIEW}&view_only=true",
        "owner": "agent",
    }
    assert lv.tokens(links) == {"control": CONTROL, "preview": PREVIEW}


def test_tokens_empty_without_a_session():
    assert lv.tokens({}) == {}
    assert lv.tokens({"running": True, "control": "", "preview": ""}) == {}
    assert lv.tokens({"control": "http://x/view?scale=true"}) == {}


def test_authorize_modes_and_rejections():
    links = {
        "control": f"http://x/view?token={CONTROL}",
        "preview": f"http://x/view?token={PREVIEW}",
    }
    assert lv.authorize(CONTROL, links) == "control"
    assert lv.authorize(PREVIEW, links) == "preview"
    assert lv.authorize("", links) == ""
    assert lv.authorize(None, links) == ""
    assert lv.authorize("C" * 42, links) == ""
    assert lv.authorize("Z" * 43, links) == ""


# -- mode forcing -----------------------------------------------------------


def test_inject_mode_forces_the_token_mode():
    html = "        rfb.resizeSession = false;\n"
    preview = lv.inject_mode(html, "preview")
    assert "rfb.viewOnly = true;" in preview
    assert "rfb.viewOnly = false;" not in preview
    control = lv.inject_mode(html, "control")
    assert "rfb.viewOnly = false;" in control
    assert "rfb.viewOnly = true;" not in control
    # An unknown mode and a page without the marker are left exactly as they are.
    assert lv.inject_mode(html, "bogus") == html
    assert lv.inject_mode("no marker here", "preview") == "no marker here"


def test_viewer_page_comes_from_the_library():
    pytest.importorskip("x5browser", reason="library page under test")
    html = lv.viewer_html()
    assert lv.MODE_MARKER in html  # this is the marker the gate patches


# -- handlers (no socket) --------------------------------------------------


def test_view_without_token_is_not_connect():
    response = _run(lv.handle_view(_request("/view")))
    assert response.status == 410
    assert response.headers["Access-Control-Allow-Origin"] == "*"


def test_view_with_unknown_token_is_not_connect(live_tokens):
    response = _run(lv.handle_view(_request("/view?token=" + "Z" * 43)))
    assert response.status == 410


def test_view_serves_the_page_and_sets_the_gate_cookie(live_tokens):
    response = _run(lv.handle_view(_request("/view?token=%s&view_only=false" % PREVIEW)))
    assert response.status == 200
    assert response.content_type == "text/html"
    cookie = response.cookies[lv.COOKIE_NAME]
    assert cookie.value == PREVIEW
    assert cookie["httponly"] is True
    # A cached page would replay a stale mode and skip the cookie handshake.
    assert response.headers["Cache-Control"] == "no-store"
    # The token decides the mode: the query string cannot make it interactive.
    assert b"rfb.viewOnly = true;" in response.body


def test_websockify_requires_the_gate_cookie(live_tokens):
    response = _run(lv.handle_websockify(_request("/websockify")))
    assert response.status == 410
    bogus = {lv.COOKIE_NAME: "bogus"}
    response = _run(lv.handle_websockify(_request("/websockify", bogus)))
    assert response.status == 410


def test_asset_proxy_rejects_traversal():
    from aiohttp import web

    request = _request("/core/rfb.js", match_info={"tail": "../../etc/passwd"})
    with pytest.raises(web.HTTPNotFound):
        _run(lv.handle_asset(request))
    empty = _request("/", match_info={"tail": ""})
    with pytest.raises(web.HTTPNotFound):
        _run(lv.handle_asset(empty))


def test_asset_content_type_is_a_bare_media_type():
    assert lv._content_type("text/html; charset=utf-8") == "text/html"
    assert lv._content_type(None) == "application/octet-stream"


def test_asset_proxy_serves_the_whole_file(monkeypatch):
    """Regression: the viewer died with "Unexpected end of input" at rfb.js:1931.

    `content.read(cap)` returns only the first buffered chunk, so a several
    hundred KB noVNC module reached the browser truncated. The proxy must drain
    the whole upstream body (and still refuse anything past the size cap).
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from aiohttp import web

    payload = b"export default function RFB() {}\n" * 8000  # ~232 KB, many chunks
    too_big = b"x" * (lv.MAX_ASSET_BYTES + 1024)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (stdlib handler name)
            body = too_big if self.path.endswith("huge.js") else payload
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence the test server
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        monkeypatch.setattr(lv, "relay_port", lambda: server.server_address[1])
        response = _run(lv.handle_asset(
            _request("/core/rfb.js", match_info={"tail": "core/rfb.js"})))
        assert response.body == payload
        assert response.content_length == len(payload)
        assert response.content_type == "text/javascript"
        # A cached partial copy would keep the browser broken after the fix.
        assert response.headers["Cache-Control"] == "no-store"
        with pytest.raises(web.HTTPRequestEntityTooLarge):
            _run(lv.handle_asset(
                _request("/core/huge.js", match_info={"tail": "core/huge.js"})))
    finally:
        server.shutdown()
        server.server_close()


def test_websockify_echoes_the_subprotocol_the_browser_offers(
    monkeypatch, live_tokens
):
    """Regression: noVNC opens the socket with ``Sec-WebSocket-Protocol: binary``.

    Chrome aborts any socket whose offered subprotocol was not echoed ("Sent
    non-empty 'Sec-WebSocket-Protocol' header but no response was received"),
    the viewer retries forever and the user only ever sees the loading screen.
    The gate must echo what the viewer asked for and still pass frames through.
    """
    import socket

    import aiohttp
    from aiohttp import web

    def free_port() -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    gate_port, relay_port = free_port(), free_port()
    monkeypatch.setattr(lv, "relay_port", lambda: relay_port)
    monkeypatch.setenv("X5_LIVE_VIEWER_PORT", str(gate_port))

    async def echo(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for message in ws:
            if message.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(b"echo:" + message.data)
            elif message.type == aiohttp.WSMsgType.CLOSE:
                break
        return ws

    async def scenario():
        relay = web.Application()
        relay.router.add_get("/websockify", echo)
        relay_runner = web.AppRunner(relay, access_log=None)
        await relay_runner.setup()
        await web.TCPSite(relay_runner, "127.0.0.1", relay_port).start()
        assert await lv.start_async() is True
        try:
            url = f"http://127.0.0.1:{gate_port}/websockify"
            cookie = {"Cookie": f"{lv.COOKIE_NAME}={CONTROL}"}
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(
                    url, protocols=("binary",), headers=cookie
                ) as ws:
                    assert ws.protocol == "binary"
                    await ws.send_bytes(b"hello")
                    frame = await ws.receive()
                    assert frame.type == aiohttp.WSMsgType.BINARY
                    assert frame.data == b"echo:hello"
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await session.ws_connect(url, protocols=("binary",))
        finally:
            await lv.stop_async()
            await relay_runner.cleanup()

    asyncio.run(scenario())


# -- library wiring --------------------------------------------------------


def test_links_owner_mints_on_the_gate():
    """The owner the service hands the library must mint gate-based links.

    The library's agent API defaults to a token owner on the raw relay, so this
    is the piece that moves the whole session (control, preview, human_control,
    show_preview) onto the addressed viewer.
    """
    pytest.importorskip("x5browser", reason="library token owner under test")
    owner = lv.links_owner()
    assert owner is not None
    owner.begin_session()
    report = owner.report()
    prefix = f"http://127.0.0.1:{lv.viewer_port()}/view?"
    assert report["url"].startswith(prefix), report["url"]
    assert report["preview_url"].startswith(prefix), report["preview_url"]
    minted = lv.tokens({"control": report["url"], "preview": report["preview_url"]})
    links = {"control": report["url"], "preview": report["preview_url"]}
    assert lv.authorize(minted["control"], links) == "control"
    assert lv.authorize(minted["preview"], links) == "preview"


def test_service_keeps_the_library_default_while_the_gate_is_down(monkeypatch):
    from X5Browser import service

    monkeypatch.setattr(lv, "_runner", None)
    monkeypatch.setattr(lv, "_port", 0)
    # A failed gate must never leave the browser with dead links.
    assert service._live_links_kwarg() == {}


def test_service_hands_the_gate_owner_over(monkeypatch):
    pytest.importorskip("x5browser", reason="library token owner under test")
    from X5Browser import service

    monkeypatch.setattr(lv, "is_started", lambda: True)
    links = service._live_links_kwarg().get("links")
    assert links is not None
    links.begin_session()
    assert links.report()["url"].startswith(
        f"http://127.0.0.1:{lv.viewer_port()}/view?"
    )


# -- app surface -----------------------------------------------------------


def test_app_exposes_only_the_expected_routes():
    app = lv._app()
    paths = {resource.canonical for resource in app.router.resources()}
    assert paths == {
        "/view",
        "/websockify",
        "/api/events",
        "/api/activity",
        "/{tail}",
    }


def test_library_optional_endpoints_answer_without_a_stream():
    """The page's own activity beacon / event stream never reach the relay.

    Proxying them produced a 404 per request and an endless EventSource retry
    in the browser console; the gate answers "no stream" instead.
    """
    events = _run(lv.handle_no_stream(_request("/api/events")))
    assert events.status == 204
    assert events.headers["Cache-Control"] == "no-store"
    beacon = _run(lv.handle_no_stream(_request("/api/activity")))
    assert beacon.status == 204


def test_start_and_stop_are_safe_to_repeat(monkeypatch):
    monkeypatch.setattr(lv, "_runner", None)
    monkeypatch.setattr(lv, "_port", 0)
    assert lv.is_started() is False
    assert lv.started_port() == 0
    # Never started -> nothing to close.
    assert _run(lv.stop_async()) is True
    assert lv.is_started() is False


def test_gate_binds_gates_and_closes(monkeypatch):
    """End to end on a free port: bind, serve the viewer, refuse, ungate."""
    pytest.importorskip("x5browser", reason="the gate serves the library page")
    import socket

    monkeypatch.setattr(lv, "_runner", None)
    monkeypatch.setattr(lv, "_port", 0)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
    probe.close()
    monkeypatch.setattr(lv, "viewer_port", lambda: free_port)

    async def scenario():
        import aiohttp

        assert await lv.start_async() is True
        assert lv.is_started() is True
        assert lv.started_port() == free_port
        try:
            async with aiohttp.ClientSession() as client:
                # No token -> the library's own Not Connect contract.
                async with client.get(f"http://127.0.0.1:{free_port}/view") as response:
                    assert response.status == 410
                    assert (await response.json()) == lv.NOT_CONNECT
                # A hand-made token is not a session token either.
                async with client.get(
                    f"http://127.0.0.1:{free_port}/view?token={'Z' * 43}"
                ) as response:
                    assert response.status == 410
                # A real token serves the page with the gate cookie.
                monkeypatch.setattr(lv, "tokens", lambda links=None: {"preview": PREVIEW})
                async with client.get(
                    f"http://127.0.0.1:{free_port}/view?token={PREVIEW}"
                ) as response:
                    assert response.status == 200
                    body = await response.text()
                    assert "rfb.viewOnly = true;" in body
                    assert response.cookies[lv.COOKIE_NAME].value == PREVIEW
                # The asset proxy answers 404/503 without a relay, never HTML.
                async with client.get(
                    f"http://127.0.0.1:{free_port}/core/rfb.js"
                ) as resp:
                    assert resp.status in (404, 503)
        finally:
            assert await lv.stop_async() is True
        assert lv.is_started() is False
        assert lv.started_port() == 0

    _run(scenario())
