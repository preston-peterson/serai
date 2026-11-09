"""Spike items 6 and 11 against the real bridge: a full-screen app that turns
on mouse tracking gets the browser's clicks and wheel as mouse reports (the
seed restores the mode), Shift+drag still selects, and a paste arrives exactly
once -- before and after a reconnect. The app is a tiny raw-mode probe that
logs every byte it reads.

Known gap, not asserted: the app also turns on bracketed paste (DECSET 2004)
before the attach, and no tmux format exposes that mode, so the seed cannot
restore it -- the opt-in bracketed-paste preference only brackets for an app
that enables 2004 while attached. Off by default, so default paste is exact."""

from __future__ import annotations

import asyncio
import json
import os
import sys

from .test_smoke import Stack, page, ROWS  # noqa: F401

PROBE = r'''
import os, sys, tty, termios
log = open(sys.argv[1], "ab", buffering=0)
old = termios.tcgetattr(0)
tty.setraw(0)
os.write(1, b"\x1b[?1049h\x1b[?1000h\x1b[?1006h\x1b[?2004h\x1b[2J\x1b[Hprobe-ready\r\n")
n = 0
try:
    while True:
        b = os.read(0, 65536)
        if not b:
            break
        log.write(b.hex().encode() + b"\n")
        n += len(b)
        os.write(1, b"\x1b[2;1Hgot %d bytes   " % n)
finally:
    termios.tcsetattr(0, termios.TCSADRAIN, old)
'''

PANE = "import('/static/views/panes.js').then(m => m.focusedPane())"


def read_log(path) -> bytes:
    try:
        with open(path) as f:
            return b"".join(bytes.fromhex(line.strip()) for line in f if line.strip())
    except FileNotFoundError:
        return b""


def test_mouse_reports_and_paste_reach_a_tui(tmp_path):
    probe, log = tmp_path / "probe.py", tmp_path / "probe.log"
    probe.write_text(PROBE)
    s = Stack(str(tmp_path))
    s.tmux("new-session", "-d", "-s", "shell-probe", "-x", "100", "-y", "30",
           f"{sys.executable} {probe} {log}")
    results = {}
    try:
        s.start(str(tmp_path))

        async def run():
            c = await page(s)
            try:
                await c.wait("document.querySelectorAll('.row').length >= 1", 15)
                await c.eval("document.querySelector('.row').click()")
                await c.wait(f"{ROWS}.includes('probe-ready')", 10)
                await asyncio.sleep(0.5)
                results["modes_on_attach"] = await c.eval(
                    f"{PANE}.then(p => ({{mouse: p.term.modes.mouseTrackingMode, bracketed: p.term.modes.bracketedPasteMode, alt: p.term.buffer.active.type}}))")
                x, y, w, h = await c.eval(
                    "(()=>{const r=document.querySelector('.pane .xterm-screen').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
                cols, rows = await c.eval(f"{PANE}.then(p => [p.term.cols, p.term.rows])")
                cw, lh = w / cols, h / rows
                cx, cy = x + (10 - 0.5) * cw, y + (5 - 0.5) * lh

                mark = len(read_log(log))
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=cx, y=cy, button="left", clickCount=1)
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=cx, y=cy, button="left", clickCount=1)
                await asyncio.sleep(0.6)
                results["click"] = read_log(log)[mark:]

                mark = len(read_log(log))
                await c.send("Input.dispatchMouseEvent", type="mouseWheel", x=cx, y=cy, deltaX=0, deltaY=120)
                await asyncio.sleep(0.6)
                results["wheel"] = read_log(log)[mark:]

                mark = len(read_log(log))
                await c.send("Input.dispatchMouseEvent", type="mousePressed", x=x + 2, y=y + lh * 0.5, button="left", clickCount=1, modifiers=8)
                for i in range(1, 9):
                    await c.send("Input.dispatchMouseEvent", type="mouseMoved", x=x + 2 + 200 * i / 8, y=y + lh * 0.5, button="left", modifiers=8)
                await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=x + 202, y=y + lh * 0.5, button="left", clickCount=1, modifiers=8)
                await asyncio.sleep(0.4)
                results["shift_drag_selection"] = await c.eval(f"{PANE}.then(p => p.term.getSelection())")
                results["shift_drag_bytes"] = read_log(log)[mark:]

                async def paste(text):
                    await c.eval(f"navigator.clipboard.writeText({json.dumps(text)})")
                    await c.eval(f"{PANE}.then(p => p.focus())")
                    mark = len(read_log(log))
                    await c.send("Input.dispatchKeyEvent", type="rawKeyDown", code="KeyV", key="v", modifiers=2,
                                 windowsVirtualKeyCode=86, commands=["paste"])
                    await c.send("Input.dispatchKeyEvent", type="keyUp", code="KeyV", key="v", modifiers=2,
                                 windowsVirtualKeyCode=86)
                    await asyncio.sleep(0.8)
                    return read_log(log)[mark:]

                results["paste"] = await paste("alpha\nbeta\ngamma")
                await c.eval(f"{PANE}.then(p => p.ws.close())")          # a dropped socket reattaches
                await c.wait(f"{PANE}.then(p => !!p.ws && p.ws.readyState === 1)", 10)
                await asyncio.sleep(1.0)
                results["modes_after_reconnect"] = await c.eval(
                    f"{PANE}.then(p => ({{mouse: p.term.modes.mouseTrackingMode, alt: p.term.buffer.active.type}}))")
                results["paste_after_reconnect"] = await paste("three\nfour")
                errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
                results["js_errors"] = json.dumps(errs)[:300] if errs else ""
            finally:
                await c.close()

        asyncio.run(asyncio.wait_for(run(), 90))
    finally:
        print(s.log_tails(15))
        s.stop()

    r = results
    assert r["modes_on_attach"] == {"mouse": "vt200", "bracketed": False, "alt": "alternate"}, r
    assert r["click"] == b"\x1b[<0;10;5M\x1b[<0;10;5m", r          # SGR press + release at col 10, row 5
    assert r["wheel"] == b"\x1b[<65;10;5M", r                       # wheel down as a mouse report
    assert r["shift_drag_selection"] and r["shift_drag_bytes"] == b"", r   # Shift+drag selects, sends nothing
    assert r["paste"] == b"alpha\nbeta\ngamma", r                   # exactly once, unbracketed by default
    assert r["modes_after_reconnect"] == {"mouse": "vt200", "alt": "alternate"}, r
    assert r["paste_after_reconnect"] == b"three\nfour", r
    assert not r["js_errors"], r
