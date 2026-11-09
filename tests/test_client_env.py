"""The environment serai gives the tmux clients it runs -- and so every session."""

from __future__ import annotations

import os

from serai import sessions


def test_serai_settings_do_not_leak_into_sessions(monkeypatch):
    monkeypatch.setenv("SERAI_HOST", "0.0.0.0")
    monkeypatch.setenv("SERAI_PORT", "8022")
    monkeypatch.setenv("EXAMPLE_KEEP", "yes")
    monkeypatch.delenv("TERM", raising=False)
    env = sessions.client_env()
    assert not [k for k in env if k.startswith("SERAI_")]
    assert env["EXAMPLE_KEEP"] == "yes"
    assert env["TERM"] == "xterm-256color" and env["COLORTERM"] == "truecolor"
    assert os.environ["SERAI_HOST"] == "0.0.0.0"           # serai's own process keeps its settings


def test_the_suite_never_sees_the_real_tmux_server():
    assert "TMUX" not in os.environ and "TMUX_PANE" not in os.environ
    assert os.environ["TMUX_TMPDIR"].startswith("/tmp/serai-t-")


def test_a_server_serai_starts_has_none_of_its_settings(monkeypatch):
    import shutil
    import subprocess
    import pytest
    if shutil.which("tmux") is None:
        pytest.skip("tmux not installed")
    monkeypatch.setenv("SERAI_HOST", "0.0.0.0")
    monkeypatch.setenv("SHELL", "/bin/sh")
    sock = f"{os.environ['TMUX_TMPDIR']}/tmux-{os.getuid()}/default"     # the conftest's private server
    assert sessions.create_session(sessions.create_argv("local", "shell-envdemo", "shell"))
    try:
        glob = subprocess.run(["tmux", "-S", sock, "show-environment", "-g"],
                              capture_output=True, text=True, timeout=10).stdout
        assert glob and "SERAI_" not in glob
    finally:
        subprocess.run(["tmux", "-S", sock, "kill-server"], capture_output=True, timeout=10)
