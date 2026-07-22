"""Baseline tests for serai.

Covers the parser, the attach-command builder, path quoting, the read APIs, and
the PTY<->websocket bridge. The bridge test swaps tmux for a deterministic echo
process so it runs anywhere; on a real host the same code path launches tmux.

Run:  pytest -q   (after: pip install -e . pytest httpx)
"""

import json
import os
import shutil
import socket
import stat
import io
import shlex
import subprocess
import tempfile
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import serai
from serai import auth, config, files, netcfg, sessions, settings, store, tls, updates
from serai.main import app

requires_tmux = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed")


@pytest.fixture(autouse=True)
def _auth_off(monkeypatch):
    """Most tests exercise functionality, not auth, so keep the gate open by
    default -- otherwise the middleware would 401 every API call. The auth tests
    opt back in via the `auth_on` fixture (which runs after this one)."""
    monkeypatch.setenv("SERAI_AUTH", "off")

SSH_CONFIG = """\
# @group web-stack
# @tags prod,docker
Host proj-web
    HostName 192.0.2.21
    User youruser
    Port 2222

# @group web-stack
Host proj-cache
    HostName 192.0.2.23
    # @tags prod

Host proj-db
    HostName 192.0.2.22
    # @group data
    # @tags prod,postgres

Host *.lan
    User youruser
"""


@pytest.fixture
def ssh_config(tmp_path):
    p = tmp_path / "config"
    p.write_text(SSH_CONFIG)
    return str(p)


def test_parser_reads_groups_tags_and_skips_wildcards(ssh_config):
    hosts = {h.alias: h for h in config.parse_ssh_config(ssh_config)}
    assert set(hosts) == {"proj-web", "proj-cache", "proj-db"}  # *.lan skipped

    assert hosts["proj-web"].group == "web-stack"
    assert hosts["proj-web"].tags == ["prod", "docker"]
    assert hosts["proj-web"].user == "youruser"
    assert hosts["proj-web"].port == 2222          # Port directive parsed
    assert hosts["proj-db"].port == 22             # default when unspecified

    # group above the block, tags inside the block
    assert hosts["proj-cache"].group == "web-stack"
    assert hosts["proj-cache"].tags == ["prod"]

    # both annotations inside the block
    assert hosts["proj-db"].group == "data"
    assert hosts["proj-db"].tags == ["prod", "postgres"]


def test_add_host_appends_and_parses(tmp_path):
    cfg = str(tmp_path / "config")
    config.add_host("web", hostname="192.0.2.1", user="u", path=cfg)  # pre-existing block
    h = config.add_host("db", hostname="192.0.2.2", user="me", port=2200,
                        group="data", tags=["prod", "pg"], path=cfg)
    assert h.alias == "db" and h.port == 2200
    parsed = {x.alias: x for x in config.parse_ssh_config(cfg)}
    assert set(parsed) == {"web", "db"}
    assert parsed["db"].hostname == "192.0.2.2" and parsed["db"].user == "me"
    assert parsed["db"].port == 2200 and parsed["db"].group == "data"
    assert parsed["db"].tags == ["prod", "pg"]
    # the blank line before the new block means @group bound to db, not web
    assert parsed["web"].group == "ungrouped"


def test_add_host_rejects_config_injection(tmp_path):
    cfg = str(tmp_path / "config")
    # a newline in any field could smuggle another ssh directive (ProxyCommand
    # -> local RCE) -- must be rejected before anything is written
    with pytest.raises(ValueError):
        config.add_host("ok", hostname="1.2.3.4\n    ProxyCommand touch /tmp/pwn", path=cfg)
    with pytest.raises(ValueError):
        config.add_host("bad alias", path=cfg)   # space
    with pytest.raises(ValueError):
        config.add_host("evil*", path=cfg)        # wildcard
    assert not os.path.exists(cfg)                # nothing was written


def test_add_host_rejects_duplicate_and_bad_port(tmp_path):
    cfg = str(tmp_path / "config")
    config.add_host("h1", hostname="1.2.3.4", path=cfg)
    with pytest.raises(ValueError):
        config.add_host("h1", hostname="5.6.7.8", path=cfg)   # duplicate alias
    for bad in (0, 99999, "abc"):
        with pytest.raises(ValueError):
            config.add_host("h2", port=bad, path=cfg)


def test_api_hosts_add_endpoint(monkeypatch, tmp_path):
    cfg = tmp_path / "config"
    # redirect add_host's default path (the endpoint doesn't pass one) to a tmp file
    monkeypatch.setattr(config.add_host, "__defaults__", ("", "", 22, "", None, str(cfg)))
    c = TestClient(app)  # SERAI_AUTH=off -> request is treated as admin
    r = c.post("/api/hosts", json={"alias": "lab1", "hostname": "192.0.2.7",
                                   "user": "me", "port": 2222, "group": "lab", "tags": ["dev"]})
    assert r.status_code == 200 and r.json()["ok"] is True
    h = {x.alias: x for x in config.parse_ssh_config(str(cfg))}["lab1"]
    assert h.hostname == "192.0.2.7" and h.user == "me" and h.port == 2222
    assert h.group == "lab" and h.tags == ["dev"]
    assert c.post("/api/hosts", json={"alias": "bad alias!"}).status_code == 400  # validation


def test_session_name_convention():
    assert sessions.session_name("claude", "webapp") == "cc-webapp"
    assert sessions.session_name("shell", "main") == "shell-main"
    # unsafe characters collapse to hyphens
    assert sessions.session_name("claude", "my project!/v2") == "cc-my-project--v2"


def test_list_sessions_caches_remote_within_ttl(monkeypatch):
    # Swap real discovery for a counter so we can see exactly when it runs.
    sessions.clear_cache()
    calls: list[str] = []

    def fake(host):
        calls.append(host)
        return [sessions.Session(host=host, name="shell-x", kind="shell",
                                 label="x", state="idle", attached=False)]

    monkeypatch.setattr(sessions, "_list_sessions_uncached", fake)

    # Remote: within the TTL the second call is served from cache.
    monkeypatch.setattr(sessions, "_CACHE_TTL", 100.0)
    sessions.list_sessions("proj-web")
    second = sessions.list_sessions("proj-web")
    assert calls == ["proj-web"]                   # discovery ran only once
    assert [s.name for s in second] == ["shell-x"]

    # Local is never cached -- it always discovers fresh.
    calls.clear()
    sessions.list_sessions("local")
    sessions.list_sessions("local")
    assert calls == ["local", "local"]

    # An expired TTL re-discovers the remote host.
    calls.clear()
    monkeypatch.setattr(sessions, "_CACHE_TTL", 0.0)
    sessions.list_sessions("proj-web")
    sessions.list_sessions("proj-web")
    assert calls == ["proj-web", "proj-web"]
    sessions.clear_cache()


def test_classify_recognizes_naming_schemes():
    c = sessions._classify
    assert c("cc-demo") == ("claude", "demo")          # serai's own convention
    assert c("shell-main") == ("shell", "main")
    assert c("webapp-claude") == ("claude", "webapp")  # bare <project>-claude suffix
    assert c("example-term") == ("shell", "example")         # bare <project>-term suffix
    # the -claude/-term suffix wins even under serai's shell- storage prefix
    assert c("shell-example-claude") == ("claude", "example")
    assert c("shell-toolkit-term") == ("shell", "toolkit")
    assert c("shell-TimeSheet-Claude") == ("claude", "TimeSheet")  # case-insensitive suffix
    assert c("shell-12_345") == ("shell", "12_345")    # shell- prefix, no suffix
    assert c("12_345") == ("shell", "12_345")          # plain name -> shell


def test_needs_input_detection_is_per_kind():
    # _state_for is a pure classifier: (kind, attached, secs_since_activity,
    # pane_current_command, lowercased marker text) -> state.
    st = sessions._state_for

    # A common marker flags any kind.
    m = "do you want to continue? (y/n)"
    assert st("claude", False, 999, "node", m) == "needs_input"
    assert st("shell", False, 999, "bash", m) == "needs_input"

    # A Claude permission prompt flags a claude session but not a shell one.
    m = "  2. no, and tell claude what to do"
    assert st("claude", False, 999, "node", m) == "needs_input"
    assert st("shell", False, 999, "bash", m) == "idle"

    # A shell input prompt flags a shell session but not a claude one.
    m = "[sudo] password for user:"
    assert st("shell", False, 999, "bash", m) == "needs_input"
    assert st("claude", False, 999, "node", m) != "needs_input"  # shell marker doesn't flag cc

    # No marker -> per-kind signals.
    q = "just some quiet output"
    # shells: activity age + foreground command
    assert st("shell", False, 5, "bash", q) == "running"    # recent activity
    assert st("shell", False, 999, "npm", q) == "running"   # non-shell foreground = busy
    assert st("shell", False, 999, "bash", q) == "idle"     # quiet shell at its prompt
    # claude: content-based -- "esc to interrupt" = working, else parked at prompt
    assert st("claude", False, 5, "node", "thinking… (esc to interrupt)") == "running"
    assert st("claude", False, 120, "node", q) == "done"    # at prompt, recently active
    assert st("claude", True, 120, "node", q) == "idle"     # you're attached -> not "done"
    assert st("claude", False, 99999, "node", q) == "idle"  # dormant past the done window


def test_wait_markers_configurable_via_env(monkeypatch):
    monkeypatch.setenv("SERAI_WAIT_MARKERS", "Spinning Up, custom>>")
    monkeypatch.setenv("SERAI_WAIT_MARKERS_CLAUDE", "awaiting approval")
    markers = sessions._build_markers()
    # env values are appended, lowercased, and trimmed
    assert "spinning up" in markers["common"]
    assert "custom>>" in markers["common"]
    assert "awaiting approval" in markers["claude"]
    # defaults are preserved (added to, not replaced)
    assert set(sessions._COMMON_MARKERS).issubset(set(markers["common"]))
    assert set(sessions._SHELL_MARKERS).issubset(set(markers["shell"]))


def _claude_inner(path, resume=""):
    # the shell-command tmux runs is the element just before the ';' separator
    argv = sessions.attach_argv("local", "cc-x", "claude", path, resume)
    return argv[:argv.index(";")][-1]


def test_attach_argv_local_and_remote():
    local = sessions.attach_argv("local", "shell-main", "shell")
    assert local[:5] == ["tmux", "new", "-A", "-s", "shell-main"]
    # scrollback: tmux mouse mode enabled session-scoped on attach, and OSC 52
    # publishing (set-clipboard) so copy-mode selections reach the browser
    assert local[5:] == [";", "set-option", "-t", "shell-main", "mouse", "on",
                         ";", "set-option", "-s", "set-clipboard", "on"]

    remote = sessions.attach_argv("proj-web", "shell-deploy", "shell")
    assert remote[0] == "ssh" and remote[-2] == "proj-web"
    assert "BatchMode=yes" in remote  # invariant: agent-only auth
    assert remote[-1] == ("tmux new -A -s shell-deploy ';' set-option -t shell-deploy mouse on"
                          " ';' set-option -s set-clipboard on")


def test_attach_argv_mouse_toggle():
    on = sessions.attach_argv("local", "shell-x", "shell", mouse=True)
    off = sessions.attach_argv("local", "shell-x", "shell", mouse=False)
    assert on[-11:-5] == [";", "set-option", "-t", "shell-x", "mouse", "on"]
    assert off[-11:-5] == [";", "set-option", "-t", "shell-x", "mouse", "off"]
    # set-clipboard rides along regardless of the mouse toggle
    assert on[-5:] == off[-5:] == [";", "set-option", "-s", "set-clipboard", "on"]


def test_attach_argv_history_sets_limit_before_create():
    # history-limit must be set on the server before `new` (it only applies to a
    # pane at creation); None leaves it untouched.
    none = sessions.attach_argv("local", "shell-x", "shell")
    assert none[:5] == ["tmux", "new", "-A", "-s", "shell-x"]
    h = sessions.attach_argv("local", "shell-x", "shell", history=50000)
    assert h[:7] == ["tmux", "set", "-g", "history-limit", "50000", ";", "new"]
    # remote stays argv-safe through shlex.quote
    rem = sessions.attach_argv("proj-web", "shell-x", "shell", history=50000)[-1]
    assert rem.startswith("tmux set -g history-limit 50000 ';' new -A -s shell-x")


def test_attach_argv_claude_preserves_tilde_and_quotes_spaces():
    assert _claude_inner("~/git/webapp") == "cd ~/git/webapp && claude"  # tilde stays expandable
    assert _claude_inner("~/my proj") == "cd ~/'my proj' && claude"  # space quoted, tilde free
    assert _claude_inner("/srv/app") == "cd /srv/app && claude"


def test_attach_argv_claude_resume_modes():
    base = "cd ~/git/app && claude"
    av = lambda r: _claude_inner("~/git/app", r)
    assert av("") == base
    assert av("continue") == base + " --continue"
    assert av("resume") == base + " --resume"
    # unknown/garbage resume value injects nothing (the flag is a fixed literal)
    assert av("; rm -rf ~") == base


def test_settings_save_load_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("SERAI_SETTINGS", str(tmp_path / "s.json"))
    assert settings.load() == {}  # absent -> {}
    settings.save({"serai.term": '{"theme":"Dracula"}', "n": 1})
    assert settings.load() == {"serai.term": '{"theme":"Dracula"}', "n": 1}
    (tmp_path / "s.json").write_text("not json")  # corrupt -> {}
    assert settings.load() == {}


def test_settings_api_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv("SERAI_SETTINGS", str(tmp_path / "s.json"))
    c = TestClient(app)
    assert c.get("/api/settings").json() == {}
    assert c.put("/api/settings", json={"serai.sidebar.width": "300"}).json() == {"ok": True}
    assert c.get("/api/settings").json() == {"serai.sidebar.width": "300"}
    assert c.put("/api/settings", json=[1, 2]).status_code == 400  # non-object rejected


def test_responses_send_no_cache():
    client = TestClient(app)
    assert client.get("/").headers.get("cache-control") == "no-cache"
    assert client.get("/static/app.js").headers.get("cache-control") == "no-cache"


def test_read_apis(monkeypatch, ssh_config):
    monkeypatch.setattr(config, "DEFAULT_PATH", ssh_config)
    monkeypatch.setattr(config.parse_ssh_config, "__defaults__", (ssh_config,))
    # Don't probe the (unrouteable) example hosts for real -- that would be slow.
    monkeypatch.setattr(sessions, "tcp_reachable", lambda *a, **k: True)
    client = TestClient(app)

    assert client.get("/").status_code == 200
    assert client.get("/static/app.js").status_code == 200

    hosts = client.get("/api/hosts").json()
    assert len(hosts) == 3

    sess = client.get("/api/sessions")
    assert sess.status_code == 200 and isinstance(sess.json(), list)

    with tempfile.TemporaryDirectory() as d:
        open(os.path.join(d, "hello.txt"), "w").close()
        r = client.get("/api/files", params={"host": "local", "path": d})
        assert r.status_code == 200
        names = [e["name"] for e in r.json()["entries"]]
        assert "hello.txt" in names


@requires_tmux
def test_capture_tail_finds_marker_above_blank_pane_padding():
    # tmux capture-pane pads a pane to its full height with blank lines, so a
    # short prompt sits near the top with empty space trailing it. _capture_lines
    # must tail the last *non-blank* lines or it would scan only the blank
    # bottom and miss the marker (regression: real `[sudo] password:` prompts).
    name = "shell-serai-pytest-tail"
    subprocess.run(["tmux", "kill-session", "-t", name],
                   capture_output=True)
    subprocess.run(["tmux", "new-session", "-d", "-s", name], check=True)
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", name,
             'echo "Do you want to proceed? (y/n)"', "Enter"],
            check=True,
        )
        # The marker line is near the top of an otherwise-empty tall pane.
        lines = sessions._capture_lines("local", name)
        marker = "\n".join(lines).lower()
        assert "do you want" in marker
        assert sessions._state_for("shell", False, None, "bash", marker) == "needs_input"
    finally:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)


def test_pty_websocket_bridge(monkeypatch):
    # Swap tmux for an echo program to exercise the bridge itself.
    monkeypatch.setattr(
        sessions,
        "attach_argv",
        lambda host, name, kind, path=None, resume="", mouse=True, history=None: [
            "python3", "-u", "-c",
            'import sys; print("READY"); print("GOT:"+sys.stdin.readline().strip())',
        ],
    )
    client = TestClient(app)
    with client.websocket_connect("/ws/attach?host=local&kind=shell&label=t") as ws:
        buf = ""

        def drain(needle, tries=30):
            nonlocal buf
            for _ in range(tries):
                m = ws.receive()
                if m.get("bytes"):
                    buf += m["bytes"].decode(errors="replace")
                elif m.get("text"):
                    buf += m["text"]
                if needle in buf:
                    return True
            return False

        assert drain("READY")
        ws.send_bytes(b"hello\n")
        ws.send_text(json.dumps({"resize": {"rows": 40, "cols": 120}}))
        assert drain("GOT:hello")


def _ws_close_code(monkeypatch, exists):
    # attach to a program that exits immediately, so the PTY EOFs and _close runs
    monkeypatch.setattr(sessions, "attach_argv",
                        lambda host, name, kind, path=None, resume="", mouse=True, history=None: ["python3", "-u", "-c", "print('bye')"])
    monkeypatch.setattr(sessions, "session_exists", lambda host, name: exists)
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=t") as ws:
        for _ in range(20):
            m = ws.receive()
            if m["type"] == "websocket.close":
                return m.get("code")
    return None


def test_ws_close_4410_when_session_ended(monkeypatch):
    # session gone (program exited / killed) -> 4410 so the client won't relaunch
    assert _ws_close_code(monkeypatch, exists=False) == 4410


def test_ws_close_normal_when_session_alive(monkeypatch):
    # session still alive (detach / blip) -> normal close so the client reattaches
    assert _ws_close_code(monkeypatch, exists=True) == 1000


# --- files.py SFTP path ----------------------------------------------------
#
# These mock paramiko so the remote file path is exercised without a real host.
# Two layers of fakes: a fake SFTP client for the list/read/write logic, and a
# fake SSHClient to assert the connection is agent-only (invariant #1) + cached.


class _FakeRemoteFile:
    """Context-manager file handle returned by the fake SFTP's open()."""

    def __init__(self, sftp, path, data):
        self._sftp, self._path, self._data = sftp, path, data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._data

    def write(self, chunk):
        self._sftp.written[self._path] = self._sftp.written.get(self._path, b"") + chunk


class _FakeSFTP:
    def __init__(self, attrs=(), contents=None):
        self._attrs = list(attrs)
        self._contents = contents or {}
        self.written: dict[str, bytes] = {}
        self.opened: list[tuple[str, str]] = []
        self.listed = None

    def listdir_attr(self, path):
        self.listed = path
        return self._attrs

    def open(self, path, mode):
        self.opened.append((path, mode))
        return _FakeRemoteFile(self, path, self._contents.get(path, b""))

    def rename(self, src, dst):
        self.renamed = (src, dst)

    def mkdir(self, path):
        self.made = path

    def stat(self, path):
        if path in self._contents:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o644)
        raise FileNotFoundError(path)


class _FakeClient:
    def __init__(self, sftp):
        self._sftp = sftp
        self.execs: list[str] = []
        self.exec_stdout = b""              # bytes the next exec's stdout yields
        self.stdin_chunks: list[bytes] = []  # bytes written to the next exec's stdin

    def open_sftp(self):
        return self._sftp

    def exec_command(self, cmd, timeout=None):
        self.execs.append(cmd)
        chan = SimpleNamespace(recv_exit_status=lambda: 0, shutdown_write=lambda: None)
        stdin = SimpleNamespace(write=self.stdin_chunks.append, channel=chan,
                                close=lambda: None)
        # a real channel streams: read(n) yields a chunk and then b"" at EOF, which
        # is what the folder relay consumes -- a read() returning everything
        # forever would never terminate.
        buf = io.BytesIO(self.exec_stdout)
        stdout = SimpleNamespace(channel=chan, read=buf.read, close=lambda: None)
        stderr = SimpleNamespace(read=lambda: b"")
        return stdin, stdout, stderr


def _attr(name, *, is_dir, size, mtime=1700000000):
    mode = (stat.S_IFDIR | 0o755) if is_dir else (stat.S_IFREG | 0o644)
    return SimpleNamespace(filename=name, st_mode=mode, st_size=size, st_mtime=mtime)


def _use_sftp(monkeypatch, sftp):
    client = _FakeClient(sftp)
    monkeypatch.setattr(files, "_ssh_for", lambda host: client)
    return client


def test_list_remote_maps_types_sizes_and_sorts(monkeypatch):
    attrs = [
        _attr("apple.txt", is_dir=False, size=7, mtime=1700000111),
        _attr("Zeta", is_dir=True, size=4096),     # dir size must be forced to 0
        _attr("alpha", is_dir=True, size=4096),
        _attr("Banana", is_dir=False, size=None),  # missing size -> 0
    ]
    sftp = _FakeSFTP(attrs=attrs)
    _use_sftp(monkeypatch, sftp)

    entries = files.list_remote("proj-web", "/srv")
    assert {e.name: e.mtime for e in entries}["apple.txt"] == 1700000111.0  # mtime carried through
    # dirs first (case-insensitive), then files (case-insensitive)
    assert [(e.name, e.is_dir, e.size) for e in entries] == [
        ("alpha", True, 0),
        ("Zeta", True, 0),
        ("apple.txt", False, 7),
        ("Banana", False, 0),
    ]
    assert sftp.listed == "/srv"


def test_list_remote_defaults_empty_path_to_dot(monkeypatch):
    sftp = _FakeSFTP(attrs=())
    _use_sftp(monkeypatch, sftp)
    files.list_remote("proj-web", "")
    assert sftp.listed == "."


def test_remote_paths_expand_tilde_for_sftp(monkeypatch):
    # SFTP has no ~ expansion and the UI requests "~" by default -- unexpanded,
    # every remote browse failed with FileNotFoundError ("no files" bug).
    sftp = _FakeSFTP(attrs=())
    _use_sftp(monkeypatch, sftp)
    files.list_remote("proj-web", "~")
    assert sftp.listed == "."                       # home = the SFTP start dir
    files.list_remote("proj-web", "~/git/serai")
    assert sftp.listed == "git/serai"               # home-relative
    files.list_remote("proj-web", "/srv/app")
    assert sftp.listed == "/srv/app"                # absolute passes through

    sftp2 = _FakeSFTP(contents={"git/x.txt": b"data"})
    _use_sftp(monkeypatch, sftp2)
    assert files.read_file("proj-web", "~/git/x.txt") == b"data"   # download
    files.write_file("proj-web", "~/git/up.bin", b"payload")       # upload
    assert sftp2.written == {"git/up.bin": b"payload"}


def test_read_file_remote_reads_via_sftp(monkeypatch):
    sftp = _FakeSFTP(contents={"/etc/motd": b"hello remote"})
    _use_sftp(monkeypatch, sftp)
    assert files.read_file("proj-web", "/etc/motd") == b"hello remote"
    assert sftp.opened == [("/etc/motd", "rb")]


def test_write_file_remote_writes_via_sftp(monkeypatch):
    sftp = _FakeSFTP()
    _use_sftp(monkeypatch, sftp)
    files.write_file("proj-web", "/tmp/out.bin", b"payload")
    assert sftp.written == {"/tmp/out.bin": b"payload"}
    assert sftp.opened == [("/tmp/out.bin", "wb")]


def test_list_dir_dispatches_local_vs_remote(monkeypatch, tmp_path):
    sftp = _FakeSFTP(attrs=[_attr("r.txt", is_dir=False, size=1)])
    _use_sftp(monkeypatch, sftp)

    remote = files.list_dir("proj-web", "/srv")
    assert [e.name for e in remote] == ["r.txt"] and sftp.listed == "/srv"

    # local host uses the filesystem and never touches sftp
    (tmp_path / "local.txt").write_text("x")
    local = files.list_dir("local", str(tmp_path))
    assert [e.name for e in local] == ["local.txt"]


class _FakeTransport:
    def __init__(self, active):
        self._active = active

    def is_active(self):
        return self._active


class _FakeConnectClient:
    instances: list["_FakeConnectClient"] = []

    def __init__(self):
        self.connect_kwargs = None
        self.transport = _FakeTransport(True)
        _FakeConnectClient.instances.append(self)

    def load_system_host_keys(self):
        pass

    def set_missing_host_key_policy(self, policy):
        self.policy = policy

    def connect(self, **kwargs):
        self.connect_kwargs = kwargs

    def get_transport(self):
        return self.transport


class _FakeSSHConfig:
    def parse(self, fh):
        pass

    def lookup(self, host):
        return {"hostname": "192.0.2.50", "user": "youruser", "port": "2222"}


def test_ssh_for_authenticates_via_agent_only_and_caches(monkeypatch):
    files._clients.clear()
    _FakeConnectClient.instances.clear()
    monkeypatch.setattr(files.paramiko, "SSHClient", _FakeConnectClient)
    monkeypatch.setattr(files.paramiko, "SSHConfig", _FakeSSHConfig)

    c1 = files._ssh_for("proj-web")
    kw = c1.connect_kwargs
    # invariant #1: agent-only auth, never a stored password/key material
    assert kw["allow_agent"] is True
    assert kw["look_for_keys"] is True
    assert "password" not in kw
    # ssh config values are honored, port coerced to int
    assert kw["hostname"] == "192.0.2.50"
    assert kw["username"] == "youruser"
    assert kw["port"] == 2222

    # an active transport is reused, not reconnected
    assert files._ssh_for("proj-web") is c1
    assert len(_FakeConnectClient.instances) == 1

    # a dead transport forces a fresh connect
    c1.transport = _FakeTransport(False)
    assert files._ssh_for("proj-web") is not c1
    assert len(_FakeConnectClient.instances) == 2

    files._clients.clear()


# --- fleet broadcast -------------------------------------------------------

def test_send_keys_argv_local_and_remote():
    local = sessions.send_keys_argv("local", "shell-main", "git pull")
    assert local == ["tmux", "send-keys", "-t", "shell-main", "--", "git pull", "Enter"]

    remote = sessions.send_keys_argv("proj-web", "shell-deploy", "uptime")
    assert remote[0] == "ssh"
    assert "BatchMode=yes" in remote          # invariant #1: agent-only auth
    assert "-t" not in remote                 # send-keys is one-shot, needs no tty
    assert remote[-2] == "proj-web"
    assert remote[-1] == "tmux send-keys -t shell-deploy -- uptime Enter"


def test_send_keys_argv_remote_quotes_untrusted_input():
    # invariant #3: a hostile session name / command never escapes the argv --
    # the whole tmux command is one remote string with each piece quoted once.
    cmd = sessions.send_keys_argv("proj-web", "shell-x; rm -rf ~", "echo $(id)")[-1]
    assert cmd == "tmux send-keys -t 'shell-x; rm -rf ~' -- 'echo $(id)' Enter"


def _only_local_hosts(monkeypatch):
    # Hermetic host allowlist: 'local' + a single configured alias.
    monkeypatch.setattr(config, "parse_ssh_config", lambda *a, **k: [config.Host(alias="proj-web")])


def test_broadcast_sends_to_each_target(monkeypatch):
    _only_local_hosts(monkeypatch)
    sent: list[list[str]] = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (sent.append(argv) or True))

    client = TestClient(app)
    r = client.post("/api/broadcast", json={
        "text": "uptime",
        "targets": [{"host": "local", "name": "shell-a"}, {"host": "proj-web", "name": "cc-b"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == 2
    # concurrent, so order isn't guaranteed -- assert membership
    assert ["tmux", "send-keys", "-t", "shell-a", "--", "uptime", "Enter"] in sent
    assert sessions.send_keys_argv("proj-web", "cc-b", "uptime") in sent
    assert len(sent) == 2


def test_broadcast_rejects_unknown_host(monkeypatch):
    _only_local_hosts(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv) or True))

    client = TestClient(app)
    r = client.post("/api/broadcast", json={
        "text": "uptime",
        "targets": [{"host": "evil-host", "name": "shell-a"}],
    })
    assert r.status_code == 200
    body = r.json()
    assert body["sent"] == 0
    assert body["results"][0]["ok"] is False
    assert calls == []  # never shelled out to an unconfigured host


def test_broadcast_validates_body():
    client = TestClient(app)
    assert client.post("/api/broadcast", json={"text": "", "targets": [{"host": "local", "name": "x"}]}).status_code == 400
    assert client.post("/api/broadcast", json={"text": "ls", "targets": []}).status_code == 400


def test_ws_attach_rejects_unknown_host(monkeypatch):
    monkeypatch.setattr(config, "parse_ssh_config", lambda *a, **k: [])
    client = TestClient(app)
    with client.websocket_connect("/ws/attach?host=evil&kind=shell&label=x") as ws:
        got = ""
        code = None
        for _ in range(5):
            m = ws.receive()
            if m.get("text"):
                got += m["text"]
            if m["type"] == "websocket.close":
                code = m.get("code")
                break
    assert "unknown host" in got.lower()
    assert code == 4404  # distinct code the client keys off to show a clean error


# --- host reachability -----------------------------------------------------

def test_tcp_reachable_true_for_open_port_false_for_closed():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    try:
        assert sessions.tcp_reachable("127.0.0.1", port, timeout=1.0) is True
    finally:
        srv.close()
    # nothing listening on that port now -> unreachable
    assert sessions.tcp_reachable("127.0.0.1", port, timeout=0.5) is False


# --- session rename + tags -------------------------------------------------

def test_rename_and_tags_argv_local_and_remote():
    assert sessions.rename_argv("local", "shell-a", "shell-b") == \
        ["tmux", "rename-session", "-t", "shell-a", "shell-b"]
    assert sessions.set_tags_argv("local", "shell-a", "p,q") == \
        ["tmux", "set-option", "-t", "shell-a", "@serai_tags", "p,q"]

    r = sessions.rename_argv("proj-web", "shell-a", "shell-b")
    assert r[0] == "ssh" and "BatchMode=yes" in r
    assert r[-1] == "tmux rename-session -t shell-a shell-b"
    t = sessions.set_tags_argv("proj-web", "shell-a", "p,q")
    assert t[-1] == "tmux set-option -t shell-a @serai_tags p,q"


def test_attach_argv_start_dir_applies_to_both_kinds():
    # a shell can start somewhere too -- it used to ignore the path entirely
    sh = sessions.attach_argv("local", "shell-x", "shell", "~/git/proj")
    assert "cd ~/git/proj && exec ${SHELL:-/bin/sh}" in sh   # ~ stays shell-expandable
    cc = sessions.attach_argv("local", "cc-x", "claude", "~/git/proj", "continue")
    assert "cd ~/git/proj && claude --continue" in cc
    # no path -> plain session, unchanged
    assert not any("cd " in a for a in sessions.attach_argv("local", "shell-y", "shell"))
    # a path with a space survives being quoted twice for the remote shell: the
    # command string round-trips back to the same cd, with ~ left outside the
    # quotes so the remote shell still expands it
    r = sessions.attach_argv("proj-web", "shell-z", "shell", "~/my dir")
    assert r[0] == "ssh"
    inner = next(a for a in shlex.split(r[-1]) if a.startswith("cd "))
    assert inner == "cd ~/'my dir' && exec ${SHELL:-/bin/sh}"


def test_set_dir_argv_and_clean_dir():
    assert sessions.set_dir_argv("local", "shell-a", "~/git/p") == \
        ["tmux", "set-option", "-t", "shell-a", "@serai_dir", "~/git/p"]
    r = sessions.set_dir_argv("proj-web", "shell-a", "~/odd dir")
    assert r[0] == "ssh" and r[-1] == "tmux set-option -t shell-a @serai_dir '~/odd dir'"
    assert sessions.clean_dir("  ~/git/p  ") == "~/git/p"
    assert sessions.clean_dir("") == ""                      # empty clears it
    with pytest.raises(ValueError):                          # would corrupt _FMT parsing
        sessions.clean_dir("~/a::b")


def test_start_dir_parsed_from_listing(monkeypatch):
    # @serai_dir rides the listing next to the live cwd; both reach the Session
    now = int(time.time())
    listing = f"shell-x::0::{now}::prod::bash::~/git/proj::/home/u/elsewhere\n"
    def fake_run(argv, timeout=6):
        if "list-sessions" in argv:
            return listing
        return "" if "capture-pane" in argv else ""
    monkeypatch.setattr(sessions, "_run", fake_run)
    s = sessions._list_sessions_uncached("local")[0]
    assert s.dir == "~/git/proj"          # the configured start dir
    assert s.path == "/home/u/elsewhere"  # the live cwd, still separate
    assert s.tags == ["prod"]


def test_api_dir_sets_and_validates(monkeypatch):
    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv) or True))
    monkeypatch.setattr("serai.main._known_hosts", lambda: {"local"})
    c = TestClient(app)
    r = c.post("/api/dir", json={"host": "local", "name": "shell-a", "path": " ~/git/p "})
    assert r.json() == {"ok": True, "path": "~/git/p"}
    assert calls[-1][-2:] == ["@serai_dir", "~/git/p"]
    assert c.post("/api/dir", json={"host": "evil", "name": "shell-a", "path": "/x"}).status_code == 400
    assert c.post("/api/dir", json={"host": "local", "name": "shell-a",
                                    "path": "/a::b"}).status_code == 400   # separator guard


def test_clean_tags_normalizes_and_dedupes():
    assert sessions.clean_tags(["Prod", " db ", "we b!", "Prod", ""]) == ["Prod", "db", "we-b"]


def test_session_tags_parsed_from_listing(monkeypatch):
    def fake_run(argv, timeout=6):
        if "list-sessions" in argv:
            return "shell-x::1::100::prod,db\ncc-y::0::99::\n"
        return ""  # capture-pane and friends
    monkeypatch.setattr(sessions, "_run", fake_run)
    by = {s.name: s for s in sessions._list_sessions_uncached("local")}
    assert by["shell-x"].tags == ["prod", "db"]
    assert by["cc-y"].tags == []


def test_preview_skips_prompt_furniture():
    # A Claude pane parked at its prompt: the literal last lines are a drawn input
    # box and a hint, which say nothing on a card. Show the last real output.
    parked = [
        "I refactored the scheduler and all tests pass.",
        "╭──────────────────────────────╮",
        "│ >                            │",
        "╰──────────────────────────────╯",
        "  ? for shortcuts",
    ]
    assert sessions._preview(parked) == "I refactored the scheduler and all tests pass."

    # a bare caret and rules are furniture too
    assert sessions._preview(["done: 12 files", "❯", "────────────"]) == "done: 12 files"

    # nothing but furniture -> fall back to the raw tail rather than a blank card
    assert sessions._preview(["╭─────╮", "│ >   │", "╰─────╯"]) != ""

    # a shell prompt carries its path, so it is content, not furniture
    assert "alice@box:~/git$" in sessions._preview(["alice@box:~/git$"])


def test_state_and_tail_from_listing(monkeypatch):
    # New 6-field _FMT: name::attached::activity::tags::pane_current_command::path
    now = int(time.time())
    listing = (
        # name::attached::activity::tags::command::@serai_dir::path (dir empty here)
        f"cc-working::0::{now}::::node::::/home/u/app\n"        # "esc to interrupt" -> working
        f"cc-recent::0::{now - 60}::::node::::/home/u/app\n"    # parked at prompt, recent -> done
        f"cc-stale::0::{now - 99999}::::node::::/home/u/app\n"  # dormant cc -> idle
        f"shell-run::0::{now - 99999}::::npm::::/home/u/app\n"  # process running -> working
        f"shell-idle::0::{now - 99999}::::bash::::/home/u/app\n"# quiet shell at prompt -> idle
    )
    def fake_run(argv, timeout=6):
        if "list-sessions" in argv:
            return listing
        if "capture-pane" in argv:
            # the working cc session shows Claude's live status line
            if "cc-working" in " ".join(argv):
                return "✻ Compacting the context… (esc to interrupt)\n"
            return "line one\nline two\nAll good here\n"
        return ""
    monkeypatch.setattr(sessions, "_run", fake_run)
    by = {s.name: s for s in sessions._list_sessions_uncached("local")}
    assert by["cc-working"].state == "running"
    assert by["cc-recent"].state == "done"
    assert by["cc-stale"].state == "idle"
    assert by["shell-run"].state == "running"
    assert by["shell-idle"].state == "idle"
    # the tail preview is the pane's last lines; path is the final (::-safe) field
    assert "esc to interrupt" in by["cc-working"].tail.lower()
    assert "All good here" in by["cc-recent"].tail
    assert by["cc-recent"].path == "/home/u/app"


def test_rename_endpoint_builds_prefixed_name(monkeypatch):
    cap = {}
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (cap.__setitem__("argv", argv), True)[1])
    r = TestClient(app).post("/api/rename",
                             json={"host": "local", "name": "shell-main", "kind": "shell", "label": "deploy box"})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "shell-deploy-box"}  # sanitized + prefix kept
    assert cap["argv"] == ["tmux", "rename-session", "-t", "shell-main", "shell-deploy-box"]


def test_tags_endpoint_sanitizes_and_stores(monkeypatch):
    cap = {}
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (cap.__setitem__("argv", argv), True)[1])
    r = TestClient(app).post("/api/tags",
                             json={"host": "local", "name": "shell-main", "tags": ["prod ", "d b!", "prod"]})
    assert r.json() == {"ok": True, "tags": ["prod", "d-b"]}
    assert cap["argv"] == ["tmux", "set-option", "-t", "shell-main", "@serai_tags", "prod,d-b"]


def test_rename_rejects_unknown_host(monkeypatch):
    monkeypatch.setattr(config, "parse_ssh_config", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv), True)[1])
    r = TestClient(app).post("/api/rename",
                             json={"host": "evil", "name": "shell-x", "kind": "shell", "label": "y"})
    assert r.status_code == 400 and calls == []


def test_kill_argv_local_and_remote():
    assert sessions.kill_argv("local", "shell-a") == ["tmux", "kill-session", "-t", "shell-a"]
    r = sessions.kill_argv("proj-web", "shell-a")
    assert r[0] == "ssh" and r[-1] == "tmux kill-session -t shell-a"


def test_kill_endpoint(monkeypatch):
    cap = {}
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (cap.__setitem__("argv", argv), True)[1])
    r = TestClient(app).post("/api/kill", json={"host": "local", "name": "shell-x"})
    assert r.json() == {"ok": True}
    assert cap["argv"] == ["tmux", "kill-session", "-t", "shell-x"]


def test_kill_rejects_unknown_host(monkeypatch):
    monkeypatch.setattr(config, "parse_ssh_config", lambda *a, **k: [])
    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv), True)[1])
    r = TestClient(app).post("/api/kill", json={"host": "evil", "name": "shell-x"})
    assert r.status_code == 400 and calls == []


def test_session_exists(monkeypatch):
    cap = {}
    monkeypatch.setattr(sessions, "_run", lambda argv, timeout=6: (cap.__setitem__("argv", argv), "")[1])
    assert sessions.session_exists("local", "shell-x") is True
    assert cap["argv"] == ["tmux", "has-session", "-t", "=shell-x"]  # '=' -> exact match, not prefix
    sessions.session_exists("proj-web", "shell-x")
    assert cap["argv"][0] == "ssh" and cap["argv"][-1] == "tmux has-session -t =shell-x"
    # a non-zero exit (no such session) -> _run returns None -> False
    monkeypatch.setattr(sessions, "_run", lambda argv, timeout=6: None)
    assert sessions.session_exists("local", "shell-x") is False


def test_api_hosts_annotates_reachability(monkeypatch):
    monkeypatch.setattr(config, "parse_ssh_config",
                        lambda *a, **k: [config.Host(alias="proj-web", hostname="192.0.2.21", port=2222)])
    seen = {}
    def fake_reach(hostname, port=22, timeout=2.0):
        seen["args"] = (hostname, port)
        return False
    monkeypatch.setattr(sessions, "tcp_reachable", fake_reach)

    data = TestClient(app).get("/api/hosts").json()
    assert data[0]["alias"] == "proj-web"
    assert data[0]["reachable"] is False
    assert seen["args"] == ("192.0.2.21", 2222)  # probes the resolved host:port


# --- auth ------------------------------------------------------------------

@pytest.fixture
def auth_on(tmp_path, monkeypatch):
    """Enable auth against an isolated config dir (never the real ~/.config).
    Runs after the autouse `_auth_off`, so its SERAI_AUTH win is the final one."""
    monkeypatch.setenv("SERAI_AUTH", "on")
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    auth._setup_token = None  # fresh one-time setup code per test
    return tmp_path


def test_password_hash_is_not_plaintext_and_verifies(auth_on):
    auth.add_user("alice", "correct horse")
    rec = auth.get_user("alice")
    assert "correct horse" not in json.dumps(rec)        # never stored in clear
    assert auth.verify_credentials("alice", "correct horse") == {"username": "alice", "admin": False}
    assert auth.verify_credentials("alice", "wrong") is None
    assert auth.verify_credentials("ghost", "correct horse") is None  # unknown user


def test_password_and_username_rules(auth_on):
    with pytest.raises(ValueError):
        auth.add_user("alice", "short")               # < 8 chars
    with pytest.raises(ValueError):
        auth.add_user("bad name!", "longenough")         # illegal username
    auth.add_user("alice", "longenough")
    with pytest.raises(ValueError):
        auth.add_user("alice", "longenough")           # duplicate


def test_token_sign_verify_and_tamper(auth_on):
    auth.add_user("alice", "longenough")
    tok = auth.issue_token("alice")
    assert auth.verify_token(tok)["username"] == "alice"
    assert auth.verify_token(tok[:-1] + ("A" if tok[-1] != "A" else "B")) is None  # bad sig
    assert auth.verify_token("garbage") is None
    assert auth.verify_token(auth.issue_token("alice", ttl=-1)) is None          # expired


def test_token_rejected_after_user_removed(auth_on):
    auth.add_user("a", "longenough", admin=True)
    auth.add_user("b", "longenough")
    tok = auth.issue_token("b")
    assert auth.verify_token(tok)["username"] == "b"
    auth.remove_user("b")
    assert auth.verify_token(tok) is None               # cookie dies with the user


def test_setup_open_first_run_then_login_logout(auth_on):
    # Default (trust-on-first-use): the first POST creates the admin, no code.
    c = TestClient(app)
    status = c.get("/api/auth/status").json()
    assert status["enabled"] and not status["configured"] and not status["authenticated"]
    assert status["setup_code_required"] is False

    r = c.post("/api/setup", json={"username": "alice", "password": "longenough"})
    assert r.status_code == 200 and r.json()["admin"] is True
    assert auth.COOKIE in r.cookies

    after = c.get("/api/auth/status").json()
    assert after["authenticated"] is True and after["user"] == "alice" and after["admin"] is True
    # once configured, setup is permanently closed regardless of mode
    assert c.post("/api/setup", json={"username": "x", "password": "longenough"}).status_code == 403

    c.post("/api/logout")
    assert c.get("/api/auth/status").json()["authenticated"] is False
    assert c.post("/api/login", json={"username": "alice", "password": "wrong"}).status_code == 401
    assert c.post("/api/login", json={"username": "alice", "password": "longenough"}).status_code == 200


def test_setup_code_mode_requires_token(auth_on, monkeypatch):
    monkeypatch.setenv("SERAI_SETUP_CODE", "1")
    auth._setup_token = None
    c = TestClient(app)
    assert c.get("/api/auth/status").json()["setup_code_required"] is True
    # no code / wrong code -> rejected
    assert c.post("/api/setup", json={"username": "alice", "password": "longenough"}).status_code == 403
    assert c.post("/api/setup", json={"token": "nope", "username": "alice",
                                      "password": "longenough"}).status_code == 403
    # correct code -> creates the admin
    r = c.post("/api/setup", json={"token": auth.get_setup_token(),
                                   "username": "alice", "password": "longenough"})
    assert r.status_code == 200 and r.json()["admin"] is True


def test_api_blocked_without_session(auth_on):
    auth.add_user("alice", "longenough", admin=True)
    anon = TestClient(app)
    assert anon.get("/api/sessions").status_code == 401
    assert anon.get("/api/settings").status_code == 401
    assert anon.get("/").status_code == 200               # SPA shell stays public
    assert anon.get("/api/auth/status").status_code == 200  # so does the status probe

    c = TestClient(app)
    c.post("/api/login", json={"username": "alice", "password": "longenough"})
    assert c.get("/api/sessions").status_code != 401       # cookie now lets it through


def test_users_admin_crud_and_guards(auth_on):
    auth.add_user("alice", "longenough", admin=True)
    admin = TestClient(app)
    admin.post("/api/login", json={"username": "alice", "password": "longenough"})

    assert admin.post("/api/users", json={"username": "bob", "password": "longenough"}).status_code == 200
    listing = admin.get("/api/users").json()
    assert {u["username"] for u in listing["users"]} == {"alice", "bob"}
    assert listing["me"] == "alice"

    # a non-admin cannot manage users
    bob = TestClient(app)
    bob.post("/api/login", json={"username": "bob", "password": "longenough"})
    assert bob.get("/api/users").status_code == 403
    assert bob.post("/api/users", json={"username": "x", "password": "longenough"}).status_code == 403

    # bob may change his own password but not alice's
    assert bob.post("/api/users/bob/password", json={"password": "newlongpass"}).status_code == 200
    assert bob.post("/api/users/alice/password", json={"password": "newlongpass"}).status_code == 403

    # lockout guards
    assert admin.delete("/api/users/alice").status_code == 400   # can't remove self
    assert admin.delete("/api/users/bob").status_code == 200
    auth.add_user("solo", "longenough", admin=True)
    auth.remove_user("alice")  # leave 'solo' as the only admin via the store
    solo = TestClient(app)
    solo.post("/api/login", json={"username": "solo", "password": "longenough"})
    # 'solo' is the last admin; the store guard blocks removing the last admin
    with pytest.raises(ValueError):
        auth.remove_user("solo")


def test_ws_rejects_without_session(auth_on):
    auth.add_user("alice", "longenough", admin=True)
    with TestClient(app).websocket_connect("/ws/attach?host=local&kind=shell&label=t") as ws:
        got, code = "", None
        for _ in range(5):
            m = ws.receive()
            if m.get("text"):
                got += m["text"]
            if m["type"] == "websocket.close":
                code = m.get("code")
                break
    assert "not logged in" in got.lower()
    assert code == 4401


def test_auth_off_leaves_app_open(monkeypatch):
    # the autouse _auth_off fixture already set SERAI_AUTH=off
    status = TestClient(app).get("/api/auth/status").json()
    assert status["enabled"] is False and status["authenticated"] is True


# --- TLS -------------------------------------------------------------------

def test_tls_enabled_flag(monkeypatch):
    monkeypatch.delenv("SERAI_TLS", raising=False)
    assert tls.tls_enabled() is True            # on by default
    for off in ("off", "0", "false", "no"):
        monkeypatch.setenv("SERAI_TLS", off)
        assert tls.tls_enabled() is False
    monkeypatch.setenv("SERAI_TLS", "on")
    assert tls.tls_enabled() is True


def test_tls_self_signed_generated_and_reused(tmp_path, monkeypatch):
    from cryptography import x509
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SERAI_CERT", raising=False)
    monkeypatch.delenv("SERAI_KEY", raising=False)

    cert, key = tls.resolve()
    assert cert.exists() and key.exists()
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600   # key is not world-readable

    parsed = x509.load_pem_x509_certificate(cert.read_bytes())
    cn = parsed.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value
    assert cn == "serai"
    san = parsed.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "localhost" in san.get_values_for_type(x509.DNSName)
    import ipaddress
    assert ipaddress.ip_address("127.0.0.1") in san.get_values_for_type(x509.IPAddress)
    assert parsed.not_valid_after_utc > parsed.not_valid_before_utc

    before = cert.read_bytes()
    cert2, key2 = tls.resolve()                          # second run reuses, doesn't regenerate
    assert (cert2, key2) == (cert, key)
    assert cert.read_bytes() == before


def test_tls_cert_includes_configured_hostname_and_regenerates(tmp_path, monkeypatch):
    from cryptography import x509
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SERAI_CERT", raising=False)
    monkeypatch.delenv("SERAI_KEY", raising=False)
    monkeypatch.delenv("SERAI_HOST", raising=False)
    monkeypatch.delenv("SERAI_HOSTNAME", raising=False)

    def sans(cert_path):
        c = x509.load_pem_x509_certificate(cert_path.read_bytes())
        return c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value

    cert, _ = tls.resolve()
    assert "serai.home.example" not in sans(cert).get_values_for_type(x509.DNSName)
    first = cert.read_bytes()

    # add a name + IP -> the next resolve regenerates to cover them
    monkeypatch.setenv("SERAI_HOSTNAME", "serai.home.example, 192.0.2.5")
    cert2, _ = tls.resolve()
    assert cert2 == cert and cert.read_bytes() != first        # same path, regenerated
    dns = sans(cert).get_values_for_type(x509.DNSName)
    ips = sans(cert).get_values_for_type(x509.IPAddress)
    assert "serai.home.example" in dns
    import ipaddress as _ip
    assert _ip.ip_address("192.0.2.5") in ips

    # now stable: covered -> no further regeneration
    before = cert.read_bytes()
    tls.resolve()
    assert cert.read_bytes() == before


def test_netcfg_write_preserves_other_lines_and_roundtrips(tmp_path, monkeypatch):
    env = tmp_path / "serai.env"
    env.write_text("SERAI_HOST=127.0.0.1\nSERAI_PORT=8022\nSERAI_AUTH=off\nSERAI_HOSTNAME=\n")
    monkeypatch.setenv("SERAI_ENV_FILE", str(env))
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))

    names = netcfg.write("0.0.0.0", "serai.home.example, 192.0.2.214")
    assert names == ["serai.home.example", "192.0.2.214"]
    text = env.read_text()
    assert "SERAI_HOST=0.0.0.0" in text
    assert "SERAI_HOSTNAME=serai.home.example,192.0.2.214" in text
    assert "SERAI_AUTH=off" in text and "SERAI_PORT=8022" in text   # untouched

    got = netcfg.read()
    assert got["host"] == "0.0.0.0"
    assert got["hostnames"] == "serai.home.example,192.0.2.214"
    assert got["port"] == "8022"


def test_netcfg_rejects_env_injection(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_ENV_FILE", str(tmp_path / "serai.env"))
    # '=' and newline entries are dropped -> can't smuggle in another SERAI_* line
    assert netcfg.clean_hostnames("good.lan, bad=evil, ok.lan\nSERAI_AUTH=off, two.lan") \
        == ["good.lan", "two.lan"]
    assert netcfg.valid_host("0.0.0.0") and netcfg.valid_host("serai.lan")
    assert not netcfg.valid_host("a b")
    assert not netcfg.valid_host("x\nSERAI_AUTH=off")
    assert not netcfg.valid_host("host\n")          # trailing newline rejected (fullmatch)
    with pytest.raises(ValueError):
        netcfg.write("bad host", "")


def test_netcfg_appends_keys_when_absent(tmp_path, monkeypatch):
    env = tmp_path / "serai.env"
    env.write_text("# just a comment\nSERAI_PORT=9000\n")
    monkeypatch.setenv("SERAI_ENV_FILE", str(env))
    netcfg.write("0.0.0.0", "serai.lan")
    text = env.read_text()
    assert "SERAI_HOST=0.0.0.0" in text and "SERAI_HOSTNAME=serai.lan" in text
    assert "SERAI_PORT=9000" in text                 # preserved


def test_netcfg_no_service_means_no_self_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))         # no user unit under here
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert netcfg.service_mode() is None
    r = netcfg.restart()
    assert r["ok"] is False and r["mode"] is None


def test_network_api_admin_only(auth_on):
    auth.add_user("alice", "longenough", admin=True)
    auth.add_user("bob", "longenough")
    anon = TestClient(app)
    assert anon.get("/api/network").status_code in (401, 403)  # gated
    bob = TestClient(app)
    bob.post("/api/login", json={"username": "bob", "password": "longenough"})
    assert bob.get("/api/network").status_code == 403          # non-admin
    admin = TestClient(app)
    admin.post("/api/login", json={"username": "alice", "password": "longenough"})
    assert admin.get("/api/network").status_code == 200


def test_tls_bind_host_added_to_cert(tmp_path, monkeypatch):
    from cryptography import x509
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.delenv("SERAI_CERT", raising=False)
    monkeypatch.delenv("SERAI_KEY", raising=False)
    monkeypatch.delenv("SERAI_HOSTNAME", raising=False)
    monkeypatch.setenv("SERAI_HOST", "serai.home.example")
    cert, _ = tls.resolve()
    c = x509.load_pem_x509_certificate(cert.read_bytes())
    san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert "serai.home.example" in san.get_values_for_type(x509.DNSName)


def test_tls_bring_your_own_cert(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path / "cfg"))
    # generate a pair, then point BYO env at it
    monkeypatch.delenv("SERAI_CERT", raising=False)
    monkeypatch.delenv("SERAI_KEY", raising=False)
    cert, key = tls.resolve()
    monkeypatch.setenv("SERAI_CERT", str(cert))
    monkeypatch.setenv("SERAI_KEY", str(key))
    assert tls.resolve() == (cert, key)


def test_tls_byo_missing_or_half_set_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_CERT", str(tmp_path / "nope.pem"))
    monkeypatch.setenv("SERAI_KEY", str(tmp_path / "nope.key"))
    with pytest.raises(SystemExit):
        tls.resolve()
    monkeypatch.delenv("SERAI_KEY", raising=False)       # only one set -> error
    with pytest.raises(SystemExit):
        tls.resolve()


# --- session restore after reboot -----------------------------------------

def test_restore_argv_local_and_remote():
    cl = sessions.restore_argv("local", "cc-x", "claude", "/home/u/app")
    assert cl == ["tmux", "new", "-A", "-d", "-s", "cc-x", "-c", "/home/u/app", "claude --continue"]
    sh = sessions.restore_argv("local", "shell-y", "shell", "/home/u")
    assert sh == ["tmux", "new", "-A", "-d", "-s", "shell-y", "-c", "/home/u"]  # no command -> a shell
    rem = sessions.restore_argv("web1", "cc-z", "claude", "/srv/app")
    assert rem[0] == "ssh" and rem[-2] == "web1" and "BatchMode=yes" in rem
    assert rem[-1] == "tmux new -A -d -s cc-z -c /srv/app 'claude --continue'"


def test_restore_argv_path_stays_a_single_argv_element():
    # a path with spaces / shell metachars must stay one argv element (invariant #3)
    hostile = "/home/u/my proj; rm -rf /"
    av = sessions.restore_argv("local", "shell-x", "shell", hostile)
    assert av[av.index("-c") + 1] == hostile          # present verbatim, one element
    rem = sessions.restore_argv("web1", "shell-x", "shell", hostile)[-1]
    assert "'/home/u/my proj; rm -rf /'" in rem        # shlex.quote keeps it intact for ssh


def test_store_snapshot_keeps_missing_and_updates_tags(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([
        {"host": "local", "name": "a", "kind": "shell", "label": "a", "path": "/p", "tags": ["t1"]},
        {"host": "local", "name": "b", "kind": "shell", "label": "b", "path": "/q", "tags": []},
    ])
    assert {r["name"] for r in store.saved()} == {"a", "b"}
    # a poll that only sees 'a' must NOT drop 'b' (the reboot case), but updates its tags
    store.upsert([{"host": "local", "name": "a", "kind": "shell", "label": "a", "path": "/p", "tags": ["t1", "t2"]}])
    assert {r["name"] for r in store.saved()} == {"a", "b"}
    assert next(r["tags"] for r in store.saved() if r["name"] == "a") == ["t1", "t2"]
    store.remove("local", "a")                          # explicit kill drops it
    assert {r["name"] for r in store.saved()} == {"b"}
    p = tmp_path / "sessions.json"
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_api_sessions_saved_and_restore(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([
        {"host": "local", "name": "cc-proj", "kind": "claude", "label": "proj",
         "path": "/home/u/proj", "tags": ["prod"]},
        {"host": "local", "name": "shell-x", "kind": "shell", "label": "x", "path": "/home/u", "tags": []},
    ])
    c = TestClient(app)
    saved = c.get("/api/sessions/saved").json()
    assert {r["name"] for r in saved} == {"cc-proj", "shell-x"}

    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv) or True))
    monkeypatch.setattr(sessions, "session_exists", lambda host, name: False)
    r = c.post("/api/sessions/restore").json()
    assert r["restored"] == 2 and r["skipped"] == 0
    # claude recreated detached w/ --continue in its dir; shell recreated; tags reapplied
    assert ["tmux", "new", "-A", "-d", "-s", "cc-proj", "-c", "/home/u/proj", "claude --continue"] in calls
    assert ["tmux", "new", "-A", "-d", "-s", "shell-x", "-c", "/home/u"] in calls
    assert ["tmux", "set-option", "-t", "cc-proj", "@serai_tags", "prod"] in calls


def test_api_restore_skips_running_sessions(tmp_path, monkeypatch):
    # Already-running sessions (recreated by hand, or a remote host that never
    # rebooted) must be skipped: `tmux new -A -d` on an existing session tries to
    # attach and fails without a tty, so blind recreation reports false failures.
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([
        {"host": "local", "name": "shell-alive", "kind": "shell", "label": "alive", "path": "/a", "tags": []},
        {"host": "local", "name": "shell-dead", "kind": "shell", "label": "dead", "path": "/d", "tags": []},
    ])
    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv) or True))
    monkeypatch.setattr(sessions, "session_exists", lambda host, name: name == "shell-alive")
    r = TestClient(app).post("/api/sessions/restore").json()
    assert r["restored"] == 1 and r["skipped"] == 1
    skipped = next(x for x in r["results"] if x["name"] == "shell-alive")
    assert skipped["ok"] is True and skipped.get("skipped") is True
    assert not any("shell-alive" in " ".join(a) for a in calls)   # nothing run for it
    assert any("shell-dead" in " ".join(a) for a in calls)        # the dead one recreated


def test_api_restore_honours_per_session_resume(tmp_path, monkeypatch):
    # A target may choose how its Claude session comes back. Everything else still
    # comes from the snapshot, and an unrecognised choice falls back to continue.
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([
        {"host": "local", "name": "cc-a", "kind": "claude", "label": "a", "path": "/a", "tags": []},
        {"host": "local", "name": "cc-b", "kind": "claude", "label": "b", "path": "/b", "tags": []},
        {"host": "local", "name": "cc-c", "kind": "claude", "label": "c", "path": "/c", "tags": []},
        {"host": "local", "name": "cc-d", "kind": "claude", "label": "d", "path": "/d", "tags": []},
    ])
    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv) or True))
    monkeypatch.setattr(sessions, "session_exists", lambda host, name: False)
    TestClient(app).post("/api/sessions/restore", json={"targets": [
        {"host": "local", "name": "cc-a", "resume": "continue"},
        {"host": "local", "name": "cc-b", "resume": "resume"},
        {"host": "local", "name": "cc-c", "resume": ""},          # fresh
        {"host": "local", "name": "cc-d", "resume": "; rm -rf /"},  # nonsense -> continue
    ]})
    launched = {a[a.index("-s") + 1]: a[-1] for a in calls if "-s" in a}
    assert launched["cc-a"] == "claude --continue"
    assert launched["cc-b"] == "claude --resume"
    assert launched["cc-c"] == "claude"                 # fresh conversation
    assert launched["cc-d"] == "claude --continue"      # never the raw value


def test_api_restore_targets_pick_a_subset(tmp_path, monkeypatch):
    # {"targets": [...]} selects snapshot entries; kind/path/tags still come from
    # the snapshot, and unknown targets are ignored rather than executed.
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([
        {"host": "local", "name": "shell-a", "kind": "shell", "label": "a", "path": "/a", "tags": []},
        {"host": "local", "name": "shell-b", "kind": "shell", "label": "b", "path": "/b", "tags": []},
    ])
    calls = []
    monkeypatch.setattr(sessions, "run_send", lambda argv, timeout=6: (calls.append(argv) or True))
    monkeypatch.setattr(sessions, "session_exists", lambda host, name: False)
    r = TestClient(app).post("/api/sessions/restore", json={"targets": [
        {"host": "local", "name": "shell-b"},
        {"host": "local", "name": "shell-not-saved"},   # not in the snapshot -> ignored
    ]}).json()
    assert r["restored"] == 1
    joined = [" ".join(a) for a in calls]
    assert any("shell-b" in c and "-c /b" in c for c in joined)   # path from the snapshot
    assert not any("shell-a" in c for c in joined)                # unselected stays untouched
    assert not any("shell-not-saved" in c for c in joined)        # never executed


def test_file_ops_local_roundtrip(tmp_path):
    base = str(tmp_path)
    files.file_op("mkdir", "local", f"{base}/newdir")
    assert os.path.isdir(f"{base}/newdir")
    (tmp_path / "a.txt").write_text("hello")
    files.file_op("rename", "local", f"{base}/a.txt", f"{base}/newdir/b.txt")   # move
    assert (tmp_path / "newdir" / "b.txt").read_text() == "hello"
    files.file_op("copy", "local", f"{base}/newdir", f"{base}/newdir2")         # recursive dir copy
    assert (tmp_path / "newdir2" / "b.txt").read_text() == "hello"
    files.file_op("delete", "local", f"{base}/newdir2/b.txt")                   # file delete
    assert not (tmp_path / "newdir2" / "b.txt").exists()
    files.file_op("delete", "local", f"{base}/newdir")                          # recursive dir delete
    assert not (tmp_path / "newdir").exists()


def test_file_ops_refuse_catastrophic_targets():
    for bad in ("", "/", "~", "~/", ".", ".."):
        with pytest.raises(ValueError):
            files.delete_path("local", bad)
    with pytest.raises(ValueError):
        files.rename_path("local", "/tmp/x", "~")  # dest guarded too


def test_file_ops_remote_tilde_and_shell_safety(monkeypatch):
    sftp = _FakeSFTP()
    client = _use_sftp(monkeypatch, sftp)
    # rename goes over SFTP with home-relative expansion
    files.rename_path("proj-web", "~/old.txt", "~/dir/new.txt")
    assert sftp.renamed == ("old.txt", "dir/new.txt")
    files.make_dir("proj-web", "~/made")
    assert sftp.made == "made"
    # recursive delete/copy run over exec_command -- hostile paths stay quoted
    hostile = "~/x; rm -rf $HOME; `reboot`.txt"
    files.delete_path("proj-web", hostile)
    assert client.execs[-1] == "rm -rf -- 'x; rm -rf $HOME; `reboot`.txt'"
    files.copy_path("proj-web", "~/a dir", "~/b dir")
    assert client.execs[-1] == "cp -a -- 'a dir' 'b dir'"


def test_transfer_local_to_local_and_refuses_dirs(tmp_path):
    src = tmp_path / "src"; dst = tmp_path / "dst"
    src.mkdir(); dst.mkdir()
    (src / "a.txt").write_text("relay me")
    files.transfer_path("local", f"{src}/a.txt", "local", f"{dst}/a.txt")
    assert (dst / "a.txt").read_text() == "relay me"
    with pytest.raises(ValueError):                       # folders are refused
        files.transfer_path("local", str(src), "local", f"{dst}/copy")


def test_transfer_relays_remote_to_local(monkeypatch, tmp_path):
    # read from the (mocked) remote over SFTP, write locally -- the server is
    # the relay; the hosts never talk to each other
    sftp = _FakeSFTP(contents={"logs/app.log": b"remote bytes"})
    _use_sftp(monkeypatch, sftp)
    files.transfer_path("proj-web", "~/logs/app.log", "local", f"{tmp_path}/app.log")
    assert (tmp_path / "app.log").read_bytes() == b"remote bytes"


def test_transfer_folder_local_to_local(tmp_path):
    src = tmp_path / "src" / "proj"; dst = tmp_path / "dst"
    (src / "sub").mkdir(parents=True); dst.mkdir()
    (src / "top.txt").write_text("t")
    (src / "sub" / "deep.txt").write_text("d")
    # transfer_path dispatches folders to the tar relay
    files.transfer_path("local", str(src), "local", f"{dst}/proj")
    assert (dst / "proj" / "top.txt").read_text() == "t"
    assert (dst / "proj" / "sub" / "deep.txt").read_text() == "d"
    with pytest.raises(ValueError):  # the folder keeps its name across hosts
        files.transfer_tree("local", str(src), "local", f"{dst}/renamed")


def test_folder_relay_streams_instead_of_buffering(tmp_path):
    # The whole point of the relay change: memory is a fixed buffer, not the size
    # of the tree. Buffering 32 MB would show up plainly in the peak.
    src = tmp_path / "src" / "big"; src.mkdir(parents=True)
    (tmp_path / "dst").mkdir()
    blob = b"\0" * (4 * 1024 * 1024)
    for i in range(8):                                     # 32 MB across 8 files
        (src / f"f{i}.bin").write_bytes(blob)

    tracemalloc.start()
    try:
        moved = files.transfer_tree("local", str(src), "local", f"{tmp_path / 'dst'}/big")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert (tmp_path / "dst" / "big" / "f7.bin").read_bytes() == blob  # it really copied
    assert moved > 32 * 1024 * 1024                                    # and all of it moved
    assert peak < 8 * 1024 * 1024, f"peak {peak} bytes -- the tar looks buffered"


def test_folder_relay_reports_progress(tmp_path):
    # the relay counts bytes anyway; on_progress just carries them out, so a
    # multi-GB paste can report instead of blocking silently
    src = tmp_path / "src" / "big"; src.mkdir(parents=True)
    (tmp_path / "dst").mkdir()
    blob = b"\0" * (1024 * 1024)
    for i in range(6):                                   # 6 MB, several progress ticks
        (src / f"f{i}.bin").write_bytes(blob)

    seen = []
    moved = files.transfer_tree("local", str(src), "local", f"{tmp_path / 'dst'}/big",
                                on_progress=seen.append)
    assert (tmp_path / "dst" / "big" / "f5.bin").read_bytes() == blob   # it still copied
    assert len(seen) >= 2, "expected several progress ticks, not one at the end"
    assert seen == sorted(seen), "byte counts must only ever climb"
    assert seen[-1] <= moved and moved > 6 * 1024 * 1024                # final is the total
    # a single file has nothing to stream: one tick, equal to its size
    (tmp_path / "one.txt").write_bytes(b"hello")
    ticks = []
    n = files.transfer_path("local", str(tmp_path / "one.txt"), "local",
                            f"{tmp_path / 'dst'}/one.txt", on_progress=ticks.append)
    assert n == 5 and ticks == [5]


def test_api_files_transfer_streams_progress_then_result(monkeypatch, tmp_path):
    # the endpoint emits NDJSON: {"moved":N}... then a terminal ok (or error)
    monkeypatch.setattr("serai.main._known_hosts", lambda: {"local", "proj-web"})

    def fake_transfer(src_host, src_path, dst_host, dst_path, on_progress=None):
        for n in (1024, 4096):
            on_progress(n)
        return 4096
    monkeypatch.setattr(files, "transfer_path", fake_transfer)
    c = TestClient(app)
    r = c.post("/api/files/transfer", json={"host": "local", "path": "/d/x",
                                            "src_host": "proj-web", "src_path": "/s/x"})
    assert r.status_code == 200
    lines = [json.loads(x) for x in r.text.strip().split("\n")]
    assert lines[:2] == [{"moved": 1024}, {"moved": 4096}]
    assert lines[-1] == {"ok": True, "moved": 4096}          # terminal line carries the total

    # a failure arrives as the stream's last line, not a dropped connection
    def boom(*a, **k):
        raise RuntimeError("remote tar failed")
    monkeypatch.setattr(files, "transfer_path", boom)
    r = c.post("/api/files/transfer", json={"host": "local", "path": "/d/x",
                                            "src_host": "proj-web", "src_path": "/s/x"})
    assert json.loads(r.text.strip().split("\n")[-1]) == {"error": "remote tar failed"}
    # unknown source host is still refused outright
    assert c.post("/api/files/transfer", json={"host": "local", "path": "/d/x",
                                               "src_host": "evil", "src_path": "/s/x"}).status_code == 400


def test_untar_refuses_path_traversal(tmp_path):
    # a hostile source could hand back a tar with ../-escaping members; the
    # local extraction must block them (data_filter on 3.12+, manual guard older)
    import io as _io, tarfile as _tar
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w") as tf:
        info = _tar.TarInfo("../evil.txt")
        payload = b"pwned"
        info.size = len(payload)
        tf.addfile(info, _io.BytesIO(payload))
    dest = tmp_path / "safe"; dest.mkdir()
    with pytest.raises(Exception):
        # the relay extracts from a stream, so the guard has to hold there too
        files._untar_local_stream(_io.BytesIO(buf.getvalue()), str(dest))
    assert not (tmp_path / "evil.txt").exists()   # nothing escaped the dest


def test_transfer_folder_remote_ends_are_quoted(monkeypatch, tmp_path):
    client = _use_sftp(monkeypatch, _FakeSFTP())
    # local -> remote: the untar runs on the destination with quoted parent
    src = tmp_path / "my dir"; src.mkdir(); (src / "f.txt").write_text("x")
    files.transfer_tree("local", str(src), "proj-web", "~/dest parent/my dir")
    assert client.execs[-1] == "tar -C 'dest parent' -xf -"
    assert b"".join(client.stdin_chunks)          # the tar stream was written
    # remote -> local: the tar runs on the source with quoted name
    import io as _io, tarfile as _tar
    buf = _io.BytesIO()
    with _tar.open(fileobj=buf, mode="w") as tf:
        d = _tar.TarInfo("odd; name"); d.type = _tar.DIRTYPE; tf.addfile(d)
        i = _tar.TarInfo("odd; name/in.txt"); i.size = 2; tf.addfile(i, _io.BytesIO(b"ok"))
    client.exec_stdout = buf.getvalue()
    monkeypatch.setattr(files, "_is_dir", lambda host, path: True)
    files.transfer_path("proj-web", "~/odd; name", "local", f"{tmp_path}/odd; name")
    assert client.execs[-1] == "tar -C . -cf - -- 'odd; name'"
    assert (tmp_path / "odd; name" / "in.txt").read_bytes() == b"ok"


def test_api_transfer_validates_source_host(monkeypatch):
    c = TestClient(app)
    called = []
    monkeypatch.setattr(files, "transfer_path", lambda *a: called.append(a))
    monkeypatch.setattr("serai.main._known_hosts", lambda: {"local", "proj-web"})
    r = c.post("/api/files/op", json={"op": "transfer", "host": "local", "path": "/tmp/x",
                                      "src_host": "evil-host", "src_path": "/etc/passwd"})
    assert r.status_code == 400 and not called            # unknown source host rejected
    r = c.post("/api/files/op", json={"op": "transfer", "host": "local", "path": "/tmp/x",
                                      "src_host": "proj-web", "src_path": "~/x"})
    assert r.status_code == 200 and called == [("proj-web", "~/x", "local", "/tmp/x")]


def test_api_files_op_validation_and_dispatch(monkeypatch):
    c = TestClient(app)
    calls = []
    monkeypatch.setattr(files, "file_op", lambda op, host, path, dest=None: calls.append((op, host, path, dest)))
    assert c.post("/api/files/op", json={"op": "chmod", "host": "local", "path": "/x"}).status_code == 400
    assert c.post("/api/files/op", json={"op": "delete", "host": "evil-host", "path": "/x"}).status_code == 400
    assert c.post("/api/files/op", json={"op": "rename", "host": "local", "path": "/x"}).status_code == 400  # no dest
    assert not calls  # nothing dispatched for rejected requests
    r = c.post("/api/files/op", json={"op": "rename", "host": "local", "path": "/a", "dest": "/b"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert calls == [("rename", "local", "/a", "/b")]


def test_version_header_gated_only(tmp_path, monkeypatch):
    # Gated API responses carry the running version (the UI's status bar + its
    # "updated underneath you -- reload" detection); the public login surface
    # doesn't advertise it.
    from serai import __version__
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    c = TestClient(app)
    gated = c.get("/api/sessions/saved")
    assert gated.headers.get("x-serai-version") == __version__
    public = c.get("/api/auth/status")
    assert "x-serai-version" not in public.headers


def test_version_is_read_from_disk_for_graceful_deploys(tmp_path, monkeypatch):
    # a frontend-only deploy replaces files without restarting; the running
    # process must advertise the NEW on-disk version so tabs get their prompt
    import serai.main as main
    vf = tmp_path / "__init__.py"
    vf.write_text('__version__ = "9.9.9"\n')
    monkeypatch.setattr(main, "_VERSION_FILE", vf)
    monkeypatch.setitem(main._ver_cache, "at", 0.0)   # expire the cache
    assert main._current_version() == "9.9.9"
    r = TestClient(app).get("/api/sessions/saved")
    assert r.headers.get("x-serai-version") == "9.9.9"
    vf.unlink()                                        # unreadable -> keep last known
    monkeypatch.setitem(main._ver_cache, "at", 0.0)
    assert main._current_version() == "9.9.9"


def _installer_detect(src: Path, installed: Path) -> str:
    out = subprocess.run(
        ["bash", str(src / "install.sh"), "--prefix", str(installed), "--detect-only"],
        capture_output=True, text=True, timeout=30,
    )
    assert out.returncode == 0, out.stderr
    return out.stdout.strip().splitlines()[-1]


def test_installer_detects_frontend_vs_backend_changes(tmp_path):
    # build a fake checkout + a matching "installed" copy, then poke each side
    repo = Path(__file__).resolve().parent.parent
    src = tmp_path / "src"; installed = tmp_path / "installed"
    for base in (src, installed):
        (base / "serai").mkdir(parents=True)
        (base / "web").mkdir()
        for f in ("config.py", "__init__.py"):
            (base / "serai" / f).write_text((repo / "serai" / f).read_text())
        (base / "web" / "app.js").write_text("// app")
        (base / "run.sh").write_text("#!/bin/sh\n")
        (base / "pyproject.toml").write_text((repo / "pyproject.toml").read_text())
    import shutil as _sh
    _sh.copy(repo / "install.sh", src / "install.sh")

    assert _installer_detect(src, installed) == "frontend-only"          # identical
    (src / "web" / "app.js").write_text("// app v2")
    assert _installer_detect(src, installed) == "frontend-only"          # web-only change
    (src / "serai" / "__init__.py").write_text('__version__ = "9.9.9"\n')
    assert _installer_detect(src, installed) == "frontend-only"          # version-only bump
    (src / "serai" / "config.py").write_text("# changed backend\n")
    assert _installer_detect(src, installed) == "backend-changed"        # real backend change
    (src / "serai" / "config.py").write_text((repo / "serai" / "config.py").read_text())
    (src / "run.sh").write_text("#!/bin/sh\n# changed\n")
    assert _installer_detect(src, installed) == "backend-changed"        # run.sh counts as backend


def test_version_header_absent_for_anonymous_callers(auth_on):
    # An unauthenticated probe of a gated route gets a 401 -- the rejection
    # itself must not advertise the running version.
    r = TestClient(app).get("/api/sessions")
    assert r.status_code == 401
    assert "x-serai-version" not in r.headers


# --- update check ----------------------------------------------------------
# The network call itself is never exercised; _fetch_latest is monkeypatched.
# What matters here is the version comparison, the interval/opt-out precedence,
# and that an unreachable GitHub degrades instead of raising.

def _updates_env(tmp_path, monkeypatch):
    """Point both the settings blob and the update cache at a temp dir."""
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SERAI_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.delenv("SERAI_UPDATE_CHECK", raising=False)


def test_update_version_compare_handles_prefix_and_prerelease():
    newer = updates.is_newer
    assert newer("2.15.0", "2.14.2")
    assert newer("v2.15.0", "2.14.2")          # a "v" prefix is not a version bump
    assert not newer("2.14.2", "2.14.2")
    assert not newer("2.14.1", "2.14.2")
    assert not newer("2.9.0", "2.14.2")        # numeric, not lexical: 9 < 14
    assert newer("2.14.10", "2.14.2")
    assert newer("2.15.0", "2.15.0-rc1")       # a release beats its own pre-release
    assert not newer("2.15.0-rc1", "2.15.0")
    assert not newer("garbage", "2.14.2")      # unparseable never claims an update


def test_update_interval_defaults_to_weekly_and_reads_the_settings_blob(tmp_path, monkeypatch):
    _updates_env(tmp_path, monkeypatch)
    assert updates.interval() == "weekly"                      # the shipped default
    settings.save({updates.SETTING_KEY: "daily"})
    assert updates.interval() == "daily"
    settings.save({updates.SETTING_KEY: '"monthly"'})          # JSON-quoted localStorage value
    assert updates.interval() == "monthly"
    settings.save({updates.SETTING_KEY: "nonsense"})
    assert updates.interval() == "weekly"                      # unknown -> default, not crash


def test_update_env_off_overrides_the_stored_choice(tmp_path, monkeypatch):
    _updates_env(tmp_path, monkeypatch)
    settings.save({updates.SETTING_KEY: "daily"})
    monkeypatch.setenv("SERAI_UPDATE_CHECK", "off")
    assert updates.interval() == "off"
    called = []
    monkeypatch.setattr(updates, "_fetch_latest", lambda: called.append(1) or {})
    # even "check now" must not reach the network when the install disabled it
    st = updates.status(force=True)
    assert called == [] and st["env_locked"] is True and st["interval"] == "off"


def test_update_status_reports_available_and_caches(tmp_path, monkeypatch):
    _updates_env(tmp_path, monkeypatch)
    calls = []

    def fake():
        calls.append(1)
        return {"latest": "99.0.0", "url": "https://example.invalid/r", "error": None,
                "no_releases": False}

    monkeypatch.setattr(updates, "_fetch_latest", fake)
    st = updates.status()                       # never checked -> due
    assert st["available"] is True and st["latest"] == "99.0.0"
    assert st["current"] == serai.__version__
    updates.status()                            # within the interval -> cached
    assert len(calls) == 1, "a second call inside the interval must not re-poll"
    updates.status(force=True)                  # "check now" ignores the interval
    assert len(calls) == 2


def test_update_status_survives_an_unreachable_github(tmp_path, monkeypatch):
    _updates_env(tmp_path, monkeypatch)
    monkeypatch.setattr(updates, "_fetch_latest",
                        lambda: {"error": "couldn't reach GitHub (URLError)"})
    st = updates.status()
    assert st["available"] is False and st["error"]
    assert st["current"] == serai.__version__   # still reports what's running


def test_update_endpoints_round_trip(tmp_path, monkeypatch):
    _updates_env(tmp_path, monkeypatch)
    monkeypatch.setattr(updates, "_fetch_latest",
                        lambda: {"latest": "99.0.0", "url": None, "error": None,
                                 "no_releases": False})
    c = TestClient(app)
    body = c.get("/api/updates").json()
    assert body["available"] is True
    assert c.post("/api/updates/check").json()["latest"] == "99.0.0"


def test_head_on_the_ui_routes_matches_get():
    # Uptime monitors probe with HEAD. FastAPI's @app.get registers GET only
    # (a plain Starlette route would add HEAD), so these answered 405 while GET
    # answered 200 -- indistinguishable from an outage to a monitor.
    c = TestClient(app)
    for path in ("/", "/favicon.ico"):
        get_r, head_r = c.get(path), c.head(path)
        assert get_r.status_code == 200, path
        assert head_r.status_code == 200, f"HEAD {path} -> {head_r.status_code}"
        # a HEAD reply carries the headers of the GET but no body
        assert head_r.headers.get("content-type") == get_r.headers.get("content-type")
        assert head_r.content == b""


# --- settings merge --------------------------------------------------------
# The UI mirrors its whole localStorage into one blob. Replacing on write made
# that last-writer-wins ACROSS TABS: a tab opened before a preference existed
# has no such key, so its next save dropped the key for everyone.

def test_settings_put_merges_instead_of_replacing(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_SETTINGS", str(tmp_path / "settings.json"))
    c = TestClient(app)
    # tab A knows about the update cadence and saves it
    assert c.put("/api/settings", json={"serai.updates.interval": "daily",
                                        "serai.term.font": "mono"}).status_code == 200
    # tab B has been open since before that setting existed; it saves a splitter
    assert c.put("/api/settings", json={"serai.term.font": "mono",
                                        "serai.files.height": "240"}).status_code == 200
    blob = c.get("/api/settings").json()
    assert blob["serai.updates.interval"] == "daily", "tab B clobbered a key it never knew about"
    assert blob["serai.files.height"] == "240"       # and tab B's own write landed


def test_settings_merge_keeps_unrelated_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_SETTINGS", str(tmp_path / "settings.json"))
    settings.save({"a": "1", "b": "2"})
    assert settings.merge({"b": "3", "c": "4"}) == {"a": "1", "b": "3", "c": "4"}
    assert settings.load() == {"a": "1", "b": "3", "c": "4"}


def test_update_interval_survives_a_save_from_an_unaware_tab(tmp_path, monkeypatch):
    """The reported bug, end to end: set daily, let another tab save, still daily."""
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("SERAI_SETTINGS", str(tmp_path / "settings.json"))
    monkeypatch.delenv("SERAI_UPDATE_CHECK", raising=False)
    c = TestClient(app)
    c.put("/api/settings", json={updates.SETTING_KEY: "daily"})
    assert updates.interval() == "daily"
    c.put("/api/settings", json={"serai.term.size": "15"})     # an older tab saves
    assert updates.interval() == "daily", "the cadence reverted to the default"
