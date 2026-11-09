// One or two terminal panes side by side (v3: two at most), a draggable
// divider, and which one has focus. The rail and the palette open sessions
// through open(); what is open survives a reload.

import { h } from "../core/dom.js";
import * as store from "../core/state.js";
import { prefs, setPrefs, remember } from "../core/prefs.js";
import { openMenu } from "../core/menu.js";
import { Pane } from "../term/pane.js";

const MAX = 2;
let root = null;
const panes = [];
let focused = null;
let divider = null;

export function mount(el) {
  root = el;
  const saved = remember("panes") || [];
  if (saved.length) saved.slice(0, MAX).forEach((t, i) => (i === 0 ? ensure(0) : ensure(1)).attach(t));
  else ensure(0).refresh();
  setFocus(panes[0]);
  store.subscribe(["sessions", "saved", "kinds"], () => panes.forEach((p) => p.refresh()));
}

function ensure(i) {
  while (panes.length <= i) {
    const p = new Pane({ onFocus: setFocus, actions: paneActions });
    panes.push(p);
  }
  layout();
  return panes[i];
}

function paneActions(p) {
  const second = panes.length > 1;
  return [
    !second ? h("button.btn.ghost", { type: "button", title: "Open a second pane (⇧-click a session)", onclick: (e) => { e.stopPropagation(); split(); } }, "split") : null,
    second ? h("button.btn.ghost", { type: "button", title: "Swap panes", onclick: (e) => { e.stopPropagation(); swap(); } }, "⇄") : null,
    second ? h("button.btn.ghost", { type: "button", title: "Close this pane (the session keeps running)", onclick: (e) => { e.stopPropagation(); closePane(p); } }, "✕") : null,
    h("button.btn.ghost", { type: "button", title: "Pane menu", onclick: (e) => { e.stopPropagation(); paneMenu(p, e.currentTarget); } }, "⋮"),
  ];
}

function paneMenu(p, anchor) {
  openMenu(anchor, [
    { label: "Copy selection", icon: "⧉", kbd: "Ctrl+Shift+C", disabled: !p.term.hasSelection(), onClick: () => p.clip.copy() },
    { label: "Paste", icon: "⎘", kbd: "Ctrl+V", onClick: () => p.clip.pasteFromClipboard() },
    "sep",
    { label: "Reconnect", icon: "↻", disabled: !p.target, onClick: () => p.target && p.attach(p.target) },
    { label: "Clear scrollback", icon: "⌫", onClick: () => p.term.clear() },
    panes.length > 1 ? { label: "Close pane", icon: "✕", onClick: () => closePane(p) } : null,
  ].filter(Boolean));
}

function layout() {
  const kids = [];
  panes.forEach((p, i) => {
    if (i === 1) kids.push(divider || (divider = makeDivider()));
    kids.push(p.el);
    p.renderActions();
  });
  root.replaceChildren(...kids);
  applyRatio();
}

function applyRatio() {
  if (panes.length < 2) { panes[0] && (panes[0].el.style.flex = "1 1 0"); return; }
  const r = Math.min(0.8, Math.max(0.2, prefs().splitRatio || 0.5));
  panes[0].el.style.flex = `${r} 1 0`;
  panes[1].el.style.flex = `${1 - r} 1 0`;
}

function makeDivider() {
  const d = h("div.divider", { role: "separator", "aria-orientation": "vertical", title: "Drag to resize" });
  d.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    d.setPointerCapture(e.pointerId);
    const box = root.getBoundingClientRect();
    const move = (ev) => {
      setPrefs({ splitRatio: (ev.clientX - box.left) / box.width });
      applyRatio();
    };
    const up = () => { d.removeEventListener("pointermove", move); d.removeEventListener("pointerup", up); };
    d.addEventListener("pointermove", move);
    d.addEventListener("pointerup", up);
  });
  return d;
}

function setFocus(p) {
  if (!p) return;
  focused = p;
  panes.forEach((q) => q.el.classList.toggle("focused", q === p));
  store.set({ focusedKey: p.target ? `${p.target.host}\u001f${p.target.name}` : "" });
}

function persist() {
  remember("panes", panes.map((p) => p.target).filter(Boolean)
    .map(({ host, name, kind, label }) => ({ host, name, kind, label })));
}

/** Open a session. where: "focused" (default) | "second". If it is already
 *  showing in a pane, that pane takes focus -- and, with target.fresh (after
 *  a restart, which ended the old instance), reattaches in place. */
export function open(target, where = "focused") {
  const showing = panes.find((p) => p.isAttachedTo(target.host, target.name));
  if (showing) {
    if (target.fresh) showing.attach(target);
    setFocus(showing);
    showing.focus();
    persist();
    return showing;
  }
  let p;
  if (where === "second") {
    p = panes.length > 1 ? panes.find((q) => q !== focused) : ensure(1);
  } else {
    p = focused || ensure(0);
  }
  p.attach(target);
  setFocus(p);
  p.focus();
  persist();
  return p;
}

export function split() {
  if (panes.length >= MAX) return;
  const p = ensure(1);
  p.refresh();
  setFocus(p);
}

export function swap() {
  if (panes.length < 2) return;
  panes.reverse();
  layout();
  persist();
}

export function closePane(p) {
  if (panes.length < 2) { p.detach(); persist(); return; }
  const i = panes.indexOf(p);
  panes.splice(i, 1);
  p.dispose();
  layout();
  setFocus(panes[0]);
  persist();
}

/** A session went away (closed from the rail): drop it from any pane. */
export function forget(host, name) {
  for (const p of [...panes]) if (p.isAttachedTo(host, name)) closePane(p);
}

export const focusedPane = () => focused;
export const all = () => panes.slice();

export function focusIndex(i) {
  const p = panes[i];
  if (p) { setFocus(p); p.focus(); }
}
