import React, { useState, useRef, useEffect, useCallback } from 'react';

/* Exact PDF Maker design, data-driven: every studio (pdf_1, pdf_2...) runs
   in this ONE shell. Approved page code RUNS live in the white page
   (hot-reload); render errors are reported silently to the agent and never
   shown to the user. Export renders every page like pdf motion (0→100).
   Sending in the floating composer posts to the studio branch session. */

function studioIdFromLocation() {
  try {
    const m = (window.location.pathname || '').match(/\/studio-pdf\/(?:shell\/)?([A-Za-z0-9_-]+)/);
    if (m && m[1] !== 'assets') return m[1];
    return new URLSearchParams(window.location.search).get('studio') || 'pdf_1';
  } catch (_) {
    return 'pdf_1';
  }
}

function hashStr(v) {
  let h = 5381;
  const s = String(v || '');
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) | 0;
  return 'h' + (h >>> 0).toString(36);
}

/* Heavy libraries load on demand, never with the shell: Babel standalone
   (~2.8MB) and the export stack (html-to-image + jsPDF) used to be static
   imports, so the browser downloaded and parsed all of it BEFORE the toolbar,
   the composer or the canvas could paint — that is why the studio looked like
   it opened seconds late. Now the chrome paints at once and a page box just
   keeps its black loading template until the design compiler is really here. */
let babelTransform = null;
let babelPromise = null;
function loadBabel() {
  if (!babelPromise) {
    babelPromise = import('@babel/standalone')
      .then((m) => { babelTransform = m.transform; return m; })
      // Drop the failed promise so a later call really retries.
      .catch((e) => { babelPromise = null; throw e; });
  }
  return babelPromise;
}

let exportPromise = null;
function loadExportLibs() {
  if (!exportPromise) {
    exportPromise = Promise.all([import('html-to-image'), import('jspdf')])
      .then(([img, pdf]) => ({ toPng: img.toPng, jsPDF: pdf.jsPDF }))
      .catch((e) => { exportPromise = null; throw e; });
  }
  return exportPromise;
}

/* Compile ONE approved component (React only, no imports) and run it.
   Classic runtime is forced so plain JSX becomes React.createElement, AND
   react/jsx-runtime is shimmed for any code path that still requires it
   (model-written import or automatic-runtime output) — like pdf motion's
   bundled runtime, a missing jsx-runtime must never blank the page. */
function compileComponent(code) {
  // Only reachable once the compiler is ready (page boxes stay on their
  // loading template until then); a clear error beats a mysterious crash.
  if (!babelTransform) throw new Error('Design compiler not loaded yet');
  const out = babelTransform(code, {
    presets: [['react', { runtime: 'classic' }]],
    plugins: ['transform-modules-commonjs'],
    filename: 'page.jsx',
  }).code;
  const mod = { exports: {} };
  const jsxShim = (type, config, maybeKey) => {
    const props = { ...(config || {}) };
    delete props.__self;
    delete props.__source;
    if (maybeKey !== undefined) props.key = maybeKey;
    return React.createElement(type, props);
  };
  const need = (name) => {
    if (name === 'react') return React;
    if (name === 'react/jsx-runtime' || name === 'react/jsx-runtime.js') {
      return { jsx: jsxShim, jsxs: jsxShim, Fragment: React.Fragment };
    }
    throw new Error(`Module not available: ${name}`);
  };
  new Function('React', 'require', 'module', 'exports', out)(React, need, mod, mod.exports);
  const Comp = (mod.exports && mod.exports.default) || mod.exports;
  if (typeof Comp !== 'function') throw new Error('No component exported');
  return Comp;
}

class PageBoundary extends React.Component {
  constructor(p) {
    super(p);
    this.state = { failed: false };
  }
  static getDerivedStateFromError(e) {
    return { failed: true };
  }
  componentDidCatch(err) {
    try { this.props.onError && this.props.onError(err); } catch (_) {}
  }
  componentDidUpdate(prev) {
    if (prev.ckey !== this.props.ckey) this.setState({ failed: false });
  }
  render() {
    // Fallback protection: a broken render shows the last good design,
    // never a blank page. The error still reaches the agent silently.
    if (this.state.failed) return this.props.fallback || null;
    return this.props.children;
  }
}

function LivePage({ code, ckey, onError }) {
  // good = last design that actually rendered; trying = new candidate.
  // Anything that fails (compile or render) keeps the good one visible.
  const [good, setGood] = useState(null);
  const [trying, setTrying] = useState(null);
  const reportedRef = useRef({});
  useEffect(() => {
    let alive = true;
    if (!code) return () => { alive = false; };
    try {
      const C = compileComponent(code);
      if (alive) setTrying({ Comp: C, ckey });
    } catch (e) {
      if (alive && !reportedRef.current[ckey]) {
        reportedRef.current[ckey] = true;
        try { onError && onError(e); } catch (_) {}
      }
      // Compile failed: the last good design stays on screen (fallback).
    }
    return () => { alive = false; };
  }, [code]);
  // Promote the candidate once it survives one paint without tripping
  // the boundary; a trip clears it first (cleanup) so good stays.
  useEffect(() => {
    if (!trying) return undefined;
    const t = setTimeout(() => {
      setGood((prev) => (prev && prev.ckey === trying.ckey ? prev : { Comp: trying.Comp, ckey: trying.ckey }));
      setTrying(null);
    }, 350);
    return () => clearTimeout(t);
  }, [trying]);
  const failCandidate = useCallback((e) => {
    try {
      const key = 'r:' + (trying ? trying.ckey : ckey);
      if (!reportedRef.current[key]) {
        reportedRef.current[key] = true;
        if (onError) onError(e);
      }
    } catch (_) {}
    setTrying(null); // reveal the last good design underneath
  }, [trying, ckey, onError]);
  const shown = trying || good;
  const wrapRef = useRef(null);
  // No white edges, ever: walk down to the first NON-transparent background
  // (roots are often transparent wrappers around the real colored card) and
  // paint it on the fill + the page + the frame, so zooming never grows white.
  useEffect(() => {
    try {
      const wrap = wrapRef.current;
      if (!wrap) return;
      let node = wrap.firstElementChild;
      let depth = 0;
      let paint = null;
      while (node && depth < 5 && !paint) {
        try {
          const cs = getComputedStyle(node);
          const img = cs.backgroundImage;
          if (img && img !== 'none') {
            paint = { image: img, color: cs.backgroundColor };
          } else {
            const bg = cs.backgroundColor;
            if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
              paint = { image: null, color: bg };
            }
          }
        } catch (_) {}
        node = node && node.firstElementChild;
        depth += 1;
      }
      const targets = [wrap, wrap.parentElement, wrap.closest ? wrap.closest('.fit-box') : null];
      // Reset on unmount/design swap so a removed design never stains the
      // frame behind a fresh blank page.
      const cleanup = () => {
        for (const t of targets) {
          if (!t) continue;
          try {
            t.style.background = '';
            t.style.backgroundImage = '';
            t.style.backgroundColor = '';
          } catch (_) {}
        }
      };
      if (paint) {
        for (const t of targets) {
          if (!t) continue;
          try {
            if (paint.image) {
              t.style.backgroundImage = paint.image;
              t.style.backgroundColor = paint.color || 'transparent';
            } else {
              t.style.backgroundImage = 'none';
              t.style.background = paint.color;
            }
          } catch (_) {}
        }
      } else {
        // No detectable paint: stay transparent (never white) so no
        // white halo grows around the design at any zoom.
        for (const t of targets) {
          if (!t) continue;
          try { t.style.background = 'transparent'; } catch (_) {}
        }
      }
      return cleanup;
    } catch (_) {}
    return undefined;
  }, [shown]);
  if (!shown) return null;
  const GoodComp = good && good.ckey !== shown.ckey ? good.Comp : null;
  return (
    <PageBoundary ckey={shown.ckey} onError={failCandidate} fallback={GoodComp ? <GoodComp /> : null}>
      <div ref={wrapRef} className="live-fill"><shown.Comp /></div>
    </PageBoundary>
  );
}

const PX_TO_MM = 0.2645833333;

async function waitFontsReady() {
  try { await document.fonts.ready; } catch (_) {}
  await new Promise((r) => setTimeout(r, 350));
}

const I = {
  pdf: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><path d="M14 2v6h6" /><path d="M9 13h6M9 17h4" /></svg>
  ),
  undo: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 7v6h6" /><path d="M21 17a9 9 0 0 0-15-6.7L3 13" /></svg>
  ),
  redo: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M21 7v6h-6" /><path d="M3 17a9 9 0 0 1 15-6.7L21 13" /></svg>
  ),
  reset: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 1 0 3-6.7" /><polyline points="3 4 3 9 8 9" /></svg>
  ),
  trash: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18" /><path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" /><path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" /><line x1="10" y1="11" x2="10" y2="17" /><line x1="14" y1="11" x2="14" y2="17" /></svg>
  ),
  dots: (
    <svg viewBox="0 0 24 24" fill="currentColor"><circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" /></svg>
  ),
  size: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="5" y="3" width="14" height="18" rx="2" /><path d="M9 3v18" strokeDasharray="2 2" /></svg>
  ),
  down: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="5" x2="12" y2="19" /><polyline points="6 13 12 19 18 13" /></svg>
  ),
  up: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="19" x2="12" y2="5" /><polyline points="6 11 12 5 18 11" /></svg>
  ),
  plus: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round"><path d="M5 12h14" /><path d="M12 5v14" /></svg>
  ),
  mic: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10v2a7 7 0 0 0 14 0v-2" /><line x1="12" y1="19" x2="12" y2="23" /><line x1="8" y1="23" x2="16" y2="23" /></svg>
  ),
  send: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round"><line x1="12" y1="19" x2="12" y2="5" /><polyline points="6 11 12 5 18 11" /></svg>
  ),
  stop: (
    <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
  ),
};

const PAGE_SIZES = [
  { id: 'a4-portrait', label: 'A4 portrait', short: 'Portrait', w: 794, h: 1123 },
  { id: 'a4-landscape', label: 'A4 landscape', short: 'Landscape', w: 1123, h: 794 },
];

function useFitScale(w, h, reserve) {
  const [s, setS] = useState(1);
  useEffect(() => {
    const calc = () => {
      // TOTAL side padding (left+right): 10px each on phones, 16px each on
      // desktop — must match .feed-slide CSS or the page overflows and shifts.
      const padH = window.innerWidth <= 640 ? 20 : 32;
      const availW = window.innerWidth - padH;
      const availH = window.innerHeight - 44 - reserve;
      setS(Math.max(0.15, Math.min(1.5, availW / w, availH / h)));
    };
    calc();
    window.addEventListener('resize', calc);
    return () => window.removeEventListener('resize', calc);
  }, [w, h, reserve]);
  return s;
}

function Lights() {
  const dots = Array.from({ length: 25 }, (_, i) => {
    const r = Math.floor(i / 5), c = i % 5;
    return Math.max(Math.abs(r - 2), Math.abs(c - 2));
  });
  return (
    <div className="st-lights" aria-hidden="true">
      <div className="dot-square working">
        {dots.map((ring, i) => (
          <span key={i} data-ring={ring} style={{ animationDelay: `${ring * 0.3}s` }} />
        ))}
      </div>
      <div className="st-lights-word">Loading…</div>
    </div>
  );
}

/* الحل الوسط بين الطرفين: مهلة grace قبل إظهار الأنيميشن.
   أول paint يغطي الصندوق بأسود سادة فوراً (مفيش صفحة بيضا)، ولو
   التصميم وصل بسرعة (كاش) المكوّن يتفك قبل الـ 250ms فالأنيميشن
   ميظهرش أصلاً (مفيش فلاش ثانية واحدة). لو اتأخر فعلاً تظهر الـ Lights. */
function PendingLights() {
  const [show, setShow] = useState(false);
  useEffect(() => {
    const t = setTimeout(() => setShow(true), 250);
    return () => clearTimeout(t);
  }, []);
  if (!show) return <div className="st-black" aria-hidden="true" />;
  return (
    <div className="st-black"><Lights /></div>
  );
}

export default function App() {
  const [studioId] = useState(studioIdFromLocation);
  // Full UI state persists per studio: leaving and re-entering restores
  // page, zoom, orientation, draft text and the orientation lock exactly.
  const uiKey = `x5:studio-ui:${studioId}`;
  const readUI = () => {
    try {
      const raw = localStorage.getItem(uiKey);
      if (!raw) return {};
      const o = JSON.parse(raw);
      return o && typeof o === 'object' ? o : {};
    } catch (_) {
      return {};
    }
  };
  // Server snapshot cache + hash: first paint restores the last known title,
  // page count and size instantly (no white flash); load() recomputes the
  // hash and rewrites the cache only when it breaks (server changed).
  const cacheKey = `x5:studio-cache:${studioId}`;
  const readCache = () => {
    try {
      const raw = localStorage.getItem(cacheKey);
      if (!raw) return {};
      const o = JSON.parse(raw);
      return o && typeof o === 'object' ? o : {};
    } catch (_) {
      return {};
    }
  };
  const writeCache = (obj) => {
    try {
      localStorage.setItem(cacheKey, JSON.stringify(obj || {}));
    } catch (_) {}
  };
  const [studioTitle, setStudioTitle] = useState(() => {
    try {
      return String(readCache().title || '');
    } catch (_) {
      return '';
    }
  });
  const [pageSize, setPageSize] = useState(() => {
    try {
      const c = readCache().pageSizeId || readUI().pageSizeId;
      return PAGE_SIZES.find((p) => p.id === c) || PAGE_SIZES[0];
    } catch (_) {
      return PAGE_SIZES[0];
    }
  });
  // Cached count first (instant last-page restore); the server truth
  // corrects it on load without any scroll animation (see bootedRef).
  const [count, setCount] = useState(() => {
    try {
      const c = Number(readCache().count);
      return Number.isInteger(c) && c >= 1 && c <= 50 ? c : 1;
    } catch (_) {
      return 1;
    }
  });
  const [codes, setCodes] = useState({}); // page_no -> approved code (runs live)
  // صفحات السيرفر قال إن ليها تصميم لكن لسه متحمّلش على الشاشة (pending):
  // نعرض لودر بدل الورقة البيضا. البذرة من الكاش عشان أول paint ميبيّضش
  // قبل ما أول load() يرد.
  const [knownCoded, setKnownCoded] = useState(() => {
    try {
      const flags = String(readCache().flags || '');
      const next = {};
      for (let i = 0; i < flags.length && i < 50; i++) {
        if (flags[i] === '1') next[i + 1] = true;
      }
      return next;
    } catch (_) {
      return {};
    }
  });
  // No boot overlay: the studio opens instantly, every box paints dark at
  // once (never white), approved designs stream in, and the Lights animation
  // appears only when a box really waits past the 250ms grace (PendingLights).
  const [pageIndex, setPageIndex] = useState(() => {
    try {
      const i = readUI().pageIndex;
      const c = Number(readCache().count);
      const max = Number.isInteger(c) && c >= 1 ? c - 1 : 0;
      if (!Number.isInteger(i) || i < 0) return 0;
      return Math.min(i, Math.max(0, max));
    } catch (_) {
      return 0;
    }
  });
  // Entry guard: until the FIRST load() settles we know nothing about the
  // server truth (old caches carry no flags) — every box stays pending/dark
  // meanwhile so the entry never flashes a white sheet before designs land.
  const [entered, setEntered] = useState(false);
  // 'loading' | 'ready' | 'failed' for the lazily fetched design compiler
  // (Babel standalone). A coded box keeps its black loading template until
  // 'ready', so a design box is never painted empty while the compiler is
  // still on its way.
  const [babelState, setBabelState] = useState('loading');
  // Entry guard for the instant landing scroll below (no glide on open).
  const bootedRef = useRef(false);
  // Inline title rename (double-click the name): draft while editing.
  const [editingTitle, setEditingTitle] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [toast, setToast] = useState('');
  const [msg, setMsg] = useState(() => {
    try {
      return String(readUI().msg || '').slice(0, 4000);
    } catch (_) {
      return '';
    }
  });
  const [attachOpen, setAttachOpen] = useState(false);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [recLabel, setRecLabel] = useState('Recording…');
  const recRef = useRef({ stream: null, rec: null, chunks: [], timer: null, raf: null, ctx: null, t0: 0, stopTimer: null, sendAfter: false, discarded: false });
  // Local optimistic flag (instant black screen right after send).
  // The SERVER designing flag is the source of truth across reloads:
  // running = local busy OR server designing (see slides render below).
  const [busyPages, setBusyPages] = useState({}); // page_no -> true while ITS design runs
  const [designingPages, setDesigningPages] = useState([]); // server truth: turns running now
  const seenDesigningRef = useRef({}); // pages confirmed designing since our send
  const emptyEndRef = useRef({}); // pages already reported as ended-without-design
  const [leavingPages, setLeavingPages] = useState({}); // page_no -> true during black fade-out
  // Cached pages run direct: when the design is already loaded (from cache
  // or poll) no loading overlay ever covers it — lights show only while a
  // turn is actually generating on that box.
  const [stopping, setStopping] = useState(false); // STOP pressed, loop not halted yet
  const [stepDir, setStepDir] = useState({}); // page_no -> 'undo'|'redo' while its step animation runs
  // Stopped tasks offer Continue (refresh-safe, per page): resume the last
  // request as a new turn — delivered slides persist, work resumes.
  const continueKey = `x5:studio-continue:${studioId}`;
  const [continueMap, setContinueMap] = useState(() => {
    try {
      const raw = localStorage.getItem(continueKey);
      const arr = raw ? JSON.parse(raw) : [];
      if (!Array.isArray(arr)) return {};
      const next = {};
      for (const v of arr) {
        const k = Number(v);
        if (Number.isInteger(k) && k >= 1 && k <= 50) next[k] = true;
      }
      return next;
    } catch (_) {
      return {};
    }
  });
  const setContinueStored = (updater) => {
    setContinueMap((prev) => {
      const next = typeof updater === 'function' ? updater(prev) : updater;
      try {
        localStorage.setItem(continueKey, JSON.stringify(
          Object.keys(next || {}).map(Number).filter((x) => Number.isInteger(x))));
      } catch (_) {}
      return next;
    });
  };
  const [locked, setLocked] = useState(() => {
    try {
      return readUI().locked === true;
    } catch (_) {
      return false;
    }
  }); // orientation locks on first prompt
  const [histCounts, setHistCounts] = useState({}); // page_no -> {undo, redo}
  const [missing, setMissing] = useState(false);
  const imgInputRef = useRef(null);
  const fontInputRef = useRef(null);
  const missRef = useRef(0);
  const failRef = useRef(0); // consecutive unreachable polls (offline toast)
  const offlineToastRef = useRef(0); // last offline toast epoch (throttle 60s)
  const landedRef = useRef(false); // first scroll lands instantly (no entry glide)
  const geomRef = useRef({ h: 0 }); // feed height (resize transients hold the page)
  const sendHashRef = useRef({});
  const t0Ref = useRef(Date.now()); // entry moment (kept for timing guards)
  const codesRef = useRef({}); // live mirror of codes (load/fetch race-safe)
  const [expPct, setExpPct] = useState(-1); // export progress -1 = hidden
  const [expStep, setExpStep] = useState({ done: 0, total: 0 }); // real label: page done/total
  const expCancelRef = useRef(false); // export STOP flag (checked between pages)
  const expAbortRef = useRef(null); // in-flight render abort (STOP kills it too)
  const expGenRef = useRef(0); // export generation: cancel/restart invalidates a running loop
  // One page render MUST NOT hang: slow font hosts (gstatic timeouts) used to
  // freeze the whole export with no error and no way out.
  const RENDER_TIMEOUT_MS = 30000;
  const feedRef = useRef(null);
  const agentRef = useRef(null);
  const taRef = useRef(null);
  const touchY = useRef(null);
  const lockScroll = useRef(false);
  const reportedRef = useRef({});
  const [zoom, setZoom] = useState(() => {
    try {
      const z = Number(readUI().zoom);
      return z >= 0.3 && z <= 3 ? z : 1;
    } catch (_) {
      return 1;
    }
  });
  const [agentH, setAgentH] = useState(150);
  // The template always sits FULLY above the agent composer (default): the
  // full composer height is reserved, so no footer ever hides under it —
  // centered in the visible area on phone and desktop alike.
  const fit = useFitScale(pageSize.w, pageSize.h, agentH + 16);
  const s = fit * zoom;
  const clampZ = (z) => Math.max(0.3, Math.min(3, z));
  const zoomRef = useRef(1);
  const sRef = useRef(1);
  const idxRef = useRef(0);
  const anchorRef = useRef(null);
  const pinchRef = useRef(null);
  const [pinching, setPinching] = useState(false); // active 2-finger pinch: zoom follows 1:1
  const panRef = useRef(null);
  const panAnim = useRef(null); // the ONE pan rAF (follow + momentum + zoom-keep)
  const [panning, setPanning] = useState(false);
  const [calm, setCalm] = useState(true);
  const calmT = useRef(null);
  const animRef = useRef(null);
  useEffect(() => { zoomRef.current = zoom; sRef.current = s; idxRef.current = pageIndex; codesRef.current = codes; });

  const showToast = (t) => {
    setToast(t);
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => setToast(''), 1800);
  };

  const autoGrow = () => {
    const el = taRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(216, el.scrollHeight) + 'px';
  };

  const fetchCode = useCallback(async (n) => {
    try {
      const r = await fetch(`/api/studio/page?studio_id=${encodeURIComponent(studioId)}&page_no=${n}`, { credentials: 'same-origin' });
      const d = await r.json();
      if (d && d.has_code && d.code) {
        setCodes((prev) => (prev[n] === d.code ? prev : { ...prev, [n]: d.code }));
        // Its design landed: fade the black template out, then swap to the
        // live design (hash changed since send).
        setBusyPages((prev) => {
          if (!prev[n]) return prev;
          if (hashStr(d.code) === sendHashRef.current[n]) return prev;
          setLeavingPages((lp) => (lp[n] ? lp : { ...lp, [n]: true }));
          setTimeout(() => {
            setBusyPages((cur) => {
              if (!cur[n]) return cur;
              const next = { ...cur };
              delete next[n];
              return next;
            });
            setLeavingPages((lp) => {
              if (!lp[n]) return lp;
              const next = { ...lp };
              delete next[n];
              return next;
            });
          }, 380);
          return prev;
        });
      } else {
        // Truly empty: blank is the truth.
        setKnownCoded((prev) => {
          if (!prev[n]) return prev;
          const next = { ...prev };
          delete next[n];
          return next;
        });
        setCodes((prev) => {
          if (!(n in prev)) return prev;
          const next = { ...prev };
          delete next[n];
          return next;
        });
      }
    } catch (_) {}
  }, [studioId]);

  const load = useCallback(async () => {
    try {
      const r = await fetch(`/api/studio?studio_id=${encodeURIComponent(studioId)}`, { credentials: 'same-origin' });
      if (r.status === 404) {
        // Studio deleted (e.g. with the chat): stop polling and say so once.
        missRef.current += 1;
        if (missRef.current >= 3) setMissing(true);
        return;
      }
      missRef.current = 0;
      const data = await r.json();
      if (data && data.meta) {
        setStudioTitle(String(data.meta.title || ''));
        if (data.history) setHistCounts(data.history);
        // Server size is the truth (pdf-motion preset method): the preview
        // ALWAYS equals the box the model designs for — mismatch impossible.
        try {
          const pg = data.page;
          if (pg && Number.isInteger(pg.w) && Number.isInteger(pg.h)) {
            const match = PAGE_SIZES.find((p) => p.w === pg.w && p.h === pg.h);
            if (match) setPageSize((prev) => (prev.id === match.id ? prev : match));
          }
          if (data.size_locked) setLocked(true);
        } catch (_) {}
        const pages = Array.isArray(data.pages) ? data.pages : [];
        // Server designing flags: turns running RIGHT NOW. Re-entering shows
        // the black loading template from this truth — never a blank reset.
        let designing = [];
        try {
          const raw = Array.isArray(data.designing) ? data.designing : [];
          designing = raw.map(Number).filter((n) => Number.isInteger(n) && n >= 1);
          setDesigningPages(designing);
          for (const n of designing) seenDesigningRef.current[n] = true;
          if (designing.length) setLocked(true);
        } catch (_) {}
        if (pages.length) {
          setCount((c) => Math.max(c, pages.length));
          // Orientation locks as soon as ANY approved design exists (or any
          // turn runs): it is changeable only before the first request.
          try {
            if (pages.some((p) => p && p.has_code)) setLocked(true);
          } catch (_) {}
          const coded = new Set();
          try {
            const nextKnown = {};
            pages.forEach((p) => {
              if (p && p.has_code) {
                const n = Number(p.page_no);
                coded.add(n);
                nextKnown[n] = true;
                // Design exists but not on screen yet: stream it in unless it
                // already runs live (skip re-fetch on every 3s poll). The box
                // shows pending lights meanwhile — never a white flash.
                if (!codesRef.current[n]) fetchCode(n);
              }
            });
            setKnownCoded(nextKnown);
          } catch (_) {}
          // A turn we sent ended with NO design: free the send mark honestly
          // instead of loading forever (blank is the truth here).
          setBusyPages((prev) => {
            const keys = Object.keys(prev);
            if (!keys.length) return prev;
            let changed = false;
            const next = { ...prev };
            for (const k of keys) {
              const n = Number(k);
              if (seenDesigningRef.current[n] && designing.indexOf(n) < 0 && !coded.has(n)) {
                delete next[n];
                changed = true;
                if (!emptyEndRef.current[n]) {
                  emptyEndRef.current[n] = true;
                  showToast('Turn ended with no design — Continue or describe again');
                }
                setContinueStored((prev) => (prev[n] ? prev : { ...prev, [n]: true }));
              }
            }
            return changed ? next : prev;
          });
        }
        // Cache + hash: snapshot the server truth; a broken hash (changed
        // title/pages/size) rewrites the cache so the next entry restores it.
        try {
          const pages = Array.isArray(data.pages) ? data.pages : [];
          const flags = pages.map((p) => (p && p.has_code ? '1' : '0')).join('');
          const pg = data.page || {};
          const sig = `${String((data.meta && data.meta.title) || '')}|${pages.length}|${flags}|${pg.w || ''}x${pg.h || ''}`;
          const h = hashStr(sig);
          const prev = readCache();
          if (prev.hash !== h) {
            writeCache({
              hash: h,
              title: String((data.meta && data.meta.title) || ''),
              count: pages.length || 1,
              flags,
              pageSizeId: (() => {
                try {
                  const m = PAGE_SIZES.find((p) => p.w === pg.w && p.h === pg.h);
                  if (m) return m.id;
                  return String(readCache().pageSizeId || 'a4-portrait');
                } catch (_) {
                  return 'a4-portrait';
                }
              })(),
            });
          }
        } catch (_) {}
        failRef.current = 0;
        // First truth landed (entry guard for scroll only).
        if (!bootedRef.current) {
          bootedRef.current = true;
        }
        return data;
      }
      return null;
    } catch (_) {
      // The server is unreachable (restarting / network down): the 3s poll
      // retries silently, but after a streak say so once instead of leaving
      // stale spinners with no explanation.
      if (!missing) {
        failRef.current = (failRef.current || 0) + 1;
        const now = Date.now();
        if (failRef.current >= 5 && now - (offlineToastRef.current || 0) > 60000) {
          offlineToastRef.current = now;
          showToast('Server unreachable — is the console running? It retries on its own.');
        }
      }
      return null;
    }
  }, [studioId, fetchCode, missing]);

  /* The design compiler is fetched right away, in parallel with the first
     load(), but never blocks the shell's first paint. Bounded retries (3);
     if it truly cannot load, say so instead of spinning forever. */
  useEffect(() => {
    let alive = true;
    let attempts = 0;
    const tryLoad = () => {
      attempts += 1;
      loadBabel().then(() => {
        if (alive) setBabelState('ready');
      }).catch(() => {
        if (!alive) return;
        if (attempts < 3) {
          setTimeout(tryLoad, 1500);
        } else {
          setBabelState('failed');
          showToast('Design compiler failed to load — reload the page');
        }
      });
    };
    tryLoad();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /* Entry: instant open — boxes stay dark-pending until this first truth
     lands, then meta + codes stream in behind them (never a white flash). */
  useEffect(() => {
    let alive = true;
    (async () => {
      const data = await load();
      if (!alive) return;
      setEntered(true);
      try {
        const pages = data && Array.isArray(data.pages) ? data.pages : [];
        // Stream each approved design in as it arrives (no await-all gate).
        pages.filter((p) => p && p.has_code).forEach((p) => {
          fetchCode(Number(p.page_no));
        });
      } catch (_) {}
    })();
    return () => { alive = false; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [load, fetchCode]);

  /* Persist UI state on every change: re-entry restores everything. */
  useEffect(() => {
    try {
      localStorage.setItem(uiKey, JSON.stringify({
        pageIndex, zoom, pageSizeId: pageSize.id,
        msg: String(msg || '').slice(0, 4000), locked,
      }));
    } catch (_) {}
  }, [uiKey, pageIndex, zoom, pageSize, msg, locked]);

  useEffect(() => {
    autoGrow();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    if (missing) return undefined; // deleted studio: never poll again
    const t = setInterval(load, 3000); // hot-reload poll
    return () => clearInterval(t);
  }, [load, missing]);

  const closeToChat = () => {
    try {
      window.top.postMessage('x5:studio-missing', '*');
    } catch (_) {}
  };

  useEffect(() => {
    if (!missing) return undefined; // dead link: leave on its own after a beat
    const t = setTimeout(() => closeToChat(), 5000);
    return () => clearTimeout(t);
  }, [missing]);

  /* Silent error channel: render failures go to the agent, never the user. */
  const reportError = useCallback((n, err) => {
    try {
      const key = `${n}:${hashStr(String((err && err.message) || err))}`;
      if (reportedRef.current[key]) return;
      reportedRef.current[key] = true;
      fetch('/api/studio/page_error', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, page_no: n, error: String((err && err.message) || err).slice(0, 500) }),
      }).catch(() => {});
    } catch (_) {}
  }, [studioId]);

  const poke = useCallback(() => {
    setCalm(false);
    clearTimeout(calmT.current);
    calmT.current = setTimeout(() => setCalm(true), 1000);
  }, []);

  useEffect(() => {
    const el = agentRef.current;
    if (!el) return;
    const measure = () => setAgentH(el.offsetHeight || 150);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    window.addEventListener('resize', measure);
    return () => { ro.disconnect(); window.removeEventListener('resize', measure); };
  }, []);

  const smoothTo = (el, target) => {
    cancelAnimationFrame(animRef.current);
    const start = el.scrollTop;
    const dist = target - start;
    if (Math.abs(dist) < 2) return;
    // Shorter, softer glide: fast flicks land in ~350ms, long jumps cap low.
    const dur = Math.min(380, 150 + Math.abs(dist) * 0.10);
    const t0 = performance.now();
    lockScroll.current = true;
    el.style.scrollSnapType = 'none';
    const step = (t) => {
      const p = Math.min(1, (t - t0) / dur);
      // easeOutCubic: instant start, buttery settle (no harsh snap-back).
      const e = 1 - Math.pow(1 - p, 3);
      el.scrollTop = start + dist * e;
      if (p < 1) {
        animRef.current = requestAnimationFrame(step);
      } else {
        el.style.scrollSnapType = 'y mandatory';
        setTimeout(() => { lockScroll.current = false; }, 40);
      }
    };
    animRef.current = requestAnimationFrame(step);
  };
  const cancelAnim = () => {
    cancelAnimationFrame(animRef.current);
    lockScroll.current = false;
    const el = feedRef.current;
    if (el) el.style.scrollSnapType = 'y mandatory';
  };

  // Pages are NEVER created by navigation: the ONLY way to add a page is
  // the fixed + button (addPage below), which then scrolls to the new page.
  const goTo = useCallback((i) => {
    if (!Number.isInteger(i)) return;
    poke();
    stopPanGlide();
    const total = Math.max(1, count);
    const clamped = Math.max(0, Math.min(i, total - 1));
    setPageIndex((prev) => (clamped === prev ? prev : clamped));
  }, [poke, count]);

  // THE single page creator (fixed + button, phone + desktop): creates one
  // real page on the server, then scrolls to it automatically (one press).
  const addPage = useCallback(async () => {
    if (studioBusy()) {
      showToast('Wait for generation to finish');
      return;
    }
    poke();
    try {
      const r = await fetch('/api/studio/add_page', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data || data.success !== true) {
        showToast(`Could not add page: ${String((data && data.error) || `HTTP ${r.status}`).slice(0, 100)}`);
        return;
      }
      const nxt = Number(data.page_no);
      await load();
      setCount((c) => Math.max(c + 1, Number.isInteger(nxt) && nxt >= 1 ? nxt : c + 1));
      if (Number.isInteger(nxt) && nxt >= 1) setPageIndex(nxt - 1);
      else setPageIndex((prev) => prev + 1);
      showToast(`Page ${Number.isInteger(nxt) && nxt >= 1 ? nxt : 'added'} added`);
    } catch (_) {
      showToast('Could not add page');
    }
  }, [studioId, load, poke]);

  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    const target = pageIndex * el.clientHeight;
    // Entry (cache restore + server correction): land INSTANTLY on the last
    // page every time — no glide down from the first slide, no animation.
    // Only real user moves after boot use the smooth glide below.
    if (!landedRef.current || !bootedRef.current) {
      landedRef.current = true;
      try {
        cancelAnimationFrame(animRef.current);
        el.style.scrollSnapType = 'none';
        el.scrollTop = target;
        el.style.scrollSnapType = 'y mandatory';
      } catch (_) {
        el.scrollTop = target;
      }
      geomRef.current.h = el.clientHeight;
      return;
    }
    smoothTo(el, target);
  }, [pageIndex, count, s, pageSize.id]);

  const onFeedScroll = () => {
    if (lockScroll.current) return;
    poke();
    const el = feedRef.current;
    if (!el || !el.clientHeight) return;
    const h = el.clientHeight;
    if (Math.abs(h - (geomRef.current.h || h)) > 2) {
      // Viewport geometry changed (rotate / phone-mode / keyboard): HOLD the
      // current page instead of adopting a transient neighbor mid-resize.
      geomRef.current.h = h;
      smoothTo(el, idxRef.current * h);
      return;
    }
    geomRef.current.h = h;
    // Scrolling NEVER creates pages (the fixed + button is the only creator):
    // overscroll past the end simply settles back on the last real page.
    const i = Math.round(el.scrollTop / h);
    const total = Math.max(1, count);
    const clamped = Math.max(0, Math.min(i, total - 1));
    if (clamped !== idxRef.current) setPageIndex(clamped);
    else if (i !== clamped) smoothTo(el, clamped * h);
  };

  const doZoomAt = useCallback((factor, cx, cy) => {
    const feed = feedRef.current;
    const slide = feed?.children[idxRef.current];
    const box = slide?.querySelector('.fit-box');
    if (!feed || !slide || !box) { setZoom((z) => clampZ(z * factor)); return; }
    const rect = box.getBoundingClientRect();
    const s0 = sRef.current || 1;
    const sr = slide.getBoundingClientRect();
    anchorRef.current = {
      px: (cx - rect.left) / s0, py: (cy - rect.top) / s0,
      cx, cy, sx: sr.left, sy: sr.top, i: idxRef.current,
    };
    setZoom((z) => clampZ(z * factor));
  }, []);

  // One shared pan glide: stop whatever scroll animation runs, then ease
  // this slide's scroll toward (tx,ty) and finish with a momentum tail.
  // Pass NaN/NaN with a velocity for a pure momentum glide after release.
  const stopPanGlide = () => {
    try { cancelAnimationFrame(panAnim.current); } catch (_) {}
    panAnim.current = null;
  };
  const glideScrollTo = (slide, tx, ty, momentum) => {
    stopPanGlide();
    let vx = (momentum && Number.isFinite(momentum.x)) ? momentum.x : 0;
    let vy = (momentum && Number.isFinite(momentum.y)) ? momentum.y : 0;
    const aimed = Number.isFinite(tx) && Number.isFinite(ty);
    const tick = () => {
      if (!slide || !slide.isConnected) { panAnim.current = null; return; }
      if (aimed) {
        const nx = slide.scrollLeft + (tx - slide.scrollLeft) * 0.32;
        const ny = slide.scrollTop + (ty - slide.scrollTop) * 0.32;
        slide.scrollLeft = nx;
        slide.scrollTop = ny;
        if (Math.abs(tx - nx) > 0.4 || Math.abs(ty - ny) > 0.4) {
          panAnim.current = requestAnimationFrame(tick);
          return;
        }
        slide.scrollLeft = tx;
        slide.scrollTop = ty;
      }
      vx *= 0.93;
      vy *= 0.93;
      if (Math.abs(vx) < 0.15 && Math.abs(vy) < 0.15) { panAnim.current = null; return; }
      slide.scrollLeft = slide.scrollLeft + vx;
      slide.scrollTop = slide.scrollTop + vy;
      panAnim.current = requestAnimationFrame(tick);
    };
    panAnim.current = requestAnimationFrame(tick);
  };

  useEffect(() => {
    const a = anchorRef.current;
    if (!a) return;
    anchorRef.current = null;
    if (a.i !== idxRef.current) return;
    const slide = feedRef.current?.children[a.i];
    const box = slide?.querySelector('.fit-box');
    if (!slide || !box) return;
    // Glide (never jump) the pan so the cursor/pinch point stays put.
    glideScrollTo(
      slide,
      box.offsetLeft + a.px * sRef.current - (a.cx - a.sx),
      box.offsetTop + a.py * sRef.current - (a.cy - a.sy),
      null
    );
  }, [s]);

  // Desktop wheel: one notch = exactly one page. The tick is swallowed and
  // the feed glides the FULL page (a native tick scrolls ~100px then snap
  // pulls it back, so the feed looks dead). Proven on Windows mice.
  const wheelAcc = useRef(0);
  const wheelLock = useRef(false);
  useEffect(() => {
    const el = feedRef.current;
    if (!el) return;
    const onWheel = (e) => {
      poke();
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        doZoomAt(e.deltaY < 0 ? 1.12 : 0.9, e.clientX, e.clientY);
        return;
      }
      if (zoomRef.current !== 1) return;
      e.preventDefault();
      if (wheelLock.current) return;
      const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaY;
      wheelAcc.current += dy;
      if (Math.abs(wheelAcc.current) >= 30) {
        const dir = wheelAcc.current > 0 ? 1 : -1;
        wheelAcc.current = 0;
        wheelLock.current = true;
        goTo(idxRef.current + dir);
        setTimeout(() => { wheelLock.current = false; }, 450);
      }
    };
    el.addEventListener('wheel', onWheel, { passive: false });
    return () => el.removeEventListener('wheel', onWheel);
  }, [doZoomAt, poke, goTo]);

  const touchDist = (t) => Math.hypot(t[0].clientX - t[1].clientX, t[0].clientY - t[1].clientY);
  const onTouchStart = (e) => {
    poke();
    cancelAnim();
    stopPanGlide();
    if (e.touches.length === 2) {
      pinchRef.current = { d: touchDist(e.touches), z: zoomRef.current };
      touchY.current = null;
      setPinching(true); // zoom follows the fingers 1:1 (no transition lag)
    } else if (e.touches.length === 1) {
      touchY.current = e.touches[0].clientY;
    }
  };
  const pinchRaf = useRef(null);
  const onTouchMove = (e) => {
    const p = pinchRef.current;
    if (e.touches.length === 2 && p && p.d > 0) {
      // rAF-throttled pinch: one zoom set per frame, no jank on phones.
      const next = clampZ(p.z * touchDist(e.touches) / p.d);
      if (pinchRaf.current) return;
      pinchRaf.current = requestAnimationFrame(() => {
        pinchRaf.current = null;
        setZoom(next);
      });
    }
  };
  const onTouchEnd = (e) => {
    if (pinchRef.current) {
      if (e.touches.length < 2) {
        pinchRef.current = null;
        setPinching(false);
      }
      return;
    }
    if (touchY.current == null) return;
    if (zoomRef.current !== 1) { touchY.current = null; return; }
    const dy = touchY.current - e.changedTouches[0].clientY;
    touchY.current = null;
    // Lighter flick (35px): phones flip pages with a short swipe.
    if (Math.abs(dy) < 35) return;
    if (dy > 0) goTo(idxRef.current + 1);
    else goTo(idxRef.current - 1);
  };

  // Pan anywhere while zoomed: the scroll eases toward the drag target every
  // frame (buttery follow in any direction), tracks flick velocity, and
  // glides on with momentum after release.
  const onBoxPointerDown = (e) => {
    if (zoomRef.current <= 1 || e.pointerType === 'touch') return;
    stopPanGlide();
    const slide = e.currentTarget.parentElement;
    panRef.current = {
      slide, x: e.clientX, y: e.clientY,
      sl: slide.scrollLeft, st: slide.scrollTop,
      tx: slide.scrollLeft, ty: slide.scrollTop,
      lx: e.clientX, ly: e.clientY, t: performance.now(), vx: 0, vy: 0,
    };
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (_) {}
    setPanning(true);
    const follow = () => {
      const p = panRef.current;
      if (!p || !p.slide || !p.slide.isConnected) { panAnim.current = null; return; }
      p.slide.scrollLeft = p.slide.scrollLeft + (p.tx - p.slide.scrollLeft) * 0.32;
      p.slide.scrollTop = p.slide.scrollTop + (p.ty - p.slide.scrollTop) * 0.32;
      panAnim.current = requestAnimationFrame(follow);
    };
    panAnim.current = requestAnimationFrame(follow);
  };
  const onBoxPointerMove = (e) => {
    const p = panRef.current;
    if (!p) return;
    p.tx = p.sl - (e.clientX - p.x);
    p.ty = p.st - (e.clientY - p.y);
    const now = performance.now();
    const dt = Math.max(1, now - p.t);
    const ivx = ((p.lx - e.clientX) / dt) * 16.7;
    const ivy = ((p.ly - e.clientY) / dt) * 16.7;
    p.vx = p.vx * 0.65 + ivx * 0.35;
    p.vy = p.vy * 0.65 + ivy * 0.35;
    p.lx = e.clientX;
    p.ly = e.clientY;
    p.t = now;
  };
  const onBoxPointerUp = () => {
    const p = panRef.current;
    panRef.current = null;
    setPanning(false);
    if (p && p.slide && p.slide.isConnected && (Math.abs(p.vx) > 1 || Math.abs(p.vy) > 1)) {
      // Flick release: glide on with the tracked velocity (capped).
      glideScrollTo(p.slide, NaN, NaN, {
        x: Math.max(-38, Math.min(38, p.vx)),
        y: Math.max(-38, Math.min(38, p.vy)),
      });
    } else {
      stopPanGlide();
    }
  };

  const zoomStep = (factor) => {
    const slide = feedRef.current?.children[idxRef.current];
    if (!slide) { setZoom((z) => clampZ(z * factor)); return; }
    const r = slide.getBoundingClientRect();
    doZoomAt(factor, r.left + r.width / 2, r.top + r.height / 2);
  };
  const onBoxDoubleClick = (e) => {
    if (zoomRef.current > 1.2) setZoom(1);
    else doZoomAt(2, e.clientX, e.clientY);
  };

  // Orientation follows the SERVER studio box (single source of truth).
  // Pre-request it switches the real box (the model designs for it);
  // after the first request the server rejects the switch (frozen).
  const toggleOrientation = async () => {
    if (locked) {
      showToast('Size locked ✓');
      return;
    }
    const next = pageSize.id === 'a4-portrait' ? PAGE_SIZES[1] : PAGE_SIZES[0];
    try {
      const r = await fetch('/api/studio/orientation', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          studio_id: studioId,
          orientation: next.id === 'a4-portrait' ? 'portrait' : 'landscape',
        }),
      });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) {
        showToast(String((d && d.error) || 'Size locked ✓').slice(0, 120));
        setLocked(true);
        await load();
        return;
      }
    } catch (_) {
      showToast('Size change failed');
      return;
    }
    setPageSize(next);
    setZoom(1);
    showToast(next.short);
  };
  useEffect(() => {
    const onKey = (e) => {
      if (e.key === 'ArrowDown') goTo(pageIndex + 1);
      if (e.key === 'ArrowUp') goTo(pageIndex - 1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [pageIndex, goTo]);

  const clearBusy = (n) => {
    setBusyPages((prev) => {
      if (!prev[n]) return prev;
      const next = { ...prev };
      delete next[n];
      return next;
    });
    setLeavingPages((prev) => {
      if (!prev[n]) return prev;
      const next = { ...prev };
      delete next[n];
      return next;
    });
  };

  const sendMsg = async (e, presetText) => {
    e?.preventDefault();
    const v = String(presetText !== undefined ? presetText : msg).trim();
    if (!v) return;
    const n = pageIndex + 1;
    if (isRunning(n)) return; // locked while THIS page designs (others sendable)
    setMsg('');
    if (taRef.current) taRef.current.style.height = 'auto';
    // The request itself is never shown: only the black loading template
    // runs, plus the STOP mark while the turn executes. `v` is kept so any
    // failure hands it back to the composer instead of losing it.
    armSend(n);
    try {
      const r = await fetch('/api/studio/chat', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, page_no: n, message: v }),
      }).catch(() => null);
      const data = r ? await r.json().catch(() => ({})) : {};
      await finishTurn(n, data, v);
    } catch (_) {
      setStopping(false);
      if (v) {
        setMsg(v);
        setTimeout(autoGrow, 0);
      }
      showToast('Studio request failed — try again');
      clearBusy(n);
      return;
    }
    // No short clear here: the black template stays until the design lands
    // (server designing flag) or the turn ends empty (poll frees the mark).
  };

  const isRunning = (n) => !!(busyPages[n] || designingPages.indexOf(n) >= 0);

  // Shared turn ending: stopped -> Continue pill; failed -> loading hides,
  // the error shows, and the sent text is handed BACK to the composer so
  // nothing the user typed is ever lost. A turn that never started (network
  // or HTTP error page) frees everything the same way instead of hanging.
  const finishTurn = async (n, data, sentText) => {
    setStopping(false);
    const giveBack = () => {
      if (String(sentText || '').trim()) {
        setMsg(String(sentText || ''));
        setTimeout(autoGrow, 0);
      }
    };
    if (!data || (data.stopped !== true && data.ok !== true && data.ok !== false)) {
      giveBack();
      showToast('Studio request failed — try again');
      clearBusy(n);
      await load();
      return;
    }
    await load();
    if (data.stopped === true) {
      showToast('Stopped');
      setContinueStored((prev) => ({ ...prev, [n]: true }));
      clearBusy(n);
      return;
    }
    if (data.ok === false) {
      giveBack();
      showToast(`Design failed: ${String(data.error || 'unknown error').slice(0, 120)}`);
      clearBusy(n);
    }
  };

  // Arm one send: fresh optimistic mark + orientation lock + safety cap.
  // The SERVER designing flag keeps the loading template alive past the
  // cap; the cap only frees the LOCAL mark, never the truth.
  const armSend = (n) => {
    setContinueStored((prev) => {
      if (!prev[n]) return prev;
      const next = { ...prev };
      delete next[n];
      return next;
    });
    emptyEndRef.current[n] = false;
    sendHashRef.current[n] = hashStr(codes[n] || '');
    setBusyPages((prev) => ({ ...prev, [n]: true }));
    setLocked(true);
    setTimeout(() => clearBusy(n), 1800000);
  };

  // STOP halts this page's turn (server breaks at once, flag drops).
  // Per-page cancel: other slides' parallel turns keep running.
  // Shows a loading animation until the halt actually lands.
  const stopStudio = async () => {
    setStopping(true);
    try {
      await fetch('/api/studio/cancel', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, page_no: pageIndex + 1 }),
      }).catch(() => null);
    } catch (_) {}
    // Safety: if the turn already ended elsewhere, drop the animation.
    setTimeout(() => setStopping(false), 15000);
  };

  // Continue a stopped task: the server re-runs the last request as a new
  // turn — delivered slides persist on disk, so work resumes, never restarts.
  const continueTurn = async () => {
    const n = pageIndex + 1;
    if (isRunning(n)) return;
    armSend(n);
    try {
      const r = await fetch('/api/studio/continue', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, page_no: n }),
      }).catch(() => null);
      const data = r ? await r.json().catch(() => ({})) : {};
      await finishTurn(n, data);
    } catch (_) {
      setStopping(false);
      showToast('Continue failed');
      clearBusy(n);
    }
  };

  /* Real uploads: images + font files only, straight into studio assets. */
  const uploadFile = (kind, file) => {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const r = await fetch('/api/studio/upload', {
          method: 'POST', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ studio_id: studioId, kind, name: file.name, data: String(reader.result || '') }),
        });
        const data = await r.json().catch(() => ({}));
        if (data && data.success) {
          showToast(`${data.file} saved ✓`);
          await load();
        } else {
          showToast((data && data.error) || 'Upload rejected');
        }
      } catch (_) {
        showToast('Upload failed');
      }
    };
    reader.onerror = () => showToast('Upload failed');
    reader.readAsDataURL(file);
  };

  /* Voice exactly like the main agent: STT gate, then the recording template. */
  const fmtTime = (ms) => {
    const s = Math.floor(ms / 1000);
    return `${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
  };
  const stopBars = () => {
    const R = recRef.current;
    try { R.raf && cancelAnimationFrame(R.raf); } catch (_) {}
    R.raf = null;
    try { R.ctx && R.ctx.close().catch(() => {}); } catch (_) {}
    R.ctx = null;
  };
  const startBars = (stream) => {
    const R = recRef.current;
    try {
      stopBars();
      const AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      R.ctx = new AC();
      const src = R.ctx.createMediaStreamSource(stream);
      const an = R.ctx.createAnalyser();
      an.fftSize = 256;
      an.smoothingTimeConstant = 0.55;
      src.connect(an);
      const COUNT = 28;
      R.bars = new Array(COUNT).fill(4);
      const freq = new Uint8Array(an.frequencyBinCount);
      const time = new Uint8Array(an.fftSize);
      const loop = () => {
        if (!R.stream) return;
        an.getByteTimeDomainData(time);
        let sum = 0;
        for (let i = 0; i < time.length; i++) {
          const v = (time[i] - 128) / 128;
          sum += v * v;
        }
        const level = Math.min(1, Math.max(0, (Math.sqrt(sum / time.length) - 0.015) / 0.32));
        an.getByteFrequencyData(freq);
        const els = document.querySelectorAll('#stVoiceWave .voice-bar');
        R.bars = R.bars.map((cur, i) => {
          const band = freq[Math.floor((i / R.bars.length) * freq.length * 0.7)] || 0;
          const target = 4 + level * (6 + (band / 255) * 30);
          return target > cur ? target : cur + (target - cur) * 0.35;
        });
        els.forEach((sp, i) => { sp.style.height = `${Math.round(R.bars[i] || 4)}px`; });
        R.raf = requestAnimationFrame(loop);
      };
      loop();
    } catch (_) { stopBars(); }
  };
  const hideVoice = () => {
    const R = recRef.current;
    if (R.timer) { clearInterval(R.timer); R.timer = null; }
    stopBars();
    setTranscribing(false);
    setRecording(false);
  };
  const ensureSttThen = async (fn) => {
    // STT gate: no voice model -> route to the model-connect template.
    try {
      const res = await fetch('/api/ai_models', { credentials: 'same-origin' });
      if (res.ok) {
        const data = await res.json().catch(() => null);
        const models = data && data.models ? data.models : data;
        const stt = models ? models.stt || models.STT : null;
        if (stt && (stt.configured || stt.model_name || stt.connected)) {
          fn();
          return;
        }
      } else {
        fn();
        return;
      }
    } catch (_) {
      fn();
      return;
    }
    showToast('Connect a voice-to-text model first');
    try {
      window.top.postMessage('x5:open-ai-models-stt', '*');
    } catch (_) {}
  };
  const startVoice = async () => {
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      showToast('Voice input not supported here');
      return;
    }
    const R = recRef.current;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mime = ['audio/webm', 'audio/mp4', 'audio/ogg'].find((m) => {
        try { return MediaRecorder.isTypeSupported(m); } catch (_) { return false; }
      }) || '';
      R.rec = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
      R.stream = stream;
      R.chunks = [];
      R.discarded = false;
      R.sendAfter = false;
      R.rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) R.chunks.push(ev.data); };
      R.rec.onstop = () => {
        try { stream.getTracks().forEach((t) => t.stop()); } catch (_) {}
        R.stream = null;
        finishVoice();
      };
      R.rec.start();
      setRecording(true);
      setRecLabel('Recording…');
      R.t0 = Date.now();
      if (R.timer) clearInterval(R.timer);
      R.timer = setInterval(() => {
        const el = document.getElementById('stVoiceTimer');
        if (el) el.textContent = fmtTime(Date.now() - R.t0);
      }, 250);
      startBars(stream);
      if (R.stopTimer) clearTimeout(R.stopTimer);
      R.stopTimer = setTimeout(() => stopVoice(false), 60000);
    } catch (_) {
      stopBars();
      hideVoice();
      showToast('Mic unavailable');
    }
  };
  const stopVoice = (sendAfter) => {
    const R = recRef.current;
    if (R.stopTimer) { clearTimeout(R.stopTimer); R.stopTimer = null; }
    R.sendAfter = !!sendAfter;
    if (R.rec && R.stream) {
      try { R.rec.stop(); } catch (_) {}
    } else {
      hideVoice();
    }
  };
  const cancelVoice = () => {
    const R = recRef.current;
    R.discarded = true;
    R.sendAfter = false;
    if (R.stopTimer) { clearTimeout(R.stopTimer); R.stopTimer = null; }
    const doomed = R.stream;
    R.stream = null;
    if (R.rec && doomed) {
      const rec = R.rec;
      rec.onstop = () => {
        try { doomed.getTracks().forEach((t) => t.stop()); } catch (_) {}
      };
      try { rec.stop(); } catch (_) {
        try { doomed.getTracks().forEach((t) => t.stop()); } catch (_) {}
      }
    } else if (doomed) {
      try { doomed.getTracks().forEach((t) => t.stop()); } catch (_) {}
    }
    R.chunks = [];
    stopBars();
    hideVoice();
    if (taRef.current) taRef.current.focus();
  };
  const finishVoice = async () => {
    const R = recRef.current;
    if (R.discarded) { R.discarded = false; R.sendAfter = false; return; }
    setTranscribing(true);
    setRecLabel('Transcribing…');
    if (R.timer) { clearInterval(R.timer); R.timer = null; }
    try {
      const blob = new Blob(R.chunks, { type: (R.rec && R.rec.mimeType) || 'audio/webm' });
      R.chunks = [];
      if (!blob.size) throw new Error('Empty recording');
      const dataUrl = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(String(reader.result || ''));
        reader.onerror = () => reject(new Error('read failed'));
        reader.readAsDataURL(blob);
      });
      const res = await fetch('/api/stt', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ audio: dataUrl }),
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
      const text = String((data && data.text) || '').trim().slice(0, 4000);
      hideVoice();
      if (!text) {
        showToast('No speech detected');
      } else if (R.sendAfter) {
        R.sendAfter = false;
        sendMsg(null, text);
      } else {
        setMsg((prev) => `${prev} ${text}`.trim().slice(0, 4000));
        setTimeout(autoGrow, 0);
        if (taRef.current) taRef.current.focus();
      }
    } catch (err) {
      hideVoice();
      showToast(err instanceof Error ? err.message : 'Voice failed');
    } finally {
      R.sendAfter = false;
      stopBars();
    }
  };

  /* Undo/redo one delivered state on the current page: the current design
     fades OUT smoothly, then the stepped version fades IN smoothly and
     settles — opacity only, no sliding, no flicker. The step REALLY rewrites
     the slide file on the server, so any edit sent afterwards applies to the
     version on screen (reads always load the current file). Stepping is
     locked while ITS page generates. */
  const stepHistory = async (direction) => {
    const n = pageIndex + 1;
    if (isRunning(n) || stepDir[n]) return; // one step at a time per page
    const clearStepDir = () => {
      setStepDir((prev) => {
        if (!prev[n]) return prev;
        const next = { ...prev };
        delete next[n];
        return next;
      });
    };
    // Phase 1: current design fades out smoothly (220ms).
    setStepDir((prev) => ({ ...prev, [n]: 'leaving' }));
    await new Promise((r) => setTimeout(r, 230));
    try {
      const r = await fetch('/api/studio/history', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, page_no: n, direction }),
      });
      const data = await r.json().catch(() => ({}));
      if (data && data.success) {
        // Silent by design: the stepped code swaps in underneath, then
        // fades in smoothly and settles — no toast needed.
        if (data.history) setHistCounts(data.history);
        await fetchCode(n);
        await load();
        // Phase 2: stepped version fades in smoothly.
        setStepDir((prev) => ({ ...prev, [n]: 'arrived' }));
      } else {
        clearStepDir();
        showToast((data && data.error) || 'Nothing to step');
        return;
      }
    } catch (_) {
      clearStepDir();
      showToast('History failed');
      return;
    }
    // Let the fade-in settle before clearing its direction.
    setTimeout(clearStepDir, 450);
  };

  /* Delete ONE slide entirely (later slides shift up to its number).
     Blank slides delete at once; designed slides ask first (no undo).
     Locked while ANY turn runs — renumbering mid-turn would corrupt it. */
  const [confirmDelete, setConfirmDelete] = useState(null); // {page_no} or null
  const [moreOpen, setMoreOpen] = useState(false); // phone ⋯ panel
  const studioBusy = () => (
    designingPages.length > 0 || Object.keys(busyPages).length > 0
  );
  // Inline rename: double-click the PDF name -> edit -> auto-saves.
  const startTitleEdit = () => {
    setDraftTitle(String(studioTitle || ''));
    setEditingTitle(true);
  };
  const submitTitleRename = async () => {
    const name = String(draftTitle || '').trim().slice(0, 80);
    setEditingTitle(false);
    if (!name || name === String(studioTitle || '')) return;
    try {
      const r = await fetch('/api/studio/rename', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, title: name }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data || data.success !== true) {
        showToast(String((data && data.error) || `HTTP ${r.status}`).slice(0, 120));
        return;
      }
      setStudioTitle(String(data.title || name));
      try {
        const prev = readCache();
        writeCache({ ...prev, title: String(data.title || name) });
      } catch (_) {}
      showToast('Renamed ✓');
    } catch (_) {
      showToast('Rename failed');
    }
  };
  // Phones have no double-click: a single tap on the name edits it there
  // (desktops keep double-click so single clicks never edit by accident).
  const startTitleEditTouch = () => {
    try {
      if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) startTitleEdit();
    } catch (_) {}
  };
  const askDeletePage = () => {
    const n = pageIndex + 1;
    if (studioBusy()) {
      showToast('Wait for generation to finish');
      return;
    }
    if (count <= 1) {
      showToast('Cannot delete the only page');
      return;
    }
    if (!codes[n]) {
      doDeletePage(n);
      return;
    }
    setConfirmDelete({ page_no: n });
  };
  const doDeletePage = async (n) => {
    setConfirmDelete(null);
    setMoreOpen(false);
    if (studioBusy()) {
      showToast('Wait for generation to finish');
      return;
    }
    try {
      const r = await fetch('/api/studio/delete_page', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ studio_id: studioId, page_no: n }),
      });
      const data = await r.json().catch(() => ({}));
      if (!r.ok || !data || data.success !== true) {
        showToast(`Delete failed: ${String((data && data.error) || `HTTP ${r.status}`).slice(0, 120)}`);
        return;
      }
      const total = Math.max(1, Number(data.pages) || 0);
      if (data.history) setHistCounts(data.history);
      // Page numbers shifted: drop stale code + per-page marks, refetch fresh.
      setCodes({});
      emptyEndRef.current = {};
      setContinueStored((prev) => {
        const next = {};
        for (const k of Object.keys(prev || {})) {
          const p = Number(k);
          if (!Number.isInteger(p)) continue;
          if (p === n) continue;
          next[p > n ? p - 1 : p] = true;
        }
        return next;
      });
      setCount(total);
      if (n > total) setPageIndex(Math.max(0, total - 1));
      await load();
      for (let k = 1; k <= total; k++) fetchCode(k);
      showToast(`Slide ${n} deleted`);
    } catch (_) {
      showToast('Delete failed');
    }
  };

  // Menus (+ attach, ⋯ more) close with their exit animation when tapping
  // outside them — same as the main template.
  useEffect(() => {
    if (!attachOpen && !moreOpen) return undefined;
    const onDown = (e) => {
      try {
        const t = e.target;
        if (t && t.closest && (t.closest('.attach-wrap') || t.closest('.more-pop') || t.closest('.tb-more'))) return;
      } catch (_) {}
      setAttachOpen(false);
      setMoreOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [attachOpen, moreOpen]);

  /* Render ONE page to PNG, bounded: a hanging font host aborts instead of
     freezing the export. withFonts=false skips webfont embedding (system
     fallback) for the retry pass. toPng is handed in because the export stack
     loads on demand (see loadExportLibs). */
  const renderPagePng = (toPng, node, w, h, bg, withFonts) => {
    const ctl = new AbortController();
    expAbortRef.current = ctl;
    const timer = setTimeout(() => { try { ctl.abort(); } catch (_) {} }, RENDER_TIMEOUT_MS);
    const opts = {
      pixelRatio: 3, cacheBust: true, backgroundColor: bg,
      width: w, height: h, style: { transform: 'none', margin: '0' },
      fetchRequestInit: { signal: ctl.signal },
    };
    if (!withFonts) opts.skipFonts = true;
    return toPng(node, opts).finally(() => {
      clearTimeout(timer);
      if (expAbortRef.current === ctl) expAbortRef.current = null;
    });
  };

  // Cancel is INSTANT: the overlay vanishes at once and the running loop is
  // superseded (it exits silently at its next check and can never save).
  const cancelExport = () => {
    expGenRef.current += 1;
    expCancelRef.current = true;
    try { expAbortRef.current && expAbortRef.current.abort(); } catch (_) {}
    setExpPct(-1);
    setExpStep({ done: 0, total: 0 });
    showToast('Export cancelled');
  };

  /* Export like pdf motion: every .pdf-page -> PNG at real size -> one PDF.
     REAL progress (page done/total + 0→100), bounded renders, surfaced
     errors (never a silent hang), font-fallback retry, and a cancel mark. */
  const exportPDF = async () => {
    if (expPct >= 0) return;
    // This run's generation: any cancel/restart supersedes it, so a late
    // render can never pop a file dialog after the user cancelled.
    const gen = expGenRef.current + 1;
    expGenRef.current = gen;
    const alive = () => gen === expGenRef.current && !expCancelRef.current;
    try {
      await waitFontsReady();
      if (!alive()) return;
      const nodes = Array.from(feedRef.current?.querySelectorAll('.pdf-page') || []);
      if (!nodes.length) { showToast('No pages to export'); return; }
      expCancelRef.current = false;
      setExpPct(0);
      setExpStep({ done: 0, total: nodes.length });
      // Export-only libraries load here, with the overlay already up so the
      // wait is visible; a failure is surfaced by the catch below.
      const { toPng, jsPDF } = await loadExportLibs();
      if (!alive()) return;
      let pdf = null;
      let skipped = 0;
      let degraded = 0; // pages rendered with system-font fallback
      let firstProblem = '';
      for (let i = 0; i < nodes.length; i++) {
        if (!alive()) return;
        setExpStep({ done: i, total: nodes.length });
        const node = nodes[i];
        const w = node.offsetWidth;
        const h = node.offsetHeight;
        const bg = getComputedStyle(node).backgroundColor;
        let dataUrl = null;
        try {
          dataUrl = await renderPagePng(toPng, node, w, h, bg, true);
        } catch (e1) {
          if (!alive()) return;
          // Webfont hosts unreachable (gstatic timeouts): retry WITHOUT
          // embedded fonts so the page still exports with system fonts
          // instead of killing the whole file.
          try {
            dataUrl = await renderPagePng(toPng, node, w, h, bg, false);
            degraded += 1;
          } catch (e2) {
            skipped += 1;
            if (!firstProblem) {
              firstProblem = String((e2 && e2.message) || (e1 && e1.message) || 'render failed')
                .replace(/^AbortError.*$/i, 'page render timed out (fonts unreachable?)')
                .slice(0, 120);
            }
            setExpPct(Math.round(((i + 1) / nodes.length) * 100));
            setExpStep({ done: i + 1, total: nodes.length });
            continue;
          }
        }
        if (!alive()) return;
        try {
          const orientation = h >= w ? 'portrait' : 'landscape';
          const format = [w * PX_TO_MM, h * PX_TO_MM];
          if (!pdf) pdf = new jsPDF({ orientation, unit: 'mm', format, compress: true });
          else pdf.addPage(format, orientation);
          pdf.addImage(dataUrl, 'PNG', 0, 0, format[0], format[1]);
        } catch (e3) {
          skipped += 1;
          if (!firstProblem) {
            firstProblem = String((e3 && e3.message) || 'PDF page failed').slice(0, 120);
          }
        }
        setExpPct(Math.round(((i + 1) / nodes.length) * 100));
        setExpStep({ done: i + 1, total: nodes.length });
      }
      // Superseded (cancelled/restarted): exit silently — cancelExport
      // already hid the overlay and toasted, so nothing pops up late.
      if (!alive()) return;
      if (!pdf) throw new Error(firstProblem || 'no pages rendered');
      pdf.save(`${studioTitle || studioId}.pdf`);
      const notes = [];
      if (skipped) notes.push(`${skipped} page${skipped === 1 ? '' : 's'} skipped${firstProblem ? `: ${firstProblem}` : ''}`);
      if (degraded) notes.push(`${degraded} page${degraded === 1 ? '' : 's'} used system fonts (webfonts unreachable)`);
      showToast(notes.length ? `PDF saved ✓ (${notes.join('; ')})` : 'PDF saved ✓');
    } catch (e) {
      // Surfaced, never a silent hang: the toast tells what broke.
      showToast(`Export failed: ${String((e && e.message) || e || 'unknown error').slice(0, 140)}`);
    } finally {
      expCancelRef.current = false;
      // Only the current generation resets the overlay: a cancelled run
      // already hid it instantly, and a stale loop must not touch it.
      if (gen === expGenRef.current) {
        setTimeout(() => {
          if (gen === expGenRef.current) { setExpPct(-1); setExpStep({ done: 0, total: 0 }); }
        }, 600);
      }
    }
  };

  // Exactly the real pages — no trailing auto-created blank (the + button
  // is the only creator). Minimum one page so the studio never looks empty.
  const slides = Array.from({ length: Math.max(1, count) }, (_, i) => i);

  const blankPageStyle = {
    width: pageSize.w,
    height: pageSize.h,
    minWidth: pageSize.w,
    minHeight: pageSize.h,
    maxWidth: pageSize.w,
    maxHeight: pageSize.h,
    overflow: 'hidden',
    boxSizing: 'border-box',
    background: '#ffffff',
    color: '#17231c',
    fontFamily: '"IBM Plex Sans Arabic", "Noto Sans Arabic", Tahoma, sans-serif',
    lineHeight: 1.45,
    paddingInline: 72,
    paddingBlockStart: 56,
    paddingBlockEnd: 18,
    borderRadius: 10,
    display: 'flex',
    flexDirection: 'column',
    breakAfter: 'page',
    breakInside: 'avoid',
    textAlign: 'start',
  };

  if (missing) {
    return (
      <div className="studio">
        <div className="st-missing">
          <div className="st-missing-box">
            <div className="st-missing-title">Studio not found</div>
            <div className="st-missing-text">This studio ({studioId}) is no longer available. Studios are never deleted with the chat — this one was removed separately. Create a new PDF to start a fresh studio.</div>
            <button type="button" className="st-missing-btn" onClick={closeToChat}>Back to chat</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="studio">
      <header className="toolbar">
        <div className="tb-mark"><span className="tb-square" /></div>
        <div className="tb-group">
          <button className="tb-pdf" onClick={exportPDF} title="Export PDF">
            <span className="tb-pdf-icon">{I.pdf}</span>
            <span className="tb-pdf-word">PDF</span>
          </button>
          <span className="tb-sep" />
          <button className="tb-icon tb-nav" title="Previous page" onClick={() => goTo(pageIndex - 1)}>{I.up}</button>
          <button className="tb-icon tb-nav" title="Next page" onClick={() => goTo(pageIndex + 1)}>{I.down}</button>
          <button type="button" className="tb-add" title="Add new page" aria-label="Add new page" onClick={addPage}>{I.plus}</button>
          <span className="tb-sep" />
          <button className={'tb-icon tb-step' + (((histCounts[String(pageIndex + 1)] || {}).undo > 0) && !stepDir[pageIndex + 1] && !isRunning(pageIndex + 1) ? '' : ' is-disabled')} title={isRunning(pageIndex + 1) ? 'Locked while this page generates' : 'Step back'} disabled={!((histCounts[String(pageIndex + 1)] || {}).undo > 0) || !!stepDir[pageIndex + 1] || isRunning(pageIndex + 1)} onClick={() => stepHistory('undo')}>{I.undo}</button>
          <button className={'tb-icon tb-step' + (((histCounts[String(pageIndex + 1)] || {}).redo > 0) && !stepDir[pageIndex + 1] && !isRunning(pageIndex + 1) ? '' : ' is-disabled')} title={isRunning(pageIndex + 1) ? 'Locked while this page generates' : 'Step forward'} disabled={!((histCounts[String(pageIndex + 1)] || {}).redo > 0) || !!stepDir[pageIndex + 1] || isRunning(pageIndex + 1)} onClick={() => stepHistory('redo')}>{I.redo}</button>
          <button className={'tb-icon tb-del' + (studioBusy() ? ' is-disabled' : '')} title="Delete this slide" disabled={studioBusy()} onClick={askDeletePage}>{I.trash}</button>
          <span className="tb-sep" />
          <div className="tb-size-wrap">
            <button className={'tb-icon' + (locked ? ' is-disabled' : '')} title={locked ? 'Size locked after the first request' : 'Toggle portrait / landscape'} disabled={locked} onClick={() => { if (locked) { showToast('Size locked ✓'); return; } toggleOrientation(); }}>{I.size}</button>
            <span className="tb-size-label">{pageSize.short}</span>
          </div>
          <span className="tb-sep" />
          <div className="tb-zoom" title="Zoom">
            <button className="tb-icon tb-zoom-btn" onClick={() => zoomStep(0.8)} aria-label="Zoom out">−</button>
            <span className="tb-zoom-val" onClick={() => setZoom(1)} title="Reset zoom">{Math.round(zoom * 100)}%</span>
            <button className="tb-icon tb-zoom-btn" onClick={() => zoomStep(1.25)} aria-label="Zoom in">+</button>
            <button className="tb-icon tb-zoom-btn" onClick={() => setZoom(1)} aria-label="Reset to actual size" title="Reset to actual size">{I.reset}</button>
          </div>
          <button type="button" className={'tb-icon tb-more' + (moreOpen ? ' is-active' : '')} aria-label="More options" title="More options" onClick={() => setMoreOpen((o) => !o)}>{I.dots}</button>
        </div>
        <div className="tb-right">
          {editingTitle ? (
            <input className="tb-title-edit" dir="auto" value={draftTitle} maxLength={80} autoFocus
              aria-label="Rename studio"
              onChange={(e) => setDraftTitle(e.target.value)}
              onBlur={submitTitleRename}
              onKeyDown={(e) => { if (e.key === 'Enter') submitTitleRename(); if (e.key === 'Escape') setEditingTitle(false); }} />
          ) : (
            <span className="tb-title" dir="auto" title="Double-click to rename (tap on phones)" onClick={startTitleEditTouch} onDoubleClick={startTitleEdit}>{studioTitle || studioId}</span>
          )}
          <span className="tb-count" title="Current page / total pages">{Math.min(pageIndex + 1, Math.max(1, count))} / {Math.max(1, count)}</span>
        </div>
      </header>
      <div className={'more-pop' + (moreOpen ? ' open' : '')} role="menu" aria-label="More options" aria-hidden={!moreOpen}>
        <button type="button" className="tb-icon" title="Previous page" aria-label="Previous page" onClick={() => { setMoreOpen(false); goTo(pageIndex - 1); }}>{I.up}</button>
        <button type="button" className="tb-icon" title="Next page" aria-label="Next page" onClick={() => { setMoreOpen(false); goTo(pageIndex + 1); }}>{I.down}</button>
        <button type="button" className={'tb-icon tb-step' + (((histCounts[String(pageIndex + 1)] || {}).undo > 0) && !stepDir[pageIndex + 1] && !isRunning(pageIndex + 1) ? '' : ' is-disabled')} title="Step back" aria-label="Step back" disabled={!((histCounts[String(pageIndex + 1)] || {}).undo > 0) || !!stepDir[pageIndex + 1] || isRunning(pageIndex + 1)} onClick={() => { setMoreOpen(false); stepHistory('undo'); }}>{I.undo}</button>
        <button type="button" className={'tb-icon tb-step' + (((histCounts[String(pageIndex + 1)] || {}).redo > 0) && !stepDir[pageIndex + 1] && !isRunning(pageIndex + 1) ? '' : ' is-disabled')} title="Step forward" aria-label="Step forward" disabled={!((histCounts[String(pageIndex + 1)] || {}).redo > 0) || !!stepDir[pageIndex + 1] || isRunning(pageIndex + 1)} onClick={() => { setMoreOpen(false); stepHistory('redo'); }}>{I.redo}</button>
        <button type="button" className={'tb-icon' + (locked ? ' is-disabled' : '')} title={locked ? 'Size locked' : 'Toggle portrait / landscape'} aria-label="Toggle orientation" disabled={locked} onClick={() => { setMoreOpen(false); toggleOrientation(); }}>{I.size}</button>
        <button type="button" className="tb-icon" title={`Reset zoom (now ${Math.round(zoom * 100)}%)`} aria-label="Reset zoom" onClick={() => { setMoreOpen(false); setZoom(1); }}>{I.reset}</button>
        <button type="button" className={'tb-icon tb-del' + (studioBusy() ? ' is-disabled' : '')} title="Delete this slide" aria-label="Delete this slide" disabled={studioBusy()} onClick={() => { askDeletePage(); setMoreOpen(false); }}>{I.trash}</button>
      </div>

      <main className="canvas canvas-dark">
        <div
          className="feed"
          ref={feedRef}
          onScroll={onFeedScroll}
          onMouseMove={poke}
          onTouchStart={onTouchStart}
          onTouchMove={onTouchMove}
          onTouchEnd={onTouchEnd}
        >
          {slides.map((i) => {
            const n = i + 1;
            const code = codes[n];
            // Server designing flag is the truth across reloads; the local
            // flag only covers the instant right after send. pending also
            // covers has_code-but-not-streamed-yet so the box never flashes
            // white while its design travels over the network.
            const running = !!busyPages[n] || designingPages.indexOf(n) >= 0;
            // !entered covers the entry moment before the first load lands
            // (old caches carry no flags): dark box instead of a white sheet.
            // A design that arrived before the compiler did keeps the very
            // same black template, so the box is never painted empty.
            const pending = !code
              ? (!entered || !!knownCoded[n] || running)
              : babelState !== 'ready';
            const leaving = !!leavingPages[n];
            return (
              <section className="feed-slide" key={i} style={{ paddingTop: 8, paddingBottom: agentH + 8 }}>
                <div dir="ltr" className={'fit-box' + (zoom > 1 ? ' is-zoomed' : '') + (panning ? ' is-panning' : '') + (running ? ' is-running' : '')}
                  style={{ width: pageSize.w * s, height: pageSize.h * s }}
                  onPointerDown={onBoxPointerDown}
                  onPointerMove={onBoxPointerMove}
                  onPointerUp={onBoxPointerUp}
                  onPointerCancel={onBoxPointerUp}
                  onDoubleClick={onBoxDoubleClick}
                >
                  <div className={'fit-inner' + (pinching ? ' is-live' : '')} style={{ width: pageSize.w, height: pageSize.h, transform: `scale(${s})` }}>
                    {code ? (
                      <div className={'pdf-page live' + (stepDir[n] === 'leaving' ? ' is-leaving' : stepDir[n] ? ' is-arrived' : '')} data-page={n} style={{ width: pageSize.w, height: pageSize.h }}>
                        <LivePage code={code} ckey={`${n}:${hashStr(code)}`} onError={(e) => reportError(n, e)} />
                      </div>
                    ) : pending ? (
                      leaving ? (
                        <div className="st-black is-leaving"><Lights /></div>
                      ) : (
                        <PendingLights key={`pending-${n}`} />
                      )
                    ) : (
                      <section data-page={n} className="pdf-page ds-nobreak" dir="rtl" style={blankPageStyle}>
                        <div style={{ flex: '1 1 0%', display: 'flex', flexDirection: 'column', minHeight: 0 }} />
                      </section>
                    )}
                    {running && code && (
                      <div className={'st-black is-over' + (leaving ? ' is-leaving' : '')}><Lights /></div>
                    )}
                  </div>
                </div>
              </section>
            );
          })}
        </div>

        {/* Prev/next float OVER the page (fixed to the screen, never scaled by
            zoom; auto-hide when idle, desktop only — phones swipe instead). */}
        <button className={'nav-arrow nav-up' + (calm ? ' is-hidden' : '')} onClick={() => goTo(pageIndex - 1)} aria-label="Previous page" title="Previous page">{I.up}</button>
        <button className={'nav-arrow nav-down' + (calm ? ' is-hidden' : '')} onClick={() => goTo(pageIndex + 1)} aria-label="Next page" title="Next page">{I.down}</button>

        {expPct >= 0 && (
          <div className="exp-ov" role="status" aria-live="polite">
            <div className="exp-box">
              <div className="exp-pct">{expPct}%</div>
              <div className="exp-bar"><div className="exp-fill" style={{ width: `${expPct}%` }} /></div>
              <div className="exp-label">
                {expStep.total > 0
                  ? `Rendering page ${Math.min(expStep.done + 1, expStep.total)} of ${expStep.total}…`
                  : 'Rendering pages…'}
              </div>
              <button type="button" className="exp-cancel" onClick={cancelExport} aria-label="Cancel export">
                Cancel
              </button>
            </div>
          </div>
        )}
      </main>

      <div className="agent-float" ref={agentRef}>
        <div className="composer-switch">
          <form className={'composer' + (recording || transcribing ? ' is-hidden' : '')} onSubmit={sendMsg} autoComplete="off">
            <div className="composer-attachments"></div>
            <div className="attach-wrap">
              <button type="button" className={'composer-btn attach-btn' + (attachOpen ? ' active' : '')} aria-label="Attach image or font" onClick={() => setAttachOpen((o) => !o)}>{I.plus}</button>
              <div className={'attach-menu' + (attachOpen ? ' open' : '')} aria-hidden={!attachOpen}>
                  <button type="button" onClick={() => { setAttachOpen(false); if (imgInputRef.current) imgInputRef.current.click(); }}>
                    <span className="opt-tile" aria-hidden="true">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="4" width="18" height="16" rx="3" /><circle cx="9" cy="10" r="1.8" /><path d="m5.5 18 5-5 3.5 3.5 2-2 3 3" /></svg>
                    </span>
                    <span className="opt-text"><b>Image</b><i>PNG, JPG, WebP, GIF</i></span>
                  </button>
                  <button type="button" onClick={() => { setAttachOpen(false); if (fontInputRef.current) fontInputRef.current.click(); }}>
                    <span className="opt-tile" aria-hidden="true">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M5 20h14" /><path d="M12 4v16" /><path d="M8 8h8" /></svg>
                    </span>
                    <span className="opt-text"><b>Fonts</b><i>TTF, OTF, WOFF</i></span>
                  </button>
                </div>
              <input ref={imgInputRef} type="file" accept="image/png,image/jpeg,image/webp,image/gif" hidden onChange={(e) => { const f = e.target.files && e.target.files[0]; e.target.value = ''; if (f) uploadFile('image', f); }} />
              <input ref={fontInputRef} type="file" accept=".ttf,.otf,.woff,.woff2" hidden onChange={(e) => { const f = e.target.files && e.target.files[0]; e.target.value = ''; if (f) uploadFile('font', f); }} />
            </div>

            <textarea ref={taRef} rows="1" dir="auto" placeholder="Describe your design, or paste your content..." value={msg}
              onChange={(e) => { setMsg(e.target.value); autoGrow(); }}
              onKeyDown={(e) => { if (e.key === 'Enter' && !e.shiftKey) sendMsg(e); }} />

            <div className="composer-send-group">
              <button type="button" className={'composer-btn mic' + (recording ? ' listening' : '') + (transcribing ? ' loading' : '')} aria-label="Voice input" onClick={() => { if (transcribing) return; if (recording) { stopVoice(false); return; } ensureSttThen(startVoice); }}>{I.mic}</button>
              <span className="send-group-divider" aria-hidden="true"></span>
              {isRunning(pageIndex + 1) ? (
                <button type="button" className="send-btn is-stop" aria-label="Stop generation" title="Stop" onClick={stopStudio}>{stopping ? (<span className="send-dots" aria-hidden="true"><i /><i /><i /></span>) : I.stop}</button>
              ) : (!String(msg || '').trim() && continueMap[pageIndex + 1] ? (
                <button type="button" className="continue-pill" aria-label="Continue stopped task" title="Continue" onClick={continueTurn}>Continue</button>
              ) : (
                <button type="submit" className="send-btn" aria-label="Send message">{I.send}</button>
              ))}
            </div>
          </form>
          <div className={'voice-panel' + ((recording || transcribing) ? '' : ' is-hidden') + (transcribing ? ' transcribing' : '')}>
            <span className="voice-dot" aria-hidden="true"></span>
            <span className="voice-timer" id="stVoiceTimer">00:00</span>
            <div className="voice-wave" id="stVoiceWave" aria-hidden="true">
              {Array.from({ length: 28 }, (_, i) => (
                <span key={i} className="voice-bar" />
              ))}
            </div>
            <span className="voice-label">{recLabel}</span>
            <div className="voice-panel-actions">
              <button type="button" className="composer-btn voice-cancel" aria-label="Cancel recording" title="Cancel" onClick={cancelVoice}>
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round"><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></svg>
              </button>
              <button type="button" className={'composer-btn voice-stop' + (transcribing ? ' loading' : '')} aria-label="Stop and transcribe" title="Stop" onClick={() => stopVoice(false)}>
                <svg viewBox="0 0 24 24" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>
              </button>
              <button type="button" className={'send-btn voice-send' + (transcribing ? ' loading' : '')} aria-label="Send recording directly" title="Send" onClick={() => stopVoice(true)}>{I.send}</button>
            </div>
          </div>
        </div>
      </div>

      {confirmDelete && (
        <div className="confirm-ov" role="alertdialog" aria-live="assertive" aria-label="Confirm delete">
          <div className="confirm-box">
            <div className="confirm-title">Delete slide {confirmDelete.page_no}?</div>
            <div className="confirm-text">This slide has a design. Deletion cannot be undone — later slides shift up to take its number.</div>
            <div className="confirm-actions">
              <button type="button" className="confirm-cancel" onClick={() => setConfirmDelete(null)}>Cancel</button>
              <button type="button" className="confirm-danger" onClick={() => doDeletePage(confirmDelete.page_no)}>Delete</button>
            </div>
          </div>
        </div>
      )}
      {toast && <div className="toast">{toast}</div>}
    </div>
  );
}
