"""The phone layout (approved mockup 08) at 390x844 with touch emulation:
the triage list, a full-screen terminal with the key bar, select mode's
plain-text overlay, the ⋮ sheet with actions and files, and back."""

from __future__ import annotations

import asyncio
import json

from .cdp import CDP
from .test_smoke import stack  # noqa: F401  (pytest fixture)

TERM = "document.querySelector('.pane.focused .xterm-rows').innerText"


async def phone_page(stack) -> CDP:
    c = await CDP.connect(stack.dbg)
    for d in ("Page", "Runtime"):
        await c.send(f"{d}.enable")
    await c.send("Emulation.setDeviceMetricsOverride", width=390, height=844, deviceScaleFactor=2, mobile=True)
    await c.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=5)
    await c.send("Emulation.setFocusEmulationEnabled", enabled=True)
    await c.send("Page.navigate", url=f"http://127.0.0.1:{stack.port}/")
    return c


def test_phone_list_terminal_keybar_select_and_sheet(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await phone_page(stack)
        try:
            check("the phone list replaces the desktop layout", await c.wait(
                "document.body.classList.contains('phone') && document.querySelectorAll('.pcard').length >= 2", 15))
            check("the desktop top bar is gone", await c.eval(
                "getComputedStyle(document.getElementById('topbar')).display === 'none'"))
            await c.eval("[...document.querySelectorAll('.pcard')].find(x => x.querySelector('.name').textContent === 'demo').click()")
            check("a tap opens the full-screen terminal", await c.wait(
                f"!document.querySelector('.p-term').hidden && {TERM}.includes('line-')", 10))
            check("all nine keys fit at 390px", await c.eval(
                "(()=>{const b=document.querySelector('.keybar'); return b.scrollWidth <= b.clientWidth + 1})()"))

            await c.eval("document.querySelector('.pane.focused .xterm-helper-textarea').focus()")
            await c.send("Input.insertText", text="echo kb-$((3*3))")
            await c.eval("[...document.querySelectorAll('.keybar .k')].find(k => k.textContent === 'esc').click()")
            await c.key("Enter", "Enter", text="\r", vk=13)
            check("typing works with the key bar present", await c.wait(f"{TERM}.includes('kb-9')", 6))

            await c.eval("[...document.querySelectorAll('.keybar .k')].find(k => k.textContent === 'select').click()")
            sel = await c.eval("(()=>{const o=document.querySelector('.selover'); return o && "
                               "{text: o.textContent, us: getComputedStyle(o).userSelect}})()")
            check("select mode lays selectable plain text over the terminal",
                  sel and sel["us"] == "text" and "kb-9" in sel["text"], json.dumps(sel)[:120])
            await c.eval("document.querySelector('.selbanner button').click()")
            check("done leaves select mode", await c.eval("!document.querySelector('.selover')"))

            await c.eval("document.querySelector('.ahead .btn').click()")
            check("⋮ opens the sheet with actions", await c.wait(
                "[...document.querySelectorAll('.sheet .frow')].some(r => r.textContent.includes('Close session'))", 5))
            check("and the session's files", await c.wait(
                "document.querySelectorAll('.sheet .sfiles .frow').length >= 1", 8))
            await c.eval("document.querySelector('.sheet-scrim').click()")
            check("tapping outside closes it", await c.eval("!document.querySelector('.sheet')"))

            await c.eval("document.querySelector('.ahead .back').click()")
            check("‹ goes back to the list", await c.eval("!document.querySelector('.p-list').hidden"))
            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 90))
    assert not failures, "phone failures:\n  " + "\n  ".join(failures)



async def touch_drag(c, x, y0, dy, steps):
    """A drag through the browser's real touch pipeline (CDP), which -- unlike
    page-built TouchEvents -- starts a native scroll, as a phone does."""
    await c.send("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y0, "id": 1}])
    y = y0
    for _ in range(steps):
        y += dy
        await c.send("Input.dispatchTouchEvent", type="touchMove", touchPoints=[{"x": x, "y": y, "id": 1}])
        await asyncio.sleep(0.016)
    await c.send("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])


VP = "document.querySelector('.pane.focused .xterm-viewport').scrollTop"


def test_phone_touch_scroll_is_the_native_scroll_and_taps_still_type(stack):
    """On a phone every drag is the browser's own (momentum) scroll: the text
    layer lets touches through to xterm's scroll layer. A JS scroll + glide
    replacing it tested fine headless and felt worse on a real phone."""
    async def run():
        c = await phone_page(stack)
        try:
            await c.wait("document.querySelectorAll('.pcard').length >= 2", 15)
            await c.eval("[...document.querySelectorAll('.pcard')].find(x => x.querySelector('.name').textContent === 'demo').click()")
            await c.wait(f"!document.querySelector('.p-term').hidden && {TERM}.includes('line-')", 10)
            await asyncio.sleep(0.5)
            assert await c.eval("getComputedStyle(document.querySelector('.pane.focused .xterm-screen')).pointerEvents") == "none"
            x, y = await c.eval("(()=>{const r=document.querySelector('.pane.focused .xterm').getBoundingClientRect();"
                                "return [r.left+r.width/2, r.top+r.height/2]})()")
            await c.eval("document.activeElement && document.activeElement.blur()")
            before = await c.eval(VP)
            await c.send("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y, "id": 1}])
            await c.send("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])
            await asyncio.sleep(0.3)
            assert await c.eval(VP) == before, "a tap must not scroll"
            assert await c.eval("document.activeElement && document.activeElement.classList.contains('xterm-helper-textarea')"), \
                "a tap must still focus the terminal (so the keyboard comes up)"
            await touch_drag(c, x, y - 100, 5, 30)           # finger down 150 px: back into history
            moved = before - await c.eval(VP)
            assert moved >= 100, f"the drag must scroll the history natively (moved {moved}px)"
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 60))
