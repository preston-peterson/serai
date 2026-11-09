"""The v3 session form in a real browser: + New creates a session and attaches it,
and the rail row's ⋮ reopens the same form to tag it -- the rail regrouping is
the only proof a user gets that the tag landed.

Shares test_smoke's throwaway tmux + serai + Chromium (pytest finds the fixture by
name), so it never touches the user's tmux server or config. Invented names only.
"""

from __future__ import annotations

import asyncio
import json

from .test_smoke import stack, page

ROWS = "document.querySelector('.pane .xterm-rows').innerText"

# The form's rows are addressed by the label a person reads, not by a test id.
FIELD = ("[...document.querySelectorAll('.dialog.form .field')]"
         ".find(f => f.querySelector('label').textContent === '{name}')")
BUTTON = ("[...document.querySelectorAll('.dialog-foot button')]"
          ".find(b => b.textContent === '{label}').click()")
ROW = ("[...document.querySelectorAll('.rail .row')]"
       ".find(r => r.querySelector('.name').textContent === 'made')")


def test_new_session_then_tag_it_from_the_rail(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    def field(name):
        return FIELD.format(name=name)

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)

            await c.eval("[...document.querySelectorAll('.topbar .slots button')]"
                         ".find(b => b.textContent.includes('+ New')).click()")
            await c.wait("!!document.querySelector('.dialog.form')", 10)
            check("a shell has no launch rule and no args", await c.eval(
                f"({field('Launch')}).hidden && ({field('Args')}).hidden"))
            check("shell is the kind on by default", await c.eval(
                f"({field('Kind')}).querySelector('button.on').textContent") == "shell")

            await c.eval(f"({field('Kind')}).querySelectorAll('button')[0].click()")
            await c.eval(f"({field('Label')}).querySelector('input').focus()")
            await c.send("Input.insertText", text="made")
            check("the label says what the session becomes", "shell-made" in await c.eval(
                f"({field('Label')}).querySelector('.help').textContent"))
            check("no saved profile to match", await c.eval(
                "document.querySelector('.dialog.form .note').hidden"))

            await c.eval(BUTTON.format(label="Create & attach"))
            check("the form closes once it has created", await c.wait(
                "!document.querySelector('.dialog.form')", 10))
            check("the new session is on the rail", await c.wait(f"!!{ROW}", 20))
            check("and it is live in the pane", await c.wait(f"{ROWS}.includes('$')", 20))

            await c.eval(f"({ROW}).querySelector('.more').click()")
            await c.wait("!!document.querySelector('.menu .item')", 10)
            await c.eval("[...document.querySelectorAll('.menu .item')]"
                         ".find(i => i.textContent.includes('Edit session')).click()")
            check("Edit reopens the same form", await c.wait(
                "document.querySelector('.dialog.form .dialog-head h2').textContent"
                " === 'Edit session · made'", 10))
            check("its label is prefilled", await c.eval(
                f"({field('Label')}).querySelector('input').value") == "made")
            check("its host is shown but locked", await c.eval(
                f"(() => {{ const s = {field('Host')}.querySelector('select');"
                " return s.disabled && s.value === 'local'; })()"))
            check("and it starts with nothing to warn about", await c.eval(
                "document.querySelector('.dialog.form .note.warn').hidden"))

            await c.eval(f"({field('Tags')}).querySelector('input').focus()")
            await c.send("Input.insertText", text="alpha")
            await c.key("Enter", "Enter", text="\r", vk=13)
            check("the tag becomes a chip", await c.eval(
                f"[...({field('Tags')}).querySelectorAll('.chip')]"
                ".some(x => x.textContent === 'alpha')"))

            await c.eval(BUTTON.format(label="Save"))
            check("Save closes the form", await c.wait("!document.querySelector('.dialog.form')", 15))
            check("and the rail regroups under it", await c.wait(
                "[...document.querySelectorAll('.rail .group-head')]"
                ".some(g => g.textContent.includes('alpha'))", 20))

            # Reopening Edit shows the saved tag as a chip (the control once drew
            # only after a change, so existing tags looked empty).
            await c.eval(f"({ROW}).querySelector('.more').click()")
            await c.wait("!!document.querySelector('.menu .item')", 10)
            await c.eval("[...document.querySelectorAll('.menu .item')]"
                         ".find(i => i.textContent.includes('Edit session')).click()")
            await c.wait("!!document.querySelector('.dialog.form')", 10)
            check("Edit opens with the existing tags as chips", await c.wait(
                f"[...({field('Tags')}).querySelectorAll('.chip')].some(x => x.textContent === 'alpha')", 5))
            await c.eval("document.querySelector('.dialog .close').click()")

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 120))
    assert not failures, "session form failures:\n  " + "\n  ".join(failures)
