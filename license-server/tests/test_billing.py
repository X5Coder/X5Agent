from __future__ import annotations

from datetime import timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.db import SessionLocal
from app.models import CheckoutSession, Plan, Subscription
from app.timeutils import utcnow

from helpers import device_fingerprint, heartbeat, signup_and_login, unique_email

SANDBOX_HEADERS = {"X-Sandbox-Secret": "test-sandbox-secret"}


def _plans(client: TestClient) -> dict[str, dict]:
    response = client.get("/v1/plans")
    assert response.status_code == 200, response.text
    return {plan["name"]: plan for plan in response.json()}


def _checkout(
    client: TestClient,
    token: str,
    fingerprint: str,
    plan_id: str,
    return_url: str | None = None,
) -> dict:
    payload = {"plan_id": plan_id, "device_fingerprint": fingerprint}
    if return_url:
        payload["return_url"] = return_url
    response = client.post(
        "/v1/billing/checkout",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_seeded_plans_are_sellable(client: TestClient) -> None:
    plans = _plans(client)
    assert "Free Trial" in plans and plans["Free Trial"]["is_trial"] is True
    paid = [p for p in plans.values() if not p["is_trial"]]
    assert {p["name"] for p in paid} >= {"Pro Monthly", "Pro Yearly"}
    for plan in paid:
        assert plan["price_cents"] > 0
        assert plan["duration_days"] >= 30


def test_checkout_requires_a_session(client: TestClient) -> None:
    response = client.post(
        "/v1/billing/checkout",
        json={"plan_id": "x", "device_fingerprint": "a" * 64},
    )
    assert response.status_code == 401


def test_trial_plan_cannot_be_bought(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    plans = _plans(client)
    response = client.post(
        "/v1/billing/checkout",
        json={
            "plan_id": plans["Free Trial"]["id"],
            "device_fingerprint": fingerprint,
        },
        headers={"Authorization": f"Bearer {session['refresh_token']}"},
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "plan_unavailable"


def test_sandbox_purchase_activates_the_plan(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    token = session["refresh_token"]
    plan = _plans(client)["Pro Monthly"]

    created = _checkout(client, token, fingerprint, plan["id"])
    assert created["checkout_url"].startswith("http://sandbox.test:8789/pay/")
    assert created["checkout_url"].endswith(created["session_id"])
    assert created["amount_cents"] == plan["price_cents"]
    # The sandbox has no card step: the session is already paid and active.
    assert created["instant"] is True
    assert created["status"] == "paid"

    view = client.get(
        f"/v1/sandbox/session/{created['session_id']}", headers=SANDBOX_HEADERS
    )
    assert view.status_code == 200, view.text
    assert view.json()["status"] == "paid"
    assert view.json()["plan"]["name"] == "Pro Monthly"

    paid = client.post(
        "/v1/sandbox/pay",
        json={"session_id": created["session_id"]},
        headers=SANDBOX_HEADERS,
    )
    assert paid.status_code == 200, paid.text
    body = paid.json()
    assert body["status"] == "paid"
    assert body["subscription_id"]

    status = client.post(
        "/v1/billing/checkout/status",
        json={"session_id": created["session_id"], "device_fingerprint": fingerprint},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert status.status_code == 200, status.text
    license_state = status.json()["license"]
    assert license_state["state"] == "active"
    assert license_state["plan"] == "Pro Monthly"
    assert license_state["can_work"] is True
    assert 29 <= license_state["remaining_seconds"] // 86400 <= 30

    beat = heartbeat(client, token, fingerprint)
    assert beat.status_code == 200, beat.text
    assert beat.json()["state"] == "active"

    # Paying twice never creates a second live subscription.
    again = client.post(
        "/v1/sandbox/pay",
        json={"session_id": created["session_id"]},
        headers=SANDBOX_HEADERS,
    )
    assert again.status_code == 200
    assert again.json()["subscription_id"] == body["subscription_id"]
    with SessionLocal() as db:
        live = db.scalars(
            select(Subscription).where(
                Subscription.account_id == session["account_id"],
                Subscription.state == "active",
            )
        ).all()
    assert len(live) == 1


def test_sandbox_requires_its_secret(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    plan = _plans(client)["Pro Yearly"]
    created = _checkout(client, session["refresh_token"], fingerprint, plan["id"])

    assert client.post(
        "/v1/sandbox/pay", json={"session_id": created["session_id"]}
    ).status_code == 403
    assert client.post(
        "/v1/sandbox/pay",
        json={"session_id": created["session_id"]},
        headers={"X-Sandbox-Secret": "wrong"},
    ).status_code == 403
    assert client.get(
        f"/v1/sandbox/session/{created['session_id']}"
    ).status_code == 403

    # A buyer cannot change the session through the console API: sandbox
    # purchases are completed by the server, and the poll only reads them.
    forged = client.post(
        "/v1/billing/checkout/status",
        json={"session_id": created["session_id"], "device_fingerprint": fingerprint},
        headers={"Authorization": f"Bearer {session['refresh_token']}"},
    )
    assert forged.status_code == 200
    assert forged.json()["status"] == "paid"


def test_expired_and_foreign_sessions_are_refused(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    plan = _plans(client)["Pro Monthly"]
    created = _checkout(client, session["refresh_token"], fingerprint, plan["id"])

    with SessionLocal() as db:
        row = db.get(CheckoutSession, created["session_id"])
        assert row is not None
        # The sandbox completes at creation; put it back to pending so this
        # test exercises the expiry guard of the pay endpoint itself.
        row.status = "pending"
        row.expires_at = utcnow() - timedelta(minutes=1)
        db.commit()

    paid = client.post(
        "/v1/sandbox/pay",
        json={"session_id": created["session_id"]},
        headers=SANDBOX_HEADERS,
    )
    assert paid.status_code == 409
    assert paid.json()["detail"] == "checkout_expired"

    # Another account can never poll this session.
    _other_email, other_fingerprint, other = signup_and_login(client)
    fresh = _checkout(client, session["refresh_token"], fingerprint, plan["id"])
    foreign = client.post(
        "/v1/billing/checkout/status",
        json={"session_id": fresh["session_id"], "device_fingerprint": other_fingerprint},
        headers={"Authorization": f"Bearer {other['refresh_token']}"},
    )
    assert foreign.status_code == 404


def test_return_url_must_be_http_and_suspended_cannot_buy(
    client: TestClient,
) -> None:
    _email, fingerprint, session = signup_and_login(client)
    token = session["refresh_token"]
    plan = _plans(client)["Pro Monthly"]

    bad = client.post(
        "/v1/billing/checkout",
        json={
            "plan_id": plan["id"],
            "device_fingerprint": fingerprint,
            "return_url": "javascript:alert(1)",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    assert bad.status_code == 422

    good = _checkout(
        client, token, fingerprint, plan["id"], "http://localhost:7777/subscription"
    )
    assert good["session_id"]

    with SessionLocal() as db:
        live = db.scalars(
            select(Subscription).where(
                Subscription.account_id == session["account_id"],
                Subscription.state.in_(("trial", "active")),
            )
        ).first()
        assert live is not None
        live.state = "suspended"
        live.state_updated_at = utcnow()
        db.commit()

    blocked = client.post(
        "/v1/billing/checkout",
        json={"plan_id": plan["id"], "device_fingerprint": fingerprint},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "account_suspended"


def test_paid_plan_replaces_a_running_trial(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    token = session["refresh_token"]
    plan = _plans(client)["Pro Monthly"]
    created = _checkout(client, token, fingerprint, plan["id"])
    client.post(
        "/v1/sandbox/pay",
        json={"session_id": created["session_id"]},
        headers=SANDBOX_HEADERS,
    )
    with SessionLocal() as db:
        rows = db.scalars(
            select(Subscription)
            .where(Subscription.account_id == session["account_id"])
            .order_by(Subscription.ends_at.desc())
        ).all()
        plans = {
            db.get(Plan, row.plan_id).name: row for row in rows if db.get(Plan, row.plan_id)
        }
    assert plans["Pro Monthly"].state == "active"
    assert plans["Free Trial"].state == "ended"
    assert plans["Free Trial"].ends_at <= plans["Pro Monthly"].ends_at


def test_checkout_rejects_unknown_plan(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    response = client.post(
        "/v1/billing/checkout",
        json={"plan_id": "does-not-exist", "device_fingerprint": fingerprint},
        headers={"Authorization": f"Bearer {session['refresh_token']}"},
    )
    assert response.status_code == 404


def test_buying_the_same_plan_again_extends_it(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    token = session["refresh_token"]
    plan = _plans(client)["Pro Monthly"]
    first = _checkout(client, token, fingerprint, plan["id"])
    client.post(
        "/v1/sandbox/pay",
        json={"session_id": first["session_id"]},
        headers=SANDBOX_HEADERS,
    )
    second = _checkout(client, token, fingerprint, plan["id"])
    client.post(
        "/v1/sandbox/pay",
        json={"session_id": second["session_id"]},
        headers=SANDBOX_HEADERS,
    )
    with SessionLocal() as db:
        row = db.scalars(
            select(Subscription).where(
                Subscription.account_id == session["account_id"],
                Subscription.state == "active",
            )
        ).one()
        days = (row.ends_at - utcnow()).total_seconds() / 86400
    assert 55 < days < 65, days


def test_invalid_return_url_is_rejected(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    plan = _plans(client)["Pro Monthly"]
    response = client.post(
        "/v1/billing/checkout",
        json={
            "plan_id": plan["id"],
            "device_fingerprint": fingerprint,
            "return_url": "javascript:alert(1)",
        },
        headers={"Authorization": f"Bearer {session['refresh_token']}"},
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == "bad_return_url"


# --------------------------------------------------- onboarding: choose a plan


def _choose(client: TestClient, token: str, fingerprint: str, plan_id: str,
            return_url: str | None = None):
    payload = {"plan_id": plan_id, "device_fingerprint": fingerprint}
    if return_url:
        payload["return_url"] = return_url
    return client.post(
        "/v1/billing/choose",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )


def test_choose_requires_a_session(client: TestClient) -> None:
    response = client.post(
        "/v1/billing/choose",
        json={"plan_id": "x", "device_fingerprint": "a" * 64},
    )
    assert response.status_code == 401


def test_choose_free_trial_records_the_choice(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    token = session["refresh_token"]
    assert session["plan_chosen"] is False

    plan = _plans(client)["Free Trial"]
    response = _choose(client, token, fingerprint, plan["id"])
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["chosen"] is True
    assert body["license"]["state"] == "trial"
    assert body["license"]["can_work"] is True
    assert body["license"]["plan_chosen"] is True

    beat = heartbeat(client, token, fingerprint)
    assert beat.status_code == 200, beat.text
    assert beat.json()["plan_chosen"] is True

    # Idempotent: choosing the trial again never restarts or duplicates it.
    again = _choose(client, token, fingerprint, plan["id"])
    assert again.status_code == 200, again.text
    assert again.json()["chosen"] is True
    with SessionLocal() as db:
        rows = db.scalars(
            select(Subscription).where(Subscription.account_id == session["account_id"])
        ).all()
    assert len(rows) == 1


def test_choose_paid_opens_a_checkout_then_completion_records_it(
    client: TestClient,
) -> None:
    _email, fingerprint, session = signup_and_login(client)
    token = session["refresh_token"]
    plan = _plans(client)["Pro Monthly"]

    response = _choose(client, token, fingerprint, plan["id"],
                       return_url="http://console.test/")
    assert response.status_code == 200, response.text
    body = response.json()
    # Sandbox has no hosted checkout: the choice and the purchase land at once.
    assert body["chosen"] is True
    assert body["instant"] is True
    assert body["subscription_id"]
    assert body["license"]["state"] == "active"
    assert body["license"]["plan_chosen"] is True
    assert body["license"]["plan"] == "Pro Monthly"


def test_choose_rejects_an_unknown_plan(client: TestClient) -> None:
    _email, fingerprint, session = signup_and_login(client)
    response = _choose(client, session["refresh_token"], fingerprint, "nope")
    assert response.status_code == 404
    assert response.json()["detail"] == "plan_unavailable"
