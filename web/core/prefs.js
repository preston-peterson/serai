// Per-viewer preferences (terminal look, copy/paste options, layout bits).
// Stored in this browser; Settings edits them. A failed storage read (private
// window, blocked site data) just means defaults.

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
  window.dispatchEvent(new CustomEvent("serai:prefs", { detail: patch }));
  return cache;
}

/** Small per-viewer UI memory (collapsed groups, open panes) under its own key. */
export function remember(key, value) {
  try {
    if (value === undefined) return JSON.parse(localStorage.getItem(`serai.v3.${key}`) || "null");
    localStorage.setItem(`serai.v3.${key}`, JSON.stringify(value));
  } catch { /* ignore */ }
  return value;
}
