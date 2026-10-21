/**
 * subscription.js — the plan page inside the console + the license gate.
 *
 * Owns:
 *  - the same glass plan cards as the first-run page (with the sidebar visible),
 *  - the license account form (only when the stored session is gone),
 *  - the purchase hand-off: a plan is stored server-side and the card updates in
 *    place on this page (a hosted provider page is used only when the license
 *    server returns one),
 *  - the hard gate: the console stays locked on this page until the account
 *    exists on the license server AND is allowed to work on this device.
 *
 * The license key can never be stopped from the UI: there is deliberately no
 * disconnect action anywhere in the console.
 */
"use strict";

import { toast } from "./ui.js?v=13";
import { navigate } from "./navigation.js?v=15";

const PENDING_KEY = "x5_pending_checkout";
const LOCK_CLASS = "license-locked";
// Pages reachable while the license gate is closed. The studio shell has its
// own server-side gate and must not be torn down from under a running design.
const GATE_ALLOWED = new Set(["subscription", "settings"]);

let _plans = null;
let _plansError = "";
let _status = null;
let _busy = false;
let _gateLocked = false;
let _gateWired = false;
// Bumped on every real change: an unchanged refresh must not rebuild the grid
// (that is what replayed the entrance animation twice on page open).
let _renderKey = "";

async function _getJson(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.ok === false) {
    if (!data.error && r.status >= 500) {
      throw new Error("The license server did not answer (" + r.status + "). Try again in a moment.");
    }
    throw new Error(data.error || "Request failed (" + r.status + ")");
  }
  return data;
}

/* ------------------------------------------------------------ small format */

function _money(cents, currency) {
  const value = Math.max(0, Number(cents) || 0) / 100;
  const code = String(currency || "USD").toUpperCase().slice(0, 8);
  try {
    return new Intl.NumberFormat("en-US", {
      style: "currency",
      currency: code,
      maximumFractionDigits: value % 1 === 0 ? 0 : 2,
    }).format(value);
  } catch (_) {
    return value.toFixed(2) + " " + code;
  }
}

function _remaining(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  if (days > 0) return days + " day" + (days === 1 ? "" : "s") + (hours ? " " + hours + "h" : "");
  if (hours > 0) return hours + " hour" + (hours === 1 ? "" : "s");
  return Math.max(0, Math.floor(total / 60)) + " min";
}

function _plansText(plan) {
  const days = Number(plan.duration_days) || 0;
  if (plan.is_trial) return days + (days === 1 ? " day" : " days");
  if (days === 30) return "month";
  if (days === 90) return "3 months";
  if (days === 365) return "year";
  return days + " days";
}

function _billingText(plan) {
  const days = Number(plan.duration_days) || 0;
  if (days === 365) return "billed yearly";
  if (days === 90) return "billed every 3 months";
  return "billed monthly";
}

function _tierName(plan) {
  // The admin owns plan names (Free / Pro / Pro Plus / Ultra).
  return String(plan.name || "Plan").replace(/^X5Agent\s*[-–]\s*/i, "");
}

// The same feature list on every card, unless the admin writes a custom one
// for that plan in the panel (one feature per line).
const DEFAULT_FEATURES = [
  "Your own complete personal agent",
  "Continuous feature updates",
  "Your data stays on your device",
  "Full browser control",
  "One session, usable on any device",
  "Every feature unlocked, including future ones",
];

function _features(plan) {
  const custom = Array.isArray(plan.features) ? plan.features.filter(Boolean) : [];
  return custom.length ? custom : DEFAULT_FEATURES;
}

function _ctaLabel(plan) {
  if (plan.is_trial) return "Start free";
  return "Get " + _tierName(plan);
}

/**
 * What a longer plan saves against paying the monthly plan every month.
 * Derived from the live prices, so the admin only edits prices.
 */
function _savings(plan) {
  if (plan.is_trial) return "";
  const days = Number(plan.duration_days) || 0;
  const monthly = (_plans || []).find(
    (item) => !item.is_trial && Number(item.duration_days) === 30 && Number(item.price_cents) > 0
  );
  if (!monthly || days <= 30) return "";
  const months = Math.round(days / 30);
  const full = Number(monthly.price_cents) * months;
  const price = Number(plan.price_cents) || 0;
  const saved = Math.round(full - price);
  if (saved <= 0) return "";
  const percent = Math.round((saved / full) * 100);
  return "Save " + _money(saved, plan.currency) + " (" + percent + "%)";
}

function _checkSvg() {
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m20 6-11 11-5-5"/></svg>';
}

function _markSvg() {
  return '<svg viewBox="0 0 32 32" fill="none" xmlns="http://www.w3.org/2000/svg">' +
    '<rect x="6" y="6" width="20" height="20" rx="5" fill="#fff"/></svg>';
}

function _esc(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* --------------------------------------------------------------------- plans */

function _licenseWorks(license) {
  return Boolean(license && license.connected && license.can_work);
}

function _isCurrentPlan(plan, license) {
  if (!_licenseWorks(license)) return false;
  if (plan.is_trial) return String(license.state || "") === "trial";
  return String(license.plan || "") === String(plan.name || "");
}

/** The running plan's own row (for price comparison when upgrading). */
function _currentPlanRow(license) {
  if (!_licenseWorks(license) || !license.plan) return null;
  return (_plans || []).find((p) => String(p.name || "") === String(license.plan)) || null;
}

function _planCard(plan, license) {
  const card = document.createElement("article");
  const current = _isCurrentPlan(plan, license);
  // "Most popular" is chosen in the admin panel, never inferred from price.
  card.className = "plan-card" + (plan.is_featured ? " is-recommended" : "");

  const paidActive = _licenseWorks(license) && String(license.state || "") === "active";
  // The free trial is a one-time grant: it stays claimable even after a paid
  // plan was bought first (the server adds its days on top), and it is simply
  // spent once it has been used.
  const trialUsed = Boolean(license && license.trial_used);
  const trialBlocked = plan.is_trial && trialUsed;

  let badge = "";
  if (current) badge = '<span class="plan-badge is-blue">Active</span>';
  else if (plan.is_featured) badge = '<span class="plan-badge is-blue">Most popular</span>';
  else if (plan.is_trial) badge = '<span class="plan-badge">Free</span>';

  const priceHtml = plan.is_trial
    ? '<div class="plan-price"><span class="amount">$0</span><span class="per">' +
      _plansText(plan) + ' free<br>no card required</span></div>'
    : '<div class="plan-price"><span class="amount">' + _money(plan.price_cents, plan.currency) +
      '</span><span class="per">' + _esc(plan.currency || "USD") + " / " + _plansText(plan) +
      '<br>' + _billingText(plan) + '</span></div>';

  let note;
  if (plan.is_trial) {
    if (trialUsed) note = "Your free trial was already used";
    else if (paidActive) note = "+" + (Number(plan.duration_days) || 0) + " free days on your plan";
    else note = "No card required";
  } else {
    // Longer plans show what they save against the monthly price.
    note = _savings(plan);
  }
  const list = _features(plan).map((f) => "<li>" + _checkSvg() + _esc(f) + "</li>").join("");

  card.innerHTML =
    badge +
    '<div class="plan-mark" aria-hidden="true">' + _markSvg() + "</div>" +
    "<h2>" + _esc(_tierName(plan)) + "</h2>" +
    priceHtml +
    '<button class="plan-cta" type="button"><span class="plan-spinner"></span><span class="cta-text">' +
    _esc(_ctaLabel(plan)) + "</span></button>" +
    // Keep the note row even when empty so the skeleton grid and the real
    // cards (priced the same) never shift by one line.
    '<p class="plan-note' + (note && !plan.is_trial ? " is-save" : "") + '">' +
      (note ? _esc(note) : "&nbsp;") + "</p>" +
    '<div class="plan-rule"></div>' +
    '<ul class="plan-features">' + list + "</ul>";

  const btn = card.querySelector(".plan-cta");
  // The label lives in its own span so the busy spinner survives a relabel.
  const label = card.querySelector(".plan-cta .cta-text");
  const setLabel = (text) => {
    if (label) label.textContent = text;
    else btn.textContent = text;
  };
  const currentRow = _currentPlanRow(license);
  const upgrade = Boolean(
    currentRow && !plan.is_trial && Number(plan.price_cents) > Number(currentRow.price_cents)
  );

  if (current) {
    setLabel("Current plan");
    btn.disabled = true;
  } else if (trialBlocked) {
    setLabel("Trial used");
    btn.disabled = true;
  } else if (!license || !license.connected) {
    setLabel("Sign in required");
    btn.addEventListener("click", _askSignIn);
  } else {
    // One label shape for every plan: Get <name> (or Start free).
    const labelText = plan.is_trial
      ? (paidActive ? "Use free trial" : _ctaLabel(plan))
      : _ctaLabel(plan);
    setLabel(labelText);
    btn.addEventListener("click", () => _startCheckout(plan, btn));
  }
  return card;
}

function _renderPlans(plans, license) {
  const box = document.getElementById("subPlans");
  if (!box) return;
  if (!plans) {
    _renderKey = "";
    box.textContent = "";
    const loading = document.createElement("p");
    loading.className = "sub-loading";
    loading.textContent = _plansError || "Loading plans…";
    box.append(loading);
    return;
  }
  const identity = (license && [license.state, license.plan, license.connected, license.can_work].join("|")) || "";
  const key =
    plans
      .map(
        (p) =>
          `${p.id}:${p.price_cents}:${p.duration_days}:${p.is_trial ? 1 : 0}:${p.is_featured ? 1 : 0}:${(p.features || []).join("|")}`
      )
      .join(",") + "#" + identity;
  if (key === _renderKey && box.children.length) return;
  _renderKey = key;
  box.textContent = "";
  if (!plans.length) {
    const empty = document.createElement("p");
    empty.className = "sub-loading";
    empty.textContent = "No plans are available right now.";
    box.append(empty);
    return;
  }
  // Free trial first, then paid plans cheapest-first.
  const ordered = plans
    .filter((p) => p && p.id)
    .slice()
    .sort((a, b) => {
      if (Boolean(b.is_trial) !== Boolean(a.is_trial)) return a.is_trial ? -1 : 1;
      return Number(a.price_cents) - Number(b.price_cents);
    });
  for (const plan of ordered) {
    box.append(_planCard(plan, license));
  }
  // First paint animates; later rebuilds (a purchase, a sign-in) stay still.
  // Marked after the rise finishes, so an in-flight animation is never cut.
  if (box.dataset.rendered === "1") return;
  setTimeout(() => { box.dataset.rendered = "1"; }, 650);
}

/* ----------------------------------------------------------- status + auth */

function _askSignIn() {
  // One credential store: the console sign-in also restores the license
  // session, so a lost license session simply goes there (no form here).
  location.assign("/login.html?relogin=1&next=" + encodeURIComponent("/subscription"));
}

/** Send a signed-out console to the sign-in page once (never a redirect loop). */
function _bounceToSignIn() {
  const KEY = "x5_license_bounce_at";
  try {
    // One automatic attempt per tab: a refused sign-in must not ping-pong
    // between this page and the sign-in page. A fresh tab tries again.
    if (sessionStorage.getItem(KEY) === "1") return;
    sessionStorage.setItem(KEY, "1");
  } catch (_) {
    return;
  }
  location.assign("/login.html?relogin=1&next=" + encodeURIComponent("/subscription"));
}

/** Tell the rest of the console (dashboard bar, chat gate) the plan moved. */
function _notifyLicenseChanged() {
  try {
    window.dispatchEvent(new CustomEvent("x5:license-changed"));
  } catch (_) {}
}

function _renderStatus(license) {
  _status = license || null;
  if (license && license.connected) {
    try {
      sessionStorage.removeItem("x5_license_bounce_at");
    } catch (_) {}
  } else {
    // The sign-in page stashes the license server's own refusal reason, so a
    // failed recovery says why instead of repeating "sign in again".
    try {
      const why = sessionStorage.getItem("x5_license_err");
      if (why) {
        sessionStorage.removeItem("x5_license_err");
        toast(why);
      }
    } catch (_) {}
  }
  _applyGate(license, true);
  _renderPlans(_plans, license);
}

/* ------------------------------------------------------------------- the gate */

function _isStudioRoute() {
  return (location.pathname || "").indexOf("/studio-pdf/") === 0;
}

function _applyGate(license, known) {
  _gateLocked = !_licenseWorks(license);
  document.body.classList.toggle(LOCK_CLASS, _gateLocked);
  if (!_gateLocked) return;
  // No answer yet (boot): keep the page the user asked for, only block
  // navigation. A real answer that is not allowed forces the plans page.
  if (known === false) return;
  // A signed-out install has no license session anywhere: the sign-in page is
  // the only place that can restore it, so go there instead of a dead end.
  if (!license || !license.connected) {
    _bounceToSignIn();
    return;
  }
  if (GATE_ALLOWED.has(_currentPage()) || _isStudioRoute()) return;
  navigate("subscription", { push: false });
}

function _wireGate() {
  if (_gateWired) return;
  _gateWired = true;
  document.addEventListener(
    "click",
    (event) => {
      if (!_gateLocked) return;
      const target = event.target instanceof Element ? event.target.closest("[data-nav]") : null;
      if (!target) return;
      const page = target.dataset.nav || "";
      if (GATE_ALLOWED.has(page)) return;
      event.preventDefault();
      event.stopPropagation();
      toast("Your subscription is not active on this device.");
      navigate("subscription", { push: false });
    },
    true
  );
  window.addEventListener("x5:page-open", (event) => {
    if (!_gateLocked) return;
    if (GATE_ALLOWED.has(event.detail)) return;
    navigate("subscription", { push: false });
  });
}

/* ------------------------------------------------------------------ purchase */

async function _startCheckout(plan, button) {
  if (_busy || !plan || !plan.id) return;
  _busy = true;
  if (button) {
    button.disabled = true;
    button.classList.add("loading");
  }
  try {
    const data = await _getJson("/api/license/checkout", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_id: plan.id }),
    });
    const checkout = data.checkout || {};
    if (checkout.paid) {
      // Sandbox provider: charged and active at once — update this page only.
      _renderStatus(checkout.license || _status);
      _notifyLicenseChanged();
      toast("Plan activated — thank you");
      return;
    }
    if (checkout.instant) {
      const fresh = await _getJson("/api/license/status");
      _renderStatus(fresh.license);
      _notifyLicenseChanged();
      return;
    }
    if (checkout.checkout_url && checkout.session_id) {
      // Hosted provider: come back to this page, which confirms the payment.
      try { sessionStorage.setItem(PENDING_KEY, checkout.session_id); } catch (_) {}
      location.href = checkout.checkout_url;
      return;
    }
    throw new Error("The payment page is unavailable.");
  } catch (err) {
    toast((err && err.message) || "Could not start the checkout");
  } finally {
    _busy = false;
    if (button && document.body.contains(button)) {
      button.disabled = false;
      button.classList.remove("loading");
    }
    if (_status) _renderPlans(_plans, _status);
  }
}

/** A hosted payment that sent the buyer back: confirm it once, in place. */
async function _resumeCheckout() {
  let sessionId = "";
  try { sessionId = sessionStorage.getItem(PENDING_KEY) || ""; } catch (_) { return; }
  if (!sessionId) return;
  try {
    const data = await _getJson(
      "/api/license/checkout?session=" + encodeURIComponent(sessionId)
    );
    const checkout = data.checkout || {};
    if (checkout.paid) {
      try { sessionStorage.removeItem(PENDING_KEY); } catch (_) {}
      _renderStatus(checkout.license || _status);
      _notifyLicenseChanged();
      toast("Plan activated — thank you");
      return;
    }
    if (checkout.status === "expired" || checkout.status === "cancelled") {
      try { sessionStorage.removeItem(PENDING_KEY); } catch (_) {}
    }
  } catch (_) {
    // Keep the marker: the next visit can still confirm it.
  }
}

/* --------------------------------------------------------------------- boot */

async function _refresh() {
  try {
    const data = await _getJson("/api/license/status");
    _renderStatus(data.license);
  } catch (err) {
    toast((err && err.message) || "Subscription status unavailable.");
    _renderStatus(_status);
  }
  if (!_plans) {
    try {
      const data = await _getJson("/api/license/plans");
      _plans = data.plans || [];
      _plansError = "";
    } catch (err) {
      _plans = null;
      _plansError = (err && err.message) || "Plans are unavailable.";
    }
    _renderPlans(_plans, _status);
  }
  await _resumeCheckout();
}

/** Current page name from the URL (/agent, /settings, /studio-pdf/<id>, ...). */
function _currentPage() {
  const path = (location.pathname || "").replace(/\/+$/, "");
  const match = path.match(/\/([a-z-]+)$/);
  return match ? match[1] : "";
}

/**
 * The license gate: nothing in the console opens until the account exists on
 * the license server and is allowed to work here. The status fetch already
 * runs in initSubscription; this wires the gate and applies the last answer
 * (fail-closed when nothing is known yet).
 */
export function initLicenseGate() {
  _wireGate();
  _applyGate(_status, _status !== null);
}

export function licenseGateLocked() {
  return _gateLocked;
}

export function initSubscription() {
  window.addEventListener("x5:page-open", (event) => {
    if (event.detail === "subscription") _refresh();
  });
  // The license changed anywhere (a purchase in another tab, an admin
  // extension): re-read the page instead of showing stale cards.
  window.addEventListener("x5:license-changed", () => {
    _refresh();
  });

  return _refresh();
}
