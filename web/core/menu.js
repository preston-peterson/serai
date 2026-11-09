// Popover menus: the rail row ⋮, the pane ⋮, the terminal's right-click menu.
// One open at a time; closes on an outside press, Esc, scroll, resize, or a pick.

import { h } from "./dom.js";

let current = null;

/** openMenu(at, items, opts) -> close()
 *  at: a DOMRect/element to hang under, or {x, y} for a pointer position.
 *  items: [{label, icon, kbd, sub, danger, hint, disabled, onClick}] or "sep". */
export function openMenu(at, items, { onClose } = {}) {
  closeMenu();
  const el = h("div.menu", { role: "menu" },
    items.map((it) => {
      if (it === "sep") return h("div.sep");
      if (it.hint && !it.label) return h("div.hint", it.hint);
      return [
        h(`div.item${it.danger ? ".danger" : ""}${it.disabled ? ".disabled" : ""}`, {
          role: "menuitem",
          tabindex: "-1",
          onclick: (e) => {
            e.stopPropagation();
            if (it.disabled) return;
            closeMenu();
            it.onClick && it.onClick();
          },
        },
        h("span.ico", it.icon || ""), it.label,
        it.sub ? h("span.sub", it.sub) : null,
        it.kbd ? h("kbd", it.kbd) : null),
        it.hint ? h("div.hint", it.hint) : null,
      ];
    }));
  el.addEventListener("mousedown", (e) => e.stopPropagation());
  document.body.append(el);
  place(el, at);
  const off = () => closeMenu();
  const onKey = (e) => { if (e.key === "Escape") { e.stopPropagation(); closeMenu(); } };
  setTimeout(() => {
    window.addEventListener("mousedown", off);
    window.addEventListener("resize", off);
    window.addEventListener("scroll", off, true);
    window.addEventListener("keydown", onKey, true);
  });
  current = {
    el,
    close() {
      window.removeEventListener("mousedown", off);
      window.removeEventListener("resize", off);
      window.removeEventListener("scroll", off, true);
      window.removeEventListener("keydown", onKey, true);
      el.remove();
      if (onClose) onClose();
    },
  };
  return closeMenu;
}

export function closeMenu() {
  if (current) {
    const c = current;
    current = null;
    c.close();
  }
}

export const menuOpen = () => current !== null;

function place(el, at) {
  let x, y;
  if (at instanceof Element) {
    const r = at.getBoundingClientRect();
    x = r.left;
    y = r.bottom + 4;
  } else if (at && "left" in at) {
    x = at.left;
    y = at.bottom + 4;
  } else {
    x = at.x;
    y = at.y;
  }
  const { width: w, height: hgt } = el.getBoundingClientRect();
  const pad = 6;
  if (x + w > innerWidth - pad) x = Math.max(pad, innerWidth - w - pad);
  if (y + hgt > innerHeight - pad) y = Math.max(pad, y - hgt - (at instanceof Element ? at.getBoundingClientRect().height + 8 : 0));
  el.style.left = `${x}px`;
  el.style.top = `${y}px`;
}
