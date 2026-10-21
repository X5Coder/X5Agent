from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .config import settings

_hasher = PasswordHasher()
_private_key: Ed25519PrivateKey | None = None
_public_pem: str | None = None


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def new_refresh_token() -> str:
    return secrets.token_urlsafe(48)


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def hash_ip(ip: str | None) -> str | None:
    """Store only a peppered hash of the client IP, never the raw address."""
    if not ip:
        return None
    return hashlib.sha256(f"{settings.jwt_secret}:{ip}".encode("utf-8")).hexdigest()


def init_keys() -> None:
    global _private_key, _public_pem

    keys_dir: Path = settings.keys_dir
    keys_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(keys_dir, 0o750)
    private_path = keys_dir / "entitlement_private.pem"
    public_path = keys_dir / "entitlement_public.pem"

    if not private_path.exists() or not public_path.exists():
        key = Ed25519PrivateKey.generate()
        private_path.write_bytes(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_path.write_bytes(
            key.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )
        os.chmod(private_path, 0o600)
        os.chmod(public_path, 0o644)

    # Enforce on every start so a pre-existing key cannot stay world-readable.
    os.chmod(private_path, 0o600)
    os.chmod(public_path, 0o644)

    loaded = serialization.load_pem_private_key(
        private_path.read_bytes(), password=None
    )
    if not isinstance(loaded, Ed25519PrivateKey):
        raise RuntimeError("entitlement key is not an Ed25519 private key")
    _private_key = loaded
    _public_pem = public_path.read_text(encoding="utf-8").strip()


def public_key_pem() -> str:
    if _public_pem is None:
        init_keys()
    assert _public_pem is not None
    return _public_pem


def issue_entitlement(
    account_id: str, device_id: str, state: str, plan_name: str | None
) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "iss": "x5agent-license",
        "aud": "x5agent-client",
        "sub": account_id,
        "did": device_id,
        "state": state,
        "plan": plan_name,
        "iat": now,
        "exp": now + timedelta(seconds=settings.entitlement_ttl_seconds),
        "jti": secrets.token_hex(16),
    }
    if _private_key is None:
        init_keys()
    assert _private_key is not None
    return jwt.encode(payload, _private_key, algorithm="EdDSA")


def issue_admin_token(admin_id: str, ttl_seconds: int | None = None) -> str:
    if ttl_seconds is None:
        ttl_seconds = settings.admin_session_seconds
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "iss": "x5agent-license",
            "sub": admin_id,
            "role": "admin",
            "iat": now,
            "exp": now + timedelta(seconds=ttl_seconds),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_admin_token(token: str) -> dict:
    return jwt.decode(
        token,
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer="x5agent-license",
        options={"require": ["exp", "sub"]},
    )
