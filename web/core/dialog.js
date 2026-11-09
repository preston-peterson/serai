// The ONE modal system: every dialog, confirm and prompt goes through open().
// Esc and a click on the scrim close the top dialog, Tab is trapped inside it,
// and focus returns to whatever had it before. No window.confirm/prompt anywhere.

import { h } from "./dom.js";

const stack = [];

/** open({title, body, actions, wide, tabs, onClose}) -> {el, close(result)}
 *  body: Node or array of Nodes. actions: [{label, kind: "primary"|"danger"|"",
 *  onClick(dlg) -> false keeps it open, left: true puts it before the spacer}].
 *  The returned `done` promise resolves with whatever close() was given. */
export function open({ title, body, actions = [], wide = false, onClose, head } = {}) {
  const prevFocus = document.activeElement;
  let resolveDone;
  const done = new Promise((r) => { resolveDone = r; });
  const closeBtn = h("button.close", { type: "button", title: "Close (Esc)", "aria-label": "Close" }, "✕");
  const foot = h("div.dialog-foot");
  const leftActs = actions.filter((a) => a.left);
  const rightActs = actions.filter((a) => !a.left);
  const mkBtn = (a) => h(`button.btn${a.kind ? "." + a.kind : ""}`, {
    type: "button",
    disabled: a.disabled,
    onclick: async () => {
      const keep = a.onClick ? await a.onClick(dlg) : undefined;
      if (keep !== false && a.close !== false) dlg.close(a.value);
    },
  }, a.label);
  foot.append(...leftActs.map(mkBtn), h("span.spacer"), ...rightActs.map(mkBtn));
  const panel = h(`div.dialog${wide ? ".wide" : ""}`, { role: "dialog", "aria-modal": "true", "aria-label": title },
    h("div.dialog-head", h("h2", title), head || null, closeBtn),
    h("div.dialog-body", body),
    actions.length ? foot : null);
  const scrim = h("div.scrim", panel);
  scrim.addEventListener("mousedown", (e) => { if (e.target === scrim) dlg.close(undefined); });
  closeBtn.addEventListener("click", () => dlg.close(undefined));

  const dlg = {
    el: panel,
    done,
    close(result) {
      const i = stack.indexOf(dlg);
      if (i < 0) return;
      stack.splice(i, 1);
      scrim.remove();
      if (onClose) onClose(result);
      resolveDone(result);
      if (prevFocus && prevFocus.focus && document.contains(prevFocus)) prevFocus.focus();
    },
  };
  stack.push(dlg);
  document.body.append(scrim);
  const first = panel.querySelector("[autofocus], input, select, textarea, button.primary") || closeBtn;
  first.focus();
  return dlg;
}

document.addEventListener("keydown", (e) => {
  const top = stack[stack.length - 1];
  if (!top) return;
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();
    top.close(undefined);
  } else if (e.key === "Tab") {
    const f = [...top.el.querySelectorAll("button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])")]
      .filter((x) => !x.disabled && x.offsetParent !== null);
    if (!f.length) return;
    const first = f[0], last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
}, true);

export const isOpen = () => stack.length > 0;

/** confirm({title, message, confirmLabel, danger, detail}) -> Promise<boolean> */
export function confirm({ title, message, confirmLabel = "OK", danger = false, detail } = {}) {
  const dlg = open({
    title,
    body: [h("p", { style: { margin: "4px 0 10px", color: "var(--text-dim)" } }, message),
      detail ? h("div.cmdline", detail) : null],
    actions: [
      { label: "Cancel", value: false },
      { label: confirmLabel, kind: danger ? "danger" : "primary", value: true },
    ],
  });
  return dlg.done.then((v) => v === true);
}

/** prompt({title, label, value, placeholder, confirmLabel}) -> Promise<string|null> */
export function prompt({ title, label, value = "", placeholder = "", confirmLabel = "OK" } = {}) {
  const input = h("input.input", { value, placeholder, autofocus: true });
  let dlg;
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); dlg.close(input.value); } });
  dlg = open({
    title,
    body: h("div.field", h("label", label || ""), input),
    actions: [
      { label: "Cancel", value: null },
      { label: confirmLabel, kind: "primary", onClick: (d) => { d.close(input.value); return false; } },
    ],
  });
  return dlg.done.then((v) => (typeof v === "string" ? v : null));
}

// --- toasts ----------------------------------------------------------------

let toastHost = null;

/** toast(text, kind = "ok" | "warn" | "error", ms) -- reports results, never intent. */
export function toast(text, kind = "ok", ms = 2200) {
  if (!toastHost) {
    toastHost = h("div.toasts", { role: "status", "aria-live": "polite" });
    document.body.append(toastHost);
  }
  const el = h(`div.toast.${kind}`, text);
  toastHost.append(el);
  setTimeout(() => el.remove(), ms);
  return el;
}
