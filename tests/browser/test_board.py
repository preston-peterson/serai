"""The v3 board overview in a real browser: the ⊞ in the top bar's left group
swaps the attached view for a grid of cards with live pane tails; a card click
lands back on the attached view and attaches. Only the choice is remembered.

Shares test_smoke's throwaway tmux + serai + Chromium (pytest finds the fixture
by name), so it never touches the user's tmux server or config. Invented names
only: the fixture's demo/other sessions.
"""

from __future__ import annotations

import asyncio
import json

from .test_smoke import stack, page

BOARD = "[...document.querySelectorAll('.topbar .slots button')].find(b => b.textContent.includes('⊞'))"
CARD = ("[...document.querySelectorAll('#board .card')]"
        ".find(c => c.querySelector('.name').textContent === '{name}')")
LAYOUT = "document.querySelector('#app > .layout')"


def test_board_overview_and_back_to_attached(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)
            await c.wait(f"({BOARD}) !== undefined", 10)
            check("board starts hidden", await c.eval(
                "document.getElementById('board').hidden && !document.querySelector('#app > .layout').hidden"))

            await c.eval(f"({BOARD}).click()")
            check("two cards on the board", await c.wait(
                "document.querySelectorAll('#board .card').length === 2", 10))
            check("the attached view is hidden", await c.eval(f"({LAYOUT}).hidden"))
            check("the board button is lit", await c.eval(f"({BOARD}).classList.contains('on')"))
            check("a state chip per state present", await c.eval(
                "document.querySelectorAll('.board .strip .btn.sm').length >= 1"))
            check("count text on the strip's right", await c.eval(
                "document.querySelector('.board .strip .count').textContent.includes('sessions')"))
            check("the demo card previews its pane tail", await c.eval(
                CARD.format(name="demo") + ".querySelector('.term').textContent.includes('line-')"))
            check("previews are text, never HTML", await c.eval(
                "[...document.querySelectorAll('#board .card .term')].every(t => !t.querySelector('*'))"))

            await c.eval(CARD.format(name="demo") + ".click()")
            check("a card click brings back the attached view", await c.wait(
                f"!({LAYOUT}).hidden && document.getElementById('board').hidden && "
                f"!({BOARD}).classList.contains('on')", 10))
            check("the pane attached to the demo session", await c.wait(
                "document.querySelector('.pane .xterm-rows').innerText.includes('line-')", 10))

            await c.send("Page.navigate", url=f"http://127.0.0.1:{stack.port}/")
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)
            check("reload lands on the attached view (default)", await c.eval(
                "document.getElementById('board').hidden && !document.querySelector('#app > .layout').hidden"))

            await c.eval(f"({BOARD}).click()")
            await c.wait("document.querySelectorAll('#board .card').length === 2", 10)
            await c.send("Page.navigate", url=f"http://127.0.0.1:{stack.port}/")
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)
            check("the chosen board is remembered across a reload", await c.eval(
                "!document.getElementById('board').hidden && document.querySelector('#app > .layout').hidden"))

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 90))
    assert not failures, "board overview failures:\n  " + "\n  ".join(failures)
