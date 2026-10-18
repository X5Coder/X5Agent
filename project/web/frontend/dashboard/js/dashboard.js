/**
 * dashboard.js — real dashboard stats for X5Agent
 * Owns: fetching /api/dashboard + /api/cost + /api/balance + /api/models,
 * populating the stat cards. Measured totals from /api/cost; truthful empty states.
 */
"use strict";

import { onPush } from "./events.js?v=7";

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = String(value);
}

function formatCompact(n) {
  if (typeof n !== "number" || !isFinite(n)) return "0";
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(n);
}

async function fetchJson(url) {
  try {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return null;
    return await res.json();
  } catch (_) {
    return null;
  }
}

function populateStats(dash, cost, models, balance) {
  // Measured totals from /api/cost (usage_log). No per-message estimates.
  const totals = cost?.totals && typeof cost.totals === "object" ? cost.totals : null;
  const totalRequests = totals && isFinite(Number(totals.requests)) ? Number(totals.requests) : 0;
  const estTokens = totals && isFinite(Number(totals.tokens)) ? Number(totals.tokens) : 0;
  const messages = typeof dash?.messages === "number" ? dash.messages : 0;

  setText("statRequests", formatCompact(totalRequests));
  setText(
    "statRequestsMeta",
    totalRequests === 0 ? "No measured calls yet" : `${messages} messages in store`
  );

  setText("statTokens", formatCompact(estTokens));
  setText(
    "statTokensMeta",
    totalRequests === 0 ? "No usage yet" : "measured tokens"
  );

  // Balance card: measured lifetime spend (every request captured its own
  // price; the server summed the captures). Provider-reported remaining
  // credits -> $X.XX. Nothing measured yet -> honest $0.00 empty state.
  const balValue = balance && typeof balance.balance === "number" ? balance.balance : null;
  let balReqs = 0;
  try {
    balReqs = Math.max(0, parseInt(balance?.requests, 10) || 0);
  } catch (_) {}
  if (balance?.measured) {
    setText("statHosting", `$${(balValue || 0).toFixed(2)}`);
    setText("statHostingMeta", balReqs > 0 ? `measured spend · ${balReqs} request${balReqs === 1 ? "" : "s"}` : "no measured usage yet");
  } else if (balance?.unlimited) {
    setText("statHosting", "Unlimited");
    setText("statHostingMeta", "own API key · unlimited usage");
  } else if (balValue !== null && isFinite(balValue)) {
    setText("statHosting", `$${balValue.toFixed(2)}`);
    setText("statHostingMeta", "remaining credits");
  } else if (cost?.totals && typeof cost.totals.cost === "number") {
    // Older backend without a measured balance: fall back to the cost
    // totals (same measured source) instead of showing an empty card.
    const c = cost.totals.cost;
    let rq = 0;
    try {
      rq = Math.max(0, parseInt(cost.totals.requests, 10) || 0);
    } catch (_) {}
    setText("statHosting", `$${c.toFixed(2)}`);
    setText("statHostingMeta", rq > 0 ? `measured spend · ${rq} request${rq === 1 ? "" : "s"}` : "no measured usage yet");
  } else if (balance) {
    setText("statHosting", "—");
    setText("statHostingMeta", "connect a model in AI Models");
  }

  // Toolbar pill reflects the live model connection (not just the server process).
  const cloud = Array.isArray(models?.cloud) ? models.cloud : [];
  let live = cloud.find((p) => p.active && p.routable && p.connected) || null;
  if (!live && models?.remote?.connected) {
    live = { name: "AI Text Model", model: models?.remote?.default_model || models?.default_model };
  }
  const status = String(dash?.status || "unknown");
  const isOk = status === "operational";
  const pill = document.getElementById("toolbarStatus");
  const pillText = document.getElementById("toolbarStatusText");
  const pillState = !isOk ? status : live ? "Active" : "Disconnected";
  const pillGood = isOk && !!live;
  if (pillText) pillText.textContent = pillState;
  if (pill) {
    pill.style.color = pillGood ? "var(--green)" : "var(--red)";
    const dot = pill.querySelector(".status-dot");
    if (dot) {
      dot.style.background = pillGood ? "var(--green)" : "var(--red)";
      dot.style.boxShadow = pillGood ? "0 0 0 3px rgba(77,107,54,.2)" : "0 0 0 3px rgba(178,58,42,.2)";
    }
  }

  const modelEl = document.getElementById("statModel");
  if (modelEl) {
    modelEl.textContent = live ? String(live.model || models?.default_model || "—").slice(0, 24) : "No model connected";
    modelEl.classList.add("small");
  }
  setText("statModelMeta", live ? `${live.name} · live` : "Connect a model in AI Models");

  // Sidebar footer shows the console version (single source: server APP_VERSION).
  const ver = typeof dash?.version === "string" ? dash.version.trim().slice(0, 24) : "";
  if (ver) setText("sideVersion", ver.startsWith("v") ? ver : `v${ver}`);
}

export async function initDashboard() {
  await refreshDashboard();

  // `dashboard` sync section (third in priority): render without extra POSTs.
  // Accepts the flat bundle and a {hash,data} shell.
  onPush("dashboard", (d) => {
    if (!d) return;
    const b = d && typeof d === "object" && d.data && typeof d.data === "object" ? d.data : d;
    populateStats(b.dashboard, b.cost, b.models, b.balance);
  });

  // A model connected/disconnected on the AI Models page: re-read status
  // now instead of waiting for a reload.
  window.addEventListener("x5:models-changed", () => {
    refreshDashboard();
  });
}

export async function refreshDashboard() {
  if (!document.getElementById("statRequests")) return;

  const [dash, cost, models, balance] = await Promise.all([
    fetchJson("/api/dashboard"),
    fetchJson("/api/cost"),
    fetchJson("/api/models"),
    fetchJson("/api/balance"),
  ]);
  // Always render (even on partial failure): honest empty states.
  populateStats(dash, cost, models, balance);
}
