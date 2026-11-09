// The files drawer: the v2 file browser, ported. A slim footer bar under the
// panes (chevron, crumbs, refresh/upload) that opens onto a sortable table.
//
// It follows the focused session -- first time on a host opens that session's
// dir, after that each host keeps the last dir you browsed, and browsing the
// host you're already on is never interrupted. Cut/copy/paste works within a
// host through /api/files/op and across hosts through the byte-counted relay;
// the running count and upload progress show up in the handle.

import { h, fill, keyed } from "../core/dom.js";
import * as store from "../core/state.js";
import { toast } from "../core/dialog.js";
import { openMenu } from "../core/menu.js";
import { remember } from "../core/prefs.js";
import { shortPath } from "../term/pane.js";
import {
  joinPath, fmtSize, fmtDate, listDir, fileOp, downloadFile, dlUrl,
  pasteClip, deleteSelected, newFolder, uploadFiles,
} from "./files-ops.js";

const UP = { up: true, name: "..", is_dir: true };   // keyed as "..": no entry can be
const MIN_H = 120;

let drawer, grip, chev, crumbsEl, statusEl, body, table, tbody, msg, picker;
let cwd = { host: null, path: "~" };       // null host: nothing loaded yet
let entries = [];
let loadedHere = false;                    // the current dir listed at least once
let lastDirs = remember("filesLastDirs") || {};
let fileSort = remember("filesSort") || { key: "name", dir: 1 };
let sel = new Set();                       // selected entry names (this listing)
let anchor = null, cursor = null;          // shift-range ends; cursor = keyboard's
let clip = null;                           // {op: "cut"|"copy", host, items: [{path, name}]}
let editing = null;                        // entry name with an inline rename open
let refreshing = false;
let pathInput = null;                      // the open path-edit input, if any
let open = false;
let lastFollowedKey = null;

// -------------------------------------------------------------------- mount

export function mount(drawerEl) {
  drawer = drawerEl;
  drawer.hidden = false;

  grip = h("div.drawer-grip", { title: "Drag to resize" });
  grip.addEventListener("mousedown", startResize);

  picker = h("input", {
    type: "file", multiple: true, hidden: true,
    onchange: async (e) => {
      const list = [...e.target.files];
      e.target.value = "";
      if (ready() && await uploadFiles(ctx(), list)) reload(true);
    },
  });

  crumbsEl = h("div.crumbs", { title: "Click the empty space to type a path" });
  crumbsEl.addEventListener("click", (e) => {
    const crumb = e.target.closest(".crumb");
    if (crumb) {
      e.stopPropagation();
      if (crumb.dataset.path && crumb.dataset.path !== cwd.path) loadFiles(cwd.host, crumb.dataset.path);
      return;
    }
    if (!e.target.closest("input")) showPathInput(true);
  });

  statusEl = h("span.status");
  const handle = h("div.drawer-handle", { onclick: onHandleClick },
    (chev = h("span.chev", "▴")),
    h("span.lbl", "files"),
    crumbsEl,
    statusEl,
    h("span.actions",
      h("button.btn.sm", {
        type: "button", title: "Refresh",
        onclick: (e) => { e.stopPropagation(); refreshFiles(); },
      }, "↻"),
      h("button.btn.sm", {
        type: "button", title: "Upload files here",
        onclick: (e) => { e.stopPropagation(); picker.click(); },
      }, "upload")));

  table = h("table.ftable", { tabindex: "0", "aria-label": "Files" },
    h("thead", h("tr",
      sortableTh("Name", "name"),
      sortableTh("Size", "size"),
      sortableTh("Modified", "mtime"))),
    (tbody = h("tbody")));
  table.addEventListener("contextmenu", (e) => {
    if (e.target.closest("tr[data-key]")) return;   // a row handled its own
    e.preventDefault();
    rowMenu(e, null);
  });
  wireKeys();

  msg = h("div.file-msg", { hidden: true });
  body = h("div.drawer-body", table, msg);
  drawer.append(grip, handle, picker, body);
  wireDrop();

  const hSaved = remember("filesHeight");
  if (typeof hSaved === "number" && hSaved > 0) applyHeight(hSaved);
  setOpen(remember("filesOpen") === true);
  renderCrumbs();
  paintSortArrows();

  store.subscribe(["focusedKey", "sessions"], follow);

  // Keep the listing current: on window focus, and a gentle poll while the
  // window is focused, so files added/removed externally show without a nav.
  window.addEventListener("focus", refreshFiles);
  setInterval(() => { if (document.hasFocus()) refreshFiles(); }, 5000);
}

function sortableTh(label, key) {
  const arrow = h("span.sort-ind");
  const th = h("th", { dataset: { sort: key }, title: "Sort by " + label.toLowerCase() }, label, arrow);
  th.addEventListener("click", () => {
    fileSort = fileSort.key === key ? { key, dir: -fileSort.dir } : { key, dir: 1 };
    remember("filesSort", fileSort);
    paintSortArrows();
    renderTable();
  });
  return th;
}

function paintSortArrows() {
  for (const th of table.querySelectorAll("th[data-sort]")) {
    th.querySelector(".sort-ind").textContent =
      fileSort.key === th.dataset.sort ? (fileSort.dir > 0 ? "▲" : "▼") : "";
  }
}

// ------------------------------------------------------------------- follow
// v2's final rule: the drawer follows the session you switch to -- its own dir
// when it has one. (An earlier version stayed put on a host you were already
// browsing, and with a fleet of local sessions the pane never moved: the
// owner's complaint.) With no dir: stay put on the same host, else that host's
// last-browsed dir. It reacts to a *change* of focused session only, so
// clicking back into the same pane never resets where you have browsed to.

function follow() {
  const fk = store.get().focusedKey;
  if (fk === lastFollowedKey) return;    // fires on every poll; only moves on focus
  if (!fk) { lastFollowedKey = fk; return; }
  const sep = fk.indexOf("\u001f");
  const host = fk.slice(0, sep), name = fk.slice(sep + 1);
  const s = store.session(host, name);
  if (!s) return;                        // just created, not listed yet: retry next poll
  lastFollowedKey = fk;
  const dir = s.dir || s.path;
  if (dir) return void loadFiles(host, dir);
  if (loadedHere && host === cwd.host) return;
  loadFiles(host, lastDirs[host] || "~");
}

// --------------------------------------------------------------------- load

function ctx() {
  return { host: cwd.host, path: cwd.path, entries, setStatus };
}

/** Before the first listing there is no directory to act in. */
function ready() {
  if (cwd.host) return true;
  toast("open a session first -- the file browser follows it", "warn", 3500);
  return false;
}

function setStatus(text) { statusEl.textContent = text || ""; }

function showMsg(text) { msg.textContent = text; msg.hidden = !text; }

// Only the newest listing may land: a slow one (a quiet refresh, a follow,
// your own click) finishing late would otherwise put one dir's rows under
// another dir's path, and delete/paste/rename would act on the wrong files.
let loadSeq = 0;
let navPending = 0;                    // seq of a non-quiet load still in flight

async function loadFiles(host, path, quiet) {
  const my = ++loadSeq;
  if (!quiet) navPending = my;
  if (host !== cwd.host || path !== cwd.path) {
    sel = new Set(); anchor = cursor = null; editing = null;   // nav abandons an inline edit
  }
  cwd = { host, path };
  showPathInput(false);
  renderCrumbs();
  if (!quiet) showMsg("loading…");
  let list;
  try {
    list = await listDir(host, path);
  } catch (err) {
    if (my !== loadSeq) return;
    if (navPending === my) navPending = 0;
    // quiet = a background refresh: don't flash or clobber on a transient error.
    // A failed navigation empties the table: cwd already moved, and the old
    // rows under the new path would make every file action act on the wrong dir.
    if (!quiet) { entries = []; renderTable(); showMsg(err.message); toast(err.message, "error", 5000); }
    return;
  }
  if (my !== loadSeq) return;
  if (navPending === my) navPending = 0;
  entries = list;
  showMsg("");
  if (!quiet) {
    loadedHere = true;
    // only explicit navigations count as "where I left this host" -- the 5s
    // auto-refresh must not save over a good entry while a load is in flight
    lastDirs[host] = path;
    remember("filesLastDirs", lastDirs);
  }
  renderTable();
}

function refreshFiles() {
  // don't nuke an in-progress edit, and let a navigation in flight land first
  if (refreshing || editing || !cwd.host || navPending) return;
  refreshing = true;
  const scroll = body.scrollTop;
  loadFiles(cwd.host, cwd.path, true).finally(() => {
    body.scrollTop = scroll;
    refreshing = false;
  });
}

const reload = (quiet) => loadFiles(cwd.host, cwd.path, quiet);

// ------------------------------------------------------------------- render

function sortedEntries() {
  const { key, dir } = fileSort;
  const byName = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  return [...entries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1;   // folders first, always
    let r = 0;
    if (key === "size") r = (a.size || 0) - (b.size || 0);
    else if (key === "mtime") r = (a.mtime || 0) - (b.mtime || 0);
    else r = byName(a, b);
    return (r * dir) || byName(a, b);
  });
}

function renderTable() {
  paintSortArrows();
  keyed(tbody, [UP, ...sortedEntries()], (e) => e.name, makeRow, updateRow);
  paintSelection();
}

function makeRow(e) {
  if (e.up) {
    return h("tr.up", { title: "Parent directory (Backspace)", ondblclick: goUp },
      h("td.n", "↑ .."), h("td.r", ""), h("td.r", ""));
  }
  return updateRow(h("tr", {
    onclick: (ev) => { clickSelect(ev, e.name); table.focus(); },
    ondblclick: () => { const x = entryByName(e.name); if (x) openEntry(x); },
    oncontextmenu: (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (!sel.has(e.name)) selectFile(e.name);   // right-click inside a selection keeps it
      rowMenu(ev, entryByName(e.name) || e);
    },
    // v2 parity: drag a file out of the browser onto the desktop to download it
    draggable: e.is_dir ? null : "true",
    ondragstart: e.is_dir ? null : (ev) => {
      ev.dataTransfer.setData("DownloadURL",
        `application/octet-stream:${e.name}:${location.origin}${dlUrl(cwd.host, joinPath(cwd.path, e.name))}`);
    },
  }, h("td.n", h("span.nm")), h("td.r.sz"), h("td.r.dt")), e);
}

function updateRow(tr, e) {
  if (e.up) return tr;
  const nm = tr.querySelector(".nm");
  if (editing !== e.name) nm.textContent = (e.is_dir ? "▸ " : "· ") + e.name;
  tr.querySelector(".sz").textContent = e.is_dir ? "dir" : fmtSize(e.size);
  tr.querySelector(".dt").textContent = fmtDate(e.mtime);
  const cutPath = joinPath(cwd.path, e.name);
  tr.classList.toggle("cut",
    !!(clip && clip.op === "cut" && clip.host === cwd.host && clip.items.some((it) => it.path === cutPath)));
  return tr;
}

// ---------------------------------------------------------------- selection
// GNOME-style: single click selects, Ctrl toggles, Shift ranges; double click
// (or Enter) opens a dir or downloads a file.

const entryByName = (name) => entries.find((x) => x.name === name);

function paintSelection() {
  for (const tr of tbody.children) {
    const k = tr.dataset.key;
    if (k === undefined || k === "..") continue;
    tr.classList.toggle("sel", sel.has(k));
  }
  const cur = cursor && tbody.querySelector(`tr[data-key="${CSS.escape(cursor)}"]`);
  if (cur) cur.scrollIntoView({ block: "nearest" });
}

function selectFile(name) {
  sel = new Set(name == null ? [] : [name]);
  anchor = cursor = name;
  paintSelection();
}

function selectRange(toName) {
  const names = sortedEntries().map((x) => x.name);
  const a = names.indexOf(anchor), b = names.indexOf(toName);
  if (a < 0 || b < 0) return selectFile(toName);
  sel = new Set(names.slice(Math.min(a, b), Math.max(a, b) + 1));
  cursor = toName;
  paintSelection();
}

function clickSelect(ev, name) {
  if (ev.ctrlKey || ev.metaKey) {
    if (sel.has(name)) sel.delete(name); else sel.add(name);
    anchor = cursor = name;
    paintSelection();
  } else if (ev.shiftKey && anchor) {
    selectRange(name);
  } else {
    selectFile(name);
  }
}

function openEntry(e) {
  if (e.is_dir) loadFiles(cwd.host, joinPath(cwd.path, e.name));
  else downloadFile(ctx(), e.name);
}

const goUp = () => loadFiles(cwd.host, joinPath(cwd.path, ".."));

// --------------------------------------------------------------------- menu

function selectedNames(entry) {
  return sel.size ? [...sel] : entry ? [entry.name] : [];
}

function rowMenu(ev, entry) {
  const n = entry ? Math.max(1, sel.size) : 0;
  const many = (base) => (n > 1 ? `${base} (${n})` : base);
  const names = selectedNames(entry);
  openMenu({ x: ev.clientX, y: ev.clientY }, [
    entry && n === 1 && !entry.is_dir
      ? { label: "Download", icon: "↓", onClick: () => downloadFile(ctx(), entry.name) } : null,
    n ? { label: many("Cut"), icon: "✂", onClick: () => clipSet("cut", names) } : null,
    n ? { label: many("Copy"), icon: "⧉", onClick: () => clipSet("copy", names) } : null,
    clip ? { label: "Paste", icon: "⎘", sub: clip.host, onClick: () => doPaste() } : null,
    n ? "sep" : null,
    n === 1 ? { label: "Rename", icon: "✎", kbd: "F2", onClick: () => startRename(entry.name) } : null,
    n ? { label: many("Delete"), icon: "✕", danger: true, onClick: () => doDelete(names) } : null,
    entry || clip ? "sep" : null,
    { label: "New folder", icon: "+", onClick: doNewFolder },
  ].filter(Boolean));
}

function clipSet(op, names) {
  if (!names.length) return;
  clip = {
    op, host: cwd.host,
    items: names.map((nm) => {
      const e = entryByName(nm);
      return { path: joinPath(cwd.path, nm), name: nm, isDir: !!(e && e.is_dir) };
    }),
  };
  renderTable();   // show cut items dimmed
}

async function doPaste() {
  if (!clip) return;
  await pasteClip(ctx(), clip, (c) => { clip = c; }, (pasted) => {
    reload(true).then(() => {
      sel = new Set(pasted);
      anchor = cursor = pasted[pasted.length - 1] || null;
      paintSelection();
    });
  });
}

async function doDelete(names) {
  if (!(await deleteSelected(ctx(), names))) return;
  if (clip) {
    const gone = new Set(names.map((n) => joinPath(cwd.path, n)));
    clip.items = clip.items.filter((it) => !gone.has(it.path));
    if (!clip.items.length) clip = null;
  }
  sel = new Set();
  anchor = cursor = null;
  await reload(true);
}

async function doNewFolder() {
  if (!ready()) return;
  const nn = await newFolder(ctx());
  if (!nn) return;
  await reload(true);
  if (entryByName(nn)) {
    sel = new Set([nn]);
    anchor = cursor = nn;
    paintSelection();
  }
}

// -------------------------------------------------------------- inline edit
// Rename is inline, GNOME-style: the name cell becomes an input, Enter commits
// (the stem is preselected), Esc or a click away cancels. The background
// refresh pauses while one is open.

function startRename(name) {
  const tr = tbody.querySelector(`tr[data-key="${CSS.escape(name)}"]`);
  const nm = tr && tr.querySelector(".nm");
  if (!nm) return;
  editing = name;
  const at = { ...cwd };                 // the dir this edit belongs to, fixed now
  const input = h("input.inline-edit", {
    value: name,
    onclick: (ev) => ev.stopPropagation(),
    ondblclick: (ev) => ev.stopPropagation(),
  });
  fill(nm, input);
  input.focus();
  const dot = name.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : name.length);
  let done = false;
  const finish = () => { editing = null; renderTable(); };
  const commit = async () => {
    if (done) return;
    done = true;
    editing = null;
    const nn = input.value.trim();
    if (!nn || nn === name || nn.includes("/") || nn === "." || nn === "..") return renderTable();
    if (at.host !== cwd.host || at.path !== cwd.path) return renderTable();   // navigated away
    const from = joinPath(at.path, name), to = joinPath(at.path, nn);
    if (await fileOp({ ...ctx(), host: at.host, path: at.path }, { op: "rename", path: from, dest: to })) {
      sel = new Set([nn]);
      anchor = cursor = nn;
    }
    await reload(true);
  };
  input.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    if (ev.key === "Enter") commit();
    else if (ev.key === "Escape") { done = true; finish(); }
  });
  input.addEventListener("blur", () => { if (!done) { done = true; finish(); } });
}

function wireKeys() {
  table.addEventListener("keydown", (e) => {
    if (e.target !== table) return;            // the inline input keeps its own keys
    const names = sortedEntries().map((x) => x.name);
    const only = sel.size === 1 ? [...sel][0] : null;
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!names.length) return;
      const i = names.indexOf(cursor);
      const ni = e.key === "ArrowDown"
        ? Math.min(names.length - 1, i + 1)
        : Math.max(0, i < 0 ? 0 : i - 1);
      if (e.shiftKey && anchor) selectRange(names[ni]);
      else selectFile(names[ni]);
    } else if (e.key === "Enter" && only) {
      const x = entryByName(only);
      if (x) openEntry(x);
    } else if (e.key === "F2" && only) {
      e.preventDefault();
      startRename(only);
    } else if (e.key === "Delete" && sel.size) {
      e.preventDefault();
      doDelete([...sel]);
    } else if (e.ctrlKey && (e.key === "x" || e.key === "X")) {
      e.preventDefault();
      clipSet("cut", selectedNames(only ? entryByName(only) : null));
    } else if (e.ctrlKey && (e.key === "c" || e.key === "C")) {
      e.preventDefault();
      clipSet("copy", selectedNames(only ? entryByName(only) : null));
    } else if (e.ctrlKey && (e.key === "v" || e.key === "V")) {
      e.preventDefault();
      doPaste();
    } else if (e.key === "Backspace") {
      e.preventDefault();
      goUp();
    } else if (e.ctrlKey && (e.key === "a" || e.key === "A")) {
      e.preventDefault();
      sel = new Set(names);
      anchor = names[0] || null;
      cursor = names[names.length - 1] || null;
      paintSelection();
    } else if (e.ctrlKey && e.key === "l") {
      e.preventDefault();
      showPathInput(true);                     // GNOME muscle memory: type a path
    }
  });
}

// -------------------------------------------------------------------- crumbs

function renderCrumbs() {
  const kids = [h("span.host", cwd.host || "local"), h("span.sep", "·")];
  if (cwd.path) {
    const abs = cwd.path.startsWith("/");
    const segs = cwd.path.split("/").filter(Boolean);
    const parts = abs ? ["/", ...segs] : (segs.length ? segs : ["~"]);
    let prefix = "";
    parts.forEach((seg, i) => {
      if (i > 1 || (i === 1 && parts[0] !== "/")) kids.push(h("span.sep", "/"));
      prefix = i === 0 ? seg : (prefix === "/" ? "/" + seg : `${prefix}/${seg}`);
      const last = i === parts.length - 1;
      kids.push(h(`button.crumb${last ? ".current" : ""}`, { type: "button", dataset: { path: prefix } },
        last ? h("b", seg) : seg));
    });
  }
  const tip = cwd.host ? `${cwd.host} · ${shortPath(cwd.path)}` : "files";
  fill(crumbsEl, ...kids);
  crumbsEl.title = tip + " — click the empty space to type a path";
  crumbsEl.scrollLeft = crumbsEl.scrollWidth;  // deep paths: keep the tail visible
}

// Clicking the crumbs' blank area swaps to a plain text input; Enter jumps,
// Esc/blur swaps back.
//
// `pathInput` is nulled BEFORE the node is removed: removing a focused input
// makes Chrome fire its blur synchronously, inside remove() -- a blur handler
// that reached for the DOM first would re-enter this function mid-call and
// remove() a node that the outer call had already detached (NotFoundError).
function showPathInput(on) {
  if (on) {
    if (pathInput) return void pathInput.focus();
    const segs = [...crumbsEl.children];
    const input = h("input.input.path-input", {
      value: cwd.path, spellcheck: false, "aria-label": "Path",
      onkeydown: (e) => {
        e.stopPropagation();
        if (e.key === "Enter") {
          e.preventDefault();
          loadFiles(cwd.host || "local", input.value.trim() || "~");
        } else if (e.key === "Escape") {
          e.preventDefault();
          showPathInput(false);
        }
      },
      onblur: () => { if (pathInput === input) showPathInput(false); },
      onclick: (e) => e.stopPropagation(),
    });
    pathInput = input;
    fill(crumbsEl, segs[0], segs[1], input);
    input.focus();
    input.select();
    return;
  }
  const i = pathInput;
  if (!i) return;
  pathInput = null;
  i.remove();
  renderCrumbs();
}

// ----------------------------------------------------------------- open/size

function setOpen(v) {
  open = v;
  drawer.classList.toggle("open", v);
  chev.textContent = v ? "▾" : "▴";
  remember("filesOpen", v);
}

function onHandleClick(e) {
  if (e.target.closest(".crumbs, button")) return;   // crumbs and actions act, not toggle
  setOpen(!open);
}

function applyHeight(hgt) {
  const max = Math.max(MIN_H, Math.round(window.innerHeight * 0.7));
  body.style.height = `${Math.max(MIN_H, Math.min(hgt, max))}px`;
}

let resizing = false, resizeFrom = 0, resizeH0 = 0;
function startResize(e) {
  e.preventDefault();
  resizing = true;
  resizeFrom = e.clientY;
  resizeH0 = body.getBoundingClientRect().height;
  document.body.classList.add("resizing");
}
window.addEventListener("mousemove", (e) => {
  if (resizing) applyHeight(resizeH0 + (resizeFrom - e.clientY));   // drag up -> taller
});
window.addEventListener("mouseup", () => {
  if (!resizing) return;
  resizing = false;
  document.body.classList.remove("resizing");
  remember("filesHeight", Math.round(body.getBoundingClientRect().height));
});
window.addEventListener("resize", () => {
  // re-clamp the live height to the new window; closed, the body has no rect
  if (open) applyHeight(Math.round(body.getBoundingClientRect().height));
});

// -------------------------------------------------------------------- upload
// Drag OS files onto the drawer to upload them to the current directory.
// Handled at the window level: a drop that misses must still be swallowed --
// the browser default is to NAVIGATE to the dropped file, replacing the app.
// Only external drags react (types includes "Files"), so downloading a row by
// dragging it out is untouched.

const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
const overDrawer = (e) => e.target instanceof Element && !!e.target.closest(".drawer");
const setDropHighlight = (on) => drawer.classList.toggle("drop-target", on);

function wireDrop() {
  window.addEventListener("dragover", (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();                        // window-wide: makes every release safe
    const over = overDrawer(e);
    e.dataTransfer.dropEffect = over ? "copy" : "none";
    setDropHighlight(over);
  });
  window.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) setDropHighlight(false);   // left the window entirely
  });
  window.addEventListener("drop", async (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    setDropHighlight(false);
    if (!overDrawer(e)) return;
    if (ready() && await uploadFiles(ctx(), e.dataTransfer.files)) reload(true);
  });
}
