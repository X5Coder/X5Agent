/**
 * folder.js — working-folder UI (the ONE folder the agent works in)
 * Owns: the folder page browser (list, breadcrumb, upload, mkdir, download)
 * and the composer folder picker (pill + dialog). The server resolves every
 * path inside this folder; the terminal is separate and unrestricted.
 * Event-driven (push): reloads on folder-version pushes + focus/visibility.
 */
"use strict";

import { toast, openModal, closeModal, modalShell } from "./ui.js?v=13";
import { onPush } from "./events.js?v=7";

let folderPath = "";
let folderVersion = 0;
let folderBusy = false;
let openFile = null; // opened file view { full, name, meta, kind, size } — null = listing
let revealName = null; // {name} from a chat token click — reveal once, then clear
let currentRoot = "";
let currentName = "Working Folder";

function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  const kb = n / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  return `${(kb / 1024).toFixed(1)} MB`;
}

function fmtDate(ts) {
  try {
    const d = new Date(Number(ts) * 1000);
    if (Number.isNaN(d.getTime())) return "";
    return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (_) {
    return "";
  }
}

function fmtCount(entry) {
  if (entry.type !== "folder") return "";
  const n = Number(entry.items) || 0;
  const plus = entry.items_truncated ? "+" : "";
  if (n === 0) return "empty";
  if (n === 1) return "1 item";
  return `${n}${plus} items`;
}

function isImageName(name) {
  return /\.(png|jpe?g|gif|webp|bmp|svg|avif)$/i.test(String(name || ""));
}

function isPdfName(name) {
  return /\.pdf$/i.test(String(name || ""));
}

function isHtmlName(name) {
  return /\.html?$/i.test(String(name || ""));
}

/* Inline preview URL: the download endpoint serves ?preview=1 as inline
   (PDF/HTML/images) so the file renders instead of forcing a download. */
function previewUrl(full) {
  return "/api/folder/download?path=" + encodeURIComponent(full) + "&preview=1";
}

/* Preview size caps: the endpoint streams any size, but embedding a huge
   file would freeze the page — over the cap we show download-only. */
const PDF_PREVIEW_MAX = 20 * 1024 * 1024;
const HTML_PREVIEW_MAX = 5 * 1024 * 1024;
const IMG_PREVIEW_MAX = 10 * 1024 * 1024;

/* File kind by extension — each kind gets its own thumb icon + tint. */
function fileKind(name) {
  const m = /\.([a-z0-9]+)$/i.exec(String(name || ""));
  const ext = (m && m[1] ? m[1] : "").toLowerCase();
  if (["mp4", "webm", "mov", "mkv", "avi", "m4v"].includes(ext)) return "video";
  if (["mp3", "wav", "ogg", "oga", "m4a", "flac", "aac"].includes(ext)) return "audio";
  if (["pdf", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "txt", "md", "html", "json", "xml"].includes(ext)) return "doc";
  return "file";
}

function thumbSvg(kind) {
  if (kind === "video") return videoSvg();
  if (kind === "audio") return audioSvg();
  if (kind === "doc") return docSvg();
  return fileSvg();
}

function escName(s) {
  return String(s || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function joinPath(base, name) {
  return base ? `${base}/${name}` : name;
}

let lastSig = "";
let lastPath = "\0";

function sigFor(path, entries) {
  try {
    // The opened file is part of the view: entering/leaving it must re-render
    // even when the folder entries are unchanged (otherwise Back looks dead).
    const view = openFile ? `file:${openFile.name}` : "list";
    return path + "|" + view + "|" + entries.map((e) => `${e?.type}:${e?.name}:${e?.size ?? e?.items ?? ""}`).join(",");
  } catch (_) {
    return "";
  }
}

async function loadFolder() {
  const list = document.getElementById("wsList");
  const bar = document.getElementById("wsPathBar");
  if (!list) return;
  if (folderBusy) return;
  folderBusy = true;
  try {
    const res = await fetch("/api/folder?path=" + encodeURIComponent(folderPath), { cache: "no-store" });
    if (!res.ok) throw new Error("load failed");
    const data = await res.json();
    const entries = Array.isArray(data?.entries) ? data.entries : [];
    folderPath = typeof data?.path === "string" ? data.path : folderPath;
    if (typeof data?.root === "string" && data.root) currentRoot = data.root;
    if (typeof data?.name === "string" && data.name) currentName = data.name;
    // Newest first — files and folders share one timeline, latest on top.
    try {
      entries.sort((a, b) => {
        const ta = Number(a?.mtime) || 0;
        const tb = Number(b?.mtime) || 0;
        if (tb !== ta) return tb - ta;
        return String(a?.name || "").localeCompare(String(b?.name || ""));
      });
    } catch (_) {}

    // Silent background refresh: skip re-render when nothing changed,
    // so polling never flickers in front of the user.
    const sig = sigFor(folderPath, entries);
    if (folderPath === lastPath && sig && sig === lastSig) return;
    lastPath = folderPath;
    lastSig = sig;

    if (bar) {
      // Root name + relative path segments, with a copy-full-path button.
      const rootLabel = currentName || "Working Folder";
      const parts = folderPath ? folderPath.split("/") : [];
      let html = `<span class="ws-path-ico" aria-hidden="true">${folderSvg()}</span>`;
      html += `<span class="ws-crumbs"><button type="button" data-ws-nav="" class="ws-crumb-btn ws-root">${escName(rootLabel)}</button>`;
      let acc = "";
      parts.forEach((p) => {
        acc = acc ? `${acc}/${p}` : p;
        html += `<span class="ws-crumb-sep">/</span><button type="button" data-ws-nav="${escName(acc)}" class="ws-crumb-btn">${escName(p)}</button>`;
      });
      if (openFile) html += `<span class="ws-crumb-sep">/</span><span class="ws-crumb-cur">${escName(openFile.name)}</span>`;
      html += `</span><button type="button" class="ws-copy-btn" id="wsCopyPath" title="Copy full path" aria-label="Copy full path">${copySvg()}</button>`;
      bar.innerHTML = html;
      bar.querySelectorAll("[data-ws-nav]").forEach((b) => {
        b.addEventListener("click", () => {
          folderPath = b.dataset.wsNav || "";
          openFile = null;
          loadFolder();
        });
      });
      // Long paths: keep the deepest segment in view.
      try {
        const crumbs = bar.querySelector(".ws-crumbs");
        if (crumbs) crumbs.scrollLeft = crumbs.scrollWidth;
      } catch (_) {}
      bar.querySelector("#wsCopyPath")?.addEventListener("click", async (e) => {
        const btn = e.currentTarget;
        const fullPath = folderPath && currentRoot
          ? currentRoot.replace(/[\\/]+$/, "") + (currentRoot.includes("\\") ? "\\" : "/") + folderPath.replace(/\//g, currentRoot.includes("\\") ? "\\" : "/")
          : (currentRoot || folderPath);
        try {
          if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(fullPath);
          } else {
            const ta = document.createElement("textarea");
            ta.value = fullPath;
            document.body.appendChild(ta);
            ta.select();
            document.execCommand("copy");
            ta.remove();
          }
          btn.classList.add("copied");
          setTimeout(() => btn.classList.remove("copied"), 1200);
          toast("Path copied");
        } catch (_) {
          toast("Copy failed");
        }
      });
    }

    // Opened file view: the file must still exist, otherwise fall back to listing.
    if (openFile && !entries.some((e) => e?.type !== "folder" && e?.name === openFile.name)) openFile = null;

    list.innerHTML = "";
    if (openFile) {
      list.classList.toggle("ws-grid", false);
      renderFileView(list, openFile);
      return;
    }
    list.classList.toggle("ws-grid", entries.length > 0);
    if (!entries.length) {
      const empty = document.createElement("div");
      empty.className = "ws-empty";
      empty.innerHTML = `
        <div class="ws-empty-word">This folder is empty</div>
        <button type="button" class="glass-btn" id="wsEmptyUpload">Upload your first file</button>`;
      list.appendChild(empty);
      empty.querySelector("#wsEmptyUpload")?.addEventListener("click", () => {
        document.getElementById("wsFileInput")?.click();
      });
      return;
    }
    entries.forEach((e, i) => {
      // Outer is a div (not a <button>): a clickable delete control inside
      // a <button> is invalid HTML and the outer card still fires on trash
      // clicks in some browsers — that is the "redirect on delete" bug.
      const btn = document.createElement("div");
      btn.className = "ws-card";
      btn.setAttribute("role", "button");
      btn.setAttribute("tabindex", "0");
      if (i < 12) btn.setAttribute("data-anim", "");
      const isFolder = e?.type === "folder";
      const full = joinPath(folderPath, e?.name || "");
      const meta = isFolder ? fmtCount(e) : [fmtSize(e?.size), fmtDate(e?.mtime)].filter(Boolean).join(" · ");
      const kind = isFolder ? "folder" : fileKind(e?.name);
      const kindLabel = isFolder ? "Folder" : kind === "file" ? "File" : kind[0].toUpperCase() + kind.slice(1);
      const thumb = isFolder
        ? `<span class="ws-thumb folder">${folderSvg()}</span>`
        : isImageName(e?.name)
          ? `<img class="ws-thumb-img" loading="lazy" alt="" src="${previewUrl(full)}" />`
          : `<span class="ws-thumb ${kind}">${thumbSvg(kind)}</span>`;
      const go = isFolder ? arrowSvg() : downloadSvg();
      const del = `<button type="button" class="ws-del" title="Delete ${escName(e?.name || "")}" aria-label="Delete ${escName(e?.name || "")}">${trashSvg()}</button>`;
      btn.innerHTML = `
        ${thumb}
        <span class="ws-info">
          <span class="ws-kind">${kindLabel}</span>
          <span class="ws-name"></span>
          <span class="ws-meta">${escName(meta)}</span>
        </span>
        <span class="ws-go" aria-hidden="true">${go}</span>
        ${del}`;
      btn.querySelector(".ws-name").textContent = e?.name || "";
      btn.title = isFolder ? `Open ${e?.name}` : `View ${e?.name}`;
      const openEntry = () => {
        if (isFolder) {
          folderPath = full;
          openFile = null;
          loadFolder();
        } else {
          openFile = { full, name: e?.name || "", meta, kind, size: Number(e?.size) || 0 };
          lastSig = "";
          loadFolder();
        }
      };
      btn.addEventListener("click", openEntry);
      // Keyboard open (Enter/Space) only when the card itself is focused —
      // the inner delete <button> handles its own keys natively.
      btn.addEventListener("keydown", (ev) => {
        if (ev.target !== btn) return;
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          openEntry();
        }
      });
      const delBtn = btn.querySelector(".ws-del");
      if (delBtn) {
        // Delete never opens the entry: stop the click here and show the
        // animated confirm dialog (works for files and folders alike).
        delBtn.addEventListener("click", (ev) => {
          ev.preventDefault();
          ev.stopPropagation();
          openDeleteModal(full, e?.name || "", isFolder);
        });
      }
      list.appendChild(btn);
    });
    // One-shot reveal from a chat token click: scroll to the card + flash it.
    if (revealName) {
      try {
        const target = revealName;
        revealName = null;
        const cards = list.querySelectorAll(".ws-card .ws-name");
        for (const n of cards) {
          if (n && n.textContent === target) {
            const card = n.closest(".ws-card");
            if (card) {
              try {
                card.scrollIntoView({ block: "nearest" });
              } catch (_) {}
              card.classList.add("ws-flash");
              setTimeout(() => {
                try {
                  if (card.isConnected) card.classList.remove("ws-flash");
                } catch (_) {}
              }, 1600);
            }
            break;
          }
        }
      } catch (_) {
        revealName = null;
      }
    }
    // Silent swap: folder contents (and folder-enter) replace the boot
    // placeholders with no re-animation.
  } catch (_) {
    // Invalidate the signature so the next successful poll always re-renders
    // (otherwise a stale error could stick when data is unchanged).
    lastSig = "";
    // A failed first load must still surface the retry box instead of
    // leaving the list empty.
    if (!list.children.length) {
      list.innerHTML = `<div class="grid-empty" data-ws-error><span>Could not load the folder.</span><button type="button" class="ws-retry-btn" id="wsRetryBtn">Retry</button></div>`;
      document.getElementById("wsRetryBtn")?.addEventListener("click", () => {
        lastSig = "";
        loadFolder();
      });
    }
  } finally {
    folderBusy = false;
  }
}

function folderSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" /></svg>`;
}

function copySvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="14" height="14" x="8" y="8" rx="2" /><path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2" /></svg>`;
}

function fileSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" /><path d="M14 2v4a2 2 0 0 0 2 2h4" /></svg>`;
}

function videoSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="14" x="3" y="5" rx="2" /><polygon points="10 9.5 15 12 10 14.5 10 9.5" fill="currentColor" stroke="none" /></svg>`;
}

function audioSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></svg>`;
}

function docSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" /><path d="M14 2v4a2 2 0 0 0 2 2h4" /><line x1="9" y1="13" x2="15" y2="13" /><line x1="9" y1="17" x2="13" y2="17" /></svg>`;
}

function arrowSvg() {
  return `<svg class="row-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6" /></svg>`;
}

function downloadSvg() {
  return `<svg class="row-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" /><polyline points="7 10 12 15 17 10" /><line x1="12" y1="15" x2="12" y2="3" /></svg>`;
}

function trashSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18" /><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6" /><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2" /></svg>`;
}

function warnSvg() {
  return `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" width="20" height="20"><path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z" /><path d="M12 9v4" /><path d="M12 17h.01" /></svg>`;
}

function readAsDataURL(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("read failed"));
    reader.readAsDataURL(file);
  });
}

async function uploadPicked(file) {
  if (!file || file.size === 0) {
    toast("Empty file");
    return;
  }
  if (file.size > 10 * 1024 * 1024) {
    toast("File too large (max 10MB)");
    return;
  }
  try {
    const data = await readAsDataURL(file);
    const res = await fetch("/api/folder", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "upload", path: folderPath, name: file.name, data }),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out?.error || "upload failed");
    toast("Uploaded");
    loadFolder();
  } catch (err) {
    toast(err instanceof Error && err.message ? err.message : "Upload failed");
  }
}

function validFolderName(v) {
  const s = String(v || "").trim();
  if (!s) return "Name required";
  if (s.length > 100) return "Too long (max 100)";
  if (s.includes("/") || s.includes("\\") || s === ".." || s === ".") return "Single name only, no paths";
  return "";
}

async function createFolder() {
  openModal(modalShell({
    icon: folderSvg(),
    title: "New folder",
    subtitle: folderPath ? `Inside ${currentName} / ${folderPath}` : `Inside ${currentName}`,
    body: `
      <div class="field">
        <label for="wsFolderName">Folder name</label>
        <div class="field-wrap">
          <input class="input" id="wsFolderName" maxlength="100" placeholder="e.g. project-live" autocomplete="off" />
        </div>
        <span class="hint" id="wsFolderErr" style="color: var(--red-hi); display: none;"></span>
      </div>`,
    foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn" id="wsFolderSave" type="button">Create</button>`,
  }));
  const input = document.getElementById("wsFolderName");
  const err = document.getElementById("wsFolderErr");
  const save = document.getElementById("wsFolderSave");
  const showErr = (msg) => {
    if (!err) return;
    if (msg) {
      err.textContent = msg;
      err.style.display = "";
    } else {
      err.textContent = "";
      err.style.display = "none";
    }
  };
  input?.addEventListener("input", () => {
    if (err && err.style.display !== "none" && !validFolderName(input.value)) showErr("");
  });
  const submit = async () => {
    const bad = validFolderName(input?.value);
    if (bad) {
      showErr(bad);
      return;
    }
    save.disabled = true;
    try {
      const res = await fetch("/api/folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "mkdir", path: folderPath, name: String(input.value).trim() }),
      });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(out?.error || "create failed");
      closeModal();
      toast("Folder created");
      loadFolder();
    } catch (e) {
      showErr(e instanceof Error && e.message ? e.message : "Create failed");
      save.disabled = false;
    }
  };
  save?.addEventListener("click", submit);
  input?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  });
}

function downloadFile(full, name) {
  const a = document.createElement("a");
  a.href = "/api/folder/download?path=" + encodeURIComponent(full);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
}

/* Big preview dialog (animated modal): the PDF renders with the
   browser's built-in viewer (all pages, scrollable), HTML renders static,
   images render large. Footer holds the full-file download — PDF pages
   are not separate files, so per-page download buttons are intentionally
   not offered; the whole file downloads with one tap. */
function openPreviewDialog(file) {
  const url = previewUrl(file.full);
  const size = Number(file.size) || 0;
  let body;
  if (isPdfName(file.name)) {
    body = size > PDF_PREVIEW_MAX
      ? `<p class="ws-preview-note">This PDF is too large for preview — download it to view.</p>`
      : `<iframe class="ws-preview-frame ws-preview-dialog-pdf" src="${url}" title="PDF preview"></iframe>`;
  } else if (isHtmlName(file.name)) {
    // sandbox="allow-same-origin" WITHOUT allow-scripts: the frame keeps
    // the console origin so its images/fonts load WITH the owner cookie
    // (opaque-origin frames send no cookies, so every authenticated
    // subresource 401s and previews show no pictures). Scripts stay fully
    // blocked (no allow-scripts), as do forms, popups and top navigation.
    body = size > HTML_PREVIEW_MAX
      ? `<p class="ws-preview-note">This HTML file is too large for preview — download it to view.</p>`
      : `<iframe class="ws-preview-frame ws-preview-dialog-html" src="${url}" title="HTML preview" sandbox="allow-same-origin"></iframe>`;
  } else {
    body = size > IMG_PREVIEW_MAX
      ? `<p class="ws-preview-note">This image is too large for preview — download it to view.</p>`
      : `<img class="ws-preview-frame ws-preview-dialog-img" src="${url}" alt="" />`;
  }
  openModal(modalShell({
    icon: docSvg(),
    title: "Preview",
    subtitle: escName(file.name),
    body: `<div class="ws-preview-dialog">${body}</div>`,
    foot: `
      <button class="glass-btn" data-close type="button">Close</button>
      <button class="glass-btn" id="wsPreviewDownload" type="button">Download file</button>`,
  }));
  document.getElementById("wsPreviewDownload")?.addEventListener("click", () => {
    downloadFile(file.full, file.name);
  });
}

/* Opened file view: identity + actions only — the preview lives ONLY in
   the Preview dialog (never auto-loads here, so opening a file can never
   trigger an automatic download anymore; that was the HTML/PDF bug). */
function renderFileView(list, file) {
  const view = document.createElement("div");
  view.className = "ws-file";
  view.setAttribute("data-anim", "");
  const kindLabel = file.kind === "file" ? "File" : file.kind[0].toUpperCase() + file.kind.slice(1);
  const size = Number(file.size) || 0;
  let canPreview = false;
  if (isPdfName(file.name)) canPreview = size <= PDF_PREVIEW_MAX;
  else if (isHtmlName(file.name)) canPreview = size <= HTML_PREVIEW_MAX;
  else if (isImageName(file.name)) canPreview = size <= IMG_PREVIEW_MAX;
  const note = canPreview
    ? `<p class="ws-preview-note">Tap Preview to view it here.</p>`
    : `<p class="ws-preview-note">Preview isn't available for this file here.</p>`;
  view.innerHTML = `
    <span class="ws-thumb big ${file.kind}">${thumbSvg(file.kind)}</span>
    <span class="ws-kind">${escName(kindLabel)}</span>
    <span class="ws-file-name"></span>
    <span class="ws-meta">${escName(file.meta || "")}</span>
    ${note}
    <span class="ws-file-actions">
      <button type="button" class="glass-btn" id="wsFileBack">Back</button>
      ${canPreview ? `<button type="button" class="glass-btn" id="wsFilePreview">Preview</button>` : ""}
      <button type="button" class="glass-btn danger" id="wsFileDelete">Delete</button>
      <button type="button" class="glass-btn" id="wsFileDownload">Download</button>
    </span>`;
  view.querySelector(".ws-file-name").textContent = file.name;
  list.appendChild(view);
  view.querySelector("#wsFileBack")?.addEventListener("click", () => {
    openFile = null;
    lastSig = "";
    loadFolder();
  });
  view.querySelector("#wsFilePreview")?.addEventListener("click", () => {
    openPreviewDialog(file);
  });
  view.querySelector("#wsFileDownload")?.addEventListener("click", () => {
    downloadFile(file.full, file.name);
  });
  view.querySelector("#wsFileDelete")?.addEventListener("click", () => {
    openDeleteModal(file.full, file.name, false);
  });
}

/* Delete confirmation: folders warn that everything inside goes too. */
function openDeleteModal(full, name, isFolder) {
  openModal(modalShell({
    icon: warnSvg(),
    title: isFolder ? "Delete folder" : "Delete file",
    subtitle: name,
    body: isFolder
      ? `<p class="ws-preview-note">“${escName(name)}” and <b>everything inside it</b> will be permanently deleted.</p>`
      : `<p class="ws-preview-note">“${escName(name)}” will be permanently deleted.</p>`,
    foot: `
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn danger" id="wsDeleteConfirm" type="button">Delete</button>`,
  }));
  document.getElementById("wsDeleteConfirm")?.addEventListener("click", async (ev) => {
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      const res = await fetch("/api/folder", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: "delete", path: folderPath, name }),
      });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(out?.error || "delete failed");
      closeModal();
      if (openFile && openFile.name === name) openFile = null;
      toast(isFolder ? "Folder deleted" : "File deleted");
      lastSig = "";
      loadFolder();
    } catch (e) {
      toast(e instanceof Error && e.message ? e.message : "Delete failed");
      btn.disabled = false;
    }
  });
}

function folderVisible() {
  try {
    const page = document.querySelector('.page[data-page="folder"]');
    return !!page && page.classList.contains("active");
  } catch (_) {
    return false;
  }
}

/* ---------- composer folder picker ---------- */
let pickerPath = "";

function setPillLabels(name, path) {
  const label = name || "Working Folder";
  ["folderLabel", "schedRunFolderLabel"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.textContent = label;
  });
  ["folderBtn", "schedRunFolderBtn"].forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.title = path ? `Working folder — ${path}` : label;
  });
}

async function refreshFolderPill() {
  try {
    const res = await fetch("/api/folder", { cache: "no-store" });
    if (!res.ok) return;
    const data = await res.json();
    if (data && typeof data.name === "string") setPillLabels(data.name, data.root);
  } catch (_) {}
}

function pickerRow(name, sub, dir) {
  const row = document.createElement("button");
  row.type = "button";
  row.className = "folder-row";
  const icon = document.createElement("span");
  icon.className = "folder-row-ico";
  icon.innerHTML = folderSvg();
  const txt = document.createElement("span");
  txt.className = "folder-row-txt";
  const b = document.createElement("b");
  b.textContent = name;
  txt.appendChild(b);
  if (sub) {
    const i = document.createElement("i");
    i.textContent = sub;
    txt.appendChild(i);
  }
  const chev = document.createElement("span");
  chev.className = "folder-row-chev";
  chev.innerHTML = arrowSvg();
  row.appendChild(icon);
  row.appendChild(txt);
  row.appendChild(chev);
  row.addEventListener("click", () => {
    pickerPath = dir;
    renderPicker();
  });
  return row;
}

async function renderPicker() {
  const list = document.getElementById("folderPickList");
  const pathEl = document.getElementById("folderPickPath");
  const current = document.getElementById("folderPickCurrent");
  if (!list) return;
  list.innerHTML = `<div class="folder-pick-loading">Loading…</div>`;
  try {
    const res = await fetch("/api/folder/browse?path=" + encodeURIComponent(pickerPath), { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.error || "Could not open that folder");
    pickerPath = typeof data.path === "string" ? data.path : pickerPath;
    if (pathEl) pathEl.value = pickerPath;
    if (current) current.textContent = pickerPath || "";
    list.innerHTML = "";
    if (data.parent) {
      list.appendChild(pickerRow("..", "Parent folder", data.parent));
    }
    (data.entries || []).forEach((entry) => {
      list.appendChild(pickerRow(entry.name, "", entry.path));
    });
    if (!data.parent && !(data.entries || []).length) {
      list.innerHTML = `<div class="folder-pick-empty">No sub-folders here. Use "Choose this folder" to work in it.</div>`;
    }
    const roots = document.getElementById("folderPickRoots");
    if (roots && !roots.dataset.built && Array.isArray(data.roots)) {
      roots.dataset.built = "1";
      roots.innerHTML = "";
      data.roots.forEach((root) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "folder-chip";
        b.textContent = root.name;
        b.title = root.path;
        b.addEventListener("click", () => {
          pickerPath = root.path;
          renderPicker();
        });
        roots.appendChild(b);
      });
    }
  } catch (e) {
    list.innerHTML = `<div class="folder-pick-empty">${escName(e instanceof Error ? e.message : "Could not open that folder")}</div>`;
  }
}

async function chooseCurrentFolder() {
  const btn = document.getElementById("folderPickUse");
  if (btn) btn.disabled = true;
  try {
    const res = await fetch("/api/folder/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: pickerPath }),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out?.error || "Could not use that folder");
    closeModal();
    folderPath = "";
    openFile = null;
    lastSig = "";
    setPillLabels(out.name, out.path);
    toast("Working folder set");
    window.dispatchEvent(new CustomEvent("x5:folder-changed", { detail: out }));
    if (folderVisible()) loadFolder();
  } catch (e) {
    const note = document.getElementById("folderPickErr");
    if (note) {
      note.textContent = e instanceof Error && e.message ? e.message : "Could not use that folder";
      note.style.display = "";
    }
    if (btn) btn.disabled = false;
  }
}

async function resetToDefault() {
  try {
    const res = await fetch("/api/folder/set", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "reset" }),
    });
    const out = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(out?.error || "Could not reset");
    closeModal();
    folderPath = "";
    openFile = null;
    lastSig = "";
    setPillLabels(out.name, out.path);
    toast("Using the default folder");
    window.dispatchEvent(new CustomEvent("x5:folder-changed", { detail: out }));
    if (folderVisible()) loadFolder();
  } catch (e) {
    toast(e instanceof Error && e.message ? e.message : "Could not reset");
  }
}

export async function openFolderPicker() {
  if (!pickerPath) pickerPath = "";
  openModal(modalShell({
    icon: folderSvg(),
    title: "Working folder",
    subtitle: "Where the agent reads, writes and runs files",
    body: `
      <div class="folder-pick">
        <div class="folder-pick-bar">
          <span class="folder-pick-label">Folder</span>
          <input class="input" id="folderPickPath" spellcheck="false" autocomplete="off" placeholder="Type a folder path" />
          <button class="glass-btn" id="folderPickGo" type="button">Go</button>
        </div>
        <div class="folder-pick-roots" id="folderPickRoots"></div>
        <div class="folder-pick-list" id="folderPickList"></div>
        <p class="folder-pick-current" id="folderPickCurrent"></p>
        <p class="hint" id="folderPickErr" style="color: var(--red-hi); display: none;"></p>
      </div>`,
    foot: `
      <button class="glass-btn" id="folderPickReset" type="button">Use default</button>
      <button class="glass-btn" data-close type="button">Cancel</button>
      <button class="glass-btn" id="folderPickUse" type="button">Choose this folder</button>`,
  }));
  await renderPicker();
  const pathInput = document.getElementById("folderPickPath");
  const go = async () => {
    pickerPath = String(pathInput?.value || "").trim();
    const err = document.getElementById("folderPickErr");
    if (err) err.style.display = "none";
    await renderPicker();
  };
  document.getElementById("folderPickGo")?.addEventListener("click", go);
  pathInput?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      go();
    }
  });
  document.getElementById("folderPickUse")?.addEventListener("click", chooseCurrentFolder);
  document.getElementById("folderPickReset")?.addEventListener("click", resetToDefault);
}

function initFolderPickers() {
  ["folderBtn", "schedRunFolderBtn"].forEach((id) => {
    document.getElementById(id)?.addEventListener("click", (e) => {
      e.preventDefault();
      openFolderPicker();
    });
  });
  refreshFolderPill();
  window.addEventListener("x5:folder-changed", (e) => {
    const d = (e && e.detail) || {};
    setPillLabels(d.name, d.path);
  });
}

export function initWorkingFolder() {
  document.getElementById("wsUploadBtn")?.addEventListener("click", () => {
    document.getElementById("wsFileInput")?.click();
  });
  document.getElementById("wsMkdirBtn")?.addEventListener("click", createFolder);
  document.getElementById("wsFileInput")?.addEventListener("change", (e) => {
    const f = e.target && e.target.files && e.target.files[0];
    e.target.value = "";
    if (f) uploadPicked(f);
  });
  window.addEventListener("x5:page-open", (e) => {
    if (e && e.detail === "folder") loadFolder();
  });
  // Chat blue-token click: show that file exactly at its path, then flash it.
  window.addEventListener("x5:reveal-folder", (e) => {
    try {
      const d = (e && e.detail) || {};
      const dir = typeof d.dir === "string" ? d.dir.replace(/\\/g, "/").slice(0, 500) : "";
      const name = typeof d.name === "string" ? d.name.slice(0, 200) : "";
      if (dir.split("/").includes("..")) return;
      folderPath = dir;
      openFile = null;
      revealName = name || null;
      lastSig = "";
      loadFolder();
    } catch (_) {}
  });
  // Push replaces polling: reload only when the server reports a new
  // folder version (agent-side writes included via its sweep).
  onPush("folder", (v) => {
    try {
      const next = v && typeof v.version === "number" ? v.version : 0;
      if (next && next !== folderVersion && folderVisible() && !document.hidden) {
        folderVersion = next;
        loadFolder();
      } else if (next) {
        folderVersion = Math.max(folderVersion, next);
      }
    } catch (_) {}
  });
  window.addEventListener("focus", () => {
    if (folderVisible()) loadFolder();
  });
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && folderVisible()) loadFolder();
  });
  initFolderPickers();
  // Returned for the app.js boot barrier (black splash waits on it).
  return loadFolder();
}
