"""Console owner auth: setup-once gate, pbkdf2 verify, sessions (no I/O outside tmp)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from web.server import console_auth as ca


def _tmp(tmp_path: Path) -> Path:
    return tmp_path / "console_auth.json"


def test_setup_then_verify(tmp_path):
    p = _tmp(tmp_path)
    assert ca.is_setup(p) is False
    assert ca.setup_owner("Owner@Gmail.com", "SecretPass1", p) == "owner@gmail.com"
    assert ca.is_setup(p) is True
    assert ca.owner_email(p) == "owner@gmail.com"
    assert ca.verify_owner("owner@gmail.com", "SecretPass1", p) is True
    assert ca.verify_owner("owner@gmail.com", "wrong-pass", p) is False
    assert ca.verify_owner("other@gmail.com", "SecretPass1", p) is False


def test_no_plaintext_password_stored(tmp_path):
    p = _tmp(tmp_path)
    ca.setup_owner("a@gmail.com", "SecretPass1", p)
    assert "SecretPass1" not in p.read_text(encoding="utf-8")


def test_setup_refuses_second_owner(tmp_path):
    p = _tmp(tmp_path)
    ca.setup_owner("a@gmail.com", "SecretPass1", p)
    with pytest.raises(RuntimeError):
        ca.setup_owner("c@gmail.com", "OtherPass2", p)


def test_owner_email_is_an_english_gmail_address():
    assert ca.email_problem("kareem.diaa+work@gmail.com") is None
    assert ca.email_problem("Kareem@Gmail.com") is None, "case is normalised"
    for bad in ("kareem@yahoo.com", "kareem@gmail.co", "kareem@gmail.com.eg",
                "بريد@gmail.com", "not-an-email", "", ".hidden@gmail.com"):
        assert ca.email_problem(bad), f"{bad!r} must be refused"


def test_owner_password_is_english_only():
    assert ca.password_problem("Kareem1234##") is None
    assert ca.password_problem("short") == "password too short (min 8)"
    assert ca.password_problem("") == "password is required"
    for bad in ("كلمةالسر123", "pass\tword", "pass word"):
        problem = ca.password_problem(bad)
        assert problem and "English" in problem, f"{bad!r} must be refused"


def test_setup_validates_input(tmp_path):
    with pytest.raises(ValueError):
        ca.setup_owner("not-an-email", "SecretPass1", _tmp(tmp_path))
    with pytest.raises(ValueError):
        ca.setup_owner("a@gmail.com", "short", _tmp(tmp_path))
    with pytest.raises(ValueError):
        ca.setup_owner("a@yahoo.com", "SecretPass1", _tmp(tmp_path))
    with pytest.raises(ValueError):
        ca.setup_owner("a@gmail.com", "كلمةالسر123", _tmp(tmp_path))


def test_change_password(tmp_path):
    p = _tmp(tmp_path)
    ca.setup_owner("a@gmail.com", "OldPass11", p)
    with pytest.raises(ValueError):
        ca.change_password("a@gmail.com", "wrong-old", "NewPass22", p)
    with pytest.raises(ValueError):
        ca.change_password("a@gmail.com", "OldPass11", "short", p)
    with pytest.raises(ValueError):
        ca.change_password("a@gmail.com", "OldPass11", "كلمةالسر123", p)
    ca.change_password("a@gmail.com", "OldPass11", "NewPass22", p)
    assert ca.verify_owner("a@gmail.com", "NewPass22", p) is True
    assert ca.verify_owner("a@gmail.com", "OldPass11", p) is False


def test_sessions_lifecycle():
    token = ca.create_session()
    assert ca.session_valid(token) is True
    assert ca.session_valid("bogus") is False
    assert ca.session_valid(None) is False
    ca.drop_session(token)
    assert ca.session_valid(token) is False


def test_cookie_parse():
    assert ca.session_from_cookie("x5sess=abc123; other=1") == "abc123"
    assert ca.session_from_cookie("other=1") is None
    assert ca.session_from_cookie(None) is None


def test_auth_status_keeps_the_owner_email_private(monkeypatch):
    """Real request: the gate question is public, the owner email is not."""
    import json
    import socket
    import threading
    import time

    from web.server import server as S

    monkeypatch.setattr(ca, "is_setup", lambda *a, **k: True)
    monkeypatch.setattr(ca, "owner_email", lambda *a, **k: "owner@gmail.com")
    monkeypatch.setattr(ca, "session_valid", lambda token: bool(token))

    httpd = S.ThreadedServer(("127.0.0.1", 0), S.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def status(cookie: str) -> dict:
        head = b"GET /api/auth/status HTTP/1.1\r\nHost: t\r\n"
        if cookie:
            head += f"Cookie: {cookie}\r\n".encode()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(head + b"\r\n")
            chunks = []
            deadline = time.time() + 5
            while time.time() < deadline:
                part = sock.recv(65536)
                if not part:
                    break
                chunks.append(part)
            raw = b"".join(chunks)
        finally:
            sock.close()
        assert b"200" in raw.split(b"\r\n")[0], raw[:200]
        return json.loads(raw.split(b"\r\n\r\n", 1)[1])

    try:
        anon = status("")
        assert anon["owner_missing"] is False, anon
        assert anon["email"] == "", anon  # no session -> nothing about the owner

        signed_in = status("x5sess=ok")
        assert signed_in["email"] == "owner@gmail.com", signed_in
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_browser_setup_creates_the_owner_once(tmp_path, monkeypatch):
    """First run: the browser creates the owner once, then setup is closed."""
    import json
    import socket
    import threading
    import time

    from web.server import server as S

    # Licensing bypass: this test is about the owner record, not the network.
    monkeypatch.setenv("X5_LICENSE_BYPASS", "1")
    target = {"path": tmp_path / "console_auth.json"}
    monkeypatch.setattr(ca, "owner_path", lambda path=None: path or target["path"])
    monkeypatch.setattr(ca, "create_session", lambda: "test-session")

    httpd = S.ThreadedServer(("127.0.0.1", 0), S.DashboardHandler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    def post(body: dict):
        payload = json.dumps(body).encode()
        head = ("POST /api/auth/setup HTTP/1.1\r\nHost: t\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(payload)}\r\n\r\n").encode()
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            sock.sendall(head + payload)
            chunks = []
            deadline = time.time() + 5
            while time.time() < deadline:
                part = sock.recv(65536)
                if not part:
                    break
                chunks.append(part)
            raw = b"".join(chunks)
        finally:
            sock.close()
        head_raw, _, body_raw = raw.partition(b"\r\n\r\n")
        status_line = head_raw.split(b"\r\n")[0]
        return status_line, head_raw, json.loads(body_raw or b"{}")

    try:
        line, head_raw, body = post({"email": "Owner@Gmail.com", "password": "SecretPass1"})
        assert b"200" in line, line
        assert body.get("ok") is True and body.get("email") == "owner@gmail.com", body
        assert b"x5sess=test-session" in head_raw, head_raw[:400]
        assert target["path"].exists(), "owner record must be written"

        # Create-once: a second creation is refused and the record is untouched.
        line, _, _ = post({"email": "other@gmail.com", "password": "OtherPass2"})
        assert b"409" in line, line
        assert ca.owner_email(target["path"]) == "owner@gmail.com"

        # Invalid credentials never create a record.
        target["path"] = tmp_path / "fresh.json"
        line, _, body = post({"email": "not-an-email", "password": "short"})
        assert b"422" in line, line
        assert body.get("error"), body
        assert not target["path"].exists()
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_startup_boots_in_first_run_mode(tmp_path, monkeypatch):
    """No owner + no env + no TTY: the server starts so /login.html can create it."""
    from web.server import server as S

    record = tmp_path / "console_auth.json"
    monkeypatch.setattr(ca, "owner_path", lambda path=None: path or record)
    monkeypatch.delenv("X5_EMAIL", raising=False)
    monkeypatch.delenv("X5_PASSWORD", raising=False)
    assert S._ensure_owner_from_terminal() is True
    assert not record.exists(), "booting must not create anything by itself"


# ------------------------------------------------- first-run license account


def _boot(monkeypatch):
    import threading

    from web.server import server as S

    httpd = S.ThreadedServer(("127.0.0.1", 0), S.DashboardHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, httpd.server_address[1]


def _request(port: int, method: str, path: str, body: dict | None = None,
             cookie: str = ""):
    import json
    import socket
    import time

    payload = json.dumps(body).encode() if body is not None else b""
    head = f"{method} {path} HTTP/1.1\r\nHost: t\r\n"
    if cookie:
        head += f"Cookie: {cookie}\r\n"
    if body is not None:
        head += "Content-Type: application/json\r\n"
        head += f"Content-Length: {len(payload)}\r\n"
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        sock.sendall(head.encode() + b"\r\n" + payload)
        chunks = []
        deadline = time.time() + 5
        while time.time() < deadline:
            part = sock.recv(65536)
            if not part:
                break
            chunks.append(part)
        raw = b"".join(chunks)
    finally:
        sock.close()
    head_raw, _, body_raw = raw.partition(b"\r\n\r\n")
    return head_raw, body_raw


def test_server_guard_blocks_work_but_allows_login(monkeypatch):
    """Every HTTP request passes the guard; login/status stay reachable."""
    from core.agent_guard import points

    monkeypatch.setattr(ca, "is_setup", lambda: True)
    monkeypatch.setattr(ca, "session_valid", lambda token: bool(token))
    monkeypatch.setattr(
        points,
        "server_guard",
        lambda: (_ for _ in ()).throw(
            points.GuardRefusal("seal_failed", "Protected code verification failed.")
        ),
    )
    httpd, port = _boot(monkeypatch)
    try:
        head, _body = _request(port, "GET", "/api/dashboard", cookie="x5sess=ok")
        assert b"403" in head.split(b"\r\n")[0], head[:200]
        head, _body = _request(port, "GET", "/api/auth/status")
        assert b"200" in head.split(b"\r\n")[0], head[:200]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_setup_registers_the_license_account_first(tmp_path, monkeypatch):
    """A console owner is only created when the license server accepted it."""
    import json

    from core.licensing import LicenseError, license_service
    from web.server import server as S

    target = tmp_path / "console_auth.json"
    monkeypatch.setattr(ca, "owner_path", lambda path=None: path or target)
    monkeypatch.setattr(ca, "create_session", lambda: "test-session")
    monkeypatch.delenv("X5_LICENSE_BYPASS", raising=False)

    calls: list[tuple] = []
    monkeypatch.setattr(license_service, "register",
                        lambda email, pw: calls.append(("register", email, pw)))

    def refuse(*_a, **_k):
        raise LicenseError("Could not reach the license server. Try again.")

    monkeypatch.setattr(license_service, "login", refuse)

    httpd, port = _boot(monkeypatch)
    try:
        # Server refuses -> nothing local is created, the error is readable.
        monkeypatch.setattr(license_service, "register", refuse)
        head, body_raw = _request(port, "POST", "/api/auth/setup",
                                  {"email": "Owner@Gmail.com", "password": "SecretPass1"})
        assert b"502" in head.split(b"\r\n")[0], head[:200]
        assert json.loads(body_raw)["license_failed"] is True
        assert not target.exists(), "a failed signup must not write the owner"

        # Server accepts -> owner + session + next page.
        monkeypatch.setattr(license_service, "register",
                            lambda email, pw: calls.append(("register", email, pw)))
        head, body_raw = _request(port, "POST", "/api/auth/setup",
                                  {"email": "Owner@Gmail.com", "password": "SecretPass1"})
        assert b"200" in head.split(b"\r\n")[0], head[:200]
        body = json.loads(body_raw)
        assert body["ok"] is True and body["next"] == "/plans.html"
        assert target.exists()
        assert calls and calls[-1] == ("register", "owner@gmail.com", "SecretPass1")
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_pages_wait_for_a_plan_choice(monkeypatch):
    """Signed in without a chosen plan: pages go to /plans.html, APIs stay open."""
    from web.server import server as S

    monkeypatch.setattr(ca, "is_setup", lambda *a, **k: True)
    monkeypatch.setattr(ca, "session_valid", lambda token: token == "ok")
    monkeypatch.setattr(S.DashboardHandler, "_onboarding_needed", lambda self: True)

    httpd, port = _boot(monkeypatch)
    try:
        head, _body = _request(port, "GET", "/", cookie="x5sess=ok")
        assert b"302" in head.split(b"\r\n")[0], head[:200]
        assert b"Location: /plans.html" in head, head[:400]

        head, _body = _request(port, "GET", "/plans.html", cookie="x5sess=ok")
        assert b"200" in head.split(b"\r\n")[0], head[:200]

        head, _body = _request(port, "GET", "/api/health", cookie="x5sess=ok")
        assert b"200" in head.split(b"\r\n")[0], head[:200]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_plans_page_bounces_once_the_plan_is_chosen(monkeypatch):
    from web.server import server as S

    monkeypatch.setattr(ca, "is_setup", lambda *a, **k: True)
    monkeypatch.setattr(ca, "session_valid", lambda token: token == "ok")
    monkeypatch.setattr(S.DashboardHandler, "_onboarding_needed", lambda self: False)

    httpd, port = _boot(monkeypatch)
    try:
        head, _body = _request(port, "GET", "/plans.html", cookie="x5sess=ok")
        assert b"302" in head.split(b"\r\n")[0], head[:200]
        assert b"Location: /" in head, head[:400]
    finally:
        httpd.shutdown()
        httpd.server_close()
