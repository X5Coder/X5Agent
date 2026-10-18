/**
 * events.js — THE ONE server -> browser stream (single SSE connection).
 *
 * Transport rule: server -> browser is ONE SSE stream (GET /api/events,
 * opened once when the UI loads). Browser -> server is plain POST only
 * (/api/sync below + the chat POSTs). No WebSocket anywhere, no polling.
 *
 * Sync rule (the browser owns the cache): the browser keeps the last data it
 * received + the server hash for every section, and POSTs only the hashes it
 * holds to /api/sync (a tiny request, all sections at once). The server
 * answers ON THIS STREAM, in priority order — status, agent, dashboard,
 * rest — with a section frame (fresh data + its new hash) when the hash
 * broke, or an `unchanged` frame (op_id + section, no data) when it still
 * matches, so the stored render stays. Every frame carries seq (number),
 * op_id (id) and section (item), so the UI always knows what arrived and
 * where it belongs.
 *
 * Auth rule: the session lives in a cookie (30 days) that this stream and
 * every request carry. A dead session answers 401 — the sync probe below
 * redirects to the login page; anything else (proxy 502, idle reset,
 * offline) only redials, never logs the user out.
 */
"use strict";

import { loadBootCache, saveBootCache } from "./boot_cache.js?v=2";

// Section priority order = the server's answer order.
const SECTION_ORDER = ["status", "agent", "dashboard", "rest"];
// Which push channels each section feeds (modules subscribe by channel).
const SECTION_CHANNELS = {
  status: ["chat", "browser"],
  agent: ["chat_last50"],
  dashboard: ["dashboard"],
  rest: ["workspace", "scheduler", "gateway"],
};
// Transient channels (run progress + live card): pushed, never hashed.
const TRANSIENT = new Set(["run", "chat_run", "chat_done", "live", "chat"]);
// Channels worth remembering for late subscribers + the persistent cache.
const STATE_TYPES = new Set(["chat", "browser", "chat_last50", "dashboard",
  "workspace", "scheduler", "gateway", "live"]);
// Every SSE event name this client listens for (section frames + signals).
const CHANNELS = ["ping", "unchanged", "changed", ...SECTION_ORDER,
  ...Object.values(SECTION_CHANNELS).flat(), ...TRANSIENT];

const SYNC_MIN_MS = 1200;   // one sync request per ~1.2s (coalesced)
const SYNC_RETRY_MS = 4000; // a failed sync retries once, bounded by the next push

const handlers = new Map();   // channel -> Set<fn>
const lastState = new Map();  // channel -> last data (replayed to late subscribers)
const lastJson = new Map();   // channel -> last data as JSON (skip identical emits)
const sectionHash = new Map(); // section -> hash the browser holds

let started = false;
let sseSource = null;      // the ONE stream: created once, never duplicated
let streamOpen = false;
let reconnectTimer = null;
let reconnectAttempt = 0;
let syncTimer = null;
let syncRetryTimer = null;
let syncSeq = 0;
let lastSyncAt = 0;

// Persistent cache: restored BEFORE any module subscribes, so the first
// onPush replay paints cached data instantly on refresh. The sync answer
// then reconciles: unchanged hash = cached render stays, broken hash =
// re-render + cache rewritten with the new hash.
let cacheLoaded = false;
// Migrate legacy channel key from the pre-section cache (chat_last50 was a
// section, now it is a channel inside the `agent` section).
try {
  const cached = loadBootCache();
  if (cached) {
    for (const [k, v] of Object.entries(cached.hashes || {})) {
      if (typeof k === "string" && typeof v === "string" && SECTION_ORDER.includes(k)) {
        sectionHash.set(k, v);
      }
    }
    for (const [k, v] of Object.entries(cached.states || {})) {
      if (typeof k === "string" && STATE_TYPES.has(k)) lastState.set(k, v);
    }
    cacheLoaded = lastState.size > 0;
  }
} catch (_) {}

export function hasBootCache() {
  return cacheLoaded;
}

function persistCache() {
  // Best-effort: writes ALWAYS go through here, so new data and its hash
  // land in the store in the same step.
  try {
    const hashes = {};
    const states = {};
    for (const [k, v] of sectionHash) {
      if (typeof v === "string") hashes[k] = v;
    }
    for (const [k, v] of lastState) {
      if (STATE_TYPES.has(k)) states[k] = v;
    }
    saveBootCache(hashes, states);
  } catch (_) {}
}

// Boot barrier: resolves once the first sync answer cycle is applied (data
// or unchanged). app.js waits on this plus every module's first load — or
// lifts early on the fast path when the cache already painted the shell.
let readyResolve = null;
const readyPromise = new Promise((resolve) => {
  readyResolve = resolve;
});
let readySent = false;

function markReady() {
  if (readySent) return;
  readySent = true;
  try {
    readyResolve();
  } catch (_) {}
}

export function whenFirstSync() {
  return readyPromise;
}

export function onPush(type, fn) {
  if (typeof type !== "string" || typeof fn !== "function") return;
  if (!handlers.has(type)) handlers.set(type, new Set());
  handlers.get(type).add(fn);
  // Late subscriber (module init is async): replay the newest snapshot slice.
  try {
    if (STATE_TYPES.has(type) && lastState.has(type)) fn(lastState.get(type));
  } catch (_) {}
}

function emit(type, data) {
  const set = handlers.get(type);
  if (!set) return;
  for (const fn of set) {
    try {
      fn(data);
    } catch (_) {}
  }
}

function setChannel(channel, data) {
  // Remember + render one channel, skipping an identical repaint (so a
  // section frame that carries the same value costs nothing).
  let json = "";
  try {
    json = JSON.stringify(data);
  } catch (_) {
    json = "";
  }
  lastState.set(channel, data);
  if (json && lastJson.get(channel) === json) return;
  lastJson.set(channel, json);
  emit(channel, data);
}

function applySection(section, data, hash) {
  // One section frame: store the minted hash and render every channel it
  // owns (the section data is always keyed by those channel names).
  if (typeof hash === "string" && hash) sectionHash.set(section, hash);
  if (data && typeof data === "object") {
    for (const ch of SECTION_CHANNELS[section] || []) {
      if (Object.prototype.hasOwnProperty.call(data, ch)) setChannel(ch, data[ch]);
    }
  }
  persistCache();
}

function onServerPush(channel, data) {
  if (typeof channel !== "string") return;
  if (channel === "run" || channel === "chat_run" || channel === "chat_done") {
    emit(channel, data); // progress only: never cached, never hashed
    return;
  }
  if (STATE_TYPES.has(channel)) {
    setChannel(channel, data);
    persistCache();
    return;
  }
  emit(channel, data);
}

function redirectToLogin() {
  try {
    closeStream();
  } catch (_) {}
  try {
    location.replace("/login.html");
  } catch (_) {}
}

function sendSync(force) {
  // Browser -> server: hashes only. The answers arrive on the SSE stream.
  if (!streamOpen) return; // a sync fired on open covers it
  const now = Date.now();
  if (!force && now - lastSyncAt < SYNC_MIN_MS) {
    if (!syncTimer) {
      syncTimer = setTimeout(() => {
        syncTimer = null;
        sendSync(false);
      }, SYNC_MIN_MS - (now - lastSyncAt));
    }
    return;
  }
  lastSyncAt = now;
  const opId = "r_" + (++syncSeq);
  const sections = SECTION_ORDER.map((s) => ({
    section: s,
    hash: sectionHash.get(s) || "",
  }));
  let req;
  try {
    req = fetch("/api/sync", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op_id: opId, sections }),
    });
  } catch (_) {
    return;
  }
  req.then((res) => {
    if (res.status === 401) {
      redirectToLogin(); // session expired: back to the login page
      return;
    }
    if (res.status >= 500 || res.status === 0) scheduleSyncRetry();
  }).catch(() => scheduleSyncRetry());
}

function scheduleSyncRetry() {
  if (syncRetryTimer) return;
  syncRetryTimer = setTimeout(() => {
    syncRetryTimer = null;
    sendSync(true);
  }, SYNC_RETRY_MS);
}

export function requestSync() {
  // Any user interaction may have moved server data: ask with the held hashes.
  sendSync(false);
}

function reconnectDelay() {
  // Capped backoff: 1s -> 2s -> 5s -> 15s steady. This is a persistent
  // subscription (same as native EventSource redial), not an operation
  // retry, so it keeps redialling instead of giving up on the dashboard.
  reconnectAttempt += 1;
  if (reconnectAttempt <= 1) return 1000;
  if (reconnectAttempt === 2) return 2000;
  if (reconnectAttempt === 3) return 5000;
  return 15000;
}

function closeStream() {
  try {
    if (sseSource) sseSource.close();
  } catch (_) {}
  sseSource = null;
  streamOpen = false;
}

function scheduleReconnect() {
  // Never stack timers: one pending redial at most (single-stream rule).
  if (reconnectTimer) return;
  const wait = reconnectDelay();
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    sseSource = null;
    connectStream();
  }, wait);
}

async function handleStreamError() {
  // A closed stream is NOT proof of logout: a proxy/idle timeout resets long
  // SSE streams while the session is still valid. The sync endpoint is
  // session-gated, so its HTTP status is the proof: 401 = dead session ->
  // login page; anything else (ok / no owner / unreachable) -> redial.
  let status = 0;
  try {
    const res = await fetch("/api/sync", {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ op_id: "probe", sections: [] }),
    });
    status = res.status;
  } catch (_) {
    status = -1; // unreachable: never redirect on a network failure
  }
  if (status === 401) {
    redirectToLogin();
    return;
  }
  if (status === 200 || status === 503) {
    reconnectAttempt = 0; // session alive: redial at once, no backoff
    closeStream();
    connectStream();
    return;
  }
  closeStream();
  scheduleReconnect();
}

function onFrame(e) {
  let frame;
  try {
    frame = JSON.parse(e.data);
  } catch (_) {
    return;
  }
  if (!frame || typeof frame !== "object") return;
  const type = typeof frame.type === "string" ? frame.type : e.type;
  reconnectAttempt = 0; // healthy stream: reset the redial backoff
  if (type === "ping") return;
  if (type === "unchanged") {
    // The hash still matches: the stored copy stays, nothing to render.
    markReady();
    return;
  }
  if (type === "changed") {
    // Server data moved: re-sync; the hashes decide what actually re-renders.
    sendSync(false);
    return;
  }
  if (SECTION_CHANNELS[type]) {
    const payload = frame.data && typeof frame.data === "object" ? frame.data : {};
    applySection(type, payload.data, payload.hash);
    markReady();
    return;
  }
  onServerPush(type, frame.data && typeof frame.data === "object" ? frame.data : {});
}

function connectStream() {
  if (sseSource) return;
  let src;
  try {
    src = new EventSource("/api/events");
  } catch (_) {
    scheduleReconnect();
    return;
  }
  sseSource = src;
  // Named SSE events: one listener per channel the server can emit.
  for (const name of CHANNELS) {
    try {
      src.addEventListener(name, onFrame);
    } catch (_) {}
  }
  src.onopen = () => {
    streamOpen = true;
    reconnectAttempt = 0;
    // Opening the stream is also the auth check: a dead session makes this
    // sync answer 401 and the page leaves for login.
    sendSync(true);
  };
  src.onerror = () => {
    // Native EventSource redials by itself while CONNECTING/OPEN; act only
    // when it gave up (CLOSED, e.g. proxy 502 or a 401 gate).
    streamOpen = false;
    try {
      if (src.readyState !== 2) return;
    } catch (_) {}
    handleStreamError();
  };
}

function wireInteractions() {
  // "Any interaction" keeps the cached copy honest without ever polling:
  // the throttle in sendSync coalesces a burst into one tiny hash request.
  try {
    document.addEventListener("click", () => sendSync(false), true);
    document.addEventListener("keydown", () => sendSync(false), true);
    window.addEventListener("focus", () => sendSync(false));
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) sendSync(false);
    });
  } catch (_) {}
}

export function startEvents() {
  // Single stream: second calls are no-ops, so multi-module init can never
  // open a second SSE connection.
  if (started) return;
  started = true;
  wireInteractions();
  try {
    connectStream();
  } catch (_) {}
}
