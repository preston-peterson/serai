"""Dialogs never outgrow the window. A long ★ Favorites panel (30 invented
favorites) once centred past both edges of the screen -- title, ✕ and the last
rows unreachable -- because the shared dialog had no height cap and nothing in
it scrolled. Now the head and foot stay put and the body scrolls, the table's
header row sticks, and dragging a row to the edge scrolls the list under it.
Checked on the desktop, on a short window (the session form), and at phone size.

Shares test_smoke's throwaway tmux + serai + Chromium. Invented names only.
"""

from __future__ import annotations

import asyncio
import json
import os

from .test_smoke import stack, page  # noqa: F401  (pytest fixture)

N = 30
PANEL = "document.querySelector('.dialog.favs')"
BODY = "document.querySelector('.dialog.favs .dialog-body')"

# Fits: the whole panel is inside the viewport, and so is its ✕.
FITS = ("(() => {{ const r = document.querySelector('{sel}').getBoundingClientRect();"
        " const x = document.querySelector('{sel} .dialog-head .close').getBoundingClientRect();"
        " return r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth"
        " && x.top >= 0 && x.bottom <= innerHeight; }})()")


def seed_favorites(stack) -> None:
    prof = {}
    for i in range(1, N + 1):
        name = f"shell-fav-{i:02d}"
        prof[f"local::{name}"] = {"host": "local", "name": name, "kind": "shell", "label": f"fav-{i:02d}",
                                  "path": "", "tags": [], "args": "", "owner": "", "favorite": True,
                                  "launch": "new"}
    cfg = stack.env["SERAI_CONFIG_DIR"]
    os.makedirs(cfg, exist_ok=True)
    with open(os.path.join(cfg, "sessions.json"), "w") as f:
        json.dump(prof, f)


def test_a_long_dialog_fits_the_window_and_scrolls(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    seed_favorites(stack)

    async def open_favorites(c, phone=False):
        if phone:
            await c.eval("[...document.querySelectorAll('button')]"
                         ".find(b => b.textContent.includes('favorites')).click()")
        else:
            await c.eval("document.querySelector('.topbar button[title=\"Favorites\"]').click()")
        return await c.wait(f"!!{PANEL} && {PANEL}.querySelectorAll('tbody tr').length === {N}", 15)

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)

            # -- desktop, 1300x820 ------------------------------------------------
            check("the ★ panel opens with every favorite", await open_favorites(c))
            check("it fits the window, ✕ included", await c.eval(FITS.format(sel=".dialog.favs")),
                  await c.eval(f"JSON.stringify({PANEL}.getBoundingClientRect())"))
            check("its body scrolls", await c.eval(f"{BODY}.scrollHeight > {BODY}.clientHeight + 100"))

            await c.eval(f"{BODY}.scrollTop = {BODY}.scrollHeight")
            await asyncio.sleep(0.1)
            check("the last row can be reached", await c.eval(
                f"(() => {{ const b = {BODY}.getBoundingClientRect();"
                f" const r = [...{PANEL}.querySelectorAll('tbody tr')].pop().getBoundingClientRect();"
                " return r.top >= b.top && r.bottom <= b.bottom + 1; })()"))
            check("the header row stays at the top", await c.eval(
                f"(() => {{ const b = {BODY}.getBoundingClientRect();"
                f" const h = {PANEL}.querySelector('thead th').getBoundingClientRect();"
                " return Math.abs(h.top - b.top) <= 1; })()"),
                await c.eval(f"JSON.stringify([{BODY}.getBoundingClientRect().top,"
                             f" {PANEL}.querySelector('thead th').getBoundingClientRect().top])"))

            # Drag the first row's grip to the bottom edge and hold it there: the
            # list scrolls by itself and the row travels down with it.
            await c.eval(f"{BODY}.scrollTop = 0")
            await asyncio.sleep(0.1)
            g = await c.eval(f"(() => {{ const r = {PANEL}.querySelector('tbody tr .grip').getBoundingClientRect();"
                             " return [r.left + r.width / 2, r.top + r.height / 2]; })()")
            bottom = await c.eval(f"{BODY}.getBoundingClientRect().bottom")
            await c.send("Input.dispatchMouseEvent", type="mousePressed", x=g[0], y=g[1], button="left", clickCount=1)
            for i in range(1, 9):
                await c.send("Input.dispatchMouseEvent", type="mouseMoved", button="left",
                             x=g[0], y=g[1] + (bottom - 8 - g[1]) * i / 8)
            await asyncio.sleep(1.2)
            scrolled = await c.eval(f"{BODY}.scrollTop")
            await c.send("Input.dispatchMouseEvent", type="mouseReleased", x=g[0], y=bottom - 8,
                         button="left", clickCount=1)
            check("holding a row at the bottom edge scrolls the list", scrolled > 200, f"scrollTop={scrolled}")
            idx = await c.eval(f"[...{PANEL}.querySelectorAll('tbody tr')]"
                               ".findIndex(r => r.querySelector('.t-name').textContent.endsWith('fav-01'))")
            check("and the dragged row went with it", idx > 10, f"index={idx}")
            await c.eval(f"{PANEL}.querySelector('.close').click()")
            await c.wait(f"!{PANEL}", 5)

            # -- a short window: the session form keeps its buttons on screen ----
            await c.send("Emulation.setDeviceMetricsOverride", width=1300, height=460,
                         deviceScaleFactor=1, mobile=False)
            await c.eval("[...document.querySelectorAll('.topbar .slots button')]"
                         ".find(b => b.textContent.includes('+ New')).click()")
            await c.wait("!!document.querySelector('.dialog.form')", 10)
            check("a short window still shows the whole form dialog",
                  await c.eval(FITS.format(sel=".dialog.form")))
            check("with Create & attach on screen", await c.eval(
                "(() => { const b = [...document.querySelectorAll('.dialog-foot button')]"
                ".find(x => x.textContent === 'Create & attach').getBoundingClientRect();"
                " return b.bottom <= innerHeight; })()"))
            await c.eval("document.querySelector('.dialog.form .close').click()")
            await c.wait("!document.querySelector('.dialog.form')", 5)

            # -- phone, 390x844 ---------------------------------------------------
            await c.send("Emulation.setDeviceMetricsOverride", width=390, height=844,
                         deviceScaleFactor=2, mobile=True)
            await c.send("Page.reload")
            await c.wait("document.body.classList.contains('phone')", 15)
            check("the phone's ★ opens the same panel", await open_favorites(c, phone=True))
            check("it fits the phone screen, ✕ included", await c.eval(FITS.format(sel=".dialog.favs")),
                  await c.eval(f"JSON.stringify({PANEL}.getBoundingClientRect())"))
            check("and scrolls there too", await c.eval(f"{BODY}.scrollHeight > {BODY}.clientHeight + 100"))
            check("with no sideways scroll", await c.eval(f"{BODY}.scrollWidth <= {BODY}.clientWidth + 1"),
                  await c.eval(f"JSON.stringify([{BODY}.scrollWidth, {BODY}.clientWidth])"))
            check("and Restore on screen", await c.eval(
                f"(() => {{ const b = {PANEL}.querySelector('tbody .acts .btn').getBoundingClientRect();"
                " return b.right <= innerWidth && b.left >= 0; })()"))

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 120))
    assert not failures, "dialog failures:\n  " + "\n  ".join(failures)
