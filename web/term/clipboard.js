// The copy/paste model (v3 plan 3.5), in one place.
//
// Selection is xterm's own: plain drag, double-click a word, triple-click a
// line, Shift+click to extend. Under the control-mode bridge xterm holds the
// pane's real buffer and scrollback, so this works the same in a shell and in
// a full-screen agent. (When the app itself has mouse tracking on, a plain
// drag goes to the app; Shift+drag still selects -- standard terminal rules.)
//
//   copy   Ctrl+Shift+C, Ctrl+Insert, Cmd+C, and Ctrl+C *when text is
//          selected* (it copies and clears; with nothing selected Ctrl+C is
//          ^C, an interrupt, as always). Right-click menu: Copy.
//   paste  Ctrl+V, Ctrl+Shift+V, Shift+Insert, Cmd+V all arrive as the
//          browser's native paste event -- the one path, so a paste can never
//          be sent twice. Right-click menu: Paste (reads the clipboard API).
//
// Pasted text goes to the pane as raw bytes with line endings normalised to
// \n, never through term.paste(): bracketed paste is opt-in (Settings), since
// a TUI that mishandles CSI 200~ (grok did) swallows every later keystroke.
// Every toast reports what actually happened, not what was attempted.

import { openMenu } from "../core/menu.js";
import { toast } from "../core/dialog.js";

const enc = new TextEncoder();

/** installClipboard(term, {send(bytes), prefs()}) -> {copy(), pasteFromClipboard()} */
export function installClipboard(term, { send, prefs }) {
  const p = () => (prefs ? prefs() : {});

  async function copy() {
    const text = term.getSelection();
    if (!text) {
      toast("nothing selected -- drag over text first (Shift+drag inside apps that use the mouse)", "warn", 3600);
      return false;
    }
    const ok = await writeClipboard(text);
    if (ok) {
      const lines = text.split("\n").length;
      toast(lines > 1 ? `copied ${lines} lines` : `copied ${text.length} character${text.length === 1 ? "" : "s"}`);
      term.clearSelection();
    } else {
      toast("copy failed -- the browser refused clipboard access for this page", "error", 5000);
    }
    term.focus();
    return ok;
  }

  function insert(text) {
    text = String(text || "").replace(/\r\n?/g, "\n");
    if (!text) {
      toast("nothing to paste -- the clipboard holds no text", "warn", 3000);
      return false;
    }
    if (p().bracketedPaste && term.modes.bracketedPasteMode) text = `\x1b[200~${text}\x1b[201~`;
    if (!send(enc.encode(text))) {
      toast("not connected -- the paste was not sent", "error", 3500);
      return false;
    }
    return true;
  }

  async function pasteFromClipboard() {
    let text;
    try {
      text = await navigator.clipboard.readText();
    } catch {
      toast("the browser blocked reading the clipboard -- use Ctrl+V instead", "warn", 4500);
      return false;
    }
    term.focus();
    return insert(text);
  }

  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== "keydown") return true;
    const ctrl = e.ctrlKey && !e.metaKey, meta = e.metaKey && !e.ctrlKey;
    const code = e.code;
    const copyKey = (ctrl && e.shiftKey && code === "KeyC") || (ctrl && !e.shiftKey && code === "Insert")
      || (meta && !e.shiftKey && code === "KeyC")
      || (ctrl && !e.shiftKey && !e.altKey && code === "KeyC" && term.hasSelection());
    if (copyKey) {
      e.preventDefault();
      copy();
      return false;
    }
    const pasteKey = ((ctrl || meta) && code === "KeyV") || (e.shiftKey && !e.ctrlKey && !e.metaKey && code === "Insert");
    if (pasteKey) {
      // Not preventDefault: the browser then fires its native paste event on
      // xterm's textarea, which is handled below. Returning false keeps xterm
      // from also sending ^V (quoted-insert, which hung grok).
      return false;
    }
    return true;
  });

  // Capture phase on the terminal element: ours runs before xterm's own paste
  // handler, which would call term.paste() and bracket the text.
  term.element.addEventListener("paste", (e) => {
    const cd = e.clipboardData;
    const text = cd ? (cd.getData("text/plain") || cd.getData("text")) : "";
    e.preventDefault();
    e.stopImmediatePropagation();
    insert(text);
  }, true);

  // Copy-on-select is opt-in (Settings > Terminal).
  term.element.addEventListener("mouseup", () => {
    if (p().copyOnSelect && term.hasSelection()) setTimeout(copy, 0);
  });

  // Right-click: our menu, unless the app is tracking the mouse (then the
  // click belongs to it; Shift+right-click still opens ours).
  term.element.addEventListener("contextmenu", (e) => {
    if (term.modes.mouseTrackingMode !== "none" && !e.shiftKey) return;
    e.preventDefault();
    const has = term.hasSelection();
    openMenu({ x: e.clientX, y: e.clientY }, [
      { label: "Copy", icon: "⧉", kbd: "Ctrl+Shift+C", disabled: !has, onClick: copy },
      { label: "Paste", icon: "⎘", kbd: "Ctrl+V", onClick: pasteFromClipboard },
      "sep",
      { label: "Select all", icon: "▤", onClick: () => term.selectAll() },
      { label: "Clear selection", icon: " ", disabled: !has, onClick: () => term.clearSelection() },
    ]);
  });

  return { copy, pasteFromClipboard, insert };
}

/** Write text to the system clipboard; true only if it actually landed. The
 *  async API needs a secure context (https or localhost, which serai is); the
 *  execCommand fallback covers browsers that refuse it inside our gesture. */
export async function writeClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch { /* fall through */ }
  }
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;left:-9999px;top:0;opacity:0";
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { ok = false; }
  ta.remove();
  return ok;
}

/** OSC 52 from apps (a TUI's own "copy"): write-only. A clipboard *read* request
 *  answers empty -- a program in a pane must never read your clipboard. */
export function osc52Provider() {
  return {
    async readText() { return ""; },
    async writeText(_sel, text) {
      if (!text) return;
      if (await writeClipboard(text)) toast(`copied ${text.length} character${text.length === 1 ? "" : "s"} from the app`);
    },
  };
}
