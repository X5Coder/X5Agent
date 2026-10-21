from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import (
    Account,
    Device,
    Notice,
    Plan,
    Subscription,
    new_id,
)
from .timeutils import iso_z, utcnow

WORKING_STATES = frozenset({"trial", "active"})
BLOCKING_STATES = frozenset({"warning", "suspended", "expired", "none"})


def latest_subscription(db: Session, account_id: str) -> Subscription | None:
    return db.scalars(
        select(Subscription)
        .where(Subscription.account_id == account_id)
        .order_by(Subscription.ends_at.desc())
    ).first()


def effective_state(sub: Subscription | None, now: datetime) -> str:
    if sub is None:
        return "none"
    if sub.state in {"warning", "suspended"}:
        return sub.state
    if sub.ends_at <= now:
        return "expired"
    return sub.state


def sync_expired(db: Session, sub: Subscription | None, now: datetime) -> None:
    if sub is None:
        return
    if sub.state in {"trial", "active"} and sub.ends_at <= now:
        sub.state = "expired"
        sub.state_updated_at = now
        db.commit()


def trial_used(db: Session, account_id: str) -> bool:
    """The free trial is a one-time grant per account, never a repeatable plan."""
    return (
        db.scalars(
            select(Subscription.id).where(
                Subscription.account_id == account_id,
                Subscription.source == "trial",
            )
        ).first()
        is not None
    )


def grant_trial(db: Session, account: Account, now: datetime) -> Subscription | None:
    """Claim the one free trial.

    A user who bought a paid plan first still keeps the trial they never used:
    the trial days are appended to the running subscription, so paid time is
    never shortened and the trial is consumed exactly once (the ledger row
    below is what marks it used).
    """
    if account.email_verified_at is None:
        return None
    if trial_used(db, account.id):
        return None
    plan = db.scalars(
        select(Plan).where(Plan.is_trial.is_(True), Plan.active.is_(True))
    ).first()
    if plan is None:
        return None

    running = latest_subscription(db, account.id)
    if (
        running is not None
        and running.state in ("trial", "active", "warning")
        and running.ends_at > now
    ):
        running.ends_at = running.ends_at + timedelta(days=plan.duration_days)
        running.state_updated_at = now
        ledger = Subscription(
            id=new_id(),
            account_id=account.id,
            plan_id=plan.id,
            starts_at=now,
            ends_at=now,
            state="used",
            source="trial",
            state_updated_at=now,
        )
        db.add(ledger)
        db.commit()
        db.refresh(ledger)
        return ledger

    sub = Subscription(
        id=new_id(),
        account_id=account.id,
        plan_id=plan.id,
        starts_at=now,
        ends_at=now + timedelta(days=plan.duration_days),
        state="trial",
        source="trial",
        state_updated_at=now,
    )
    db.add(sub)
    db.commit()
    db.refresh(sub)
    return sub


def details_json(details) -> str | None:
    """Bounded JSON of the non-secret device marks (never a credential)."""
    if details is None:
        return None
    payload = {key: value for key, value in details.model_dump().items() if value}
    if not payload:
        return None
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)[:2048]


def update_device_details(device: Device, details) -> None:
    text = details_json(details)
    if text:
        device.details_json = text


def bind_device(
    db: Session,
    account: Account,
    fingerprint_hash: str,
    ip_hash: str | None,
    now: datetime,
    details=None,
) -> Device:
    """Last device wins: the newest login becomes the only active device."""
    device = db.scalars(
        select(Device).where(
            Device.account_id == account.id,
            Device.fingerprint_hash == fingerprint_hash,
        )
    ).first()

    if device is None:
        device = Device(
            id=new_id(),
            account_id=account.id,
            fingerprint_hash=fingerprint_hash,
            details_json=details_json(details),
            last_ip_hash=ip_hash,
            first_seen=now,
            last_seen=now,
            active=True,
        )
        db.add(device)
    else:
        device.active = True
        device.last_seen = now
        device.last_ip_hash = ip_hash
        update_device_details(device, details)

    db.flush()

    db.execute(
        update(Device)
        .where(
            Device.account_id == account.id,
            Device.id != device.id,
            Device.active.is_(True),
        )
        .values(active=False)
    )
    # Other devices keep their token row on purpose: their next heartbeat must
    # answer 409 device_superseded (not 401 invalid_token) so the client can show
    # the right notice. The device.active flag is what blocks them, and any later
    # login on that device revokes its old tokens.
    db.commit()
    db.refresh(device)
    return device


def issue_notice(
    db: Session, account_id: str, kind: str, dedupe_key: str
) -> Notice:
    existing = db.scalars(
        select(Notice).where(
            Notice.account_id == account_id, Notice.dedupe_key == dedupe_key
        )
    ).first()
    if existing is not None:
        return existing
    notice = Notice(
        id=new_id(),
        account_id=account_id,
        kind=kind,
        dedupe_key=dedupe_key,
        created_at=utcnow(),
    )
    db.add(notice)
    db.commit()
    db.refresh(notice)
    return notice


def ensure_state_notice(
    db: Session, account_id: str, sub: Subscription | None, state: str
) -> None:
    if sub is None:
        issue_notice(db, account_id, "subscription_required", f"none:{account_id}")
        return
    if state in {"warning", "suspended", "expired"}:
        stamp = sub.state_updated_at.isoformat(timespec="seconds")
        issue_notice(
            db,
            account_id,
            state,
            f"{state}:{sub.id}:{stamp}",
        )


def collect_notices(db: Session, account_id: str) -> list[dict]:
    rows = db.scalars(
        select(Notice)
        .where(Notice.account_id == account_id, Notice.delivered_at.is_(None))
        .order_by(Notice.created_at)
    ).all()
    if not rows:
        return []
    now = utcnow()
    payload = [
        {
            "id": notice.id,
            "kind": notice.kind,
            "created_at": iso_z(notice.created_at),
        }
        for notice in rows
    ]
    for notice in rows:
        notice.delivered_at = now
    db.commit()
    return payload


def plan_name_for(db: Session, sub: Subscription | None) -> str | None:
    if sub is None:
        return None
    plan = db.get(Plan, sub.plan_id)
    return plan.name if plan is not None else None


def subscription_payload(
    sub: Subscription | None,
    state: str,
    now: datetime,
    plan_name: str | None,
) -> dict:
    if sub is None:
        return {
            "state": state,
            "plan": None,
            "subscription_id": None,
            "starts_at": None,
            "expires_at": None,
            "remaining_seconds": 0,
            "can_work": state in WORKING_STATES,
        }
    remaining = max(0, int((sub.ends_at - now).total_seconds()))
    return {
        "state": state,
        "plan": plan_name,
        "subscription_id": sub.id,
        "starts_at": iso_z(sub.starts_at),
        "expires_at": iso_z(sub.ends_at),
        "remaining_seconds": remaining,
        "can_work": state in WORKING_STATES,
    }
