"""Sandbox payment provider callbacks.

Only the sandbox checkout service calls these endpoints, and it proves itself
with the shared secret from the environment. The browser never sees the secret
and never posts the payment result itself, so a buyer cannot mark their own
session paid.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from ..billing import CheckoutError, complete_checkout, get_session, session_view
from ..config import settings
from ..db import get_db
from ..schemas import SandboxPayRequest
from ..timeutils import utcnow

router = APIRouter(prefix="/v1/sandbox", tags=["sandbox"])


def _require_sandbox(secret: str | None = Header(default=None, alias="X-Sandbox-Secret")) -> None:
    expected = settings.sandbox_shared_secret
    if not settings.sandbox_enabled:
        raise HTTPException(status_code=503, detail="checkout_unavailable")
    if not secret or not secrets.compare_digest(secret, expected):
        raise HTTPException(status_code=403, detail="sandbox_denied")


@router.get("/session/{session_id}", dependencies=[Depends(_require_sandbox)])
def sandbox_session(session_id: str, db: Session = Depends(get_db)) -> dict:
    session = get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="checkout_not_found")
    return session_view(db, session, utcnow())


@router.post("/pay", dependencies=[Depends(_require_sandbox)])
def sandbox_pay(body: SandboxPayRequest, db: Session = Depends(get_db)) -> dict:
    session = get_session(db, body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="checkout_not_found")
    now = utcnow()
    try:
        subscription = complete_checkout(db, session, now)
    except CheckoutError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.code) from exc
    view = session_view(db, session, now)
    view["subscription_id"] = subscription.id
    return view
