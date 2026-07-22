"use strict";

const KIND_GLYPH = { claude: "\u2726", shell: "\u276f" }; // ✦  ❯
const REFRESH_MS = 5000;
const MAX_REATTACH = 6;   // consecutive failed reattaches before giving up
const STABLE_MS = 6000;   // a session must stay up this long to count as healthy

let hosts = [];
let sessionList = [];
let groupMode = "tag";   // the rail groups by tag; the old host/all modes are retired
let filterText = "";
// Per-terminal state — the xterm instance, its websocket, reconnect timers, and
// the session it's attached to — now lives on each pane object (see createPane).
let fleetMode = false;    // fleet broadcast: pick sessions, send one command to all
const selected = new Map(); // session id -> {host, name}
let sessionsLoaded = false; // false until the first /api/sessions returns

// Terminal settings (theme/font/size + scrollback, columns, cursor, bold) are
// driven by saved prefs with a live picker in the UI; presets live in themes.js.
const TERM_DEFAULTS = {
  theme: "GNOME Dark",
  font: "'DejaVu Sans Mono', monospace",
  size: 15,
  scrollback: 5000,
  cols: 0,            // 0 = fit the terminal to its pane; >0 = fixed column count
  cursorStyle: "block",
  cursorBlink: true,
  boldBright: true,
  scrollbackMouse: true,  // tmux mouse mode on attach -> wheel scrolls history (Shift+drag to select)
};
function loadTermSettings() {
  try { return { ...TERM_DEFAULTS, ...JSON.parse(localStorage.getItem("serai.term") || "{}") }; }
  catch { return { ...TERM_DEFAULTS }; }
}
let termSettings = loadTermSettings();

// --- terminal panes --------------------------------------------------------
// The terminal area holds one or two panes side by side, each with its own
// xterm instance and tmux websocket. The "focused" pane is the target of the
// sidebar, the new-session form, the settings, and the clipboard actions.
// Lifting the old single global term/ws into a per-pane object is what lets you
// watch (and type into) two sessions at once.

const MAX_PANES = 6;
let layoutMode = (() => { try { return localStorage.getItem("serai.layout") === "grid" ? "grid" : "row"; } catch { return "row"; } })();
let dragSrcPane = null; // the pane currently being drag-reordered
const panes = [];      // [{el,title,termEl,term,fit,ws,active,attachGen,reconnect…}]
let focused = null;    // the pane the sidebar / settings act on
const termRow = document.getElementById("term-row");
const termMenu = document.getElementById("term-menu");
let menuPane = null;   // the pane the right-click menu was last opened over
let paneDivider = null; // the draggable bar between the two panes (when split)

function termOptions() {
  return {
    fontFamily: termSettings.font,
    fontSize: termSettings.size,
    scrollback: termSettings.scrollback,
    cursorBlink: termSettings.cursorBlink,
    cursorStyle: termSettings.cursorStyle,
    drawBoldTextInBrightColors: termSettings.boldBright,
    theme: TERM_THEMES[termSettings.theme] || TERM_THEMES["GNOME Dark"],
  };
}

// Size a pane's terminal: fit to its area, or to a fixed column count (rows
// always follow the pane height). paneRefit() also pushes the size to tmux.
function paneFit(p) {
  if (termSettings.cols > 0) {
    const dims = p.fit.proposeDimensions();
    p.term.resize(termSettings.cols, (dims && dims.rows) || p.term.rows || 24);
  } else {
    p.fit.fit();
  }
}
function paneSendResize(p) {
  if (p.ws && p.ws.readyState === WebSocket.OPEN) {
    p.ws.send(JSON.stringify({ resize: { rows: p.term.rows, cols: p.term.cols } }));
  }
}
function paneRefit(p) { paneFit(p); paneSendResize(p); }
function refitAll() { panes.forEach(paneRefit); }
function fitAll() { panes.forEach(paneFit); }
function sendResizeAll() { panes.forEach(paneSendResize); }

// --- terminal copy / paste -------------------------------------------------
// xterm renders to a canvas (no native copy) and Ctrl+Shift+C is the browser's
// dev-tools shortcut, so wire the clipboard ourselves. Copy-on-select is the
// reliable path (no shortcut collision); Ctrl+Insert / Ctrl+Shift+V / Shift+
// Insert and a right-click menu also work. Clipboard needs a secure context --
// serai is HTTPS (and localhost), so it's available.
function paneCopy(p) {
  const sel = p.term.getSelection();
  if (sel && navigator.clipboard) navigator.clipboard.writeText(sel).catch(() => {});
  return !!sel;
}
async function panePaste(p) {
  try {
    const text = await navigator.clipboard.readText();
    if (text) p.term.paste(text);
  } catch { /* clipboard blocked or empty */ }
}
// Position a fixed context menu at the pointer, flipping above/left of it when
// it would overflow the viewport (fixed elements never scroll into view, so an
// unclamped menu opened near the bottom edge just gets cut off).
function placeMenu(menu, x, y) {
  menu.hidden = false; // must be rendered to measure
  const { width: w, height: h } = menu.getBoundingClientRect();
  const pad = 6;
  let left = x, top = y;
  if (left + w > window.innerWidth - pad) left = Math.max(pad, x - w);  // flip left
  if (top + h > window.innerHeight - pad) top = Math.max(pad, y - h);   // flip up
  menu.style.left = left + "px";
  menu.style.top = top + "px";
}

document.getElementById("tm-copy").addEventListener("click", () => { if (menuPane) paneCopy(menuPane); termMenu.hidden = true; });
document.getElementById("tm-paste").addEventListener("click", () => { if (menuPane) panePaste(menuPane); termMenu.hidden = true; });
window.addEventListener("mousedown", (e) => { if (!termMenu.contains(e.target)) termMenu.hidden = true; });

// Make `p` the focused pane: sidebar clicks, settings, and the files pane (which
// follows the attached session's host) all act on it.
function setFocus(p) {
  focused = p;
  panes.forEach((q) => q.el.classList.toggle("focused", q === p));
  if (p && p.active) showFilesFor(p.active.host, sessionDir(p.active));
  renderPaneTabs();
}

// The pane bar (state dot + name + host). Painted from the live sessionList on
// every poll, not stamped once at attach -- otherwise its dot freezes at whatever
// the state was when you attached while the tab and rail move on, and the same
// session shows two different colours at once.
function paintPaneBar(p) {
  if (!p || !p.active || !p.title) return;
  const t = p.active;
  const st = sessionList.find((x) => x.host === t.host && x.name === t.name);
  const where = t.host === "local" ? "local" : t.host;
  p.title.innerHTML =
    `<i class="dot ${st ? st.state : "idle"}"></i> ` +
    `<strong>${escapeHtml(t.label)}</strong>` +
    `<span class="mono muted" style="margin-left:6px">· ${escapeHtml(where)}</span>`;
}

// Pane tabs above the terminal (the mockup's .ptabs): one per open pane, with
// the attached session's live state dot. Click to focus, x to close, + to split.
function renderPaneTabs() {
  const el = document.getElementById("pane-tabs");
  if (!el) return;
  el.innerHTML = "";
  panes.forEach(paintPaneBar); // keep every pane bar's dot in step with the tabs
  for (const p of panes) {
    const a = p.active;
    const s = a ? sessionList.find((x) => x.host === a.host && x.name === a.name) : null;
    const tab = document.createElement("span");
    tab.className = "ptab" + (p === focused ? " on" : "");
    const label = a ? `${a.kind === "claude" ? "cc · " : ""}${a.label}` : "no session";
    tab.innerHTML =
      `<i class="dot ${s ? s.state : "idle"}"></i>` +
      `<span class="ptab-nm">${escapeHtml(label)}</span>` +
      (panes.length > 1 ? `<button class="x" type="button" title="close this pane">✕</button>` : "");
    tab.onclick = () => setFocus(p);
    const x = tab.querySelector(".x");
    if (x) x.onclick = (ev) => { ev.stopPropagation(); closePane(p); };
    el.appendChild(tab);
  }
  if (panes.length < MAX_PANES) {
    const add = document.createElement("span");
    add.className = "ptab ptab-add";
    add.textContent = "＋";
    add.title = "add another terminal pane";
    add.onclick = () => splitPane();
    el.appendChild(add);
  }
}

function createPane() {
  const el = document.createElement("div");
  el.className = "pane empty"; // 'empty' until a session attaches -> shows the hint
  el.innerHTML =
    `<div class="pane-head">` +
    `<span class="pane-title muted">no session selected</span>` +
    `<button class="pane-close" type="button" title="close this pane" hidden>✕</button>` +
    `</div><div class="pane-term"></div>`;
  termRow.appendChild(el);

  const termEl = el.querySelector(".pane-term");
  const t = new Terminal(termOptions());
  const f = new FitAddon.FitAddon();
  t.loadAddon(f);
  t.open(termEl);
  termEl.style.background = (t.options.theme || {}).background || "#202628";

  // call-to-action shown while the pane has no session (hidden once one attaches)
  const hint = document.createElement("div");
  hint.className = "pane-hint";
  hint.textContent = "Click a session in the sidebar to open it here";
  el.appendChild(hint);

  const p = {
    el, title: el.querySelector(".pane-title"), termEl, term: t, fit: f,
    ws: null, active: null, attachGen: 0,
    reconnectAttempts: 0, reconnectTimer: null, stableTimer: null,
  };

  // Drag to scroll, on touch.
  //
  // An attached session has nothing for the browser to scroll: tmux owns the
  // scrollback and repaints the visible pane, so xterm's own viewport is exactly
  // as tall as its content (measured: scrollHeight === clientHeight). A finger
  // drag therefore moved nothing, and pane history above the fold was
  // unreachable on a phone.
  //
  // What does work is the wheel: xterm encodes it as a mouse event and tmux
  // scrolls into its history (that is what "mouse scrollback" in ⚙ enables). So
  // translate a one-finger vertical drag into the wheel events tmux already
  // understands, rather than trying to make the DOM scroll something that isn't
  // there. With mouse mode off tmux ignores the wheel -- exactly as on desktop.
  // Lines of pane movement per wheel notch we dispatch. tmux nominally scrolls 5
  // (its default `-N 5` on WheelUp/Down in copy-mode), but xterm's own pixel ->
  // notch conversion swallows some of that, and the figure below is what was
  // actually measured end to end against `#{scroll_position}`: four notches move
  // about fifteen lines. Calibrating from the measurement rather than the
  // documented default is what makes the drag track the finger (0.9x) instead of
  // outrunning it -- at one notch per line-height the pane moved ~4.5x the
  // distance dragged, which is what made it feel chunky and uncontrollable.
  // Re-measure this if the wheel path changes; the ratio is the whole feel.
  const LINES_PER_NOTCH = 4;

  function enableTouchScroll(pane) {
    const surface = pane.termEl;
    let lastY = null, lastX = null, acc = 0, mode = null;   // mode: "scroll" | "ignore"

    const lineHeight = () => {
      const vp = surface.querySelector(".xterm-viewport");
      const rows = pane.term.rows || 24;
      return Math.max(12, Math.round(((vp && vp.clientHeight) || 480) / rows));
    };
    // px of drag per wheel notch, so content tracks the finger 1:1
    const notchDistance = () => Math.max(16, Math.round(lineHeight() * LINES_PER_NOTCH));

    // Coalesce the line count and send at most one frame per SCROLL_MS. Without
    // this a fast drag would queue a command per touchmove, and over ssh they
    // would still be arriving after the finger stopped -- the pane would coast
    // past where you let go.
    let pendingLines = 0, scrollTimer = null, lastSent = 0;
    const SCROLL_MS = 55;
    function flushScroll() {
      if (scrollTimer) return;
      const wait = Math.max(0, SCROLL_MS - (Date.now() - lastSent));
      scrollTimer = setTimeout(() => {
        scrollTimer = null;
        const lines = pendingLines; pendingLines = 0;
        if (!lines) return;
        lastSent = Date.now();
        try {
          pane.ws.send(JSON.stringify({ scroll: { lines } }));
        } catch { /* socket went away mid-gesture -- the drag just stops */ }
      }, wait);
    }

    surface.addEventListener("touchstart", (ev) => {
      if (ev.touches.length !== 1) { mode = "ignore"; return; }
      lastY = ev.touches[0].clientY; lastX = ev.touches[0].clientX;
      acc = 0; mode = null;                     // decided on the first move
    }, { passive: true });

    surface.addEventListener("touchmove", (ev) => {
      if (mode === "ignore" || lastY === null || ev.touches.length !== 1) return;
      const y = ev.touches[0].clientY, x = ev.touches[0].clientX;
      if (mode === null) {
        // Claim the gesture only once it is clearly vertical, so a horizontal
        // drag still selects text or reaches anything else listening.
        const dy = Math.abs(y - lastY), dx = Math.abs(x - lastX);
        if (dy < 6 && dx < 6) return;
        mode = dy > dx ? "scroll" : "ignore";
        if (mode === "ignore") return;
      }
      acc += y - lastY; lastY = y; lastX = x;

      ev.preventDefault();     // stop the page rubber-banding under the terminal

      // Preferred path: ask tmux to scroll an exact number of lines, over the
      // socket that is already open. One line of travel moves one line of pane,
      // which is as fine as tmux goes -- the wheel below can only land on
      // ~4-line notches, which is what made this lurch.
      if (pane.ws && pane.ws.readyState === WebSocket.OPEN) {
        const lines = Math.trunc(acc / lineHeight());
        if (!lines) return;
        acc -= lines * lineHeight();
        pendingLines += lines;                      // finger down (+) => back in history
        flushScroll();
        return;
      }

      // Fallback (socket not up): the wheel, calibrated to track the finger.
      const step = notchDistance();
      const notches = Math.trunc(acc / step);
      if (!notches) return;
      acc -= notches * step;
      const vp = surface.querySelector(".xterm-viewport");
      if (!vp) return;
      // finger down => older output, which is wheel *up* (negative deltaY)
      const delta = notches > 0 ? -lineHeight() : lineHeight();
      // A fast flick can cover many notches in one event; cap it so a stray
      // gesture can't fling the pane through hundreds of lines of history.
      for (let i = 0; i < Math.min(Math.abs(notches), 4); i++) {
        vp.dispatchEvent(new WheelEvent("wheel", {
          deltaY: delta, deltaMode: 0, bubbles: true, cancelable: true,
        }));
      }
    }, { passive: false });

    const end = () => { lastY = lastX = null; acc = 0; mode = null; };
    surface.addEventListener("touchend", end, { passive: true });
    surface.addEventListener("touchcancel", end, { passive: true });
  }

  // keystrokes -> this pane's websocket
  t.onData((d) => {
    if (p.ws && p.ws.readyState === WebSocket.OPEN) p.ws.send(new TextEncoder().encode(d));
  });
  // auto-copy a selection when the mouse is released ("copy on select")
  t.element.addEventListener("mouseup", () => { paneCopy(p); });
  enableTouchScroll(p);
  t.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown" || !(e.ctrlKey || e.shiftKey)) return true;
    const ctrl = e.ctrlKey, shift = e.shiftKey;
    // copy: Ctrl+Shift+C or Ctrl+Insert
    if ((ctrl && shift && e.code === "KeyC") || (ctrl && !shift && e.code === "Insert")) {
      if (paneCopy(p)) { e.preventDefault(); return false; }
      return true; // nothing selected -> let Ctrl+C through (SIGINT)
    }
    // paste: Ctrl+Shift+V or Shift+Insert
    if ((ctrl && shift && e.code === "KeyV") || (shift && !ctrl && e.code === "Insert")) {
      e.preventDefault(); panePaste(p); return false;
    }
    return true;
  });
  // tmux copy-mode selections (a plain drag while mouse mode is on) arrive as
  // OSC 52 -- bridge them to the system clipboard. Unlike Shift+drag (xterm's
  // own selection, limited to what xterm holds on screen), tmux copy mode
  // auto-scrolls through the full history, so long copies work. Payload is
  // "<targets>;<base64 text>"; "?" is a clipboard *query*, never write on it.
  t.parser.registerOscHandler(52, (data) => {
    const b64 = data.slice(data.indexOf(";") + 1);
    if (!b64 || b64 === "?") return true;
    try {
      const bytes = Uint8Array.from(atob(b64), (c) => c.charCodeAt(0));
      const text = new TextDecoder().decode(bytes); // base64 wraps UTF-8 bytes
      if (text && navigator.clipboard) navigator.clipboard.writeText(text).catch(() => {});
    } catch { /* malformed payload -- ignore */ }
    return true; // handled -- don't let it fall through
  });
  // right-click menu: Copy (when there's a selection) / Paste, for this pane
  t.element.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    menuPane = p;
    document.getElementById("tm-copy").classList.toggle("disabled", !p.term.getSelection());
    placeMenu(termMenu, e.clientX, e.clientY);
  });
  // any click into the pane focuses it (so the next sidebar click targets it)
  el.addEventListener("mousedown", () => setFocus(p), true);
  const closeBtn = el.querySelector(".pane-close");
  closeBtn.setAttribute("draggable", "false");
  closeBtn.addEventListener("click", (e) => { e.stopPropagation(); closePane(p); });

  // drag a pane by its header to rearrange it (swaps with the drop-target pane)
  const head = el.querySelector(".pane-head");
  head.setAttribute("draggable", "true");
  head.addEventListener("dragstart", (e) => {
    dragSrcPane = p; el.classList.add("dragging");
    e.dataTransfer.effectAllowed = "move";
    try { e.dataTransfer.setData("text/plain", "pane"); } catch { /* ignore */ }
  });
  head.addEventListener("dragend", () => {
    el.classList.remove("dragging");
    panes.forEach((q) => q.el.classList.remove("drag-over"));
    dragSrcPane = null;
  });
  el.addEventListener("dragover", (e) => {
    if (!dragSrcPane || dragSrcPane === p) return;
    e.preventDefault(); e.dataTransfer.dropEffect = "move";
    el.classList.add("drag-over");
  });
  el.addEventListener("dragleave", () => { el.classList.remove("drag-over"); });
  el.addEventListener("drop", (e) => {
    e.preventDefault(); el.classList.remove("drag-over");
    if (dragSrcPane && dragSrcPane !== p) swapPanes(dragSrcPane, p);
  });

  panes.push(p);
  relayout(); // places the pane (row/grid), wires the divider, sizes + fits
  return p;
}

function splitPane() {
  if (panes.length >= MAX_PANES) return null;
  const p = createPane(); // createPane() places it via relayout()
  setFocus(p);
  return p;
}

// Re-flow the panes after any change (add / close / reorder / layout toggle).
// Row mode is a flex row; the 2-pane case keeps the resizable divider, 3+ get a
// thin separator (CSS). Grid mode is a roughly-square CSS grid (quadrant-style),
// with the last pane spanning to fill its row. Always re-fits every xterm.
function relayout() {
  if (paneDivider) { paneDivider.remove(); paneDivider = null; }
  panes.forEach((p) => { p.el.style.flexGrow = ""; p.el.style.gridColumn = ""; });
  panes.forEach((p) => termRow.appendChild(p.el)); // DOM order = panes[] order
  const n = panes.length;
  const grid = layoutMode === "grid" && n > 1;
  termRow.classList.toggle("grid", grid);
  if (grid) {
    const cols = Math.ceil(Math.sqrt(n));
    const rows = Math.ceil(n / cols);
    termRow.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    termRow.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
    const span = cols - ((n - 1) % cols); // make the last pane fill the bottom row
    panes[n - 1].el.style.gridColumn = span > 1 ? `span ${span}` : "";
  } else {
    termRow.style.gridTemplateColumns = "";
    termRow.style.gridTemplateRows = "";
    if (layoutMode === "row" && n === 2) { // a draggable divider for the 2-pane case
      paneDivider = makePaneDivider();
      termRow.insertBefore(paneDivider, panes[1].el);
    }
  }
  updateSplitChrome();
  refitAll();
}

function swapPanes(a, b) {
  const ia = panes.indexOf(a), ib = panes.indexOf(b);
  if (ia < 0 || ib < 0 || ia === ib) return;
  panes[ia] = b; panes[ib] = a;
  relayout();
}

function setLayout(mode) {
  layoutMode = mode === "grid" ? "grid" : "row";
  try { localStorage.setItem("serai.layout", layoutMode); } catch { /* ignore */ }
  relayout();
}

// Open a session directly in a second pane: split if there's room, otherwise
// load it into the other (non-focused) pane. Skips the split-then-focus-then-
// click dance — pick the session you want beside the current one in one click.
function openInSplit(target) {
  let p;
  if (panes.length < MAX_PANES) {
    p = splitPane();
  } else {
    p = panes.find((q) => q !== focused) || panes[0];
    setFocus(p);
  }
  if (p) paneAttach(p, target);
}

// A draggable bar between the two panes: dragging sets their flex-grow ratio so
// one session can get more room than the other (an even 50/50 by default).
function makePaneDivider() {
  const d = document.createElement("div");
  d.className = "pane-divider";
  d.title = "drag to resize";
  d.addEventListener("mousedown", (e) => {
    e.preventDefault();
    document.body.classList.add("resizing-x");
    const onMove = (ev) => {
      const rect = termRow.getBoundingClientRect();
      if (rect.width <= 0 || panes.length < 2) return;
      let frac = (ev.clientX - rect.left) / rect.width;
      frac = Math.max(0.15, Math.min(0.85, frac)); // keep both panes usable
      panes[0].el.style.flexGrow = String(frac);
      panes[1].el.style.flexGrow = String(1 - frac);
      fitAll();
    };
    const onUp = () => {
      document.body.classList.remove("resizing-x");
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      sendResizeAll();
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
  return d;
}

function closePane(p) {
  if (panes.length <= 1) return; // never close the last pane
  if (p.ws) { p.ws.onclose = null; p.ws.close(); } // intentional -- never reattach
  clearTimeout(p.reconnectTimer);
  clearTimeout(p.stableTimer);
  p.term.dispose();
  panes.splice(panes.indexOf(p), 1);
  p.el.remove();
  if (focused === p) setFocus(panes[0]);
  relayout(); // re-flow the remaining panes (divider/grid/sizing/fit)
}

// Per-pane close button + the focus ring only matter when split; the topbar
// split button is disabled once we're at the max.
function updateSplitChrome() {
  const multi = panes.length > 1;
  termRow.classList.toggle("multi", multi);
  panes.forEach((p) => { p.el.querySelector(".pane-close").hidden = !multi; });
  const sb = document.getElementById("split");
  if (sb) sb.disabled = panes.length >= MAX_PANES;
  const lb = document.getElementById("layout");
  if (lb) {
    lb.disabled = panes.length < 2; // layout only matters with 2+ panes
    lb.innerHTML = layoutMode === "grid" ? "▭ row" : "▦ grid";
    lb.title = layoutMode === "grid" ? "Switch to a single row" : "Switch to a grid layout";
  }
  renderPaneTabs(); // pane count changed -> re-render the tab strip
}

window.addEventListener("resize", refitAll);

// The first pane exists from the start; the split button adds the second.
setFocus(createPane());

// --- small UI utilities ----------------------------------------------------

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

// Transient corner notification. `html` is trusted markup the caller builds (it
// must escapeHtml() any server/session strings it interpolates).
function toast(html, kind = "", ms = 5000) {
  const box = document.getElementById("toasts");
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.innerHTML = html;
  el.onclick = () => el.remove();
  box.appendChild(el);
  setTimeout(() => el.remove(), ms);
  return el;
}

// --- server-side settings sync ---------------------------------------------
// Mirror every serai.* localStorage pref to the server (~/.config/serai/...) so
// prefs follow you across browsers/devices. Each serai.* write schedules a
// debounced push; on load we pull the server's copy into localStorage first, so
// the existing per-feature restores read the authoritative values.
let _rawSetItem = null;
let _pushTimer = null;
// Keys that stay device-local, never entering the cross-device settings sync.
// The sync is whole-blob last-writer-wins, so fast-changing per-screen state
// (like "the directory I'm browsing") would make every open tab fight over it
// -- one stale tab could pin the file pane to an old directory forever.
const SYNC_LOCAL_ONLY = new Set(["serai.files.lastdirs"]);
function syncBlob() {
  const blob = {};
  for (let i = 0; i < localStorage.length; i++) {
    const k = localStorage.key(i);
    if (k && k.startsWith("serai.") && !SYNC_LOCAL_ONLY.has(k)) blob[k] = localStorage.getItem(k);
  }
  return blob;
}
// The server merges what we send, so a tab that predates a preference no longer
// drops that key for everyone else when it saves.
function pushSettings() {
  return fetch("/api/settings", {
    method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(syncBlob()),
  }).catch(() => { /* offline -- keep the local copy */ });
}
function schedulePush() {
  clearTimeout(_pushTimer);
  _pushTimer = setTimeout(pushSettings, 500);
}
// Save now and resolve when the server has it -- for the cases that read state
// straight back and would otherwise race the debounce.
function pushSettingsNow() {
  clearTimeout(_pushTimer);
  _pushTimer = null;
  return pushSettings();
}
try {
  _rawSetItem = localStorage.setItem.bind(localStorage);
  localStorage.setItem = function (k, v) {
    _rawSetItem(k, v);
    if (typeof k === "string" && k.startsWith("serai.")) schedulePush();
  };
} catch { /* storage locked down (private mode) -- sync disabled, app still works */ }

async function pullSettings() {
  if (!_rawSetItem) return;
  try {
    const blob = await (await fetch("/api/settings")).json();
    if (blob && typeof blob === "object") {
      for (const [k, v] of Object.entries(blob)) {
        // skip device-local keys: a stale server copy must not clobber this
        // screen's state (covers blobs written before the exclusion existed)
        if (k.startsWith("serai.") && !SYNC_LOCAL_ONLY.has(k) && typeof v === "string") _rawSetItem(k, v); // no echo push
      }
    }
  } catch { /* offline -- fall back to the local cache */ }
}

// --- auth ------------------------------------------------------------------
// serai is a shell into the whole fleet, so the app is gated by a login. The
// SPA polls /api/auth/status on boot to pick setup (no users yet) vs login vs
// straight through (auth disabled), and shows a full-screen cover until a
// session cookie is in hand. User management is admin-only, in the account modal.

const AUTH_OFF = { enabled: false, configured: true, authenticated: true, user: null, admin: true };
let authStatus = { ...AUTH_OFF };
let authMode = "login";        // or "setup"
let _authResolve = null;       // resolves requireAuth() once a sign-in succeeds
let _authPromise = null;       // de-dupes concurrent prompts (e.g. several 401s)

async function fetchAuthStatus() {
  try { authStatus = await (await fetch("/api/auth/status")).json(); }
  catch { authStatus = { ...AUTH_OFF }; }
  return authStatus;
}

function showAuthMsg(text, kind) {
  const el = document.getElementById("auth-msg");
  el.textContent = text || "";
  el.className = "auth-msg" + (kind ? " " + kind : "");
  el.hidden = !text;
}

function renderAuthMode() {
  const setup = authMode === "setup";
  const needCode = setup && authStatus.setup_code_required;
  document.getElementById("auth-title").textContent =
    setup ? "Create the first user" : "Sign in to serai";
  document.getElementById("auth-setup-row").hidden = !needCode;
  document.getElementById("auth-confirm-row").hidden = !setup;  // retype only when creating
  document.getElementById("auth-pass").setAttribute(
    "autocomplete", setup ? "new-password" : "current-password");
  document.getElementById("auth-submit").textContent = setup ? "create" : "sign in";
  const hint = document.getElementById("auth-hint");
  if (needCode) {
    hint.textContent = "serai printed a one-time setup code to its log " +
      "(journalctl --user -u serai). Paste it to create the first admin.";
  } else if (setup) {
    hint.textContent = "You're the first user — create the admin account. " +
      "Do this now, before others on your network can reach serai.";
  } else {
    hint.textContent = "";
  }
  hint.hidden = !setup;
}

// Resolves once the user is authenticated -- or immediately if auth is off.
async function requireAuth() {
  await fetchAuthStatus();
  if (!authStatus.enabled || authStatus.authenticated) return;
  if (_authPromise) return _authPromise;          // already prompting
  authMode = authStatus.configured ? "login" : "setup";
  renderAuthMode();
  showAuthMsg("");
  document.getElementById("auth-overlay").hidden = false;
  document.getElementById(authMode === "setup" ? "auth-token" : "auth-user").focus();
  _authPromise = new Promise((res) => { _authResolve = res; });
  try { await _authPromise; } finally { _authPromise = null; _authResolve = null; }
}

async function submitAuth(ev) {
  if (ev) ev.preventDefault();
  const btn = document.getElementById("auth-submit");
  const username = document.getElementById("auth-user").value.trim();
  const password = document.getElementById("auth-pass").value;
  if (authMode === "setup" && password !== document.getElementById("auth-pass2").value) {
    showAuthMsg("passwords don't match — retype them", "bad");
    return;
  }
  let res;
  btn.disabled = true;
  try {
    if (authMode === "setup") {
      const token = document.getElementById("auth-token").value.trim();
      res = await fetch("/api/setup", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token, username, password }),
      });
    } else {
      res = await fetch("/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });
    }
  } catch {
    showAuthMsg("network error -- is serai still running?", "bad");
    btn.disabled = false;
    return;
  }
  const data = await res.json().catch(() => ({}));
  btn.disabled = false;
  if (!res.ok) { showAuthMsg(data.error || "could not sign in", "bad"); return; }
  document.getElementById("auth-pass").value = "";
  document.getElementById("auth-pass2").value = "";
  document.getElementById("auth-token").value = "";
  await fetchAuthStatus();
  document.getElementById("auth-overlay").hidden = true;
  updateAccountButton();
  if (_authResolve) _authResolve();
}

async function logout() {
  await fetch("/api/logout", { method: "POST" }).catch(() => {});
  location.reload();
}

function updateAccountButton() {
  const btn = document.getElementById("account");
  const foot = document.getElementById("auth-foot");
  if (!authStatus.enabled) {
    btn.hidden = true;
    if (foot) foot.textContent = "auth: off";
    return;
  }
  btn.hidden = false;
  const who = authStatus.user || "account";
  btn.textContent = who.slice(0, 2); // a round avatar, so initials only
  btn.title = `Signed in as ${who} — account & users`;
  if (foot) foot.textContent = "auth: on";
}

// account + user management (admin-only) modal
async function openAccount() {
  document.getElementById("acct-user").textContent = authStatus.user || "(auth off)";
  document.getElementById("acct-pass").value = "";
  const usersSec = document.getElementById("acct-users");
  usersSec.hidden = !authStatus.admin;
  const netSec = document.getElementById("acct-network");
  netSec.hidden = !authStatus.admin;
  if (authStatus.admin) { renderUsers(); renderNetwork(); }
  document.getElementById("account-form").hidden = false;
}

async function renderNetwork() {
  const info = document.getElementById("net-info");
  const save = document.getElementById("net-save");
  info.textContent = "loading…";
  let net;
  try {
    const r = await fetch("/api/network");
    if (!r.ok) throw 0;
    net = await r.json();
  } catch { info.textContent = "could not load network settings"; return; }

  document.getElementById("net-bind").value = net.host || "";
  document.getElementById("net-hostnames").value = net.hostnames || "";

  const ip = net.detected_ip ? `<span class="mono">${escapeHtml(net.detected_ip)}</span>` : "unknown";
  const covers = (net.cert_sans || []).map((s) => escapeHtml(s)).join(", ") || "—";
  let line = `LAN IP ${ip} is covered automatically. Cert currently validates: <span class="mono">${covers}</span>.`;
  if (net.service === null) {
    line += " serai isn't running as a service, so it can't restart itself — " +
      "apply, then restart it yourself.";
    save.textContent = "save (restart manually)";
  } else {
    line += " Applying restarts serai (you'll reconnect in a moment).";
    save.textContent = "apply & restart";
  }
  info.innerHTML = line;
}

async function saveNetwork() {
  const host = document.getElementById("net-bind").value.trim();
  const hostnames = document.getElementById("net-hostnames").value.trim();
  if (!confirm("Apply network settings? This restarts serai and briefly drops your connection.")) return;
  let res;
  try {
    res = await fetch("/api/network", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host, hostnames }),
    });
  } catch { toast("network error", "error"); return; }
  const d = await res.json().catch(() => ({}));
  if (!res.ok) { toast(escapeHtml(d.error || "could not save"), "error"); return; }

  const r = d.restart || {};
  if (r.ok) {
    toast("Applied — serai is restarting. Reconnecting…", "ok", 8000);
    closeAccount();
    // the service is cycling; give it a moment, then reload onto the new cert/bind
    setTimeout(() => location.reload(), 4000);
  } else {
    toast(escapeHtml("Saved, but " + (r.reason || "restart it yourself to apply")), "warn", 9000);
  }
}

document.getElementById("net-save").addEventListener("click", saveNetwork);
function closeAccount() { document.getElementById("account-form").hidden = true; }

async function renderUsers() {
  const box = document.getElementById("acct-user-list");
  box.innerHTML = '<span class="muted">loading…</span>';
  let data;
  try {
    const r = await fetch("/api/users");
    if (!r.ok) throw 0;
    data = await r.json();
  } catch { box.innerHTML = '<span class="muted">could not load users</span>'; return; }
  box.innerHTML = "";
  for (const u of data.users) {
    const row = document.createElement("div");
    row.className = "acct-user-row";
    row.innerHTML = `<span class="mono">${escapeHtml(u.username)}</span>` +
      (u.admin ? '<span class="acct-badge">admin</span>' : "") +
      '<span class="acct-user-actions"></span>';
    const actions = row.querySelector(".acct-user-actions");
    const reset = document.createElement("button");
    reset.className = "linkish"; reset.textContent = "reset pw";
    reset.onclick = () => resetUserPw(u.username);
    actions.appendChild(reset);
    if (u.username !== data.me) {
      const del = document.createElement("button");
      del.className = "linkish danger"; del.textContent = "remove";
      del.onclick = () => removeUser(u.username);
      actions.appendChild(del);
    }
    box.appendChild(row);
  }
}

async function addUser() {
  const username = document.getElementById("acct-new-user").value.trim();
  const password = document.getElementById("acct-new-pass").value;
  const admin = document.getElementById("acct-new-admin").checked;
  const r = await fetch("/api/users", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, admin }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(escapeHtml(d.error || "could not add user"), "error"); return; }
  document.getElementById("acct-new-user").value = "";
  document.getElementById("acct-new-pass").value = "";
  document.getElementById("acct-new-admin").checked = false;
  toast(`added <b>${escapeHtml(username)}</b>`, "ok", 3000);
  renderUsers();
}

async function removeUser(username) {
  if (!confirm(`Remove user "${username}"? Their sessions stay in tmux, but they’re signed out everywhere.`)) return;
  const r = await fetch(`/api/users/${encodeURIComponent(username)}`, { method: "DELETE" });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(escapeHtml(d.error || "could not remove user"), "error"); return; }
  renderUsers();
}

async function resetUserPw(username) {
  const pw = prompt(`New password for "${username}" (at least 8 characters):`);
  if (pw == null) return;
  await setPassword(username, pw, `password updated for ${username}`);
}

async function changeMyPassword() {
  const pw = document.getElementById("acct-pass").value;
  if (!pw) return;
  if (await setPassword(authStatus.user, pw, "password updated")) {
    document.getElementById("acct-pass").value = "";
  }
}

async function setPassword(username, password, okMsg) {
  const r = await fetch(`/api/users/${encodeURIComponent(username)}/password`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) { toast(escapeHtml(d.error || "could not update password"), "error"); return false; }
  toast(escapeHtml(okMsg), "ok", 3000);
  return true;
}

document.getElementById("auth-card").addEventListener("submit", submitAuth);
document.getElementById("account").addEventListener("click", openAccount);
document.getElementById("acct-close").addEventListener("click", closeAccount);
document.getElementById("acct-logout").addEventListener("click", logout);
document.getElementById("acct-pass-save").addEventListener("click", changeMyPassword);
document.getElementById("acct-add").addEventListener("click", addUser);

// --- data ------------------------------------------------------------------

async function loadHosts() {
  hosts = await (await fetch("/api/hosts")).json();
}

// --- running version ---------------------------------------------------------
// The server stamps X-Serai-Version on gated responses; read it off the normal
// sessions poll (no extra request). First sighting goes to the status bar; a
// CHANGE means the service was updated under this open tab -- surface a reload
// prompt instead of quietly running stale code (the post-reboot lesson).
let serverVersion = null;
let versionNotified = false;
function trackServerVersion(v) {
  if (!v) return;
  const el = document.getElementById("version-foot");
  if (!serverVersion) {
    serverVersion = v;
    if (el) el.textContent = `serai v${v}`;
    return;
  }
  if (v !== serverVersion) {
    if (el) { el.textContent = `serai v${serverVersion} → v${v} (reload)`; el.classList.add("stale"); }
    if (!versionNotified) {
      versionNotified = true;
      toast(`serai was updated to v${escapeHtml(v)} — reload to get it`, "ok", 15000);
    }
  }
}

async function loadSessions() {
  const spin = document.getElementById("sessions-spinner");
  if (spin) spin.classList.remove("hidden");
  try {
    const res = await fetch("/api/sessions");
    if (res.status === 401) {            // cookie expired mid-session -- re-login, then retry
      if (spin) spin.classList.add("hidden");
      await requireAuth();
      return loadSessions();
    }
    trackServerVersion(res.headers.get("X-Serai-Version"));
    sessionList = await res.json();
  } catch {
    sessionList = [];
  }
  sessionsLoaded = true;
  if (spin) spin.classList.add("hidden");
  renderWorkspaces();
  renderTree();
  loadSavedSessions(); // refresh the post-reboot restore banner
}

// --- resume sessions after a reboot -----------------------------------------
// serai snapshots open sessions server-side; when saved sessions aren't running
// (typically a reboot killed the tmux server) a sidebar banner offers to bring
// them back: "view" expands the list to pick individual ones, or resume all.
let savedSessions = [];
let restoreDismissed = false;
let restoreExpanded = false;
const restoreChecked = new Map(); // "host::name" -> keep checkbox state across re-renders
const restoreResume = new Map();  // "host::name" -> "" | "continue" | "resume", per Claude session

async function loadSavedSessions() {
  try {
    const r = await (await fetch("/api/sessions/saved")).json();
    savedSessions = Array.isArray(r) ? r : [];
  } catch { savedSessions = []; }
  updateRestoreBanner();
}

function missingSaved() {
  const running = new Set(sessionList.map((s) => `${s.host}::${s.name}`));
  return savedSessions.filter((r) => !running.has(`${r.host}::${r.name}`));
}

function updateRestoreBanner() {
  const banner = document.getElementById("restore-banner");
  if (!banner) return;
  const missing = missingSaved();
  if (!missing.length || restoreDismissed) { banner.hidden = true; return; }
  document.getElementById("restore-msg").textContent =
    `↺ resume ${missing.length} session${missing.length === 1 ? "" : "s"} from before?`;
  document.getElementById("restore-view").textContent = restoreExpanded ? "hide" : "view";
  document.getElementById("restore-list").hidden = !restoreExpanded;
  document.getElementById("restore-selected").hidden = !restoreExpanded;
  if (restoreExpanded) renderRestoreList(missing);
  banner.hidden = false;
}

function renderRestoreList(missing) {
  const list = document.getElementById("restore-list");
  list.innerHTML = "";
  for (const r of missing) {
    const key = `${r.host}::${r.name}`;
    // A div, not a label: the row now holds a <select>, and clicking that inside
    // a label would toggle the checkbox with it.
    const row = document.createElement("div");
    row.className = "restore-row";
    const tick = document.createElement("label");
    tick.className = "restore-tick";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = restoreChecked.get(key) !== false; // default: checked
    cb.addEventListener("change", () => restoreChecked.set(key, cb.checked));
    tick.appendChild(cb);
    const text = document.createElement("span");
    text.className = "restore-row-text";
    text.innerHTML =
      `<span class="kind">${KIND_GLYPH[r.kind] || ""}</span> ${escapeHtml(r.label || r.name)}` +
      `<span class="mono muted"> · ${escapeHtml(r.host)}</span>`;
    tick.appendChild(text);
    row.appendChild(tick);

    // Only Claude sessions have anything to choose; a shell just comes back.
    if (r.kind === "claude") {
      const sel = document.createElement("select");
      sel.className = "restore-resume";
      sel.title = "How this Claude session comes back";
      for (const [value, label] of [["continue", "continue"], ["resume", "resume…"], ["", "fresh"]]) {
        const o = document.createElement("option");
        o.value = value; o.textContent = label;
        sel.appendChild(o);
      }
      sel.value = restoreResume.get(key) ?? "continue";
      sel.addEventListener("change", () => restoreResume.set(key, sel.value));
      row.appendChild(sel);
    }
    list.appendChild(row);
  }
}

async function doResume(targets) {
  const buttons = [document.getElementById("restore-all"), document.getElementById("restore-selected")];
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const body = targets ? { targets: targets.map((r) => ({
      host: r.host,
      name: r.name,
      // per-session choice; "continue" keeps the old behaviour for anything untouched
      resume: restoreResume.get(`${r.host}::${r.name}`) ?? "continue",
    })) } : {};
    const data = await (await fetch("/api/sessions/restore", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })).json();
    const n = data.restored || 0;
    toast(`Resumed ${n} session${n === 1 ? "" : "s"}` +
          (data.skipped ? ` (${data.skipped} already running)` : ""), "ok", 4000);
  } catch { toast("Resume failed", "error", 4000); }
  buttons.forEach((b) => { b.disabled = false; });
  await loadSessions(); // pick up the recreated sessions (also refreshes the banner)
}

document.getElementById("restore-all").addEventListener("click", () => doResume(missingSaved()));
document.getElementById("restore-selected").addEventListener("click", () => {
  const picked = missingSaved().filter((r) => restoreChecked.get(`${r.host}::${r.name}`) !== false);
  if (picked.length) doResume(picked);
});
document.getElementById("restore-view").addEventListener("click", () => {
  restoreExpanded = !restoreExpanded;
  updateRestoreBanner();
});
document.getElementById("restore-dismiss").addEventListener("click", () => {
  restoreDismissed = true;
  document.getElementById("restore-banner").hidden = true;
});

// --- sidebar ---------------------------------------------------------------

function hostTags(alias) {
  const h = hosts.find((x) => x.alias === alias);
  return h ? h.tags : [];
}

function matchesFilter(s) {
  if (!filterText) return true;
  const hay = `${s.host} ${s.name} ${s.label} ${(s.tags || []).join(" ")} ${hostTags(s.host).join(" ")}`.toLowerCase();
  return hay.includes(filterText);
}

function groupedData() {
  const visible = sessionList.filter((s) => matchesFilter(s) && matchesWorkspace(s));
  const groups = new Map();
  const push = (key, s) => {
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(s);
  };

  // The rail groups by tag -- that's how projects are actually organised here.
  // A session carrying several tags appears under each of them.
  for (const s of visible) sessionTags(s).forEach((t) => push(t, s));

  // alphabetical, with the untagged catch-all last
  return [...groups.entries()].sort((a, b) => {
    if ((a[0] === "untagged") !== (b[0] === "untagged")) return a[0] === "untagged" ? 1 : -1;
    return a[0].localeCompare(b[0]);
  });
}

// Collapsed sidebar groups, persisted per grouping mode ("host:web1", "tag:prod",
// …) so a collapse survives the 5s re-render and reloads, and collapsing a tag
// group doesn't also collapse a same-named host group.
const GROUPS_COLLAPSED_KEY = "serai.groups.collapsed";
let collapsedGroups = (() => {
  try { return new Set(JSON.parse(localStorage.getItem(GROUPS_COLLAPSED_KEY) || "[]")); }
  catch { return new Set(); }
})();
function saveCollapsedGroups() {
  try { localStorage.setItem(GROUPS_COLLAPSED_KEY, JSON.stringify([...collapsedGroups])); } catch { /* ignore */ }
}
function groupKey(name) { return `${groupMode}:${name}`; }

function toggleAllGroups() {
  const groups = groupedData();
  const anyOpen = groups.some(([g]) => !collapsedGroups.has(groupKey(g)));
  for (const [g] of groups) {
    if (anyOpen) collapsedGroups.add(groupKey(g));
    else collapsedGroups.delete(groupKey(g));
  }
  saveCollapsedGroups();
  renderTree();
}
document.getElementById("tree-toggle")?.addEventListener("click", toggleAllGroups);

function renderTree() {
  const tree = document.getElementById("tree");
  const groups = groupedData();
  tree.innerHTML = "";

  // the collapse/expand-all button tracks whether anything is left to collapse
  const allBtn = document.getElementById("tree-toggle");
  if (allBtn) {
    const anyOpen = groups.some(([g]) => !collapsedGroups.has(groupKey(g)));
    allBtn.textContent = anyOpen ? "▾▾" : "▸▸";
    allBtn.title = anyOpen ? "collapse all groups" : "expand all groups";
  }

  if (!groups.length) {
    tree.innerHTML = sessionsLoaded
      ? `<div class="tree-loading">${filterText ? "no matching sessions" : "no sessions yet"}</div>`
      : `<div class="tree-loading"><span class="spinner"></span>loading sessions…</div>`;
    return;
  }

  for (const [group, items] of groups) {
    // while filtering, ignore collapse so a match can never hide
    const collapsed = !filterText && collapsedGroups.has(groupKey(group));
    const head = document.createElement("div");
    head.className = "group-head" + (collapsed ? " collapsed" : "");
    // a collapsed group must not hide an alert: surface the "worst" child state
    const agg = items.some((s) => s.state === "needs_input") ? "needs_input"
      : items.some((s) => s.state === "running") ? "running" : "idle";
    const hasActive = items.some((s) =>
      panes.some((p) => p.active && p.active.host === s.host && p.active.name === s.name));
    head.innerHTML =
      `<span class="chev">${collapsed ? "▸" : "▾"}</span>` +
      `<span${collapsed && hasActive ? ' class="has-active"' : ""}>${group}</span>` +
      (collapsed ? `<span class="dot ${agg}"></span>` : "") +
      `<span class="count">${items.length}</span>`;
    head.onclick = () => {
      const k = groupKey(group);
      if (collapsedGroups.has(k)) collapsedGroups.delete(k);
      else collapsedGroups.add(k);
      saveCollapsedGroups();
      renderTree();
    };
    tree.appendChild(head);
    if (collapsed) continue;

    for (const s of items) {
      const row = document.createElement("div");
      row.className = "row session";
      if (panes.some((p) => p.active && p.active.host === s.host && p.active.name === s.name)) row.classList.add("active");
      const tagsHtml = (s.tags || []).map((t) => `<span class="tag">${t}</span>`).join("");
      row.innerHTML =
        `<span class="dot ${s.state}"></span>` +
        `<span class="bkind ${s.kind === "claude" ? "cc" : ""}">${s.kind === "claude" ? "cc" : "sh"}</span>` +
        `<span class="name">${escapeHtml(s.label)}</span>` +
        (tagsHtml ? `<span class="tags">${tagsHtml}</span>` : "") +
        `<span class="rw">${escapeHtml(s.state === "running" ? "now" : fmtAge(s.age))}</span>` +
        `<button class="row-split" title="open in a split pane (side by side)">\u25eb</button>` +
        `<button class="row-edit" title="rename / tag">\u270e</button>` +
        `<button class="row-del" title="kill session">\u2715</button>`;
      row.onclick = () => attach({ host: s.host, name: s.name, kind: s.kind, label: s.label, dir: s.dir, path: s.path });
      row.querySelector(".row-split").onclick = (ev) => { ev.stopPropagation(); openInSplit({ host: s.host, name: s.name, kind: s.kind, label: s.label }); };
      row.querySelector(".row-edit").onclick = (ev) => { ev.stopPropagation(); openEditSession(s); };
      row.querySelector(".row-del").onclick = (ev) => { ev.stopPropagation(); killSession(s); };
      if (fleetMode) {
        const cb = document.createElement("input");
        cb.type = "checkbox";
        cb.className = "rowcheck";
        cb.checked = selected.has(s.id);
        cb.onclick = (ev) => { ev.stopPropagation(); toggleSelect(s); };
        row.prepend(cb);
      }
      tree.appendChild(row);
    }
  }
  renderBoard();     // keep the board in sync wherever the tree re-renders
  renderPaneTabs();  // ...and the pane tabs' state dots
}

// --- board (the landing view) ----------------------------------------------
//
// serai's home surface: a state-first grid of every session, workspace tabs
// (sourced from your ssh-config groups), and a summary strip. Clicking a card
// attaches and switches to the terminal view; the board button switches back.
// Same sessionList data, same attach() path.
const STATE_ORDER = { needs_input: 0, running: 1, done: 2, idle: 3 };
const STATE_WORD = { needs_input: "blocked", running: "working", done: "done", idle: "idle" };

// A card's right-aligned timestamp, phrased per state (the mockup's .wn):
// "active" / "waiting 2m" / "finished 4m" / "2h ago".
function fmtAge(secs) {
  if (secs == null || secs < 0) return "";
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}
function stateWhen(s) {
  const a = fmtAge(s.age);
  if (s.state === "running") return "active";
  if (s.state === "needs_input") return a ? `waiting ${a}` : "waiting";
  if (s.state === "done") return a ? `finished ${a}` : "finished";
  return a ? `${a} ago` : "";
}

// Colour the pane-tail preview the way the mockup does, so a card reads as a
// live terminal: prompts/permission asks in the blocked colour, successes green,
// box-drawing chrome dimmed, everything else the base dim.
const TAIL_BLOCKED = /(❯\s*\d|\(y\/n\)|\[y\/n\]|password:|\[sudo\]|do you want|proceed\?|are you sure)/i;
const TAIL_OK = /(✔|✓|\bpassed\b|\bsuccess\b|\bcompleted\b)/i;
const TAIL_CHROME = /^[\s─-╿▀-▟_=\-·•]+$/;  // box-drawing / rules
function tailHtml(tail, state) {
  const lines = tail.split("\n");
  // The line carrying the signal gets the state's colour, so every non-idle card
  // reads as live the way the mockup does -- real pane text rarely contains the
  // mockup's tidy "✔ done" markers, so state drives the colour, not just content.
  const sc = { needs_input: "b", done: "h", running: "p" }[state] || "";
  let last = -1;
  for (let i = lines.length - 1; i >= 0; i--) if (lines[i].trim()) { last = i; break; }
  return lines.map((ln, i) => {
    const e = escapeHtml(ln);
    if (!ln.trim()) return e;
    if (TAIL_CHROME.test(ln)) return `<span class="d">${e}</span>`;
    if (TAIL_BLOCKED.test(ln)) return `<span class="b">${e}</span>`;
    if (TAIL_OK.test(ln)) return `<span class="p">${e}</span>`;
    if (/^\s*❯/.test(ln)) return `<span class="a">${e}</span>`;   // a Claude input prompt
    if (i === last && sc) return `<span class="${sc}">${e}</span>`;
    return e;
  }).join("\n");
}

// Selected tags; empty means "all". A set rather than a single value because you're
// often working across a couple of projects at once and want both in view.
const activeTags = new Set();

// Workspaces are your per-session tags -- that's how projects are actually
// grouped here. A session can carry several, so it shows under each of them.
function sessionTags(s) {
  const own = (s.tags || []).filter(Boolean);
  return own.length ? own : ["untagged"];
}
function matchesWorkspace(s) {
  if (!activeTags.size) return true;                     // nothing picked -> show everything
  return sessionTags(s).some((t) => activeTags.has(t));  // union of the picked tags, not intersection
}

// The workspace picker: a compact control in the top bar, because ~20 tags never
// fit a horizontal strip. Selecting a tag narrows both the board and the rail.
function tagCounts() {
  const m = new Map();
  for (const s of sessionList) for (const t of sessionTags(s)) m.set(t, (m.get(t) || 0) + 1);
  return m;
}
function tagOrder(counts) {
  const t = [...counts.keys()].filter((x) => x !== "untagged").sort((a, b) => a.localeCompare(b));
  if (counts.has("untagged")) t.push("untagged"); // the catch-all sits last
  return t;
}

function renderWorkspaces() {
  const btn = document.getElementById("ws-btn");
  const menu = document.getElementById("ws-menu");
  if (!btn || !menu) return;
  const counts = tagCounts();
  for (const t of [...activeTags]) if (!counts.has(t)) activeTags.delete(t); // the tag went away

  // One tag reads as its name; several collapse to a count, because the top bar has no
  // room for a list of every selected tag.
  let sum = 0;
  for (const t of activeTags) sum += counts.get(t) || 0;
  const name = activeTags.size === 0 ? "all tags"
    : activeTags.size === 1 ? [...activeTags][0]
      : `${activeTags.size} tags`;
  btn.innerHTML = `◇ ${escapeHtml(name)} ` +
    `<span class="cnt">${activeTags.size === 0 ? sessionList.length : sum}</span> ▾`;

  const redraw = () => { renderWorkspaces(); renderTree(); }; // rail and board follow
  const addRow = (label, cnt, on, onClick) => {
    const row = document.createElement("div");
    row.className = "menu-row" + (on ? " on" : "");
    row.innerHTML = `<span class="tick">${on ? "✓" : ""}</span>` +
      `<span>${escapeHtml(label)}</span><span class="c">${cnt}</span>`;
    row.onclick = onClick;
    menu.appendChild(row);
  };

  menu.innerHTML = "";
  addRow("all tags", sessionList.length, activeTags.size === 0, () => { activeTags.clear(); redraw(); });
  const sep = document.createElement("div");
  sep.className = "menu-sep";
  menu.appendChild(sep);
  for (const t of tagOrder(counts)) {
    addRow(t, counts.get(t) || 0, activeTags.has(t), () => {
      if (activeTags.has(t)) activeTags.delete(t); else activeTags.add(t);
      redraw(); // menu stays open on purpose, so several can be picked in one go
    });
  }
  sizeWsMenu(); // the row count changed, so refit to the window
}

// Size the menu to the space actually below it. A flat max-height in CSS is measured
// against the viewport, but the menu starts a header-height down — so the list got cut
// off with a third of the window still empty. Grow to fit every tag, and only scroll
// once we genuinely reach the bottom of the window.
function sizeWsMenu() {
  const menu = document.getElementById("ws-menu");
  if (!menu || menu.hidden) return;
  const top = menu.getBoundingClientRect().top;
  menu.style.maxHeight = Math.max(120, window.innerHeight - top - 12) + "px";
}

document.getElementById("ws-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  const menu = document.getElementById("ws-menu");
  menu.hidden = !menu.hidden;
  if (!menu.hidden) sizeWsMenu();
});
window.addEventListener("resize", sizeWsMenu);
window.addEventListener("mousedown", (e) => {
  const p = document.getElementById("ws-picker");
  if (p && !p.contains(e.target)) document.getElementById("ws-menu").hidden = true;
});
window.addEventListener("keydown", (e) => {
  if (e.key === "Escape") document.getElementById("ws-menu").hidden = true;
});

function renderBoard() {
  const view = document.getElementById("board-view");
  if (!view || view.hidden) return; // self-guard: no-op while in the attached view

  const items = sessionList
    .filter((s) => matchesFilter(s) && matchesWorkspace(s))
    .sort((a, b) => (STATE_ORDER[a.state] ?? 9) - (STATE_ORDER[b.state] ?? 9)
      || a.host.localeCompare(b.host) || a.label.localeCompare(b.label));

  // summary strip: total + a count per state, scoped to the active workspace
  const scope = sessionList.filter(matchesWorkspace);
  const counts = {};
  for (const s of scope) counts[s.state] = (counts[s.state] || 0) + 1;
  const chip = (state, word) => counts[state]
    ? `<span class="sc"><i class="dot ${state}"></i><b>${counts[state]}</b> ${word}</span>` : "";
  document.getElementById("board-summary").innerHTML =
    `<span class="sc"><b>${scope.length}</b> session${scope.length === 1 ? "" : "s"}</span>` +
    chip("running", "working") + chip("needs_input", "blocked") +
    chip("done", "done") + chip("idle", "idle");

  const grid = document.getElementById("board-grid");
  // Fewer cards -> bigger cards. Narrow to one project and the room goes into the
  // pane preview instead of whitespace; the tiers are pure CSS custom properties.
  grid.classList.toggle("large", items.length > 0 && items.length <= 2);
  grid.classList.toggle("roomy", items.length > 2 && items.length <= 6);

  if (!items.length) {
    grid.innerHTML = `<div class="board-empty">${filterText ? "no matching sessions" : "no sessions here yet"}</div>`;
    return;
  }
  grid.innerHTML = "";
  for (const s of items) {
    const active = panes.some((p) => p.active && p.active.host === s.host && p.active.name === s.name);
    const card = document.createElement("div");
    card.className = `bcard st-${s.state}` + (active ? " active" : "");
    const tagsHtml = (s.tags || []).map((t) => `<span class="tag">${escapeHtml(t)}</span>`).join("");
    const live = s.state === "running" || s.state === "needs_input";
    card.innerHTML =
      `<div class="bcard-h">` +
        `<span class="bkind ${s.kind === "claude" ? "cc" : ""}">${s.kind === "claude" ? "cc" : "sh"}</span>` +
        `<span class="bname">${escapeHtml(s.label)}</span>` +
        `<span class="bhost">${s.host === "local" ? "local" : escapeHtml(s.host)}</span>` +
      `</div>` +
      `<div class="btail">${s.tail ? tailHtml(s.tail, s.state) : `<span class="d">${escapeHtml(s.path || "")}</span>`}` +
        `${live ? ` <span class="caret"></span>` : ""}</div>` +
      `<div class="bcard-f">` +
        `<span class="bchip"><i class="dot${s.state === "running" ? " pulse" : ""}"></i>${STATE_WORD[s.state] || s.state}</span>` +
        `<span class="wn">${escapeHtml(stateWhen(s))}</span>` +
        `<button class="mini" type="button" title="${active ? "attached" : "attach"}">${active ? "attached" : "attach"}</button>` +
      `</div>`;
    card.onclick = () => attach({ host: s.host, name: s.name, kind: s.kind, label: s.label, dir: s.dir, path: s.path });
    grid.appendChild(card);
  }
}

// --- board <-> attached view switch ----------------------------------------
function boardShown() { return !document.getElementById("board-view").hidden; }
// The board button flips between the two views; the wordmark always goes home to
// the board. Clicking a card is *not* a way back -- it re-attaches you to a
// different session -- so without a real toggle there was no way to leave the
// board without changing what you were attached to.
function showBoard() {
  document.getElementById("board-view").hidden = false;
  document.querySelector(".layout").hidden = true;
  document.getElementById("board-toggle").classList.add("active");
  renderBoard();
}
function showAttached() {
  document.getElementById("board-view").hidden = true;
  document.querySelector(".layout").hidden = false;
  document.getElementById("board-toggle").classList.remove("active");
  refitAll(); // the panes were display:none -- size their terminals to the pane now
}

document.getElementById("board-toggle").addEventListener("click",
  () => (boardShown() ? showAttached() : showBoard()));
// the board is the landing view, so the button starts lit to match it
document.getElementById("board-toggle").classList.toggle("active", boardShown());
document.getElementById("brand").addEventListener("click", showBoard); // the wordmark is home too
document.getElementById("jump").addEventListener("click", () => openPalette()); // the search pill opens the palette
document.getElementById("board-filter").addEventListener("input", (e) => {
  filterText = e.target.value.trim().toLowerCase();
  const f = document.getElementById("filter"); if (f) f.value = e.target.value; // keep the two filters in sync
  renderBoard();
});

// --- attach ----------------------------------------------------------------
//
// tmux keeps every session alive, so a dropped websocket (laptop sleep, network
// blip, server restart) just needs a fresh attach to resume. We reattach to the
// same session automatically with capped backoff. Switching sessions bumps
// attachGen, so a stale socket's close handler can never reattach the old one.

function paneOpenSocket(p, target, gen) {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const q = new URLSearchParams({
    host: target.host,
    name: target.name || "",
    kind: target.kind,
    label: target.label || "main",
  });
  if (target.path) q.set("path", target.path);
  if (target.resume) q.set("resume", target.resume);
  q.set("mouse", termSettings.scrollbackMouse ? "1" : "0");
  q.set("history", String(termSettings.scrollback));  // tmux history-limit (depth)

  const sock = new WebSocket(`${proto}://${location.host}/ws/attach?${q}`);
  p.ws = sock;
  sock.binaryType = "arraybuffer";
  sock.onopen = () => {
    if (gen !== p.attachGen) { sock.close(); return; } // a newer attach won the race
    paneRefit(p);
    p.term.focus();
    // The local websocket always opens instantly even if the downstream ssh is
    // still timing out, so "open" alone doesn't mean healthy. Only clear the
    // backoff once the session has actually stayed up a while; a connection that
    // dies sooner (e.g. ssh timeout) keeps counting toward the give-up cap.
    clearTimeout(p.stableTimer);
    p.stableTimer = setTimeout(() => {
      if (p.reconnectAttempts > 0) p.term.write("\r\n\x1b[90m[reconnected]\x1b[0m\r\n");
      p.reconnectAttempts = 0;
    }, STABLE_MS);
  };
  sock.onmessage = (e) => {
    if (typeof e.data === "string") p.term.write(e.data);
    else p.term.write(new Uint8Array(e.data));
  };
  sock.onclose = (ev) => {
    clearTimeout(p.stableTimer);
    if (gen !== p.attachGen) return; // superseded by a newer attach
    if (ev && ev.code === 4404) { // unknown host -- don't loop, show a clean error
      toast(`Unknown host "${escapeHtml(target.host)}" — it isn't in your ssh config, so it can't be attached.`, "error", 7000);
      return;
    }
    if (ev && ev.code === 4410) { // session ended (program exited / killed) -- do NOT relaunch
      p.term.write("\r\n\x1b[90m[session ended]\x1b[0m\r\n");
      loadSessions(); // drop it from the sidebar
      return;
    }
    if (ev && ev.code === 4401) { // not authenticated -- prompt a login, then reattach
      p.term.write("\r\n\x1b[33m[signed out — log back in to reattach]\x1b[0m\r\n");
      requireAuth().then(() => { if (gen === p.attachGen) paneOpenSocket(p, target, gen); });
      return;
    }
    paneScheduleReattach(p, target, gen);
  };
}

function paneScheduleReattach(p, target, gen) {
  if (p.reconnectAttempts >= MAX_REATTACH) {
    p.term.write(`\r\n\x1b[33m[gave up after ${MAX_REATTACH} tries \u2014 the host looks unreachable. ` +
      `Check it's up, then click the session to retry.]\x1b[0m\r\n`);
    p.reconnectAttempts = 0;
    return;
  }
  const delay = Math.min(500 * 2 ** p.reconnectAttempts, 5000);
  p.reconnectAttempts++;
  p.term.write(`\r\n\x1b[90m[disconnected \u2014 reattaching in ${(delay / 1000).toFixed(1)}s (${p.reconnectAttempts}/${MAX_REATTACH})]\x1b[0m\r\n`);
  clearTimeout(p.reconnectTimer);
  p.reconnectTimer = setTimeout(() => {
    if (gen === p.attachGen) paneOpenSocket(p, target, gen);
  }, delay);
}

function paneAttach(p, target) {
  p.active = target;
  p.el.classList.remove("empty"); // a session is attached -> drop the hint
  p.attachGen++;
  p.reconnectAttempts = 0;
  clearTimeout(p.reconnectTimer);
  clearTimeout(p.stableTimer);
  if (p.ws) {
    const old = p.ws;
    p.ws = null;
    old.onclose = null; // this close is intentional -- never reattach it
    old.close();
  }
  p.term.reset();
  paneOpenSocket(p, target, p.attachGen);

  paintPaneBar(p);
  if (p === focused) showFilesFor(target.host, sessionDir(target));
  renderTree();
}

// The sidebar, the new-session form, and the command palette all attach into
// whichever pane is currently focused.
function attach(target) {
  showAttached();
  if (typeof setRailOpen === "function") setRailOpen(false); // phone: picking a session closes the drawer
  paneAttach(focused || panes[0], target);
}

// --- files -----------------------------------------------------------------

let filesCwd = { host: "local", path: "~" };
let filesEntries = [];                  // last listing; re-sorted client-side

// Remember the last-browsed directory per host (persisted) and never clobber
// manual navigation: focusing/attaching a session on the SAME host leaves the
// file pane where the user left it; switching hosts restores that host's last
// dir (first visit: the session's dir). This was the "always hopping back to ~"
// complaint -- every click into a pane used to reset the pane.
const FILES_DIRS_KEY = "serai.files.lastdirs";
let filesLastDirs = (() => {
  try { return JSON.parse(localStorage.getItem(FILES_DIRS_KEY) || "{}") || {}; }
  catch { return {}; }
})();
// The session's own directory, if it has one: the configured "start in" dir
// wins over the pane's live cwd.
function sessionDir(t) {
  if (!t) return "";
  const s = sessionList.find((x) => x.host === t.host && x.name === t.name);
  return (s && (s.dir || s.path)) || t.dir || t.path || "";
}

// Follow the session you just opened. This used to bail whenever the host
// matched and otherwise prefer the last-browsed dir, so with a fleet of local
// sessions the file pane never moved -- it always opened somewhere other than
// the session you were actually in.
function showFilesFor(host, hintPath) {
  if (hintPath) { loadFiles(host, hintPath); return; }
  if (host === filesCwd.host) return;  // nothing better to go on -> stay put
  loadFiles(host, filesLastDirs[host] || "~");
}
let fileSort = { key: "name", dir: 1 }; // key: name|size|mtime; dir: 1 asc, -1 desc
let colW = { size: 80, date: 150 };     // resizable column widths (px)
const FILE_PREFS_KEY = "serai.files.prefs";

function saveFilePrefs() {
  try { localStorage.setItem(FILE_PREFS_KEY, JSON.stringify({ sort: fileSort, colW })); } catch { /* ignore */ }
}
function loadFilePrefs() {
  try {
    const p = JSON.parse(localStorage.getItem(FILE_PREFS_KEY) || "null");
    if (p && p.sort) fileSort = p.sort;
    if (p && p.colW) colW = p.colW;
  } catch { /* ignore */ }
}

function applyFileCols() {
  const f = document.getElementById("files");
  f.style.setProperty("--w-size", colW.size + "px");
  f.style.setProperty("--w-date", colW.date + "px");
  document.querySelectorAll("#files-cols .fcol[data-sort]").forEach((c) => {
    const ind = c.querySelector(".sort-ind");
    if (ind) ind.textContent = fileSort.key === c.dataset.sort ? (fileSort.dir > 0 ? " ▲" : " ▼") : "";
    c.classList.toggle("active", fileSort.key === c.dataset.sort);
  });
}

function sortedEntries() {
  const { key, dir } = fileSort;
  const byName = (a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase());
  return [...filesEntries].sort((a, b) => {
    if (a.is_dir !== b.is_dir) return a.is_dir ? -1 : 1; // folders first, always
    let r = 0;
    if (key === "size") r = (a.size || 0) - (b.size || 0);
    else if (key === "mtime") r = (a.mtime || 0) - (b.mtime || 0);
    else r = byName(a, b);
    return (r * dir) || byName(a, b);
  });
}

function fmtSize(n) {
  if (!n) return "";
  if (n < 1024) return `${n} B`;
  if (n < 1048576) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1048576).toFixed(1)} MB`;
}

function fmtDate(secs) {
  if (!secs) return "";
  const d = new Date(secs * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

async function loadFiles(host, path, quiet) {
  const changedDir = filesCwd.host !== host || filesCwd.path !== path;
  if (changedDir) { fileSel = new Set(); selAnchor = selCursor = null; filesEditing = false; } // navigation abandons any inline edit
  filesCwd = { host, path };
  document.getElementById("files-host").textContent = host === "local" ? "local" : host;
  document.getElementById("files-path").value = path; // keep the editable bar in sync
  renderCrumbs(path);
  const box = document.getElementById("file-rows");
  // quiet = a background refresh: don't flash the loading row or clobber the
  // current list on a transient error.
  if (!quiet) box.innerHTML = `<div class="file-msg"><span class="spinner"></span> loading…</div>`;
  let data;
  try {
    data = await (await fetch(`/api/files?host=${encodeURIComponent(host)}&path=${encodeURIComponent(path)}`)).json();
  } catch {
    if (!quiet) box.innerHTML = `<div class="file-msg">could not list directory</div>`;
    return;
  }
  if (data.error) {
    if (!quiet) box.innerHTML = `<div class="file-msg">${escapeHtml(data.error)}</div>`;
    return;
  }
  filesEntries = data.entries || [];
  // Remember only explicit navigations that actually listed -- not `quiet`
  // background refreshes: the 5s auto-refresh can fire before boot's initial
  // load and would otherwise save the pristine "~" over the real last dir.
  if (!quiet) {
    filesLastDirs[host] = path;
    try { localStorage.setItem(FILES_DIRS_KEY, JSON.stringify(filesLastDirs)); } catch { /* ignore */ }
  }
  renderFileRows();
}

// Re-list the current directory, keeping scroll position (manual + auto refresh).
let filesRefreshing = false;
let filesEditing = false; // an inline new-folder/rename input is open
async function refreshFiles() {
  if (filesRefreshing || filesEditing) return; // don't nuke an in-progress inline edit
  filesRefreshing = true;
  const el = document.getElementById("files");
  const scroll = el.scrollTop;
  try {
    await loadFiles(filesCwd.host, filesCwd.path, true);
    el.scrollTop = scroll;
  } finally {
    filesRefreshing = false;
  }
}

// GNOME-style interactions: single click selects, double click opens, right
// click for the operations menu, and the pane takes keyboard input (arrows,
// Enter, F2, Delete, Backspace = up a directory).
let fileSel = new Set(); // selected entry names (within the current listing)
let selAnchor = null;    // fixed end of a shift-range
let selCursor = null;    // moving end (last row acted on; keyboard starts here)
let fileClip = null;     // clipboard: {op: "cut"|"copy", host, items: [{path, name}]}

// Small monochrome type glyphs (kept to widely-supported characters so the
// mono font renders them everywhere): dirs, images, video, audio, archives,
// config, docs/text, code -- everything else gets the plain dot.
const ICON_BY_EXT = {
  png: "▣", jpg: "▣", jpeg: "▣", gif: "▣", svg: "▣", webp: "▣", ico: "▣", bmp: "▣",
  mp4: "▶", mkv: "▶", webm: "▶", mov: "▶", avi: "▶",
  mp3: "♪", wav: "♪", flac: "♪", ogg: "♪", m4a: "♪",
  zip: "▤", tar: "▤", gz: "▤", tgz: "▤", bz2: "▤", xz: "▤", "7z": "▤", rar: "▤", deb: "▤", rpm: "▤",
  json: "⚙", yaml: "⚙", yml: "⚙", toml: "⚙", ini: "⚙", conf: "⚙", env: "⚙",
  md: "≡", txt: "≡", log: "≡", pdf: "≡", csv: "≡", rst: "≡",
  py: "#", js: "#", ts: "#", sh: "#", bash: "#", rs: "#", go: "#", c: "#", h: "#",
  cpp: "#", java: "#", rb: "#", php: "#", css: "#", html: "#", sql: "#", lua: "#",
};
function iconFor(e) {
  if (e.is_dir) return "▸";
  const dot = e.name.lastIndexOf(".");
  const ext = dot > 0 ? e.name.slice(dot + 1).toLowerCase() : "";
  return ICON_BY_EXT[ext] || "·";
}

function entryByName(name) { return filesEntries.find((x) => x.name === name); }
function dlUrl(name) {
  const full = joinPath(filesCwd.path, name);
  return `/api/file?host=${encodeURIComponent(filesCwd.host)}&path=${encodeURIComponent(full)}`;
}

// Fetch the bytes in the page, then hand the download manager a blob: URL.
//
// The obvious approaches both fail here. window.open() spawns a blank tab the
// browser tears down as soon as the transfer starts. A plain same-origin anchor
// makes the *download manager* fetch the URL itself -- and serai's cert is
// self-signed, so on a phone you've clicked through an interstitial and the
// origin carries a certificate error; Chrome refuses downloads from those and
// reports "Download cancelled". Fetching in the page reuses the connection you
// already accepted, and a blob: URL isn't subject to that origin check.
// Past this a download is a real risk to the tab rather than a slow moment.
const DL_WARN_BYTES = 100 * 1024 * 1024;

// The size the file list already knows, so we can warn before starting.
function entryFor(name) {
  const row = document.querySelector(`#file-rows .file-row[data-name="${CSS.escape(name)}"]`);
  return row ? row._entry : null;
}

async function downloadFile(name) {
  const known = (() => { const e = entryFor(name); return e && !e.is_dir ? (e.size || 0) : 0; })();
  // The whole file lands in page memory (see the blob note below), so a big one
  // can take the tab down with it -- likelier on a phone. Say so, don't just try.
  if (known > DL_WARN_BYTES) {
    const ok = await confirmAction(
      `${name} is ${fmtSize(known)}. serai has to hold the whole file in this tab's memory ` +
      `to download it, which can crash the tab — especially on a phone. Download anyway?`,
      "download", false);
    if (!ok) return;
  }

  const t = toast(`Downloading ${escapeHtml(name)}…`, "", 15 * 60 * 1000);
  try {
    const res = await fetch(dlUrl(name), { credentials: "same-origin" });
    if (!res.ok) throw new Error(`server said ${res.status}`);
    const ctype = res.headers.get("content-type") || "application/octet-stream";
    const total = Number(res.headers.get("content-length")) || known || 0;

    // Read the body in chunks so a large file reports progress instead of sitting
    // on a frozen "Downloading…" for a minute. Memory is the same either way.
    let blob;
    if (res.body && res.body.getReader) {
      const reader = res.body.getReader();
      const chunks = [];
      let got = 0, painted = 0;
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        chunks.push(value);
        got += value.length;
        if (Date.now() - painted > 200) { // don't thrash the DOM on every chunk
          painted = Date.now();
          t.innerHTML = total
            ? `Downloading ${escapeHtml(name)} — ${Math.round((got / total) * 100)}% of ${fmtSize(total)}`
            : `Downloading ${escapeHtml(name)} — ${fmtSize(got)}`;
        }
      }
      blob = new Blob(chunks, { type: ctype });
    } else {
      blob = await res.blob(); // no streaming body available: take it in one go
    }

    // Wrap in a File, not a bare Blob: Chrome on Android ignores the download
    // attribute for blob: URLs and names the result after the blob's UUID -- a
    // File carries its own name, which it does honour.
    const type = blob.type || ctype;
    let src = blob;
    try { src = new File([blob], name, { type }); } catch { /* older browsers: Blob is fine */ }
    const obj = URL.createObjectURL(src);
    const a = document.createElement("a");
    a.href = obj;
    a.download = name;
    a.style.display = "none";
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 0); // tearing it down in the same tick can lose the click
    setTimeout(() => URL.revokeObjectURL(obj), 60000);
    if (t && t.remove) t.remove();
    toast(`Downloaded ${escapeHtml(name)}`, "ok", 3000);
  } catch (e) {
    if (t && t.remove) t.remove();
    // a real message beats the browser silently cancelling
    toast(`Download failed: ${escapeHtml(String((e && e.message) || e))}`, "error", 7000);
  }
}
function paintSelection() {
  document.querySelectorAll("#file-rows .file-row").forEach((r) => {
    r.classList.toggle("selected", fileSel.has(r.dataset.name));
  });
  const cur = selCursor && document.querySelector(`#file-rows .file-row[data-name="${CSS.escape(selCursor)}"]`);
  if (cur) cur.scrollIntoView({ block: "nearest" });
}
function selectFile(name) { // single selection (also the modifier-less click)
  fileSel = new Set(name == null ? [] : [name]);
  selAnchor = selCursor = name;
  paintSelection();
}
function selectRange(toName) { // anchor .. toName in display order
  const names = sortedEntries().map((x) => x.name);
  const a = names.indexOf(selAnchor), b = names.indexOf(toName);
  if (a < 0 || b < 0) return selectFile(toName);
  fileSel = new Set(names.slice(Math.min(a, b), Math.max(a, b) + 1));
  selCursor = toName;
  paintSelection();
}
function clickSelect(ev, name) { // GNOME-style: plain / Ctrl toggles / Shift ranges
  if (ev.ctrlKey || ev.metaKey) {
    if (fileSel.has(name)) fileSel.delete(name); else fileSel.add(name);
    selAnchor = selCursor = name;
    paintSelection();
  } else if (ev.shiftKey && selAnchor) {
    selectRange(name);
  } else {
    selectFile(name);
  }
}
function openEntry(e) {
  if (!e) return;
  if (e.is_dir) loadFiles(filesCwd.host, joinPath(filesCwd.path, e.name));
  else downloadFile(e.name);
}

function renderFileRows() {
  const box = document.getElementById("file-rows");
  box.innerHTML = "";

  const up = document.createElement("div");
  up.className = "file-row dir";
  up.innerHTML = `<span class="fname"><span class="ficon">\u2191</span><span class="fn-text">..</span></span>`;
  up.ondblclick = () => loadFiles(filesCwd.host, joinPath(filesCwd.path, ".."));
  up.onclick = null;
  box.appendChild(up);

  for (const e of sortedEntries()) {
    const row = document.createElement("div");
    const full0 = joinPath(filesCwd.path, e.name);
    row.className = "file-row" + (e.is_dir ? " dir" : "")
      + (fileSel.has(e.name) ? " selected" : "")
      + (fileClip && fileClip.op === "cut" && fileClip.host === filesCwd.host
         && fileClip.items.some((it) => it.path === full0) ? " cut" : "");
    row.dataset.name = e.name;
    row._entry = e; // so the delegated long-press can find the entry it was held on
    row.innerHTML =
      `<span class="fname"><span class="ficon">${iconFor(e)}</span>` +
      `<span class="fn-text">${escapeHtml(e.name)}</span></span>` +
      `<span class="fsize">${e.is_dir ? "dir" : fmtSize(e.size)}</span>` +
      `<span class="fdate">${fmtDate(e.mtime)}</span>`;
    row.onclick = (ev) => { clickSelect(ev, e.name); filesBox.focus(); };
    row.ondblclick = () => openEntry(e);
    row.oncontextmenu = (ev) => {
      ev.preventDefault(); ev.stopPropagation();
      if (!fileSel.has(e.name)) selectFile(e.name); // right-click inside a selection keeps it
      openFileMenu(ev, e);
    };
    if (!e.is_dir) {
      // drag a file out of the browser (onto the desktop) to download it
      row.draggable = true;
      row.addEventListener("dragstart", (ev) => {
        ev.dataTransfer.setData("DownloadURL",
          `application/octet-stream:${e.name}:${location.origin}${dlUrl(e.name)}`);
      });
    }
    box.appendChild(row);
  }
}

// --- file operations (cut / copy / paste / rename / delete / new folder) ----

const fileMenu = document.getElementById("file-menu");
let fileMenuEntry = null; // the entry the menu was opened on (null = empty area)

async function fileOp(body) {
  try {
    const r = await (await fetch("/api/files/op", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: filesCwd.host, ...body }),
    })).json();
    if (r.error) { toast(escapeHtml(r.error), "error", 6000); return false; }
    return true;
  } catch { toast("file operation failed", "error", 4000); return false; }
}

function openFileMenu(ev, entry) {
  fileMenuEntry = entry || null;
  const n = entry ? Math.max(1, fileSel.size) : 0; // rows the menu would act on
  const en = (id, on) => document.getElementById(id).classList.toggle("disabled", !on);
  const lbl = (id, base) => { document.getElementById(id).textContent = n > 1 ? `${base} (${n})` : base; };
  lbl("fm-cut", "Cut"); lbl("fm-copy", "Copy"); lbl("fm-delete", "Delete");
  const pasteOk = !!fileClip && fileClip.host === filesCwd.host;
  en("fm-open", n === 1);
  en("fm-download", n === 1 && !!entry && !entry.is_dir);
  en("fm-cut", n >= 1);
  en("fm-copy", n >= 1);
  en("fm-paste", pasteOk);
  en("fm-rename", n === 1);   // renaming several things at once is a footgun
  en("fm-delete", n >= 1);
  en("fm-newdir", true);
  // On a phone the same menu opens as a bottom sheet -- finger-sized rows anchored
  // to the bottom, rather than a floating menu placed at a cursor that isn't there.
  if (isPhone()) openFileSheet(); else placeMenu(fileMenu, ev.clientX, ev.clientY);
}

function isPhone() { return window.matchMedia("(max-width: 820px)").matches; }
let sheetOpenedAt = 0;
function openFileSheet() {
  fileMenu.style.left = ""; fileMenu.style.top = ""; // drop placeMenu's coords so the sheet rules win
  fileMenu.classList.add("sheet");
  fileMenu.hidden = false;
  document.getElementById("sheet-scrim").hidden = false;
  sheetOpenedAt = Date.now();
}
function closeFileMenu() {
  fileMenu.hidden = true;
  fileMenu.classList.remove("sheet");
  const sc = document.getElementById("sheet-scrim");
  if (sc) sc.hidden = true;
}
document.getElementById("sheet-scrim").addEventListener("click", closeFileMenu);
fileMenu.addEventListener("click", () => { // any action closes the sheet with it
  fileMenu.classList.remove("sheet");
  document.getElementById("sheet-scrim").hidden = true;
});

// empty-area right-click: paste / new folder
document.getElementById("files").addEventListener("contextmenu", (ev) => {
  ev.preventDefault();
  openFileMenu(ev, null);
});
window.addEventListener("mousedown", (ev) => {
  // Releasing a long-press fires a synthetic mousedown on the row underneath, which
  // would read as "tapped outside" and shut the sheet the instant it opened.
  if (Date.now() - sheetOpenedAt < 700) return;
  if (!fileMenu.contains(ev.target)) closeFileMenu();
});

// Long-press stands in for right-click on touch. Delegated from the container so
// one timer runs no matter how the list re-renders, and a drag or scroll cancels it.
let swallowNextClick = false;
(function wireLongPress() {
  const box = document.getElementById("files");
  let timer = null, sx = 0, sy = 0, target = null;
  const cancel = () => { if (timer) { clearTimeout(timer); timer = null; } };
  box.addEventListener("touchstart", (ev) => {
    const t = ev.touches[0];
    sx = t.clientX; sy = t.clientY; target = ev.target;
    timer = setTimeout(() => {
      timer = null;
      const rowEl = target && target.closest && target.closest(".file-row");
      const entry = rowEl && rowEl._entry;
      if (entry && !fileSel.has(entry.name)) selectFile(entry.name);
      openFileMenu({ clientX: sx, clientY: sy, preventDefault() {}, stopPropagation() {} }, entry || null);
      // the press also lands as a click -- don't let it re-select or hit a sheet row
      swallowNextClick = true;
      setTimeout(() => { swallowNextClick = false; }, 900); // never outlive its own release
    }, 500);
  }, { passive: true });
  box.addEventListener("touchmove", (ev) => {
    const t = ev.touches[0];
    if (Math.abs(t.clientX - sx) > 10 || Math.abs(t.clientY - sy) > 10) cancel();
  }, { passive: true });
  box.addEventListener("touchend", cancel);
  box.addEventListener("touchcancel", cancel);
})();

// The sheet opens under the finger, so releasing the long-press lands a synthetic
// click on whatever row of the sheet is beneath it -- which would fire an action
// nobody chose (Delete, if you're unlucky). Swallow exactly that one click, at the
// window so it's caught wherever it lands, not just inside the file list.
window.addEventListener("click", (ev) => {
  // isTrusted only: a programmatic click (the download anchor) must never be eaten.
  if (swallowNextClick && ev.isTrusted) { swallowNextClick = false; ev.stopPropagation(); ev.preventDefault(); }
}, true);

function clipSet(op) {
  const names = fileSel.size ? [...fileSel] : (fileMenuEntry ? [fileMenuEntry.name] : []);
  if (!names.length) return;
  fileClip = { op, host: filesCwd.host,
               items: names.map((n) => {
                 const e = entryByName(n);
                 return { path: joinPath(filesCwd.path, n), name: n, isDir: !!(e && e.is_dir) };
               }) };
  renderFileRows(); // show cut items dimmed
}

// Cross-host copy with live byte count. The relay streams NDJSON -- one
// {"moved":N} per chunk, then a terminal {"ok"} or {"error"} -- so a multi-GB
// folder reports instead of sitting silent behind a blocked POST. Returns true
// only on that terminal ok, so a cut still deletes the source only on success.
async function transferWithProgress(srcHost, srcPath, dest, label) {
  let last = null;
  try {
    const res = await fetch("/api/files/transfer", {
      method: "POST", headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({ host: filesCwd.host, path: dest, src_host: srcHost, src_path: srcPath }),
    });
    if (!res.ok || !res.body) {
      const j = await res.json().catch(() => ({}));
      throw new Error(j.error || `server said ${res.status}`);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();                       // keep the partial line for next read
      for (const ln of lines) {
        if (!ln.trim()) continue;
        const msg = JSON.parse(ln);
        last = msg;
        if (msg.ok === undefined && msg.error === undefined) {
          // no total to divide by: a tar stream doesn't know its own size, and a
          // du-based guess would disagree with it. Show the honest number.
          setUploadProgress(`${label} — ${fmtSize(msg.moved)}`, null);
        }
      }
    }
  } catch (e) {
    setUploadProgress(null);
    toast(`Transfer failed: ${escapeHtml(String((e && e.message) || e))}`, "error", 7000);
    return false;
  }
  setUploadProgress(null);
  if (!last || last.error) {
    toast(`Transfer failed: ${escapeHtml((last && last.error) || "no response")}`, "error", 7000);
    return false;
  }
  toast(`Copied ${escapeHtml(label)} — ${fmtSize(last.moved || 0)}`, "ok", 3000);
  return true;
}

async function pasteClip() {
  if (!fileClip) return;
  const cross = fileClip.host !== filesCwd.host;
  const items = fileClip.items; // files relay as bytes, folders as a tar stream
  const pasted = [];
  for (const it of items) {
    let destName = it.name;
    let dest = joinPath(filesCwd.path, destName);
    if (!cross && dest === it.path) {
      if (fileClip.op === "cut") continue; // moving something onto itself: nothing to do
      const dot = destName.lastIndexOf(".");
      destName = dot > 0 ? `${destName.slice(0, dot)} (copy)${destName.slice(dot)}` : `${destName} (copy)`;
      dest = joinPath(filesCwd.path, destName);
    }
    if (cross) {
      const n = items.length > 1 ? `${pasted.length + 1}/${items.length} ${destName}` : destName;
      if (!(await transferWithProgress(fileClip.host, it.path, dest, n))) break;
      // a cross-host cut = transfer + delete the source (on the source host)
      if (fileClip.op === "cut" && !(await fileOp({ op: "delete", host: fileClip.host, path: it.path }))) break;
    } else {
      if (!(await fileOp({ op: fileClip.op === "cut" ? "rename" : "copy", path: it.path, dest }))) break;
    }
    pasted.push(destName);
  }
  if (fileClip.op === "cut" && pasted.length) fileClip = null;
  await loadFiles(filesCwd.host, filesCwd.path, true);
  fileSel = new Set(pasted);
  selAnchor = selCursor = pasted[pasted.length - 1] || null;
  paintSelection();
}

async function deleteSelected(names) {
  names = names.filter((n) => entryByName(n));
  if (!names.length) return;
  let what;
  if (names.length === 1) {
    const e = entryByName(names[0]);
    what = e.is_dir ? `the folder "${names[0]}" and everything inside it` : `"${names[0]}"`;
  } else {
    const preview = names.slice(0, 3).map((n) => `"${n}"`).join(", ");
    what = `${names.length} items (${preview}${names.length > 3 ? ", …" : ""})`;
  }
  if (!(await confirmAction(`Delete ${what}? This is permanent.`))) return;
  for (const n of names) {
    if (!(await fileOp({ op: "delete", path: joinPath(filesCwd.path, n) }))) break;
    if (fileClip) {
      fileClip.items = fileClip.items.filter((it) => it.path !== joinPath(filesCwd.path, n));
      if (!fileClip.items.length) fileClip = null;
    }
  }
  fileSel = new Set();
  selAnchor = selCursor = null;
  await loadFiles(filesCwd.host, filesCwd.path, true);
}

function startRename(name) {
  const row = [...document.querySelectorAll("#file-rows .file-row")].find((r) => r.dataset.name === name);
  const txt = row && row.querySelector(".fn-text");
  if (!txt) return;
  const input = document.createElement("input");
  input.className = "inline-edit";
  input.value = name;
  txt.replaceWith(input);
  input.focus();
  filesEditing = true; // pause the background refresh so it can't wipe this input
  const dot = name.lastIndexOf(".");
  input.setSelectionRange(0, dot > 0 ? dot : name.length); // select the stem, GNOME-style
  let done = false;
  const commit = async () => {
    if (done) return; done = true; filesEditing = false;
    const nn = input.value.trim();
    if (!nn || nn === name || nn.includes("/")) { renderFileRows(); return; }
    if (await fileOp({ op: "rename", path: joinPath(filesCwd.path, name), dest: joinPath(filesCwd.path, nn) })) {
      fileSel = new Set([nn]);
      selAnchor = selCursor = nn;
    }
    await loadFiles(filesCwd.host, filesCwd.path, true);
  };
  input.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    if (ev.key === "Enter") commit();
    else if (ev.key === "Escape") { done = true; filesEditing = false; renderFileRows(); }
  });
  input.addEventListener("blur", () => { if (!done) { done = true; filesEditing = false; renderFileRows(); } });
  input.addEventListener("click", (ev) => ev.stopPropagation());
  input.addEventListener("dblclick", (ev) => ev.stopPropagation());
}

function startNewFolder() {
  const box = document.getElementById("file-rows");
  const row = document.createElement("div");
  row.className = "file-row dir";
  row.innerHTML = `<span class="fname"><span class="ficon">▸</span></span>`;
  const input = document.createElement("input");
  input.className = "inline-edit";
  input.placeholder = "new folder name";
  row.querySelector(".fname").appendChild(input);
  box.prepend(row);
  input.focus();
  filesEditing = true; // pause the background refresh so it can't wipe this input
  let done = false;
  const commit = async () => {
    if (done) return; done = true; filesEditing = false;
    const nn = input.value.trim();
    if (!nn || nn.includes("/")) { renderFileRows(); return; }
    if (await fileOp({ op: "mkdir", path: joinPath(filesCwd.path, nn) })) {
      fileSel = new Set([nn]);
      selAnchor = selCursor = nn;
    }
    await loadFiles(filesCwd.host, filesCwd.path, true);
  };
  input.addEventListener("keydown", (ev) => {
    ev.stopPropagation();
    if (ev.key === "Enter") commit();
    else if (ev.key === "Escape") { done = true; filesEditing = false; renderFileRows(); }
  });
  input.addEventListener("blur", () => { if (!done) { done = true; filesEditing = false; renderFileRows(); } });
}

document.getElementById("fm-open").addEventListener("click", () => { fileMenu.hidden = true; openEntry(fileMenuEntry); });
document.getElementById("fm-download").addEventListener("click", () => { fileMenu.hidden = true; if (fileMenuEntry) downloadFile(fileMenuEntry.name); });
document.getElementById("fm-cut").addEventListener("click", () => { fileMenu.hidden = true; clipSet("cut"); });
document.getElementById("fm-copy").addEventListener("click", () => { fileMenu.hidden = true; clipSet("copy"); });
document.getElementById("fm-paste").addEventListener("click", () => { fileMenu.hidden = true; pasteClip(); });
document.getElementById("fm-rename").addEventListener("click", () => { fileMenu.hidden = true; if (fileMenuEntry) startRename(fileMenuEntry.name); });
document.getElementById("fm-delete").addEventListener("click", () => { fileMenu.hidden = true; if (fileMenuEntry) deleteSelected(fileSel.size ? [...fileSel] : [fileMenuEntry.name]); });
document.getElementById("fm-newdir").addEventListener("click", () => { fileMenu.hidden = true; startNewFolder(); });

// a minimal promise-based confirm (used by delete; the app never uses window.confirm)
let _confirmResolve = null;
function confirmAction(msg, okLabel = "delete", danger = true) {
  return new Promise((resolve) => {
    _confirmResolve = resolve;
    document.getElementById("confirm-msg").textContent = msg;
    const ok = document.getElementById("confirm-ok");
    ok.textContent = okLabel;                  // not every confirm is a deletion
    ok.classList.toggle("danger", danger);
    ok.classList.toggle("primary", !danger);
    document.getElementById("confirm-form").hidden = false;
    ok.focus();
  });
}
function _confirmDone(v) {
  document.getElementById("confirm-form").hidden = true;
  if (_confirmResolve) { _confirmResolve(v); _confirmResolve = null; }
}
document.getElementById("confirm-ok").addEventListener("click", () => _confirmDone(true));
document.getElementById("confirm-cancel").addEventListener("click", () => _confirmDone(false));
document.getElementById("confirm-form").addEventListener("mousedown", (e) => {
  if (e.target === document.getElementById("confirm-form")) _confirmDone(false);
});
document.getElementById("confirm-form").addEventListener("keydown", (e) => {
  if (e.key === "Escape") { e.preventDefault(); _confirmDone(false); }
});

function joinPath(base, part) {
  if (part === "..") {
    const trimmed = base.replace(/\/+$/, "");
    const idx = trimmed.lastIndexOf("/");
    return idx > 0 ? trimmed.slice(0, idx) : (base.startsWith("/") ? "/" : "~");
  }
  return base.endsWith("/") ? base + part : `${base}/${part}`;
}

// Breadcrumbs: the path renders as clickable segments (GNOME-style); clicking
// the empty end of the bar (or Ctrl+L) swaps to the plain text input, Enter
// jumps there, Escape/blur swaps back.
const crumbsBar = document.getElementById("files-crumbs");
const pathInput = document.getElementById("files-path");

function renderCrumbs(path) {
  crumbsBar.innerHTML = "";
  const abs = path.startsWith("/");
  const segs = path.split("/").filter(Boolean);        // "~/git/x" -> ["~","git","x"]
  const parts = abs ? ["/", ...segs] : (segs.length ? segs : ["~"]);
  let prefix = "";
  parts.forEach((seg, i) => {
    if (i > 1 || (i === 1 && parts[0] !== "/")) {
      const sep = document.createElement("span");
      sep.className = "crumb-sep";
      sep.textContent = "/";
      crumbsBar.appendChild(sep);
    }
    prefix = i === 0 ? seg : (prefix === "/" ? "/" + seg : `${prefix}/${seg}`);
    const target = prefix;
    const last = i === parts.length - 1;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "crumb" + (last ? " current" : "");
    b.textContent = seg;
    if (!last) b.onclick = (ev) => { ev.stopPropagation(); loadFiles(filesCwd.host, target); };
    else b.onclick = (ev) => ev.stopPropagation(); // current segment: not a jump
    crumbsBar.appendChild(b);
  });
  crumbsBar.scrollLeft = crumbsBar.scrollWidth; // deep paths: keep the tail visible
}

function pathEditMode(on) {
  crumbsBar.hidden = on;
  pathInput.hidden = !on;
  if (on) {
    pathInput.value = filesCwd.path;
    pathInput.focus();
    pathInput.select();
  }
}
crumbsBar.addEventListener("click", () => pathEditMode(true)); // empty-area click
pathInput.addEventListener("blur", () => pathEditMode(false));

// Manual path entry: type a path and press Enter to jump there on the current
// host, alongside click-to-navigate (which keeps this bar in sync via loadFiles).
document.getElementById("files-path").addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    loadFiles(filesCwd.host, e.target.value.trim() || "~");
    pathEditMode(false);
  } else if (e.key === "Escape") {
    e.preventDefault();
    pathEditMode(false);
  }
});

// Keep the listing current: manual refresh button, on window focus, and a
// gentle poll while the window is focused (so files added/removed externally
// show up without a navigate).
document.getElementById("files-refresh").addEventListener("click", refreshFiles);
window.addEventListener("focus", refreshFiles);
setInterval(() => { if (document.hasFocus()) refreshFiles(); }, 5000);

// Upload with a live progress bar (XHR: fetch still can't report upload
// progress everywhere). Files go up sequentially; the bar shows per-file
// progress and an (i/n) counter for batches.
// frac null -> indeterminate: the folder relay knows how much has moved but not
// how much there is, so it animates rather than showing a fake percentage.
function setUploadProgress(label, frac) {
  const bar = document.getElementById("upload-bar");
  if (label == null) { bar.hidden = true; return; }
  bar.hidden = false;
  document.getElementById("upload-label").textContent = label;
  const fill = document.getElementById("upbar-fill");
  fill.classList.toggle("indeterminate", frac == null);
  fill.style.width = frac == null ? "35%" : `${Math.round(frac * 100)}%`;
}

function uploadOne(file, dest, label) {
  return new Promise((resolve) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/upload?host=${encodeURIComponent(filesCwd.host)}&path=${encodeURIComponent(dest)}`);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) setUploadProgress(label, ev.loaded / ev.total);
    };
    xhr.onload = () => resolve(xhr.status >= 200 && xhr.status < 300);
    xhr.onerror = () => resolve(false);
    const fd = new FormData();
    fd.append("file", file);
    xhr.send(fd);
  });
}

async function uploadFiles(fileList) {
  const files = [...fileList];
  if (!files.length) return;
  for (let i = 0; i < files.length; i++) {
    const f = files[i];
    const label = files.length > 1 ? `uploading ${f.name} (${i + 1}/${files.length})` : `uploading ${f.name}`;
    setUploadProgress(label, 0);
    if (!(await uploadOne(f, joinPath(filesCwd.path, f.name), label))) {
      toast(`upload failed: ${escapeHtml(f.name)}`, "error", 5000);
    }
  }
  setUploadProgress(null);
  loadFiles(filesCwd.host, filesCwd.path);
}

document.getElementById("upload").addEventListener("change", (ev) => {
  uploadFiles(ev.target.files);
  ev.target.value = "";
});

// Drag OS files onto the files section (the list OR its header bar) to upload
// them to the current directory. Handled at the window level: a drop that
// misses the target must be swallowed -- the browser default is to NAVIGATE to
// the dropped file, replacing the app, which read as "drag and drop doesn't
// work". Only external file drags react (types includes "Files"), so dragging
// a file row *out* to download is left untouched.
const filesBox = document.getElementById("files");
const filesHead = document.querySelector(".files-head");
const hasFiles = (e) => e.dataTransfer && [...e.dataTransfer.types].includes("Files");
const overFilesZone = (e) => e.target instanceof Element && !!e.target.closest("#files, .files-head");
function setDropHighlight(on) {
  filesBox.classList.toggle("drop-target", on);
  filesHead.classList.toggle("drop-target", on);
}
window.addEventListener("dragover", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault(); // window-wide: makes every release safe (no navigation)
  const over = overFilesZone(e);
  e.dataTransfer.dropEffect = over ? "copy" : "none"; // no-drop cursor off-target
  setDropHighlight(over);
});
window.addEventListener("dragleave", (e) => {
  if (!e.relatedTarget) setDropHighlight(false); // left the window entirely
});
window.addEventListener("drop", (e) => {
  if (!hasFiles(e)) return;
  e.preventDefault();
  setDropHighlight(false);
  if (overFilesZone(e)) uploadFiles(e.dataTransfer.files);
});

// keyboard navigation for the file pane (the pane is focusable; clicking a row
// focuses it): arrows move the selection, Enter opens, F2 renames, Delete
// deletes (confirmed), Backspace goes up a directory.
filesBox.addEventListener("keydown", (e) => {
  if (e.target !== filesBox) return; // inline rename/new-folder inputs handle their own keys
  const names = sortedEntries().map((x) => x.name);
  const only = fileSel.size === 1 ? [...fileSel][0] : null;
  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    if (!names.length) return;
    const i = names.indexOf(selCursor);
    const ni = e.key === "ArrowDown"
      ? Math.min(names.length - 1, i + 1)
      : Math.max(0, i < 0 ? 0 : i - 1);
    if (e.shiftKey && selAnchor) selectRange(names[ni]); // grow/shrink the range
    else selectFile(names[ni]);
  } else if (e.key === "Enter" && only) {
    openEntry(entryByName(only));
  } else if (e.key === "F2" && only) {
    e.preventDefault();
    startRename(only);
  } else if (e.key === "Delete" && fileSel.size) {
    e.preventDefault();
    deleteSelected([...fileSel]);
  } else if (e.key === "Backspace") {
    e.preventDefault();
    loadFiles(filesCwd.host, joinPath(filesCwd.path, ".."));
  } else if (e.ctrlKey && (e.key === "a" || e.key === "A")) {
    e.preventDefault(); // select all
    fileSel = new Set(names);
    selAnchor = names[0] || null;
    selCursor = names[names.length - 1] || null;
    paintSelection();
  } else if (e.ctrlKey && e.key === "l") {
    e.preventDefault();
    pathEditMode(true); // GNOME muscle memory: Ctrl+L = type a path
  }
});

// --- terminal / files splitter ---------------------------------------------
// Drag the bar between the terminal and the files pane to resize them. The
// terminal is flex:1, so setting the files height reflows it; the chosen height
// persists in localStorage.

const FILES_H_KEY = "serai.files.height";
const vsplit = document.getElementById("vsplit");
let splitting = false, splitStartY = 0, splitStartH = 0;

function setFilesHeight(h) {
  const maxH = Math.max(80, filesBox.parentElement.clientHeight - 220); // leave terminal room
  filesBox.style.height = Math.max(80, Math.min(h, maxH)) + "px";
  fitAll();
}

vsplit.addEventListener("mousedown", (e) => {
  e.preventDefault();
  splitting = true;
  splitStartY = e.clientY;
  splitStartH = filesBox.getBoundingClientRect().height;
  document.body.classList.add("resizing");
});
window.addEventListener("mousemove", (e) => {
  if (splitting) setFilesHeight(splitStartH + (splitStartY - e.clientY)); // drag up -> taller files
});
window.addEventListener("mouseup", () => {
  if (!splitting) return;
  splitting = false;
  document.body.classList.remove("resizing");
  try { localStorage.setItem(FILES_H_KEY, String(filesBox.getBoundingClientRect().height)); } catch { /* ignore */ }
  sendResizeAll();
});

function restoreFilesHeight() {
  try {
    const h = parseFloat(localStorage.getItem(FILES_H_KEY));
    if (h > 0) setFilesHeight(h); // clamps to the current window so the terminal keeps room
  } catch { /* ignore */ }
}
restoreFilesHeight();

// --- collapsible file browser ----------------------------------------------
// A chevron in the files header minimizes the browser to just that footer bar,
// handing the space to the terminal panes (handy on small screens). Persisted.
// v2 key: the approved design shows files as a slim footer drawer, so collapsed
// is now the default (the old key's value is deliberately not carried over).
const FILES_COLLAPSED_KEY = "serai.files.collapsed.v2";
let filesCollapsed = (() => {
  try { const v = localStorage.getItem(FILES_COLLAPSED_KEY); return v === null ? true : v === "1"; }
  catch { return true; }
})();
function setFilesCollapsed(c) {
  filesCollapsed = c;
  filesBox.hidden = c;
  vsplit.hidden = c; // nothing to resize while the list is hidden
  document.querySelector(".main").classList.toggle("files-collapsed", c);
  const btn = document.getElementById("files-toggle");
  btn.textContent = c ? "▴" : "▾";
  btn.title = c ? "Show the file browser" : "Collapse the file browser";
  try { localStorage.setItem(FILES_COLLAPSED_KEY, c ? "1" : "0"); } catch { /* ignore */ }
  refitAll(); // the terminal area just grew/shrank
}
document.getElementById("files-toggle").addEventListener("click", () => setFilesCollapsed(!filesCollapsed));
setFilesCollapsed(filesCollapsed); // apply the persisted state at load

// --- sidebar / main splitter -----------------------------------------------
// Drag the vertical bar between the sidebar and the main area to resize the
// sidebar; the chosen width persists in localStorage (mirrors the vsplit).

const SIDEBAR_W_KEY = "serai.sidebar.width";
const hsplit = document.getElementById("hsplit");
const sidebar = document.querySelector(".sidebar");
let xsplitting = false, xStart = 0, xStartW = 0;

function setSidebarWidth(w) {
  const maxW = Math.max(160, window.innerWidth - 360); // keep room for the main pane
  sidebar.style.width = Math.max(160, Math.min(w, maxW)) + "px";
  fitAll();
}

hsplit.addEventListener("mousedown", (e) => {
  e.preventDefault();
  xsplitting = true;
  xStart = e.clientX;
  xStartW = sidebar.getBoundingClientRect().width;
  document.body.classList.add("resizing-x");
});
window.addEventListener("mousemove", (e) => {
  if (xsplitting) setSidebarWidth(xStartW + (e.clientX - xStart)); // drag right -> wider sidebar
});
window.addEventListener("mouseup", () => {
  if (!xsplitting) return;
  xsplitting = false;
  document.body.classList.remove("resizing-x");
  try { localStorage.setItem(SIDEBAR_W_KEY, String(sidebar.getBoundingClientRect().width)); } catch { /* ignore */ }
  sendResizeAll();
});

function restoreSidebarWidth() {
  try {
    const w = parseFloat(localStorage.getItem(SIDEBAR_W_KEY));
    if (w > 0) setSidebarWidth(w);
  } catch { /* ignore */ }
}
restoreSidebarWidth();

// --- controls --------------------------------------------------------------

// (the rail's filter box is gone -- filtering lives on the board and in the palette)

// (Host/Tagged/All grouping is retired -- the rail is always host-grouped, and
// the workspace tabs in the top bar do the slicing the modes used to.)

// Inline new-session form (replaces the old prompt() chain).
const nsForm = document.getElementById("new-session-form");
const nsHost = document.getElementById("ns-host");
const nsKind = document.getElementById("ns-kind");
const nsLabel = document.getElementById("ns-label");
const nsPath = document.getElementById("ns-path");
const nsPathRow = document.getElementById("ns-path-row");
const nsResume = document.getElementById("ns-resume");
const nsResumeRow = document.getElementById("ns-resume-row");
let nsPathDirty = false; // true once the user hand-edits the path

function refreshClaudePath() {
  if (nsKind.value === "claude" && !nsPathDirty) {
    nsPath.value = "~/git/" + ((nsLabel.value || "").trim() || "myproject");
  }
}

function syncPathRow() {
  nsPathRow.hidden = false;                          // every kind can start somewhere
  nsResumeRow.hidden = nsKind.value !== "claude";    // resume is Claude-only
  refreshClaudePath();
}

function populateHostOptions() {
  // Host options come from /api/hosts at runtime (ssh config is the source of
  // truth) plus the implicit "local" — no hosts are hardcoded. Offline hosts
  // stay selectable but are dimmed and tagged so their state is obvious.
  const keep = nsHost.value;
  nsHost.innerHTML = "";
  const localOpt = document.createElement("option");
  localOpt.value = "local";
  localOpt.textContent = "local";
  nsHost.appendChild(localOpt);
  for (const h of hosts) {
    const opt = document.createElement("option");
    opt.value = h.alias;
    const offline = h.reachable === false;
    opt.textContent = offline ? `${h.alias} (offline)` : h.alias;
    if (offline) opt.classList.add("offline");
    nsHost.appendChild(opt);
  }
  if (keep) nsHost.value = keep;
}

function openNewSession() {
  populateHostOptions();
  // the "+ add host" affordance writes to ~/.ssh/config -> admins only
  document.getElementById("ns-addhost").hidden = !(authStatus && authStatus.admin);
  nsKind.value = "shell";
  nsLabel.value = "main";
  nsPath.value = "";
  nsResume.value = "";
  nsPathDirty = false;
  syncPathRow();
  nsForm.hidden = false;
  nsLabel.focus();
  nsLabel.select();
  // re-probe reachability so the picker reflects each host's current state
  loadHosts().then(populateHostOptions);
}

function closeNewSession() {
  nsForm.hidden = true;
}

function submitNewSession() {
  const host = nsHost.value || "local";
  const kind = nsKind.value || "shell";
  const label = (nsLabel.value || "").trim() || "main";
  // a shell may start somewhere too; blank just means "wherever tmux would"
  const typed = nsPath.value.trim();
  const path = kind === "claude" ? (typed || "~/git/" + label) : (typed || null);
  const resume = kind === "claude" ? nsResume.value : "";
  closeNewSession();
  attach({ host, name: "", kind, label, path, resume });
  setTimeout(loadSessions, 800);
}

document.getElementById("new-session").addEventListener("click", () => {
  nsForm.hidden ? openNewSession() : closeNewSession();
});
nsKind.addEventListener("change", syncPathRow);
nsLabel.addEventListener("input", refreshClaudePath);
nsPath.addEventListener("input", () => { nsPathDirty = true; });
document.getElementById("ns-cancel").addEventListener("click", closeNewSession);
document.getElementById("ns-submit").addEventListener("click", submitNewSession);
nsForm.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitNewSession(); }
  else if (e.key === "Escape") { e.preventDefault(); closeNewSession(); }
});

// --- add host (writes a Host block to ~/.ssh/config) ------------------------

const ahForm = document.getElementById("addhost-form");
const ahError = document.getElementById("ah-error");
const ahVal = (id) => document.getElementById(id).value.trim();

function openAddHost() {
  ahError.textContent = "";
  for (const [id, v] of [["ah-alias", ""], ["ah-hostname", ""], ["ah-user", ""],
    ["ah-port", "22"], ["ah-group", ""], ["ah-tags", ""]]) {
    document.getElementById(id).value = v;
  }
  ahForm.hidden = false;
  document.getElementById("ah-alias").focus();
}
function closeAddHost() { ahForm.hidden = true; }

async function submitAddHost() {
  const alias = ahVal("ah-alias");
  if (!alias) { ahError.textContent = "alias is required"; return; }
  const body = {
    alias,
    hostname: ahVal("ah-hostname"),
    user: ahVal("ah-user"),
    port: parseInt(ahVal("ah-port"), 10) || 22,
    group: ahVal("ah-group"),
    tags: ahVal("ah-tags").split(",").map((t) => t.trim()).filter(Boolean),
  };
  try {
    const res = await fetch("/api/hosts", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json();
    if (!res.ok) { ahError.textContent = data.error || "could not add host"; return; }
    closeAddHost();
    await loadHosts();      // refresh /api/hosts so the picker includes it
    populateHostOptions();
    nsHost.value = alias;    // and select the new host
    toast(`added host ${escapeHtml(alias)}`, "ok", 3000);
  } catch { ahError.textContent = "request failed"; }
}

document.getElementById("ns-addhost").addEventListener("click", openAddHost);
document.getElementById("ah-cancel").addEventListener("click", closeAddHost);
document.getElementById("ah-add").addEventListener("click", submitAddHost);
ahForm.addEventListener("mousedown", (e) => { if (e.target === ahForm) closeAddHost(); });
ahForm.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); submitAddHost(); }
  else if (e.key === "Escape") { e.preventDefault(); closeAddHost(); }
});

// --- fleet broadcast -------------------------------------------------------
// Pick several sessions, then type one command line to send to all of them at
// once (tmux send-keys per target, server-side). Useful for fleet commands.

const fleetBtn = document.getElementById("fleet");
const broadcastEl = document.getElementById("broadcast");
const bcastInput = document.getElementById("bcast-input");
const bcastStatus = document.getElementById("bcast-status");

const SELECTED_KEY = "serai.fleet.selected";
function saveSelected() {
  try { localStorage.setItem(SELECTED_KEY, JSON.stringify([...selected.values()])); } catch { /* private mode */ }
}
function loadSelected() {
  try {
    for (const t of JSON.parse(localStorage.getItem(SELECTED_KEY) || "[]")) {
      if (t && t.host && t.name) selected.set(`${t.host}::${t.name}`, { host: t.host, name: t.name });
    }
  } catch { /* ignore corrupt/blocked storage */ }
}

function updateBcastStatus(msg) {
  bcastStatus.textContent = msg || `${selected.size} selected`;
}

function toggleSelect(s) {
  if (selected.has(s.id)) selected.delete(s.id);
  else selected.set(s.id, { host: s.host, name: s.name });
  saveSelected();
  updateBcastStatus();
}

function clearSelected() {
  selected.clear();
  saveSelected();
  updateBcastStatus();
  renderTree();
}

function setFleet(on) {
  fleetMode = on;
  fleetBtn?.classList.toggle("active", on);
  broadcastEl.hidden = !on;
  updateBcastStatus(); // selection persists across toggles (cleared via the clear button)
  renderTree();
}

async function sendBroadcast() {
  const text = bcastInput.value;
  const targets = [...selected.values()];
  if (!text.trim()) return updateBcastStatus("type a command");
  if (!targets.length) return updateBcastStatus("select sessions first");
  updateBcastStatus(`sending to ${targets.length}…`);
  try {
    const res = await (await fetch("/api/broadcast", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text, targets }),
    })).json();
    if (res.error) {
      updateBcastStatus(res.error);
      toast(`broadcast: ${escapeHtml(res.error)}`, "error");
    } else {
      const results = res.results || [];
      const bad = results.filter((r) => !r.ok);
      const lines = results.map((r) =>
        `<div class="t-line ${r.ok ? "good" : "bad"}">${r.ok ? "✓" : "✗"} ` +
        `${escapeHtml(r.host)}${r.name ? " · " + escapeHtml(r.name) : ""}</div>`).join("");
      toast(`<div class="t-title">broadcast → ${res.sent}/${results.length} sent</div>${lines}`,
        bad.length ? "warn" : "ok");
      updateBcastStatus(`sent ${res.sent}/${results.length}`);
    }
  } catch {
    updateBcastStatus("broadcast failed");
    toast("broadcast failed", "error");
  }
  bcastInput.value = "";
  bcastInput.focus();
  setTimeout(loadSessions, 600); // states may flip to running after the command
}

fleetBtn?.addEventListener("click", () => setFleet(!fleetMode));
document.getElementById("bcast-clear").addEventListener("click", clearSelected);
document.getElementById("bcast-send").addEventListener("click", sendBroadcast);
bcastInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendBroadcast(); }
});

// --- session rename + tags -------------------------------------------------
// Edit (✎) on a session row opens this; rename goes through tmux rename-session
// and tags are stored on the session as the @serai_tags user option.

const editForm = document.getElementById("edit-form");
const editLabel = document.getElementById("edit-label");
const editTags = document.getElementById("edit-tags");
const editDir = document.getElementById("edit-dir");
let editing = null; // the session being edited

function openEditSession(s) {
  editing = s;
  editLabel.value = s.label;
  editTags.value = (s.tags || []).join(", ");
  // show the configured start dir; fall back to the live cwd so the field says
  // where the session actually is rather than sitting empty
  editDir.value = s.dir || s.path || "";
  editForm.hidden = false;
  editLabel.focus();
  editLabel.select();
}

function closeEditSession() { editForm.hidden = true; editing = null; }

async function saveEditSession() {
  if (!editing) return;
  const s = editing;
  const newLabel = editLabel.value.trim();
  const newTags = editTags.value.split(",").map((t) => t.trim()).filter(Boolean);
  const newDir = editDir.value.trim();
  const dirChanged = newDir !== (s.dir || s.path || "");
  closeEditSession();
  let name = s.name;
  // rename first (it changes the tmux name), then tag whatever the new name is
  if (newLabel && newLabel !== s.label) {
    try {
      const res = await (await fetch("/api/rename", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host: s.host, name: s.name, kind: s.kind, label: newLabel }),
      })).json();
      if (res.name) name = res.name;
      panes.forEach((p) => {
        if (p.active && p.active.host === s.host && p.active.name === s.name) {
          p.active.name = name; p.active.label = newLabel; // keep the attached session in sync
        }
      });
    } catch { /* fall through to the reload */ }
  }
  try {
    await fetch("/api/tags", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: s.host, name, tags: newTags }),
    });
  } catch { /* ignore; reload shows the resulting state */ }
  if (dirChanged) {
    try {
      const r = await (await fetch("/api/dir", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ host: s.host, name, path: newDir }),
      })).json();
      if (r.error) toast(escapeHtml(r.error), "error", 5000);
      else {
        // follow it now if this session is the one on screen
        panes.forEach((p) => {
          if (p.active && p.active.host === s.host && p.active.name === name) {
            p.active.dir = newDir;
            if (p === focused && newDir) loadFiles(s.host, newDir);
          }
        });
      }
    } catch { /* ignore; reload shows the resulting state */ }
  }
  loadSessions();
}

document.getElementById("edit-save").addEventListener("click", saveEditSession);
document.getElementById("edit-cancel").addEventListener("click", closeEditSession);
editForm.addEventListener("mousedown", (e) => { if (e.target === editForm) closeEditSession(); });
editForm.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); saveEditSession(); }
  else if (e.key === "Escape") { e.preventDefault(); closeEditSession(); }
});

// --- terminal settings ------------------------------------------------------
// The ⚙ button opens a picker; changes apply live and persist in localStorage.

function applyTermSettings() {
  const t = TERM_THEMES[termSettings.theme] || TERM_THEMES["GNOME Dark"];
  panes.forEach((p) => {
    p.term.options.theme = t;
    p.term.options.fontFamily = termSettings.font;
    p.term.options.fontSize = termSettings.size;
    p.term.options.scrollback = termSettings.scrollback;
    p.term.options.cursorStyle = termSettings.cursorStyle;
    p.term.options.cursorBlink = termSettings.cursorBlink;
    p.term.options.drawBoldTextInBrightColors = termSettings.boldBright;
    p.termEl.style.background = t.background;
  });
  refitAll();
}
function saveTermSettings() {
  try { localStorage.setItem("serai.term", JSON.stringify(termSettings)); } catch { /* ignore */ }
}

const settingsForm = document.getElementById("settings-form");
const setTheme = document.getElementById("set-theme");
const setFont = document.getElementById("set-font");
const setSize = document.getElementById("set-size");
const setScrollback = document.getElementById("set-scrollback");
const setCols = document.getElementById("set-cols");
const setCursor = document.getElementById("set-cursor");
const setBlink = document.getElementById("set-blink");
const setBold = document.getElementById("set-bold");
const setMouse = document.getElementById("set-mouse");

function openSettings() {
  setTheme.innerHTML = TERM_THEME_ORDER.map((n) => `<option value="${escapeHtml(n)}">${escapeHtml(n)}</option>`).join("");
  setFont.innerHTML = TERM_FONTS.map((f) => `<option value="${escapeHtml(f.value)}">${escapeHtml(f.name)}</option>`).join("");
  setTheme.value = termSettings.theme;
  setFont.value = termSettings.font;
  setSize.value = termSettings.size;
  setScrollback.value = termSettings.scrollback;
  setCols.value = String(termSettings.cols);
  setCursor.value = termSettings.cursorStyle;
  setBlink.checked = termSettings.cursorBlink;
  setBold.checked = termSettings.boldBright;
  setMouse.checked = termSettings.scrollbackMouse;
  // grid-layout and fleet-broadcast live here now -- the top bar was stripped to
  // the mockup, which has no room for them. Sync both to their live state.
  const layoutSel = document.getElementById("set-layout");
  if (layoutSel) {
    layoutSel.value = layoutMode;
    layoutSel.disabled = panes.length < 2; // layout only means something when split
  }
  const fleetCb = document.getElementById("set-fleet");
  if (fleetCb) fleetCb.checked = fleetMode;
  const updSel = document.getElementById("set-updates");
  if (updSel) {
    updSel.value = currentInterval();
    // SERAI_UPDATE_CHECK=off is an install-wide decision; don't pretend the
    // dropdown can override it
    updSel.disabled = !!updateState.env_locked;
  }
  renderUpdateStatus();
  settingsForm.hidden = false;
}
function closeSettings() { settingsForm.hidden = true; }

// --- update check ----------------------------------------------------------
// The server does the polling (once per instance, cached); this just shows the
// answer and lets the operator pick a cadence or ask for a check now. The whole
// notification is a dot on the gear -- no modal on a tool left open for days.

const UPDATE_DEFAULT = "weekly";
const UPDATE_KEY = "serai.updates.interval";
let updateState = { interval: UPDATE_DEFAULT };

// What the picker should show. Your stored choice wins over the server's view:
// the server answer can lag a save, and reading it back was what made a fresh
// choice snap to the default. An install-wide SERAI_UPDATE_CHECK=off still wins
// over both, because no local preference can re-enable it.
function currentInterval() {
  if (updateState.env_locked) return "off";
  let stored = null;
  try { stored = localStorage.getItem(UPDATE_KEY); } catch { /* storage locked down */ }
  return stored || updateState.interval || UPDATE_DEFAULT;
}

function fmtAgo(ts) {
  if (!ts) return "never checked";
  const secs = Math.max(0, Date.now() / 1000 - ts);
  if (secs < 90) return "checked just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `checked ${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return `checked ${hrs}h ago`;
  return `checked ${Math.round(hrs / 24)}d ago`;
}

function renderUpdateStatus() {
  const el = document.getElementById("upd-status");
  const gear = document.getElementById("settings");
  const s = updateState;
  if (gear) gear.classList.toggle("has-update", !!s.available);
  if (!el) return;
  el.classList.remove("ok", "warn");
  if (s.env_locked) { el.textContent = "disabled by SERAI_UPDATE_CHECK"; return; }
  if (s.available) {
    // link straight to the release notes -- "available" is only useful if you
    // can see what changed and how to take it
    el.innerHTML = s.url
      ? `<a href="${escapeHtml(s.url)}" target="_blank" rel="noopener noreferrer">v${escapeHtml(s.latest)} available</a>`
      : `v${escapeHtml(s.latest)} available`;
    el.classList.add("ok");
  } else if (s.error) {
    el.textContent = s.error;
    el.classList.add("warn");
  } else if (s.no_releases) {
    el.textContent = "no releases published yet";
  } else if (currentInterval() === "off") {
    // your choice, not the server's lagging copy -- picking "never" should read
    // as off immediately rather than after the next round trip
    el.textContent = "checks are off";
  } else {
    el.textContent = `up to date · ${fmtAgo(s.checked_at)}`;
  }
  renderApplyRow();
}

// The one-click updater. Shown only when an update is available AND the caller
// is an admin AND the server says this install can replace itself (a systemd
// unit -- a dev checkout or `./run.sh` can't). When it can't self-update, the
// release-notes link above still tells the admin what to do by hand.
function renderApplyRow() {
  const row = document.getElementById("upd-apply-row");
  const note = document.getElementById("upd-apply-note");
  if (!row) return;
  const s = updateState;
  const canShow = !!s.available && !!(authStatus && authStatus.admin) && !!s.can_apply;
  row.hidden = !canShow;
  if (canShow && note && !note.dataset.busy) {
    note.classList.remove("warn");
    note.textContent = "installs it and restarts";
  }
}

async function refreshUpdates(force) {
  try {
    const res = await fetch(force ? "/api/updates/check" : "/api/updates",
                            force ? { method: "POST" } : undefined);
    if (!res.ok) return;
    updateState = await res.json();
  } catch { /* offline -- keep whatever we last knew */ }
  renderUpdateStatus();
}

document.getElementById("set-updates")?.addEventListener("change", async (e) => {
  const choice = e.target.value;
  localStorage.setItem(UPDATE_KEY, choice);   // syncs to the server blob
  updateState.interval = choice;
  renderUpdateStatus();
  // Save and wait for it to land before asking the server anything: the old
  // code refetched on a timer and could read back the pre-change value, which
  // then overwrote the choice you'd just made.
  await pushSettingsNow();
  refreshUpdates(false);   // a fresh cadence applies now, not at the next load
});

document.getElementById("upd-check")?.addEventListener("click", async (e) => {
  const btn = e.target;
  btn.disabled = true;
  const el = document.getElementById("upd-status");
  if (el) { el.classList.remove("ok", "warn"); el.textContent = "checking…"; }
  await refreshUpdates(true);
  btn.disabled = false;
});

document.getElementById("upd-apply")?.addEventListener("click", async (e) => {
  const btn = e.target;
  const note = document.getElementById("upd-apply-note");
  const ver = updateState.latest;
  if (!confirm(`Update serai to v${ver}? This installs it and restarts the ` +
               `server — your terminals reconnect on their own (tmux keeps them).`)) return;
  btn.disabled = true;
  document.getElementById("upd-check")?.setAttribute("disabled", "true");
  if (note) { note.dataset.busy = "1"; note.classList.remove("warn"); note.textContent = "downloading & installing…"; }
  try {
    const res = await fetch("/api/updates/apply", { method: "POST" });
    const body = await res.json().catch(() => ({}));
    if (res.ok && body.ok) {
      // The server is restarting. trackServerVersion() will see the new
      // X-Serai-Version on the next gated response and prompt a reload; say so.
      if (note) { note.textContent = `installing v${body.to_version} — the page will offer to reload`; }
    } else {
      if (note) { note.classList.add("warn"); note.textContent = body.error || "update failed"; }
      btn.disabled = false;
      document.getElementById("upd-check")?.removeAttribute("disabled");
    }
  } catch {
    // A dropped connection mid-restart is the SUCCESS shape, not a failure:
    // systemd tore the old process down before the response flushed.
    if (note) { note.textContent = "restarting — the page will offer to reload"; }
  } finally {
    if (note) delete note.dataset.busy;
  }
});

function commit(patch) { Object.assign(termSettings, patch); applyTermSettings(); saveTermSettings(); }

setTheme.addEventListener("change", () => commit({ theme: setTheme.value }));
setFont.addEventListener("change", () => commit({ font: setFont.value }));
setSize.addEventListener("input", () => {
  const v = parseInt(setSize.value, 10);
  if (v >= 8 && v <= 32) commit({ size: v });
});
setScrollback.addEventListener("input", () => {
  const v = parseInt(setScrollback.value, 10);
  if (v >= 0 && v <= 500000) commit({ scrollback: v });
});
setCols.addEventListener("change", () => commit({ cols: parseInt(setCols.value, 10) || 0 }));
setCursor.addEventListener("change", () => commit({ cursorStyle: setCursor.value }));
setBlink.addEventListener("change", () => commit({ cursorBlink: setBlink.checked }));
setBold.addEventListener("change", () => commit({ boldBright: setBold.checked }));
setMouse.addEventListener("change", () => {
  // server-side (tmux) option, applied on attach -> re-attach the live session so it takes effect now
  termSettings.scrollbackMouse = setMouse.checked;
  saveTermSettings();
  panes.forEach((p) => { if (p.active) paneAttach(p, p.active); });
});

// --- phone affordances -----------------------------------------------------
// A soft keyboard has no esc/tab/ctrl/arrows, which makes a terminal unusable on
// a phone, so a key bar sends the sequences straight down the pane's socket.
const KEY_SEQ = {
  esc: "\x1b", tab: "\t", ctrlc: "\x03",
  up: "\x1b[A", down: "\x1b[B", left: "\x1b[D", right: "\x1b[C",
  pipe: "|", tilde: "~", slash: "/", dash: "-",
};
function paneSendKey(seq) {
  const p = focused || panes[0];
  if (!p || !p.ws || p.ws.readyState !== WebSocket.OPEN) return;
  p.ws.send(new TextEncoder().encode(seq));
  p.term.focus();
}
document.getElementById("keybar").addEventListener("click", (e) => {
  const b = e.target.closest("[data-k]");
  if (b && KEY_SEQ[b.dataset.k] !== undefined) paneSendKey(KEY_SEQ[b.dataset.k]);
});

// The rail slides over on a phone rather than holding a column.
const railEl = document.querySelector(".sidebar");
const railScrim = document.getElementById("rail-scrim");
function setRailOpen(on) {
  railEl.classList.toggle("open", on);
  railScrim.hidden = !on;
}
document.getElementById("rail-toggle").addEventListener("click", () => setRailOpen(!railEl.classList.contains("open")));
railScrim.addEventListener("click", () => setRailOpen(false));

document.getElementById("mobile-nav").addEventListener("click", (e) => {
  const b = e.target.closest("[data-m]");
  if (!b) return;
  setRailOpen(false);
  if (b.dataset.m === "board") showBoard();
  if (b.dataset.m === "jump") openPalette();
  if (b.dataset.m === "files") { showAttached(); setFilesCollapsed(!filesCollapsed); }
  if (b.dataset.m === "new") openNewSession();
});

document.getElementById("settings").addEventListener("click", openSettings);
document.getElementById("set-layout").addEventListener("change", (e) => setLayout(e.target.value));
document.getElementById("set-fleet").addEventListener("change", (e) => setFleet(e.target.checked));
document.getElementById("split")?.addEventListener("click", splitPane);
document.getElementById("layout")?.addEventListener("click", () => setLayout(layoutMode === "grid" ? "row" : "grid"));
document.getElementById("set-close").addEventListener("click", closeSettings);
settingsForm.addEventListener("mousedown", (e) => { if (e.target === settingsForm) closeSettings(); });
settingsForm.addEventListener("keydown", (e) => { if (e.key === "Escape") { e.preventDefault(); closeSettings(); } });

// --- command palette (double-tap Shift) ------------------------------------

const paletteEl = document.getElementById("palette");
const paletteInput = document.getElementById("palette-input");
const paletteListEl = document.getElementById("palette-list");
let paletteItems = []; // [{session, text, positions, score}]
let paletteIndex = 0;

function escHtml(ch) {
  return ch === "&" ? "&amp;" : ch === "<" ? "&lt;" : ch === ">" ? "&gt;" : ch === '"' ? "&quot;" : ch;
}

// What each session matches against (and shows): kind, label, and host.
function paletteText(s) {
  return `${s.kind === "claude" ? "cc" : "shell"} · ${s.label} · ${s.host}`;
}

// Subsequence fuzzy match. Returns matched positions + a score (contiguous runs
// and word-starts rank higher; shorter text breaks ties), or null on no match.
function fuzzyScore(query, text) {
  if (!query) return { score: 0, positions: [] };
  const q = query.toLowerCase(), t = text.toLowerCase();
  let qi = 0, score = 0, prev = -2, run = 0;
  const positions = [];
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) {
      run = prev === ti - 1 ? run + 1 : 0;
      score += 1 + run * 3;
      if (ti === 0 || /[\s·\-_./]/.test(t[ti - 1])) score += 4; // word-start bonus
      positions.push(ti);
      prev = ti;
      qi++;
    }
  }
  if (qi < q.length) return null; // not all query chars consumed
  return { score: score - t.length * 0.04, positions };
}

function renderPaletteRow(item, i) {
  const row = document.createElement("div");
  row.className = "palette-row" + (i === paletteIndex ? " active" : "");
  if (item.action === "new") {
    row.innerHTML = `<span class="kind">+</span><span class="ptext">new session…</span>`;
    row.onclick = () => choosePalette(i);
    return row;
  }
  const { session: s, text, positions } = item;
  const pos = new Set(positions);
  let inner = "";
  for (let c = 0; c < text.length; c++) {
    const ch = escHtml(text[c]);
    inner += pos.has(c) ? `<span class="hl">${ch}</span>` : ch;
  }
  row.innerHTML =
    `<span class="kind">${KIND_GLYPH[s.kind] || ""}</span>` +
    `<span class="ptext">${inner}</span>` +
    `<span class="dot ${s.state}"></span>` +
    `<button class="pkill" title="kill session">✕</button>`;
  row.onclick = () => choosePalette(i);
  row.querySelector(".pkill").onclick = (ev) => { ev.stopPropagation(); killFromPalette(item); };
  return row;
}

// Kill a session (the ✕ on a sidebar row and in the palette). The attached
// session, if any, tears down server-side and closes with code 4410 -> the
// client shows [session ended] and does not reattach.
async function killSession(s) {
  if (!confirm(`Kill session "${s.label}" on ${s.host}? This ends its tmux session.`)) return false;
  try {
    await fetch("/api/kill", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ host: s.host, name: s.name }),
    });
    toast(`killed ${escapeHtml(s.label)}`, "warn", 3000);
  } catch { toast("kill failed", "error"); }
  await loadSessions();
  return true;
}

async function killFromPalette(item) {
  if (await killSession(item.session)) refreshPalette(); // re-render against the updated list
}

function refreshPalette() {
  const q = paletteInput.value.trim();
  const scored = [];
  for (const s of sessionList) {
    const text = paletteText(s);
    const m = fuzzyScore(q, text);
    if (m) scored.push({ session: s, text, positions: m.positions, score: m.score });
  }
  if (q) scored.sort((a, b) => b.score - a.score || a.text.localeCompare(b.text));
  // sessions first (Enter jumps to the top match), then a pinned "new session" action
  paletteItems = [...scored, { action: "new" }];
  paletteIndex = 0;
  paletteListEl.innerHTML = "";
  paletteItems.forEach((item, i) => paletteListEl.appendChild(renderPaletteRow(item, i)));
}

function movePalette(delta) {
  if (!paletteItems.length) return;
  paletteIndex = (paletteIndex + delta + paletteItems.length) % paletteItems.length;
  [...paletteListEl.children].forEach((el, i) => el.classList.toggle("active", i === paletteIndex));
  const el = paletteListEl.children[paletteIndex];
  if (el && el.scrollIntoView) el.scrollIntoView({ block: "nearest" });
}

function choosePalette(i) {
  const item = paletteItems[i];
  if (!item) return;
  closePalette();
  if (item.action === "new") { openNewSession(); return; }
  const s = item.session;
  attach({ host: s.host, name: s.name, kind: s.kind, label: s.label, dir: s.dir, path: s.path });
}

function openPalette() {
  if (!paletteEl.hidden) return;
  paletteInput.value = "";
  paletteEl.hidden = false;
  refreshPalette();
  paletteInput.focus();
}

function closePalette() {
  paletteEl.hidden = true;
}

paletteInput.addEventListener("input", refreshPalette);
paletteInput.addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); movePalette(1); }
  else if (e.key === "ArrowUp") { e.preventDefault(); movePalette(-1); }
  else if (e.key === "Enter") { e.preventDefault(); choosePalette(paletteIndex); }
  else if (e.key === "Escape") { e.preventDefault(); closePalette(); }
});
// click on the backdrop (outside the box) closes it
paletteEl.addEventListener("mousedown", (e) => { if (e.target === paletteEl) closePalette(); });

// Double-tap Shift opens the palette. A non-Shift key between taps breaks it, so
// holding Shift to type capitals never triggers; e.repeat ignores key autorepeat.
let lastShift = 0;
window.addEventListener("keydown", (e) => {
  if (e.key === "Shift") {
    if (e.repeat || !paletteEl.hidden) return;
    const now = performance.now();
    if (now - lastShift < 400) { lastShift = 0; openPalette(); }
    else lastShift = now;
  } else {
    lastShift = 0;
  }
});

// --- boot ------------------------------------------------------------------

// file columns: click a header to sort (toggles direction), drag a divider to resize
document.querySelectorAll("#files-cols .fcol[data-sort]").forEach((c) => {
  c.addEventListener("click", (e) => {
    if (e.target.classList.contains("col-resize")) return; // a resize drag, not a sort
    const key = c.dataset.sort;
    if (fileSort.key === key) fileSort.dir = -fileSort.dir;
    else { fileSort.key = key; fileSort.dir = 1; }
    saveFilePrefs();
    applyFileCols();
    renderFileRows();
  });
});
document.querySelectorAll("#files-cols .col-resize").forEach((h) => {
  h.addEventListener("mousedown", (e) => {
    e.preventDefault();
    e.stopPropagation();
    const col = h.parentElement.dataset.col; // "size" | "date"
    const startX = e.clientX, startW = colW[col];
    const onMove = (ev) => { colW[col] = Math.max(50, Math.min(400, startW + (startX - ev.clientX))); applyFileCols(); };
    const onUp = () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      document.body.classList.remove("col-resizing");
      saveFilePrefs();
    };
    document.body.classList.add("col-resizing");
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  });
});

// --- boot ------------------------------------------------------------------

(async function boot() {
  await requireAuth();  // block on a login/setup screen until a session cookie is in hand
  updateAccountButton();
  await pullSettings(); // server prefs -> localStorage (authoritative across devices)
  // (re-)apply everything from the now-current localStorage. These also ran at
  // load from the local cache (no flash); this re-applies if the server differed.
  termSettings = loadTermSettings(); applyTermSettings();
  loadFilePrefs(); applyFileCols();
  restoreFilesHeight();
  restoreSidebarWidth();
  loadSelected(); updateBcastStatus();
  // fire-and-forget: the server answers from cache unless a check is due, so
  // this costs nothing on load and paints the gear dot if a release is out
  refreshUpdates(false);
  // re-read the last-browsed dirs now that pullSettings may have updated them
  // (mirrors the termSettings re-read above), then bring the file pane up
  // BEFORE the session sweep -- listing a dir is fast, probing hosts is not.
  try { filesLastDirs = JSON.parse(localStorage.getItem(FILES_DIRS_KEY) || "{}") || {}; } catch { /* keep the parse-time copy */ }
  loadFiles("local", filesLastDirs["local"] || "~"); // resume where you were browsing
  await loadHosts();
  await loadSessions();
  setInterval(loadSessions, REFRESH_MS);
})();
