// The board overview: an optional landing behind the ⊞ in the top bar (the
// attached view is the default). A state-filter strip over a grid of cards,
// each showing a live tail of the pane. Cards keep their DOM identity across
// polls (keyed), and the rail's ⋮ menu is the one session menu here too.

import { h, fill, keyed, ago } from "../core/dom.js";
import { keyOf } from "../core/api.js";
import * as store from "../core/state.js";
import { remember } from "../core/prefs.js";
import * as topbar from "../views/topbar.js";
import { rowMenu, openSession } from "../views/rail.js";
import { kindInfo } from "../term/pane.js";

const STATE_ORDER = ["needs_input", "stuck", "running", "done", "idle"];

let boardEl = null;
let strip = null;
let grid = null;
let empty = null;
let btn = null;
let stateFilter = "";            // one state shown alone, "" = all

export function mount(el) {
  boardEl = el;
  // Persistent strip/grid/empty: keyed() reconciles the grid in place, so cards
  // keep identity (and an open ⋮ menu) across the 5-second polls.
  strip = h("div.strip");
  grid = h("div.grid");
  empty = h("div.empty");
  boardEl.append(h("div.wrap", strip, grid, empty));
  btn = h("button.btn.icon", { type: "button", title: "Board overview", onclick: () => show(boardEl.hidden) }, "⊞");
  topbar.addButton(-10, btn);
  store.subscribe(["sessions", "saved", "tag", "kinds"], render);
  if (remember("view") === "board") show(true);
  else render();
}

function show(on) {
  boardEl.hidden = !on;
  document.querySelector("#app > .layout").hidden = on;
  btn.classList.toggle("on", on);
  remember("view", on ? "board" : "attached");
  if (on) render();
}

function visible() {
  const st = store.get();
  const tagged = st.tag ? st.sessions.filter((s) => (s.tags || []).includes(st.tag)) : st.sessions;
  return stateFilter ? tagged.filter((s) => s.state === stateFilter) : tagged;
}

function byStateThenLabel(a, b) {
  const d = STATE_ORDER.indexOf(a.state) - STATE_ORDER.indexOf(b.state);
  return d || (a.label || a.name).localeCompare(b.label || b.name);
}

function isFavorite(s) {
  return (store.get().saved || []).some((r) => r.host === s.host && r.name === s.name && r.favorite);
}

function render() {
  if (!boardEl || boardEl.hidden) return;
  const all = store.get().tag
    ? store.get().sessions.filter((s) => (s.tags || []).includes(store.get().tag))
    : store.get().sessions;
  // A filter on a state nothing is in any more would hide its own chip and
  // leave the board stuck on "No sessions in this state": drop it.
  if (stateFilter && !all.some((s) => s.state === stateFilter)) stateFilter = "";
  const shown = [...visible()].sort(byStateThenLabel);
  fill(strip,
    ...STATE_ORDER.filter((st) => all.some((s) => s.state === st)).map((st) =>
      h("button.btn.sm", {
        type: "button",
        class: stateFilter === st ? "on" : "",
        onclick: () => { stateFilter = stateFilter === st ? "" : st; render(); },
      }, h(`span.dot.${store.STATE_CLASS[st]}`), store.STATE_LABEL[st],
        h("span.n", String(all.filter((s) => s.state === st).length)))),
    h("span.count", `${shown.length} sessions · click a card to attach`));
  if (shown.length) {
    empty.hidden = true;
    grid.hidden = false;
    keyed(grid, shown, keyOf, renderCard, updateCard);
  } else {
    grid.replaceChildren();
    grid.hidden = true;
    empty.hidden = false;
    empty.textContent = all.length ? "No sessions in this state." : "No sessions yet. Start one with + New.";
  }
}

function renderCard(s) {
  const el = h("div.card",
    h("div.ch", h("span.dot"), h("span.kind"), h("span.name"), h("span.host"), h("span.fav", "★"),
      h("button.more", { type: "button", title: "Session actions", "aria-label": "Session actions" }, "⋮")),
    h("div.term"),
    h("div.cf", h("b"), h("span.age")));
  el.addEventListener("click", (e) => {
    if (e.target.closest(".more")) return;
    show(false);
    openSession(el._s, e.shiftKey ? "second" : "focused");
  });
  el.querySelector(".more").addEventListener("click", (e) => {
    e.stopPropagation();
    rowMenu(el._s, e.currentTarget, el);
  });
  updateCard(el, s);
  return el;
}

function updateCard(el, s) {
  el._s = s;
  const info = kindInfo(s.kind);
  const cls = store.STATE_CLASS[s.state] || "idle";
  const dot = el.querySelector(".dot");
  dot.className = `dot ${cls}${cls === "working" || cls === "blocked" ? " pulse" : ""}`;
  dot.title = store.STATE_LABEL[s.state] || s.state;
  el.className = `card ${cls}`;
  const chip = el.querySelector(".kind");
  chip.className = `kind ${info.chip}`;
  chip.textContent = info.chip.toUpperCase();
  el.querySelector(".name").textContent = s.label || s.name;
  el.querySelector(".name").title = s.name;
  const host = el.querySelector(".host");
  host.textContent = s.host === "local" ? "" : s.host;
  host.hidden = s.host === "local";
  el.querySelector(".fav").hidden = !isFavorite(s);
  el.querySelector(".term").textContent = String(s.tail || "").split("\n").slice(-6).join("\n");
  const state = el.querySelector(".cf b");
  state.textContent = store.STATE_LABEL[s.state] || s.state;
  state.style.color = `var(--st-${cls})`;
  el.querySelector(".cf .age").textContent = s.state === "running" ? "now" : ago(s.age);
}
