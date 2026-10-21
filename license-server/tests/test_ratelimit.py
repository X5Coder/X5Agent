from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.ratelimit import RateLimiter, _match, default_rules, install_rate_limits


def test_limiter_allows_up_to_the_limit_then_blocks():
    limiter = RateLimiter()
    for _ in range(3):
        allowed, retry_after = limiter.allow("k", 3, 60.0, now=100.0)
        assert allowed is True
        assert retry_after == 0.0

    allowed, retry_after = limiter.allow("k", 3, 60.0, now=100.5)
    assert allowed is False
    assert 59.0 <= retry_after <= 60.0


def test_limiter_window_expires():
    limiter = RateLimiter()
    for _ in range(2):
        limiter.allow("k", 2, 10.0, now=1000.0)
    assert limiter.allow("k", 2, 10.0, now=1005.0)[0] is False
    assert limiter.allow("k", 2, 10.0, now=1010.5)[0] is True


def test_limiter_state_is_bounded():
    limiter = RateLimiter(max_keys=8)
    for index in range(200):
        assert limiter.allow(f"key-{index}", 5, 60.0, now=1.0)[0] is True
    assert limiter.size() <= 8


def test_rule_matching_prefers_exact_then_prefix():
    rules = (
        ("POST", "/v1/admin/login", 5, 60.0),
        ("*", "/v1/admin/", 120, 60.0),
    )
    assert _match("POST", "/v1/admin/login", rules) == rules[0]
    assert _match("GET", "/v1/admin/accounts", rules) == rules[1]
    assert _match("POST", "/v1/auth/login", rules) is None


def test_repair_mirror_is_bounded_by_default_rules():
    matched = _match("GET", "/mirror/" + "a" * 40 + "/project/launch.py", default_rules())
    assert matched is not None
    assert matched[2] == 120
    assert matched[3] == 60.0


def test_middleware_answers_429_with_retry_after():
    app = FastAPI()
    install_rate_limits(app, rules=(("POST", "/login", 2, 60.0),))

    @app.post("/login")
    def login() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.post("/login").status_code == 200
        assert client.post("/login").status_code == 200
        blocked = client.post("/login")
        assert blocked.status_code == 429
        assert blocked.json()["detail"] == "rate_limited"
        assert int(blocked.headers["Retry-After"]) >= 1


def test_middleware_leaves_unmatched_paths_alone():
    app = FastAPI()
    install_rate_limits(app, rules=(("POST", "/login", 1, 60.0),))

    @app.get("/v1/health")
    def health() -> dict:
        return {"ok": True}

    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/v1/health").status_code == 200
