"""Subscription page contract.

The page must show every plan on the very first visit, own the license
account form (create/sign-in) and wire the console gate at boot.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "web" / "frontend" / "dashboard"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_page_has_pricing_layout_and_account_form():
    html = _read(DASH / "index.html")
    for token in (
        'data-page="subscription"',
        'class="pricing-page"',
        'id="subAuthForm"',
        'id="subAuthEmail"',
        'id="subAuthPassword"',
        'id="subPlans"',
        'id="subStatusLine"',
        'id="subMeta"',
        'id="subWait"',
    ):
        assert token in html, token
    assert "Plans &amp; pricing" in html


def test_subscription_js_owns_account_and_gate():
    js = _read(DASH / "js" / "subscription.js")
    assert "/api/license/register" in js
    assert "/api/license/login" in js
    assert "export function initLicenseGate" in js
    assert "GATE_ALLOWED" in js
    assert "license-locked" in js
    assert "is-recommended" in js
    assert "sub-features" in js


def test_gate_is_wired_at_boot():
    app = _read(DASH / "js" / "app.js")
    assert "initLicenseGate" in app
    assert app.count("initLicenseGate()") >= 2  # cached + first-run boot paths


def test_module_versions_agree():
    app = _read(DASH / "js" / "app.js")
    settings = _read(DASH / "js" / "settings.js")
    match = re.search(r"subscription\.js\?v=(\d+)", app)
    assert match, "app.js must import subscription.js with a cache version"
    assert "subscription.js?v=" + match.group(1) in settings


def test_subscription_section_has_no_inline_script_or_style():
    html = _read(DASH / "index.html")
    start = html.index('data-page="subscription"')
    end = html.index('data-page="settings"')
    section = html[start:end]
    assert "<script" not in section
    assert "style=" not in section
