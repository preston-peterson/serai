"""Persist saved session profiles so they can be started again on purpose.

A live tmux session is an instance; this file is the *profile* -- host, name,
kind, label, dir, tags, args, owner -- of sessions the operator meant to keep.
Creating or seeing a session upserts a profile; an explicit kill/forget
(:func:`remove`) drops it. A session that merely vanishes (``/exit``, reboot)
keeps its profile, so resume is on demand from + New / the palette, not a
nag. Stored as JSON under the config dir (``~/.config/serai/sessions.json``);
descriptors only, never credentials (invariant #1).
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path

from . import paths

try:
    import fcntl                      # POSIX only; absence is handled, not fatal
except ImportError:
    fcntl = None

# the descriptor kept per session -- enough to recreate it, nothing sensitive.
# `favorite` and `launch` are store-side only: they are never read from a live
# session (they are not tmux options), so upsert decides them purely from the
# stored record, never a poll.
_FIELDS = ("host", "name", "kind", "label", "path", "tags", "args", "owner",
           "favorite", "launch")

# how a profile starts an agent: a fresh run, pick up the last conversation, or
# open the picker. "" is the clear -- fall back to effective_launch's default.
LAUNCH_MODES = ("new", "continue", "resume", "")


def _path() -> Path:
    return paths.config_dir() / "sessions.json"


def _key(host: str, name: str) -> str:
    return f"{host}::{name}"


# Serialises every load-mutate-save on sessions.json. Two levels, for the two
# ways it can be lost:
#   * the thread lock covers in-process races -- upsert runs in the executor
#     pool on every /api/sessions poll (from every tab) while set_tags /
#     set_args / set_favorite / remove run from other handlers, so two
#     read-modify-writes can interleave and one is silently lost (the same
#     fault settings.merge takes a lock for). An RLock because locked callers
#     may nest (favorites -> saved).
#   * the flock covers other *processes*: an external script editing
#     sessions.json raced a live poll and lost its update the same way. It
#     lives in a sidecar (sessions.json.lock, 0600, created on first use) so
#     the lock file is never the one os.replace() swaps out from under a
#     holder. On a platform without fcntl, or a filesystem that won't take
#     flock, the thread lock alone still holds (in-process safety preserved).
_lock = threading.RLock()


def _lock_path() -> Path:
    return Path(str(_path()) + ".lock")


@contextlib.contextmanager
def _locked():
    """Hold both locks for one load-mutate-save (or a consistent read)."""
    with _lock:
        if fcntl is None:
            yield
            return
        try:
            fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            yield                          # unreadable dir: thread lock only
            return
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
            except OSError:
                pass                       # fs won't flock: thread lock only
            yield
        finally:
            os.close(fd)                   # closing the fd drops the flock


def _is_self_dir(path: str) -> bool:
    """True if ``path`` is serai's own working directory -- where a tmux session
    lands when it is created with no start directory. Never a meaningful dir for
    a session, so ``upsert`` refuses to record it. Compared normalised, so a
    trailing slash can't smuggle the same directory past the check."""
    from . import sessions            # leaf import: sessions never imports store
    return sessions._same_dir(path, sessions.own_dir())


def _load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    """Serialise the snapshot and hand it to the one atomic writer (0600: profiles
    are private, and the rename means a poll can never read a half-written file)."""
    paths.atomic_write(_path(), json.dumps(data, indent=2, sort_keys=True), 0o600)


def upsert(records: list[dict]) -> None:
    """Add/update snapshot entries for the given (currently-running) sessions.

    Never removes an entry that is merely absent from ``records`` -- a missing
    session may be gone from a reboot, which is what restore is for. Only writes
    when something actually changed.

    The rule throughout: **a degraded live value must never destroy good stored
    state.** The snapshot exists to survive a session ceasing to exist, so it
    cannot throw away what it needs to recreate one the instant that session is
    seen in a diminished state.

    Tags are sticky: an *empty* tags value on a live session does not overwrite
    non-empty stored tags -- e.g. an attached session recreated tag-less by a
    reconnect after a restart. A deliberate clear is recorded by the explicit
    tag-set path (see ``set_tags`` -> ``store.set_tags``), not here.

    The stored dir follows the same rule, with one extra guard. It prefers the
    session's configured start-in dir (``@serai_dir``) over ``pane_current_path``,
    since that is the dir the session should be *recreated* in and it doesn't
    drift when you cd. Failing that it takes the live pane's cwd -- except when
    that cwd is serai's own working directory, which is never a real session dir:
    it is simply where a session lands when it was created with no start dir at
    all. Letting that overwrite a good stored dir is how every Claude session
    once ended up restoring into serai's install directory.
    """
    with _locked():
        data = _load()
        changed = False
        for r in records:
            rec = {k: r.get(k) for k in _FIELDS}
            if not rec["host"] or not rec["name"]:
                continue
            k = _key(rec["host"], rec["name"])
            prior = data.get(k)
            # empty live tags must not clobber good stored tags (the bug this guards)
            if not rec.get("tags") and prior and prior.get("tags"):
                rec["tags"] = prior["tags"]
            # same rule for the extra agent args: a session recreated without its
            # @serai_args comes back bare, and that must not erase the stored value.
            # A deliberate clear goes through set_args, not through a degraded poll.
            if not rec.get("args") and prior and prior.get("args"):
                rec["args"] = prior["args"]
            # ...and for the owner. A session recreated without its @serai_owner must
            # not silently become unowned, which would hide it from the user whose
            # session it is. Ownership is only ever set at creation, so there is no
            # "deliberate clear" path to respect here.
            if not rec.get("owner") and prior and prior.get("owner"):
                rec["owner"] = prior["owner"]
            # favorite is store-side only (set_favorite, never a live value): a poll
            # carries it forward from the prior record and never clears it. Kept a
            # clean bool so the JSON is true/false, never null. The live snapshot has
            # no favorite, so it is decided by the stored state, not the poll -- the
            # same three-for-three rule as tags/dir/args/owner.
            rec["favorite"] = bool((prior or {}).get("favorite"))
            # ...and for launch, store-side in exactly the same way: a poll carries
            # the stored choice forward and never clears it. The default for an
            # undecided profile is computed on read by effective_launch, never
            # written, so a poll storing "resume" here would be the bug.
            rec["launch"] = (prior or {}).get("launch") or ""
            # An explicit start-in dir is authoritative over wherever the pane sits.
            # serai's own cwd is disqualified in *either* role: it is not a dir anyone
            # chose, and once it leaks into @serai_dir it would otherwise reassert
            # itself on every poll. Falling back, keep what we had rather than record
            # "nowhere" or serai's own directory as a session's project dir.
            live_dir, live_path = r.get("dir") or "", rec.get("path") or ""
            if live_dir and not _is_self_dir(live_dir):
                rec["path"] = live_dir
            elif not live_path or _is_self_dir(live_path):
                rec["path"] = (prior or {}).get("path") or ""
            if prior != rec:
                data[k] = rec
                changed = True
        if changed:
            _save(data)


def set_tags(host: str, name: str, tags: list) -> None:
    """Record a deliberate tag change (including a clear) straight into the
    snapshot, so ``upsert``'s sticky-tags rule can't later resurrect old tags.
    A no-op if the session isn't snapshotted yet -- the next poll will add it."""
    with _locked():
        data = _load()
        k = _key(host, name)
        rec = data.get(k)
        if rec is None:
            return
        if rec.get("tags") != tags:
            rec["tags"] = tags
            _save(data)


def set_args(host: str, name: str, args: str) -> None:
    """Record a deliberate change to a session's extra agent args (including a
    clear) straight into the snapshot, so ``upsert``'s sticky rule can't later
    resurrect the old value. A no-op if the session isn't snapshotted yet."""
    with _locked():
        data = _load()
        rec = data.get(_key(host, name))
        if rec is None:
            return
        if rec.get("args") != args:
            rec["args"] = args
            _save(data)


def set_favorite(host: str, name: str, favorite: bool) -> None:
    """Record a deliberate favorite/unfavorite (including a create-path claim)
    straight into the snapshot, so upsert's carry-forward rule can't later change
    it. A no-op if the session isn't snapshotted yet -- the next poll will add
    the record (see upsert's favorite rule), and a claim that precedes it is
    re-applied by the caller once the record exists."""
    with _locked():
        data = _load()
        rec = data.get(_key(host, name))
        if rec is None:
            return
        if bool(rec.get("favorite")) != bool(favorite):
            rec["favorite"] = bool(favorite)
            _save(data)


def set_launch(host: str, name: str, launch: str) -> None:
    """Record a deliberate launch choice (``new`` / ``continue`` / ``resume``, or
    ``""`` to clear it and fall back to the per-kind default). Like set_favorite
    this is the only writer that can change or clear a stored value a poll merely
    carries forward; a no-op if the session isn't snapshotted yet.

    Raises ValueError for anything outside LAUNCH_MODES. The value is a fixed
    literal, never free text -- the same discipline as resume_flag's choices, and
    what keeps a guessed payload from becoming a command-line fragment (invariant
    #3)."""
    if launch not in LAUNCH_MODES:
        raise ValueError(f"invalid launch mode: {launch!r}")
    with _locked():
        data = _load()
        rec = data.get(_key(host, name))
        if rec is None:
            return
        if rec.get("launch") != launch:
            rec["launch"] = launch
            _save(data)


def effective_launch(rec: dict) -> str:
    """The launch rule a profile actually means: what was chosen, or the default
    for its kind -- ``resume`` for an agent (there is a conversation to return to)
    and ``new`` for anything else. Computed, never stored, so the default can
    change without a migration."""
    stored = rec.get("launch") or ""
    if stored:
        return stored
    from . import sessions            # leaf import: sessions never imports store
    return "resume" if sessions._AGENTS.get(rec.get("kind")) else "new"


def favorites() -> list[dict]:
    """The saved profiles flagged as favorites, in the stable saved() order."""
    return [r for r in saved() if r.get("favorite")]


def remove(host: str, name: str) -> None:
    """Drop a profile (called on an explicit kill / forget)."""
    with _locked():
        data = _load()
        if data.pop(_key(host, name), None) is not None:
            _save(data)


def get(host: str, name: str) -> dict | None:
    """The saved profile for ``host``/``name``, or None."""
    return _load().get(_key(host, name))


def saved() -> list[dict]:
    """The saved session descriptors, in a stable order.

    Each record carries ``launch`` as its *effective* value (the choice, or the
    per-kind default), so every reader -- the favorites menu, + New, the palette
    -- sees one launch rule without each having to re-implement the default. The
    default is computed on read and never written back: on disk an undecided
    profile stays ``launch: ""``.

    Read under the lock too: ``atomic_write`` already means a reader never sees
    a half-written file, but holding the lock makes ``saved()`` a consistent
    snapshot with respect to a locked writer's whole load-mutate-save, not just
    to its final rename."""
    with _locked():
        data = _load()
    out = sorted(data.values(), key=lambda r: (r.get("host", ""), r.get("name", "")))
    for r in out:
        r["launch"] = effective_launch(r)
    return out
