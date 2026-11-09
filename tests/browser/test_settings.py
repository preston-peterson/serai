"""The v3 Settings dialog in a real browser: one ⚙ opens one dialog of tabs, every
control applies the moment it changes (there is no Save), and reopening shows the
stored answer rather than the default.

Shares test_smoke's throwaway tmux + serai + Chromium (pytest finds the fixture by
name), so it never touches the user's tmux server or config. Invented names only.
"""

from __future__ import annotations

import asyncio
import json
import os

from .test_smoke import stack, page

# Rows are addressed by the label a person reads, not by a test id.
FIELD = ("[...document.querySelectorAll('.dialog .field')]"
         ".find(f => f.querySelector('label').textContent === '{name}')")
GEAR = "[...document.querySelectorAll('.topbar .slots button')]" \
       ".find(b => b.textContent.includes('⚙')).click()"
DONE = "[...document.querySelectorAll('.dialog-foot button')]" \
       ".find(b => b.textContent === 'Done').click()"
COPY = FIELD.format(name="Copy on select") + " .querySelector('button.switch')"
SIZE = FIELD.format(name="Size") + " .querySelector('input')"
PANEL = ".dialog .p:not([hidden])"


def test_settings_applies_as_you_click_and_reopens_with_it(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await page(stack)
        shots = os.environ.get("SERAI_TEST_SHOTS")
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)
            await c.wait("[...document.querySelectorAll('.topbar .slots button')]"
                         ".some(b => b.textContent.includes('⚙'))", 10)
            await c.eval(GEAR)
            await c.wait("!!document.querySelector('.dialog .tabs')", 10)

            check("three tabs in order", await c.eval(
                "[...document.querySelectorAll('.dialog .tab')]"
                ".map(t => t.textContent).join(',')") == "Terminal,Appearance,Updates")
            check("Terminal is the tab that opens", await c.eval(
                "document.querySelector('.dialog .tab.active').textContent") == "Terminal")
            check("the two panels you are not looking at are hidden", await c.eval(
                "[...document.querySelectorAll('.dialog-body .p')].filter(p => p.hidden).length") == 2)
            check("copy on select starts off", await c.eval(
                f"!({COPY}).classList.contains('on')"))

            await c.eval(f"({COPY}).click()")
            check("the switch flips as soon as you click it", await c.eval(
                f"({COPY}).classList.contains('on')"))

            await c.eval(f"(() => {{ const i = ({SIZE});"
                         " i.value = '17'; i.dispatchEvent(new Event('change', {bubbles: true})); })()")
            check("and the number took the typed size", await c.eval(
                f"({SIZE}).value") == "17")

            await c.eval(DONE)
            check("Done closes the dialog", await c.wait("!document.querySelector('.dialog')", 10))
            stored = await c.eval("JSON.parse(localStorage.getItem('serai.v3.prefs'))") or {}
            check("copyOnSelect reached storage as true", stored.get("copyOnSelect") is True,
                  repr(stored)[:160])
            check("fontSize reached storage as 17", stored.get("fontSize") == 17,
                  repr(stored)[:160])

            await c.eval(GEAR)
            await c.wait("!!document.querySelector('.dialog .tabs')", 10)
            check("reopened with the switch still on", await c.wait(f"({COPY}).classList.contains('on')", 5))
            check("and the size still its own", await c.eval(f"({SIZE}).value") == "17")
            if shots:
                os.makedirs(shots, exist_ok=True)
                await c.shot(os.path.join(shots, "settings-terminal.png"))

            await c.eval("[...document.querySelectorAll('.dialog .tab')]"
                         ".find(t => t.textContent === 'Updates').click()")
            check("switching tabs only moves the panel", await c.wait(
                f"document.querySelector('.dialog .tab.active').textContent === 'Updates'"
                f" && !!document.querySelector('{PANEL}')", 5))
            if shots:
                await c.shot(os.path.join(shots, "settings-updates.png"))
            seg_sel = f"{PANEL} .seg button"
            check("the interval has four choices, one on", await c.eval(
                f"[...document.querySelectorAll('{seg_sel}')].length === 4 && "
                f"[...document.querySelectorAll('{seg_sel}')]"
                ".filter(b => b.classList.contains('on')).length === 1"))

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 120))
    assert not failures, "settings dialog failures:\n  " + "\n  ".join(failures)
