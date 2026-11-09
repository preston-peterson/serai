"""The v3 Account dialog in a real browser with auth ON: sign in through the
cover, open the avatar menu, change the password in Profile, sign out, and prove
the new one is the one that gets you back in.

Its own throwaway tmux + serai + Chromium -- the pattern from test_smoke, with
SERAI_AUTH on and the admin created in-process (into this test's own
SERAI_CONFIG_DIR) before serai starts, so the cover opens on "Sign in" rather
than on first-run setup. It never touches the user's tmux server, config, or
user store. Invented names only.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import pytest

from .cdp import CDP
from serai import auth

FLATPAK = "io.github.ungoogled_software.ungoogled_chromium"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OLD_PW = "first-long-pw"
NEW_PW = "second-long-pw"
DAVE_PW = "daves-long-pw"
# What a generated reset password has to look like, and what it must not contain.
RESET_PW_RE = re.compile(r"[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}-[A-Za-z0-9]{4}")
LOOKALIKE = set("Il1O0")


def browser_cmd() -> list[str] | None:
    if os.environ.get("SERAI_TEST_BROWSER"):
        return shlex.split(os.environ["SERAI_TEST_BROWSER"])
    for b in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        if shutil.which(b):
            return [shutil.which(b)]
    if shutil.which("flatpak") and subprocess.run(["flatpak", "info", FLATPAK],
                                                  capture_output=True).returncode == 0:
        return ["flatpak", "run", FLATPAK]
    return None


pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed"),
    pytest.mark.skipif(browser_cmd() is None, reason="no Chromium found (set SERAI_TEST_BROWSER)"),
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def login_works(port: int, username: str, password: str) -> bool:
    """Ask serai directly, outside the browser, whether these credentials pass --
    the proof that a refused change never reached the server."""
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status == 200
    except urllib.error.HTTPError:
        return False


class Stack:
    """tmux + serai (auth ON) + Chromium, all throwaway, torn down together."""

    def __init__(self, tmp):
        self.sock = tempfile.mkdtemp(prefix="serai-ac-", dir="/tmp")   # tmux socket paths are short
        self.home = os.path.join(tmp, "home")
        os.makedirs(self.home)
        self.port, self.dbg = free_port(), free_port()
        self.profile = f"/tmp/serai-account-{os.getpid()}-{self.port}"
        self.cfg = os.path.join(tmp, "cfg")
        self.procs = []
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TMUX_TMPDIR=self.sock, HOME=self.home, SHELL="/bin/sh", ENV="", PS1="$ ",
                        SERAI_AUTH="on", SERAI_TLS="off", SERAI_SKIP_PATH_PROBE="1",
                        SERAI_CONFIG_DIR=self.cfg,
                        XDG_CONFIG_HOME=os.path.join(self.home, ".config"))

    def tmux(self, *a):
        return subprocess.run(["tmux", "-f", "/dev/null", *a], env=self.env, capture_output=True, text=True)

    def start(self, log_dir):
        self.log_dir = log_dir
        self.procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "serai.main:app", "--host", "127.0.0.1", "--port", str(self.port)],
            cwd=REPO, env=self.env, start_new_session=True,
            stdout=open(os.path.join(log_dir, "serai.log"), "w"), stderr=subprocess.STDOUT))
        extra = ["--no-sandbox"] if os.environ.get("CI") else []
        self.procs.append(subprocess.Popen(
            [*browser_cmd(), "--headless=new", "--disable-gpu", "--hide-scrollbars", *extra,
             f"--remote-debugging-port={self.dbg}", f"--user-data-dir={self.profile}",
             "--window-size=1300,820", "about:blank"],
            start_new_session=True, stdout=open(os.path.join(log_dir, "browser.log"), "w"),
            stderr=subprocess.STDOUT))
        for _ in range(80):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.port}/api/auth/status", timeout=1)
                return
            except Exception:
                time.sleep(0.25)
        raise RuntimeError("serai did not start (see serai.log)")

    def log_tails(self, lines=40) -> str:
        """The end of serai's and the browser's logs, printed at teardown so a
        failing test shows them (pytest only displays captured output on
        failure) and nobody has to go and open files under /tmp."""
        out = []
        for name in ("serai.log", "browser.log"):
            try:
                with open(os.path.join(self.log_dir, name), errors="replace") as f:
                    tail = f.readlines()[-lines:]
            except (OSError, AttributeError):
                continue
            out.append(f"----- {name} (last {len(tail)} lines) -----\n" + "".join(tail))
        return "\n".join(out)

    def stop(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, 15)
            except ProcessLookupError:
                pass
        for p in self.procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, 9)
        self.tmux("kill-server")
        shutil.rmtree(self.sock, ignore_errors=True)
        shutil.rmtree(self.profile, ignore_errors=True)


@pytest.fixture
def stack(tmp_path, monkeypatch):
    s = Stack(str(tmp_path))
    # The users have to exist before the cover opens, so create them here -- in
    # this process, against this stack's own config dir -- instead of claiming
    # them through the first-run setup form. carol is the non-admin half.
    monkeypatch.setenv("SERAI_CONFIG_DIR", s.cfg)
    auth.add_user("alice", OLD_PW, admin=True)
    auth.add_user("carol", OLD_PW, admin=False)
    try:
        s.start(str(tmp_path))
        yield s
    finally:
        print(s.log_tails())
        s.stop()


async def page(stack) -> CDP:
    c = await CDP.connect(stack.dbg)
    for d in ("Page", "Runtime"):
        await c.send(f"{d}.enable")
    await c.send("Browser.grantPermissions", origin=f"http://127.0.0.1:{stack.port}",
                 permissions=["clipboardReadWrite", "clipboardSanitizedWrite"])
    await c.send("Emulation.setFocusEmulationEnabled", enabled=True)
    await c.send("Page.navigate", url=f"http://127.0.0.1:{stack.port}/")
    return c


COVER = ".auth-cover"
AVATAR = "document.querySelector('.topbar button.avatar')"
MENU = "[...document.querySelectorAll('.menu .item')]"
FIELD = ("[...document.querySelectorAll('.dialog .field')]"
         ".find(f => f.querySelector('label').textContent === '{name}')")
BODY_BTN = ("[...document.querySelectorAll('.dialog-body button')]"
            ".find(b => b.textContent === '{label}')")
FOOT_BTN = ("[...document.querySelectorAll('.dialog-foot button')]"
            ".find(b => b.textContent === '{label}')")
TAB = "[...document.querySelectorAll('.dialog .tab')]"
TOAST = "[...document.querySelectorAll('.toasts .toast')].map(t => t.textContent).join('|')"


def filled(label: str, value: str) -> str:
    """Set a dialog input directly -- these are password fields, and typing into
    one through CDP would put the value in the browser log as well as the page."""
    return (f"(() => {{ const i = {FIELD.format(name=label)}.querySelector('input');"
            f" i.value = {json.dumps(value)}; }})()")


def test_account_password_change_then_sign_in_again(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await page(stack)
        shots = os.environ.get("SERAI_TEST_SHOTS")

        async def sign_in(username, password):
            # The login card has exactly two inputs: username, then password.
            await c.wait(f"!!document.querySelector('{COVER} .auth-card .input')", 20)
            await c.eval(f"[...document.querySelectorAll('{COVER} .auth-card .input')][0].focus()")
            await c.send("Input.insertText", text=username)
            await c.eval(f"[...document.querySelectorAll('{COVER} .auth-card .input')][1].focus()")
            await c.send("Input.insertText", text=password)
            await c.eval(f"document.querySelector('{COVER} .auth-card button[type=submit]').click()")
            await c.wait(f"!document.querySelector('{COVER}')", 20)

        async def open_account():
            await c.wait(f"{AVATAR} && {AVATAR}.offsetParent !== null", 20)
            await c.eval(f"({AVATAR}).click()")
            await c.wait("!!document.querySelector('.menu .item')", 10)
            await c.eval(f"{MENU}.find(i => i.textContent.includes('Account…')).click()")
            await c.wait("!!document.querySelector('.dialog .tabs')", 10)

        async def log_out():
            await open_account()
            # Log out reloads the page, which can cut this evaluation short.
            try:
                await c.eval(f"({FOOT_BTN.format(label='Log out')}).click()")
            except RuntimeError:
                pass
            await c.wait(f"!!document.querySelector('{COVER}')", 20)

        try:
            # --- in through the cover ---------------------------------------
            check("the cover is up while auth is on", await c.wait(f"!!document.querySelector('{COVER}')", 15))
            await sign_in("alice", OLD_PW)
            check("the avatar shows who is signed in", await c.wait(
                f"{AVATAR} && {AVATAR}.textContent === 'A'", 10))

            # --- avatar menu > Account… -------------------------------------
            await open_account()

            check("three tabs, the admin two badged", await c.eval(
                f"{TAB}.length === 3 && {TAB}.filter(t => t.querySelector('.badge')).length === 2"))
            check("Profile opens", await c.eval(
                f"document.querySelector('.dialog .tab.active').firstChild.textContent") == "Profile")
            check("footer is Log out left, Close right", await c.eval(
                "[...document.querySelectorAll('.dialog-foot')][0]"
                ".querySelector('button').textContent") == "Log out")
            check("signed in as alice, ADMIN", await c.wait(
                f"{FIELD.format(name='Signed in as')}.textContent.includes('alice')"
                f" && !!({FIELD.format(name='Signed in as')}).querySelector('span.kind.cc')", 5))
            check("password fields are big enough for a phone", await c.eval(
                f"getComputedStyle(({FIELD.format(name='New password')}).querySelector('input'))"
                ".fontSize") == "16px")

            # --- a change that must not be sent ----------------------------
            await c.eval(filled("Current password", OLD_PW))
            await c.eval(filled("New password", NEW_PW))
            await c.eval(filled("Confirm", "something-else"))
            await c.eval(f"({BODY_BTN.format(label='Change password')}).click()")
            check("a mismatch is refused", await c.wait(f"({TOAST}).includes(\"don't match\")", 6))
            check("and nothing reached the server", login_works(stack.port, "alice", OLD_PW))

            await c.eval(filled("New password", "short"))
            await c.eval(filled("Confirm", "short"))
            await c.eval(f"({BODY_BTN.format(label='Change password')}).click()")
            check("too short is refused too", await c.wait(f"({TOAST}).includes('At least 8')", 6))
            check("still not sent", login_works(stack.port, "alice", OLD_PW))

            # --- and one the server refuses --------------------------------
            await c.eval(filled("Current password", "wrong-current-pw"))
            await c.eval(filled("New password", NEW_PW))
            await c.eval(filled("Confirm", NEW_PW))
            await c.eval(f"({BODY_BTN.format(label='Change password')}).click()")
            check("a wrong current password is named", await c.wait(
                f"({TOAST}).includes('current password is not right')", 6))
            check("and it did not take", login_works(stack.port, "alice", OLD_PW))

            await c.eval(filled("Current password", OLD_PW))
            await c.eval(filled("New password", NEW_PW))
            await c.eval(filled("Confirm", NEW_PW))
            await c.eval(f"({BODY_BTN.format(label='Change password')}).click()")
            check("the right change is answered", await c.wait(f"({TOAST}).includes('password changed')", 8))
            check("the fields are cleared after it", await c.eval(
                f"[...({FIELD.format(name='New password')}).parentElement.querySelectorAll('input')]"
                ".every(i => i.value === '')"))
            if shots:
                os.makedirs(shots, exist_ok=True)
                await c.shot(os.path.join(shots, "account-profile.png"))

            # --- the other two tabs an admin gets --------------------------
            await c.eval(f"{TAB}[1].click()")
            check("Users lists this account", await c.wait(
                "!!document.querySelector('.dialog .table') "
                "&& [...document.querySelectorAll('.dialog .table tbody tr')]"
                ".some(r => r.textContent.includes('alice · you'))", 10))
            check("your own row has no Remove, anyone else's does", await c.eval(
                "[...document.querySelectorAll('.dialog .table tbody tr')].every(r =>"
                " [...r.querySelectorAll('button')].some(b => b.textContent === 'Remove…')"
                " === !r.textContent.includes(' · you'))"))
            check("the add row is there", await c.eval(
                "!!document.querySelector('.dialog .addrow input')"
                " && [...document.querySelectorAll('.dialog .addrow button')]"
                ".some(b => b.textContent.trim() === 'Add')"))

            # -- reset someone else's password, shown once ------------------
            await c.eval("[...document.querySelectorAll('.dialog .table tbody tr')]"
                         ".find(r => r.textContent.includes('carol'))"
                         ".querySelector('button').click()")
            check("the reset opens its own dialog", await c.wait(
                "document.querySelectorAll('.dialog').length === 2 "
                "&& !!document.querySelector('.dialog .cmdline')", 10))
            reset_pw = await c.eval("[...document.querySelectorAll('.dialog')].pop()"
                                    ".querySelector('.cmdline').textContent")
            check("in four groups of four", bool(RESET_PW_RE.fullmatch(reset_pw or "")),
                  f"({len(reset_pw or '')} chars)")
            check("with no look-alike characters", not set(reset_pw or "") & LOOKALIKE,
                  f"({len(set(reset_pw or ''))} distinct)")
            check("and it is not the throwaway one", reset_pw != OLD_PW)
            await c.eval(f"({FOOT_BTN.format(label='Copy')}).click()")
            check("Copy copies it", await c.wait(f"({TOAST}).includes('copied')", 6))
            check("the clipboard holds that same string",
                  await c.eval("navigator.clipboard.readText()") == reset_pw)
            check("it says it is shown once", await c.eval(
                "[...document.querySelectorAll('.dialog')].pop().textContent.includes('Shown once')"))
            await c.eval(f"({FOOT_BTN.format(label='Done')}).click()")
            check("Done closes it, back to the account dialog", await c.wait(
                "document.querySelectorAll('.dialog').length === 1", 6))

            # -- add someone, then forget them ------------------------------
            await c.eval("document.querySelector('.dialog .addrow input').focus()")
            await c.send("Input.insertText", text="dave")
            await c.eval("document.querySelectorAll('.dialog .addrow input')[1].focus()")
            await c.send("Input.insertText", text=DAVE_PW)
            check("the admin tick starts off", await c.eval(
                "!document.querySelector('.dialog .addrow .check').classList.contains('on')"))
            await c.eval("[...document.querySelectorAll('.dialog .addrow button')]"
                         ".find(b => b.textContent.trim() === 'Add').click()")
            check("Add adds a user", await c.wait(
                "[...document.querySelectorAll('.dialog .table tbody tr')]"
                ".some(r => r.textContent.includes('dave') && r.textContent.includes('USER'))", 10))
            await c.eval("[...document.querySelectorAll('.dialog .table tbody tr')]"
                         ".find(r => r.textContent.includes('dave'))"
                         ".querySelector('button.danger').click()")
            check("Remove… asks first", await c.wait(
                "document.querySelectorAll('.dialog').length === 2", 6))
            await c.eval(f"({FOOT_BTN.format(label='Remove')}).click()")
            check("and the confirm forgets them", await c.wait(
                "![...document.querySelectorAll('.dialog .table tbody tr')]"
                ".some(r => r.textContent.includes('dave'))", 10))
            if shots:
                await c.shot(os.path.join(shots, "account-users.png"))

            await c.eval(f"{TAB}[2].click()")
            check("Network is seeded from what is in effect", await c.wait(
                f"({FIELD.format(name='Listen address')}).querySelector('input').value === '127.0.0.1'", 8))
            check("it says a restart is coming", await c.eval(
                "!!document.querySelector('.dialog .note.warn')"))

            await c.eval(f"({FOOT_BTN.format(label='Close')}).click()")
            check("Close closes it", await c.wait("!document.querySelector('.dialog')", 6))

            # --- out, then back in with the new password -------------------
            await log_out()
            check("Log out brings the cover back", await c.eval(f"!!document.querySelector('{COVER}')"))
            await sign_in("alice", NEW_PW)
            check("the new password signs in", await c.eval(f"!document.querySelector('{COVER}')"))
            check("and the old one is gone", not login_works(stack.port, "alice", OLD_PW))

            # --- a non-admin gets the Profile tab and nothing else ---------
            # v2 shipped the "admin section visible to a normal user" bug twice
            # (the JS hid it, a CSS display rule beat [hidden]). Here the admin
            # panels are never built, so there is nothing left for CSS to undo.
            await log_out()
            await sign_in("carol", reset_pw)
            check("carol gets in with the reset password", await c.eval(f"!document.querySelector('{COVER}')"))
            check("and not the one she was made with", not login_works(stack.port, "carol", OLD_PW))
            await open_account()
            check("a non-admin sees one tab", await c.wait(
                f"{TAB}.length === 1 && {TAB}[0].firstChild.textContent === 'Profile'", 8))
            check("with no admin panel behind it", await c.eval(
                "document.querySelectorAll('.dialog .table, .dialog .addrow, .dialog .note.warn')"
                ".length") == 0)
            check("and is marked USER", await c.eval(
                f"!!({FIELD.format(name='Signed in as')}).querySelector('span.kind.sh')"))

            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 150))
    assert not failures, "account dialog failures:\n  " + "\n  ".join(failures)
