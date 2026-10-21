from __future__ import annotations

import hashlib
import uuid

from fastapi.testclient import TestClient


def device_fingerprint(seed: str | None = None) -> str:
    return hashlib.sha256((seed or uuid.uuid4().hex).encode()).hexdigest()


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}@example.com"


def register(
    client: TestClient,
    email: str,
    password: str = "Password123",
    fingerprint: str | None = None,
) -> dict:
    response = client.post(
        "/v1/auth/register",
        json={
            "email": email,
            "password": password,
            "device_fingerprint": fingerprint or device_fingerprint(email),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def login(
    client: TestClient,
    email: str,
    password: str = "Password123",
    fingerprint: str | None = None,
) -> dict:
    response = client.post(
        "/v1/auth/login",
        json={
            "email": email,
            "password": password,
            "device_fingerprint": fingerprint or device_fingerprint(email),
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def signup_and_login(
    client: TestClient, email: str | None = None
) -> tuple[str, str, dict]:
    email = email or unique_email()
    fingerprint = device_fingerprint(email)
    register(client, email, fingerprint=fingerprint)
    session = login(client, email, fingerprint=fingerprint)
    return email, fingerprint, session


def heartbeat(
    client: TestClient, refresh_token: str, fingerprint: str
):
    return client.post(
        "/v1/heartbeat",
        json={"device_fingerprint": fingerprint},
        headers={"Authorization": f"Bearer {refresh_token}"},
    )


def admin_headers(client: TestClient) -> dict:
    response = client.post(
        "/v1/admin/login",
        json={"email": "admin@example.com", "password": "AdminPass123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}
