"""The single owner of the client license session.

Lifecycle:
    create -> start_background() -> login() -> heartbeat every 10 min
           -> logout()/stop_background()

Rules enforced here:
- A NEW process must complete a heartbeat before any run (ensure_verified).
- local_gate() is what every run/step/tool batch calls: it never touches the
  network. It reads the state file, verifies the pinned Ed25519 token, and
  tolerates a short outage (LOCAL_GRACE_SECONDS) while the subscription end
  date is still in the future. If the file was refreshed by another process it
  re-reads it once (repair); a real violation fails with a message that
  carries the site address.
- can_work() stays the strict local check (no grace).
- 409 device_superseded / 401 invalid_token clear the session immediately.
- The password is used once and never stored or logged.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from . import config, device, notices, store
from .client import LicenseClient, LicenseServerError, validate_session_payload
from .entitlement import EntitlementError, verify_entitlement

_LOG = logging.getLogger("x5agent.license")


class LicenseError(Exception):
    """A user-facing license action failed (safe message only)."""


class LicenseService:
    def __init__(
        self,
        *,
        state_path: Path | None = None,
        client: Any | None = None,
        public_key_pem: str | None = None,
        heartbeat_interval: float = config.HEARTBEAT_INTERVAL_SECONDS,
        bypass: bool | None = None,
    ) -> None:
        self._state_path = Path(state_path) if state_path is not None else None
        self._client = client if client is not None else LicenseClient()
        self._public_key = public_key_pem or config.public_key_pem()
        self._heartbeat_interval = max(1.0, float(heartbeat_interval))
        self._bypass_override = bypass
        self._lock = threading.RLock()
        self._network_lock = threading.Lock()
        self._thread_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._boot_verified = False
        self._last_attempt = 0.0
        self._state = store.load_state(self._state_path)

    # ------------------------------------------------------------------ state

    def _bypass(self) -> bool:
        if self._bypass_override is not None:
            return bool(self._bypass_override)
        return config.bypass_enabled()

    def _snapshot(self) -> dict:
        with self._lock:
            state = dict(self._state)
            state["notices"] = [dict(n) for n in (self._state.get("notices") or [])]
        return state

    def _can_work_locked(self, state: dict) -> bool:
        if self._bypass():
            return True
        if not self._boot_verified:
            return False
        if state.get("state") not in notices.WORKING_STATES:
            return False
        token = state.get("entitlement_token") or ""
        account_id = state.get("account_id") or ""
        device_id = state.get("device_id") or ""
        if not token or not account_id or not device_id:
            return False
        try:
            payload = verify_entitlement(
                token,
                self._public_key,
                account_id,
                device_id,
                now=time.time() + float(state.get("server_time_offset") or 0.0),
            )
        except (EntitlementError, ValueError, TypeError):
            return False
        return payload.get("state") in notices.WORKING_STATES

    def can_work(self) -> bool:
        if self._bypass():
            return True
        with self._lock:
            return self._can_work_locked(self._state)

    # ------------------------------------------------------------------ local

    def _local_token_ok(self, state: dict) -> bool:
        """Verify the signed entitlement on disk: no network, no I/O beyond RAM."""
        if self._bypass():
            return True
        if str(state.get("state") or "") not in notices.WORKING_STATES:
            return False
        token = str(state.get("entitlement_token") or "")
        account_id = str(state.get("account_id") or "")
        device_id = str(state.get("device_id") or "")
        if not token or not account_id or not device_id:
            return False
        now = time.time() + float(state.get("server_time_offset") or 0.0)
        try:
            claims = verify_entitlement(
                token,
                self._public_key,
                account_id,
                device_id,
                now=now,
                grace_seconds=config.LOCAL_GRACE_SECONDS,
            )
        except (EntitlementError, ValueError, TypeError):
            return False
        if str(claims.get("state") or "") not in notices.WORKING_STATES:
            return False
        # Inside the grace window the token is already past its exp, so the
        # subscription end date on disk becomes the second bound.
        token_exp = claims.get("exp")
        if isinstance(token_exp, (int, float)) and float(token_exp) <= now:
            if not _subscription_open(state, now):
                return False
        return True

    def local_gate(self) -> tuple[bool, str]:
        """Offline license decision used before every tool call.

        The gate never contacts the server: it reads the state file (plus the
        signed 15-minute entitlement inside it), and if that fails it re-reads
        the file once in case another process refreshed it. Only a real local
        violation fails, and the message then carries the fixed site address.
        The background heartbeat thread is what renews the token.
        """
        if self._bypass():
            return True, ""
        with self._lock:
            current = dict(self._state)
        if self._local_token_ok(current):
            return True, ""

        # Repair pass: reload what is on disk (a heartbeat may have renewed it).
        try:
            reloaded = store.load_state(self._state_path)
        except Exception as exc:  # noqa: BLE001 - unreadable file must not crash a run
            _LOG.warning("license state reload failed (%s)", type(exc).__name__)
            reloaded = None
        if isinstance(reloaded, dict):
            with self._lock:
                self._state = reloaded
            if self._local_token_ok(reloaded):
                return True, ""

        message = notices.blocked_message(
            str(current.get("state") or ""),
            current.get("notices") or [],
            str(current.get("last_error") or ""),
        )
        return False, notices.with_site(message)

    def _state_is_blocking(self) -> bool:
        with self._lock:
            return self._state.get("state") in {
                "warning",
                "suspended",
                "expired",
                "none",
                "device_superseded",
                "auth_required",
                "account_disabled",
            }

    def status(self) -> dict:
        state = self._snapshot()
        # The UI must agree with the gate the agent itself runs, so it uses the
        # same local decision (disk token + bounded offline grace), never a
        # network call and never the stricter boot flag.
        can_work, gate_message = self.local_gate()
        now = time.time()
        last = float(state.get("last_heartbeat_at") or 0.0)
        stored_remaining = int(state.get("remaining_seconds") or 0)
        elapsed = max(0, int(now - last)) if last else 0
        message = "" if can_work else gate_message
        return {
            "connected": bool(state.get("refresh_token")),
            "can_work": can_work,
            "message": message,
            "bypass": self._bypass(),
            "state": state.get("state") or "signed_out",
            "email": state.get("email") or "",
            "plan": state.get("plan") or "",
            "starts_at": state.get("starts_at") or "",
            "expires_at": state.get("expires_at") or "",
            "remaining_seconds": max(0, stored_remaining - elapsed),
            "plan_chosen": bool(state.get("plan_chosen")),
            "trial_used": bool(state.get("trial_used")),
            "notices": state.get("notices") or [],
            "last_heartbeat_at": last,
            "last_error": state.get("last_error") or "",
            "server_url": config.server_url(),
            "insecure_transport": config.server_url().lower().startswith("http://"),
            "device_short": device.fingerprint_short(),
        }

    def blocked_message(self) -> str:
        if self._bypass() or self.can_work():
            return ""
        state = self._snapshot()
        return notices.blocked_message(
            str(state.get("state") or ""),
            state.get("notices") or [],
            str(state.get("last_error") or ""),
        )

    # ----------------------------------------------------------------- persist

    def _persist(self) -> None:
        self._state = store.save_state(self._state, self._state_path)

    def _set_error(self, message: str, *, next_state: str | None = None) -> None:
        with self._lock:
            self._state["last_error"] = str(message)[:400]
            if next_state:
                self._state["state"] = next_state
                self._state["can_work"] = False
            try:
                self._persist()
            except OSError as exc:
                _LOG.warning("license state save failed (%s)", type(exc).__name__)

    def _drop_session(self, next_state: str) -> None:
        with self._lock:
            self._state = store.clear_session(self._state, next_state=next_state)
            self._state["last_error"] = (
                "" if next_state == "signed_out" else notices.MESSAGES.get(next_state, "")
            )
            self._boot_verified = False
            try:
                self._persist()
            except OSError as exc:
                _LOG.warning("license state save failed (%s)", type(exc).__name__)

    def _apply_payload(
        self, payload: dict, *, refresh_token: str | None = None, email: str | None = None
    ) -> None:
        try:
            verify_entitlement(
                payload["entitlement_token"],
                self._public_key,
                payload["account_id"],
                payload["device_id"],
                now=time.time() + float(payload.get("server_time_offset") or 0.0),
            )
        except (EntitlementError, KeyError, TypeError, ValueError) as exc:
            raise EntitlementError("invalid entitlement token") from exc

        with self._lock:
            if email:
                self._state["email"] = email.strip()[:320]
            if refresh_token:
                self._state["refresh_token"] = refresh_token
            self._state.update(
                {
                    "server_url": config.server_url(),
                    "account_id": payload["account_id"],
                    "device_id": payload["device_id"],
                    "entitlement_token": payload["entitlement_token"],
                    "state": payload["state"],
                    "plan": payload.get("plan") or "",
                    "starts_at": payload.get("starts_at") or "",
                    "expires_at": payload.get("expires_at") or "",
                    "subscription_id": payload.get("subscription_id") or "",
                    "remaining_seconds": int(payload.get("remaining_seconds") or 0),
                    "can_work": bool(payload.get("can_work")),
                    "plan_chosen": bool(payload.get("plan_chosen")),
                    "trial_used": bool(payload.get("trial_used")),
                    "last_heartbeat_at": time.time(),
                    "last_error": "",
                    "notices": payload.get("notices") or [],
                    "server_time_offset": float(payload.get("server_time_offset") or 0.0),
                    "updated_at": time.time(),
                }
            )
            self._boot_verified = True
            self._persist()

    # ----------------------------------------------------------------- actions

    def _authenticate(
        self, email: str, password: str, call, error_mapper, label: str
    ) -> dict:
        if self._bypass():
            return self.status()
        email_text = email.strip() if isinstance(email, str) else ""
        password_text = password if isinstance(password, str) else ""
        if not email_text or not password_text:
            raise LicenseError("Enter your email and password.")
        if len(email_text) > 320 or len(password_text) > 512:
            raise LicenseError("The email or password is too long.")

        fingerprint = device.machine_fingerprint()
        with self._network_lock:
            try:
                payload = call(
                    email_text,
                    password_text,
                    fingerprint,
                    device.fingerprint_details(),
                )
            except LicenseServerError as exc:
                raise LicenseError(error_mapper(exc)) from exc
            try:
                self._apply_payload(
                    payload,
                    refresh_token=payload["refresh_token"],
                    email=email_text,
                )
            except EntitlementError as exc:
                self._set_error(notices.MESSAGES["invalid_entitlement"],
                                next_state="invalid_entitlement")
                raise LicenseError(notices.MESSAGES["invalid_entitlement"]) from exc
            except OSError as exc:
                raise LicenseError("Could not save the license session.") from exc
        _LOG.info("license %s succeeded", label)
        return self.status()

    def login(self, email: str, password: str) -> dict:
        return self._authenticate(
            email, password, self._client.login, _login_error, "login"
        )

    def register(self, email: str, password: str) -> dict:
        """Create an account and sign in at once (no email confirmation step)."""
        return self._authenticate(
            email, password, self._client.register, _register_error, "signup"
        )

    def logout(self) -> dict:
        if self._bypass():
            return self.status()
        with self._lock:
            token = self._state.get("refresh_token") or ""
        if token:
            with self._network_lock:
                try:
                    self._client.logout(token)
                except LicenseServerError as exc:
                    _LOG.warning("license logout call failed (%s)", exc.code or exc.status)
                except Exception as exc:  # never block a local sign-out
                    _LOG.warning("license logout call failed (%s)", type(exc).__name__)
        self._drop_session("signed_out")
        return self.status()

    def heartbeat(self, timeout: float | None = None) -> dict:
        if self._bypass():
            return self.status()
        with self._lock:
            token = self._state.get("refresh_token") or ""
        if not token:
            return self.status()
        with self._network_lock:
            return self._heartbeat_locked(timeout=timeout)

    def _heartbeat_locked(self, timeout: float | None = None) -> dict:
        with self._lock:
            token = self._state.get("refresh_token") or ""
        if not token:
            return self.status()
        self._last_attempt = time.time()
        try:
            payload = self._client.heartbeat(
                token,
                device.machine_fingerprint(),
                timeout=timeout,
                details=device.fingerprint_details(),
            )
        except LicenseServerError as exc:
            self._handle_heartbeat_error(exc)
            return self.status()
        except Exception as exc:
            _LOG.warning("license heartbeat failed (%s)", type(exc).__name__)
            self._set_error(notices.MESSAGES["network"], next_state="network")
            return self.status()
        try:
            self._apply_payload(payload)
        except EntitlementError:
            self._set_error(
                notices.MESSAGES["invalid_entitlement"],
                next_state="invalid_entitlement",
            )
        except OSError as exc:
            _LOG.warning("license state save failed (%s)", type(exc).__name__)
            self._set_error(notices.MESSAGES["check_failed"])
        return self.status()

    def _handle_heartbeat_error(self, exc: LicenseServerError) -> None:
        code = exc.code or ""
        if exc.status == 409 or code == "device_superseded":
            self._drop_session("device_superseded")
            return
        if exc.status == 401 or code == "invalid_token":
            self._drop_session("auth_required")
            return
        if exc.status == 403 and code == "account_disabled":
            self._drop_session("account_disabled")
            return
        if exc.status == 429 or code == "rate_limited":
            self._set_error(notices.MESSAGES["rate_limited"])
            return
        if exc.network:
            # Transient: keep the working state; the signed 15-minute token is
            # the only grace. Once it expires can_work() turns false.
            self._set_error(notices.MESSAGES["network"])
            return
        if exc.status >= 500:
            self._set_error(notices.MESSAGES["server_error"])
            return
        self._set_error(notices.MESSAGES["check_failed"])

    def ensure_verified(self, timeout: float = 3.0) -> bool:
        """Make sure this process contacted the server; never raises.

        True means the server was reached and a token was stored this process
        (even if the subscription itself blocks work). Callers must also call
        can_work() to decide whether a run may start.
        """
        if self._bypass():
            return True
        if self.can_work():
            return True
        with self._lock:
            verified = self._boot_verified
            token = self._state.get("refresh_token") or ""
            blocking = self._state_is_blocking()
        # A verified process knows the subscription is blocked; no need to
        # re-hit the server on every request. An UNVERIFIED process must still
        # contact the server once (no offline grace), even for a blocked state.
        if verified and blocking:
            return True
        if not token:
            return False
        with self._lock:
            recent = (time.time() - self._last_attempt) < 2.0
        if recent and not verified:
            return False
        acquired = self._network_lock.acquire(timeout=max(0.1, float(timeout)))
        if not acquired:
            return verified
        try:
            with self._lock:
                if self._boot_verified and (self.can_work() or self._state_is_blocking()):
                    return True
            self._heartbeat_locked(
                timeout=min(config.BOOT_HEARTBEAT_TIMEOUT_SECONDS, max(1.0, timeout))
            )
            with self._lock:
                return self._boot_verified
        finally:
            self._network_lock.release()

    def plans(self) -> list[dict]:
        if self._bypass():
            return []
        try:
            return self._client.plans()
        except LicenseServerError as exc:
            raise LicenseError(_plans_error(exc)) from exc
        except Exception as exc:
            raise LicenseError(notices.MESSAGES["network"]) from exc

    # ----------------------------------------------------------------- billing

    def start_checkout(self, plan_id: str, return_url: str | None = None) -> dict:
        """Create a payment session for one plan (opens the provider page)."""
        if self._bypass():
            raise LicenseError("Payments are disabled while licensing is bypassed.")
        plan_text = plan_id.strip() if isinstance(plan_id, str) else ""
        if not plan_text or len(plan_text) > 64:
            raise LicenseError("Choose a plan first.")
        with self._lock:
            token = self._state.get("refresh_token") or ""
        if not token:
            raise LicenseError("Sign in to your license account first.")

        fingerprint = device.machine_fingerprint()
        with self._network_lock:
            try:
                result = self._client.create_checkout(
                    token, fingerprint, plan_text, return_url
                )
                if result.get("instant"):
                    # Sandbox: already paid, so refresh the plan in place.
                    current = self._heartbeat_locked(timeout=8.0)
                    return {
                        **result,
                        "paid": True,
                        "license": current,
                    }
                return result
            except LicenseServerError as exc:
                raise LicenseError(_checkout_error(exc)) from exc
            except Exception as exc:
                raise LicenseError(notices.MESSAGES["network"]) from exc

    def choose_plan(self, plan_id: str, return_url: str | None = None) -> dict:
        """Record the onboarding choice.

        A free/trial plan is recorded immediately and returns
        ``{"chosen": True}``; a paid plan returns a provider checkout that the
        caller opens, and the choice is recorded when the payment completes.
        """
        if self._bypass():
            raise LicenseError("Plans are disabled while licensing is bypassed.")
        plan_text = plan_id.strip() if isinstance(plan_id, str) else ""
        if not plan_text or len(plan_text) > 64:
            raise LicenseError("Choose a plan first.")
        with self._lock:
            token = self._state.get("refresh_token") or ""
        if not token:
            raise LicenseError("Sign in to your license account first.")

        fingerprint = device.machine_fingerprint()
        with self._network_lock:
            try:
                result = self._client.choose_plan(
                    token, fingerprint, plan_text, return_url
                )
            except LicenseServerError as exc:
                raise LicenseError(_checkout_error(exc)) from exc
            except Exception as exc:
                raise LicenseError(notices.MESSAGES["network"]) from exc

        if result.get("chosen"):
            with self._lock:
                self._state["plan_chosen"] = True
                try:
                    self._persist()
                except OSError as exc:
                    _LOG.warning("license state save failed (%s)", type(exc).__name__)
            if result.get("instant"):
                # A sandbox purchase is already active: read the real plan.
                return {"chosen": True, "license": self.heartbeat()}
            return {"chosen": True, "license": self.status()}
        return {
            "chosen": False,
            "checkout": result.get("checkout") or {},
            "license": self.status(),
        }

    def checkout_status(self, session_id: str) -> dict:
        """Poll one checkout; a paid session refreshes the license at once."""
        session_text = session_id.strip() if isinstance(session_id, str) else ""
        if not session_text or len(session_text) > 64:
            raise LicenseError("Invalid checkout.")
        with self._lock:
            token = self._state.get("refresh_token") or ""
        if not token:
            raise LicenseError("Sign in to your license account first.")

        fingerprint = device.machine_fingerprint()
        with self._network_lock:
            try:
                result = self._client.checkout_status(
                    token, fingerprint, session_text
                )
            except LicenseServerError as exc:
                raise LicenseError(_checkout_error(exc)) from exc
            except Exception as exc:
                raise LicenseError(notices.MESSAGES["network"]) from exc

        paid = result.get("status") == "paid"
        plan_name = (result.get("plan") or {}).get("name") or ""
        # A paid session always re-reads the truth: a renewal extends the same
        # plan, so nothing else in the cached snapshot would look different.
        current = self.heartbeat() if paid else self.status()
        return {
            "status": result.get("status") or "pending",
            "paid": paid,
            "plan": plan_name,
            "license": current,
        }

    # ---------------------------------------------------------------- lifecycle

    def start_background(self) -> None:
        if self._bypass():
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run_loop, name="x5-license", daemon=True
            )
            self._thread.start()

    def stop_background(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._thread_lock:
            thread = self._thread
            self._thread = None
        if thread is not None:
            thread.join(timeout=max(0.1, float(timeout)))

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.heartbeat()
            except Exception as exc:
                _LOG.warning("license background heartbeat failed (%s)", type(exc).__name__)
            self._stop.wait(self._heartbeat_interval)


def _subscription_open(state: dict, now: float) -> bool:
    """True while the stored subscription end date is still in the future."""
    raw = str(state.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        from datetime import datetime, timezone

        text = raw.replace("Z", "+00:00")
        end = datetime.fromisoformat(text)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        return end.timestamp() > float(now)
    except (ValueError, TypeError, OverflowError):
        return False


def _login_error(exc: LicenseServerError) -> str:
    code = exc.code or ""
    if exc.network:
        return notices.MESSAGES["network"]
    if exc.status == 401 or code == "invalid_credentials":
        return notices.MESSAGES["invalid_credentials"]
    if exc.status == 403 and code == "account_disabled":
        return notices.MESSAGES["account_disabled"]
    if exc.status == 429 or code == "rate_limited":
        return notices.MESSAGES["rate_limited"]
    if exc.status == 422:
        return "Check the email and password format."
    if exc.status >= 500:
        return notices.MESSAGES["server_error"]
    return notices.MESSAGES["check_failed"]


def _register_error(exc: LicenseServerError) -> str:
    code = exc.code or ""
    if exc.network:
        return notices.MESSAGES["network"]
    if exc.status == 409 or code == "email_taken":
        return notices.MESSAGES["email_taken"]
    if exc.status == 429 or code == "rate_limited":
        return notices.MESSAGES["rate_limited"]
    if exc.status == 422:
        return "Use a valid email and a password of at least 8 characters."
    if exc.status >= 500:
        return notices.MESSAGES["server_error"]
    return notices.MESSAGES["check_failed"]


def _plans_error(exc: LicenseServerError) -> str:
    if exc.network:
        return notices.MESSAGES["network"]
    return notices.MESSAGES["server_error"]


def _checkout_error(exc: LicenseServerError) -> str:
    code = exc.code or ""
    if exc.network:
        return notices.MESSAGES["network"]
    if code == "checkout_expired" or code == "checkout_not_pending":
        return "This checkout expired. Start a new one."
    if code == "checkout_not_found":
        return "This checkout was not found. Start a new one."
    if code == "plan_unavailable":
        return "This plan is no longer available."
    if code == "checkout_unavailable":
        return "Payments are not available right now. Try again later."
    if code == "account_suspended":
        return "This account is suspended. Contact support before buying a plan."
    if code == "account_disabled":
        return notices.MESSAGES["account_disabled"]
    if exc.status == 429 or code == "rate_limited":
        return notices.MESSAGES["rate_limited"]
    if exc.status == 401:
        return "Sign in again to continue."
    if exc.status >= 500:
        return notices.MESSAGES["server_error"]
    return notices.MESSAGES["check_failed"]


license_service = LicenseService()
