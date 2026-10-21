"""Device authentication for endpoints that act for a signed-in client.

The refresh token is the credential; the device fingerprint must match the
device the token was issued to. A revoked/expired token is 401, a disabled
account is 403, and a token from a device that was superseded by a newer
sign-in is 409 (the client turns that into its own notice).
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Account, Device, RefreshToken
from .security import hash_ip, hash_token
from .timeutils import utcnow

bearer_scheme = HTTPBearer(auto_error=False)


def require_device(
    db: Session,
    credentials: HTTPAuthorizationCredentials | None,
    fingerprint: str,
    request: Request | None = None,
) -> tuple[Account, Device]:
    now = utcnow()
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    token_row = db.scalars(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(credentials.credentials)
        )
    ).first()
    if (
        token_row is None
        or token_row.revoked_at is not None
        or token_row.expires_at <= now
    ):
        raise HTTPException(status_code=401, detail="invalid_token")

    account = db.get(Account, token_row.account_id)
    if account is None or account.status != "active":
        raise HTTPException(status_code=403, detail="account_disabled")

    device = db.get(Device, token_row.device_id)
    if (
        device is None
        or not device.active
        or device.fingerprint_hash != fingerprint
    ):
        raise HTTPException(status_code=409, detail="device_superseded")

    device.last_seen = now
    if request is not None:
        client_ip = request.client.host if request.client else None
        device.last_ip_hash = hash_ip(client_ip)
    db.commit()
    return account, device
