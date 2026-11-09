// Preferences (terminal look, copy/paste options, layout bits). Kept in this
// browser, and the ones worth carrying between devices are mirrored to the
// server's settings file (serai/settings.py, merged key by key) and pulled
// back at boot -- as v2 did. A failed storage read (private window, blocked
// site data) just means defaults.

import { get, put } from "./api.js";

const KEY = "serai.v3.prefs";

export const DEFAULTS = {
  font: "ui-monospace, 'DejaVu Sans Mono', monospace",
  fontSize: 14,
  cursorStyle: "block",      // block | bar | underline
  cursorBlink: true,
  scrollback: 10000,         // lines kept in the browser and seeded on attach
  theme: "Slate",
  copyOnSelect: false,       // v3 decision: off; Ctrl+Shift+C / Ctrl+C copy
  bracketedPaste: false,     // off: a TUI that mishandles it freezes (grok)
  railWidth: 300,
  splitRatio: 0.5,
};

let cache = null;

export function prefs() {
  if (!cache) {
    let stored = {};
    try { stored = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch { stored = {}; }
    cache = { ...DEFAULTS, ...stored };
  }
  return cache;
}

export function setPrefs(patch) {
  cache = { ...prefs(), ...patch };
  try { localStorage.setItem(KEY, JSON.stringify(cache)); } catch { /* storage blocked: session-only */ }
  push(KEY, cache);
  window.dispatchEvent(new CustomEvent("serai:prefs", { detail: patch }));
  return cache;
}

/** Small per-viewer UI memory (collapsed groups, open panes) under its own key. */
export function remember(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`serai.v3.${key}`) || "null");
    localStorage.setItem(`serai.v3.${key}`, JSON.stringify(value));
  } catch { /* ignore */ }
  if (SYNCED.has(key)) push(`serai.v3.${key}`, value);
  return value;
}

// -- server mirror -------------------------------------------------------------

// What follows you between devices. Everything else remember()s is about this
// screen (open panes, board vs attached, drawer height, phone recents).
const SYNCED = new Set(["favOrder", "collapsed", "tag"]);
const pending = {};
let pushTimer = 0;

function push(storageKey, value) {
  pending[storageKey] = JSON.stringify(value);
  clearTimeout(pushTimer);
  pushTimer = setTimeout(() => {
    const blob = { ...pending };
    for (const k of Object.keys(pending)) delete pending[k];
    put("/api/settings", blob).catch(() => { /* offline: local copy stands */ });
  }, 500);
}

const V2_TERM = "serai.term";

/** At boot, before the views mount: take the server's copy of the synced
 *  preferences, and -- the first time v3 meets an install that ran v2 --
 *  import v2's terminal settings so the upgrade keeps your look. */
export async function pullSettings() {
  let blob;
  try { blob = await get("/api/settings"); } catch { return; }
  if (!blob || typeof blob !== "object") return;
  const take = (k) => {
    if (typeof blob[k] !== "string") return false;
    try { localStorage.setItem(k, blob[k]); } catch { /* storage blocked */ }
    return true;
  };
  for (const key of SYNCED) take(`serai.v3.${key}`);
  if (!take(KEY) && typeof blob[V2_TERM] === "string") {
    let v2 = null;
    try { v2 = JSON.parse(blob[V2_TERM]); } catch { v2 = null; }
    if (v2 && typeof v2 === "object") {
      const p = {};
      if (typeof v2.font === "string") p.font = v2.font;
      if (Number.isFinite(v2.size)) p.fontSize = Math.min(28, Math.max(9, Math.round(v2.size)));
      if (Number.isFinite(v2.scrollback)) p.scrollback = Math.min(100000, Math.max(1000, Math.round(v2.scrollback)));
      if (["block", "bar", "underline"].includes(v2.cursorStyle)) p.cursorStyle = v2.cursorStyle;
      if (typeof v2.cursorBlink === "boolean") p.cursorBlink = v2.cursorBlink;
      if (typeof v2.theme === "string") p.theme = v2.theme;     // pane.js falls back if unknown
      cache = null;
      setPrefs(p);                                         // also writes the v3 key back
      return;
    }
  }
  cache = null;                                            // re-read what we just took
}
