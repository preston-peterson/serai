"""Persist the set of open sessions so they can be recreated after a reboot.

tmux sessions die when the machine reboots -- there is nothing to reconnect to
afterward -- so serai keeps a snapshot of what was open (host, name, kind,
label, working dir, tags) and can recreate them on demand. Stored as JSON under
the config dir (``~/.config/serai/sessions.json``, matching auth/settings); it
holds only session *descriptors*, never any credentials (invariant #1).

The snapshot is updated from live discovery and only ever *adds or updates*
entries -- it never drops one just because a session went missing, since that is
exactly the reboot case we restore. Explicit kills call :func:`remove`.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

# the descriptor kept per session -- enough to recreate it, nothing sensitive
_FIELDS = ("host", "name", "kind", "label", "path", "tags")


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
    """
    data = _load()
    changed = False
    for r in records:
        rec = {k: r.get(k) for k in _FIELDS}
        if not rec["host"] or not rec["name"]:
            continue
        k = _key(rec["host"], rec["name"])
        if data.get(k) != rec:
            data[k] = rec
            changed = True
    if changed:
        _atomic_write(data)


def remove(host: str, name: str) -> None:
    """Drop a session from the snapshot (called on an explicit kill)."""
    data = _load()
    if data.pop(_key(host, name), None) is not None:
        _atomic_write(data)


def saved() -> list[dict]:
    """The saved session descriptors, in a stable order."""
    return sorted(_load().values(), key=lambda r: (r.get("host", ""), r.get("name", "")))
