"""Plumbing shared by the API routers.

Kept in one module so the routers stay a set of handlers: the worker pool every
blocking call is pushed onto, the host allowlist, the ownership rules, and the
body -> host -> owner gate that every mutating session route runs first.

Routers reach these through the module (`common._target(request)`) rather than
importing the names, exactly as they do for `sessions` / `store` / `config` --
the tests monkeypatch these attributes and have to keep reaching them.
"""

from __future__ import annotations

import asyncio
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse

from .. import auth, config, sessions, store
from .. import __version__

_pool = ThreadPoolExecutor(max_workers=8)

# The version as installed ON DISK, cached a few seconds. A frontend-only
# deploy copies new files without restarting the service (see install.sh);
# reading the version from disk lets the still-running process advertise the
# new one, so open tabs show it and get their "updated — reload" prompt.
_VERSION_FILE = Path(__file__).resolve().parent.parent / "__init__.py"
_ver_cache = {"at": 0.0, "v": __version__}


def _current_version() -> str:
    now = time.monotonic()
    if now - _ver_cache["at"] > 3.0:
        _ver_cache["at"] = now
        try:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', _VERSION_FILE.read_text())
            if m:
                _ver_cache["v"] = m.group(1)
        except OSError:
            pass  # unreadable -> keep the last known version
    return _ver_cache["v"]


def _known_hosts() -> set[str]:
    """Valid attach/broadcast targets: 'local' plus every ssh-config alias.

    Validating a client-supplied host against this set is a hard guard for
    invariant #3: an unchecked alias is handed to ssh as a positional arg, so a
    value like `-oProxyCommand=...` would be parsed as an option and run a local
    command. Restricting to configured hosts (invariant #6) closes that off.
    """
    return {"local", *(h.alias for h in config.parse_ssh_config())}


def _session(request: Request) -> dict | None:
    """Current {username, admin} for an authenticated request. With auth off,
    everyone is treated as an admin so management endpoints still function."""
    if not auth.auth_enabled():
        return {"username": None, "admin": True}
    return auth.verify_token(request.cookies.get(auth.COOKIE))


def _owns(sess: dict | None, owner: str) -> bool:
    """May this caller see/use a session owned by `owner`?

    Admins see everything -- they have to, to manage the fleet. Everyone else
    sees only what they created. An **unowned** session ("" -- made outside
    serai, or before ownership existed) is admin-only: those are the fleet the
    operator already had, and a new account should start with an empty board.

    This is an *organisational* boundary, not a security one. Every session runs
    as serai's own OS user, so any account that can open a shell here can reach
    every session regardless of what this returns. Confining users properly would
    mean running their sessions as separate OS users.
    """
    if sess is None:
        return False
    if sess.get("admin"):
        return True
    return bool(owner) and owner == sess.get("username")


async def _may_touch(sess: dict | None, host: str, name: str) -> bool:
    """Authorise an action on a session the caller named directly.

    A *live* session someone else owns is refused. A saved profile that is
    not live is the same rule -- otherwise a guessed name would let anyone
    forget someone else's profile via /api/kill. A name with no live session
    and no profile is a create, and that is fair game (ws_attach stamps the
    owner).
    """
    loop = asyncio.get_event_loop()
    if await loop.run_in_executor(_pool, sessions.session_exists, host, name):
        owner = await loop.run_in_executor(_pool, sessions.get_owner, host, name)
        return _owns(sess, owner)
    rec = await loop.run_in_executor(_pool, store.get, host, name)
    if rec is not None:
        return _owns(sess, rec.get("owner", "") or "")
    return True


async def _target(request: Request) -> tuple[dict, str, str] | JSONResponse:
    """The body → host allowlist → owner gate shared by every mutating session route.

    Returns ``(body, host, name)`` on success — the body stays the caller's, so it
    can read its own extra fields off it — or the error response to return:

    - 400 ``invalid target``: a body that isn't a JSON object, a name that isn't a
      non-empty string, or a host off the ssh-config allowlist. The host check is a
      hard guard for invariant #3: an unchecked alias reaches ssh as a positional
      argument, where `-oProxyCommand=...` would be parsed as an option.
    - 403 ``not your session``: the named session (live *or* saved profile)
      belongs to someone else (item 8 — the list is filtered, but a name is
      guessable, so every mutation checks too).

    Not a FastAPI dependency: these handlers read the body themselves for their
    other fields, and a dependency would have to read it first anyway.
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "invalid target"}, status_code=400)
    host = body.get("host", "local")
    name = body.get("name")
    if host not in _known_hosts() or not sessions.valid_name(name):
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
    return body, host, name
