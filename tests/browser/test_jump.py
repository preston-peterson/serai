"""The v3 jump palette and the ★ favorites panel in a real browser: find the
demo shell with the palette's pill, Enter attaches it, its rail row's ⋮ stars
it, and the ★ panel then lists it with an Attach button.

Shares test_smoke's throwaway tmux + serai + Chromium (pytest finds the fixture
by name), so it never touches the user's tmux server or config. Invented names
only (shell-demo / shell-other come from that fixture).
"""

from __future__ import annotations

import asyncio
import json

from .test_smoke import stack, page

ROWS = "document.querySelector('.pane .xterm-rows').innerText"
PILL = "[...document.querySelectorAll('.topbar .slots button')].find(b => b.textContent.includes('jump to session'))"
STAR = "[...document.querySelectorAll('.topbar .slots button')].find(b => b.textContent.includes('★'))"
DEMO_ROW = ("[...document.querySelectorAll('.rail .row')]"
            ".find(r => r.querySelector('.name').textContent === 'demo')")
FAVS_ROW = "document.querySelector('.dialog.favs tbody tr')"


def test_jump_to_demo_then_star_it_then_attach(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)

            await c.eval(f"({PILL}).click()")
            await c.wait("!!document.querySelector('.dialog.palette .pin input')", 10)
            check("the palette opens with the caret in the input", await c.eval(
                "document.activeElement === document.querySelector('.dialog.palette .pin input')"))
            await c.send("Input.insertText", text="dem")
            check("demo ranks first", await c.wait(
                "document.querySelector('.dialog.palette .pr.first .name').textContent.includes('demo')", 5))
            check("the match is bolded", await c.wait(
                "!!document.querySelector('.dialog.palette .pr.first .name b')", 5))
            check("a second pane could be had with ⇧↵", await c.eval(
                "[...document.querySelectorAll('.dialog.palette .keys span')]"
                ".some(s => s.textContent.includes('second pane'))"))
            await c.key("Enter", "Enter", text="\r", vk=13)
            check("Enter closed the palette", await c.wait(
                "!document.querySelector('.dialog.palette')", 10))
            check("the pane attached shell-demo", await c.wait(f"{ROWS}.includes('line-')", 15))

            await c.eval(f"({DEMO_ROW}).querySelector('.more').click()")
            await c.wait("!!document.querySelector('.menu .item')", 10)
            await c.eval("[...document.querySelectorAll('.menu .item')]"
                         ".find(i => i.textContent.includes('Add to favorites')).click()")
            check("the ★ button counts the favorite", await c.wait(
                f"({STAR}).textContent.replace(/\\s/g, '') === '★1'", 15))

            await c.eval(f"({STAR}).click()")
            await c.wait("!!document.querySelector('.dialog.favs')", 10)
            check("one row, demo, with an Attach button", await c.eval(
                f"[...document.querySelectorAll('.dialog.favs tbody tr')].length === 1 "
                f"&& {FAVS_ROW}.textContent.includes('demo') "
                f"&& [...({FAVS_ROW}).querySelectorAll('button')].some(b => b.textContent === 'Attach')"))
            check("it shows as running", await c.wait(
                f"!!({FAVS_ROW}) .querySelector('.up')", 12))
            check("the note is there", await c.eval(
                "document.querySelector('.dialog.favs .note').textContent.includes('use Forget')"))
            await c.eval(f"[...({FAVS_ROW}).querySelectorAll('button')]"
                         ".find(b => b.textContent === 'Attach').click()")
            check("Attach closed the panel", await c.wait(
                "!document.querySelector('.dialog.favs')", 10))
            check("demo is still the pane's session", await c.wait(
                "document.querySelector('.pane .pane-head .name').textContent === 'demo'", 10))

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 120))
    assert not failures, "jump + favorites failures:\n  " + "\n  ".join(failures)


def test_palette_shortcuts_never_leak_into_the_terminal(stack):
    """Ctrl+K inside a terminal is readline's kill-line and must stay a shell
    keystroke; Ctrl+Shift+P opens the palette without typing ^P into the pane."""
    rows = "document.querySelector('.pane .xterm-rows').innerText"

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.row').length >= 2", 15)
            await c.eval("[...document.querySelectorAll('.row')].find(r => r.querySelector('.name').textContent==='other').click()")
            await c.wait(f"{rows}.includes('$')", 10)
            await c.eval("document.querySelector('.pane .xterm-helper-textarea').focus()")
            # The fixture's shell is sh (dash: no line editing); readline's
            # Ctrl+A / Ctrl+K need bash.
            await c.send("Input.insertText", text="exec bash --norc --noprofile")
            await c.key("Enter", "Enter", text="\r", vk=13)
            await asyncio.sleep(0.5)
            await c.send("Input.insertText", text="echo kept-$((40+2))")
            await c.key("KeyA", "a", mods=2, vk=65)          # Ctrl+A: start of line
            await c.key("KeyK", "k", mods=2, vk=75)          # Ctrl+K: kill to end
            await asyncio.sleep(0.3)
            assert not await c.eval("!!document.querySelector('.dialog.palette')"), \
                "Ctrl+K in a terminal must not open the palette"
            await c.key("Enter", "Enter", text="\r", vk=13)
            await c.send("Input.insertText", text="echo marker-$((1+1))")
            await c.key("Enter", "Enter", text="\r", vk=13)
            await c.wait(f"{rows}.includes('marker-2')", 5)
            assert "kept-42" not in await c.eval(rows), "Ctrl+K reached the shell and killed the line"

            await c.key("KeyP", "P", mods=2 | 8, vk=80)      # Ctrl+Shift+P
            assert await c.wait("!!document.querySelector('.dialog.palette')", 3)
            await c.key("Escape", "Escape", vk=27)
            await asyncio.sleep(0.2)
            await c.send("Input.insertText", text="echo after-palette")
            await c.key("Enter", "Enter", text="\r", vk=13)
            await c.wait(f"{rows}.includes('after-palette\\n')", 5)
            lines = (await c.eval(rows)).splitlines()
            assert not any(l.strip().startswith("$ echo marker") and "after" in l for l in lines), \
                "Ctrl+Shift+P leaked ^P (history recall) into the pane"
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 60))
