/**
 * schedule.js — right-edge scheduler panel (same template as the browser
 * floating preview: #browserTab rail + #liveMini free window).
 *
 * Single responsibility: show scheduled jobs pushed over SSE (`scheduler`
 * frames) in a right-edge tab + floating list. Each job rectangle shows the
 * task name, the fixed clock icon, a live countdown to next_run and the
 * creation time; a click opens the full detail dialog (task name + skill
 * "Task Instructions" view/edit with pencil + countdown + creation date + run
 * rectangles #RUN<n>, each opening its own full agent-like session).
 * Agent section only (body.sched-visible). No polling anywhere: SSE push +
 * one local 1s tick for the countdown text only.
 */
"use strict";

import { onPush } from "./events.js?v=7";
import { openModal, closeModal, toast } from "./ui.js?v=13";

let jobs = []; // summarized jobs from the server (executing flag included)
let miniOpen = false;
let tabY = null;
let tickBound = false;

function nowMs() {
  return Date.now();
}

function parseRunMs(iso) {
  if (!iso) return null;
  const t = Date.parse(iso);
  return Number.isFinite(t) ? t : null;
}

function lastRun(job) {
  const h = Array.isArray(job.run_history) ? job.run_history : [];
  return h.length ? h[h.length - 1] : null;
}

function statusOf(job) {
  if (job.executing) return "running";
  const lr = lastRun(job);
  if (!lr) return job.status === "paused" ? "paused" : "waiting";
  return lr.success ? "ok" : "fail";
}

/** Soonest-first ordering: running jobs on top, then the earliest next_run.
 * Paused jobs sort last (their countdown is stopped — they run nothing). */
function sortedJobs() {
  const arr = jobs.slice();
  const effNext = (j) => (j.status === "paused" ? null : parseRunMs(j.next_run));
  arr.sort((a, b) => {
    const ae = a.executing ? 1 : 0;
    const be = b.executing ? 1 : 0;
    if (ae !== be) return be - ae;
    const an = effNext(a);
    const bn = effNext(b);
    if (an !== null && bn !== null) return an - bn;
    if (an !== null) return -1;
    if (bn !== null) return 1;
    return String(a.task).localeCompare(String(b.task));
  });
  return arr;
}

function countdownText(job) {
  if (job.executing) return "Running…";
  // Paused stops the countdown no matter what next_run still holds.
  if (!job.executing && job.status === "paused") return "Paused";
  const ms = parseRunMs(job.next_run);
  if (ms === null) {
    const lr = lastRun(job);
    if (lr) return lr.success ? "Done" : "Failed";
    return job.status === "paused" ? "Paused" : "Completed";
  }
  let s = Math.max(0, Math.floor((ms - nowMs()) / 1000));
  if (s <= 0) return "Due now…";
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (d > 0) return `in ${d}d ${h}h`;
  if (h > 0) return `in ${h}h ${m}m`;
  if (m > 0) return `in ${m}m ${sec}s`;
  return `in ${sec}s`;
}

function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}

function fmtWhen(iso) {
  // Natural English date with AM/PM: "Oct 12, 2026, 12:03 PM".
  // Raw fallback untouched for unparseable input.
  if (!iso) return "—";
  try {
    const t = new Date(iso);
    if (Number.isNaN(t.getTime())) return String(iso).slice(0, 40);
    return t.toLocaleString("en-US", {
      month: "short", day: "numeric", year: "numeric",
      hour: "numeric", minute: "2-digit", hour12: true,
    });
  } catch (_) {
    return String(iso).slice(0, 40);
  }
}

/* ---------- right-edge tab (same rail template as #browserTab) ---------- */

function placeSchedTab(y) {
  // Fixed-right rail like the browser tab: only the vertical position moves.
  const tab = document.getElementById("scheduleTab");
  if (!tab) return;
  const h = tab.offsetHeight || 150;
  const top = typeof y === "number" ? y : 0;
  const cy = Math.min(Math.max(60, top), Math.max(60, window.innerHeight - h - 8));
  tabY = cy;
  tab.style.left = "auto";
  tab.style.right = "0";
  tab.style.bottom = "auto";
  tab.style.top = `${cy}px`;
}

function enforceTabSeparation() {
  // The two right-edge tabs must never slide under each other — no overlap,
  // ever. Only the scheduler tab moves (the browser template is never
  // touched): below the browser tab first, above it when there is no room
  // below, top edge as the last resort. Skipped mid-drag so it never fights
  // the user's hand (resolved on release + every 1s tick).
  try {
    const tab = document.getElementById("scheduleTab");
    const bt = document.getElementById("browserTab");
    if (!tab || tab.classList.contains("dragging")) return;
    if (!bt || bt.classList.contains("off")) return;
    const r = tab.getBoundingClientRect();
    const b = bt.getBoundingClientRect();
    if (r.left >= b.right || b.left >= r.right) return;
    if (r.top >= b.bottom || b.top >= r.bottom) return;
    const h = r.height || tab.offsetHeight || 150;
    const maxTop = Math.max(60, window.innerHeight - h - 8);
    let ny = b.bottom + 12;
    if (ny > maxTop) ny = b.top - h - 12;
    if (ny < 60 || ny > maxTop) ny = 60;
    placeSchedTab(ny);
  } catch (_) {}
}

function bindTabClampOnce() {
  if (bindTabClampOnce.done) return;
  bindTabClampOnce.done = true;
  window.addEventListener("resize", () => {
    try {
      if (tabY !== null) placeSchedTab(tabY);
      enforceTabSeparation();
    } catch (_) {}
  });
}

let schedPull = null; // active horizontal pull-out gesture {sx, sy, fresh}
let schedPullSX = 0; // pull gesture start X (module-level: shared by tab handlers)
let schedSeq = 0; // lifecycle generation: every open/dock/pull bumps it so a stale timer from rapid open/close can never kill a newer state

function schedGuard() {
  // Visibility watchdog: after every transition settles, one of the two must
  // be usable — the open window (miniOpen) or the rail tab (parked). If rapid
  // open/close left both gone/stuck, rebuild the correct one. Converges: a
  // fix schedules no further work beyond one quiet re-check.
  try {
    if (!jobs.length) return;
    const tab = document.getElementById("scheduleTab");
    const mini = document.getElementById("scheduleMini");
    const miniShown = !!(mini && !mini.classList.contains("mini-hidden"));
    if (miniOpen) {
      if (!miniShown) {
        miniOpen = false;
        try { setSchedTabOff(false); } catch (_) {}
        openMini(); // reopens at a safe spot (bumps the generation)
      }
    } else {
      if (tab) {
        tab.classList.remove("pulling", "dragging");
        tab.style.opacity = "";
        tab.style.transform = "";
        try { setSchedTabOff(false); } catch (_) {}
      } else {
        try { ensureTab(); } catch (_) {} // recreates the tab if removed
      }
      if (mini && !mini.classList.contains("mini-hidden")) {
        try { schedDockToTab(mini.getBoundingClientRect().top); } catch (_) {}
      }
    }
  } catch (_) {}
}

function schedGuardSoon() {
  // Re-check after transitions settle (850ms > longest 720ms flight).
  // Generation-captured: a newer lifecycle invalidates the pending check so
  // the guard never yanks a fresh pull mid-gesture.
  try { const g = schedSeq; setTimeout(() => { if (g === schedSeq) schedGuard(); }, 850); } catch (_) {}
}

function schedKillAnims(mini) {
  // A superseded suck/bloom (fill:forwards) would otherwise pin the window
  // invisible under the new state — always kill before reusing the node.
  try {
    const anims = mini && mini.getAnimations ? mini.getAnimations() : [];
    anims.forEach((a) => { try { a.cancel(); } catch (_) {} });
  } catch (_) {}
}

function schedTabReset() {
  // The tab is hidden ONLY via .off — never leave a stale inline push/fade
  // that would keep it half-gone while parked.
  try {
    const tab = document.getElementById("scheduleTab");
    if (!tab) return;
    tab.classList.remove("pulling", "dragging");
    tab.style.transform = "";
    tab.style.opacity = "";
  } catch (_) {}
}

function initSchedTabDrag(tab) {
  // Right-rail gestures (schedule ONLY, the browser tab is never touched):
  // tap opens, vertical drag slides the tab up/down, horizontal pull-left
  // draws the window out of the right edge like a drawer and releasing it
  // opens the window where it was pulled. Hover never moves the tab (CSS
  // hover only tints). Touch gets a wider tap threshold (finger jitter).
  let sy = 0;
  let oy = 0;
  let moved = false;
  let dragging = false;
  let tapLimit = 6;
  tab.style.touchAction = "none";
  tab.addEventListener("pointerdown", (e) => {
    dragging = true;
    moved = false;
    schedPull = null;
    tapLimit = e.pointerType === "touch" ? 14 : 6;
    schedPullSX = e.clientX;
    sy = e.clientY;
    oy = tabY !== null ? tabY : tab.getBoundingClientRect().top;
    tab.classList.add("dragging");
    try {
      tab.setPointerCapture?.(e.pointerId);
    } catch (_) {}
    e.preventDefault();
  });
  tab.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - schedPullSX;
    const dy = e.clientY - sy;
    // Pull threshold follows the pointer: touch jitters, so it needs the full
    // run-up; a mouse engages almost immediately (no tap risk: a tap never
    // travels 12px purely horizontal). Past 60px left always wins.
    const need = e.pointerType === "touch" ? 24 : 12;
    if (!schedPull && dx < -need && (Math.abs(dx) > Math.abs(dy) * 1.15 + 8 || dx < -60)) {
      // Pull-out takes over: freeze vertical sliding, start the drawer
      // preview emerging from the right edge at the pointer height.
      schedPull = { sx: schedPullSX, sy, fresh: !document.getElementById("scheduleMini") };
      tab.classList.add("pulling");
      schedPullPreview(e.clientX, e.clientY);
    }
    if (schedPull) {
      // Cache coords, paint once per frame (never style-thrash per event).
      schedPull.lx = e.clientX;
      schedPull.ly = e.clientY;
      schedPullSchedule();
    } else {
      if (Math.abs(dy) > tapLimit || Math.abs(dx) > tapLimit) moved = true;
      if (moved) placeSchedTab(oy + e.clientY - sy);
    }
    e.preventDefault();
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    tab.classList.remove("dragging");
    const isUp = !e || e.type === "pointerup";
    if (schedPull) {
      // Cancel never opens (interrupted gesture just slides back).
      const pull = schedPull;
      schedPull = null;
      tab.classList.remove("pulling");
      const far = isUp && e && typeof e.clientX === "number" ? pull.sx - e.clientX : 0;
      const px = isUp && e && typeof e.clientX === "number" ? e.clientX : pull.sx;
      const py = isUp && e && typeof e.clientY === "number" ? e.clientY : pull.sy;
      // Magnetic catch: a partial pull (~48px, a third of the bloom run) is
      // enough — no need to drag to the middle, release grabs it open.
      if (far > 48) schedPullCommit(px, py, far);
      else schedPullCancel(pull);
      return;
    }
    if (isUp && !moved) {
      const px = e && typeof e.clientX === "number" ? e.clientX : undefined;
      const py = e && typeof e.clientY === "number" ? e.clientY : undefined;
      openMini(px, py);
    } else if (moved) {
      enforceTabSeparation();
    }
  };
  tab.addEventListener("pointerup", end);
  tab.addEventListener("pointercancel", end);
}

function setSchedTabOff(off) {
  // Same as #browserTab.off: while the floating window is open the tab
  // hides (a tap can only open — never close, exactly like the browser).
  try {
    document.getElementById("scheduleTab")?.classList.toggle("off", !!off);
  } catch (_) {}
}

function schedClampPos(l, t, w, h) {
  // Strict inside-viewport clamp: the floating window never leaves the frame.
  // Falls back to the top-left safe corner when the viewport is tiny.
  try {
    const vw = window.innerWidth || 320;
    const vh = window.innerHeight || 480;
    const ww = Math.min(w || 272, Math.max(200, vw - 16));
    const hh = Math.min(h || 200, Math.max(140, vh - 78));
    const maxL = Math.max(8, vw - ww - 8);
    const maxT = Math.max(70, vh - hh - 8);
    return [
      Math.min(Math.max(8, l), maxL),
      Math.min(Math.max(70, t), maxT),
    ];
  } catch (_) {
    return [8, 70];
  }
}

function schedReducedMotion() {
  try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  catch (_) { return false; }
}

function schedTabDelta(mini) {
  // Vector from the window's right-center to the collapsed tab's center —
  // the black-hole axis (dock/suck-back) and its reverse (bloom open).
  // Measured while the tab is still visible. Falls back to a plain
  // rightward nudge when the tab is gone.
  try {
    const r = mini.getBoundingClientRect();
    const tab = document.getElementById("scheduleTab");
    if (!tab) return { dx: Math.max(120, Math.round(r.width * 0.6)), dy: 0 };
    const t = tab.getBoundingClientRect();
    return {
      dx: Math.round((t.left + t.width / 2) - r.right + 4),
      dy: Math.round((t.top + t.height / 2) - (r.top + r.height / 2)),
    };
  } catch (_) { return { dx: 160, dy: 0 }; }
}

function schedBloomFromTab(mini, delta, ms) {
  // Reverse black hole: the window blooms OUT of the tab point with the
  // same expo-out curve as the suck-in (~520ms). The tab must be visible
  // when `delta` is measured; it hides at bloom time (caller order).
  // .sched-anim stays ON afterwards forever: removing it would let the base
  // x5-rise replay and blink the window (same-task restart only).
  try {
    if (!mini || typeof mini.animate !== "function" || schedReducedMotion()) return;
    const d = delta || schedTabDelta(mini);
    const dur = ms || 520;
    mini.classList.remove("sched-anim");
    void mini.offsetWidth;
    mini.classList.add("sched-anim");
    try { mini.dataset.schedDrag = "1"; } catch (_) {} // 1s tick never yanks mid-bloom
    mini.style.transformOrigin = "100% 50%";
    const g = schedSeq; // stale bloom must not drop a newer pull's guard flag
    mini.animate(
      [
        { transform: `translateX(${d.dx}px) translateY(${d.dy}px) scale(0.05)`, opacity: 0, offset: 0 },
        { transform: `translateX(${Math.round(d.dx * 0.52)}px) translateY(${Math.round(d.dy * 0.52)}px) scale(0.55)`, opacity: 0.92, offset: 0.45 },
        { transform: "translateX(0px) scale(1)", opacity: 1, offset: 1 },
      ],
      { duration: dur, easing: "cubic-bezier(0.32, 0.72, 0, 1)" }
    );
    setTimeout(() => { if (g === schedSeq) { try { delete mini.dataset.schedDrag; } catch (_) {} } }, dur + 30);
  } catch (_) {}
}

function schedSettleFrom(mini, fromLeft, fromTop, fromScale, fromO, ms) {
  // FLIP settle after a pull release: continues EXACTLY from the pull's
  // visual state (same scale + opacity the finger left) and glides+grows
  // into the parked spot — zero jump in place, zero pop.
  try {
    if (!mini || typeof mini.animate !== "function" || schedReducedMotion()) return;
    const r = mini.getBoundingClientRect();
    const ddx = Math.round(fromLeft - r.left);
    const ddy = Math.round(fromTop - r.top);
    const s = Number(fromScale);
    const o = Number(fromO);
    if (!ddx && !ddy && !(Number.isFinite(s) && s < 0.999)) return;
    const dur = ms || 280;
    mini.classList.remove("sched-anim");
    void mini.offsetWidth;
    mini.classList.add("sched-anim");
    try { mini.dataset.schedDrag = "1"; } catch (_) {}
    mini.style.transformOrigin = "100% 50%"; // same origin as the pull bloom
    const g2 = schedSeq;
    mini.animate(
      [
        { transform: `translateX(${ddx}px) translateY(${ddy}px) scale(${Number.isFinite(s) ? s : 1})`, opacity: Number.isFinite(o) ? o : 1, offset: 0 },
        { transform: "translateX(0px) translateY(0px) scale(1)", opacity: 1, offset: 1 },
      ],
      { duration: dur, easing: "cubic-bezier(0.22, 1, 0.36, 1)" }
    );
    setTimeout(() => {
      if (g2 !== schedSeq) return;
      try { delete mini.dataset.schedDrag; } catch (_) {}
      try { mini.style.transformOrigin = ""; } catch (_) {}
    }, dur + 30);
  } catch (_) {}
}

function schedPullTop(y, h) {
  // Vertical-only clamp while pulling: the window may hang past the right
  // edge mid-gesture (it is coming out from behind it) — strictly parked
  // only on release.
  try {
    const vh = window.innerHeight || 480;
    return Math.min(Math.max(70, y), Math.max(70, vh - (h || 220) - 8));
  } catch (_) { return 70; }
}

function schedPullProgress(pulled) {
  // One continuous birth curve for the whole pull (preview → move → commit
  // settle): starts a ghost at the tab point, full-size past ~140px.
  const p = Math.min(1, Math.max(0, pulled) / 140);
  const e = 1 - (1 - p) * (1 - p);
  return { p, s: 0.05 + 0.95 * e, o: 0.05 + 0.95 * p };
}

function schedPullPreview(px, pointerY) {
  // The window is born at the tab and comes out WITH the finger: its center
  // tracks the pointer while it grows out of the tab point (scale + opacity
  // over the first ~140px). Same task as the build — no paint, no flash.
  ++schedSeq; // invalidate any pending dock/cancel finish
  let mini = document.getElementById("scheduleMini");
  schedKillAnims(mini); // a mid-flight suck must not pin the reborn window
  if (!mini) {
    mini = schedBuildMini();
    if (!mini) { schedPull = null; return; } // build failed — stay a tab tap
  }
  try {
    mini.classList.remove("docking", "mini-hidden", "sched-emerge", "sched-anim");
    delete mini.dataset.docking;
    mini.classList.add("sched-pulling");
    try { mini.dataset.schedDrag = "1"; } catch (_) {} // 1s tick never fights the finger
    // The tab itself stays put until the first move drives it out (below).
    mini.style.visibility = "";
    const w = mini.offsetWidth || 272;
    const h = mini.offsetHeight || 220;
    // Anchor geometry parked ONCE — every later frame moves via transform
    // only (compositor), left/top are never touched again mid-pull.
    schedPull.w = w;
    schedPull.h = h;
    schedPull.left0 = schedPull.sx - w / 2;
    schedPull.top0 = schedPullTop(pointerY - 90, h);
    schedPull.lx = px;
    schedPull.ly = pointerY;
    schedPull.raf = 0;
    mini.style.left = `${schedPull.left0}px`;
    mini.style.top = `${schedPull.top0}px`;
    mini.style.right = "auto";
    mini.style.transformOrigin = "100% 50%";
    schedPullApply(); // first frame, same task — no flash
    renderMini();
  } catch (_) {}
}

function schedPullApply() {
  // ONE compositor-cheap frame for the whole pull: position via transform
  // only (left/top stay parked from preview) + opacity + tab slide-out. No
  // layout reads, no layout writes — mid-pull jank is impossible.
  const pull = schedPull;
  const mini = pull && document.getElementById("scheduleMini");
  if (!pull || !mini || typeof pull.left0 !== "number") return;
  try {
    const g = schedPullProgress(pull.sx - pull.lx);
    const tx = Math.round(pull.lx - pull.sx);
    const ty = Math.round(schedPullTop(pull.ly - 90, pull.h || 220) - pull.top0);
    mini.style.transform = `translate(${tx}px, ${ty}px) scale(${g.s.toFixed(3)})`;
    mini.style.opacity = String(g.o);
    // The collapsed tab slides OUT to the right as its window leaves it —
    // pure outward motion driven by the finger (fully out past ~80%), never
    // a blink: the base transform transition glides every step.
    try {
      const tab = document.getElementById("scheduleTab");
      if (tab) tab.style.transform = `translateX(${(g.p * 130).toFixed(1)}%)`;
    } catch (_) {}
  } catch (_) {}
}

function schedPullSchedule() {
  // Coalesce bursts of pointermove into one rAF frame (many moves per paint
  // would otherwise stack style recalcs and stutter).
  if (!schedPull || schedPull.raf) return;
  try {
    schedPull.raf = requestAnimationFrame(() => {
      try { if (schedPull) schedPull.raf = 0; } catch (_) {}
      schedPullApply();
    });
  } catch (_) { schedPullApply(); }
}

function schedPullCommit(px, py, far) {
  // Released far enough: park strictly inside (out from under the browser
  // template) continuing the pull's own scale/opacity (no pop), then a
  // normal floating window, draggable by its head right away.
  ++schedSeq; // this commit owns the window now; stale finishes stay dead
  const mini = document.getElementById("scheduleMini");
  let from = null;
  try {
    if (mini) {
      schedKillAnims(mini); // measure the real rect, not a stale suck's ghost
      const r = mini.getBoundingClientRect();
      from = { left: r.left, top: r.top };
    }
  } catch (_) { from = null; }
  try {
    if (mini) {
      const w = mini.offsetWidth || 272;
      const h = mini.offsetHeight || 220;
      let [left, top] = schedClampPos(px - w / 2, py - 90, w, h);
      [left, top] = avoidBrowserOverlap(left, top, w, h);
      mini.style.left = `${left}px`;
      mini.style.top = `${top}px`;
      mini.style.right = "auto";
      mini.style.transform = "";
      mini.style.opacity = "";
      mini.classList.remove("sched-pulling");
      try { delete mini.dataset.schedDrag; } catch (_) {}
      renderMini();
      try { enforceMiniSeparation(); } catch (_) {}
      // Continue the pull's own scale/opacity into the settle (origin stays
      // right-center until the settle releases it) — no pop, no jump.
      const cg = schedPullProgress(Number(far) || 0);
      if (from) schedSettleFrom(mini, from.left, from.top, cg.s, cg.o);
    }
  } catch (_) {}
  miniOpen = true;
  try {
    const tab = document.getElementById("scheduleTab");
    if (tab) {
      // Release: the tab keeps flying out to the right and fades (smooth
      // continuation of the pull, no jump) while .off kills pointer events.
      tab.style.transform = "translateX(130%)";
      tab.style.opacity = "0";
    }
  } catch (_) {}
  setSchedTabOff(true);
  schedGuardSoon();
}

function schedPullCancel(pull) {
  // Short pull: sucked back into the tab (fast mirror of the dock suck),
  // then parked — the collapsed tab never moved.
  ++schedSeq; // this cancel owns the outcome; a newer pull/open overrules it
  const mySeq = schedSeq;
  const mini = document.getElementById("scheduleMini");
  const hide = () => {
    if (mySeq !== schedSeq) return; // superseded — leave the newer state alone
    try {
      const m = document.getElementById("scheduleMini");
      if (m && pull && pull.fresh) m.remove();
      else if (m) {
        const anims = m.getAnimations ? m.getAnimations() : [];
        anims.forEach((a) => { try { a.cancel(); } catch (_) {} });
        try { delete m.dataset.schedDrag; } catch (_) {}
        m.classList.remove("sched-pulling", "docking", "sched-anim", "sched-emerge");
        m.style.transformOrigin = "";
        m.style.transform = "";
        m.style.opacity = "";
        m.classList.add("mini-hidden");
      }
    } catch (_) {}
    miniOpen = false;
    try {
      const tab = document.getElementById("scheduleTab");
      if (tab) {
        // Glide back IN from the right: clear the inline push in the same
        // task as .off removal → one smooth slide-in + fade-in.
        tab.style.transform = "";
        tab.style.opacity = "";
      }
    } catch (_) {}
    setSchedTabOff(false);
    schedGuardSoon(); // watchdog: the rail tab must be back and alive
  };
  try {
    if (!mini) { hide(); return; }
    if (!schedReducedMotion() && typeof mini.animate === "function") {
      const fromT = mini.style.transform && mini.style.transform !== "none"
        ? mini.style.transform : "scale(1)";
      const fromO = Number(mini.style.opacity);
      const d = schedTabDelta(mini);
      mini.classList.remove("sched-pulling");
      mini.classList.add("docking");
      mini.style.transformOrigin = "100% 50%";
      mini.style.transform = "";
      mini.style.opacity = "";
      mini.animate(
        [
          { transform: fromT, opacity: Number.isFinite(fromO) ? fromO : 1, offset: 0 },
          { transform: `translateX(${d.dx}px) translateY(${d.dy}px) scale(0.05)`, opacity: 0, offset: 1 },
        ],
        { duration: 320, easing: "cubic-bezier(0.32, 0.72, 0, 1)", fill: "forwards" }
      );
      setTimeout(hide, 340);
      return;
    }
  } catch (_) {}
  hide();
}

function avoidBrowserOverlap(left, top, w, h) {
  // Open in the free area: candidates are tried in order (requested spot,
  // below, above, left, corners) and the first non-overlapping one wins, so
  // the window never opens under the browser template. Last resort parks
  // top-left — still outside the right-edge browser stack.
  try {
    const blocks = [];
    const live = document.getElementById("liveMini");
    if (live && !live.classList.contains("mini-hidden")) blocks.push(live.getBoundingClientRect());
    const bt = document.getElementById("browserTab");
    if (bt && !bt.classList.contains("off")) blocks.push(bt.getBoundingClientRect());
    if (!blocks.length) return schedClampPos(left, top, w, h);
    const clamp = (l, t) => schedClampPos(l, t, w, h);
    const hits = (l, t) => blocks.some((r) => l < r.right && l + w > r.left && t < r.bottom && t + h > r.top);
    const bottom = Math.max(...blocks.map((r) => r.bottom));
    const topMost = Math.min(...blocks.map((r) => r.top));
    const leftEdge = Math.min(...blocks.map((r) => r.left));
    const maxTop = Math.max(70, window.innerHeight - h - 8);
    const cands = [
      [left, top],
      [left, bottom + 12],
      [left, topMost - h - 12],
      [leftEdge - w - 12, top],
      [8, 70],
      [8, maxTop],
    ];
    for (const [cl, ct] of cands) {
      const [fl, ft] = clamp(cl, ct);
      if (!hits(fl, ft)) return [fl, ft];
    }
    return [8, 70];
  } catch (_) {
    return [left, top];
  }
}

function ensureTab() {
  // Visible while at least one job exists, hidden when none.
  let tab = document.getElementById("scheduleTab");
  if (!jobs.length) {
    if (tab) tab.remove();
    const mini = document.getElementById("scheduleMini");
    if (mini) mini.remove();
    miniOpen = false;
    return;
  }
  if (!tab) {
    tab = document.createElement("div");
    tab.id = "scheduleTab";
    tab.setAttribute("role", "button");
    tab.setAttribute("tabindex", "0");
    tab.setAttribute("aria-label", "Open scheduled tasks");
    tab.innerHTML = `
      <span class="tab-count" id="schedTabCount"></span>
      <span class="tab-label">Scheduled Tasks</span>`;
    tab.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openMini();
      }
    });
    document.body.appendChild(tab);
    // Right side is shared with the browser tab: default below it so the
    // two tabs never cover each other (both stay freely draggable).
    if (tabY !== null) placeSchedTab(tabY);
    else {
      let defY = Math.floor(window.innerHeight * 0.62);
      try {
        const bt = document.getElementById("browserTab");
        if (bt) {
          const r = bt.getBoundingClientRect();
          defY = Math.min(window.innerHeight - 170, r.bottom + 12);
        }
      } catch (_) {}
      placeSchedTab(defY);
    }
    initSchedTabDrag(tab);
    bindTabClampOnce();
  }
  const badge = tab.querySelector("#schedTabCount");
  if (badge) badge.textContent = String(jobs.length);
  const running = jobs.some((j) => j.executing);
  tab.classList.toggle("has-running", running);
  enforceTabSeparation();
}

/* ---------- floating list (mirrors #liveMini, pinned left) ---------- */

function cardClass(job) {
  const st = statusOf(job);
  if (st === "running") return "is-running";
  if (st === "ok") return "is-ok";
  if (st === "fail") return "is-fail";
  return "";
}

function renderMini() {
  const mini = document.getElementById("scheduleMini");
  if (!mini) return;
  const list = mini.querySelector(".sched-list");
  if (!list) return;
  const arr = sortedJobs();
  if (!arr.length) {
    list.innerHTML = `<div class="sched-empty">No scheduled tasks</div>`;
  } else {
    list.innerHTML = "";
    for (const job of arr) {
      const card = document.createElement("div");
      card.className = `sched-card ${cardClass(job)}`;
      card.dataset.job = job.job_id;
      const icon = document.createElement("span");
      icon.className = "sched-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9" /><polyline points="12 7 12 12 15.5 14" /></svg>`;
      const mid = document.createElement("div");
      mid.className = "sched-mid";
      const name = document.createElement("div");
      name.className = "sched-name";
      name.dir = "auto";
      name.textContent = job.task || job.job_id;
      const row = document.createElement("div");
      row.className = "sched-row";
      const cd = document.createElement("div");
      cd.className = "sched-count";
      cd.dataset.cd = job.job_id;
      cd.textContent = countdownText(job);
      const created = document.createElement("div");
      created.className = "sched-created";
      created.textContent = fmtWhen(job.created_at);
      created.dir = "ltr";
      row.appendChild(cd);
      row.appendChild(created);
      mid.appendChild(name);
      mid.appendChild(row);
      // Whole rectangle opens the detail dialog (same as tapping a preview).
      card.setAttribute("role", "button");
      card.setAttribute("tabindex", "0");
      card.addEventListener("click", () => openDetail(job.job_id));
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openDetail(job.job_id);
        }
      });
      card.appendChild(icon);
      card.appendChild(mid);
      list.appendChild(card);
    }
  }
  const title = mini.querySelector(".sched-mini-count");
  if (title) title.textContent = `${arr.length} task${arr.length === 1 ? "" : "s"}`;
}

function enforceMiniSeparation() {
  // The two floating windows must never slide under each other — no overlap,
  // ever. Only the scheduler window moves (the browser player is never
  // touched): below first, then above, then left, then screen corners. The
  // last resort parks top-left. Skipped mid-drag; the 1s tick + drag release
  // keep re-enforcing, so even a dragged browser player pushes ours away.
  try {
    const mini = document.getElementById("scheduleMini");
    const live = document.getElementById("liveMini");
    if (!mini || mini.classList.contains("mini-hidden")) return;
    if (!live || live.classList.contains("mini-hidden")) return;
    if (mini.dataset.schedDrag === "1") return;
    const r = mini.getBoundingClientRect();
    const b = live.getBoundingClientRect();
    if (r.left >= b.right || b.left >= r.right) return;
    if (r.top >= b.bottom || b.top >= r.bottom) return;
    const w = r.width || mini.offsetWidth || 272;
    const h = r.height || mini.offsetHeight || 200;
    const cands = [
      [r.left, b.bottom + 12],
      [r.left, b.top - h - 12],
      [b.left - w - 12, r.top],
      [8, 70],
    ];
    for (const [cl, ct] of cands) {
      const [nl, nt] = schedClampPos(cl, ct, w, h);
      if (nl < b.right && nl + w > b.left && nt < b.bottom && nt + h > b.top) continue;
      mini.style.left = `${nl}px`;
      mini.style.top = `${nt}px`;
      mini.style.right = "auto";
      return;
    }
    mini.style.left = "8px";
    mini.style.top = "70px";
    mini.style.right = "auto";
  } catch (_) {}
}

function tickCountdowns() {
  // Local 1s tick: countdown text + tab/mini separation — no network, no re-render.
  // Covers the floating rectangles AND the open detail dialog (data-cd-dialog).
  try { enforceTabSeparation(); } catch (_) {}
  try { enforceMiniSeparation(); } catch (_) {}
  const nodes = document.querySelectorAll("#scheduleMini [data-cd], [data-cd-dialog]");
  if (!nodes.length) return;
  const byId = new Map(jobs.map((j) => [j.job_id, j]));
  for (const el of nodes) {
    const job = byId.get(el.getAttribute("data-cd") || el.getAttribute("data-cd-dialog"));
    if (!job) continue;
    const t = countdownText(job);
    if (el.textContent !== t) el.textContent = t;
  }
}

function schedDockToTab(top) {
  // Back to the right edge with an iPhone-like black-hole suck: the window
  // shrinks into the collapsed tab point (slow ~700ms, expo-out ease —
  // transform + opacity only, so it stays on the compositor and glides).
  // The tab slides back in from the right as the visible target while the
  // window drops to the same Y — the tab itself stays rock steady.
  // The scheduler tab never hides — it stays on the rail while jobs exist.
  ++schedSeq; // this dock owns the outcome; a newer open/pull overrules it
  const mySeq = schedSeq;
  try {
    if (typeof top === "number" && Number.isFinite(top)) placeSchedTab(top);
  } catch (_) {}
  try { enforceTabSeparation(); } catch (_) {}
  const mini = document.getElementById("scheduleMini");
  try {
    // The tab re-enters from the right: drop any stale inline push first so
    // the .off removal below glides it in (never a blink).
    const tab = document.getElementById("scheduleTab");
    if (tab) {
      tab.style.transform = "";
      tab.style.opacity = "";
    }
  } catch (_) {}
  setSchedTabOff(false);
  const finish = () => {
    if (mySeq !== schedSeq) return; // superseded by a newer open/pull — hands off
    const m = document.getElementById("scheduleMini");
    if (m) {
      try {
        delete m.dataset.docking;
        const anims = m.getAnimations ? m.getAnimations() : [];
        anims.forEach((a) => { try { a.cancel(); } catch (_) {} });
      } catch (_) {}
      m.classList.remove("docking");
      m.classList.add("mini-hidden");
      m.style.transformOrigin = "";
    }
    miniOpen = false;
    schedGuardSoon(); // watchdog: tab back on the rail, window parked
  };
  if (mini && !mini.dataset.docking) {
    mini.dataset.docking = "1";
    schedKillAnims(mini); // never stack a new suck over a stale one
    mini.classList.remove("sched-anim", "sched-pulling");
    mini.style.opacity = "";
    mini.style.transform = "";
    const reduce = schedReducedMotion();
    let sucked = false;
    if (!reduce && typeof mini.animate === "function") {
      try {
        // Right-center of the window must land on the tab center (measured
        // AFTER the tab was parked at the drop Y above).
        const d = schedTabDelta(mini);
        const mx = Math.round(d.dx * 0.52);
        const my = Math.round(d.dy * 0.52);
        const ex = d.dx;
        const ey = d.dy;
        mini.style.transformOrigin = "100% 50%";
        mini.classList.add("docking");
        mini.animate(
          [
            { transform: "translateX(0px) scale(1)", opacity: 1, offset: 0 },
            { transform: "translateX(-12px) scale(0.988)", opacity: 1, offset: 0.3 },
            { transform: `translateX(${mx}px) translateY(${my}px) scale(0.58)`, opacity: 0.94, offset: 0.62 },
            { transform: `translateX(${ex}px) translateY(${ey}px) scale(0.05)`, opacity: 0, offset: 1 },
          ],
          { duration: 700, easing: "cubic-bezier(0.32, 0.72, 0, 1)", fill: "forwards" }
        );
        sucked = true;
      } catch (_) { sucked = false; }
    }
    if (!sucked) {
      try { mini.classList.add("docking"); } catch (_) {}
    }
    setTimeout(finish, sucked ? 720 : 60);
  } else {
    finish();
  }
}

function schedMaybeDock(mini, wasMoved) {
  // Dock priority: pushing the window a quarter or more past the right edge
  // docks it (the suck wins) — only a smaller nudge snaps back inside.
  // Anything else snaps fully back inside — never outside the frame.
  try {
    const r = mini.getBoundingClientRect();
    const w = r.width || mini.offsetWidth || 300;
    const h = r.height || mini.offsetHeight || 200;
    if (wasMoved && r.left >= window.innerWidth - w * 0.75) {
      schedDockToTab(r.top);
      return;
    }
    const [nx, ny] = schedClampPos(r.left, r.top, w, h);
    mini.style.left = `${nx}px`;
    mini.style.top = `${ny}px`;
    mini.style.right = "auto";
  } catch (_) {}
}

function initMiniDrag(mini) {
  // Free smooth drag anywhere via the head (same as the browser player).
  // A fast rightward fling also docks (the window gets sucked in no matter
  // where the throw ends) — velocity is sampled from the last moves.
  const head = mini.querySelector(".sched-mini-head");
  if (!head) return;
  let sx = 0;
  let sy = 0;
  let ox = 0;
  let oy = 0;
  let dragging = false;
  let moved = false;
  let samples = []; // {x, t} of the last moves for fling detection
  head.style.touchAction = "none";
  head.style.cursor = "grab";
  head.addEventListener("pointerdown", (e) => {
    if (e.target && e.target.closest && e.target.closest("button")) return;
    dragging = true;
    moved = false;
    samples = [{ x: e.clientX, t: performance.now() }];
    try { mini.dataset.schedDrag = "1"; } catch (_) {}
    sx = e.clientX;
    sy = e.clientY;
    const r = mini.getBoundingClientRect();
    ox = r.left;
    oy = r.top;
    try {
      head.setPointerCapture?.(e.pointerId);
    } catch (_) {}
    head.style.cursor = "grabbing";
    e.preventDefault();
  });
  head.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) > 5) moved = true;
    const elW = mini.offsetWidth || 272;
    const elH = mini.offsetHeight || 200;
    // Like the browser player: while dragging the window may be pushed half
    // past the right edge — a drop a quarter-or-more out docks (suck wins),
    // anything less snaps strictly back inside on release.
    const nx = Math.min(Math.max(8, ox + e.clientX - sx), Math.max(8, window.innerWidth - elW / 2));
    const ny = Math.min(Math.max(70, oy + e.clientY - sy), Math.max(70, window.innerHeight - elH - 8));
    mini.style.left = `${nx}px`;
    mini.style.top = `${ny}px`;
    mini.style.right = "auto";
    try {
      samples.push({ x: e.clientX, t: performance.now() });
      if (samples.length > 6) samples.shift();
    } catch (_) {}
    e.preventDefault();
  });
  const stop = (e) => {
    if (!dragging) return;
    dragging = false;
    head.style.cursor = "grab";
    try { delete mini.dataset.schedDrag; } catch (_) {}
    try {
      // Fling: a fast rightward throw docks from anywhere (suction).
      let fling = false;
      if (moved && samples.length >= 2) {
        const a = samples[0];
        const b = samples[samples.length - 1];
        const dt = Math.max(1, b.t - a.t);
        const endX = e && typeof e.clientX === "number" ? e.clientX : b.x;
        fling = (b.x - a.x) / dt > 0.9 && endX - sx > 40;
      }
      if (fling) {
        schedDockToTab(mini.getBoundingClientRect().top);
        return;
      }
    } catch (_) {}
    try {
      schedMaybeDock(mini, moved);
    } catch (_) {}
    try { enforceMiniSeparation(); } catch (_) {}
  };
  head.addEventListener("pointerup", stop);
  head.addEventListener("pointercancel", stop);
}

let schedClampBound = false;
function bindMiniClampOnce() {
  if (schedClampBound) return;
  schedClampBound = true;
  window.addEventListener("resize", () => {
    try {
      const mini = document.getElementById("scheduleMini");
      if (!mini || mini.classList.contains("mini-hidden")) return;
      const w = mini.offsetWidth || 300;
      const h = mini.offsetHeight || 200;
      const r = mini.getBoundingClientRect();
      const [nx, ny] = schedClampPos(r.left, r.top, w, h);
      mini.style.left = `${nx}px`;
      mini.style.top = `${ny}px`;
      mini.style.right = "auto";
    } catch (_) {}
    try { enforceMiniSeparation(); } catch (_) {}
  });
}

function schedBuildMini() {
  // Single builder for the floating window (tap-open, pull preview, reopen
  // all share the SAME node shape + minimize-into-tab behavior).
  const mini = document.createElement("div");
  mini.id = "scheduleMini";
  mini.innerHTML = `
    <div class="sched-mini-head" data-drag>
      <span class="sched-mini-title">Scheduled Tasks</span>
      <span class="sched-mini-spot">
        <span class="sched-mini-count"></span>
        <button type="button" class="sched-mini-x" data-act="minimize" aria-label="Minimize" title="Minimize">✕</button>
      </span>
    </div>
    <div class="sched-list"></div>`;
  mini.querySelector('[data-act="minimize"]').addEventListener("click", () => {
    // × parks the window alive (same node, same size/scroll) — the tab stays
    // on the rail so reopening is instant. Nothing is destroyed.
    try {
      schedDockToTab(mini.getBoundingClientRect().top);
    } catch (_) {
      schedDockToTab(0);
    }
  });
  document.body.appendChild(mini);
  initMiniDrag(mini);
  bindMiniClampOnce();
  return mini;
}

function openMini(atX, atY) {
  // Reopen reuses the SAME node with no rebuild (parked alive on minimize,
  // like the browser player) — instant, keeps scroll and size.
  ++schedSeq; // this open owns the window; any pending dock/cancel dies
  let mini = document.getElementById("scheduleMini");
  if (mini) {
    // Tap reopens with the reverse-black-hole bloom out of the tab point.
    // Single-motion order: kill strays → reset tab → render content → park
    // final spot (separation included) → measure the tab delta → ONE bloom.
    // Nothing moves after.
    try {
      schedKillAnims(mini);
      schedTabReset();
      mini.classList.remove("mini-hidden", "sched-pulling", "docking", "sched-emerge", "sched-anim");
      delete mini.dataset.docking;
      mini.style.opacity = "";
      mini.style.transform = "";
      mini.style.transformOrigin = "";
      renderMini(); // content first: clamp + measure see the final size
      const w = mini.offsetWidth || 300;
      const h = mini.offsetHeight || 200;
      const r = mini.getBoundingClientRect();
      const [nx, ny] = schedClampPos(r.left, r.top, w, h);
      mini.style.left = `${nx}px`;
      mini.style.top = `${ny}px`;
      mini.style.right = "auto";
      try { enforceMiniSeparation(); } catch (_) {}
      const delta = schedTabDelta(mini);
      setSchedTabOff(true);
      schedBloomFromTab(mini, delta, 520);
    } catch (_) {}
    miniOpen = true;
    schedGuardSoon();
    return;
  }
  mini = schedBuildMini();
  if (!mini) return;
  // Free-floating window: opens around the tap point when the tab was tapped
  // (otherwise docked on the right rail under the tab), clamped on-screen.
  mini.style.visibility = "hidden";
  renderMini(); // content first: measure sees the final size (hidden still lays out)
  const mw = mini.offsetWidth || 300;
  const mh = mini.offsetHeight || 220;
  let [left, top] = schedClampPos(
    typeof atX === "number" ? atX - mw / 2 : window.innerWidth - mw - 52,
    typeof atY === "number" ? atY : Math.round(window.innerHeight * 0.3),
    mw, mh);
  // Open where the browser template isn't (free area, same screen logic).
  [left, top] = avoidBrowserOverlap(left, top, mw, mh);
  mini.style.left = `${left}px`;
  mini.style.top = `${top}px`;
  mini.style.right = "auto";
  mini.style.visibility = "";
  // Fresh nodes bloom out of the tab point (reverse black hole, tap path).
  // Same single-motion order as reopen: render → park → measure → bloom.
  try { enforceMiniSeparation(); } catch (_) {}
  const delta = schedTabDelta(mini);
  setSchedTabOff(true);
  schedBloomFromTab(mini, delta, 520);
  miniOpen = true;
  schedGuardSoon();
}

/* ---------- detail dialog: full task + Task Instructions + runs ---------- */

async function fetchDetail(jobId) {
  const res = await fetch(`/api/scheduler?job_id=${encodeURIComponent(jobId)}`);
  if (!res.ok) throw new Error("load failed");
  const data = await res.json();
  if (!data || !data.job) throw new Error("job not found");
  return data.job;
}

function runRow(run, idx, jobId, skillText) {
  // One rectangle per execution: status dot + #RUN<n> chip + status pill +
  // run time + open arrow. The whole rectangle is clickable (opens the
  // run's full session).
  const ok = !!run.success;
  const when = run.at ? fmtWhen(run.at) : "—";
  const num = Number.isFinite(Number(idx)) ? Number(idx) + 1 : 1;
  const preview = String(run.output || "").trim().slice(0, 90);
  return `
    <div class="sched-run ${ok ? "is-ok" : "is-fail"}" role="button" tabindex="0" data-run="${idx}" title="Open session">
      <span class="sched-run-dot"></span>
      <span class="sched-run-chip" dir="ltr">#RUN${num}</span>
      <span class="sched-run-state" dir="auto">${ok ? "Success" : "Failed"}</span>
      <span class="sched-run-time">${esc(when)}</span>
      <span class="sched-run-go" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6" /></svg></span>
      ${preview ? `<span class="sched-run-preview" dir="auto">${esc(preview)}</span>` : ""}
    </div>`;
}

async function openDetail(jobId) {
  let job;
  try {
    job = await fetchDetail(jobId);
  } catch (_) {
    toast("Couldn't load the task");
    return;
  }
  const runs = Array.isArray(job.run_history) ? job.run_history.slice().reverse() : [];
  // Done tasks never pause: the Pause button is hidden for completed /
  // one-shot finished jobs (only active + paused jobs can toggle).
  const canPause = job.status === "active" || job.status === "paused";
  const execTotal = job.repeat_count
    ? `${job.run_count || 0} / ${job.repeat_count}`
    : `${runs.length} run${runs.length === 1 ? "" : "s"}`;
  openModal(`
    <div class="modal-head sched-task-head">
      <div class="sched-head-text">
        <span class="sched-head-eyebrow">Scheduled task</span>
        <h3 class="sched-head-title" dir="auto">${esc(job.task || job.job_id)}</h3>
      </div>
      <button class="modal-close" data-close type="button" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
    <div class="modal-body sched-detail">
      <section class="sched-glass">
        <h4 class="sched-part">Schedule</h4>
        <div class="sched-meta">
          <div><b>Status</b> <span dir="auto">${esc(job.status || "—")}</span></div>
          <div><b>Time left</b> <span data-cd-dialog="${esc(job.job_id)}">${esc(countdownText(job))}</span></div>
          <div><b>Created</b> <span dir="ltr">${esc(fmtWhen(job.created_at))}</span></div>
          <div><b>Repeat</b> <span dir="auto">${esc(job.repeat || "One-time")}</span></div>
          <div><b>Next run</b> <span dir="ltr">${esc(job.next_run ? fmtWhen(job.next_run) : "—")}</span></div>
          <div><b>Last run</b> <span dir="ltr">${esc(job.last_run ? fmtWhen(job.last_run) : "—")}</span></div>
          <div><b>Executions</b> <span dir="ltr">${esc(execTotal)}</span></div>
        </div>
      </section>
      <section class="sched-glass sched-skill-card">
        <div class="sched-skill-head">
          <h4>Task Instructions</h4>
          <span class="sched-skill-actions">
            <button type="button" class="sched-icon-btn" id="schedSkillCopyBtn" title="Copy" aria-label="Copy instructions">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>
            </button>
            <button type="button" class="sched-icon-btn" id="schedSkillToggleBtn" title="Edit" aria-label="Edit instructions">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" /></svg>
            </button>
            <button type="button" class="sched-icon-btn" id="schedSkillCancelBtn" title="Cancel" aria-label="Cancel editing" hidden>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
            </button>
          </span>
        </div>
        <pre class="sched-skill" dir="auto" id="schedSkillPre">${esc(job.full_instructions || job.skill_preview || "—")}</pre>
        <textarea class="input sched-edit" id="schedSkillEdit" rows="12" dir="auto" hidden></textarea>
      </section>
      <section class="sched-glass sched-exec-card">
        <div class="sched-exec-head">
          <h4 class="sched-part">Executions</h4>
          <span class="sched-exec-count" dir="ltr">${esc(execTotal)}</span>
        </div>
        <div class="sched-runs">
          ${runs.length ? runs.map((r, i) => runRow(r, runs.length - 1 - i, job.job_id, "")).join("") : '<div class="sched-empty">Not executed yet</div>'}
        </div>
      </section>
    </div>
    <div class="modal-foot sched-detail-foot">
      <button class="glass-btn danger sched-foot-danger" id="schedDeleteBtn" type="button">Delete task</button>
      <span class="sched-foot-spacer" aria-hidden="true"></span>
      ${canPause ? `<button class="glass-btn ${job.status === "paused" ? "accent" : "warn"}" id="schedPauseBtn" type="button">${job.status === "paused" ? "Resume" : "Pause"}</button>` : ""}
      <button class="glass-btn sched-foot-primary" data-close type="button">Cancel</button>
    </div>`);
  // One pencil button: tap → edit opens and the pencil BECOMES the save mark.
  // Tap the save mark → instructions saved, back to pencil. × cancels.
  const PENCIL_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" /></svg>`;
  const SAVE_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12" /></svg>`;
  document.getElementById("schedSkillCopyBtn")?.addEventListener("click", async () => {
    const text = document.getElementById("schedSkillPre")?.textContent || "";
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch (_) {
      toast("Copy failed");
    }
  });
  const toggleBtn = document.getElementById("schedSkillToggleBtn");
  const cancelBtn = document.getElementById("schedSkillCancelBtn");
  const pre = document.getElementById("schedSkillPre");
  const area = document.getElementById("schedSkillEdit");
  if (area) area.value = job.full_instructions || job.skill_preview || "";
  let editing = false;
  const setEditing = (on) => {
    editing = on;
    if (!pre || !area || !toggleBtn || !cancelBtn) return;
    pre.hidden = on;
    area.hidden = !on;
    toggleBtn.innerHTML = on ? SAVE_SVG : PENCIL_SVG;
    toggleBtn.title = on ? "Save" : "Edit";
    toggleBtn.setAttribute("aria-label", on ? "Save instructions" : "Edit instructions");
    toggleBtn.classList.toggle("sched-save", on);
    cancelBtn.hidden = !on;
    if (on) area.focus();
  };
  toggleBtn?.addEventListener("click", async () => {
    if (!editing) {
      setEditing(true);
      return;
    }
    const text = (area?.value || "").trim();
    if (!text) {
      toast("Instructions are empty");
      return;
    }
    toggleBtn.disabled = true;
    try {
      const res = await fetch("/api/scheduler", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "update_skill", job_id: job.job_id, instructions: text }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "save failed");
      toast("Instructions saved");
      if (pre) pre.textContent = text;
      setEditing(false);
    } catch (err) {
      toast("Save failed");
    } finally {
      toggleBtn.disabled = false;
    }
  });
  cancelBtn?.addEventListener("click", () => {
    if (area) area.value = pre ? pre.textContent : "";
    setEditing(false);
  });
  document.querySelectorAll(".sched-run[data-run]").forEach((row) => {
    const go = () => {
      const idx = Number(row.getAttribute("data-run"));
      try {
        closeModal();
      } catch (_) {}
      openRunPage(job.job_id, idx);
    };
    row.addEventListener("click", go);
    row.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        go();
      }
    });
  });
  // Pause / resume: pausing stops the countdown (engine only runs active
  // jobs). The floating card flips to "Paused" on the next push tick.
  document.getElementById("schedPauseBtn")?.addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const paused = job.status === "paused";
    btn.disabled = true;
    try {
      const res = await fetch("/api/scheduler", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: paused ? "resume" : "pause", job_id: job.job_id }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || (paused ? "Resume failed" : "Pause failed"));
      try {
        closeModal();
      } catch (_) {}
      toast(paused ? "Task resumed" : "Task paused — countdown stopped");
    } catch (err) {
      toast((err && err.message) || (paused ? "Resume failed" : "Pause failed"));
      btn.disabled = false;
    }
  });
  // Delete asks a real confirmation dialog first. Removes job + skill +
  // branch session. Cancel returns to this same task dialog (refreshed).
  document.getElementById("schedDeleteBtn")?.addEventListener("click", () => {
    openModal(`
      <div class="modal-head">
        <div class="tile"><span class="tile-letters">!</span></div>
        <div class="modal-head-text"><h3>Delete this task?</h3><p>This cannot be undone</p></div>
        <button class="modal-close" data-close type="button" aria-label="Close">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
            <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
      <div class="modal-body"><p class="modal-body-text" dir="auto">“${esc(job.task || job.job_id)}” and its skill will be removed permanently.</p></div>
      <div class="modal-foot sched-confirm-foot">
        <button class="glass-btn" id="schedConfirmCancel" type="button">Cancel</button>
        <button class="glass-btn danger" id="schedConfirmDelete" type="button">Delete</button>
      </div>`);
    document.getElementById("schedConfirmCancel")?.addEventListener("click", () => {
      openDetail(job.job_id);
    });
    document.getElementById("schedConfirmDelete")?.addEventListener("click", async (ev) => {
      const btn = ev.currentTarget;
      btn.disabled = true;
      btn.classList.add("loading");
      try {
        const res = await fetch("/api/scheduler", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "delete", job_id: job.job_id }),
        });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(data.error || "delete failed");
        try {
          closeModal();
        } catch (_) {}
        toast("Task deleted");
      } catch (_) {
        btn.disabled = false;
        btn.classList.remove("loading");
        toast("Delete failed");
      }
    });
  });
}

/* ---------- run page: a FULL agent-like section per run, hash-routed ----------
 * Preview opens /#sched-<jobid>-<runIdx> (a real session URL, not a dialog):
 * skill as the user message + execution output as the agent reply + a working
 * follow-up composer. Refresh/deep-link keeps the same session. Back returns
 * to the agent page. */

const HASH_RE = /^#sched-([A-Za-z0-9_-]{1,64})-(\d{1,3})$/;

let runView = null; // {jobId, runIdx, skill, task} while the run page is open
let schedFmtCache = null; // compiled rich-format helpers from chat.js (shared)

const SCHED_ICONS = {
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" /></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10" /><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10" /></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>',
};
const SCHED_THINK_ARROW = `<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 2.5 L8 6 L4 9.5"/></svg>`;

function schedEscapeHtml(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// Minimal rich renderer mirroring chat.js formatRich (code fences, bold,
// lists, tables, quotes) so run pages read like the main session.
function schedFormatRich(text) {
  try {
    const chunks = [];
    let src = String(text ?? "");
    src = src.replace(/```([^`\n]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
      const label = schedEscapeHtml((lang || "").trim() || "code");
      const body = schedEscapeHtml(String(code || "").replace(/^\n+|\s+$/g, ""));
      chunks.push(`<div class="codeblock" dir="ltr"><div class="code-head"><span class="code-lang">${label}</span><button type="button" class="code-copy" data-codecopy aria-label="Copy code">${SCHED_ICONS.copy}<span>Copy</span></button></div><pre><code>${body}</code></pre></div>`);
      return `%%SCHEDCODE${chunks.length - 1}%%`;
    });
    let e = schedEscapeHtml(src);
    e = e.replace(/`([^`\n]+?)`/g, "<code>$1</code>");
    e = e.replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>");
    e = e.replace(/~~([^~]+?)~~/g, "<del>$1</del>");
    const lines = e.split("\n");
    let html = "";
    let list = null;
    const flush = () => { if (list) { html += `</${list}>`; list = null; } };
    const autoDir = (t) => (/[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/.test(String(t || "")) ? "rtl" : "ltr");
    const isPipeRow = (l) => /^\s*\|.*\|\s*$/.test(l);
    const isSepRow = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l);
    const splitCells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    for (let li = 0; li < lines.length; li++) {
      const line = lines[li];
      const ul = line.match(/^\s*[-*]\s+(.*)$/);
      const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
      const h = line.match(/^\s*#{1,4}\s+(.*)$/);
      const quote = line.match(/^\s*>\s?(.*)$/);
      const codeRef = line.trim().match(/^%%SCHEDCODE(\d+)%%$/);
      if (isPipeRow(line) && li + 1 < lines.length && isSepRow(lines[li + 1])) {
        flush();
        const head = splitCells(line);
        let rowsHtml = "";
        li += 2;
        while (li < lines.length && isPipeRow(lines[li])) {
          const cells = splitCells(lines[li]);
          rowsHtml += `<tr>${cells.map((c, idx) => (idx === 0 && cells.length > 1
            ? `<th scope="row" dir="${autoDir(c)}">${c}</th>`
            : `<td dir="${autoDir(c)}">${c}</td>`)).join("")}</tr>`;
          li++;
        }
        li--;
        html += `<table dir="${autoDir(head.join(" "))}"><thead><tr>${head.map((c) => `<th dir="${autoDir(c)}">${c}</th>`).join("")}</tr></thead><tbody>${rowsHtml}</tbody></table>`;
      } else if (ul) {
        if (list !== "ul") { flush(); html += "<ul>"; list = "ul"; }
        html += `<li dir="${autoDir(ul[1])}">${ul[1]}</li>`;
      } else if (ol) {
        if (list !== "ol") { flush(); html += "<ol>"; list = "ol"; }
        html += `<li dir="${autoDir(ol[1])}">${ol[1]}</li>`;
      } else {
        flush();
        if (codeRef && chunks[Number(codeRef[1])] !== undefined) html += chunks[Number(codeRef[1])];
        else if (line.trim() === "") continue;
        else if (h) html += `<p class="md-h" dir="auto"><b>${h[1]}</b></p>`;
        else if (quote) html += `<blockquote><p dir="auto">${quote[1]}</p></blockquote>`;
        else html += `<p dir="auto">${line}</p>`;
      }
    }
    flush();
    html = html.replace(/%%SCHEDCODE(\d+)%%/g, (m, i) => chunks[Number(i)] || "");
    return html || `<p dir="auto">${schedEscapeHtml(text)}</p>`;
  } catch (_) {
    return `<p dir="auto">${schedEscapeHtml(text)}</p>`;
  }
}

function schedDetectDir(text) {
  const s = String(text || "").slice(0, 2000);
  if (!s.trim()) return null;
  const ar = (s.match(/[ء-غف-ي]/g) || []).length;
  const lat = (s.match(/[A-Za-z]/g) || []).length;
  if (!ar && !lat) return null;
  return ar >= lat ? "rtl" : "ltr";
}

function schedApplyDir(el, text) {
  const d = schedDetectDir(text);
  el.classList.remove("rtl", "ltr");
  if (!d) return;
  el.classList.add(d);
  const c = el.querySelector(".msg-content");
  if (c) c.setAttribute("dir", d);
}

async function schedCopyText(text) {
  try {
    await navigator.clipboard.writeText(String(text || ""));
    toast("Copied");
  } catch (_) {
    toast("Copy failed");
  }
}

function schedActBtn(icon, label, fn, danger) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "msg-act" + (danger ? " danger" : "");
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = SCHED_ICONS[icon] || "";
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    fn();
  });
  return b;
}

function schedMsgPost(jobId, body) {
  return fetch("/api/scheduler", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ job_id: jobId, ...(body || {}) }),
  }).then(async (res) => {
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
    return data;
  });
}

function schedScrollRun() {
  try {
    document.getElementById("schedRunScroll")?.scrollTo(0,
      document.getElementById("schedRunScroll").scrollHeight);
  } catch (_) {}
}

// Working steps renderer: exact copy of the main session thinking-block
// (open while running, settled collapsed with elapsed above the reply —
// header text stays "Working" like the main chat, never "Done").
function schedMakeWorkBlock(open) {
  const block = document.createElement("div");
  block.className = "thinking-block" + (open ? " open" : " done");
  const header = document.createElement("div");
  header.className = "thinking-header";
  header.setAttribute("role", "button");
  header.setAttribute("tabindex", "0");
  header.dir = "ltr";
  const sq = document.createElement("div");
  sq.className = "dot-square";
  const SIZE = 5;
  const center = 2;
  for (let r = 0; r < SIZE; r++) {
    for (let c = 0; c < SIZE; c++) {
      const dot = document.createElement("span");
      dot.dataset.ring = Math.max(Math.abs(r - center), Math.abs(c - center));
      dot.classList.add(r >= 1 && r <= 3 && c >= 1 && c <= 3 ? "lit-white" : "lit-black");
      sq.appendChild(dot);
    }
  }
  const wt = document.createElement("span");
  wt.className = "working-text";
  wt.textContent = "Working";
  header.appendChild(sq);
  header.appendChild(wt);
  const steps = document.createElement("div");
  steps.className = "steps";
  const inner = document.createElement("div");
  inner.className = "steps-inner";
  steps.appendChild(inner);
  block.appendChild(header);
  block.appendChild(steps);
  block._stepsInner = inner;
  block._header = header;
  block._sq = sq;
  const toggle = () => block.classList.toggle("open");
  header.addEventListener("click", toggle);
  header.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
  });
  return block;
}

function schedStepItem(step) {
  const title = String((step && step.event) || "Step").slice(0, 80) || "Step";
  const reason = step && typeof step.reason === "string" ? step.reason.trim().slice(0, 500) : "";
  const output = step && typeof step.output === "string" ? step.output.trim().slice(0, 500) : "";
  const shot = step && typeof step.shot === "string" ? step.shot : "";
  const item = document.createElement("div");
  item.className = "step-item " + (step && step.failed === true ? "fail" : "done");
  const head = document.createElement("div");
  head.className = "step-head";
  head.setAttribute("role", "button");
  head.setAttribute("tabindex", "0");
  const bullet = document.createElement("span");
  bullet.className = "step-bullet";
  const titleEl = document.createElement("span");
  titleEl.className = "step-title";
  titleEl.textContent = title;
  head.appendChild(bullet);
  head.appendChild(titleEl);
  const content = document.createElement("div");
  content.className = "step-content";
  const inner = document.createElement("div");
  inner.className = "step-content-inner";
  content.appendChild(inner);
  if (reason) {
    const r = document.createElement("div");
    r.className = "step-text step-reason";
    r.setAttribute("dir", "auto");
    r.textContent = reason;
    inner.appendChild(r);
  }
  if (output) {
    const o = document.createElement("div");
    o.className = "step-text step-out";
    o.setAttribute("dir", "auto");
    o.textContent = output;
    inner.appendChild(o);
  }
  if (shot) {
    const wrap = document.createElement("div");
    wrap.className = "step-img";
    const img = document.createElement("img");
    img.alt = "Step image";
    img.decoding = "async";
    img.loading = "lazy";
    img.dataset.shot = shot;
    img.addEventListener("error", () => wrap.remove(), { once: true });
    wrap.appendChild(img);
    inner.appendChild(wrap);
  }
  item.appendChild(head);
  item.appendChild(content);
  if (reason || output || shot) {
    const arrow = document.createElement("span");
    arrow.className = "step-arrow";
    arrow.innerHTML = SCHED_THINK_ARROW;
    head.appendChild(arrow);
    const toggle = (e) => {
      if (e) e.stopPropagation();
      item.classList.toggle("open");
      const img = inner.querySelector(".step-img img");
      if (img && !img.getAttribute("src") && img.dataset.shot) {
        img.src = "/api/shot?path=" + encodeURIComponent(img.dataset.shot);
      }
    };
    head.addEventListener("click", toggle);
    head.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); }
    });
  }
  return item;
}

function schedWorkBlock(steps, elapsed, open) {
  // Working wrapper for one assistant turn: steps list + elapsed seconds.
  const block = schedMakeWorkBlock(open !== false);
  if (!open) {
    try {
      block.classList.remove("open");
      block.classList.add("done");
      const secs = Number(elapsed);
      if (Number.isFinite(secs) && secs >= 0) {
        const t = document.createElement("span");
        t.className = "working-time";
        t.textContent = `${Math.round(secs)}s`;
        const arrow = block._header.querySelector(":scope > .arrow");
        if (arrow) block._header.insertBefore(t, arrow);
        else block._header.appendChild(t);
      }
    } catch (_) {}
  }
  const list = Array.isArray(steps) ? steps : [];
  for (const s of list.slice(0, 12)) {
    try { block._stepsInner.appendChild(schedStepItem(s)); } catch (_) {}
  }
  if (list.length && !block._header.querySelector(":scope > .arrow")) {
    const arrow = document.createElement("span");
    arrow.className = "arrow";
    arrow.innerHTML = SCHED_THINK_ARROW;
    block._header.appendChild(arrow);
  }
  return block;
}

function schedAttachUserBar(el, jobId) {
  const old = el.querySelector(":scope > .msg-actions");
  if (old) old.remove();
  const bar = document.createElement("div");
  bar.className = "msg-actions below";
  const raw = el.dataset.raw || "";
  const mid = el.dataset.mid || "";
  [
    ["copy", "Copy", () => schedCopyText(raw)],
    ["edit", "Edit", () => schedStartEdit(el)],
    ["trash", "Delete", () => schedConfirmDelete(el, jobId)],
  ].forEach(([icon, label, fn], i) => {
    const b = schedActBtn(icon, label, fn, icon === "trash");
    b.style.animationDelay = `${i * 60}ms`;
    bar.appendChild(b);
  });
  el.appendChild(bar);
}

function schedAttachAssistantBar(el, jobId) {
  const old = el.querySelector(":scope > .msg-actions");
  if (old) old.remove();
  const raw = el.dataset.raw || "";
  const bar = document.createElement("div");
  bar.className = "msg-actions below";
  [
    ["copy", "Copy", () => schedCopyText(raw)],
    ["edit", "Edit", () => schedStartEdit(el)],
    ["refresh", "Regenerate", () => schedRegenerate(el)],
    ["trash", "Delete", () => schedConfirmDelete(el, jobId)],
  ].forEach(([icon, label, fn], i) => {
    const b = schedActBtn(icon, label, fn, icon === "trash");
    b.style.animationDelay = `${i * 60}ms`;
    bar.appendChild(b);
  });
  el.appendChild(bar);
}

function bubble(who, text, mid, extra) {
  // Exact copy of the main agent chat shape: .msg.user / .msg.assistant with
  // rich formatting + hover action bars, and the settled Working block BEFORE
  // the reply text inside .msg-content (never after it, never "Done").
  const el = document.createElement("div");
  el.className = who === "user" ? "msg user" : "msg assistant";
  if (who === "pending") el.classList.add("is-pending");
  if (mid) el.dataset.mid = String(mid).slice(0, 16);
  el.dataset.raw = String(text ?? "");
  schedApplyDir(el, text);
  const content = document.createElement("div");
  content.className = "msg-content";
  content.setAttribute("dir", "auto");
  const wrap = document.createElement("div");
  // defer:true leaves the reply empty for the typing animation (the live
  // turn types into el._wrap, exactly like the main chat's typeRich shell).
  const deferType = !!(extra && extra.defer);
  if (!deferType) wrap.innerHTML = schedFormatRich(text);
  content.appendChild(wrap);
  el.appendChild(content);
  el._wrap = wrap;
  // Main-chat rule: steps decide the block's fate — with steps the settled
  // Working block sits above the reply; with none no Working artifact at all.
  if (extra && Array.isArray(extra.steps) && extra.steps.length) {
    try {
      content.prepend(schedWorkBlock(extra.steps, extra.elapsed, false));
    } catch (_) {}
  }
  // Final media renders under the reply like the main chat (bounded).
  // Deferred (typing) bubbles append it after the animation finishes.
  if (!deferType) schedAppendMedia(content, extra);
  // Code-copy buttons inside rich text work like the main chat.
  // Deferred (typing) bubbles bind them after the animation finishes.
  if (!deferType) schedBindCodeCopy(content);
  return el;
}

function schedAppendMedia(content, extra) {
  // Final images + voice lines under the reply (bounded, main-chat order).
  try {
    const imgs = extra && Array.isArray(extra.images) ? extra.images.slice(0, 6) : [];
    for (const im of imgs) {
      const shot = im && typeof im.shot === "string" ? im.shot : (typeof im === "string" ? im : "");
      if (!shot) continue;
      const wrap = document.createElement("div");
      wrap.className = "step-img";
      const img = document.createElement("img");
      img.alt = "Step image";
      img.decoding = "async";
      img.loading = "lazy";
      img.src = "/api/shot?path=" + encodeURIComponent(shot);
      img.addEventListener("error", () => wrap.remove(), { once: true });
      wrap.appendChild(img);
      content.appendChild(wrap);
    }
    const auds = extra && Array.isArray(extra.audio) ? extra.audio.slice(0, 4) : [];
    for (const au of auds) {
      const nm = au && typeof au.audio === "string" ? au.audio : "";
      if (!nm) continue;
      const line = document.createElement("div");
      line.className = "msg-voice-line";
      line.dir = "ltr";
      line.textContent = `Voice: ${nm}`;
      content.appendChild(line);
    }
  } catch (_) {}
}

function schedBindCodeCopy(root) {
  // Wire code-copy buttons inside rich text (same as the main chat).
  try {
    root.querySelectorAll("[data-codecopy]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const code = btn.closest(".codeblock")?.querySelector("code")?.textContent || "";
        schedCopyText(code);
      });
    });
  } catch (_) {}
}

function schedTypeRich(holder, fullText) {
  // Exact copy of the main chat's typeRich (format first, then animate the
  // formatted result itself): text-node chunks, typing cursor, list markers
  // appear with first text, tables/code pop in when finished. Never throws.
  return new Promise((resolve) => {
    try {
      const template = document.createElement("div");
      template.innerHTML = schedFormatRich(fullText);
      const live = template.cloneNode(true);
      const jobs = [];
      const walker = document.createTreeWalker(live, NodeFilter.SHOW_TEXT);
      const textNodes = [];
      let t;
      while ((t = walker.nextNode())) textNodes.push(t);
      textNodes.forEach((tn) => {
        jobs.push({ tn, full: tn.textContent });
        tn.textContent = "";
      });
      holder.innerHTML = "";
      holder.classList.add("typing-live");
      holder.appendChild(live);
      let ji = 0;
      let ci = 0;
      const markItemText = () => {
        try {
          const job = jobs[ji];
          if (job && job.full.length && job.tn.parentElement) {
            const li = job.tn.parentElement.closest("li");
            if (li) li.classList.add("has-text");
          }
        } catch (_) {}
      };
      const advance = () => {
        while (ji < jobs.length && ci >= jobs[ji].full.length) {
          ji++;
          ci = 0;
        }
        markItemText();
      };
      advance();
      if (!jobs.some((j) => j.full.length)) {
        holder.classList.remove("typing-live");
        resolve(live);
        return;
      }
      const cursor = document.createElement("span");
      cursor.className = "typing-cursor";
      const placeCursor = () => {
        cursor.remove();
        if (ji < jobs.length) {
          const tn = jobs[ji].tn;
          tn.parentNode.insertBefore(cursor, tn.nextSibling);
        } else {
          live.appendChild(cursor);
        }
      };
      placeCursor();
      schedScrollRun();
      (function tick() {
        if (ji >= jobs.length) {
          cursor.remove();
          schedScrollRun();
          holder.classList.remove("typing-live");
          resolve(live);
          return;
        }
        const startJi = ji;
        const job = jobs[ji];
        const chunk = 2 + (Math.random() < 0.3 ? 1 : 0);
        job.tn.textContent += job.full.slice(ci, ci + chunk);
        ci += chunk;
        advance();
        for (let k = startJi; k < ji; k++) {
          try {
            const anc = jobs[k].tn.parentElement
              ? jobs[k].tn.parentElement.closest("table,pre")
              : null;
            if (anc && !anc.classList.contains("pop-in")) anc.classList.add("pop-in");
          } catch (_) {}
        }
        placeCursor();
        schedScrollRun();
        setTimeout(tick, 6 + Math.random() * 10);
      })();
    } catch (_) {
      try {
        holder.innerHTML = schedFormatRich(fullText);
      } catch (_) {}
      resolve(holder);
    }
  });
}

// Edit mode: the message text lands in the run composer with a banner;
// SEND saves it in place, then regenerates the reply below it.
let schedEditing = null; // {mid, original, role} while editing a bubble

function schedStartEdit(el) {
  const input = document.getElementById("schedRunInput");
  if (!input || !el?.dataset?.mid) {
    toast("Nothing to edit");
    return;
  }
  schedEditing = {
    mid: el.dataset.mid,
    original: el.dataset.raw || "",
    role: el.classList.contains("user") ? "user" : "assistant",
  };
  input.value = schedEditing.original;
  try {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  } catch (_) {}
  schedPaintEditBanner();
  try { input.focus(); } catch (_) {}
}

function schedPaintEditBanner() {
  let banner = document.getElementById("schedEditBanner");
  const form = document.getElementById("schedRunForm");
  if (schedEditing && form && !banner) {
    banner = document.createElement("div");
    banner.id = "schedEditBanner";
    banner.className = "edit-banner";
    banner.innerHTML = `<span class="edit-strip-icon" aria-hidden="true">${SCHED_ICONS.edit}</span><span class="edit-banner-text">Editing message — SEND saves</span><button type="button" class="edit-strip-x" id="schedEditCancel" aria-label="Cancel editing">✕</button>`;
    form.prepend(banner);
    banner.querySelector("#schedEditCancel")?.addEventListener("click", schedCancelEdit);
  }
  if (banner) banner.hidden = !schedEditing;
}

function schedCancelEdit() {
  schedEditing = null;
  const input = document.getElementById("schedRunInput");
  if (input) {
    input.value = "";
    try { input.style.height = "auto"; } catch (_) {}
  }
  document.getElementById("schedEditBanner")?.remove();
}

async function schedSaveEdit(jobId, newText) {
  // 1. save the edited text in place (branch-local).
  await schedMsgPost(jobId, { action: "msg_edit", id: schedEditing.mid, content: newText });
  // 2. refresh the bubble on screen (rich render + raw for next edit).
  const log = document.getElementById("schedRunLog");
  const live = schedEditing.mid && log
    ? log.querySelector(`.msg[data-mid="${CSS.escape(schedEditing.mid)}"]`)
    : null;
  if (live) {
    live.dataset.raw = newText;
    const content = live.querySelector(".msg-content > div");
    if (content) content.innerHTML = schedFormatRich(newText);
    schedApplyDir(live, newText);
    if (live.classList.contains("user")) schedAttachUserBar(live, jobId);
    else schedAttachAssistantBar(live, jobId);
  }
  if (schedEditing.role === "user") {
    // 3. user edit restarts the thread here: prune everything below the
    // anchor (server-confirmed), then regenerate one fresh reply.
    try {
      await schedMsgPost(jobId, { action: "msg_prune", id: schedEditing.mid });
    } catch (err) {
      toast(err instanceof Error ? err.message : "Delete failed");
      return false;
    }
    try {
      let n = live ? live.nextElementSibling : null;
      while (n) {
        const nx = n.nextElementSibling;
        n.remove();
        n = nx;
      }
    } catch (_) {}
    const mid = schedEditing.mid;
    schedEditing = null;
    document.getElementById("schedEditBanner")?.remove();
    await schedRunTurn(jobId, newText, [], { skipHistoryIds: mid ? [mid] : null });
    return true;
  }
  schedEditing = null;
  document.getElementById("schedEditBanner")?.remove();
  return true;
}

function schedConfirmDelete(el, jobId) {
  const isUser = el.classList.contains("user");
  openModal(`
    <div class="modal-head">
      <div class="tile"><span class="tile-letters">!</span></div>
      <div class="modal-head-text"><h3>Delete message?</h3><p>Removes it from this task session</p></div>
      <button class="modal-close" data-close type="button" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
    <div class="modal-body"><p class="modal-body-text">${
      isUser
        ? "This message will be deleted from the task session."
        : "This reply will be deleted from the task session."
    }</p></div>
    <div class="modal-foot">
      <button class="btn" data-close type="button">Cancel</button>
      <button class="btn btn-danger" id="schedConfirmDelBtn" type="button">Delete</button>
    </div>`);
  document.getElementById("schedConfirmDelBtn")?.addEventListener("click", async () => {
    closeModal();
    try {
      if (el.dataset.mid) await schedMsgPost(jobId, { action: "msg_delete", id: el.dataset.mid });
      el.remove();
    } catch (err) {
      toast(err instanceof Error ? err.message : "Delete failed");
    }
  });
}

function schedPrevUserText(shell) {
  let prev = shell.previousElementSibling;
  while (prev) {
    if (prev.classList && prev.classList.contains("user") && String(prev.dataset.raw || "").trim()) {
      return String(prev.dataset.raw);
    }
    prev = prev.previousElementSibling;
  }
  return "";
}

async function schedRegenerate(shell) {
  // Same contract as the main chat: drop the reply + everything below it
  // (server-confirmed prune), then generate fresh for the same prompt.
  if (!runView || !shell?.dataset?.mid) {
    toast("Nothing to regenerate");
    return;
  }
  const userText = schedPrevUserText(shell);
  if (!userText) {
    toast("Nothing to regenerate");
    return;
  }
  let anchorMid = null;
  try {
    let p = shell.previousElementSibling;
    while (p) {
      if (p.classList && p.classList.contains("user") && p.dataset.mid) {
        anchorMid = p.dataset.mid;
        break;
      }
      p = p.previousElementSibling;
    }
  } catch (_) {}
  try {
    if (anchorMid) await schedMsgPost(runView.jobId, { action: "msg_prune", id: anchorMid });
    else if (shell.dataset.mid) await schedMsgPost(runView.jobId, { action: "msg_delete", id: shell.dataset.mid });
  } catch (err) {
    toast(err instanceof Error ? err.message : "Delete failed");
    return;
  }
  try {
    const anchor = anchorMid && document.getElementById("schedRunLog")
      ? document.getElementById("schedRunLog").querySelector(`.msg[data-mid="${CSS.escape(anchorMid)}"]`)
      : null;
    let n = anchor ? anchor.nextElementSibling : shell;
    while (n) {
      const nx = n.nextElementSibling;
      n.remove();
      n = nx;
    }
  } catch (_) {}
  await schedRunTurn(runView.jobId, userText, [], { skipHistoryIds: anchorMid ? [anchorMid] : null });
}

// One live turn against the branch session — exact copy of the main chat
// contract: normal send appends user bubble + Working block, then the reply
// (Working settled above the text); edit/regenerate reuses the existing user
// bubble (skipHistoryIds) so no duplicate user message is ever appended.
async function schedRunTurn(jobId, userText, atts, opts) {
  const log = document.getElementById("schedRunLog");
  const form = document.getElementById("schedRunForm");
  const input = document.getElementById("schedRunInput");
  const sendBtn = document.getElementById("schedRunSend");
  const skipIds = (opts && Array.isArray(opts.skipHistoryIds)) ? opts.skipHistoryIds : null;
  const isResend = !!(skipIds && skipIds.length);
  let userEl = null;
  if (!isResend) {
    userEl = bubble("user", userText || "Attachments", `u-${Date.now().toString(36)}`);
    schedAttachUserBar(userEl, jobId);
    log?.appendChild(userEl);
  }
  const workShell = document.createElement("div");
  workShell.className = "msg assistant";
  workShell.appendChild(schedWorkBlock([], 0, true));
  const workBlock = workShell.firstChild;
  log?.appendChild(workShell);
  schedScrollRun();
  if (form) form.dataset.busy = "1";
  if (sendBtn) sendBtn.disabled = true;
  if (input) input.disabled = true;
  try {
    const body = {
      action: "chat",
      message: userText,
      attachments: atts || [],
      mode: runMode,
    };
    if (skipIds && skipIds.length) body.skip_history_ids = skipIds;
    const res = await fetch("/api/scheduler", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ job_id: jobId, ...body }),
    });
    const data = await res.json().catch(() => ({}));
    workShell.remove();
    if (!res.ok) {
      if (userEl) { try { userEl.remove(); } catch (_) {} }
      const errEl = bubble("agent", `Send failed: ${data.error || res.status}`);
      schedAttachAssistantBar(errEl, jobId);
      log?.appendChild(errEl);
      return;
    }
    // Normal send: adopt the persisted server ids (no fake temp id stays).
    try {
      if (userEl && data.ids && data.ids.user) {
        userEl.dataset.mid = String(data.ids.user).slice(0, 16);
        userEl.dataset.raw = String(userText || "");
      }
    } catch (_) {}
    const steps = Array.isArray(data.events) ? data.events : [];
    const replyText = String(data.reply || "—");
    // Live reply shell (empty text) + typing animation, exactly like the
    // main chat: the settled Working block stays above while the answer
    // types out below it; bars + copy wire up when typing finishes.
    const replyEl = bubble("agent", replyText,
      (data.ids && data.ids.assistant) || `a-${Date.now().toString(36)}`,
      { steps, elapsed: data.elapsed, images: data.images, audio: data.audio, defer: true });
    log?.appendChild(replyEl);
    schedScrollRun();
    try {
      await schedTypeRich(replyEl._wrap || replyEl.querySelector(".msg-content > div"), replyText);
    } catch (_) {}
    try {
      const contentEl = replyEl.querySelector(".msg-content");
      if (contentEl) schedAppendMedia(contentEl, { images: data.images, audio: data.audio });
    } catch (_) {}
    schedBindCodeCopy(replyEl);
    schedAttachAssistantBar(replyEl, jobId);
    schedScrollRun();
  } catch (_) {
    try { workShell.remove(); } catch (_) {}
    if (userEl && !isResend) { try { userEl.remove(); } catch (_) {} }
    const errEl = bubble("agent", "Send failed (connection lost)");
    schedAttachAssistantBar(errEl, jobId);
    log?.appendChild(errEl);
  } finally {
    if (form) form.dataset.busy = "";
    if (sendBtn) sendBtn.disabled = false;
    if (input) {
      input.disabled = false;
      try { input.focus(); } catch (_) {}
    }
  }
  schedScrollRun();
}

function updateSchedVisibility() {
  // The scheduler tab + list live in the Agent section only (agent page and
  // its full run sessions — never dashboard/workspace/models/...). The
  // browser edge template shares the same gate via body.browser-visible.
  let on = false;
  try {
    on = !!document.querySelector('.page[data-page="agent"].active')
      || !!document.querySelector('.page[data-page="schedrun"].active');
  } catch (_) {}
  try {
    document.body.classList.toggle("sched-visible", on);
    document.body.classList.toggle("browser-visible", on);
  } catch (_) {}
}

function showPage(name) {
  // Same activation the router uses (pages + titles + scroll mode).
  const titles = {
    agent: ["Agent", "Chat with your AI assistant"],
    schedrun: ["Scheduled Task", "Follow-up chat for one scheduled run"],
  };
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.classList.toggle("active", b.dataset.nav === (name === "schedrun" ? "agent" : name));
  });
  document.querySelectorAll(".page").forEach((p) => {
    p.classList.toggle("active", p.dataset.page === name);
  });
  const t = titles[name] || titles.agent;
  const h = document.getElementById("pageTitle");
  const s = document.getElementById("pageSub");
  if (h) h.textContent = t[0];
  if (s) s.textContent = t[1];
  const content = document.getElementById("content");
  if (content) {
    content.classList.toggle("no-scroll", name === "agent" || name === "schedrun");
    content.scrollTop = 0;
  }
  updateSchedVisibility();
}

function hideRunPage() {
  if (!runView) return;
  runView = null;
  showPage("agent");
}

function closeFloatingForRun() {
  // Entering a task session hides the floating Scheduled-Tasks window when
  // it is visible (parked alive, same as minimize — never destroyed).
  try {
    const mini = document.getElementById("scheduleMini");
    if (mini && !mini.classList.contains("mini-hidden")) schedDockToTab(mini.getBoundingClientRect().top);
  } catch (_) {}
}

async function renderRunPage(jobId, runIdx) {
  closeFloatingForRun();
  let detail;
  try {
    detail = await fetchDetail(jobId);
  } catch (_) {
    toast("Couldn't load the task");
    hideRunPage();
    try {
      history.replaceState(null, "", "/");
    } catch (_) {}
    return;
  }
  const hist = Array.isArray(detail.run_history) ? detail.run_history : [];
  const run = hist[runIdx] || hist[hist.length - 1] || null;
  const skill = detail.full_instructions || detail.skill_preview || detail.task || "";
  runView = { jobId, runIdx, skill, task: detail.task || detail.job_id };
  showPage("schedrun");
  // No task-name header (removed for mobile fit) — only the #RUN status chip.
  // Run header chip: #RUN<n> + status + run time (same session identity as the dialog).
  const bar = document.getElementById("schedRunBar");
  let chip = document.getElementById("schedRunChip");
  if (bar && !chip) {
    chip = document.createElement("span");
    chip.id = "schedRunChip";
    chip.className = "schedrun-status";
    bar.appendChild(chip);
  }
  if (chip) {
    const n = hist.length ? (hist.indexOf(run) + 1 || hist.length) : 0;
    const st = run ? (run.success ? "Success" : "Failed") : "Not executed";
    chip.textContent = n ? `#RUN${n} · ${st} · ${fmtWhen(run && run.at)}` : "Task session";
    chip.classList.toggle("is-ok", !!(run && run.success));
    chip.classList.toggle("is-fail", !!(run && !run.success));
  }
  const log = document.getElementById("schedRunLog");
  const scroll = document.getElementById("schedRunScroll");
  if (log) {
    log.innerHTML = "";
    // A new task session starts fresh: drop any stale edit banner.
    schedCancelEdit();
    const sess = Array.isArray(detail.session) ? detail.session : [];
    if (sess.length) {
      // The job's own branch session: every execution + follow-up lives here,
      // rendered exactly like the main session (rich text + action bars +
      // Working blocks with steps + elapsed).
      for (const m of sess) {
        const isAgent = m.role === "assistant";
        const el = bubble(isAgent ? "agent" : "user", m.content || "—", m.id,
          { steps: m.steps, elapsed: m.elapsed, images: m.images, audio: m.audio });
        if (isAgent) schedAttachAssistantBar(el, jobId);
        else schedAttachUserBar(el, jobId);
        log.appendChild(el);
      }
      // Jump to the previewed run's own reply (fallback: bottom).
      const anchor = run && run.assistant_msg
        ? log.querySelector(`[data-mid="${CSS.escape(run.assistant_msg)}"]`)
        : null;
      requestAnimationFrame(() => {
        try {
          if (anchor) {
            anchor.scrollIntoView({ block: "center" });
            anchor.classList.add("flash");
            setTimeout(() => anchor.classList.remove("flash"), 1600);
          } else {
            scroll?.scrollTo(0, scroll.scrollHeight);
          }
        } catch (_) {}
      });
    } else {
      // Legacy fallback: jobs executed before branch sessions existed.
      // The skill arrives as a normal user message (copyable/editable), the
      // run output as a normal agent reply with its own action bar.
      const skillEl = bubble("user", skill || "—");
      schedAttachUserBar(skillEl, jobId);
      log.appendChild(skillEl);
      const outEl = bubble("agent", (run && run.output) || (detail.executing ? "Running now…" : "No output yet"));
      schedAttachAssistantBar(outEl, jobId);
      log.appendChild(outEl);
    }
    // Code-copy buttons inside the history work like the main chat.
    log.querySelectorAll("[data-codecopy]").forEach((btn) => {
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const code = btn.closest(".codeblock")?.querySelector("code")?.textContent || "";
        schedCopyText(code);
      });
    });
  }
}

function openRunPage(jobId, runIdx) {
  const idx = Number.isFinite(Number(runIdx)) ? Number(runIdx) : 0;
  try {
    history.pushState({ schedrun: true }, "", `/#sched-${jobId}-${idx}`);
  } catch (_) {}
  renderRunPage(jobId, idx);
}

function handleHash() {
  // Deep-link / refresh on a run session re-opens the same full section.
  let m = null;
  try {
    m = HASH_RE.exec(location.hash || "");
  } catch (_) {}
  if (m) {
    renderRunPage(m[1], Number(m[2]));
  } else if (runView) {
    hideRunPage();
  }
}



/* ---------- push wiring ---------- */

function onSchedPush(data) {
  try {
    const list = data && Array.isArray(data.jobs) ? data.jobs : [];
    jobs = list.filter((j) => j && typeof j.job_id === "string");
    ensureTab();
    // Closed by default: never auto-pop. Only refresh the list when the
    // window already exists (open or parked) — never rebuild the DOM under
    // an active pull gesture (the release re-renders with fresh data).
    if (document.getElementById("scheduleMini") && !schedPull) {
      miniOpen = true;
      renderMini();
    }
  } catch (_) {}
}

async function fetchInitial() {
  try {
    const res = await fetch("/api/scheduler");
    if (!res.ok) return;
    const data = await res.json();
    onSchedPush(data);
    // Closed by default on first load: drop any restored node.
    const mini = document.getElementById("scheduleMini");
    if (mini) mini.remove();
    miniOpen = false;
  } catch (_) {}
}

let runMode = "restricted"; // permission mode of the run-page composer
let runPending = []; // [{type, name, dataUrl}] max 2, cleared after each send
let runRecorder = null;
let runRecChunks = [];
let runRecStream = null;

function readFileAsDataURL(file) {
  return new Promise((resolve, reject) => {
    try {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error("read failed"));
      reader.readAsDataURL(file);
    } catch (e) {
      reject(e);
    }
  });
}

function renderRunChips() {
  const box = document.getElementById("schedRunAttachments");
  if (!box) return;
  box.innerHTML = "";
  runPending.forEach((att, i) => {
    const chip = document.createElement("div");
    chip.className = "sched-att-chip";
    const label = document.createElement("span");
    label.textContent = att.name;
    label.dir = "auto";
    const x = document.createElement("button");
    x.type = "button";
    x.textContent = "✕";
    x.setAttribute("aria-label", "Remove attachment");
    x.addEventListener("click", () => {
      runPending.splice(i, 1);
      renderRunChips();
    });
    chip.appendChild(label);
    chip.appendChild(x);
    box.appendChild(chip);
  });
}

function bindRunFormOnce() {
  if (bindRunFormOnce.done) return;
  bindRunFormOnce.done = true;
  const form = document.getElementById("schedRunForm");
  const input = document.getElementById("schedRunInput");
  if (!form || !input) return;

  // Textarea autogrow (same feel as the main composer).
  input.addEventListener("input", () => {
    try {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 120) + "px";
    } catch (_) {}
  });

  // Attach menu (same template as the main composer).
  const attachBtn = document.getElementById("schedRunAttachBtn");
  const attachMenu = document.getElementById("schedRunAttachMenu");
  attachBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    document.getElementById("schedRunModeMenu")?.classList.remove("open");
    attachMenu?.classList.toggle("open");
  });
  document.addEventListener("click", (e) => {
    try {
      if (attachMenu?.classList.contains("open")
        && !attachMenu.contains(e.target) && e.target !== attachBtn
        && !attachBtn?.contains(e.target)) attachMenu.classList.remove("open");
      const mm = document.getElementById("schedRunModeMenu");
      const mb = document.getElementById("schedRunModeBtn");
      if (mm?.classList.contains("open") && !mm.contains(e.target)
        && !mb?.contains(e.target)) mm.classList.remove("open");
    } catch (_) {}
  });
  const pickers = {
    image: document.getElementById("schedRunFileImage"),
    video: document.getElementById("schedRunFileVideo"),
    document: document.getElementById("schedRunFileDocument"),
  };
  attachMenu?.querySelectorAll("button[data-attach]").forEach((btn) => {
    btn.addEventListener("click", () => {
      attachMenu.classList.remove("open");
      const kind = btn.getAttribute("data-attach");
      try {
        pickers[kind]?.click();
      } catch (_) {}
    });
  });
  Object.entries(pickers).forEach(([kind, picker]) => {
    picker?.addEventListener("change", async () => {
      const files = picker.files ? Array.from(picker.files).slice(0, 2) : [];
      picker.value = "";
      for (const file of files) {
        if (runPending.length >= 2) {
          toast("Max 2 attachments");
          break;
        }
        const limit = kind === "image" ? 8 * 1024 * 1024 : kind === "video" ? 24 * 1024 * 1024 : 100 * 1024 * 1024;
        if (file.size > limit) {
          toast("File too large");
          continue;
        }
        try {
          const dataUrl = await readFileAsDataURL(file);
          runPending.push({ type: kind, name: (file.name || "file").slice(0, 200), data: dataUrl });
        } catch (_) {
          toast("Couldn't read the file");
        }
      }
      renderRunChips();
    });
  });

  // Mode pill (same template: Restricted / Free).
  const modeBtn = document.getElementById("schedRunModeBtn");
  const modeMenu = document.getElementById("schedRunModeMenu");
  const modeLabel = document.getElementById("schedRunModeLabel");
  const paintMode = () => {
    if (modeLabel) modeLabel.textContent = runMode === "free" ? "Full access" : "Restricted";
    modeMenu?.querySelectorAll("button").forEach((b) => {
      b.classList.toggle("active", b.dataset.mode === runMode);
    });
  };
  if (modeMenu) {
    const ICONS = {
      restricted: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3.5 19 6v5.2c0 4.2-2.9 7.3-7 9.2-4.1-1.9-7-5-7-9.2V6l7-2.5Z"/><path d="m8.8 12 2 2 4.4-4.6"/></svg>`,
      free: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2.8 4.8 13.2h5.4L9 21.2l8.2-10.4h-5.4L13 2.8Z"/></svg>`,
    };
    modeMenu.innerHTML = `<div class="mode-head">Agent permission</div>`;
    [
      { val: "restricted", title: "Restricted", desc: "Ask before any terminal command (default)" },
      { val: "free", title: "Full access", desc: "Terminal runs without asking" },
    ].forEach((opt) => {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.mode = opt.val;
      b.className = "mode-opt";
      b.innerHTML = `<span class="mode-opt-icon" aria-hidden="true">${ICONS[opt.val]}</span>`
        + `<span><b></b><span class="desc"></span></span><span class="radio" aria-hidden="true"></span>`;
      b.querySelector("b").textContent = opt.title;
      b.querySelector(".desc").textContent = opt.desc;
      b.addEventListener("click", () => {
        runMode = b.dataset.mode === "free" ? "free" : "restricted";
        paintMode();
        modeMenu.classList.remove("open");
      });
      modeMenu.appendChild(b);
    });
  }
  paintMode();
  modeBtn?.addEventListener("click", (e) => {
    e.stopPropagation();
    attachMenu?.classList.remove("open");
    modeMenu?.classList.toggle("open");
  });

  // Mic: record → /api/stt → insert transcript (same backend as main chat).
  const micBtn = document.getElementById("schedRunMicBtn");
  micBtn?.addEventListener("click", async () => {
    try {
      if (runRecorder) {
        runRecorder.stop();
        return;
      }
      if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
        toast("Recording isn't supported here");
        return;
      }
      runRecStream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = ["audio/webm", "audio/mp4"].find((m) => { try { return MediaRecorder.isTypeSupported(m); } catch (_) { return false; } });
      runRecChunks = [];
      runRecorder = mime ? new MediaRecorder(runRecStream, { mimeType: mime }) : new MediaRecorder(runRecStream);
      runRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size) runRecChunks.push(e.data);
      };
      runRecorder.onstop = async () => {
        const rec = runRecorder;
        runRecorder = null;
        micBtn.classList.remove("recording");
        try {
          runRecStream?.getTracks().forEach((t) => t.stop());
        } catch (_) {}
        runRecStream = null;
        const blob = new Blob(runRecChunks, { type: rec.mimeType || "audio/webm" });
        runRecChunks = [];
        if (!blob.size) return;
        micBtn.disabled = true;
        try {
          const dataUrl = await new Promise((resolve, reject) => {
            const r = new FileReader();
            r.onload = () => resolve(r.result);
            r.onerror = () => reject(new Error("read failed"));
            r.readAsDataURL(blob);
          });
          const res = await fetch("/api/stt", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ audio: dataUrl }),
          });
          const data = await res.json().catch(() => ({}));
          const text = res.ok ? String(data.text || "").trim() : "";
          if (text) {
            input.value = (input.value ? input.value + " " : "") + text;
            input.dispatchEvent(new Event("input"));
            input.focus();
          } else {
            toast("No clear audio heard");
          }
        } catch (_) {
          toast("Voice transcription failed");
        } finally {
          micBtn.disabled = false;
        }
      };
      runRecorder.start();
      micBtn.classList.add("recording");
      toast("Recording… tap again to stop");
    } catch (_) {
      toast("Couldn't start recording");
      runRecorder = null;
      micBtn.classList.remove("recording");
    }
  });

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!runView || form.dataset.busy === "1") return;
    const msg = (input.value || "").trim();
    // Edit mode: SEND saves the edited text in place (then regenerates
    // below it for user edits) — same contract as the main composer.
    if (schedEditing) {
      if (!msg) {
        toast("Message is empty");
        return;
      }
      input.value = "";
      input.style.height = "auto";
      form.dataset.busy = "1";
      const sendBtn = document.getElementById("schedRunSend");
      if (sendBtn) sendBtn.disabled = true;
      try {
        await schedSaveEdit(runView.jobId, msg);
      } catch (err) {
        toast(err instanceof Error ? err.message : "Save failed");
      } finally {
        form.dataset.busy = "";
        if (sendBtn) sendBtn.disabled = false;
        if (input) {
          input.disabled = false;
          try { input.focus(); } catch (_) {}
        }
      }
      return;
    }
    if (!msg && !runPending.length) return;
    input.value = "";
    input.style.height = "auto";
    const atts = runPending.slice(0, 2);
    runPending = [];
    renderRunChips();
    // Normal send goes through the shared live-turn renderer (user bubble +
    // Working block + rich reply with steps + action bars).
    await schedRunTurn(runView.jobId, msg, atts);
  });
  document.getElementById("schedRunBack")?.addEventListener("click", () => {
    // Deterministic return to the main session: strip the run hash and show
    // the Agent page directly (history.back() is unreliable — on a deep link
    // it leaves the app, and router state may already be "agent").
    try {
      history.replaceState(null, "", location.pathname);
    } catch (_) {}
    hideRunPage();
  });
}

export function initSchedule() {
  onPush("scheduler", onSchedPush);
  bindRunFormOnce();
  // Hash session routing: open run page on deep-link/refresh, close on back.
  window.addEventListener("hashchange", handleHash);
  // Browser back/forward lands here too (after the router): keep views in sync.
  window.addEventListener("popstate", () => {
    try {
      if (!HASH_RE.exec(location.hash || "")) hideRunPage();
    } catch (_) {}
  });
  // Sidebar clicks while a run page is open: strip the session hash so the
  // URL never points at a hidden session (router owns the page switch).
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      if (!runView) return;
      // The router early-returns on same-page clicks, so leaving the run
      // page for Agent is done here; other pages are handled by x5:page-open.
      if (btn.dataset.nav === "agent") {
        try {
          history.replaceState(null, "", location.pathname);
        } catch (_) {}
        hideRunPage();
      }
    });
  });
  // Router switches (sidebar/back-forward) re-gate Agent-only visibility.
  // The router owns the page; here we only drop any stale session hash.
  window.addEventListener("x5:page-open", () => {
    try {
      if (HASH_RE.exec(location.hash || "")) {
        history.replaceState(null, "", location.pathname);
      }
    } catch (_) {}
    runView = null;
    updateSchedVisibility();
  });
  handleHash();
  updateSchedVisibility();
  if (!tickBound) {
    tickBound = true;
    setInterval(tickCountdowns, 1000);
  }
  // Returned for the app.js boot barrier (black splash waits on it).
  // Kept LAST: an early return above used to skip all wiring, leaving
  // body.sched-visible / body.browser-visible unset so both right-edge
  // templates stayed display:none forever.
  return fetchInitial();
}
