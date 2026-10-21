from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..licensing import (
    bind_device,
    collect_notices,
    effective_state,
    ensure_state_notice,
    grant_trial,
    latest_subscription,
    plan_name_for,
    subscription_payload,
    sync_expired,
    trial_used,
)
from ..models import Account, RefreshToken, new_id
from ..schemas import LoginRequest, RefreshTokenRequest, RegisterRequest
from ..security import (
    hash_ip,
    hash_password,
    hash_token,
    issue_entitlement,
    new_refresh_token,
    verify_password,
)
from ..timeutils import iso_z, utcnow
from ..validation import normalize_email

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def _session_payload(
    db: Session,
    account: Account,
    device_id: str,
    raw_refresh_token: str,
    now,
) -> dict:
    sub = latest_subscription(db, account.id)
    sync_expired(db, sub, now)
    state = effective_state(sub, now)
    ensure_state_notice(db, account.id, sub, state)
    notices = collect_notices(db, account.id)
    plan_name = plan_name_for(db, sub)

    payload = {
        "refresh_token": raw_refresh_token,
        "account_id": account.id,
        "device_id": device_id,
        "server_time": iso_z(now),
        "entitlement_token": issue_entitlement(
            account.id, device_id, state, plan_name
        ),
        "notices": notices,
    }
    payload.update(subscription_payload(sub, state, now, plan_name))
    payload["plan_chosen"] = account.plan_chosen_at is not None
    payload["trial_used"] = trial_used(db, account.id)
    return payload


def _start_session(
    db: Session, account: Account, device, now
) -> dict:
    """Issue the one live refresh token for this device and build the session."""
    db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.device_id == device.id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )

    raw_token = new_refresh_token()
    db.add(
        RefreshToken(
            id=new_id(),
            account_id=account.id,
            device_id=device.id,
            token_hash=hash_token(raw_token),
            expires_at=now + timedelta(days=settings.refresh_token_days),
            created_at=now,
        )
    )
    db.commit()

    return _session_payload(db, account, device.id, raw_token, now)


@router.post("/register", status_code=201)
def register(
    body: RegisterRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    """Self-serve signup: no email step, the account works immediately.

    Only uniqueness of the email is enforced. The trial plan (when enabled) is
    granted here exactly once, so the caller gets a ready-to-use session.
    """
    email = normalize_email(body.email)
    existing = db.scalars(select(Account).where(Account.email == email)).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="email_taken")

    now = utcnow()
    account = Account(
        id=new_id(),
        email=email,
        password_hash=hash_password(body.password),
        email_verified_at=now,
    )
    db.add(account)
    try:
        db.commit()
    except IntegrityError as exc:
        # Two concurrent signups with the same address: first one wins.
        db.rollback()
        raise HTTPException(status_code=409, detail="email_taken") from exc
    db.refresh(account)

    client_ip = request.client.host if request.client else None
    device = bind_device(
        db,
        account,
        body.device_fingerprint,
        hash_ip(client_ip),
        now,
        body.device_details,
    )
    grant_trial(db, account, now)
    return _start_session(db, account, device, now)


@router.post("/login")
def login(
    body: LoginRequest, request: Request, db: Session = Depends(get_db)
) -> dict:
    email = normalize_email(body.email)
    account = db.scalars(select(Account).where(Account.email == email)).first()
    if account is None or not verify_password(account.password_hash, body.password):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    if account.status != "active":
        raise HTTPException(status_code=403, detail="account_disabled")

    now = utcnow()
    if account.email_verified_at is None:
        # Legacy rows from the SMTP-era schema: signup is self-activating now.
        account.email_verified_at = now
        db.commit()
        db.refresh(account)

    client_ip = request.client.host if request.client else None
    device = bind_device(
        db,
        account,
        body.device_fingerprint,
        hash_ip(client_ip),
        now,
        body.device_details,
    )
    grant_trial(db, account, now)

    return _start_session(db, account, device, now)


@router.post("/logout")
def logout(
    body: RefreshTokenRequest, db: Session = Depends(get_db)
) -> dict:
    token = db.scalars(
        select(RefreshToken).where(
            RefreshToken.token_hash == hash_token(body.refresh_token)
        )
    ).first()
    if token is not None and token.revoked_at is None:
        token.revoked_at = utcnow()
        db.commit()
    return {"ok": True}
