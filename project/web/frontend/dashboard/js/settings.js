/**
 * settings.js — check-then-confirm updates, backup/restore, chat history and account.
 * Owns: project version line, check-then-confirm updates (check only:
 * compares updates.json (repo root) with GitHub and shows its features,
 * with a separate "Update now" button that runs only after your approval —
 * code only, so no data is ever lost — plus live server-side progress while
 * updating), backup/restore, logout (behind a transform-animated confirm
 * dialog via the shared modal shell). License controls live on the
 * Subscription page only; the key can never be stopped from here.
 */
"use strict";

import { closeModal, modalShell, openModal, toast } from "./ui.js?v=13";

async function _getJson(url, opts) {
  const r = await fetch(url, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || ("Request failed (" + r.status + ")"));
  return data;
}

function _setStatus(text, ok) {
  const el = document.getElementById("updateStatus");
  if (!el) return;
  el.textContent = text;
  el.classList.toggle("ok", ok === true);
  el.classList.toggle("warn", ok === false);
}

function _showProgress(show, done) {
  const prog = document.getElementById("updateProg");
  const bar = document.getElementById("updateBar");
  const log = document.getElementById("updateLog");
  if (prog) prog.hidden = !show;
  if (log) log.hidden = !show;
  if (bar) {
    bar.classList.toggle("update-bar-running", show && !done);
    bar.style.transform = show && done ? "scaleX(1)" : "";
  }
  if (log && show) log.textContent = "";
}

function _hideNotes() {
  const wrap = document.getElementById("updateNotesWrap");
  if (wrap) wrap.hidden = true;
}

function _showNotes(title, body) {
  const wrap = document.getElementById("updateNotesWrap");
  const titleEl = document.getElementById("updateNotesTitle");
  const bodyEl = document.getElementById("updateNotes");
  if (!wrap || !titleEl || !bodyEl) return;
  if (!body) {
    wrap.hidden = true;
    return;
  }
  titleEl.textContent = title || "What's new";
  bodyEl.textContent = body;
  wrap.hidden = false;
}

function _setUpdateButtons(mode) {
  // One glass button visible at a time: "check" | "update" | "none" (while busy).
  const checkBtn = document.getElementById("checkUpdateBtn");
  const runBtn = document.getElementById("updateRunBtn");
  if (checkBtn) {
    checkBtn.hidden = mode !== "check";
    checkBtn.disabled = false;
  }
  if (runBtn) {
    runBtn.hidden = mode !== "update";
    runBtn.disabled = false;
  }
}

function _pollUpdateProgress(btn) {
  let shown = 0;
  const tick = async () => {
    let snap;
    try {
      snap = await _getJson("/api/update_progress");
    } catch (_) {
      setTimeout(tick, 2000);
      return;
    }
    const log = document.getElementById("updateLog");
    const lines = Array.isArray(snap.lines) ? snap.lines : [];
    if (log && lines.length > shown) {
      log.textContent += lines.slice(shown).join("\n") + "\n";
      log.scrollTop = log.scrollHeight;
      shown = lines.length;
    }
    if (snap.done) {
      _showProgress(true, true);
      if (snap.ok) {
        _setStatus("Updated to the latest release. Restart the console to apply it.", true);
        toast("Updated — restart the console");
        _setUpdateButtons("check");
      } else {
        _setStatus("Update failed — see the log above.", false);
        _setUpdateButtons("update");
      }
      return;
    }
    setTimeout(tick, 1500);
  };
  setTimeout(tick, 1200);
}

async function _onCheckUpdates() {
  _setUpdateButtons("none");
  _setStatus("Checking updates.json on GitHub for a new version…");
  _showProgress(false);
  _hideNotes();
  let info;
  try {
    info = await _getJson("/api/update_check");
  } catch (e) {
    _setStatus("Check failed: " + e.message, false);
    _setUpdateButtons("check");
    return;
  }
  const local = info.local || "unknown";
  const localTag = "local v" + local;
  if (info.error && !info.latest && info.up_to_date !== false) {
    _setStatus("Check failed: " + info.error + " (" + localTag + ")", false);
    _setUpdateButtons("check");
    return;
  }
  if (info.up_to_date) {
    _setStatus("You are up to date — " + localTag + ".", true);
    _setUpdateButtons("check");
    return;
  }
  if (info.up_to_date === false) {
    // Update found: tell first, show the features, reveal the Update button.
    const remoteTag = info.latest ? "v" + info.latest : "a newer version";
    _setStatus("New version " + remoteTag + " available (" + localTag + ") — read what's new, then press Update now. No data will be lost: the update refreshes code only.");
    _showNotes("What's new in v" + (info.latest || ""), info.notes);
    _setUpdateButtons("update");
    return;
  }
  _setStatus("Check failed: " + (info.error || "nothing to compare yet.") + " (" + localTag + ")", false);
  _setUpdateButtons("check");
}

async function _onRunUpdate() {
  _setUpdateButtons("none");
  _setStatus("Updating now — code only, your data is never touched. Watch the progress below.");
  _showProgress(true, false);
  try {
    await _getJson("/api/update_run", { method: "POST" });
  } catch (e) {
    _setStatus("Update could not start: " + e.message, false);
    _setUpdateButtons("update");
    return;
  }
  _pollUpdateProgress();
}

const LOGOUT_ICON = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" /><polyline points="16 17 21 12 16 7" /><line x1="21" y1="12" x2="9" y2="12" /></svg>`;

function openLogoutConfirm(onConfirm) {
  // Shared modal shell: transform rise on open, transform sink on close
  // (openModal/closeModal own both animations; data-close + Escape + outside
  // all funnel through the same animated close path).
  openModal(modalShell({
    icon: LOGOUT_ICON,
    title: "Sign out?",
    subtitle: "End this console session",
    body: `<div class="block"><p class="modal-body-text">You will be signed out on this device and local preferences will be cleared.</p></div>`,
    foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn danger" id="logoutConfirmBtn" type="button">Sign out</button>`,
  }));
  document.getElementById("logoutConfirmBtn")?.addEventListener("click", () => {
    closeModal();
    if (typeof onConfirm === "function") onConfirm();
  });
}

async function _refreshChatCount() {
  // Live message count for the main chat (conversation.json only).
  const badge = document.getElementById("chatCountBadge");
  if (!badge) return;
  try {
    const res = await fetch("/api/conversation", { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    const msgs = data && Array.isArray(data.messages) ? data.messages : null;
    if (!res.ok || !msgs) throw new Error("count failed");
    badge.textContent = msgs.length === 0
      ? "No messages"
      : `${msgs.length} message${msgs.length === 1 ? "" : "s"}`;
  } catch (_) {
    badge.textContent = "—";
  }
}

function _setBackupStatus(text) {
  const el = document.getElementById("backupStatus");
  if (el) el.textContent = text;
}

function _formatSize(bytes) {
  // Honest size: small backups show KB (never a confusing "0.0 MB").
  const n = Number(bytes);
  if (!Number.isFinite(n) || n < 0) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

function _formatDateTime(iso) {
  try {
    const d = new Date(iso);
    if (Number.isNaN(d.getTime())) return String(iso || "");
    return d.toLocaleString();
  } catch (_) {
    return String(iso || "");
  }
}

function _rememberLocalBackup(entry) {
  // Fallback only: the server meta (backup_meta.json via /api/backup/info)
  // is authoritative; this keeps a date on screen if the info call fails.
  try {
    localStorage.setItem("x5:last-backup", JSON.stringify(entry));
  } catch (_) {}
}

function _readLocalBackup() {
  try {
    const raw = localStorage.getItem("x5:last-backup");
    const d = JSON.parse(raw || "null");
    if (d && d.at) return d;
  } catch (_) {}
  return null;
}

async function _loadBackupInfo() {
  // Paints "Last backup: <date> • <size> • <n> files" under Backup & restore.
  try {
    const r = await fetch("/api/backup/info", { cache: "no-store", credentials: "same-origin" });
    const d = await r.json().catch(() => ({}));
    if (r.ok && d && d.has_backup && d.last_backup_at) {
      const parts = [`Last backup: ${_formatDateTime(d.last_backup_at)}`];
      if (Number(d.size_bytes) > 0) parts.push(_formatSize(d.size_bytes));
      if (Number(d.files) > 0) parts.push(`${d.files} file${Number(d.files) === 1 ? "" : "s"}`);
      _setBackupStatus(parts.join(" • "));
      _rememberLocalBackup({ at: d.last_backup_at, files: d.files, size: d.size_bytes });
      return;
    }
  } catch (_) {}
  const local = _readLocalBackup();
  if (local) {
    _setBackupStatus(`Last backup: ${_formatDateTime(local.at)}`);
  } else {
    _setBackupStatus("No backup yet.");
  }
}

function _showBackupProgress(show, running, label) {
  const prog = document.getElementById("backupProg");
  const bar = document.getElementById("backupBar");
  const lab = document.getElementById("backupLabel");
  if (prog) prog.hidden = !show;
  if (bar) {
    bar.classList.toggle("update-bar-running", !!running);
    bar.style.transform = show && !running ? "scaleX(1)" : "";
  }
  if (lab) {
    if (label) {
      lab.textContent = label;
      lab.hidden = false;
    } else {
      lab.hidden = true;
    }
  }
}

async function _onBackupNow(btn) {
  if (btn) btn.disabled = true;
  _setBackupStatus("Preparing your backup…");
  _showBackupProgress(true, true, "Zipping chat, studios, memories, skills, files and settings…");
  try {
    const r = await fetch("/api/backup/download", { credentials: "same-origin" });
    if (r.status === 401) {
      location.href = "/login.html";
      return;
    }
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error((d && d.error) || ("Backup failed (" + r.status + ")"));
    }
    const blob = await r.blob();
    const cd = r.headers.get("Content-Disposition") || "";
    let name = "x5agent-backup.zip";
    try {
      const m = cd.match(/filename="([^"]+)"/);
      if (m && m[1]) name = m[1];
    } catch (_) {}
    const url = URL.createObjectURL(blob);
    try {
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } finally {
      setTimeout(() => URL.revokeObjectURL(url), 10000);
    }
    const size = _formatSize(blob.size);
    _showBackupProgress(true, false, "");
    toast("Backup downloaded");
    // Server meta (backup_meta.json) is authoritative for the date/count —
    // it was written while the zip was built, so it is fresh by now.
    try {
      const info = await _getJson("/api/backup/info");
      if (info && info.has_backup && info.last_backup_at) {
        _rememberLocalBackup({ at: info.last_backup_at, files: info.files, size: info.size_bytes, name });
        const tail = [`Last backup: ${_formatDateTime(info.last_backup_at)}`];
        if (Number(info.files) > 0) tail.push(`${info.files} file${Number(info.files) === 1 ? "" : "s"}`);
        _setBackupStatus(`Backup ready — ${name} (${size}). Keep it somewhere safe. ${tail.join(" • ")}.`);
      } else {
        const when = new Date().toISOString();
        _rememberLocalBackup({ at: when, files: 0, size: blob.size, name });
        _setBackupStatus(`Backup ready — ${name} (${size}). Keep it somewhere safe. Last backup: ${_formatDateTime(when)}.`);
      }
    } catch (_) {
      const when = new Date().toISOString();
      _rememberLocalBackup({ at: when, files: 0, size: blob.size, name });
      _setBackupStatus(`Backup ready — ${name} (${size}). Keep it somewhere safe. Last backup: ${_formatDateTime(when)}.`);
    }
  } catch (e) {
    _setBackupStatus("Backup failed: " + (e && e.message ? e.message : "unknown error"));
    _showBackupProgress(false, false, "");
    toast((e && e.message) || "Backup failed");
  } finally {
    if (btn) btn.disabled = false;
  }
}

function _uploadBackupFile(file) {
  // Raw zip bytes (no multipart): the server streams them back to Data.
  // XMLHttpRequest drives the loading bar with real upload progress.
  _setBackupStatus("Restoring… do not close this page.");
  _showBackupProgress(true, true, "Uploading " + file.name + "…");
  const done = (ok, msg) => {
    if (ok) {
      _setBackupStatus(msg);
      _showBackupProgress(true, false, "");
      toast("Restore done — reloading…");
      // Drop cached paint + any half-dead Continue pill: the restored chat
      // is a different history, so a refresh must repaint from the server.
      try { localStorage.removeItem("x5_boot_cache_v1"); } catch (_) {}
      try { localStorage.removeItem("x5:stopped-task"); } catch (_) {}
      try { sessionStorage.clear(); } catch (_) {}
      setTimeout(() => location.reload(), 1500);
    } else {
      _setBackupStatus(msg);
      _showBackupProgress(false, false, "");
      toast(msg);
    }
  };
  try {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", "/api/backup/restore", true);
    xhr.responseType = "json";
    xhr.upload.onprogress = (ev) => {
      try {
        const bar = document.getElementById("backupBar");
        const lab = document.getElementById("backupLabel");
        if (ev && ev.lengthComputable && ev.total > 0 && bar) {
          bar.classList.remove("update-bar-running");
          bar.style.transform = `scaleX(${Math.min(1, ev.loaded / ev.total).toFixed(3)})`;
          if (lab) {
            const pct = Math.round((ev.loaded / ev.total) * 100);
            lab.textContent = `Uploading… ${pct}%`;
            lab.hidden = false;
          }
        } else if (lab) {
          lab.textContent = "Uploading…";
          lab.hidden = false;
        }
      } catch (_) {}
    };
    xhr.onload = () => {
      if (xhr.status === 401) {
        location.href = "/login.html";
        return;
      }
      const d = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300 && d && d.ok) {
        const n = Number(d.files || 0);
        done(true, `Restore done — ${n} file${n === 1 ? "" : "s"} restored. Reloading…`);
      } else {
        done(false, "Restore failed: " + ((d && d.error) || ("HTTP " + xhr.status)));
      }
    };
    xhr.onerror = () => done(false, "Restore failed: connection error");
    xhr.ontimeout = () => done(false, "Restore failed: timed out");
    xhr.send(file);
  } catch (e) {
    done(false, "Restore failed: " + (e && e.message ? e.message : "unknown error"));
  }
}

function _onRestorePick() {
  const input = document.getElementById("restoreFileInput");
  if (!input) return;
  input.value = "";
  input.click();
}

export function initSettings() {
  document.getElementById("checkUpdateBtn")?.addEventListener("click", (e) => {
    _onCheckUpdates(e.currentTarget);
  });

  document.getElementById("backupNowBtn")?.addEventListener("click", (e) => {
    _onBackupNow(e.currentTarget);
  });

  document.getElementById("restorePickBtn")?.addEventListener("click", () => {
    _onRestorePick();
  });

  document.getElementById("restoreFileInput")?.addEventListener("change", (e) => {
    const f = e.target && e.target.files && e.target.files[0];
    if (!f) return;
    if (!/\.zip$/i.test(f.name)) {
      toast("Pick a .zip backup file");
      e.target.value = "";
      return;
    }
    // Destructive: the backup replaces ALL current data.
    openModal(modalShell({
      icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="17 8 12 3 7 8" /><line x1="12" y1="3" x2="12" y2="15" /></svg>`,
      title: "Restore backup?",
      subtitle: f.name,
      body: `<div class="block"><p class="modal-body-text">This replaces ALL current data (chat, PDF Studios, memories, skills, files, settings and API keys) with the backup. You may need to sign in again afterwards. This cannot be undone.</p></div>`,
      foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn danger" id="restoreConfirmBtn" type="button">Restore now</button>`,
    }));
    document.getElementById("restoreConfirmBtn")?.addEventListener("click", () => {
      closeModal();
      _uploadBackupFile(f);
      try { e.target.value = ""; } catch (_) {}
    }, { once: true });
  });

  document.getElementById("updateRunBtn")?.addEventListener("click", (e) => {
    _onRunUpdate(e.currentTarget);
  });

  // The license itself has no Settings controls on purpose: a subscription is
  // managed on the Subscription page and can never be stopped from the UI.

  document.getElementById("clearChatBtn")?.addEventListener("click", () => {
    // Destructive: clears the MAIN chat history + its embeddings + studios.
    // Saved memories are never touched — the confirm says so explicitly.
    openModal(modalShell({
      icon: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1 2 2v2" /></svg>`,
      title: "Delete chat history?",
      subtitle: "Main chat only",
      body: `<div class="block"><p class="modal-body-text">This deletes the main agent chat and its search index only. Every PDF Studio and your saved memories will NOT be deleted — studios are never deleted by anything.</p></div>`,
      foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn danger" id="clearChatConfirmBtn" type="button">Delete chat</button>`,
    }));
    document.getElementById("clearChatConfirmBtn")?.addEventListener("click", async (e) => {
      const btn = e.currentTarget;
      // Loading spinner until the server confirms BOTH files are wiped
      // (conversation.json + chat embeddings) — the response only arrives
      // after the full delete, so closing here means fully deleted.
      btn.disabled = true;
      btn.classList.add("loading");
      try {
        const data = await _getJson("/api/conversation/clear", { method: "POST" });
        closeModal();
        // Reset the Agent view to the empty welcome state (same as fresh history).
        try {
          const inner = document.getElementById("chatInner");
          if (inner) {
            inner.querySelectorAll(".msg").forEach((m) => m.remove());
            if (!document.getElementById("welcomeMsg")) {
              const w = document.createElement("div");
              w.className = "chat-welcome";
              w.id = "welcomeMsg";
              w.innerHTML = "<h2>How may I assist you, sir?</h2><p>I&#39;m X5Agent, your personal AI agent, here to save you time and handle the work for you</p>";
              inner.prepend(w);
            }
          }
        } catch (_) {}
        _refreshChatCount();
        // No task exists anymore: disarm a stale Continue pill (same key
        // the chat uses) and tell the Agent view to recompute its button.
        // Drop the boot hash-cache too: its chat_last50 slice is stale now —
        // the next agent-section sync mints a fresh empty hash instead of
        // repainting deleted messages for one beat.
        try { localStorage.removeItem("x5:stopped-task"); } catch (_) {}
        try { localStorage.removeItem("x5_boot_cache_v1"); } catch (_) {}
        try { window.dispatchEvent(new CustomEvent("x5:chat-cleared")); } catch (_) {}
        toast(data && typeof data.removed === "number" && data.removed
          ? `Chat + search index deleted (${data.removed} messages)`
          : "Chat + search index deleted");
      } catch (err) {
        btn.disabled = false;
        btn.classList.remove("loading");
        toast((err && err.message) || "Delete failed");
      }
    });
  });

  document.getElementById("logoutBtn")?.addEventListener("click", () => {
    openLogoutConfirm(() => {
      fetch("/api/auth/logout", { method: "POST" }).catch(() => {}).finally(() => {
        try {
          ["x5-gateway-whatsapp", "x5-gateway-telegram", "x5_boot_cache_v1"].forEach((k) => localStorage.removeItem(k));
        } catch (_) {
          // ignore
        }
        location.href = "/login.html";
      });
    });
  });

  // Returned for the app.js boot barrier (black splash waits on both).
  // NOTE: keep this return LAST - everything above wires buttons, and an
  // earlier return would leave them dead (unreachable code).
  return Promise.allSettled([
    _refreshChatCount(),
    _loadBackupInfo(),
  ]);
}
