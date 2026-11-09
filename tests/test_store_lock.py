"""Concurrency tests for the store's load-mutate-save lock.

``upsert`` runs in the executor pool on every /api/sessions poll from every tab
while ``set_tags`` / ``set_args`` / ``set_favorite`` / ``remove`` run from other
handlers, so without a lock one read-modify-write silently reverts another
(the item 0g trap). These tests pin that a concurrent writer's update is never
lost; the first one fails (at least intermittently) with the lock removed.
"""

import os
import stat
import sys
import importlib
import threading
import time

import pytest

from serai import store


NAMES = [f"cc-{i}" for i in range(8)]


def _rec(name):
    return {"host": "local", "name": name, "kind": "claude",
            "label": name[3:], "path": f"/srv/{name}", "tags": ["seed"]}


def test_concurrent_set_tags_lose_no_updates(tmp_path, monkeypatch):
    """8 threads, each tagging its own session 50 times, all at once: every
    record must end at its thread's last write. Without the lock, a thread's
    save carries a stale read of *another* thread's record and reverts it."""
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([_rec(n) for n in NAMES])

    def writer(i):
        for r in range(50):
            store.set_tags("local", NAMES[i], [f"t{i}-r{r}"])

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = {r["name"]: r["tags"] for r in store.saved()}
    lost = {n: final[n] for n in NAMES if final[n] != [f"t{n[3:]}-r49"]}
    assert not lost, f"lost updates: {lost}"


def test_upsert_loop_never_resurrects_favorite_after_unset(tmp_path, monkeypatch):
    """upsert carries `favorite` forward from the stored record; that is only
    safe if it can't interleave with set_favorite. Thread A polls upsert in a
    loop while B sets the flag True then False -- the final state must be the
    last explicit set (False), not a True loaded into an upsert's read window."""
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    store.upsert([_rec("cc-s")])

    stop = threading.Event()

    def poller():
        while not stop.is_set():
            store.upsert([_rec("cc-s")])

    a = threading.Thread(target=poller)
    a.start()
    try:
        time.sleep(0.01)
        store.set_favorite("local", "cc-s", True)
        time.sleep(0.02)
        store.set_favorite("local", "cc-s", False)
        time.sleep(0.02)          # more upserts land after the last explicit set
    finally:
        stop.set()
        a.join()

    rec = store.get("local", "cc-s")
    assert rec["favorite"] is False


@pytest.mark.skipif(store.fcntl is None, reason="no fcntl on this platform")
def test_lock_file_exists_with_mode_0600(tmp_path, monkeypatch):
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    lock = tmp_path / "sessions.json.lock"
    assert not lock.exists()                      # not created until a write
    store.upsert([_rec("cc-a")])
    assert lock.exists()
    assert stat.S_IMODE(os.stat(lock).st_mode) == 0o600


def test_store_imports_and_works_without_fcntl(tmp_path, monkeypatch):
    """A platform without fcntl must still import the module (thread lock
    only) and keep every behaviour that does not need cross-process safety."""
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(tmp_path))
    monkeypatch.setitem(sys.modules, "fcntl", None)
    try:
        m = importlib.reload(store)
        assert m.fcntl is None
        m.upsert([_rec("cc-a")])                  # the lock-free fallback works
        m.set_tags("local", "cc-a", ["x"])
        assert next(r["tags"] for r in m.saved() if r["name"] == "cc-a") == ["x"]
        assert not (tmp_path / "sessions.json.lock").exists()
    finally:
        monkeypatch.undo()                        # real fcntl back in sys.modules
        importlib.reload(store)                   # ...and re-bound in the module
