from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Plan
from ..security import public_key_pem

router = APIRouter(prefix="/v1", tags=["public"])


@router.get("/plans")
def list_public_plans(db: Session = Depends(get_db)) -> list[dict]:
    plans = db.scalars(
        select(Plan).where(Plan.active.is_(True)).order_by(Plan.duration_days)
    ).all()
    return [
        {
            "id": plan.id,
            "name": plan.name,
            "duration_days": plan.duration_days,
            "price_cents": plan.price_cents,
            "currency": plan.currency,
            "is_trial": plan.is_trial,
            # Admin-controlled card content: one feature per line, and which
            # plan carries the "Most popular" badge.
            "features": [
                line.strip()
                for line in (plan.features or "").splitlines()
                if line.strip()
            ],
            "is_featured": plan.is_featured,
        }
        for plan in plans
    ]


@router.get("/public-key")
def get_public_key() -> dict:
    return {"algorithm": "EdDSA", "public_key_pem": public_key_pem()}
