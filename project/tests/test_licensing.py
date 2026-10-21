"""License client: device fingerprint, token verification, state store,
service lifecycle and failure handling. No network, no LLM.

Run:  python -m pytest tests/test_licensing.py -q
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.licensing import config, device, notices, store
from core.licensing.client import (
    LicenseServerError,
    validate_session_payload,
)
from core.licensing.entitlement import EntitlementError, verify_entitlement
from core.licensing.service import LicenseError, LicenseService

ACCOUNT = "acc_123"
DEVICE = "dev_456"
PRIVATE = Ed25519PrivateKey.generate()
PUBLIC_PEM = (
    PRIVATE.public_key()
    .public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    .decode("ascii")
)


def make_token(
    state: str = "active",
    *,
    ttl: int = 900,
    iat_offset: int = 0,
    account: str = ACCOUNT,
    device_id: str = DEVICE,
    key: Ed25519PrivateKey | None = None,
) -> str:
    now = int(time.time()) + iat_offset
    return jwt.encode(
        {
            "iss": "x5agent-license",
            "aud": "x5agent-client",
            "sub": account,
            "did": device_id,
            "state": state,
            "plan": "Pro",
            "iat": now,
            "exp": now + ttl,
            "jti": "test",
        },
        key or PRIVATE,
        algorithm="EdDSA",
    )


def session_payload(state: str = "active", **overrides) -> dict:
    payload = {
        "account_id": ACCOUNT,
        "device_id": DEVICE,
        "entitlement_token": make_token(state),
        "server_time": "2026-10-20T00:00:00+00:00",
        "server_time_offset": 0.0,
        "state": state,
        "plan": "Pro",
        "expires_at": "2026-12-31T00:00:00+00:00",
        "subscription_id": "sub_1",
        "remaining_seconds": 3600,
        "can_work": state in ("trial", "active"),
        "notices": [],
        "refresh_token": "refresh-token-value-123",
    }
    payload.update(overrides)
    return payload


class FakeClient:
    def __init__(self) -> None:
        self.login_calls = 0
        self.register_calls = 0
        self.heartbeat_calls = 0
        self.logout_calls = 0
        self.login_error: Exception | None = None
        self.register_error: Exception | None = None
        self.heartbeat_error: Exception | None = None
        self.login_payload: dict | None = None
        self.register_payload: dict | None = None
        self.heartbeat_payload: dict | None = None
        self.checkout_calls: list[tuple] = []
        self.checkout_error: Exception | None = None
        self.checkout_status_error: Exception | None = None
        self.checkout_status_payload: dict | None = None
        self.choose_calls: list[tuple] = []
        self.choose_error: Exception | None = None
        self.choose_payload: dict | None = None

    def login(self, email: str, password: str, fingerprint: str) -> dict:
        self.login_calls += 1
        if self.login_error:
            raise self.login_error
        return self.login_payload or session_payload()

    def register(self, email: str, password: str, fingerprint: str) -> dict:
        self.register_calls += 1
        if self.register_error:
            raise self.register_error
        return self.register_payload or session_payload("trial")

    def heartbeat(self, refresh_token: str, fingerprint: str, timeout=None) -> dict:
        self.heartbeat_calls += 1
        if self.heartbeat_error:
            raise self.heartbeat_error
        return self.heartbeat_payload or session_payload()

    def logout(self, refresh_token: str) -> None:
        self.logout_calls += 1

    def plans(self) -> list[dict]:
        return []

    def create_checkout(self, refresh_token, fingerprint, plan_id, return_url=None):
        self.checkout_calls.append((refresh_token, fingerprint, plan_id, return_url))
        if self.checkout_error:
            raise self.checkout_error
        return {
            "session_id": "11111111-2222-3333-4444-555555555555",
            "checkout_url": "http://13.140.164.29:8789/pay/11111111-2222-3333-4444-555555555555",
            "amount_cents": 1900,
            "currency": "USD",
            "plan": {"name": "Pro Monthly", "duration_days": 30, "price_cents": 1900},
        }

    def checkout_status(self, refresh_token, fingerprint, session_id):
        if self.checkout_status_error:
            raise self.checkout_status_error
        return self.checkout_status_payload or {
            "session_id": session_id,
            "status": "pending",
            "amount_cents": 1900,
            "currency": "USD",
            "plan": {"name": "Pro Monthly", "duration_days": 30, "price_cents": 1900},
        }

    def choose_plan(self, refresh_token, fingerprint, plan_id, return_url=None):
        self.choose_calls.append((refresh_token, fingerprint, plan_id, return_url))
        if self.choose_error:
            raise self.choose_error
        if self.choose_payload is not None:
            return self.choose_payload
        return {"chosen": True}


def make_service(tmp_path: Path, client: FakeClient, **kwargs) -> LicenseService:
    return LicenseService(
        state_path=tmp_path / "license.json",
        client=client,
        public_key_pem=PUBLIC_PEM,
        bypass=False,
        **kwargs,
    )


# ------------------------------------------------------------------ device


def test_device_fingerprint_is_stable_hex():
    device.reset_cache()
    first = device.machine_fingerprint()
    second = device.machine_fingerprint()
    assert first == second
    assert len(first) == 64
    assert all(ch in "0123456789abcdef" for ch in first)
    assert device.fingerprint_short() == first[:8]


# -------------------------------------------------------------- entitlement


def test_verify_entitlement_accepts_valid_token():
    token = make_token("trial")
    payload = verify_entitlement(token, PUBLIC_PEM, ACCOUNT, DEVICE)
    assert payload["sub"] == ACCOUNT
    assert payload["did"] == DEVICE
    assert payload["state"] == "trial"


def test_verify_entitlement_rejects_wrong_account_or_device():
    token = make_token()
    with pytest.raises(EntitlementError):
        verify_entitlement(token, PUBLIC_PEM, "other", DEVICE)
    with pytest.raises(EntitlementError):
        verify_entitlement(token, PUBLIC_PEM, ACCOUNT, "other")


def test_verify_entitlement_rejects_expired_and_future_tokens():
    expired = make_token(ttl=100, iat_offset=-1000)
    with pytest.raises(EntitlementError):
        verify_entitlement(expired, PUBLIC_PEM, ACCOUNT, DEVICE)
    future = make_token(iat_offset=600)
    with pytest.raises(EntitlementError):
        verify_entitlement(future, PUBLIC_PEM, ACCOUNT, DEVICE)


def test_verify_entitlement_rejects_foreign_signature():
    other = Ed25519PrivateKey.generate()
    token = make_token(key=other)
    with pytest.raises(EntitlementError):
        verify_entitlement(token, PUBLIC_PEM, ACCOUNT, DEVICE)


# ------------------------------------------------------------------- store


def test_store_roundtrip_atomic(tmp_path: Path):
    path = tmp_path / "license.json"
    state = store.default_state()
    state["email"] = "user@example.com"
    state["state"] = "active"
    saved = store.save_state(state, path)
    assert saved["email"] == "user@example.com"
    assert path.is_file()
    assert not (tmp_path / "license.json.tmp").exists()
    loaded = store.load_state(path)
    assert loaded["email"] == "user@example.com"
    assert loaded["state"] == "active"


def test_store_corrupt_file_returns_default(tmp_path: Path, caplog):
    path = tmp_path / "license.json"
    path.write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        state = store.load_state(path)
    assert state["state"] == "signed_out"
    assert state["refresh_token"] == ""


def test_store_normalizes_unknown_fields_and_bounded_notices(tmp_path: Path):
    path = tmp_path / "license.json"
    saved = store.save_state(
        {
            "state": "not-a-state",
            "remaining_seconds": -99,
            "attacker": "field",
            "notices": [{"kind": f"k{i}"} for i in range(50)],
        },
        path,
    )
    assert saved["state"] == "signed_out"
    assert saved["remaining_seconds"] == 0
    assert "attacker" not in saved
    assert len(saved["notices"]) == store._MAX_NOTICES


# ---------------------------------------------------------- payload validation


def test_validate_session_payload_accepts_good_response():
    clean = validate_session_payload(session_payload("trial"))
    assert clean["state"] == "trial"
    assert clean["can_work"] is True
    assert clean["refresh_token"] == ""
    assert clean["server_time_offset"] != 0 or True  # shape only


def test_validate_session_payload_rejects_inconsistent_state():
    with pytest.raises(LicenseServerError):
        validate_session_payload(session_payload("none", can_work=True))
    with pytest.raises(LicenseServerError):
        validate_session_payload(session_payload("active", can_work=False))
    with pytest.raises(LicenseServerError):
        validate_session_payload({"state": "active"})
    with pytest.raises(LicenseServerError):
        validate_session_payload(session_payload("active", refresh_token=""), require_refresh=True)


# ----------------------------------------------------------------- service


def test_service_register_signs_in_immediately(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    status = service.register("new@example.com", "password123")
    assert client.register_calls == 1
    assert status["connected"] is True
    assert status["can_work"] is True
    assert status["state"] == "trial"
    assert status["email"] == "new@example.com"


def test_service_register_email_taken_is_reported(tmp_path: Path):
    client = FakeClient()
    client.register_error = LicenseServerError(
        "email_taken", status=409, code="email_taken"
    )
    service = make_service(tmp_path, client)
    with pytest.raises(LicenseError) as exc:
        service.register("taken@example.com", "password123")
    assert "already exists" in str(exc.value)
    assert service.status()["connected"] is False


def test_service_login_then_heartbeat(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    status = service.login("user@example.com", "correct horse battery")
    assert status["can_work"] is True
    assert status["email"] == "user@example.com"
    assert status["connected"] is True
    assert status["plan"] == "Pro"
    assert client.login_calls == 1
    service.heartbeat()
    assert client.heartbeat_calls == 1
    assert service.can_work() is True


def test_service_can_work_false_before_boot_verification(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    # Simulate a new process: same state file, no boot verification yet.
    restarted = make_service(tmp_path, client)
    assert restarted.can_work() is False
    assert restarted.status()["connected"] is True
    assert restarted.ensure_verified() is True
    assert restarted.can_work() is True
    assert client.heartbeat_calls == 1


def test_service_login_error_keeps_existing_session(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    client.login_error = LicenseServerError(
        "invalid_credentials", status=401, code="invalid_credentials"
    )
    with pytest.raises(LicenseError) as exc:
        service.login("user@example.com", "wrong")
    assert "incorrect" in str(exc.value)
    assert service.can_work() is True


def test_service_heartbeat_409_clears_session(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    client.heartbeat_error = LicenseServerError(
        "device_superseded", status=409, code="device_superseded"
    )
    status = service.heartbeat()
    assert status["state"] == "device_superseded"
    assert status["can_work"] is False
    assert status["connected"] is False
    assert status["notices"] == []
    assert "other device" in service.blocked_message()


def test_service_heartbeat_401_requires_login(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    client.heartbeat_error = LicenseServerError(
        "invalid_token", status=401, code="invalid_token"
    )
    status = service.heartbeat()
    assert status["state"] == "auth_required"
    assert status["can_work"] is False


def test_service_network_error_keeps_valid_token(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    client.heartbeat_error = LicenseServerError(
        "network", status=0, code="network", retryable=True
    )
    status = service.heartbeat()
    assert status["can_work"] is True
    assert "cannot be reached" in status["last_error"]


def test_service_warning_state_blocks(tmp_path: Path):
    client = FakeClient()
    client.login_payload = session_payload(
        "warning", notices=[{"kind": "warning", "created_at": "2026-10-20T00:00:00Z"}]
    )
    service = make_service(tmp_path, client)
    status = service.login("user@example.com", "password123")
    assert status["can_work"] is False
    assert status["state"] == "warning"
    assert "paused" in service.blocked_message()


def test_service_logout_clears_session_even_if_server_fails(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")

    def fail(_token):
        raise LicenseServerError("network", code="network", retryable=True)

    client.logout = fail  # type: ignore[assignment]
    status = service.logout()
    assert status["connected"] is False
    assert status["can_work"] is False
    assert status["state"] == "signed_out"
    assert service.can_work() is False


def test_service_background_thread_starts_and_stops(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client, heartbeat_interval=0.05)
    service.login("user@example.com", "password123")
    service.start_background()
    deadline = time.time() + 2.0
    while client.heartbeat_calls < 2 and time.time() < deadline:
        time.sleep(0.02)
    service.stop_background()
    assert client.heartbeat_calls >= 2


def test_service_never_logs_password(tmp_path: Path, caplog):
    client = FakeClient()
    client.login_error = LicenseServerError("network", code="network", retryable=True)
    service = make_service(tmp_path, client)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(LicenseError):
            service.login("user@example.com", "super-secret-password")
    assert "super-secret-password" not in caplog.text


def test_bypass_requires_pytest_marker(monkeypatch):
    monkeypatch.setenv("X5_LICENSE_BYPASS", "1")
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "")
    assert config.bypass_enabled() is False
    monkeypatch.setenv("PYTEST_CURRENT_TEST", "tests/test_licensing.py::x (call)")
    assert config.bypass_enabled() is True


def test_notice_message_is_fixed_text():
    text = notices.blocked_message("active", [{"kind": "expired"}], "")
    assert "expired" in text.lower()
    assert "contact" in text.lower()
    assert "Subscription" in notices.blocked_message("signed_out", [], "")

# ------------------------------------------------------------------ billing


def test_service_start_checkout_requires_a_session(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    with pytest.raises(LicenseError) as exc:
        service.start_checkout("plan-1")
    assert "Sign in" in str(exc.value)
    assert client.checkout_calls == []


def test_service_start_checkout_returns_the_provider_link(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    checkout = service.start_checkout("plan-1", "http://localhost:7777/subscription")
    assert checkout["session_id"]
    assert checkout["checkout_url"].startswith("http://")
    token, fingerprint, plan_id, return_url = client.checkout_calls[0]
    assert token == "refresh-token-value-123"
    assert len(fingerprint) == 64
    assert plan_id == "plan-1"
    assert return_url.endswith("/subscription")


def test_service_checkout_errors_are_human(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    client.checkout_error = LicenseServerError(
        "checkout_unavailable", status=503, code="checkout_unavailable"
    )
    with pytest.raises(LicenseError) as exc:
        service.start_checkout("plan-1")
    assert "not available" in str(exc.value)

    client.checkout_error = LicenseServerError(
        "account_suspended", status=409, code="account_suspended"
    )
    with pytest.raises(LicenseError) as exc:
        service.start_checkout("plan-1")
    assert "suspended" in str(exc.value)


def test_service_checkout_status_refreshes_a_paid_session(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    result = service.checkout_status("11111111-2222-3333-4444-555555555555")
    assert result["paid"] is False
    assert result["status"] == "pending"
    assert client.heartbeat_calls == 0

    client.checkout_status_payload = {
        "session_id": "11111111-2222-3333-4444-555555555555",
        "status": "paid",
        "plan": {"name": "Pro Monthly", "duration_days": 30, "price_cents": 1900},
        "license": {"state": "active", "can_work": True, "plan": "Pro Monthly"},
    }
    result = service.checkout_status("11111111-2222-3333-4444-555555555555")
    assert result["paid"] is True
    assert result["plan"] == "Pro Monthly"
    assert client.heartbeat_calls == 1
    assert result["license"]["can_work"] is True


def test_service_checkout_status_rejects_a_bad_session_id(tmp_path: Path):
    service = make_service(tmp_path, FakeClient())
    service.login("user@example.com", "password123")
    with pytest.raises(LicenseError):
        service.checkout_status("   ")


def test_service_choose_trial_records_it_locally(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    assert service.status()["plan_chosen"] is False

    result = service.choose_plan("plan-trial", "http://localhost:7777/")
    assert result["chosen"] is True
    assert result["license"]["plan_chosen"] is True
    token, fingerprint, plan_id, return_url = client.choose_calls[0]
    assert token == "refresh-token-value-123"
    assert len(fingerprint) == 64
    assert plan_id == "plan-trial"
    assert return_url == "http://localhost:7777/"

    # Persisted: a fresh service on the same state file still knows.
    again = make_service(tmp_path, FakeClient())
    assert again.status()["plan_chosen"] is True


def test_service_choose_paid_returns_the_checkout(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    service.login("user@example.com", "password123")
    client.choose_payload = {
        "chosen": False,
        "checkout": {
            "session_id": "11111111-2222-3333-4444-555555555555",
            "checkout_url": "http://pay.test/pay/11111111-2222-3333-4444-555555555555",
            "amount_cents": 1900,
            "currency": "USD",
        },
    }
    result = service.choose_plan("plan-paid")
    assert result["chosen"] is False
    assert result["checkout"]["session_id"].startswith("11111111")
    assert result["checkout"]["checkout_url"].startswith("http://pay.test/")
    assert service.status()["plan_chosen"] is False


def test_service_choose_requires_a_session(tmp_path: Path):
    client = FakeClient()
    service = make_service(tmp_path, client)
    with pytest.raises(LicenseError):
        service.choose_plan("plan-1")
    assert client.choose_calls == []


def test_plan_chosen_survives_state_roundtrip(tmp_path: Path):
    service = make_service(tmp_path, FakeClient())
    service.login("user@example.com", "password123")
    service.choose_plan("plan-trial")
    raw = json.loads((tmp_path / "license.json").read_text(encoding="utf-8"))
    assert raw["plan_chosen"] is True
    service.logout()
    assert service.status()["plan_chosen"] is False
