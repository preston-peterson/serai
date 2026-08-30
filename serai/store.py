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

import json
import os
import tempfile
import time
from pathlib import Path

# the descriptor kept per session -- enough to recreate it, nothing sensitive
_FIELDS = ("host", "name", "kind", "label", "path", "tags", "args", "owner")

# In-memory "seen live this run" tracking, for the resume-after-exit affordance.
# Not persisted: it answers "which sessions did I just have open?", which is a
# property of the current serai run, not durable state. A machine reboot (all
# sessions gone) is the restore banner's job, driven by the persistent snapshot;
# this is the narrower "you exited an agent session a moment ago -- resume it?".
_seen_live: dict[str, float] = {}   # host::name -> wall time last seen live
_live_now: set[str] = set()         # keys live as of the most recent mark_live
# how long an exited session keeps offering resume (env override for forks/tests)
_RESUME_WINDOW = 6 * 3600.0


def _config_dir() -> Path:
    override = os.environ.get("SERAI_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "serai"


def _path() -> Path:
    return _config_dir() / "sessions.json"


def _key(host: str, name: str) -> str:
    return f"{host}::{name}"


def _is_self_dir(path: str) -> bool:
    """True if ``path`` is serai's own working directory -- where a tmux session
    lands when it is created with no start directory. Never a meaningful dir for
    a session, so ``upsert`` refuses to record it. Compared normalised, so a
    trailing slash can't smuggle the same directory past the check."""
    from . import sessions            # leaf import: sessions never imports store
    return sessions._same_dir(path, sessions.own_dir())


def _load() -> dict:
    try:
        data = json.loads(_path().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(data: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".sessions-")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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
        _atomic_write(data)


def set_tags(host: str, name: str, tags: list) -> None:
    """Record a deliberate tag change (including a clear) straight into the
    snapshot, so ``upsert``'s sticky-tags rule can't later resurrect old tags.
    A no-op if the session isn't snapshotted yet -- the next poll will add it."""
    data = _load()
    k = _key(host, name)
    rec = data.get(k)
    if rec is None:
        return
    if rec.get("tags") != tags:
        rec["tags"] = tags
        _atomic_write(data)


def set_args(host: str, name: str, args: str) -> None:
    """Record a deliberate change to a session's extra agent args (including a
    clear) straight into the snapshot, so ``upsert``'s sticky rule can't later
    resurrect the old value. A no-op if the session isn't snapshotted yet."""
    data = _load()
    rec = data.get(_key(host, name))
    if rec is None:
        return
    if rec.get("args") != args:
        rec["args"] = args
        _atomic_write(data)


def remove(host: str, name: str) -> None:
    """Drop a profile (called on an explicit kill / forget)."""
    data = _load()
    if data.pop(_key(host, name), None) is not None:
        _atomic_write(data)


def get(host: str, name: str) -> dict | None:
    """The saved profile for ``host``/``name``, or None."""
    return _load().get(_key(host, name))


def saved() -> list[dict]:
    """The saved session descriptors, in a stable order."""
    return sorted(_load().values(), key=lambda r: (r.get("host", ""), r.get("name", "")))


def mark_live(keys) -> None:
    """Record which sessions are live right now (called each /api/sessions poll).
    Stamps a last-seen time so a session that later vanishes can be offered for
    resume, and remembers the current live set to tell live from exited."""
    global _live_now
    now = time.time()
    keys = list(keys)
    for k in keys:
        _seen_live[k] = now
    _live_now = set(keys)


def recently_exited(window: float | None = None) -> list[dict]:
    """Claude sessions seen live earlier this run but no longer live -- the
    resume-after-`/exit` candidates.

    Requires a snapshot record (for the dir/tags/kind needed to relaunch), so an
    explicitly-killed session (``remove`` dropped its record) never resurfaces,
    and only *agent* sessions are offered -- a shell has nothing to resume. Scoped
    to a recent window so old exits don't pile up; the reboot case, where nothing
    was seen live this run, stays the restore banner's job.
    """
    from . import sessions            # leaf import: sessions never imports store
    agents = sessions.AGENT_KINDS
    win = _RESUME_WINDOW if window is None else window
    now = time.time()
    data = _load()
    out = []
    for k, seen in _seen_live.items():
        if k in _live_now or (now - seen) > win:
            continue
        rec = data.get(k)
        if rec and rec.get("kind") in agents:
            out.append(rec)
    return sorted(out, key=lambda r: (r.get("host", ""), r.get("name", "")))
