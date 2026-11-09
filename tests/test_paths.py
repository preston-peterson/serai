"""Tests for serai.paths -- the one config dir and the one atomic writer.

The pins here are the two contracts every other module now depends on: the
config dir is created 0700 (and an existing dir's mode is left alone), and a
write either lands whole or doesn't land at all, leaving no temp file behind.

Run:  pytest -q tests/test_paths.py
"""

import os
import stat

import pytest

from serai import paths


# --- config_dir ------------------------------------------------------------

def test_config_dir_honours_the_env_override(monkeypatch, tmp_path):
    target = tmp_path / "cfg"
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(target))
    assert paths.config_dir() == target
    assert target.is_dir()                       # created on the way out


def test_config_dir_falls_back_to_xdg_then_home(monkeypatch, tmp_path):
    monkeypatch.delenv("SERAI_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.config_dir() == tmp_path / "serai"      # read-only: it exists
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    assert paths.config_dir() == tmp_path / "home" / ".config" / "serai"


def test_config_dir_is_created_0700(monkeypatch, tmp_path):
    # the dir holds password hashes and the cookie secret, so it is owner-only
    target = tmp_path / "deep" / "cfg"
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(target))
    assert paths.config_dir() == target
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_config_dir_does_not_restamp_an_existing_dir(monkeypatch, tmp_path):
    target = tmp_path / "cfg"
    target.mkdir()
    target.chmod(0o755)                          # the operator's choice
    monkeypatch.setenv("SERAI_CONFIG_DIR", str(target))
    paths.config_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o755


# --- atomic_write ----------------------------------------------------------

def test_atomic_write_replaces_the_file_and_leaves_no_temp(tmp_path):
    p = tmp_path / "thing.json"
    paths.atomic_write(p, '{"a": 1}')
    assert p.read_text(encoding="utf-8") == '{"a": 1}'
    # nothing else in the directory: the temp file was renamed, not left behind
    assert sorted(q.name for q in tmp_path.iterdir()) == ["thing.json"]


def test_atomic_write_overwrites_in_place(tmp_path):
    p = tmp_path / "thing.json"
    paths.atomic_write(p, "first")
    paths.atomic_write(p, "second and longer")
    assert p.read_text(encoding="utf-8") == "second and longer"
    assert sorted(q.name for q in tmp_path.iterdir()) == ["thing.json"]


def test_atomic_write_defaults_to_0600_and_honours_an_explicit_mode(tmp_path):
    secret = tmp_path / "auth_secret"
    paths.atomic_write(secret, "deadbeef")
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600

    public = tmp_path / "cert.pem"
    paths.atomic_write(public, b"-----BEGIN CERTIFICATE-----\n", mode=0o644)
    assert public.read_bytes() == b"-----BEGIN CERTIFICATE-----\n"
    assert stat.S_IMODE(public.stat().st_mode) == 0o644


def test_atomic_write_encodes_str_as_utf8(tmp_path):
    p = tmp_path / "notes.txt"
    paths.atomic_write(p, "serai — café")
    assert p.read_bytes() == "serai — café".encode("utf-8")


def test_atomic_write_creates_a_missing_parent(tmp_path):
    p = tmp_path / "nested" / "dir" / "sessions.json"
    paths.atomic_write(p, "{}")
    assert p.read_text(encoding="utf-8") == "{}"


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_atomic_write_leaves_the_old_file_intact_when_the_write_fails(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    p = ro / "sessions.json"
    paths.atomic_write(p, "good")
    ro.chmod(0o500)                              # readable, not writable
    try:
        with pytest.raises(OSError):
            paths.atomic_write(p, "half-written garbage")
        assert p.read_text(encoding="utf-8") == "good"
        assert stat.S_IMODE(p.stat().st_mode) == 0o600
        # the failed temp file was cleaned up, not orphaned next to the target
        assert sorted(q.name for q in ro.iterdir()) == ["sessions.json"]
    finally:
        ro.chmod(0o700)                          # let pytest's tmp cleanup work
