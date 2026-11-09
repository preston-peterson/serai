"""The attach bridge: one tmux control-mode client <-> one browser terminal.

v3 replaces the PTY bridge. Under a PTY the browser was the *outer terminal* of
a tmux client: tmux repainted into it, kept it on the alternate screen, and
owned scrollback, selection and the mouse -- the root of the copy/paste pain.
Here tmux streams each pane's raw output (``%output``), so xterm.js receives
exactly what the program wrote and owns its own buffer, selection, scrollback
and alt-screen state. Keystrokes go back byte-exact via ``send-keys -H``.

Wire protocol on the websocket (unchanged transport, new meaning):

  binary, both ways   pane bytes (browser -> server: input to the pane)
  text, server->      {"seed":"begin","cols":C,"rows":R}  reset, resize to C x R
                      {"seed":"end"}                      the snapshot is complete
                      {"size":{"cols":C,"rows":R}}        the pane is not the
                                                          browser's size; adopt it
                      {"ping":1}                          keepalive
  text, ->server      {"resize":{"cols":C,"rows":R}}      the browser's size
                      {"ping":1}                          keepalive (ignored)

The snapshot ("seed") is one atomic control line -- state, history, live grid
and the saved normal screen -- so no pane output can land between its parts
(tmux runs a line's commands in one loop iteration). Output that arrives while
the seed is in flight is held and split at the last reply's ``seq``: what came
before is already in the snapshot, what came after is sent after it.

Backpressure: pane output queued for the socket is counted; past the
high-water mark the bridge pauses the pane in tmux (``refresh-client -A
'%N:pause'``), drops what is still in flight, waits until the socket has
drained, then continues the pane and re-seeds in the same atomic line. tmux's
own ``pause-after`` ``%pause`` takes the same path. A re-seed is the only
correct recovery once output was dropped; it costs one snapshot, never memory.
The reader keeps reading throughout, so keystrokes and command replies are
never stuck behind a slow browser.

Nothing user-supplied reaches a control line here: pane ids come from tmux,
sizes are ints, and the input bytes travel as hex (invariant #3, enforced again
by ``tmuxctl.ARG_SAFE``).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass
from typing import Awaitable, Callable

from .tmuxctl import ControlClient, ControlClosed, ControlError, Fmt, Line, _pane_state

log = logging.getLogger("serai.bridge")

# Everything the seed needs to rebuild a pane's terminal state, in one reply.
# Order matters: PaneState.parse reads it positionally; cursor_shape is the one
# word (the rest are ints) and goes last.
STATE_FMT = Fmt(
    "'#{alternate_on} #{history_size} #{cursor_x} #{cursor_y} #{cursor_flag} "
    "#{insert_flag} #{keypad_cursor_flag} #{keypad_flag} #{wrap_flag} #{origin_flag} "
    "#{mouse_standard_flag} #{mouse_button_flag} #{mouse_all_flag} #{mouse_utf8_flag} "
    "#{mouse_sgr_flag} #{scroll_region_upper} #{scroll_region_lower} "
    "#{pane_width} #{pane_height} #{cursor_blinking} #{cursor_shape}'")
PANES_FMT = Fmt("'#{pane_id} #{pane_active}'")
SIZE_FMT = Fmt("'#{pane_width} #{pane_height}'")

HIGH_WATER = 1 << 20          # pane bytes queued for the socket before we pause
COALESCE = 64 * 1024          # merge queued output into frames up to this size
MAX_DIM = 1000                # cols/rows clamp for anything the browser sends
LAYOUT_DEBOUNCE = 0.1         # seconds to let a burst of %layout-change settle

# DECSCUSR base per tmux cursor_shape; +1 is the steady (non-blinking) variant.
_SHAPES = {"block": 1, "underline": 3, "bar": 5}


@dataclass
class PaneState:
    alt: bool
    hist: int
    cx: int
    cy: int
    cursor: bool
    insert: bool
    keypad_cursor: bool
    keypad: bool
    wrap: bool
    origin: bool
    mouse_standard: bool
    mouse_button: bool
    mouse_all: bool
    mouse_utf8: bool
    mouse_sgr: bool
    scroll_upper: int
    scroll_lower: int
    width: int
    height: int
    blink: bool
    shape: str

    @classmethod
    def parse(cls, raw: bytes) -> "PaneState":
        parts = raw.decode(errors="replace").split()
        if len(parts) != 21:
            raise ValueError(f"unexpected pane state: {raw!r}")
        n = [int(x) for x in parts[:20]]
        return cls(bool(n[0]), n[1], n[2], n[3], bool(n[4]), bool(n[5]), bool(n[6]),
                   bool(n[7]), bool(n[8]), bool(n[9]), bool(n[10]), bool(n[11]),
                   bool(n[12]), bool(n[13]), bool(n[14]), n[15], n[16], n[17], n[18],
                   bool(n[19]), parts[20])


def build_seed(st: PaneState, history: list[bytes], live: list[bytes],
               saved: list[bytes]) -> bytes:
    """The bytes that rebuild a pane in a freshly reset terminal of its size.

    Measured on tmux 3.6 (spike 3b): with the alternate screen live, the plain
    capture is the *alt* grid and ``-a`` is the saved normal screen. History is
    captured with ``-J`` (wrapped lines rejoined, so xterm can reflow them);
    the grids without it, one row per line, so row counts stay exact.
    """
    # capture-pane clamps "-E -1" to the first visible line when a pane has no
    # history at all, so that reply is only history if history exists.
    hist = history if st.hist > 0 else []
    if st.alt:
        normal, alt = hist + saved, live
    else:
        normal, alt = hist + live, []
    buf = bytearray(b"\r\n".join(normal))
    if st.alt:
        buf += b"\x1b[?1049h\x1b[H" + b"\r\n".join(alt)
    # Modes, then the cursor. DECSTBM homes the cursor and so does DECOM, so
    # both come before the CUP; under DECOM the CUP row is margin-relative.
    if not (st.scroll_upper == 0 and st.scroll_lower == st.height - 1):
        buf += f"\x1b[{st.scroll_upper + 1};{st.scroll_lower + 1}r".encode()
    if st.origin:
        buf += b"\x1b[?6h"
    if not st.wrap:
        buf += b"\x1b[?7l"
    if st.insert:
        buf += b"\x1b[4h"
    if st.keypad_cursor:
        buf += b"\x1b[?1h"
    if st.keypad:
        buf += b"\x1b="
    for on, mode in ((st.mouse_standard, 1000), (st.mouse_button, 1002),
                     (st.mouse_all, 1003), (st.mouse_utf8, 1005), (st.mouse_sgr, 1006)):
        if on:
            buf += f"\x1b[?{mode}h".encode()
    if st.shape in _SHAPES:
        buf += f"\x1b[{_SHAPES[st.shape] + (0 if st.blink else 1)} q".encode()
    row = st.cy - st.scroll_upper if st.origin else st.cy
    buf += f"\x1b[{row + 1};{st.cx + 1}H".encode()
    if not st.cursor:
        buf += b"\x1b[?25l"
    return bytes(buf)


def clamp_dim(value: object, default: int) -> int:
    try:
        return max(2, min(int(value), MAX_DIM))
    except (TypeError, ValueError):
        return default


_LIVE, _SEEDING, _RESYNC = "live", "seeding", "resync"


class Bridge:
    """Pumps one session's active pane to one browser terminal.

    The caller starts the control client, constructs this, ``await start()``,
    then runs :meth:`writer` and :meth:`maintain` as tasks for the bridge's
    lifetime and feeds browser frames to :meth:`input` / :meth:`resize`.
    """

    def __init__(self, client: ControlClient,
                 send_bytes: Callable[[bytes], Awaitable[None]],
                 send_text: Callable[[str], Awaitable[None]],
                 *, cols: int = 80, rows: int = 24, history: int = 10000,
                 high_water: int = HIGH_WATER) -> None:
        self._client = client
        self._send_bytes = send_bytes
        self._send_text = send_text
        self._cols, self._rows = cols, rows
        self._history = max(0, int(history))
        self._high_water = high_water
        self._pane: str | None = None
        self._pane_b = b""
        self._state = _LIVE
        self._held: list[tuple[int, bytes | None]] = []   # None = a %pause mark
        self._out: deque[tuple[bytes | str, int]] = deque()
        self._queued = 0                       # live pane bytes in self._out
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._jobs: asyncio.Queue[str] = asyncio.Queue()
        self._layout_timer: asyncio.TimerHandle | None = None
        self._unsubs: list[Callable[[], None]] = []
        self._sid: str | None = None           # our session's id, fixed at start
        self.ended: str | None = None          # why the bridge stopped on its own

    @property
    def pane(self) -> str | None:
        return self._pane

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        c = self._client
        self._sid = c.session_id
        for name, cb in (("output", self._on_output),
                         ("extended-output", self._on_output),
                         ("pause", self._on_pause),
                         ("layout-change", self._on_layout),
                         ("window-pane-changed", self._on_retarget),
                         ("session-window-changed", self._on_retarget),
                         ("session-changed", self._on_session_changed)):
            self._unsubs.append(c.subscribe(name, cb))
        self._set_pane(await self._active_pane())
        await self.resize(self._cols, self._rows)
        await self._seed()

    def close(self) -> None:
        for off in self._unsubs:
            off()
        self._unsubs.clear()
        if self._layout_timer is not None:
            self._layout_timer.cancel()

    # -- browser -> tmux ------------------------------------------------------

    async def input(self, data: bytes) -> None:
        if not data or self._pane is None:
            return
        try:
            await self._client.send_pane(self._pane, data)
        except ControlError:
            self._jobs.put_nowait("retarget")   # the pane went away under us

    async def resize(self, cols: object, rows: object) -> None:
        self._cols = clamp_dim(cols, self._cols)
        self._rows = clamp_dim(rows, self._rows)
        # refresh-client -C, never resize-window: the latter flips the window
        # to window-size manual for good, a footprint on the user's session.
        await self._client.send_command("refresh-client", "-C",
                                        f"{self._cols}x{self._rows}")

    def ping(self) -> None:
        self._put('{"ping":1}')

    # -- tmux -> browser ------------------------------------------------------

    async def writer(self) -> None:
        """Drain the outbound queue to the socket, coalescing pane bytes."""
        while True:
            if not self._out:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            item, counted = self._out.popleft()
            if isinstance(item, str):
                await self._send_text(item)
                continue
            buf = bytearray(item)
            while (self._out and not isinstance(self._out[0][0], str)
                   and len(buf) < COALESCE):
                nxt, c = self._out.popleft()
                buf += nxt
                counted += c
            try:
                await self._send_bytes(bytes(buf))
            finally:
                self._queued -= counted

    async def maintain(self) -> None:
        """Serialise every re-seed: overflow, a tmux %pause, a pane switch and
        a size check each queue a job here, so two can never interleave."""
        while True:
            job = await self._jobs.get()
            try:
                if job == "resync":
                    await self._resync()
                elif job == "retarget":
                    await self._retarget()
                elif job == "size":
                    await self._size_check()
            except ControlClosed:
                return
            except ControlError as exc:
                log.info("bridge %s job failed: %s", job, exc)
            except Exception:
                # Never let one bad reply end the job loop: the next resync or
                # retarget must still run, or the pane would stay paused.
                log.exception("bridge %s job crashed", job)

    # -- internals ------------------------------------------------------------

    def _put(self, item: bytes | str, counted: int = 0) -> None:
        self._out.append((item, counted))
        self._queued += counted
        self._idle.clear()
        self._wake.set()

    def _set_pane(self, pane: str) -> None:
        self._pane = pane
        self._pane_b = pane.encode()

    async def _active_pane(self) -> str:
        sid = self._sid or self._client.session_id
        if not sid:
            raise ControlClosed("no session")
        rows = await self._client.send_command("list-panes", "-t", sid, "-F", PANES_FMT)
        panes = [r.split() for r in rows if r.strip()]
        for p in panes:
            if len(p) >= 2 and p[1] == b"1":
                return p[0].decode()
        if not panes:
            raise ControlError("session has no panes")
        return panes[0][0].decode()

    def _on_output(self, line: Line) -> None:
        if not line.args or line.args[0] != self._pane_b:
            return
        if self._state == _RESYNC:
            return                       # a re-seed is coming; this is in it
        if self._state == _SEEDING:
            self._held.append((self._client.notify_seq, line.value))
            return
        self._put(line.value, len(line.value))
        if self._queued > self._high_water:
            self._begin_resync()

    def _on_pause(self, line: Line) -> None:
        if not line.args or line.args[0] != self._pane_b:
            return
        if self._state == _SEEDING:
            self._held.append((self._client.notify_seq, None))
        elif self._state == _LIVE:
            self._begin_resync()

    def _begin_resync(self) -> None:
        self._state = _RESYNC
        self._jobs.put_nowait("resync")

    def _on_layout(self, line: Line) -> None:
        if self._layout_timer is not None:
            self._layout_timer.cancel()
        loop = asyncio.get_running_loop()
        self._layout_timer = loop.call_later(
            LAYOUT_DEBOUNCE, self._jobs.put_nowait, "size")

    def _on_retarget(self, line: Line) -> None:
        self._jobs.put_nowait("retarget")

    def _on_session_changed(self, line: Line) -> None:
        # The client was moved to another session: ours was destroyed and tmux
        # (detach-on-destroy off) switched us. Never stream a different session.
        # (The client records the new id before subscribers run, so compare
        # with the one captured at start.)
        sid = line.args[0].decode(errors="replace") if line.args else ""
        if sid != self._sid:
            self.ended = "session-changed"
            asyncio.ensure_future(self._client.close())

    async def _resync(self) -> None:
        if self._state != _RESYNC or self._pane is None:
            return
        try:
            await self._client.pause_pane(self._pane)
        except ControlError:
            pass
        await self._idle.wait()          # everything already queued has gone out
        await self._seed()

    async def _retarget(self) -> None:
        pane = await self._active_pane()
        if pane != self._pane:
            self._set_pane(pane)
            await self._seed()

    async def _size_check(self) -> None:
        if self._pane is None:
            return
        (raw,) = await self._client.send_command(
            "display-message", "-p", "-t", self._pane, SIZE_FMT)
        w, h = (int(x) for x in raw.split())
        if (w, h) != (self._cols, self._rows):
            # Smaller than the browser (a split made outside serai) or another
            # client won "latest": the browser adopts the pane's real size.
            self._cols, self._rows = w, h
            self._put(json.dumps({"size": {"cols": w, "rows": h}}))

    async def _seed(self) -> None:
        pane = self._pane
        if pane is None:
            return
        self._state = _SEEDING
        self._held = []
        hist = max(1, self._history)
        # Every seed starts by un-pausing the pane (an empty reply when it is
        # not paused). Whoever paused it -- our overflow, tmux's pause-after, a
        # %pause that landed before this snapshot -- the snapshot supersedes
        # what was dropped, and output must flow again after it.
        cmds: list[tuple[str, ...]] = [
            ("refresh-client", "-A", _pane_state(pane, "continue")),
            ("display-message", "-p", "-t", pane, STATE_FMT),
            ("capture-pane", "-p", "-e", "-J", "-q", "-t", pane, "-S", str(-hist), "-E", "-1"),
            ("capture-pane", "-p", "-e", "-q", "-t", pane, "-S", "0", "-E", "-"),
            ("capture-pane", "-p", "-e", "-q", "-a", "-t", pane, "-S", "0", "-E", "-"),
        ]
        try:
            replies = await self._client.send_batch(*cmds)
        except ControlError:
            self._state = _LIVE
            self._held = []
            self._jobs.put_nowait("retarget")    # the pane vanished mid-seed
            return
        state_r, hist_r, live_r, saved_r = replies[-4:]
        st = PaneState.parse(state_r[0] if state_r else b"")
        history = list(hist_r) if self._history > 0 else []
        self._cols, self._rows = st.width, st.height
        self._put(json.dumps({"seed": "begin", "cols": st.width, "rows": st.height}))
        self._put(build_seed(st, history, list(live_r), list(saved_r)))
        self._put('{"seed":"end"}')
        cut = replies[-1].seq
        held, self._held = self._held, []
        self._state = _LIVE
        for seq, value in held:
            if seq <= cut:
                continue                  # already in the snapshot
            if value is None:             # tmux paused us after the snapshot
                self._begin_resync()
                return
            self._put(value, len(value))
        if self._queued > self._high_water:
            self._begin_resync()
