/**
 * mcp.js — MCP Servers page
 * Owns: listing registered MCP servers, add dialog, connect/disconnect/remove,
 * the OAuth popup handoff + bounded polling, and the server detail dialog.
 * The server owns every OAuth secret; this module only ever sees the public
 * record (name, URL, status, tool catalogue).
 */
"use strict";

import { openModal, closeModal, modalShell, toast } from "./ui.js?v=13";

const MAX_NAME = 60;
const MAX_URL = 2048;
const POLL_MS = 2000;
const POLL_TRIES = 60;

const ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-linecap="round" stroke-linejoin="round">
  <rect x="3" y="3" width="7" height="7" rx="2" />
  <rect x="14" y="14" width="7" height="7" rx="2" />
  <path d="M10 6.5h4.5A2.5 2.5 0 0 1 17 9v4" />
  <path d="M14 17.5h-4.5A2.5 2.5 0 0 1 7 15v-4" />
</svg>`;

const STATES = {
  connected: { label: "Connected", badge: "green" },
  auth_required: { label: "Authorization needed", badge: "amber" },
  error: { label: "Failed", badge: "red" },
  disconnected: { label: "Not connected", badge: "muted" },
};

let grid = null;
let empty = null;
let note = null;
let addBtn = null;
let servers = [];
let initialized = false;
let pollTimer = null;
let pollCount = 0;
let pollId = "";

function escapeHtml(value) {
  return String(value == null ? "" : value).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[ch]));
}

function prettyUrl(raw) {
  try {
    const url = new URL(String(raw || ""));
    return url.host + (url.pathname === "/" ? "" : url.pathname);
  } catch (_) {
    return String(raw || "");
  }
}

function stateOf(server) {
  return STATES[server && server.status] || STATES.disconnected;
}

async function fetchServers() {
  const res = await fetch("/api/mcp/servers", { cache: "no-store" });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data && data.error ? data.error : `HTTP ${res.status}`);
  return Array.isArray(data && data.servers) ? data.servers : [];
}

async function api(action, payload) {
  const res = await fetch("/api/mcp/servers", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    cache: "no-store",
    body: JSON.stringify(Object.assign({ action }, payload || {})),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data && data.error ? data.error : `HTTP ${res.status}`);
  return data;
}

function setNote(text) {
  if (!note) return;
  note.textContent = text || "";
  note.hidden = !text;
}

function render() {
  if (!grid || !empty) return;
  const waiting = (server) => pollId === server.id;
  grid.innerHTML = servers.map((server) => {
    const state = stateOf(server);
    const toolsLine = server.tool_count
      ? `${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}`
      : "No tools reported";
    const statusText = waiting(server)
      ? "Waiting for authorization…"
      : (server.error || state.label);
    const primary = server.status === "connected"
      ? `<button class="glass-btn mcp-act" data-act="disconnect" type="button">Disconnect</button>
         <button class="glass-btn mcp-act" data-act="connect" type="button">Refresh</button>`
      : `<button class="glass-btn mcp-act" data-act="connect" type="button">${server.status === "auth_required" ? "Authorize" : "Connect"}</button>`;
    return `<article class="mini-card mcp-card" data-id="${escapeHtml(server.id)}">
      <div class="mini-top">
        <div class="mini-icon">${ICON}</div>
        <span class="badge ${state.badge}">${state.label}</span>
      </div>
      <button class="mcp-open" data-act="details" type="button">
        <span class="mini-title">${escapeHtml(server.name)}</span>
        <span class="mcp-url">${escapeHtml(prettyUrl(server.url))}</span>
      </button>
      <p class="mini-desc mcp-status">${waiting(server) ? '<span class="mcp-spinner" aria-hidden="true"></span>' : ""}${escapeHtml(statusText)}</p>
      <div class="mini-foot">
        <span class="mini-ver">${escapeHtml(toolsLine)}</span>
        <div class="mcp-actions">
          ${primary}
          <button class="glass-btn danger mcp-act" data-act="remove" type="button">Remove</button>
        </div>
      </div>
    </article>`;
  }).join("");
  empty.hidden = servers.length > 0;
}

async function refresh(showError) {
  try {
    servers = await fetchServers();
    render();
    setNote("");
  } catch (err) {
    if (showError) toast(err instanceof Error ? err.message : "Could not load MCP servers");
  }
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  const had = !!pollId;
  pollId = "";
  pollCount = 0;
  if (had) render();
}

function startPolling(serverId) {
  stopPolling();
  pollId = serverId;
  pollCount = 0;
  render();
  pollTimer = setInterval(async () => {
    pollCount += 1;
    if (pollCount > POLL_TRIES) {
      stopPolling();
      toast("Authorization is still pending. Press Authorize again when ready.");
      return;
    }
    try {
      servers = await fetchServers();
      const current = servers.find((item) => item.id === serverId);
      if (!current || current.status === "connected" || current.status === "error") {
        stopPolling();
        return;
      }
      render();
    } catch (_) {}
  }, POLL_MS);
}

function openAuth(authUrl, serverId) {
  if (!authUrl) return;
  let win = null;
  try {
    win = window.open(authUrl, "x5_mcp_oauth", "popup=yes,width=540,height=760");
  } catch (_) {
    win = null;
  }
  if (!win) {
    // Popup blocked: finish in this tab; the callback returns to /mcp.
    window.location.assign(authUrl);
    return;
  }
  toast("Complete the authorization in the new window");
  startPolling(serverId);
}

async function runConnect(serverId) {
  const server = servers.find((item) => item.id === serverId);
  const name = server ? server.name : "MCP server";
  try {
    toast(`Connecting ${name}…`);
    const result = await api("connect", { id: serverId });
    servers = await fetchServers();
    render();
    if (result && result.auth_url) {
      toast(`Authorize ${name} to finish`);
      openAuth(result.auth_url, serverId);
    } else {
      toast(`${name} connected`);
    }
  } catch (err) {
    try {
      servers = await fetchServers();
      render();
    } catch (_) {}
    toast(err instanceof Error ? err.message : "Connection failed");
  }
}

async function runDisconnect(serverId) {
  const server = servers.find((item) => item.id === serverId);
  const name = server ? server.name : "MCP server";
  try {
    await api("disconnect", { id: serverId });
    servers = await fetchServers();
    render();
    toast(`${name} disconnected`);
  } catch (err) {
    toast(err instanceof Error ? err.message : "Disconnect failed");
  }
}

function openAddDialog() {
  stopPolling();
  openModal(modalShell({
    icon: ICON,
    title: "Add MCP server",
    subtitle: "Name it and paste its MCP endpoint URL",
    body: `
      <div class="field">
        <label for="mcpName">Name</label>
        <div class="field-wrap">
          <input class="input" id="mcpName" maxlength="${MAX_NAME}" placeholder="Notion" autocomplete="off" />
        </div>
      </div>
      <div class="field">
        <label for="mcpUrl">Server URL</label>
        <div class="field-wrap">
          <input class="input" id="mcpUrl" maxlength="${MAX_URL}" placeholder="https://mcp.example.com/mcp" autocomplete="off" spellcheck="false" />
        </div>
        <span class="hint">HTTPS anywhere, or HTTP on this machine and your local network (localhost, 192.168.x.x, .local). If the server asks for OAuth, authorization opens next.</span>
      </div>
      <div class="field"><p class="err" id="mcpErr" hidden></p></div>`,
    foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn" id="mcpSaveBtn" type="button">Add server</button>`,
  }));

  const nameEl = document.getElementById("mcpName");
  const urlEl = document.getElementById("mcpUrl");
  const errEl = document.getElementById("mcpErr");
  const saveBtn = document.getElementById("mcpSaveBtn");

  function showError(message) {
    if (!errEl) return;
    errEl.textContent = message;
    errEl.hidden = false;
  }

  async function submit() {
    const name = String(nameEl && nameEl.value || "").trim();
    const url = String(urlEl && urlEl.value || "").trim();
    if (errEl) errEl.hidden = true;
    if (!name || !url) {
      showError("Name and server URL are required");
      return;
    }
    if (!saveBtn) return;
    saveBtn.disabled = true;
    saveBtn.classList.add("loading");
    try {
      const result = await api("add", { name, url });
      servers = await fetchServers();
      render();
      closeModal();
      toast("MCP server added");
      if (result && result.auth_url && result.server) {
        openAuth(result.auth_url, result.server.id);
      }
    } catch (err) {
      showError(err instanceof Error ? err.message : "Could not add the server");
    } finally {
      saveBtn.disabled = false;
      saveBtn.classList.remove("loading");
    }
  }

  saveBtn?.addEventListener("click", submit);
  [nameEl, urlEl].forEach((el) => {
    el?.addEventListener("keydown", (event) => {
      if (event.key === "Enter") {
        event.preventDefault();
        submit();
      }
    });
  });
  setTimeout(() => nameEl?.focus(), 120);
}

function openDetails(server) {
  if (!server) return;
  const state = stateOf(server);
  const tools = Array.isArray(server.tools) ? server.tools : [];
  const toolsHtml = tools.length
    ? `<div class="mcp-tools">${tools.map((tool) => `
        <div class="mcp-tool">
          <div class="mcp-tool-name">${escapeHtml(tool.name)}</div>
          ${tool.description ? `<div class="mcp-tool-desc">${escapeHtml(tool.description)}</div>` : ""}
        </div>`).join("")}</div>`
    : `<p class="modal-body-text">${server.status === "connected" ? "This server reported no tools." : "Connect the server to read its tools."}</p>`;
  openModal(modalShell({
    icon: ICON,
    title: escapeHtml(server.name),
    subtitle: escapeHtml(prettyUrl(server.url)),
    body: `
      <div class="block">
        <div class="mcp-state">
          <span class="badge ${state.badge}">${state.label}</span>
          ${server.tool_count ? `<span class="mcp-state-text">${server.tool_count} tool${server.tool_count === 1 ? "" : "s"}</span>` : ""}
        </div>
        ${server.error ? `<p class="mcp-error">${escapeHtml(server.error)}</p>` : ""}
      </div>
      <div class="block">
        <div class="block-title">Endpoint</div>
        <div class="conn-detail mcp-endpoint">${escapeHtml(server.url)}</div>
      </div>
      <div class="block">
        <div class="block-title">Tools</div>
        ${toolsHtml}
      </div>`,
    foot: `
      <button class="glass-btn" data-close type="button">Close</button>
      ${server.status === "connected"
        ? `<button class="glass-btn danger" id="mcpDetailDisconnect" type="button">Disconnect</button>`
        : `<button class="glass-btn" id="mcpDetailConnect" type="button">${server.status === "auth_required" ? "Authorize" : "Connect"}</button>`}`,
  }));
  document.getElementById("mcpDetailConnect")?.addEventListener("click", () => {
    closeModal();
    runConnect(server.id);
  });
  document.getElementById("mcpDetailDisconnect")?.addEventListener("click", () => {
    closeModal();
    runDisconnect(server.id);
  });
}

function confirmRemove(server) {
  if (!server) return;
  openModal(modalShell({
    icon: ICON,
    title: `Remove ${escapeHtml(server.name)}?`,
    subtitle: "Only this device forgets the connection",
    body: `<div class="block"><p class="modal-body-text">The MCP server itself is not changed. The saved authorization on this device is deleted.</p></div>`,
    foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn danger" id="mcpRemoveBtn" type="button">Remove</button>`,
  }));
  document.getElementById("mcpRemoveBtn")?.addEventListener("click", async (event) => {
    const btn = event.currentTarget;
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      await api("remove", { id: server.id });
      closeModal();
      servers = await fetchServers();
      render();
      toast("MCP server removed");
    } catch (err) {
      btn.disabled = false;
      btn.classList.remove("loading");
      toast(err instanceof Error ? err.message : "Remove failed");
    }
  });
}

export function initMcp() {
  if (initialized) return Promise.resolve();
  initialized = true;
  grid = document.getElementById("mcpGrid");
  empty = document.getElementById("mcpEmpty");
  note = document.getElementById("mcpNote");
  addBtn = document.getElementById("addMcpBtn");

  addBtn?.addEventListener("click", openAddDialog);
  grid?.addEventListener("click", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-act]") : null;
    if (!target) return;
    const card = target.closest(".mcp-card");
    if (!card) return;
    const server = servers.find((item) => item.id === card.dataset.id);
    if (!server) return;
    const action = target.dataset.act;
    if (action === "details") openDetails(server);
    else if (action === "connect") runConnect(server.id);
    else if (action === "disconnect") runDisconnect(server.id);
    else if (action === "remove") confirmRemove(server);
  });

  window.addEventListener("x5:page-open", (event) => {
    if (event.detail === "mcp") refresh(true);
  });
  window.addEventListener("message", (event) => {
    if (event.origin !== window.location.origin) return;
    const data = event.data;
    if (!data || data.type !== "x5-mcp-oauth") return;
    stopPolling();
    refresh(true);
  });
  return refresh();
}
