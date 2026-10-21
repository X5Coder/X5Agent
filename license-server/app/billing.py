"""Checkout sessions (sandbox provider now, Dodo later).

A purchase is one row: created when the console asks for a plan, completed by
the payment provider calling back with its shared secret. Completing twice is
safe (the same subscription is returned), an expired or cancelled session can
never be completed, and a suspended account cannot buy its way around a
support block.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .config import settings
from .licensing import latest_subscription
from .models import Account, CheckoutSession, Plan, Subscription, new_id
from .timeutils import iso_z, utcnow

STATUS_PENDING = "pending"
STATUS_PAID = "paid"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"


class CheckoutError(Exception):
    """A checkout action failed; code is safe to return to the caller."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = int(status)


def normalize_return_url(raw: str | None) -> str:
    """The console URL the buyer returns to (untrusted, validated)."""
    if raw is None or not str(raw).strip():
        return settings.checkout_return_url
    text = str(raw).strip()
    if len(text) > 2048:
        raise CheckoutError("bad_return_url", 422)
    parts = urlsplit(text)
    if (
        parts.scheme not in ("http", "https")
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise CheckoutError("bad_return_url", 422)
    return text


def checkout_url(session_id: str) -> str:
    return f"{settings.sandbox_public_url}/pay/{session_id}"


def get_session(db: Session, session_id: str) -> CheckoutSession | None:
    if not session_id or len(session_id) > 64:
        return None
    return db.get(CheckoutSession, session_id)


def expire_if_stale(db: Session, session: CheckoutSession, now: datetime) -> None:
    if session.status == STATUS_PENDING and session.expires_at <= now:
        session.status = STATUS_EXPIRED
        db.commit()


def create_checkout(
    db: Session,
    account: Account,
    plan: Plan | None,
    return_url: str | None,
    now: datetime,
) -> CheckoutSession:
    if plan is None or not plan.active or plan.is_trial:
        raise CheckoutError("plan_unavailable", 404)
    if account.status != "active":
        raise CheckoutError("account_disabled", 403)
    if not settings.sandbox_enabled:
        raise CheckoutError("checkout_unavailable", 503)
    latest = latest_subscription(db, account.id)
    if latest is not None and latest.state == "suspended":
        raise CheckoutError("account_suspended", 409)

    session = CheckoutSession(
        id=new_id(),
        account_id=account.id,
        plan_id=plan.id,
        provider=settings.checkout_provider,
        status=STATUS_PENDING,
        amount_cents=plan.price_cents,
        currency=plan.currency,
        return_url=normalize_return_url(return_url),
        created_at=now,
        expires_at=now + timedelta(seconds=settings.checkout_ttl_seconds),
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def complete_checkout(
    db: Session, session: CheckoutSession, now: datetime
) -> Subscription:
    """Mark a session paid and activate its plan (idempotent)."""
    if session.status == STATUS_PAID and session.subscription_id:
        existing = db.get(Subscription, session.subscription_id)
        if existing is not None:
            return existing
    if session.status != STATUS_PENDING:
        raise CheckoutError("checkout_not_pending", 409)
    if session.expires_at <= now:
        session.status = STATUS_EXPIRED
        db.commit()
        raise CheckoutError("checkout_expired", 409)

    account = db.get(Account, session.account_id)
    plan = db.get(Plan, session.plan_id)
    if account is None or account.status != "active":
        raise CheckoutError("account_disabled", 403)
    if plan is None or not plan.active or plan.is_trial:
        raise CheckoutError("plan_unavailable", 409)

    # Buying the same plan again extends the running period, so a renewal can
    # never throw away paid days. Buying a DIFFERENT paid plan is an upgrade:
    # the remaining paid days stay and the new plan's full duration is added on
    # top (total = remaining + new period). Only a trial (or no subscription) is
    # replaced outright by the purchase.
    running = db.scalars(
        select(Subscription)
        .where(
            Subscription.account_id == account.id,
            Subscription.state.in_(("trial", "active", "warning")),
            Subscription.ends_at > now,
        )
        .order_by(Subscription.ends_at.desc())
    ).first()

    if (
        running is not None
        and running.state != "trial"
        and running.plan_id == plan.id
        and plan.duration_days > 0
    ):
        running.ends_at = running.ends_at + timedelta(days=plan.duration_days)
        running.state = "active"
        running.state_updated_at = now
        subscription = running
        db.flush()
    elif running is not None and running.state in ("active", "warning"):
        # Paid -> paid upgrade: keep every remaining day and stack the new
        # plan's period on top of the current end.
        running.plan_id = plan.id
        running.ends_at = running.ends_at + timedelta(days=plan.duration_days)
        running.state = "active"
        running.source = session.provider
        running.state_updated_at = now
        subscription = running
        db.flush()
    else:
        # The paid plan replaces whatever the account was running (trial or an
        # older paid period) so the newest purchase is always the live one.
        db.execute(
            update(Subscription)
            .where(
                Subscription.account_id == account.id,
                Subscription.state.in_(("trial", "active", "warning")),
                Subscription.ends_at > now,
            )
            .values(state="ended", ends_at=now, state_updated_at=now)
        )
        subscription = Subscription(
            id=new_id(),
            account_id=account.id,
            plan_id=plan.id,
            starts_at=now,
            ends_at=now + timedelta(days=plan.duration_days),
            state="active",
            source=session.provider,
            state_updated_at=now,
            created_at=now,
        )
        db.add(subscription)
        # The FK is a plain column (no ORM relationship), so flush the new
        # subscription before the session row points at it: Postgres rejects an
        # update that references a row which is not inserted yet.
        db.flush()

    session.status = STATUS_PAID
    session.completed_at = now
    session.subscription_id = subscription.id
    session.amount_cents = plan.price_cents
    session.currency = plan.currency
    # A completed purchase is an explicit plan choice: onboarding is done.
    if account.plan_chosen_at is None:
        account.plan_chosen_at = now
    db.commit()
    db.refresh(subscription)
    return subscription


def auto_complete(db: Session, session: CheckoutSession, now: datetime) -> Subscription | None:
    """Sandbox provider has no card step: pay the session at once.

    The real provider (Dodo later) keeps the hosted checkout, so nothing here
    runs outside sandbox mode.
    """
    if settings.checkout_provider != "sandbox" or not settings.sandbox_enabled:
        return None
    return complete_checkout(db, session, now)


def session_view(db: Session, session: CheckoutSession, now: datetime) -> dict:
    """Safe shape shared by the console poll and the sandbox page."""
    expire_if_stale(db, session, now)
    plan = db.get(Plan, session.plan_id)
    return {
        "session_id": session.id,
        "status": session.status,
        "amount_cents": int(session.amount_cents),
        "currency": session.currency,
        "created_at": iso_z(session.created_at),
        "expires_at": iso_z(session.expires_at),
        "completed_at": iso_z(session.completed_at) if session.completed_at else None,
        "return_url": session.return_url or settings.checkout_return_url,
        "plan": {
            "id": plan.id if plan else "",
            "name": plan.name if plan else "",
            "duration_days": plan.duration_days if plan else 0,
            "price_cents": plan.price_cents if plan else 0,
            "currency": plan.currency if plan else session.currency,
        },
        "subscription_id": session.subscription_id,
    }
