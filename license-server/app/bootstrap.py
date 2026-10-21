from __future__ import annotations

from sqlalchemy import select

from .config import settings
from .db import SessionLocal
from .models import AdminUser, Plan, new_id
from .security import hash_password

# Plans seeded only when the catalogue is completely empty. Once an operator
# owns the catalogue (MAX_PLANS = 4, edited in the panel) a restart must never
# add plans back, so an existing row of any name blocks seeding.
DEFAULT_PLANS = (
    {"name": "Free Trial", "duration_days": 7, "price_cents": 0, "is_trial": True},
    {"name": "Pro Monthly", "duration_days": 30, "price_cents": 1900, "is_trial": False},
    {"name": "Pro Yearly", "duration_days": 365, "price_cents": 19000, "is_trial": False},
)


def seed_defaults() -> None:
    """Create the plans (only on an empty catalogue) and the optional bootstrap admin."""
    with SessionLocal() as db:
        catalogue_exists = db.scalars(select(Plan.id).limit(1)).first() is not None
        if not catalogue_exists:
            for plan in DEFAULT_PLANS:
                db.add(
                    Plan(
                        id=new_id(),
                        name=plan["name"],
                        duration_days=plan["duration_days"],
                        price_cents=plan["price_cents"],
                        currency="USD",
                        is_trial=plan["is_trial"],
                        active=True,
                    )
                )
            db.commit()

        if not settings.admin_bootstrap_email:
            return

        if len(settings.admin_bootstrap_password) < 8:
            raise RuntimeError(
                "ADMIN_BOOTSTRAP_PASSWORD must be at least 8 characters"
            )

        existing = db.scalars(
            select(AdminUser).where(
                AdminUser.email == settings.admin_bootstrap_email
            )
        ).first()
        if existing is None:
            db.add(
                AdminUser(
                    id=new_id(),
                    email=settings.admin_bootstrap_email,
                    password_hash=hash_password(
                        settings.admin_bootstrap_password
                    ),
                    is_active=True,
                )
            )
            db.commit()
