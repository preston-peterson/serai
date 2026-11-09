"""Preferences follow you between devices through the server's settings file,
and the first v3 boot on an install that ran v2 imports v2's terminal look."""

from __future__ import annotations

import asyncio
import json
import os

from .test_smoke import Stack, page  # noqa: F401
import pytest


@pytest.fixture
def v2_stack(tmp_path):
    s = Stack(str(tmp_path))
    cfg = s.env["SERAI_CONFIG_DIR"]
    os.makedirs(cfg, exist_ok=True)
    # what a v2 install left in settings.json: its terminal settings, as a string
    v2 = {"theme": "Nord", "font": "'DejaVu Sans Mono', monospace", "size": 17,
          "scrollback": 20000, "cursorStyle": "bar", "cursorBlink": False}
    with open(os.path.join(cfg, "settings.json"), "w") as f:
        json.dump({"serai.term": json.dumps(v2)}, f)
    s.tmux("new-session", "-d", "-s", "shell-demo", "sh")
    try:
        s.start(str(tmp_path))
        yield s
    finally:
        print(s.log_tails())
        s.stop()


PREFS = "JSON.parse(localStorage.getItem('serai.v3.prefs') || 'null')"


def test_v2_terminal_settings_are_imported_then_follow_you(v2_stack):
    async def run():
        c = await page(v2_stack)
        try:
            await c.wait("document.querySelectorAll('.row').length >= 1", 15)
            p = await c.eval(PREFS)
            assert p and p["theme"] == "Nord" and p["fontSize"] == 17 and p["cursorStyle"] == "bar" \
                and p["cursorBlink"] is False and p["scrollback"] == 20000, p

            # change one, give the 500 ms debounce time to reach the server ...
            await c.eval("import('/static/core/prefs.js').then(m => m.setPrefs({fontSize: 19}))")
            await asyncio.sleep(1.2)
            # ... then be "another device": no local storage at all
            await c.eval("localStorage.clear()")
            await c.send("Page.reload")
            await c.wait("document.querySelectorAll('.row').length >= 1", 15)
            p = await c.eval(PREFS)
            assert p and p["fontSize"] == 19 and p["theme"] == "Nord", p
        finally:
            await c.close()

    asyncio.run(asyncio.wait_for(run(), 60))
    with open(os.path.join(v2_stack.env["SERAI_CONFIG_DIR"], "settings.json")) as f:
        assert "serai.v3.prefs" in json.load(f), "the server copy was never written"
