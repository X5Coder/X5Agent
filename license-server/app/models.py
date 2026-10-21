from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base
from .timeutils import utcnow


def new_id() -> str:
    return str(uuid.uuid4())


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # Set once the owner has explicitly picked a plan during onboarding (the
    # first run after signup). NULL means "signup done, plan not chosen yet".
    plan_chosen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    fingerprint_hash: Mapped[str] = mapped_column(String(64), index=True)
    # Non-secret identity marks the client already collects (machine id, node
    # id, hostname, OS/platform): kept as bounded JSON for support, never a
    # secret and never sent back to another account.
    details_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), unique=True)
    duration_days: Mapped[int] = mapped_column(Integer, nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # What the plan card lists (one feature per line), and whether the card
    # carries the "Most popular" badge. Both are edited from the admin panel.
    features: Mapped[str] = mapped_column(Text, default="", nullable=False)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True, nullable=False)
    state: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    source: Mapped[str] = mapped_column(String(20), default="system", nullable=False)
    state_updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    device_id: Mapped[str] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class Notice(Base):
    __tablename__ = "notices"
    __table_args__ = (
        UniqueConstraint("account_id", "dedupe_key", name="uq_notices_account_dedupe"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(160), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


class CheckoutSession(Base):
    """One pending/paid purchase attempt (sandbox provider today, Dodo later)."""

    __tablename__ = "checkout_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[str] = mapped_column(ForeignKey("plans.id"), index=True)
    provider: Mapped[str] = mapped_column(String(20), default="sandbox", nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    currency: Mapped[str] = mapped_column(String(8), default="USD", nullable=False)
    return_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    subscription_id: Mapped[str | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
