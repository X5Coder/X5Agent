from __future__ import annotations

import re

from fastapi import HTTPException

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def normalize_email(raw: str) -> str:
    email = raw.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(status_code=422, detail="invalid_email")
    return email
