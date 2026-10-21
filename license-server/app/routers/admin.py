from __future__ import annotations

from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import settings
from ..db import get_db
from ..licensing import (
    effective_state,
    issue_notice,
    latest_subscription,
    sync_expired,
    trial_used,
)
from ..models import (
    Account,
    AdminUser,
    CheckoutSession,
    Device,
    Plan,
    RefreshToken,
    Subscription,
    new_id,
)
from ..schemas import (
    ActivateRequest,
    AdminLoginRequest,
    ExtendRequest,
    PlanCreateRequest,
    PlanUpdateRequest,
)
from ..security import (
    decode_admin_token,
    issue_admin_token,
    verify_password,
)
from ..timeutils import iso_z, utcnow
from ..validation import normalize_email

router = APIRouter(prefix="/v1/admin", tags=["admin"])
_admin_bearer = HTTPBearer(auto_error=False)

# Four plans is the whole catalogue: free trial, one month, three months, year.
MAX_PLANS = 4

# The panel keeps one HttpOnly cookie so a refresh or a server restart never
# asks for the password again. A bearer header still wins (API clients, tests).
ADMIN_COOKIE = "x5admin"


def _cookie_secure(request: Request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    return request.url.scheme == "https" or proto == "https"


def _read_token(
    request: Request, credentials: HTTPAuthorizationCredentials | None
) -> str:
    if credentials is not None and credentials.credentials:
        return credentials.credentials
    return (request.cookies.get(ADMIN_COOKIE) or "").strip()


def require_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_admin_bearer),
    db: Session = Depends(get_db),
) -> AdminUser:
    raw = _read_token(request, credentials)
    if not raw:
        raise HTTPException(status_code=401, detail="missing_token")
    try:
        payload = decode_admin_token(raw)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="invalid_token") from exc
    admin = db.get(AdminUser, str(payload.get("sub", "")))
    if admin is None or not admin.is_active:
        raise HTTPException(status_code=401, detail="invalid_token")
    if payload.get("role") != "admin":
        raise HTTPException(status_code=403, detail="not_admin")
    return admin


def _account_or_404(db: Session, account_id: str) -> Account:
    account = db.get(Account, account_id)
    if account is None:
        raise HTTPException(status_code=404, detail="account_not_found")
    return account


def _subscription_or_409(db: Session, account_id: str) -> Subscription:
    sub = latest_subscription(db, account_id)
    if sub is None:
        raise HTTPException(status_code=409, detail="no_subscription")
    return sub


def _subscription_view(db: Session, sub: Subscription, now) -> dict:
    state = effective_state(sub, now)
    plan = db.get(Plan, sub.plan_id)
    return {
        "id": sub.id,
        "plan": plan.name if plan else None,
        "plan_id": sub.plan_id,
        "state": state,
        "stored_state": sub.state,
        "starts_at": iso_z(sub.starts_at),
        "expires_at": iso_z(sub.ends_at),
        "remaining_seconds": max(0, int((sub.ends_at - now).total_seconds())),
        "source": sub.source,
        "created_at": iso_z(sub.created_at),
    }


def checkout_view_status(session: CheckoutSession, now) -> str:
    """What the admin sees: paid, a live pending session, or failed.

    A pending session past its own expiry is dead - it used to sit in the
    dashboard as "waiting for payment" forever, so it is reported as failed.
    """
    if session.status == "paid":
        return "paid"
    if session.status == "pending" and session.expires_at > now:
        return "pending"
    return "failed"


@router.post("/login")
def admin_login(
    body: AdminLoginRequest,
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
) -> dict:
    email = normalize_email(body.email)
    admin = db.scalars(
        select(AdminUser).where(AdminUser.email == email)
    ).first()
    if (
        admin is None
        or not admin.is_active
        or not verify_password(admin.password_hash, body.password)
    ):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    token = issue_admin_token(admin.id)
    response.set_cookie(
        ADMIN_COOKIE,
        token,
        max_age=settings.admin_session_seconds,
        httponly=True,
        samesite="strict",
        secure=_cookie_secure(request),
        path="/",
    )
    return {
        "access_token": token,
        "token_type": "bearer",
        "expires_in": settings.admin_session_seconds,
        "email": admin.email,
    }


@router.get("/session")
def admin_session(admin: AdminUser = Depends(require_admin)) -> dict:
    """Who is signed in — the panel calls this on load to skip the login card."""
    return {"email": admin.email, "id": admin.id}


@router.post("/logout")
def admin_logout(response: Response) -> dict:
    response.delete_cookie(ADMIN_COOKIE, path="/")
    return {"ok": True}


@router.get("/accounts")
def list_accounts(
    search: str = "",
    limit: int = 50,
    offset: int = 0,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    base = select(Account)
    if search.strip():
        base = base.where(
            Account.email.ilike(f"%{search.strip().lower()}%")
        )

    total = db.scalar(select(func.count()).select_from(base.subquery())) or 0
    accounts = db.scalars(
        base.order_by(Account.created_at.desc()).offset(offset).limit(limit)
    ).all()
    if not accounts:
        return {"total": total, "items": []}

    ids = [account.id for account in accounts]
    subscriptions = db.scalars(
        select(Subscription)
        .where(Subscription.account_id.in_(ids))
        .order_by(Subscription.ends_at.desc())
    ).all()
    latest: dict[str, Subscription] = {}
    for sub in subscriptions:
        latest.setdefault(sub.account_id, sub)

    plan_ids = {sub.plan_id for sub in subscriptions}
    plans = (
        {
            plan.id: plan
            for plan in db.scalars(
                select(Plan).where(Plan.id.in_(plan_ids))
            ).all()
        }
        if plan_ids
        else {}
    )

    device_rows = db.execute(
        select(
            Device.account_id,
            func.count(Device.id),
            func.max(Device.last_seen),
        )
        .where(Device.account_id.in_(ids))
        .group_by(Device.account_id)
    ).all()
    device_map = {
        row[0]: {"count": int(row[1]), "last_seen": row[2]}
        for row in device_rows
    }

    now = utcnow()
    items = []
    for account in accounts:
        sub = latest.get(account.id)
        state = effective_state(sub, now)
        device_info = device_map.get(account.id, {})
        items.append(
            {
                "id": account.id,
                "email": account.email,
                "status": account.status,
                "state": state,
                "plan": plans[sub.plan_id].name
                if sub is not None and sub.plan_id in plans
                else None,
                "expires_at": iso_z(sub.ends_at) if sub else None,
                "remaining_seconds": max(
                    0, int((sub.ends_at - now).total_seconds())
                )
                if sub
                else 0,
                "devices": device_info.get("count", 0),
                "last_seen": iso_z(device_info.get("last_seen")),
                "created_at": iso_z(account.created_at),
            }
        )
    return {"total": total, "items": items}


@router.get("/accounts/{account_id}")
def account_detail(
    account_id: str,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    account = _account_or_404(db, account_id)
    now = utcnow()

    subscriptions = db.scalars(
        select(Subscription)
        .where(Subscription.account_id == account.id)
        .order_by(Subscription.ends_at.desc())
    ).all()
    devices = db.scalars(
        select(Device)
        .where(Device.account_id == account.id)
        .order_by(Device.last_seen.desc())
    ).all()
    latest = subscriptions[0] if subscriptions else None
    if latest is not None:
        sync_expired(db, latest, now)

    checkouts = db.scalars(
        select(CheckoutSession)
        .where(CheckoutSession.account_id == account.id)
        .order_by(CheckoutSession.created_at.desc())
        .limit(20)
    ).all()
    plan_names = {
        plan.id: plan.name
        for plan in db.scalars(select(Plan)).all()
    }
    active_tokens = db.scalars(
        select(RefreshToken).where(
            RefreshToken.account_id == account.id,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
    ).all()
    device_ids = {device.id for device in devices}
    tokens_by_device: dict[str, int] = {}
    for token in active_tokens:
        if token.device_id in device_ids:
            tokens_by_device[token.device_id] = tokens_by_device.get(token.device_id, 0) + 1

    # The password is stored as Argon2id only, so it can never be shown. The
    # admin sees the algorithm, the hash prefix and the length: enough to
    # identify the stored record without shipping the whole hash to a browser.
    password_hash = str(account.password_hash or "")
    algo = password_hash.split("$")[1] if password_hash.startswith("$") else "unknown"

    return {
        "account": {
            "id": account.id,
            "email": account.email,
            "status": account.status,
            "created_at": iso_z(account.created_at),
            "plan_chosen_at": iso_z(account.plan_chosen_at),
            "email_verified_at": iso_z(account.email_verified_at),
            "password_algo": algo,
            "password_hash_prefix": password_hash[:30],
            "password_hash_length": len(password_hash),
            "has_password": bool(password_hash),
        },
        "current": _subscription_view(db, latest, now)
        if latest is not None
        else None,
        "subscriptions": [
            _subscription_view(db, sub, now) for sub in subscriptions
        ],
        "trial_used": bool(trial_used(db, account.id)),
        "devices": [
            {
                "id": device.id,
                "fingerprint_hash": device.fingerprint_hash,
                "active": device.active,
                "first_seen": iso_z(device.first_seen),
                "last_seen": iso_z(device.last_seen),
                "details": _device_details(device.details_json),
                "last_ip_hash": device.last_ip_hash,
                "session_tokens": tokens_by_device.get(device.id, 0),
            }
            for device in devices
        ],
        "active_devices": sum(1 for device in devices if device.active),
        "session_tokens": len(active_tokens),
        "checkouts": [
            {
                "id": checkout.id,
                "plan": plan_names.get(checkout.plan_id, ""),
                "provider": checkout.provider,
                "status": checkout_view_status(checkout, now),
                "amount_cents": checkout.amount_cents,
                "currency": checkout.currency,
                "created_at": iso_z(checkout.created_at),
                "completed_at": iso_z(checkout.completed_at),
                "subscription_id": checkout.subscription_id,
            }
            for checkout in checkouts
        ],
    }


def _device_details(raw: str | None) -> dict:
    """Bounded, parsed device identity fields (never a secret)."""
    if not raw:
        return {}
    try:
        import json

        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    allowed = {
        "machine_id",
        "node_id",
        "hostname",
        "system",
        "machine",
        "platform",
        "python",
        # App watermark: which build this device runs and whether its source
        # is still the shipped one (clean) or has been modified.
        "app_version",
        "app_hash",
        "app_state",
    }
    return {
        str(key): str(value)[:200]
        for key, value in data.items()
        if str(key) in allowed
    }


@router.post("/accounts/{account_id}/warn")
def warn_account(
    account_id: str,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    _account_or_404(db, account_id)
    sub = _subscription_or_409(db, account_id)
    now = utcnow()
    sub.state = "warning"
    sub.state_updated_at = now
    db.commit()
    issue_notice(
        db,
        account_id,
        "warning",
        f"warning:{sub.id}:{now.isoformat(timespec='seconds')}",
    )
    return {"ok": True, "state": "warning"}


@router.post("/accounts/{account_id}/suspend")
def suspend_account(
    account_id: str,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    _account_or_404(db, account_id)
    sub = _subscription_or_409(db, account_id)
    now = utcnow()
    sub.state = "suspended"
    sub.state_updated_at = now
    db.commit()
    issue_notice(
        db,
        account_id,
        "suspended",
        f"suspended:{sub.id}:{now.isoformat(timespec='seconds')}",
    )
    return {"ok": True, "state": "suspended"}


@router.post("/accounts/{account_id}/cancel")
def cancel_subscription(
    account_id: str,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Cancel the running plan: it ends now instead of at its end date."""
    _account_or_404(db, account_id)
    sub = _subscription_or_409(db, account_id)
    now = utcnow()
    sub.state = "expired"
    sub.ends_at = now
    sub.state_updated_at = now
    db.commit()
    issue_notice(
        db,
        account_id,
        "expired",
        f"cancelled:{sub.id}:{now.isoformat(timespec='seconds')}",
    )
    return {"ok": True, "state": "expired"}


@router.post("/accounts/{account_id}/clear")
def clear_block(
    account_id: str,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    _account_or_404(db, account_id)
    sub = _subscription_or_409(db, account_id)
    if sub.state not in {"warning", "suspended"}:
        raise HTTPException(status_code=409, detail="not_blocked")

    now = utcnow()
    plan = db.get(Plan, sub.plan_id)
    if sub.ends_at <= now:
        state = "expired"
    elif plan is not None and plan.is_trial:
        state = "trial"
    else:
        state = "active"

    sub.state = state
    sub.state_updated_at = now
    db.commit()
    issue_notice(
        db,
        account_id,
        "reactivated",
        f"reactivated:{sub.id}:{now.isoformat(timespec='seconds')}",
    )
    return {"ok": True, "state": state}


@router.post("/accounts/{account_id}/extend")
def extend_subscription(
    account_id: str,
    body: ExtendRequest,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    _account_or_404(db, account_id)
    sub = _subscription_or_409(db, account_id)
    now = utcnow()
    sub.ends_at = max(now, sub.ends_at) + timedelta(days=body.days)
    plan = db.get(Plan, sub.plan_id)
    sub.state = "trial" if plan is not None and plan.is_trial else "active"
    sub.state_updated_at = now
    sub.source = "admin"
    db.commit()
    db.refresh(sub)
    return {
        "ok": True,
        "expires_at": iso_z(sub.ends_at),
        "remaining_seconds": max(0, int((sub.ends_at - now).total_seconds())),
    }


@router.post("/accounts/{account_id}/activate")
def activate_subscription(
    account_id: str,
    body: ActivateRequest,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    _account_or_404(db, account_id)
    plan = db.get(Plan, body.plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan_not_found")
    if not plan.active:
        raise HTTPException(status_code=409, detail="plan_inactive")

    now = utcnow()
    days = body.days if body.days is not None else plan.duration_days
    state = "trial" if plan.is_trial else "active"
    sub = latest_subscription(db, account_id)

    if sub is None:
        sub = Subscription(
            id=new_id(),
            account_id=account_id,
            plan_id=plan.id,
            starts_at=now,
            ends_at=now + timedelta(days=days),
            state=state,
            source="admin",
            state_updated_at=now,
        )
        db.add(sub)
    else:
        sub.plan_id = plan.id
        sub.starts_at = now
        sub.ends_at = now + timedelta(days=days)
        sub.state = state
        sub.source = "admin"
        sub.state_updated_at = now

    db.commit()
    db.refresh(sub)
    return {
        "ok": True,
        "subscription_id": sub.id,
        "state": state,
        "expires_at": iso_z(sub.ends_at),
    }


@router.get("/stats")
def stats(
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    now = utcnow()
    total_accounts = db.scalar(select(func.count()).select_from(Account)) or 0
    active_paid = (
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.state == "active",
                Subscription.ends_at > now,
            )
        )
        or 0
    )
    active_trials = (
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(
                Subscription.state == "trial",
                Subscription.ends_at > now,
            )
        )
        or 0
    )
    expired = (
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.ends_at <= now)
        )
        or 0
    )
    blocked = (
        db.scalar(
            select(func.count())
            .select_from(Subscription)
            .where(Subscription.state.in_(["warning", "suspended"]))
        )
        or 0
    )
    return {
        "accounts": total_accounts,
        "active_paid": active_paid,
        "active_trials": active_trials,
        "expired": expired,
        "blocked": blocked,
    }


@router.get("/plans")
def admin_plans(
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> list[dict]:
    plans = db.scalars(select(Plan).order_by(Plan.duration_days)).all()
    return [
        {
            "id": plan.id,
            "name": plan.name,
            "duration_days": plan.duration_days,
            "price_cents": plan.price_cents,
            "currency": plan.currency,
            "is_trial": plan.is_trial,
            "active": plan.active,
            "features": plan.features or "",
            "is_featured": plan.is_featured,
        }
        for plan in plans
    ]


@router.post("/plans", status_code=201)
def create_plan(
    body: PlanCreateRequest,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="invalid_plan_name")
    existing = db.scalar(select(func.count()).select_from(Plan)) or 0
    if existing >= MAX_PLANS:
        raise HTTPException(status_code=409, detail="plan_limit_reached")
    plan = Plan(
        id=new_id(),
        name=name,
        duration_days=body.duration_days,
        price_cents=body.price_cents,
        currency=body.currency.upper(),
        is_trial=body.is_trial,
        active=body.active,
        features=(body.features or "").strip(),
        is_featured=body.is_featured,
    )
    db.add(plan)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="plan_name_taken"
        ) from exc
    db.refresh(plan)
    return {"ok": True, "plan_id": plan.id}


@router.patch("/plans/{plan_id}")
def update_plan(
    plan_id: str,
    body: PlanUpdateRequest,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan_not_found")

    changes = body.model_dump(exclude_unset=True)
    for key, value in changes.items():
        if key == "name":
            value = str(value).strip()
            if not value:
                raise HTTPException(
                    status_code=422, detail="invalid_plan_name"
                )
        if key == "currency" and value is not None:
            value = str(value).upper()
        setattr(plan, key, value)

    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(
            status_code=409, detail="plan_name_taken"
        ) from exc
    db.refresh(plan)
    return {"ok": True, "plan_id": plan.id}


@router.delete("/plans/{plan_id}")
def delete_plan(
    plan_id: str,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Remove a plan nobody has used. Plans with history are disabled instead."""
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="plan_not_found")

    subscriptions = db.scalar(
        select(func.count())
        .select_from(Subscription)
        .where(Subscription.plan_id == plan_id)
    )
    checkouts = db.scalar(
        select(func.count())
        .select_from(CheckoutSession)
        .where(CheckoutSession.plan_id == plan_id)
    )
    if subscriptions or checkouts:
        # Deleting would either orphan history (Postgres refuses it anyway) or
        # erase what an account was sold: disable keeps both honest.
        raise HTTPException(status_code=409, detail="plan_in_use")

    db.delete(plan)
    db.commit()
    return {"ok": True, "deleted": plan_id}


@router.get("/billing")
def admin_billing(
    limit: int = 20,
    admin: AdminUser = Depends(require_admin),
    db: Session = Depends(get_db),
) -> dict:
    """Read-only view of checkout sessions (the one billing truth right now)."""
    limit = max(1, min(100, int(limit)))
    sessions = db.scalars(
        select(CheckoutSession)
        .order_by(CheckoutSession.created_at.desc())
        .limit(limit)
    ).all()
    now = utcnow()
    items = []
    for session in sessions:
        account = db.get(Account, session.account_id)
        plan = db.get(Plan, session.plan_id)
        items.append(
            {
                "id": session.id,
                "email": account.email if account is not None else "",
                "plan": plan.name if plan is not None else "",
                "status": checkout_view_status(session, now),
                "amount_cents": session.amount_cents,
                "currency": session.currency,
                "created_at": iso_z(session.created_at),
            }
        )

    def count(*conditions) -> int:
        return int(
            db.scalar(
                select(func.count())
                .select_from(CheckoutSession)
                .where(*conditions)
            )
            or 0
        )

    paid_cents = int(
        db.scalar(
            select(func.coalesce(func.sum(CheckoutSession.amount_cents), 0)).where(
                CheckoutSession.status == "paid"
            )
        )
        or 0
    )
    # A checkout ends paid or failed. A pending session counts as live only
    # until its own expiry; after that it is a failed attempt, never a
    # "waiting for payment" row that never resolves.
    live_pending = count(
        CheckoutSession.status == "pending", CheckoutSession.expires_at > now
    )
    dead_pending = count(
        CheckoutSession.status == "pending", CheckoutSession.expires_at <= now
    )
    paid_count = count(CheckoutSession.status == "paid")
    return {
        "totals": {
            "sessions": count(),
            "paid": paid_count,
            "failed": count(CheckoutSession.status.in_(("expired", "cancelled")))
            + dead_pending,
            "pending": live_pending,
            "paid_cents": paid_cents,
        },
        "items": items,
    }
