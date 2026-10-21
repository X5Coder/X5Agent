from __future__ import annotations

import os
import stat

import pytest

from app.config import settings

from helpers import (
    device_fingerprint,
    heartbeat,
    login,
    signup_and_login,
    unique_email,
)


def test_health(client):
    response = client.get("/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "x5agent-license"


def test_entitlement_private_key_permissions(client):
    if os.name != "posix":
        pytest.skip("POSIX permission check")
    private_path = settings.keys_dir / "entitlement_private.pem"
    assert stat.S_IMODE(private_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(settings.keys_dir.stat().st_mode) == 0o750


def test_register_activates_account_and_starts_trial(client):
    email = unique_email()
    fingerprint = device_fingerprint(email)
    response = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": "Password123",
            "device_fingerprint": fingerprint,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["state"] == "trial"
    assert body["can_work"] is True
    assert body["plan"] == "Free Trial"
    assert body["refresh_token"]
    assert body["entitlement_token"]
    assert body["account_id"]
    assert body["device_id"]

    # The same credentials log in right away: no email step exists.
    session = client.post(
        "/v1/auth/login",
        json={
            "email": email,
            "password": "Password123",
            "device_fingerprint": fingerprint,
        },
    )
    assert session.status_code == 200
    assert session.json()["state"] == "trial"


def test_verify_email_endpoint_is_gone(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert "/v1/auth/verify-email" not in paths


def test_login_session_and_entitlement(client):
    import jwt

    _, _, body = signup_and_login(client)
    assert body["state"] == "trial"
    assert body["can_work"] is True
    assert body["plan"] == "Free Trial"
    assert 6 * 24 * 3600 < body["remaining_seconds"] <= 7 * 24 * 3600
    assert body["refresh_token"]

    public_key = client.get("/v1/public-key").json()["public_key_pem"]
    claims = jwt.decode(
        body["entitlement_token"],
        public_key,
        algorithms=["EdDSA"],
        audience="x5agent-client",
        issuer="x5agent-license",
    )
    assert claims["sub"] == body["account_id"]
    assert claims["did"] == body["device_id"]
    assert claims["state"] == "trial"
    assert claims["exp"] > claims["iat"]


def test_registration_validation(client):
    email = unique_email()
    fingerprint = device_fingerprint(email)

    first = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": "Password123",
            "device_fingerprint": fingerprint,
        },
    )
    assert first.status_code == 201

    duplicate = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": "Password123",
            "device_fingerprint": fingerprint,
        },
    )
    assert duplicate.status_code == 409

    bad_email = client.post(
        "/v1/auth/register",
        json={
            "email": "not-an-email",
            "password": "Password123",
            "device_fingerprint": fingerprint,
        },
    )
    assert bad_email.status_code == 422

    short_password = client.post(
        "/v1/auth/register",
        json={
            "email": unique_email(),
            "password": "short",
            "device_fingerprint": fingerprint,
        },
    )
    assert short_password.status_code == 422

    wrong_password = client.post(
        "/v1/auth/login",
        json={
            "email": email,
            "password": "WrongPassword1",
            "device_fingerprint": fingerprint,
        },
    )
    assert wrong_password.status_code == 401


def test_heartbeat_requires_valid_token_and_device(client):
    _, fingerprint, session = signup_and_login(client)

    missing = client.post(
        "/v1/heartbeat",
        json={"device_fingerprint": fingerprint},
    )
    assert missing.status_code == 401

    wrong_device = heartbeat(client, session["refresh_token"], "f" * 64)
    assert wrong_device.status_code == 409
    assert wrong_device.json()["detail"] == "device_superseded"

    ok = heartbeat(client, session["refresh_token"], fingerprint)
    assert ok.status_code == 200
    body = ok.json()
    assert body["state"] == "trial"
    assert body["can_work"] is True
    assert body["entitlement_token"]
    assert body["server_time"]


def test_last_device_wins(client):
    email, fingerprint_one, first_session = signup_and_login(client)
    fingerprint_two = device_fingerprint("second-device-" + email)

    second_session = login(client, email, fingerprint=fingerprint_two)

    old_device = heartbeat(
        client, first_session["refresh_token"], fingerprint_one
    )
    assert old_device.status_code == 409
    assert old_device.json()["detail"] == "device_superseded"

    new_device = heartbeat(
        client, second_session["refresh_token"], fingerprint_two
    )
    assert new_device.status_code == 200
    assert new_device.json()["state"] == "trial"

    # Switching back wins again: the newest device is the only active one.
    back = login(client, email, fingerprint=fingerprint_one)
    assert (
        heartbeat(client, back["refresh_token"], fingerprint_one).status_code
        == 200
    )
    assert (
        heartbeat(
            client, second_session["refresh_token"], fingerprint_two
        ).status_code
        == 409
    )
