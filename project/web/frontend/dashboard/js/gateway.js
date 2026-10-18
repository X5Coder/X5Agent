/**
 * gateway.js — WhatsApp (Baileys link-device) + Telegram (Bot API) connections
 * Owns: badges from server config, glass config dialogs, live QR/code polling,
 * allowed-number + chat-ID managers, real connection tests. Server is
 * authoritative (localStorage no longer used). Secrets never render.
 */
"use strict";

import { openModal, closeModal, modalShell, toast, animateIn } from "./ui.js?v=13";
import { onPush } from "./events.js?v=7";

const WA_ICON = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51a12.8 12.8 0 0 0-.57-.01c-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.872.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 0 1-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 0 1-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884a9.82 9.82 0 0 1 6.988 2.896 9.825 9.825 0 0 1 2.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0 0 12.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 0 0 5.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 0 0-3.48-8.413Z" /></svg>`;
const TG_ICON = `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M11.944 0A12 12 0 0 0 0 12a12 12 0 0 0 12 12 12 12 0 0 0 12-12A12 12 0 0 0 12 0a12 12 0 0 0-.056 0zm4.962 7.224c.1-.002.321.023.465.14a.506.506 0 0 1 .171.325c.016.093.036.306.02.472-.18 1.898-.962 6.502-1.36 8.627-.168.9-.499 1.201-.82 1.23-.696.065-1.225-.46-1.9-.902-1.056-.693-1.653-1.124-2.678-1.8-1.185-.78-.417-1.21.26-1.91.177-.184 3.247-2.977 3.307-3.23.007-.032.014-.15-.056-.212s-.174-.041-.249-.024c-.106.024-1.793 1.14-5.061 3.345-.48.33-.913.49-1.302.48-.428-.008-1.252-.241-1.865-.44-.752-.245-1.349-.374-1.297-.789.027-.216.325-.437.893-.663 3.498-1.524 5.83-2.529 6.998-3.014 3.332-1.386 4.025-1.627 4.476-1.635z" /></svg>`;

async function apiGet(url) {
  const res = await fetch(url, { cache: "no-store" });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out?.error || "request failed");
  return out;
}

async function apiPost(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const out = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(out?.error || "request failed");
  return out;
}

function esc(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function digits(v, max = 15) {
  return String(v || "").replace(/\D/g, "").slice(0, max);
}

/* Split a stored international number into country-code + rest for the templates. */
function splitIntl(full) {
  const d = digits(full);
  if (d.startsWith("20") && d.length > 10) return { code: "20", number: d.slice(2) };
  if (d.length > 10) return { code: d.slice(0, d.length - 10), number: d.slice(-10) };
  if (d.length > 7) return { code: d.slice(0, d.length - 7), number: d.slice(-7) };
  return { code: "20", number: d };
}

/* World country codes: [name, dial]. Rendered in a custom searchable picker. */
const COUNTRIES = [["Afghanistan","93"],["Albania","355"],["Algeria","213"],["Andorra","376"],["Angola","244"],["Anguilla","1264"],["Antigua and Barbuda","1268"],["Argentina","54"],["Armenia","374"],["Aruba","297"],["Australia","61"],["Austria","43"],["Azerbaijan","994"],["Bahamas","1242"],["Bahrain","973"],["Bangladesh","880"],["Barbados","1246"],["Belarus","375"],["Belgium","32"],["Belize","501"],["Benin","229"],["Bermuda","1441"],["Bhutan","975"],["Bolivia","591"],["Bosnia and Herzegovina","387"],["Botswana","267"],["Brazil","55"],["British Virgin Islands","1284"],["Brunei","673"],["Bulgaria","359"],["Burkina Faso","226"],["Burundi","257"],["Cambodia","855"],["Cameroon","237"],["Canada","1"],["Cape Verde","238"],["Cayman Islands","1345"],["Central African Republic","236"],["Chad","235"],["Chile","56"],["China","86"],["Colombia","57"],["Comoros","269"],["Congo","242"],["Congo, DR","243"],["Cook Islands","682"],["Costa Rica","506"],["Croatia","385"],["Cuba","53"],["Curacao","599"],["Cyprus","357"],["Czech Republic","420"],["Denmark","45"],["Djibouti","253"],["Dominica","1767"],["Dominican Republic","1809"],["Ecuador","593"],["Egypt","20"],["El Salvador","503"],["Equatorial Guinea","240"],["Eritrea","291"],["Estonia","372"],["Eswatini","268"],["Ethiopia","251"],["Falkland Islands","500"],["Faroe Islands","298"],["Fiji","679"],["Finland","358"],["France","33"],["French Guiana","594"],["French Polynesia","689"],["Gabon","241"],["Gambia","220"],["Georgia","995"],["Germany","49"],["Ghana","233"],["Gibraltar","350"],["Greece","30"],["Greenland","299"],["Grenada","1473"],["Guadeloupe","590"],["Guam","1671"],["Guatemala","502"],["Guinea","224"],["Guinea-Bissau","245"],["Guyana","592"],["Haiti","509"],["Honduras","504"],["Hong Kong","852"],["Hungary","36"],["Iceland","354"],["India","91"],["Indonesia","62"],["Iran","98"],["Iraq","964"],["Ireland","353"],["Israel","972"],["Italy","39"],["Ivory Coast","225"],["Jamaica","1876"],["Japan","81"],["Jordan","962"],["Kazakhstan","7"],["Kenya","254"],["Kiribati","686"],["Kosovo","383"],["Kuwait","965"],["Kyrgyzstan","996"],["Laos","856"],["Latvia","371"],["Lebanon","961"],["Lesotho","266"],["Liberia","231"],["Libya","218"],["Liechtenstein","423"],["Lithuania","370"],["Luxembourg","352"],["Macau","853"],["Madagascar","261"],["Malawi","265"],["Malaysia","60"],["Maldives","960"],["Mali","223"],["Malta","356"],["Marshall Islands","692"],["Martinique","596"],["Mauritania","222"],["Mauritius","230"],["Mexico","52"],["Micronesia","691"],["Moldova","373"],["Monaco","377"],["Mongolia","976"],["Montenegro","382"],["Montserrat","1664"],["Morocco","212"],["Mozambique","258"],["Myanmar","95"],["Namibia","264"],["Nauru","674"],["Nepal","977"],["Netherlands","31"],["New Caledonia","687"],["New Zealand","64"],["Nicaragua","505"],["Niger","227"],["Nigeria","234"],["North Korea","850"],["North Macedonia","389"],["Norway","47"],["Oman","968"],["Pakistan","92"],["Palau","680"],["Palestine","970"],["Panama","507"],["Papua New Guinea","675"],["Paraguay","595"],["Peru","51"],["Philippines","63"],["Poland","48"],["Portugal","351"],["Puerto Rico","1787"],["Qatar","974"],["Romania","40"],["Russia","7"],["Rwanda","250"],["Saint Kitts and Nevis","1869"],["Saint Lucia","1758"],["Saint Vincent","1784"],["Samoa","685"],["San Marino","378"],["Saudi Arabia","966"],["Senegal","221"],["Serbia","381"],["Seychelles","248"],["Sierra Leone","232"],["Singapore","65"],["Slovakia","421"],["Slovenia","386"],["Solomon Islands","677"],["Somalia","252"],["South Africa","27"],["South Korea","82"],["South Sudan","211"],["Spain","34"],["Sri Lanka","94"],["Sudan","249"],["Suriname","597"],["Sweden","46"],["Switzerland","41"],["Syria","963"],["Taiwan","886"],["Tajikistan","992"],["Tanzania","255"],["Thailand","66"],["Togo","228"],["Tonga","676"],["Trinidad and Tobago","1868"],["Tunisia","216"],["Turkey","90"],["Turkmenistan","993"],["Uganda","256"],["Ukraine","380"],["United Arab Emirates","971"],["United Kingdom","44"],["United States","1"],["Uruguay","598"],["Uzbekistan","998"],["Vanuatu","678"],["Vatican","379"],["Venezuela","58"],["Vietnam","84"],["Virgin Islands, US","1340"],["Yemen","967"],["Zambia","260"],["Zimbabwe","263"]];

function phoneRow(prefix, codeVal, numVal, numPlaceholder) {
  const code = String(codeVal || "20").replace(/\D/g, "") || "20";
  return `
    <div class="phone-row">
      <div class="field code cc-field">
        <label id="${prefix}CodeLabel">Code</label>
        <input type="hidden" id="${prefix}Code" value="${esc(code)}" />
        <button type="button" class="cc-btn" aria-labelledby="${prefix}CodeLabel" aria-haspopup="listbox" aria-expanded="false">
          <span class="cc-val">+${esc(code)}</span>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9" /></svg>
        </button>
        <div class="cc-menu" hidden role="listbox" aria-label="Country code">
          <div class="cc-search"><input type="search" placeholder="Search countries…" aria-label="Search countries" autocomplete="off" /></div>
          <div class="cc-list"></div>
        </div>
      </div>
      <div class="field num">
        <label for="${prefix}Number">Number</label>
        <div class="field-wrap"><input class="input" id="${prefix}Number" inputmode="tel" placeholder="${esc(numPlaceholder)}" value="${esc(numVal)}" maxlength="15" /></div>
      </div>
    </div>`;
}

/* Custom country picker: button + searchable glass menu (no native select). */
function initCcPicker(field) {
  const btn = field.querySelector(".cc-btn");
  const menu = field.querySelector(".cc-menu");
  const hidden = field.querySelector('input[type="hidden"]');
  const search = menu.querySelector(".cc-search input");
  const list = menu.querySelector(".cc-list");
  if (!btn || !menu || !hidden || !search || !list) return;
  const paint = (filter) => {
    const f = String(filter || "").trim().toLowerCase();
    const fd = f.replace(/\D/g, "");
    const items = COUNTRIES.filter(([n, d]) => !f || n.toLowerCase().includes(f) || (fd && d.startsWith(fd)));
    list.innerHTML = items.length
      ? items.map(([n, d]) => `<button type="button" role="option" data-dial="${d}" aria-selected="${hidden.value === d}" class="${hidden.value === d ? "sel" : ""}"><span>${esc(n)}</span><b>+${d}</b></button>`).join("")
      : `<span class="hint">No matches.</span>`;
    list.querySelectorAll("[data-dial]").forEach((b) => {
      b.addEventListener("click", () => {
        hidden.value = b.dataset.dial;
        btn.querySelector(".cc-val").textContent = "+" + b.dataset.dial;
        close();
      });
    });
    const sel = list.querySelector(".sel");
    if (sel) sel.scrollIntoView({ block: "nearest" });
  };
  let onScroll = null;
  let onResize = null;
  const position = () => {
    const r = btn.getBoundingClientRect();
    const w = Math.min(300, Math.max(248, r.width + 160));
    const roomBelow = window.innerHeight - r.bottom - 12;
    const roomAbove = r.top - 12;
    const h = Math.min(292, Math.max(roomBelow, roomAbove, 160));
    menu.style.width = `${w}px`;
    menu.style.maxHeight = `${h}px`;
    const left = Math.max(8, Math.min(r.left, window.innerWidth - w - 8));
    menu.style.left = `${left}px`;
    menu.style.top = roomBelow >= 200 || roomBelow >= roomAbove ? `${r.bottom + 6}px` : `${Math.max(8, r.top - h - 6)}px`;
  };
  const close = () => {
    menu.hidden = true;
    btn.setAttribute("aria-expanded", "false");
    try {
      field.appendChild(menu);
    } catch (_) {}
    if (onScroll) {
      document.removeEventListener("scroll", onScroll, true);
      onScroll = null;
    }
    if (onResize) {
      window.removeEventListener("resize", onResize);
      onResize = null;
    }
  };
  const open = () => {
    /* Hoist to body: modal transform would trap fixed positioning inside it. */
    try {
      document.body.appendChild(menu);
    } catch (_) {}
    menu.hidden = false;
    btn.setAttribute("aria-expanded", "true");
    position();
    onScroll = () => {
      if (!menu.hidden) {
        if (!document.contains(btn)) {
          close();
          return;
        }
        position();
      }
    };
    onResize = onScroll;
    document.addEventListener("scroll", onScroll, true);
    window.addEventListener("resize", onResize);
    search.value = "";
    paint("");
    search.focus();
  };
  btn.addEventListener("click", (e) => {
    e.stopPropagation();
    if (menu.hidden) {
      open();
      setTimeout(() => {
        document.addEventListener("click", function h(ev) {
          if (!document.contains(btn)) {
            document.removeEventListener("click", h);
            return;
          }
          if (!field.contains(ev.target)) {
            close();
            document.removeEventListener("click", h);
          }
        });
      }, 0);
    } else {
      close();
    }
  });
  search.addEventListener("click", (e) => e.stopPropagation());
  search.addEventListener("input", () => paint(search.value));
  search.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      close();
      btn.focus();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const first = list.querySelector("[data-dial]");
      if (first) first.click();
    }
  });
  menu.addEventListener("click", (e) => e.stopPropagation());
}

function initCcPickers(root) {
  try {
    root.querySelectorAll(".cc-field").forEach(initCcPicker);
  } catch (_) {}
}

function refreshBadges(cfg) {
  try {
    const wa = cfg?.whatsapp || {};
    const tg = cfg?.telegram || {};
    const waBadge = document.getElementById("waBadge");
    const tgBadge = document.getElementById("tgBadge");
    if (waBadge) {
      const bad = wa.live?.state === "mismatch";
      const live = wa.live?.state === "connected";
      const saved = (wa.agent_number || "").length >= 7;
      waBadge.textContent = live ? "Connected" : bad ? "Wrong number" : saved ? "Number saved" : "Not connected";
      waBadge.classList.toggle("green", live);
      waBadge.classList.toggle("amber", !live && (bad || saved));
      waBadge.classList.toggle("muted", !live && !bad && !saved);
    }
    if (tgBadge) {
      const on = Boolean(tg.has_token);
      tgBadge.textContent = on ? "Connected" : "Not connected";
      tgBadge.classList.toggle("green", on);
      tgBadge.classList.toggle("muted", !on);
    }
  } catch (_) {}
}

/* ================= WhatsApp ================= */

// Modal generation: push handlers from a closed modal exit quietly.
let waGen = 0;

async function openWhatsAppModal() {
  let cfg;
  try {
    cfg = await apiGet("/api/gateway/config");
  } catch (e) {
    toast(e instanceof Error ? e.message : "Load failed");
    return;
  }
  const wa = cfg.whatsapp || {};
  if ((wa.agent_number || "").length >= 7) openWaLink(wa);
  else openWaNumber(wa);
}

/* Step 1 — agent number + country code only, then continue. */
function openWaNumber(wa) {
  const parts = splitIntl(wa.agent_number || "");
  openModal(
    modalShell({
      icon: WA_ICON,
      title: "Connect WhatsApp",
      subtitle: "Step 1 · agent number",
      body: `
        <div class="block">
          <div class="block-title">Agent number (required)</div>
          ${phoneRow("wa", parts.code, parts.number, "100 000 0000")}
          <span class="hint">The number the agent fully controls, e.g. <bdi dir="ltr">+20 100 000 0000</bdi>. Nothing continues without it.</span>
          <span class="hint err" id="waNumErr" hidden></span>
        </div>`,
      foot: `
        <button class="glass-btn" data-close type="button">Cancel</button>
        <button class="glass-btn" id="waNumSave" type="button">Continue</button>`,
    })
  );
  initCcPickers(document.getElementById("modalPanel"));
  document.getElementById("waNumSave")?.addEventListener("click", async () => {
    const err = document.getElementById("waNumErr");
    const full = digits(document.getElementById("waCode")?.value) + digits(document.getElementById("waNumber")?.value);
    if (full.length < 7 || full.length > 15) {
      if (err) {
        err.textContent = "Enter a valid number: country code + number (7-15 digits).";
        err.hidden = false;
      }
      return;
    }
    const btn = document.getElementById("waNumSave");
    if (btn) btn.disabled = true;
    try {
      await apiPost("/api/gateway/config", {
        channel: "whatsapp",
        settings: { agent_number: full, mode: "self", allowed: [] },
      });
      toast("Number saved");
      const cfg = await apiGet("/api/gateway/config");
      refreshBadges(cfg);
      openWaLink(cfg.whatsapp || { agent_number: full, mode: "self", allowed: [] });
    } catch (e) {
      toast(e instanceof Error ? e.message : "Save failed");
      if (btn) btn.disabled = false;
    }
  });
}

/* Step 2 — QR / code cards, live refresh, number verify, people, switches. */
function openWaLink(wa) {
  const gen = ++waGen;
  const allowed0 = Array.isArray(wa.allowed) ? wa.allowed.slice() : [];
  const connected0 = wa.live?.state === "connected";
  const parts = splitIntl(wa.agent_number || "");
  openModal(
    modalShell({
      icon: WA_ICON,
      title: "Connect WhatsApp",
      subtitle: `Step 2 · <bdi dir="ltr">+${esc(wa.agent_number)}</bdi>`,
      body: `
      <div id="waLinkRoot">
        <div class="wa-link-error" id="waMismatchBox" hidden></div>
        <div class="block">
          <div class="wa-number-lock wa-agent-card"><span class="wa-agent-avatar" aria-hidden="true">${WA_ICON}</span><span class="wa-agent-meta"><small>Agent number</small><bdi dir="ltr">+${esc(wa.agent_number)}</bdi></span><button class="wa-edit-number" id="waEditNumBtn" type="button" aria-label="Edit number"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 3a2.85 2.83 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5Z" /></svg></button></div>
          <div class="wa-number-edit" id="waNumberEdit" hidden>
            ${phoneRow("wa", parts.code, parts.number, "100 000 0000")}
            <div class="row-end"><button class="glass-btn" id="waNumCancel" type="button">Cancel</button><button class="glass-btn accent" id="waNumUpdate" type="button">Update number</button></div>
            <span class="hint">Changing the number drops any previous link.</span>
          </div>
        </div>
        </div>
        <div class="block" id="waLinkBlock"${connected0 ? " hidden" : ""}>
          <div class="block-title">Choose a link method</div>
          <div class="wa-link-choices">
            <div class="wa-link-choice"><div><b>QR</b><div class="hint">Scan from your phone: WhatsApp → Linked devices → Link a device.</div></div><button class="glass-btn" id="waShowQr" type="button">Show QR</button></div>
            <div class="wa-link-choice"><div><b>Link code</b><div class="hint">Enter an 8-character code in the same menu instead of scanning.</div></div><button class="glass-btn" id="waShowCode" type="button">Show code</button></div>
          </div>
          <div class="qr-box" id="waQrBox" hidden>
            <div class="wa-qr-loader" id="waQrLoader"><span></span><div class="hint">Preparing the QR code… it appears here as soon as it is ready and refreshes automatically.</div></div>
            <img id="waQrImg" alt="WhatsApp link QR" hidden />
          </div>
          <div class="wa-code-panel" id="waCodeBox" hidden>
            <div class="wa-code-shimmer" id="waCodeShimmer"><i></i><i></i><i></i><i></i></div>
            <code class="wa-code-big" id="waPairCode" dir="ltr" hidden>— — — —</code>
            <div class="row-end"><button class="glass-btn" id="waPairBtn" type="button">New code</button></div>
            <span class="hint">On your phone: WhatsApp → Settings → Linked devices → Link a device → Link with code. The code refreshes automatically.</span>
          </div>
        </div>
        <div class="block" id="waSuccessBlock"${connected0 ? "" : " hidden"}>
          <div class="wa-link-success"><span class="wa-link-success-icon">✓</span><div><b>Linked ✓</b><div class="hint" id="waSuccessText">The linked number matches the agent number.</div></div></div>
        </div>
        <div class="block" id="waPeoplePanel"${connected0 ? "" : " hidden"}>
          <div class="wa-people-panel">
            <div class="wa-people-head"><div><div class="block-title">Replies</div><div class="hint">The agent replies to you in your own private chat (same number).</div></div></div>
            <div class="chip-list" id="waAllowed" hidden></div>
            <div><button class="wa-add-person" id="waAddToggle" type="button"><span>+</span> Add number</button></div>
            <div class="wa-add-form" id="waAddForm" hidden>
              ${phoneRow("waNew", parts.code, "", "100 000 0001")}
              <div class="wa-add-actions"><button class="glass-btn" id="waAddCancel" type="button">Cancel</button><button class="glass-btn" id="waAddNum" type="button">Add</button></div>
            </div>
          </div>
        </div>
        <div class="block" id="waOptsPanel"${connected0 ? "" : " hidden"}>
          <div class="block-title">Reply options</div>
          <label class="toggle-row"><span class="t-text"><strong>Read receipts (blue ticks)</strong><span>Marks messages as read after the agent finishes replying</span></span><span class="switch"><input type="checkbox" id="waOptRead" /></span></label>
          <label class="toggle-row"><span class="t-text"><strong>Typing indicator</strong><span>Shows “typing…” to the other side before sending the reply</span></span><span class="switch"><input type="checkbox" id="waOptTyping" /></span></label>
          <label class="toggle-row"><span class="t-text"><strong>Reply to message</strong><span>Sends the reply quoted on the last message received</span></span><span class="switch"><input type="checkbox" id="waOptReply" /></span></label>
        </div>
      </div>`,
      foot: `
        <button class="glass-btn danger spread" id="waDisconnect" type="button"${connected0 ? "" : " hidden"}>Disconnect</button>
        <button class="glass-btn" data-close type="button">Close</button>`,
    })
  );
  initCcPickers(document.getElementById("modalPanel"));

  /* ---------- persisted state ---------- */
  const currentAllowed = () => {
    const box = document.getElementById("waAllowed");
    if (!box) return Array.isArray(wa.allowed) ? wa.allowed.slice() : [];
    return Array.from(box.querySelectorAll("[data-del]")).map((b) => b.dataset.del);
  };
  const currentOpts = () => ({
    read_receipts: document.getElementById("waOptRead")?.checked ?? !!wa.read_receipts,
    typing_indicator: document.getElementById("waOptTyping")?.checked ?? wa.typing_indicator !== false,
    reply_to_message: document.getElementById("waOptReply")?.checked ?? !!wa.reply_to_message,
  });
  const persistWa = async (patch) => {
    const full = digits(document.getElementById("waCode")?.value) + digits(document.getElementById("waNumber")?.value);
    const nextAllowed = patch && patch.allowed !== undefined ? patch.allowed : currentAllowed();
    // No mode switch in the UI: self-only while the list is empty,
    // selected automatically once another number is added.
    const body = {
      agent_number: full.length >= 7 ? full : wa.agent_number,
      mode: nextAllowed.length ? "selected" : "self",
      allowed: nextAllowed,
      ...currentOpts(),
    };
    const out = await apiPost("/api/gateway/config", { channel: "whatsapp", settings: body });
    const s = out.settings || {};
    wa.agent_number = s.agent_number || wa.agent_number;
    wa.mode = s.mode || wa.mode;
    wa.allowed = Array.isArray(s.allowed) ? s.allowed : wa.allowed;
    wa.read_receipts = !!s.read_receipts;
    wa.typing_indicator = s.typing_indicator !== false;
    wa.reply_to_message = !!s.reply_to_message;
    paintAllowed(wa.allowed);
    try {
      const cfg = await apiGet("/api/gateway/config").catch(() => null);
      if (cfg) refreshBadges(cfg);
    } catch (_) {}
    return s;
  };

  /* ---------- allowed numbers + LID identity ---------- */
  const paintAllowed = (list) => {
    const box = document.getElementById("waAllowed");
    if (!box) return;
    const arr = Array.isArray(list) ? list : [];
    box.hidden = !arr.length;
    box.innerHTML = arr.map((n) => `<span class="chip"><bdi dir="ltr">+${esc(n)}</bdi><button type="button" data-del="${esc(n)}" aria-label="Remove +${esc(n)}">×</button></span>`).join("");
    box.querySelectorAll("[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        try {
          await persistWa({ allowed: currentAllowed().filter((x) => x !== b.dataset.del) });
          toast("Number removed");
        } catch (e) {
          toast(e instanceof Error ? e.message : "Remove failed");
        }
      });
    });
  };
  paintAllowed(allowed0);

  const setSw = (id, v) => {
    const el = document.getElementById(id);
    if (el) el.checked = !!v;
  };
  setSw("waOptRead", wa.read_receipts);
  setSw("waOptTyping", wa.typing_indicator !== false);
  setSw("waOptReply", wa.reply_to_message);
  ["waOptRead", "waOptTyping", "waOptReply"].forEach((id) => {
    document.getElementById(id)?.addEventListener("change", async () => {
      try {
        await persistWa({});
        toast("Options saved");
      } catch (e) {
        toast(e instanceof Error ? e.message : "Save failed");
      }
    });
  });

  document.getElementById("waAddToggle")?.addEventListener("click", () => {
    const f = document.getElementById("waAddForm");
    if (!f) return;
    f.hidden = !f.hidden;
    if (!f.hidden) {
      f.classList.remove("wa-pop-in");
      void f.offsetWidth;
      f.classList.add("wa-pop-in");
    }
  });
  document.getElementById("waAddCancel")?.addEventListener("click", () => {
    const f = document.getElementById("waAddForm");
    if (f) f.hidden = true;
  });
  document.getElementById("waAddNum")?.addEventListener("click", async () => {
    const full = digits(document.getElementById("waNewCode")?.value) + digits(document.getElementById("waNewNumber")?.value);
    if (full.length < 7 || full.length > 15) {
      toast("Enter country code + number (7-15 digits)");
      return;
    }
    const next = currentAllowed();
    if (!next.includes(full)) next.push(full);
    try {
      await persistWa({ allowed: next });
      const n = document.getElementById("waNewNumber");
      if (n) n.value = "";
      const f = document.getElementById("waAddForm");
      if (f) f.hidden = true;
      toast("Number added");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Add failed");
    }
  });
  const setNumEdit = (open) => {
    const f = document.getElementById("waNumberEdit");
    if (!f) return;
    if (open && f.hidden) {
      f.classList.remove("wa-closing");
      f.hidden = false;
    } else if (!open && !f.hidden) {
      f.classList.add("wa-closing");
      setTimeout(() => {
        f.hidden = true;
        f.classList.remove("wa-closing");
      }, 240);
    }
  };
  document.getElementById("waEditNumBtn")?.addEventListener("click", () => {
    const f = document.getElementById("waNumberEdit");
    setNumEdit(!f || f.hidden);
  });
  document.getElementById("waNumCancel")?.addEventListener("click", () => setNumEdit(false));
  document.getElementById("waNumUpdate")?.addEventListener("click", async () => {
    try {
      await persistWa({});
      toast("Number updated");
      const cfg = await apiGet("/api/gateway/config").catch(() => null);
      if (cfg) openWaLink(cfg.whatsapp || wa);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Update failed");
    }
  });
  document.getElementById("waDisconnect")?.addEventListener("click", async () => {
    try {
      await apiPost("/api/gateway/config", { channel: "disconnect", settings: { target: "whatsapp" } });
      try {
        await apiPost("/api/gateway/whatsapp/disconnect", {});
      } catch (_) {}
      refreshBadges(await apiGet("/api/gateway/config"));
      toast("WhatsApp disconnected");
      closeModal();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Disconnect failed");
    }
  });

  /* ---------- QR / code panels ---------- */
  const popIn = (el) => {
    if (!el) return;
    el.classList.remove("wa-pop-in");
    void el.offsetWidth;
    el.classList.add("wa-pop-in");
  };
  const toggleMethod = (which) => {
    const qrBox = document.getElementById("waQrBox");
    const codeBox = document.getElementById("waCodeBox");
    if (!qrBox || !codeBox) return;
    if (which === "qr") {
      codeBox.hidden = true;
      qrBox.hidden = !qrBox.hidden;
      if (!qrBox.hidden) popIn(qrBox);
    } else {
      qrBox.hidden = true;
      codeBox.hidden = !codeBox.hidden;
      if (!codeBox.hidden) {
        popIn(codeBox);
        if (!codeBox.dataset.asked) {
          codeBox.dataset.asked = "1";
          requestPair();
        }
      }
    }
  };
  document.getElementById("waShowQr")?.addEventListener("click", () => toggleMethod("qr"));
  document.getElementById("waShowCode")?.addEventListener("click", () => toggleMethod("code"));

  let lastQr = "";
  const paintQr = (st) => {
    const img = document.getElementById("waQrImg");
    const loader = document.getElementById("waQrLoader");
    if (!img || !loader) return;
    const url = st.qr_data_url || "";
    if (url) {
      if (url !== lastQr) {
        lastQr = url;
        img.setAttribute("src", url);
      }
      img.hidden = false;
      loader.hidden = true;
    } else {
      lastQr = "";
      img.hidden = true;
      loader.hidden = false;
    }
  };
  const paintPair = (st) => {
    const code = document.getElementById("waPairCode");
    const shim = document.getElementById("waCodeShimmer");
    if (!code || !shim) return;
    const p = st.pairing || "";
    if (p) {
      code.textContent = p.replace(/(.{4})/g, "$1 ").trim();
      code.hidden = false;
      shim.hidden = true;
    } else {
      code.hidden = true;
      shim.hidden = false;
    }
  };
  const requestPair = async () => {
    try {
      const st = await apiPost("/api/gateway/whatsapp/pair", {});
      paintPair(st);
    } catch (e) {
      toast(e instanceof Error ? e.message : "Code failed");
    }
  };
  document.getElementById("waPairBtn")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    if (btn) btn.disabled = true;
    try {
      await requestPair();
    } finally {
      setTimeout(() => {
        if (btn) btn.disabled = false;
      }, 10000);
    }
  });

  /* ---------- live status + verify + poll ---------- */
  // Starts "connected" when the window opens on an active link, otherwise
  // the first connected poll would rebuild the modal in an endless loop.
  let lastState = connected0 ? "connected" : "";
  const paintLive = (st) => {
    const dot = document.getElementById("waLiveDot");
    const txt = document.getElementById("waLiveText");
    const box = document.getElementById("waLiveBox");
    if (!dot || !txt || !box) return;
    const linked = st.state === "connected";
    const bad = st.state === "mismatch";
    box.classList.toggle("ok", linked);
    box.classList.toggle("err", bad);
    dot.classList.toggle("ok", linked);
    dot.classList.toggle("err", bad);
    txt.textContent = linked
      ? `Linked ✓ +${st.phone || st.linked || wa.agent_number}`
      : bad
        ? "Wrong number linked — session wiped."
        : st.state === "pairing"
          ? "Code issued — enter it on your phone…"
          : st.state === "qr"
            ? "Waiting for scan…"
            : st.error || st.state || "Starting…";
  };
  const paintMismatch = (st) => {
    const box = document.getElementById("waMismatchBox");
    if (!box) return;
    const saved = wa.agent_number || "";
    const linked = st.linked || "";
    box.innerHTML = `<div>The linked number (<bdi dir="ltr">+${esc(linked)}</bdi>) is not the agent number (<bdi dir="ltr">+${esc(saved)}</bdi>) — rejected, session wiped.</div><div class="row-end"><button class="glass-btn" id="waRetryLink" type="button">Relink</button></div>`;
    box.hidden = false;
    document.getElementById("waRetryLink")?.addEventListener("click", async () => {
      try {
        await apiPost("/api/gateway/whatsapp/reset", {});
        await apiPost("/api/gateway/whatsapp/connect", {}).catch(() => null);
        box.hidden = true;
        toast("Start linking again");
      } catch (e) {
        toast(e instanceof Error ? e.message : "Failed");
      }
    });
  };
  const hideMismatch = () => {
    const box = document.getElementById("waMismatchBox");
    if (box) box.hidden = true;
  };
  const showConnectedView = (st) => {
    const linkBlock = document.getElementById("waLinkBlock");
    const successBlock = document.getElementById("waSuccessBlock");
    const people = document.getElementById("waPeoplePanel");
    const opts = document.getElementById("waOptsPanel");
    if (linkBlock) linkBlock.hidden = true;
    if (successBlock) successBlock.hidden = false;
    if (people) people.hidden = false;
    if (opts) opts.hidden = false;
    const num = st.phone || st.linked || wa.agent_number;
    const t = document.getElementById("waSuccessText");
    if (t) t.textContent = `Linked to +${num} — matches the agent number ✓`;
  };
  const handleLinkState = async (st) => {
    if (!st) return;
    paintLive(st);
    if (st.state === "connected") {
      if (lastState !== "connected") {
        lastState = "connected";
        try {
          const cfg = await apiGet("/api/gateway/config");
          refreshBadges(cfg);
          toast("WhatsApp linked ✓");
          openWaLink({ ...(cfg.whatsapp || {}), live: { state: "connected" } });
        } catch (_) {
          showConnectedView(st);
        }
      } else {
        showConnectedView(st);
      }
      return;
    }
    if (st.state === "mismatch") {
      lastState = "mismatch";
      paintMismatch(st);
      try {
        refreshBadges(await apiGet("/api/gateway/config"));
      } catch (_) {}
      return;
    }
    lastState = st.state || "";
    hideMismatch();
    paintQr(st);
    if (st.pairing) paintPair(st);
  };
  // Push-driven (SSE): no interval anywhere. Stale generations exit quietly.
  const onGatewayPush = (st) => {
    if (gen !== waGen || !document.getElementById("waLinkRoot")) return;
    handleLinkState(st);
  };
  onPush("gateway", onGatewayPush);
  paintLive(wa.live || { state: "idle" });
  const fetchStatus = async () => {
    if (gen !== waGen || !document.getElementById("waLinkRoot")) return;
    try {
      let st = await apiGet("/api/gateway/whatsapp/status");
      if (st.state === "idle") {
        await apiPost("/api/gateway/whatsapp/connect", {}).catch(() => null);
        if (gen !== waGen || !document.getElementById("waLinkRoot")) return;
        st = await apiGet("/api/gateway/whatsapp/status").catch(() => st);
      }
      onGatewayPush(st);
    } catch (_) {}
  };
  if (!connected0) {
    // Drop any stale link attempt first so the window always starts clean.
    apiPost("/api/gateway/whatsapp/reset", {}).catch(() => {}).finally(() => {
      lastQr = "";
      fetchStatus();
    });
  } else {
    fetchStatus();
  }
}

/* ================= Telegram ================= */

async function openTelegramModal() {
  let cfg;
  try {
    cfg = await apiGet("/api/gateway/config");
  } catch (e) {
    toast(e instanceof Error ? e.message : "Load failed");
    return;
  }
  const tg = cfg.telegram || {};
  const chats = Array.isArray(tg.chat_ids) ? tg.chat_ids.slice() : [];
  const hasSaved = Boolean(tg.has_token);
  openModal(
    modalShell({
      icon: TG_ICON,
      title: "Connect Telegram",
      subtitle: hasSaved ? "Connected · tap to update" : "Link a bot to your agent",
      body: `
        <div class="block">
          <div class="block-title">Bot token (required first)</div>
          <div class="field" style="margin-bottom:0;">
            <label for="tgToken">Bot token</label>
            <div class="field-wrap"><input class="input" id="tgToken" type="password" placeholder="123456789:AAH-xxxx" value="" autocomplete="off" /></div>
            <span class="hint">Issued by @BotFather.${hasSaved ? " A token is already saved — entering a new one replaces it." : " Everything below unlocks once a valid token is entered."}</span>
          </div>
        </div>
        <div id="tgStep2"${hasSaved ? "" : " hidden"}>
          <div class="block" data-anim>
            <div class="block-title">Allowed chat IDs</div>
            <div class="chip-list" id="tgChats"></div>
            <div class="field" style="margin-bottom:8px;">
              <label for="tgChatNew">Chat ID <span class="hint-inline">e.g. 123456789 or -100… for groups</span></label>
              <div class="field-wrap"><input class="input" id="tgChatNew" inputmode="numeric" placeholder="123456789" maxlength="20" /></div>
            </div>
            <div class="row-end"><button class="glass-btn" id="tgChatAdd" type="button">+ Add chat ID</button></div>
            <span class="hint">Users and groups together — the bot watches and runs commands for every ID here. Add or remove anytime.</span>
          </div>
          <div class="block" data-anim>
            <div class="block-title">How to link</div>
            <ol class="steps">
              <li><b>1</b><span>Open Telegram and start a chat with <strong>@BotFather</strong>.</span></li>
              <li><b>2</b><span>Send <strong>/newbot</strong>, follow the prompts, paste the token above.</span></li>
              <li><b>3</b><span>Press <strong>/start</strong> on your bot, add the chat IDs, then save to test.</span></li>
            </ol>
          </div>
          <div class="block" data-anim>
            <div class="block-title">Result</div>
            <div class="stack" id="tgTestOut"><span class="hint">Saving runs the connection test and shows the result here.</span></div>
          </div>
        </div>`,
      foot: `
        ${hasSaved ? `<button class="glass-btn danger spread" id="tgDisconnect" type="button">Disconnect</button>` : ""}
        <button class="glass-btn" data-close type="button">Cancel</button>
        <button class="glass-btn" id="tgSave" type="button">Save</button>`,
    })
  );

  /* Progressive reveal: the rest appears (animated) once a valid token exists. */
  const tokenInput = document.getElementById("tgToken");
  const revealStep2 = () => {
    const step2 = document.getElementById("tgStep2");
    if (!step2 || !step2.hidden) return;
    const typed = String(tokenInput?.value || "").trim();
    if (!/^\d+:[\w-]{10,}$/.test(typed)) return;
    step2.hidden = false;
    animateIn(step2);
  };
  tokenInput?.addEventListener("input", revealStep2);

  const paintChats = () => {
    const box = document.getElementById("tgChats");
    if (!box) return;
    box.innerHTML = chats.length
      ? chats.map((c) => `<span class="chip">${esc(c)}<button type="button" data-del="${esc(c)}" aria-label="Remove ${esc(c)}">×</button></span>`).join("")
      : `<span class="hint">No chat IDs yet — nobody can talk to the bot until you add one.</span>`;
    box.querySelectorAll("[data-del]").forEach((b) => {
      b.addEventListener("click", async () => {
        const idx = chats.indexOf(b.dataset.del);
        if (idx >= 0) chats.splice(idx, 1);
        paintChats();
        await saveTg(false);
      });
    });
  };

  const saveTg = async () => {
    const out = await apiPost("/api/gateway/config", {
      channel: "telegram",
      settings: {
        token: String(document.getElementById("tgToken")?.value || ""),
        username: "",
        chat_ids: chats,
      },
    });
    const cfg2 = await apiGet("/api/gateway/config").catch(() => null);
    if (cfg2) refreshBadges(cfg2);
    return out;
  };

  const paintTestResult = (r) => {
    const out = document.getElementById("tgTestOut");
    if (!out) return;
    const who = r.bot?.username ? `@${esc(r.bot.username)}` : "bot";
    let html = `<div class="conn-result ${r.ok ? "ok" : "err"}"><span class="conn-dot ${r.ok ? "ok" : "err"}"></span><span class="conn-text">${r.ok ? `Connected ✓ ${who} — test message delivered.` : esc(r.error || "Connection failed")}</span></div>`;
    (r.chats || []).forEach((c) => {
      html += `<div class="conn-result ${c.ok ? "ok" : "err"}"><span class="conn-dot ${c.ok ? "ok" : "err"}"></span><span class="conn-text">${esc(String(c.id))} — ${c.ok ? "message delivered ✓" : esc(c.error || "failed")}</span></div>`;
    });
    out.innerHTML = html;
  };

  paintChats();

  document.getElementById("tgChatAdd")?.addEventListener("click", async () => {
    const inp = document.getElementById("tgChatNew");
    const v = String(inp?.value || "").trim();
    if (!/^-?\d+$/.test(v)) {
      toast("Chat ID is digits only (groups start with -)");
      return;
    }
    if (!chats.includes(v)) chats.push(v);
    if (inp) inp.value = "";
    paintChats();
    try {
      await saveTg(false);
      toast("Chat ID added");
    } catch (e) {
      toast(e instanceof Error ? e.message : "Add failed");
    }
  });

  /* One Save: persists, then auto-tests and shows the result before closing. */
  document.getElementById("tgSave")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    const typed = String(document.getElementById("tgToken")?.value || "").trim();
    if (!typed && !hasSaved) {
      toast("Enter the bot token first");
      return;
    }
    btn.disabled = true;
    btn.classList.add("loading");
    try {
      await saveTg();
      const r = await apiPost("/api/gateway/telegram/test", {});
      paintTestResult(r);
      const cfg2 = await apiGet("/api/gateway/config").catch(() => null);
      if (cfg2) refreshBadges(cfg2);
      if (r.ok) {
        toast("Telegram connected ✓");
        setTimeout(() => {
          if (document.getElementById("tgTestOut")) closeModal();
        }, 3000);
      } else {
        toast("Saved, but the test failed — check the result");
        btn.disabled = false;
        btn.classList.remove("loading");
      }
    } catch (e) {
      const out = document.getElementById("tgTestOut");
      if (out) out.innerHTML = `<div class="conn-result err"><span class="conn-dot err"></span><span class="conn-text">${esc(e instanceof Error ? e.message : "Save failed")}</span></div>`;
      btn.disabled = false;
      btn.classList.remove("loading");
    }
  });

  document.getElementById("tgDisconnect")?.addEventListener("click", async () => {
    try {
      await apiPost("/api/gateway/config", { channel: "disconnect", settings: { target: "telegram" } });
      refreshBadges(await apiGet("/api/gateway/config"));
      toast("Telegram disconnected");
      closeModal();
    } catch (e) {
      toast(e instanceof Error ? e.message : "Disconnect failed");
    }
  });
}

export function initGateway() {
  // Returned for the app.js boot barrier (black splash waits on it).
  // Even on failure badges must settle (honest "Not connected").
  return apiGet("/api/gateway/config")
    .then(refreshBadges)
    .catch(() => refreshBadges(null));
  document.querySelectorAll("[data-gateway]").forEach((card) => {
    card.addEventListener("click", () => {
      if (card.dataset.gateway === "whatsapp") openWhatsAppModal();
      else openTelegramModal();
    });
  });
}
