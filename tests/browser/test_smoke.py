"""v3 browser smoke: a real headless Chromium against a throwaway serai and a
throwaway tmux server (TMUX_TMPDIR, no user config, invented session names).
Never the user's tmux server or config.

Skips unless a Chromium is found: SERAI_TEST_BROWSER (a command line), then
chromium / google-chrome on PATH, then the ungoogled-chromium flatpak.
Screenshots go to SERAI_TEST_SHOTS when set (never into the repo).
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

import pytest

from .cdp import CDP

FLATPAK = "io.github.ungoogled_software.ungoogled_chromium"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


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


class Stack:
    """tmux + serai + Chromium, all throwaway, torn down together."""

    def __init__(self, tmp):
        self.sock = tempfile.mkdtemp(prefix="serai-sm-", dir="/tmp")   # tmux socket paths are short
        self.home = os.path.join(tmp, "home")
        os.makedirs(self.home)
        self.port, self.dbg = free_port(), free_port()
        self.profile = f"/tmp/serai-smoke-{os.getpid()}-{self.port}"
        self.procs = []
        self.env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        self.env.update(TMUX_TMPDIR=self.sock, HOME=self.home, SHELL="/bin/sh", ENV="", PS1="$ ",
                        SERAI_AUTH="off", SERAI_TLS="off", SERAI_SKIP_PATH_PROBE="1",
                        SERAI_CONFIG_DIR=os.path.join(tmp, "cfg"),
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
        """The end of serai's and the browser's logs. Printed at teardown, so a
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
def stack(tmp_path):
    s = Stack(str(tmp_path))
    s.tmux("new-session", "-d", "-s", "shell-demo", "-x", "120", "-y", "36", "sh")
    s.tmux("send-keys", "-t", "shell-demo", "i=1; while [ $i -le 80 ]; do echo line-$i; i=$((i+1)); done", "Enter")
    s.tmux("new-session", "-d", "-s", "shell-other", "sh")
    s.tmux("set-option", "-t", "shell-demo", "@serai_tags", "demo")
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


ROWS = "document.querySelector('.pane .xterm-rows').innerText"


def test_attached_view_and_the_copy_paste_model(stack):
    failures = []

    def check(name, ok, detail=""):
        if not ok:
            failures.append(f"{name} {detail}".strip())

    async def run():
        c = await page(stack)
        shots = os.environ.get("SERAI_TEST_SHOTS")
        try:
            await c.wait("document.querySelectorAll('.row').length >= 2", 15)
            await c.eval("[...document.querySelectorAll('.row')].find(r => r.querySelector('.name').textContent==='demo').click()")
            check("attach seeds the screen", await c.wait(f"{ROWS}.includes('line-80')", 10))
            check("history is in xterm's own scrollback", await c.eval(
                "(()=>{const v=document.querySelector('.pane .xterm-viewport');return v.scrollHeight>v.clientHeight})()"))

            await c.eval("document.querySelector('.pane .xterm-helper-textarea').focus()")
            await c.send("Input.insertText", text="echo typed-$((6*7))")
            await c.key("Enter", "Enter", text="\r", vk=13)
            check("typing round-trips", await c.wait(f"{ROWS}.includes('typed-42')", 8))

            x, y, w, h = await c.eval(
                "(()=>{const r=document.querySelector('.pane .xterm-screen').getBoundingClientRect();return [r.left,r.top,r.width,r.height]})()")
            lh = h / 36
            await c.drag(x + 2, y + lh * 0.5, x + 300, y + lh * 3.5)
            await c.key("KeyC", "C", mods=2 | 8, vk=67)
            await asyncio.sleep(0.4)
            clip = await c.eval("navigator.clipboard.readText()")
            check("plain drag + Ctrl+Shift+C reaches the clipboard",
                  clip and "line-" in clip and clip.count("\n") >= 2, repr(clip)[:80])

            await c.drag(x + 2, y + lh * 4.5, x + 200, y + lh * 5.5)
            await c.key("KeyC", "c", mods=2, vk=67)
            await asyncio.sleep(0.4)
            clip2 = await c.eval("navigator.clipboard.readText()")
            check("Ctrl+C with a selection copies instead of interrupting",
                  clip2 and clip2 != clip and "^C" not in await c.eval(ROWS), repr(clip2)[:80])

            await c.send("Input.insertText", text="sleep 30")
            await c.key("Enter", "Enter", text="\r", vk=13)
            await asyncio.sleep(0.6)
            await c.key("KeyC", "c", mods=2, text="\x03", vk=67)
            await asyncio.sleep(0.3)
            await c.send("Input.insertText", text="echo after-$((1+1))-int")
            await c.key("Enter", "Enter", text="\r", vk=13)
            check("Ctrl+C with nothing selected interrupts", await c.wait(f"{ROWS}.includes('after-2-int')", 4))

            await c.eval("navigator.clipboard.writeText('echo pasted-$((2+3))')")
            await c.send("Input.dispatchKeyEvent", type="rawKeyDown", code="KeyV", key="v", modifiers=2,
                         windowsVirtualKeyCode=86, commands=["paste"])
            await c.send("Input.dispatchKeyEvent", type="keyUp", code="KeyV", key="v", modifiers=2,
                         windowsVirtualKeyCode=86)
            await asyncio.sleep(0.3)
            await c.key("Enter", "Enter", text="\r", vk=13)
            await c.wait(f"{ROWS}.includes('pasted-5')", 4)
            n = sum(1 for line in (await c.eval(ROWS)).splitlines() if line.strip() == "pasted-5")
            check("Ctrl+V pastes exactly once", n == 1, f"(seen {n})")

            await c.eval("(()=>{const r=[...document.querySelectorAll('.row')].find(r=>r.querySelector('.name').textContent==='other');"
                         "r.dispatchEvent(new MouseEvent('click',{bubbles:true,shiftKey:true}))})()")
            check("Shift+click opens a second, live pane", await c.wait(
                "document.querySelectorAll('.pane').length===2 && "
                "[...document.querySelectorAll('.pane .xterm-rows')].every(r=>r.innerText.includes('$'))", 8))
            if shots:
                os.makedirs(shots, exist_ok=True)
                await c.shot(os.path.join(shots, "attached-two-panes.png"))
            errs = [e for e in c.events if e.get("method") == "Runtime.exceptionThrown"]
            check("no JS exceptions", not errs, json.dumps(errs)[:300])
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 90))
    assert not failures, "browser smoke failures:\n  " + "\n  ".join(failures)
