"""/ws/attach end to end: the real endpoint, a real tmux server on a throwaway
socket dir (TMUX_TMPDIR) with no user config, and the control-mode bridge in
between. Never touches the user's tmux server."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time

import pytest
from fastapi.testclient import TestClient

from serai import sessions, tmuxctl
from serai.main import app
from serai.ws import attach as ws_attach

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture
def tmux(monkeypatch, tmp_path):
    sock = tempfile.mkdtemp(prefix="serai-ws-", dir="/tmp")    # short: sockets are length-capped
    monkeypatch.setenv("TMUX_TMPDIR", sock)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))                      # no ~/.tmux.conf
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("ENV", "")
    monkeypatch.setenv("SERAI_AUTH", "off")
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path / "cfg"))
    # Pings guarantee a frame at least this often, so no receive() blocks long.
    monkeypatch.setattr(ws_attach, "WS_PING_INTERVAL", 0.1)

    def run(*args):
        return subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=10)
    yield run
    run("kill-server")
    shutil.rmtree(sock, ignore_errors=True)


class Conn:
    """Collects frames from a websocket, with deadlines instead of hangs."""

    def __init__(self, ws):
        self.ws = ws
        self.data = b""
        self.texts: list[dict] = []
        self.close_code = None

    def pump(self, until, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end and self.close_code is None:
            m = self.ws.receive()
            if m["type"] == "websocket.close":
                self.close_code = m.get("code")
                break
            if m.get("bytes") is not None:
                self.data += m["bytes"]
            elif m.get("text") is not None:
                try:
                    self.texts.append(json.loads(m["text"]))
                except ValueError:
                    self.texts.append({"raw": m["text"]})
            if until(self):
                return True
        return until(self)


def _seeded(c):
    return {"seed": "end"} in c.texts


def test_attach_creates_seeds_and_round_trips_input(tmux):
    with TestClient(app).websocket_connect(
            "/ws/attach?host=local&kind=shell&label=wsdemo&cols=100&rows=30") as ws:
        c = Conn(ws)
        assert c.pump(_seeded)
        begin = next(t for t in c.texts if t.get("seed") == "begin")
        assert (begin["cols"], begin["rows"]) == (100, 30)   # created at the browser's size
        ws.send_bytes(b"echo ws-$((6*7))\r")
        assert c.pump(lambda c: b"ws-42" in c.data)
    assert tmux("has-session", "-t", "=shell-wsdemo").returncode == 0, \
        "closing the socket must leave the session running (invariant #4)"


def test_attach_never_touches_the_users_tmux_options(tmux):
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=opts") as ws:
        assert Conn(ws).pump(_seeded)
    assert tmux("show-options", "-t", "shell-opts", "mouse").stdout.strip() in ("", "mouse off")
    assert "manual" not in tmux("show-options", "-w", "-t", "shell-opts", "window-size").stdout


def test_resize_frame_sizes_the_window(tmux):
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=rsz") as ws:
        c = Conn(ws)
        assert c.pump(_seeded)
        ws.send_text(json.dumps({"resize": {"cols": 101, "rows": 31}}))

        def sized(_):
            r = tmux("display-message", "-p", "-t", "shell-rsz", "#{window_width}x#{window_height}")
            return r.stdout.strip() == "101x31"
        assert c.pump(sized)


def test_browser_ping_never_reaches_the_pane_and_server_pings(tmux):
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=png") as ws:
        c = Conn(ws)
        assert c.pump(_seeded)
        ws.send_text(json.dumps({"ping": 1}))
        ws.send_bytes(b"echo after-ping\r")
        assert c.pump(lambda c: b"after-ping\r\n" in c.data)
        assert c.pump(lambda c: {"ping": 1} in c.texts)
    assert '{"ping"' not in tmux("capture-pane", "-p", "-t", "shell-png").stdout


def test_close_4410_when_the_session_is_killed(tmux):
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=gone") as ws:
        c = Conn(ws)
        assert c.pump(_seeded)
        tmux("kill-session", "-t", "=shell-gone")
        assert c.pump(lambda c: c.close_code is not None)
        assert c.close_code == 4410, "a gone session must not be auto-recreated"


def test_close_4410_when_the_program_exits(tmux):
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=bye") as ws:
        c = Conn(ws)
        assert c.pump(_seeded)
        ws.send_bytes(b"exit\r")
        assert c.pump(lambda c: c.close_code is not None)
        assert c.close_code == 4410


def test_unattachable_but_alive_is_retryable(tmux, monkeypatch):
    tmux("new-session", "-d", "-s", "shell-stuck")
    monkeypatch.setattr(tmuxctl, "control_argv", lambda host, name, pause_after=3: ["false"])
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=stuck") as ws:
        c = Conn(ws)
        assert c.pump(lambda c: c.close_code is not None)
        assert c.close_code == 1011
        assert any("error" in t for t in c.texts)


def test_failed_create_is_final(tmux, monkeypatch):
    monkeypatch.setattr(sessions, "create_session", lambda argv: False)
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=nope") as ws:
        c = Conn(ws)
        assert c.pump(lambda c: c.close_code is not None)
        assert c.close_code == 4410


def test_new_session_gets_its_dir_and_args_in_the_create(tmux, tmp_path, monkeypatch):
    """They ride the create command: a separate set-option raced it and failed
    on a session that didn't exist yet."""
    # A stand-in agent kind whose command is `true`: never launch a real agent
    # CLI from a test (it would start with this test's HOME and network access).
    monkeypatch.setitem(sessions._AGENTS, "fakeagent",
                        {"cmd": "true", "prefix": "fa-", "chip": "fa", "glyph": "x", "resume": {}})
    proj = tmp_path / "proj"
    proj.mkdir()
    url = f"/ws/attach?host=local&kind=fakeagent&label=dirargs&path={proj}&args=--demo-flag"
    with TestClient(app).websocket_connect(url) as ws:
        assert Conn(ws).pump(_seeded)
    show = lambda opt: tmux("show-options", "-v", "-t", "fa-dirargs", opt).stdout.strip()
    assert show("@serai_dir") == str(proj)
    assert show("@serai_args") == "--demo-flag"


def test_create_argv_puts_the_dir_option_last():
    # tmux reads an argument ending in ';' as a separator; a dir may end in one,
    # so it must be the final argument, where it can only end its own command.
    argv = sessions.create_argv("local", "cc-x", "claude", "/p", args="--a",
                                owner="bob", tags="t", start_dir="/p;")
    assert argv[-2:] == ["@serai_dir", "/p;"]
    assert argv.index("@serai_args") < argv.index("@serai_dir")


def test_session_state_tells_absent_from_unreachable(monkeypatch):
    rc = {}

    def fake_run(argv, **kw):
        if rc["v"] == "timeout":
            raise subprocess.TimeoutExpired(argv, 6)
        return subprocess.CompletedProcess(argv, rc["v"])
    monkeypatch.setattr(sessions.subprocess, "run", fake_run)
    for value, want in ((0, True), (1, False), (255, None), (127, None), ("timeout", None)):
        rc["v"] = value
        assert sessions.session_state("local", "x") is want, value
    rc["v"] = 255
    assert sessions.session_exists("local", "x") is False


def test_unreachable_host_neither_creates_nor_skips_the_owner_check(tmux, monkeypatch):
    created = []
    monkeypatch.setattr(sessions, "session_state", lambda host, name: None)
    monkeypatch.setattr(sessions, "create_session", lambda argv: created.append(argv) or True)
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=down") as ws:
        c = Conn(ws)
        assert c.pump(lambda c: c.close_code is not None)
    assert c.close_code == 1011, "unknown is retryable, never a final 4410"
    assert created == []


def test_listing_flags_an_agent_session_parked_at_its_shell(tmux, monkeypatch):
    """at_prompt drives the v3 relaunch strip: an agent kind whose harness has
    exited to the _EXIT_SHELL prompt. A shell session is never "at prompt"."""
    monkeypatch.setitem(sessions._AGENTS, "fakeagent",
                        {"cmd": "true", "prefix": "fa-", "chip": "fa", "glyph": "x", "resume": {}})
    monkeypatch.setattr(sessions, "AGENT_KINDS", sessions.AGENT_KINDS | {"fakeagent"})
    assert sessions.create_session(sessions.create_argv("local", "fa-parked", "fakeagent"))
    assert sessions.create_session(sessions.create_argv("local", "shell-plain", "shell"))
    deadline = time.time() + 5
    while time.time() < deadline:
        by = {s.name: s for s in sessions.list_sessions("local")}
        if "fa-parked" in by and by["fa-parked"].at_prompt:
            break
        time.sleep(0.2)
    assert by["fa-parked"].at_prompt is True
    assert by["shell-plain"].at_prompt is False
    assert by["fa-parked"].as_dict()["at_prompt"] is True
