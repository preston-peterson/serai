// Account: ONE dialog -- Profile for everyone, Users and Network for admins.
// Reached from the avatar menu, and the only place a person changes their own
// password. The footer is the same on every tab (Log out left, Close right)
// because each panel ends itself -- Change password, Add, Apply & restart --
// and a dialog-wide Save would promise something the panel already did.
//
// The two admin tabs are not rendered at all for anyone else. Their endpoints
// 403 for a non-admin, and a tab that opens an empty panel reads as broken
// rather than as not-yours. (Reminder, since it comes up: serai runs as one OS
// user, so an account is organisational separation, not a sandbox.)

import { h, fill } from "../core/dom.js";
import { api, get, post } from "../core/api.js";
import * as store from "../core/state.js";
import * as dialog from "../core/dialog.js";
import * as topbar from "../views/topbar.js";
import { logout } from "./login.js";

const PW_MIN = 8;
// No I/l/O/0/1: a reset password is read off one screen and typed on a phone.
const ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";
const TAB_LABEL = { profile: "Profile", users: "Users", network: "Network" };

const isAdmin = () => !!(store.get().auth || {}).admin;
const userName = () => (store.get().auth || {}).user || "";

export function mount() {
  topbar.addAccountItem(() => ({ label: "Account…", icon: "☺", onClick: () => openAccount() }));
}

export function openAccount(tab = "profile") {
  const admin = isAdmin();
  const panels = { profile: profilePanel() };
  if (admin) {
    panels.users = usersPanel();
    panels.network = networkPanel();
  }
  const tabs = h("div.tabs", { role: "tablist" }, ...Object.keys(panels).map((name) => h("div.tab", {
    dataset: { tab: name }, role: "tab", onclick: () => show(name),
  }, TAB_LABEL[name], admin && name !== "profile" ? h("span.badge", "admin") : null)));
  const show = (name) => {
    for (const [k, el] of Object.entries(panels)) el.hidden = k !== name;
    for (const t of tabs.children) {
      const on = t.dataset.tab === name;
      t.classList.toggle("active", on);
      t.setAttribute("aria-selected", String(on));
    }
  };
  show(panels[tab] ? tab : "profile");
  return dialog.open({
    title: "Account",
    wide: true,
    head: tabs,
    body: Object.values(panels),
    actions: [
      { label: "Log out", left: true, onClick: logout },
      { label: "Close" },
    ],
  });
}

// -------------------------------------------------------------------- profile

function profilePanel() {
  const user = userName();
  const admin = isAdmin();
  const cur = pwField("current-password");
  const next = pwField("new-password");
  const again = pwField("new-password");

  /** Changing your own password needs the current one (server-side too): a cookie
   *  left in an unattended browser is not enough to take the account over. */
  async function change() {
    if (next.value !== again.value) return refuse("The new passwords don't match", next);
    if (next.value.length < PW_MIN) return refuse("At least 8 characters", next);
    try {
      await post(`/api/users/${encodeURIComponent(user)}/password`, { current: cur.value, password: next.value });
    } catch (err) {
      return refuse(err.status === 403 ? "Your current password is not right" : err.message, cur);
    }
    cur.value = next.value = again.value = "";
    dialog.toast("password changed");
    return false;
  }

  return h("div.p",
    h("div.field", h("label", "Signed in as"), h("div.inline",
      h("div.avatar", (user || "?")[0].toUpperCase()),
      h("b", user || "?"),
      h(`span.kind.${admin ? "cc" : "sh"}`, admin ? "ADMIN" : "USER"))),
    h("div.field", h("label", "Current password"), cur),
    h("div.field", h("label", "New password"), next),
    h("div.field", h("label", "Confirm"), again),
    h("div.field", h("label", ""), h("button.btn", { type: "button", onclick: change }, "Change password")));
}

/** Password inputs are 16px whatever .input says: below that a phone zooms the
 *  field on focus and the person loses the row they are typing in. */
function pwField(autocomplete, placeholder = "") {
  return h("input.input", { type: "password", autocomplete, placeholder, style: { fontSize: "16px" } });
}

function refuse(message, focusEl) {
  dialog.toast(message, "error", 5000);
  if (focusEl) focusEl.focus();
  return false;
}

// ---------------------------------------------------------------------- users

function usersPanel() {
  const list = h("div");
  let me = "";

  const uname = h("input.input", { placeholder: "username", autocomplete: "off", spellcheck: false });
  const upw = pwField("new-password", "password");
  let adminWanted = false;
  const adminBox = checkControl(false, "admin", (on) => { adminWanted = on; });

  async function redraw() {
    let r;
    try { r = await get("/api/users"); } catch (err) { return void dialog.toast(err.message, "error", 5000); }
    me = r.me || "";
    fill(list, h("table.table",
      h("thead", h("tr", h("th", "User"), h("th", "Role"), h("th", ""))),
      h("tbody", ...(r.users || []).map(userRow))));
  }

  function userRow(u) {
    const isMe = u.username === me;
    return h("tr",
      h("td.n", isMe ? `${u.username} · you` : u.username),
      h("td", h(`span.kind.${u.admin ? "cc" : "sh"}`, u.admin ? "ADMIN" : "USER")),
      h("td", h("div.inline",
        // Your own row has neither: serai never leaves itself without an admin,
        // and changing your own password needs the current one (Profile tab) --
        // a button that can only ever fail is noise.
        isMe ? null : h("button.btn.sm", { type: "button", onclick: () => resetPassword(u) }, "Reset password…"),
        isMe ? null : h("button.btn.sm.danger", { type: "button", onclick: () => removeUser(u) }, "Remove…"))));
  }

  async function addUser() {
    const username = uname.value.trim();
    if (!username) { refuse("A username is needed", uname); return; }
    try {
      await post("/api/users", { username, password: upw.value, admin: adminWanted });
    } catch (err) { return void dialog.toast(err.message, "error", 5000); }
    uname.value = upw.value = "";
    adminBox.set(false);
    redraw();
  }

  /** A reset generates the password here and shows it exactly once: the server
   *  stores a hash and could never read this one back, so if this dialog closes
   *  with it un-copied the only way in is another reset. */
  async function resetPassword(u) {
    const pw = genPassword();
    try {
      await post(`/api/users/${encodeURIComponent(u.username)}/password`, { password: pw });
    } catch (err) { return void dialog.toast(err.message, "error", 5000); }
    dialog.open({
      title: `Reset password for ${u.username}`,
      body: [
        h("div.cmdline", pw),
        h("div.note", `Shown once. Give it to ${u.username}; they can change it in Profile.`),
      ],
      actions: [
        { label: "Copy", close: false, onClick: async () => {
          try { await navigator.clipboard.writeText(pw); dialog.toast("copied"); }
          catch (err) { dialog.toast(err.message, "error", 5000); }
          return false;
        } },
        { label: "Done", kind: "primary" },
      ],
    });
  }

  async function removeUser(u) {
    const ok = await dialog.confirm({
      title: `Remove ${u.username}?`,
      message: "They can no longer sign in. Sessions they started keep running.",
      confirmLabel: "Remove", danger: true,
    });
    if (!ok) return;
    try {
      await api(`/api/users/${encodeURIComponent(u.username)}`, { method: "DELETE" });
    } catch (err) { return void dialog.toast(err.message, "error", 5000); }
    redraw();
  }

  redraw();
  return h("div.p", { hidden: true }, list,
    h("div.addrow", uname, upw, adminBox, h("button.btn.primary", { type: "button", onclick: addUser }, "Add")));
}

/** 16 characters in four readable groups, from the CSPRNG. Bytes at or above
 *  `limit` are discarded so every character lands in the same number of byte
 *  buckets: a plain modulo over this 57-char alphabet would give 28 of them five
 *  buckets and the other 29 four, i.e. a quarter more likely. */
function genPassword() {
  const limit = 256 - (256 % ALPHABET.length);
  const chars = [];
  while (chars.length < 16) {
    const bytes = new Uint8Array((16 - chars.length) * 2);
    crypto.getRandomValues(bytes);
    for (const b of bytes) if (b < limit) chars.push(ALPHABET[b % ALPHABET.length]);
  }
  return [0, 4, 8, 12].map((i) => chars.slice(i, i + 4).join("")).join("-");
}

// -------------------------------------------------------------------- network

function networkPanel() {
  const host = h("input.input", { placeholder: "127.0.0.1" });
  const names = h("input.input", { placeholder: "serai.lan, 192.0.2.10" });
  const applyBtn = h("button.btn.primary", { type: "button", onclick: apply }, "Apply & restart");

  async function apply() {
    if (applyBtn.disabled) return;                  // it restarts serai; send it once
    applyBtn.disabled = true;
    try {
      const r = (await post("/api/network", { host: host.value.trim(), hostnames: names.value })) || {};
      names.value = (r.hostnames || []).join(",");  // the names the server accepted
      const restart = r.restart || {};
      if (restart.ok) dialog.toast("network applied -- serai is restarting", "ok", 5000);
      else dialog.toast(restart.reason || "restart serai to apply", "warn", 8000);
    } catch (err) {
      dialog.toast(err.message, "error", 5000);
    } finally {
      applyBtn.disabled = false;
    }
  }

  get("/api/network").then((r) => {                // seeded from what is in effect
    host.value = r.host || "";
    names.value = r.hostnames || "";
  }).catch((err) => dialog.toast(err.message, "error", 5000));

  return h("div.p", { hidden: true },
    h("div.field", h("label", "Listen address"), host),
    h("div.field", h("label", "Certificate names"), names,
      h("div.help", "names and IPs the self-signed certificate covers")),
    h("div.note.warn", "Apply restarts serai. Terminals reconnect; tmux sessions are untouched."),
    h("div.field", h("label", ""), applyBtn));
}

// ------------------------------------------------------------------ controls

/** The .check box, same as the session form's: the box is CSS, the <i> is its mark. */
function checkControl(on, text, onChange) {
  const tick = h("i", on ? "✓" : "");
  const el = h(`button.check${on ? ".on" : ""}`, { type: "button", role: "checkbox", "aria-checked": String(!!on) },
    tick, text);
  const paint = (next) => {
    el.classList.toggle("on", next);
    el.setAttribute("aria-checked", String(next));
    tick.textContent = next ? "✓" : "";
    onChange(next);
  };
  el.addEventListener("click", () => paint(!el.classList.contains("on")));
  el.set = (next) => { if (next !== el.classList.contains("on")) paint(next); };
  return el;
}
