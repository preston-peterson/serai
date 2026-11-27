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


def test_an_agent_directory_follows_its_label(stack):
    """+ New for an agent fills Directory with ~/git/<label> as a real value that
    follows the label until you type in the field (v2's rule; 3.0.0 had shrunk it
    to a placeholder, so the form showed a path and the session started in ~).
    Emptying the field hands it back to the label, and creating with it empty
    still means ~/git/<label>. The directory doesn't exist here, so `cd` fails
    and the agent never starts: the session drops to its shell, still alive."""
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    def field(name):
        return FIELD.format(name=name)

    DIR = f"({field('Directory')}).querySelector('input')"
    LABEL = f"({field('Label')}).querySelector('input')"

    async def kind(c, k):
        await c.eval(f"[...({field('Kind')}).querySelectorAll('button')].find(b => b.textContent === '{k}').click()")

    async def run():
        c = await page(stack)
        try:
            await c.wait("document.querySelectorAll('.rail .row').length >= 2", 15)
            await c.eval("[...document.querySelectorAll('.topbar .slots button')]"
                         ".find(b => b.textContent.includes('+ New')).click()")
            await c.wait("!!document.querySelector('.dialog.form')", 10)

            await kind(c, "claude")
            await c.eval(f"{LABEL}.focus()")
            await c.send("Input.insertText", text="pro")
            check("the directory is filled from the label", await c.eval(f"{DIR}.value") == "~/git/pro",
                  await c.eval(f"{DIR}.value"))
            await c.send("Input.insertText", text="be")
            check("and follows it", await c.eval(f"{DIR}.value") == "~/git/probe")

            await kind(c, "shell")
            check("a shell starts in ~ unless told", await c.eval(f"{DIR}.value") == ""
                  and await c.eval(f"{DIR}.placeholder") == "~ (home) if empty")
            await kind(c, "claude")
            check("back to an agent, it fills again", await c.eval(f"{DIR}.value") == "~/git/probe")

            await c.eval(f"{DIR}.focus(); {DIR}.select()")
            await c.send("Input.insertText", text="~/elsewhere")
            await c.eval(f"{LABEL}.focus()")
            await c.send("Input.insertText", text="2")
            check("a typed directory is left alone", await c.eval(f"{DIR}.value") == "~/elsewhere")

            await c.eval(f"{DIR}.focus(); {DIR}.select()")
            await c.key("Backspace", "Backspace", vk=8)
            check("emptying it doesn't refill under the caret", await c.eval(f"{DIR}.value") == "")
            check("and the placeholder says what empty means", await c.eval(f"{DIR}.placeholder") == "~/git/probe2")

            await c.eval(BUTTON.format(label="Create & attach"))
            await c.wait("!document.querySelector('.dialog.form')", 10)
            got = ""
            for _ in range(40):
                got = stack.tmux("display-message", "-p", "-t", "=cc-probe2:", "#{@serai_dir}").stdout.strip()
                if got:
                    break
                await asyncio.sleep(0.25)
            check("the session's directory is ~/git/<label>", got.endswith("git/probe2"), repr(got))
            await asyncio.sleep(1.5)
            alive = stack.tmux("has-session", "-t", "=cc-probe2").returncode == 0
            pane = stack.tmux("capture-pane", "-p", "-t", "=cc-probe2:").stdout
            check("a missing directory leaves the session alive at its shell", alive, pane[-300:])
            print("pane after a missing directory:\n" + pane.strip())

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 120))
    assert not failures, "agent directory failures:\n  " + "\n  ".join(failures)
