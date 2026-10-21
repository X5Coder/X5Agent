"""Subscription page contract.

The page shows every plan on the very first visit and wires the console gate
at boot. License credentials live at the sign-in step only: neither the
Subscription page nor the first-run plan page may show an account form.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DASH = ROOT / "web" / "frontend" / "dashboard"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_page_has_pricing_layout_without_account_form():
    html = _read(DASH / "index.html")
    for token in (
        'data-page="subscription"',
        'class="pricing-page"',
        'id="subToolBtn"',
        'id="subPlans"',
        'class="sub-plans"',
    ):
        assert token in html, token
    assert "subAuthForm" not in html
    assert "Create your license account" not in html


def test_plans_page_shows_plans_only():
    html = _read(DASH / "plans.html")
    assert 'id="plans"' in html
    assert "authForm" not in html
    assert "authPanel" not in html
    assert "/api/license/login" not in html
    assert "/api/license/register" not in html


def test_subscription_js_owns_plans_and_gate():
    js = _read(DASH / "js" / "subscription.js")
    assert "/api/license/register" not in js
    assert "/api/license/login" not in js
    assert "export function initLicenseGate" in js
    assert "GATE_ALLOWED" in js
    assert "license-locked" in js
    assert "is-recommended" in js
    assert "plan-features" in js


def test_gate_is_wired_at_boot():
    app = _read(DASH / "js" / "app.js")
    assert "initLicenseGate" in app
    assert app.count("initLicenseGate()") >= 2  # cached + first-run boot paths


def test_subscription_module_has_a_cache_version():
    app = _read(DASH / "js" / "app.js")
    match = re.search(r"subscription\.js\?v=(\d+)", app)
    assert match, "app.js must import subscription.js with a cache version"


def test_subscription_section_has_no_inline_script_or_style():
    html = _read(DASH / "index.html")
    start = html.index('data-page="subscription"')
    end = html.index('data-page="settings"')
    section = html[start:end]
    assert "<script" not in section
    assert "style=" not in section
