// The one client-side store. Views subscribe to the keys they draw from and
// re-render only when one of those changed; nothing else holds app state.

const state = {
  auth: null,        // /api/auth/status payload
  kinds: {},         // /api/kinds: { claude: {chip, glyph, prefix, resume}, ... }
  sessions: [],      // live sessions from the last poll
  saved: [],         // saved profiles (/api/sessions/saved)
  tag: "",           // workspace filter: "" = all tags
  connected: true,   // last poll reached the server
  version: "",
  focusedKey: "",    // key() of the session in the focused pane
};

const subs = new Set();

export function get() {
  return state;
}

/** Merge a patch; notify subscribers whose keys intersect what changed. */
export function set(patch) {
  const changed = [];
  for (const [k, v] of Object.entries(patch)) {
    if (state[k] !== v) {
      state[k] = v;
      changed.push(k);
    }
  }
  if (!changed.length) return;
  for (const s of [...subs]) {
    if (!s.keys || s.keys.some((k) => changed.includes(k))) {
      try { s.fn(state, changed); } catch (err) { console.error("[serai] subscriber failed", err); }
    }
  }
}

/** subscribe(["sessions","tag"], fn) -> unsubscribe. keys null = everything. */
export function subscribe(keys, fn) {
  const s = { keys, fn };
  subs.add(s);
  return () => subs.delete(s);
}

/** Live session by host/name, or undefined. */
export function session(host, name) {
  return state.sessions.find((s) => s.host === host && s.name === name);
}

/** Server state names -> the UI's vocabulary (and CSS dot classes). */
export const STATE_CLASS = { running: "working", needs_input: "blocked", stuck: "stuck", done: "done", idle: "idle" };
export const STATE_LABEL = { running: "working", needs_input: "needs you", stuck: "looping", done: "done", idle: "idle" };
