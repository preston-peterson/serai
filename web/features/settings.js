// Settings: ONE dialog, three tabs, no Save button. Every control writes its own
// preference the moment it changes, because the panes re-read prefs on
// "serai:prefs" -- a terminal already open resizes, recolours and restyles live.
// The only server writes are the update interval and the update itself.
//
// Two rules the whole panel follows, both learned the hard way (see the backlog):
// a control is seeded from the stored value, never the server's default, so it
// can't show an answer that isn't in effect; and a value that cannot land
// (env-locked interval, an update in flight) is said out loud, not silently
// ignored.

import { h, fill } from "../core/dom.js";
import { get, post, put } from "../core/api.js";
import * as store from "../core/state.js";
import * as dialog from "../core/dialog.js";
import { prefs, setPrefs } from "../core/prefs.js";
import * as topbar from "../views/topbar.js";
import { TERM_FONTS, TERM_THEME_ORDER } from "../term/themes.js";

const UPDATE_DOT_MS = 6 * 60 * 60 * 1000;    // "check on mount and every 6 hours"
const TABS = [["terminal", "Terminal"], ["appearance", "Appearance"], ["updates", "Updates"]];
const INTERVALS = [["daily", "daily"], ["weekly", "weekly"], ["monthly", "monthly"], ["off", "never"]];
const DENSITY = "dense-comfortable";

let gear = null;
let updateState = null;                       // last /api/updates answer we have seen
const openUpdatePanels = new Set();           // updates panels of open dialogs

export function mount() {
  gear = h("button.btn.icon", { type: "button", title: "Settings", onclick: () => openSettings() });
  showUpdateDot();
  topbar.addButton(40, gear);
  applyDensity(prefs().railDensity === "comfortable");   // the saved density, before anyone opens the dialog
  refreshUpdates();
  setInterval(refreshUpdates, UPDATE_DOT_MS);
}

export function openSettings(tab = "terminal") {
  const panels = {
    terminal: terminalPanel(),
    appearance: appearancePanel(),
    updates: updatesPanel(),
  };
  const tabs = h("div.tabs", { role: "tablist" }, ...TABS.map(([name, label]) => h("div.tab", {
    dataset: { tab: name }, role: "tab", onclick: () => show(name),
  }, label)));
  const show = (name) => {
    for (const [k, el] of Object.entries(panels)) el.hidden = k !== name;
    for (const t of tabs.children) {
      const on = t.dataset.tab === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
    }
  };
  show(tab);
  const dlg = dialog.open({
    title: "Settings",
    wide: true,
    head: tabs,
    body: Object.values(panels),
    actions: [{ label: "Done", kind: "primary" }],
  });
  openUpdatePanels.add(panels.updates);
  dlg.done.then(() => openUpdatePanels.delete(panels.updates));
  return dlg;
}

// ------------------------------------------------------------------- terminal

function terminalPanel() {
  const fontSel = fontSelect();
  return h("div.p",
    row("Font", fontSel),
    row("Size", numberField("fontSize", 9, 28)),
    row("Cursor", seg(["block", "bar", "underline"], prefs().cursorStyle,
      (v) => setPrefs({ cursorStyle: v }))),
    row("Cursor blink", switchControl("cursorBlink")),
    row("Scrollback lines", numberField("scrollback", 1000, 100000),
      h("div.help", "history seeded from tmux when you attach")),
    row("Copy on select", switchControl("copyOnSelect"),
      h("div.help", "copying is Ctrl+Shift+C / Ctrl+C with a selection; "
        + "turn this on to copy on every drag")),
    row("Bracketed paste", switchControl("bracketedPaste"),
      h("div.help", "off by default: some agent TUIs hang when paste is bracketed")));
}

/** TERM_FONTS plus, when the stored font is none of them, that font -- the
 *  default is the platform ui-monospace, and a picker that showed DejaVu for it
 *  would be describing a font the terminal isn't using. */
function fontSelect() {
  const fonts = [...TERM_FONTS];
  if (!fonts.some((f) => f.value === prefs().font)) fonts.unshift({ name: "System default", value: prefs().font });
  const el = h("select.select", { onchange: () => setPrefs({ font: el.value }) },
    ...fonts.map((f) => h("option", { value: f.value }, f.name)));
  el.value = prefs().font;
  return el;
}

function numberField(key, min, max) {
  const el = h("input.input", { type: "number", min, max, step: 1, value: String(prefs()[key]) });
  el.addEventListener("change", () => {
    const n = Math.round(Number(el.value));
    const v = el.value.trim() && Number.isFinite(n) ? Math.min(max, Math.max(min, n)) : prefs()[key];
    el.value = String(v);                    // what was typed that didn't count is undone
    if (v !== prefs()[key]) setPrefs({ [key]: v });
  });
  return el;
}

// ----------------------------------------------------------------- appearance

function appearancePanel() {
  const themeSel = h("select.select", { onchange: () => setPrefs({ theme: themeSel.value }) },
    ...TERM_THEME_ORDER.map((name) => h("option", { value: name }, name)));
  themeSel.value = prefs().theme;
  const chips = store.get().auth;
  return h("div.p",
    row("Terminal palette", themeSel),
    row("Rail density", seg(["compact", "comfortable"], prefs().railDensity || "compact", (v) => {
      setPrefs({ railDensity: v });
      applyDensity(v === "comfortable");
    })),
    chips && chips.admin
      ? row("Show owner chips", switchControl("ownerChips"),
        h("div.help", "whose session it is, on every rail row"))
      : null);
}

function applyDensity(comfortable) {
  document.body.classList.toggle(DENSITY, !!comfortable);
}

// -------------------------------------------------------------------- updates

function updatesPanel() {
  const panel = h("div.p", { hidden: true });
  paintUpdates(panel);
  return panel;
}

function paintUpdates(panel) {
  const u = updateState || {};
  const admin = !!(store.get().auth && store.get().auth.admin);
  const canApply = !!(u.available && u.can_apply && admin);
  // The URL comes from GitHub's release payload via the server; only ever link
  // an https: one (never a javascript: or data: URL, whatever arrives).
  const safeUrl = typeof u.url === "string" && /^https:\/\//i.test(u.url);
  const notes = safeUrl ? h("a", { href: u.url, target: "_blank", rel: "noopener" }, "release notes") : null;
  let busy = false;

  const checkBtn = h("button.btn", { type: "button", onclick: async () => {
    if (busy) return;
    busy = true;
    checkBtn.disabled = true;
    try {
      updateState = await post("/api/updates/check");
      showUpdateDot();
    } catch (err) {
      dialog.toast(err.message, "error", 5000);
    } finally {
      busy = false;
      for (const p of openUpdatePanels) if (p.isConnected) paintUpdates(p);
    }
  } }, "Check now");

  const actions = h("div.field", h("label", "Check for updates"),
    seg(INTERVALS, u.interval || "weekly", async (v) => {
      try {
        await put("/api/settings", { "serai.updates.interval": v });
        updateState = { ...(updateState || {}), interval: v };
      } catch (err) {
        dialog.toast(err.message, "error", 5000);
      }
      for (const p of openUpdatePanels) if (p.isConnected) paintUpdates(p);
    }),
    u.env_locked ? h("div.help", "locked off by SERAI_UPDATE_CHECK on this server") : null);

  const status = !u.current
    ? h("div.note", "update status unavailable")
    : u.available
      ? h("div.note", `v${u.current} · latest is v${u.latest} — `, notes)
      : h("div.note", `v${u.current} · up to date`);

  fill(panel,
    actions,
    row("Status", status),
    h("div.field", h("label", ""), h("div", { style: { display: "flex", gap: "8px" } },
      checkBtn, canApply
        ? h("button.btn.primary", { type: "button", onclick: () => applyUpdate(u.latest) }, "Update now")
        : null)));
}

async function applyUpdate(latest) {
  const ok = await dialog.confirm({
    title: `Update to v${latest}?`,
    message: `Download, verify and install v${latest}, then restart serai? Terminals reconnect on their own.`,
    confirmLabel: "Update",
  });
  if (!ok) return;
  try {
    await post("/api/updates/apply");
    dialog.toast("updating -- serai will restart", "ok", 6000);
  } catch (err) {
    dialog.toast(err.message, "error", 8000);
  }
}

async function refreshUpdates() {
  try {
    updateState = await get("/api/updates");
  } catch { return; }                          // unreachable: keep the last answer
  showUpdateDot();
  for (const p of openUpdatePanels) if (p.isConnected) paintUpdates(p);
}

/** The dot is added or removed rather than hidden: .updot sets `display`, and a
 *  display rule beats the [hidden] attribute. */
function showUpdateDot() {
  const avail = !!(updateState && updateState.available);
  if (gear) gear.title = avail ? `Settings -- v${updateState.latest} available` : "Settings";
  fill(gear, "⚙", avail ? h("span.updot") : null);
}

// ------------------------------------------------------------------ controls

function row(label, ...controls) {
  return h("div.field", h("label", label), ...controls);
}

/** A button group where a radio set would be: few choices, all visible, one on. */
function seg(options, value, onPick) {
  const el = h("div.seg");
  const choices = options.map((o) => (Array.isArray(o) ? o : [o, o]));
  const redraw = () => fill(el, ...choices.map(([v, label]) => h(`button${v === value ? ".on" : ""}`, {
    type: "button", onclick: () => { value = v; redraw(); onPick(v); },
  }, label)));
  redraw();
  return el;
}

function switchControl(key) {
  const el = h("button.switch", { type: "button", role: "switch" });
  const redraw = () => {
    const on = !!prefs()[key];
    el.classList.toggle("on", on);
    el.setAttribute("aria-checked", String(on));
  };
  el.addEventListener("click", () => { setPrefs({ [key]: !prefs()[key] }); redraw(); });
  redraw();                                    // seeded from the stored pref, not the default
  return el;
}
