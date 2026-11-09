// The jump palette: one box for "get me to a session". Live sessions first,
// then saved profiles that aren't running (a restore, via the same launchTarget
// the favorites panel uses), then "start something new with this name".
// Opened from the top-bar pill, Ctrl+K / Ctrl+Shift+P, or two Shift presses.

import { h, fill, ago } from "../core/dom.js";
import * as store from "../core/state.js";
import * as dialog from "../core/dialog.js";
import * as topbar from "../views/topbar.js";
import * as panes from "../views/panes.js";
import { kindInfo } from "../term/pane.js";
import { openNew } from "./profile.js";
import { launchTarget } from "./favorites.js";

const MAX_ITEMS = 12;
const DOUBLE_SHIFT_MS = 350;

export function mount() {
  topbar.addButton(10, h("button.pill", {
    type: "button", title: "Jump to a session (Shift twice, or Ctrl+Shift+P; Ctrl+K outside a terminal)",
    onclick: () => openPalette(),
  }, "⌕ jump to session…", h("kbd", "⇧⇧")));
  let lastShift = 0;
  // Capture phase: this runs before xterm's own key handling, so a shortcut we
  // take is never also typed into the pane (Ctrl+Shift+P would send ^P).
  document.addEventListener("keydown", (e) => {
    if (dialog.isOpen()) { lastShift = 0; return; }   // a dialog owns the keyboard
    const inTerminal = !!(e.target && e.target.closest && e.target.closest(".xterm"));
    const take = () => { e.preventDefault(); e.stopPropagation(); lastShift = 0; openPalette(); };
    if (e.ctrlKey && e.shiftKey && e.code === "KeyP") return take();
    // Ctrl+K is readline's kill-to-end-of-line: leave it to a focused terminal.
    if (e.ctrlKey && !e.shiftKey && e.code === "KeyK" && !inTerminal) return take();
    if (e.key !== "Shift") { lastShift = 0; return; } // any other key breaks the pair
    if (e.repeat) return;
    const now = performance.now();
    if (now - lastShift <= DOUBLE_SHIFT_MS) { lastShift = 0; openPalette(); return; }
    lastShift = now;
  }, true);
}

/** The single match rule: a prefix on the label beats a substring in any field,
 *  which beats a subsequence in any field. -1 = no match. Live-before-saved and
 *  label order settle ties (Array#sort is stable, so the base order holds). */
function rank(item, q) {
  if (!q) return 0;
  const fields = [item.label || "", item.name, item.host, (item.tags || []).join(" ")]
    .map((f) => f.toLowerCase());
  if (fields[0].startsWith(q)) return 0;
  if (fields.some((f) => f.includes(q))) return 1;
  return fields.some((f) => subseq(q, f)) ? 2 : -1;
}

const subseq = (q, s) => {
  let at = 0;
  for (const ch of q) {
    at = s.indexOf(ch, at);
    if (at < 0) return false;
    at++;
  }
  return true;
};

/** Character spans of q inside text: the substring if it is one, else the
 *  greedy subsequence. Rendered as <b> through h(), never innerHTML. */
function marks(text, q) {
  if (!q) return [];
  const low = text.toLowerCase();
  const at = low.indexOf(q);
  if (at >= 0) return [[at, at + q.length]];
  const runs = [];
  let i = 0;
  for (const ch of q) {
    i = low.indexOf(ch, i);
    if (i < 0) return [];
    runs.push([i, ++i]);
  }
  const merged = [];
  for (const r of runs) {
    const last = merged[merged.length - 1];
    if (last && r[0] <= last[1]) last[1] = r[1];
    else merged.push(r);
  }
  return merged;
}

function labelNode(text, q) {
  const spans = marks(text, q);
  if (!spans.length) return text;
  const kids = [];
  let at = 0;
  for (const [a, b] of spans) {
    if (a > at) kids.push(text.slice(at, a));
    kids.push(h("b", text.slice(a, b)));
    at = b;
  }
  if (at < text.length) kids.push(text.slice(at));
  return kids;
}

export function openPalette() {
  const input = h("input", {
    placeholder: "jump to session…", autocomplete: "off", spellcheck: false, autofocus: true,
    oninput: () => redraw(),
  });
  const res = h("div.res");
  const keys = h("div.keys", h("span", "↑↓ navigate"), h("span", "↵ attach"),
    h("span", "⇧↵ open in second pane"), h("span", "esc close"));
  const dlg = dialog.open({
    title: "Jump to session", bare: true, top: true, cls: "palette",
    body: [h("div.pin", h("span", "⌕"), input), res, keys],
  });
  // Closing returns focus to the terminal. done.then -- not onClose -- because
  // close() hands focus back to the opener after onClose runs; this microtask
  // lands after that and wins. (profile.js fought the same ordering.)
  let newDialogFollows = false;               // the + New form wants the caret
  dlg.done.then(() => {
    if (!newDialogFollows) { const p = panes.focusedPane(); if (p) p.focus(); }
  });
  redraw();
  input.focus();

  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      move(e.key === "ArrowDown" ? 1 : -1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const first = res.querySelector(".pr.first");
      if (first) openItem(first._item, e.shiftKey ? "second" : "focused");
    }
  });
  return dlg;

  function query() { return input.value.trim(); }

  function candidates(q) {
    const live = store.get().sessions.map((s) => ({
      role: "live", host: s.host, name: s.name, kind: s.kind, label: s.label,
      tags: s.tags || [], state: s.state, age: s.age,
      hint: (store.STATE_LABEL[s.state] || s.state) + (ago(s.age) ? ` · ${ago(s.age)}` : ""),
    }));
    const saved = (store.get().saved || [])
      .filter((r) => !store.session(r.host, r.name))
      .map((r) => ({
        role: "saved", host: r.host, name: r.name, kind: r.kind, label: r.label,
        tags: r.tags || [], hint: "saved · not running ↵ restore",
      }));
    return live.concat(saved)
      .sort((a, b) => (a.label || a.name).localeCompare(b.label || b.name))
      .map((it) => ({ it, r: rank(it, q) })).filter((x) => x.r >= 0)
      .sort((x, y) => x.r - y.r || Number(x.it.role === "saved") - Number(y.it.role === "saved"))
      .slice(0, MAX_ITEMS)
      .map((x) => x.it);
  }

  function redraw() {
    const q = query().toLowerCase();
    const rows = candidates(q).map((item) => {
      const el = h("div.pr", { onclick: (e) => openItem(item, e.shiftKey ? "second" : "focused") },
        h(`span.dot.${dotClass(item)}`),
        h(`span.kind.${kindInfo(item.kind).chip}`, kindInfo(item.kind).chip.toUpperCase()),
        h("span.name", labelNode(item.label || item.name, q)),
        item.host !== "local" ? h("span.host", item.host) : null,
        ...(item.tags || []).map((t) => h("span.chip", t)),
        h("span.hint", item.hint));
      el._item = item;
      return el;
    });
    const raw = query();
    if (raw) rows.push(h("div.pr.new", { onclick: () => openItem({ role: "new", label: raw }) },
      h("span.name", `New session “${raw}”`), h("span.hint", "↵ new")));
    fill(res, rows);
    highlight(0);
  }

  function dotClass(item) {
    return (item.role === "live" && store.STATE_CLASS[item.state]) || "idle";
  }

  function move(delta) {
    const rows = [...res.querySelectorAll(".pr")];
    if (!rows.length) return;
    const i = rows.indexOf(res.querySelector(".pr.first")) + delta;
    highlight(Math.max(0, Math.min(i, rows.length - 1)));
  }

  function highlight(i) {
    const rows = [...res.querySelectorAll(".pr")];
    rows.forEach((r, n) => r.classList.toggle("first", n === i));
    if (rows[i]) rows[i].scrollIntoView({ block: "nearest" });
  }

  function openItem(item, where = "focused") {
    if (item.role === "new") {
      newDialogFollows = true;
      dlg.close();
      openNew({ label: item.label });
      return;
    }
    if (item.role === "live") {
      panes.open({ host: item.host, name: item.name, kind: item.kind, label: item.label }, where);
    } else {
      const prof = (store.get().saved || []).find((r) => r.host === item.host && r.name === item.name);
      if (prof) panes.open(launchTarget(prof), where);
    }
    dlg.close();
  }
}
