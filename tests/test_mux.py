"""Remote control clients share one ssh master per host: the ControlPath guard
and the per-host start lock (both found against a real host by
tests/browser/test_remote.py)."""

from __future__ import annotations

import asyncio
import os
import tempfile

from serai import tmuxctl
from serai.ws import attach


def test_mux_options_only_when_the_socket_path_fits(monkeypatch):
    short = tempfile.mkdtemp(prefix="srm-", dir="/tmp")
    monkeypatch.setenv("XDG_RUNTIME_DIR", short)
    argv = tmuxctl.control_argv("host-a", "shell-demo")
    assert argv[0] == "ssh" and "ControlMaster=auto" in argv
    assert f"ControlPath={short}/serai-ssh/%C" in argv
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/tmp/" + "d" * 60)       # ssh would refuse this socket name
    argv = tmuxctl.control_argv("host-a", "shell-demo")
    assert argv[0] == "ssh" and not any(a.startswith("Control") for a in argv)
    assert not os.path.exists("/tmp/" + "d" * 60)                     # and nothing was created for it
    monkeypatch.setenv("XDG_RUNTIME_DIR", short)
    monkeypatch.setenv("SERAI_SSH_MUX", "0")
    assert not tmuxctl.uses_mux("host-a")
    assert tmuxctl.control_argv("local", "shell-demo")[0] == "tmux"


def test_attaches_to_one_host_start_one_at_a_time(monkeypatch):
    monkeypatch.setattr(tmuxctl, "uses_mux", lambda host: host != "local")
    monkeypatch.setattr(attach, "_MUX_LOCKS", {})
    live: dict[str, int] = {}
    peak: dict[str, int] = {}

    class Fake:
        def __init__(self, host):
            self.host = host

        async def start(self):
            live[self.host] = live.get(self.host, 0) + 1
            peak[self.host] = max(peak.get(self.host, 0), live[self.host])
            await asyncio.sleep(0.05)
            live[self.host] -= 1

    async def run():
        hosts = ["host-a", "host-a", "host-a", "host-b", "local", "local"]
        t0 = asyncio.get_running_loop().time()
        await asyncio.gather(*(attach._start_control(h, Fake(h)) for h in hosts))
        return asyncio.get_running_loop().time() - t0

    took = asyncio.run(run())
    assert peak == {"host-a": 1, "host-b": 1, "local": 2}     # one master set-up per host; local never waits
    assert took < 0.25                                         # hosts don't wait on each other
