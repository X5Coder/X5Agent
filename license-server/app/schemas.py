from __future__ import annotations

from pydantic import BaseModel, Field

FINGERPRINT_PATTERN = r"^[A-Fa-f0-9]{16,128}$"


class DeviceDetails(BaseModel):
    """Non-secret device marks sent with signup/login/heartbeat.

    These are exactly the values the client hashes into its fingerprint; the
    server keeps them for support (which machine is bound) and never treats
    them as credentials.
    """

    machine_id: str = Field(default="", max_length=128)
    node_id: str = Field(default="", max_length=64)
    hostname: str = Field(default="", max_length=128)
    system: str = Field(default="", max_length=32)
    machine: str = Field(default="", max_length=64)
    platform: str = Field(default="", max_length=128)
    python: str = Field(default="", max_length=32)
    # App watermark reported by the client: which build is installed and
    # whether its code still matches what was shipped (clean/modified/unknown).
    app_version: str = Field(default="", max_length=24)
    app_hash: str = Field(default="", max_length=32)
    app_state: str = Field(default="", max_length=16)


class RegisterRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    device_fingerprint: str = Field(
        min_length=16, max_length=128, pattern=FINGERPRINT_PATTERN
    )
    device_details: DeviceDetails | None = None


class LoginRequest(RegisterRequest):
    pass


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=10, max_length=512)


class HeartbeatRequest(BaseModel):
    device_fingerprint: str = Field(
        min_length=16, max_length=128, pattern=FINGERPRINT_PATTERN
    )
    device_details: DeviceDetails | None = None


class AdminLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class ExtendRequest(BaseModel):
    days: int = Field(ge=1, le=3650)


class ActivateRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    days: int | None = Field(default=None, ge=1, le=3650)


class PlanCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    duration_days: int = Field(ge=1, le=3650)
    price_cents: int = Field(default=0, ge=0)
    currency: str = Field(default="USD", min_length=3, max_length=8)
    is_trial: bool = False
    active: bool = True
    # One feature per line; empty falls back to the client's standard list.
    features: str = Field(default="", max_length=4000)
    is_featured: bool = False


class PlanUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    duration_days: int | None = Field(default=None, ge=1, le=3650)
    price_cents: int | None = Field(default=None, ge=0)
    currency: str | None = Field(default=None, min_length=3, max_length=8)
    is_trial: bool | None = None
    active: bool | None = None
    features: str | None = Field(default=None, max_length=4000)
    is_featured: bool | None = None


class CheckoutRequest(BaseModel):
    plan_id: str = Field(min_length=1, max_length=64)
    device_fingerprint: str = Field(
        min_length=16, max_length=128, pattern=FINGERPRINT_PATTERN
    )
    return_url: str | None = Field(default=None, max_length=2048)


class CheckoutStatusRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
    device_fingerprint: str = Field(
        min_length=16, max_length=128, pattern=FINGERPRINT_PATTERN
    )


class ChoosePlanRequest(BaseModel):
    """Onboarding plan choice: a trial is recorded at once, a paid plan opens
    the provider checkout and is recorded when the payment completes."""

    plan_id: str = Field(min_length=1, max_length=64)
    device_fingerprint: str = Field(
        min_length=16, max_length=128, pattern=FINGERPRINT_PATTERN
    )
    return_url: str | None = Field(default=None, max_length=2048)


class SandboxPayRequest(BaseModel):
    session_id: str = Field(min_length=8, max_length=64)
