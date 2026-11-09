// DOM helpers. No framework: h() builds elements, keyed() reconciles a list in
// place so a 5-second poll doesn't rebuild (and blur, and re-scroll) the rail.

/** h("div.row.selected", {onclick, title, dataset:{key}}, child, [kids], "text")
 *  - tag may carry ".class" segments and one "#id"
 *  - attrs: on<event> -> listener, class/className, style (object or string),
 *    dataset (object), hidden/disabled/checked/value (properties), anything
 *    else -> setAttribute (null/false skips it)
 *  - children: strings become text nodes (never HTML); null/false/undefined
 *    are skipped; arrays are flattened */
export function h(spec, attrs, ...children) {
  const m = /^([a-z0-9-]+)?((?:[.#][\w-]+)*)$/i.exec(spec);
  if (!m) throw new Error(`bad element spec: ${spec}`);
  const el = document.createElement(m[1] || "div");
  for (const part of m[2].match(/[.#][\w-]+/g) || []) {
    if (part[0] === ".") el.classList.add(part.slice(1));
    else el.id = part.slice(1);
  }
  if (attrs && (attrs instanceof Node || typeof attrs !== "object" || Array.isArray(attrs))) {
    children.unshift(attrs);
    attrs = null;
  }
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "class" || k === "className") String(v).split(/\s+/).filter(Boolean).forEach((c) => el.classList.add(c));
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k === "dataset") Object.assign(el.dataset, v);
    else if (k === "hidden" || k === "disabled" || k === "checked" || k === "value" || k === "selected") el[k] = v;
    else el.setAttribute(k, v === true ? "" : String(v));
  }
  append(el, children);
  return el;
}

function append(el, kids) {
  for (const c of kids) {
    if (c === null || c === undefined || c === false) continue;
    if (Array.isArray(c)) append(el, c);
    else el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
}

/** Replace all children of el. */
export function fill(el, ...children) {
  el.replaceChildren();
  append(el, children);
  return el;
}

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

/** HTML-escape for the rare place a string must go into innerHTML. Prefer h(). */
export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Keyed reconciliation: make parent's children match items, in order.
 *  key(item) -> string; render(item) -> new element; update(el, item) -> patch
 *  an existing one in place. Elements keep identity (focus, hover, an open
 *  menu anchored on them) across polls. */
export function keyed(parent, items, key, render, update) {
  const old = new Map();
  for (const el of [...parent.children]) {
    if (el.dataset.key !== undefined) old.set(el.dataset.key, el);
  }
  let cursor = parent.firstElementChild;
  for (const item of items) {
    const k = String(key(item));
    let el = old.get(k);
    if (el) {
      old.delete(k);
      if (update) update(el, item);
    } else {
      el = render(item);
      el.dataset.key = k;
    }
    if (el !== cursor) parent.insertBefore(el, cursor);
    else cursor = cursor.nextElementSibling;
  }
  for (const el of old.values()) el.remove();
}

/** "now", "42s", "5m", "3h", "2d" from seconds (negative/unknown -> ""). */
export function ago(secs) {
  if (secs === null || secs === undefined || secs < 0) return "";
  if (secs < 10) return "now";
  if (secs < 60) return `${Math.round(secs)}s`;
  if (secs < 3600) return `${Math.round(secs / 60)}m`;
  if (secs < 86400) return `${Math.round(secs / 3600)}h`;
  return `${Math.round(secs / 86400)}d`;
}
