"""The ported file browser in the v3 drawer, in a real browser: it follows the
focused session, opens through the chevron, navigates through the crumbs' path
edit, and creates / renames / deletes real files under the throwaway HOME
(invented names only).

Shares test_smoke's throwaway tmux + serai + Chromium (pytest finds the fixture
by name), so it never touches the user's tmux server or config.
"""

from __future__ import annotations

import asyncio
import os

from .test_smoke import stack, page

# The chevron is the toggle; a click on a crumb or a button never collapses.
TOGGLE = "document.querySelector('.drawer-handle .chev').click()"
KEYS = ("[...document.querySelectorAll('.drawer-body .ftable tbody tr[data-key]')]"
        ".map(r => r.dataset.key).join(',')")
HAS = lambda name: f"({KEYS}).split(',').includes('{name}')"   # noqa: E731
ROW = lambda name: (f"[...document.querySelectorAll('.drawer-body .ftable tbody tr[data-key]')]"
                    f".find(r => r.dataset.key === '{name}')")    # noqa: E731
MENU_ITEM = ("[...document.querySelectorAll('.menu .item')]"
             ".find(i => i.textContent.includes('{label}'))")


def test_files_drawer_ported(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def on_disk(rel, test):
        path = os.path.join(stack.home, "proj", *rel.split("/"))
        return await asyncio.get_running_loop().run_in_executor(None, test, path)

    async def run():
        c = await page(stack)
        proj = os.path.join(stack.home, "proj")
        os.makedirs(os.path.join(proj, "sub"))
        with open(os.path.join(proj, "a.txt"), "w") as f:
            f.write("demo\n")
        shot = os.environ.get("SERAI_TEST_SHOTS")
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 1", 15)
            check("the drawer is mounted and shows", await c.eval(
                "!document.getElementById('drawer').hidden"))
            check("collapsed by default", await c.eval(
                "!document.getElementById('drawer').classList.contains('open')"))

            await c.eval("[...document.querySelectorAll('.rail .row')]"
                         ".find(r => r.querySelector('.name').textContent === 'demo').click()")
            await c.wait("document.querySelectorAll('.pane').length === 1", 10)
            check("it follows the focused session's host", await c.wait(
                "document.querySelector('.drawer-handle .crumbs').textContent.includes('local')", 10))

            await c.eval(TOGGLE)
            check("the chevron opens the drawer", await c.wait(
                "document.getElementById('drawer').classList.contains('open')", 5))

            # Path edit: click the crumbs' blank area, type the target, Enter to go.
            await c.eval("document.querySelector('.drawer-handle .crumbs').click()")
            await c.wait("!!document.querySelector('.path-input')", 5)
            await c.eval("(() => { const i = document.querySelector('.path-input');"
                         "i.value = '~/proj';"
                         "i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()")
            check(".. first, dirs first, both entries", await c.wait(
                f"({KEYS}) === '..,sub,a.txt'", 10),
                str(await c.eval(f"({KEYS})")))
            if shot:
                os.makedirs(shot, exist_ok=True)
                await c.shot(os.path.join(shot, "files-drawer.png"))

            # New folder: right-click an empty area -> menu -> dialog.prompt.
            await c.eval("(() => { const t = document.querySelector('.ftable');"
                         "t.dispatchEvent(new MouseEvent('contextmenu', {bubbles: true, cancelable: true,"
                         "clientX: t.getBoundingClientRect().left + 20, clientY: window.innerHeight - 60})); })()")
            await c.wait(MENU_ITEM.format(label="New folder") + " && true", 5)
            await c.eval(f"({MENU_ITEM.format(label='New folder')}).click()")
            await c.wait("!!document.querySelector('.dialog .field input')", 5)
            await c.eval("(() => { const i = document.querySelector('.dialog .field input');"
                         "i.value = 'made';"
                         "i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()")
            await c.wait(f"!document.querySelector('.dialog') && ({HAS('made')})", 10)
            check("made exists on disk", await on_disk("made", os.path.isdir))

            # Rename: select the row, F2 puts an inline input in it, Enter commits.
            await c.eval(f"(({ROW('made')}) || document.body).dispatchEvent("
                         "new MouseEvent('click', {bubbles: true}))")
            await c.eval("(() => { const t = document.querySelector('.ftable');"
                         "t.focus(); t.dispatchEvent(new KeyboardEvent('keydown',"
                         "{key: 'F2', bubbles: true})); })()")
            await c.wait("!!document.querySelector('.inline-edit')", 5)
            await c.eval("(() => { const i = document.querySelector('.inline-edit');"
                         "i.value = 'renamed';"
                         "i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true})); })()")
            await c.wait(f"!document.querySelector('.inline-edit') && ({HAS('renamed')})", 10)
            check("renamed exists on disk", await on_disk("renamed", os.path.isdir))
            check("made is gone from disk", await on_disk("made", lambda p: not os.path.exists(p)))

            # Delete: right-click the row -> menu -> the v3 confirm (its danger
            # button lives in the dialog footer; v2's inline confirmAction is gone).
            await c.eval(f"(({ROW('renamed')}) || document.body).dispatchEvent("
                         "new MouseEvent('contextmenu', {bubbles: true, cancelable: true,"
                         "clientX: 220, clientY: 320}))")
            await c.wait(MENU_ITEM.format(label="Delete") + " && true", 5)
            await c.eval(f"({MENU_ITEM.format(label='Delete')}).click()")
            await c.wait("!!document.querySelector('.dialog .dialog-foot .danger')", 5)
            await c.eval("document.querySelector('.dialog .dialog-foot .danger').click()")
            await c.wait(f"!document.querySelector('.dialog') && !({HAS('renamed')})", 10)
            check("gone from disk", await on_disk("renamed", lambda p: not os.path.exists(p)))

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]

            def exc_text(e):     # the message, not the stack — it fits where 300 chars don't
                d = e["params"]["exceptionDetails"]
                ex = d.get("exception") or {}
                return (f'{d.get("text")} at {d.get("url", "?").rsplit("/", 1)[-1]}:'
                        f'{d.get("lineNumber")} {ex.get("description") or ex.get("value") or ""}'[:400])

            check("no JS exceptions", not errs, " | ".join(map(exc_text, errs)))
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 120))
    assert not failures, "files drawer failures:\n  " + "\n  ".join(failures)
