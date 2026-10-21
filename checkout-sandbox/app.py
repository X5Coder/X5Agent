"""Sandbox checkout — a fake payment provider for testing the subscription flow.

It plays Dodo's role until the real Dodo account is connected: it shows the
plan and price, presents a (non-collecting) card form, then asks the license
server to mark the session paid with a shared secret. No card data is read,
stored or logged, and the browser never receives the secret.

Configuration (environment):
    LICENSE_API_URL        base URL of the license server (default loopback)
    SANDBOX_SHARED_SECRET  the same value the license server holds (required)
    PORT                   listen port (default 8789)
"""

from __future__ import annotations

import html
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "assets"

LICENSE_API_URL = (os.getenv("LICENSE_API_URL") or "http://127.0.0.1:8788").strip().rstrip("/")
SHARED_SECRET = (os.getenv("SANDBOX_SHARED_SECRET") or "").strip()
HTTP_TIMEOUT = 10.0
MAX_RESPONSE_BYTES = 64 * 1024
MAX_RETRIES = 3
SESSION_ID_RE = re.compile(r"^[0-9a-fA-F-]{8,64}$")

app = FastAPI(title="X5Agent Sandbox Checkout", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/assets", StaticFiles(directory=str(ASSETS_DIR)), name="assets")


class ProviderError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = int(status)


def _request(method: str, path: str, payload: dict | None = None) -> dict:
    if not SHARED_SECRET:
        raise ProviderError("not_configured")
    body = None
    headers = {"Accept": "application/json", "X-Sandbox-Secret": SHARED_SECRET}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        LICENSE_API_URL + path, data=body, headers=headers, method=method
    )

    last: ProviderError | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            try:
                detail = json.loads(exc.read(4096).decode("utf-8")).get("detail")
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, AttributeError):
                detail = None
            raise ProviderError(str(detail or f"http_{status}"), status=status) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = ProviderError("network")
            if attempt < MAX_RETRIES:
                time.sleep(0.4 * (2**attempt))
                continue
            raise last from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("bad_response")
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("bad_response") from exc
        if not isinstance(data, dict):
            raise ProviderError("bad_response")
        return data
    raise last or ProviderError("network")


def fetch_session(session_id: str) -> dict:
    return _request("GET", f"/v1/sandbox/session/{session_id}")


def pay_session(session_id: str) -> dict:
    return _request("POST", "/v1/sandbox/pay", {"session_id": session_id})


def _money(cents: int, currency: str) -> str:
    amount = max(0, int(cents)) / 100
    text = f"{amount:,.2f}".rstrip("0").rstrip(".")
    code = (currency or "USD").upper()[:8]
    return f"{text} {code}"


def _page(title: str, body: str, *, status_code: int = 200) -> HTMLResponse:
    document = f"""<!doctype html>
<html lang="en" dir="ltr">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<meta name="color-scheme" content="dark" />
<title>{html.escape(title)}</title>
<link rel="stylesheet" href="/assets/checkout.css" />
</head>
<body>
<main class="shell">
  <header class="brand">
    <span class="mark" aria-hidden="true"></span>
    <div>
      <p class="brand-name">X5Agent</p>
      <p class="brand-sub">Sandbox checkout</p>
    </div>
  </header>
  {body}
  <p class="foot">Test payment provider — no card is charged and no card data is stored.</p>
</main>
</body>
</html>
"""
    return HTMLResponse(document, status_code=status_code)


def _plan_name(session: dict) -> str:
    plan = session.get("plan") if isinstance(session.get("plan"), dict) else {}
    name = plan.get("name")
    return name if isinstance(name, str) and name.strip() else "Subscription"


def _return_url(session: dict) -> str:
    url = session.get("return_url")
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        return url
    return ""


def _pay_page(session: dict, session_id: str) -> HTMLResponse:
    plan = session.get("plan") if isinstance(session.get("plan"), dict) else {}
    name = html.escape(_plan_name(session))
    days = plan.get("duration_days")
    days = int(days) if isinstance(days, int) and days > 0 else 0
    price = html.escape(_money(session.get("amount_cents", 0), session.get("currency", "USD")))
    body = f"""
  <section class="card">
    <p class="eyebrow">Order summary</p>
    <h1>{name}</h1>
    <p class="muted">{days} days · one device</p>
    <p class="price">{price}</p>
    <form method="post" action="/pay/{session_id}" class="pay-form">
      <div class="field">
        <label for="card">Card number (test)</label>
        <input id="card" name="card" inputmode="numeric" autocomplete="off"
               value="4242 4242 4242 4242" readonly />
      </div>
      <div class="row">
        <div class="field">
          <label for="exp">Expiry</label>
          <input id="exp" name="exp" value="12/34" readonly />
        </div>
        <div class="field">
          <label for="cvc">CVC</label>
          <input id="cvc" name="cvc" value="123" readonly />
        </div>
      </div>
      <button class="btn" type="submit">Pay {price}</button>
    </form>
    <p class="muted small">You will return to X5Agent when the payment is done.</p>
  </section>
"""
    return _page("Checkout", body)


def _done_page(session: dict) -> HTMLResponse:
    name = html.escape(_plan_name(session))
    return_url = html.escape(_return_url(session))
    link = (
        f'<a class="btn" href="{return_url}">Return to X5Agent</a>'
        if return_url
        else '<p class="muted">You can close this tab and return to X5Agent.</p>'
    )
    body = f"""
  <section class="card ok">
    <span class="tick" aria-hidden="true">✓</span>
    <h1>Payment complete</h1>
    <p class="muted">{name} is active on your account.</p>
    {link}
  </section>
"""
    return _page("Payment complete", body)


def _message_page(title: str, message: str, status_code: int = 200) -> HTMLResponse:
    body = f"""
  <section class="card">
    <h1>{html.escape(title)}</h1>
    <p class="muted">{html.escape(message)}</p>
  </section>
"""
    return _page(title, body, status_code=status_code)


@app.get("/health")
def health() -> JSONResponse:
    ok = bool(SHARED_SECRET)
    return JSONResponse(
        {
            "status": "ok" if ok else "unconfigured",
            "service": "x5agent-checkout-sandbox",
        },
        status_code=200 if ok else 503,
    )


@app.get("/", response_class=HTMLResponse)
def landing() -> HTMLResponse:
    return _message_page(
        "Sandbox checkout",
        "Open the checkout link from the X5Agent console (Settings → Subscription).",
    )


def _load_or_error(session_id: str):
    if not SESSION_ID_RE.match(session_id or ""):
        return None, _message_page("Link not valid", "This payment link is not valid.", 404)
    try:
        return fetch_session(session_id), None
    except ProviderError as exc:
        if exc.status == 404:
            return None, _message_page("Link not valid", "This payment link is not valid.", 404)
        if not SHARED_SECRET:
            return None, _message_page(
                "Not configured", "This checkout service has no shared secret.", 503
            )
        return None, _message_page(
            "Cannot reach the license server",
            "The payment could not be prepared. Please try again from X5Agent.",
            502,
        )


@app.get("/pay/{session_id}", response_class=HTMLResponse)
def pay_get(session_id: str) -> HTMLResponse:
    session, error = _load_or_error(session_id)
    if error is not None:
        return error
    status = str(session.get("status") or "")
    if status == "paid":
        return _done_page(session)
    if status != "pending":
        return _message_page(
            "This link expired",
            "The checkout link is no longer usable. Start a new one from X5Agent.",
            410,
        )
    return _pay_page(session, session_id)


@app.post("/pay/{session_id}", response_class=HTMLResponse)
def pay_post(session_id: str) -> HTMLResponse:
    # The card fields in the form are decorative: they are never read, stored
    # or logged, so the sandbox collects no payment data at all.
    session, error = _load_or_error(session_id)
    if error is not None:
        return error
    try:
        paid = pay_session(session_id)
    except ProviderError as exc:
        if exc.status in (409, 404):
            return _message_page(
                "This link expired or was already closed",
                "Start a new checkout from X5Agent.",
                409,
            )
        return _message_page(
            "Payment failed",
            "The license server did not confirm the payment. Please try again.",
            502,
        )
    return _done_page(paid if paid else session)
