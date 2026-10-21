/**
 * navigation.js — sidebar drawer + X5Agent page routing
 * Owns: drawer open/close, sidebar collapse, real-URL page switching, toolbar titles.
 * Routing is real (History API: /agent, /models ...) — no hash. Server falls back
 * unknown extensionless paths to index.html so refresh/deep-link works.
 */
"use strict";

import { animateIn, hideChartTip } from "./ui.js?v=13";

const BP_MOBILE = 1024;
const COLLAPSE_KEY = "x5_sidebar";

const PAGES = ["dashboard", "agent", "folder", "gateway", "skills", "ai-models", "mcp", "subscription", "settings"];

function routeFromPath() {
  try {
    const seg = (location.pathname || "/").replace(/\/+$/, "").split("/").pop() || "";
    if (seg === "" || seg === "index.html") return "agent";
    if (PAGES.includes(seg)) return seg;
  } catch (_) {}
  return "agent";
}

function urlFor(name) {
  return name === "agent" ? "/" : "/" + name;
}

const TITLES = {
  dashboard: { title: "Dashboard", sub: "Overview of your agent activity" },
  agent: { title: "Agent", sub: "Chat with your AI assistant" },
  folder: { title: "Working Folder", sub: "Your agent's files and folders" },
  gateway: { title: "WhatsApp gateway", sub: "Connect and manage your WhatsApp agent" },
  skills: { title: "Skills", sub: "Reusable instructions for your agent" },
  "ai-models": { title: "AI Models", sub: "Connect any AI model" },
  mcp: { title: "MCP Servers", sub: "Connect remote tools over OAuth" },
  subscription: { title: "Subscription", sub: "Your plan, payment and renewal" },
  settings: { title: "Settings", sub: "Preferences and account" },
};

let sidebar = null;
let scrim = null;
let content = null;
let pageTitle = null;
let pageSub = null;
let currentPage = null;
// Entrance plays ONCE per page on first open, data swaps in silently
// later — revisits never replay the animation.
const seenPages = new Set();

function isMobile() {
  return window.innerWidth <= BP_MOBILE;
}

export function closeDrawer() {
  sidebar?.classList.remove("open");
  scrim?.classList.remove("open");
  if (isMobile()) document.body.style.overflow = "";
}

export function openDrawer() {
  if (!isMobile()) {
    setCollapsed(false);
    return;
  }
  sidebar?.classList.add("open");
  scrim?.classList.add("open");
}

export function navigate(name, opts) {
  if (!TITLES[name]) return;
  const push = !opts || opts.push !== false;
  if (currentPage === name) {
    // Keep the URL canonical even when re-selecting the same page.
    try {
      history.replaceState(null, "", urlFor(name));
    } catch (_) {}
    closeDrawer();
    return;
  }
  currentPage = name;

  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.nav === name);
  });
  document.querySelectorAll(".page").forEach((p) => {
    p.classList.toggle("active", p.dataset.page === name);
  });

  if (pageTitle) pageTitle.textContent = TITLES[name].title;
  if (pageSub) pageSub.textContent = TITLES[name].sub;

  content?.classList.toggle("no-scroll", name === "agent");
  if (content) content.scrollTop = 0;

  const active = document.querySelector(`.page[data-page="${name}"]`);
  // Agent page animates as one block (chat + composer) on first open only.
  if (active && name !== "agent" && !seenPages.has(name)) {
    seenPages.add(name);
    animateIn(active);
  }
  if (name === "agent") {
    if (!seenPages.has(name)) {
      seenPages.add(name);
      const chat = active?.querySelector(".chat");
      if (chat) {
        chat.classList.remove("chat-enter");
        void chat.offsetWidth;
        chat.classList.add("chat-enter");
      }
    }
    requestAnimationFrame(() => {
      window.dispatchEvent(new CustomEvent("x5:agent-open"));
    });
  }
  window.dispatchEvent(new CustomEvent("x5:page-open", { detail: name }));
  try {
    // A studio session owns its URL (/studio-pdf/<id>): never rewrite it
    // underneath the overlay, or the next refresh loses the studio.
    const m = (location.pathname || "").match(/^\/studio-pdf\/([A-Za-z0-9_-]+)/);
    if (m && m[1] !== "shell" && m[1] !== "assets") return void closeDrawer();
    if (push) history.pushState({ page: name }, "", urlFor(name));
    else history.replaceState({ page: name }, "", urlFor(name));
  } catch (_) {}
  closeDrawer();
}

export function isCollapsed() {
  try {
    return document.body.classList.contains("sidebar-collapsed");
  } catch (_) {
    return false;
  }
}

export function setCollapsed(collapsed) {
  try {
    document.body.classList.toggle("sidebar-collapsed", !!collapsed);
    try {
      localStorage.setItem(COLLAPSE_KEY, collapsed ? "collapsed" : "expanded");
    } catch (_) {}
  } catch (_) {}
}

export function initNavigation() {
  sidebar = document.getElementById("sidebar");
  scrim = document.getElementById("scrim");
  content = document.getElementById("content");
  pageTitle = document.getElementById("pageTitle");
  pageSub = document.getElementById("pageSub");

  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => navigate(btn.dataset.nav));
    // Tooltip label when the sidebar is collapsed to icons-only.
    try {
      const label = (btn.textContent || "").trim();
      if (label && !btn.getAttribute("title")) btn.setAttribute("title", label);
    } catch (_) {}
  });

  document.getElementById("menuBtn")?.addEventListener("click", () => {
    if (isMobile()) openDrawer();
    else setCollapsed(false);
  });
  // Clicking the brand logo zooms the template: toggles expand/collapse.
  document.querySelector(".brand")?.addEventListener("click", () => {
    if (!isMobile()) setCollapsed(!isCollapsed());
  });
  document.getElementById("collapseBtn")?.addEventListener("click", () => {
    setCollapsed(!isCollapsed());
  });
  // Collapsed rail is the default (desktop only; drawer mode ignores it via CSS).
  // Only an explicit "expanded" choice opens the full sidebar.
  try {
    if (localStorage.getItem(COLLAPSE_KEY) !== "expanded") setCollapsed(true);
  } catch (_) {
    setCollapsed(true);
  }
  scrim?.addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeDrawer();
  });

  // Swipe gestures for the drawer (phone only): drag in from the sidebar
  // edge to open, drag back toward the edge to close. RTL-aware:
  // the sidebar lives at inline-start (right edge in RTL, left in LTR).
  let touchX = null;
  let touchY = null;
  document.addEventListener("touchstart", (e) => {
    if (!isMobile()) return;
    try {
      const t = e.touches && e.touches[0];
      if (!t) return;
      touchX = t.clientX;
      touchY = t.clientY;
    } catch (_) {
      touchX = null;
    }
  }, { passive: true });
  document.addEventListener("touchend", (e) => {
    if (!isMobile() || touchX === null) return;
    try {
      const t = e.changedTouches && e.changedTouches[0];
      if (!t) return;
      const dx = t.clientX - touchX;
      const dy = t.clientY - (touchY || 0);
      if (Math.abs(dx) < 60 || Math.abs(dx) < Math.abs(dy) * 2) return;
      const rtl = document.documentElement.getAttribute("dir") === "rtl";
      const atEdge = rtl ? touchX > window.innerWidth - 28 : touchX < 28;
      const open = !!sidebar && sidebar.classList.contains("open");
      if (!open && atEdge && ((rtl && dx < 0) || (!rtl && dx > 0))) openDrawer();
      else if (open && ((rtl && dx > 0) || (!rtl && dx < 0))) closeDrawer();
    } catch (_) {}
    touchX = null;
    touchY = null;
  }, { passive: true });

  let resizeTimer = null;
  window.addEventListener("resize", () => {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => {
      if (!isMobile()) closeDrawer();
      hideChartTip();
    }, 90);
  });

  content?.addEventListener("scroll", hideChartTip, { passive: true });

  // Browser back/forward buttons follow real history.
  window.addEventListener("popstate", () => {
    navigate(routeFromPath(), { push: false });
  });

  // Deep-link: refresh keeps the user in the same section (real URL, no hash).
  navigate(routeFromPath(), { push: false });
}