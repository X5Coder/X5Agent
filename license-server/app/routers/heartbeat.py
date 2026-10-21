from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..licensing import (
    collect_notices,
    effective_state,
    ensure_state_notice,
    latest_subscription,
    plan_name_for,
    subscription_payload,
    sync_expired,
    trial_used,
    update_device_details,
)
from ..models import Account, Device, RefreshToken
from ..schemas import HeartbeatRequest
from ..security import hash_ip, hash_token, issue_entitlement
from ..timeutils import iso_z, utcnow

router = APIRouter(prefix="/v1", tags=["heartbeat"])
_bearer = HTTPBearer(auto_error=False)


@router.post("/heartbeat")
def heartbeat(
    body: HeartbeatRequest,
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> dict:
    now = utcnow()
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="missing_token")
    raw_token = credentials.credentials

    token_row = db.scalars(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(raw_token)
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
        or device.fingerprint_hash != body.device_fingerprint
    ):
        raise HTTPException(status_code=409, detail="device_superseded")

    client_ip = request.client.host if request.client else None
    device.last_seen = now
    device.last_ip_hash = hash_ip(client_ip)
    update_device_details(device, body.device_details)
    db.commit()

    sub = latest_subscription(db, account.id)
    sync_expired(db, sub, now)
    state = effective_state(sub, now)
    ensure_state_notice(db, account.id, sub, state)
    notices = collect_notices(db, account.id)
    plan_name = plan_name_for(db, sub)

    payload = {
        "server_time": iso_z(now),
        "account_id": account.id,
        "device_id": device.id,
        "entitlement_token": issue_entitlement(
            account.id, device.id, state, plan_name
        ),
        "notices": notices,
    }
    payload.update(subscription_payload(sub, state, now, plan_name))
    payload["plan_chosen"] = account.plan_chosen_at is not None
    payload["trial_used"] = trial_used(db, account.id)
    return payload
