"""Sync sections: hash-only requests answered on the stream, in priority order."""
from __future__ import annotations

import json
import queue
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.server import server as S  # noqa: E402


class _Sub:
    """One fake SSE subscriber: register, then read the fanned-out frames."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue(maxsize=64)
        with S._EVENTS_LOCK:
            S._EVENT_SUBS.append({"q": self.q})

    def frames(self) -> list:
        out = []
        while True:
            try:
                out.append(self.q.get_nowait())
            except Exception:
                return out

    def close(self) -> None:
        with S._EVENTS_LOCK:
            for sub in list(S._EVENT_SUBS):
                if sub.get("q") is self.q:
                    S._EVENT_SUBS.remove(sub)


class _FakeHandler:
    """Minimal stand-in for the request handler (no sockets involved)."""

    def __init__(self, body):
        self._body = body
        self.sent: list = []

    def _read_json_body(self, max_bytes=65536):
        return self._body

    def _send_json(self, obj, status=200, headers=None):
        self.sent.append((status, obj))


def _with_stub_builders(data: dict) -> dict:
    """Replace the real section builders with deterministic stubs."""
    saved = dict(S._SYNC_BUILDERS)
    S._SYNC_BUILDERS.clear()
    S._SYNC_BUILDERS.update({name: (lambda n=name: data[n]) for name in data})
    return saved


# ---- hash ------------------------------------------------------------------

def test_section_hash_is_stable_and_breaks_on_change():
    a = S._section_hash({"x": [1, 2, 3]})
    assert a == S._section_hash({"x": [1, 2, 3]})  # deterministic
    assert a != S._section_hash({"x": [1, 2, 4]})
    assert a != S._section_hash({"x": [1, 2, 3], "y": 1})
    # Unserializable data never raises, still yields a hash.
    assert isinstance(S._section_hash({"bad": object()}), str)


# ---- SSE wire format -------------------------------------------------------

def test_sse_frame_is_a_named_event_with_json_data():
    raw = S._sse_frame({"seq": 7, "type": "unchanged", "data": {"op_id": "r_3", "section": "agent"}})
    text = raw.decode("utf-8")
    assert text.startswith("event: unchanged\ndata: ")
    assert text.endswith("\n\n")
    body = json.loads(text.split("data: ", 1)[1].strip())
    assert body["seq"] == 7
    assert body["data"]["op_id"] == "r_3"
    assert body["data"]["section"] == "agent"


def test_sse_frame_sanitises_the_event_name():
    """A malformed type can never inject extra SSE lines."""
    text = S._sse_frame({"type": "bad\ndata: injected"}).decode("utf-8")
    assert text.count("\n") == 3, "an injected newline must not add an SSE field line"
    lines = text.split("\n")
    assert lines[0] == "event: baddatainjected"  # name cleaned, newline dropped
    assert lines[1].startswith("data: ")
    assert lines[2:] == ["", ""]  # the frame terminator, and nothing else
    assert json.loads(lines[1][6:])["type"] == "bad\ndata: injected"
    assert S._sse_frame({"type": ""}).decode("utf-8").startswith("event: message\n")


# ---- /api/sync handler -----------------------------------------------------

def test_sync_post_accepts_hashes_only_and_ignores_unknown_sections():
    sub = _Sub()
    saved = _with_stub_builders({"status": {"chat": {"active": False, "input": ""},
                                            "browser": {"running": False, "stream": None}}})
    try:
        fake = _FakeHandler({"op_id": "r_1", "sections": [
            {"section": "status", "hash": "nope"},
            {"section": "not_a_section", "hash": "x"},
            {"section": 42, "hash": "x"},
        ]})
        S.DashboardHandler._handle_sync_post(fake)
        status, ack = fake.sent[0]
        assert status == 200
        assert ack["op_id"] == "r_1"
        assert ack["sections"] == ["status"]
        frames = sub.frames()
        assert [f["type"] for f in frames] == ["status"]
        assert frames[0]["data"]["section"] == "status"
        assert frames[0]["data"]["hash"]
        assert "seq" in frames[0]
    finally:
        sub.close()
        S._SYNC_BUILDERS.clear()
        S._SYNC_BUILDERS.update(saved)


def test_sync_post_probe_without_sections_only_acks():
    """The 401 probe path: an empty sync must never fan out a section."""
    sub = _Sub()
    try:
        fake = _FakeHandler({"op_id": "probe", "sections": []})
        S.DashboardHandler._handle_sync_post(fake)
        assert fake.sent[0][1]["ok"] is True
        assert sub.frames() == []
    finally:
        sub.close()


def test_sync_post_is_bounded_to_the_four_sections():
    sub = _Sub()
    saved = _with_stub_builders({n: {"n": n} for n in S._SYNC_ORDER})
    try:
        many = [{"section": n, "hash": ""} for n in S._SYNC_ORDER] + [
            {"section": n, "hash": ""} for n in S._SYNC_ORDER]
        fake = _FakeHandler({"op_id": "r_9", "sections": many})
        S.DashboardHandler._handle_sync_post(fake)
        assert fake.sent[0][1]["sections"] == list(S._SYNC_ORDER)
        assert len(sub.frames()) == len(S._SYNC_ORDER)
    finally:
        sub.close()
        S._SYNC_BUILDERS.clear()
        S._SYNC_BUILDERS.update(saved)


# ---- answers ---------------------------------------------------------------

def test_matching_hash_answers_unchanged_and_missing_hash_sends_data():
    sub = _Sub()
    held = S._section_hash({"v": 1})
    saved = _with_stub_builders({"agent": {"v": 1}, "dashboard": {"v": 2}})
    try:
        S._sync_answer("r_5", {"agent": held, "dashboard": ""})
        frames = sub.frames()
        assert [f["type"] for f in frames] == ["unchanged", "dashboard"]
        assert frames[0]["data"] == {"op_id": "r_5", "section": "agent"}
        assert "data" not in frames[0]["data"]
        assert frames[1]["data"]["hash"] == S._section_hash({"v": 2})
        assert frames[1]["data"]["data"] == {"v": 2}
    finally:
        sub.close()
        S._SYNC_BUILDERS.clear()
        S._SYNC_BUILDERS.update(saved)


def test_answers_come_back_in_priority_order_not_request_order():
    sub = _Sub()
    saved = _with_stub_builders({n: {"n": n} for n in S._SYNC_ORDER})
    try:
        # Requested worst-first: the answer still follows the priority order.
        S._sync_answer("r_6", {"rest": "", "dashboard": "", "status": ""})
        assert [f["type"] for f in sub.frames()] == ["status", "dashboard", "rest"]
    finally:
        sub.close()
        S._SYNC_BUILDERS.clear()
        S._SYNC_BUILDERS.update(saved)


def test_a_failing_builder_never_breaks_the_other_sections():
    sub = _Sub()
    saved = _with_stub_builders({"status": {"ok": True}})

    def _boom():
        raise RuntimeError("builder exploded")

    S._SYNC_BUILDERS["agent"] = _boom
    try:
        S._sync_answer("r_7", {"agent": "", "status": ""})
        assert [f["type"] for f in sub.frames()] == ["status"]
    finally:
        sub.close()
        S._SYNC_BUILDERS.clear()
        S._SYNC_BUILDERS.update(saved)


def test_section_changes_signals_are_broadcast_as_changed():
    """Watchers signal the owning section instead of shipping raw data."""
    sub = _Sub()
    try:
        S._events_broadcast("changed", {"section": "rest"})
        frame = sub.frames()[0]
        assert frame["type"] == "changed"
        assert frame["data"] == {"section": "rest"}
    finally:
        sub.close()


def test_gateway_snapshot_is_the_single_gateway_state_builder():
    live = S._gateway_snapshot()
    assert isinstance(live, dict)
    assert "state" in live
    # The rest section carries exactly this shape (one owner, no drift).
    rest = S._sync_rest()
    assert set(rest) == {"workspace", "scheduler", "gateway"}
    assert rest["gateway"] == live


def test_every_section_is_keyed_by_the_channels_it_feeds():
    """Client contract: one section frame renders exactly its own channels."""
    assert set(S._sync_status()) == {"chat", "browser"}
    assert set(S._sync_agent()) == {"chat_last50"}
    assert set(S._sync_dashboard()) == {"dashboard"}
    assert set(S._sync_rest()) == {"workspace", "scheduler", "gateway"}
    assert isinstance(S._sync_agent()["chat_last50"], list)
    assert isinstance(S._sync_dashboard()["dashboard"], dict)


# ---- real transport --------------------------------------------------------
# The wire contract (named SSE events + hash-only sync) is verified against a
# real HTTP server on a loopback port: opening the stream, posting hashes, and
# reading the answers back in priority order.

import re  # noqa: E402
import socket  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402


def _drain(sock, needle: bytes, deadline: float, count: int = 1) -> bytes:
    """Read until `needle` appeared `count` times (or the deadline passed)."""
    buf = b""
    while buf.count(needle) < count and time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _read_all(sock, deadline: float) -> bytes:
    """Read a complete one-shot response (until the server closes)."""
    buf = b""
    while time.time() < deadline:
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
    return buf


def _sync_post(port: int, body: dict, cookie: bool = True) -> bytes:
    payload = json.dumps(body).encode("utf-8")
    head = b"POST /api/sync HTTP/1.1\r\nHost: t\r\n"
    if cookie:
        head += b"Cookie: x5sess=ok\r\n"
    head += b"Content-Type: application/json\r\nContent-Length: "
    head += str(len(payload)).encode() + b"\r\n\r\n"
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(head + payload)
        return _read_all(sock, time.time() + 5)
    finally:
        sock.close()


def test_stream_and_sync_roundtrip(monkeypatch):
    from web.server import console_auth as ca

    monkeypatch.setattr(ca, "is_setup", lambda: True)
    monkeypatch.setattr(ca, "session_valid", lambda token: bool(token))
    monkeypatch.setattr(S, "_events_watchers", lambda: None)

    httpd = S.ThreadedServer(("127.0.0.1", 0), S.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    stream = None
    try:
        # 1. No session cookie: the stream itself is refused (401 gate).
        bad = socket.create_connection(("127.0.0.1", port), timeout=5)
        bad.sendall(b"GET /api/events HTTP/1.1\r\nHost: t\r\n\r\n")
        reply = _read_all(bad, time.time() + 5)
        bad.close()
        assert b"401" in reply.split(b"\r\n")[0]

        # 2. With a cookie the stream opens and carries NO boot snapshot: the
        #    browser asks for data itself (hash-only sync).
        stream = socket.create_connection(("127.0.0.1", port), timeout=5)
        stream.sendall(b"GET /api/events HTTP/1.1\r\nHost: t\r\nCookie: x5sess=ok\r\n\r\n")
        head = _drain(stream, b"retry: 3000", time.time() + 5)
        assert b"200" in head.split(b"\r\n")[0], head[:200]
        assert b"text/event-stream" in head
        assert b"event: hello" not in head, "the hello snapshot is gone"

        # 3. A first sync (no hashes held) answers with all four sections, in
        #    priority order, on the stream.
        ack = _sync_post(port, {"op_id": "r_1",
                                "sections": [{"section": n, "hash": ""} for n in S._SYNC_ORDER]})
        assert b'"ok": true' in ack, ack[:400]
        assert b"r_1" in ack
        frames = _drain(stream, b"event: rest", time.time() + 8)
        answered = re.findall(rb"event: (\w+)\ndata: ([^\n]*)\n", frames)
        order = [name for name, _ in answered]
        assert order[:4] == [b"status", b"agent", b"dashboard", b"rest"], order
        assert b"event: hello" not in frames
        held = {}
        seqs = []
        for name, raw in answered[:4]:
            payload = json.loads(raw)
            assert payload["type"] == name.decode()
            assert payload["seq"] > 0
            inner = payload["data"]
            assert inner["section"] == name.decode()
            assert inner["op_id"] == "r_1"
            assert inner["hash"]
            seqs.append(payload["seq"])
            held[name.decode()] = inner["hash"]
        assert seqs == sorted(seqs), "frames must be numbered in order"

        # 4. Re-sending the SAME hashes answers `unchanged` per section — no
        #    payload, so the stored copy keeps rendering.
        _sync_post(port, {"op_id": "r_2",
                          "sections": [{"section": n, "hash": held[n]} for n in S._SYNC_ORDER]})
        same = _drain(stream, b"event: unchanged", time.time() + 8, count=4)
        unchanged = re.findall(rb"event: unchanged\ndata: ([^\n]*)\n", same)
        assert len(unchanged) == 4, same[-400:]
        for raw in unchanged:
            inner = json.loads(raw)["data"]
            assert inner["op_id"] == "r_2"
            assert inner["section"] in S._SYNC_ORDER
            assert "data" not in inner
            assert "hash" not in inner
    finally:
        try:
            if stream is not None:
                stream.close()
        except Exception:
            pass
        httpd.shutdown()
        httpd.server_close()


def test_sync_post_without_session_is_unauthorized(monkeypatch):
    from web.server import console_auth as ca

    monkeypatch.setattr(ca, "is_setup", lambda: True)
    monkeypatch.setattr(ca, "session_valid", lambda token: bool(token))
    httpd = S.ThreadedServer(("127.0.0.1", 0), S.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        reply = _sync_post(port, {"op_id": "r_3", "sections": []}, cookie=False)
        assert b"401" in reply.split(b"\r\n")[0]
        # The gate itself owns the refusal: the handler never ran.
        assert b'"ok"' not in reply
    finally:
        httpd.shutdown()
        httpd.server_close()
