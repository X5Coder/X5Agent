/**
 * chat.js — X5Agent agent chat
 * Owns: real history (/api/conversation), send (/api/chat),
 * attach menu (image/video metadata), voice input, thinking + typing.
 */
"use strict";

import { openModal, closeModal, toast } from "./ui.js?v=13";
import { navigate } from "./navigation.js?v=15";
import { onPush } from "./events.js?v=7";
import { remoteLiveUrl } from "./live-url.js?v=2";

// POST helpers: browser -> server is plain POST only.
// Server -> browser streaming is SSE (the POST response body below,
// plus GET /api/events for pushes).
async function postAnswer(body) {
  const res = await fetch("/api/chat_answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const ok = res.ok;
  return { ok, status: res.status };
}
async function postCancel() {
  try {
    await fetch("/api/chat_cancel", { method: "POST" });
  } catch (_) {}
}

const MAX_MESSAGE = 4000;
/* no chat avatars — assistant messages are content-only */

let chatScroll = null;
let chatInner = null;
let composerInput = null;
let sendBtn = null;
let attachPreview = null;
let composerAttachBox = null;
let pendingAttachments = [];
let mentionMenu = null;
let pendingSkill = null; // {name, instructions} — picked via "/" menu, injected on send
let pendingFolderRefs = []; // [{path, name}] — picked via "@" menu, sent as metadata
let skillsCache = null; // {at, list} — short-lived GET /api/skills cache

let pendingQuestion = null;
// Unanswered agent question (ask/approval/needs_model) persisted across page
// refresh: the server keeps the same question id blocked (up to its timeout),
// so restoring the strip lets the user still answer it. Live handoffs are
// excluded (they restore through the live state instead).
const PENDING_Q_KEY = "x5:pending-question";
function storePendingQuestion(frame) {
  try {
    if (!frame || !frame.id || frame.kind === "live") return;
    if (!["ask", "approval", "needs_model"].includes(frame.kind || "ask")) return;
    localStorage.setItem(PENDING_Q_KEY, JSON.stringify({
      id: String(frame.id).slice(0, 16),
      kind: frame.kind || "ask",
      text: String(frame.text || frame.command || "").slice(0, 2000),
      choices: Array.isArray(frame.choices) ? frame.choices.slice(0, 8).map((c) => String(c).slice(0, 200)) : [],
      command: String(frame.command || "").slice(0, 500),
      needs_kind: String(frame.needs_kind || "text").slice(0, 20),
      tool: String(frame.tool || "").slice(0, 40),
    }));
  } catch (_) {}
}
function readPendingStored() {
  try {
    const raw = localStorage.getItem(PENDING_Q_KEY);
    if (!raw) return null;
    const q = JSON.parse(raw);
    if (!q || typeof q !== "object" || !q.id || q.kind === "live") return null;
    if (!["ask", "approval", "needs_model"].includes(q.kind)) return null;
    return { type: "question", ...q };
  } catch (_) {
    return null;
  }
}
function clearPendingStored() {
  try {
    localStorage.removeItem(PENDING_Q_KEY);
  } catch (_) {}
}
async function restorePendingQuestion() {
  // One-shot: if the user refreshes mid-question, the strip comes back
  // exactly as it was until they choose (or it expires server-side).
  // Validated: a stored strip alone never raises the template — it returns
  // only when a live run (or its pending question frame) still exists on
  // the server. Stale stores are dropped so refresh never shows a stuck
  // essay question.
  try {
    if (document.getElementById("questionCard")) return;
    if (!document.querySelector('.page[data-page="agent"].active')) return;
    // Server truth first: an actually-waiting question rebuilds the strip
    // with its live qid (answering resumes the paused loop) — even when the
    // frames buffer and the local store both missed it.
    try {
      const pq = await fetch("/api/pending_questions", { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
      const open = pq && Array.isArray(pq.questions) ? pq.questions.filter((x) => x && x.id) : [];
      if (open.length) {
        const q = open[0];
        showQuestionCard({ type: "question", id: String(q.id), kind: q.kind || "ask", text: String(q.text || q.command || ""), choices: Array.isArray(q.choices) ? q.choices : [], command: String(q.command || ""), needs_kind: String(q.needs_kind || "text"), tool: String(q.tool || "") });
        return;
      }
    } catch (_) {}
    const saved = readPendingStored();
    if (!saved || !saved.id) return;
    let live = false;
    try {
      live = isChatLive();
    } catch (_) {}
    if (!live) {
      try {
        const fd = await fetch("/api/chat_frames", { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
        if (fd && fd.active) live = true;
        if (!live && fd && Array.isArray(fd.frames)) {
          live = fd.frames.some((f) => f && f.type === "question" && String(f.id) === String(saved.id));
        }
      } catch (_) {}
    }
    if (!live) {
      try { clearPendingStored(); } catch (_) {}
      return;
    }
    showQuestionCard(saved);
  } catch (_) {}
}
let activeThinking = null;
let lastUserText = "";
let lastUserEl = null;
let activeRun = null;
// Run generation: every stream turn owns a token so a superseded turn
// (STOP / fresh answer / newer message) exits quietly — no error text,
// no blocking fallback, no state clobbering.
let runGen = 0;
// Continue reentrancy: exactly one resume at a time — a double-tap must
// never start the same stopped task twice (single-task invariant).
let continueBusy = false;
// STOP generation: bumped only by explicit STOP/abort, never by stream
// attempts — a 409-retry sleeping through a STOP must never auto-start.
let stopGen = 0;
// Refresh-mid-run guard: true from script load until resumeActiveRun settles.
// Section/history renders must not wipe or duplicate the live run while set.
let resumePending = true;
let resumeSettled = false;
// Full pending user text across refresh (preview from /api/chat_frames is
// only 200 chars and prefix-matching it creates empty/duplicate bubbles).
const PENDING_USER_KEY = "x5:pending-user";
function storePendingUser(text) {
  try {
    const v = String(text || "").slice(0, 4000);
    if (v.trim()) localStorage.setItem(PENDING_USER_KEY, v);
  } catch (_) {}
}
function readPendingUser() {
  try {
    const v = localStorage.getItem(PENDING_USER_KEY);
    return typeof v === "string" ? v.slice(0, 4000) : "";
  } catch (_) { return ""; }
}
function clearPendingUser() {
  try { localStorage.removeItem(PENDING_USER_KEY); } catch (_) {}
}
function enforceSingleLiveBlock(keepEl) {
  // Protection: at most ONE live Working stays open. Any other live-looking
  // block (a second #thinkingMsg, or a history block left open) is settled
  // (wave stopped + collapsed) so refresh can never show two runnings.
  // Never throws, never touches the kept live element.
  try {
    if (!chatInner) return;
    chatInner.querySelectorAll(".msg.assistant").forEach((m) => {
      try {
        if (keepEl && (m === keepEl)) return;
        if (m.id === "thinkingMsg" && m !== keepEl) m.removeAttribute("id");
        const b = (m._block) || m.querySelector(":scope > .msg-content > .thinking-block, :scope > .thinking-block");
        if (!b || b === (keepEl && keepEl._block)) return;
        if (!b.classList.contains("done")) settleThinkingBlock(b);
        b.classList.remove("open");
      } catch (_) {}
    });
  } catch (_) {}
}

function isNearBottom(margin = 320) {
  // Same 320px threshold as the scroll-to-latest button: inside it the user
  // is "following" the run, outside it they scrolled up to read freely.
  try {
    if (!chatScroll) return true;
    return chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight <= margin;
  } catch (_) {
    return true;
  }
}

function scrollToBottom(smooth = true, force = false) {
  // Sticky auto-scroll: while the run streams/animates we follow ONLY when
  // the user is already near the bottom. Scrolled up = free (the glass
  // scroll-to-latest button takes them down on tap). Explicit callers
  // (user send, scroll button, history init) pass force=true. Never throws.
  if (!chatScroll) return;
  try {
    if (!force && !isNearBottom()) return;
    chatScroll.scrollTo({ top: chatScroll.scrollHeight, behavior: smooth ? "smooth" : "auto" });
  } catch (_) {
    try {
      if (force || isNearBottom()) chatScroll.scrollTop = chatScroll.scrollHeight;
    } catch (_) {}
  }
}

function escapeHtml(text) {
  return String(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function humanizeEvent(text) {
  // Never show raw storage IDs — friendly names only.
  const t = String(text || "");
  if (/^analyzing\s*·/i.test(t)) return "Analyzing · Your Image";
  if (/^analyzing video\s*·/i.test(t)) return "Analyzing video · Your Video";
  if (/^transcribing video\s*·/i.test(t)) return "Transcribing video · Your Video";
  if (/^checking (video job|transcribe job)\s*·/i.test(t)) return t.split("·")[0].trim();
  return t;
}

function isRtlStrong(ch) {
  // Numeric code-point ranges (no literal glyphs): Hebrew, Arabic blocks,
  // Arabic Supplement/Extended-A, Presentation Forms A/B.
  const c = String(ch || "").codePointAt(0) || 0;
  return (c >= 0x0590 && c <= 0x05ff) || (c >= 0x0600 && c <= 0x06ff) ||
    (c >= 0x0750 && c <= 0x077f) || (c >= 0x08a0 && c <= 0x08ff) ||
    (c >= 0xfb50 && c <= 0xfdff) || (c >= 0xfe70 && c <= 0xfeff);
}

function isLtrStrong(ch) {
  // Basic Latin + Latin/Greek/Cyrillic Extended (numeric ranges only).
  const c = String(ch || "").codePointAt(0) || 0;
  if ((c >= 65 && c <= 90) || (c >= 97 && c <= 122)) return true;
  return (c >= 0x00c0 && c <= 0x024f) || (c >= 0x0370 && c <= 0x03ff) ||
    (c >= 0x0400 && c <= 0x04ff) || (c >= 0x1e00 && c <= 0x1eff);
}

function autoDir(content) {
  // Explicit direction for one rich block (first-strong-char rule), so list
  // markers and table cells sit on the right side from the very first typed
  // frame — an empty dir=auto node would default to LTR (bullet on the
  // left, cell text jumping sides). Tags/entities are skipped so markup
  // never fakes the direction.
  try {
    const text = String(content || "")
      .replace(/<[^>]*>/g, "")
      .replace(/&[A-Za-z0-9#]+;/g, "");
    for (const ch of text) {
      if (isRtlStrong(ch)) return "rtl";
      if (isLtrStrong(ch)) return "ltr";
    }
  } catch (_) {}
  return "auto";
}

const CODE_LANG_NAMES = {
  js: "JavaScript", javascript: "JavaScript", ts: "TypeScript", typescript: "TypeScript",
  py: "Python", python: "Python", html: "HTML", xml: "XML", css: "CSS", json: "JSON",
  yaml: "YAML", yml: "YAML", sh: "Shell", bash: "Shell", shell: "Shell", sql: "SQL",
  java: "Java", c: "C", cpp: "C++", "c++": "C++", cs: "C#", "c#": "C#", go: "Go",
  rust: "Rust", php: "PHP", rb: "Ruby", ruby: "Ruby", swift: "Swift", kotlin: "Kotlin",
  md: "Markdown", markdown: "Markdown", txt: "Text", text: "Text",
};

function codeLangOf(tag) {
  // Display name for a fence tag (bounded). Unknown tags show as-is.
  try {
    const raw = String(tag || "").trim().slice(0, 16);
    const t = raw.toLowerCase();
    if (CODE_LANG_NAMES[t]) return { key: t, label: CODE_LANG_NAMES[t] };
    if (!t) return { key: "", label: "code" };
    return { key: t, label: raw };
  } catch (_) {
    return { key: "", label: "code" };
  }
}

function codeFamily(key) {
  // One small grammar per family; everything else shares the C-like one.
  if (!key) return "code";
  if (key === "py" || key === "python" || key === "rb" || key === "ruby") return "python";
  if (key === "html" || key === "xml") return "markup";
  if (key === "css") return "css";
  if (key === "sh" || key === "bash" || key === "shell") return "shell";
  if (key === "json" || key === "yaml" || key === "yml") return "data";
  if (key === "md" || key === "markdown" || key === "txt" || key === "text") return "plain";
  return "code";
}

const KW_CODE = new Set("break case catch class const continue debugger default delete do else export extends false finally for function if import in instanceof new null return super switch this throw true try typeof var void while with yield async await static get set of from as let enum implements interface package private protected public".split(" "));
const KW_PY = new Set("False None True and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield".split(" "));
const KW_SHELL = new Set("if then else elif fi for while until do done case esac function select time in".split(" "));

function runCodeTokens(s, re, classify) {
  // Walk one grammar over the source, escaping every piece. Never throws.
  const out = [];
  let last = 0;
  let m = null;
  try {
    re.lastIndex = 0;
    while ((m = re.exec(s)) !== null) {
      if (m.index > last) out.push(escapeHtml(s.slice(last, m.index)));
      let cls = "";
      try {
        cls = classify(m, s, re.lastIndex) || "";
      } catch (_) {
        cls = "";
      }
      out.push(cls ? `<span class="${cls}">${escapeHtml(m[0])}</span>` : escapeHtml(m[0]));
      last = re.lastIndex;
      if (!m[0].length) re.lastIndex += 1;
    }
  } catch (_) {}
  try {
    if (last < s.length) out.push(escapeHtml(s.slice(last)));
  } catch (_) {}
  return out.join("");
}

function highlightCode(src, family) {
  // Tiny dependency-free highlighter (comments/strings/numbers/keywords +
  // function names). Bounded: oversized blocks stay plain text. Never throws.
  try {
    const s = String(src ?? "");
    if (!s || s.length > 20000 || family === "plain") return escapeHtml(s);
    const isFnCall = (rest) => /^\s*\(/.test(rest);
    if (family === "python") {
      const re = /("""[\s\S]*?(?:"""|$)|'''[\s\S]*?(?:'''|$))|('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")|(#[^\n]*)|(\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|([A-Za-z_]\w*)/g;
      return runCodeTokens(s, re, (m, full, idx) => {
        if (m[1] || m[2]) return "tok-str";
        if (m[3]) return "tok-com";
        if (m[4]) return "tok-num";
        if (m[5]) return KW_PY.has(m[5]) ? "tok-kw" : (isFnCall(full.slice(idx)) ? "tok-fn" : "");
        return "";
      });
    }
    if (family === "shell") {
      const re = /(#[^\n]*)|('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*"|`(?:[^`\\]|\\.)*`)|(--?[A-Za-z][\w-]*)|(\b\d+(?:\.\d+)?\b)|(\$[\w#?$!*]+)|([A-Za-z_]\w*)/g;
      return runCodeTokens(s, re, (m) => {
        if (m[1]) return "tok-com";
        if (m[2] || m[5]) return "tok-str";
        if (m[3]) return "tok-attr";
        if (m[4]) return "tok-num";
        if (m[6]) return KW_SHELL.has(m[6]) ? "tok-kw" : "";
        return "";
      });
    }
    if (family === "markup") {
      const re = /(<!--[\s\S]*?(?:-->|$))|(<\/?[A-Za-z][\w:.-]*|\/?>)|([A-Za-z_:][\w:.-]*(?=\s*=\s*["']))|('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")/g;
      return runCodeTokens(s, re, (m) => {
        if (m[1]) return "tok-com";
        if (m[2]) return "tok-tag";
        if (m[3]) return "tok-attr";
        if (m[4]) return "tok-str";
        return "";
      });
    }
    if (family === "css") {
      const re = /(\/\*[\s\S]*?(?:\*\/|$))|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(@[\w-]+)|(#[0-9a-fA-F]{3,8}\b)|(\b\d+(?:\.\d+)?(?:px|em|rem|%|s|ms|vh|vw|ch|fr|deg)?\b)|([A-Za-z-]+(?=\s*:))/g;
      return runCodeTokens(s, re, (m) => {
        if (m[1]) return "tok-com";
        if (m[2]) return "tok-str";
        if (m[3]) return "tok-kw";
        if (m[4] || m[5]) return "tok-num";
        if (m[6]) return "tok-attr";
        return "";
      });
    }
    if (family === "data") {
      const re = /("(?:[^"\\\n]|\\.)*"(?=\s*:))|("(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*')|(\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|(\btrue\b|\bfalse\b|\bnull\b)/g;
      return runCodeTokens(s, re, (m) => {
        if (m[1]) return "tok-attr";
        if (m[2]) return "tok-str";
        if (m[3]) return "tok-num";
        if (m[4]) return "tok-kw";
        return "";
      });
    }
    const re = /(\/\*[\s\S]*?(?:\*\/|$))|(\/\/[^\n]*)|(`(?:[^`\\]|\\.)*`?|'(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")|(\b0[xX][0-9a-fA-F]+\b|\b\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b)|([A-Za-z_$][\w$]*)/g;
    return runCodeTokens(s, re, (m, full, idx) => {
      if (m[1] || m[2]) return "tok-com";
      if (m[3]) return "tok-str";
      if (m[4]) return "tok-num";
      if (m[5]) return KW_CODE.has(m[5]) ? "tok-kw" : (isFnCall(full.slice(idx)) ? "tok-fn" : "");
      return "";
    });
  } catch (_) {
    try {
      return escapeHtml(String(src ?? ""));
    } catch (_) {
      return "";
    }
  }
}

const COPY_SVG = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>';

  // WhatsApp-style voice record bubble (agent TTS output)
const MV_PLAY_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M8 5.5v13a1 1 0 0 0 1.53.85l10.2-6.5a1 1 0 0 0 0-1.7L9.53 4.65A1 1 0 0 0 8 5.5z"/></svg>';
const MV_PAUSE_SVG = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6.2" y="5" width="4" height="14" rx="1.4"/><rect x="13.8" y="5" width="4" height="14" rx="1.4"/></svg>';
const MV_BARS = 34;
let mvPlaying = null;

function mvStopOther(current) {
  // One voice record plays at a time - starting a new one pauses the rest.
  try {
    if (mvPlaying && mvPlaying !== current) mvPlaying.pause();
  } catch (_) {}
  try {
    document.querySelectorAll(".msg-voice").forEach((b) => {
      if (b !== current && typeof b._mvSetPlaying === "function") b._mvSetPlaying(false);
    });
  } catch (_) {}
}

function mvHashBars(seed, n) {
  // Deterministic bar heights per record so the shape is stable live and
  // after refresh (no real waveform fetch needed). Never throws.
  const out = [];
  try {
    let h = 2166136261;
    const s = String(seed || "v");
    for (let i = 0; i < s.length; i++) {
      h ^= s.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    for (let i = 0; i < n; i++) {
      h ^= h << 13;
      h = Math.imul(h, 0x5bd1e995);
      h ^= h >>> 15;
      const v = (h >>> 0) / 4294967295;
      // Tall middle, tapered ends (voice-note look), min height visible.
      const taper = Math.sin((Math.PI * (i + 0.5)) / n) * 0.65 + 0.35;
      out.push(Math.max(0.14, Math.min(1, (0.25 + v * 0.75) * taper)));
    }
  } catch (_) {
    for (let i = 0; i < n; i++) out.push(0.4);
  }
  return out;
}

function mvFormat(sec) {
  try {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  } catch (_) {
    return "0:00";
  }
}

function mvUrl(name) {
  return "/api/audio?path=" + encodeURIComponent(name);
}

function mvSetPlayingVisual(box, btn, playing) {
  try {
    box.classList.toggle("playing", !!playing);
    btn.innerHTML = playing ? MV_PAUSE_SVG : MV_PLAY_SVG;
    btn.setAttribute("aria-label", playing ? "Pause voice message" : "Play voice message");
    btn.setAttribute("aria-pressed", playing ? "true" : "false");
  } catch (_) {}
}

function createVoiceBubble(name, durationSec) {
  // Standalone record bubble: round play button + wave bars that fill as it
  // plays + duration label. Click/drag on the wave seeks. Never throws.
  const box = document.createElement("div");
  box.className = "msg-voice";
  box.setAttribute("dir", "ltr");
  try {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mv-play";
    btn.setAttribute("aria-label", "Play voice message");
    btn.setAttribute("aria-pressed", "false");
    btn.innerHTML = MV_PLAY_SVG;

    const wave = document.createElement("div");
    wave.className = "mv-wave";
    wave.setAttribute("role", "slider");
    wave.setAttribute("aria-label", "Voice message progress");
    wave.setAttribute("aria-valuemin", "0");
    wave.setAttribute("aria-valuemax", "100");
    wave.setAttribute("aria-valuenow", "0");
    wave.tabIndex = 0;
    const heights = mvHashBars(name, MV_BARS);
    const bars = heights.map((hgt) => {
      const b = document.createElement("span");
      b.className = "mv-bar";
      b.style.transform = `scaleY(${hgt.toFixed(3)})`;
      wave.appendChild(b);
      return b;
    });

    const time = document.createElement("span");
    time.className = "mv-time";
    time.textContent = mvFormat(durationSec);

    const audioEl = document.createElement("audio");
    audioEl.preload = "metadata";
    audioEl.src = mvUrl(name);
    box.appendChild(audioEl);
    if (!audioEl.isConnected) {
      try {
        audioEl.load();
      } catch (_) {}
    }

    const setPlaying = (playing) => mvSetPlayingVisual(box, btn, playing);
    box._mvSetPlaying = setPlaying;

    const paint = (frac) => {
      try {
        const cut = Math.max(0, Math.min(1, Number(frac) || 0));
        const lit = Math.floor(cut * bars.length);
        bars.forEach((b, i) => b.classList.toggle("on", i < lit));
        wave.setAttribute("aria-valuenow", String(Math.round(cut * 100)));
      } catch (_) {}
    };

    const total = () => {
      try {
        const d = Number(audioEl.duration);
        if (Number.isFinite(d) && d > 0) return d;
      } catch (_) {}
      const f = Number(durationSec);
      return Number.isFinite(f) && f > 0 ? f : 0;
    };

    const tick = () => {
      try {
        const d = total();
        time.textContent = audioEl.paused && audioEl.currentTime <= 0
          ? mvFormat(d)
          : `${mvFormat(audioEl.currentTime)} / ${mvFormat(d)}`;
        paint(d > 0 ? audioEl.currentTime / d : 0);
      } catch (_) {}
    };

    const seekToFrac = (clientX) => {
      try {
        const r = wave.getBoundingClientRect();
        if (!r || r.width <= 0) return;
        const frac = Math.max(0, Math.min(1, (clientX - r.left) / r.width));
        const d = total();
        if (d > 0) audioEl.currentTime = frac * d;
        paint(frac);
      } catch (_) {}
    };

    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      try {
        if (audioEl.paused) {
          mvStopOther(box);
          audioEl.play().catch(() => setPlaying(false));
        } else audioEl.pause();
      } catch (_) {}
    });
    audioEl.addEventListener("play", () => {
      mvPlaying = audioEl;
      setPlaying(true);
      tick();
    });
    audioEl.addEventListener("pause", () => {
      if (mvPlaying === audioEl) mvPlaying = null;
      setPlaying(false);
      tick();
    });
    audioEl.addEventListener("timeupdate", tick);
    audioEl.addEventListener("loadedmetadata", tick);
    audioEl.addEventListener("ended", () => {
      try {
        audioEl.currentTime = 0;
      } catch (_) {}
      setPlaying(false);
      tick();
    });
    audioEl.addEventListener("error", () => {
      box.classList.add("broken");
      btn.disabled = true;
      time.textContent = "failed";
    }, { once: true });

    let dragging = false;
    wave.addEventListener("pointerdown", (e) => {
      dragging = true;
      try {
        wave.setPointerCapture(e.pointerId);
      } catch (_) {}
      seekToFrac(e.clientX);
    });
    wave.addEventListener("pointermove", (e) => {
      if (dragging) seekToFrac(e.clientX);
    });
    wave.addEventListener("pointerup", () => { dragging = false; });
    wave.addEventListener("pointercancel", () => { dragging = false; });
    wave.addEventListener("keydown", (e) => {
      try {
        const d = total();
        if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
          e.preventDefault();
          const step = e.key === "ArrowRight" ? 2 : -2;
          if (d > 0) audioEl.currentTime = Math.max(0, Math.min(d, audioEl.currentTime + step));
          tick();
        } else if (e.key === " " || e.key === "Enter") {
          e.preventDefault();
          btn.click();
        }
      } catch (_) {}
    });

    box.appendChild(btn);
    box.appendChild(wave);
    box.appendChild(time);
    box.appendChild(audioEl);
  } catch (_) {}
  return box;
}

function appendFinalVoice(el, audioList) {
  // Voice records spoken via text_to_speech, shown as standalone bubbles
  // under the reply (live and after refresh). Accepts the engine's
  // [{audio, duration, voice, lang}] items (or bare filename strings).
  const list = (Array.isArray(audioList) ? audioList : [])
    .map((item) => {
      if (typeof item === "string") return { audio: item, duration: 0 };
      if (item && typeof item.audio === "string") {
        return { audio: item.audio, duration: Number(item.duration) || 0 };
      }
      return null;
    })
    .filter((v) => v && v.audio)
    .slice(0, 4);
  if (!el || !list.length) return;
  const content = el.querySelector(".msg-content");
  if (!content || content.querySelector(":scope > .msg-final-voice")) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-final-voice";
  list.forEach((v) => wrap.appendChild(createVoiceBubble(v.audio, v.duration)));
  content.appendChild(wrap);
  scrollToBottom();
}

function fileDownloadUrl(rel, preview) {
  // Working-folder file URL (server-validated). preview=1 renders inline.
  try {
    return "/api/folder/download?path=" + encodeURIComponent(String(rel || "")) + (preview ? "&preview=1" : "");
  } catch (_) {
    return "";
  }
}

function formatFileSize(bytes) {
  try {
    const n = Number(bytes) || 0;
    if (n < 1024) return `${n} B`;
    if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
    return `${(n / 1048576).toFixed(1)} MB`;
  } catch (_) {
    return "";
  }
}

function openPdfPreview(file) {
  // Designed PDF card preview: the embedded file fills the screen, plus one
  // general download for the whole file. Slide images live INSIDE the PDF
  // itself (embedded at build time) — never listed separately. Never throws.
  try {
    const name = String(file?.name || "file");
    const url = fileDownloadUrl(file?.path, true);
    const dl = fileDownloadUrl(file?.path, false);
    openModal(`
    <div class="pdf-preview-dialog">
    <div class="modal-head">
      <div class="modal-head-text"><h3>${name.replace(/</g, "&lt;")}</h3><p>PDF preview — full screen</p></div>
      <button class="modal-close" data-close type="button" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
      </button>
    </div>
    <div class="modal-body">
      <iframe class="pdf-preview-frame" src="${url}" title="PDF preview"></iframe>
    </div>
    <div class="modal-foot">
      <button class="btn" data-close type="button">Close</button>
      <a class="btn btn-primary" href="${dl}" download>Download PDF</a>
    </div>
    </div>`);
  } catch (_) {}
}

function createFileCard(file) {
  const card = document.createElement("div");
  card.className = "msg-file";
  try {
    const icon = document.createElement("div");
    icon.className = "msg-file-icon";
    icon.textContent = "PDF";
    const meta = document.createElement("div");
    meta.className = "msg-file-meta";
    const title = document.createElement("div");
    title.className = "msg-file-name";
    title.setAttribute("dir", "auto");
    title.textContent = String(file?.name || "file");
    const sub = document.createElement("div");
    sub.className = "msg-file-sub";
    const bits = [];
    if (Number(file?.pages) > 0) bits.push(`${Number(file.pages)} pages`);
    if (file?.size) bits.push(formatFileSize(file.size));
    if (Array.isArray(file?.images) && file.images.length) bits.push(`${file.images.length} images`);
    sub.textContent = bits.join(" · ") || "PDF file";
    meta.appendChild(title);
    meta.appendChild(sub);
    const actions = document.createElement("div");
    actions.className = "msg-file-actions";
    const preview = document.createElement("button");
    preview.type = "button";
    preview.className = "needs-model-btn glass";
    preview.textContent = "Preview";
    preview.addEventListener("click", () => openPdfPreview(file));
    const dl = document.createElement("a");
    dl.className = "needs-model-btn glass";
    dl.textContent = "Download";
    dl.href = fileDownloadUrl(file?.path, false);
    dl.setAttribute("download", "");
    actions.appendChild(preview);
    actions.appendChild(dl);
    card.appendChild(icon);
    card.appendChild(meta);
    card.appendChild(actions);
    card.addEventListener("click", (e) => {
      try {
        if (e.target && e.target.closest && e.target.closest("a,button")) return;
        openPdfPreview(file);
      } catch (_) {}
    });
  } catch (_) {}
  return card;
}

function appendFinalFiles(el, files) {
  // Working-folder files produced this turn (studio pages): a designed file
  // card under the reply — click opens the preview, with per-image and
  // whole-file downloads. Live and after refresh. Never throws.
  try {
    const list = (Array.isArray(files) ? files : []).filter(
      (f) => f && typeof f.path === "string" && f.path
    ).slice(0, 4);
    if (!el || !list.length) return;
    const content = el.querySelector(".msg-content");
    if (!content || content.querySelector(":scope > .msg-final-files")) return;
    const wrap = document.createElement("div");
    wrap.className = "msg-final-files";
    list.forEach((f) => wrap.appendChild(createFileCard({
      name: String(f.name || "file").slice(0, 100),
      path: String(f.path).slice(0, 300),
      size: Number(f.size) || 0,
      pages: Number(f.pages) || 0,
      images: Array.isArray(f.images) ? f.images.slice(0, 20) : [],
    })));
    content.appendChild(wrap);
    scrollToBottom();
  } catch (_) {}
}

function codeblockShell(langTag, rawCode) {
  // Professional code card: fixed header (language + copy) over expanded,
  // highlighted, LTR code. Copy reads textContent (spans + entities resolve).
  try {
    const { key, label } = codeLangOf(langTag);
    const code = highlightCode(rawCode, codeFamily(key));
    return `<div class="codeblock" dir="ltr"><div class="code-head"><span class="code-lang">${escapeHtml(label)}</span><button type="button" class="code-copy" aria-label="Copy code">${COPY_SVG}<span>Copy</span></button></div><pre><code>${code}</code></pre></div>`;
  } catch (_) {
    try {
      return `<pre dir="ltr"><code>${escapeHtml(String(rawCode ?? ""))}</code></pre>`;
    } catch (_) {
      return "";
    }
  }
}

async function copyCodeBlock(btn) {
  // Copy one code card (clipboard API, textarea fallback for http).
  try {
    const block = btn && btn.closest ? btn.closest(".codeblock") : null;
    const codeEl = block ? block.querySelector("pre code") : null;
    const text = codeEl ? codeEl.textContent || "" : "";
    if (!text) {
      toast("Nothing to copy");
      return;
    }
    let ok = false;
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        await navigator.clipboard.writeText(text);
        ok = true;
      }
    } catch (_) {
      ok = false;
    }
    if (!ok) {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.setAttribute("readonly", "");
      ta.style.position = "fixed";
      ta.style.opacity = "0";
      document.body.appendChild(ta);
      ta.select();
      try {
        ok = document.execCommand("copy");
      } catch (_) {
        ok = false;
      }
      ta.remove();
    }
    const label = btn.querySelector("span");
    if (ok) {
      if (label) label.textContent = "Copied";
      btn.classList.add("copied");
      toast("Copied");
      setTimeout(() => {
        try {
          if (label) label.textContent = "Copy";
          btn.classList.remove("copied");
        } catch (_) {}
      }, 1400);
    } else {
      toast("Copy failed");
    }
  } catch (_) {
    try {
      toast("Copy failed");
    } catch (_) {}
  }
}

function formatRich(text) {
  // Global-AI style formatting: code blocks, bold, italic, code, lists. dir=auto per block.
  const chunks = [];
  // Fences are extracted from the RAW text first (placeholders survive the
  // later escaping); the code shell escapes + highlights internally.
  let src = String(text ?? "");
  src = src.replace(/```([^`\n]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    chunks.push(codeblockShell((lang || "").trim(), code.replace(/^\n+|\s+$/g, "")));
    return `%%X5CODE${chunks.length - 1}%%`;
  });
  let esc = escapeHtml(src);
  esc = esc.replace(/`([^`\n]+?)`/g, "<code>$1</code>");
  esc = esc.replace(/\*\*([^*]+?)\*\*/g, "<b>$1</b>");
  esc = esc.replace(/(^|[^*\w])\*([^*\n]+?)\*/g, "$1<i>$2</i>");
  esc = esc.replace(/~~([^~]+?)~~/g, "<del>$1</del>");
  const lines = esc.split("\n");
  let html = "";
  let list = null;
  const flush = () => {
    if (list) {
      html += `</${list}>`;
      list = null;
    }
  };
  const isPipeRow = (l) => /^\s*\|.*\|\s*$/.test(l);
  const isSepRow = (l) => /^\s*\|[\s:|-]+\|\s*$/.test(l);
  const splitCells = (l) =>
    l
      .trim()
      .replace(/^\||\|$/g, "")
      .split("|")
      .map((c) => c.trim());
  for (let li = 0; li < lines.length; li++) {
    const line = lines[li];
    const ul = line.match(/^\s*[-*]\s+(.*)$/);
    const ol = line.match(/^\s*\d+[.)]\s+(.*)$/);
    const h = line.match(/^\s*#{1,4}\s+(.*)$/);
    const quote = line.match(/^\s*>\s?(.*)$/);
    const codeRef = line.trim().match(/^%%X5CODE(\d+)%%$/);
    if (isPipeRow(line) && li + 1 < lines.length && isSepRow(lines[li + 1])) {
      flush();
      const head = splitCells(line);
      let rowsHtml = "";
      li += 2;
      while (li < lines.length && isPipeRow(lines[li])) {
        const cells = splitCells(lines[li]);
        // First body column is the row header (spec/label column): semantic
        // th + distinct styling + its own divider, so it never looks merged.
        rowsHtml += `<tr>${cells.map((c, idx) => (idx === 0 && cells.length > 1
          ? `<th scope="row" dir="${autoDir(c)}">${c}</th>`
          : `<td dir="${autoDir(c)}">${c}</td>`)).join("")}</tr>`;
        li++;
      }
      li--;
      // Explicit table direction from the header language (never auto): an
      // auto table can stay LTR while typing and strand an Arabic table
      // backwards — explicit dir pins the spec column right in RTL Arabic,
      // left in English, from the first frame.
      html += `<table dir="${autoDir(head.join(" "))}"><thead><tr>${head.map((c) => `<th dir="${autoDir(c)}">${c}</th>`).join("")}</tr></thead><tbody>${rowsHtml}</tbody></table>`;
    } else if (ul) {
      if (list !== "ul") {
        flush();
        html += "<ul>";
        list = "ul";
      }
      html += `<li dir="${autoDir(ul[1])}">${ul[1]}</li>`;
    } else if (ol) {
      if (list !== "ol") {
        flush();
        html += "<ol>";
        list = "ol";
      }
      html += `<li dir="${autoDir(ol[1])}">${ol[1]}</li>`;
    } else {
      flush();
      if (codeRef && chunks[Number(codeRef[1])] !== undefined) {
        html += chunks[Number(codeRef[1])];
      } else if (line.trim() === "") {
        continue;
      } else if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) {
        html += "<hr>";
      } else if (h) {
        html += `<p class="md-h" dir="auto"><b>${h[1]}</b></p>`;
      } else if (quote) {
        html += `<blockquote><p dir="auto">${quote[1]}</p></blockquote>`;
      } else {
        html += `<p dir="auto">${line}</p>`;
      }
    }
  }
  flush();
  // leftover inline placeholders (rare) restore as-is
  html = html.replace(/%%X5CODE(\d+)%%/g, (m, i) => chunks[Number(i)] || "");
  return html;
}

/* ---------- clickable working-folder file links in assistant replies ----------
   Explicit format ONLY: [label](folder:path/file.ext) — nothing else
   becomes a link. Plain sentences (even with a slash) stay plain text and
   never jump to the folder page. The button shows the label and opens the
   file exactly at its path (same reveal flow as @file tokens). */
function normalizeFolderPath(raw) {
  // Validated folder-relative path or null. Never throws.
  try {
    let rel = String(raw || "").replace(/\\/g, "/").trim();
    if (!rel) return null;
    rel = rel.replace(/^\/+/, "");
    rel = rel.replace(/^\/+/, "");
    if (!rel || rel.length > 500) return null;
    const parts = rel.split("/").filter(Boolean);
    if (!parts.length) return null;
    if (parts.some((p) => p === ".." || p === ".")) return null;
    if (parts.some((p) => p.length > 100)) return null;
    if (/[<>:"|?*\x00-\x1f]/.test(rel)) return null;
    if (rel.includes("://") || rel.includes("@")) return null; // URLs, emails
    if (parts.length === 1 && !parts[0].includes(".")) return null; // a bare word is not a file
    if (parts.every((p) => /^\d+$/.test(p))) return null; // dates, not paths
    return parts.join("/");
  } catch (_) {
    return null;
  }
}

function linkifyFolderPaths(root) {
  // Explicit [label](folder:path) only — no auto-linking of plain text.
  // Text nodes only — code blocks, links and buttons untouched.
  try {
    if (!root || !root.querySelectorAll) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
      acceptNode(node) {
        try {
          const pe = node.parentElement;
          if (pe && pe.closest && pe.closest("pre,code,a,button,.codeblock,.ws-path-link")) return NodeFilter.FILTER_REJECT;
          const v = node.nodeValue || "";
          if (!/folder:/i.test(v)) return NodeFilter.FILTER_REJECT;
        } catch (_) {
          return NodeFilter.FILTER_REJECT;
        }
        return NodeFilter.FILTER_ACCEPT;
      },
    });
    const nodes = [];
    while (walker.nextNode()) nodes.push(walker.currentNode);
    const FOLDER_LINK_RE = /\[([^\]\n]{1,120})\]\(folder:([^)]{1,500})\)/gi;
    nodes.forEach((node) => {
      try {
        const text = node.nodeValue || "";
        FOLDER_LINK_RE.lastIndex = 0;
        let m;
        let last = 0;
        let hit = false;
        const frag = document.createDocumentFragment();
        while ((m = FOLDER_LINK_RE.exec(text)) !== null) {
          const label = (m[1] || "").trim();
          const rawPath = (m[2] || "").trim();
          if (!label || !rawPath) continue;
          const norm = normalizeFolderPath(rawPath);
          if (!norm) continue;
          const start = m.index;
          const end = m.index + m[0].length;
          if (start > last) frag.appendChild(document.createTextNode(text.slice(last, start)));
          const btn = document.createElement("button");
          btn.type = "button";
          btn.className = "ws-path-link";
          btn.setAttribute("data-ws-path", norm);
          btn.setAttribute("dir", "auto");
          btn.title = "Open in the working folder";
          btn.textContent = label;
          frag.appendChild(btn);
          last = end;
          hit = true;
        }
        if (!hit) return;
        if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
        node.parentNode.replaceChild(frag, node);
      } catch (_) {}
    });
  } catch (_) {}
}

function hideWelcome() {
  document.getElementById("welcomeMsg")?.remove();
}

/* ---------- per-message direction: Arabic right, English left (both roles) ----------
   Frozen once at stream start so it never flips after finishing. ---------- */
function detectMsgDir(text) {
  const s = String(text || "").slice(0, 2000);
  if (!s.trim()) return null;
  const ar = (s.match(/[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]/g) || []).length;
  const lat = (s.match(/[A-Za-z]/g) || []).length;
  if (!ar && !lat) return null;
  return ar >= lat ? "rtl" : "ltr";
}

function applyMsgDir(el, text, fallback) {
  const d = detectMsgDir(text) || fallback || null;
  el.classList.remove("rtl", "ltr");
  if (!d) return;
  el.classList.add(d);
  const c = el.querySelector(".msg-content");
  if (c) c.setAttribute("dir", d);
}

function attachBadgeIcon(type) {
  if (type === "video")
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polygon points="23 7 16 12 23 17 23 7" /><rect x="1" y="5" width="15" height="14" rx="2" /></svg>';
  if (type === "document")
    return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="9" y1="13" x2="15" y2="13" /><line x1="9" y1="17" x2="13" y2="17" /></svg>';
  return '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" /><circle cx="8.5" cy="8.5" r="1.5" /><polyline points="21 15 16 10 5 21" /></svg>';
}

function appendUserMessage(text, attachments) {
  const list = Array.isArray(attachments) ? attachments : attachments ? [attachments] : [];
  hideWelcome();
  const el = document.createElement("div");
  el.className = "msg user";
  applyMsgDir(el, text, "rtl");
  el.setAttribute("data-anim", "");
  list.forEach((attachment) => {
    if (!attachment) return;
    // Attached IMAGES render centered and prominent in the bubble (the user
    // points at them with @name); other files keep the compact badge.
    if (attachment.type === "image") {
      const src = attachment.dataUrl || attachment.thumb || "";
      if (typeof src === "string" && /^(data:image|blob:|https?:)/.test(src)) {
        const fig = document.createElement("div");
        fig.className = "msg-attach-img";
        const im = document.createElement("img");
        im.src = src;
        im.alt = attachment.name || "image";
        im.loading = "lazy";
        fig.appendChild(im);
        el.appendChild(fig);
        return;
      }
    }
    const name = document.createElement("span");
    name.className = "msg-attach-name";
    name.textContent = attachment.name;
    const badge = document.createElement("div");
    badge.className = "msg-attach";
    if (attachment.type === "document") badge.classList.add("ft-" + docTypeOf(attachment.name));
    badge.innerHTML = attachBadgeIcon(attachment.type);
    badge.appendChild(name);
    el.appendChild(badge);
  });
  if (text || list.length) {
    const content = document.createElement("div");
    content.className = "msg-content";
    el.querySelectorAll(".msg-attach").forEach((b) => content.appendChild(b));
    if (text) {
      const wrapper = document.createElement("div");
      wrapper.innerHTML = formatRich(text);
      content.appendChild(wrapper);
    }
    el.appendChild(content);
  }
  el.classList.add("anim");
  el.dataset.raw = text || "";
  chatInner.appendChild(el);
  scrollToBottom(true, true);
  return el;
}

function attachUserBar(el) {
  const old = el.querySelector(":scope > .msg-actions");
  if (old) old.remove();
  const bar = document.createElement("div");
  bar.className = "msg-actions below";
  const raw = el.dataset.raw || "";
  [
    ["copy", "Copy", () => copyText(raw)],
    ["edit", "Edit", () => editUserMessage(el)],
    ["trash", "Delete", () => confirmDeleteMessage(el)],
  ].forEach(([icon, label, fn], i) => {
    const b = makeActBtn(icon, label, fn, icon === "trash");
    b.style.animationDelay = `${i * 60}ms`;
    bar.appendChild(b);
  });
  el.appendChild(bar);
}

function appendAssistantShell() {
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.innerHTML = `<div class="msg-content"><p dir="auto"></p></div>`;
  chatInner.appendChild(el);
  return { el, p: el.querySelector("p") };
}

/* ---------- thinking-block: Working header + expandable steps ----------
   Working header (dot-square 5x5 + wave text + arrow) stays open while the
   agent works; every step arrow shows the model reason only, plus one
   curated detail line per tool (todo steps, file names, short code/terminal
   preview) and/or image — raw tool output is never dumped.
   When done the wave stops (static white, 3x3 square) and the block
   collapses — the final reply lives below it. Header is English LTR;
   step content is dir=auto so Arabic reads RTL. ---------- */
const squareAnimations = new Map();

function startSquareAnimation(square) {
  stopSquareAnimation(square);
  const dots = [...square.querySelectorAll("span")];
  const rings = [[], [], []];
  dots.forEach((dot) => {
    rings[+dot.dataset.ring].push(dot);
  });
  let timers = [];
  let stopped = false;
  function runCycle() {
    if (stopped) return;
    dots.forEach((d) => d.classList.remove("on"));
    timers.push(setTimeout(() => {
      if (stopped) return;
      rings[0].forEach((d) => d.classList.add("on"));
    }, 60));
    timers.push(setTimeout(() => {
      if (stopped) return;
      rings[1].forEach((d) => d.classList.add("on"));
    }, 360));
    timers.push(setTimeout(() => {
      if (stopped) return;
      rings[2].forEach((d) => d.classList.add("on"));
    }, 660));
    timers.push(setTimeout(() => {
      if (stopped) return;
      dots.forEach((d) => d.classList.remove("on"));
    }, 1300));
    timers.push(setTimeout(runCycle, 1600));
  }
  squareAnimations.set(square, {
    stop: () => {
      stopped = true;
      timers.forEach((t) => clearTimeout(t));
      timers = [];
    },
  });
  runCycle();
}

function stopSquareAnimation(square) {
  const anim = squareAnimations.get(square);
  if (anim) {
    anim.stop();
    squareAnimations.delete(square);
  }
}

function createDotSquare() {
  const square = document.createElement("div");
  square.className = "dot-square";
  const SIZE = 5;
  const center = 2;
  for (let row = 0; row < SIZE; row++) {
    for (let col = 0; col < SIZE; col++) {
      const dot = document.createElement("span");
      const ring = Math.max(Math.abs(row - center), Math.abs(col - center));
      dot.dataset.ring = ring;
      const isWhite = row >= 1 && row <= 3 && col >= 1 && col <= 3;
      if (isWhite) dot.classList.add("lit-white");
      else dot.classList.add("lit-black");
      square.appendChild(dot);
    }
  }
  return square;
}

const THINK_ARROW_SVG = `<svg viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M4 2.5 L8 6 L4 9.5"/></svg>`;

function createThinkingBlock() {
  const block = document.createElement("div");
  block.className = "thinking-block open";

  const header = document.createElement("div");
  header.className = "thinking-header";
  header.setAttribute("role", "button");
  header.setAttribute("tabindex", "0");

  const dotSquare = createDotSquare();

  const workingText = document.createElement("span");
  workingText.className = "working-text";
  workingText.textContent = "Working";

  // No arrow yet: it appears only once the first step begins.
  header.appendChild(dotSquare);
  header.appendChild(workingText);

  const steps = document.createElement("div");
  steps.className = "steps";
  const stepsInner = document.createElement("div");
  stepsInner.className = "steps-inner";
  steps.appendChild(stepsInner);

  block.appendChild(header);
  block.appendChild(steps);

  startSquareAnimation(dotSquare);
  const toggle = () => block.classList.toggle("open");
  header.addEventListener("click", toggle);
  header.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });

  block._dotSquare = dotSquare;
  block._stepsInner = stepsInner;
  block._t0 = Date.now();
  return block;
}

function ensureHeaderArrow(block) {
  // The header arrow exists only once steps began (never on a fresh
  // Working). Never throws, never duplicates.
  try {
    if (!block) return;
    const header = block.querySelector(":scope > .thinking-header");
    if (!header || header.querySelector(":scope > .arrow")) return;
    if (!block._stepsInner || !block._stepsInner.children.length) return;
    const arrow = document.createElement("span");
    arrow.className = "arrow";
    arrow.innerHTML = THINK_ARROW_SVG;
    header.appendChild(arrow);
  } catch (_) {}
}

function settleThinkingBlock(block, elapsedSec) {
  // Working stops: static white text + static 3x3 square, elapsed seconds
  // shown next to Working, block collapses.
  if (!block || block.dataset.settled === "1") return;
  block.dataset.settled = "1";
  ensureHeaderArrow(block);
  try {
    if (block._dotSquare) {
      stopSquareAnimation(block._dotSquare);
      block._dotSquare.querySelectorAll("span").forEach((d) => d.classList.remove("on"));
    }
  } catch (_) {}
  try {
    let secs = null;
    if (typeof elapsedSec === "number" && isFinite(elapsedSec) && elapsedSec >= 0) {
      secs = Math.round(elapsedSec);
    } else if (typeof block._t0 === "number") {
      secs = Math.max(0, Math.round((Date.now() - block._t0) / 1000));
    }
    if (secs !== null && !block.querySelector(":scope > .thinking-header > .working-time")) {
      const header = block.querySelector(":scope > .thinking-header");
      if (header) {
        const t = document.createElement("span");
        t.className = "working-time";
        t.textContent = `${secs}s`;
        const arrow = header.querySelector(":scope > .arrow");
        if (arrow) header.insertBefore(t, arrow);
        else header.appendChild(t);
      }
    }
  } catch (_) {}
  block.classList.add("done");
  block.classList.remove("open");
}

function resumeThinkingBlock(block) {
  // Continue-after-STOP: the SAME frozen Working block comes back to life —
  // wave restarts, block reopens, elapsed timer restarts from zero. Steps
  // stay exactly as they were; new steps append below in the same list.
  // Never throws.
  try {
    if (!block || !block.isConnected) return;
    try { delete block.dataset.settled; } catch (_) {}
    try { delete block.dataset.stopped; } catch (_) {}
    block.classList.remove("done");
    block.classList.add("open");
    try {
      block.querySelectorAll(":scope > .thinking-header > .working-time").forEach((t) => {
        try { t.remove(); } catch (_) {}
      });
    } catch (_) {}
    try {
      if (block._dotSquare) startSquareAnimation(block._dotSquare);
    } catch (_) {}
    try { block._t0 = Date.now(); } catch (_) {}
    ensureHeaderArrow(block);
  } catch (_) {}
}

function settleThinkingStopped(thinkingEl) {
  // STOP mid-task: freeze everything in place — the Working block stays
  // visible (stops animating, shows elapsed seconds, stays expandable via
  // its header arrow) and a --- Stopped --- divider is added right below
  // it. The user bubble + thinking bubble are tagged data-stopped so a
  // later history reload drops these live artifacts (their persisted copies
  // own the view from then on — never a duplicate). Idempotent: a second
  // STOP adds nothing. Never throws.
  try {
    if (!thinkingEl || !thinkingEl.isConnected) return false;
    if (thinkingEl.dataset.stopped === "1") return true;
    thinkingEl.dataset.stopped = "1";
    try {
      if (typeof lastUserEl !== "undefined" && lastUserEl && lastUserEl.isConnected) {
        lastUserEl.dataset.stopped = "1";
      }
    } catch (_) {}
    const block = thinkingEl._block || thinkingEl.querySelector(":scope > .thinking-block");
    if (block) {
      try {
        block.querySelectorAll(".step-item.running").forEach((it) => {
          it.classList.remove("running");
        });
      } catch (_) {}
      settleThinkingBlock(block);
      block.dataset.stopped = "1";
    }
    try {
      const next = thinkingEl.nextElementSibling;
      if (!next || !next.classList || !next.classList.contains("stopped-line")) {
        const line = document.createElement("div");
        line.className = "stopped-line";
        line.textContent = "Stopped";
        thinkingEl.after(line);
      }
    } catch (_) {}
    scrollToBottom();
    return true;
  } catch (_) {
    return false;
  }
}

function stepReasonText(step) {
  // Optional Reason node: the model's own words only. Empty when the model
  // wrote none — the step then shows with no arrow (unless output/image).
  const r = step && typeof step.reason === "string" ? step.reason.trim() : "";
  return r ? r.slice(0, 500) : "";
}

function stepKey(step) {
  // One tool call = one step: keyed by the call id first, so a completion
  // whose label differs from its preview (question Answered, tool errors)
  // updates the SAME item instead of orphaning a forever-running row.
  try {
    const id = typeof step === "object" && step ? step.key || step.id || "" : "";
    if (id) return String(id);
  } catch (_) {}
  const t = typeof step === "string" ? step : (step && (step.event || "")) || "";
  return `::${t}`;
}


function ensureStepArrow(item) {
  // The arrow exists only while the step has expandable content
  // (reason text, output text, or an image). Never throws.
  try {
    if (!item || !item._head || item._head.querySelector(":scope > .step-arrow")) return;
    const inner = item._inner;
    const has = !!(
      (inner && inner.querySelector(":scope > .step-text, :scope > .step-img")) ||
      (typeof item._shot === "string" && item._shot)
    );
    if (!has) return;
    const arrow = document.createElement("span");
    arrow.className = "step-arrow";
    arrow.innerHTML = THINK_ARROW_SVG;
    item._head.appendChild(arrow);
  } catch (_) {}
}

function markStepRunning(item) {
  // The step was just requested: orange dot until it completes, then the
  // dot alone turns green — or red when the tool call failed. No check mark.
  try {
    if (!item) return;
    item._done = false;
    item.classList.remove("done");
    item.classList.remove("fail");
    item.classList.add("running");
  } catch (_) {}
}

function createStepItem(step) {
  const title = humanizeEvent(typeof step === "string" ? step : step?.event || step?.text || "") || "Step";
  const reason = stepReasonText(typeof step === "object" ? step : null);
  const output = typeof step === "object" && step && typeof step.output === "string" ? step.output.slice(0, 500) : "";
  const failed = typeof step === "object" && !!step && step.failed === true;
  const media = typeof step === "object" && step ? step.media || null : null;
  const shot = typeof step === "object" && step
    ? (media && typeof media.shot === "string" && media.shot) || (typeof step.shot === "string" && step.shot) || ""
    : "";
  const genImage = typeof step === "object" && step
    ? (media && typeof media.image === "string" && media.image) || (typeof step.image === "string" && step.image) || ""
    : "";
  // Voice records render once as a WhatsApp-style bubble on the assistant
  // message (appendFinalVoice), never inside the step.
  // Voice records render once as a WhatsApp-style bubble on the assistant
  // message (appendFinalVoice), never inside the step.

  const item = document.createElement("div");
  item.className = "step-item";

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
    const reasonEl = document.createElement("div");
    reasonEl.className = "step-text step-reason";
    reasonEl.setAttribute("dir", "auto");
    reasonEl.textContent = reason;
    inner.appendChild(reasonEl);
  }

  item.appendChild(head);
  item.appendChild(content);
  item._head = head;
  item._inner = inner;
  item._done = false;
  item._shot = shot;
  item._genImage = genImage;

  const toggle = (e) => {
    if (e) e.stopPropagation();
    // No expandable content → plain line, nothing to open.
    if (!head.querySelector(":scope > .step-arrow")) return;
    item.classList.toggle("open");
    if (item.classList.contains("open")) {
      const img = inner.querySelector(".step-img img");
      if (img && !img.getAttribute("src") && img.dataset.shot) {
        img.src = "/api/shot?path=" + encodeURIComponent(img.dataset.shot);
      }
      const gen = inner.querySelector(".step-img.gen img");
      if (gen && !gen.getAttribute("src") && gen.dataset.gen) {
        gen.src = "/api/image?path=" + encodeURIComponent(gen.dataset.gen);
      }
    }
  };
  head.addEventListener("click", toggle);
  head.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      toggle();
    }
  });

  completeStepItem(item, { output, shot, image: genImage, failed });
  return item;
}

function completeStepItem(item, { output = "", shot = "", image = "", failed = false } = {}) {
  // Fills a started step with its curated detail (under the reason) and/or
  // image, then marks it done: green bullet on success, red on any tool
  // failure (any method or input). The detail is never raw tool
  // output — the backend sends one short per-tool line only. Never throws.
  try {
    if (!item || !item._inner) return;
    const inner = item._inner;
    if (typeof output === "string" && output.trim() && !inner.querySelector(":scope > .step-out")) {
      const outEl = document.createElement("div");
      outEl.className = "step-text step-out";
      outEl.setAttribute("dir", "auto");
      outEl.textContent = output.trim().slice(0, 500);
      inner.appendChild(outEl);
    }
    if (typeof shot === "string" && shot && !inner.querySelector(":scope > .step-img")) {      const url = "/api/shot?path=" + encodeURIComponent(shot);
      const wrap = document.createElement("div");
      wrap.className = "step-img";
      const img = document.createElement("img");
      img.alt = "Step image";
      img.decoding = "async";
      img.loading = "lazy";
      img.dataset.shot = shot;
      img.addEventListener("error", () => wrap.remove(), { once: true });
      img.addEventListener("click", (e) => {
        e.stopPropagation();
        openShotLightbox(url);
      });
      wrap.appendChild(img);
      inner.appendChild(wrap);
      // Image-only expand still lazy-loads on first open.
      if (item.classList.contains("open") && !img.getAttribute("src")) {
        img.src = url;
      }
    }
    if (typeof image === "string" && image && !inner.querySelector(":scope > .step-img.gen")) {
      const url = "/api/image?path=" + encodeURIComponent(image);
      const wrap = document.createElement("div");
      wrap.className = "step-img gen";
      const img = document.createElement("img");
      img.alt = "Generated image";
      img.decoding = "async";
      img.loading = "lazy";
      img.dataset.gen = image;
      img.addEventListener("error", () => wrap.remove(), { once: true });
      img.addEventListener("click", (e) => {
        e.stopPropagation();
        openShotLightbox(url);
      });
      wrap.appendChild(img);
      inner.appendChild(wrap);
      if (item.classList.contains("open") && !img.getAttribute("src")) {
        img.src = url;
      }
    }
    if (!item._done) {
      item._done = true;
      item.classList.remove("running");
      item.classList.add(failed ? "fail" : "done");
    }
    // Late content (output/image at completion) earns the arrow then.
    ensureStepArrow(item);
  } catch (_) {}
}

function addThinking(text) {
  // A previous STOP keeps its settled bubble on screen — demote it (drop
  // the live id, content untouched) so the new run owns #thinkingMsg and a
  // later STOP settles the new task, never the old one. Never throws.
  try {
    const prev = document.getElementById("thinkingMsg");
    if (prev && prev.dataset.stopped === "1") prev.removeAttribute("id");
  } catch (_) {}
  const el = document.createElement("div");
  el.className = "msg assistant";
  el.id = "thinkingMsg";
  const block = createThinkingBlock();
  el.appendChild(block);
  el._block = block;
  // Direction from the very first frame (same language as the reply):
  // the Working line never jumps sides later.
  if (text) applyMsgDir(el, text, "ltr");
  chatInner.appendChild(el);
  scrollToBottom(true, true);
  try { enforceSingleLiveBlock(el); } catch (_) {}
  return el;
}

function typeRich(container, fullText) {
  // Format FIRST, then animate writing the formatted result itself (fast).
  return new Promise((resolve) => {
    const template = document.createElement("div");
    template.innerHTML = formatRich(fullText);
    try {
      linkifyFolderPaths(template);
    } catch (_) {}
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
    container.innerHTML = "";
    container.classList.add("typing-live");
    container.appendChild(live);
    let ji = 0;
    let ci = 0;
    const markItemText = () => {
      // A list item earns its marker the moment its first text arrives —
      // empty items stay markerless while typing (no stray dots).
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
      container.classList.remove("typing-live");
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
    scrollToBottom();
    (function tick() {
      if (ji >= jobs.length) {
        cursor.remove();
        scrollToBottom();
        container.classList.remove("typing-live");
        resolve(live);
        return;
      }
      const startJi = ji;
      const job = jobs[ji];
      const chunk = 2 + (Math.random() < 0.3 ? 1 : 0);
      job.tn.textContent += job.full.slice(ci, ci + chunk);
      ci += chunk;
      advance();
      // Blocks that just finished typing (tables / code) pop in with animation.
      for (let k = startJi; k < ji; k++) {
        try {
          const anc = jobs[k].tn.parentElement
            ? jobs[k].tn.parentElement.closest("table,pre")
            : null;
          if (anc && !anc.classList.contains("pop-in")) anc.classList.add("pop-in");
        } catch (_) {
          // ignore lookup failures mid-stream
        }
      }
      placeCursor();
      scrollToBottom();
      setTimeout(tick, 6 + Math.random() * 10);
    })();
  });
}

function typeMessage(text, steps = [], elapsed = null) {
  const { el, p } = appendAssistantShell();
  applyMsgDir(el, text, "ltr");
  // Reserve the action-bar height now: icons appearing later push nothing.
  const ph = document.createElement("div");
  ph.className = "msg-actions below msg-actions-ph";
  el.appendChild(ph);
  const list = Array.isArray(steps) ? steps : [];
  const seen = new Set();
  const items = [];
  if (list.length) {
    list.slice(0, 12).forEach((s, i) => {
      // Dedupe by unique call key — the same event label may repeat across calls.
      const key = typeof s === "string" ? `str:${i}:${s}` : (s?.key || `idx:${i}:${s?.event || ""}`);
      if (!key || seen.has(key)) return;
      seen.add(key);
      try {
        const item = createStepItem(s);
        completeStepItem(item, {});
        items.push(item);
      } catch (_) {}
    });
  }
  if (items.length) {
    // Settled block: Working stopped, elapsed seconds shown, collapsed,
    // steps expandable anytime.
    const block = createThinkingBlock();
    items.forEach((item) => block._stepsInner.appendChild(item));
    settleThinkingBlock(block, elapsed);
    p.before(block);
  }
  const holder = document.createElement("div");
  p.replaceWith(holder);
  return typeRich(holder, text).then(() => el);
}

function cleanHistoryText(raw) {
  // Strip the injected [Attached files] block on display; show file chips instead.
  const text = String(raw || "");
  const files = [];
  const nameRe = /name="([^"]+)"/g;
  let m;
  while ((m = nameRe.exec(text)) !== null) {
    if (files.length < 4) files.push(m[1].slice(0, 60));
  }
  const blockStart = text.indexOf("[Attached files");
  const clean = blockStart >= 0 ? text.slice(0, blockStart).trim() : text;
  return { text: clean.slice(0, MAX_MESSAGE), files };
}

const HIST_PAGE = 50; // one page = 50 older messages per scroll-up load
let histLoadingMore = false;
let histHasMore = true; // server says so per page; false = reached the top
let histOldestId = null; // id of the oldest rendered message (next page goes before it)

async function loadHistory() {
  try {
    histHasMore = true;
    histOldestId = null;
    const res = await fetch("/api/conversation", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    const msgs = Array.isArray(data?.messages) ? data.messages : [];
    renderHistoryMessages(msgs);
    try {
      const valid = msgs.filter((m) => m && m.id && (m.role === "user" || m.role === "assistant"));
      histOldestId = valid.length ? String(valid[0].id) : null;
      // Full-list load: older pages exist only when the list was capped.
      histHasMore = Array.isArray(data?.messages) && typeof data?.has_more === "boolean"
        ? data.has_more : false;
    } catch (_) {}
  } catch (_) {
    // keep welcome message on failure
  }
}

async function loadOlderHistory() {
  // Scroll-up pagination: fetch the 50 messages BEFORE the oldest rendered
  // one and prepend them above (position preserved, no jump).
  if (histLoadingMore || !histHasMore || !histOldestId) return;
  if (!chatScroll || !chatInner) return;
  histLoadingMore = true;
  try {
    const res = await fetch(
      "/api/conversation?limit=" + HIST_PAGE + "&before_id=" + encodeURIComponent(histOldestId),
      { cache: "no-store" }
    );
    if (!res.ok) return;
    const data = await res.json();
    const msgs = Array.isArray(data?.messages) ? data.messages : [];
    if (!msgs.length) {
      histHasMore = false;
      return;
    }
    const prevHeight = chatScroll.scrollHeight;
    prependHistoryMessages(msgs);
    try {
      const valid = msgs.filter((m) => m && m.id);
      if (valid.length) histOldestId = String(valid[0].id);
    } catch (_) {}
    histHasMore = data?.has_more !== false && msgs.length >= HIST_PAGE ? true : Boolean(data?.has_more);
    if (msgs.length < HIST_PAGE && !data?.has_more) histHasMore = false;
    // Keep the viewport anchored: new content above shifts scroll down equally.
    try {
      chatScroll.scrollTop += chatScroll.scrollHeight - prevHeight;
    } catch (_) {}
  } catch (_) {
    // keep current view on failure
  } finally {
    histLoadingMore = false;
  }
}

// Internal STOP note (engine _STOPPED_NOTE): kept in context for the model,
// never shown as a reply. Matches old markers persisted before the flag.
const STOPPED_NOTE_TEXT = "The user stopped this task before it finished (STOP). Do not continue it unless the user asks again.";
function isStoppedMarker(m) {
  try {
    if (!m || m.role !== "assistant") return false;
    if (m.stopped === true) return true;
    return String(m.content || "").trim() === STOPPED_NOTE_TEXT;
  } catch (_) {
    return false;
  }
}

// Stopped-task Continue state: a STOP mid-task arms a Continue pill on the
// send button (empty composer only). Survives refresh two ways: the persisted
// history (last assistant message is the stopped marker) re-arms it on load,
// and localStorage covers the live beat before any reload lands. The stored
// value is the stopped task's user message id (or "1" when not persisted yet)
// so Continue resumes in place via skip_history_ids — no new user bubble.
// Never throws.
const STOPPED_TASK_KEY = "x5:stopped-task";
const CONTINUE_INPUT = "Continue the stopped task from where it stopped.";
const CONTINUE_LABEL = "Continue";
function readStoppedUserId() {
  try {
    const v = localStorage.getItem(STOPPED_TASK_KEY);
    if (typeof v === "string" && v) return v.slice(0, 16);
  } catch (_) {}
  return "";
}
function setStoppedTask(userId) {
  try {
    localStorage.setItem(STOPPED_TASK_KEY, String(userId || "1").slice(0, 16) || "1");
  } catch (_) {}
  try { updateSendEnabled(); } catch (_) {}
}
function clearStoppedTask() {
  try { localStorage.removeItem(STOPPED_TASK_KEY); } catch (_) {}
  try { updateSendEnabled(); } catch (_) {}
}
function syncStoppedFromHistory(msgs) {
  // The last assistant message decides: stopped marker → arm Continue (with
  // the preceding user id for an idempotent resume); a normal reply → disarm
  // (the task finished or a newer request superseded it). A trailing user
  // bubble keeps whatever the live session set. Never throws.
  try {
    // A live/catchup run owns the UI: history must never re-arm Continue
    // under it (that was the refresh-while-running pill bug).
    try {
      if (resumePending || activeRun || catchupResume) return;
    } catch (_) {}
    if (!Array.isArray(msgs) || !msgs.length) return;
    const valid = msgs.filter((m) => m && (m.role === "user" || m.role === "assistant") && String(m.content || "").trim());
    if (!valid.length) {
      // Emptied chat (e.g. Delete chat history): no task exists anymore —
      // disarm Continue so the button falls back to normal send.
      try { clearStoppedTask(); } catch (_) {}
      return;
    }
    const last = valid[valid.length - 1];
    if (last.role === "assistant" && isStoppedMarker(last)) {
      // A newer unsent task (pending-user) supersedes the stopped one: the
      // new SEND already cleared the pill — history must not resurrect it
      // just because the new reply is not persisted yet.
      try {
        if (readPendingUser()) return;
      } catch (_) {}
      let uid = "";
      for (let i = valid.length - 2; i >= 0; i--) {
        if (valid[i].role === "user" && valid[i].id) { uid = String(valid[i].id).slice(0, 16); break; }
      }
      setStoppedTask(uid || readStoppedUserId() || "1");
    } else if (last.role === "assistant") {
      clearStoppedTask();
    }
  } catch (_) {}
}
function stoppedStepsBlock(m) {
  // Frozen Working block for a STOP marker from persisted steps (or null).
  // Never throws.
  try {
    const histSteps = Array.isArray(m.steps) ? m.steps.slice(0, 12) : [];
    const seenHist = new Set();
    const histItems = [];
    histSteps.forEach((s, i) => {
      const key = typeof s === "string" ? `str:${i}:${s}` : (s?.key || `idx:${i}:${s?.event || ""}`);
      if (!key || seenHist.has(key)) return;
      seenHist.add(key);
      try {
        const item = createStepItem(s);
        completeStepItem(item, {});
        histItems.push(item);
      } catch (_) {}
    });
    if (histItems.length) {
      const block = createThinkingBlock();
      histItems.forEach((item) => block._stepsInner.appendChild(item));
      settleThinkingBlock(block, m.elapsed);
      return block;
    }
  } catch (_) {}
  return null;
}
function appendStoppedLine(host) {
  // --- Stopped --- divider (same marker as the live STOP path). Never throws.
  try {
    const line = document.createElement("div");
    line.className = "stopped-line";
    line.textContent = "Stopped";
    host.appendChild(line);
  } catch (_) {}
}
function buildHistoryNode(m) {
  // Builds (but does not attach) one persisted history bubble. Returns null
  // when the message carries nothing displayable. Never throws.
  try {
    if (!m || (m.role !== "user" && m.role !== "assistant") || !String(m.content || "").trim()) return null;
    if (m.role === "user") {
      const cleaned = cleanHistoryText(m.content);
      if (!cleaned.text && !cleaned.files.length) return null;
      const el = document.createElement("div");
      el.className = "msg user";
      applyMsgDir(el, cleaned.text, "rtl");
      const content = document.createElement("div");
      content.className = "msg-content";
      cleaned.files.forEach((fname) => {
        const badge = document.createElement("div");
        badge.className = "msg-attach";
        badge.innerHTML = attachBadgeIcon("image");
        const nm = document.createElement("span");
        nm.className = "msg-attach-name";
        nm.textContent = fname;
        badge.appendChild(nm);
        content.appendChild(badge);
      });
      if (cleaned.text) {
        const wrapper = document.createElement("div");
        wrapper.innerHTML = formatRich(cleaned.text);
        content.appendChild(wrapper);
      }
      el.appendChild(content);
      if (m.id) el.dataset.mid = String(m.id);
      attachUserBar(el);
      return el;
    }
    const rawText = String(m.content || "");
    const el = document.createElement("div");
    el.className = "msg assistant";
    if (isStoppedMarker(m)) {
      // STOP marker, not a reply: frozen Working (when steps exist) + the
      // Stopped divider only — unless the task was continued (resumed),
      // in which case the divider is gone but the steps stay. The note
      // text never renders.
      applyMsgDir(el, "", "ltr");
      const content = document.createElement("div");
      content.className = "msg-content";
      try {
        const blk = stoppedStepsBlock(m);
        if (blk) content.appendChild(blk);
      } catch (_) {}
      if (m.resumed !== true) appendStoppedLine(content);
      el.appendChild(content);
      if (m.id) el.dataset.mid = String(m.id);
      return el;
    }
    applyMsgDir(el, rawText, "ltr");
    const content = document.createElement("div");
    content.className = "msg-content";
    const p = document.createElement("div");
    p.innerHTML = formatRich(rawText.slice(0, MAX_MESSAGE));
    content.appendChild(p);
    el.appendChild(content);
    try {
      const histSteps = Array.isArray(m.steps) ? m.steps.slice(0, 12) : [];
      const seenHist = new Set();
      const histItems = [];
      histSteps.forEach((s, i) => {
        const key = typeof s === "string" ? `str:${i}:${s}` : (s?.key || `idx:${i}:${s?.event || ""}`);
        if (!key || seenHist.has(key)) return;
        seenHist.add(key);
        try {
          const item = createStepItem(s);
          completeStepItem(item, {});
          histItems.push(item);
        } catch (_) {}
      });
      if (histItems && histItems.length) {
        const block = createThinkingBlock();
        histItems.forEach((item) => block._stepsInner.appendChild(item));
        settleThinkingBlock(block, m.elapsed);
        p.before(block);
      }
    } catch (_) {}
    const rich = document.createElement("div");
    rich.innerHTML = formatRich(rawText);
    try {
      linkifyFolderPaths(rich);
    } catch (_) {}
    p.replaceWith(rich);
    appendFinalImages(el, m.images);
    appendFinalVoice(el, m.audio);
    appendFinalFiles(el, m.files);
    el.dataset.raw = rawText;
    if (m.id) el.dataset.mid = String(m.id);
    attachAssistantBar(el, rawText);
    return el;
  } catch (_) {
    return null;
  }
}

function prependHistoryMessages(msgs) {
  // Prepends one older page ABOVE the current top (oldest-first order).
  try {
    if (!Array.isArray(msgs) || !msgs.length || !chatInner) return;
    hideWelcome();
    const frag = document.createDocumentFragment();
    msgs.forEach((m) => {
      const node = buildHistoryNode(m);
      if (node) frag.appendChild(node);
    });
    if (frag.childNodes.length) chatInner.prepend(frag);
  } catch (_) {}
}

function onChatLast50Section(msgs) {
  // The `agent` sync section (highest priority after status): last-50 render
  // immediately, but never wipe an in-flight run — same keep-rules as
  // loadHistory. resumePending covers the window BEFORE resumeActiveRun sets
  // its flags (that was the refresh-duplicate race: the section rendered, then
  // resume appended a second user bubble + second Working).
  try {
    if (resumePending || activeRun || activeThinking || catchupResume) return; // a run owns the UI right now
    if (!Array.isArray(msgs) || !msgs.length) {
      // Emptied chat: no task exists — disarm Continue.
      try { clearStoppedTask(); } catch (_) {}
      return;
    }
    renderHistoryMessages(msgs);
  } catch (_) {}
}

function renderHistoryMessages(msgs) {
  try {
    const valid = msgs.filter(
      (m) => m && (m.role === "user" || m.role === "assistant") && String(m.content || "").trim()
    );
    // State-aware button on EVERY render, including the empty one: an
    // emptied chat disarms Continue (normal send), a trailing stopped
    // marker re-arms it. Runs before the empty early-return below.
    try { syncStoppedFromHistory(msgs); } catch (_) {}
    if (valid.length === 0) return;
    hideWelcome();
    // Refresh mid-run must never wipe in-flight UI: keep the thinking bubble
    // and any optimistic bubble/stream shell not persisted yet (no data-mid),
    // then re-append them after the persisted history renders.
    // Stopped artifacts are NOT kept: their persisted copies (user request +
    // stopped note + frozen Working) render from history below — keeping the
    // live ones would duplicate the task. Their --- Stopped --- dividers go
    // with them (transient session markers only).
    try {
      chatInner.querySelectorAll(":scope > .stopped-line").forEach((d) => {
        try { d.remove(); } catch (_) {}
      });
    } catch (_) {}
    const keep = [];
    const histStopped = valid.length > 0 && isStoppedMarker(valid[valid.length - 1]);
    chatInner.querySelectorAll(".msg").forEach((m) => {
      try {
        if (m.dataset.stopped === "1") {
          // Persist not done yet: keep the live frozen Working so history
          // reload cannot erase the steps. Once the stopped marker lands,
          // the persisted copy owns the view.
          if (!histStopped) {
            keep.push(m);
            m.remove();
          } else {
            m.remove();
          }
        } else if (m.id === "thinkingMsg" || !m.dataset.mid) {
          keep.push(m);
          m.remove();
        } else {
          m.remove();
        }
      } catch (_) {
        try {
          m.remove();
        } catch (_) {}
      }
    });
    // bounded render: last 100 to avoid DOM blowup
    let lastSeenUserText = "";
    valid.slice(-100).forEach((m) => {
      if (m.role === "user") {
        const cleaned = cleanHistoryText(m.content);
        if (!cleaned.text && !cleaned.files.length) return;
        const el = document.createElement("div");
        el.className = "msg user";
        applyMsgDir(el, cleaned.text, "rtl");
        const content = document.createElement("div");
        content.className = "msg-content";
        cleaned.files.forEach((fname) => {
          const badge = document.createElement("div");
          badge.className = "msg-attach";
          badge.innerHTML = attachBadgeIcon("image");
          const nm = document.createElement("span");
          nm.className = "msg-attach-name";
          nm.textContent = fname;
          badge.appendChild(nm);
          content.appendChild(badge);
        });
        if (cleaned.text) {
          const wrapper = document.createElement("div");
          wrapper.innerHTML = formatRich(cleaned.text);
          content.appendChild(wrapper);
        }
        el.appendChild(content);
        el.dataset.raw = cleaned.text;
        if (m.id) el.dataset.mid = String(m.id);
        if (cleaned.text) lastSeenUserText = cleaned.text;
        attachUserBar(el);
        chatInner.appendChild(el);
      } else {
        const { el, p } = appendAssistantShell();
        if (isStoppedMarker(m)) {
          // STOP marker, not a reply: frozen Working (when steps exist) +
          // the Stopped divider only — unless continued (resumed): steps
          // stay, divider gone. The note text never renders.
          try {
            const host = (p && p.parentNode) || el;
            try { if (p) p.remove(); } catch (_) {}
            const blk = stoppedStepsBlock(m);
            if (blk) host.appendChild(blk);
            if (m.resumed !== true) appendStoppedLine(host);
          } catch (_) {}
          if (m.id) el.dataset.mid = String(m.id);
          return;
        }
        const rawText = String(m.content).slice(0, MAX_MESSAGE);
        applyMsgDir(el, rawText, "ltr");
        // After refresh: settled Working (stopped, collapsed) + reply below.
        // Steps stay expandable anytime — reason + output + image per step.
        try {
          const histSteps = Array.isArray(m.steps) ? m.steps.slice(0, 12) : [];
          const seenHist = new Set();
          const histItems = [];
          histSteps.forEach((s, i) => {
            const key = typeof s === "string" ? `str:${i}:${s}` : (s?.key || `idx:${i}:${s?.event || ""}`);
            if (!key || seenHist.has(key)) return;
            seenHist.add(key);
            try {
              const item = createStepItem(s);
              completeStepItem(item, {});
              histItems.push(item);
            } catch (_) {}
          });
          if (histItems.length) {
            const block = createThinkingBlock();
            histItems.forEach((item) => block._stepsInner.appendChild(item));
            settleThinkingBlock(block, m.elapsed);
            p.before(block);
          }
        } catch (_) {}
        // Screenshots of the screenshot tool are part of the final reply, so they stay.
        const rich = document.createElement("div");
        rich.innerHTML = formatRich(rawText);
        try {
          linkifyFolderPaths(rich);
        } catch (_) {}
        p.replaceWith(rich);
        appendFinalImages(el, m.images);
        appendFinalVoice(el, m.audio);
        appendFinalFiles(el, m.files);
        el.dataset.raw = rawText;
        if (m.id) el.dataset.mid = String(m.id);
        if (lastSeenUserText) el.dataset.userText = lastSeenUserText;
        attachAssistantBar(el, rawText);
      }
    });
    keep.forEach((m) => {
      try {
        chatInner.appendChild(m);
      } catch (_) {}
    });
    scrollToBottom(false, true);
  } catch (_) {
    // keep welcome message on failure
  }
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const max = String(file.type || "").startsWith("video") ? 24 * 1024 * 1024 : 8 * 1024 * 1024;
    if (file.size > max || file.size === 0) {
      reject(new Error("too large"));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

async function maybeCompressImage(file) {
  // Phone photos are huge — downscale before upload (fixes 413s, speeds up vision).
  try {
    if (!file.type.startsWith("image/")) return file;
    if (file.size <= 1536 * 1024) return file;
    if (typeof createImageBitmap !== "function") return file;
    const bmp = await createImageBitmap(file);
    const maxDim = 1600;
    const scale = Math.min(1, maxDim / Math.max(bmp.width, bmp.height));
    if (scale >= 1) {
      if (bmp.close) bmp.close();
      return file;
    }
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(bmp.width * scale);
    canvas.height = Math.round(bmp.height * scale);
    canvas.getContext("2d").drawImage(bmp, 0, 0, canvas.width, canvas.height);
    if (bmp.close) bmp.close();
    const blob = await new Promise((res) => canvas.toBlob(res, "image/jpeg", 0.85));
    if (!blob) return file;
    const name = file.name.replace(/\.[a-z0-9]+$/i, "") + ".jpg";
    return new File([blob], name, { type: "image/jpeg" });
  } catch (_) {
    return file;
  }
}

/* Deleted: old flow-step system replaced by the thinking-block above. */

/* Deleted: buildStepBox/bindStepToggle/toggleStepFlow/attachStepOutput —
   every step is now a step-item inside the thinking-block above. */

function finalShotUrl(shot) {
  return "/api/shot?path=" + encodeURIComponent(shot);
}

function finalImageUrl(item) {
  // Unified resolver: {shot} -> /api/shot, {image} -> /api/image,
  // {folder} -> /api/folder/download (working-folder pictures, inline),
  // bare strings auto-detected by extension. Never throws.
  try {
    if (typeof item === "string") {
      const s = item;
      if (/\.(png|jpe?g|webp|gif)$/i.test(s) && !s.includes("/") && !s.includes("\\")) return { url: "/api/image?path=" + encodeURIComponent(s), label: "Generated image" };
      return { url: finalShotUrl(s), label: "Screenshot" };
    }
    if (item && typeof item.image === "string" && item.image) return { url: "/api/image?path=" + encodeURIComponent(item.image), label: "Generated image" };
    if (item && typeof item.folder === "string" && item.folder) {
      const w = item.folder;
      if (w.length <= 200 && !w.includes("..") && !w.includes("\\") && /\.(png|jpe?g|webp|gif)$/i.test(w)) return { url: "/api/folder/download?path=" + encodeURIComponent(w) + "&preview=1", label: "Image" };
      return null;
    }
    if (item && typeof item.shot === "string" && item.shot) return { url: finalShotUrl(item.shot), label: "Screenshot" };
  } catch (_) {}
  return null;
}

function appendFinalImages(el, images) {
  // Unified end-of-reply image template: screenshots + generated images
  // share ONE professional centered card layout (live and after refresh).
  // Click any card for the fullscreen zoom viewer. Never throws.
  const list = (Array.isArray(images) ? images : [])
    .map((item) => finalImageUrl(item))
    .filter((v) => v && v.url)
    .slice(0, 6);
  if (!el || !list.length) return;
  const content = el.querySelector(".msg-content");
  if (!content || content.querySelector(":scope > .msg-final-shots")) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-final-shots";
  list.forEach(({ url, label }) => {
    const card = document.createElement("div");
    card.className = "final-shot";
    card.setAttribute("role", "button");
    card.setAttribute("tabindex", "0");
    card.setAttribute("aria-label", (label || "Image") + " — press Enter to enlarge");
    const img = document.createElement("img");
    img.alt = label || "Image";
    img.decoding = "async";
    img.loading = "lazy";
    img.src = url;
    img.addEventListener("error", () => card.remove(), { once: true });
    const open = () => openShotLightbox(url);
    img.addEventListener("click", open);
    card.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); }
    });
    card.appendChild(img);
    wrap.appendChild(card);
  });
  if (wrap.children.length) content.appendChild(wrap);
}

function escShotLightbox(e) {
  if (e && e.key === "Escape") closeShotLightbox();
}

function closeShotLightbox() {
  try {
    document.querySelector(".shotbox")?.remove();
    document.body.classList.remove("shotbox-open");
    document.removeEventListener("keydown", escShotLightbox);
  } catch (_) {}
}

function openShotLightbox(src) {
  // Fullscreen image viewer with zoom: buttons + wheel (desktop),
  // buttons + drag + double-tap (phone). Esc/backdrop closes.
  if (typeof src !== "string" || !src) return;
  closeShotLightbox();
  const back = document.createElement("div");
  back.className = "shotbox";
  back.innerHTML = `
    <div class="shotbox-stage"><img class="shotbox-img" alt="Screenshot enlarged" draggable="false"></div>
    <div class="shotbox-bar">
      <button type="button" data-zoom="out" aria-label="Zoom out">−</button>
      <button type="button" data-zoom="reset" aria-label="Reset zoom">1:1</button>
      <button type="button" data-zoom="in" aria-label="Zoom in">+</button>
      <button type="button" data-zoom="close" aria-label="Close">✕</button>
    </div>`;
  const img = back.querySelector(".shotbox-img");
  img.src = src;
  let scale = 1;
  let tx = 0;
  let ty = 0;
  const apply = () => {
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
  };
  const zoomBy = (f) => {
    scale = Math.min(5, Math.max(1, scale * f));
    if (scale === 1) {
      tx = 0;
      ty = 0;
    }
    apply();
  };
  back.querySelector('[data-zoom="in"]').addEventListener("click", (e) => {
    e.stopPropagation();
    zoomBy(1.4);
  });
  back.querySelector('[data-zoom="out"]').addEventListener("click", (e) => {
    e.stopPropagation();
    zoomBy(1 / 1.4);
  });
  back.querySelector('[data-zoom="reset"]').addEventListener("click", (e) => {
    e.stopPropagation();
    scale = 1;
    tx = 0;
    ty = 0;
    apply();
  });
  back.querySelector('[data-zoom="close"]').addEventListener("click", (e) => {
    e.stopPropagation();
    closeShotLightbox();
  });
  back.addEventListener("click", (e) => {
    if (e.target === back || (e.target && e.target.classList && e.target.classList.contains("shotbox-stage"))) {
      closeShotLightbox();
    }
  });
  back.addEventListener("wheel", (e) => {
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });
  let drag = null;
  img.style.touchAction = "none";
  img.addEventListener("pointerdown", (e) => {
    if (scale <= 1) return;
    drag = { x: e.clientX - tx, y: e.clientY - ty };
    try {
      img.setPointerCapture(e.pointerId);
    } catch (_) {}
  });
  img.addEventListener("pointermove", (e) => {
    if (!drag) return;
    tx = e.clientX - drag.x;
    ty = e.clientY - drag.y;
    apply();
  });
  ["pointerup", "pointercancel"].forEach((ev) => img.addEventListener(ev, () => {
    drag = null;
  }));
  img.addEventListener("dblclick", (e) => {
    e.preventDefault();
    if (scale > 1) {
      scale = 1;
      tx = 0;
      ty = 0;
    } else {
      scale = 2.2;
    }
    apply();
  });
  document.addEventListener("keydown", escShotLightbox);
  document.body.appendChild(back);
  document.body.classList.add("shotbox-open");
}

async function sendStream(value, attachments, thinking, skipHistoryIds, isResume) {
  // Chat request: POST /api/chat_stream, progress streams back as SSE
  // frames in the response body (server -> browser SSE, browser -> server
  // POST — no WebSocket). Returns true when fully handled, false to fall
  // back to the blocking endpoint.
  // isResume (seamless Continue): the message body is intentionally EMPTY —
  // the internal instruction never leaves the browser (the server resumes
  // the stopped task by id and the model never sees it either).
  activeThinking = thinking;
  const myGen = ++runGen;
  const ctl = new AbortController();
  activeRun = { cancelled: false, gen: myGen, thinking, abort: () => { try { ctl.abort(); } catch (_) {} } };
  let res;
  const streamBody = { message: isResume ? "" : value, attachments };
  if (isResume) streamBody.resume = true;
  if (pendingSkill) streamBody.skill = pendingSkill;
  if (pendingFolderRefs.length) streamBody.folder_refs = pendingFolderRefs.map((r) => ({ name: r.name, path: r.path }));
  if (skipHistoryIds && skipHistoryIds.length) streamBody.skip_history_ids = skipHistoryIds;
  try {
    res = await fetch("/api/chat_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(streamBody),
      signal: ctl.signal,
    });
  } catch (err) {
    const wasCancelled = Boolean(activeRun && activeRun.cancelled);
    activeRun = null;
    if (err && (err.aborted || err.name === "AbortError" || wasCancelled)) {
      throw { aborted: true };
    }
    return false;
  }
  if (res && res.status === 409) {
    let body409 = {};
    try { body409 = await res.json(); } catch (_) { body409 = {}; }
    activeRun = null;
    if (body409 && body409.retry) throw { retry: true };
    return false;
  }
  if (res && res.status === 403) {
    // License gate: answer with the fixed server message (never "Error:"),
    // keep the task out of the history and let the user fix it in Settings.
    let body403 = {};
    try { body403 = await res.json(); } catch (_) { body403 = {}; }
    if (body403 && body403.license_blocked) {
      activeRun = null;
      try { thinking.remove(); } catch (_) {}
      activeThinking = null;
      try { clearStoppedTask(); } catch (_) {}
      const { el: blockedEl, p: blockedP } = appendAssistantShell();
      blockedP.textContent = String(body403.error || "License required — open the Subscription page.");
      scrollToBottom();
      return true;
    }
  }
  if (!res || !res.ok || !res.body) {
    // Stale resume (the stopped task is no longer trailing — a newer task
    // superseded it): drop the dead pill with a clear toast, never an error
    // bubble and never a blocking fallback that would persist a stray reply.
    if (res && res.status === 410 && isResume) {
      activeRun = null;
      try {
        thinking.remove();
      } catch (_) {}
      activeThinking = null;
      try { clearStoppedTask(); } catch (_) {}
      toast("Stopped task expired — send as a new message");
      return true;
    }
    activeRun = null;
    return false;
  }

  // One thinking-block per turn: Working header + steps stay open above
  // while the answer streams below it. On done the block settles
  // (wave stops, collapsed) and the reply sits underneath.
  let shell = null;
  let shellP = null;
  // reply_assembly (long replies in parts): the server relays every accepted
  // part live. Parts are appended into the SAME place the final answer will
  // live and re-rendered instantly, so the user watches the reply grow; at
  // done the final rich text replaces it with no retyping flash.
  let livePartsEl = null;
  let livePartsRaw = "";
  const collected = [];
  const liveItems = new Map();
  // Streamed text buffered by loop step (never shown live): a step that
  // turns into tool events moves its text inside the event; the final
  // reply is typed once at done with the normal animation.
  const tokenSteps = new Map();

  function flushStepTokens(loopStep, item) {
    // Move this loop step's buffered text into its tool event (inside the
    // expandable content). Runs once per step. Never throws.
    try {
      if (typeof loopStep !== "number" || !item || !item._inner) return;
      if (item._flushedStep === loopStep) return;
      const text = (tokenSteps.get(loopStep) || "").trim().slice(0, 500);
      tokenSteps.delete(loopStep);
      if (!text) return;
      item._flushedStep = loopStep;
      const t = document.createElement("div");
      t.className = "step-text step-tokens";
      t.setAttribute("dir", "auto");
      t.textContent = text;
      item._inner.appendChild(t);
      ensureStepArrow(item);
      scrollToBottom();
    } catch (_) {}
  }
  function activeBlock() {
    try {
      if (thinking && thinking.isConnected && thinking._block) return thinking._block;
    } catch (_) {}
    return null;
  }

  function appendToken(step, delta) {
    // Buffer only — nothing is shown live. The reply is typed once at done
    // with the normal animation; tool-phase text moves inside its event.
    if (typeof delta !== "string" || !delta) return;
    try {
      if (typeof step === "number") {
        tokenSteps.set(step, (tokenSteps.get(step) || "") + delta);
      }
    } catch (_) {}
  }

  function appendReplyPart(msg) {
    // One accepted long-reply part: show it NOW, exactly where the final
    // answer goes (fake stream). The whole accumulated text is re-rendered
    // instantly so Markdown spanning two parts still reads correctly.
    try {
      const text = String(msg?.text || "");
      if (!text) return;
      ensureShell();
      if (!livePartsEl || !livePartsEl.isConnected) {
        livePartsEl = document.createElement("div");
        livePartsEl.className = "msg-live-parts";
        livePartsEl.setAttribute("dir", "auto");
        try {
          shellP.parentNode.insertBefore(livePartsEl, shellP);
          shellP.remove();
        } catch (_) {
          try { shell.querySelector(".msg-content")?.appendChild(livePartsEl); } catch (_) {}
        }
      }
      // Same join rule as the engine: add a separator only when the boundary
      // would otherwise glue text together.
      if (livePartsRaw && !/[\s]$/.test(livePartsRaw) && !/^\s/.test(text)) livePartsRaw += "\n\n";
      livePartsRaw += text;
      livePartsEl.innerHTML = formatRich(livePartsRaw);
      try { linkifyFolderPaths(livePartsEl); } catch (_) {}
      scrollToBottom();
    } catch (_) {}
  }

  function ensureShell() {
    if (shell) return;
    const s = appendAssistantShell();
    shell = s.el;
    shellP = s.p;
    // The thinking bubble stays above; the answer shell goes right after it.
    try {
      if (thinking && thinking.isConnected) thinking.after(shell);
    } catch (_) {}
    // Freeze direction from the user's message at stream start — never
    // re-applied at done, so the bubble never flips after finishing.
    applyMsgDir(shell, value, "ltr");
    // Reserve the action-bar height now: icons appearing at done push nothing.
    const ph = document.createElement("div");
    ph.className = "msg-actions below msg-actions-ph";
    shell.appendChild(ph);
  }
  function appendStepStart(step) {
    // A tool call appears immediately: title + reason, open block,
    // bullet pending. Completion fills output/image and marks it done.
    const block = activeBlock();
    if (!block) {
      ensureShell();
      scrollToBottom();
      return;
    }
    const text = humanizeEvent(typeof step === "string" ? step : step?.event || "");
    if (!text) {
      scrollToBottom();
      return;
    }
    const key = stepKey(step);
    if (liveItems.has(key)) {
      scrollToBottom();
      return;
    }
    try {
      const item = createStepItem({
        event: text,
        reason: typeof step === "object" && step ? step.reason : "",
      });
      markStepRunning(item);
      block._stepsInner.appendChild(item);
      // Tool-phase text of this loop step moves inside the event.
      flushStepTokens(typeof step === "object" && step ? step.loopStep : undefined, item);
      // First step: the header arrow appears now.
      ensureHeaderArrow(block);
      liveItems.set(key, item);
      collected.push({ event: text, reason: stepReasonText(step), key });
    } catch (_) {}
    scrollToBottom();
  }
  function appendStep(step) {
    // Completion: the started step gains output/image + green bullet.
    // Never duplicates. A step never announced at start is created whole.
    const block = activeBlock();
    const text = humanizeEvent(typeof step === "string" ? step : step?.event || "");
    if (!text) {
      if (!block) ensureShell();
      scrollToBottom();
      return;
    }
    const key = stepKey(step);
    const output = typeof step === "object" && step ? step.output || step.out || null : null;
    const reason = typeof step === "object" && step ? step.reason || "" : "";
    const media = typeof step === "object" && step && step.media && typeof step.media === "object" ? step.media : null;
    const shot = media && typeof media.shot === "string" && media.shot
      ? media.shot
      : (typeof step === "object" && step && typeof step.shot === "string" ? step.shot : "");
    const genImage = media && typeof media.image === "string" && media.image
      ? media.image
      : (typeof step === "object" && step && typeof step.image === "string" ? step.image : "");
    if (!block) {
      ensureShell();
      collected.push({ event: text, output, key, media, reason });
      scrollToBottom();
      return;
    }
    let item = liveItems.get(key);
    if (!item) {
      try {
        item = createStepItem({ event: text, reason, output, media, shot, image: genImage });
        block._stepsInner.appendChild(item);
        // First step: the header arrow appears now.
        ensureHeaderArrow(block);
        liveItems.set(key, item);
      } catch (_) {
        scrollToBottom();
        return;
      }
    } else {
      // The completion label may differ from the preview (Answered, errors):
      // the row keeps its place and takes the final title.
      try {
        const titleEl = item._head && item._head.querySelector(":scope > .step-title");
        if (titleEl && text && titleEl.textContent !== text) titleEl.textContent = text;
      } catch (_) {}
      completeStepItem(item, {
        output: typeof output === "string" ? output : "",
        shot: typeof shot === "string" ? shot : "",
        image: typeof genImage === "string" ? genImage : "",
        failed: typeof step === "object" && !!step && step.failed === true,
      });
    }
    // Tool-phase text of this loop step moves inside the event.
    flushStepTokens(typeof step === "object" && step ? step.loopStep : undefined, item);
    // A completed call carrying a watch stream opens the player bar by itself
    // (e.g. browser start) — no pause, the loop keeps running.
    try {
      const st = media && media.stream;
      if (st && typeof st.url === "string" && st.url) {
        if (st.tool === "show_preview") showPreviewPlayer(st.url);
        else if (st.view_only !== false) {
          markLiveSeen();
          showEdgeTab(st.url);
        }
      }
    } catch (_) {}
    const idx = collected.findIndex((c) => stepKey(c) === key);
    const entry = { event: text, output, key, media, reason };
    if (idx >= 0) collected[idx] = entry;
    else collected.push(entry);
    scrollToBottom();
  }
  // SSE frames arrive in the POST response body ("data: {...}\n\n" each).
  // Read them live with a streaming reader — instant, no polling.
  let doneReceived = false;
  let turnResult = null; // true = handled, false = fall back
  function onFrame(msg2) {
    if (turnResult !== null) return;
    try {
      if (!activeRun || activeRun.gen !== myGen) {
        turnResult = true;
        return;
      }
    } catch (_) {}
    try {
        if (msg2?.type === "event_start") appendStepStart({ event: msg2.event, key: msg2.key, reason: msg2.reason, loopStep: msg2.step });
        else if (msg2?.type === "event") appendStep({ event: msg2.event, output: msg2.output, key: msg2.key, media: msg2.media, reason: msg2.reason, shot: msg2.shot, loopStep: msg2.step });
        else if (msg2?.type === "token") appendToken(msg2.step, msg2.text);
        else if (msg2?.type === "reply_part") appendReplyPart(msg2);
        else if (msg2?.type === "live") {
          showLiveLink(msg2.live);
        }
        else if (msg2?.type === "live_end") clearLiveUI();
        else if (msg2?.type === "question") {
          if (msg2.kind === "live") {
            markLiveSeen();
            if (msg2.view_only) showEdgeTab(msg2.url, msg2.id);
            else showLiveControlCard(msg2);
          }             else showQuestionCardUnlessStopped(msg2);
        }
        else if (msg2?.type === "question-expired") {
          if (pendingQuestion && pendingQuestion.id === msg2.id) {
            hideQuestionCard();
            clearPendingStored();
            try { pendingModelConnect = null; } catch (_) {}
            toast("Question expired — continued without answer");
          }
        } else if (msg2?.type === "cancelled") {
          // Stopped mid-task: the Working block stays visible (frozen with
          // elapsed seconds, still expandable) plus a --- Stopped ---
          // divider below it. Only the empty answer shell is dropped.
          if (shell) {
            try {
              shell.remove();
            } catch (_) {}
            shell = null;
          }
          livePartsEl = null;
          try {
            settleThinkingStopped(thinking);
          } catch (_) {}
          // The task stopped mid-way: arm the Continue pill (empty composer
          // only). The user id lets Continue resume in place with no bubble.
          try {
            const _mid = (lastUserEl && lastUserEl.dataset && lastUserEl.dataset.mid) || "";
            setStoppedTask(_mid || readStoppedUserId() || "1");
          } catch (_) {}
          try { clearPendingUser(); } catch (_) {}
          // A stopped turn owns no question: drop any stale strip + store
          // so refresh never restores it.
          try { hideQuestionCard(); } catch (_) {}
          try { clearPendingStored(); } catch (_) {}
          activeThinking = null;
          activeRun = null;
          turnResult = true;
          return;
        } else if (msg2?.type === "done") {
          // Sync mark FIRST: the server already persisted this reply. The
          // reader loop must stop here and no blocking fallback may run —
          // the async render below (typing animation) takes seconds, and
          // without this the stream close reads as "not done" and resends
          // via /api/chat, persisting a second duplicate reply.
          doneReceived = true;
          turnResult = true;
          // The turn finished (including a continued task): no stopped task
          // remains, so the Continue pill goes away immediately.
          try { clearStoppedTask(); } catch (_) {}
          try { clearPendingUser(); } catch (_) {}
          (async () => {
            try {
          // Push beat the stream: chat_done fired first and the history
          // reload already rendered this persisted reply (same assistant
          // id). Drop local artifacts and render nothing — one copy only.
          try {
            const _aid = msg2 && msg2.ids && msg2.ids.assistant;
            const _key = _aid ? String(_aid).replace(/[^0-9a-zA-Z-]/g, "") : "";
            if (_key && chatInner && chatInner.querySelector('.msg.assistant[data-mid="' + _key + '"]')) {
              try { thinking.remove(); } catch (_) {}
              try { if (shell) shell.remove(); } catch (_) {}
              shell = null;
              livePartsEl = null;
              doneReceived = true;
              activeThinking = null;
              activeRun = null;
              turnResult = true;
              return;
            }
          } catch (_) {}
          const replyText = String(msg2.reply || "");
          // Finalization: the thinking-block settles (Working stops,
          // collapsed) and stays above; the final reply is always last.
          let finalEl;
          const block = activeBlock();
          // Steps decide the block's fate: with steps it settles above the
          // reply; with none the Working artifact is dropped entirely.
          const liveHadSteps = !!(block && block._stepsInner && block._stepsInner.children.length);
          if (block && liveHadSteps) settleThinkingBlock(block);
          if (shell) {
            // Direction stays as frozen at stream start (never re-applied here).
            const content = shell.querySelector(".msg-content");
            try {
              shellP.remove();
            } catch (_) {}
            // The settled block moves to the top of the final message.
            // No steps: the Working artifact is dropped, reply only.
            try {
              if (block && block.isConnected && liveHadSteps) content?.prepend(block);
            } catch (_) {}
            try {
              thinking.remove();
            } catch (_) {}
            // Long reply already delivered part-by-part: render the final
            // rich text IN PLACE (instant) — retyping would blank the answer
            // the user just watched arrive and replay it for long seconds.
            let holder = null;
            if (livePartsEl && livePartsEl.isConnected) {
              holder = livePartsEl;
              try {
                holder.innerHTML = formatRich(replyText);
                try { linkifyFolderPaths(holder); } catch (_) {}
              } catch (_) {
                await typeRich(holder, replyText);
              }
            } else {
              holder = document.createElement("div");
              try {
                content?.appendChild(holder);
              } catch (_) {
                shell.appendChild(holder);
              }
              await typeRich(holder, replyText);
            }
            shellP = holder;
            livePartsEl = null;
            appendFinalImages(shell, msg2.images);
            appendFinalVoice(shell, msg2.audio);
            appendFinalFiles(shell, msg2.files);
            finalEl = shell;
            shell = null;
          } else if (liveHadSteps) {
            // No streamed text: the thinking bubble BECOMES the final message.
            try {
              thinking.removeAttribute("id");
            } catch (_) {}
            const holder = document.createElement("div");
            holder.className = "msg-content";
            thinking.appendChild(holder);
            await typeRich(holder, replyText);
            appendFinalImages(thinking, msg2.images);
            appendFinalVoice(thinking, msg2.audio);
            appendFinalFiles(thinking, msg2.files);
            finalEl = thinking;
          } else {
            // Direct answer, no steps: Working artifact dropped, reply only.
            thinking.remove();
            finalEl = await typeMessage(replyText, collected);
            appendFinalImages(finalEl, msg2.images);
            appendFinalVoice(finalEl, msg2.audio);
            appendFinalFiles(finalEl, msg2.files);
          }
          const ids = msg2.ids || null;
          if (lastUserEl && ids && ids.user) lastUserEl.dataset.mid = ids.user;
          if (ids && ids.assistant) finalEl.dataset.mid = ids.assistant;
          finalEl.dataset.raw = replyText;
          // A resumed turn stores NO prompt: regenerate falls back to the
          // original request bubble, never the internal instruction.
          finalEl.dataset.userText = isResume ? "" : value;
          attachAssistantBar(finalEl, replyText);
          doneReceived = true;
          // Turn finished: no question can still own the strip — drop any
          // stale store so refresh never restores a finished essay.
          try { clearPendingStored(); } catch (_) {}
          activeThinking = null;
          activeRun = null;
          turnResult = true;
            } catch (_) {
              turnResult = true;
            }
          })();
          return;
        } else if (msg2?.type === "error") {
          try {
            thinking.remove();
            if (shell) shell.remove();
          } catch (_) {}
          livePartsEl = null;
          try { hideQuestionCard(); } catch (_) {}
          try { clearPendingStored(); } catch (_) {}
          activeThinking = null;
          activeRun = null;
          const { el: errEl, p: perr } = appendAssistantShell();
          perr.textContent =
            `Error: ${[msg2.error, msg2.detail].filter(Boolean).join(" — ") || "request failed"}`;
          if (msg2.needs_model) appendModelConnectButton(errEl, msg2.needs_kind);
          scrollToBottom();
          turnResult = true;
          return;
        }
    } catch (_) {}
  }
  // Stream the SSE body: split on blank lines, parse each "data:" frame.
  try {
    const reader = res.body.getReader();
    const decoder = new TextDecoder("utf-8");
    let buf = "";
    for (;;) {
      if (turnResult !== null) break;
      try {
        if (!activeRun || activeRun.gen !== myGen) {
          turnResult = true;
          break;
        }
      } catch (_) {}
      let chunk;
      try {
        chunk = await reader.read();
      } catch (_) {
        break;
      }
      if (chunk.done) break;
      try {
        buf += decoder.decode(chunk.value, { stream: true });
      } catch (_) {
        break;
      }
      let idx;
      while (turnResult === null && (idx = buf.indexOf("\n\n")) >= 0) {
        const part = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        for (const line of part.split("\n")) {
          const t = line.trim();
          if (!t.startsWith("data:")) continue;
          let msg2 = null;
          try {
            msg2 = JSON.parse(t.slice(5).trim());
          } catch (_) {
            continue;
          }
          if (msg2 && typeof msg2 === "object") onFrame(msg2);
          if (turnResult !== null) break;
        }
      }
    }
    try { await reader.cancel(); } catch (_) {}
  } catch (_) {
    // Genuine transport break below decides fallback vs quiet exit.
  } finally {
    try {
      turnResult = turnResult === null ? true : turnResult;
    } catch (_) {}
  }
  if (!doneReceived) {
    // Superseded turn (STOP / fresh answer / newer message took over):
    // exit quietly — no error text, no blocking fallback. Only a genuine
    // break of the OWNED stream still shows "Connection lost".
    if (!activeRun || activeRun.gen !== myGen) return true;
    if (!shell && !(thinking && thinking.isConnected && thinking._block)) {
      thinking.remove();
      return false;
    }
    const perr = document.createElement("p");
    perr.textContent = "Connection lost — please try again.";
    try {
      (shell || thinking).querySelector(".msg-content")?.appendChild(perr);
    } catch (_) {
      try {
        (shell || thinking).appendChild(perr);
      } catch (_) {}
    }
    scrollToBottom();
  }
  return turnResult === null ? true : turnResult;
}

const SEND_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="19" x2="12" y2="5" /><polyline points="6 11 12 5 18 11" /></svg>`;
const STOP_SVG = `<svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>`;

function setSending(sending) {
  if (!sendBtn) return;
  // Generation owns the button: drop any prune/regenerate loading ring so
  // the STOP icon shows and stays clickable (loading blocks pointer events).
  if (sending) sendBtn.classList.remove("loading");
  sendBtn.classList.toggle("stop", sending);
  // Generating owns the button as STOP — a Continue pill never survives it.
  if (sending) sendBtn.classList.remove("continue");
  sendBtn.innerHTML = sending ? STOP_SVG : SEND_SVG;
  sendBtn.setAttribute("aria-label", sending ? "Stop generation" : "Send message");
  sendBtn.title = sending ? "Stop generation" : "Send message";
  if (sending) {
    sendBtn.disabled = false;
  } else {
    updateSendEnabled();
  }
}

async function sendBlocking(value, attachments, thinking, skipHistoryIds, isResume) {
  try {
    const blockBody = { message: isResume ? "" : value, attachments };
    if (isResume) blockBody.resume = true;
    if (pendingSkill) blockBody.skill = pendingSkill;
    if (pendingFolderRefs.length) blockBody.folder_refs = pendingFolderRefs.map((r) => ({ name: r.name, path: r.path }));
    if (skipHistoryIds && skipHistoryIds.length) blockBody.skip_history_ids = skipHistoryIds;
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(blockBody),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      // Fail clearly as an error state (never a canned reply): show the
      // server detail too so the real cause is visible, and carry the
      // no-model flag so the error bubble still offers the Models shortcut.
      const err = new Error(
        [data?.error, data?.detail].filter(Boolean).join(" — ") || `HTTP ${res.status}`
      );
      err.needsModel = Boolean(data?.needs_model);
      err.needsKind = String(data?.needs_kind || "text");
      throw err;
    }
    thinking.remove();
    activeThinking = null;
    const replyText = String(data?.reply || "");
    const finalEl = await typeMessage(replyText, data?.events, data?.elapsed);
    // Blocking-path completion ends any stopped task, same as the stream.
    try { clearStoppedTask(); } catch (_) {}
    appendFinalImages(finalEl, data?.images);
    appendFinalVoice(finalEl, data?.audio);
    appendFinalFiles(finalEl, data?.files);
    const ids = data?.ids || null;
    if (lastUserEl && ids && ids.user) lastUserEl.dataset.mid = ids.user;
    if (ids && ids.assistant) finalEl.dataset.mid = ids.assistant;
    finalEl.dataset.raw = replyText;
    // A resumed turn stores NO prompt (regenerate uses the original bubble).
    finalEl.dataset.userText = isResume ? "" : value;
    attachAssistantBar(finalEl, replyText);
  } catch (err) {
    thinking.remove();
    activeThinking = null;
    const { el: errEl, p } = appendAssistantShell();
    p.textContent = `Error: ${err instanceof Error ? err.message : String(err)}`;
    // No model connected: offer the one action that fixes it — AI Models with
    // the connect dialog already open (same shortcut as the pause strip).
    if (err && err.needsModel) appendModelConnectButton(errEl, err.needsKind);
    scrollToBottom();
  }
}

async function handleSend() {
  // Editing mode: SEND saves the change into the SAME bubble (no new message).
  if (editingTarget) {
    await saveComposerEdit(composerInput.value);
    return;
  }
  // Open agent question: choice (or own reply) + SEND.
  if (pendingQuestion && pendingQuestion.kind === "ask") {
    await sendQuestionAnswer();
    return;
  }
  // Model-activation pause: the loop waits for the strip's Skip / Connect.
  if (pendingQuestion && pendingQuestion.kind === "needs_model") {
    toast("Pick Skip or Connect first");
    return;
  }
  // Single-task invariant: while a run is live (local stream or resumed
  // pre-refresh catch-up) the button is STOP, never SEND — a new task starts
  // only after STOP. Defense-in-depth for any direct call path.
  if (continueBusy) return;
  try {
    if (sendBtn && sendBtn.classList.contains("stop")) return;
  } catch (_) {}
  if (activeRun || (catchupResume && activeThinking)) {
    toast("A task is still running — STOP it first");
    return;
  }
  const value = composerInput.value.trim().slice(0, MAX_MESSAGE);
  const pending = pendingAttachments.slice();
  // Picks count only while their "@name" / "/name" token is still in the text.
  if (pendingSkill && !hasToken(value, "/", pendingSkill.name)) pendingSkill = null;
  pendingFolderRefs = pendingFolderRefs.filter((r) => hasToken(value, "@", r.name));
  if (!value && !pending.length && !pendingSkill && !pendingFolderRefs.length) return;
  if (isUploadingFiles()) {
    toast("Waiting for files to finish uploading…");
    return;
  }
  if (hasFailedFiles()) {
    toast("Remove failed files to send…");
    return;
  }

  // A new request supersedes any running generation.
  await abortActiveStream();

  // A typed message is a brand-new request: a previously stopped task is
  // superseded and its Continue pill goes away with it.
  clearStoppedTask();

  lastUserEl = appendUserMessage(value, pending);
  attachUserBar(lastUserEl);
  // Stash this turn's picks on the bubble so regenerate / edit-resend
  // inject the SAME @ ids + / skill, then paint its @ names blue.
  try { stashBubbleRefs(lastUserEl, pending); } catch (_) {}
  try { paintBubbleMentions(lastUserEl); } catch (_) {}
  if (value) lastUserText = value;
  try { storePendingUser(value); } catch (_) {}
  hideMentionMenu();

  // Real file bytes go to the server (bounded, compressed); the agent loop analyzes them.
  // @image1.. labels in the text match the image order here and on the server.
  let attachments = [];
  try {
    for (const item of pending) {
      // Documents are already read at attach time (progress card); wait for
      // the read and reuse the bytes instead of reading twice.
      if (item._ready) {
        try {
          await item._ready;
        } catch (_) {}
      }
      const ready = item.type === "image" ? await maybeCompressImage(item.file) : item.file;
      const dataUrl = item.dataUrl || (await readAsDataURL(ready));
      if (item.type === "document" && !dataUrl) {
        toast("Remove failed files to send…");
        updateSendEnabled();
        return;
      }
      attachments.push({ type: item.type, name: ready.name || item.name, data: dataUrl });
    }
  } catch (_) {
    toast("File too large (image 8MB / video 24MB / document 100MB)");
    return;
  }

  composerInput.value = "";
  autoResize();
  clearAttachment();
  await executeAgentTurn(value, attachments);
  // Picks are one-shot: a new turn starts clean.
  pendingSkill = null;
  pendingFolderRefs = [];
}

async function resolveStoppedUserId() {
  // The stored stopped-task user id, resolved against persisted history
  // when it was never captured live ("1": STOP pressed before the first
  // persist). Without the real id Continue would persist a stray "Continue"
  // user bubble and the old Stopped divider could never clear. Never throws.
  try {
    const stored = readStoppedUserId();
    if (stored && stored !== "1") return stored;
    const res = await fetch("/api/conversation", { cache: "no-store" });
    if (!res.ok) return stored;
    const data = await res.json().catch(() => ({}));
    const msgs = Array.isArray(data?.messages) ? data.messages : [];
    const valid = msgs.filter((m) => m && (m.role === "user" || m.role === "assistant") && String(m.content || "").trim());
    // Trailing-only: only the LAST assistant message may own Continue. A
    // buried stopped marker (a newer task was sent after it) must never be
    // resurrected — otherwise Continue resumes the old task under the new one.
    let lastAsst = null;
    let lastAsstIdx = -1;
    for (let i = valid.length - 1; i >= 0; i--) {
      if (valid[i].role === "assistant") { lastAsst = valid[i]; lastAsstIdx = i; break; }
    }
    if (lastAsst && isStoppedMarker(lastAsst) && lastAsst.resumed !== true) {
      for (let j = lastAsstIdx - 1; j >= 0; j--) {
        if (valid[j].role === "user" && valid[j].id) {
          const uid = String(valid[j].id).slice(0, 16);
          try { setStoppedTask(uid); } catch (_) {}
          return uid;
        }
      }
    }
    return stored;
  } catch (_) {
    try { return readStoppedUserId(); } catch (_) { return ""; }
  }
}

async function handleContinue() {
  // Seamless resume of the stopped task: no user bubble, nothing sent — the
  // server resumes the original task by id (skip_history_ids + resume:true)
  // and the model continues as if the task never stopped (no instruction,
  // no stopped note in its context). Typed text never reaches here (that
  // path is a brand-new request via handleSend). Never throws outward.
  if (continueBusy) return;
  try {
    continueBusy = true;
    let stoppedId = readStoppedUserId();
    if (!stoppedId || stoppedId === "1") {
      const t0 = Date.now();
      while (Date.now() - t0 < 8000) {
        stoppedId = await resolveStoppedUserId();
        if (stoppedId && stoppedId !== "1") break;
        await new Promise((r) => setTimeout(r, 250));
      }
    }
    // A live run (local or resumed pre-refresh catch-up) owns the button:
    // Continue must never start the old task under it.
    if (activeRun || catchupResume) return;
    if (!stoppedId || stoppedId === "1") {
      if (!activeRun) toast("Stopped task expired — send as a new message");
      return;
    }
    if (editingTarget || pendingQuestion) return;
    if ((composerInput.value || "").trim()) return;
    if (pendingAttachments.length || pendingSkill || pendingFolderRefs.length) return;
    hideMentionMenu();
    // Adopt the stopped Working block so the continuation lands in the SAME
    // block and step list: unstarted steps start there, an unfinished step
    // completes there, finished steps are followed by the next ones. Prefers
    // the live frozen bubble, else the history-rendered stopped marker.
    // The block is only adopted when it is still attached with a step list.
    let adopted = null;
    try {
      const live = document.getElementById("thinkingMsg");
      if (live && live.dataset && live.dataset.stopped === "1") adopted = live;
      else {
        const all = chatInner ? Array.from(chatInner.querySelectorAll(".msg.assistant")) : [];
        for (let i = all.length - 1; i >= 0; i--) {
          try {
            if (all[i].querySelector(":scope .stopped-line")) { adopted = all[i]; break; }
          } catch (_) {}
        }
      }
      if (adopted) {
        if (!adopted._block) {
          try { adopted._block = adopted.querySelector(".thinking-block") || null; } catch (_) { adopted._block = null; }
        }
        if (!adopted._block || !adopted._block._stepsInner) adopted = null;
      }
      if (typeof lastUserEl !== "undefined" && lastUserEl && lastUserEl.dataset) {
        delete lastUserEl.dataset.stopped;
      }
    } catch (_) { adopted = null; }
    composerInput.value = "";
    autoResize();
    // resume:true travels with the reused id; the message body itself goes
    // out EMPTY so the internal instruction can never surface anywhere.
    let opts = { resume: true, skipHistoryIds: [stoppedId] };
    if (adopted) {
      opts.reuseThinking = adopted;
    }
    await executeAgentTurn(CONTINUE_INPUT, [], opts);
  } catch (_) {} finally {
    try { continueBusy = false; } catch (_) {}
  }
}

async function executeAgentTurn(value, attachments, opts) {
  // Runs one agent request WITHOUT appending a new user bubble.
  // Used by normal send (bubble already appended) and by resend/regenerate.
  // Guard: only ONE turn runs at a time — a second call while one is active
  // is ignored (that was the double-reply bug: edit/regenerate racing send).
  // opts.skipHistoryIds: when re-sending an EDITED message, the server reuses
  // the existing persisted user bubble instead of appending a duplicate.
  // opts.reuseThinking: Continue adopts the stopped Working element so the
  // run resumes the SAME block/step list instead of opening a new bubble.
  // opts.resume: seamless Continue — nothing is sent (empty message +
  // resume:true); the server resumes the stopped task by id and the model
  // never sees the internal instruction either.
  if (activeRun) return;
  // The composer stays enabled so the user can type during generation
  // (pressing SEND while generating stops the loop, typed text is kept).
  // A catch-up run from a pre-refresh generation no longer owns the UI.
  // A new local run takes over; completion still arrives via push.
  catchupResume = false;
  try { resumePending = false; resumeSettled = false; } catch (_) {}
  // A new agent turn may legitimately start the browser again.
  clearBrowserClosedFlag();
  const adopted = opts && opts.reuseThinking && opts.reuseThinking.isConnected && opts.reuseThinking._block
    ? opts.reuseThinking : null;
  // A stopped task stays on screen (settled Working + Stopped line) — only
  // a live bubble is dropped; a stopped one is demoted (id stripped) and
  // kept, and the next history reload replaces it with its persisted copy.
  // An adopted block is never demoted: it becomes the live run again.
  try {
    const prev = document.getElementById("thinkingMsg");
    if (prev && prev !== adopted) {
      if (prev.dataset.stopped === "1") prev.removeAttribute("id");
      else prev.remove();
    }
  } catch (_) {}
  activeThinking = null;
  setSending(true);

  let thinking;
  if (adopted) {
    // Same-block resume: own the frozen bubble again, drop THIS task's
    // Stopped divider(s), and wake its Working wave — steps continue below.
    thinking = adopted;
    try {
      adopted.id = "thinkingMsg";
      try { adopted.querySelectorAll(".stopped-line").forEach((n) => { try { n.remove(); } catch (_) {} }); } catch (_) {}
      try {
        const nx = adopted.nextElementSibling;
        if (nx && nx.classList && nx.classList.contains("stopped-line")) nx.remove();
      } catch (_) {}
      try { delete adopted.dataset.stopped; } catch (_) {}
      resumeThinkingBlock(adopted._block);
      scrollToBottom();
    } catch (_) {}
  } else {
    thinking = addThinking(value);
  }
  const skipIds = opts && Array.isArray(opts.skipHistoryIds) ? opts.skipHistoryIds : null;
  const isResume = !!(opts && opts.resume);
  // Turn token: STOP/abort bumps stopGen, so a retry sleeping through an
  // explicit STOP never auto-starts afterwards (single-task invariant).
  const turnGen = stopGen;
  try {
    let handled = false;
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        handled = await sendStream(value, attachments, thinking, skipIds, isResume);
        break;
      } catch (e) {
        if (e && e.aborted) return;
        if (e && e.retry && attempt < 3) {
          try { await new Promise((r) => setTimeout(r, 750 * (attempt + 1))); } catch (_) {}
          // An explicit STOP/abort during backoff wins — exit quietly and
          // leave the frozen state STOP produced untouched.
          try { if (turnGen !== stopGen) return; } catch (_) { return; }
          try { if (sendBtn && !sendBtn.classList.contains("stop")) return; } catch (_) { return; }
          continue;
        }
        handled = false;
        break;
      }
    }
    if (!handled) await sendBlocking(value, attachments, thinking, skipIds, isResume);
  } finally {
    // Only clear state this turn still owns — a newer turn may have taken
    // over while this one was winding down.
    try {
      if (!activeRun || activeRun.thinking === thinking) {
        activeRun = null;
        setSending(false);
      }
    } catch (_) {}
    composerInput.focus();
  }
}

function autoResize() {
  if (!composerInput) return;
  composerInput.style.height = "auto";
  composerInput.style.height = `${Math.min(composerInput.scrollHeight, 160)}px`;
  try {
    renderComposerHL();
  } catch (_) {}
  // Edge glow while the user is writing (simple, transform-free pulse in CSS).
  try {
    document.getElementById("composer")?.classList.toggle(
      "has-text", composerInput.value.trim().length > 0);
  } catch (_) {}
}

/* ---------- attachments (metadata only, never uploaded) ---------- */
function imageLabelFor(index) {
  return `image${index + 1}`;
}

function pendingImages() {
  return pendingAttachments.filter((a) => a.type === "image");
}

function formatSize(bytes) {
  const kb = bytes / 1024;
  return kb > 1024 ? `${(kb / 1024).toFixed(1)} MB` : `${Math.round(kb)} KB`;
}

function showAttachmentPreview(file, type) {
  if (pendingAttachments.length >= 2) {
    toast("Max 2 attachments");
    return;
  }
  const item = { type, name: file.name, file, thumb: null, uid: `doc${Date.now().toString(36)}${pendingAttachments.length}` };
  if (type !== "document") {
    try {
      item.thumb = URL.createObjectURL(file);
    } catch (_) {
      item.thumb = null;
    }
  }
  // Documents load async — send stays disabled until _done. Images/video are instant.
  item._done = type !== "document";
  pendingAttachments.push(item);
  renderAttachmentPreviews();
  updateSendEnabled();
  if (type === "document") startDocumentRead(item);
}

/* Document card: read once at attach time so the edge bar shows real progress.
   The bytes are reused at send — never read twice. */

/* Unified document icon: one glyph, tinted by file type only.
   Key always comes from this allowlist, so it is safe as a CSS class. */
const DOC_TYPE_EXTS = {
  pdf: ["pdf"],
  doc: ["doc", "docx"],
  xls: ["xls", "xlsx", "csv"],
  ppt: ["ppt", "pptx"],
};
function docTypeOf(name) {
  const ext =
    typeof name === "string" && name.includes(".") ? name.toLowerCase().split(".").pop() : "";
  for (const key of Object.keys(DOC_TYPE_EXTS)) {
    if (DOC_TYPE_EXTS[key].includes(ext)) return key;
  }
  return "txt";
}

/* Human label for the file type line (second row under the name). */
function docLabelFor(name) {
  const ext =
    typeof name === "string" && name.includes(".") ? name.toLowerCase().split(".").pop() : "";
  if (ext === "pdf") return "PDF";
  if (ext === "doc" || ext === "docx") return "Word";
  if (ext === "xls" || ext === "xlsx") return "Excel";
  if (ext === "csv") return "CSV";
  if (ext === "ppt" || ext === "pptx") return "PowerPoint";
  if (ext === "txt") return "Text";
  if (ext === "md") return "Markdown";
  if (ext === "json" || ext === "xml" || ext === "html") return ext.toUpperCase();
  return ext ? ext.toUpperCase() : "File";
}

function isUploadingFiles() {
  return pendingAttachments.some((a) => a && a.type === "document" && !a._done);
}

function hasFailedFiles() {
  return pendingAttachments.some((a) => a && a.type === "document" && a._done && !a.dataUrl);
}

function isContinueMode() {
  // The send button is a Continue pill only when: idle, a task is stopped
  // mid-way, and the composer is completely empty. Any typed text, pick, or
  // attachment means a brand-new request instead (normal SEND). Never throws.
  try {
    if (!sendBtn || sendBtn.classList.contains("stop")) return false;
    // A live run — including a resumed pre-refresh run this tab follows via
    // push (no local stream) — never shows Continue under it.
    if (activeRun || catchupResume || resumePending) return false;
    if (editingTarget || pendingQuestion) return false;
    if (pendingAttachments.length || pendingSkill || pendingFolderRefs.length) return false;
    if (isUploadingFiles() || hasFailedFiles()) return false;
    if ((composerInput.value || "").trim()) return false;
    return !!readStoppedUserId();
  } catch (_) { return false; }
}
function refreshContinueButton() {
  // Returns true while the Continue pill owns the button. Adding the class
  // restarts the CSS entrance animation. Never throws.
  try {
    if (!sendBtn || sendBtn.classList.contains("stop")) return false;
    const on = isContinueMode();
    const was = sendBtn.classList.contains("continue");
    if (on) {
      sendBtn.classList.add("continue");
      // Enforced on EVERY refresh, not just on arming: a late
      // setSending(false) rewrites innerHTML (arrow) after the pill was
      // armed, which used to leave an oval+arrow mix with no label.
      // The label always wins while Continue owns the button.
      if (sendBtn.textContent !== CONTINUE_LABEL) sendBtn.textContent = CONTINUE_LABEL;
      sendBtn.setAttribute("aria-label", "Continue stopped task");
      sendBtn.title = "Continue stopped task";
      sendBtn.disabled = false;
      return true;
    }
    if (was) {
      sendBtn.classList.remove("continue");
      sendBtn.innerHTML = SEND_SVG;
      sendBtn.setAttribute("aria-label", "Send message");
      sendBtn.title = "Send message";
    }
    return false;
  } catch (_) { return false; }
}
function updateSendEnabled() {
  if (!sendBtn) return;
  // While generating the button is STOP and must stay clickable.
  if (sendBtn.classList.contains("stop")) return;
  // A stopped task with an empty composer owns the button as Continue.
  if (refreshContinueButton()) return;
  // Send is enabled only when every attached file succeeded — and an
  // empty composer with nothing pending stays disabled (a normal send
  // with nothing to send). Question strips are exempt: chip choices go
  // through SEND with an empty composer. Edit mode is exempt too: its own
  // updater owns the disabled state right after this.
  const blocked = isUploadingFiles() || hasFailedFiles();
  let idleEmpty = false;
  try {
    idleEmpty = !(composerInput.value || "").trim()
      && !pendingAttachments.length && !pendingSkill && !pendingFolderRefs.length
      && !pendingQuestion && !editingTarget;
  } catch (_) { idleEmpty = false; }
  sendBtn.disabled = blocked || idleEmpty;
  sendBtn.classList.toggle("is-waiting", blocked);
  try {
    if (composerInput) {
      if (isUploadingFiles()) composerInput.placeholder = "Uploading files…";
      else if (hasFailedFiles()) composerInput.placeholder = "Remove failed files to send…";
      else composerInput.placeholder = "Message your agent…";
    }
  } catch (_) {}
}

function docRowFor(uid) {
  if (!composerAttachBox) return null;
  return composerAttachBox.querySelector(`[data-uid="${uid}"]`);
}

function updateDocProgress(uid, frac) {
  const row = docRowFor(uid);
  if (!row) return;
  const sizeEl = row.querySelector(".attach-doc-size");
  const p = Math.min(0.999, Math.max(0, frac || 0));
  try {
    row.style.setProperty("--p", p.toFixed(3));
  } catch (_) {}
  row.classList.add("is-loading");
  row.classList.remove("is-ready", "is-error");
  if (sizeEl) sizeEl.textContent = `Loading… ${Math.round(p * 100)}%`;
}

function finishDocRead(uid, ok, size) {
  const row = docRowFor(uid);
  const item = pendingAttachments.find((a) => a && a.uid === uid);
  if (item) {
    item._done = true;
    item._failed = !ok;
  }
  if (row) {
    const sizeEl = row.querySelector(".attach-doc-size");
    try {
      row.style.setProperty("--p", ok ? "1" : "0");
    } catch (_) {}
    row.classList.remove("is-loading");
    row.classList.add(ok ? "is-ready" : "is-error");
    if (sizeEl) sizeEl.textContent = ok ? formatSize(size) : "Failed to load";
  }
  updateSendEnabled();
}

function startDocumentRead(item) {
  item._done = false;
  item._failed = false;
  updateSendEnabled();
  updateDocProgress(item.uid, 0);
  item._ready = new Promise((resolve) => {
    const reader = new FileReader();
    reader.onprogress = (e) => {
      if (e.lengthComputable && e.total > 0) updateDocProgress(item.uid, e.loaded / e.total);
    };
    reader.onload = () => {
      item.dataUrl = typeof reader.result === "string" ? reader.result : null;
      finishDocRead(item.uid, !!item.dataUrl, item.file.size);
      resolve();
    };
    reader.onerror = () => {
      finishDocRead(item.uid, false, 0);
      resolve();
    };
    try {
      reader.readAsDataURL(item.file);
    } catch (_) {
      finishDocRead(item.uid, false, 0);
      resolve();
    }
  });
}

function removeAttachmentAt(index) {
  const item = pendingAttachments[index];
  const row = item ? docRowFor(item.uid) : null;
  const dropNow = () => {
    const at = item ? pendingAttachments.indexOf(item) : index;
    const gone = at >= 0 ? pendingAttachments.splice(at, 1)[0] : null;
    if (gone?.thumb) {
      try {
        URL.revokeObjectURL(gone.thumb);
      } catch (_) {}
    }
    renderAttachmentPreviews();
    updateSendEnabled();
  };
  // No row on screen (or reduced motion): remove instantly.
  if (!item || !row || row.classList.contains("leaving")) {
    if (!item) return;
    if (row && row.classList.contains("leaving")) return;
    dropNow();
    return;
  }
  try {
    if (window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      dropNow();
      return;
    }
  } catch (_) {}
  // Play the delete animation first. When it is the last card inside the
  // composer box (doc or image tile), collapse the box (while the fading
  // card is still inside) so the composer shrinks with animation instead
  // of snapping, then clear the DOM.
  const isLastDoc = pendingAttachments.filter((a) => a && (a.type === "document" || a.type === "image")).length <= 1;
  row.classList.add("leaving");
  row.classList.remove("is-loading");
  if (isLastDoc && composerAttachBox) {
    setTimeout(() => {
      try {
        composerAttachBox.classList.add("collapsing");
      } catch (_) {}
    }, 120);
    setTimeout(() => {
      dropNow();
      try {
        composerAttachBox.classList.remove("collapsing");
      } catch (_) {}
    }, 470);
    return;
  }
  setTimeout(dropNow, 230);
}

function clearAttachment() {
  pendingAttachments.forEach((a) => {
    if (a.thumb) {
      try {
        URL.revokeObjectURL(a.thumb);
      } catch (_) {}
    }
  });
  pendingAttachments = [];
  renderAttachmentPreviews();
  updateSendEnabled();
  hideMentionMenu();
}

/* Fixed-size document card, rendered INSIDE the composer box (above writing):
   unified file icon, name / type / size rows, edge loading bar while reading,
   natural X (always on touch, hover on desktop). Send unlocks only on success. */
function appendDocumentCard(item, index, pos) {
  if (!composerAttachBox) return;
  const row = document.createElement("div");
  row.className = "attach-item attach-doc ft-" + docTypeOf(item.name);
  row.setAttribute("data-anim", "");
  row.setAttribute("data-uid", item.uid);
  // Entrance animation plays once per attached file — re-renders (second
  // file added, progress ticks) must not replay it. Staggered by position.
  if (!item._shown) {
    row.classList.add("anim");
    const slot = typeof pos === "number" ? pos : 0;
    row.style.animationDelay = `${Math.min(slot, 5) * 70}ms`;
    item._shown = true;
  }
  const loading = !item._done && !item.dataUrl;
  if (loading) row.classList.add("is-loading");
  else if (item.dataUrl) row.classList.add("is-ready");
  else if (item._done) row.classList.add("is-error");
  row.innerHTML =
    '<div class="attach-doc-icon" aria-hidden="true">' +
    '<svg class="attach-doc-file" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">' +
    '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>' +
    '<polyline points="14 2 14 8 20 8"/>' +
    '<line x1="9" y1="13" x2="15" y2="13"/>' +
    '<line x1="9" y1="17" x2="13" y2="17"/>' +
    "</svg>" +
    "</div>" +
    '<div class="attach-doc-meta">' +
    '<div class="attach-doc-top"><span class="attach-doc-name"></span>' +
    '<button type="button" class="attach-remove" aria-label="Remove attachment">×</button></div>' +
    '<div class="attach-doc-type"></div>' +
    '<div class="attach-doc-size">Loading… 0%</div>' +
    "</div>" +
    '<span class="attach-doc-bar" aria-hidden="true"></span>';
  row.querySelector(".attach-doc-name").textContent = item.name;
  row.querySelector(".attach-doc-name").title = item.name;
  row.querySelector(".attach-doc-type").textContent = docLabelFor(item.name);
  row.querySelector(".attach-remove").addEventListener("click", () => removeAttachmentAt(index));
  // Re-render after the read already finished (second attachment added):
  // restore the completed state instead of showing "uploading" again.
  if (item.dataUrl) {
    const doneSize = row.querySelector(".attach-doc-size");
    if (doneSize) doneSize.textContent = formatSize(item.file.size);
  } else if (item._done && !item.dataUrl) {
    row.classList.remove("is-loading");
    row.classList.add("is-error");
    const errSize = row.querySelector(".attach-doc-size");
    if (errSize) errSize.textContent = "Failed to load";
  }
  composerAttachBox.appendChild(row);
}

/* Image tile INSIDE the composer box (above writing, same strip as docs):
   Manus-style 54px tile, image covers it, X top-end, click = fullscreen. */
function appendImageTile(item, index, pos) {
  if (!composerAttachBox) return;
  const tile = document.createElement("div");
  tile.className = "attach-imgtile";
  tile.setAttribute("data-anim", "");
  tile.setAttribute("data-uid", item.uid);
  if (!item._shown) {
    tile.classList.add("anim");
    const slot = typeof pos === "number" ? pos : 0;
    tile.style.animationDelay = `${Math.min(slot, 5) * 70}ms`;
    item._shown = true;
  }
  const img = document.createElement("img");
  img.className = "attach-imgtile-img";
  img.src = item.thumb;
  img.alt = String(item.name || "image").slice(0, 200);
  img.addEventListener("click", () => openImageLightbox(item.thumb, item.name));
  const x = document.createElement("button");
  x.type = "button";
  x.className = "attach-imgtile-x";
  x.setAttribute("aria-label", "Remove attachment");
  x.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M18 6 6 18"></path><path d="m6 6 12 12"></path></svg>';
  x.addEventListener("click", (e) => {
    e.stopPropagation();
    removeAttachmentAt(index);
  });
  tile.appendChild(img);
  tile.appendChild(x);
  tile.title = String(item.name || "");
  composerAttachBox.appendChild(tile);
}

/* Document + image cards live inside the composer box. One render entry so the
   strip above (video/text only) and the composer box never drift apart. */
function renderComposerDocuments() {
  if (!composerAttachBox) return;
  composerAttachBox.innerHTML = "";
  const inside = pendingAttachments.filter((a) => a && (a.type === "document" || (a.type === "image" && a.thumb)));
  if (!inside.length) {
    composerAttachBox.classList.remove("visible");
    return;
  }
  try {
    composerAttachBox.classList.remove("collapsing");
  } catch (_) {}
  composerAttachBox.classList.add("visible");
  inside.forEach((item, pos) => {
    if (item.type === "document") appendDocumentCard(item, pendingAttachments.indexOf(item), pos);
    else appendImageTile(item, pendingAttachments.indexOf(item), pos);
  });
}

/* Image-tile helpers (Manus-style 54px preview above the composer). */
const CHAT_IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i;
function isChatImageName(name) {
  return CHAT_IMG_EXT.test(String(name || ""));
}
function isSafePreviewSrc(src) {
  const s = String(src || "");
  return s.startsWith("blob:") || s.startsWith("data:image") || s.startsWith("/api/folder/download?path=");
}
function openImageLightbox(src, name) {
  if (!isSafePreviewSrc(src)) return;
  try {
    document.querySelector(".x5-img-lightbox")?.remove();
    const box = document.createElement("div");
    box.className = "x5-img-lightbox";
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    const img = document.createElement("img");
    img.className = "x5-img-lightbox-img";
    img.src = src;
    img.alt = String(name || "image preview").slice(0, 200);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "x5-img-lightbox-x";
    close.setAttribute("aria-label", "Close preview");
    close.textContent = "×";
    const done = () => {
      try {
        box.remove();
      } catch (_) {}
      try {
        document.removeEventListener("keydown", onKey, true);
      } catch (_) {}
    };
    const onKey = (e) => {
      if (e && e.key === "Escape") {
        e.stopPropagation();
        done();
      }
    };
    close.addEventListener("click", (e) => {
      e.stopPropagation();
      done();
    });
    box.addEventListener("click", (e) => {
      if (e.target === box) done();
    });
    box.appendChild(img);
    box.appendChild(close);
    document.body.appendChild(box);
    document.addEventListener("keydown", onKey, true);
  } catch (_) {}
}

function renderAttachmentPreviews() {
  // Document + image cards render INSIDE the composer box, not in a
  // separate strip above. The strip above stays for video/text only.
  renderComposerDocuments();
  if (!attachPreview) return;
  attachPreview.innerHTML = "";
  // A lone document/image must not leave an empty pill floating above the box.
  if (!pendingAttachments.some((a) => a && a.type !== "document" && a.type !== "image")) {
    attachPreview.classList.remove("visible");
    return;
  }
  attachPreview.classList.add("visible");
  const images = pendingImages();
  pendingAttachments.forEach((item, index) => {
    if (item.type === "document" || item.type === "image") return;
    const row = document.createElement("div");
    row.className = "attach-item";
    const media =
      item.type === "text"
        ? `<span class="attach-kind">TXT</span>`
        : item.thumb && item.type === "image"
          ? `<img class="attach-thumb" src="${item.thumb}" alt="" />`
          : item.thumb
            ? `<video class="attach-thumb" src="${item.thumb}" muted preload="metadata"></video>`
            : `<span class="attach-kind">${item.type === "image" ? "Image" : "Video"}</span>`;
    const tag =
      item.type === "image" ? `<span class="attach-tag">@${imageLabelFor(images.indexOf(item))}</span>` : "";
    row.innerHTML = `${media}<span class="attach-name"></span>${tag}<span class="attach-size">${escapeHtml(formatSize(item.file.size))}</span><button type="button" class="attach-remove" aria-label="Remove">×</button>`;
    row.querySelector(".attach-name").textContent = item.name;
    row.querySelector(".attach-remove").addEventListener("click", () => removeAttachmentAt(index));
    attachPreview.appendChild(row);
  });
}

function initAttachments() {
  const attachBtn = document.getElementById("attachBtn");
  const attachMenu = document.getElementById("attachMenu");
  if (!attachBtn || !attachMenu) return;

  attachBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    const open = attachMenu.classList.toggle("open");
    attachBtn.classList.toggle("active", open);
  });
  document.addEventListener("click", (e) => {
    if (!attachMenu.classList.contains("open")) return;
    if (attachMenu.contains(e.target) || e.target === attachBtn) return;
    attachMenu.classList.remove("open");
    attachBtn.classList.remove("active");
  });

  attachMenu.querySelectorAll("button[data-attach]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const type = btn.dataset.attach;
      const input = document.getElementById(
        type === "image" ? "fileInputImage" : type === "video" ? "fileInputVideo" : "fileInputDocument"
      );
      attachMenu.classList.remove("open");
      attachBtn.classList.remove("active");
      if (!input) return;
      input.onchange = () => {
        if (input.files && input.files[0]) {
          const f = input.files[0];
          const limit = type === "document" ? 100 * 1024 * 1024 : 25 * 1024 * 1024;
          if (f.size > limit) {
            toast(type === "document" ? "Document too large (max 100MB)" : "File too large (max 25MB)");
          } else {
            showAttachmentPreview(f, type);
          }
        }
        input.value = "";
      };
      setTimeout(() => input.click(), 80);
    });
  });
}

/* ---------- voice: record -> STT model -> raw text into composer ---------- */
// Pre-check: the STT template must be connected BEFORE recording.
// Missing -> toast + auto-open the STT template dialog (guides to connect).
async function ensureSttModel() {
  try {
    const res = await fetch("/api/ai_models", { cache: "no-store" });
    if (!res.ok) return true; // fail-open: let the STT endpoint answer
    const data = await res.json().catch(() => null);
    const models = data && data.models ? data.models : data;
    const stt = models ? models.stt || models.STT : null;
    if (stt && (stt.configured || stt.model_name || stt.connected)) return true;
  } catch (_) {
    return true;
  }
  try {
    toast("Connect a voice-to-text model first");
  } catch (_) {}
  try {
    const { navigate } = await import("./navigation.js?v=15");
    navigate("ai-models");
  } catch (_) {}
  try {
    window.dispatchEvent(new CustomEvent("x5:open-model", { detail: { kind: "stt" } }));
  } catch (_) {}
  try {
    if (typeof window.openAiModelDialog === "function") window.openAiModelDialog("stt");
  } catch (_) {}
  return false;
}
let mediaRecorder = null;
let audioChunks = [];
let isRecording = false;
let recStopTimer = null;
let audioCtx = null;
let voiceRaf = null;
let voiceTimerInt = null;
let voiceStartAt = 0;
let voiceSendAfter = false;
let voiceDiscarded = false;

function formatVoiceTime(ms) {
  const s = Math.floor(ms / 1000);
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return `${mm}:${ss}`;
}

function setVoiceLabel(text) {
  const label = document.getElementById("voiceLabel");
  if (label) label.textContent = text;
}

/* Smooth height morph (FLIP): the container eases to the visible template's
   height while templates crossfade with a gentle rise. Bounded 260ms to
   match the CSS rhythm; cleanup always restores auto height. */
function morphSwitchHeight(switchEl, showEl, hideEl) {
  if (!switchEl || !showEl) return;
  try {
    const from = switchEl.offsetHeight;
    showEl.classList.remove("is-hidden");
    if (hideEl) hideEl.classList.add("is-hidden");
    const to = showEl.offsetHeight;
    if (!from || !to || from === to) return;
    switchEl.classList.add("animating-height");
    switchEl.style.height = `${from}px`;
    void switchEl.offsetHeight;
    switchEl.style.height = `${to}px`;
    setTimeout(() => {
      switchEl.style.height = "";
      switchEl.classList.remove("animating-height");
    }, 300);
  } catch (_) {
    switchEl.style.height = "";
    switchEl.classList.remove("animating-height");
  }
}

function showVoicePanel() {
  const panel = document.getElementById("voicePanel");
  const composer = document.getElementById("composer");
  if (!panel || !composer) return;
  clearVoiceLoading();
  panel.classList.remove("transcribing");
  // Instant swap to the shorter recording template (see morphSwitchHeight).
  const switchEl = panel?.closest(".composer-switch");
  morphSwitchHeight(switchEl, panel, composer);
  const timer = document.getElementById("voiceTimer");
  voiceStartAt = Date.now();
  if (timer) timer.textContent = "00:00";
  if (voiceTimerInt) clearInterval(voiceTimerInt);
  voiceTimerInt = setInterval(() => {
    if (timer) timer.textContent = formatVoiceTime(Date.now() - voiceStartAt);
  }, 250);
  setVoiceLabel("Recording…");
}

function hideVoicePanel() {
  const panel = document.getElementById("voicePanel");
  const composer = document.getElementById("composer");
  if (voiceTimerInt) {
    clearInterval(voiceTimerInt);
    voiceTimerInt = null;
  }
  clearVoiceLoading();
  // Swap back: the writing template returns instantly with the text inside.
  const switchEl = panel?.closest(".composer-switch");
  if (panel) panel.classList.remove("transcribing");
  morphSwitchHeight(switchEl, composer, panel);
}

function startVoiceBars(stream) {
  // True voice-level bars: time-domain RMS gates the overall height
  // (silence stays flat), per-band spectrum shapes each bar.
  // Fast attack + slow release keeps motion smooth and honest.
  try {
    stopVoiceBars();
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    audioCtx = new AC();
    const src = audioCtx.createMediaStreamSource(stream);
    const analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.55;
    src.connect(analyser);
    const wave = document.getElementById("voiceWave");
    if (!wave) return;
    wave.innerHTML = "";
    const COUNT = 32;
    for (let i = 0; i < COUNT; i++) {
      const b = document.createElement("span");
      b.className = "voice-bar";
      wave.appendChild(b);
    }
    const freq = new Uint8Array(analyser.frequencyBinCount);
    const time = new Uint8Array(analyser.fftSize);
    const spans = [...wave.children];
    const smooth = new Array(spans.length).fill(4);
    const loop = () => {
      if (!wave.isConnected || !wave.children.length) return;
      analyser.getByteTimeDomainData(time);
      let sum = 0;
      for (let i = 0; i < time.length; i++) {
        const v = (time[i] - 128) / 128;
        sum += v * v;
      }
      const rms = Math.sqrt(sum / time.length);
      const level = Math.min(1, Math.max(0, (rms - 0.015) / 0.32));
      analyser.getByteFrequencyData(freq);
      spans.forEach((sp, i) => {
        const band = freq[Math.floor((i / spans.length) * freq.length * 0.7)] || 0;
        const target = 4 + level * (6 + (band / 255) * 30);
        const cur = smooth[i];
        smooth[i] = target > cur ? target : cur + (target - cur) * 0.35;
        sp.style.height = `${Math.round(smooth[i])}px`;
      });
      voiceRaf = requestAnimationFrame(loop);
    };
    loop();
  } catch (_) {
    stopVoiceBars();
  }
}

function stopVoiceBars() {
  try {
    if (voiceRaf) cancelAnimationFrame(voiceRaf);
  } catch (_) {}
  voiceRaf = null;
  const wave = document.getElementById("voiceWave");
  if (wave) wave.innerHTML = "";
  document.getElementById("voiceBars")?.remove();
  if (audioCtx) {
    audioCtx.close().catch(() => {});
    audioCtx = null;
  }
}

function setMicState(state) {
  const micBtn = document.getElementById("micBtn");
  if (!micBtn) return;
  micBtn.classList.toggle("listening", state === "recording");
  micBtn.classList.toggle("loading", state === "loading");
}

async function startRecording() {
  if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
    toast("Voice input not supported here");
    return;
  }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const mime =
      ["audio/webm", "audio/mp4", "audio/ogg"].find((m) => {
        try {
          return MediaRecorder.isTypeSupported(m);
        } catch (_) {
          return false;
        }
      }) || "";
    mediaRecorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
    audioChunks = [];
    mediaRecorder.ondataavailable = (e) => {
      if (e.data && e.data.size) audioChunks.push(e.data);
    };
    mediaRecorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      finishRecording();
    };
    mediaRecorder.start();
    isRecording = true;
    voiceSendAfter = false;
    voiceDiscarded = false;
    setMicState("recording");
    showVoicePanel();
    startVoiceBars(stream);
    recStopTimer = setTimeout(stopRecording, 60000);
  } catch (_) {
    stopVoiceBars();
    hideVoicePanel();
    toast("Mic unavailable");
  }
}

function stopRecording() {
  if (recStopTimer) {
    clearTimeout(recStopTimer);
    recStopTimer = null;
  }
  if (mediaRecorder && isRecording) {
    isRecording = false;
    try {
      mediaRecorder.stop();
    } catch (_) {
      setMicState("idle");
    }
  }
}

function cancelVoiceRecording() {
  // Discard the take entirely — no transcription, back to the composer.
  voiceDiscarded = true;
  voiceSendAfter = false;
  if (recStopTimer) {
    clearTimeout(recStopTimer);
    recStopTimer = null;
  }
  if (mediaRecorder && isRecording) {
    isRecording = false;
    const rec = mediaRecorder;
    rec.onstop = () => {
      try {
        rec.stream?.getTracks().forEach((t) => t.stop());
      } catch (_) {}
    };
    try {
      rec.stop();
    } catch (_) {}
  }
  audioChunks = [];
  stopVoiceBars();
  setMicState("idle");
  hideVoicePanel();
  composerInput?.focus();
}

function stopVoiceToComposer() {
  // Stop -> transcribe -> text appears in the writing box (user sends manually).
  voiceSendAfter = false;
  setVoiceTranscribing("voiceStopBtn");
  stopRecording();
}

function sendVoiceDirectly() {
  // Stop -> transcribe -> send immediately without manual step.
  voiceSendAfter = true;
  setVoiceTranscribing("voiceSendBtn");
  stopRecording();
}

function setVoiceTranscribing(activeBtnId) {
  // Freeze the counter the moment transcription starts.
  if (voiceTimerInt) {
    clearInterval(voiceTimerInt);
    voiceTimerInt = null;
  }
  const panel = document.getElementById("voicePanel");
  if (panel) panel.classList.add("transcribing");
  // Loading spinner runs inside the pressed button until transcription finishes.
  ["voiceStopBtn", "voiceSendBtn"].forEach((id) => {
    document.getElementById(id)?.classList.toggle("loading", id === activeBtnId);
  });
  setVoiceLabel("Transcribing…");
}

function clearVoiceLoading() {
  ["voiceStopBtn", "voiceSendBtn"].forEach((id) => {
    document.getElementById(id)?.classList.remove("loading");
  });
}

async function finishRecording() {
  if (voiceDiscarded) {
    voiceDiscarded = false;
    voiceSendAfter = false;
    return;
  }
  setMicState("loading");
  setVoiceTranscribing();
  try {
    const blob = new Blob(audioChunks, { type: (mediaRecorder && mediaRecorder.mimeType) || "audio/webm" });
    audioChunks = [];
    if (!blob.size) throw new Error("Empty recording");
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ""));
      reader.onerror = () => reject(new Error("read failed"));
      reader.readAsDataURL(blob);
    });
    const res = await fetch("/api/stt", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ audio: dataUrl }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
    const text = String(data?.text || "").trim();
    if (!text) {
      hideVoicePanel();
      toast("No speech detected");
    } else {
      // Raw transcript first — the user edits freely, then sends to the agent normally.
      composerInput.value = `${composerInput.value} ${text}`.trim().slice(0, MAX_MESSAGE);
      autoResize();
      hideVoicePanel();
      if (voiceSendAfter) {
        voiceSendAfter = false;
        composerInput.focus();
        await handleSend();
        return;
      }
      composerInput.focus();
    }
  } catch (err) {
    hideVoicePanel();
    toast(err instanceof Error ? err.message : "Voice failed");
  } finally {
    voiceSendAfter = false;
    stopVoiceBars();
    setMicState("idle");
  }
}

function initMic() {
  const micBtn = document.getElementById("micBtn");
  if (micBtn) {
    micBtn.addEventListener("click", async () => {
      if (micBtn.classList.contains("loading")) return;
      if (isRecording) stopVoiceToComposer();
      else {
        // STT gate: no model -> toast + auto-open the STT dialog, no recording.
        try {
          const ok = await ensureSttModel();
          if (!ok) return;
        } catch (_) {}
        startRecording();
      }
    });
  }
  document.getElementById("voiceCancelBtn")?.addEventListener("click", cancelVoiceRecording);
  document.getElementById("voiceStopBtn")?.addEventListener("click", () => {
    if (isRecording) stopVoiceToComposer();
  });
  document.getElementById("voiceSendBtn")?.addEventListener("click", () => {
    if (isRecording) sendVoiceDirectly();
  });
}

/* ---------- message gestures: hover (desktop CSS) + long-press (touch) ---------- */
function hideAllUserBars() {
  chatInner?.querySelectorAll(".msg .msg-actions.show").forEach((b) => b.classList.remove("show"));
}

function initUserMessageGestures() {
  if (!chatInner) return;
  const LONG_MS = 550;
  let lpTimer = null;
  let startX = 0;
  let startY = 0;
  const clearLp = () => {
    if (lpTimer) clearTimeout(lpTimer);
    lpTimer = null;
  };
  chatInner.addEventListener(
    "touchstart",
    (e) => {
      if (e.touches.length !== 1) return;
      const msg = e.target.closest?.(".msg");
      const bar = msg?.querySelector(":scope > .msg-actions");
      if (!msg || !bar) return;
      const t = e.touches[0];
      startX = t.clientX;
      startY = t.clientY;
      clearLp();
      lpTimer = setTimeout(() => {
        hideAllUserBars();
        bar.classList.add("show");
        try {
          navigator.vibrate?.(10);
        } catch (_) {}
        lpTimer = null;
      }, LONG_MS);
    },
    { passive: true }
  );
  chatInner.addEventListener(
    "touchmove",
    (e) => {
      if (!lpTimer) return;
      const t = e.touches[0];
      if (!t) return;
      if (Math.abs(t.clientX - startX) > 10 || Math.abs(t.clientY - startY) > 10) clearLp();
    },
    { passive: true }
  );
  ["touchend", "touchcancel"].forEach((ev) => chatInner.addEventListener(ev, clearLp, { passive: true }));
  document.addEventListener(
    "touchstart",
    (e) => {
      if (!e.target.closest?.(".msg")) hideAllUserBars();
    },
    { passive: true }
  );
  chatScroll?.addEventListener("scroll", hideAllUserBars, { passive: true });
}

/* ---------- @ / pickers: uploads + working-folder files, "/" skills ---------- */
function hideMentionMenu() {
  if (mentionMenu) {
    mentionMenu.remove();
    mentionMenu = null;
  }
  folderPickerSeq++;
  closePathbar(true);
}

function replaceTriggerToken(text) {
  if (!composerInput) return;
  const pos = composerInput.selectionStart ?? composerInput.value.length;
  const before = composerInput.value.slice(0, pos);
  const after = composerInput.value.slice(pos);
  // "@" opens mid-word too (كمل@file works, mail@x stays blocked since
  // Latin word-chars can't precede it); "/" keeps the old space rule so
  // paths like a/b never open the skills panel. Surrogate halves are
  // excluded so an emoji before @ can't be split in two.
  const m = before.match(/(^|[^\w@\uD800-\uDFFF])@([^@\n]*)$/)
    || before.match(/(^|\s)\/([^@\n]*)$/);
  const head = m ? before.slice(0, m.index + m[1].length) : before;
  // Trailing space stays in BOTH directions (LTR + Arabic RTL) so typing
  // continues cleanly after the picked token.
  const insert = `${text} `;
  const caret = head.length + insert.length;
  try {
    // setRangeText keeps native bidi/undo state (plain .value assignment
    // resets it and the caret lands on the wrong visual side in RTL).
    composerInput.focus();
    composerInput.setSelectionRange(head.length, pos);
    composerInput.setRangeText(insert, head.length, pos, "end");
  } catch (_) {
    composerInput.value = `${head}${insert}${after}`;
    try {
      composerInput.setSelectionRange(caret, caret);
    } catch (_) {}
  }
  hideMentionMenu();
  try {
    autoResize();
  } catch (_) {}
  try {
    renderComposerHL();
  } catch (_) {}
  // Re-assert the caret after layout resolves: in mixed Arabic/Latin lines
  // the visual caret only settles once direction + wrapping are computed.
  try {
    requestAnimationFrame(() => {
      try {
        if (!composerInput) return;
        if (document.activeElement !== composerInput) composerInput.focus();
        const end = Math.min(caret, composerInput.value.length);
        composerInput.setSelectionRange(end, end);
        renderComposerHL();
        try {
          composerInput.scrollTop = composerInput.scrollHeight;
        } catch (_) {}
      } catch (_) {}
    });
  } catch (_) {}
}

/* Pathbar picker state (breadcrumb bar attached above the composer). */
let folderPickerDir = "";
let folderPickerSeq = 0;

/* Composer highlight: ACTIVE @file / /skill tokens paint blue in a backdrop
   layer under the transparent-text textarea (same proven pattern as the
   AI-models protocol editor). Inactive @... stays the default text color. */
let composerHL = null;

function escapeHL(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function hlTokenRanges(text) {
  // [{a, b, kind, key}] of ACTIVE tokens only. kind: "ws" (key=path),
  // "skill" (key=name), "img" (upload label, no navigation target).
  const ranges = [];
  const push = (prefix, name, kind, key) => {
    if (!name) return;
    try {
      const extra = prefix === "/" ? "/" : "";
      const re = new RegExp(`(?<![\\w@${extra}])` + escapeRegExp(prefix + name) + `(?![\\w\\u0600-\\u06FF])`, "gu");
      let m;
      while ((m = re.exec(text)) !== null) {
        if (m[0]) ranges.push({ a: m.index, b: m.index + m[0].length, kind, key });
        if (re.lastIndex === m.index) re.lastIndex++;
      }
    } catch (_) {}
  };
  pendingFolderRefs.forEach((r) => push("@", r.name, "ws", r.path));
  if (pendingSkill) push("/", pendingSkill.name, "skill", pendingSkill.name);
  pendingAttachments.forEach((a, i) => {
    if (a && a.type === "image") push("@", imageLabelFor(i), "img", null);
  });
  ranges.sort((x, y) => x.a - y.a || y.b - x.b);
  const clean = [];
  for (const r of ranges) {
    if (clean.length && r.a < clean[clean.length - 1].b) continue; // overlap guard
    clean.push(r);
  }
  return clean;
}

function tokenAtCaret() {
  // Blue token strictly under the caret (edges count as edit, not a hit).
  try {
    if (!composerInput) return null;
    const pos = composerInput.selectionStart;
    if (pos == null || pos !== composerInput.selectionEnd) return null;
    const ranges = hlTokenRanges(composerInput.value);
    return ranges.find((r) => pos > r.a && pos < r.b) || null;
  } catch (_) {
    return null;
  }
}

function revealFolderFile(path) {
  // Jump to the Working Folder page with that file revealed exactly at its path.
  try {
    const rel = String(path || "").replace(/\\/g, "/").replace(/^\/+/, "").slice(0, 500);
    if (!rel || rel.split("/").includes("..")) return;
    const parts = rel.split("/").filter(Boolean);
    const name = parts.pop() || "";
    window.dispatchEvent(new CustomEvent("x5:reveal-folder", { detail: { dir: parts.join("/"), name } }));
    navigate("folder");
  } catch (_) {}
}

function revealSkill(name) {
  // Jump to the Skills page and open that skill's detail.
  try {
    const nm = String(name || "").slice(0, 100);
    if (!nm.trim()) return;
    navigate("skills");
    window.dispatchEvent(new CustomEvent("x5:reveal-skill", { detail: { name: nm } }));
  } catch (_) {}
}

function renderComposerHL() {
  if (!composerInput || !composerHL) return;
  let text = "";
  try {
    text = composerInput.value;
  } catch (_) {
    return;
  }
  let selA = -1;
  let selB = -1;
  try {
    selA = composerInput.selectionStart ?? -1;
    selB = composerInput.selectionEnd ?? -1;
  } catch (_) {}
  const ranges = hlTokenRanges(text);
  const cuts = new Set([0, text.length]);
  ranges.forEach((r) => {
    cuts.add(r.a);
    cuts.add(r.b);
  });
  const hasSel = selA >= 0 && selB > selA;
  const sA = Math.min(selA, text.length);
  const sB = Math.min(selB, text.length);
  if (hasSel) {
    cuts.add(sA);
    cuts.add(sB);
  }
  const pts = [...cuts].sort((x, y) => x - y);
  let html = "";
  for (let i = 0; i < pts.length - 1; i++) {
    const a = pts[i];
    const b = pts[i + 1];
    if (b <= a) continue;
    const chunk = text.slice(a, b);
    const isTok = ranges.some((r) => a >= r.a && b <= r.b);
    const isSel = hasSel && a >= sA && b <= sB;
    if (isSel) html += `<span class="hl-sel">${escapeHL(chunk)}</span>`;
    else if (isTok) html += `<span class="hl-tok">${escapeHL(chunk)}</span>`;
    else html += escapeHL(chunk);
  }
  composerHL.innerHTML = html;
  try {
    // Mirror the textarea's RESOLVED base direction (not auto): mixed
    // Arabic/Latin text then lays out identically in both layers.
    const cs = window.getComputedStyle(composerInput);
    if (cs && cs.direction) composerHL.style.direction = cs.direction;
    if (cs && cs.textAlign) composerHL.style.textAlign = cs.textAlign;
    composerHL.scrollTop = composerInput.scrollTop;
    composerHL.scrollLeft = composerInput.scrollLeft;
  } catch (_) {}
}

function initComposerHL() {
  if (!composerInput || composerHL) return;
  try {
    const wrap = document.createElement("div");
    wrap.className = "composer-hlwrap";
    composerInput.parentNode.insertBefore(wrap, composerInput);
    wrap.appendChild(composerInput);
    const bd = document.createElement("div");
    bd.className = "composer-hl";
    bd.setAttribute("aria-hidden", "true");
    wrap.insertBefore(bd, composerInput);
    composerHL = bd;
    ["input", "click", "keyup", "select"].forEach((ev) => composerInput.addEventListener(ev, renderComposerHL));
    // Clicking a BLUE token jumps to its home (file -> folder at its
    // path, skill -> Skills detail). Edges/drag-select stay for editing.
    composerInput.addEventListener("click", () => {
      try {
        setTimeout(() => {
          const hit = tokenAtCaret();
          if (!hit || hit.kind === "img" || !hit.key) return;
          if (hit.kind === "ws") revealFolderFile(hit.key);
          else if (hit.kind === "skill") revealSkill(hit.key);
        }, 0);
      } catch (_) {}
    });
    composerInput.addEventListener("scroll", () => {
      if (!composerHL) return;
      try {
        composerHL.scrollTop = composerInput.scrollTop;
        composerHL.scrollLeft = composerInput.scrollLeft;
      } catch (_) {}
    }, { passive: true });
    renderComposerHL();
  } catch (_) {
    composerHL = null;
  }
}

const WS_FOLDER_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"></path></svg>';
const WS_FILE_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13.5 2.5H6.5a2 2 0 0 0-2 2v15a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V8.5Z"></path><path d="M13.5 2.5v6h6"></path></svg>';

function ensurePathbar() {
  const form = document.getElementById("composer");
  if (!form) return null;
  let bar = document.getElementById("wsPathbar");
  if (!bar) {
    bar = document.createElement("div");
    bar.className = "pk-bar";
    bar.id = "wsPathbar";
    form.prepend(bar);
  }
  bar.classList.remove("closing");
  return bar;
}

function closePathbar(animated) {
  const bar = document.getElementById("wsPathbar");
  if (!bar) return;
  if (!animated) {
    bar.remove();
    return;
  }
  if (bar.classList.contains("closing")) return;
  bar.classList.add("closing");
  bar.addEventListener("animationend", () => bar.remove(), { once: true });
  setTimeout(() => {
    if (bar.isConnected) bar.remove();
  }, 300);
}

function wsChip(svg, text, onClick, isCurrent) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pk-chip clickable select-none" + (isCurrent ? " pk-current" : "");
  b.setAttribute("aria-haspopup", "dialog");
  b.setAttribute("aria-expanded", "false");
  if (svg) {
    const icon = document.createElement("span");
    icon.innerHTML = svg;
    icon.style.display = "inline-flex";
    b.appendChild(icon);
  }
  const label = document.createElement("span");
  label.textContent = text;
  b.appendChild(label);
  b.addEventListener("click", onClick);
  return b;
}

function wsRow(svg, name, meta, onClick, iconCls, thumbUrl) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pk-row";
  // Image files show the picture itself instead of the default icon.
  // thumbUrl is either a blob:/data: URL (attached uploads) or the jailed
  // /api/folder/download?path= URL (working-folder files) — both validated.
  if (thumbUrl && isSafePreviewSrc(thumbUrl)) {
    const im = document.createElement("img");
    im.className = "pk-thumb-img";
    im.src = thumbUrl;
    im.alt = "";
    im.loading = "lazy";
    im.addEventListener("error", () => {
      try {
        const fb = document.createElement("span");
        fb.innerHTML = svg;
        fb.style.display = "inline-flex";
        if (iconCls) fb.className = iconCls;
        im.replaceWith(fb);
      } catch (_) {}
    });
    b.appendChild(im);
  } else {
    const icon = document.createElement("span");
    icon.innerHTML = svg;
    icon.style.display = "inline-flex";
    if (iconCls) icon.className = iconCls;
    b.appendChild(icon);
  }
  const label = document.createElement("span");
  label.textContent = name;
  label.style.overflow = "hidden";
  label.style.textOverflow = "ellipsis";
  label.style.whiteSpace = "nowrap";
  b.appendChild(label);
  const m = document.createElement("span");
  m.className = "pk-meta";
  m.textContent = meta;
  b.appendChild(m);
  b.addEventListener("click", onClick);
  return b;
}

function wsSection(title) {
  const d = document.createElement("div");
  d.className = "pk-section";
  d.textContent = title;
  return d;
}

function wsNav(rel) {
  // Enter a folder (or climb via breadcrumb): navigation only, writes
  // nothing into the composer. Only picking a FILE inserts "@name".
  folderPickerDir = rel || "";
  showMentionMenu(folderPickerDir ? `${folderPickerDir}/` : "");
  if (composerInput) {
    try {
      composerInput.focus({ preventScroll: true });
    } catch (_) {
      composerInput.focus();
    }
  }
}

function escapeRegExp(s) {
  return String(s).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function hasToken(value, prefix, name) {
  // Boundary-aware "@name" / "/name" check (plain includes() would match
  // "@note" inside "@notes" or "/b" inside "a/b").
  try {
    const extra = prefix === "/" ? "/" : "";
    const re = new RegExp(`(?<![\\w@${extra}])${escapeRegExp(prefix + name)}(?![\\w\\u0600-\\u06FF])`, "u");
    return re.test(String(value));
  } catch (_) {
    return String(value).includes(prefix + name);
  }
}

/* Working-folder/skill picks stashed per user bubble so resends (regenerate /
   edit-resend) inject the SAME @ ids + / skill as the original turn.
   Uploaded files were mirrored into the working folder under their own name, so
   their names resolve again as {name, path:name} (missing files skip
   silently server-side). Never throws. */
function stashBubbleRefs(userEl, uploads) {
  try {
    if (!userEl || !userEl.dataset) return;
    userEl.dataset.wsrefs = JSON.stringify((pendingFolderRefs || []).slice(0, 10));
    userEl.dataset.skill = pendingSkill
      ? JSON.stringify({ name: pendingSkill.name, instructions: String(pendingSkill.instructions || "").slice(0, 6000) })
      : "";
    const ups = [];
    (uploads || []).forEach((a, i) => {
      try {
        if (a && a.type === "image") ups.push(imageLabelFor(i));
        else if (a && a.name) ups.push(String(a.name));
      } catch (_) {}
    });
    userEl.dataset.attnames = JSON.stringify(ups.slice(0, 4));
  } catch (_) {}
}

function restoreBubbleRefs(userEl, text) {
  // Re-arm the one-shot picks from the bubble's stash, keeping only tokens
  // still present in the resent text (same hasToken rule as normal send).
  try { pendingFolderRefs = []; } catch (_) {}
  try { pendingSkill = null; } catch (_) {}
  const value = String(text || "");
  try {
    const refs = JSON.parse((userEl && userEl.dataset && userEl.dataset.wsrefs) || "[]");
    if (Array.isArray(refs)) {
      refs.forEach((r) => {
        try {
          if (r && r.name && r.path && hasToken(value, "@", String(r.name))) {
            if (!pendingFolderRefs.some((x) => x.path === r.path)) {
              pendingFolderRefs.push({ name: String(r.name).slice(0, 200), path: String(r.path).slice(0, 500) });
            }
          }
        } catch (_) {}
      });
    }
  } catch (_) {}
  try {
    const ups = JSON.parse((userEl && userEl.dataset && userEl.dataset.attnames) || "[]");
    if (Array.isArray(ups)) {
      ups.forEach((n) => {
        try {
          const nm = String(n || "");
          if (nm && hasToken(value, "@", nm) && !pendingFolderRefs.some((x) => x.name === nm)) {
            if (pendingFolderRefs.length < 10) pendingFolderRefs.push({ name: nm.slice(0, 200), path: nm.slice(0, 500) });
          }
        } catch (_) {}
      });
    }
  } catch (_) {}
  try {
    const s = JSON.parse((userEl && userEl.dataset && userEl.dataset.skill) || "null");
    if (s && s.name && s.instructions && hasToken(value, "/", String(s.name))) {
      pendingSkill = { name: String(s.name).slice(0, 100), instructions: String(s.instructions).slice(0, 6000) };
    }
  } catch (_) {}
}

function paintBubbleMentions(userEl) {
  // Blue @file / /skill names inside the just-sent bubble (same blue as the
  // composer). Text nodes only — code blocks are never touched. Never throws.
  try {
    if (!userEl || !userEl.dataset) return;
    const names = [];
    try {
      (JSON.parse(userEl.dataset.wsrefs || "[]") || []).forEach((r) => {
        if (r && r.name) names.push({ p: "@", n: String(r.name) });
      });
    } catch (_) {}
    try {
      (JSON.parse(userEl.dataset.attnames || "[]") || []).forEach((n) => {
        if (n) names.push({ p: "@", n: String(n) });
      });
    } catch (_) {}
    try {
      const s = JSON.parse(userEl.dataset.skill || "null");
      if (s && s.name) names.push({ p: "/", n: String(s.name) });
    } catch (_) {}
    if (!names.length) return;
    const content = userEl.querySelector(":scope > .msg-content");
    if (!content) return;
    const acceptFn = (node) => {
      try {
        const pe = node.parentElement;
        if (pe && pe.closest && pe.closest("pre,code")) return NodeFilter.FILTER_REJECT;
        const v = node.nodeValue || "";
        if (!v.includes("@") && !v.includes("/")) return NodeFilter.FILTER_REJECT;
      } catch (_) {
        return NodeFilter.FILTER_REJECT;
      }
      return NodeFilter.FILTER_ACCEPT;
    };
    names.forEach(({ p, n }) => {
      if (!n) return;
      let re;
      try {
        const extra = p === "/" ? "/" : "";
        re = new RegExp(`(?<![\\w@${extra}])${escapeRegExp(p + n)}(?![\\w\\u0600-\\u06FF])`, "gu");
      } catch (_) {
        return;
      }
      let nodes = [];
      try {
        const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT, { acceptNode: acceptFn });
        while (walker.nextNode()) nodes.push(walker.currentNode);
      } catch (_) {
        return;
      }
      nodes.forEach((node) => {
        try {
          const t = node.nodeValue;
          if (!t || !re.test(t)) return;
          re.lastIndex = 0;
          const frag = document.createDocumentFragment();
          let last = 0;
          let m;
          re.lastIndex = 0;
          while ((m = re.exec(t)) !== null) {
            if (m.index > last) frag.appendChild(document.createTextNode(t.slice(last, m.index)));
            const sp = document.createElement("span");
            sp.className = "hl-tok-msg";
            sp.textContent = m[0];
            frag.appendChild(sp);
            last = m.index + m[0].length;
            if (re.lastIndex === m.index) re.lastIndex++;
          }
          if (last < t.length) frag.appendChild(document.createTextNode(t.slice(last)));
          node.parentNode.replaceChild(frag, node);
        } catch (_) {}
      });
    });
  } catch (_) {}
}

function continuesActivePick(q, prefix) {
  // True while the trigger text belongs to an already-picked (blue) token:
  // typing right after it, or DELETING chars inside it — the panel must
  // never pop open for these keystrokes (no fetch, no flash).
  const s = String(q || "");
  if (!s) return false;
  const names = [];
  if (prefix === "@") {
    pendingFolderRefs.forEach((r) => names.push(r.name));
    pendingAttachments.forEach((a, i) => {
      if (a && a.type === "image") names.push(imageLabelFor(i));
    });
  } else if (pendingSkill) {
    names.push(pendingSkill.name);
  }
  const low = s.toLowerCase();
  return names.some((n) => {
    if (!n) return false;
    const nl = String(n).toLowerCase();
    return s === n || s.startsWith(n + " ") || s.startsWith(n + "\n") || nl.startsWith(low);
  });
}

async function showMentionMenu(query) {
  // "@" picker: breadcrumb pathbar above the template (uploads + dir contents).
  const mySeq = ++folderPickerSeq;
  const raw = String(query || "").replace(/^"+|"+$/g, "");
  let dir = "";
  let filter = raw.toLowerCase();
  const slash = raw.lastIndexOf("/");
  if (slash >= 0) {
    dir = raw.slice(0, slash);
    filter = raw.slice(slash + 1).toLowerCase();
  }
  folderPickerDir = dir;
  let entries = null;
  try {
    const res = await fetch("/api/folder?path=" + encodeURIComponent(dir), { cache: "no-store" });
    const data = await res.json().catch(() => null);
    if (res.ok && data && Array.isArray(data.entries)) entries = data.entries;
  } catch (_) {
    entries = null;
  }
  if (mySeq !== folderPickerSeq) return; // stale keystroke, a newer fetch owns the bar
  if (!entries && !pendingAttachments.length) {
    hideMentionMenu();
    return;
  }
  renderPathbar(dir, filter, entries || []);
}

function renderPathbar(dir, filter, entries) {
  const bar = ensurePathbar();
  if (!bar) return;
  bar.innerHTML = "";
  // Breadcrumb: working-folder root + segments (click any of them to go back).
  const crumb = document.createElement("div");
  crumb.className = "pk-crumb";
  crumb.appendChild(wsChip(null, "Working folder", () => wsNav(""), !dir));
  if (dir) {
    let acc = "";
    const segs = dir.split("/").filter(Boolean);
    segs.forEach((seg, i) => {
      acc = acc ? `${acc}/${seg}` : seg;
      const sep = document.createElement("span");
      sep.className = "pk-sep";
      sep.textContent = "\\";
      crumb.appendChild(sep);
      const target = acc;
      crumb.appendChild(wsChip(null, seg, () => wsNav(target), i === segs.length - 1));
    });
  }
  bar.appendChild(crumb);
  // Pending uploads (the send-template content) first.
  const upRows = [];
  pendingAttachments.forEach((item, i) => {
    const label = item.type === "image" ? `@${imageLabelFor(i)}` : `@${item.name}`;
    if (!filter || label.toLowerCase().includes(filter) || String(item.name || "").toLowerCase().includes(filter)) {
      upRows.push({ item, i, label });
    }
  });
  // Directory contents: folders enter, files pick + animated close.
  const shown = entries.filter(
    (e) => e && e.name && (!filter || String(e.name).toLowerCase().includes(filter))
  );
  // Strict rule: zero rows = no bar — EXCEPT browse mode (empty filter
  // while navigating folders): an empty folder keeps the panel with its
  // breadcrumb plus an upload row instead of closing it.
  if (!upRows.length && !shown.length) {
    if (filter) {
      hideMentionMenu();
      return;
    }
    const d = document.createElement("div");
    d.className = "pk-empty";
    const t = document.createElement("span");
    t.textContent = "This folder is empty — upload the first file";
    d.appendChild(t);
    const add = document.createElement("button");
    add.type = "button";
    add.className = "pk-empty-add";
    add.textContent = "+";
    add.title = "Upload a file here";
    add.setAttribute("aria-label", "Upload a file to this folder");
    add.addEventListener("click", () => uploadToPickerDir(dir));
    d.appendChild(add);
    bar.appendChild(d);
    return;
  }
  if (upRows.length) {
    bar.appendChild(wsSection("Attached"));
    upRows.forEach(({ item, i, label }) => {
      const thumb = item && item.type === "image" && item.thumb ? item.thumb : null;
      bar.appendChild(wsRow(WS_FILE_SVG, label, String(item.name || ""), () => replaceTriggerToken(label), null, thumb));
    });
  }
  shown.slice(0, 100).forEach((e) => {
    const rel = dir ? `${dir}/${e.name}` : e.name;
    const safeRel = !String(rel || "").split("/").includes("..");
    if (e.type === "folder") {
      // Folders only navigate (never a pick), path chips update via wsNav.
      bar.appendChild(wsRow(WS_FOLDER_SVG, e.name, `${e.items ?? 0} items`, () => wsNav(rel), "pk-ico-folder"));
    } else {
      const thumb = safeRel && isChatImageName(e.name)
        ? "/api/folder/download?path=" + encodeURIComponent(rel)
        : null;
      bar.appendChild(wsRow(WS_FILE_SVG, e.name, formatSize(e.size || 0), () => {
        pickFolderFile(rel);
        closePathbar(true);
      }, null, thumb));
    }
  });
}

function pickFolderFile(rel) {
  // Picking a file writes only "@name" into the text; the server injects
  // that file's metadata exactly where the token stands.
  const name = String(rel).split("/").pop();
  if (!pendingFolderRefs.some((r) => r.path === rel)) {
    if (pendingFolderRefs.length >= 10) {
      toast("Max 10 folder files");
      return;
    }
    pendingFolderRefs.push({ path: rel, name });
  }
  replaceTriggerToken(`@${name}`);
}

function uploadToPickerDir(dir) {
  // "+" on an empty picker folder: opens the file manager, uploads straight
  // into that working-folder subfolder, then refreshes the picker instantly. The
  // server bumps the folder version on upload, so the folder page updates by
  // itself too (push, no polling).
  try {
    const input = document.createElement("input");
    input.type = "file";
    input.style.display = "none";
    document.body.appendChild(input);
    input.addEventListener("change", async () => {
      const file = input.files && input.files[0];
      input.remove();
      if (!file) return;
      if (!file.size) {
        toast("Empty file");
        return;
      }
      if (file.size > 100 * 1024 * 1024) {
        toast("File too large (max 100MB)");
        return;
      }
      try {
        const dataUrl = await new Promise((resolve, reject) => {
          const reader = new FileReader();
          reader.onload = () => resolve(String(reader.result || ""));
          reader.onerror = () => reject(new Error("read failed"));
          reader.readAsDataURL(file);
        });
        const res = await fetch("/api/folder", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ action: "upload", path: dir || "", name: file.name, data: dataUrl }),
        });
        const out = await res.json().catch(() => ({}));
        if (!res.ok) throw new Error(out?.error || "upload failed");
        toast("Uploaded to the working folder");
        showMentionMenu(folderPickerDir ? `${folderPickerDir}/` : "");
      } catch (err) {
        toast(err instanceof Error && err.message ? err.message : "Upload failed");
      }
    }, { once: true });
    input.click();
  } catch (_) {}
}

const WS_SKILL_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--icon-secondary)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"></path></svg>';

async function showSkillsMenu(query) {
  // "/" picker: same attached panel design as "@", but with skills.
  const mySeq = ++folderPickerSeq;
  const q = String(query || "").trim().toLowerCase();
  try {
    if (!skillsCache || Date.now() - skillsCache.at > 15000) {
      const res = await fetch("/api/skills", { cache: "no-store" });
      const data = await res.json().catch(() => null);
      skillsCache = { at: Date.now(), list: (data && data.skills) || [] };
    }
  } catch (_) {
    hideMentionMenu();
    return;
  }
  if (mySeq !== folderPickerSeq) return; // stale keystroke
  const list = (skillsCache.list || []).filter(
    (s) => s && s.name && (!q || String(s.name).toLowerCase().includes(q))
  );
  if (!list.length) {
    hideMentionMenu();
    return;
  }
  renderSkillsPanel(list.slice(0, 100));
}

function renderSkillsPanel(list) {
  const bar = ensurePathbar();
  if (!bar) return;
  bar.innerHTML = "";
  const crumb = document.createElement("div");
  crumb.className = "pk-crumb";
  crumb.appendChild(wsChip(null, "Skills", () => hideMentionMenu(), true));
  bar.appendChild(crumb);
  list.forEach((s) => {
    bar.appendChild(wsRow(WS_SKILL_SVG, `/${s.name}`, String(s.description || "").slice(0, 60), () => pickSkill(s.name), "pk-ico-skill"));
  });
}

async function pickSkill(name) {
  try {
    const res = await fetch("/api/skills?name=" + encodeURIComponent(name), { cache: "no-store" });
    const data = await res.json().catch(() => null);
    const full = (data && data.skill) || null;
    if (!full) {
      toast("Skill not found");
      return;
    }
    const instr = String(full.full_instructions || full.instructions || full.content || "").slice(0, 6000);
    if (!instr.trim()) {
      toast("Skill is empty");
      return;
    }
    pendingSkill = { name: full.skill_name || full.name || name, instructions: instr };
    replaceTriggerToken(`/${pendingSkill.name}`);
  } catch (_) {
    toast("Skill load failed");
  }
}

function checkMention() {
  if (!composerInput) return;
  const pos = composerInput.selectionStart ?? composerInput.value.length;
  const before = composerInput.value.slice(0, pos);
  const sm = before.match(/(^|\s)\/([^@\n]*)$/);
  if (sm) {
    // Still writing next to the picked (blue) skill: never reopen.
    if (continuesActivePick(sm[2], "/")) {
      hideMentionMenu();
      return;
    }
    showSkillsMenu(sm[2]);
    return;
  }
  // Same "@" rule as replaceTriggerToken: fires inside Arabic text
  // (كمل@file) but never inside Latin words (mail@x = email, blocked).
  const am = before.match(/(^|[^\w@\uD800-\uDFFF])@([^@\n]*)$/);
  if (am) {
    // Still writing next to a picked (blue) ref: never reopen the panel.
    if (continuesActivePick(am[2], "@")) {
      hideMentionMenu();
      return;
    }
    showMentionMenu(am[2]);
  } else hideMentionMenu();
}

/* ---------- question / approval cards ----------
   Ask = fused strip above the composer: lettered choice rows on top, the
   composer itself is the custom reply (essay = question only, no rows).
   Selection never sends — SEND posts the answer straight into the paused
   run, where it lands inside the question step's arrow (Q + A) and the loop
   continues from there (no user bubble, no restart). While asking, + / mode
   / mic hide (composer.asking), STOP flips to SEND, and Working freezes
   until the answer is sent. */
function pauseWorkingLoop() {
  // The loop is paused on this question (server blocks for the answer), so
  // the Working square freezes too — no motion while waiting. Never throws.
  try {
    const sq = activeThinking && activeThinking._block && activeThinking._block._dotSquare;
    if (sq) stopSquareAnimation(sq);
  } catch (_) {}
}

function resumeWorkingLoop() {
  // The answer is on its way — Working moves again (unless settled). Never throws.
  try {
    const blk = activeThinking && activeThinking._block;
    if (blk && blk.isConnected && blk.dataset.settled !== "1" && blk._dotSquare) {
      startSquareAnimation(blk._dotSquare);
    }
  } catch (_) {}
}

function hideQuestionCard() {
  const card = document.getElementById("questionCard");
  pendingQuestion = null;
  try {
    document.getElementById("composer")?.classList.remove("asking");
  } catch (_) {}
  if (!card) return;
  // Exit runs as keyframes (ask-out), never a transition: swapping the
  // animation property always moves, even with a retained enter fill.
  // (Never set inline animation:none here — inline wins over the stylesheet
  // and would cancel the exit itself.)
  card.classList.add("leaving");
  setTimeout(() => {
    try {
      card.remove();
    } catch (_) {}
    // The strip is gone — give the freed space back, unless a newer strip
    // already took its place (it owns the padding now).
    try {
      if (!document.getElementById("questionCard")) {
        const sc = document.getElementById("chatScroll");
        if (sc) sc.style.paddingBottom = "";
      }
    } catch (_) {}
  }, 220);
  resumeWorkingLoop();
  // The run is still generating after the strip leaves — back to STOP.
  try {
    const stillRunning = !!document.getElementById("thinkingMsg") || !!activeThinking;
    if (stillRunning && sendBtn && !sendBtn.classList.contains("stop")) setSending(true);
  } catch (_) {}
}

function isChatLive() {
  // A pause strip belongs to a live run only — after STOP (or a finished
  // turn) a late/duplicate question frame must never raise the template.
  try {
    if (activeRun) return true;
    if (catchupResume) return true;
    if (activeThinking && activeThinking.isConnected) return true;
    const t = document.getElementById("thinkingMsg");
    if (t && t.isConnected && t.dataset.stopped !== "1") return true;
  } catch (_) {}
  return false;
}

function clearChoiceOnType() {
  // Typing a sentence while an ask choice is selected clears the selection
  // (the typed text wins). The pre-selection stash is dropped — the typed
  // sentence is the answer now. Programmatic composer writes never fire
  // "input", so select/deselect never re-trigger this. Never throws.
  try {
    if (!pendingQuestion || pendingQuestion.kind !== "ask") return;
    if (pendingQuestion.selected == null) return;
    if (!document.getElementById("questionCard")) return;
    pendingQuestion.selected = null;
    pendingQuestion.typedCache = null;
    document.querySelectorAll("#questionCard .q-chip.selected").forEach((x) => {
      x.classList.remove("selected");
      x.setAttribute("aria-pressed", "false");
    });
  } catch (_) {}
}

function showQuestionCardUnlessStopped(q) {
  if (!isChatLive()) {
    try { clearPendingStored(); } catch (_) {}
    return;
  }
  showQuestionCard(q);
}

function showQuestionCard(q) {
  // Guard: valid calls only — same id already visible never re-renders
  // (late/duplicate frames must not pop the strip again), and a replacing
  // strip removes the old one instantly so exit + enter animations never
  // overlap. Essay uses the same ask-in / ask-out keyframes.
  try {
    if (!q || !q.id) return;
    const shown = document.getElementById("questionCard");
    if (shown && pendingQuestion && String(pendingQuestion.id) === String(q.id)) return;
    if (shown) {
      try { shown.remove(); } catch (_) {}
      pendingQuestion = null;
      try {
        document.getElementById("composer")?.classList.remove("asking");
      } catch (_) {}
    }
  } catch (_) {}
  pendingQuestion = { id: q.id, kind: q.kind || "ask", selected: null, typedCache: null };
  const wrap = document.querySelector(".composer-wrap");
  if (!wrap) return;
  // The strip belongs to the writing template: same container, and it
  // survives refresh until answered (or server-side expiry clears it).
  storePendingQuestion(q);
  const card = document.createElement("div");
  card.className = "question-card from-composer";
  card.id = "questionCard";
  if (pendingQuestion.kind === "needs_model") {
    // Paused tool needs a model kind, decided right here above the composer:
    // Skip continues the task without it, Connect also continues and jumps
    // to AI Models with the right dialog auto-opened.
    const kind = String(q.needs_kind || "text").toLowerCase();
    const tool = String(q.tool || "");
    card.classList.add("needs-model");
    card.innerHTML = `
      <div class="question-kind">Model needed</div>
      <div class="question-text"></div>
      <div class="question-options">
        <button type="button" class="glass-btn" data-decide="skip">Skip</button>
        <button type="button" class="glass-btn accent" data-decide="connect">Connect</button>
      </div>`;
    card.querySelector(".question-text").textContent =
      q.text || (tool ? `الأداة ${tool} تحتاج إلى موديل غير متوصل.` : "هذه المهمة تحتاج إلى موديل غير متوصل.");
    card.querySelectorAll("[data-decide]").forEach((btn, idx) => {
      btn.style.animationDelay = `${80 + idx * 55}ms`;
      btn.addEventListener("click", () => decideNeedsModel(q.id, btn.dataset.decide, kind));
    });
    pauseWorkingLoop();
  } else if (pendingQuestion.kind === "approval") {
    card.innerHTML = `
      <div class="question-kind">Approval needed</div>
      <div class="question-command"></div>
      <div class="question-options">
        <button type="button" class="btn btn-primary" data-decide="continue">Continue</button>
        <button type="button" class="btn btn-danger" data-decide="stop">Stop</button>
      </div>`;
    card.querySelector(".question-command").textContent = q.command || q.text || "Run command?";
    card.querySelectorAll("[data-decide]").forEach((btn) => {
      btn.addEventListener("click", () => decideApproval(q.id, btn.dataset.decide));
    });
  } else {
    // ONE unified strip: question text + compact pill choices in a single
    // wrapping bar fused above the composer (same template, not two).
    // Essay (no choices) shows the text only — the composer below answers.
    card.classList.add("ask");
    card.setAttribute("dir", "auto");
    card.innerHTML = `
      <div class="question-kind">Agent question</div>
      <div class="question-text" dir="auto"></div>
      <div class="question-chips"></div>`;
    card.querySelector(".question-text").textContent = q.text || "";
    const box = card.querySelector(".question-chips");
    const choices = Array.isArray(q.choices) ? q.choices.slice(0, 8) : [];
    const LETTERS = ["A", "B", "C", "D", "E", "F", "G", "H"];
    // Essay (no choices): flag it so the fused single-strip style always
    // wins over the base floating card, exactly like the choices strip.
    if (choices.length === 0) card.classList.add("essay");
    choices.forEach((c, idx) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "q-chip";
      b.style.animationDelay = `${80 + idx * 55}ms`;
      const key = document.createElement("span");
      key.className = "q-chip-key";
      key.textContent = LETTERS[idx] || String(idx + 1);
      key.setAttribute("aria-hidden", "true");
      b.appendChild(key);
      const label = document.createElement("span");
      label.className = "q-chip-label";
      label.textContent = String(c).slice(0, 120);
      label.setAttribute("dir", "auto");
      b.appendChild(label);
      b.setAttribute("aria-pressed", "false");
      b.addEventListener("click", () => {
        if (!pendingQuestion) return;
        // Toggle: first tap selects (stashing any typed text, composer
        // cleared); second tap on the SAME choice deselects and restores
        // the stashed text. Switching to another choice keeps the stash.
        // Same behavior for every choice label (including صح / غلط).
        if (b.classList.contains("selected")) {
          pendingQuestion.selected = null;
          box.querySelectorAll(".q-chip").forEach((x) => {
            x.classList.remove("selected");
            x.setAttribute("aria-pressed", "false");
          });
          try {
            composerInput.value = typeof pendingQuestion.typedCache === "string"
              ? pendingQuestion.typedCache : "";
            pendingQuestion.typedCache = null;
            autoResize();
            updateSendEnabled();
          } catch (_) {}
        } else {
          try {
            if (pendingQuestion.selected == null) {
              pendingQuestion.typedCache = composerInput.value || "";
            }
          } catch (_) {}
          pendingQuestion.selected = String(c);
          box.querySelectorAll(".q-chip").forEach((x) => {
            const on = x === b;
            x.classList.toggle("selected", on);
            x.setAttribute("aria-pressed", on ? "true" : "false");
          });
          try {
            composerInput.value = "";
            autoResize();
            updateSendEnabled();
          } catch (_) {}
        }
        try {
          composerInput.focus();
        } catch (_) {}
      });
      box.appendChild(b);
    });
    try {
      document.getElementById("composer")?.classList.add("asking");
    } catch (_) {}
    // The loop is paused on this question: STOP becomes SEND so the answer
    // can actually be sent (choice or typed text via the normal SEND).
    try {
      setSending(false);
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.classList.remove("is-waiting");
      }
    } catch (_) {}
    pauseWorkingLoop();
  }
  const composerNode = document.getElementById("composer");
  if (composerNode) wrap.insertBefore(card, composerNode.closest(".composer-switch") || composerNode);
  else wrap.prepend(card);
  // Restart the enter keyframes on every real call (fresh node or cached
  // restore): a forced reflow guarantees ask-in plays instead of popping.
  try {
    void card.offsetHeight;
  } catch (_) {}
  // The strip overlays the scroll: free its height + margin below the last
  // message so it never covers it while visible.
  try {
    const sc = document.getElementById("chatScroll");
    if (sc) sc.style.paddingBottom = `${200 + (card.offsetHeight || 160) + 24}px`;
  } catch (_) {}
  scrollToBottom();
}

async function decideApproval(qid, decision) {
  // Instant action — resumes the loop silently, no chat message.
  hideQuestionCard();
  try {
    const res = await postAnswer({ id: qid, decision });
    if (res && res.ok) clearPendingStored();
    // 410 = question already expired (answered, cancelled, timed out):
    // nothing to report, same as the skip/connect paths below.
    else if (!res || res.status !== 410) toast("Answer failed");
  } catch (_) {
    toast("Answer failed");
  }
}

async function decideNeedsModel(qid, decision, kind) {
  // Skip answers at once and the loop continues without the tool.
  // Connect does NOT answer yet: the strip stays above the composer
  // (refresh-safe via the stored pending question) while the AI Models
  // dialog opens; saving the model auto-answers and the SAME tool reruns.
  const isConnect = decision === "connect";
  if (isConnect) {
    pendingModelConnect = { id: qid, kind: String(kind || "text").toLowerCase() };
    pauseWorkingLoop();
    openModelPage(kind);
    return;
  }
  hideQuestionCard();
  try { clearPendingStored(); } catch (_) {}
  try {
    const res = await postAnswer({ id: qid, decision: "skip" });
    if (!(res && res.ok) && (!res || res.status !== 410)) toast("Answer failed");
  } catch (_) {
    toast("Answer failed");
  }
}

// A model saved on the AI Models page resumes the paused tool in place:
// the pending Connect answers now, the loop reruns the SAME tool, and the
// agent page shows it continue. STOP or expiry before that drops the wait.
let pendingModelConnect = null;
window.addEventListener("x5:models-changed", async () => {
  if (!pendingModelConnect) return;
  const { id } = pendingModelConnect;
  pendingModelConnect = null;
  try {
    const res = await postAnswer({ id, decision: "connect" });
    if (res && res.ok) {
      try { clearPendingStored(); } catch (_) {}
      hideQuestionCard();
      try { navigate("agent"); } catch (_) {}
    } else if (!res || res.status !== 410) {
      toast("Resume failed");
    }
  } catch (_) {
    toast("Resume failed");
  }
});

/* ---------------- live browser: control card + floating preview ---------------- */
function liveFrame(url) {
  // Remote console: loopback viewer links are re-pointed at this page's
  // host (same viewer port), or the iframe can never reach them. Local
  // pages keep the original URL untouched.
  let clean = remoteLiveUrl(url);
  if (!clean) clean = String(url || "");
  // Mixed-content guard: an http viewer inside an https console is blocked
  // outright by the browser. Best-effort upgrade to https so the live view
  // still opens when the relay serves TLS; otherwise the original URL stays.
  try {
    if (location.protocol === "https:" && clean.startsWith("http://")) {
      clean = "https://" + clean.slice("http://".length);
    }
  } catch (_) {}
  const frame = document.createElement("iframe");
  frame.className = "live-frame";
  frame.src = clean;
  frame.title = "Live browser";
  frame.setAttribute("allow", "clipboard-read; clipboard-write");
  frame.setAttribute("sandbox", "allow-scripts allow-same-origin allow-forms allow-pointer-lock");
  // Eager: the live view must stay connected while scrolling or tabbing away.
  frame.loading = "eager";
  return frame;
}

// The noVNC viewer (rfb.js inside the live iframe) warns
// "noVNC requires a secure context (TLS)" when the console is opened over
// plain http on a LAN IP. localhost and https are secure contexts and stay
// quiet. This explains it once instead of letting the console warn alone.
// The flag lives in sessionStorage, so a refresh does not repeat a notice
// the user already read in this tab.
const INSECURE_NOTICE_KEY = "x5_insecure_live_notice";
let insecureLiveWarned = false;
try {
  insecureLiveWarned = sessionStorage.getItem(INSECURE_NOTICE_KEY) === "1";
} catch (_) {}
function warnInsecureLiveOnce() {
  if (insecureLiveWarned) return;
  let insecure = false;
  try {
    insecure = window.isSecureContext === false;
  } catch (_) {}
  if (!insecure) return;
  insecureLiveWarned = true;
  try {
    sessionStorage.setItem(INSECURE_NOTICE_KEY, "1");
  } catch (_) {}
  try {
    toast("Live view is on an insecure connection — open via localhost or HTTPS to silence the noVNC secure-context warning");
  } catch (_) {}
}

function showLiveControlCard(q) {
  // Big in-steps handoff card: the browser interactive inside, fullscreen +
  // confirm-and-continue only, no ×. No default sentences anywhere: the ask
  // line renders only when the handoff carries real text, otherwise it is gone.
  // Always the last window; closing animates out.
  hideQuestionCard();
  document.getElementById("liveCard")?.remove();
  pendingQuestion = { id: q.id, kind: "live", selected: null };
  const url = String(q.url || "");
  if (!url) return;
  // A handoff owns the screen: the floating preview window and the right-edge
  // tab close first (and stay hidden until the user continues), so only the
  // handoff card remains — never a preview lurking behind it.
  manualControlOpen = true;
  pendingLiveQid = q.id;
  stopLiveWatch();
  try {
    document.getElementById("liveMini")?.remove();
    document.getElementById("browserTab")?.classList.add("off");
  } catch (_) {}
  const ask = String(q.reason || "").trim().slice(0, 500);
  const host = chatInner || document.body;
  const card = document.createElement("div");
  card.className = "live-card";
  card.id = "liveCard";
  card.innerHTML = `
    ${ask ? `<div class="live-ask"></div>` : ""}
    <div class="live-body"></div>
    <div class="live-actions">
      <button type="button" class="live-full" data-act="full" aria-label="Open fullscreen in a new tab">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6" /><path d="M9 21H3v-6" /><path d="M21 3l-7 7" /><path d="M3 21l7-7" /></svg>
      </button>
      <button type="button" class="glass-btn live-continue">Confirm &amp; Continue</button>
    </div>`;
  if (ask) {
    card.querySelector(".live-ask").textContent = ask;
    card.querySelector(".live-ask").setAttribute("dir", "auto");
  }
  card.querySelector(".live-body").appendChild(liveFrame(url));
  card.querySelector('[data-act="full"]').addEventListener("click", () => {
    try {
      window.open(remoteLiveUrl(url) || url, "_blank", "noopener");
    } catch (_) {}
  });
  card.querySelector(".live-continue").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.textContent = "…";
    try {
      const res = await postAnswer({ id: q.id, decision: "continue" });
      if (!res.ok) {
        // 410 = handoff already expired: close the card, no resume to offer.
        if (res.status === 410) {
          pendingQuestion = null;
          pendingLiveQid = null;
          card.classList.add("leaving");
          setTimeout(() => card.remove(), 200);
          try { clearPendingStored(); } catch (_) {}
          return;
        }
        throw new Error("bad status");
      }
      pendingQuestion = null;
      // Handoff over: bring the edge tab back at once (the resumed loop
      // emits no live frame, so waiting for it leaves the tab hidden).
      manualControlOpen = false;
      pendingLiveQid = null;
      card.classList.add("leaving");
      setTimeout(() => card.remove(), 200);
      try { restoreBrowserTabAfterContinue(); } catch (_) {}
      toast("Resumed");
    } catch (_) {
      btn.disabled = false;
      btn.textContent = "Confirm & Continue";
      toast("Answer failed");
    }
  });
  host.appendChild(card);
  scrollToBottom();
}

/* ---------- right-edge browser tab: fixed right, vertical drag only ---------- */
let browserStreamUrl = "";
let pendingLiveQid = null;
// Pinned to the right edge on desktop and phone alike. Only the vertical
// offset is remembered; there is no horizontal position and no shrinking.
let browserTabY = null;
// While the big manual control card is open, the preview template stays hidden.
let manualControlOpen = false;

function placeTab(x, y) {
  // Fixed-right rail: the tab never leaves the right edge. The first arg is
  // kept for caller compatibility but ignored — only the vertical position
  // moves, clamped on-screen. Left/right are pinned so no axis can fight
  // and stretch the tab.
  const tab = document.getElementById("browserTab");
  if (!tab) return;
  const h = tab.offsetHeight || 150;
  const top = (typeof y === "number" ? y : (typeof x === "number" ? x : 0)) || 0;
  const cy = Math.min(Math.max(60, top), Math.max(60, window.innerHeight - h - 8));
  browserTabY = cy;
  tab.style.left = "auto";
  tab.style.right = "0";
  tab.style.bottom = "auto";
  tab.style.top = `${cy}px`;
}

let tabClampBound = false;
function bindTabClampOnce() {
  // Rotation/resize must never strand a freely-placed tab off-screen.
  if (tabClampBound) return;
  tabClampBound = true;
  window.addEventListener("resize", () => {
    try {
      if (browserTabY !== null) placeTab(0, browserTabY);
    } catch (_) {}
  });
}

function refreshEdgeTab() {
  const tab = document.getElementById("browserTab");
  if (!tab) return;
  const badge = tab.querySelector(".tab-continue");
  if (badge) badge.hidden = !pendingLiveQid;
}

function showEdgeTab(url, continueQid) {
  // Partially-visible tab on the right edge while the browser runs.
  // Pull it out (or click it) to open the live player.
  const clean = typeof url === "string" ? url : "";
  if (!clean) return;
  browserStreamUrl = clean;
  if (continueQid) {
    pendingLiveQid = continueQid;
    pendingQuestion = { id: continueQid, kind: "live", selected: null };
  } else if (manualControlOpen) {
    // Manual control card owns the screen: keep the URL fresh but leave the
    // preview template hidden until the card closes.
    return;
  }
  let tab = document.getElementById("browserTab");
  if (!tab) {
    tab = document.createElement("div");
    tab.id = "browserTab";
    tab.setAttribute("role", "button");
    tab.setAttribute("tabindex", "0");
    tab.setAttribute("aria-label", "Open browser preview");
    tab.innerHTML = `
      <button type="button" class="tab-x" aria-label="Close browser">✕</button>
      <span class="tab-label">Browser Running</span>
      <span class="tab-continue" hidden aria-hidden="true"></span>`;
    tab.querySelector(".tab-x").addEventListener("click", (e) => {
      e.stopPropagation();
      showCloseBrowserDialog();
    });
    document.body.appendChild(tab);
    if (browserTabY !== null) placeTab(0, browserTabY);
    initTabDrag(tab);
    bindTabClampOnce();
  }
  warnInsecureLiveOnce();
  refreshEdgeTab();
  // A parked handoff hid the tab with .off and the resumed loop emits no
  // live frame for human_control, so every normal show must unhide it again
  // (never while a visible player owns the screen — the tab stays parked).
  try {
    const _mini = document.getElementById("liveMini");
    const _shown = !!(_mini && !_mini.classList.contains("mini-hidden"));
    if (!_shown) tab.classList.remove("off");
  } catch (_) {}
  const player = document.getElementById("liveMini");
  if (player) {
    // Sticky frame: never re-point an open player here (no auto reload).
    // Fresh links stay in browserStreamUrl and apply only on manual reload
    // or the next fresh build.
    syncPlayerContinue(player);
  }
}

function restoreBrowserTabAfterContinue() {
  // Take-Control Continue closed the handoff card but nothing re-shows the
  // edge tab (no live frame follows). Restore it at once from the remembered
  // link, else ask the server for the current live link. No boot, no reload.
  try {
    const mini = document.getElementById("liveMini");
    if (mini && !mini.classList.contains("mini-hidden")) {
      refreshEdgeTab();
      return;
    }
    if (typeof browserStreamUrl === "string" && browserStreamUrl) {
      try { showEdgeTab(browserStreamUrl); } catch (_) {}
      try { document.getElementById("browserTab")?.classList.remove("off"); } catch (_) {}
      try { if (typeof liveGuardSoon === "function") liveGuardSoon(); } catch (_) {}
      return;
    }
    fetch("/api/live_state", { cache: "no-store" })
      .then((r) => (r.ok ? r.json().catch(() => null) : null))
      .then((data) => {
        try {
          const st = data && data.active;
          const u = st && typeof st.url === "string" ? st.url : "";
          if (u) {
            showEdgeTab(u);
          } else {
            fetch("/api/browser", { cache: "no-store" })
              .then((rb) => (rb.ok ? rb.json().catch(() => null) : null))
              .then((bd) => {
                try {
                  const su = bd && bd.stream && bd.stream.url;
                  if (typeof su === "string" && su) showEdgeTab(su);
                } catch (_) {}
                try { document.getElementById("browserTab")?.classList.remove("off"); } catch (_) {}
              })
              .catch(() => {});
          }
          try { document.getElementById("browserTab")?.classList.remove("off"); } catch (_) {}
        } catch (_) {}
      })
      .catch(() => {});
  } catch (_) {}
}

function showLiveLink(live) {
  // One place decides how a live link is shown: a control link stays the
  // right-edge tab so the user can drive it; the show_preview tool opens the
  // floating watch-only window (idempotent — no reopen when already on screen);
  // every other watch-only link keeps the existing right-edge tab.
  // Guard: the floating preview player NEVER renders a control URL. If a
  // control link ever arrives here (stale frame, mixed state), convert it to
  // the watch-only form instead of opening an interactive surface.
  try {
    let url = live && typeof live.url === "string" ? live.url : "";
    if (!url) return;
    markLiveSeen();
    if (live.view_only === false && live.tool !== "show_preview") {
      showEdgeTab(url);
      return;
    }
    if (live.tool === "show_preview") {
      showPreviewPlayer(toPreviewUrl(url));
      return;
    }
    showEdgeTab(url);
  } catch (_) {}
}

function toPreviewUrl(url) {
  // Force the watch-only form: the library mints a distinct preview token,
  // so a control URL must never be shown in the preview player. Best-effort
  // client-side guard (server already prefers the preview link): flip the
  // explicit view_only flag to true, preserving token/scale/path.
  try {
    const u = new URL(String(url || ""), location.href);
    u.searchParams.set("view_only", "true");
    return u.toString();
  } catch (_) {
    return String(url || "");
  }
}

function showCloseBrowserDialog() {
  // The × opens a real confirm dialog (closing stops the browser for real).
  openModal(`
    <div class="modal-head">
      <div class="tile"><span class="tile-letters">!</span></div>
      <div class="modal-head-text"><h3>Close browser?</h3><p>The live session stops for real</p></div>
      <button class="modal-close" data-close type="button" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
    <div class="modal-body"><p class="modal-body-text">Closing stops the browser session and the agent loses the live page. The tab and the preview will be removed.</p></div>
    <div class="modal-foot">
      <button class="btn" data-close type="button">Cancel</button>
      <button class="btn btn-danger" id="confirmCloseBrowserBtn" type="button">Close browser</button>
    </div>`);
  document.getElementById("confirmCloseBrowserBtn")?.addEventListener("click", async (e) => {
    // Close only clears the UI after the server confirms the browser stopped;
    // otherwise the poller truthfully brings the still-running tab back.
    const btn = e.currentTarget;
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      const { res, data } = await postBrowserApi("close", 60000);
      if (!res.ok) {
        const why = String((data && (data.detail || data.error)) || "").slice(0, 160);
        throw new Error(why || "Close failed");
      }
      try {
        sessionStorage.setItem(BROWSER_CLOSED_KEY, "1");
      } catch (_) {}
      closeModal();
      clearLiveUI();
      toast("Browser closed");
    } catch (err) {
      btn.disabled = false;
      btn.classList.remove("loading");
      toast(err instanceof Error && err.message ? err.message : "Close failed");
    }
  });
}

/* ---------- floating live player (opens from the edge tab, fresh load) ---------- */
function syncPlayerContinue(player) {
  // Paused live handoff: the Continue button lives in the player header.
  // Preview mode: a Take-control button sits on the right instead.
  const spot = player ? player.querySelector(".live-mini-spot") : null;
  if (!spot) return;
  let btn = spot.querySelector('[data-act="continue"]');
  if (pendingLiveQid) {
    if (!btn) {
      btn = document.createElement("button");
      btn.type = "button";
      btn.className = "glass-btn live-mini-btn";
      btn.dataset.act = "continue";
      btn.textContent = "Continue";
      spot.prepend(btn);
    }
    const qid = pendingLiveQid;
    btn.hidden = false;
    btn.onclick = () => answerLiveContinue(qid, btn);
    spot.querySelector('[data-act="take-control"]')?.remove();
  } else {
    if (btn) btn.remove();
    let ctl = spot.querySelector('[data-act="take-control"]');
    if (!ctl) {
      ctl = document.createElement("button");
      ctl.type = "button";
      ctl.className = "live-full";
      ctl.dataset.act = "take-control";
      ctl.innerHTML = TAKE_CONTROL_SVG;
      ctl.setAttribute("aria-label", "Take control");
      ctl.title = "Take control — switch to the control live and open it big in chat";
      ctl.onclick = () => takeControlFromPreview(ctl);
      const reloadBtn = spot.querySelector('[data-act="reload"]');
      if (reloadBtn) spot.insertBefore(ctl, reloadBtn);
      else spot.prepend(ctl);
    }
  }
}

function showPreviewPlayer(url) {
  // show_preview tool: bring the floating preview window up (watch-only).
  // Already showing this exact link -> do nothing (no reopen, no flicker).
  // The player is watch-only only: force the preview form so a control URL
  // can never render interactive inside the floating template.
  try {
    const clean = toPreviewUrl(String(url || ""));
    if (!clean) return;
    markLiveSeen();
    const mini = document.getElementById("liveMini");
    const frame = mini && mini.querySelector(".live-frame");
    if (mini && frame && frame.getAttribute("src") === (remoteLiveUrl(clean) || clean)) return;
    browserStreamUrl = clean;
    openLivePlayer();
  } catch (_) {}
}

function openLivePlayer(atX, atY) {
  // Tap on the edge tab. If the remembered link was lost somehow, recover
  // it from the server instead of silently doing nothing.
  if (!browserStreamUrl) {
    fetch("/api/live_state", { cache: "no-store" })
      .then((r) => (r.ok ? r.json().catch(() => null) : null))
      .then((data) => {
        const u = data && data.active && data.active.url;
        if (typeof u === "string" && u.startsWith("http")) {
          browserStreamUrl = u;
          openLivePlayer(atX, atY);
        } else {
          try {
            toast("No live stream — start the browser first");
          } catch (_) {}
        }
      })
      .catch(() => {
        try {
          toast("No live stream — start the browser first");
        } catch (_) {}
      });
    return;
  }
  // Instant open with the remembered link (the tap must never wait on the
  // network); the persisted URL is adopted in the background when it differs.
  _buildLivePlayer(atX, atY);
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => {
      try {
        ctl.abort();
      } catch (_) {}
    }, 6000);
    fetch("/api/live_state", { cache: "no-store", signal: ctl.signal })
      .then((r) => (r.ok ? r.json().catch(() => null) : null))
      .then((data) => {
        try {
          clearTimeout(timer);
          const u = data && data.active && data.active.url;
          // Remember only — never re-point the open frame here (no auto reload).
          if (typeof u === "string" && u.startsWith("http") && u !== browserStreamUrl) {
            browserStreamUrl = u;
          }
        } catch (_) {}
      })
      .catch(() => {
        try {
          clearTimeout(timer);
        } catch (_) {}
      });
  } catch (_) {}
}

function _buildLivePlayer(atX, atY, forPull) {
  if (!browserStreamUrl) return;
  ++liveSeq; // this open owns the player; any pending dock finish dies
  // Sticky frame: reopening (minimize -> maximize) reuses the SAME iframe
  // with no reload — only a real error rebuilds it. The URL is adopted when
  // it actually changed (fresh session), never re-pointed blindly.
  const existing = document.getElementById("liveMini");
  if (existing) {
    // Reopen blooms out of the tab point (single motion: kill strays, park
    // final spot, measure, ONE bloom — nothing moves after).
    try {
      liveKillAnims(existing);
      liveTabReset();
      existing.classList.remove("mini-hidden", "docking", "live-pulling", "live-anim");
      try { delete existing.dataset.docking; } catch (_) {}
      existing.style.opacity = "";
      existing.style.transform = "";
      existing.style.transformOrigin = "";
      // Re-clamp inside the viewport: a docked-away position must never
      // restore half off-screen.
      const _w = existing.offsetWidth || 264;
      const _h = existing.offsetHeight || 160;
      const _r = existing.getBoundingClientRect();
      existing.style.left = `${Math.min(Math.max(8, _r.left), Math.max(8, window.innerWidth - _w - 8))}px`;
      existing.style.top = `${Math.min(Math.max(70, _r.top), Math.max(70, window.innerHeight - _h - 8))}px`;
      existing.style.right = "auto";
      const delta = liveTabDelta(existing);
      document.getElementById("browserTab")?.classList.add("off");
      if (!forPull) liveBloomFromTab(existing, delta, 520);
      startLiveWatch();
    } catch (_) {}
    if (!forPull) liveGuardSoon();
    return;
  }
  if (!forPull) document.getElementById("browserTab")?.classList.add("off");
  const mini = document.createElement("div");
  mini.id = "liveMini";
  mini.innerHTML = `
    <div class="live-mini-head" data-drag>
      <span>Browser Running</span>
      <span class="live-mini-spot">
        <button type="button" class="live-full" data-act="reload" aria-label="Reload live view">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10" /><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10" /></svg>
        </button>
        <button type="button" class="live-mini-x" data-act="close" aria-label="Minimize" title="Minimize">✕</button>
      </span>
    </div>
    <div class="live-mini-body"></div>
    <div class="live-resize" data-edge="right" aria-hidden="true"></div>
    <div class="live-resize" data-edge="bottom" aria-hidden="true"></div>
    <div class="live-resize" data-edge="corner" aria-hidden="true"></div>
    <div class="live-resize" data-edge="corner-left" aria-hidden="true"></div>`;
  // Fresh iframe only on first build or after a real error. Minimize /
  // maximize never rebuilds — same frame, no reload (only error reloads).
  const freshFrame = liveFrame(browserStreamUrl);
  try {
    freshFrame.addEventListener("error", () => {
      try { refreshLiveFrameFromServer(); } catch (_) {}
    });
  } catch (_) {}
  mini.querySelector(".live-mini-body").appendChild(freshFrame);
  mini.querySelector('[data-act="reload"]').addEventListener("click", () => reloadLiveFrame());
  mini.querySelector('[data-act="close"]').addEventListener("click", () => {
    // Player × sucks the player back into the edge tab (same black-hole
    // language): SAME iframe kept alive, stream keeps running, reopen is
    // instant with no Loading. Real closing = edge tab ×.
    try {
      dockPlayerToTab(mini.getBoundingClientRect().top);
    } catch (_) {
      dockPlayerToTab(0);
    }
  });
  syncPlayerContinue(mini);
  document.body.appendChild(mini);
  startLiveWatch();
  warnInsecureLiveOnce();
  // Free-floating window: opens around the tap point (desktop and phone),
  // then drags smoothly anywhere via makeDraggable below. Position is
  // clamped against the MEASURED size (CSS widens it on phones), so it can
  // never stick out past the viewport edges.
  mini.style.visibility = "hidden";
  const mw = mini.offsetWidth || 264;
  const mh = mini.offsetHeight || 160;
  const maxLeft = Math.max(8, window.innerWidth - mw - 8);
  const maxTop = Math.max(70, window.innerHeight - mh - 8);
  const left = typeof atX === "number"
    ? Math.min(Math.max(8, atX - mw / 2), maxLeft)
    : Math.min(Math.max(8, window.innerWidth - mw - 16), maxLeft);
  const top = typeof atY === "number"
    ? Math.min(Math.max(70, atY), maxTop)
    : Math.min(Math.max(70, Math.round(window.innerHeight * 0.3)), maxTop);
  mini.style.left = `${left}px`;
  mini.style.top = `${top}px`;
  mini.style.visibility = "";
  makeDraggable(mini, mini.querySelector("[data-drag]"));
  makeResizable(mini);
  bindMiniClampOnce();
  if (!forPull) {
    // Fresh nodes bloom out of the tab point (reverse black hole).
    const delta = liveTabDelta(mini);
    liveBloomFromTab(mini, delta, 520);
    liveGuardSoon();
  }
}

const TAKE_CONTROL_SVG = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3l7 17 2.5-7.5L22 10z" /><path d="M14.5 13.5L19 18" /></svg>`;

function reloadLiveFrame() {
  // Re-point the iframe at the freshest known stream URL (heals "Not Connected").
  // Rendered through the remote rewrite so reloads never regress to loopback.
  const player = document.getElementById("liveMini");
  const frame = player ? player.querySelector(".live-frame") : null;
  if (!frame) return;
  const fresh = remoteLiveUrl(browserStreamUrl || frame.getAttribute("src") || "");
  if (fresh) frame.src = fresh;
}

async function refreshLiveFrameFromServer() {
  // The minted viewer link can die server-side (WSL forwarding flap, expired
  // token after an x5-novnc restart) while the player keeps reloading the
  // same stale src forever. Ask the server for the persisted URL first and
  // adopt it when it differs; otherwise fall back to a plain reload.
  // Best-effort and bounded — failures simply reload the known URL.
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => {
      try {
        ctl.abort();
      } catch (_) {}
    }, 8000);
    const res = await fetch("/api/live_state", { cache: "no-store", signal: ctl.signal });
    clearTimeout(timer);
    const data = res.ok ? await res.json().catch(() => null) : null;
    const u = data && data.active && data.active.url;
    const fresh = typeof u === "string" && u.startsWith("http") ? u : "";
    const player = document.getElementById("liveMini");
    const frame = player ? player.querySelector(".live-frame") : null;
    if (!frame) return;
    if (fresh) {
      browserStreamUrl = fresh;
      const out = remoteLiveUrl(fresh);
      if (frame.getAttribute("src") !== out) frame.src = out;
      else reloadLiveFrame();
      return;
    }
  } catch (_) {}
  try {
    reloadLiveFrame();
  } catch (_) {}
}

let lastLiveRecoverAt = 0;

async function recoverExpiredLiveLink() {
  // The stored token died server-side (console restart, new live session), so
  // reloading it can never work: the panel would sit on "Loading" until the
  // user asked the agent again. Mint a fresh link instead - persisted one
  // first, then the server's preview mint - and re-point the open player.
  // Bounded (once per 30s), skipped while a control/handoff UI owns the
  // screen, and the frame keeps its old src until a new link really exists.
  const now = Date.now();
  if (now - lastLiveRecoverAt < 30000) return;
  if (manualControlOpen || pendingLiveQid) return;
  lastLiveRecoverAt = now;
  let url = "";
  try {
    const res = await fetch("/api/live_state", { cache: "no-store" });
    const data = res.ok ? await res.json().catch(() => null) : null;
    const u = data && data.active && data.active.url;
    if (typeof u === "string" && u.startsWith("http")) url = u;
  } catch (_) {}
  if (!url) {
    try {
      const { res, data } = await postBrowserApi("preview", 30000);
      const u = data && typeof data.url === "string" ? data.url : "";
      if (res.ok && u) url = u;
    } catch (_) {}
  }
  if (!url) return;
  browserStreamUrl = url;
  const player = document.getElementById("liveMini");
  const frame = player ? player.querySelector(".live-frame") : null;
  if (!frame) return;
  markLiveSeen();
  const out = remoteLiveUrl(url);
  if (frame.getAttribute("src") !== out) frame.src = out;
  else reloadLiveFrame();
}

let liveWatchTimer = 0;
let liveWatchMisses = 0;

function stopLiveWatch() {
  try {
    if (liveWatchTimer) clearInterval(liveWatchTimer);
  } catch (_) {}
  liveWatchTimer = 0;
  liveWatchMisses = 0;
}

function startLiveWatch() {
  // The VNC socket can die silently (forwarding flap, insecure-context drop,
  // WSL distro terminate cycle) while the page sits "loading" forever:
  // a cheap state probe of the exact frame URL mints a fresh link when the
  // token expired and reloads it after 2 misses when the relay is unreachable.
  // Runs only while the player is open and is cleared with it, so it never
  // spins alone.
  stopLiveWatch();
  try {
    liveWatchTimer = setInterval(async () => {
      try {
        const player = document.getElementById("liveMini");
        const frame = player ? player.querySelector(".live-frame") : null;
        const src = frame ? frame.getAttribute("src") : "";
        if (!src) {
          stopLiveWatch();
          return;
        }
        const state = await liveUrlState(src);
        if (state === "expired") {
          // A dead token never recovers by reloading: mint a fresh one.
          liveWatchMisses = 0;
          try {
            await recoverExpiredLiveLink();
          } catch (_) {}
          return;
        }
        if (state === "unreachable") {
          liveWatchMisses += 1;
          if (liveWatchMisses >= 2) {
            liveWatchMisses = 0;
            // Adopt a fresh server URL when the stored one died (new host or
            // rotated token); plain reload otherwise.
            try {
              await refreshLiveFrameFromServer();
            } catch (_) {}
          }
          return;
        }
        liveWatchMisses = 0;
      } catch (_) {}
    }, 20000);
  } catch (_) {}
}

let liveHiddenAt = 0;

function reviveLiveFrames() {
  const frames = document.querySelectorAll("#liveMini .live-frame, .live-card .live-frame");
  if (!frames.length) {
    // No live UI at all while the tab may still run: a running browser with
    // no known link (server reboot cleared it, open never persisted one)
    // would otherwise leave no edge tab forever. Re-attach in background.
    fetch("/api/browser", { cache: "no-store" })
      .then((r) => (r.ok ? r.json().catch(() => null) : null))
      .then((bd) => {
        if (!bd || !bd.running) return;
        const su = bd.stream && bd.stream.url;
        if (typeof su === "string" && su) {
          markLiveSeen();
          showEdgeTab(su);
        } else {
          autoMintMissingPreview();
        }
      })
      .catch(() => {});
    return;
  }
  // Frames exist: never auto-reload them here. Remember the freshest link
  // for the next manual reload; the open frame keeps its connection.
  fetch("/api/live_state", { cache: "no-store" })
    .then((r) => (r.ok ? r.json().catch(() => null) : null))
    .then((data) => {
      const st = data && data.active;
      const u = st && typeof st.url === "string" ? st.url : "";
      if (!u) return;
      if (u !== browserStreamUrl) browserStreamUrl = u;
    })
    .catch(() => {});
}

document.addEventListener("visibilitychange", () => {
  try {
    if (document.hidden) {
      liveHiddenAt = Date.now();
    } else if (liveHiddenAt && Date.now() - liveHiddenAt > 20000) {
      liveHiddenAt = 0;
      reviveLiveFrames();
    }
  } catch (_) {}
});

function dockPlayerToTab(top) {
  // Back to the right edge with the same iPhone-like black-hole suck as the
  // scheduler template: the edge tab slides back in first as the visible
  // target at the drop Y, the player shrinks into it (~700ms expo-out,
  // compositor-only), then parks alive (SAME iframe, stream keeps running) —
  // never destroyed, so reopening is instant with no Loading. Only a real
  // browser close destroys it.
  ++liveSeq; // this dock owns the outcome; a newer open/pull overrules it
  const mySeq = liveSeq;
  try {
    const y = Math.min(Math.max(70, top), Math.max(70, window.innerHeight - 140));
    if (browserTabY !== null) placeTab(0, y);
    else {
      const tab0 = document.getElementById("browserTab");
      if (tab0) {
        tab0.style.left = "auto";
        tab0.style.right = "0";
        tab0.style.top = `${y}px`;
        tab0.style.bottom = "auto";
      }
    }
  } catch (_) {}
  const mini = document.getElementById("liveMini");
  try {
    // The tab re-enters from the right: drop any stale inline push first so
    // the .off removal below glides it in (never a blink).
    const tab = document.getElementById("browserTab");
    if (tab) {
      tab.style.transform = "";
      tab.style.opacity = "";
    }
  } catch (_) {}
  try { document.getElementById("browserTab")?.classList.remove("off"); } catch (_) {}
  const finish = () => {
    if (mySeq !== liveSeq) return; // superseded by a newer open/pull — hands off
    const m = document.getElementById("liveMini");
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
    const tab = document.getElementById("browserTab");
    if (!tab && browserStreamUrl) {
      try { showEdgeTab(browserStreamUrl); } catch (_) {}
      return;
    }
    try { refreshEdgeTab(); } catch (_) {}
    liveGuardSoon(); // watchdog: tab back on the rail, player parked
  };
  if (mini && !mini.dataset.docking) {
    mini.dataset.docking = "1";
    liveKillAnims(mini); // never stack a new suck over a stale one
    mini.classList.remove("live-anim", "live-pulling");
    mini.style.opacity = "";
    mini.style.transform = "";
    let sucked = false;
    if (!liveReducedMotion() && typeof mini.animate === "function") {
      try {
        // Right-center of the player must land on the tab center (measured
        // AFTER the tab was parked at the drop Y above).
        const d = liveTabDelta(mini);
        const mx = Math.round(d.dx * 0.52);
        const my = Math.round(d.dy * 0.52);
        mini.style.transformOrigin = "100% 50%";
        mini.classList.add("docking");
        mini.animate(
          [
            { transform: "translateX(0px) scale(1)", opacity: 1, offset: 0 },
            { transform: "translateX(-12px) scale(0.988)", opacity: 1, offset: 0.3 },
            { transform: `translateX(${mx}px) translateY(${my}px) scale(0.58)`, opacity: 0.94, offset: 0.62 },
            { transform: `translateX(${d.dx}px) translateY(${d.dy}px) scale(0.05)`, opacity: 0, offset: 1 },
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

function maybeDockPlayer(mini, wasMoved) {
  // Dock priority: pushing the player a quarter or more past the viewport's
  // right edge docks it (the suck wins) — then it shrinks into the edge tab
  // with the black-hole animation and the tab returns at the drop height. A
  // tap, a leftward move, or a gentle drop never hides it: it snaps inside.
  // NOTE: `>=` is deliberate — the drag clamp parks left at exactly the half
  // boundary at full shove, so strict `>` would never fire there.
  try {
    const r = mini.getBoundingClientRect();
    const w = r.width || mini.offsetWidth || 264;
    const h = r.height || mini.offsetHeight || 160;
    if (wasMoved && r.left >= window.innerWidth - w * 0.75) {
      dockPlayerToTab(r.top);
      return;
    }
    const nx = Math.min(Math.max(0, r.left), Math.max(0, window.innerWidth - w - 8));
    const ny = Math.min(Math.max(0, r.top), Math.max(0, window.innerHeight - h - 8));
    mini.style.left = `${nx}px`;
    mini.style.top = `${ny}px`;
  } catch (_) {}
}

/* ---------- edge motion: same black-hole/pull language as the scheduler template (browser ids) ----------
   Mirrors schedule.js one-to-one (pull-follow + reverse bloom open + FLIP
   settle + black-hole suck dock + generation guard + watchdog), only the
   node ids differ (#browserTab / #liveMini). Kept beside the player on
   purpose: the player owns iframe/resize/live-watch lifecycles that must
   never tangle with the scheduler template. */
let liveSeq = 0; // lifecycle generation: stale timers never kill newer state
let livePull = null; // active horizontal pull-out gesture {sx, sy}
let livePullSX = 0; // pull gesture start X

function liveReducedMotion() {
  try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  catch (_) { return false; }
}

function liveGuard() {
  // Visibility watchdog: repair stuck states only — never resurrect a real
  // close (clearLiveUI removes both nodes deliberately: both-missing = gone).
  try {
    if (livePull) return;
    if (typeof manualControlOpen !== "undefined" && manualControlOpen) return;
    const tab = document.getElementById("browserTab");
    const mini = document.getElementById("liveMini");
    if (!tab && !mini) return;
    const shown = !!(mini && !mini.classList.contains("mini-hidden"));
    if (shown) {
      if (tab && !tab.classList.contains("off")) {
        try { tab.classList.add("off"); } catch (_) {}
      }
      return;
    }
    if (tab) {
      tab.classList.remove("pulling", "dragging");
      tab.style.opacity = "";
      tab.style.transform = "";
      try { tab.classList.remove("off"); } catch (_) {}
    }
  } catch (_) {}
}

function liveGuardSoon() {
  // Re-check after transitions settle; generation-captured so a newer
  // lifecycle invalidates the pending check (never yanks a fresh pull).
  try { const g = liveSeq; setTimeout(() => { if (g === liveSeq) liveGuard(); }, 850); } catch (_) {}
}

function liveKillAnims(mini) {
  // A superseded suck/bloom (fill:forwards) would pin the player invisible
  // under the new state — always kill before reusing the node.
  try {
    const anims = mini && mini.getAnimations ? mini.getAnimations() : [];
    anims.forEach((a) => { try { a.cancel(); } catch (_) {} });
  } catch (_) {}
}

function liveTabReset() {
  // The tab hides ONLY via .off — never leave a stale inline push/fade.
  try {
    const tab = document.getElementById("browserTab");
    if (!tab) return;
    tab.classList.remove("pulling", "dragging");
    tab.style.transform = "";
    tab.style.opacity = "";
  } catch (_) {}
}

function liveTabDelta(mini) {
  // Vector from the player's right-center to the edge tab's center — the
  // black-hole axis (dock/suck-back) and its reverse (bloom open).
  try {
    const r = mini.getBoundingClientRect();
    const tab = document.getElementById("browserTab");
    if (!tab) return { dx: Math.max(120, Math.round(r.width * 0.6)), dy: 0 };
    const t = tab.getBoundingClientRect();
    return {
      dx: Math.round((t.left + t.width / 2) - r.right + 4),
      dy: Math.round((t.top + t.height / 2) - (r.top + r.height / 2)),
    };
  } catch (_) { return { dx: 160, dy: 0 }; }
}

function liveBloomFromTab(mini, delta, ms) {
  // Reverse black hole out of the tab point, same expo-out curve as the suck.
  // .live-anim stays ON afterwards forever (removing it would replay x5-rise).
  try {
    if (!mini || typeof mini.animate !== "function" || liveReducedMotion()) return;
    const d = delta || liveTabDelta(mini);
    const dur = ms || 520;
    mini.classList.remove("live-anim");
    void mini.offsetWidth;
    mini.classList.add("live-anim");
    mini.style.transformOrigin = "100% 50%";
    mini.animate(
      [
        { transform: `translateX(${d.dx}px) translateY(${d.dy}px) scale(0.05)`, opacity: 0, offset: 0 },
        { transform: `translateX(${Math.round(d.dx * 0.52)}px) translateY(${Math.round(d.dy * 0.52)}px) scale(0.55)`, opacity: 0.92, offset: 0.45 },
        { transform: "translateX(0px) scale(1)", opacity: 1, offset: 1 },
      ],
      { duration: dur, easing: "cubic-bezier(0.32, 0.72, 0, 1)" }
    );
  } catch (_) {}
}

function liveSettleFrom(mini, fromLeft, fromTop, fromScale, fromO, ms) {
  // FLIP settle after a pull release: continues EXACTLY from the pull's
  // visual state (same scale + opacity the finger left) and glides+grows
  // into the parked spot — zero jump in place, zero pop.
  try {
    if (!mini || typeof mini.animate !== "function" || liveReducedMotion()) return;
    const r = mini.getBoundingClientRect();
    const ddx = Math.round(fromLeft - r.left);
    const ddy = Math.round(fromTop - r.top);
    const s = Number(fromScale);
    const o = Number(fromO);
    if (!ddx && !ddy && !(Number.isFinite(s) && s < 0.999)) return;
    const dur = ms || 280;
    mini.classList.remove("live-anim");
    void mini.offsetWidth;
    mini.classList.add("live-anim");
    mini.style.transformOrigin = "100% 50%"; // same origin as the pull bloom
    mini.animate(
      [
        { transform: `translateX(${ddx}px) translateY(${ddy}px) scale(${Number.isFinite(s) ? s : 1})`, opacity: Number.isFinite(o) ? o : 1, offset: 0 },
        { transform: "translateX(0px) translateY(0px) scale(1)", opacity: 1, offset: 1 },
      ],
      { duration: dur, easing: "cubic-bezier(0.22, 1, 0.36, 1)" }
    );
    setTimeout(() => { try { mini.style.transformOrigin = ""; } catch (_) {} }, dur + 30);
  } catch (_) {}
}

function livePullTop(y, h) {
  // Vertical-only clamp mid-pull: the player may hang past the right edge
  // (coming out from behind it) — strictly parked only on release.
  try {
    const vh = window.innerHeight || 480;
    return Math.min(Math.max(70, y), Math.max(70, vh - (h || 160) - 8));
  } catch (_) { return 70; }
}

function livePullProgress(pulled) {
  // One continuous birth curve for the whole pull (preview → move → commit
  // settle): starts a ghost at the tab point, full-size past ~140px.
  const p = Math.min(1, Math.max(0, pulled) / 140);
  const e = 1 - (1 - p) * (1 - p);
  return { p, s: 0.05 + 0.95 * e, o: 0.05 + 0.95 * p };
}

function livePullPreview(px, pointerY) {
  // Born at the tab, out WITH the finger: center tracks the pointer while it
  // grows (scale + opacity over ~140px). Same task as the build — no flash.
  ++liveSeq; // invalidate any pending dock finish
  let mini = document.getElementById("liveMini");
  if (!mini) {
    if (!browserStreamUrl || !livePull) return;
    _buildLivePlayer(livePull.sx - 132, pointerY, true); // built for pull: no bloom, tab stays
    mini = document.getElementById("liveMini");
    if (!mini) { livePull = null; return; }
  }
  try {
    liveKillAnims(mini);
    liveTabReset();
    mini.classList.remove("docking", "mini-hidden", "live-pulling", "live-anim");
    try { delete mini.dataset.docking; } catch (_) {}
    mini.classList.add("live-pulling");
    mini.style.visibility = "";
    const w = mini.offsetWidth || 264;
    const h = mini.offsetHeight || 160;
    // Anchor geometry parked ONCE — every later frame moves via transform
    // only (compositor), left/top are never touched again mid-pull.
    livePull.w = w;
    livePull.h = h;
    livePull.left0 = livePull.sx - w / 2;
    livePull.top0 = livePullTop(pointerY - 90, h);
    livePull.lx = px;
    livePull.ly = pointerY;
    livePull.raf = 0;
    mini.style.left = `${livePull.left0}px`;
    mini.style.top = `${livePull.top0}px`;
    mini.style.right = "auto";
    mini.style.transformOrigin = "100% 50%";
    livePullApply(); // first frame, same task — no flash
  } catch (_) {}
}

function livePullApply() {
  // ONE compositor-cheap frame for the whole pull: position via transform
  // only (left/top stay parked from preview) + opacity + tab slide-out. No
  // layout reads, no layout writes — mid-pull jank is impossible.
  const pull = livePull;
  const mini = pull && document.getElementById("liveMini");
  if (!pull || !mini || typeof pull.left0 !== "number") return;
  try {
    const g = livePullProgress(pull.sx - pull.lx);
    const tx = Math.round(pull.lx - pull.sx);
    const ty = Math.round(livePullTop(pull.ly - 90, pull.h || 160) - pull.top0);
    mini.style.transform = `translate(${tx}px, ${ty}px) scale(${g.s.toFixed(3)})`;
    mini.style.opacity = String(g.o);
    try {
      const tab = document.getElementById("browserTab");
      if (tab) tab.style.transform = `translateX(${(g.p * 130).toFixed(1)}%)`;
    } catch (_) {}
  } catch (_) {}
}

function livePullSchedule() {
  // Coalesce bursts of pointermove into one rAF frame (many moves per paint
  // would otherwise stack style recalcs and stutter).
  if (!livePull || livePull.raf) return;
  try {
    livePull.raf = requestAnimationFrame(() => {
      try { if (livePull) livePull.raf = 0; } catch (_) {}
      livePullApply();
    });
  } catch (_) { livePullApply(); }
}

function livePullCommit(px, py, far) {
  // Magnetic catch: parked strictly inside continuing the pull's own
  // scale/opacity (no pop, no jump) — then a normal player, draggable away.
  ++liveSeq; // this commit owns the player now
  const mini = document.getElementById("liveMini");
  let from = null;
  try {
    if (mini) {
      liveKillAnims(mini); // measure the real rect, not a stale suck's ghost
      const r = mini.getBoundingClientRect();
      from = { left: r.left, top: r.top };
    }
  } catch (_) { from = null; }
  try {
    if (mini) {
      const w = mini.offsetWidth || 264;
      const h = mini.offsetHeight || 160;
      const maxLeft = Math.max(8, window.innerWidth - w - 8);
      const maxTop = Math.max(70, window.innerHeight - h - 8);
      mini.style.left = `${Math.min(Math.max(8, px - w / 2), maxLeft)}px`;
      mini.style.top = `${Math.min(Math.max(70, py - 90), maxTop)}px`;
      mini.style.right = "auto";
      mini.style.transform = "";
      mini.style.opacity = "";
      mini.classList.remove("live-pulling");
      // Continue the pull's own scale/opacity into the settle (origin stays
      // right-center until the settle releases it) — no pop, no jump.
      const cg = livePullProgress(Number(far) || 0);
      if (from) liveSettleFrom(mini, from.left, from.top, cg.s, cg.o);
    }
  } catch (_) {}
  try {
    const tab = document.getElementById("browserTab");
    if (tab) {
      // The tab keeps flying out right and fades while .off kills its clicks.
      tab.style.transform = "translateX(130%)";
      tab.style.opacity = "0";
      tab.classList.add("off");
    }
  } catch (_) {}
  liveGuardSoon();
}

function livePullCancel(pull) {
  // Short pull: sucked back into the tab (fast mirror of the dock suck).
  // The player node is never removed (same iframe stays alive).
  ++liveSeq; // this cancel owns the outcome; a newer pull/open overrules it
  const mySeq = liveSeq;
  const mini = document.getElementById("liveMini");
  const hide = () => {
    if (mySeq !== liveSeq) return; // superseded — leave the newer state alone
    try {
      const m = document.getElementById("liveMini");
      if (m) {
        const anims = m.getAnimations ? m.getAnimations() : [];
        anims.forEach((a) => { try { a.cancel(); } catch (_) {} });
        m.classList.remove("live-pulling", "docking", "live-anim");
        m.style.transformOrigin = "";
        m.style.transform = "";
        m.style.opacity = "";
        m.classList.add("mini-hidden");
      }
    } catch (_) {}
    try {
      const tab = document.getElementById("browserTab");
      if (tab) {
        // Glide back IN from the right in the same task as .off removal.
        tab.style.transform = "";
        tab.style.opacity = "";
        tab.classList.remove("off");
      }
    } catch (_) {}
    liveGuardSoon(); // watchdog: the rail tab must be back and alive
  };
  try {
    if (!mini) { hide(); return; }
    if (!liveReducedMotion() && typeof mini.animate === "function") {
      const fromT = mini.style.transform && mini.style.transform !== "none"
        ? mini.style.transform : "scale(1)";
      const fromO = Number(mini.style.opacity);
      const d = liveTabDelta(mini);
      mini.classList.remove("live-pulling");
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

function initTabDrag(tab) {
  // Right-rail gestures: tap opens the player, vertical drag slides the tab,
  // horizontal pull-left draws the player out from behind the edge (release
  // past ~48px grabs it open, shorter sucks back). Hover never moves the tab
  // (CSS hover only tints). Presses starting on the × button are left alone
  // so closing keeps working. Touch gets a wider tap threshold (jitter).
  let sy = 0;
  let oy = 0;
  let moved = false;
  let dragging = false;
  let tapLimit = 6;
  // The tab owns its gestures (no native scroll starting on it): a finger
  // always jitters, so touch gets a wider tap threshold than the mouse.
  // Otherwise every phone tap reads as a drag and the player never opens.
  tab.style.touchAction = "none";
  tab.addEventListener("pointerdown", (e) => {
    if (e.target && e.target.closest && e.target.closest("button")) return;
    const r = tab.getBoundingClientRect();
    dragging = true;
    moved = false;
    livePull = null;
    tapLimit = e.pointerType === "touch" ? 14 : 6;
    livePullSX = e.clientX;
    sy = e.clientY;
    oy = r.top;
    tab.classList.add("dragging");
    try {
      tab.setPointerCapture?.(e.pointerId);
    } catch (_) {}
    e.preventDefault();
  });
  tab.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    const dx = e.clientX - livePullSX;
    const dy = e.clientY - sy;
    // Pull threshold follows the pointer: touch jitters, so it needs the full
    // run-up; a mouse engages almost immediately (a tap never travels 12px
    // purely horizontal). Past 60px left always wins.
    const need = e.pointerType === "touch" ? 24 : 12;
    if (!livePull && dx < -need && (Math.abs(dx) > Math.abs(dy) * 1.15 + 8 || dx < -60)) {
      // Pull-out takes over: freeze vertical sliding, birth the player at
      // the tab and drag it out under the finger.
      livePull = { sx: livePullSX, sy };
      tab.classList.add("pulling");
      livePullPreview(e.clientX, e.clientY);
    }
    if (livePull) {
      // Cache coords, paint once per frame (never style-thrash per event).
      livePull.lx = e.clientX;
      livePull.ly = e.clientY;
      livePullSchedule();
    } else {
      if (Math.abs(dy) > tapLimit || Math.abs(dx) > tapLimit) moved = true;
      if (moved) placeTab(0, oy + e.clientY - sy);
    }
    e.preventDefault();
  });
  const end = (e) => {
    if (!dragging) return;
    dragging = false;
    tab.classList.remove("dragging");
    const isUp = !e || e.type === "pointerup";
    if (livePull) {
      // Cancel never opens (interrupted gesture just sucks back).
      const pull = livePull;
      livePull = null;
      tab.classList.remove("pulling");
      const far = isUp && e && typeof e.clientX === "number" ? pull.sx - e.clientX : 0;
      const px = isUp && e && typeof e.clientX === "number" ? e.clientX : pull.sx;
      const py = isUp && e && typeof e.clientY === "number" ? e.clientY : pull.sy;
      if (far > 48) livePullCommit(px, py, far);
      else livePullCancel(pull);
      return;
    }
    if (isUp && !moved) {
      const px = e && typeof e.clientX === "number" ? e.clientX : undefined;
      const py = e && typeof e.clientY === "number" ? e.clientY : undefined;
      openLivePlayer(px, py);
    }
  };
  tab.addEventListener("pointerup", end);
  tab.addEventListener("pointercancel", end);
  tab.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      openLivePlayer();
    }
  });
}

function makeDraggable(el, handle) {
  // Free smooth drag anywhere (mouse + touch via Pointer Events).
  // translate-only motion. Presses starting on buttons are left alone
  // so clicks keep working.
  if (!el || !handle) return;
  let sx = 0;
  let sy = 0;
  let ox = 0;
  let oy = 0;
  let dragging = false;
  let moved = false;
  let samples = []; // {x, t} of the last moves — fling detection for liveMini
  handle.style.touchAction = "none";
  handle.style.cursor = "grab";
  handle.addEventListener("pointerdown", (e) => {
    if (e.target && e.target.closest && e.target.closest("button")) return;
    if (el.classList.contains("docked")) return;
    dragging = true;
    moved = false;
    samples = [{ x: e.clientX, t: performance.now() }];
    sx = e.clientX;
    sy = e.clientY;
    const r = el.getBoundingClientRect();
    ox = r.left;
    oy = r.top;
    try {
      handle.setPointerCapture?.(e.pointerId);
    } catch (_) {}
    handle.style.cursor = "grabbing";
    e.preventDefault();
  });
  handle.addEventListener("pointermove", (e) => {
    if (!dragging) return;
    if (Math.abs(e.clientX - sx) + Math.abs(e.clientY - sy) > 5) moved = true;
    // Clamp against the measured size, but allow pushing past the right edge
    // up to half out — only a drop there docks (see maybeDockPlayer).
    const elW = el.offsetWidth || 60;
    const elH = el.offsetHeight || 40;
    const nx = Math.min(Math.max(0, ox + e.clientX - sx), Math.max(0, window.innerWidth - elW / 2));
    const ny = Math.min(Math.max(0, oy + e.clientY - sy), Math.max(0, window.innerHeight - elH - 8));
    el.style.left = `${nx}px`;
    el.style.top = `${ny}px`;
    try {
      samples.push({ x: e.clientX, t: performance.now() });
      if (samples.length > 6) samples.shift();
    } catch (_) {}
    e.preventDefault();
  });
  const stop = (e) => {
    if (dragging && el.id === "liveMini") {
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
          dockPlayerToTab(el.getBoundingClientRect().top);
        } else {
          maybeDockPlayer(el, moved);
        }
      } catch (_) {}
    }
    dragging = false;
    handle.style.cursor = "grab";
  };
  handle.addEventListener("pointerup", stop);
  handle.addEventListener("pointercancel", stop);
}

function makeResizable(el) {
  // Drag edges/corners to resize (mouse + touch). Bounded to the viewport.
  if (!el) return;
  const MIN_W = 200;
  const MIN_H = 150;
  el.querySelectorAll(".live-resize").forEach((grip) => {
    const edge = grip.dataset.edge || "corner";
    grip.style.touchAction = "none";
    grip.addEventListener("pointerdown", (e) => {
      if (el.classList.contains("docked")) return;
      e.preventDefault();
      e.stopPropagation();
      const r = el.getBoundingClientRect();
      const sx = e.clientX;
      const sy = e.clientY;
      const ow = r.width;
      const oh = r.height;
      try {
        grip.setPointerCapture?.(e.pointerId);
      } catch (_) {}
      const move = (ev) => {
        const dx = ev.clientX - sx;
        const dy = ev.clientY - sy;
        const maxW = Math.max(MIN_W + 40, window.innerWidth - r.left - 8);
        if (edge === "right" || edge === "corner") {
          el.style.width = `${Math.min(Math.max(MIN_W, ow + dx), maxW)}px`;
        }
        if (edge === "corner-left") {
          // Grow leftward: the right side stays anchored.
          const maxWLeft = Math.max(MIN_W + 40, r.right - 8);
          const newW = Math.min(Math.max(MIN_W, ow - dx), maxWLeft);
          el.style.width = `${newW}px`;
          el.style.left = `${Math.max(0, r.left + (ow - newW))}px`;
        }
        if (edge === "bottom" || edge === "corner" || edge === "corner-left") {
          const frame = el.querySelector(".live-mini-body .live-frame");
          if (frame) {
            const h = Math.min(Math.max(110, oh - 44 + dy), Math.max(150, window.innerHeight - 140));
            frame.style.aspectRatio = "auto";
            frame.style.height = `${h}px`;
          }
        }
        ev.preventDefault();
      };
      const stop = () => {
        grip.removeEventListener("pointermove", move);
        grip.removeEventListener("pointerup", stop);
        grip.removeEventListener("pointercancel", stop);
      };
      grip.addEventListener("pointermove", move);
      grip.addEventListener("pointerup", stop);
      grip.addEventListener("pointercancel", stop);
    });
  });
}

let miniClampBound = false;
function bindMiniClampOnce() {
  // Rotation/resize must never strand the player off-screen (the phone case:
  // a 94vw window suddenly wider than the viewport). Re-clamp inside.
  if (miniClampBound) return;
  miniClampBound = true;
  window.addEventListener("resize", () => {
    try {
      const mini = document.getElementById("liveMini");
      if (!mini || mini.classList.contains("mini-hidden")) return;
      const w = mini.offsetWidth || 264;
      const h = mini.offsetHeight || 160;
      const r = mini.getBoundingClientRect();
      const nx = Math.min(Math.max(0, r.left), Math.max(0, window.innerWidth - w - 8));
      const ny = Math.min(Math.max(0, r.top), Math.max(0, window.innerHeight - h - 8));
      mini.style.left = `${nx}px`;
      mini.style.top = `${ny}px`;
    } catch (_) {}
  });
}

function clearLiveUI(persist) {
  stopLiveWatch();
  try {
    document.getElementById("browserTab")?.remove();
    document.getElementById("liveMini")?.remove();
  } catch (_) {}
  pendingLiveQid = null;
  if (persist === true) {
    // Dismissing the preview forgets it server-side too (no zombie restore).
    fetch("/api/live_state", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ clear: true }),
    }).catch(() => {});
  }
}

async function answerLiveContinue(qid, btn) {
  // Resume a paused live handoff — silent, no chat message is sent.
  if (btn) {
    btn.disabled = true;
    btn.textContent = "…";
  }
  try {
    const res = await postAnswer({ id: qid, decision: "continue" });
    if (!res.ok) {
      // 410 = handoff already expired (cancelled/timed out): drop the
      // button instead of offering a resume that can never land.
      if (res.status === 410) {
        pendingQuestion = null;
        pendingLiveQid = null;
        if (btn) btn.remove();
        try { clearPendingStored(); } catch (_) {}
        return;
      }
      throw new Error("bad status");
    }
    pendingQuestion = null;
    pendingLiveQid = null;
    if (btn) btn.remove();
    refreshEdgeTab();
    try { restoreBrowserTabAfterContinue(); } catch (_) {}
    toast("Resumed");
  } catch (_) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = "Continue";
    }
    toast("Answer failed");
  }
}

async function takeControlFromPreview(btn) {
  // Take-over stops the agent loop first, then mints the CONTROL live URL
  // and opens the big in-chat control card. A spinner runs inside the icon
  // until control lands, so the wait is visible.
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  try {
    try {
      await abortActiveStream();
    } catch (_) {}
    const { res, data } = await postBrowserApi("control");
    const url = data && typeof data.url === "string" ? data.url : "";
    if (!res.ok || !url) {
      const why = String((data && (data.detail || data.error)) || "").slice(0, 160);
      throw new Error(staleServerHint(why) || why || "control failed");
    }
    try {
      sessionStorage.removeItem(BROWSER_CLOSED_KEY);
    } catch (_) {}
    markLiveSeen();
    browserStreamUrl = url;
    // Hide the preview template right away: the player and the edge tab go
    // away, and only the big chat control card stays on screen.
    try {
      document.getElementById("liveMini")?.remove();
      document.getElementById("browserTab")?.classList.add("off");
    } catch (_) {}
    showControlCard(url);
  } catch (err) {
    toast(err instanceof Error && err.message ? err.message : "Control failed");
  } finally {
    if (btn && btn.isConnected) {
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  }
}

function showControlCard(url) {
  // Big centered in-chat card for manual control: status dot + X only
  // (no default title sentence anywhere), X closes it with the leaving animation.
  const clean = String(url || "");
  if (!clean) return;
  hideQuestionCard();
  manualControlOpen = true;
  stopLiveWatch();
  document.getElementById("liveControlManual")?.remove();
  document.getElementById("liveMini")?.remove();
  document.getElementById("browserTab")?.classList.add("off");
  const host = chatInner || document.body;
  const card = document.createElement("div");
  card.className = "live-card live-control-manual";
  card.id = "liveControlManual";
  card.innerHTML = `
    <div class="live-head">
      <button type="button" class="live-mini-x" data-act="dismiss" aria-label="Close">✕</button>
    </div>
    <div class="live-body"></div>`;
  card.querySelector(".live-body").appendChild(liveFrame(clean));
  card.querySelector('[data-act="dismiss"]').addEventListener("click", () => {
    // X only: forget it server-side too, so a later refresh never brings
    // this dismissed card back. (Paused-loop handoffs have no X on purpose.)
    try {
      fetch("/api/live_state", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ clear: true }),
      }).catch(() => {});
    } catch (_) {}
    manualControlOpen = false;
    document.getElementById("browserTab")?.classList.remove("off");
    showEdgeTab(browserStreamUrl || clean);
    card.classList.add("leaving");
    setTimeout(() => card.remove(), 200);
  });
  host.appendChild(card);
  scrollToBottom();
}

async function liveUrlState(url, timeoutMs = 6000) {
  // Three states, because a dead token must never look like a healthy link:
  //   "alive"       - the viewer answered for a valid token
  //   "expired"     - the gate answered Not Connect (410): the token is dead
  //   "unreachable" - nothing answered at all (relay down, wrong host)
  // The gate publishes CORS headers only on that 410, so a READABLE status is
  // itself the dead-token signal; a live token sends no such headers and the
  // same request rejects. The no-cors probe then separates alive from offline.
  // Probes the remote-rewritten URL (a remote page checks this machine's
  // viewer instead of the viewer's own localhost). Never throws.
  const ctl = new AbortController();
  const timer = setTimeout(() => {
    try {
      ctl.abort();
    } catch (_) {}
  }, timeoutMs);
  try {
    const u = remoteLiveUrl(String(url || "")) || String(url || "");
    if (!u.startsWith("http")) return "unreachable";
    try {
      const res = await fetch(u, { mode: "cors", cache: "no-store", signal: ctl.signal });
      return res.status === 410 ? "expired" : "alive";
    } catch (_) {}
    try {
      await fetch(u, { mode: "no-cors", cache: "no-store", signal: ctl.signal });
      return "alive";
    } catch (_) {
      return "unreachable";
    }
  } catch (_) {
    return "unreachable";
  } finally {
    clearTimeout(timer);
  }
}

async function restoreLive() {
  // After a refresh: bring back the stream card / preview bar so the running
  // task keeps all its visible data. Returns true while something is live.
  // A user-confirmed close stays closed across refresh (no resurrection).
  try {
    if (userClosedBrowser()) return false;
    const res = await fetch("/api/live_state", { cache: "no-store" });
    if (!res.ok) return false;
    const data = await res.json();
    const st = data && data.active;
    if (!st || !st.url) return false;
    if (st.mode === "control" && data.pending && st.qid) {
      showLiveControlCard({ id: st.qid, kind: "live", url: st.url, tool: st.tool, reason: st.reason });
      return true;
    }
    if (st.mode === "control" && st.url) {
      // Browser runs with no paused loop: show the right-edge tab (interactive),
      // never a blocking card the user did not ask for. The persisted link is
      // probed first: the library can still report a session whose stream is
      // already dead, and refresh must never resurrect a dead tab. An expired
      // token is not resurrected either - it gets a freshly minted watch-only
      // link (the tab's control button re-takes control on demand).
      const state = await liveUrlState(st.url);
      if (state === "expired") {
        autoMintMissingPreview();
        return false;
      }
      if (state !== "alive") return false;
      browserStreamUrl = String(st.url);
      showEdgeTab(st.url, data.pending ? st.qid : null);
      return true;
    }
    if (st.mode === "preview") {
      // Same dead-link guard as above: no probe, no tab. An expired token gets
      // a freshly minted link, so a console restart needs no user action.
      if (!st.url) return false;
      const state = await liveUrlState(st.url);
      if (state === "expired") {
        autoMintMissingPreview();
        return false;
      }
      if (state !== "alive") return false;
      showEdgeTab(st.url, data.pending ? st.qid : null);
      return true;
    }
    return false;
  } catch (_) {
    return false;
  }
}

let browserDownStreak = 0;
let lastAutoPreviewAt = 0;

async function autoMintMissingPreview() {
  // The browser runs but no stream link is known anywhere (server reboot
  // cleared it, or the open never persisted one): mint the stable cached
  // preview link in the background so the edge tab can appear. Bounded:
  // at most once per 60s, never while a control/handoff UI owns the screen,
  // and never boots anything (the preview action fails clearly while down).
  try {
    if (manualControlOpen || pendingLiveQid) return;
    if (document.getElementById("liveMini") || document.getElementById("browserTab")) return;
    if (document.getElementById("liveControlManual") || document.getElementById("liveCard")) return;
    const now = Date.now();
    if (now - lastAutoPreviewAt < 60000) return;
    lastAutoPreviewAt = now;
    const { res, data } = await postBrowserApi("preview", 30000);
    const url = data && typeof data.url === "string" ? data.url : "";
    if (res.ok && url) {
      clearBrowserClosedFlag();
      markLiveSeen();
      showEdgeTab(url);
    }
  } catch (_) {}
}

async function autoCheckBrowserOnAgentOpen() {
  // Auto-check every time the agent page opens: browser running -> fetch the
  // live link and show the right-edge template automatically (no click needed).
  // Skips when any live UI already owns the screen, a handoff is pending, or
  // the user explicitly closed the browser. Dead links are probed first so a
  // stale session never pops a broken player; a running browser with no link
  // yet falls back to the bounded background mint (at most once per 60s).
  try {
    if (manualControlOpen || pendingLiveQid) return;
    if (document.getElementById("liveMini") || document.getElementById("browserTab")) return;
    if (document.getElementById("liveControlManual") || document.getElementById("liveCard")) return;
    if (userClosedBrowser()) return;
    const res = await fetch("/api/browser", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json().catch(() => null);
    if (!data || !data.running) return;
    const url = data.stream && typeof data.stream.url === "string" ? data.stream.url : "";
    if (url) {
      const state = await liveUrlState(url);
      if (state === "expired") {
        // The stored token is gone (console restart, new live session): mint a
        // fresh link instead of silently leaving the user with nothing.
        autoMintMissingPreview();
        return;
      }
      if (state !== "alive") return;
      clearBrowserClosedFlag();
      markLiveSeen();
      showEdgeTab(url);
    } else {
      autoMintMissingPreview();
    }
  } catch (_) {}
}


function onBrowserPush(data) {
  // Push version of the old poller: reflects reality only — never starts
  // the browser by itself. Down-hysteresis (4 misses) keeps relay flaps
  // from flickering the tab or wiping a just-opened player: explicit close
  // paths clear the UI immediately, so waiting here only affects surprises.
  if (document.hidden) return;
  const page = document.querySelector('.page[data-page="agent"]');
  if (!page || !page.classList.contains("active")) return;
  try {
    const running = Boolean(data && data.running);
    const tab = document.getElementById("browserTab");
    const player = document.getElementById("liveMini");
    if (running) {
      browserDownStreak = 0;
      const url = data.stream && typeof data.stream.url === "string" ? data.stream.url : "";
      if (url) {
        markLiveSeen();
        showEdgeTab(url);
      } else {
        // Browser runs but no link is known: mint the missing preview in the
        // background so the edge tab appears (bounded inside).
        autoMintMissingPreview();
      }
    } else if (tab || player) {
      browserDownStreak += 1;
      if (browserDownStreak >= 4) {
        // Browser reads down: hide the tab/player LOCALLY only. The server
        // keeps the persisted stream (a flap must never delete the recovery
        // link), so refresh restores it; explicit close paths clear it.
        clearLiveUI();
      }
    } else {
      browserDownStreak = 0;
    }
  } catch (_) {
  }
}

function isChatIdle() {
  return !(activeRun || activeThinking
    || document.getElementById("thinkingMsg")
    || (chatInner && chatInner.querySelector(".typing-cursor")));
}

function onLivePush(data) {
  // Push version of the old live poller: when the server stream clears and
  // the chat is idle, reload history once (the loop finished).
  // Never wipes a running chat: the just-sent message is not persisted yet.
  try {
    if (data && typeof data.url === "string" && data.url) {
      showLiveLink(data);
      return;
    }
    if (isChatIdle()) {
      clearLiveUI();
      loadHistory().catch(() => {});
    }
  } catch (_) {}
}

async function onChatDonePush(data) {
  // A loop finished somewhere (this tab or another): if idle, the reply is
  // persisted now — reload once. Replaces the old catchup poller.
  try {
    if (data && data.gateway) {
      activeThinking = null;
      catchupResume = false;
      // Remember replies already on screen so only the fresh gateway reply
      // gets the real SSE-style typing animation below.
      let known = null;
      try {
        known = new Set();
        chatInner?.querySelectorAll(".msg.assistant").forEach((m) => {
          if (m.dataset.mid) known.add(m.dataset.mid);
        });
      } catch (_) { known = null; }
      try {
        document.getElementById("thinkingMsg")?.remove();
        chatInner?.querySelectorAll(".msg.gateway-pending").forEach((item) => item.remove());
      } catch (_) {}
      try { await loadHistory(); } catch (_) {}
      // Gateway reply: same typing animation as a local run (typeRich on the
      // reply text only — steps/images/voice stay as rendered). Best-effort.
      try {
        const assistants = chatInner ? Array.from(chatInner.querySelectorAll(".msg.assistant")) : [];
        let target = null;
        for (let i = assistants.length - 1; i >= 0; i--) {
          const m = assistants[i];
          if (known && m.dataset.mid && known.has(m.dataset.mid)) continue;
          if (!String(m.dataset.raw || "").trim()) continue;
          target = m;
          break;
        }
        if (target) {
          const raw = String(target.dataset.raw || "").slice(0, MAX_MESSAGE);
          const content = target.querySelector(":scope > .msg-content");
          const rich = content
            ? content.querySelector(":scope > div:not(.thinking-block):not(.msg-final-shots):not(.msg-voice)")
            : null;
          if (raw && rich) await typeRich(rich, raw);
          scrollToBottom();
        }
      } catch (_) {}
      return;
    }
    // A resumed pre-refresh run just landed: settle it FIRST (before the
    // idle gate — the catch-up Working itself makes the chat non-idle, so
    // the old order never reached this branch and the reply never rendered).
    if (catchupResume) {
      // Drop the mid-less resume bubble + thinking so history renders a
      // single copy of each. Never a Stopped freeze here: chat_done means
      // the run finished and persisted (STOP travels as a chat push).
      catchupResume = false;
      activeThinking = null;
      setSending(false);
      try { clearPendingUser(); } catch (_) {}
      try { resumePending = false; resumeSettled = false; } catch (_) {}
      try {
        document.getElementById("thinkingMsg")?.remove();
        if (lastUserEl && !lastUserEl.dataset.mid) lastUserEl.remove();
        lastUserEl = null;
      } catch (_) {}
      catchupPartsEl = null;
      catchupPartsRaw = "";
      loadHistory().catch(() => {});
      return;
    }
    if (!isChatIdle()) return;
    loadHistory().catch(() => {});
  } catch (_) {}
}

function onChatPush(data) {
  // A resumed run died (e.g. server restarted): stop spinning, no polling.
  try {
    if (data && data.gateway && data.active) {
      if (!document.getElementById("thinkingMsg")) {
        const preview = String(data.input || "").slice(0, MAX_MESSAGE);
        hideWelcome();
        if (preview) {
          const pending = appendUserMessage(preview, []);
          pending.classList.add("gateway-pending");
        }
        activeThinking = addThinking(preview);
        // Follow this channel-driven run live: same Working block + steps
        // as a local run, settled by the gateway chat_done frame.
        catchupLiveItems = new Map();
        catchupPartsEl = null;
        catchupPartsRaw = "";
        catchupResume = true;
      }
      return;
    }
    if (data && !data.active && !activeRun) {
      // A followed pre-refresh run is settled by chat_done (history reload),
      // never here — freezing it as Stopped would lie about a finished task
      // and hide its reply until the next manual refresh.
      if (catchupResume) return;
      // STOP / died run: freeze Working in place. Never delete it — that
      // was the wipe-then-Continue-from-scratch bug.
      // Idle too: no live question can survive — drop stale strips/stores.
      try { hideQuestionCard(); } catch (_) {}
      try {
        document.getElementById("liveCard")?.remove();
        manualControlOpen = false;
        pendingLiveQid = null;
      } catch (_) {}
      try { clearPendingStored(); } catch (_) {}
      const thinkingEl = document.getElementById("thinkingMsg");
      if (thinkingEl && thinkingEl.dataset.stopped !== "1") {
        try { settleThinkingStopped(thinkingEl); } catch (_) {}
        try {
          // A newer unsent request supersedes the stopped task: never re-arm
          // Continue over its pending text.
          if (!readPendingUser()) {
            const _mid = (typeof lastUserEl !== "undefined" && lastUserEl && lastUserEl.dataset && lastUserEl.dataset.mid) || "";
            setStoppedTask(_mid || readStoppedUserId() || "1");
          }
        } catch (_) {}
      }
      activeThinking = null;
      setSending(false);
      catchupResume = false;
      try { clearPendingUser(); } catch (_) {}
      try { resumePending = false; resumeSettled = false; } catch (_) {}
    }
  } catch (_) {}
}

let catchupResume = false;

// A tab that refreshed mid-run owns no stream: rebuild the running task's
// Working block from the server's buffered progress frames, then keep it live
// as new "run" pushes arrive. Rendering is idempotent (keyed step items), so a
// frame seen twice (replay + push) never duplicates a step.
let catchupLiveItems = new Map();
// Long-reply parts replayed by a refreshed tab: the visible reply grows under
// the catch-up Working block until chat_done swaps in the persisted history.
let catchupPartsEl = null;
let catchupPartsRaw = "";
// During bulk replay skips per-frame scrolling (that was the refresh hang:
// 300 frames × forced layout). One scroll lands at the end instead.
let replayQuiet = false;

function renderRunFrame(frame) {
  try {
    if (!frame || typeof frame !== "object") return;
    const block = activeThinking && activeThinking._block;
    const type = frame.type;
    if (type === "event_start") {
      if (!block) return;
      const key = stepKey({ key: frame.key, event: frame.event });
      if (catchupLiveItems.has(key)) return;
      const item = createStepItem({ event: frame.event, reason: frame.reason || "", output: null, media: null, shot: "" });
      markStepRunning(item);
      block._stepsInner.appendChild(item);
      ensureHeaderArrow(block);
      catchupLiveItems.set(key, item);
      if (!replayQuiet) scrollToBottom();
      return;
    }
    if (type === "event") {
      if (!block) return;
      const key = stepKey({ key: frame.key, event: frame.event });
      let item = catchupLiveItems.get(key);
      if (!item) {
        item = createStepItem({ event: frame.event, reason: frame.reason || "", output: null, media: frame.media || null, shot: "" });
        block._stepsInner.appendChild(item);
        ensureHeaderArrow(block);
        catchupLiveItems.set(key, item);
      } else {
        try {
          const titleEl = item._head && item._head.querySelector(":scope > .step-title");
          const finalTitle = humanizeEvent(frame.event || "");
          if (titleEl && finalTitle && titleEl.textContent !== finalTitle) {
            titleEl.textContent = finalTitle;
          }
        } catch (_) {}
        completeStepItem(item, {
          output: typeof frame.output === "string" ? frame.output : "",
          shot: typeof frame.shot === "string" ? frame.shot : "",
          failed: !!frame && frame.failed === true,
        });
      }
      try {
        const st = frame.media && frame.media.stream;
        if (st && typeof st.url === "string" && st.url) {
          if (st.tool === "show_preview") showPreviewPlayer(toPreviewUrl(st.url));
          else if (st.view_only !== false) {
            markLiveSeen();
            showEdgeTab(st.url);
          }
        }
      } catch (_) {}
      if (!replayQuiet) scrollToBottom();
      return;
    }
    if (type === "question") {
      if (frame.kind === "live") {
        markLiveSeen();
        if (frame.view_only) showEdgeTab(frame.url, frame.id);
        else showLiveControlCard(frame);
      } else showQuestionCardUnlessStopped(frame);
      return;
    }
    if (type === "question-expired") {
      // Replay or push: a stale strip for this id must never survive.
      try {
        if (pendingQuestion && String(pendingQuestion.id) === String(frame.id)) {
          hideQuestionCard();
          try { pendingModelConnect = null; } catch (_) {}
          toast("Question expired — continued without answer");
        }
      } catch (_) {}
      try {
        const saved = readPendingStored();
        if (saved && String(saved.id) === String(frame.id)) clearPendingStored();
      } catch (_) {}
      return;
    }
    if (type === "live") {
      showLiveLink(frame.live);
      return;
    }
    if (type === "live_end") clearLiveUI();
    if (type === "reply_part") {
      const text = String(frame.text || "");
      const host = activeThinking;
      if (!text || !host) return;
      if (!catchupPartsEl || !catchupPartsEl.isConnected) {
        catchupPartsEl = document.createElement("div");
        catchupPartsEl.className = "msg-live-parts";
        catchupPartsEl.setAttribute("dir", "auto");
        const content = host.querySelector(":scope > .msg-content");
        try { (content || host).appendChild(catchupPartsEl); } catch (_) {}
      }
      if (catchupPartsRaw && !/[\s]$/.test(catchupPartsRaw) && !/^\s/.test(text)) catchupPartsRaw += "\n\n";
      catchupPartsRaw += text;
      try {
        catchupPartsEl.innerHTML = formatRich(catchupPartsRaw);
        try { linkifyFolderPaths(catchupPartsEl); } catch (_) {}
      } catch (_) {}
      if (!replayQuiet) scrollToBottom();
      return;
    }
  } catch (_) {}
}

function replayRunFrames(frames) {
  catchupLiveItems = new Map();
  catchupPartsEl = null;
  catchupPartsRaw = "";
  if (!Array.isArray(frames) || !frames.length) return;
  // Cap: the server buffers 300, but rendering hundreds of step cards in one
  // go hangs the tab — the last 150 carry the live state.
  const list = frames.length > 150 ? frames.slice(-150) : frames;
  replayQuiet = true;
  try {
    for (const frame of list) renderRunFrame(frame);
  } finally {
    replayQuiet = false;
  }
  try { scrollToBottom(false); } catch (_) {}
}

function onRunPush(frame) {
  // Only a re-attached viewer renders pushed frames: the tab that owns the
  // POST stream already renders them from its own response body.
  if (!catchupResume) return;
  renderRunFrame(frame);
}

function lastStoppedAssistant() {
  // The on-screen stopped Working bubble (live or history-rendered), or null.
  try {
    const live = document.getElementById("thinkingMsg");
    if (live && live.dataset && live.dataset.stopped === "1") return live;
    const all = chatInner ? Array.from(chatInner.querySelectorAll(".msg.assistant")) : [];
    for (let i = all.length - 1; i >= 0; i--) {
      const m = all[i];
      try {
        if ((m.dataset && m.dataset.stopped === "1") || m.querySelector(":scope .stopped-line")) return m;
      } catch (_) {}
    }
  } catch (_) {}
  return null;
}

async function resumeActiveRun() {
  // Refresh while the agent runs: re-show the pending request as running and
  // pick up its reply when it lands. A STOP already marked cancelled reports
  // active=false — never rebuild a live Working block (that was the duplicate
  // steps-after-refresh bug). A Continue mid-run adopts the frozen block only
  // when the server resume_id matches its task; any other run gets a fresh one.
  // Idempotent + single-live protected: a second call (double sync / double
  // refresh) never appends a second bubble or second Working.
  if (resumeSettled || activeRun || (catchupResume && activeThinking)) {
    try { resumePending = false; } catch (_) {}
    return;
  }
  let fd = null;
  // Bounded retry: the POST may still be registering (or the agent section may
  // have just rendered). Up to ~3s so an instant refresh after send still
  // finds its run instead of showing the task with no Working.
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      fd = await fetch("/api/chat_frames", { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null);
    } catch (_) {
      fd = null;
    }
    if (fd && fd.active) break;
    // Keep polling only while WE have an unsent-looking pending message; a
    // truly idle server (no run, nothing pending) stops at once.
    try {
      if (!readPendingUser()) break;
    } catch (_) { break; }
    if (attempt < 5) {
      try { await new Promise((r) => setTimeout(r, 500)); } catch (_) { break; }
    }
  }
  if (!fd || !fd.active) {
    // No live run: an unsent pending request (e.g. sent during the 409
    // still-stopping window, then refreshed before it registered) must come
    // back as an editable draft — never vanish, never auto-run the old task.
    try {
      const draft = readPendingUser();
      if (draft && composerInput && !(composerInput.value || "").trim()) {
        composerInput.value = draft;
        try { autoResize(); } catch (_) {}
        try { updateSendEnabled(); } catch (_) {}
      }
    } catch (_) {}
    try { resumePending = false; resumeSettled = false; } catch (_) {}
    return;
  }
  // A live run already rebuilt while we fetched (section + second resume raced):
  // never build a second Working.
  if (activeRun || (catchupResume && activeThinking) || document.getElementById("thinkingMsg")) {
    try { resumePending = false; resumeSettled = true; } catch (_) {}
    return;
  }
  hideWelcome();
  // Full text first: server input_full (new) > stored pending (same tab,
  // pre-refresh) > 200-char preview (never render the preview alone as a
  // truncated bubble when a fuller copy exists).
  const serverFull = String((fd && (fd.input_full || fd.input)) || "").slice(0, 4000);
  const preview = serverFull.slice(0, 200);
  // Server truth wins: input_full is the task actually running. The stored
  // pending text only fills the gap when the server has nothing yet
  // (registration race) — longer-wins used to resurrect the old task here.
  const storedFull = readPendingUser();
  const fullText = serverFull || storedFull;
  if (fullText) {
    // No duplicate resume bubble: reuse the tail user bubble when it already
    // shows this pending input (double refresh of the same run). Strict match
    // on the FULL text — the old prefix match on the truncated preview is
    // what cloned every long message into a second bubble.
    const cleaned = cleanHistoryText(fullText);
    const want = String(cleaned.text || fullText);
    let tail = null;
    try {
      const users = chatInner.querySelectorAll(".msg.user");
      tail = users.length ? users[users.length - 1] : null;
    } catch (_) {
      tail = null;
    }
    const tailRaw = String((tail && tail.dataset.raw) || "");
    // Reuse the tail bubble ONLY when it is the last message on screen.
    // Otherwise an equal-text older bubble (e.g. task A when resuming the
    // same-text task B) would be stolen and its id overwritten on done.
    let tailIsLast = false;
    try {
      const allMsgs = chatInner ? chatInner.querySelectorAll(":scope > .msg") : [];
      tailIsLast = !!(tail && allMsgs.length && allMsgs[allMsgs.length - 1] === tail);
    } catch (_) { tailIsLast = false; }
    if (tail && want && tailRaw === want && tailIsLast) {
      lastUserEl = tail;
    } else if (!want) {
      if (tail) lastUserEl = tail;
    } else {
      lastUserEl = appendUserMessage(cleaned.text || fullText, []);
      attachUserBar(lastUserEl);
    }
    if (cleaned.text) lastUserText = cleaned.text;
  }
  // Adopt the frozen Working ONLY for the same task: this run must be a
  // Continue resume (server resume_id) AND the user bubble right before the
  // frozen block must carry that id. A brand-new task sent after a STOP gets
  // a FRESH Working — adopting the old task's block was the refresh bug
  // (last message with no Working, previous message's Working running).
  let thinking = null;
  try {
    const rid = fd && typeof fd.resume_id === "string" ? fd.resume_id.slice(0, 16) : "";
    const frozen = lastStoppedAssistant();
    if (frozen && rid) {
      let owner = "";
      try {
        let p = frozen.previousElementSibling;
        while (p && !(p.classList && p.classList.contains("msg"))) p = p.previousElementSibling;
        if (p && p.classList.contains("user")) owner = String((p.dataset && p.dataset.mid) || "").slice(0, 16);
      } catch (_) { owner = ""; }
      if (owner && owner === rid) {
        if (!frozen._block) {
          try { frozen._block = frozen.querySelector(".thinking-block") || null; } catch (_) { frozen._block = null; }
        }
        if (frozen._block && frozen._block._stepsInner) {
          thinking = frozen;
          try {
            frozen.id = "thinkingMsg";
            try { frozen.querySelectorAll(".stopped-line").forEach((n) => { try { n.remove(); } catch (_) {} }); } catch (_) {}
            try {
              const nx = frozen.nextElementSibling;
              if (nx && nx.classList && nx.classList.contains("stopped-line")) nx.remove();
            } catch (_) {}
            try { delete frozen.dataset.stopped; } catch (_) {}
            resumeThinkingBlock(frozen._block);
          } catch (_) {}
        }
      }
    }
  } catch (_) {
    thinking = null;
  }
  if (!thinking) thinking = addThinking(preview);
  activeThinking = thinking;
  // Single-live protection: any older open block is settled + collapsed, so
  // only THIS resumed Working animates (the "first Working still running"
  // confusion). History settled blocks stay collapsed below.
  try { enforceSingleLiveBlock(thinking); } catch (_) {}
  // The button shows STOP while the resumed run lives — even though this
  // page owns no stream for it. Completion (or death) arrives via push
  // (chat_done / chat events) — no polling.
  setSending(true);
  catchupResume = true;
  try {
    if (Array.isArray(fd.frames)) replayRunFrames(fd.frames);
  } catch (_) {}
  try { resumePending = false; resumeSettled = true; } catch (_) {}
}

async function sendQuestionAnswer() {
  // The answer resumes the paused run — it is never posted as a user
  // message. It lands inside the question step's arrow (Q + A, from the
  // loop's own completion event) and the loop continues from there.
  // Nothing auto-sends: chip choice or typed text, both go through SEND.
  const q = pendingQuestion;
  if (!q) return;
  const typed = composerInput.value.trim().slice(0, 2000);
  const chosen = typeof q.selected === "string" && q.selected.trim() ? q.selected.trim().slice(0, 2000) : "";
  const answer = typed || chosen;
  if (!answer) {
    toast("Select a choice or type a reply");
    return;
  }
  hideQuestionCard();
  composerInput.value = "";
  autoResize();
  sendBtn.disabled = true;
  composerInput.disabled = true;
  try {
    const res = await postAnswer({ id: q.id, answer });
    if (!res.ok && (!res || res.status !== 410)) toast("Answer failed");
    else clearPendingStored();
    // The loop resumes on the still-open stream; its completion event
    // carries the Q + A into the step arrow, then work continues.
    // (410 = question already expired: strip is already hidden, nothing left.)
  } catch (_) {
    toast("Answer failed");
  } finally {
    sendBtn.disabled = false;
    composerInput.disabled = false;
    composerInput.focus();
  }
}

/* ---------- message actions: copy / share / edit / regenerate / delete ---------- */
const ICONS = {
  copy: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" /><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" /></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" /></svg>',
  refresh: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10" /><path d="M3.5 15a9 9 0 1 0 2.1-9.4L1 10" /></svg>',
  trash: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>',
};

async function postMessageApi(body) {
  const res = await fetch("/api/message", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data?.error || `HTTP ${res.status}`);
  return data;
}

async function pruneBelowServer(anchorMid) {
  // One atomic server prune: everything below the anchor is deleted in a
  // single read-modify-write (+embeddings) and the server confirms the
  // removed count. The caller waits for this — nothing below can leak into
  // the resent context. Throws when the anchor is gone. Never half-done.
  const res = await fetch("/api/message", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "prune", id: anchorMid }),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error((data && data.error) || ("HTTP " + res.status));
  return data;
}

/* ---------- browser API with bounded wait (never hangs the button) ---------- */
const BROWSER_CLOSED_KEY = "x5-browser-closed";

function userClosedBrowser() {
  try {
    return sessionStorage.getItem(BROWSER_CLOSED_KEY) === "1";
  } catch (_) {
    return false;
  }
}

const LIVE_SEEN_KEY = "x5-live-seen";

function markLiveSeen() {
  // The browser was legitimately started (agent or explicit user action).
  try {
    sessionStorage.setItem(LIVE_SEEN_KEY, "1");
  } catch (_) {}
}

function liveSeen() {
  try {
    return sessionStorage.getItem(LIVE_SEEN_KEY) === "1";
  } catch (_) {
    return false;
  }
}

function clearBrowserClosedFlag() {
  try {
    sessionStorage.removeItem(BROWSER_CLOSED_KEY);
  } catch (_) {}
}

async function postBrowserApi(action, timeoutMs = 45000) {
  const c = new AbortController();
  const t = setTimeout(() => {
    try {
      c.abort();
    } catch (_) {}
  }, timeoutMs);
  try {
    const res = await fetch("/api/browser", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
      signal: c.signal,
    });
    const data = await res.json().catch(() => ({}));
    return { res, data };
  } catch (err) {
    if (err && (err.name === "AbortError" || err.aborted)) {
      throw new Error("request timed out");
    }
    throw err;
  } finally {
    clearTimeout(t);
  }
}

function staleServerHint(text) {
  // Old server builds reject the control action with the legacy message.
  return /preview\|close/.test(String(text || ""))
    ? "Server is old — restart it to enable control"
    : "";
}

function makeActBtn(icon, label, fn, danger) {
  const b = document.createElement("button");
  b.type = "button";
  b.className = "msg-act" + (danger ? " danger" : "");
  b.title = label;
  b.setAttribute("aria-label", label);
  b.innerHTML = ICONS[icon] || "";
  b.addEventListener("click", (e) => {
    e.stopPropagation();
    fn();
  });
  return b;
}

function appendModelConnectButton(shell, kind) {
  // The error stays a real error; this only adds the fix next to it.
  try {
    const wrap = document.createElement("div");
    wrap.className = "needs-model-wrap";
    const note = document.createElement("p");
    note.className = "needs-model-note";
    note.textContent = "Connect a model, then send your message again.";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "needs-model-btn glass";
    btn.textContent = "Connect a model";
    btn.addEventListener("click", () => openModelPage(kind || "text"));
    wrap.append(note, btn);
    (shell || document.createElement("div")).appendChild(wrap);
  } catch (_) {}
}

function openModelPage(kind) {
  // Glass-button path: go to AI Models and auto-open that template dialog.
  const k = String(kind || "text").toLowerCase();
  try { navigate("ai-models"); } catch (_) {}
  try {
    setTimeout(() => {
      try {
        if (typeof window.openAiModelDialog === "function") window.openAiModelDialog(k);
        else window.dispatchEvent(new CustomEvent("x5:open-model", { detail: { kind: k } }));
      } catch (_) {}
    }, 120);
  } catch (_) {}
}


function attachAssistantBar(shell, raw) {
  const old = shell.querySelector(":scope > .msg-actions");
  if (old) old.remove();
  const bar = document.createElement("div");
  bar.className = "msg-actions below";
  [
    ["copy", "Copy", () => copyText(raw)],
    ["edit", "Edit", () => editAssistantMessage(shell, raw)],
    ["refresh", "Regenerate", () => regenerateReply(shell)],
    ["trash", "Delete", () => confirmDeleteMessage(shell)],
  ].forEach(([icon, label, fn], i) => {
    const b = makeActBtn(icon, label, fn, icon === "trash");
    b.style.animationDelay = `${i * 60}ms`;
    bar.appendChild(b);
  });
  shell.appendChild(bar);
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(String(text || ""));
    toast("Copied");
  } catch (_) {
    toast("Copy failed");
  }
}

function confirmDeleteMessage(shell) {
  const isUser = shell.classList.contains("user");
  openModal(`
    <div class="modal-head">
      <div class="tile"><span class="tile-letters">!</span></div>
      <div class="modal-head-text"><h3>Delete message?</h3><p>Removes it from context permanently</p></div>
      <button class="modal-close" data-close type="button" aria-label="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.6" stroke-linecap="round">
          <line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" />
        </svg>
      </button>
    </div>
    <div class="modal-body"><p class="modal-body-text">${
      isUser
        ? "This user message will be deleted from history, so the agent will no longer see it in context."
        : "This assistant reply will be deleted from history."
    }</p></div>
    <div class="modal-foot">
      <button class="btn" data-close type="button">Cancel</button>
      <button class="btn btn-danger" id="confirmDelBtn" type="button">Delete</button>
    </div>`);
  document.getElementById("confirmDelBtn")?.addEventListener("click", async () => {
    closeModal();
    await deleteMessage(shell);
  });
}

/* ---------- edit in composer: message -> composer box -> SEND saves it ---------- */
let editingTarget = null;

function ensureEditBanner() {
  let banner = document.getElementById("editBanner");
  const composer = document.getElementById("composer");
  if (banner) {
    // The strip lives INSIDE the composer (top row of the send template).
    if (composer && banner.parentNode !== composer) composer.prepend(banner);
    return banner;
  }
  if (!composer) return null;
  banner = document.createElement("div");
  banner.id = "editBanner";
  banner.className = "edit-banner";
  banner.hidden = true;
  banner.innerHTML = `<span class="edit-strip-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5L17 3z" /></svg></span><span class="edit-banner-text">Editing message — SEND saves</span><button type="button" class="edit-strip-x" id="editCancelBtn" aria-label="Cancel editing">✕</button>`;
  composer.prepend(banner);
  banner.querySelector("#editCancelBtn")?.addEventListener("click", cancelComposerEdit);
  return banner;
}

function showEditBanner() {
  const banner = ensureEditBanner();
  if (!banner) return;
  banner.classList.remove("closing");
  banner.hidden = false;
}

function hideEditBanner() {
  const banner = document.getElementById("editBanner");
  if (!banner || banner.hidden) return;
  // Exit animation first, then detach.
  banner.classList.add("closing");
  setTimeout(() => {
    banner.hidden = true;
    banner.classList.remove("closing");
  }, 180);
}

function updateEditSendState() {
  // SEND stays enabled in edit mode (same text = regenerate the reply).
  if (!editingTarget || !sendBtn || !composerInput) return;
  if (sendBtn.classList.contains("stop")) return;
  const cur = String(composerInput.value || "").trim().slice(0, MAX_MESSAGE);
  sendBtn.disabled = !(cur.length > 0);
}

async function pruneBelow(el) {
  // Deletes EVERY bubble below el (user + assistant, server + DOM +
  // embeddings) so an edit/regenerate restarts the thread from this point.
  // Each bubble fades/slides out with animation (staggered), then is removed.
  // Returns the removed count. Never throws.
  let removed = 0;
  try {
    await abortActiveStream();
    const victims = [];
    let node = el ? el.nextElementSibling : null;
    while (node) {
      victims.push(node);
      node = node.nextElementSibling;
    }
    victims.forEach((n, i) => {
      try {
        n.style.transition = "opacity 220ms ease, transform 220ms ease";
        n.style.transitionDelay = `${Math.min(i * 40, 400)}ms`;
        n.style.opacity = "0";
        n.style.transform = "translateY(10px)";
      } catch (_) {}
    });
    const waitMs = Math.min(victims.length * 40, 400) + 240;
    try {
      await new Promise((r) => setTimeout(r, waitMs));
    } catch (_) {}
    for (const n of victims) {
      try {
        const rid = n.dataset ? n.dataset.mid : null;
        if (rid) {
          try {
            await postMessageApi({ action: "delete", id: rid });
          } catch (_) {}
        }
        n.remove();
        removed += 1;
      } catch (_) {}
    }
  } catch (_) {}
  return removed;
}

function startComposerEdit(el) {
  const raw = el.dataset.raw || "";
  if (!raw) {
    toast("Nothing to edit");
    return;
  }
  abortActiveStream();
  hideMentionMenu();
  clearAttachment();
  editingTarget = { el, mid: el.dataset.mid || null, original: raw };
  composerInput.value = raw;
  autoResize();
  showEditBanner();
  // Edit mode is a send intent, never Continue: drop the pill (if armed)
  // back to the normal send arrow.
  try { updateSendEnabled(); } catch (_) {}
  updateEditSendState();
  composerInput.focus();
}

function cancelComposerEdit() {
  editingTarget = null;
  composerInput.value = "";
  autoResize();
  hideEditBanner();
  if (sendBtn) sendBtn.disabled = false;
  // Leaving edit mode re-evaluates the button (a stopped task re-arms
  // Continue on the empty composer, otherwise normal send).
  try { updateSendEnabled(); } catch (_) {}
  composerInput.focus();
}

function resolveLiveMsg(mid) {
  // History reloads rebuild every bubble: a node captured earlier (edit
  // target, action-bar shell) may be detached. Resolve the LIVE node by id
  // so prune/rerender/delete hit the real thread. Never throws.
  try {
    const key = String(mid || "").replace(/[^0-9a-zA-Z-]/g, "");
    if (!key || !chatInner) return null;
    return chatInner.querySelector('.msg[data-mid="' + key + '"]');
  } catch (_) {
    return null;
  }
}

async function saveComposerEdit(value) {
  const target = editingTarget;
  if (!target) return false;
  const v = String(value || "").trim().slice(0, MAX_MESSAGE);
  if (!v) {
    toast("Message is empty");
    return true;
  }
  // SEND loading on the composer button until prune + regenerate is ready.
  if (sendBtn) {
    try {
      sendBtn.disabled = true;
      sendBtn.classList.add("loading");
    } catch (_) {}
  }
  try {
    let { el, mid } = target;
    if (mid) {
      // The bubble may have left the screen (history re-render) after edit
      // opened: acting on the detached node would prune nothing and resend
      // with a stale context. Rebind to the live node or abort truthfully.
      const live = resolveLiveMsg(mid);
      if (!live) {
        editingTarget = null;
        composerInput.value = "";
        autoResize();
        hideEditBanner();
        if (sendBtn) sendBtn.disabled = false;
        toast("Message changed on screen — open it and edit again");
        return true;
      }
      el = live;
    }
    const isUserEdit = el.classList.contains("user");
    const orig = String(target.original || "").trim().slice(0, MAX_MESSAGE);
    const changed = v !== orig;
    if (changed && mid) {
      try {
        await postMessageApi({ action: "edit", id: mid, content: v });
      } catch (err) {
        toast(err instanceof Error ? err.message : "Save failed");
        return true;
      }
      // Re-render the SAME bubble in place (old embedding replaced server-side).
      const content = el.querySelector(".msg-content");
      if (content) {
        content.innerHTML = "";
        const div = document.createElement("div");
        div.innerHTML = formatRich(v);
        try {
          if (el.classList.contains("assistant")) linkifyFolderPaths(div);
        } catch (_) {}
        content.appendChild(div);
      }
      el.dataset.raw = v;
      if (el.classList.contains("assistant")) attachAssistantBar(el, v);
      else {
        attachUserBar(el);
        try { paintBubbleMentions(el); } catch (_) {}
      }
    }
    editingTarget = null;
    composerInput.value = "";
    autoResize();
    hideEditBanner();
    if (isUserEdit) {
      // Edit (or same-text resend) restarts the thread here in strict order
      // (SEND keeps loading throughout):
      // 1. verified atomic prune — the server deletes EVERYTHING below in
      //    one write and confirms it; nothing below can leak into context.
      // 2. resend the edited message with the EXISTING user id (no duplicate
      //    persist, last-20 context).
      // 3. regenerate its single reply.
      if (mid) {
        try {
          await pruneBelowServer(mid);
        } catch (err) {
          toast(err instanceof Error && err.message ? err.message : "Delete failed");
          return true;
        }
        // DOM mirror of the confirmed prune (instant, no extra requests).
        try {
          let n = el ? el.nextElementSibling : null;
          while (n) {
            const nx = n.nextElementSibling;
            n.remove();
            n = nx;
          }
        } catch (_) {}
      } else {
        // Never-persisted bubble: legacy per-node cleanup (DOM + best-effort).
        await pruneBelow(el);
      }
      lastUserEl = el;
      lastUserText = v;
      composerInput.focus();
      // A previous generation may still run: stop it first (same as
      // regenerate/new-send), otherwise the old turn owns activeRun and
      // this resend is silently dropped — or both persist a reply.
      await abortActiveStream();
      // Resend carries the SAME @ ids + / skill as the original turn.
      try { restoreBubbleRefs(el, v); } catch (_) {}
      // Single attempt only: retrying without skip_history_ids would
      // persist a duplicate user message and generate a second reply.
      // Prune is done: hand the button to the new turn so it shows STOP.
      try { sendBtn?.classList.remove("loading"); } catch (_) {}
      try {
        await executeAgentTurn(v, [], { skipHistoryIds: mid ? [mid] : null });
      } catch (_) {
        toast("Send failed — try again");
      }
      return true;
    }
    composerInput.focus();
    return true;
  } finally {
    if (sendBtn) {
      try {
        sendBtn.classList.remove("loading");
        // Recompute instead of blindly enabling: an empty composer with
        // no task stays a disabled normal send.
        updateSendEnabled();
      } catch (_) {}
    }
  }
}

async function editAssistantMessage(shell, raw) {
  void raw;
  startComposerEdit(shell);
}

async function deleteMessage(shell) {
  const mid = shell && shell.dataset ? shell.dataset.mid : null;
  const live = mid ? resolveLiveMsg(mid) : null;
  if (mid) {
    try {
      await postMessageApi({ action: "delete", id: mid });
    } catch (err) {
      toast(err instanceof Error ? err.message : "Delete failed");
      return;
    }
  }
  try {
    (live || shell).remove();
  } catch (_) {}
}

function previousUserText(shell) {
  // Walk back to the nearest user bubble for a reliable regenerate prompt.
  let prev = shell.previousElementSibling;
  while (prev) {
    if (prev.classList && prev.classList.contains("user") && String(prev.dataset.raw || "").trim()) {
      return String(prev.dataset.raw);
    }
    prev = prev.previousElementSibling;
  }
  return "";
}

async function regenerateReply(shell) {
  // Regenerate from this reply's prompt: delete EVERYTHING from the reply
  // down (the reply + all bubbles below, server + DOM + embeddings), then
  // generate fresh for the same prompt. No duplicate user bubble appended.
  if (editingTarget) cancelComposerEdit();
  await abortActiveStream();
  // Same staleness as edit: rebind to the live bubble before pruning.
  try {
    const _rmid = shell && shell.dataset ? shell.dataset.mid : null;
    if (_rmid) {
      const _live = resolveLiveMsg(_rmid);
      if (!_live) {
        toast("Message changed on screen — try again");
        return;
      }
      shell = _live;
    }
  } catch (_) {}
  const userText = shell.dataset.userText || previousUserText(shell) || lastUserText;
  if (!userText) {
    toast("Nothing to regenerate");
    return;
  }
  // SEND loading until the fresh reply lands (same contract as edit-resend).
  if (sendBtn) {
    try {
      sendBtn.disabled = true;
      sendBtn.classList.add("loading");
    } catch (_) {}
  }
  try {
    // Anchor = the nearest persisted user bubble above: one atomic server
    // prune drops the reply + everything below it (verified count), then
    // the DOM mirrors it instantly.
    let anchorMid = null;
    try {
      let p = shell.previousElementSibling;
      while (p) {
        if (p.classList && p.classList.contains("user") && p.dataset && p.dataset.mid) {
          anchorMid = p.dataset.mid;
          lastUserEl = p;
          break;
        }
        p = p.previousElementSibling;
      }
    } catch (_) {}
    if (anchorMid) {
      try {
        await pruneBelowServer(anchorMid);
      } catch (err) {
        toast(err instanceof Error && err.message ? err.message : "Delete failed");
        return;
      }
      try {
        let n = shell;
        while (n) {
          const nx = n.nextElementSibling;
          n.remove();
          n = nx;
        }
      } catch (_) {}
    } else {
      // No persisted anchor (fresh thread): legacy per-node cleanup.
      const victims = [];
      try {
        let node = shell.nextElementSibling;
        while (node) {
          victims.push(node);
          node = node.nextElementSibling;
        }
        victims.unshift(shell);
        victims.forEach((n, i) => {
          try {
            n.style.transition = "opacity 220ms ease, transform 220ms ease";
            n.style.transitionDelay = `${Math.min(i * 40, 400)}ms`;
            n.style.opacity = "0";
            n.style.transform = "translateY(10px)";
          } catch (_) {}
        });
        const waitMs = Math.min(victims.length * 40, 400) + 240;
        try {
          await new Promise((r) => setTimeout(r, waitMs));
        } catch (_) {}
      } catch (_) {}
      try {
        for (const n of victims) {
          try {
            const rid = n.dataset ? n.dataset.mid : null;
            if (rid) {
              try {
                await postMessageApi({ action: "delete", id: rid });
              } catch (_) {}
            }
            n.remove();
          } catch (_) {}
        }
      } catch (_) {
        try { shell.remove(); } catch (_) {}
      }
    }
    if (userText) lastUserText = userText;
    // Reuse the kept user bubble (no duplicate persist); only the reply is new.
    // Same picks as the original turn: the anchor bubble's stash re-arms
    // @ ids + / skill so the resent context injects them identically.
    try { restoreBubbleRefs(lastUserEl, userText); } catch (_) {}
    // Prune is done: hand the button to the new turn so it shows STOP
    // (loading would cover the stop icon and block its click).
    try { sendBtn?.classList.remove("loading"); } catch (_) {}
    await executeAgentTurn(userText, [], { skipHistoryIds: anchorMid ? [anchorMid] : null });
  } finally {
    if (sendBtn) {
      try {
        sendBtn.classList.remove("loading");
        // Recompute instead of blindly enabling: an empty composer with
        // no task stays a disabled normal send.
        updateSendEnabled();
      } catch (_) {}
    }
  }
}

async function editUserMessage(el) {
  startComposerEdit(el);
}

async function abortActiveStream() {
  // Invalidate any 409-retry sleeping in executeAgentTurn first: an abort
  // during backoff must never auto-start afterwards.
  try { stopGen++; } catch (_) {}
  try { hideQuestionCard(); } catch (_) {}
  try { clearPendingStored(); } catch (_) {}
  try { pendingModelConnect = null; } catch (_) {}
  if (!activeRun) return;
  activeRun.cancelled = true;
  try {
    if (typeof activeRun.abort === "function") activeRun.abort();
  } catch (_) {}
  await postCancel();
  activeRun = null;
  setSending(false);
}

async function stopGeneration() {
  // The send button is a stop button while anything generates — including a
  // pre-refresh run this page never owned (no local stream to abort).
  // STOP also drops any open question strip (ask/approval/needs_model) so a
  // refresh never restores it and the stopped task never auto-resumes.
  // Bump the generation first so a 409-retry sleeping through this STOP
  // never auto-starts the task the user just killed.
  try { stopGen++; } catch (_) {}
  try { hideQuestionCard(); } catch (_) {}
  try { clearPendingStored(); } catch (_) {}
  try { pendingModelConnect = null; } catch (_) {}
  // STOP during a Take-Control handoff also drops the blocking control card
  // (the loop was paused on it; cancel wakes it with stop). The edge tab
  // comes back at once so refresh sees the same state.
  try {
    document.getElementById("liveCard")?.remove();
    manualControlOpen = false;
    pendingLiveQid = null;
    pendingQuestion = null;
  } catch (_) {}
  try {
    if (activeRun) {
      activeRun.cancelled = true;
      try {
        if (typeof activeRun.abort === "function") activeRun.abort();
      } catch (_) {}
      activeRun = null;
    }
    // Freeze BEFORE cancel so the inactive chat push cannot delete Working.
    try {
      const thinkingEl = document.getElementById("thinkingMsg");
      if (thinkingEl) settleThinkingStopped(thinkingEl);
    } catch (_) {}
    await postCancel();
  } catch (_) {}
  catchupResume = false;
  try {
    const thinkingEl = document.getElementById("thinkingMsg");
    if (thinkingEl) settleThinkingStopped(thinkingEl);
  } catch (_) {}
  try { clearPendingUser(); } catch (_) {}
  try { resumePending = false; resumeSettled = false; } catch (_) {}
  // Arm the Continue pill: the stopped task resumes in place on tap (empty
  // composer only — any typed text is a brand-new request instead).
  try {
    const _mid = (typeof lastUserEl !== "undefined" && lastUserEl && lastUserEl.dataset && lastUserEl.dataset.mid) || "";
    setStoppedTask(_mid || readStoppedUserId() || "1");
  } catch (_) {}
  activeThinking = null;
  // The browser template belongs to the stopped task's visible state too:
  // bring the edge tab back so it survives the next refresh as-is.
  try { restoreBrowserTabAfterContinue(); } catch (_) {}
  setSending(false);
  try {
    composerInput.focus();
  } catch (_) {}
}

export async function initChat() {
  chatScroll = document.getElementById("chatScroll");
  chatInner = document.getElementById("chatInner");
  composerInput = document.getElementById("composerInput");
  sendBtn = document.getElementById("sendBtn");
  attachPreview = document.getElementById("attachmentPreview");
  composerAttachBox = document.getElementById("composerAttachments");
  const composer = document.getElementById("composer");
  if (!chatScroll || !chatInner || !composerInput || !sendBtn || !composer) return;

  // History first: resumeActiveRun appends after it, so nothing wipes it.
  await loadHistory().catch(() => {});
  initComposerHL();
  initAttachments();
  initMic();
  initUserMessageGestures();
  syncModeFromServer().catch(() => {});
  ensureEditBanner();
  // Restored live sessions survive refresh — the task keeps all its data.
  // One-shot restore; afterwards everything arrives via push (no polling).
  restoreLive().catch(() => {});
  onPush("browser", onBrowserPush);
  onPush("live", onLivePush);
  onPush("chat", onChatPush);
  onPush("chat_done", onChatDonePush);
  onPush("run", onRunPush);
  // The `agent` section arrives first (last-50): render immediately.
  onPush("chat_last50", onChatLast50Section);
  // Returning to the agent page re-checks the browser automatically, so a
  // session started while another page was open still shows its template.
  window.addEventListener("x5:agent-open", () => {
    autoCheckBrowserOnAgentOpen().catch(() => {});
  });
  // A refresh mid-generation resumes the running state instead of losing it.
  // The pending question restores only AFTER the resume settles, so its
  // live-run validation sees the rebuilt Working block / server frames.
  try {
    await resumeActiveRun().catch(() => {});
  } catch (_) {}
  // A refresh mid-question restores the strip until it is answered/expired.
  try {
    await restorePendingQuestion().catch(() => {});
  } catch (_) {}
  // Manual copy is off on agent messages (the copy button still works,
  // the composer stays fully editable).
  chatScroll.addEventListener("copy", (e) => {
    // Copy stays off on agent messages, but the live template is copyable.
    try {
      if (e.target && e.target.closest && e.target.closest(".live-card")) return;
    } catch (_) {}
    e.preventDefault();
  });
  // Code-card copy buttons (all replies, live + history): one delegated
  // listener — cards are rendered dynamically by formatRich.
  chatScroll.addEventListener("click", (e) => {
    try {
      const btn = e.target && e.target.closest ? e.target.closest(".code-copy") : null;
      if (!btn || !chatScroll.contains(btn)) return;
      e.stopPropagation();
      e.preventDefault();
      copyCodeBlock(btn);
    } catch (_) {}
  });
  // Code-card copy buttons (all replies, live + history): one delegated
  // listener — cards are rendered dynamically by formatRich.
  chatScroll.addEventListener("click", (e) => {
    try {
      const btn = e.target && e.target.closest ? e.target.closest(".code-copy") : null;
      if (!btn || !chatScroll.contains(btn)) return;
      e.stopPropagation();
      e.preventDefault();
      copyCodeBlock(btn);
    } catch (_) {}
  });
  // Folder path links (model-written folder/file paths): one delegated
  // listener — links are rendered dynamically by linkifyFolderPaths and
  // reuse the same reveal flow as @file tokens (never throws).
  chatScroll.addEventListener("click", (e) => {
    try {
      const link = e.target && e.target.closest ? e.target.closest(".ws-path-link") : null;
      if (!link || !chatScroll.contains(link)) return;
      e.stopPropagation();
      e.preventDefault();
      revealFolderFile(link.getAttribute("data-ws-path") || "");
    } catch (_) {}
  });

  // A resumed pre-refresh run still owns the UI: keep STOP, never fall back
  // to SEND/Continue under it (that was the running-with-Continue-pill bug).
  // Exception: an open choice dialog owns the button as SEND (typing or
  // picking an answer goes through it) — STOP must never cover it.
  try {
    if (pendingQuestion && document.getElementById("questionCard")) setSending(false);
    else if (catchupResume && activeThinking) setSending(true);
    else setSending(false);
  } catch (_) {
    setSending(false);
  }
  sendBtn.addEventListener("click", (e) => {
    // While generating, the send button is a stop button (works even for a
    // pre-refresh run: the cancel reaches the server either way).
    if (sendBtn.classList.contains("stop")) {
      e.preventDefault();
      e.stopPropagation();
      stopGeneration();
      toast("Stopped");
      return;
    }
    // Stopped task + empty composer: the pill resumes it in place (nothing
    // is sent — any typed text would be a brand-new request instead).
    if (sendBtn.classList.contains("continue")) {
      e.preventDefault();
      e.stopPropagation();
      handleContinue();
    }
  }, true);
  composer.addEventListener("submit", (e) => {
    e.preventDefault();
    if (sendBtn.classList.contains("stop")) {
      stopGeneration();
      return;
    }
    if (sendBtn.classList.contains("continue")) {
      handleContinue();
      return;
    }
    handleSend();
  });
  composerInput.addEventListener("input", autoResize);
  composerInput.addEventListener("input", clearChoiceOnType);
  composerInput.addEventListener("input", updateSendEnabled);
  composerInput.addEventListener("input", checkMention);
  composerInput.addEventListener("input", updateEditSendState);
  composerInput.addEventListener("paste", (e) => {
    // Oversize paste becomes a TXT file automatically (safe limit respected).
    try {
      const text = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
      if (!text) return;
      const cur = composerInput.value || "";
      const start = composerInput.selectionStart ?? cur.length;
      const end = composerInput.selectionEnd ?? cur.length;
      const full = cur.slice(0, start) + text + cur.slice(end);
      if (full.length <= MAX_MESSAGE) return;
      e.preventDefault();
      if (pendingAttachments.length >= 2) {
        const room = Math.max(0, MAX_MESSAGE - cur.length);
        composerInput.value = (cur + text.slice(0, room)).slice(0, MAX_MESSAGE);
        autoResize();
        toast("احذف مرفقاً أولاً (بحد أقصى 2)");
        return;
      }
      const fname = `pasted-${Date.now().toString(36)}.txt`;
      showAttachmentPreview(new File([full], fname, { type: "text/plain" }), "text");
      composerInput.value = "";
      autoResize();
      toast("النص الطويل تحول لملف TXT تلقائياً");
    } catch (_) {}
  });
  composerInput.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && editingTarget) {
      e.preventDefault();
      cancelComposerEdit();
      return;
    }
    if (e.key === "Escape" && (mentionMenu || document.getElementById("wsPathbar"))) {
      e.preventDefault();
      hideMentionMenu();
      return;
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      composer.requestSubmit();
    }
  });
  window.addEventListener("x5:agent-open", () => scrollToBottom(false, true));
  // Settings → Delete chat history wiped everything: no task exists, so
  // Continue disarms and the button falls back to a disabled normal send.
  window.addEventListener("x5:chat-cleared", () => {
    try { clearStoppedTask(); } catch (_) {}
  });

  // Scroll-up pagination: reaching the top auto-loads the 50 older messages.
  // Scroll-to-latest glass button: appears when the user scrolls up.
  const scrollBtn = document.getElementById("chatScrollBtn");
  const updateScrollBtn = () => {
    if (!chatScroll || !scrollBtn) return;
    const far = chatScroll.scrollHeight - chatScroll.scrollTop - chatScroll.clientHeight > 320;
    scrollBtn.classList.toggle("show", far);
  };
  chatScroll.addEventListener("scroll", () => {
    updateScrollBtn();
    try {
      if (chatScroll.scrollTop <= 120) loadOlderHistory();
    } catch (_) {}
  }, { passive: true });
  scrollBtn?.addEventListener("click", () => scrollToBottom(true, true));
  updateScrollBtn();
}
