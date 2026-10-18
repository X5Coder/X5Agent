/**
 * charts.js — X5Agent usage block charts (tokens + requests)
 * Owns: fetching /api/cost (90-day window), range switching (7D / 30D /
 * 90D / months), one SVG
 * chart per card: grid-squares background, tall connected columns rising
 * from the bottom axis, days on the x axis, values on the left y axis.
 * Hovering (or tapping on phones) any column shows its details card.
 * Real data only; honest empty state. Silent refresh: unchanged data
 * never touches the DOM.
 */
"use strict";

import { attachChartTooltip, pinChartTip } from "./ui.js?v=13";
import { onPush } from "./events.js?v=7";

const MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const RANGE_LABELS = { "7d": "Last 7 days", "30d": "Last 30 days", "90d": "Last 90 days", "months": "By month" };

function takeLast(arr, n) {
  const list = Array.isArray(arr) ? arr.slice(-n) : [];
  while (list.length < n) list.unshift(typeof list[0] === "string" ? "—" : 0);
  return list;
}

function sliceRange(data, range) {
  const counts = Array.isArray(data?.counts) ? data.counts : [];
  const labels = Array.isArray(data?.labels) ? data.labels : [];
  const daily = Array.isArray(data?.daily) ? data.daily : [];
  const rawTokens = Array.isArray(data?.tokens) ? data.tokens : null;
  const n = Math.max(counts.length, labels.length, daily.length, rawTokens ? rawTokens.length : 0);
  const pad = (arr) => {
    const l = arr.slice(-n);
    while (l.length < n) l.unshift(0);
    return l;
  };
  const lab = labels.slice(-n);
  while (lab.length < n) lab.unshift("—");
  const c = pad(counts.map((v) => Number(v) || 0));
  const d = pad(daily.map((v) => Number(v) || 0));
  // Measured tokens when present; otherwise derive nothing (no fabricated estimates).
  const t = rawTokens ? pad(rawTokens.map((v) => Number(v) || 0)) : c.map(() => 0);
  if (range === "90d") return { labels: lab, counts: c, costs: d, tokens: t };
  if (range === "30d") {
    const k = 30;
    return { labels: lab.slice(-k), counts: c.slice(-k), costs: d.slice(-k), tokens: t.slice(-k) };
  }
  if (range === "months") {
    // Aggregate the available days into calendar months (only real data, never padded).
    const buckets = new Map();
    for (let i = 0; i < n; i++) {
      const m = /^(\d{1,2})\/\d{1,2}$/.exec(String(lab[i] || ""));
      if (!m) continue;
      const mi = Math.min(12, Math.max(1, parseInt(m[1], 10)));
      if (!buckets.has(mi)) buckets.set(mi, { c: 0, d: 0, t: 0 });
      buckets.get(mi).c += c[i];
      buckets.get(mi).d += d[i];
      buckets.get(mi).t += t[i];
    }
    const keys = [...buckets.keys()].sort((a, b) => a - b);
    return {
      labels: keys.map((k) => MONTH_NAMES[k - 1]),
      counts: keys.map((k) => buckets.get(k).c),
      costs: keys.map((k) => Math.round(buckets.get(k).d * 100) / 100),
      tokens: keys.map((k) => buckets.get(k).t),
    };
  }
  return { labels: takeLast(lab, 7), counts: takeLast(c, 7), costs: takeLast(d, 7), tokens: takeLast(t, 7) };
}

function formatCompact(n) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}K`;
  return String(Math.round(n));
}

function escHtml(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

function niceCeiling(rawMax) {
  // Headroom so the tallest stack never touches the top edge, snapped to a
  // 1/2/2.5/5 step so the y axis labels stay round.
  const target = Math.max(1, rawMax * 1.15);
  const exp = Math.floor(Math.log10(target));
  const base = Math.pow(10, exp);
  const norm = target / base;
  const snap = norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10;
  return snap * base;
}

function renderBars(el, labels, values, unit, accent) {
  if (!el) return;
  const n = Math.max(labels.length, values.length);
  const vals = Array.from({ length: n }, (_, i) => Number(values[i]) || 0);
  const labs = Array.from({ length: n }, (_, i) => String(labels[i] ?? "—"));
  const total = vals.reduce((a, b) => a + b, 0);
  const isTokens = accent === "tok";
  const caption = isTokens ? "Token usage block chart" : "Requests block chart";

  const W = 600;
  const H = 280;
  const padL = 48;
  const padR = 10;
  const padT = 18;
  const padB = 32;
  const plotW = W - padL - padR;
  const plotH = H - padT - padB;
  const baseY = padT + plotH;

  if (!(total > 0)) {
    el.innerHTML = `
      <div class="bars-wrap" data-anim>
        <svg class="bars-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" aria-hidden="true">
          ${[0, 1, 2, 3, 4].map((k) => `<line x1="${padL}" y1="${(padT + (plotH * k) / 4).toFixed(1)}" x2="${W - padR}" y2="${(padT + (plotH * k) / 4).toFixed(1)}" class="bars-grid" />`).join("")}
          <line x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}" class="bars-axis" />
        </svg>
        <div class="chart-empty">No activity in this range — send a message to the agent.</div>
      </div>`;
    return;
  }

  const rawMax = Math.max(...vals);
  const max = niceCeiling(rawMax);
  const peakIdx = vals.indexOf(rawMax);
  const slot = plotW / n;
  const colW = Math.max(4, Math.min(slot * 0.42, 34));
  const rx = Math.min(5, colW / 3);
  const TICKS = 4;

  const grid = [];
  for (let k = 0; k <= TICKS; k++) {
    const gy = padT + (plotH * k) / TICKS;
    const gv = (max * (TICKS - k)) / TICKS;
    grid.push(
      `<line x1="${padL}" y1="${gy.toFixed(1)}" x2="${W - padR}" y2="${gy.toFixed(1)}" class="bars-grid" />` +
      `<text x="${padL - 8}" y="${(gy + 4).toFixed(1)}" text-anchor="end" class="bars-tick">${escHtml(formatCompact(gv))}</text>`
    );
  }
  // Vertical gridlines through every slot: the background reads as squares.
  for (let i = 0; i <= n; i++) {
    const gx = padL + slot * i;
    grid.push(`<line x1="${gx.toFixed(1)}" y1="${padT}" x2="${gx.toFixed(1)}" y2="${baseY}" class="bars-grid faint" />`);
  }

  // Connected tall columns: one solid column per period rising from the
  // bottom axis; a transparent hit area covers the whole slot so hovering
  // (or tapping) anywhere shows that period's details card.
  const bars = vals
    .map((v, i) => {
      const bx = padL + slot * i + (slot - colW) / 2;
      const by = padT + (1 - Math.max(0, v) / max) * plotH;
      const bh = Math.max(0, baseY - by);
      const isPeak = i === peakIdx;
      const cls = `bars-col ${isTokens ? "bars-col-tok" : "bars-col-req"}${isPeak ? " peak" : ""}`;
      const tip = `${escHtml(formatCompact(v))}${escHtml(unit)}`;
      let s = "";
      if (bh > 0) {
        s += `<rect x="${bx.toFixed(1)}" y="${by.toFixed(1)}" width="${colW.toFixed(1)}" height="${bh.toFixed(1)}" rx="${rx.toFixed(1)}" class="${cls}" />`;
      }
      s += `<rect class="flow-hit" x="${(padL + slot * i).toFixed(1)}" y="${padT}" width="${slot.toFixed(1)}" height="${plotH}" data-label="${escHtml(labs[i])}" data-value="${tip}" tabindex="0" role="img" aria-label="${escHtml(labs[i])}: ${tip}" />`;
      if (isPeak && bh > 0) {
        s += `<text x="${(bx + colW / 2).toFixed(1)}" y="${Math.max(10, by - 6).toFixed(1)}" text-anchor="middle" class="bars-peak-val">${tip}</text>`;
      }
      return s;
    })
    .join("");

  // Thin x labels so phones never crowd.
  const maxTicks = n > 12 ? 5 : 8;
  const keep = new Set();
  if (n <= maxTicks) {
    for (let i = 0; i < n; i++) keep.add(i);
  } else {
    for (let k = 0; k < maxTicks; k++) keep.add(Math.round((k * (n - 1)) / (maxTicks - 1)));
  }
  const xlabels = vals
    .map((_, i) => {
      const cx = padL + slot * i + slot / 2;
      return `<text x="${cx.toFixed(1)}" y="${baseY + 20}" text-anchor="middle" class="bars-x">${keep.has(i) ? escHtml(labs[i]) : ""}</text>`;
    })
    .join("");

  el.innerHTML = `
    <div class="bars-wrap" data-anim>
      <svg class="bars-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet" role="img" aria-label="${escHtml(caption)}">
        ${isTokens ? `<defs><linearGradient id="x5g-tok" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="var(--chart-line-hi)" /><stop offset="1" stop-color="var(--chart-line)" /></linearGradient></defs>` : ""}
        ${grid.join("")}
        ${bars}
        <line x1="${padL}" y1="${baseY}" x2="${W - padR}" y2="${baseY}" class="bars-axis" />
        ${xlabels}
      </svg>
    </div>`;

  attachChartTooltip(el);
  // Touch screens have no hover: tapping a column shows its details at once.
  try {
    if (window.matchMedia("(hover: none)").matches) {
      el.querySelectorAll(".flow-hit").forEach((hit) => {
        hit.addEventListener("click", () => pinChartTip(hit));
      });
    }
  } catch (_) {
    // matchMedia unavailable — hover tooltips still work
  }
}

function setBadge(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function setRangeLabel(id, range) {
  const el = document.getElementById(id);
  if (el) el.textContent = RANGE_LABELS[range] || RANGE_LABELS["7d"];
}

export async function initCharts() {
  const tokensEl = document.getElementById("chartTokens");
  const reqEl = document.getElementById("chartRequests");
  if (!tokensEl || !reqEl) return;

  // Silent background refresh: skip DOM updates when data is unchanged
  // (no flicker), and keep the last good chart if a tick fails instead of
  // wiping it. The page entrance played once at boot — data swaps in with
  // no re-animation.
  const seen = { tokens: null, req: null };
  let firstPaint = true;
  let range = "7d";
  let cached = null;

  function sig(r, labels, values) {
    return r + "|" + labels.join("|") + "#" + values.join(",");
  }

  function paint() {
    if (!cached) return;
    const { labels, counts, tokens } = sliceRange(cached, range);

    const sTok = sig(range, labels, tokens);
    const sReq = sig(range, labels, counts);
    if (sTok !== seen.tokens) {
      seen.tokens = sTok;
      renderBars(tokensEl, labels, tokens, "", "tok");
    }
    if (sReq !== seen.req) {
      seen.req = sReq;
      renderBars(reqEl, labels, counts, "", "req");
    }
    firstPaint = false;

    const totalReq = counts.reduce((a, b) => a + b, 0);
    const totalTok = tokens.reduce((a, b) => a + b, 0);
    setBadge("tokensBadge", `${formatCompact(totalTok)} total`);
    setBadge("requestsBadge", `${formatCompact(totalReq)} total`);
    setRangeLabel("tokensRangeLabel", range);
    setRangeLabel("requestsRangeLabel", range);
  }

  function paintEmpty() {
    renderBars(tokensEl, [], [], "", "tok");
    renderBars(reqEl, [], [], "", "req");
    setBadge("tokensBadge", "unavailable");
    setBadge("requestsBadge", "unavailable");
    firstPaint = false;
  }

  async function load() {
    let data = null;
    try {
      const res = await fetch("/api/cost?days=90", { cache: "no-store" });
      if (!res.ok) throw new Error("bad status");
      data = await res.json();
    } catch (_) {
      data = null;
    }
    if (!data) {
      // Transient failure: keep showing the last good chart (or empty on very first paint).
      if (firstPaint) paintEmpty();
      return;
    }
    cached = data;
    paint();
  }

  document.querySelectorAll("[data-chart-range]").forEach((box) => {
    box.addEventListener("click", (e) => {
      const btn = e.target.closest("button[data-range]");
      if (!btn || btn.dataset.range === range) return;
      range = btn.dataset.range;
      document.querySelectorAll("[data-chart-range] button").forEach((b) => {
        b.classList.toggle("active", b.dataset.range === range);
      });
      paint();
    });
  });

  await load();
  // Push replaces the old 30s poll: reload only when the server fans a new
  // dashboard snapshot (same freshness, O(1) total).
  onPush("dashboard", () => {
    load().catch(() => {});
  });
}