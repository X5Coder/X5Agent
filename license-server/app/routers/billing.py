from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from ..authn import bearer_scheme, require_device
from ..billing import (
    CheckoutError,
    auto_complete,
    checkout_url,
    create_checkout,
    get_session,
    session_view,
)
from ..db import get_db
from ..licensing import (
    effective_state,
    grant_trial,
    latest_subscription,
    plan_name_for,
    subscription_payload,
    sync_expired,
    trial_used,
)
from ..models import Account, Plan
from ..schemas import CheckoutRequest, CheckoutStatusRequest, ChoosePlanRequest
from ..timeutils import iso_z, utcnow

router = APIRouter(prefix="/v1/billing", tags=["billing"])


def _license_snapshot(db: Session, account: Account, now) -> dict:
    sub = latest_subscription(db, account.id)
    sync_expired(db, sub, now)
    state = effective_state(sub, now)
    snapshot = subscription_payload(sub, state, now, plan_name_for(db, sub))
    snapshot["plan_chosen"] = account.plan_chosen_at is not None
    snapshot["trial_used"] = trial_used(db, account.id)
    return snapshot


@router.post("/choose")
def choose_plan(
    body: ChoosePlanRequest,
    request: Request,
    credentials=Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> dict:
    """Record the onboarding choice.

    The free trial (and any plan with a zero price) is recorded immediately;
    a paid plan returns the provider checkout, and completion records the
    choice for us (see billing.complete_checkout).
    """
    account, _device = require_device(
        db, credentials, body.device_fingerprint, request
    )
    plan = db.get(Plan, body.plan_id)
    if plan is None or not plan.active:
        raise HTTPException(status_code=404, detail="plan_unavailable")
    now = utcnow()
    if plan.is_trial:
        # The trial is a one-time grant, and it stays available even after a
        # paid plan was bought first (its days are added to that subscription).
        if trial_used(db, account.id):
            raise HTTPException(status_code=409, detail="trial_already_used")
        grant_trial(db, account, now)
        if account.plan_chosen_at is None:
            account.plan_chosen_at = now
            db.commit()
            db.refresh(account)
        return {
            "chosen": True,
            "instant": True,
            "plan_id": plan.id,
            "license": _license_snapshot(db, account, now),
        }
    if plan.price_cents <= 0:
        # A genuine paid subscription can never be traded down to a free plan.
        running = latest_subscription(db, account.id)
        if (
            running is not None
            and running.state in ("active", "warning")
            and running.ends_at > now
        ):
            raise HTTPException(status_code=409, detail="plan_downgrade_refused")
        raise HTTPException(status_code=409, detail="plan_unavailable")
    try:
        session = create_checkout(db, account, plan, body.return_url, now)
    except CheckoutError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    subscription = auto_complete(db, session, now)
    if subscription is not None:
        # Sandbox: charged and active at once, no hosted page to visit.
        return {
            "chosen": True,
            "instant": True,
            "plan_id": plan.id,
            "subscription_id": subscription.id,
            "license": _license_snapshot(db, account, now),
        }
    return {
        "chosen": False,
        "plan_id": plan.id,
        "checkout": {
            "session_id": session.id,
            "checkout_url": checkout_url(session.id),
            "expires_at": iso_z(session.expires_at),
            "amount_cents": session.amount_cents,
            "currency": session.currency,
        },
    }


@router.post("/checkout")
def start_checkout(
    body: CheckoutRequest,
    request: Request,
    credentials=Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> dict:
    account, _device = require_device(
        db, credentials, body.device_fingerprint, request
    )
    plan = db.get(Plan, body.plan_id)
    now = utcnow()
    try:
        session = create_checkout(db, account, plan, body.return_url, now)
    except CheckoutError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc

    subscription = auto_complete(db, session, now)
    return {
        "session_id": session.id,
        "checkout_url": checkout_url(session.id),
        "expires_at": iso_z(session.expires_at),
        "amount_cents": session.amount_cents,
        "currency": session.currency,
        "instant": subscription is not None,
        "status": "paid" if subscription is not None else "pending",
        "license": (
            _license_snapshot(db, account, now) if subscription is not None else None
        ),
    }


@router.post("/checkout/status")
def checkout_status(
    body: CheckoutStatusRequest,
    request: Request,
    credentials=Depends(bearer_scheme),
    db: Session = Depends(get_db),
) -> dict:
    account, _device = require_device(
        db, credentials, body.device_fingerprint, request
    )
    session = get_session(db, body.session_id)
    if session is None or session.account_id != account.id:
        raise HTTPException(status_code=404, detail="checkout_not_found")
    now = utcnow()
    view = session_view(db, session, now)
    view["license"] = _license_snapshot(db, account, now)
    return view
