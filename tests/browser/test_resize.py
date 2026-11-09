"""The rail's right edge and the split divider both resize by dragging; the
rail width is a preference, so it survives a reload."""

from __future__ import annotations

import asyncio

from .test_smoke import page, stack  # noqa: F401  (pytest fixture)

RAIL_W = "document.getElementById('rail').getBoundingClientRect().width"


def test_rail_and_split_divider_resize(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.row').length >= 2", 15)
            w0 = await c.eval(RAIL_W)
            gx, gy = await c.eval("(()=>{const r=document.querySelector('.rail-grip').getBoundingClientRect();"
                                  "return [r.left + r.width/2, r.top + r.height/2]})()")
            await c.drag(gx, gy, gx + 120, gy)
            await asyncio.sleep(0.8)                          # the settings push is debounced
            w1 = await c.eval(RAIL_W)
            check("dragging the rail edge widens the rail", abs(w1 - (w0 + 120)) < 4, f"{w0} -> {w1}")
            await c.drag(gx + 120, gy, -500, gy)
            check("the rail has a minimum width", await c.eval(RAIL_W) >= 180)
            await c.drag(await c.eval("document.querySelector('.rail-grip').getBoundingClientRect().left + 3"),
                         gy, 400, gy)
            await asyncio.sleep(0.8)
            await c.send("Page.reload")
            await c.wait("document.querySelectorAll('.row').length >= 2", 15)
            w2 = await c.eval(RAIL_W)
            check("the width survives a reload", abs(w2 - 400) < 6, f"(got {w2})")

            await c.eval("document.querySelectorAll('.row')[0].click()")
            await c.eval("document.querySelectorAll('.row')[1].dispatchEvent(new MouseEvent('click',{bubbles:true,shiftKey:true}))")
            await c.wait("document.querySelectorAll('.pane').length === 2", 8)
            p0 = await c.eval("document.querySelector('.pane').getBoundingClientRect().width")
            dx, dy = await c.eval("(()=>{const r=document.querySelector('.divider').getBoundingClientRect();"
                                  "return [r.left + r.width/2, r.top + r.height/2]})()")
            await c.drag(dx, dy, dx - 150, dy)
            p1 = await c.eval("document.querySelector('.pane').getBoundingClientRect().width")
            check("dragging the divider resizes the left pane", p0 - p1 > 100, f"{p0} -> {p1}")
            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, str(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 60))
    assert not failures, "resize failures:\n  " + "\n  ".join(failures)
