// The ★ Favorites panel: the starred subset of the saved profiles, in the
// viewer's own order, one click from a pane. A favorite is a launcher, not a
// live-session bookmark -- closing the session keeps the row (server rule in
// /api/kill), and Forget (which drops the profile) is the only way out.
//
// launchTarget() lives here because this panel is its main caller; the jump
// palette imports it so a saved row restores exactly like a favorite would.

import { h, fill, ago } from "../core/dom.js";
import { post, key } from "../core/api.js";
import * as store from "../core/state.js";
import * as dialog from "../core/dialog.js";
import { openMenu } from "../core/menu.js";
import { remember } from "../core/prefs.js";
import * as topbar from "../views/topbar.js";
import * as panes from "../views/panes.js";
import { kindInfo, shortPath } from "../term/pane.js";
import { openEdit } from "./profile.js";

const NOTE = "Favorites are saved profiles with a star. Closing a running favorite "
  + "keeps it here; use Forget to remove it.";

/** Re-create descriptor for a saved profile: the same resume rule the profile
 *  carries, minus what can't mean anything -- "new" asks for no conversation,
 *  and a shell has none to come back to. */
export function launchTarget(profile) {
  const kind = profile.kind || "shell";
  const launch = profile.launch || "resume";
  return {
    host: profile.host, name: profile.name, kind, label: profile.label,
    path: profile.path || "",
    resume: launch === "new" || !kindInfo(kind).agent ? "" : launch,
    args: profile.args || "",
    tags: (profile.tags || []).join(","),
  };
}

const button = { btn: null };

export function mount() {
  button.btn = h("button.btn", { type: "button", title: "Favorites", onclick: () => openFavorites() },
    h("span", { style: { color: "var(--st-blocked)" } }, "★"),
    h("span.n", { style: { color: "var(--text-faint)" } }));
  topbar.addButton(20, button.btn);
  store.subscribe(["saved"], renderCount);
  renderCount();
}

const favorites = () => (store.get().saved || []).filter((r) => r.favorite);

function renderCount() {
  if (button.btn) button.btn.querySelector(".n").textContent = String(favorites().length);
}

/** The viewer's remembered order first, anything newer appended alphabetically. */
function ordered() {
  const byKey = new Map(favorites().map((f) => [key(f.host, f.name), f]));
  const out = [];
  for (const k of remember("favOrder") || []) {
    if (byKey.has(k)) { out.push(byKey.get(k)); byKey.delete(k); }
  }
  return out.concat([...byKey.values()].sort((a, b) =>
    (a.label || a.name).localeCompare(b.label || b.name) || a.name.localeCompare(b.name)));
}

export function openFavorites() {
  const body = h("div");
  let dragging = false;                        // a repaint mid-drag would drop the row
  let off = () => {};
  let openedPane = false;                      // Attach/Restore: the caret belongs there
  const dlg = dialog.open({ title: "★ Favorites", wide: true, cls: "favs", body, actions: [],
    onClose: () => off() });
  off = store.subscribe(["saved", "sessions"], () => { if (!dragging) render(); });
  dlg.done.then(() => { if (openedPane) { const p = panes.focusedPane(); if (p) p.focus(); } });
  render();
  return dlg;

  function render() {
    if (!dlg.el.isConnected) return;
    const favs = ordered();
    fill(body, favs.length ? table(favs) : emptyState(), h("div.note", NOTE));
  }

  function table(favs) {
    return h("table.table",
      h("thead", h("tr", h("th", ""), h("th", "Session"), h("th", "Host"), h("th", "Directory"),
        h("th", "Launch"), h("th", "Status"), h("th", ""))),
      h("tbody", favs.map(row)));
  }

  function emptyState() {
    return h("div.empty", h("div.big", "★"), h("h3", "No favorites yet"),
      h("p", "Star a session in its row menu, or tick Save to favorites in the New session form."));
  }

  function row(p) {
    const live = store.session(p.host, p.name);
    const info = kindInfo(p.kind);
    const dot = live ? store.STATE_CLASS[live.state] || "idle" : "idle";
    const age = live ? ago(live.age) : "";
    const grip = h("td.grip", { title: "Drag to reorder" }, "⋮⋮");
    const tr = h("tr", { dataset: { key: key(p.host, p.name) } },
      grip,
      h("td.t-name", h(`span.dot.${dot}`), h(`span.kind.${info.chip}`, info.chip.toUpperCase()),
        (live && live.label) || p.label || p.name),
      h("td", p.host),
      h("td.dim", shortPath(p.path || (live && live.dir) || "")),
      h("td", p.launch || ""),
      live ? h("td", h("span.up", age ? `${live.state} · ${age}` : live.state))
        : h("td", h("span.dn", "not running")),
      h("td.acts",
        h("button.btn.primary", { type: "button", onclick: () => launchIt(p, live) },
          live ? "Attach" : "Restore"),
        live ? h("button.btn", { type: "button", onclick: () => openEdit(live) }, "Edit") : null,
        h("button.btn.ghost", { type: "button", title: "More", "aria-label": "More",
          onclick: (e) => rowMenu(e.currentTarget, p) }, "⋮")));
    grip.addEventListener("pointerdown", (e) => startDrag(e, tr));
    return tr;
  }

  function launchIt(p, live) {
    openedPane = true;
    panes.open(live ? { host: p.host, name: p.name, kind: live.kind, label: live.label }
      : launchTarget(p));
    dlg.close();
  }

  function rowMenu(anchor, p) {
    openMenu(anchor, [
      { label: "Remove from favorites", icon: "☆", onClick: () => mutate(
        () => post("/api/favorite", { host: p.host, name: p.name, favorite: false }),
        "removed from favorites") },
      { label: "Forget…", icon: "✕", danger: true, onClick: () => forget(p) },
    ]);
  }

  async function forget(p) {
    const ok = await dialog.confirm({
      title: `Forget ${p.label || p.name}?`,
      message: "Serai stops remembering this profile and takes it out of Favorites. "
        + "A session that is still running keeps running, but loses its star.",
      confirmLabel: "Forget profile", danger: true,
    });
    if (!ok) return;
    await mutate(() => post("/api/profiles/forget", { host: p.host, name: p.name }),
      "profile forgotten");
  }

  async function mutate(fn, msg) {
    try {
      await fn();
      dialog.toast(msg);
    } catch (err) {
      dialog.toast(err.message, "error", 5000);
      return;
    }
    window.dispatchEvent(new CustomEvent("serai:poll", { detail: { saved: true } }));
    render();
  }

  function startDrag(e, tr) {
    if (e.pointerType === "mouse" && e.button !== 0) return;
    e.preventDefault();
    const tbody = tr.parentElement;
    dragging = true;
    const grip = tr.querySelector(".grip");
    try { grip.setPointerCapture(e.pointerId); } catch { /* stale row */ }
    const move = (ev) => {
      const under = document.elementFromPoint(ev.clientX, ev.clientY);
      const target = under && under.closest("tr");
      if (!target || target === tr || target.parentElement !== tbody) return;
      const r = target.getBoundingClientRect();
      tbody.insertBefore(tr, ev.clientY > r.top + r.height / 2 ? target.nextElementSibling : target);
    };
    const up = () => {
      grip.removeEventListener("pointermove", move);
      grip.removeEventListener("pointerup", up);
      grip.removeEventListener("pointercancel", up);
      dragging = false;
      remember("favOrder", [...tbody.querySelectorAll("tr")].map((x) => x.dataset.key));
      render();
    };
    grip.addEventListener("pointermove", move);
    grip.addEventListener("pointerup", up);
    grip.addEventListener("pointercancel", up);
  }
}
