// The phone layout (approved mockup 08): triage and short replies.
//
// One list -- the board and the rail are the same thing here -- needs-you
// first, each card with the pane's tail inline. Tap a card: the terminal owns
// the screen, with a quick-switch strip of recent sessions and a key bar for
// what a soft keyboard lacks (esc, tab, ⇧⇥, ^C, arrows), plus select and
// paste. ⋮ opens a sheet with the session's actions and its files, without
// attaching first.
//
// It is a second arrangement of the same app, not a second app: the panes,
// the bridge, the dialogs and every feature are the desktop's own. On a phone
// the #panes element is moved into the terminal screen (xterm refits on its
// own), and only the focused pane shows.

import { h, fill, keyed, ago } from "../core/dom.js";
import { post, keyOf } from "../core/api.js";
import { confirm, toast, isOpen as dialogOpen } from "../core/dialog.js";
import { openMenu } from "../core/menu.js";
import * as store from "../core/state.js";
import { remember } from "../core/prefs.js";
import { kindInfo, shortPath } from "../term/pane.js";
import * as panes from "../views/panes.js";
import { tagMenu, accountMenu } from "../views/topbar.js";
import { isFavorite, setFavorite, relaunch, closeSession } from "../views/rail.js";
import { openNew, openEdit } from "../features/profile.js";
import { openFavorites, launchTarget } from "../features/favorites.js";
import { openPalette } from "../features/palette.js";
import { listDir, joinPath, fmtSize, downloadFile } from "../features/files-ops.js";

const PHONE = window.matchMedia("(max-width: 720px)");
const ORDER = ["needs_input", "stuck", "running", "done", "idle"];
const SECTION = { needs_input: "needs you", stuck: "looping", running: "working", done: "done", idle: "idle" };
const RECENT_MAX = 5;

let root, listScreen, termScreen, listEl, header, qswitch, keybarEl, selBanner, termHost;
let panesEl = null, panesHome = null;
let screen = "list";
let selOverlay = null;

export function mount() {
  panesEl = document.getElementById("panes");
  panesHome = panesEl.parentElement;
  build();
  PHONE.addEventListener("change", apply);
  store.subscribe(["sessions", "saved", "tag", "kinds", "focusedKey"], () => { if (PHONE.matches) render(); });
  apply();
}

/** Switch between the desktop and phone arrangements as the viewport changes. */
function apply() {
  const on = PHONE.matches;
  document.body.classList.toggle("phone", on);
  root.hidden = !on;
  if (on) {
    termHost.append(panesEl);
    // Arriving on a phone with a session already open: start on the list;
    // the terminal is one tap away and the list is the triage view.
    show(screen);
    fitViewport();
    render();
  } else {
    exitSelect();
    panesHome.prepend(panesEl);
  }
}

function build() {
  // -- list screen
  const top = h("div.ptop",
    h("div.brand", "ser", h("b", "a"), "i"),
    h("button.btn.tagpick", { type: "button", onclick: (e) => tagMenu(e.currentTarget) }),
    h("span.spacer"),
    h("button.btn.icon", { type: "button", title: "Jump to session", onclick: () => openPalette() }, "⌕"),
    h("button.avatar", { type: "button", title: "Account", onclick: (e) => accountMenu(e.currentTarget) }));
  listEl = h("div.plist");
  const fab = h("button.fab", { type: "button", title: "New session", onclick: () => openNew() }, "+");
  const nav = h("nav.pnav",
    h("button.on", { type: "button", onclick: () => show("list") }, h("i", "☰"), "sessions"),
    h("button", { type: "button", onclick: () => openFavorites() }, h("i", "★"), "favorites"),
    h("button", { type: "button", onclick: () => openPalette() }, h("i", "⌕"), "jump"));
  listScreen = h("section.p-list", top, listEl, fab, nav);

  // -- terminal screen
  header = h("div.ahead");
  qswitch = h("div.qswitch");
  selBanner = h("div.selbanner", { hidden: true },
    h("b", "SELECT"), h("span", "drag the handles, then Copy"),
    h("button.btn.sm", { type: "button", onclick: exitSelect }, "done"));
  termHost = h("div.p-termhost");
  keybarEl = buildKeybar();
  termScreen = h("section.p-term", header, qswitch, selBanner, termHost, keybarEl);

  root = h("div#phone", { hidden: true }, listScreen, termScreen);
  document.getElementById("app").append(root);

  if (window.visualViewport) {
    visualViewport.addEventListener("resize", fitViewport);
    visualViewport.addEventListener("scroll", fitViewport);
  }
  window.addEventListener("resize", fitViewport);
}

/** The phone UI is exactly the visible viewport tall, so when the soft keyboard
 *  opens the terminal shrinks above it (and the pane refits and tells tmux)
 *  instead of the keyboard covering the last lines (mockup frame 3). */
function fitViewport() {
  if (!PHONE.matches) return;
  const vv = window.visualViewport;
  const height = vv ? vv.height : window.innerHeight;
  const top = vv ? vv.offsetTop : 0;
  root.style.height = `${Math.round(height)}px`;
  root.style.transform = top ? `translateY(${Math.round(top)}px)` : "";
}

function show(which) {
  screen = which;
  listScreen.hidden = which !== "list";
  termScreen.hidden = which !== "term";
  if (which !== "term") exitSelect();
  if (which === "term") {
    const p = panes.focusedPane();
    if (p) setTimeout(() => { p.refit(); }, 0);
  }
}

// -- list -----------------------------------------------------------------

function visibleSessions() {
  const { sessions, tag } = store.get();
  return tag ? sessions.filter((s) => (s.tags || []).includes(tag)) : sessions;
}

function render() {
  const st = store.get();
  const av = listScreen.querySelector(".avatar");
  av.hidden = !(st.auth && st.auth.enabled);           // as on desktop: no sign-in, no account
  av.textContent = st.auth && st.auth.user ? st.auth.user[0].toUpperCase() : "";
  const tagBtn = listScreen.querySelector(".tagpick");
  fill(tagBtn, h("span.ico", "◇"), st.tag || "all tags", " ▾");
  const sessions = visibleSessions();
  const sections = ORDER.map((state) => [state, sessions.filter((s) => s.state === state)
    .sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name))]).filter(([, l]) => l.length);
  if (!sessions.length) {
    fill(listEl, h("div.rail-empty", h("div", "No sessions yet."), h("div.dim", "Tap + to start one.")));
  } else {
    if (listEl.querySelector(".rail-empty")) listEl.replaceChildren();
    keyed(listEl, sections, ([state]) => `sec:${state}`, ([state, items]) => {
      const body = h("div.psec-body");
      const el = h("div.psec-wrap", h("div.psec", h("span", SECTION[state]), h("span.count")), body);
      updateSection(el, state, items);
      return el;
    }, (el, [state, items]) => updateSection(el, state, items));
  }
  renderHeader();
  renderQuickSwitch();
}

function updateSection(el, state, items) {
  el.querySelector(".count").textContent = String(items.length);
  // idle sessions are many and quiet: compact cards without the tail
  keyed(el.querySelector(".psec-body"), items, keyOf, (s) => card(s, state === "idle"), (c, s) => updateCard(c, s));
}

function card(s, compact) {
  const el = h(`div.pcard${compact ? ".compact" : ""}`, { role: "button", tabindex: "0" },
    h("div.h", h("span.dot"), h("span.kind"), h("span.name"), h("span.fav", "★"), h("span.age"),
      h("button.more", { type: "button", "aria-label": "Session actions" }, "⋮")),
    compact ? null : h("div.tail"),
    compact ? null : h("div.state", h("b"), h("span.host")));
  el.addEventListener("click", (e) => {
    if (e.target.closest(".more")) return;
    openSession(el._s);
  });
  el.querySelector(".more").addEventListener("click", (e) => { e.stopPropagation(); sheet(el._s); });
  updateCard(el, s);
  return el;
}

function updateCard(el, s) {
  el._s = s;
  const info = kindInfo(s.kind);
  const cls = store.STATE_CLASS[s.state] || "idle";
  el.classList.remove("blocked", "working", "stuck", "done", "idle");
  el.classList.add(cls);
  const dot = el.querySelector(".dot");
  dot.className = `dot ${cls}${cls === "working" || cls === "blocked" ? " pulse" : ""}`;
  const chip = el.querySelector(".kind");
  chip.className = `kind ${info.chip}`;
  chip.textContent = info.chip.toUpperCase();
  el.querySelector(".name").textContent = s.label || s.name;
  el.querySelector(".fav").hidden = !isFavorite(s);
  el.querySelector(".age").textContent = s.state === "running" ? "now" : ago(s.age);
  const tail = el.querySelector(".tail");
  if (tail) tail.textContent = String(s.tail || "").split("\n").slice(-3).join("\n");
  const stateB = el.querySelector(".state b");
  if (stateB) {
    stateB.textContent = store.STATE_LABEL[s.state] || s.state;
    el.querySelector(".state .host").textContent = s.host === "local" ? "" : `· ${s.host}`;
  }
}

// -- terminal screen ---------------------------------------------------------

function openSession(s) {
  remember("phoneRecent", [keyOf(s), ...(remember("phoneRecent") || []).filter((k) => k !== keyOf(s))].slice(0, RECENT_MAX));
  panes.open({ host: s.host, name: s.name, kind: s.kind, label: s.label });
  show("term");
  render();
}

function focusedSession() {
  const p = panes.focusedPane();
  if (!p || !p.target) return null;
  return store.session(p.target.host, p.target.name) || { ...p.target, state: "idle", tags: [] };
}

function renderHeader() {
  const s = focusedSession();
  if (!s) { fill(header, h("button.back", { type: "button", onclick: () => show("list") }, "‹")); return; }
  const info = kindInfo(s.kind);
  const cls = store.STATE_CLASS[s.state] || "idle";
  fill(header,
    h("button.back", { type: "button", title: "All sessions", onclick: () => show("list") }, "‹"),
    h("div.title", h(`span.dot.${cls}`), h(`span.kind.${info.chip}`, info.chip.toUpperCase()), h("span.name", s.label || s.name)),
    h("button.btn", { type: "button", title: "Session actions", onclick: () => sheet(s) }, "⋮"));
}

function renderQuickSwitch() {
  const recent = (remember("phoneRecent") || [])
    .map((k) => store.get().sessions.find((s) => keyOf(s) === k)).filter(Boolean);
  const cur = focusedSession();
  qswitch.hidden = recent.length < 2;
  fill(qswitch, recent.map((s) => h(`button${cur && keyOf(cur) === keyOf(s) ? ".on" : ""}`, {
    type: "button", onclick: () => openSession(s),
  }, h(`span.dot.${store.STATE_CLASS[s.state] || "idle"}`), s.label || s.name)));
}

// -- key bar -------------------------------------------------------------------

function send(seq) {
  const p = panes.focusedPane();
  if (!p || !p.send(new TextEncoder().encode(seq))) toast("not connected", "warn", 2000);
}

// Arrow keys honour the app's cursor-key mode (a TUI usually turns on
// "application" mode, where ↑ is ESC O A rather than ESC [ A).
function arrow(dir) {
  const p = panes.focusedPane();
  const app = p && p.term.modes.applicationCursorKeysMode;
  send(`\x1b${app ? "O" : "["}${dir}`);
}

function buildKeybar() {
  const k = (label, title, fn, cls = "") => {
    const b = h(`button.k${cls}`, { type: "button", title }, label);
    // Never take focus: the soft keyboard must stay up while you tap keys.
    b.addEventListener("mousedown", (e) => e.preventDefault());
    b.addEventListener("touchstart", (e) => { e.preventDefault(); fn(); }, { passive: false });
    b.addEventListener("click", fn);
    return b;
  };
  return h("div.keybar", { role: "toolbar", "aria-label": "Terminal keys" },
    k("esc", "Escape", () => send("\x1b")),
    k("tab", "Tab", () => send("\t")),
    k("⇧⇥", "Shift+Tab", () => send("\x1b[Z")),
    k("^C", "Ctrl+C (interrupt)", () => send("\x03"), ".danger"),
    k("↑", "Up", () => arrow("A")),
    k("↓", "Down", () => arrow("B")),
    h("span.sep"),
    k("select", "Select text with the phone's own handles", toggleSelect, ".wide.acc.selbtn"),
    k("paste", "Paste", () => { const p = panes.focusedPane(); if (p) p.clip.pasteFromClipboard(); }, ".wide.acc"),
    k("⋯", "More keys", moreKeys, ".wide"));
}

function moreKeys() {
  const at = keybarEl.querySelector(".k:last-child").getBoundingClientRect();
  openMenu({ left: at.left - 160, bottom: at.top - 220 }, [
    { label: "←  left", onClick: () => arrow("D") },
    { label: "→  right", onClick: () => arrow("C") },
    { label: "^D  end of input", onClick: () => send("\x04") },
    { label: "^Z  suspend", onClick: () => send("\x1a") },
    { label: "^L  clear screen", onClick: () => send("\x0c") },
    { label: "^R  history search", onClick: () => send("\x12") },
    "sep",
    { label: "|", onClick: () => send("|") },
    { label: "~", onClick: () => send("~") },
    { label: "/", onClick: () => send("/") },
  ]);
}

// -- select mode -----------------------------------------------------------------
// A canvas or a mouse-handling terminal can't hand selection to the OS. So
// select mode lays a plain-text copy of the buffer (what is on screen plus
// recent scrollback) over the terminal: ordinary text, so the phone's own
// handles, magnifier and Copy popup all just work (mockup frame 4).

const SELECT_LINES = 500;

function toggleSelect() {
  if (selOverlay) exitSelect(); else enterSelect();
}

function enterSelect() {
  const p = panes.focusedPane();
  if (!p || !p.target) return;
  const buf = p.term.buffer.active;
  const end = buf.baseY + p.term.rows;
  const start = Math.max(0, end - SELECT_LINES);
  const lines = [];
  for (let i = start; i < end; i++) {
    const line = buf.getLine(i);
    lines.push(line ? line.translateToString(true) : "");
  }
  while (lines.length && !lines[lines.length - 1]) lines.pop();
  selOverlay = h("pre.selover", { tabindex: "-1" }, lines.join("\n"));
  p.el.querySelector(".term").append(selOverlay);
  selOverlay.scrollTop = selOverlay.scrollHeight;     // land where the screen was
  selBanner.hidden = false;
  keybarEl.querySelector(".selbtn").classList.add("on");
  document.activeElement && document.activeElement.blur();   // put the soft keyboard away
}

function exitSelect() {
  if (!selOverlay) return;
  selOverlay.remove();
  selOverlay = null;
  selBanner.hidden = true;
  keybarEl.querySelector(".selbtn").classList.remove("on");
}

// -- ⋮ sheet: actions + files (mockup frame 5) ------------------------------------

let sheetEl = null;

function closeSheet() {
  if (sheetEl) { sheetEl.remove(); sheetEl = null; }
}

function sheet(s) {
  closeSheet();
  const info = kindInfo(s.kind);
  const fav = isFavorite(s);
  const dir = s.dir || s.path || "~";
  const saved = (store.get().saved || []).find((r) => r.host === s.host && r.name === s.name);
  const files = h("div.sfiles");
  const row = (icon, label, meta, fn, cls = "") =>
    h(`button.frow${cls}`, { type: "button", onclick: fn }, h("span.ico", icon), label, meta ? h("span.meta", meta) : null);
  const panel = h("div.sheet", { role: "dialog", "aria-label": `${s.label || s.name} actions` },
    h("div.grab"),
    h("div.shead", h(`span.kind.${info.chip}`, info.chip.toUpperCase()), h("b", s.label || s.name),
      h("span", `· ${s.host} · ${shortPath(dir)}`),
      h("button.btn.sm", { type: "button", onclick: async () => { closeSheet(); await setFavorite(s, !fav); } },
        fav ? "★ favorite" : "☆ favorite")),
    row("✎", "Edit session…", "", () => { closeSheet(); openEdit(s); }),
    info.agent && s.at_prompt
      ? row("▶", `Relaunch ${info.cmd}`, "", async () => { closeSheet(); await relaunch(s); })
      : row("↻", "Restart…", "", () => { closeSheet(); restart(s, saved); }),
    row("✕", "Close session…", "", async () => { closeSheet(); await closeSession(s); }, ".danger"),
    h("div.sfiles-head", "files"),
    files);
  sheetEl = h("div.sheet-scrim", { onclick: (e) => { if (e.target === sheetEl) closeSheet(); } }, panel);
  document.body.append(sheetEl);
  loadSheetFiles(files, s.host, dir);
}

async function loadSheetFiles(box, host, path) {
  fill(box, h("div.frow.dim", "loading…"));
  let entries;
  try {
    entries = await listDir(host, path);
  } catch (err) {
    fill(box, h("div.frow.dim", err.message));
    return;
  }
  entries.sort((a, b) => (a.is_dir === b.is_dir ? a.name.localeCompare(b.name) : a.is_dir ? -1 : 1));
  const ctx = { host, path, entries, setStatus: (t) => { if (t) toast(t, "ok", 1500); } };
  fill(box,
    h("button.frow", { type: "button", onclick: () => loadSheetFiles(box, host, joinPath(path, "..")) },
      h("span.ico", "↑"), "..", h("span.meta", shortPath(path))),
    entries.map((e) => h("button.frow", {
      type: "button",
      onclick: () => (e.is_dir ? loadSheetFiles(box, host, joinPath(path, e.name)) : downloadFile(ctx, e.name)),
    }, h("span.ico", e.is_dir ? "▸" : "·"), e.name, h("span.meta", e.is_dir ? "dir" : fmtSize(e.size)))));
}

async function restart(s, saved) {
  const ok = await confirm({
    title: `Restart ${s.label || s.name}?`,
    message: "This ends the session and starts it again from its saved profile. Anything running in it is lost.",
    confirmLabel: "Restart",
    danger: true,
  });
  if (!ok) return;
  try {
    await post("/api/sessions/restart", { host: s.host, name: s.name });
    const target = saved ? launchTarget(saved) : { host: s.host, name: s.name, kind: s.kind, label: s.label };
    panes.open({ ...target, fresh: true });
    show("term");
  } catch (err) {
    toast(err.message, "error", 5000);
  }
}

// Esc on a phone keyboard (or a hardware one) closes the sheet first.
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && sheetEl && !dialogOpen()) { e.stopPropagation(); closeSheet(); }
}, true);
