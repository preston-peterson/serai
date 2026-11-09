// The session form: ONE dialog for starting a session (the top bar's + New) and
// for changing one (the rail's "Edit session…"). Same fields in the same order
// with the same words; the two endings are what differ. + New finishes by
// attaching, Edit finishes by saving -- and by restarting when what changed can
// only land on the next create, because tmux runs its command on create alone.
//
// The profile (host/kind/label/dir/args/tags/launch/favorite) is the durable half
// of the profile-vs-instance split; the tmux session is the instance. + New makes
// both, and the two settings that live in the profile and nowhere in tmux --
// launch and favorite -- can only be posted once the profile exists, which is
// after the next poll has snapshotted the session. So createProfile waits for
// that rather than firing the posts into the air: set_launch and set_favorite are
// no-ops for a name that isn't snapshotted yet, and the favorite you ticked while
// creating would silently never stick.

import { h, fill } from "../core/dom.js";
import { get, post, key } from "../core/api.js";
import * as store from "../core/state.js";
import * as dialog from "../core/dialog.js";
import * as topbar from "../views/topbar.js";
import * as rail from "../views/rail.js";
import * as panes from "../views/panes.js";
import { kindInfo } from "../term/pane.js";

const UNSAFE = /[^A-Za-z0-9_.-]/g;        // the charset serai's session_name() allows
const LAUNCH_MODES = [["new", "new"], ["continue", "continue last"], ["resume", "resume picker"]];
const WARN = "Directory and args take effect when the session is created. Save & restart "
  + "kills and recreates it; anything running is lost.";

let hosts = null;                          // ssh aliases, fetched once (a promise)

export function mount() {
  topbar.addButton(30, h("button.btn.primary", {
    type: "button", title: "New session (Ctrl+Shift+N)", onclick: () => openNew(),
  }, "+ New"));
  rail.setHook("edit", openEdit);
  document.addEventListener("keydown", (e) => {
    if (e.ctrlKey && e.shiftKey && e.code === "KeyN" && !dialog.isOpen()) {
      e.preventDefault();                  // the browser's own new-window shortcut
      openNew();
    }
  });
  hostList();
}

/** Start a session. prefill: {host, kind, label, path, args} for a caller that
 *  already knows part of the answer; a matching saved profile fills in the rest. */
export function openNew(prefill = {}) {
  return form({ prefill });
}

/** Edit a live session's profile -- and restart it if that is what the change takes. */
export function openEdit(session) {
  return form({ session });
}

// ------------------------------------------------------------------- one form

function form({ session = null, prefill = {} }) {
  const edit = !!session;
  // The profile behind this name: Edit needs it for what the live session doesn't
  // carry (launch, favorite); + New uses it to complete itself once the name is typed.
  const prof = edit ? savedProfile(session.host, session.name, false)
    : savedProfile(prefill.host || "local", nameOf(prefill.kind || "shell", prefill.label || ""));
  const start = {
    host: edit ? session.host : (prefill.host || "local"),
    kind: (edit ? session.kind : prefill.kind) || "shell",
    label: edit ? (session.label || session.name) : (prefill.label || ""),
    dir: edit ? dirOf(session, prof) : (prefill.path || (prof && prof.path) || ""),
    args: edit ? argsOf(session, prof) : (prefill.args || ""),
  };
  const launch0 = (edit && prof && prof.launch) || (kindInfo(start.kind).agent ? "resume" : "new");
  const st = {
    host: start.host,
    kind: start.kind,
    tags: [...((edit ? session.tags : (prof && prof.tags)) || [])],
    launch: launch0,
    favorite: edit ? !!(session.favorite || (prof && prof.favorite)) : false,
    matched: "",                           // the profile + New has filled itself from
  };
  // Two baselines, because they answer different questions. `was` is what the
  // running instance was created with, and it is what the warn note is about: a
  // directory you saved but never restarted into is still not in effect. `base` is
  // what the server has agreed to so far, so a Save that half-failed and gets
  // retried resends only what is still pending -- and from the name the session
  // actually has now, not the one a successful rename just replaced.
  const was = { dir: start.dir, args: start.args };
  const base = {
    name: session && session.name, kind: start.kind, label: start.label,
    tags: [...st.tags], dir: start.dir, args: start.args, launch: launch0, favorite: st.favorite,
  };

  const hostSel = h("select.select", {
    disabled: edit,                        // an Edit acts on this session's own host:
                                           // switching it here would retarget every call
    onchange: () => { st.host = hostSel.value; sync(); },
  });
  const kindSeg = seg(() => kindList().map((k) => [k.kind, k.kind]), (kind) => {
    const wasAgent = kindInfo(st.kind).agent;
    st.kind = kind;
    // Coming from a shell the launch row was hidden, so whatever it held was never
    // a choice; an agent's default is the picker, as it is in effective_launch.
    if (!wasAgent && kindInfo(kind).agent) launchSeg.pick("resume");
    sync();
  }, start.kind);
  const labelInput = h("input.input", { value: start.label, placeholder: "what this session is for", autofocus: true });
  const labelHelp = h("div.help");
  const dirInput = h("input.input", { value: start.dir, placeholder: "~/git/…" });
  const argsInput = h("input.input", { value: start.args, placeholder: "--chrome --model opus", spellcheck: false });
  const launchSeg = seg(LAUNCH_MODES, (v) => { st.launch = v; }, launch0);
  const tags = tagsControl(st);
  const favBtn = checkControl(st.favorite, "Save to favorites · one-click restore from the ★ menu",
    (on) => { st.favorite = on; });
  const note = h("div.note", { hidden: true });
  const warn = h("div.note.warn", { hidden: true }, WARN);
  let busy = false;

  /** The ssh aliases plus this form's own host, which stays listed even if the
   *  list is cold or missing it -- the select must never read as a different host
   *  than the one the form will act on. Also the form's first paint, so it runs
   *  only once the rows exist (sync reaches them through their own DOM). */
  function setHosts(list) {
    const names = ["local", ...list];
    if (!names.includes(st.host)) names.splice(1, 0, st.host);
    fill(hostSel, ...names.map((a) => h("option", { value: a }, a)));
    hostSel.value = st.host;
    sync();
  }

  for (const el of [labelInput, dirInput, argsInput]) el.addEventListener("input", sync);

  const dlg = dialog.open({
    cls: "form",                           // the field grid wants the mockup's width
    title: edit ? `Edit session · ${start.label}` : "New session",
    body: (edit ? [warn] : [note]).concat(
      row("Host", hostSel),
      row("Kind", kindSeg),
      row("Label", labelInput, labelHelp),
      row("Directory", dirInput, h("div.help", "where the terminal and the file drawer open")),
      row("Launch", launchSeg, h("div.help", "how the agent starts; --resume in Args overrides this")),
      row("Args", argsInput),
      row("Tags", tags.el),
      row("Favorite", favBtn)),
    actions: edit ? [
      { label: "Forget profile…", kind: "danger", left: true, close: false, onClick: forgetProfile },
      { label: "Cancel" },
      { label: "Save", close: false, onClick: () => save(false) },
      { label: "Save & restart", kind: "primary", close: false, onClick: () => save(true) },
    ] : [
      { label: "Cancel" },
      { label: "Create & attach", kind: "primary", onClick: create },
    ],
  });
  // A dialog action's `kind` is one class, while the mockup's Forget is danger + ghost.
  if (edit) dlg.el.querySelector(".dialog-foot .btn").classList.add("ghost");
  setHosts([]);                            // its own host now; the aliases land a beat later
                                           // rather than holding the form open for a fetch
  hostList().then((list) => { if (hostSel.isConnected) setHosts(list); });
  labelInput.focus();                        // open()'s own first-focus lands on the Host select
  return dlg;

  // -- one pass over everything the fields say -------------------------------

  function sync() {
    const name = nameOf(st.kind, labelInput.value);
    const agentOn = kindInfo(st.kind).agent;
    fill(labelHelp, "becomes ", h("b", { class: "mono" }, name || "…"));
    dirInput.placeholder = `~/git/${labelInput.value.trim() || "…"}`;
    rowShown(launchSeg, agentOn);
    rowShown(argsInput, agentOn);
    if (edit) warn.hidden = dirInput.value === was.dir && argsInput.value === was.args;
    else matchProfile(name);
  }

  /** A name typed over a saved profile that isn't live: say so, and use it. Only
   *  when the *match* changes -- refilling on every keystroke would undo typing. */
  function matchProfile(name) {
    const p = labelInput.value.trim() ? savedProfile(st.host, name) : null;
    const k = p ? key(p.host, p.name) : "";
    note.hidden = !p;
    if (!p || st.matched === k) return;
    st.matched = k;
    const launch = p.launch || "resume";
    dirInput.value = p.path || "";
    argsInput.value = p.args || "";
    st.tags = [...(p.tags || [])];
    tags.redraw();
    launchSeg.pick(launch);                // the seg repaints and notifies like a click
    st.launch = launch;
    fill(note, "Matches saved profile ", h("b", p.label || p.name),
      ` -- directory, args and tags filled in; launch set to ${launch}.`);
  }

  function create() {
    if (!needLabel()) return false;
    const label = labelInput.value.trim();
    const target = {
      host: st.host, kind: st.kind, label, name: nameOf(st.kind, label), path: dirInput.value,
      resume: resumeOf(st.kind, st.launch), args: argsInput.value, tags: st.tags.join(","),
    };
    panes.open(target);
    focusPaneAfterClose();
    createProfile(target);                 // in the background; the pane is the answer
    return true;
  }

  /** dialog.open hands focus back to whatever opened it. After a create or a
   *  restart the caret belongs in the terminal that just came up instead --
   *  done.then runs after that restore, so this one wins. */
  function focusPaneAfterClose() {
    dlg.done.then(() => { const p = panes.focusedPane(); if (p) p.focus(); });
  }

  /** What a create cannot carry in its own command line: the two profile-only
   *  choices. A shell has neither a launch rule nor (unless ticked) a favorite, so
   *  it has nothing to record and does not wait for anything. */
  function profilePosts({ host, name }) {
    const posts = [];
    if (kindInfo(st.kind).agent) posts.push(["/api/launch", { host, name, launch: st.launch }]);
    if (st.favorite) posts.push(["/api/favorite", { host, name, favorite: true }]);
    return posts;
  }

  /** The profile only exists once a poll has snapshotted the session it describes,
   *  so follow it -- 10 tries a second apart, the poll's own cadence -- and only
   *  then post what the create could not carry. */
  async function createProfile(target) {
    const posts = profilePosts(target);
    if (!posts.length) return;
    const { host, name } = target;
    let err = "";
    try {
      let found = false;
      for (let i = 0; i < 10 && !found; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        found = (await get("/api/sessions/saved")).some((r) => key(r.host, r.name) === key(host, name));
      }
      if (!found) {
        dialog.toast(`${name} never appeared in the saved profiles -- launch and favorite not set.`, "warn", 5000);
        return;
      }
      for (const [path, body] of posts) {
        try { await post(path, body); } catch (e) { err = e.message; }
      }
    } catch (e) {
      err = e.message;
    }
    if (err) dialog.toast(err, "error", 5000);
    pollSaved();
  }

  async function save(restart) {
    if (busy) return false;                // it stays open while it runs; no double-send
    busy = true;
    try {
      if (!needLabel()) return false;
      const name = await applyChanges();
      if (restart) {
        must(await post("/api/sessions/restart", { host: st.host, name }), "restart it");
        panes.open({
          host: st.host, kind: st.kind, label: labelInput.value.trim(), name,
          path: dirInput.value, resume: resumeOf(st.kind, st.launch),
          args: argsInput.value, fresh: true,
        });
        focusPaneAfterClose();
      }
      dialog.toast(restart ? "restarted" : "saved");
      pollSaved();
      dlg.close(true);
    } catch (e) {
      dialog.toast(e.message, "error", 5000);   // the form stays open; the edit isn't lost
    } finally {
      busy = false;
    }
    return false;
  }

  /** Only what is still pending, in the order the fields are asked for, each one
   *  moving the baseline forward as it lands. The rename goes first: tmux moves the
   *  session out from under the old name, so every call after it -- here or on a
   *  retry after a failure -- has to use the name the server answered with. */
  async function applyChanges() {
    const label = labelInput.value.trim();
    if (base.kind !== st.kind || base.label !== label) {
      const r = must(await post("/api/rename",
        { host: st.host, name: base.name, kind: st.kind, label }), "rename it");
      base.name = r.name;
      base.kind = st.kind;
      base.label = label;
    }
    if (st.tags.join(",") !== base.tags.join(",")) {
      must(await post("/api/tags", { host: st.host, name: base.name, tags: st.tags }), "set its tags");
      base.tags = [...st.tags];
    }
    if (dirInput.value !== base.dir) {
      must(await post("/api/dir", { host: st.host, name: base.name, path: dirInput.value }), "set its directory");
      base.dir = dirInput.value;
    }
    if (argsInput.value !== base.args) {
      must(await post("/api/args", { host: st.host, name: base.name, args: argsInput.value }), "set its args");
      base.args = argsInput.value;
    }
    if (kindInfo(st.kind).agent && st.launch !== base.launch) {
      must(await post("/api/launch", { host: st.host, name: base.name, launch: st.launch }), "set its launch rule");
      base.launch = st.launch;
    }
    if (st.favorite !== base.favorite) {
      must(await post("/api/favorite", { host: st.host, name: base.name, favorite: st.favorite }), "set its favorite");
      base.favorite = st.favorite;
    }
    return base.name;
  }

  /** The other meaning of "gone": drop the profile, leave the session running. */
  async function forgetProfile() {
    const ok = await dialog.confirm({
      title: `Forget ${start.label}?`,
      message: "Serai stops remembering this profile: its directory, args, tags, launch rule "
        + "and favorite. A session that is still running is remembered again from itself on the "
        + "next poll, so what this really drops is the favorite and the launch rule.",
      confirmLabel: "Forget profile",
      danger: true,
    });
    if (!ok) return false;
    try {
      await post("/api/profiles/forget", { host: st.host, name: base.name });
      dialog.toast("profile forgotten");
      pollSaved();
    } catch (e) {
      dialog.toast(e.message, "error", 5000);
    }
    return false;                          // the form stays open either way
  }

  function needLabel() {
    if (labelInput.value.trim()) return true;
    dialog.toast("A session needs a label -- it becomes the session name.", "error", 5000);
    labelInput.focus();
    return false;
  }
}

// ------------------------------------------------------------------- controls

function row(label, ...controls) {
  return h("div.field", h("label", label), ...controls);
}

/** A field the kind doesn't use is gone, not emptied: a shell has no launch rule
 *  and no agent to pass args to, and an empty row would only invite typing. */
function rowShown(control, on) {
  control.closest(".field").hidden = !on;
}

/** A button group where a radio set would be: few choices, all of them always
 *  visible, one always on. options is [[value, label]] or a getter for it (the
 *  kinds come from a table that arrives after boot). `pick` drives it from
 *  outside -- a saved profile deciding the launch rule -- by clicking the matching
 *  button, so an outside change lands through the same path a click would. */
function seg(options, onPick, value) {
  const el = h("div.seg");
  const choices = typeof options === "function" ? options : () => options;
  const redraw = () => fill(el, ...choices().map(([v, label]) => h(`button${v === value ? ".on" : ""}`, {
    type: "button", onclick: () => { value = v; redraw(); onPick(v); },
  }, label)));
  redraw();
  el.pick = (v) => {
    const b = [...el.children].find((x) => x.textContent.trim().toLowerCase().startsWith(String(v).toLowerCase()));
    if (b) b.click();
  };
  return el;
}

/** chips + an input, because tags are a list and not a comma string to keep
 *  straight. Enter or "," commits, an empty Backspace and a chip click undo. */
function tagsControl(st) {
  const el = h("div.tagsbox");
  const input = h("input", { placeholder: "add a tag, press Enter", autocomplete: "off", spellcheck: false });
  // Chips are rebuilt around the input rather than with it: moving the input out
  // of the box blurs it, and a blur commits -- so a redraw that touched it would
  // both lose the caret and re-enter itself.
  const redraw = () => {
    el.querySelectorAll(".chip").forEach((c) => c.remove());
    for (const t of st.tags) {
      el.insertBefore(h("button.chip.x", {
        type: "button", title: `Remove ${t}`,
        onclick: () => { st.tags = st.tags.filter((x) => x !== t); redraw(); },
      }, t), input);
    }
  };
  const add = () => {
    for (const raw of input.value.split(",")) {
      const t = raw.trim();
      if (t && !st.tags.includes(t)) st.tags.push(t);
    }
    input.value = "";
    redraw();
  };
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" || e.key === ",") { e.preventDefault(); add(); }
    else if (e.key === "Backspace" && !input.value && st.tags.length) { st.tags.pop(); redraw(); }
  });
  input.addEventListener("blur", add);     // typing a tag then clicking Save still counts it
  el.append(input);
  redraw();                                // the tags it opens with (Edit, a profile match)
  return { el, redraw };
}

function checkControl(on, text, onChange) {
  const tick = h("i", on ? "✓" : "");           // the box is CSS; this is only its mark
  return h(`button.check${on ? ".on" : ""}`, {
    type: "button", role: "checkbox", "aria-checked": String(!!on),
    onclick: (e) => {
      const next = !e.currentTarget.classList.contains("on");
      e.currentTarget.classList.toggle("on", next);
      e.currentTarget.setAttribute("aria-checked", String(next));
      tick.textContent = next ? "✓" : "";
      onChange(next);
    },
  }, tick, text);
}

// --------------------------------------------------------------------- lookups

/** The saved profile behind a host+name. Saved-only (the default) is the launcher
 *  question -- a name whose session is live is not one -- which is what + New
 *  asks; Edit asks with savedOnly:false, because its instance is open right here
 *  and the profile still holds the launch rule and the favorite star. */
function savedProfile(host, name, savedOnly = true) {
  if (!name) return null;
  const k = key(host, name);
  if (savedOnly && store.get().sessions.some((s) => key(s.host, s.name) === k)) return null;
  return (store.get().saved || []).find((r) => key(r.host, r.name) === k) || null;
}

const dirOf = (s, p) => s.dir || s.path || (p && p.path) || "";
const argsOf = (s, p) => s.args || (p && p.args) || "";
const nameOf = (kind, label) => kindInfo(kind).prefix + String(label || "").trim().replace(UNSAFE, "-");
/** How this form's answer starts the session: only an agent has a conversation to
 *  come back to, and "new" asks for none, so a shell never carries a resume rule --
 *  not in its command line, and not into the profile either. */
const resumeOf = (kind, launch) => (kindInfo(kind).agent && launch !== "new" ? launch : "");

function kindList() {
  const list = Array.isArray(store.get().kinds) ? store.get().kinds : [];
  // /api/kinds failing at boot leaves the table empty; a shell is still startable.
  return list.length ? list : [{ kind: "shell", agent: false, cmd: null, prefix: "shell-", resume: {} }];
}

/** ssh aliases, fetched once and kept -- so the form never waits on the network
 *  to appear, and a signed-in user's second + New already has the list. */
function hostList() {
  if (!hosts) {
    hosts = get("/api/hosts").then((r) => r.map((x) => x.alias)).catch(() => []);
  }
  return hosts;
}

function must(res, what) {
  if (!res || res.ok === false) throw new Error(`serai could not ${what}`);
  return res;
}

function pollSaved() {
  window.dispatchEvent(new CustomEvent("serai:poll", { detail: { saved: true } }));
}
