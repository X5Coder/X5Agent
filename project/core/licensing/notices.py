"""Fixed user-facing license messages.

The server sends notice KINDS only (no admin free text), so every message is
a template defined here. Keeping them in one place means the chat, the
gateway and the Subscription page always say the same thing.
"""

from __future__ import annotations

# The public site. Every blocked message ends with it, so a user who cannot
# repair the license locally always has one fixed place to go.
SITE_URL = "https://x5agent.com"

WORKING_STATES = frozenset({"trial", "active"})
BLOCKING_NOTICE_KINDS = frozenset(
    {"warning", "suspended", "expired", "subscription_required", "none"}
)

MESSAGES: dict[str, str] = {
    "warning": (
        "Your subscription has a warning. The agent is paused until it is "
        "resolved. Contact support."
    ),
    "suspended": (
        "Your subscription is suspended. The agent is paused. Contact support."
    ),
    "expired": (
        "Your subscription has ended. Subscribe again on the Subscription "
        "page to keep using the agent."
    ),
    "subscription_required": (
        "No active subscription was found. Choose a plan on the Subscription "
        "page to activate one."
    ),
    "none": (
        "No active subscription was found. Choose a plan on the Subscription "
        "page to activate one."
    ),
    "signed_out": (
        "Sign in to your license account on the Subscription page to use the "
        "agent."
    ),
    "device_superseded": (
        "This account is active on another device. The agent is paused here. "
        "Sign in again on the Subscription page to use this device."
    ),
    "auth_required": (
        "Your license session expired. Sign in again on the Subscription page."
    ),
    "network": (
        "The license server cannot be reached right now. The agent is paused "
        "until it is verified. Check your connection and retry on the "
        "Subscription page."
    ),
    "invalid_entitlement": (
        "The license server returned an invalid entitlement. The agent is "
        "paused. Retry on the Subscription page."
    ),
    "account_disabled": "This account is disabled. Contact support.",
    "email_taken": "An account already exists for this email. Sign in instead.",
    "invalid_credentials": "The email or password is incorrect.",
    "rate_limited": "Too many attempts. Wait a moment, then try again.",
    "server_error": "The license server had a problem. Try again shortly.",
    "check_failed": (
        "The license check failed. Open the Subscription page and retry."
    ),
}

_GENERIC = MESSAGES["check_failed"]

# Local (offline) check failed and could not be repaired from disk.
LOCAL_CHECK_FAILED = (
    "The license check failed on this device and could not be repaired "
    "offline. Update the site and sign in again: " + SITE_URL
)


def notice_message(kind: str) -> str:
    return MESSAGES.get(str(kind or ""), _GENERIC)


def blocked_message(
    state: str,
    notices: list[dict] | None = None,
    last_error: str = "",
) -> str:
    """Short, fixed explanation for why the agent cannot run."""
    for notice in notices or []:
        if not isinstance(notice, dict):
            continue
        kind = str(notice.get("kind") or "")
        if kind in BLOCKING_NOTICE_KINDS:
            return notice_message(kind)
    if isinstance(state, str) and state in MESSAGES:
        return MESSAGES[state]
    if isinstance(last_error, str) and last_error.strip():
        return last_error.strip()[:400]
    return _GENERIC


def with_site(message: str) -> str:
    """Append the one fixed site address to a blocked message."""
    text = str(message or "").strip() or _GENERIC
    if SITE_URL in text:
        return text
    return text + " · " + SITE_URL
