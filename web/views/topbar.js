// Top bar: brand, workspace (tag) picker, then feature slots on the right.
// Features register their own buttons with addButton() as they land, so the
// bar never shows a control that doesn't work yet.

import { h, fill } from "../core/dom.js";
import { openMenu } from "../core/menu.js";
import * as store from "../core/state.js";
import { remember } from "../core/prefs.js";
import { logout } from "../features/login.js";

let tagBtn = null;
let slots = null;
let leftSlots = null;
let avatar = null;
const buttons = [];

export function mount(el) {
  tagBtn = h("button.btn.tagpick", { type: "button", title: "Workspace: filter the rail by tag", onclick: (e) => tagMenu(e.currentTarget) });
  slots = h("div.slots");
  leftSlots = h("div.slots.left");
  avatar = h("button.avatar", { type: "button", title: "Account", onclick: (e) => accountMenu(e.currentTarget) });
  el.replaceChildren(h("div.brand", { title: "serai" }, "ser", h("b", "a"), "i"), tagBtn, leftSlots,
    h("div.spacer"), slots, avatar);
  store.set({ tag: remember("tag") || "" });
  store.subscribe(["sessions", "tag"], renderTag);
  store.subscribe(["auth"], renderAvatar);
  renderTag();
  renderAvatar();
}

/** addButton(order, el) -- lower order sits further left. A negative order
 *  goes in the left group, right after the tag picker (the board's ⊞). */
export function addButton(order, el) {
  buttons.push({ order, el });
  buttons.sort((a, b) => a.order - b.order);
  leftSlots.replaceChildren(...buttons.filter((b) => b.order < 0).map((b) => b.el));
  slots.replaceChildren(...buttons.filter((b) => b.order >= 0).map((b) => b.el));
}

function tagCounts() {
  const counts = new Map();
  for (const s of store.get().sessions) for (const t of s.tags || []) counts.set(t, (counts.get(t) || 0) + 1);
  return [...counts.entries()].sort(([a], [b]) => a.localeCompare(b));
}

function renderTag() {
  const { tag, sessions } = store.get();
  const n = tag ? sessions.filter((s) => (s.tags || []).includes(tag)).length : sessions.length;
  fill(tagBtn, h("span.ico", "◇"), tag || "all tags", h("span.n", String(n)), " ▾");
}

function tagMenu(anchor) {
  const cur = store.get().tag;
  const pick = (t) => { store.set({ tag: t }); remember("tag", t); };
  openMenu(anchor, [
    { label: "All tags", icon: cur ? "" : "✓", sub: String(store.get().sessions.length), onClick: () => pick("") },
    "sep",
    ...tagCounts().map(([t, n]) => ({ label: t, icon: cur === t ? "✓" : "", sub: String(n), onClick: () => pick(t) })),
  ]);
}

function renderAvatar() {
  const a = store.get().auth;
  const name = a && a.user;
  avatar.textContent = name ? name[0].toUpperCase() : "·";
  avatar.hidden = !(a && a.enabled);
}

const accountHooks = [];
export function addAccountItem(item) { accountHooks.push(item); }

function accountMenu(anchor) {
  const a = store.get().auth || {};
  openMenu(anchor, [
    { hint: `signed in as ${a.user || "?"}${a.admin ? " · admin" : ""}` },
    ...accountHooks.map((f) => f()).filter(Boolean),
    "sep",
    { label: "Sign out", icon: "⎋", onClick: logout },
  ]);
}
