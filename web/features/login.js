// The sign-in / first-run cover. serai is a shell into the whole fleet, so
// nothing renders behind it until a session cookie is in hand. Any 401 (an
// API call or a websocket closing 4401) raises "serai:auth" and lands here.

import { h, fill } from "../core/dom.js";
import { get, post } from "../core/api.js";
import * as store from "../core/state.js";

let pending = null;      // one prompt at a time, however many 401s arrive
const waiters = [];

export async function status() {
  let s;
  try { s = await get("/api/auth/status"); }
  catch { s = { enabled: false, configured: true, authenticated: true, user: null, admin: true }; }
  store.set({ auth: s });
  return s;
}

/** Resolves once the viewer is signed in (immediately if auth is off). */
export async function requireAuth() {
  const s = await status();
  if (!s.enabled || s.authenticated) return s;
  if (!pending) pending = showCover(s).finally(() => { pending = null; });
  return pending;
}

window.addEventListener("serai:auth", (e) => {
  if (e.detail && e.detail.then) waiters.push(e.detail.then);
  requireAuth().then(() => { while (waiters.length) waiters.shift()(); });
});

function showCover(s) {
  const setup = !s.configured;
  const user = h("input.input", { autocomplete: "username", placeholder: "username", autofocus: true });
  const pass = h("input.input", { type: "password", autocomplete: setup ? "new-password" : "current-password", placeholder: "password" });
  const pass2 = h("input.input", { type: "password", autocomplete: "new-password", placeholder: "password again" });
  const code = h("input.input", { placeholder: "setup code from the service log", autocomplete: "one-time-code" });
  const msg = h("div.auth-msg", { role: "alert", hidden: true });
  const btn = h("button.btn.primary", { type: "submit" }, setup ? "Create admin" : "Sign in");
  const form = h("form.auth-card",
    h("div.brand", "ser", h("b", "a"), "i"),
    h("h2", setup ? "Create the first user" : "Sign in"),
    setup ? h("p.dim", "This account will be the admin. Until it exists, anyone who can reach serai could claim it.") : null,
    setup && s.setup_code_required ? h("label.auth-field", h("span", "Setup code"), code) : null,
    h("label.auth-field", h("span", "Username"), user),
    h("label.auth-field", h("span", "Password"), pass),
    setup ? h("label.auth-field", h("span", "Confirm"), pass2) : null,
    msg, btn);
  const cover = h("div.auth-cover", form);
  document.body.append(cover);
  (setup && s.setup_code_required ? code : user).focus();

  return new Promise((resolve) => {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      msg.hidden = true;
      if (setup && pass.value !== pass2.value) {
        fill(msg, "The passwords don't match -- type them again.");
        msg.hidden = false;
        return;
      }
      btn.disabled = true;
      try {
        if (setup) await post("/api/setup", { token: code.value.trim(), username: user.value.trim(), password: pass.value });
        else await post("/api/login", { username: user.value.trim(), password: pass.value });
      } catch (err) {
        btn.disabled = false;
        fill(msg, err.message || "Could not sign in.");
        msg.hidden = false;
        return;
      }
      pass.value = pass2.value = code.value = "";
      cover.remove();
      resolve(await status());
    });
  });
}

export async function logout() {
  try { await post("/api/logout"); } catch { /* signed out either way */ }
  location.reload();
}
