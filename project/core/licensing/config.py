"""License client configuration (single source of truth).

The server URL is the current test deployment. Move it with
X5_LICENSE_SERVER_URL when the production endpoint changes.

The entitlement public key is PINNED here: a compromised DNS or HTTP proxy
can never mint a working entitlement token. There is deliberately no env
override for the key.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SERVER_URL = "https://dex.awnlearn.com"

# Ed25519 public key of the central license server. The private key never
# leaves the server (license-server/keys/entitlement_private.pem).
ENTITLEMENT_PUBLIC_KEY_PEM = """-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAdz77OSSgq9mP/YtkNk36KccN9tGjoCUSvQ2fO/2bcZA=
-----END PUBLIC KEY-----"""

HEARTBEAT_INTERVAL_SECONDS = 600
ENTITLEMENT_LEEWAY_SECONDS = 120
# How long an expired (but correctly signed) token still lets a run continue
# while the license server is unreachable. The background heartbeat renews the
# token every 10 minutes, so this only covers outages and never a dead plan:
# the stored subscription end date is checked too.
LOCAL_GRACE_SECONDS = 3600
REQUEST_TIMEOUT_SECONDS = 10
BOOT_HEARTBEAT_TIMEOUT_SECONDS = 4.0
MAX_RETRIES = 3
MAX_RESPONSE_BYTES = 1_048_576
STATE_FILENAME = "license.json"


def server_url() -> str:
    raw = (os.getenv("X5_LICENSE_SERVER_URL") or DEFAULT_SERVER_URL).strip()
    return raw.rstrip("/")


def allow_insecure_http() -> bool:
    """HTTP is allowed for the current test deployment only.

    Set X5_LICENSE_ALLOW_HTTP=0 once the server has a TLS endpoint; the client
    then refuses plain http:// URLs (so a password/token is never sent in
    clear text by accident).
    """
    return (os.getenv("X5_LICENSE_ALLOW_HTTP") or "1").strip() != "0"


def public_key_pem() -> str:
    return ENTITLEMENT_PUBLIC_KEY_PEM


def state_path() -> Path:
    override = (os.getenv("X5_LICENSE_STATE_PATH") or "").strip()
    if override:
        return Path(override)
    from core.paths import data_root

    return data_root() / "database" / STATE_FILENAME


def bypass_enabled() -> bool:
    """Test-only escape hatch.

    Requires BOTH the flag and an active pytest marker, so a user cannot
    disable licensing by setting an environment variable in production.
    """
    flag = (os.getenv("X5_LICENSE_BYPASS") or "").strip().lower()
    if flag not in ("1", "true", "yes", "on"):
        return False
    return bool((os.getenv("PYTEST_CURRENT_TEST") or "").strip())
