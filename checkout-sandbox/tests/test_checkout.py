from __future__ import annotations

import app as checkout
from fastapi.testclient import TestClient

client = TestClient(checkout.app)

SESSION = "11111111-2222-3333-4444-555555555555"


def session_payload(status: str = "pending", name: str = "Pro Monthly", cents: int = 1900) -> dict:
    return {
        "session_id": SESSION,
        "status": status,
        "amount_cents": cents,
        "currency": "USD",
        "plan": {
            "id": "plan-1",
            "name": name,
            "duration_days": 30,
            "price_cents": cents,
            "currency": "USD",
        },
        "return_url": "http://localhost:7777/subscription",
        "expires_at": "2026-01-01T00:00:00Z",
        "completed_at": None,
        "subscription_id": "sub-1" if status == "paid" else None,
    }


def test_health_reports_ready() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_pending_page_shows_plan_and_price(monkeypatch) -> None:
    monkeypatch.setattr(checkout, "fetch_session", lambda session_id: session_payload())
    response = client.get(f"/pay/{SESSION}")
    assert response.status_code == 200
    body = response.text
    assert "Pro Monthly" in body
    assert "19 USD" in body
    assert "30 days" in body
    assert f'action="/pay/{SESSION}"' in body
    assert "unit-test-secret" not in body


def test_paid_page_offers_return(monkeypatch) -> None:
    monkeypatch.setattr(
        checkout, "fetch_session", lambda session_id: session_payload("paid")
    )
    response = client.get(f"/pay/{SESSION}")
    assert response.status_code == 200
    assert "Payment complete" in response.text
    assert "http://localhost:7777/subscription" in response.text


def test_expired_session_is_gone(monkeypatch) -> None:
    monkeypatch.setattr(
        checkout, "fetch_session", lambda session_id: session_payload("expired")
    )
    response = client.get(f"/pay/{SESSION}")
    assert response.status_code == 410
    assert "expired" in response.text.lower()


def test_invalid_session_id_is_rejected_without_a_call(monkeypatch) -> None:
    def boom(session_id):
        raise AssertionError("provider must not be called")

    monkeypatch.setattr(checkout, "fetch_session", boom)
    response = client.get("/pay/..%2Fetc%2Fpasswd")
    assert response.status_code == 404


def test_unknown_session_is_a_dead_link(monkeypatch) -> None:
    def missing(session_id):
        raise checkout.ProviderError("checkout_not_found", status=404)

    monkeypatch.setattr(checkout, "fetch_session", missing)
    response = client.get(f"/pay/{SESSION}")
    assert response.status_code == 404


def test_pay_posts_to_the_license_server(monkeypatch) -> None:
    monkeypatch.setattr(checkout, "fetch_session", lambda session_id: session_payload())
    calls: list[str] = []

    def fake_pay(session_id):
        calls.append(session_id)
        return session_payload("paid")

    monkeypatch.setattr(checkout, "pay_session", fake_pay)
    response = client.post(f"/pay/{SESSION}")
    assert response.status_code == 200
    assert "Payment complete" in response.text
    assert calls == [SESSION]


def test_pay_refusal_is_reported(monkeypatch) -> None:
    monkeypatch.setattr(checkout, "fetch_session", lambda session_id: session_payload())

    def refuse(session_id):
        raise checkout.ProviderError("checkout_expired", status=409)

    monkeypatch.setattr(checkout, "pay_session", refuse)
    response = client.post(f"/pay/{SESSION}")
    assert response.status_code == 409


def test_plan_name_is_escaped(monkeypatch) -> None:
    monkeypatch.setattr(
        checkout,
        "fetch_session",
        lambda session_id: session_payload(name="<script>alert(1)</script>"),
    )
    response = client.get(f"/pay/{SESSION}")
    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_unreachable_server_is_a_clear_error(monkeypatch) -> None:
    def down(session_id):
        raise checkout.ProviderError("network")

    monkeypatch.setattr(checkout, "fetch_session", down)
    response = client.get(f"/pay/{SESSION}")
    assert response.status_code == 502
    assert "license server" in response.text.lower()
