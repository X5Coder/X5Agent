from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select, update

from app.db import SessionLocal
from app.models import Account, Subscription
from app.timeutils import utcnow

from helpers import (
    admin_headers,
    device_fingerprint,
    heartbeat,
    login,
    signup_and_login,
    unique_email,
)


def _account_id_from_email(client, email):
    headers = admin_headers(client)
    response = client.get(
        "/v1/admin/accounts", params={"search": email}, headers=headers
    )
    assert response.status_code == 200
    items = response.json()["items"]
    assert len(items) == 1
    return items[0]["id"], headers


def test_admin_warn_clear_extend(client):
    email, fingerprint, session = signup_and_login(client)
    account_id, headers = _account_id_from_email(client, email)

    stats = client.get("/v1/admin/stats", headers=headers)
    assert stats.status_code == 200
    assert stats.json()["accounts"] >= 1

    warned = client.post(
        f"/v1/admin/accounts/{account_id}/warn", headers=headers
    )
    assert warned.status_code == 200
    assert warned.json()["state"] == "warning"

    blocked = heartbeat(client, session["refresh_token"], fingerprint)
    assert blocked.status_code == 200
    body = blocked.json()
    assert body["state"] == "warning"
    assert body["can_work"] is False
    assert any(notice["kind"] == "warning" for notice in body["notices"])

    # Delivered notices are not re-sent.
    again = heartbeat(client, session["refresh_token"], fingerprint)
    assert again.status_code == 200
    assert again.json()["notices"] == []

    cleared = client.post(
        f"/v1/admin/accounts/{account_id}/clear", headers=headers
    )
    assert cleared.status_code == 200
    assert cleared.json()["state"] == "trial"

    after_clear = heartbeat(client, session["refresh_token"], fingerprint)
    assert after_clear.status_code == 200
    assert after_clear.json()["state"] == "trial"
    assert after_clear.json()["can_work"] is True

    before = after_clear.json()["remaining_seconds"]
    extended = client.post(
        f"/v1/admin/accounts/{account_id}/extend",
        json={"days": 10},
        headers=headers,
    )
    assert extended.status_code == 200

    after_extend = heartbeat(client, session["refresh_token"], fingerprint)
    assert after_extend.status_code == 200
    assert after_extend.json()["remaining_seconds"] > before + 9 * 24 * 3600

    suspended = client.post(
        f"/v1/admin/accounts/{account_id}/suspend", headers=headers
    )
    assert suspended.status_code == 200
    assert suspended.json()["state"] == "suspended"

    suspended_hb = heartbeat(client, session["refresh_token"], fingerprint)
    assert suspended_hb.json()["state"] == "suspended"
    assert suspended_hb.json()["can_work"] is False

    client.post(f"/v1/admin/accounts/{account_id}/clear", headers=headers)


def test_expiry_blocks_and_extend_recovers(client):
    email, fingerprint, session = signup_and_login(client)
    account_id, headers = _account_id_from_email(client, email)

    past = utcnow() - timedelta(days=1)
    with SessionLocal() as db:
        db.execute(
            update(Subscription)
            .where(Subscription.account_id == account_id)
            .values(ends_at=past)
        )
        db.commit()

    expired = heartbeat(client, session["refresh_token"], fingerprint)
    assert expired.status_code == 200
    body = expired.json()
    assert body["state"] == "expired"
    assert body["can_work"] is False
    assert any(notice["kind"] == "expired" for notice in body["notices"])

    extended = client.post(
        f"/v1/admin/accounts/{account_id}/extend",
        json={"days": 5},
        headers=headers,
    )
    assert extended.status_code == 200

    recovered = heartbeat(client, session["refresh_token"], fingerprint)
    assert recovered.status_code == 200
    assert recovered.json()["state"] == "trial"
    assert recovered.json()["can_work"] is True
    assert recovered.json()["remaining_seconds"] > 4 * 24 * 3600


def test_admin_requires_token(client):
    assert client.get("/v1/admin/accounts").status_code == 401
    assert (
        client.post(
            "/v1/admin/login",
            json={"email": "admin@example.com", "password": "WrongPass1"},
        ).status_code
        == 401
    )


def test_trial_plan_is_a_normal_plan_and_can_be_disabled(client):
    headers = admin_headers(client)
    plans = client.get("/v1/admin/plans", headers=headers).json()
    trial = next(plan for plan in plans if plan["is_trial"])
    assert trial["name"] == "Free Trial"

    try:
        disabled = client.patch(
            f"/v1/admin/plans/{trial['id']}",
            json={"active": False},
            headers=headers,
        )
        assert disabled.status_code == 200

        public_plans = client.get("/v1/plans").json()
        assert all(plan["id"] != trial["id"] for plan in public_plans)

        _, _, session = signup_and_login(client, unique_email("notrial"))
        assert session["state"] == "none"
        assert session["can_work"] is False
        assert session["plan"] is None
    finally:
        client.patch(
            f"/v1/admin/plans/{trial['id']}",
            json={"active": True},
            headers=headers,
        )


def test_account_detail_never_exposes_secrets(client):
    email, _, _ = signup_and_login(client)
    account_id, headers = _account_id_from_email(client, email)

    detail = client.get(
        f"/v1/admin/accounts/{account_id}", headers=headers
    )
    assert detail.status_code == 200
    text = detail.text.lower()
    # The panel may show WHICH password is stored (algorithm, prefix, length),
    # never the plaintext and never the whole hash or a session token.
    assert "password123" not in text
    with SessionLocal() as db:
        stored_hash = db.scalar(
            select(Account.password_hash).where(Account.id == account_id)
        )
    assert stored_hash
    assert stored_hash not in detail.text
    assert "token_hash" not in text
    assert "refresh_token" not in text
    body = detail.json()
    assert body["account"]["email"] == email
    assert body["account"]["password_algo"] == "argon2id"
    assert body["account"]["password_hash_length"] > 40
    assert len(body["account"]["password_hash_prefix"]) <= 30
    assert body["devices"]
    assert body["devices"][0]["details"] == {}
    assert body["current"]["state"] in {"trial", "active"}
    assert body["trial_used"] is True


def test_plan_delete_only_when_unused(client):
    headers = admin_headers(client)
    created = client.post(
        "/v1/admin/plans",
        json={
            "name": "Temp Plan",
            "duration_days": 14,
            "price_cents": 500,
            "currency": "usd",
        },
        headers=headers,
    )
    assert created.status_code == 201, created.text
    plan_id = created.json()["plan_id"]

    removed = client.delete(f"/v1/admin/plans/{plan_id}", headers=headers)
    assert removed.status_code == 200, removed.text
    names = [p["name"] for p in client.get("/v1/admin/plans", headers=headers).json()]
    assert "Temp Plan" not in names

    monthly = next(
        p for p in client.get("/v1/admin/plans", headers=headers).json()
        if p["name"] == "Pro Monthly"
    )
    _, fingerprint, session = signup_and_login(client)
    checkout = client.post(
        "/v1/billing/checkout",
        json={"plan_id": monthly["id"], "device_fingerprint": fingerprint},
        headers={"Authorization": f"Bearer {session['refresh_token']}"},
    )
    assert checkout.status_code == 200, checkout.text
    client.post(
        "/v1/sandbox/pay",
        json={"session_id": checkout.json()["session_id"]},
        headers={"X-Sandbox-Secret": "test-sandbox-secret"},
    )
    blocked = client.delete(f"/v1/admin/plans/{monthly['id']}", headers=headers)
    assert blocked.status_code == 409, blocked.text
    assert blocked.json()["detail"] == "plan_in_use"

    missing = client.delete("/v1/admin/plans/nope", headers=headers)
    assert missing.status_code == 404
