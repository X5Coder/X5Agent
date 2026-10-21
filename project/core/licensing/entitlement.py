"""Ed25519 entitlement-token verification.

The server signs a 15-minute token (iss=x5agent-license,
aud=x5agent-client). The public key is pinned in config.py, so a forged or
proxy-swapped token never verifies.

Expiry is checked against a server-adjusted clock (the heartbeat computes the
offset), so a wrong local clock cannot lock a paying user out or extend a
canceled subscription.
"""

from __future__ import annotations

import time

import jwt

from . import config


class EntitlementError(Exception):
    """Invalid, expired, or mismatched entitlement token."""


def verify_entitlement(
    token: str,
    public_key_pem: str,
    account_id: str,
    device_id: str,
    *,
    now: float | None = None,
    leeway: int = config.ENTITLEMENT_LEEWAY_SECONDS,
    grace_seconds: int = 0,
) -> dict:
    if not isinstance(token, str) or not token.strip():
        raise EntitlementError("missing entitlement token")
    if not isinstance(account_id, str) or not account_id:
        raise EntitlementError("missing account id")
    if not isinstance(device_id, str) or not device_id:
        raise EntitlementError("missing device id")
    try:
        payload = jwt.decode(
            token,
            public_key_pem,
            algorithms=["EdDSA"],
            audience="x5agent-client",
            issuer="x5agent-license",
            options={
                "verify_exp": False,
                "verify_iat": False,
                "require": ["sub", "did", "state", "exp", "iat"],
            },
        )
    except jwt.PyJWTError as exc:
        raise EntitlementError("invalid entitlement token") from exc
    if not isinstance(payload, dict):
        raise EntitlementError("invalid entitlement payload")

    current = time.time() if now is None else float(now)
    expires = payload.get("exp")
    issued = payload.get("iat")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        raise EntitlementError("invalid expiry")
    if isinstance(issued, bool) or not isinstance(issued, (int, float)):
        raise EntitlementError("invalid issue time")
    # grace_seconds is only used by the LOCAL offline gate: a short outage must
    # not stop a paid run between two heartbeats.
    if float(expires) + max(0, int(grace_seconds)) <= current:
        raise EntitlementError("entitlement expired")
    if float(issued) > current + max(0, int(leeway)):
        raise EntitlementError("entitlement not valid yet")

    if str(payload.get("sub") or "") != account_id:
        raise EntitlementError("entitlement account mismatch")
    if str(payload.get("did") or "") != device_id:
        raise EntitlementError("entitlement device mismatch")
    state = payload.get("state")
    if not isinstance(state, str) or state not in {
        "trial",
        "active",
        "warning",
        "suspended",
        "expired",
        "none",
    }:
        raise EntitlementError("invalid entitlement state")
    return payload
