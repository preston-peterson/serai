"""Never let the suite reach the developer's tmux server.

Every test starts with no TMUX/TMUX_PANE and its own empty socket dir, so a tmux
call anywhere -- in a test or in the code under test -- talks to a server that
exists only if that test started it. (2026-11-09: one stray kill-server on the
real socket ended every session on the machine.) Fixtures that set their own
TMUX_TMPDIR still win: they run after this one."""

from __future__ import annotations

import shutil
import tempfile

import pytest


@pytest.fixture(autouse=True)
def _private_tmux(monkeypatch):
    d = tempfile.mkdtemp(prefix="serai-t-", dir="/tmp")    # short: tmux caps socket paths
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.setenv("TMUX_TMPDIR", d)
    yield
    shutil.rmtree(d, ignore_errors=True)
