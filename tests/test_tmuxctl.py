"""Tests for serai.tmuxctl -- the tmux control-mode client core.

Pure tests cover the codecs, the line parser, the ARG_SAFE gate, and the argv
builders. Client tests run a fake control-mode tmux (a plain Python script, no
real tmux needed) over pipes and exercise the handshake, the %begin/%end
command/reply FIFO, %error, notifications, close, and the command timeout.

Async is driven with asyncio.run inside each test -- no pytest-asyncio dep.

Run:  pytest -q tests/test_tmuxctl.py
"""

import asyncio
import os
import sys

import pytest

from serai import tmuxctl
from serai.tmuxctl import (
    ARG_SAFE,
    ControlClient,
    ControlClosed,
    ControlError,
    Fmt,
    parse_line,
)

pytestmark = pytest.mark.filterwarnings("ignore::ResourceWarning")


# ---------------------------------------------------------------------------
# codecs
# ---------------------------------------------------------------------------

def test_unescape_output_octal_escapes():
    assert tmuxctl.unescape_output(rb"a\033[31mb\134c") == b"a\x1b[31mb\\c"


def test_unescape_output_leaves_lone_backslash():
    # A backslash not followed by three octal digits is left as-is.
    assert tmuxctl.unescape_output(rb"a\b") == rb"a\b"
    assert tmuxctl.unescape_output(rb"a\09b") == rb"a\09b"  # 9 is not octal
    # The first \ guards itself; the second one escapes \033.
    assert tmuxctl.unescape_output(rb"a\\033") == b"a\\\x1b"


def test_unescape_output_plain_passthrough():
    assert tmuxctl.unescape_output(b"hello") == b"hello"
    assert tmuxctl.unescape_output(b"") == b""


def test_hex_chunks_format_and_sizes():
    assert tmuxctl.hex_chunks(b"") == []
    assert tmuxctl.hex_chunks(b"hel") == ["68 65 6c"]
    assert tmuxctl.hex_chunks(b"hello", 2) == ["68 65", "6c 6c", "6f"]
    # exact multiple of size -> no trailing empty chunk
    assert tmuxctl.hex_chunks(b"hello", 5) == ["68 65 6c 6c 6f"]


def test_hex_chunks_rejects_bad_size():
    with pytest.raises(ValueError):
        tmuxctl.hex_chunks(b"x", 0)


# ---------------------------------------------------------------------------
# parse_line
# ---------------------------------------------------------------------------

def test_parse_line_block_framing():
    b = parse_line(b"%begin 1700000000 12 1", False)
    assert (b.kind, b.time, b.num, b.flags) == ("begin", 1700000000, 12, 1)
    e = parse_line(b"%end 1700000000 12 1", True)
    assert (e.kind, e.time, e.num, e.flags) == ("end", 1700000000, 12, 1)
    r = parse_line(b"%error 1700000000 13 0", True)
    assert (r.kind, r.time, r.num, r.flags) == ("error", 1700000000, 13, 0)


def test_parse_line_body_inside_block():
    # Inside a block only %end/%error are special; anything else is body.
    l = parse_line(b"can't find session: nope", True)
    assert (l.kind, l.raw) == ("body", b"can't find session: nope")
    # Even a stray %-line that is not end/error stays body verbatim.
    assert parse_line(b"%output %3 hi", True).kind == "body"


def test_parse_line_output():
    l = parse_line(b"%output %3 a\\134b", False)
    assert (l.kind, l.name, l.args, l.value) == ("notify", "output", [b"%3"], b"a\\b")
    # an empty value is b"", not a crash
    assert parse_line(b"%output %3", False).value == b""


def test_parse_line_extended_output_colon_in_value():
    l = parse_line(b"%extended-output %3 0 ; ; fg=01f1 : a : b\\134c", False)
    assert l.kind == "notify" and l.name == "extended-output"
    # args are the tokens before the FIRST " : "; everything after is the value.
    assert l.args == [b"%3", b"0", b";", b";", b"fg=01f1"]
    assert l.value == b"a : b\\c"  # the inner " : " survives, \134 decodes


def test_parse_line_other_notifications():
    l = parse_line(b"%pause %3", False)
    assert (l.name, l.args, l.value) == ("pause", [b"%3"], b"")
    l = parse_line(b"%session-changed $7 demo-1", False)
    assert (l.name, l.args) == ("session-changed", [b"$7", b"demo-1"])
    l = parse_line(b"%exit", False)
    assert (l.name, l.args) == ("exit", [])
    l = parse_line(b"%exit bad session", False)
    assert (l.name, l.args) == ("exit", [b"bad", b"session"])
    l = parse_line(b"%layout-change $7 %3", False)
    assert (l.name, l.args, l.value) == ("layout-change", [b"$7", b"%3"], b"")


# ---------------------------------------------------------------------------
# ARG_SAFE / Fmt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("arg", ["%3", "$1", "@2", "120x40", "-S", "-200",
                                 "pause-after=3", "#{pane_id}"])
def test_arg_safe_accepts(arg):
    assert ARG_SAFE.match(arg)


@pytest.mark.parametrize("arg", ["a b", "x;y", "'q'"])
def test_arg_safe_rejects(arg):
    assert not ARG_SAFE.match(arg)


def test_arg_safe_rejects_control_chars_and_empty():
    for arg in ["a\nb", "a\x01b", "a\tb", "", "~"]:
        assert not ARG_SAFE.match(arg)


# ---------------------------------------------------------------------------
# control_argv
# ---------------------------------------------------------------------------

def test_control_argv_local(monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    assert tmuxctl.control_argv("local", "demo-1") == [
        "tmux", "-C", "attach-session", "-f", "pause-after=3", "-t", "=demo-1"]
    assert tmuxctl.control_argv("local", "demo-1", pause_after=5)[4] == "pause-after=5"


def test_control_argv_remote_mux(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.delenv("SERAI_SSH_MUX", raising=False)
    argv = tmuxctl.control_argv("service-b", "demo-1")
    assert argv[0] == "ssh"
    assert "-o" in argv and "BatchMode=yes" in argv  # sessions._SSH_OPTS
    assert "ControlMaster=auto" in argv
    assert f"ControlPath={tmp_path / 'serai-ssh'}/%C" in argv
    assert "ControlPersist=600" in argv
    assert "ServerAliveInterval=15" in argv
    assert "ServerAliveCountMax=2" in argv
    assert argv[-2] == "service-b"  # host precedes the single remote command
    # The remote half is one shell string of shlex-quoted tmux argv: nothing
    # the caller supplied can word-split or add a command there (invariant #3).
    assert argv[-1].startswith("tmux -C attach-session")
    assert "pause-after=3" in argv[-1]
    assert "=demo-1" in argv[-1]
    assert ";" not in argv[-1] and "|" not in argv[-1]
    assert os.stat(tmp_path / "serai-ssh").st_mode & 0o777 == 0o700


def test_control_argv_remote_mux_off(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("SERAI_SSH_MUX", "0")
    argv = tmuxctl.control_argv("service-b", "demo-1")
    joined = " ".join(argv)
    assert "ControlMaster" not in joined
    assert "ControlPath" not in joined
    assert "ControlPersist" not in joined
    assert "ServerAliveInterval=15" in argv
    assert not (tmp_path / "serai-ssh").exists()  # dir is created only for the mux


# ---------------------------------------------------------------------------
# ControlClient against a fake control-mode tmux
# ---------------------------------------------------------------------------

FAKE_TMUX = r"""
import sys
out = sys.stdout
def emit(s):
    out.write(s + "\n")
    out.flush()
# The initial attach block (dropped by the client), then the handshake.
emit("%begin 1 0 0")
emit("%end 1 0 0")
emit("%session-changed $7 demo-1")
n = 0
for raw in sys.stdin:
    line = raw.rstrip("\n")
    n += 1
    err = False
    body = line  # default: echo the command back as the block body
    if line.startswith("fail"):
        err = True
        body = "bad"
    elif line.startswith("emit "):
        data = bytes.fromhex(line[5:].replace(" ", ""))
        # Notifications never arrive inside a block: emit before the reply.
        emit("%output %3 " + "".join("\\%03o" % b for b in data))
        body = "echoed"
    elif line == "pause":
        emit("%pause %3")
        body = "ok"
    elif line.startswith("burst"):
        # A run of pane output, then an ordinary reply -- so a test can await
        # the reply as proof the reader has drained every line before it.
        count = int(line.split()[1]) if len(line.split()) > 1 else 50
        for i in range(count):
            emit(f"%output %3 line-{i}")
        body = "burst"
    elif line == "hang":
        emit(f"%begin {1000 + n} {n} 1")
        continue  # never close the block: the reply simply never arrives
    emit(f"%begin {1000 + n} {n} 1")
    emit(body)
    emit(f"%error {1000 + n} {n} 1" if err else f"%end {1000 + n} {n} 1")
emit("%exit")
"""

FAKE_FAST_EXIT = "import sys\nsys.stdout.write('%exit gone\\n')\nsys.stdout.flush()\nsys.exit(0)\n"

FAKE_STDERR_EXIT = ("import sys\n"
                    "sys.stderr.write('boom')\n"
                    "sys.stderr.flush()\n"
                    "sys.exit(1)\n")


def _client(argv, **kw):
    return ControlClient(argv, **kw)


def _fake_argv(extra=""):
    src = FAKE_TMUX + extra
    return [sys.executable, "-c", src]


def run(coro):
    return asyncio.run(coro)


def test_start_reads_session_changed():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        assert c.session_id == "$7"
        assert c.session_name == "demo-1"
        await c.close()
    run(main())


def test_start_fails_when_exit_precedes_handshake():
    async def main():
        c = _client([sys.executable, "-c", FAKE_FAST_EXIT])
        with pytest.raises(ControlClosed):
            await c.start()
    run(main())


def test_start_error_carries_stderr_tail():
    # ssh/tmux print the reason a remote attach failed on stderr; a bare "eof"
    # in the exception would hide it, so start() appends the captured tail.
    async def main():
        c = _client([sys.executable, "-c", FAKE_STDERR_EXIT])
        with pytest.raises(ControlClosed) as ei:
            await c.start()
        assert "boom" in str(ei.value)
    run(main())


def test_send_command_echoes_body():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        assert await c.send_command("display", "-p", "hi") == [b"display -p hi"]
        await c.close()
    run(main())


def test_send_command_error_block():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        with pytest.raises(ControlError) as ei:
            await c.send_command("fail", "-x")
        assert str(ei.value) == "bad"
        # the FIFO stays aligned: the next command still gets its own block
        assert await c.send_command("ok") == [b"ok"]
        await c.close()
    run(main())


def test_send_command_validates_args():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        with pytest.raises(ValueError):
            await c.send_command("a b")
        await c.send_command(Fmt("#{a} #{b}"))  # module-owned format: allowed
        await c.close()
    run(main())


def test_subscribe_receives_decoded_output():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        got = []
        unsub = c.subscribe("output", got.append)
        await c.send_command("emit", *"68 65 6c 6c 6f".split())
        assert [ln.value for ln in got] == [b"hello"]
        assert [ln.args for ln in got] == [[b"%3"]]
        unsub()
        await c.send_command("emit", *"68 65 6c 6c 6f".split())
        assert len(got) == 1  # unsubscribed

        def boom(_line):
            raise RuntimeError("subscriber blew up")
        c.subscribe("output", boom)
        c.subscribe("output", lambda ln: got.append(ln))
        await c.send_command("emit", *"68 65".split())  # reader must survive boom
        assert [ln.value for ln in got] == [b"hello", b"he"]

        # The live pane stream arrives decoded, non-printables included:
        # a ESC [31m b \ c é -> the exact bytes, nothing re-encoded.
        await c.send_command("emit", *"61 1b 5b 33 31 6d 62 5c 63 c3 a9".split())
        assert got[-1].value == b"a\x1b[31mb\\c\xc3\xa9"
        await c.close()
    run(main())


def test_subscribe_all_receives_everything():
    async def main():
        c = _client(_fake_argv())
        seen = []
        # "*" subscribers even see the %session-changed of the handshake,
        # which starts before start() can hand back a handle.
        c.subscribe("*", lambda ln: seen.append(ln.name))
        await c.start()
        await c.send_command("pause")
        assert seen == ["session-changed", "pause"]
        await c.close()
    run(main())


def test_pause_reading_stops_the_reader_until_resume():
    # The reader gate is what lets tmux's `pause-after` work: while paused the
    # client reads nothing, so the backlog sits in the pipe/tmux buffer instead
    # of in this process. Nothing reaches a subscriber, and the command's own
    # reply block is unread too -- awaiting it is how we know the drain finished.
    async def main():
        c = _client(_fake_argv())
        await c.start()
        got = []
        c.subscribe("output", got.append)
        assert not c.reading_paused
        c.pause_reading()
        assert c.reading_paused
        burst = asyncio.create_task(c.send_command("burst", "50"))
        await asyncio.sleep(0.3)
        assert got == []
        assert not burst.done()
        c.resume_reading()
        assert not c.reading_paused
        assert await asyncio.wait_for(burst, 3) == [b"burst"]
        assert [ln.value for ln in got] == \
            [f"line-{i}".encode() for i in range(50)]
        await c.close()
    run(main())


def test_pause_reading_is_a_gate_not_a_counter():
    # Two pauses, one resume: reading comes back. A counting semaphore would
    # leave the reader wedged here (and the burst below never delivered).
    async def main():
        c = _client(_fake_argv())
        await c.start()
        got = []
        c.subscribe("output", got.append)
        c.pause_reading()
        c.pause_reading()
        assert c.reading_paused
        burst = asyncio.create_task(c.send_command("burst", "5"))
        await asyncio.sleep(0.2)
        assert got == []
        c.resume_reading()
        assert await asyncio.wait_for(burst, 3) == [b"burst"]
        assert [ln.value for ln in got] == \
            [b"line-0", b"line-1", b"line-2", b"line-3", b"line-4"]
        # resume again is a no-op, and reading is unaffected
        c.resume_reading()
        assert not c.reading_paused
        assert await c.send_command("ok") == [b"ok"]
        await c.close()
    run(main())


def test_close_while_paused_drains_and_resolves():
    # close() resumes the reader before it waits for it, so a paused client
    # still shuts down cleanly rather than burning the 2 s wait + cancel.
    async def main():
        c = _client(_fake_argv())
        await c.start()
        c.pause_reading()
        await asyncio.wait_for(c.close(), 3)
        assert c.closed.done()
        assert c.closed.result() in ("eof", "exit")
        with pytest.raises(ControlClosed):
            await c.send_command("ok")
    run(main())


def test_close_resolves_closed_and_fails_pending():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        await c.close()
        assert c.closed.done() and c.closed.result() in ("eof", "exit")
        with pytest.raises(ControlClosed):
            await c.send_command("ok")
    run(main())


def test_command_timeout_closes():
    async def main():
        c = _client(_fake_argv(), cmd_timeout=0.5)
        await c.start()
        with pytest.raises(ControlClosed):
            await c.send_command("hang")
        reason = await asyncio.wait_for(c.closed, 2)
        assert reason in ("eof", "exit")
        with pytest.raises(ControlClosed):
            await c.send_command("ok")
    run(main())


def test_continue_pane_sends_refresh_client_line():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        # The flow-control resume is exactly this control line -- the
        # <pane>:<state> token QUOTED (tmux 3.6 rejects it bare), so it rides
        # the Fmt lane; the fake echoes what it receives, and the reply body
        # proves the literal bytes on the wire. A plain str "'%3:continue'"
        # would (correctly) be refused by ARG_SAFE -- only _pane_state builds it.
        with pytest.raises(ValueError):
            await c.send_command("refresh-client", "-A", "'%3:continue'")
        assert await c.send_command("refresh-client", "-A",
                                    Fmt("'%3:continue'")) == \
            [b"refresh-client -A '%3:continue'"]
        # The method: a %pause marks the pane, continue_pane answers and clears it.
        await c.send_command("pause")
        assert c._pause_panes == {b"%3"}
        await c.continue_pane("%3")
        assert c._pause_panes == set()
        # pause_pane / set_pane_output ride the same quoted helper; spy the
        # join each method would write so its exact control line is asserted.
        sent = []
        orig = c.send_command

        async def spy(*args):
            sent.append(" ".join(args))
            return await orig(*args)

        c.send_command = spy
        await c.pause_pane("%3")
        await c.set_pane_output("%3", True)
        await c.set_pane_output("%3", False)
        c.send_command = orig
        assert sent == ["refresh-client -A '%3:pause'",
                        "refresh-client -A '%3:on'",
                        "refresh-client -A '%3:off'"]
        await c.close()
    run(main())


def test_pane_state_helper():
    from serai.tmuxctl import _pane_state
    assert _pane_state("%3", "continue") == "'%3:continue'"
    assert _pane_state("%3", "on") == "'%3:on'"
    # both pieces are validated -- an injection-shaped pane and an unknown
    # state must never reach the Fmt bypass
    with pytest.raises(ValueError):
        _pane_state("%3;kill-server", "continue")
    with pytest.raises(ValueError):
        _pane_state("%3", "bogus")
    with pytest.raises(ValueError):
        _pane_state("$7", "continue")  # session ids are not pane ids


def test_capture_always_passes_start_and_end():
    async def main():
        c = _client(_fake_argv())
        await c.start()
        assert await c.capture("%3") == \
            [b"capture-pane -p -t %3 -S -2000 -E -"]
        assert await c.capture("%3", start=0, end="$", e=True) == \
            [b"capture-pane -p -t %3 -S 0 -E $ -e"]
        await c.close()
    run(main())


def test_closed_needs_running_loop():
    c = ControlClient([sys.executable, "-c", "pass"])
    with pytest.raises(RuntimeError):
        c.closed
