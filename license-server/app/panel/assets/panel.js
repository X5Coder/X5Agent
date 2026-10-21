/* X5Agent License Admin — vanilla JS, no build step.
 *
 * Auth: the API sets one HttpOnly session cookie, so the panel stays signed
 * in across refreshes and server restarts until the operator signs out or the
 * cookie expires (a week by default). `credentials: same-origin` is sent on
 * every call; a 401 drops straight back to the sign-in card.
 */

const EMAIL_KEY = "x5admin.email";
const VIEW_KEY = "x5admin.view";
const PAGE_SIZE = 25;
// Four plans is the whole catalogue (trial, month, 3 months, year).
const MAX_PLANS = 4;

const state = {
  email: localStorage.getItem(EMAIL_KEY) || "",
  accounts: [],
  total: 0,
  offset: 0,
  search: "",
  plans: [],
  view: localStorage.getItem(VIEW_KEY) || "overview",
  detailId: "",
  planEditId: "",
};

const $ = (id) => document.getElementById(id);

/* --------------------------------------------------------------- utilities */

function escapeText(value) {
  return value === null || value === undefined ? "" : String(value);
}

function stateClass(value) {
  if (value === "trial" || value === "active") return "ok";
  if (value === "warning") return "warn";
  return "bad";
}

function stateLabel(value) {
  const labels = {
    trial: "Trial",
    active: "Active",
    warning: "Warning",
    suspended: "Suspended",
    expired: "Expired",
    none: "No subscription",
  };
  return labels[value] || escapeText(value) || "Unknown";
}

function stateTone(value) {
  if (value === "active" || value === "trial") return "ok";
  if (value === "warning") return "warn";
  if (value === "suspended" || value === "expired") return "bad";
  return "muted-pill";
}

function formatDate(iso) {
  if (!iso) return "—";
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "—";
  return when.toLocaleString();
}

function formatRemaining(seconds) {
  const total = Math.max(0, Number(seconds) || 0);
  if (!total) return "—";
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  if (days > 0) return `${days}d ${hours}h`;
  const minutes = Math.max(0, Math.floor(total / 60));
  return hours > 0 ? `${hours}h ${minutes}m` : `${minutes}m`;
}

function formatPrice(cents, currency) {
  const amount = (Math.max(0, Number(cents) || 0) / 100).toFixed(2);
  return `${currency || "USD"} ${amount}`;
}

function clearChildren(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function pill(value) {
  return el("span", `pill ${stateClass(value)}`, stateLabel(value));
}

function toast(message, bad) {
  const node = $("toast");
  node.textContent = escapeText(message);
  node.classList.toggle("bad", Boolean(bad));
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    node.hidden = true;
  }, 3200);
}

/* -------------------------------------------------------------------- api */

async function readJson(response) {
  try {
    return await response.json();
  } catch (_) {
    return null;
  }
}

function detailText(data, status) {
  if (data && typeof data.detail === "string" && data.detail.trim()) {
    return data.detail.replace(/_/g, " ");
  }
  return `Request failed (HTTP ${status}).`;
}

async function api(path, options = {}) {
  const headers = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";

  let response;
  try {
    response = await fetch(path, {
      method: options.method || "GET",
      headers,
      credentials: "same-origin",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
  } catch (_) {
    throw new Error("Network error — the license server is unreachable.");
  }

  const data = await readJson(response);
  if (response.status === 401) {
    // The session probe on boot asks quietly: no "expired" toast on a first visit.
    if (!options.silent401) endSession("Your admin session expired. Sign in again.");
    throw new Error("Session expired.");
  }
  if (!response.ok) throw new Error(detailText(data, response.status));
  return data;
}

/* --------------------------------------------------------------- session */

function showLogin() {
  $("topbar").hidden = true;
  $("dashView").hidden = true;
  $("loginView").hidden = false;
  $("adminPasswordInput").value = "";
  if (state.email) $("adminEmailInput").value = state.email;
  setTimeout(() => {
    ($("adminEmailInput").value ? $("adminPasswordInput") : $("adminEmailInput")).focus();
  }, 30);
}

function endSession(message) {
  state.email = "";
  try { localStorage.removeItem(EMAIL_KEY); } catch (_) {}
  // Clear the HttpOnly cookie server-side (the page cannot touch it itself).
  try {
    fetch("/v1/admin/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
  } catch (_) {}
  if ($("detailDialog") && $("detailDialog").open) $("detailDialog").close();
  showLogin();
  if (message) toast(message, true);
}

async function signIn(email, password) {
  const response = await fetch("/v1/admin/login", {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    credentials: "same-origin",
    body: JSON.stringify({ email, password }),
  });
  const data = await readJson(response);
  if (response.status === 401) {
    throw new Error(
      "Email or password is incorrect. Admin accounts are separate from customer accounts.",
    );
  }
  if (!response.ok) throw new Error(detailText(data, response.status));
  state.email = String((data && data.email) || email).trim().toLowerCase();
  try { localStorage.setItem(EMAIL_KEY, state.email); } catch (_) {}
}

function showDashboard() {
  $("loginView").hidden = true;
  $("dashView").hidden = false;
  $("topbar").hidden = false;
  $("adminEmail").textContent = state.email;
  showView(state.view);
}

/* One section at a time. The toolbar owns the switch: the incoming view is
   revealed (and animates in), every other view is hidden. */
const VIEWS = ["overview", "accounts", "plans", "billing"];

function showView(name) {
  const view = VIEWS.includes(name) ? name : "overview";
  state.view = view;
  try { localStorage.setItem(VIEW_KEY, view); } catch (_) {}
  for (const key of VIEWS) {
    const panel = document.querySelector(`[data-view-panel="${key}"]`);
    if (panel) panel.hidden = key !== view;
  }
  for (const tab of $("sectionTabs").querySelectorAll("[data-view]")) {
    tab.setAttribute("aria-current", tab.dataset.view === view ? "true" : "false");
  }
  window.scrollTo({ top: 0, behavior: "auto" });
}

/* ------------------------------------------------------------------ stats */

async function loadStats() {
  const data = await api("/v1/admin/stats");
  const stats = [
    ["Accounts", data.accounts],
    ["Active paid", data.active_paid],
    ["Active trials", data.active_trials],
    ["Expired", data.expired],
    ["Blocked", data.blocked],
  ];
  const host = $("stats");
  clearChildren(host);
  for (const [label, value] of stats) {
    const card = el("div", "stat");
    card.append(el("span", "stat-label", label), el("span", "stat-value", String(value ?? 0)));
    host.append(card);
  }
}

/* ---------------------------------------------------------------- billing */

function renderRevenue(totals) {
  const host = $("revenueGrid");
  if (!host) return;
  clearChildren(host);
  // Only outcomes the admin can act on: a session either paid or failed.
  // Nothing keeps waiting forever, so there is no "pending" row any more.
  const rows = [
    ["Paid", totals.paid ?? 0],
    ["Failed", totals.failed ?? 0],
    ["Collected", formatPrice(totals.paid_cents ?? 0, "USD")],
  ];
  for (const [label, value] of rows) {
    const row = el("div", "overview-row");
    row.append(
      el("span", "overview-label", label),
      el("strong", "overview-value", String(value)),
    );
    host.append(row);
  }
}

async function loadBilling() {
  const data = await api("/v1/admin/billing?limit=20");
  const totals = data.totals || {};
  renderRevenue(totals);
  const revenueMeta = $("revenueMeta");
  if (revenueMeta) {
    revenueMeta.textContent = `${formatPrice(totals.paid_cents ?? 0, "USD")} collected from ${totals.paid ?? 0} paid session${totals.paid === 1 ? "" : "s"}.`;
  }
  const meta = $("billingMeta");
  if (meta) {
    meta.textContent = `${totals.paid ?? 0} paid · ${totals.failed ?? 0} failed · ${formatPrice(totals.paid_cents ?? 0, "USD")} collected`;
  }
  const body = $("billingBody");
  if (!body) return;
  clearChildren(body);
  const items = Array.isArray(data.items) ? data.items : [];
  if (!items.length) {
    const row = el("tr");
    const cell = el("td", "empty", "No checkout sessions yet.");
    cell.colSpan = 5;
    row.append(cell);
    body.append(row);
    return;
  }
  for (const item of items) {
    const row = el("tr");
    // Paid, a checkout still in flight, or failed - nothing else.
    const label = item.status === "paid" ? "Paid" : item.status === "pending" ? "Pending" : "Failed";
    const tone = item.status === "paid" ? "ok" : item.status === "pending" ? "muted-pill" : "bad";
    const statusPill = el("span", `pill ${tone}`, label);
    const statusCell = el("td");
    statusCell.dataset.label = "Status";
    statusCell.append(statusPill);
    const created = el("td", "", formatDate(item.created_at));
    created.dataset.label = "Created";
    const who = el("td", "", escapeText(item.email) || "—");
    who.dataset.label = "Account";
    const planCell = el("td", "", escapeText(item.plan) || "—");
    planCell.dataset.label = "Plan";
    const amount = el("td", "", formatPrice(item.amount_cents, item.currency));
    amount.dataset.label = "Amount";
    row.append(created, who, planCell, statusCell, amount);
    body.append(row);
  }
}

/* --------------------------------------------------------------- accounts */

async function loadAccounts() {
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(state.offset),
  });
  if (state.search) params.set("search", state.search);
  const data = await api(`/v1/admin/accounts?${params.toString()}`);
  state.accounts = Array.isArray(data.items) ? data.items : [];
  state.total = Number(data.total) || 0;
  renderAccounts();
}

function renderAccounts() {
  const body = $("accountsBody");
  clearChildren(body);

  if (!state.accounts.length) {
    const row = el("tr");
    const cell = el("td", "empty", state.search ? "No account matches that search." : "No accounts yet.");
    cell.colSpan = 9;
    row.append(cell);
    body.append(row);
  }

  for (const account of state.accounts) {
    const row = el("tr");
    row.dataset.id = account.id;
    row.className = "clickable";

    const emailCell = el("td", "", escapeText(account.email));
    emailCell.dataset.label = "Email";
    row.append(emailCell);

    const stateCell = el("td");
    stateCell.dataset.label = "State";
    stateCell.append(pill(account.state));
    row.append(stateCell);

    const planCell = el("td", "", escapeText(account.plan) || "—");
    planCell.dataset.label = "Plan";
    row.append(planCell);

    const expiresCell = el("td", "", formatDate(account.expires_at));
    expiresCell.dataset.label = "Expires";
    row.append(expiresCell);

    const devicesCell = el("td", "", String(account.devices ?? 0));
    devicesCell.dataset.label = "Devices";
    row.append(devicesCell);

    const createdCell = el("td", "", formatDate(account.created_at));
    createdCell.dataset.label = "Created";
    row.append(createdCell);

    const actions = el("td");
    actions.dataset.label = "";
    const open = el("button", "btn tiny ghost", "Open");
    open.type = "button";
    open.dataset.open = account.id;
    actions.append(open);
    row.append(actions);

    body.append(row);
  }

  const from = state.total === 0 ? 0 : state.offset + 1;
  const to = Math.min(state.offset + PAGE_SIZE, state.total);
  $("pageInfo").textContent = `${from}–${to} of ${state.total}`;
  $("accountsMeta").textContent = state.search
    ? `${state.total} account${state.total === 1 ? "" : "s"} matching “${state.search}”`
    : `${state.total} account${state.total === 1 ? "" : "s"}`;
  $("prevPageBtn").disabled = state.offset <= 0;
  $("nextPageBtn").disabled = state.offset + PAGE_SIZE >= state.total;
}

/* ----------------------------------------------------------------- detail */

function detailItem(label, value) {
  const wrap = el("div", "detail-item");
  wrap.append(el("dt", "", label), el("dd", "", value));
  return wrap;
}

function renderDetail(data) {
  const body = $("detailBody");
  clearChildren(body);

  const account = data.account || {};
  const current = data.current;
  const subscriptions = Array.isArray(data.subscriptions) ? data.subscriptions : [];
  const devices = Array.isArray(data.devices) ? data.devices : [];
  const blocked = current && ["warning", "suspended"].includes(current.state);

  $("detailTitle").textContent = account.email || "Account";
  state.detailId = account.id || "";

  /* ------------------------------------------------------- current plan */
  const planSection = el("div", "detail-section");
  planSection.append(el("h3", "", "Current plan"));
  const planCard = el("div", "plan-panel" + (current ? "" : " is-empty"));
  if (current) {
    const head = el("div", "plan-panel-head");
    const title = el("div", "plan-panel-title");
    title.append(el("strong", "", escapeText(current.plan) || "Plan"));
    title.append(el("span", `pill ${stateTone(current.state)}`, stateLabel(current.state)));
    head.append(title);
    head.append(el("span", "plan-panel-left", formatRemaining(current.remaining_seconds)));
    planCard.append(head);
    const meta = el("p", "plan-panel-meta");
    meta.textContent = `${formatDate(current.starts_at)} → ${formatDate(current.expires_at)} · source ${current.source || "system"}`;
    planCard.append(meta);
  } else {
    planCard.append(
      el("p", "muted", "This account has no subscription yet. Pick a plan below to activate one."),
    );
  }

  const bar = el("div", "actions");
  const suspendBtn = el("button", "btn tiny danger", "Suspend");
  suspendBtn.type = "button";
  suspendBtn.dataset.action = "suspend";

  const cancelBtn = el("button", "btn tiny danger-ghost", "Cancel plan");
  cancelBtn.type = "button";
  cancelBtn.dataset.action = "cancel";

  const clearBtn = el("button", "btn tiny", "Clear block");
  clearBtn.type = "button";
  clearBtn.dataset.action = "clear";

  const extendDays = el("input", "input");
  extendDays.type = "number";
  extendDays.min = "1";
  extendDays.max = "3650";
  extendDays.value = "7";
  extendDays.id = "extendDays";
  extendDays.setAttribute("aria-label", "Days to add");

  const extendBtn = el("button", "btn tiny", "Add days");
  extendBtn.type = "button";
  extendBtn.dataset.action = "extend";

  if (!current) {
    suspendBtn.disabled = true;
    cancelBtn.disabled = true;
    clearBtn.disabled = true;
    extendBtn.disabled = true;
  } else {
    clearBtn.disabled = !blocked;
    cancelBtn.disabled = String(current.state) === "expired";
  }
  bar.append(suspendBtn, cancelBtn, clearBtn, extendDays, extendBtn);
  planCard.append(bar);

  const switchRow = el("div", "plan-switch");
  const planSelect = el("select", "input");
  planSelect.id = "activatePlan";
  planSelect.setAttribute("aria-label", "Plan to activate");
  for (const plan of state.plans) {
    const option = el(
      "option",
      "",
      `${plan.name} · ${plan.duration_days}d${plan.is_trial ? " · trial" : ""}`,
    );
    option.value = plan.id;
    planSelect.append(option);
  }
  const activateBtn = el("button", "btn tiny primary", "Activate this plan");
  activateBtn.type = "button";
  activateBtn.dataset.action = "activate";
  switchRow.append(planSelect, activateBtn);
  planCard.append(switchRow);
  planCard.append(
    el(
      "p",
      "hint",
      current
        ? "Suspend blocks the account now, Cancel plan ends it immediately, Activate replaces the plan and restarts its duration."
        : "Pick a plan and Activate it to start the subscription.",
    ),
  );
  planSection.append(planCard);

  const grid = el("dl", "detail-grid");
  grid.append(
    detailItem("Email", escapeText(account.email)),
    detailItem("Account status", escapeText(account.status) || "—"),
    detailItem("Trial used", data.trial_used ? "yes" : "no"),
    detailItem("Plan chosen", formatDate(account.plan_chosen_at)),
    detailItem("Email verified", formatDate(account.email_verified_at)),
    detailItem(
      "Password (hash)",
      account.has_password
        ? `${escapeText(account.password_algo)} · ${account.password_hash_length} chars`
        : "—",
    ),
    detailItem("Hash prefix", escapeText(account.password_hash_prefix) || "—"),
    detailItem("Devices", `${data.active_devices || 0} active / ${devices.length} total`),
    detailItem("Session tokens", String(data.session_tokens == null ? "—" : data.session_tokens)),
    detailItem("Created", formatDate(account.created_at)),
    detailItem("Account id", escapeText(account.id)),
  );
  const accountSection = el("div", "detail-section");
  accountSection.append(el("h3", "", "Account"));
  accountSection.append(grid);

  const subSection = el("div", "detail-section");
  subSection.append(el("h3", "", `Subscriptions (${subscriptions.length})`));
  if (subscriptions.length) {
    const table = el("table", "table");
    const head = el("thead");
    const headRow = el("tr");
    ["Plan", "State", "Starts", "Expires", "Source"].forEach((label) => {
      headRow.append(el("th", "", label));
    });
    head.append(headRow);
    const tbody = el("tbody");
    for (const sub of subscriptions) {
      const row = el("tr");
      row.append(
        el("td", "", escapeText(sub.plan) || "—"),
        el("td", "", stateLabel(sub.state)),
        el("td", "", formatDate(sub.starts_at)),
        el("td", "", formatDate(sub.expires_at)),
        el("td", "", escapeText(sub.source) || "—"),
      );
      tbody.append(row);
    }
    table.append(head, tbody);
    subSection.append(table);
  } else {
    subSection.append(el("p", "muted", "No subscription rows."));
  }

  const deviceSection = el("div", "detail-section");
  deviceSection.append(el("h3", "", `Devices (${devices.length})`));
  if (devices.length) {
    const table = el("table", "table");
    const head = el("thead");
    const headRow = el("tr");
    ["Device", "Active", "Last seen", "Session"].forEach((label) => {
      headRow.append(el("th", "", label));
    });
    head.append(headRow);
    const tbody = el("tbody");
    for (const device of devices) {
      const row = el("tr");

      const deviceCell = el("td");
      const details = device.details && typeof device.details === "object" ? device.details : {};
      const title = details.hostname || details.system || "Device";
      deviceCell.append(el("div", "device-name", escapeText(title)));
      const facts = [];
      if (details.system || details.machine) {
        facts.push([details.system, details.machine].filter(Boolean).join(" "));
      }
      if (details.platform) facts.push(details.platform);
      if (details.python) facts.push(`py ${details.python}`);
      if (facts.length) deviceCell.append(el("div", "device-meta", escapeText(facts.join(" · "))));
      deviceCell.append(
        el("div", "device-hash", `FP ${escapeText(device.fingerprint_hash)}`),
      );
      if (device.last_ip_hash) {
        deviceCell.append(el("div", "device-hash", `IP ${escapeText(device.last_ip_hash)}`));
      }
      // Watermark: the app reports its own build and whether its code still
      // matches what was shipped. "Modified" means the install was tampered.
      const appState = String(details.app_state || "");
      const stateText =
        appState === "modified" ? "Modified" : appState === "clean" ? "Verified" : "Unknown";
      const tone = appState === "modified" ? "is-bad" : appState === "clean" ? "is-ok" : "";
      const mark = el("span", "wm " + tone, `build ${details.app_version || "?"} · ${stateText}`);
      mark.title = details.app_hash ? `Source hash ${details.app_hash}` : "No source hash reported";
      deviceCell.append(mark);
      deviceCell.append(el("div", "device-hash", `First seen ${formatDate(device.first_seen)}`));

      row.append(
        deviceCell,
        el("td", "", device.active ? "yes" : "no"),
        el("td", "", formatDate(device.last_seen)),
        el("td", "", String(device.session_tokens || 0)),
      );
      tbody.append(row);
    }
    table.append(head, tbody);
    deviceSection.append(table);
  } else {
    deviceSection.append(el("p", "muted", "This account never signed in on a device."));
  }

  const checkoutSection = el("div", "detail-section");
  const checkouts = Array.isArray(data.checkouts) ? data.checkouts : [];
  checkoutSection.append(el("h3", "", `Checkouts (${checkouts.length})`));
  if (checkouts.length) {
    const table = el("table", "table");
    const head = el("thead");
    const headRow = el("tr");
    ["Plan", "Provider", "Status", "Amount", "Created", "Completed"].forEach((label) => {
      headRow.append(el("th", "", label));
    });
    head.append(headRow);
    const tbody = el("tbody");
    for (const checkout of checkouts) {
      const amount = `${(Number(checkout.amount_cents || 0) / 100).toFixed(2)} ${escapeText(checkout.currency) || ""}`.trim();
      const row = el("tr");
      row.append(
        el("td", "", escapeText(checkout.plan) || "—"),
        el("td", "", escapeText(checkout.provider) || "—"),
        el(
          "td",
          "",
          checkout.status === "paid"
            ? "Paid"
            : checkout.status === "pending"
              ? "Pending"
              : "Failed"
        ),
        el("td", "", amount),
        el("td", "", formatDate(checkout.created_at)),
        el("td", "", checkout.completed_at ? formatDate(checkout.completed_at) : "—"),
      );
      tbody.append(row);
    }
    table.append(head, tbody);
    checkoutSection.append(table);
  } else {
    checkoutSection.append(el("p", "muted", "No checkout attempts yet."));
  }

  body.append(planSection, accountSection, subSection, deviceSection, checkoutSection);
}

async function openDetail(accountId) {
  // The plan switch inside the dialog needs the catalogue even when the
  // operator never opened the Plans section in this visit.
  if (!state.plans.length) {
    try {
      await loadPlans();
    } catch (err) {
      /* the dialog still opens; the switch just has nothing to offer */
    }
  }
  const data = await api(`/v1/admin/accounts/${encodeURIComponent(accountId)}`);
  renderDetail(data);
  const dialog = $("detailDialog");
  if (!dialog.open) dialog.showModal();
}

async function refreshDetail() {
  if (!state.detailId) return;
  try {
    await openDetail(state.detailId);
  } catch (err) {
    toast(err.message || "Could not load the account.", true);
  }
}

/* ------------------------------------------------------------------ plans */

async function loadPlans() {
  const plans = await api("/v1/admin/plans");
  state.plans = Array.isArray(plans) ? plans : [];
  renderPlans();
}

function renderPlans() {
  const body = $("plansBody");
  clearChildren(body);

  const full = state.plans.length >= MAX_PLANS;
  const addBtn = $("newPlanBtn");
  if (addBtn) {
    addBtn.disabled = full;
    addBtn.title = full ? `All ${MAX_PLANS} plans are in use` : "";
  }
  const hint = $("plansHint");
  if (hint) {
    hint.textContent = full
      ? `The catalogue holds ${MAX_PLANS} plans and is full. Disable or delete one to replace it.`
      : `Trial is a normal plan and can be disabled · ${state.plans.length} of ${MAX_PLANS} used.`;
  }
  if (!state.plans.length) {
    const row = el("tr");
    const cell = el("td", "empty", "No plans.");
    cell.colSpan = 9;
    row.append(cell);
    body.append(row);
    return;
  }
  // Savings shown against the monthly plan, so a price edit is judged at a
  // glance (the client prints the same line on the card).
  const monthly = state.plans.find(
    (item) => !item.is_trial && Number(item.duration_days) === 30 && Number(item.price_cents) > 0,
  );
  for (const plan of state.plans) {
    const row = el("tr");
    const nameCell = el("td", "", escapeText(plan.name));
    nameCell.dataset.label = "Name";
    const daysCell = el("td", "", String(plan.duration_days));
    daysCell.dataset.label = "Days";
    const priceCell = el("td", "", formatPrice(plan.price_cents, plan.currency));
    priceCell.dataset.label = "Price";
    const savesCell = el("td");
    savesCell.dataset.label = "Saves";
    if (monthly && !plan.is_trial && Number(plan.duration_days) > 30) {
      const months = Math.round(Number(plan.duration_days) / 30);
      const full = Number(monthly.price_cents) * months;
      const saved = Math.round(full - Number(plan.price_cents || 0));
      savesCell.textContent =
        saved > 0
          ? `${formatPrice(saved, plan.currency)} (${Math.round((saved / full) * 100)}%)`
          : "—";
    } else {
      savesCell.textContent = "—";
    }
    const currencyCell = el("td", "", escapeText(plan.currency));
    currencyCell.dataset.label = "Currency";
    const popularCell = el("td");
    popularCell.dataset.label = "Popular";
    if (plan.is_featured) {
      popularCell.append(el("span", "pill ok", "Most popular"));
    } else {
      popularCell.append(el("span", "muted", "—"));
    }
    const trialCell = el("td", "", plan.is_trial ? "yes" : "no");
    trialCell.dataset.label = "Trial";
    row.append(nameCell, daysCell, priceCell, savesCell, currencyCell, popularCell, trialCell);
    const activeCell = el("td");
    activeCell.dataset.label = "Active";
    activeCell.append(
      el("span", `pill ${plan.active ? "ok" : "muted-pill"}`, plan.active ? "On" : "Off"),
    );
    row.append(activeCell);

    const cell = el("td");
    cell.dataset.label = "";
    const edit = el("button", "btn tiny", "Edit");
    edit.type = "button";
    edit.dataset.planEdit = plan.id;
    const toggle = el("button", "btn tiny ghost", plan.active ? "Disable" : "Enable");
    toggle.type = "button";
    toggle.dataset.planToggle = plan.id;
    toggle.dataset.planActive = plan.active ? "1" : "0";
    const remove = el("button", "btn tiny danger", "Delete");
    remove.type = "button";
    remove.dataset.planDelete = plan.id;
    remove.dataset.planName = plan.name;
    cell.append(edit, toggle, remove);
    row.append(cell);

    body.append(row);
  }
}

/** One form for both jobs: empty for a new plan, filled to edit an existing one. */
function startPlanForm(planId) {
  const form = $("planForm");
  const plan = planId ? state.plans.find((item) => item.id === planId) : null;
  state.planEditId = plan ? plan.id : "";
  $("planName").value = plan ? plan.name : "";
  $("planDays").value = plan ? String(plan.duration_days) : "30";
  $("planPrice").value = plan ? String(plan.price_cents) : "0";
  $("planCurrency").value = plan ? plan.currency : "USD";
  $("planTrial").checked = plan ? Boolean(plan.is_trial) : false;
  $("planFeatured").checked = plan ? Boolean(plan.is_featured) : false;
  $("planFeatures").value = plan ? String(plan.features || "") : "";
  // The trial flag is what the server's one-time grant keys on: never toggle
  // it on an existing plan by accident.
  $("planTrial").disabled = Boolean(plan);
  const submit = $("planForm").querySelector("button[type=submit]");
  if (submit) submit.textContent = plan ? "Save plan" : "Create plan";
  form.hidden = false;
  $("planName").focus();
}

/* ---------------------------------------------------------------- confirm */

function confirmAction(title, text, okLabel) {
  return new Promise((resolve) => {
    const dialog = $("confirmDialog");
    $("confirmTitle").textContent = title;
    $("confirmText").textContent = text;
    $("confirmOkBtn").textContent = okLabel || "Confirm";

    const finish = (value) => {
      $("confirmOkBtn").removeEventListener("click", onOk);
      $("confirmCancelBtn").removeEventListener("click", onCancel);
      dialog.removeEventListener("cancel", onEscape);
      if (dialog.open) dialog.close();
      resolve(value);
    };
    const onOk = () => finish(true);
    const onCancel = () => finish(false);
    const onEscape = (event) => {
      event.preventDefault();
      finish(false);
    };

    $("confirmOkBtn").addEventListener("click", onOk);
    $("confirmCancelBtn").addEventListener("click", onCancel);
    dialog.addEventListener("cancel", onEscape);
    dialog.showModal();
  });
}

/* --------------------------------------------------------------- actions */

function setDetailBusy(busy) {
  const buttons = $("detailBody").querySelectorAll("button");
  buttons.forEach((button) => {
    if (busy) {
      button.dataset.wasDisabled = button.disabled ? "1" : "0";
      button.disabled = true;
    } else {
      button.disabled = button.dataset.wasDisabled === "1";
    }
  });
}

async function runAction(label, call) {
  setDetailBusy(true);
  try {
    await call();
  } catch (err) {
    toast(err.message || "Action failed.", true);
    setDetailBusy(false);
    return;
  }
  setDetailBusy(false);
  toast(label);
  try {
    await refreshDetail();
  } catch (err) {
    toast(err.message || "Could not reload the account.", true);
  }
  await Promise.allSettled([loadAccounts(), loadStats()]);
}

async function onDetailAction(action) {
  const accountId = state.detailId;
  if (!accountId) return;

  if (action === "warn") {
    const ok = await confirmAction(
      "Warn this account?",
      "The subscription gets a warning and the agent stops working until the block is cleared.",
      "Warn account",
    );
    if (!ok) return;
    await runAction("Account warned", () =>
      api(`/v1/admin/accounts/${encodeURIComponent(accountId)}/warn`, { method: "POST" }),
    );
    return;
  }

  if (action === "clear") {
    await runAction("Block cleared", () =>
      api(`/v1/admin/accounts/${encodeURIComponent(accountId)}/clear`, { method: "POST" }),
    );
    return;
  }

  if (action === "suspend") {
    const ok = await confirmAction(
      "Suspend this account?",
      "The subscription is suspended and the agent stops working until the block is cleared.",
      "Suspend account",
    );
    if (!ok) return;
    await runAction("Account suspended", () =>
      api(`/v1/admin/accounts/${encodeURIComponent(accountId)}/suspend`, { method: "POST" }),
    );
    return;
  }

  if (action === "cancel") {
    const ok = await confirmAction(
      "Cancel the current plan?",
      "The subscription ends now instead of at its end date. The account can be given another plan at any time.",
      "Cancel plan",
    );
    if (!ok) return;
    await runAction("Plan cancelled", () =>
      api(`/v1/admin/accounts/${encodeURIComponent(accountId)}/cancel`, { method: "POST" }),
    );
    return;
  }

  if (action === "extend") {
    const input = $("extendDays");
    const days = Math.max(1, Math.min(3650, Number(input?.value) || 0));
    if (!days) {
      toast("Enter how many days to add.", true);
      return;
    }
    await runAction(`Extended by ${days} day${days === 1 ? "" : "s"}`, () =>
      api(`/v1/admin/accounts/${encodeURIComponent(accountId)}/extend`, {
        method: "POST",
        body: { days },
      }),
    );
    return;
  }

  if (action === "activate") {
    const select = $("activatePlan");
    const planId = select?.value || "";
    if (!planId) {
      toast("Create a plan first.", true);
      return;
    }
    const plan = state.plans.find((item) => item.id === planId);
    const ok = await confirmAction(
      "Activate this plan?",
      plan
        ? `Replaces the current subscription with “${plan.name}” for ${plan.duration_days} days.`
        : "Replaces the current subscription.",
      "Activate",
    );
    if (!ok) return;
    await runAction("Plan activated", () =>
      api(`/v1/admin/accounts/${encodeURIComponent(accountId)}/activate`, {
        method: "POST",
        body: { plan_id: planId },
      }),
    );
  }
}

/* ------------------------------------------------------------------ boot */

async function refreshAll() {
  try {
    await Promise.all([loadStats(), loadAccounts(), loadPlans(), loadBilling()]);
  } catch (err) {
    if (!$("loginView").hidden) return; // 401 already returned to the sign-in card
    toast(err.message || "Could not load the console.", true);
  }
}

function wire() {
  $("loginForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const email = $("adminEmailInput").value.trim();
    const password = $("adminPasswordInput").value;
    const error = $("loginError");
    const button = $("loginBtn");
    error.hidden = true;

    if (!email || !password) {
      error.textContent = "Enter the admin email and password.";
      error.hidden = false;
      return;
    }

    button.disabled = true;
    try {
      await signIn(email, password);
      showDashboard();
      await refreshAll();
    } catch (err) {
      error.textContent = err.message || "Sign-in failed.";
      error.hidden = false;
    } finally {
      button.disabled = false;
    }
  });

  $("signOutBtn").addEventListener("click", () => endSession("Signed out."));

  $("sectionTabs").addEventListener("click", (event) => {
    const button = event.target.closest("[data-view]");
    if (!button) return;
    showView(button.dataset.view);
    // A switch is also a refresh: the section never shows stale numbers.
    const reload = {
      overview: loadStats,
      accounts: loadAccounts,
      plans: loadPlans,
      billing: loadBilling,
    }[button.dataset.view];
    if (reload) reload().catch((err) => toast(err.message || "Could not refresh.", true));
  });

  $("refreshBillingBtn").addEventListener("click", () => {
    loadBilling().catch((err) => toast(err.message || "Could not refresh billing.", true));
  });

  let searchTimer = null;
  $("searchInput").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value.trim();
    searchTimer = setTimeout(() => {
      state.search = value;
      state.offset = 0;
      loadAccounts().catch((err) => toast(err.message, true));
    }, 300);
  });

  $("prevPageBtn").addEventListener("click", () => {
    state.offset = Math.max(0, state.offset - PAGE_SIZE);
    loadAccounts().catch((err) => toast(err.message, true));
  });

  $("nextPageBtn").addEventListener("click", () => {
    state.offset += PAGE_SIZE;
    loadAccounts().catch((err) => toast(err.message, true));
  });

  $("accountsBody").addEventListener("click", (event) => {
    const button = event.target.closest("[data-open]");
    const row = event.target.closest("tr[data-id]");
    const accountId = button ? button.dataset.open : row ? row.dataset.id : "";
    if (!accountId) return;
    openDetail(accountId).catch((err) => toast(err.message, true));
  });

  $("detailCloseBtn").addEventListener("click", () => $("detailDialog").close());
  $("detailDialog").addEventListener("click", (event) => {
    if (event.target === $("detailDialog")) $("detailDialog").close();
  });
  $("detailBody").addEventListener("click", (event) => {
    const button = event.target.closest("[data-action]");
    if (!button || button.disabled) return;
    onDetailAction(button.dataset.action).catch((err) =>
      toast(err.message || "Action failed.", true),
    );
  });

  $("newPlanBtn").addEventListener("click", () => {
    if (state.plans.length >= MAX_PLANS) {
      toast(`All ${MAX_PLANS} plans are in use.`, true);
      return;
    }
    startPlanForm("");
  });
  $("planCancelBtn").addEventListener("click", () => {
    $("planForm").hidden = true;
    state.planEditId = "";
  });
  $("planForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const name = $("planName").value.trim();
    const days = Number($("planDays").value) || 0;
    const price = Number($("planPrice").value) || 0;
    const currency = $("planCurrency").value.trim().toUpperCase() || "USD";
    const isTrial = $("planTrial").checked;
    const isFeatured = $("planFeatured").checked;
    const features = $("planFeatures").value.trim();
    if (!name || days < 1) {
      toast("A plan needs a name and at least one day.", true);
      return;
    }
    const editing = state.planEditId;
    const payload = {
      name,
      duration_days: Math.min(3650, days),
      price_cents: Math.max(0, price),
      currency,
      features,
      is_featured: isFeatured,
    };
    try {
      if (editing) {
        await api(`/v1/admin/plans/${encodeURIComponent(editing)}`, {
          method: "PATCH",
          body: payload,
        });
        toast("Plan updated");
      } else {
        await api("/v1/admin/plans", {
          method: "POST",
          body: { ...payload, is_trial: isTrial, active: true },
        });
        toast("Plan created");
      }
      $("planForm").hidden = true;
      state.planEditId = "";
      $("planName").value = "";
      $("planTrial").checked = false;
      $("planTrial").disabled = false;
      $("planFeatured").checked = false;
      $("planFeatures").value = "";
      await loadPlans();
    } catch (err) {
      toast(err.message || (editing ? "Could not update the plan." : "Could not create the plan."), true);
    }
  });

  $("plansBody").addEventListener("click", async (event) => {
    const editBtn = event.target.closest("[data-plan-edit]");
    if (editBtn) {
      startPlanForm(editBtn.dataset.planEdit);
      return;
    }
    const removeBtn = event.target.closest("[data-plan-delete]");
    if (removeBtn) {
      const planId = removeBtn.dataset.planDelete;
      const name = removeBtn.dataset.planName || "this plan";
      const ok = await confirmAction(
        `Delete “${name}”?`,
        "This removes the plan for good. If an account has used it, the plan is kept and only disabled instead.",
        "Delete plan",
      );
      if (!ok) return;
      removeBtn.disabled = true;
      try {
        await api(`/v1/admin/plans/${encodeURIComponent(planId)}`, { method: "DELETE" });
        toast("Plan deleted");
        await Promise.all([loadPlans(), loadStats()]);
      } catch (err) {
        toast(err.message || "Could not delete the plan.", true);
        removeBtn.disabled = false;
      }
      return;
    }

    const button = event.target.closest("[data-plan-toggle]");
    if (!button) return;
    const planId = button.dataset.planToggle;
    const nextActive = button.dataset.planActive !== "1";
    button.disabled = true;
    try {
      await api(`/v1/admin/plans/${encodeURIComponent(planId)}`, {
        method: "PATCH",
        body: { active: nextActive },
      });
      toast(nextActive ? "Plan enabled" : "Plan disabled");
      await loadPlans();
    } catch (err) {
      toast(err.message || "Could not update the plan.", true);
      button.disabled = false;
    }
  });
}

async function boot() {
  wire();
  // The cookie does the remembering: ask who is signed in instead of showing
  // the password form every visit.
  try {
    const who = await api("/v1/admin/session", { silent401: true });
    state.email = String((who && who.email) || state.email || "");
    try { localStorage.setItem(EMAIL_KEY, state.email); } catch (_) {}
    showDashboard();
    await refreshAll();
  } catch (_) {
    showLogin();
  }
}

boot();
