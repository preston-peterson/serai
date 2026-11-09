"""The v3 attach bridge: seed construction, the snapshot/stream split, and
backpressure -- against a scripted fake client, and end to end against a real
tmux server on a throwaway socket (never the user's)."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import time

import pytest

from serai import bridge as br
from serai.tmuxctl import ControlClient, ControlError, Line, Reply


def run(coro):
    return asyncio.run(asyncio.wait_for(coro, 20))


def _state(**kw) -> br.PaneState:
    base = dict(alt=False, hist=0, cx=0, cy=0, cursor=True, insert=False,
                keypad_cursor=False, keypad=False, wrap=True, origin=False,
                mouse_standard=False, mouse_button=False, mouse_all=False,
                mouse_utf8=False, mouse_sgr=False, scroll_upper=0, scroll_lower=23,
                width=80, height=24, blink=False, shape="default")
    base.update(kw)
    return br.PaneState(**base)


# --- pure pieces ------------------------------------------------------------


def test_pane_state_parses_the_format_reply():
    st = br.PaneState.parse(b"1 120 5 7 0 0 1 0 1 0 1 0 0 0 1 0 39 100 40 1 bar")
    assert st.alt and st.hist == 120 and (st.cx, st.cy) == (5, 7)
    assert not st.cursor and st.keypad_cursor and st.mouse_standard and st.mouse_sgr
    assert (st.width, st.height, st.blink, st.shape) == (100, 40, True, "bar")
    with pytest.raises(ValueError):
        br.PaneState.parse(b"1 2 3")


def test_seed_normal_screen_is_history_then_grid_then_cursor():
    seed = br.build_seed(_state(hist=2, cx=3, cy=1), [b"h1", b"h2"], [b"$ ls", b"$ "], [b""])
    assert seed == b"h1\r\nh2\r\n$ ls\r\n$ \x1b[2;4H"


def test_seed_ignores_the_history_reply_when_the_pane_has_none():
    # tmux clamps "-E -1" to the first visible line on a fresh pane; using it
    # would print that line twice.
    seed = br.build_seed(_state(hist=0), [b"$ "], [b"$ ", b""], [b""])
    assert seed.startswith(b"$ \r\n\x1b[")


def test_seed_alt_screen_restores_saved_normal_under_the_live_grid():
    seed = br.build_seed(_state(alt=True, hist=1), [b"old"], [b"ALT-1", b"ALT-2"],
                         [b"normal-1", b"normal-2"])
    assert seed.startswith(b"old\r\nnormal-1\r\nnormal-2\x1b[?1049h\x1b[HALT-1\r\nALT-2")


def test_seed_replays_modes_before_the_cursor():
    seed = br.build_seed(
        _state(cx=0, cy=10, cursor=False, wrap=False, insert=True, keypad_cursor=True,
               keypad=True, mouse_button=True, mouse_sgr=True, scroll_upper=2,
               scroll_lower=20, shape="underline", blink=True),
        [], [b""], [b""])
    tail = seed[seed.index(b"\x1b[3;21r"):]
    for part in (b"\x1b[?7l", b"\x1b[4h", b"\x1b[?1h", b"\x1b=", b"\x1b[?1002h",
                 b"\x1b[?1006h", b"\x1b[3 q"):
        assert part in tail
    assert tail.endswith(b"\x1b[11;1H\x1b[?25l")


def test_seed_origin_mode_makes_the_cursor_margin_relative():
    seed = br.build_seed(_state(origin=True, cy=10, scroll_upper=2, scroll_lower=20),
                         [], [b""], [b""])
    assert seed.endswith(b"\x1b[?6h\x1b[9;1H")


def test_clamp_dim():
    assert br.clamp_dim("120", 80) == 120
    assert br.clamp_dim(10 ** 9, 80) == br.MAX_DIM
    assert br.clamp_dim(0, 80) == 2
    assert br.clamp_dim("x", 80) == 80
    assert br.clamp_dim(None, 24) == 24


# --- the snapshot/stream split, with a scripted client -----------------------


class FakeClient:
    """Just enough of ControlClient. ``during_batch`` runs inside send_batch,
    before the reply resolves, so a test can fire output on either side of it."""

    def __init__(self):
        self.session_id = "$1"
        self.notify_seq = 0
        self.subs: dict[str, list] = {}
        self.sent: list[tuple] = []
        self.during_batch = None
        self.state = b"0 3 2 1 1 0 0 0 1 0 0 0 0 0 0 0 23 80 24 0 default"
        self.closed_called = False

    def subscribe(self, name, cb):
        self.subs.setdefault(name, []).append(cb)
        return lambda: self.subs[name].remove(cb)

    def fire(self, name, *args, value=b""):
        self.notify_seq += 1
        for cb in list(self.subs.get(name, ())):
            cb(Line(kind="notify", name=name, args=[a.encode() for a in args], value=value))

    def _reply(self, body):
        r = Reply(body)
        r.seq = self.notify_seq
        return r

    async def send_command(self, *args):
        self.sent.append(args)
        if args[0] == "list-panes":
            return self._reply([b"%5 1"])
        if args[0] == "display-message":
            return self._reply([b"80 24"])
        return self._reply([])

    async def send_batch(self, *cmds):
        self.sent.append(("batch",) + tuple(c[0] for c in cmds))
        if self.during_batch:
            hook, self.during_batch = self.during_batch, None
            hook(self)
        out = []
        for c in cmds:
            if c[0] == "display-message":
                out.append(self._reply([self.state]))
            elif c[0] == "capture-pane" and "-J" in c:
                out.append(self._reply([b"h1", b"h2", b"h3"]))
            elif c[0] == "capture-pane" and "-a" in c:
                out.append(self._reply([b""]))
            elif c[0] == "capture-pane":
                out.append(self._reply([b"$ "] + [b""] * 23))
            else:
                out.append(self._reply([]))
        return out

    async def send_pane(self, pane, data):
        self.sent.append(("send_pane", pane, data))

    async def pause_pane(self, pane):
        self.sent.append(("pause", pane))

    async def close(self):
        self.closed_called = True


class Sink:
    def __init__(self):
        self.frames: list = []

    async def send_bytes(self, b):
        self.frames.append(b)

    async def send_text(self, t):
        self.frames.append(json.loads(t))

    def joined(self):
        return b"".join(f for f in self.frames if isinstance(f, bytes))


async def _settle(b, tasks_for=0.05):
    await asyncio.sleep(tasks_for)


async def _bridge(client, **kw):
    sink = Sink()
    b = br.Bridge(client, sink.send_bytes, sink.send_text, **kw)
    await b.start()
    tasks = [asyncio.create_task(b.writer()), asyncio.create_task(b.maintain())]
    return b, sink, tasks


def test_start_resizes_then_seeds_the_active_pane():
    async def go():
        c = FakeClient()
        b, sink, tasks = await _bridge(c, cols=100, rows=30)
        await _settle(b)
        assert b.pane == "%5"
        assert ("refresh-client", "-C", "100x30") in c.sent
        assert sink.frames[0] == {"seed": "begin", "cols": 80, "rows": 24}
        assert sink.frames[-1] == {"seed": "end"}
        assert sink.joined().startswith(b"h1\r\nh2\r\nh3\r\n$ ")
        for t in tasks:
            t.cancel()
    run(go())


def test_output_during_the_snapshot_is_split_at_the_reply():
    """Before the reply: already in the snapshot, dropped. After: sent after it."""
    async def go():
        c = FakeClient()

        def race(cl):
            cl.fire("output", "%5", value=b"BEFORE")          # in the snapshot
        c.during_batch = race
        b, sink, tasks = await _bridge(c)
        await _settle(b)
        assert b"BEFORE" not in sink.joined()
        c.fire("output", "%5", value=b"AFTER")
        c.fire("output", "%9", value=b"OTHER-PANE")
        await _settle(b)
        assert sink.joined().endswith(b"AFTER")
        assert b"OTHER-PANE" not in sink.joined()
        for t in tasks:
            t.cancel()
    run(go())


def test_output_after_the_reply_but_before_the_seed_is_queued_is_kept():
    """The race the spike had: the reader dispatches post-snapshot output before
    the seeding coroutine resumes. It must land after the seed, not be reset away."""
    async def go():
        c = FakeClient()
        orig = c.send_batch

        async def batch_then_more(*cmds):
            out = await orig(*cmds)
            c.fire("output", "%5", value=b"RIGHT-AFTER")       # seq > reply seq
            return out
        c.send_batch = batch_then_more
        b, sink, tasks = await _bridge(c)
        await _settle(b)
        end = sink.frames.index({"seed": "end"})
        assert sink.frames[end + 1:] == [b"RIGHT-AFTER"]
        for t in tasks:
            t.cancel()
    run(go())


def test_overflow_pauses_the_pane_and_reseeds_once_drained():
    async def go():
        c = FakeClient()
        b, sink, tasks = await _bridge(c, high_water=1000)
        await _settle(b)
        seeds_before = sum(1 for f in sink.frames if f == {"seed": "end"})
        for _ in range(30):
            c.fire("output", "%5", value=b"x" * 100)
        await _settle(b, 0.2)
        assert ("pause", "%5") in c.sent
        assert c.sent[-1][:2] == ("batch", "refresh-client"), "continue rides the seed line"
        assert sum(1 for f in sink.frames if f == {"seed": "end"}) == seeds_before + 1
        for t in tasks:
            t.cancel()
    run(go())


def test_tmux_pause_triggers_a_reseed_without_a_second_pause():
    async def go():
        c = FakeClient()
        b, sink, tasks = await _bridge(c)
        await _settle(b)
        c.sent.clear()
        c.fire("pause", "%5")
        await _settle(b, 0.2)
        assert c.sent[-1][:2] == ("batch", "refresh-client")
        assert sink.frames[-1] == {"seed": "end"}
        for t in tasks:
            t.cancel()
    run(go())


def test_input_goes_to_the_active_pane():
    async def go():
        c = FakeClient()
        b, sink, tasks = await _bridge(c)
        await b.input(b"\x03\x1b[A")
        assert ("send_pane", "%5", b"\x03\x1b[A") in c.sent
        for t in tasks:
            t.cancel()
    run(go())


def test_being_switched_to_another_session_ends_the_bridge():
    async def go():
        c = FakeClient()
        b, sink, tasks = await _bridge(c)
        c.session_id = "$2"                       # what the client records first
        c.fire("session-changed", "$2", "someone-else")
        await _settle(b)
        assert b.ended == "session-changed" and c.closed_called
        for t in tasks:
            t.cancel()
    run(go())


def test_a_smaller_pane_makes_the_browser_adopt_its_size():
    async def go():
        c = FakeClient()
        b, sink, tasks = await _bridge(c, cols=120, rows=40)
        await _settle(b)
        await b.resize(120, 40)
        c.fire("layout-change", "@1")
        await _settle(b, br.LAYOUT_DEBOUNCE + 0.1)
        assert {"size": {"cols": 80, "rows": 24}} in sink.frames
        for t in tasks:
            t.cancel()
    run(go())


# --- end to end against real tmux --------------------------------------------

needs_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")
_ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\x1b\[[0-9]* q")


@pytest.fixture
def tmux_env():
    sock = tempfile.mkdtemp(prefix="serai-br-", dir="/tmp")   # short: sockets are length-capped
    env = {**os.environ, "TMUX_TMPDIR": sock, "PS1": "$ ", "ENV": ""}
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)                                # never touch the real server

    def tmux(*args):
        return subprocess.run(["tmux", "-f", "/dev/null", *args], env=env,
                              capture_output=True, text=True, timeout=10)
    yield env, tmux
    tmux("kill-server")
    shutil.rmtree(sock, ignore_errors=True)


def _wait(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


async def _real_bridge(env, name, **kw):
    c = ControlClient(["tmux", "-f", "/dev/null", "-C", "attach-session", "-f",
                       "pause-after=3", "-t", "=" + name], env=env)
    await c.start()
    b, sink, tasks = await _bridge(c, **kw)
    return c, b, sink, tasks


async def _wait_for(sink, needle, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if needle in sink.joined():
            return True
        await asyncio.sleep(0.05)
    return False


@needs_tmux
def test_real_tmux_seed_carries_history_and_input_round_trips(tmux_env):
    env, tmux = tmux_env
    tmux("new-session", "-d", "-s", "demo-a", "-x", "80", "-y", "24", "sh")
    tmux("send-keys", "-t", "demo-a", "for i in $(seq 1 60); do echo hist-$i; done", "Enter")
    assert _wait(lambda: "hist-60" in tmux("capture-pane", "-p", "-t", "demo-a").stdout)

    async def go():
        c, b, sink, tasks = await _real_bridge(env, "demo-a", cols=80, rows=24)
        try:
            await _settle(b, 0.3)
            assert sink.frames[0]["seed"] == "begin"
            assert (sink.frames[0]["cols"], sink.frames[0]["rows"]) == (80, 24)
            text = _ANSI.sub(b"", sink.joined())
            assert b"hist-1\r\n" in text and b"hist-60\r\n" in text
            assert text.count(b"hist-30\r\n") == 1, "history and grid must not overlap"
            await b.input(b"echo typed-$((6*7))\r")
            assert await _wait_for(sink, b"typed-42")
        finally:
            for t in tasks:
                t.cancel()
            b.close()
            await c.close()
    run(go())


@needs_tmux
def test_real_tmux_alt_screen_seed(tmux_env):
    env, tmux = tmux_env
    tmux("new-session", "-d", "-s", "demo-b", "-x", "80", "-y", "24", "sh")
    tmux("send-keys", "-t", "demo-b", "echo normal-marker", "Enter")
    tmux("send-keys", "-t", "demo-b",
         r"printf '\033[?1049h\033[HALT-MARKER'; sleep 30", "Enter")
    assert _wait(lambda: tmux("display-message", "-p", "-t", "demo-b",
                              "#{alternate_on}").stdout.strip() == "1")

    async def go():
        c, b, sink, tasks = await _real_bridge(env, "demo-b")
        try:
            await _settle(b, 0.3)
            seed = sink.joined()
            switch = seed.index(b"\x1b[?1049h")
            assert b"normal-marker" in seed[:switch]
            assert b"ALT-MARKER" in seed[switch:]
        finally:
            for t in tasks:
                t.cancel()
            b.close()
            await c.close()
    run(go())


@needs_tmux
def test_real_tmux_overflow_reseeds_and_the_screen_ends_consistent(tmux_env):
    env, tmux = tmux_env
    tmux("new-session", "-d", "-s", "demo-c", "-x", "80", "-y", "24", "sh")

    async def go():
        c, b, sink, tasks = await _real_bridge(env, "demo-c", high_water=4096)
        try:
            await _settle(b, 0.3)
            await b.input(b"seq 1 30000; echo flood-done\r")
            assert await _wait_for(sink, b"flood-done\r\n", timeout=15)
            await _settle(b, 0.5)
            seeds = sum(1 for f in sink.frames if f == {"seed": "end"})
            assert seeds >= 2, "a 4 KiB high-water must have forced a re-seed"
            # Whatever was dropped, the browser's view ends equal to tmux's: the
            # last seed plus what followed it must show the final lines.
            last = max(i for i, f in enumerate(sink.frames)
                       if isinstance(f, dict) and f.get("seed") == "begin")
            tail = b"".join(f for f in sink.frames[last:] if isinstance(f, bytes))
            assert b"30000" in _ANSI.sub(b"", tail)
        finally:
            for t in tasks:
                t.cancel()
            b.close()
            await c.close()
    run(go())
