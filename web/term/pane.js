// One terminal pane bound to one session through the control-mode bridge
// (serai/bridge.py has the wire protocol). The pane owns its xterm, its
// socket and its reconnect loop; panes.js decides where panes go.

import { h, fill } from "../core/dom.js";
import { post } from "../core/api.js";
import { toast } from "../core/dialog.js";
import * as store from "../core/state.js";
import { prefs } from "../core/prefs.js";
import { TERM_THEMES } from "./themes.js";
import { installClipboard, osc52Provider } from "./clipboard.js";

const PING_MS = 20000;          // same cadence as the server's keepalive
const STABLE_MS = 5000;         // a socket that stays up this long resets backoff
const MAX_RETRIES = 8;
const enc = new TextEncoder();

export function kindInfo(kind) {
  const k = (store.get().kinds || []).find((x) => x.kind === kind);
  return k || { kind, agent: false, cmd: null, chip: "sh", glyph: "❯", resume: {} };
}

function termOptions() {
  const p = prefs();
  return {
    fontFamily: p.font,
    fontSize: p.fontSize,
    cursorStyle: p.cursorStyle,
    cursorBlink: p.cursorBlink,
    scrollback: p.scrollback,
    theme: TERM_THEMES[p.theme] || TERM_THEMES.Slate,
    allowProposedApi: true,          // parser hooks for the query swallowing below
    macOptionClickForcesSelection: true,
    rightClickSelectsWord: false,
  };
}

export class Pane {
  /** opts: {onFocus(pane), actions(pane) -> [Node]} */
  constructor(opts = {}) {
    this.opts = opts;
    this.target = null;            // {host, name, kind, label, path, resume, args, tags}
    this.ws = null;
    this.gen = 0;                  // bumps on every attach; stale sockets check it
    this.retries = 0;

    this.dot = h("span.dot");
    this.chip = h("span.kind");
    this.title = h("span.name");
    this.sub = h("span.sub");
    this.actionsEl = h("div.actions");
    this.head = h("div.pane-head", this.dot, this.chip, this.title, this.sub, this.actionsEl);
    this.relaunch = h("div.relaunch", { hidden: true });
    this.host = h("div.term");
    this.placeholder = h("div.pane-empty", { hidden: true });
    this.el = h("div.pane", { onmousedown: () => this.opts.onFocus && this.opts.onFocus(this) },
      this.head, this.relaunch, this.host, this.placeholder);

    this.term = new window.Terminal(termOptions());
    this.fit = new window.FitAddon.FitAddon();
    this.term.loadAddon(this.fit);
    this.term.loadAddon(new window.ClipboardAddon.ClipboardAddon(undefined, osc52Provider()));
    this.term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
    this.term.open(this.host);
    this.clip = installClipboard(this.term, { send: (b) => this.send(b), prefs });
    swallowQueries(this.term);

    this.term.onData((d) => this.send(enc.encode(d)));
    this.term.onBinary((d) => {
      const u8 = new Uint8Array(d.length);
      for (let i = 0; i < d.length; i++) u8[i] = d.charCodeAt(i) & 255;
      this.send(u8);
    });
    this.term.textarea && this.term.textarea.addEventListener("focus", () => this.opts.onFocus && this.opts.onFocus(this));

    this._resizeTimer = 0;
    this._ro = new ResizeObserver(() => this.refit());
    this._ro.observe(this.host);
    this._onPrefs = () => this.applyPrefs();
    window.addEventListener("serai:prefs", this._onPrefs);
    this.renderActions();
  }

  // -- public ----------------------------------------------------------------

  attach(target) {
    this.target = { ...target };
    this.gen++;
    this.retries = 0;
    this.placeholder.hidden = true;
    this.host.hidden = false;
    this.term.reset();
    this.refresh();
    this.connect(this.gen);
  }

  detach() {
    this.gen++;
    this.closeSocket();
    this.target = null;
    this.refresh();
  }

  focus() { this.term.focus(); }

  isAttachedTo(host, name) {
    return !!this.target && this.target.host === host && this.target.name === name;
  }

  send(bytes) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    this.ws.send(bytes);
    return true;
  }

  /** Repaint head + relaunch strip from the live session (every poll). */
  refresh() {
    const t = this.target;
    if (!t) {
      this.head.hidden = true;
      this.relaunch.hidden = true;
      this.host.hidden = true;
      this.placeholder.hidden = false;
      fill(this.placeholder, h("div", "No session in this pane."),
        h("div.dim", "Pick one on the left, or start one with + New."));
      return;
    }
    this.head.hidden = false;
    const live = store.session(t.host, t.name);
    const info = kindInfo(t.kind || (live && live.kind));
    this.dot.className = `dot ${live ? store.STATE_CLASS[live.state] || "idle" : "idle"}`;
    if (live && (live.state === "running" || live.state === "needs_input")) this.dot.classList.add("pulse");
    this.chip.className = `kind ${info.chip}`;
    this.chip.textContent = info.chip.toUpperCase();
    this.title.textContent = (live && live.label) || t.label || t.name;
    const dir = (live && (live.dir || live.path)) || t.path || "";
    this.sub.textContent = `· ${t.host}${dir ? " · " + shortPath(dir) : ""}`;
    this.renderRelaunch(live, info);
  }

  renderActions() {
    fill(this.actionsEl, this.opts.actions ? this.opts.actions(this) : null);
  }

  refit() {
    if (this.host.hidden || !this.host.clientWidth) return;
    try { this.fit.fit(); } catch { return; }
    clearTimeout(this._resizeTimer);
    this._resizeTimer = setTimeout(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({ resize: { cols: this.term.cols, rows: this.term.rows } }));
      }
    }, 80);
  }

  applyPrefs() {
    const o = termOptions();
    for (const k of ["fontFamily", "fontSize", "cursorStyle", "cursorBlink", "theme"]) this.term.options[k] = o[k];
    this.refit();
  }

  dispose() {
    this.gen++;
    this.closeSocket();
    this._ro.disconnect();
    window.removeEventListener("serai:prefs", this._onPrefs);
    this.term.dispose();
    this.el.remove();
  }

  // -- relaunch strip ----------------------------------------------------------

  renderRelaunch(live, info) {
    const show = !!(live && info.agent && live.at_prompt);
    this.relaunch.hidden = !show;
    if (!show) return;
    const saved = (store.get().saved || []).find((r) => r.host === live.host && r.name === live.name);
    const launch = (saved && saved.launch) || "resume";
    const flag = launch === "new" ? "" : (info.resume[launch] || "");
    fill(this.relaunch,
      h("span.dot.idle"), ` ${info.cmd} exited · this session is at a shell prompt`, h("span.spacer"),
      h("button.btn.primary", { type: "button", onclick: () => this.doRelaunch() },
        `▶ Relaunch ${info.cmd}`, flag ? h("span.flag", ` ${flag}`) : null),
      flag ? h("button.btn.sm", { type: "button", onclick: () => this.doRelaunch("new") }, "start fresh") : null);
  }

  async doRelaunch(launch) {
    const t = this.target;
    if (!t) return;
    try {
      const r = await post("/api/sessions/relaunch", { host: t.host, name: t.name, ...(launch ? { launch } : {}) });
      toast(`started: ${r.command}`);
      this.relaunch.hidden = true;
      this.focus();
    } catch (err) {
      toast(err.message, "error", 5000);
    }
  }

  // -- socket ------------------------------------------------------------------

  closeSocket() {
    clearInterval(this._ping);
    clearTimeout(this._stable);
    clearTimeout(this._retry);
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.onclose = null;
      try { ws.close(); } catch { /* already closed */ }
    }
  }

  connect(gen) {
    this.closeSocket();
    const t = this.target;
    if (!t) return;
    try { this.fit.fit(); } catch { /* not laid out yet; the server clamps */ }
    const q = new URLSearchParams({ host: t.host, kind: t.kind || "shell", label: t.label || "main" });
    if (t.name) q.set("name", t.name);
    for (const k of ["path", "resume", "args", "tags"]) if (t[k]) q.set(k, t[k]);
    q.set("history", String(prefs().scrollback));
    q.set("cols", String(this.term.cols));
    q.set("rows", String(this.term.rows));
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/attach?${q}`);
    ws.binaryType = "arraybuffer";
    this.ws = ws;

    ws.onopen = () => {
      if (gen !== this.gen) { ws.close(); return; }
      this._stable = setTimeout(() => {
        if (this.retries) this.term.write("\r\n\x1b[90m[reconnected]\x1b[0m\r\n");
        this.retries = 0;
      }, STABLE_MS);
      this._ping = setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) ws.send('{"ping":1}');
      }, PING_MS);
    };

    ws.onmessage = (ev) => {
      if (gen !== this.gen) return;
      if (typeof ev.data !== "string") {
        this.term.write(new Uint8Array(ev.data));
        return;
      }
      let m = null;
      if (ev.data.charAt(0) === "{") { try { m = JSON.parse(ev.data); } catch { m = null; } }
      if (!m) { this.term.write(ev.data); return; }       // a plain server notice
      if (m.ping) return;
      if (m.seed === "begin") {
        // A full snapshot follows (history, screen, modes, cursor). The server
        // created/attached at our size; adopt the pane's if it differs.
        this.term.reset();
        if (m.cols && m.rows && (m.cols !== this.term.cols || m.rows !== this.term.rows)) this.term.resize(m.cols, m.rows);
      } else if (m.size) {
        this.term.resize(m.size.cols, m.size.rows);
      } else if (m.error) {
        this.term.write(`\r\n\x1b[31m[serai] ${m.error}\x1b[0m\r\n`);
      }
    };

    ws.onclose = (ev) => {
      clearInterval(this._ping);
      clearTimeout(this._stable);
      if (gen !== this.gen) return;
      this.ws = null;
      const code = ev.code;
      if (code === 4410) {
        this.term.write("\r\n\x1b[90m[session ended]\x1b[0m\r\n");
        window.dispatchEvent(new CustomEvent("serai:poll"));
        return;
      }
      if (code === 4404) { this.term.write(`\r\n\x1b[31m[serai] ${t.host} is not in your ssh config\x1b[0m\r\n`); return; }
      if (code === 4403) { this.term.write("\r\n\x1b[31m[serai] that session belongs to another user\x1b[0m\r\n"); return; }
      if (code === 4401) {
        this.term.write("\r\n\x1b[33m[signed out -- sign in again to reattach]\x1b[0m\r\n");
        window.dispatchEvent(new CustomEvent("serai:auth", { detail: { then: () => gen === this.gen && this.connect(gen) } }));
        return;
      }
      if (this.retries >= MAX_RETRIES) {
        this.term.write(`\r\n\x1b[33m[gave up after ${MAX_RETRIES} tries -- the host looks unreachable; click the session to retry]\x1b[0m\r\n`);
        this.retries = 0;
        return;
      }
      const delay = Math.min(500 * 2 ** this.retries, 5000);
      this.retries++;
      this.term.write(`\r\n\x1b[90m[disconnected -- reattaching in ${(delay / 1000).toFixed(1)}s (${this.retries}/${MAX_RETRIES})]\x1b[0m\r\n`);
      this._retry = setTimeout(() => { if (gen === this.gen) this.connect(gen); }, delay);
    };
  }
}

/** tmux already answers the pane's terminal queries (device attributes, cursor
 *  position, colour and mode reports). The raw stream still carries each query,
 *  and xterm would answer a second time -- typing "^[[24;1R" into a prompt. */
function swallowQueries(term) {
  for (const id of [{ final: "c" }, { prefix: ">", final: "c" }, { final: "n" }, { prefix: "?", final: "n" },
    { prefix: "?", intermediates: "$", final: "p" }, { intermediates: "$", final: "p" }, { final: "t" }]) {
    term.parser.registerCsiHandler(id, () => true);
  }
  for (const id of [4, 10, 11, 12]) term.parser.registerOscHandler(id, (data) => data.endsWith("?"));
}

export function shortPath(p) {
  return String(p || "").replace(/^\/home\/[^/]+/, "~").replace(/^\/root(?=\/|$)/, "~");
}
