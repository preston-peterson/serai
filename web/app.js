// Boot: sign in, load the kinds table, start polling, mount the views.
// Everything else lives in modules: core/ (state, api, dom, dialog, menu,
// prefs), term/ (the terminal pane and the copy/paste model), views/ (top bar,
// rail, panes) and features/ (one module per surface).

import { get } from "./core/api.js";
import * as store from "./core/state.js";
import { isOpen as dialogOpen } from "./core/dialog.js";
import { requireAuth } from "./features/login.js";
import * as account from "./features/account.js";
import * as board from "./features/board.js";
import * as files from "./features/files.js";
import * as profile from "./features/profile.js";
import * as settings from "./features/settings.js";
import * as palette from "./features/palette.js";
import * as favorites from "./features/favorites.js";
import * as topbar from "./views/topbar.js";
import * as rail from "./views/rail.js";
import * as panes from "./views/panes.js";
import * as phone from "./mobile/phone.js";

const POLL_MS = 5000;
const SAVED_EVERY = 3;          // saved profiles every third poll (and on demand)
let polls = 0;
let timer = 0;

async function poll({ saved = false } = {}) {
  clearTimeout(timer);
  try {
    const sessions = await get("/api/sessions");
    const patch = { sessions, connected: true };
    if (saved || polls % SAVED_EVERY === 0) patch.saved = await get("/api/sessions/saved");
    store.set(patch);
  } catch (err) {
    if (err.status !== 401) store.set({ connected: false });
  }
  polls++;
  timer = setTimeout(poll, document.hidden ? POLL_MS * 3 : POLL_MS);
}

window.addEventListener("serai:poll", (e) => poll({ saved: !!(e.detail && e.detail.saved) }));
window.addEventListener("serai:version", (e) => store.set({ version: e.detail }));
document.addEventListener("visibilitychange", () => { if (!document.hidden) poll(); });

// Global keys: Ctrl+Shift+1/2 focus a pane. (Palette, + New and the rest
// arrive with their features.)
document.addEventListener("keydown", (e) => {
  if (dialogOpen()) return;
  if (e.ctrlKey && e.shiftKey && (e.code === "Digit1" || e.code === "Digit2")) {
    e.preventDefault();
    panes.focusIndex(e.code === "Digit1" ? 0 : 1);
  }
});

async function boot() {
  await requireAuth();
  try { store.set({ kinds: await get("/api/kinds") }); } catch { /* the rail falls back to shell chips */ }
  topbar.mount(document.getElementById("topbar"));
  rail.mount(document.getElementById("rail"));
  account.mount();                          // the avatar menu's "Account…" entry
  settings.mount();                          // the ⚙ button, the saved density, the update dot
  profile.mount();                           // adds its own top-bar button and rail hook
  palette.mount();                           // the ⌕ pill, Ctrl+K, the double-Shift
  favorites.mount();                         // the ★ button and its panel
  panes.mount(document.getElementById("panes"));
  files.mount(document.getElementById("drawer"));
  board.mount(document.getElementById("board"));
  phone.mount();                             // the phone arrangement (mockup 08), when narrow
  document.getElementById("app").hidden = false;
  await poll({ saved: true });
}

boot();
