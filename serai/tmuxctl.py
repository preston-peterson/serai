"""tmux control-mode (``tmux -C``) client core: framing, codecs, and an asyncio
client. This is the protocol layer for the v3 attach bridge -- nothing here
imports FastAPI or knows about websockets; ``main.py`` wiring is deliberately
out of scope for now.

Why control mode instead of a PTY: a PTY can only carry what the pane *shows*,
so selection, copy, and scrollback have to be guessed back out of rendered
bytes (the recurring copy/paste pain, backlog items 3/11/18). Control mode is a
framed, byte-exact protocol: pane output arrives as escaped data on stdout,
commands go in as lines on stdin, and every command's reply is a contiguous
``%begin``/``%end`` block in command order.

Protocol (tmux 3.6 CONTROL MODE):
  - Commands are newline-terminated argument lines on stdin.
  - Each command produces one block: ``%begin <time> <num> <flags>``, body
    lines, then ``%end <time> <num> <flags>`` -- or ``%error ...`` in place of
    the ``%end`` when the command failed. Blocks never interleave, and no
    notification arrives inside a block.
  - Notifications are lines starting with ``%``: ``%output <pane> <value>``,
    ``%extended-output <pane> <age> <flags> : <value>``, ``%pause <pane>``,
    ``%continue <pane>``, ``%exit [reason]``, ``%session-changed <id> <name>``,
    ``%layout-change ...``, ``%window-pane-changed ...``, others. In an output
    ``value`` every non-printable byte (and backslash) is octal-escaped as
    ``\\ooo``, so the value itself never contains whitespace or a newline.

Invariant #3 (CLAUDE.md) lives in ``send_command``: user strings never reach a
control line. Every plain ``str`` argument must match ``ARG_SAFE``; module-owned
format strings are ``Fmt`` constants, so there is no ambient way to interpolate.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import sessions

log = logging.getLogger("serai.tmuxctl")

# tmux escapes non-printables (and backslashes) as octal \ooo in output values.
_OCTAL_ESCAPE = re.compile(rb"\\([0-7]{3})")

# What a caller-supplied argument may contain: option names (-S), negative
# numbers (-200), pane/session ids (%3, $1, @2), sizes (120x40), settings
# (pause-after=3), format literals (#{pane_id}). Notably absent: whitespace,
# ; | & ' " $() and every control byte -- i.e. nothing that could split or
# extend a control line.
ARG_SAFE = re.compile(r"^[A-Za-z0-9_@%$:=,.+/#{}?!-]+$")

_ON_EXIT = Callable[[str], None]


class Fmt(str):
    """A module-owned control-mode *format string* (may contain spaces and
    quotes), exempt from ``ARG_SAFE``. Only ever constructed from constants in
    this module -- never from caller or user text. The one producer here is
    :func:`_pane_state`, which validates both pieces before quoting."""


# Pane states accepted by `refresh-client -A <pane>:<state>` (tmux 3.6).
_PANE_STATES = ("on", "off", "continue", "pause")
_PANE_ID = re.compile(r"^%[0-9]+$")


def _pane_state(pane: str, state: str) -> Fmt:
    """The quoted ``'<pane>:<state>'`` token for ``refresh-client -A``.

    tmux 3.6 measures it this way: the argument *must* be quoted -- bare
    ``%0:continue`` is a parse error -- and quoting is what takes it outside
    ARG_SAFE's charset, so it goes through ``Fmt``. That is safe only because
    both pieces are validated here: ``pane`` is a pure ``%<digits>`` pane id
    and ``state`` is one of four literals; anything else raises ValueError."""
    if not _PANE_ID.match(pane):
        raise ValueError(f"not a pane id: {pane!r}")
    if state not in _PANE_STATES:
        raise ValueError(f"not a pane state: {state!r}")
    return Fmt(f"'{pane}:{state}'")


def unescape_output(value: bytes) -> bytes:
    """Decode a control-mode output value: each ``\\ooo`` becomes the byte it
    escapes. A backslash not followed by three octal digits is left as-is."""
    return _OCTAL_ESCAPE.sub(lambda m: bytes([int(m.group(1), 8)]), value)


def hex_chunks(data: bytes, size: int = 1024) -> list[str]:
    """Split ``data`` into runs of at most ``size`` bytes, each rendered as
    space-separated two-digit lowercase hex (``"68 65 6c"``). Feeds tmux's
    ``send-keys -H``, which takes space-separated hex. ``b""`` -> ``[]``."""
    if size < 1:
        raise ValueError("size must be >= 1")
    return [data[i:i + size].hex(" ") for i in range(0, len(data), size)]


@dataclass
class Line:
    """One parsed control-mode line. Field meanings follow ``kind``:

    - ``"begin"``/``"end"``/``"error"``: block framing (``time``/``num``/``flags``).
    - ``"notify"``: a notification -- ``name`` without the ``%``, ``args`` as raw
      byte tokens, ``value`` decoded via :func:`unescape_output` for
      output/extended-output (``b""`` for every other notification).
    - ``"body"``: a line inside a block (``raw`` holds it verbatim).
    """

    kind: str
    time: int = 0
    num: int = 0
    flags: int = 0
    name: str = ""
    args: list[bytes] = field(default_factory=list)
    value: bytes = b""
    raw: bytes = b""


def _int_arg(args: list[bytes], i: int) -> int:
    try:
        return int(args[i])
    except (IndexError, ValueError):
        return 0


def parse_line(line: bytes, in_block: bool) -> Line:
    """Classify one control-mode line. Inside a block only ``%end``/``%error``
    are special -- tmux output values are escaped and body data reaches us
    between blocks -- so anything else in a block is ``body`` verbatim."""
    if in_block and not (line.startswith(b"%end") or line.startswith(b"%error")):
        return Line(kind="body", raw=line)
    if line.startswith(b"%begin"):
        a = line.split()
        return Line(kind="begin", time=_int_arg(a, 1), num=_int_arg(a, 2),
                    flags=_int_arg(a, 3))
    if line.startswith(b"%end") or line.startswith(b"%error"):
        a = line.split()
        return Line(kind=line[1:].split(b" ", 1)[0].decode(errors="replace"),
                    time=_int_arg(a, 1), num=_int_arg(a, 2), flags=_int_arg(a, 3))
    if line.startswith(b"%"):
        name = line[1:].split(None, 1)[0].decode(errors="replace")
        if name == "extended-output":
            # `%extended-output <pane> <age> <flags...> : <value>` -- everything
            # after the FIRST " : " is the value (it can contain " : " itself).
            head, _, rest = line.partition(b" : ")
            return Line(kind="notify", name=name, args=head.split()[1:],
                        value=unescape_output(rest))
        if name == "output":
            # Maxsplit on single spaces: keeps the value verbatim (tmux escapes
            # every byte < 0x21, so a value never holds whitespace anyway).
            parts = line.split(b" ", 2)
            return Line(kind="notify", name=name, args=parts[1:2],
                        value=unescape_output(parts[2]) if len(parts) > 2 else b"")
        return Line(kind="notify", name=name, args=line.split()[1:])
    # Outside any block tmux only ever speaks %-lines; a stray line is still
    # data, not framing.
    return Line(kind="body", raw=line)


class ControlError(Exception):
    """A command's reply block ended with %error; the message is the block body."""


class ControlClosed(Exception):
    """The control connection ended (EOF / %exit) while a command was pending,
    or the client could not complete a handshake."""


def _mux_dir() -> str:
    """Per-user ssh multiplexing dir for control-mode connections, created 0700
    on first use."""
    base = os.environ.get("XDG_RUNTIME_DIR") or os.path.expanduser("~/.config/serai")
    path = os.path.join(base, "serai-ssh")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def control_argv(host: str, name: str, pause_after: int = 3) -> list[str]:
    """Argv for a control-mode client attached to session ``name``.

    ``-f pause-after=N`` arms tmux's flow control: after N bytes of un-acked
    pane output tmux pauses the pane (``%pause``) instead of letting the buffer
    grow unbounded; we resume it with :meth:`ControlClient.continue_pane`
    (``refresh-client -A '<pane>:continue'``) once we have drained.

    Local is plain ``tmux -C``. Remote wraps the same command in ssh (invariant
    #1: BatchMode via ``sessions._SSH_OPTS``, ssh-agent only) and hands the
    connection to a persisted multiplexed master (ControlPersist=600) so
    reconnects don't re-pay the handshake -- ``SERAI_SSH_MUX=0`` drops that.
    ``-t =name`` is tmux's exact-match form, so a prefix can't steal the attach.
    """
    tmux_cmd = ["tmux", "-C", "attach-session", "-f",
                f"pause-after={int(pause_after)}", "-t", "=" + name]
    if not host or host == "local":
        return tmux_cmd
    opts = [*sessions._SSH_OPTS]
    if os.environ.get("SERAI_SSH_MUX", "1") != "0":
        opts += ["-o", "ControlMaster=auto",
                 "-o", f"ControlPath={_mux_dir()}/%C",
                 "-o", "ControlPersist=600"]
    opts += ["-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=2"]
    remote = " ".join(shlex.quote(p) for p in tmux_cmd)
    return ["ssh", *opts, host, remote]


class ControlClient:
    """An asyncio client for one ``tmux -C`` process (local, or remote over ssh).

    Lifecycle: construct, ``await start()`` (waits for the ``%session-changed``
    that follows the initial attach block), then ``await send_command(...)`` for
    framed commands and ``subscribe(...)`` for notifications. ``closed`` resolves
    with the ``%exit`` reason (or ``"eof"``) when the process goes away.

    Output flow: subscribers to ``"output"``/``"extended-output"`` receive the
    live pane stream -- ``line.value`` is the pane's raw bytes, already decoded
    by the parser (that is what a renderer feeds on). The framing signals on
    the same channel are ``%pause``/``%continue`` (the pause-after watermark,
    answered with :meth:`continue_pane`), ``%session-changed``, and ``%exit``.
    Byte-exact replies to *commands* (``capture-pane -p`` bodies) come back
    from :meth:`send_command`, not from subscriptions.
    """

    def __init__(self, argv: list[str], *, cmd_timeout: float = 10.0,
                 stream_limit: int = 8 * 1024 * 1024) -> None:
        self.argv = list(argv)
        self.cmd_timeout = cmd_timeout
        self.stream_limit = stream_limit
        self.session_id: str | None = None
        self.session_name: str | None = None
        self.stderr_tail: bytes = b""
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._pending: collections.deque[asyncio.Future] = collections.deque()
        self._subs: dict[str, list[Callable[[Line], None]]] = {}
        self._in_block = False
        self._block_body: list[bytes] = []
        self._closed_fut: asyncio.Future | None = None
        self._pause_panes: set[bytes] = set()
        self._on_exit_cb: _ON_EXIT | None = None
        self._ready: asyncio.Future | None = None
        self._ready_msg: str | None = None

    # -- public state ---------------------------------------------------------

    @property
    def closed(self) -> asyncio.Future:
        """Resolves once with the ``%exit`` reason text, or ``"eof"``."""
        return self._ensure_closed()

    def subscribe(self, name: str,
                  cb: Callable[[Line], None]) -> Callable[[], None]:
        """Register ``cb`` for notifications named ``name`` (``"*"`` for all).
        Returns the unsubscribe function. Callbacks run on the reader task; a
        raising callback is logged, never allowed to kill the reader."""
        subs = self._subs.setdefault(name, [])
        subs.append(cb)

        def _off() -> None:
            try:
                subs.remove(cb)
            except ValueError:
                pass

        return _off

    def on_exit(self, cb: _ON_EXIT) -> None:
        """Last-resort hook fired (sync, from the reader) when the process dies:
        ``cb(reason)``. Only fires if the client itself didn't call ``close()``.
        This is the server-side reconnect trigger; it must stay sync so it
        works even when the client is mid-teardown."""
        self._on_exit_cb = cb

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self._ensure_closed()
        if self._ready is None:
            self._ready = loop.create_future()
        self._proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.stream_limit,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read_loop())
        try:
            await asyncio.wait_for(self._ready, self.cmd_timeout)
        except asyncio.TimeoutError:
            await self.close()
            raise ControlClosed(
                self._with_stderr("tmux control mode never announced a session"))
        if self._ready_msg:
            await self.close()
            raise ControlClosed(self._with_stderr(self._ready_msg))

    def _with_stderr(self, msg: str) -> str:
        """Attach the captured stderr tail: ssh and tmux print the reason a
        remote attach failed there, and a bare \"eof\" would hide it."""
        tail = self.stderr_tail.decode(errors="replace").strip()
        return f"{msg} (stderr: {tail})" if tail else msg

    async def _drain_stderr(self) -> None:
        """Keep the last 2 KiB of the child's stderr in ``stderr_tail``. The
        pipe must be drained or a chatty ssh could block on a full one."""
        try:
            while self._proc is not None and self._proc.stderr is not None:
                chunk = await self._proc.stderr.read(4096)
                if not chunk:
                    break
                self.stderr_tail = (self.stderr_tail + chunk)[-2048:]
        except Exception:
            pass

    async def send_command(self, *args: str) -> list[bytes]:
        """Write one command line, await its reply block, return the body lines.

        Replies arrive in command order, so a FIFO of futures matches blocks to
        commands. Raises ``ValueError`` for any plain ``str`` arg that fails
        ``ARG_SAFE`` (invariant #3) -- wrap intended format literals in ``Fmt``.
        ``ControlError`` when the block ends ``%error``; ``ControlClosed`` when
        the connection dies or the reply outlives ``cmd_timeout``.
        """
        for a in args:
            if isinstance(a, Fmt):
                continue
            if not isinstance(a, str) or not ARG_SAFE.match(a):
                raise ValueError(f"unsafe control-mode argument: {a!r}")
        if self._proc is None or self._proc.stdin is None:
            raise ControlClosed("control client is not started")
        if self.closed.done():
            raise ControlClosed("control connection is closed")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending.append(fut)
        try:
            self._proc.stdin.write(" ".join(args).encode() + b"\n")
            await self._proc.stdin.drain()
        except (ConnectionResetError, BrokenPipeError, OSError) as exc:
            self._discard(fut)
            raise ControlClosed(f"control stdin failed: {exc}") from exc
        try:
            return await asyncio.wait_for(fut, self.cmd_timeout)
        except asyncio.TimeoutError:
            self._discard(fut)
            await self.close()
            raise ControlClosed(f"command timed out: {args[0] if args else ''}")
        except ControlClosed:
            self._discard(fut)
            raise

    async def close(self) -> None:
        """Close stdin (control mode exits on EOF), wait <=2 s, then terminate.
        Pending commands and an unfinished handshake fail with ControlClosed."""
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(ControlClosed("client closed"))
        self._fail_pending(ControlClosed("client closed"))
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
                await asyncio.shield(self._wait_closed(proc))
                if proc.returncode is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                    await asyncio.shield(self._wait_closed(proc))
            else:
                await asyncio.shield(self._wait_closed(proc))
        closed = self._ensure_closed()
        if not closed.done():
            closed.set_result("eof")
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._stderr_task), 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._stderr_task.cancel()
            self._stderr_task = None
        if self._reader is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._reader), 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._reader.cancel()
            self._reader = None

    async def _wait_closed(self, proc: asyncio.subprocess.Process,
                           timeout: float = 2.0) -> None:
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    # -- internals ------------------------------------------------------------

    def _ensure_closed(self) -> asyncio.Future:
        if self._closed_fut is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                raise RuntimeError(
                    "ControlClient.closed needs a running event loop") from None
            self._closed_fut = loop.create_future()
        return self._closed_fut

    def _fail_pending(self, exc: BaseException) -> None:
        while self._pending:
            fut = self._pending.popleft()
            if not fut.done():
                fut.set_exception(exc)

    def _discard(self, fut: asyncio.Future) -> None:
        """A future whose wait failed is no longer being awaited; drop it from
        the FIFO so it neither leaks its exception nor misaligns later replies.
        A block that arrives for it later is dropped as a no-pending block."""
        try:
            self._pending.remove(fut)
        except ValueError:
            pass
        if fut.done() and not fut.cancelled():
            fut.exception()  # marks retrieved; quiets "exception never retrieved"

    def _set_closed(self, reason: str) -> None:
        fut = self._ensure_closed()
        if not fut.done():
            fut.set_result(reason)

    def _notify(self, line: Line) -> None:
        if line.name == "session-changed" and len(line.args) >= 1:
            self.session_id = line.args[0].decode(errors="replace")
            self.session_name = b" ".join(line.args[1:]).decode(errors="replace")
            if self._ready is not None and not self._ready.done():
                self._ready.set_result(None)
        elif line.name == "pause":
            if line.args:
                self._pause_panes.add(line.args[0])
        elif line.name == "continue" and line.args:
            self._pause_panes.discard(line.args[0])
        for cb in list(self._subs.get(line.name, ())) + list(self._subs.get("*", ())):
            try:
                cb(line)
            except Exception:
                log.exception("control-mode subscriber %r failed", line.name)

    async def _read_loop(self) -> None:
        reason = "eof"
        stdout = self._proc.stdout if self._proc is not None else None
        try:
            if stdout is None:
                raise EOFError("reader started without a process")
            while True:
                raw = await stdout.readline()
                if not raw:
                    break
                self._handle(parse_line(raw.rstrip(b"\r\n"), self._in_block))
        except asyncio.CancelledError:
            raise
        except EOFError:
            pass  # %exit (or the not-started guard); closed already set
        except (asyncio.LimitOverrunError, ValueError) as exc:
            # A single line longer than stream_limit; the framing is gone.
            log.warning("control-mode read failed: %s", exc)
            reason = "stream limit exceeded"
        except Exception as exc:  # socket died under us
            log.warning("control-mode read failed: %s", exc)
            reason = str(exc) or "read error"
        finally:
            if self._in_block and self._pending:
                fut = self._pending.popleft()
                if not fut.done():
                    fut.set_exception(
                        ControlError(b"\n".join(self._block_body)
                                     .decode(errors="replace")))
            self._in_block = False
            self._block_body = []
            self._set_closed(reason)
            self._fail_pending(ControlClosed(reason))
            if self._ready is not None and not self._ready.done():
                self._ready_msg = reason
                self._ready.set_result(None)
            cb, self._on_exit_cb = self._on_exit_cb, None
            if cb is not None:
                try:
                    cb(reason)
                except Exception:
                    log.exception("on_exit callback failed")

    def _handle(self, line: Line) -> None:
        if line.kind == "begin":
            if self._in_block:
                log.warning("nested %%begin (num=%s) -- dropping previous block",
                            line.num)
                if self._pending:
                    fut = self._pending.popleft()
                    if not fut.done():
                        fut.set_exception(
                            ControlError(b"\n".join(self._block_body)
                                         .decode(errors="replace")))
            self._in_block = True
            self._block_body = []
            return
        if line.kind in ("end", "error"):
            if not self._in_block:
                log.warning("%s outside a block (num=%s) -- ignoring",
                            line.kind, line.num)
                return
            body, self._block_body = self._block_body, []
            self._in_block = False
            if self._pending:
                fut = self._pending.popleft()
                if not fut.done():
                    if line.kind == "error":
                        fut.set_exception(
                            ControlError(b"\n".join(body)
                                         .decode(errors="replace")))
                    else:
                        fut.set_result(body)
            # else: the initial attach block (or one whose wait already timed
            # out) -- drop it silently.
            return
        if line.kind == "body":
            if self._in_block:
                self._block_body.append(line.raw)
            return
        # notify: %exit terminates the stream (tmux docs); nothing follows.
        if line.name == "exit":
            self._set_closed(b" ".join(line.args).decode(errors="replace") or "exit")
            raise EOFError
        self._notify(line)

    async def send_pane(self, pane: str, data: bytes) -> None:
        """Send raw ``data`` to ``pane`` byte-exactly via ``send-keys -H`` hex
        chunks. Nothing here can re-encode the bytes, so the TUI sees exactly
        what the client sent -- the property PTY paths kept losing."""
        for chunk in hex_chunks(data):
            # Each byte as its own argument ("send-keys -H 48 65 ..."): the
            # chunks are user data, so they ride ARG_SAFE, not Fmt.
            await self.send_command("send-keys", "-H", "-t", pane, *chunk.split())

    async def capture(self, pane: str, start: int | str = -2000,
                      end: int | str = "-", **flags: bool) -> list[bytes]:
        """``capture-pane -p`` reply body for ``pane``. ``-S``/``-E`` are always
        sent -- a literal ``-`` is a valid value there (start of history / end
        of the visible pane). Keyword booleans add plain flags (``e=True`` ->
        ``-e``)."""
        args: list[str] = ["capture-pane", "-p", "-t", pane,
                           "-S", str(start), "-E", str(end)]
        for name, on in flags.items():
            if on:
                args.append("-" + name)
        return await self.send_command(*args)

    async def continue_pane(self, pane: str) -> None:
        """Resume output to a paused pane: ``refresh-client -A '<pane>:continue'``
        (the tmux 3.6 control-mode flow-control resume; the token must be
        quoted, see :func:`_pane_state`). Clears the locally-observed pause mark
        whether the command succeeds or tmux rejects it (e.g. the pane is
        gone), then re-raises."""
        try:
            await self.send_command("refresh-client", "-A",
                                    _pane_state(pane, "continue"))
        except ControlError:
            self._pause_panes.discard(pane.encode())
            raise
        self._pause_panes.discard(pane.encode())

    async def pause_pane(self, pane: str) -> None:
        """Stop output to a pane: ``refresh-client -A '<pane>:pause'``."""
        await self.send_command("refresh-client", "-A",
                                _pane_state(pane, "pause"))

    async def set_pane_output(self, pane: str, on: bool) -> None:
        """Enable/disable a pane's output: state ``on``/``off``."""
        await self.send_command("refresh-client", "-A",
                                _pane_state(pane, "on" if on else "off"))
