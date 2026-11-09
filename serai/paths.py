"""Where serai's files live, and the one way it writes them.

Everything serai persists -- the user store and cookie secret (auth), session
profiles (store), the UI settings blob (settings), the update cache (updates),
the self-signed cert pair (tls), the service env file (netcfg) -- resolves its
home through :func:`config_dir` and lands on disk through :func:`atomic_write`.
Both were reimplemented per module; a write that wasn't atomic in one of them
could leave a half-written ``users.json`` (locking everyone out) or a
half-written ``sessions.json`` (losing every profile).

Storage dir: ``$SERAI_CONFIG_DIR``, else ``$XDG_CONFIG_HOME/serai``, else
``~/.config/serai``. A module whose file has its own override (``SERAI_SETTINGS``,
``SERAI_ENV_FILE``) still honours that first; the override only replaces the
filename, not the directory rules.

stdlib only.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

# The config dir holds password hashes, the cookie-signing secret, and session
# profiles, so it is owner-only -- matching the 0600 default of what's inside it.
_DIR_MODE = 0o700


def config_dir() -> Path:
    """serai's config directory, created 0700 if it doesn't exist yet.

    Creation is part of the contract so no caller has to remember an ``mkdir``
    before it writes (several of them used to forget one on a first run). The
    mode is applied only when *we* create the directory: if it already exists its
    mode is the operator's choice, and silently re-stamping 0700 on a dir that is
    deliberately something else (a symlinked dotfile repo, say) would overwrite
    it on every poll.
    """
    override = os.environ.get("SERAI_CONFIG_DIR")
    if override:
        d = Path(override)
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        d = Path(base) / "serai"
    _ensure_dir(d)
    return d


def atomic_write(path, data: bytes | str, mode: int = 0o600) -> None:
    """Write ``data`` to ``path`` so a reader never sees it half-written.

    A temp file in the *same directory* (rename is only atomic within a
    filesystem), flushed and fsync'd, chmodded to ``mode``, then ``os.replace``d
    onto the target -- which is atomic, so any concurrent reader sees either the
    whole old file or the whole new one. ``str`` is encoded UTF-8. If anything
    fails the temp file is removed and the existing file is left untouched.

    ``mode`` defaults to 0600 because the files here mostly hold credentials;
    pass the mode explicitly for the ones that don't (a cert, a settings blob),
    rather than inheriting the umask of whatever process wrote them.
    """
    p = Path(path)
    payload = data.encode("utf-8") if isinstance(data, str) else data
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=str(p.parent), prefix=f".{p.name}.", delete=False)
    try:
        with tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, p)
    except BaseException:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
        raise


def _ensure_dir(d: Path) -> None:
    """Create ``d`` (parents included) when missing, 0700 on what we created."""
    if d.is_dir():
        return
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(_DIR_MODE)
    except OSError:
        # A filesystem that won't take the chmod (FAT, some mounts) is still a
        # usable config dir; the files inside keep their own 0600.
        pass
