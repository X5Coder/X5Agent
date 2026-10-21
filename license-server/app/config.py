from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

DEFAULT_JWT_SECRET = "change-me"


def _as_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc


def _as_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _as_url(name: str, default: str) -> str:
    raw = (os.getenv(name) or default).strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise RuntimeError(f"{name} must be an http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError(f"{name} must be a plain base URL")
    return raw


@dataclass(frozen=True)
class Settings:
    environment: str
    database_url: str
    jwt_secret: str
    entitlement_ttl_seconds: int
    refresh_token_days: int
    keys_dir: Path
    admin_bootstrap_email: str
    admin_bootstrap_password: str
    rate_limit_enabled: bool
    rate_limit_login_per_minute: int
    rate_limit_admin_login_per_minute: int
    checkout_provider: str
    sandbox_public_url: str
    sandbox_shared_secret: str
    checkout_ttl_seconds: int
    checkout_return_url: str
    admin_session_seconds: int

    @property
    def sandbox_enabled(self) -> bool:
        return self.checkout_provider == "sandbox" and bool(self.sandbox_shared_secret)


def load_settings() -> Settings:
    environment = os.getenv("ENVIRONMENT", "development").strip().lower()
    jwt_secret = os.getenv("JWT_SECRET", DEFAULT_JWT_SECRET)
    if environment == "production" and jwt_secret == DEFAULT_JWT_SECRET:
        raise RuntimeError("JWT_SECRET must be set to a real secret in production")

    return Settings(
        environment=environment,
        database_url=os.getenv("DATABASE_URL", "sqlite:///./license.db").strip(),
        jwt_secret=jwt_secret,
        entitlement_ttl_seconds=_as_int("ENTITLEMENT_TTL_SECONDS", 900),
        refresh_token_days=_as_int("REFRESH_TOKEN_DAYS", 90),
        keys_dir=Path(
            os.getenv("KEYS_DIR", str(Path(__file__).resolve().parent.parent / "keys"))
        ),
        admin_bootstrap_email=os.getenv("ADMIN_BOOTSTRAP_EMAIL", "").strip().lower(),
        admin_bootstrap_password=os.getenv("ADMIN_BOOTSTRAP_PASSWORD", ""),
        rate_limit_enabled=_as_bool("RATE_LIMIT_ENABLED", True),
        rate_limit_login_per_minute=max(
            1, _as_int("RATE_LIMIT_LOGIN_PER_MINUTE", 10)
        ),
        rate_limit_admin_login_per_minute=max(
            1, _as_int("RATE_LIMIT_ADMIN_LOGIN_PER_MINUTE", 5)
        ),
        checkout_provider=(os.getenv("CHECKOUT_PROVIDER", "sandbox").strip().lower()
                           or "sandbox"),
        sandbox_public_url=_as_url(
            "SANDBOX_PUBLIC_URL", "http://127.0.0.1:8789"
        ),
        sandbox_shared_secret=os.getenv("SANDBOX_SHARED_SECRET", "").strip(),
        checkout_ttl_seconds=max(60, _as_int("CHECKOUT_TTL_SECONDS", 1800)),
        checkout_return_url=_as_url(
            "CHECKOUT_RETURN_URL", "http://localhost:7777/subscription"
        ),
        # The panel keeps the admin signed in across restarts for a week unless
        # the operator sets a shorter window. The cookie is HttpOnly.
        admin_session_seconds=max(900, _as_int("ADMIN_SESSION_SECONDS", 604800)),
    )


settings = load_settings()
