"""Discover live tmux sessions on the local machine and on remote hosts, and
make a best-effort guess at each session's state (running / needs input / idle).

The unifying idea: every session -- local shell, remote shell, or Claude Code --
lives inside a named tmux session. We name them by convention so we can tell
them apart and strip the prefix for display:

    cc-<project>     -> a Claude Code session   (kind="claude")
    shell-<name>     -> a plain shell           (kind="shell")
    anything else    -> kind="shell", label = full name

Remote enumeration runs `tmux` over ssh in BatchMode, so it relies on your
ssh-agent / keys and never prompts for a password -- the app stores no
credentials of its own.
"""

from __future__ import annotations

import os
import re
import shlex
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field, asdict

# Fields joined by a delimiter unlikely to appear in a session name. The last
# field is our per-session tags, stored as a tmux user option (@serai_tags) so
# they persist in the session itself with no external store (invariants #1/#4).
_FMT = ("#{session_name}::#{session_attached}::#{session_activity}::#{@serai_tags}"
        "::#{pane_current_command}::#{@serai_dir}::#{pane_current_path}")
_SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=4", "-o", "StrictHostKeyChecking=accept-new"]

# Per-host TTL cache around remote session discovery. Local discovery is cheap
# (and we want new local sessions to appear at once), so only remote hosts are
# cached. A short TTL keeps many remote hosts responsive under the concurrent
# polling in /api/sessions. Override with SERAI_TMUX_CACHE_TTL (seconds).
_CACHE_TTL = float(os.environ.get("SERAI_TMUX_CACHE_TTL", "3.0"))

# Heuristic markers that suggest a session is blocked waiting for the user. The
# check is per-kind: the common set applies to every session, plus a set tuned
# to Claude Code's permission prompts (cc-*) or to plain-shell prompts. Operators
# extend any set without touching code via the SERAI_WAIT_MARKERS* env vars
# (see _build_markers). Markers are matched against the lowercased pane tail, so
# keep them lowercase.
_COMMON_MARKERS = (
    "do you want",
    "(y/n)",
    "[y/n]",
    "press enter",
    "❯ 1.",
    "│ do you",
    "waiting for",
    "approve",
)
# Claude Code permission prompts (the numbered "Do you want to proceed?" box).
_CLAUDE_MARKERS = (
    "1. yes",
    "no, and tell",
    "don't ask again",
)
# Plain-shell prompts that block on input (sudo, ssh, package managers, etc.).
_SHELL_MARKERS = (
    "password:",
    "passphrase",
    "[sudo]",
    "(yes/no)",
    "are you sure",
)


def _env_markers(var: str) -> tuple[str, ...]:
    """Operator-supplied extra markers from a comma/newline-separated env var."""
    parts = re.split(r"[,\n]", os.environ.get(var, ""))
    return tuple(p.strip().lower() for p in parts if p.strip())


def _build_markers() -> dict[str, tuple[str, ...]]:
    """Effective markers per kind: built-in defaults + env additions.

    Env vars (comma/newline-separated, appended to -- not replacing -- the
    defaults; read once at import):
      SERAI_WAIT_MARKERS         applied to every session
      SERAI_WAIT_MARKERS_CLAUDE  applied to cc-* (Claude Code) sessions
      SERAI_WAIT_MARKERS_SHELL   applied to shell sessions
    """
    return {
        "common": _COMMON_MARKERS + _env_markers("SERAI_WAIT_MARKERS"),
        "claude": _CLAUDE_MARKERS + _env_markers("SERAI_WAIT_MARKERS_CLAUDE"),
        "shell": _SHELL_MARKERS + _env_markers("SERAI_WAIT_MARKERS_SHELL"),
    }


_MARKERS = _build_markers()


@dataclass
class Session:
    host: str            # "local" or an ssh alias
    name: str            # raw tmux session name
    kind: str            # "claude" | "shell"
    label: str           # display label (prefix stripped)
    state: str           # "running" | "needs_input" | "done" | "idle"
    attached: bool
    tags: list[str] = field(default_factory=list)  # per-session @serai_tags
    path: str = ""       # active pane's *live* cwd (used to restore after reboot)
    dir: str = ""        # configured "start in" dir (@serai_dir); "" if unset
    tail: str = ""       # short preview of the pane's last lines (for board cards)
    age: float = -1.0    # seconds since the session last saw activity (-1 = unknown)

    @property
    def id(self) -> str:
        return f"{self.host}::{self.name}"

    def as_dict(self) -> dict:
        d = asdict(self)
        d["id"] = self.id
        return d


def _classify(name: str) -> tuple[str, str]:
    """(kind, display label). serai's own convention is a cc-<project> /
    shell-<name> prefix, but many sessions also carry a <project>-claude /
    <project>-term suffix (possibly *under* serai's shell- prefix, e.g.
    shell-example-claude). We strip serai's storage prefix first, then let the
    suffix decide -- so a name's own "-claude" marker wins over a generic
    shell- prefix -- and fall back to the prefix, then plain shell."""
    core, forced = name, None
    if name.startswith("cc-"):
        core, forced = name[3:], "claude"
    elif name.startswith("shell-"):
        core, forced = name[6:], "shell"
    low = core.lower()
    if low.endswith("-claude"):
        return "claude", core[:-7]
    if low.endswith("-term"):
        return "shell", core[:-5]
    return (forced or "shell"), core


def _run(argv: list[str], timeout: int = 6) -> str | None:
    try:
        out = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


def _tmux_argv(host: str, remote_cmd: str) -> list[str]:
    """Build argv to run a tmux command locally or over ssh.

    For remote hosts the whole tmux command is handed to the remote shell as a
    single string, so quoting happens once on the remote side.
    """
    if host == "local":
        return shlex.split(remote_cmd)
    return ["ssh", *_SSH_OPTS, host, remote_cmd]


def _capture_lines(host: str, name: str, lines: int = 16) -> list[str]:
    """The pane's last non-blank lines (original case). One capture-pane call
    feeds both the wait-marker check and the board's tail preview."""
    cmd = f"tmux capture-pane -p -t {shlex.quote(name)}"
    out = _run(_tmux_argv(host, cmd))
    if not out:
        return []
    # capture-pane pads the pane to its full height with blank lines, so a short
    # prompt (e.g. a fresh `[sudo] password:` on a tall pane) sits well above the
    # physical bottom. Tail the last non-blank lines so markers -- and the
    # preview -- are caught regardless of how much empty space trails the content.
    return [ln.rstrip() for ln in out.splitlines() if ln.strip()][-lines:]


# Furniture rather than output. A Claude pane parked at its prompt ends in
# box-drawing and an empty caret, so the literal last lines of the pane say
# nothing on a board card -- skip them and show the last real output instead.
_BOX_CHARS = re.compile(r"[─-╿▀-▟=_·•—–-]")
_BARE_PROMPT = re.compile(r"^[>❯»›$#]\s*$")
_UI_HINT = re.compile(r"(\? for shortcuts|shift\+tab to cycle)", re.I)


def _is_chrome(line: str) -> bool:
    """True for a line that carries no information: a rule, a box edge, an empty
    input caret (bare or drawn inside a box), or a keyboard hint."""
    s = line.strip()
    if not s:
        return True
    if _UI_HINT.search(s):
        return True
    core = _BOX_CHARS.sub("", s).strip()  # drop borders/rules, see what's left
    return not core or bool(_BARE_PROMPT.match(core))


def _preview(lines: list[str], n: int = 12, width: int = 120) -> str:
    """A multi-line preview of the pane tail, for a board card.

    Prefers lines that actually say something; falls back to the raw tail when a
    pane is nothing but furniture, so a card is never blank.

    More lines are sent than a compact card shows: the board grows its cards when
    only a few are on screen, and the extra lines are what it grows into. CSS
    clips the rest, so this is the ceiling, not what's displayed.
    """
    content = [ln for ln in lines if not _is_chrome(ln)]
    out = []
    for ln in (content or lines)[-n:]:
        ln = ln.strip()
        out.append(ln[: width - 1] + "…" if len(ln) > width else ln)
    return "\n".join(out)


# Foreground commands that mean "a shell sitting at its prompt" -- i.e. not a
# process actively doing work. Anything else in pane_current_command reads as
# busy. Compared lowercased; tmux may prefix a login shell with "-".
_SHELL_CMDS = {"bash", "-bash", "zsh", "-zsh", "fish", "-fish", "sh", "-sh",
               "dash", "ksh", "tcsh", "csh", "login", "tmux"}
# A shell is "working" if it saw activity within _WORKING_WINDOW seconds; a
# Claude session sitting at its prompt shows "done" until it's been quiet for
# _DONE_WINDOW seconds, then decays to idle. Override with the env vars.
_WORKING_WINDOW = float(os.environ.get("SERAI_WORKING_WINDOW", "20"))
_DONE_WINDOW = float(os.environ.get("SERAI_DONE_WINDOW", "1800"))
# Claude Code prints this in its status line while a turn is actively running.
# It's the reliable "working" signal for a cc session (session_activity isn't:
# Claude's TUI repaints constantly, so its activity age always looks fresh).
_CLAUDE_BUSY = ("esc to interrupt", "esc to cancel")


def _state_for(kind: str, attached: bool, secs: float | None, command: str, marker_text: str) -> str:
    """running (working) / needs_input (blocked) / done / idle.

    Claude and shells need different signals. A Claude pane's foreground is always
    its node process and its TUI repaints, so activity age is meaningless there --
    we read its *content* instead: a permission prompt is blocked, an
    "esc to interrupt" status is working, otherwise it's parked at its prompt =
    done (finished, unread) until you open it or it goes dormant. A shell has no
    such status line, so we use the activity age and the foreground command.
    """
    markers = _MARKERS["common"] + _MARKERS.get(kind, ())
    if marker_text and any(m in marker_text for m in markers):
        return "needs_input"

    if kind == "claude":
        if marker_text and any(b in marker_text for b in _CLAUDE_BUSY):
            return "running"
        # Parked at its prompt: recently active reads as "done" (unread) until you
        # open it (attached) or it ages past the done window; long-dormant is idle.
        if not attached and secs is not None and secs < _DONE_WINDOW:
            return "done"
        return "idle"

    # shell: a running foreground process, or fresh activity, means it's busy.
    busy_cmd = bool(command) and command.lower() not in _SHELL_CMDS
    if busy_cmd or (secs is not None and secs < _WORKING_WINDOW):
        return "running"
    return "idle"


_cache_lock = threading.Lock()
_session_cache: dict[str, tuple[float, list[Session]]] = {}


def _list_sessions_uncached(host: str) -> list[Session]:
    out = _run(_tmux_argv(host, f"tmux list-sessions -F '{_FMT}'"))
    if not out:
        return []

    sessions: list[Session] = []
    for line in out.splitlines():
        parts = line.split("::", 6)  # bounded split: the path (last) may contain "::"
        if len(parts) < 2:
            continue
        name, attached_flag = parts[0], parts[1]
        attached = attached_flag.strip() == "1"
        activity = parts[2] if len(parts) > 2 else ""
        tags = clean_tags((parts[3] if len(parts) > 3 else "").split(","))
        command = parts[4] if len(parts) > 4 else ""
        start_dir = parts[5] if len(parts) > 5 else ""
        path = parts[6] if len(parts) > 6 else ""
        kind, label = _classify(name)
        lines = _capture_lines(host, name)
        # Markers only care about the recent tail; the preview wants more history
        # to look past a prompt box, so capture wide and narrow here.
        marker_text = "\n".join(lines[-8:]).lower()
        try:
            secs = time.time() - int(activity) if activity.strip() else None
        except ValueError:
            secs = None
        state = _state_for(kind, attached, secs, command, marker_text)
        sessions.append(
            Session(host=host, name=name, kind=kind, label=label, state=state,
                    attached=attached, tags=tags, path=path, dir=start_dir,
                    tail=_preview(lines),
                    age=(round(secs) if secs is not None else -1.0))
        )
    return sessions


def list_sessions(host: str) -> list[Session]:
    """Discover sessions on `host`, caching remote results for a few seconds.

    Local discovery is fast and always runs fresh, so a newly created local
    session appears immediately. Remote discovery is an ssh round trip per host
    (plus one per session for the state probe), so results are cached per host
    for `_CACHE_TTL` seconds -- this keeps many hosts responsive under the
    concurrent polling in /api/sessions.
    """
    if host == "local":
        return _list_sessions_uncached(host)

    now = time.monotonic()
    with _cache_lock:
        hit = _session_cache.get(host)
        if hit is not None and now - hit[0] < _CACHE_TTL:
            return hit[1]

    # Discover outside the lock so concurrent hosts don't serialize on it.
    result = _list_sessions_uncached(host)
    with _cache_lock:
        _session_cache[host] = (time.monotonic(), result)
    return result


def clear_cache(host: str | None = None) -> None:
    """Drop cached session results -- all hosts, or just one."""
    with _cache_lock:
        if host is None:
            _session_cache.clear()
        else:
            _session_cache.pop(host, None)


_SAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]")


def _quote_path(path: str) -> str:
    """Quote a path for `cd` while keeping a leading ~ expandable.

    shlex.quote() would wrap `~/git/app` in single quotes and stop the shell
    from expanding ~, so we quote only the part after the tilde.
    """
    path = path.strip()
    if path == "~":
        return "~"
    if path.startswith("~/"):
        return "~/" + shlex.quote(path[2:])
    return shlex.quote(path)


def session_name(kind: str, label: str) -> str:
    """Build a conventional session name from a kind + label."""
    label = _SAFE_NAME.sub("-", label.strip()) or "session"
    prefix = "cc-" if kind == "claude" else "shell-"
    return f"{prefix}{label}"


_CLAUDE_RESUME = {"continue": " --continue", "resume": " --resume"}
# The ways a Claude session can come back: fresh, pick the last conversation up,
# or open its resume picker. A value outside this set falls back to fresh, since
# the mapping is a lookup rather than interpolation -- nothing to inject.
RESUME_CHOICES = ("", "continue", "resume")


def attach_argv(host: str, name: str, kind: str, path: str | None = None,
                resume: str = "", mouse: bool = True, history: int | None = None) -> list[str]:
    """Command that attaches to a session, creating it if absent (tmux new -A).

    Claude Code sessions launch `claude` in the given project directory; shells
    just start an interactive tmux. Everything runs under a local PTY (see
    main.py), and remote sessions wrap the same tmux command in ssh -t.

    `resume` picks how a *new* claude session starts: "" -> new conversation,
    "continue" -> `claude --continue` (most recent in the dir), "resume" ->
    `claude --resume` (interactive picker). The flag is a fixed literal (never
    client text), so it stays argv-safe; tmux `new -A` only runs the command on
    create, so reattaching an existing session ignores it.

    `mouse` toggles tmux mouse mode for this session so the scroll wheel pages
    through its scrollback (under tmux the history lives in copy mode, not in
    xterm.js's buffer). Set explicitly on every attach with `-t <name>` so it's
    session-scoped -- never touches the user's global tmux config -- and so the
    toggle takes effect when you re-attach.

    `history` is how many lines of scrollback to keep (tmux `history-limit`). It
    only takes effect for a pane at *creation* and the session doesn't exist yet
    for a fresh `new`, so it's set on the server (`set -g`) before `new -A`. That
    means it applies to newly-created sessions (re-attaching keeps the existing
    pane's history) and nudges the tmux server's global default.

    The lone ';' elements are tmux command separators (argv-safe; shlex.quote
    keeps them intact for ssh; the int is stringified so it can't inject).
    """
    tmux_cmd = ["tmux"]
    if history is not None:
        tmux_cmd += ["set", "-g", "history-limit", str(int(history)), ";"]
    if path:
        # Both kinds honour a start directory. `cd` runs in the shell tmux spawns,
        # so _quote_path's expandable ~ works and no tilde reaches tmux itself;
        # a shell then `exec`s so the session behaves like an ordinary login shell.
        run = (f"claude{_CLAUDE_RESUME.get(resume, '')}" if kind == "claude"
               else "exec ${SHELL:-/bin/sh}")
        tmux_cmd += ["new", "-A", "-s", name, f"cd {_quote_path(path)} && {run}"]
    else:
        tmux_cmd += ["new", "-A", "-s", name]

    tmux_cmd += [";", "set-option", "-t", name, "mouse", "on" if mouse else "off"]
    # Publish copy-mode selections as OSC 52 (needs the outer terminal's Ms cap;
    # xterm.js reports xterm-256color, which has it). The web client bridges the
    # sequence to the browser clipboard, so a plain drag in mouse mode -- which
    # selects in tmux copy mode and auto-scrolls through the full history --
    # copies without Shift+drag's one-screen limit. Server-scoped option.
    tmux_cmd += [";", "set-option", "-s", "set-clipboard", "on"]

    if host == "local":
        return tmux_cmd
    return ["ssh", *_SSH_OPTS, "-t", host, " ".join(shlex.quote(p) for p in tmux_cmd)]


def restore_argv(host: str, name: str, kind: str, path: str = "", resume: str = "continue") -> list[str]:
    """Recreate a session *detached* (``tmux new -A -d``) so it exists again after
    a reboot without opening a terminal. Idempotent: ``-A`` no-ops if the session
    already exists (the command only runs on create). Claude sessions relaunch
    with ``--continue`` by default -- picking the last conversation back up in
    that dir -- while shells come back as a fresh shell. The saved working dir is
    passed as tmux's ``-c`` start-directory, a separate argv element that is never
    interpolated, so it stays argv-safe; remote hosts wrap the same tmux command
    in ssh (invariant #3).
    """
    cmd = ["tmux", "new", "-A", "-d", "-s", name]
    if path:
        cmd += ["-c", path]
    if kind == "claude":
        cmd.append("claude" + _CLAUDE_RESUME.get(resume, ""))
    if host == "local":
        return cmd
    return ["ssh", *_SSH_OPTS, host, " ".join(shlex.quote(p) for p in cmd)]


def send_keys_argv(host: str, name: str, text: str) -> list[str]:
    """Command that types `text` then Enter into an existing session.

    This is the fleet-broadcast primitive: the same line is sent to many
    sessions at once. Like attach_argv it stays argv-safe -- `--` stops tmux
    option parsing so a command starting with `-` is literal, the (hostile)
    session name and command text are each their own argv element, and remote
    sends quote every element once for the remote shell. Unlike attach_argv
    there is no `ssh -t`: send-keys is a one-shot that needs no tty.
    """
    tmux_cmd = ["tmux", "send-keys", "-t", name, "--", text, "Enter"]
    if host == "local":
        return tmux_cmd
    return ["ssh", *_SSH_OPTS, host, " ".join(shlex.quote(p) for p in tmux_cmd)]


def run_send(argv: list[str], timeout: int = 6) -> bool:
    """Run a one-shot tmux argv (send-keys / rename / set-option); True on success."""
    return _run(argv, timeout=timeout) is not None


def rename_argv(host: str, name: str, new_name: str) -> list[str]:
    """Command that renames a session. The caller builds `new_name` via
    session_name() so the cc-/shell- prefix is preserved (invariant #5)."""
    tmux_cmd = ["tmux", "rename-session", "-t", name, new_name]
    if host == "local":
        return tmux_cmd
    return ["ssh", *_SSH_OPTS, host, " ".join(shlex.quote(p) for p in tmux_cmd)]


def kill_argv(host: str, name: str) -> list[str]:
    """Command that kills a session (ends its tmux session)."""
    tmux_cmd = ["tmux", "kill-session", "-t", name]
    if host == "local":
        return tmux_cmd
    return ["ssh", *_SSH_OPTS, host, " ".join(shlex.quote(p) for p in tmux_cmd)]


def session_exists(host: str, name: str) -> bool:
    """True if a tmux session with this name currently exists on `host`.

    Used after a PTY closes to tell an intentional end (program exited / killed
    -> session gone) from a detach or network blip (session still alive), so the
    client knows whether to reattach; also gates restore-after-reboot (skip what
    is already running). The `=` prefix forces an exact name match -- tmux's
    default prefix matching would report "shell-x" as existing whenever
    "shell-x1" is running.
    """
    cmd = f"tmux has-session -t {shlex.quote('=' + name)}"
    return _run(_tmux_argv(host, cmd)) is not None


def set_tags_argv(host: str, name: str, tags_csv: str) -> list[str]:
    """Command that stores tags on a session as a tmux user option.

    Tags live in the session itself (@serai_tags), so they persist for the
    session's lifetime with no external store (invariants #1/#4). Argv-safe like
    the other builders; remote pieces are quoted once for the remote shell.
    """
    tmux_cmd = ["tmux", "set-option", "-t", name, "@serai_tags", tags_csv]
    if host == "local":
        return tmux_cmd
    return ["ssh", *_SSH_OPTS, host, " ".join(shlex.quote(p) for p in tmux_cmd)]


def set_dir_argv(host: str, name: str, path: str) -> list[str]:
    """Command that stores a session's "start in" directory (@serai_dir).

    Like tags, it lives on the session itself rather than an external store
    (invariants #1/#4). It seeds the file pane and post-reboot restore; it can't
    retroactively move a shell that's already running. Argv-safe, same as the
    other builders.
    """
    tmux_cmd = ["tmux", "set-option", "-t", name, "@serai_dir", path]
    if host == "local":
        return tmux_cmd
    return ["ssh", *_SSH_OPTS, host, " ".join(shlex.quote(p) for p in tmux_cmd)]


def clean_dir(path: str) -> str:
    """Normalise a user-supplied start dir, or "" to clear it.

    "::" is the field separator in _FMT, so a path containing it would corrupt
    the listing parse -- reject rather than silently mangle the row.
    """
    p = (path or "").strip()
    if "::" in p:
        raise ValueError("a start directory cannot contain '::'")
    return p


_TAG_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def clean_tags(tags) -> list[str]:
    """Normalize tags: trim, collapse unsafe chars to '-', drop empties, dedupe.

    Keeping tags within [A-Za-z0-9_.-] means they never contain the ',' or '::'
    used as storage/field delimiters, so reading them back stays unambiguous.
    """
    out: list[str] = []
    for t in tags:
        t = _TAG_UNSAFE.sub("-", str(t).strip()).strip("-")
        if t and t not in out:
            out.append(t)
    return out


def tcp_reachable(hostname: str, port: int = 22, timeout: float = 2.0) -> bool:
    """Best-effort: can we open a TCP connection to host:port right now?

    Used to dim ssh-config hosts that are currently offline. It's a hint, not a
    guarantee -- it tests raw reachability of the ssh port, not auth, and does
    not follow ProxyJump/ProxyCommand. A down or unrouteable host returns False.
    """
    try:
        with socket.create_connection((hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    for s in list_sessions("local"):
        print(f"{s.host:<10} {s.kind:<7} {s.label:<18} {s.state}")
