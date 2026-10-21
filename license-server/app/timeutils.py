from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Return a naive UTC timestamp.

    The database stores naive datetimes so SQLite and Postgres behave the same.
    Every value written here is UTC; every comparison is done with this helper.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso_z(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds") + "Z"
