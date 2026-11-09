// The session rail: tag groups, one row per live session, one ⋮ per row.
// Click opens in the focused pane, Shift+click (or Shift+Enter) in the second.
// Rows keep their DOM identity across polls (keyed), so hover, focus and an
// open menu survive the 5-second refresh.

import { h, keyed, fill, ago } from "../core/dom.js";
import { post, keyOf } from "../core/api.js";
import { confirm, toast } from "../core/dialog.js";
import { openMenu } from "../core/menu.js";
import * as store from "../core/state.js";
import { remember, prefs } from "../core/prefs.js";
import { kindInfo } from "../term/pane.js";
import * as panes from "./panes.js";

let scroll = null;
let foot = null;
const hooks = {};                       // features plug in: edit(session)
let collapsed = new Set(remember("collapsed") || []);

export function setHook(name, fn) { hooks[name] = fn; }

export function mount(railEl) {
  scroll = h("div.rail-scroll", { role: "tree", "aria-label": "Sessions" });
  foot = h("div.rail-foot");
  railEl.replaceChildren(scroll, foot);
  scroll.addEventListener("keydown", onKey);
  store.subscribe(["sessions", "saved", "tag", "focusedKey", "kinds"], render);
  store.subscribe(["version", "connected"], renderFoot);
  // Owner chips are a preference, not server state: they appear/disappear with no
  // poll to wait for. Gated on the key so dragging a splitter doesn't repaint.
  window.addEventListener("serai:prefs", (e) => {
    if (e.detail && "ownerChips" in e.detail) render();
  });
  render();
  renderFoot();
}

const UNTAGGED = "untagged";

function groupsOf(sessions, tag) {
  if (tag) return [[tag, sessions.filter((s) => (s.tags || []).includes(tag))]];
  const by = new Map();
  for (const s of sessions) {
    const tags = s.tags && s.tags.length ? s.tags : [UNTAGGED];
    for (const t of tags) {
      if (!by.has(t)) by.set(t, []);
      by.get(t).push(s);
    }
  }
  return [...by.entries()].sort(([a], [b]) =>
    a === UNTAGGED ? 1 : b === UNTAGGED ? -1 : a.localeCompare(b));
}

function render() {
  const st = store.get();
  const sessions = [...st.sessions].sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name));
  const groups = groupsOf(sessions, st.tag);
  if (!sessions.length) {
    fill(scroll, h("div.rail-empty", h("div", "No sessions yet."), h("div.dim", "Start one with + New.")));
    return;
  }
  if (scroll.querySelector(".rail-empty")) scroll.replaceChildren();
  keyed(scroll, groups, ([g]) => `g:${g}`, ([g, items]) => renderGroup(g, items), (el, [g, items]) => updateGroup(el, g, items));
}

function renderGroup(g, items) {
  const head = h("div.group-head", { role: "button", tabindex: "-1", onclick: () => toggleGroup(g) },
    h("span.chev"), h("span.gname"), h("span.count"));
  const body = h("div.group-rows", { role: "group" });
  const el = h("div.group", head, body);
  updateGroup(el, g, items);
  return el;
}

function updateGroup(el, g, items) {
  const closed = collapsed.has(g);
  el.querySelector(".chev").textContent = closed ? "▸" : "▾";
  el.querySelector(".gname").textContent = g;
  el.querySelector(".count").textContent = String(items.length);
  const body = el.querySelector(".group-rows");
  body.hidden = closed;
  keyed(body, items, (s) => keyOf(s), renderRow, updateRow);
}

function toggleGroup(g) {
  if (collapsed.has(g)) collapsed.delete(g); else collapsed.add(g);
  remember("collapsed", [...collapsed]);
  render();
}

function renderRow(s) {
  const el = h("div.row", { role: "treeitem", tabindex: "0" },
    h("span.dot"), h("span.kind"), h("span.name"), h("span.chip.owner"), h("span.host"),
    h("span.fav", "★"), h("span.age"),
    h("button.more", { type: "button", title: "Session actions", "aria-label": "Session actions" }, "⋮"));
  el.addEventListener("click", (e) => {
    if (e.target.closest(".more")) return;
    openSession(el._s, e.shiftKey ? "second" : "focused");
  });
  el.addEventListener("auxclick", (e) => { if (e.button === 1) { e.preventDefault(); openSession(el._s, "second"); } });
  el.querySelector(".more").addEventListener("click", (e) => {
    e.stopPropagation();
    rowMenu(el._s, e.currentTarget, el);
  });
  el.addEventListener("contextmenu", (e) => { e.preventDefault(); rowMenu(el._s, { x: e.clientX, y: e.clientY }, el); });
  updateRow(el, s);
  return el;
}

function updateRow(el, s) {
  el._s = s;
  const st = store.get();
  const info = kindInfo(s.kind);
  const cls = store.STATE_CLASS[s.state] || "idle";
  const dot = el.querySelector(".dot");
  dot.className = `dot ${cls}${cls === "working" || cls === "blocked" ? " pulse" : ""}`;
  dot.title = store.STATE_LABEL[s.state] || s.state;
  const chip = el.querySelector(".kind");
  chip.className = `kind ${info.chip}`;
  chip.textContent = info.chip.toUpperCase();
  el.querySelector(".name").textContent = s.label || s.name;
  el.querySelector(".name").title = s.name;
  const host = el.querySelector(".host");
  host.textContent = s.host === "local" ? "" : s.host;
  host.hidden = s.host === "local";
  const owner = el.querySelector(".chip.owner");
  owner.textContent = s.owner || "";
  owner.hidden = !(prefs().ownerChips && st.auth && st.auth.admin && s.owner);
  el.querySelector(".fav").hidden = !isFavorite(s);
  el.querySelector(".age").textContent = s.state === "running" ? "now" : ago(s.age);
  el.classList.toggle("selected", st.focusedKey === `${s.host}\u001f${s.name}`);
  el.setAttribute("aria-label", `${s.label || s.name}, ${store.STATE_LABEL[s.state] || s.state}`);
}

function isFavorite(s) {
  return (store.get().saved || []).some((r) => r.host === s.host && r.name === s.name && r.favorite);
}

export function openSession(s, where = "focused") {
  panes.open({ host: s.host, name: s.name, kind: s.kind, label: s.label }, where);
}

function rowMenu(s, at, rowEl) {
  const info = kindInfo(s.kind);
  const fav = isFavorite(s);
  const saved = (store.get().saved || []).find((r) => r.host === s.host && r.name === s.name);
  const launch = (saved && saved.launch) || "resume";
  const flag = launch === "new" ? "" : (info.resume[launch] || "");
  rowEl.classList.add("menu-open");
  openMenu(at, [
    hooks.edit ? { label: "Edit session…", icon: "✎", onClick: () => hooks.edit(s) } : null,
    { label: fav ? "Remove from favorites" : "Add to favorites", icon: "★", onClick: () => setFavorite(s, !fav) },
    { label: "Open in second pane", icon: "⫿", kbd: "⇧⏎", onClick: () => openSession(s, "second") },
    info.agent && s.at_prompt ? "sep" : null,
    info.agent && s.at_prompt ? { label: `Relaunch ${info.cmd}`, sub: flag, icon: "▶", onClick: () => relaunch(s),
      hint: `${info.cmd} exited; the session is at a shell prompt` } : null,
    "sep",
    { label: "Close session…", icon: "✕", danger: true, onClick: () => closeSession(s) },
  ].filter(Boolean), { onClose: () => rowEl.classList.remove("menu-open") });
}

async function setFavorite(s, favorite) {
  try {
    await post("/api/favorite", { host: s.host, name: s.name, favorite });
    toast(favorite ? `${s.label || s.name} added to favorites` : `${s.label || s.name} removed from favorites`);
    window.dispatchEvent(new CustomEvent("serai:poll", { detail: { saved: true } }));
  } catch (err) {
    toast(err.message, "error", 5000);
  }
}

async function relaunch(s) {
  try {
    const r = await post("/api/sessions/relaunch", { host: s.host, name: s.name });
    toast(`started: ${r.command}`);
    window.dispatchEvent(new CustomEvent("serai:poll"));
  } catch (err) {
    toast(err.message, "error", 5000);
  }
}

async function closeSession(s) {
  const fav = isFavorite(s);
  const ok = await confirm({
    title: `Close ${s.label || s.name}?`,
    message: fav
      ? "This ends the tmux session and anything running in it. It stays in Favorites, so you can launch it again."
      : "This ends the tmux session and anything running in it, and forgets its saved profile.",
    confirmLabel: "Close session",
    danger: true,
  });
  if (!ok) return;
  try {
    const r = await post("/api/kill", { host: s.host, name: s.name });
    panes.forget(s.host, s.name);
    toast(r.kept ? "closed -- its favorite is kept" : "closed");
    window.dispatchEvent(new CustomEvent("serai:poll", { detail: { saved: true } }));
  } catch (err) {
    toast(err.message, "error", 5000);
  }
}

function onKey(e) {
  const row = e.target.closest(".row");
  if (!row) return;
  const rows = [...scroll.querySelectorAll(".row")].filter((r) => r.offsetParent !== null);
  const i = rows.indexOf(row);
  if (e.key === "ArrowDown" && rows[i + 1]) { e.preventDefault(); rows[i + 1].focus(); }
  else if (e.key === "ArrowUp" && rows[i - 1]) { e.preventDefault(); rows[i - 1].focus(); }
  else if (e.key === "Enter") { e.preventDefault(); openSession(row._s, e.shiftKey ? "second" : "focused"); }
  else if (e.key === "ContextMenu" || (e.key === "F10" && e.shiftKey)) { e.preventDefault(); rowMenu(row._s, row.querySelector(".more"), row); }
}

function renderFoot() {
  const st = store.get();
  fill(foot, h("span", st.version ? `v${st.version}` : ""),
    h("span.conn", { class: st.connected ? "ok" : "bad" }, st.connected ? "● connected" : "● reconnecting…"));
}
