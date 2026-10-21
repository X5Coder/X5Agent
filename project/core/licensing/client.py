"""HTTP client for the central license server (stdlib urllib only).

All responses are untrusted: type, shape, ranges and the state/can_work
consistency are validated before anything is stored. Retries are bounded to
3 and never applied to permanent 4xx failures. Secrets (password, refresh
token) are never logged and never included in an exception message.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from . import config
from .notices import WORKING_STATES
from .store import clean_notices

_KNOWN_STATES = WORKING_STATES | {"warning", "suspended", "expired", "none"}


class LicenseServerError(Exception):
    """A license-server call failed (status 0 means network/config)."""

    def __init__(
        self,
        message: str,
        *,
        status: int = 0,
        code: str = "",
        retryable: bool = False,
        retry_after: float = 0.0,
    ) -> None:
        super().__init__(str(message)[:300])
        self.status = int(status)
        self.code = str(code)[:64]
        self.retryable = bool(retryable)
        self.retry_after = max(0.0, float(retry_after or 0.0))

    @property
    def network(self) -> bool:
        return self.status == 0


def _server_offset(server_time: Any) -> float:
    if not isinstance(server_time, str) or not server_time.strip():
        return 0.0
    text = server_time.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        offset = parsed.timestamp() - time.time()
    except (TypeError, ValueError):
        return 0.0
    return max(-86_400.0, min(86_400.0, offset))


def _optional_text(payload: dict, key: str, limit: int) -> str:
    value = payload.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LicenseServerError("Invalid license response.", code="bad_response")
    return value.strip()[:limit]


def validate_session_payload(payload: Any, *, require_refresh: bool = False) -> dict:
    """Validate and normalize a login/heartbeat response."""
    if not isinstance(payload, dict):
        raise LicenseServerError("Invalid license response.", code="bad_response")

    def required_text(key: str, limit: int) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise LicenseServerError(
                "The license server response is incomplete.", code="bad_response"
            )
        if len(value.strip()) > limit:
            raise LicenseServerError("Invalid license response.", code="bad_response")
        return value.strip()

    account_id = required_text("account_id", 64)
    device_id = required_text("device_id", 64)
    entitlement_token = required_text("entitlement_token", 8192)
    server_time = required_text("server_time", 64)

    state = payload.get("state")
    if not isinstance(state, str) or state not in _KNOWN_STATES:
        raise LicenseServerError("Invalid subscription state.", code="bad_response")

    can_work = payload.get("can_work")
    if not isinstance(can_work, bool):
        raise LicenseServerError("Invalid license response.", code="bad_response")
    if can_work != (state in WORKING_STATES):
        raise LicenseServerError(
            "The license server returned an inconsistent subscription.",
            code="bad_response",
        )

    remaining = payload.get("remaining_seconds")
    if isinstance(remaining, bool) or not isinstance(remaining, (int, float)):
        raise LicenseServerError("Invalid license response.", code="bad_response")
    remaining = max(0, min(315_360_000, int(remaining)))

    raw_notices = payload.get("notices")
    if raw_notices is not None and not isinstance(raw_notices, list):
        raise LicenseServerError("Invalid license response.", code="bad_response")
    notices = clean_notices(raw_notices)

    refresh_token = ""
    if require_refresh:
        refresh_token = required_text("refresh_token", 4096)
        if len(refresh_token) < 10:
            raise LicenseServerError("Invalid license response.", code="bad_response")

    plan_chosen = payload.get("plan_chosen", False)
    if not isinstance(plan_chosen, bool):
        raise LicenseServerError("Invalid license response.", code="bad_response")

    trial_used = payload.get("trial_used", False)
    if not isinstance(trial_used, bool):
        raise LicenseServerError("Invalid license response.", code="bad_response")

    return {
        "account_id": account_id,
        "device_id": device_id,
        "entitlement_token": entitlement_token,
        "server_time": server_time,
        "server_time_offset": _server_offset(server_time),
        "state": state,
        "plan": _optional_text(payload, "plan", 64),
        "starts_at": _optional_text(payload, "starts_at", 64),
        "expires_at": _optional_text(payload, "expires_at", 64),
        "subscription_id": _optional_text(payload, "subscription_id", 64),
        "remaining_seconds": remaining,
        "can_work": can_work,
        "notices": notices,
        "plan_chosen": plan_chosen,
        "trial_used": trial_used,
        "refresh_token": refresh_token,
    }


_CHECKOUT_STATUSES = frozenset({"pending", "paid", "expired", "cancelled"})
_SESSION_ID_RE = re.compile(r"^[A-Fa-f0-9-]{8,64}$")


def _safe_url(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip()
    if not text or len(text) > 2048:
        return ""
    parsed = urllib.parse.urlsplit(text)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    if parsed.username or parsed.password:
        return ""
    return text


def _validate_checkout(raw: Any, *, require_plan: bool = False) -> dict:
    """Validate a checkout create/status response (all fields untrusted)."""
    if not isinstance(raw, dict):
        raise LicenseServerError("Invalid checkout response.", code="bad_response")

    session_id = raw.get("session_id")
    if not isinstance(session_id, str) or not _SESSION_ID_RE.match(session_id):
        raise LicenseServerError("Invalid checkout response.", code="bad_response")

    result: dict[str, Any] = {"session_id": session_id}
    if isinstance(raw.get("instant"), bool):
        result["instant"] = raw["instant"]

    status = raw.get("status")
    if status is not None:
        if not isinstance(status, str) or status not in _CHECKOUT_STATUSES:
            raise LicenseServerError("Invalid checkout status.", code="bad_response")
        result["status"] = status
    elif require_plan:
        raise LicenseServerError("Invalid checkout status.", code="bad_response")

    url = raw.get("checkout_url")
    if url is not None:
        safe = _safe_url(url)
        if not safe:
            raise LicenseServerError("Invalid checkout link.", code="bad_response")
        result["checkout_url"] = safe
    elif not require_plan:
        raise LicenseServerError("Invalid checkout response.", code="bad_response")

    plan = raw.get("plan")
    if isinstance(plan, dict):
        try:
            price = int(plan.get("price_cents") or 0)
            days = int(plan.get("duration_days") or 0)
        except (TypeError, ValueError):
            price, days = 0, 0
        result["plan"] = {
            "name": str(plan.get("name") or "")[:80],
            "duration_days": max(0, min(3650, days)),
            "price_cents": max(0, min(1_000_000_000, price)),
            "currency": str(plan.get("currency") or "")[:8],
        }

    try:
        amount = int(raw.get("amount_cents") or 0)
    except (TypeError, ValueError):
        amount = 0
    result["amount_cents"] = max(0, min(1_000_000_000, amount))
    result["currency"] = str(raw.get("currency") or "")[:8]
    result["return_url"] = _safe_url(raw.get("return_url"))

    license_state = raw.get("license")
    if isinstance(license_state, dict):
        state = license_state.get("state")
        can_work = license_state.get("can_work")
        if isinstance(state, str) and state in _KNOWN_STATES and isinstance(can_work, bool):
            result["license"] = {
                "state": state,
                "can_work": can_work,
                "plan": str(license_state.get("plan") or "")[:64],
            }
    return result


class LicenseClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        self._base_url = (base_url or "").strip()
        self._timeout = float(timeout or config.REQUEST_TIMEOUT_SECONDS)

    def _base(self) -> str:
        raw = (self._base_url or config.server_url()).strip().rstrip("/")
        if not raw or len(raw) > 2048 or any(ch in raw for ch in "\r\n\t "):
            raise LicenseServerError(
                "The license server address is invalid.", code="bad_server_url"
            )
        parsed = urllib.parse.urlsplit(raw)
        if parsed.scheme not in ("http", "https"):
            raise LicenseServerError(
                "The license server address must use http(s).",
                code="bad_server_url",
            )
        if parsed.scheme == "http" and not config.allow_insecure_http():
            raise LicenseServerError(
                "The license server must use HTTPS (X5_LICENSE_ALLOW_HTTP=0).",
                code="insecure_transport",
            )
        if not parsed.hostname or parsed.username or parsed.password:
            raise LicenseServerError(
                "The license server address is invalid.", code="bad_server_url"
            )
        if parsed.fragment or parsed.query:
            raise LicenseServerError(
                "The license server address is invalid.", code="bad_server_url"
            )
        return raw

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        payload: dict | None = None,
        bearer: str | None = None,
        timeout: float | None = None,
    ) -> Any:
        if not path.startswith("/"):
            raise LicenseServerError("Invalid license request.", code="bad_request")
        url = self._base() + path
        body = None
        headers = {
            "Accept": "application/json",
            "User-Agent": "X5Agent-Client/1",
        }
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(url, data=body, headers=headers, method=method)

        attempts = config.MAX_RETRIES + 1
        last: LicenseServerError | None = None
        for attempt in range(attempts):
            try:
                return self._once(request, timeout or self._timeout)
            except LicenseServerError as exc:
                last = exc
                if not exc.retryable or attempt >= attempts - 1:
                    raise
                delay = min(4.0, 0.5 * (2**attempt))
                if exc.retry_after:
                    delay = max(delay, min(5.0, exc.retry_after))
                time.sleep(delay)
        raise last or LicenseServerError("License request failed.", code="unknown")

    @staticmethod
    def _once(request: urllib.request.Request, timeout: float) -> Any:
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read(config.MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as exc:
            detail_code, detail_message, retry_after = _parse_error(exc)
            status = int(getattr(exc, "code", 0) or 0)
            raise LicenseServerError(
                detail_message or f"License server error (HTTP {status}).",
                status=status,
                code=detail_code or f"http_{status}",
                retryable=status == 429 or status >= 500,
                retry_after=retry_after,
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise LicenseServerError(
                "Could not reach the license server.", code="network", retryable=True
            ) from exc
        if len(raw) > config.MAX_RESPONSE_BYTES:
            raise LicenseServerError(
                "The license server response is too large.", code="bad_response"
            )
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LicenseServerError(
                "The license server returned an invalid response.",
                code="bad_response",
            ) from exc

    def _authenticate(
        self,
        path: str,
        email: str,
        password: str,
        fingerprint: str,
        details: dict[str, str] | None = None,
    ) -> dict:
        payload = {
            "email": email,
            "password": password,
            "device_fingerprint": fingerprint,
        }
        if details:
            payload["device_details"] = details
        raw = self._request_json("POST", path, payload=payload)
        return validate_session_payload(raw, require_refresh=True)

    def login(
        self,
        email: str,
        password: str,
        fingerprint: str,
        details: dict[str, str] | None = None,
    ) -> dict:
        return self._authenticate(
            "/v1/auth/login", email, password, fingerprint, details
        )

    def register(
        self,
        email: str,
        password: str,
        fingerprint: str,
        details: dict[str, str] | None = None,
    ) -> dict:
        """Self-serve signup (no email step); the server returns a live session."""
        return self._authenticate(
            "/v1/auth/register", email, password, fingerprint, details
        )

    def heartbeat(
        self,
        refresh_token: str,
        fingerprint: str,
        timeout: float | None = None,
        details: dict[str, str] | None = None,
    ) -> dict:
        payload: dict[str, Any] = {"device_fingerprint": fingerprint}
        if details:
            payload["device_details"] = details
        raw = self._request_json(
            "POST",
            "/v1/heartbeat",
            payload=payload,
            bearer=refresh_token,
            timeout=timeout,
        )
        return validate_session_payload(raw, require_refresh=False)

    def logout(self, refresh_token: str) -> None:
        self._request_json(
            "POST",
            "/v1/auth/logout",
            payload={"refresh_token": refresh_token},
            timeout=min(self._timeout, 5.0),
        )

    def plans(self) -> list[dict]:
        raw = self._request_json("GET", "/v1/plans", timeout=min(self._timeout, 6.0))
        if not isinstance(raw, list):
            raise LicenseServerError("Invalid plans response.", code="bad_response")
        plans: list[dict] = []
        for item in raw[:50]:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                duration = int(item.get("duration_days") or 0)
                price = int(item.get("price_cents") or 0)
            except (TypeError, ValueError):
                continue
            raw_features = item.get("features")
            features: list[str] = []
            if isinstance(raw_features, list):
                for feature in raw_features[:12]:
                    text = str(feature or "").strip()
                    if text:
                        features.append(text[:120])
            plans.append(
                {
                    "id": str(item.get("id") or "")[:64],
                    "name": name.strip()[:80],
                    "duration_days": max(0, min(3650, duration)),
                    "price_cents": max(0, min(1_000_000_000, price)),
                    "currency": str(item.get("currency") or "")[:8],
                    "is_trial": bool(item.get("is_trial")),
                    # Card content owned by the admin panel.
                    "features": features,
                    "is_featured": bool(item.get("is_featured")),
                }
            )
        return plans

    def create_checkout(
        self,
        refresh_token: str,
        fingerprint: str,
        plan_id: str,
        return_url: str | None = None,
    ) -> dict:
        payload: dict[str, Any] = {
            "plan_id": plan_id,
            "device_fingerprint": fingerprint,
        }
        if return_url:
            payload["return_url"] = return_url
        raw = self._request_json(
            "POST", "/v1/billing/checkout", payload=payload, bearer=refresh_token
        )
        return _validate_checkout(raw)

    def checkout_status(
        self, refresh_token: str, fingerprint: str, session_id: str
    ) -> dict:
        raw = self._request_json(
            "POST",
            "/v1/billing/checkout/status",
            payload={"session_id": session_id, "device_fingerprint": fingerprint},
            bearer=refresh_token,
            timeout=min(self._timeout, 8.0),
        )
        return _validate_checkout(raw, require_plan=True)

    def choose_plan(
        self,
        refresh_token: str,
        fingerprint: str,
        plan_id: str,
        return_url: str | None = None,
    ) -> dict:
        """Record the onboarding choice (trial) or open a checkout (paid)."""
        payload = {"plan_id": plan_id, "device_fingerprint": fingerprint}
        if return_url:
            payload["return_url"] = return_url
        raw = self._request_json(
            "POST",
            "/v1/billing/choose",
            payload=payload,
            bearer=refresh_token,
            timeout=min(self._timeout, 8.0),
        )
        if not isinstance(raw, dict):
            raise LicenseServerError("Invalid plan response.", code="bad_response")
        chosen = raw.get("chosen")
        if not isinstance(chosen, bool):
            raise LicenseServerError("Invalid plan response.", code="bad_response")
        if chosen:
            return {
                "chosen": True,
                "instant": bool(raw.get("instant")),
            }
        checkout = _validate_checkout(raw.get("checkout"), require_plan=False)
        if not checkout.get("checkout_url"):
            raise LicenseServerError("Invalid checkout link.", code="bad_response")
        return {"chosen": False, "checkout": checkout}


def _parse_error(exc: urllib.error.HTTPError) -> tuple[str, str, float]:
    code = ""
    message = ""
    retry_after = 0.0
    try:
        retry_after = float(exc.headers.get("Retry-After") or 0)
    except (TypeError, ValueError, AttributeError):
        retry_after = 0.0
    try:
        raw = exc.read(8192)
        data = json.loads(raw.decode("utf-8")) if raw else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return code, message, retry_after
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, str):
        code = detail[:64]
        message = detail[:300]
    elif isinstance(detail, dict):
        code = str(detail.get("code") or "")[:64]
        message = str(detail.get("message") or "")[:300]
    elif isinstance(detail, list) and detail:
        first = detail[0] if isinstance(detail[0], dict) else {}
        code = "validation_error"
        message = str(first.get("msg") or "")[:300]
    return code, message, retry_after
