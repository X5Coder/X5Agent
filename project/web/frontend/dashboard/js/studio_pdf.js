/**
 * studio_pdf.js — PDF Studio overlay (full page above the agent template).
 * Owns: [STUDIO:pdf_N] whole-card glass buttons with an arrow (end of the
 * reply, English status + designed/pending counts, orange/green/grey dot) +
 * fullscreen /studio-pdf/<id> overlay with open animation + session routing
 * (pushState on open, deep-link boot, Back closes).
 * The React shell is built ONCE (web/studio-pdf/dist); data lives in
 * X5Agent/Data/studios/<id>/ (backup). Never throws.
 */
"use strict";

function studioIdFromRoute() {
  try {
    const m = (location.pathname || "").match(/^\/studio-pdf\/([A-Za-z0-9_-]+)/);
    if (m && m[1] !== "shell" && m[1] !== "assets") return m[1];
  } catch (_) {}
  return null;
}

function studioShellUrl(studioId) {
  return "/studio-pdf/shell/" + encodeURIComponent(studioId);
}

function openStudioOverlay(studioId, push) {
  try {
    let host = document.getElementById("studioOverlay");
    if (!host) {
      host = document.createElement("div");
      host.id = "studioOverlay";
      host.innerHTML =
        '<div class="st-ov-bar"><span class="st-ov-title"></span>' +
        '<button class="st-ov-close" type="button" aria-label="Close">✕</button></div>' +
        '<iframe class="st-ov-frame" title="PDF Studio"></iframe>';
      document.body.appendChild(host);
      host.querySelector(".st-ov-close").addEventListener("click", () => {
        closeStudioOverlay(true);
      });
    }
    host.querySelector(".st-ov-title").textContent = "PDF Studio · " + studioId;
    host.querySelector("iframe").src = studioShellUrl(studioId);
    // Refresh-safe: reopen this same studio after a reload (cleared on close).
    try {
      sessionStorage.setItem("x5:studio-open", studioId);
    } catch (_) {}
    // Restart the open animation on every open.
    host.classList.remove("open");
    void host.offsetWidth;
    host.classList.add("open");
    if (push !== false) {
      try {
        history.pushState({ studio: studioId }, "", "/studio-pdf/" + encodeURIComponent(studioId));
      } catch (_) {}
    }
  } catch (_) {}
}

// Verified open: a deleted studio never opens (no 404 face) — its dead
// cards are swept from the UI instead.
function openStudioVerified(sid, push) {
  try {
    if (!sid) return;
    studioInfo(sid).then((info) => {
      try {
        if (info && info.missing) {
          refreshStudioCards();
          return;
        }
        // Opened = seen: the green dot clears to grey behind the overlay.
        try {
          if (info && Number.isInteger(info.designed)) setStudioSeen(sid, info.designed);
        } catch (_) {}
        openStudioOverlay(sid, push);
        refreshStudioCards();
      } catch (_) {}
    });
  } catch (_) {}
}

function closeStudioOverlay(push) {
  try {
    const host = document.getElementById("studioOverlay");
    if (host && host.classList.contains("open")) {
      // Hide with animation, then detach the frame.
      host.classList.add("closing");
      setTimeout(() => {
        try {
          host.classList.remove("open");
          host.classList.remove("closing");
          const f = host.querySelector("iframe");
          if (f) f.src = "about:blank";
        } catch (_) {}
      }, 280);
    }
    if (push !== false) {
      try {
        history.pushState({ page: "agent" }, "", "/");
      } catch (_) {}
    }
    try {
      sessionStorage.removeItem("x5:studio-open");
    } catch (_) {}
    // Back in the chat: cards repaint with the latest page count.
    try {
      window.dispatchEvent(new CustomEvent("x5:studio-refresh-cards"));
    } catch (_) {}
  } catch (_) {}
}

async function studioInfo(studioId) {
  try {
    if (!/^[A-Za-z0-9_-]+$/.test(studioId || "")) return { title: studioId, total: 0, designed: 0 };
    const r = await fetch("/api/studio?studio_id=" + encodeURIComponent(studioId), {
      credentials: "same-origin",
    });
    if (r.status === 404) return { missing: true };
    const d = await r.json();
    if (d && d.meta) {
      const pages = Array.isArray(d.pages) ? d.pages : [];
      let designed = 0;
      for (const p of pages) {
        if (p && p.has_code) designed += 1;
      }
      let designing = 0;
      try {
        const raw = Array.isArray(d.designing) ? d.designing : [];
        designing = raw.map(Number).filter((n) => Number.isInteger(n) && n >= 1).length;
      } catch (_) {
        designing = 0;
      }
      return {
        title: String(d.meta.title || studioId).slice(0, 80),
        total: pages.length,
        designed,
        designing,
      };
    }
  } catch (_) {}
  return { title: studioId, total: 0, designed: 0, designing: 0 };
}

// Seen tracking: a card glows GREEN only while the studio holds designs the
// user has not opened yet. Opening a studio marks its designed count as seen
// (per-studio localStorage, refresh-safe); grey means all caught up.
function getStudioSeen(sid) {
  try {
    const v = Number(localStorage.getItem("x5:studio-seen:" + sid));
    return Number.isInteger(v) && v >= 0 ? v : 0;
  } catch (_) {
    return 0;
  }
}

function setStudioSeen(sid, n) {
  try {
    const v = Number(n);
    localStorage.setItem("x5:studio-seen:" + sid, String(Number.isInteger(v) && v > 0 ? v : 0));
  } catch (_) {}
}

// Chat auto-scroll: a fresh card must never leave the reply half-visible —
// scroll fully to the bottom, counting extra room for the card itself.
function scrollChatToBottom(smooth) {
  try {
    const sc = document.getElementById("chatScroll");
    if (!sc) return;
    const target = sc.scrollHeight + 240; // card height + breathing room
    try {
      sc.scrollTo({ top: target, behavior: smooth === false ? "auto" : "smooth" });
    } catch (_) {
      sc.scrollTop = target;
    }
  } catch (_) {}
}

// Single painter: title + status line + page/designed/pending counts.
// Reused on card build, on the 30s sweep, and when the overlay closes.
// Dots: orange = generating now, green = new unseen designs, grey = caught up.
function paintStudioCard(card, sid, info) {
  try {
    const b = card.querySelector(".st-row-title b");
    const status = card.querySelector(".st-row-status");
    const counts = card.querySelector(".st-row-counts");
    const dot = card.querySelector(".st-dot");
    if (b) {
      b.textContent = (info && info.title) || sid;
    }
    // A studio always owns at least its first page (default 1 on create).
    const total = (info && Number.isInteger(info.total) && info.total > 0) ? info.total : 1;
    const rawDesigned = (info && Number.isInteger(info.designed) && info.designed >= 0) ? info.designed : 0;
    const designed = Math.min(rawDesigned, total);
    const pending = Math.max(0, total - designed);
    const generating = (info && Number.isInteger(info.designing)) ? info.designing > 0 : false;
    const fresh = generating ? 0 : Math.max(0, designed - getStudioSeen(sid));
    if (status) {
      // The status line always shows the current state with its dot beside
      // it — never an "All caught up" line: a quiet studio reads Ready.
      const text = generating
        ? "Generating design…"
        : (fresh > 0
          ? `${fresh} new ${fresh === 1 ? "design" : "designs"} — tap to view`
          : "Ready");
      const label = status.querySelector("span:not(.st-dot)");
      if (label) label.textContent = text;
      else status.textContent = text;
    }
    if (counts) {
      const pg = total === 1 ? "1 page" : `${total} pages`;
      counts.textContent = `${pg} · ${designed} designed · ${pending} pending`;
    }
    if (dot) {
      dot.classList.toggle("is-running", generating);
      dot.classList.toggle("is-done", !generating && fresh > 0);
      dot.title = generating ? "Generating now" : (fresh > 0 ? "New designs" : "All caught up");
    }
    try {
      card.setAttribute("aria-label", `Open ${((info && info.title) || sid)} studio`);
    } catch (_) {}
  } catch (_) {}
}

function refreshStudioCards() {
  try {
    document.querySelectorAll(".st-row").forEach((card) => {
      const sid = (card.dataset && card.dataset.sid) || "";
      if (!sid) return;
      studioInfo(sid).then((info) => {
        try {
          if (info && info.missing) card.remove();
          else paintStudioCard(card, sid, info);
        } catch (_) {}
      });
    });
  } catch (_) {}
}

function buildStudioCard(sid) {
  // Gateway-style row card (thinner, tighter, more organized): glass tile
  // with a PDF mark + title row (status dot + name) + status line + counts
  // line + chevron. The whole card opens the studio (native button, so
  // keyboard + focus come free).
  const card = document.createElement("button");
  card.type = "button";
  card.className = "row-card st-row";
  card.setAttribute("aria-label", `Open ${sid} studio`);
  try {
    card.dataset.sid = sid;
  } catch (_) {}
  const tile = document.createElement("span");
  tile.className = "tile st-tile";
  tile.setAttribute("aria-hidden", "true");
  tile.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6M9 17h4"/></svg>';
  const body = document.createElement("span");
  body.className = "row-body";
  const title = document.createElement("span");
  title.className = "row-title st-row-title";
  const b = document.createElement("b");
  b.textContent = sid;
  b.setAttribute("dir", "auto");
  title.appendChild(b);
  const status = document.createElement("span");
  status.className = "row-sub st-row-status";
  const dot = document.createElement("span");
  dot.className = "st-dot";
  dot.setAttribute("aria-hidden", "true");
  const state = document.createElement("span");
  state.textContent = "Loading…";
  status.appendChild(dot);
  status.appendChild(state);
  const counts = document.createElement("span");
  counts.className = "row-desc st-row-counts";
  counts.textContent = "1 page · 0 designed · 1 pending";
  body.appendChild(title);
  body.appendChild(status);
  body.appendChild(counts);
  const arrow = document.createElement("span");
  arrow.setAttribute("aria-hidden", "true");
  arrow.innerHTML = '<svg class="row-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>';
  card.appendChild(tile);
  card.appendChild(body);
  card.appendChild(arrow.firstChild || arrow);
  card.addEventListener("click", () => openStudioVerified(sid, true));
  // Upgrade with the real title + ACTUAL page count when the API answers.
  // Deleted studio: hide the card automatically (dead link stays dead).
  studioInfo(sid).then((info) => {
    try {
      if (info && info.missing) {
        card.remove();
        return;
      }
      paintStudioCard(card, sid, info);
      // The card grew below the reply: settle fully to the bottom again.
      if (card.isConnected) scrollChatToBottom(true);
    } catch (_) {}
  });
  return card;
}

function decorateTextNode(t) {
  try {
    if (!t || !t.parentNode) return;
    if (!t.nodeValue || !t.nodeValue.includes("[STUDIO:")) return;
    const m = (t.nodeValue || "").match(/\[STUDIO:([A-Za-z0-9_-]+)\]/);
    if (!m) return;
    const card = buildStudioCard(m[1]);
    t.parentNode.insertBefore(card, t.nextSibling);
    t.nodeValue = (t.nodeValue || "").replace(m[0], " ");
    // The card lands at reply end with a rise-in animation: keep the chat
    // pinned fully to the bottom (with room for the card) as it appears.
    scrollChatToBottom(true);
    setTimeout(() => scrollChatToBottom(true), 500);
  } catch (_) {}
}

function decorateStudioCards(root) {
  try {
    const scope = root || document;
    const walker = document.createTreeWalker(scope, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) {
      const t = walker.currentNode;
      if (t.nodeValue && t.nodeValue.includes("[STUDIO:")) hits.push(t);
    }
    for (const t of hits) decorateTextNode(t);
  } catch (_) {}
}

export function initStudioPdf() {
  try {
    decorateStudioCards(document);
    // Opening the chat lands fully at the bottom (cards counted in): history
    // renders async, so settle twice — early and after late content lands.
    setTimeout(() => scrollChatToBottom(false), 400);
    setTimeout(() => scrollChatToBottom(false), 1500);
    const chat = document.querySelector(".chat") || document.body;
    const obs = new MutationObserver((mut) => {
      for (const m of mut) {
        // Replies type out char-by-char into one text node (no childList
        // fires), so watch characterData too — the card lands at reply end.
        if (m.type === "characterData") {
          decorateTextNode(m.target);
          continue;
        }
        for (const n of m.addedNodes || []) {
          if (n.nodeType === 1) decorateStudioCards(n);
          else if (n.nodeType === 3) decorateTextNode(n);
        }
      }
    });
    obs.observe(chat, { childList: true, characterData: true, subtree: true });
    window.addEventListener("x5:page-open", () => decorateStudioCards(document));
    // Session routing: Back out of a studio URL closes the overlay.
    window.addEventListener("popstate", () => {
      try {
        const sid = studioIdFromRoute();
        if (sid) openStudioVerified(sid, false);
        else closeStudioOverlay(false);
      } catch (_) {}
    });
    // Re-sweep rendered cards: refresh the ACTUAL page count + status, and
    // hide the card when its studio was deleted later.
    setInterval(refreshStudioCards, 30000);
    // Fast first sweep: history cards for deleted studios vanish at once.
    setTimeout(refreshStudioCards, 5000);
    // Returning from a studio: its card shows the fresh count at once.
    window.addEventListener("x5:studio-refresh-cards", refreshStudioCards);
    // Boot: reopen the studio the user was inside (route first, stored
    // session second) — a refresh never drops the session. Never open into
    // a deleted studio: verify first, sweep dead cards instead.
    try {
      let sid = studioIdFromRoute();
      if (!sid) {
        try {
          sid = sessionStorage.getItem("x5:studio-open") || null;
        } catch (_) {
          sid = null;
        }
      }
      if (sid) {
        const open = sid;
        const fromRoute = !!studioIdFromRoute();
        openStudioVerified(open, !fromRoute);
      }
    } catch (_) {}
    // The shell reports a deleted studio: close back to the chat.
    window.addEventListener("message", (e) => {
      try {
        if (e && e.data === "x5:studio-missing") closeStudioOverlay(true);
        // The shell needs the voice-to-text model template: route there
        // exactly like the main agent (AI Models + STT dialog).
        if (e && e.data === "x5:open-ai-models-stt") {
          closeStudioOverlay(false);
          import("./navigation.js?v=15").then((nav) => {
            try {
              nav.navigate("ai-models");
            } catch (_) {}
          }).catch(() => {
            try {
              location.href = "/ai-models";
            } catch (_) {}
          });
          try {
            window.dispatchEvent(new CustomEvent("x5:open-model", { detail: { kind: "stt" } }));
          } catch (_) {}
          try {
            if (typeof window.openAiModelDialog === "function") window.openAiModelDialog("stt");
          } catch (_) {}
        }
      } catch (_) {}
    });
  } catch (_) {}
}
