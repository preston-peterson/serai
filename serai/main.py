"""Serai -- a single attach point for your terminal, SSH, and coding-agent
(Claude / Grok / OpenCode) sessions across local and remote hosts.

Design notes:
  * Holds no credentials. Remote work goes through your ssh-agent and
    ~/.ssh/config; tmux gives every session persistence and resumability.
  * Binds to 127.0.0.1 by default -- put whatever access layer you like in
    front of it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, bridge, config, files, netcfg, sessions, settings, store, tmuxctl, updates
from . import __version__

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
_pool = ThreadPoolExecutor(max_workers=8)

# The version as installed ON DISK, cached a few seconds. A frontend-only
# deploy copies new files without restarting the service (see install.sh);
# reading the version from disk lets the still-running process advertise the
# new one, so open tabs show it and get their "updated — reload" prompt.
_VERSION_FILE = Path(__file__).resolve().parent / "__init__.py"
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


@asynccontextmanager
async def _lifespan(app: FastAPI):
    auth.startup_banner()  # prints the one-time setup code while no users exist
    # No zombie reaper any more: v2 pty.fork()ed a tmux client per attach and
    # had to waitpid it itself (the 2.30.x zombie saga). v3's control clients
    # and creates are asyncio / subprocess.run children, which reap their own.
    yield


app = FastAPI(title="serai", lifespan=_lifespan)

# Paths reachable without a session: the SPA shell + its static assets (no
# secrets), plus the endpoints the login/setup screens themselves call.
_PUBLIC_PATHS = {"/", "/favicon.ico", "/api/auth/status",
                 "/api/login", "/api/logout", "/api/setup"}


def _is_public(path: str) -> bool:
    return path in _PUBLIC_PATHS or path.startswith("/static/")


@app.middleware("http")
async def _require_auth(request: Request, call_next):
    """Gate every HTTP route behind a valid session cookie. serai is a shell
    into the whole fleet, so the data/command APIs must not be reachable
    unauthenticated. The SPA shell and the auth endpoints stay public so the
    login screen can load and submit; the websocket is gated inside ws_attach
    (http middleware does not see websocket scopes)."""
    if not auth.auth_enabled() or _is_public(request.url.path):
        return await call_next(request)
    sess = auth.verify_token(request.cookies.get(auth.COOKIE))
    if sess:
        # Every authenticated request is the only evidence serai has that someone
        # is actually there -- the tokens carry no server-side state. The UI polls
        # every few seconds, so this makes "has serai open" observable.
        auth.touch(sess["username"])
        return await call_next(request)
    return JSONResponse({"error": "auth required"}, status_code=401)


@app.middleware("http")
async def _no_cache(request, call_next):
    """Tell the browser to revalidate every response. This is a localhost,
    frequently-updated tool, so a plain reload should always pick up new JS/CSS
    -- no hard-refresh needed. Static files still send ETag/Last-Modified, so
    unchanged assets revalidate to a cheap 304."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    # Stamp the running version on gated responses (not the public login surface):
    # the UI reads it off its normal /api/sessions poll -- no extra request -- to
    # show it in the status bar and to prompt a reload when the server updates
    # underneath a long-lived tab. Skip 401s: those are the auth middleware
    # rejecting an anonymous caller, and an unauthenticated prober shouldn't
    # learn the version from the rejection itself.
    if not _is_public(request.url.path) and response.status_code != 401:
        response.headers["X-Serai-Version"] = _current_version()
    return response


def _known_hosts() -> set[str]:
    """Valid attach/broadcast targets: 'local' plus every ssh-config alias.

    Validating a client-supplied host against this set is a hard guard for
    invariant #3: an unchecked alias is handed to ssh as a positional arg, so a
    value like `-oProxyCommand=...` would be parsed as an option and run a local
    command. Restricting to configured hosts (invariant #6) closes that off.
    """
    return {"local", *(h.alias for h in config.parse_ssh_config())}


# --- static UI -------------------------------------------------------------

# GET *and* HEAD: FastAPI's @app.get registers GET alone, unlike a plain
# Starlette route, so HEAD / answered 405 while GET / answered 200. Uptime
# monitors habitually probe with HEAD and would read that as an outage.
@app.api_route("/", methods=["GET", "HEAD"])
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> FileResponse:
    return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


# --- auth ------------------------------------------------------------------

def _cookie_secure(request: Request) -> bool:
    """Send the Secure flag only over https (direct or via a terminating
    proxy), so the cookie still works on a plain-http localhost."""
    return (request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").lower() == "https")


def _set_session_cookie(response: Response, request: Request, username: str) -> None:
    response.set_cookie(
        auth.COOKIE, auth.issue_token(username),
        max_age=auth.SESSION_TTL, httponly=True, samesite="lax",
        secure=_cookie_secure(request), path="/",
    )


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
    if host not in _known_hosts() or not isinstance(name, str) or not name:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
    return body, host, name


@app.get("/api/auth/status")
async def api_auth_status(request: Request) -> JSONResponse:
    """Whether auth is on, configured, and the caller's session -- the SPA polls
    this on boot to choose setup vs login vs app."""
    if not auth.auth_enabled():
        return JSONResponse({"enabled": False, "configured": True,
                             "authenticated": True, "user": None, "admin": True})
    sess = auth.verify_token(request.cookies.get(auth.COOKIE))
    return JSONResponse({
        "enabled": True,
        "configured": auth.is_configured(),
        "authenticated": bool(sess),
        "user": sess["username"] if sess else None,
        "admin": bool(sess and sess["admin"]),
        "setup_code_required": auth.setup_requires_code(),
    })


@app.post("/api/login")
async def api_login(request: Request) -> JSONResponse:
    if not auth.auth_enabled():
        return JSONResponse({"ok": True})
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    loop = asyncio.get_event_loop()
    sess = await loop.run_in_executor(_pool, auth.verify_credentials, username, password)
    if not sess:
        return JSONResponse({"error": "invalid credentials"}, status_code=401)
    resp = JSONResponse({"ok": True, "user": sess["username"], "admin": sess["admin"]})
    _set_session_cookie(resp, request, sess["username"])
    return resp


@app.post("/api/logout")
async def api_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@app.post("/api/setup")
async def api_setup(request: Request) -> JSONResponse:
    """Create the first (admin) user. Only valid while no users exist and only
    with the one-time code serai printed to its console -- this closes the
    'first visitor becomes admin' hole on an exposed port."""
    if not auth.auth_enabled():
        return JSONResponse({"error": "auth is disabled"}, status_code=400)
    if auth.is_configured():
        return JSONResponse({"error": "already configured"}, status_code=403)
    body = await request.json()
    # Trust-on-first-use by default; require the printed code only in code mode.
    if auth.setup_requires_code() and not auth.check_setup_token(body.get("token") or ""):
        return JSONResponse({"error": "bad setup code"}, status_code=403)
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, auth.add_user, username, password, True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    resp = JSONResponse({"ok": True, "user": username, "admin": True})
    _set_session_cookie(resp, request, username)
    return resp


@app.get("/api/users")
async def api_users(request: Request) -> JSONResponse:
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    loop = asyncio.get_event_loop()
    users = await loop.run_in_executor(_pool, auth.list_users)
    return JSONResponse({"users": users, "me": sess["username"]})


@app.post("/api/users")
async def api_users_add(request: Request) -> JSONResponse:
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    body = await request.json()
    username = (body.get("username") or "").strip()
    password = body.get("password") or ""
    admin = bool(body.get("admin"))
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, auth.add_user, username, password, admin)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@app.delete("/api/users/{username}")
async def api_users_remove(username: str, request: Request) -> JSONResponse:
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    if username == sess["username"]:
        return JSONResponse({"error": "cannot remove yourself"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, auth.remove_user, username)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


@app.get("/api/network")
async def api_network(request: Request) -> JSONResponse:
    """Current listen address + cert hostnames (admin only)."""
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, netcfg.read))


@app.post("/api/network")
async def api_network_save(request: Request) -> JSONResponse:
    """Set the listen address + cert hostnames, then restart to apply (admin).
    serai reissues its self-signed cert on restart to cover the new names."""
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    body = await request.json()
    host = (body.get("host") or "").strip()
    hostnames = body.get("hostnames") or ""
    loop = asyncio.get_event_loop()
    try:
        names = await loop.run_in_executor(_pool, netcfg.write, host, hostnames)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    restart = await loop.run_in_executor(_pool, netcfg.restart)
    return JSONResponse({"ok": True, "host": host, "hostnames": names, "restart": restart})


@app.post("/api/users/{username}/password")
async def api_users_password(username: str, request: Request) -> JSONResponse:
    """Reset a password. Admins may reset anyone; a user may change their own."""
    sess = _session(request)
    if not sess:
        return JSONResponse({"error": "auth required"}, status_code=401)
    if not (sess["admin"] or username == sess["username"]):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    body = await request.json()
    password = body.get("password") or ""
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, auth.set_password, username, password)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})


# --- read APIs -------------------------------------------------------------

@app.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    """The UI's persisted preference blob (durable across browsers/devices)."""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, settings.load))


@app.put("/api/settings")
async def api_put_settings(request: Request) -> JSONResponse:
    """Merge into the persisted preference blob. UI prefs only -- no credentials.

    Merge, not replace: each tab sends the keys it knows about, and a tab opened
    before a preference existed would otherwise drop that key for every other
    tab the next time it saved anything.
    """
    data = await request.json()
    if not isinstance(data, dict):
        return JSONResponse({"error": "object required"}, status_code=400)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_pool, settings.merge, data)
    return JSONResponse({"ok": True})


@app.get("/api/updates")
async def api_updates() -> JSONResponse:
    """Whether a newer serai has been released. Polls upstream only when the
    operator's chosen interval says a check is due -- see serai.updates."""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, updates.status))


@app.post("/api/updates/check")
async def api_updates_check() -> JSONResponse:
    """The settings panel's "check now" -- poll regardless of the interval."""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, updates.status, True))


@app.post("/api/updates/apply")
async def api_updates_apply(request: Request) -> JSONResponse:
    """Download the latest release, verify its sha256, install it, and restart.

    Admin only -- this is the one endpoint that runs the installer over the
    running code (invariant #3: the command is argv-built from our own paths and
    a GitHub-supplied tag validated against a version regex; nothing from the
    request reaches it). Runs off the event loop; on success the service
    restarts and the UI's version-change detector prompts a reload."""
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_pool, updates.apply)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@app.get("/api/hosts")
async def api_hosts() -> JSONResponse:
    """List ssh-config hosts, each tagged with current `reachable` state.

    Reachability is a quick concurrent TCP probe of each host's ssh port so the
    UI can dim hosts that are offline. The host list itself still comes only
    from the ssh config (invariant #6) -- the probe just annotates it.
    """
    loop = asyncio.get_event_loop()
    hosts = await loop.run_in_executor(_pool, config.parse_ssh_config)
    reachable = await asyncio.gather(
        *[loop.run_in_executor(_pool, sessions.tcp_reachable, h.hostname or h.alias, h.port)
          for h in hosts]
    )
    out = []
    for h, ok in zip(hosts, reachable):
        d = h.as_dict()
        d["reachable"] = ok
        out.append(d)
    return JSONResponse(out)


@app.post("/api/hosts")
async def api_hosts_add(request: Request) -> JSONResponse:
    """Add a host to ~/.ssh/config (admin only). Stores connection metadata only
    -- no credentials; ssh config stays the single source of truth (invariants
    #1/#6). Fields are validated to a tight charset to prevent config injection."""
    sess = _session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    body = await request.json()
    loop = asyncio.get_event_loop()
    try:
        host = await loop.run_in_executor(_pool, lambda: config.add_host(
            alias=body.get("alias") or "",
            hostname=body.get("hostname") or "",
            user=body.get("user") or "",
            port=body.get("port", 22),
            group=body.get("group") or "",
            tags=body.get("tags") or [],
        ))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "host": host.as_dict()})


@app.get("/api/kinds")
async def api_kinds() -> JSONResponse:
    """The session-kinds table (shell + every coding agent), straight from
    `sessions.kinds()` -- cmd, prefix, chip, glyph and resume flags in one place,
    so the UI doesn't keep a second copy of its own. Read-only and tiny; the
    ordering it returns is the ordering the UI should render."""
    return JSONResponse(sessions.kinds())


@app.get("/api/sessions")
async def api_sessions(request: Request) -> JSONResponse:
    """Enumerate live sessions on local + every configured host, concurrently."""
    sess = _session(request)
    loop = asyncio.get_event_loop()
    hosts = await loop.run_in_executor(_pool, config.parse_ssh_config)
    targets = ["local"] + [h.alias for h in hosts]
    results = await asyncio.gather(
        *[loop.run_in_executor(_pool, sessions.list_sessions, t) for t in targets]
    )
    flat = [s.as_dict() for group in results for s in group]

    # Self-heal tags. A session recreated by a reconnect (an attached session
    # after a restart) comes back without its @serai_tags. If the restore
    # snapshot still has tags for it -- which store.upsert now keeps sticky
    # against the transient empty -- re-apply them to the live session and show
    # them straight away, so a code update never silently drops a tag. A
    # deliberate clear leaves empty stored tags (store.set_tags), so it is not
    # resurrected here.
    saved = await loop.run_in_executor(_pool, store.saved)
    by_key = {f"{r.get('host')}::{r.get('name')}": r for r in saved}
    for rec in flat:
        prior = by_key.get(f"{rec['host']}::{rec['name']}")
        if not rec.get("tags") and prior and prior.get("tags"):
            rec["tags"] = list(prior["tags"])                      # show it now
            loop.run_in_executor(                                   # and heal tmux
                _pool, sessions.run_send,
                sessions.set_tags_argv(rec["host"], rec["name"], ",".join(prior["tags"])))
        # The same self-heal for the owner, and it matters more: a session that
        # comes back without @serai_owner reads as unowned, which is admin-only,
        # so it would disappear from the board of the user it belongs to.
        if not rec.get("owner") and prior and prior.get("owner"):
            rec["owner"] = prior["owner"]
            loop.run_in_executor(
                _pool, sessions.run_send,
                sessions.set_owner_argv(rec["host"], rec["name"], prior["owner"]))
        # Favorites are store-side (set_favorite, never a live value), so the
        # board/rail renders them from the snapshot: a clean true/false, never
        # null, so the star is always a definite filled/outline state.
        rec["favorite"] = bool((prior or {}).get("favorite"))

    # The snapshot is serai's own bookkeeping about the whole fleet, so it sees
    # everything regardless of who is asking. Only the *response* is scoped to
    # the caller.
    await loop.run_in_executor(_pool, store.upsert, flat)  # saved profiles; /exit keeps them
    return JSONResponse([s for s in flat if _owns(sess, s.get("owner", ""))])


@app.get("/api/sessions/saved")
async def api_sessions_saved(request: Request) -> JSONResponse:
    """Saved session profiles (known hosts only, and only the caller's own --
    see _owns). Not-live entries are what + New and the palette offer to resume."""
    sess = _session(request)
    loop = asyncio.get_event_loop()
    saved = await loop.run_in_executor(_pool, store.saved)
    known = _known_hosts()
    return JSONResponse([r for r in saved
                         if r.get("host") in known and _owns(sess, r.get("owner", ""))])


@app.get("/api/files")
async def api_files(host: str = "local", path: str = "~") -> JSONResponse:
    # Same allowlist as every other host-taking route: files._ssh_for connects to
    # whatever string it is handed, so an unchecked alias is hostile input (#1/#3).
    if host not in _known_hosts():
        return JSONResponse({"error": "invalid target"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        entries = await loop.run_in_executor(_pool, files.list_dir, host, path)
    except Exception as exc:  # surface a usable message rather than a 500 blob
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"host": host, "path": path, "entries": [e.as_dict() for e in entries]})


_FILE_OPS = {"rename", "delete", "copy", "mkdir", "transfer"}


@app.post("/api/files/op")
async def api_files_op(request: Request) -> JSONResponse:
    """File operations for the file pane: rename/move, delete, copy, mkdir.

    Body: {op, host, path, dest?} -- dest is the full destination path for
    rename (which is also move) and copy. The host is validated against the
    ssh-config allowlist; local ops use os/shutil directly (no shell), remote
    ops use SFTP primitives or a shlex-quoted argv over the cached SSH
    connection (invariant #3).
    """
    body = await request.json()
    op = body.get("op")
    host = body.get("host", "local")
    path = body.get("path") or ""
    dest = body.get("dest")
    if op not in _FILE_OPS or host not in _known_hosts() or not isinstance(path, str) or not path:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if op in ("rename", "copy") and (not isinstance(dest, str) or not dest):
        return JSONResponse({"error": "dest required"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        if op == "transfer":
            # cross-host paste: {op, host: dst, path: dst path, src_host, src_path}
            src_host = body.get("src_host", "")
            src_path = body.get("src_path") or ""
            if src_host not in _known_hosts() or not isinstance(src_path, str) or not src_path:
                return JSONResponse({"error": "invalid source"}, status_code=400)
            await loop.run_in_executor(_pool, files.transfer_path, src_host, src_path, host, path)
        else:
            await loop.run_in_executor(_pool, files.file_op, op, host, path, dest)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True})


@app.get("/api/file")
async def api_file(host: str = "local", path: str = "") -> Response:
    if host not in _known_hosts():
        return JSONResponse({"error": "invalid target"}, status_code=400)
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_pool, files.read_file, host, path)
    name = os.path.basename(path) or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@app.post("/api/broadcast")
async def api_broadcast(request: Request) -> JSONResponse:
    """Send one command line to many sessions at once (fleet broadcast).

    Body: {"text": "uptime", "targets": [{"host": "...", "name": "..."}, ...]}.
    Each target is typed into independently via `tmux send-keys` (argv-safe; see
    sessions.send_keys_argv), concurrently, with the host validated against the
    ssh-config allowlist. Returns a per-target ok/error so the UI can report it.
    """
    body = await request.json()
    text = body.get("text")
    targets = body.get("targets")
    if not isinstance(text, str) or not text:
        return JSONResponse({"error": "text required"}, status_code=400)
    if not isinstance(targets, list) or not targets:
        return JSONResponse({"error": "no targets"}, status_code=400)

    known = _known_hosts()
    loop = asyncio.get_event_loop()

    sess = _session(request)

    async def deliver(t: dict) -> dict:
        host = t.get("host", "local")
        name = t.get("name") or ""
        if host not in known or not name:
            return {"host": host, "name": name, "ok": False, "error": "invalid target"}
        # A broadcast types into a live session, so it needs the same ownership
        # check as attaching -- otherwise it would be the way around the others.
        if not await _may_touch(sess, host, name):
            return {"host": host, "name": name, "ok": False, "error": "not your session"}
        argv = sessions.send_keys_argv(host, name, text)
        ok = await loop.run_in_executor(_pool, sessions.run_send, argv)
        return {"host": host, "name": name, "ok": bool(ok)}

    results = await asyncio.gather(*[deliver(t) for t in targets if isinstance(t, dict)])
    return JSONResponse({"sent": sum(r["ok"] for r in results), "results": results})


@app.post("/api/rename")
async def api_rename(request: Request) -> JSONResponse:
    """Rename a session. Body: {host, name, kind, label}. The new tmux name is
    built from kind+label so the cc-/shell- prefix is preserved (invariant #5)."""
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    kind = body.get("kind", "shell")
    label = body.get("label") or ""
    new_name = sessions.session_name(kind, label)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.rename_argv(host, name, new_name))
    sessions.clear_cache(host)  # reflect the change on the next poll without waiting out the TTL
    return JSONResponse({"ok": bool(ok), "name": new_name})


@app.post("/api/kill")
async def api_kill(request: Request) -> JSONResponse:
    """Kill a session. Body: {host, name}.

    Kills the *instance*. The profile is forgotten too (``store.remove`` -- item
    10's no-resurrection rule) **unless the profile is a favorite**: the star
    marks the launcher, not the running session, so closing an instance must
    not erase what you chose to keep (item 23). Forgetting a favorite stays
    deliberate -- unfavorite first (or Forget in the Favorites menu), and the
    next kill drops the profile as usual. ``kept`` tells the client which
    happened so it can say so honestly.
    """
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    _, host, name = t
    loop = asyncio.get_event_loop()
    rec = await loop.run_in_executor(_pool, store.get, host, name)
    kept = bool(rec and rec.get("favorite"))
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.kill_argv(host, name))
    sessions.clear_cache(host)
    if not kept:
        await loop.run_in_executor(_pool, store.remove, host, name)  # forget the profile
    return JSONResponse({"ok": bool(ok), "kept": kept})


@app.post("/api/sessions/restart")
async def api_restart(request: Request) -> JSONResponse:
    """Kill a session but *keep* its restore record. Body: {host, name}.

    tmux `new -A` only runs its command on create, so a change to a session's
    extra args (or its start dir) can't reach one that is already running -- the
    session has to be recreated. That is what this is for, and it is why it is
    not `/api/kill`: that one calls store.remove, which would throw away the very
    record the client needs to recreate the session from. The client attaches
    straight afterwards, which is what brings the session back.
    """
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    _, host, name = t
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.kill_argv(host, name))
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok)})


@app.post("/api/args")
async def api_args(request: Request) -> JSONResponse:
    """Set a session's extra agent arguments (the @serai_args tmux option).
    Body: {host, name, args: "--chrome"}. Sanitised server-side by clean_args,
    which shlex-quotes every token -- see invariant #3.

    Takes effect on the session's *next* create, exactly like a start dir, so the
    caller normally follows with /api/sessions/restart.
    """
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    try:
        clean = sessions.clean_args(body.get("args") or "")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send,
                                    sessions.set_args_argv(host, name, clean))
    # Record the deliberate change (including a clear) so upsert's sticky rule
    # doesn't resurrect the old value from the snapshot.
    await loop.run_in_executor(_pool, store.set_args, host, name, clean)
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok), "args": clean})


@app.post("/api/tags")
async def api_tags(request: Request) -> JSONResponse:
    """Set a session's tags (stored as the @serai_tags tmux user option).
    Body: {host, name, tags: [...]}. Tags are sanitized server-side."""
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    tags = body.get("tags")
    if not isinstance(tags, list):
        return JSONResponse({"error": "invalid target"}, status_code=400)
    clean = sessions.clean_tags(tags)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send,
                                    sessions.set_tags_argv(host, name, ",".join(clean)))
    # Record the deliberate change (including a clear) straight into the restore
    # snapshot, so upsert's sticky-tags rule doesn't later resurrect old tags.
    await loop.run_in_executor(_pool, store.set_tags, host, name, clean)
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok), "tags": clean})


@app.post("/api/favorite")
async def api_favorite(request: Request) -> JSONResponse:
    """Set a saved profile's favorite flag (the header favorites list).
    Body: {host, name, favorite: true|false}. Store-side only -- there is no
    tmux option to set. Owner-gated like every other per-session mutation, so a
    guessed name can't favorite or unfavorite someone else's session. A no-op on
    a name with no profile yet (the create path re-applies once it exists)."""
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    favorite = bool(body.get("favorite"))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_pool, store.set_favorite, host, name, favorite)
    return JSONResponse({"ok": True, "favorite": favorite})


@app.post("/api/files/transfer")
async def api_files_transfer(request: Request):
    """Cross-host copy that reports progress as it runs (NDJSON, one line each):

        {"moved": 524288} ... {"ok": true, "moved": 33600000}   -- or {"error": ...}

    A multi-GB relay is otherwise completely silent, since the work happens in a
    worker thread and the POST only answers once it's done. The relay counts bytes
    already; this just carries them out. Kept separate from /api/files/op so that
    endpoint's plain ok/error contract is untouched -- a cross-host *cut* deletes
    the source only after a success, so that must not become ambiguous.
    """
    body = await request.json()
    host = body.get("host", "local")
    path = body.get("path") or ""
    src_host = body.get("src_host", "")
    src_path = body.get("src_path") or ""
    if (host not in _known_hosts() or src_host not in _known_hosts()
            or not isinstance(path, str) or not path
            or not isinstance(src_path, str) or not src_path):
        return JSONResponse({"error": "invalid request"}, status_code=400)

    loop = asyncio.get_event_loop()
    q: asyncio.Queue = asyncio.Queue()

    # on_progress runs on the worker thread; hop back to the loop to enqueue.
    def on_progress(moved: int) -> None:
        loop.call_soon_threadsafe(q.put_nowait, moved)

    def run() -> None:
        try:
            moved = files.transfer_path(src_host, src_path, host, path, on_progress)
            loop.call_soon_threadsafe(q.put_nowait, {"ok": True, "moved": moved})
        except Exception as exc:  # surfaced as the stream's final line
            loop.call_soon_threadsafe(q.put_nowait, {"error": str(exc)})

    async def stream():
        fut = loop.run_in_executor(_pool, run)
        try:
            while True:
                item = await q.get()
                if isinstance(item, dict):        # terminal line: ok or error
                    yield json.dumps(item) + "\n"
                    return
                yield json.dumps({"moved": item}) + "\n"
        finally:
            await fut  # never leave the relay running behind a dropped response

    return StreamingResponse(stream(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache"})


@app.post("/api/dir")
async def api_dir(request: Request) -> JSONResponse:
    """Set a session's "start in" directory (the @serai_dir tmux user option).

    Body: {host, name, path}; an empty path clears it. This is where the file
    pane opens for the session and where a post-reboot restore starts it -- it
    does not move a shell that is already running.
    """
    t = await _target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    try:
        path = sessions.clean_dir(body.get("path") or "")
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send,
                                    sessions.set_dir_argv(host, name, path))
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok), "path": path})


@app.post("/api/upload")
async def api_upload(host: str = "local", path: str = "", file: UploadFile = File(...)) -> JSONResponse:
    if host not in _known_hosts():
        return JSONResponse({"error": "invalid target"}, status_code=400)
    data = await file.read()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, files.write_file, host, path, data)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "path": path, "bytes": len(data)})


# --- terminal attach over websocket ---------------------------------------
#
# v3: a tmux control-mode client per attached browser terminal, pumped by
# serai/bridge.py (see its docstring for the wire protocol). The session is
# created first, detached, when it doesn't exist; then the control client
# attaches to it. tmux stays the persistence substrate (invariant #4): closing
# the socket only ends the control client.

# Keepalive so an idle attach (a TUI waiting for input: no output, no
# keystrokes) is not dropped by a NAT / browser / idle timeout. Localhost never
# hits those; a second machine does. ASGI has no protocol ping, so this is a
# JSON control frame. 20s sits under the common 30s/60s idle cuts. 0 disables.
WS_PING_INTERVAL = 20.0

# Scrollback seeded into the browser when the client doesn't say.
DEFAULT_HISTORY = 10000


async def _ws_keepalive(ping) -> None:
    """Call ``ping()`` every WS_PING_INTERVAL seconds, forever."""
    while True:
        await asyncio.sleep(WS_PING_INTERVAL)
        ping()


async def _pump_browser(ws: WebSocket, br: "bridge.Bridge") -> None:
    """Browser -> tmux until the socket closes. Binary is input for the pane;
    text is a JSON control frame ({"resize":{...}} or a {"ping":1})."""
    while True:
        msg = await ws.receive()
        if msg["type"] == "websocket.disconnect":
            return
        if msg.get("bytes") is not None:
            await br.input(msg["bytes"])
            continue
        if msg.get("text") is None:
            continue
        try:
            payload = json.loads(msg["text"])
        except ValueError:
            continue
        resize = payload.get("resize") if isinstance(payload, dict) else None
        if isinstance(resize, dict):
            await br.resize(resize.get("cols"), resize.get("rows"))


async def _close_ws(ws: WebSocket, code: int) -> None:
    try:
        await ws.close(code=code)
    except RuntimeError:
        pass  # already closed by the other side


@app.websocket("/ws/attach")
async def ws_attach(ws: WebSocket) -> None:
    await ws.accept()

    # The http auth middleware does not see websocket scopes, so gate here:
    # without a valid session, refuse before anything reaches tmux.
    ws_sess = ({"username": None, "admin": True} if not auth.auth_enabled()
               else auth.verify_token(ws.cookies.get(auth.COOKIE)))
    if ws_sess is None:
        await ws.send_text("\r\n\x1b[31m[serai] not logged in -- refusing to attach\x1b[0m\r\n")
        await ws.close(code=4401)
        return

    q = ws.query_params
    host = q.get("host", "local")
    kind = q.get("kind", "shell")
    label = q.get("label", "main")
    path = q.get("path") or None
    resume = q.get("resume", "")
    try:
        history = q.get("history")
        history = max(0, min(int(history), 1_000_000)) if history else None
    except ValueError:
        history = None  # tmux history-limit (lines of scrollback); None -> leave default
    cols = bridge.clamp_dim(q.get("cols"), 80)
    rows = bridge.clamp_dim(q.get("rows"), 24)
    name = q.get("name") or sessions.session_name(kind, label)

    # Refuse hosts that aren't in the ssh config (invariant #3): an arbitrary
    # alias would otherwise reach ssh as a positional arg-injection vector. Close
    # with a distinct code so the client shows a clean error instead of looping.
    if host not in _known_hosts():
        await ws.send_text("\r\n\x1b[31m[serai] unknown host -- refusing to attach\x1b[0m\r\n")
        await ws.close(code=4404)
        return

    # Refuse someone else's live session, and claim a new one for whoever made it.
    # Filtering the *list* hides sessions, but only this stops them being opened.
    # (Still an organisational boundary, not a security one -- see _owns.)
    loop = asyncio.get_running_loop()
    exists = await loop.run_in_executor(_pool, sessions.session_state, host, name)
    if exists is None:
        # Couldn't ask (ssh down). Not "absent": that would take the create path
        # and skip the owner check. Retryable, so the client backs off and tries again.
        await ws.send_text(json.dumps({"error": f"{host} is not answering"}))
        await _close_ws(ws, 1011)
        return
    claim = ""
    claim_tags = ""
    if exists:
        if not _owns(ws_sess, await loop.run_in_executor(_pool, sessions.get_owner, host, name)):
            await ws.send_text("\r\n\x1b[31m[serai] that session belongs to another "
                               "user -- refusing to attach\x1b[0m\r\n")
            await ws.close(code=4403)
            return
    else:
        # Only a session this attach *creates* is claimed, and the claim rides in
        # the same tmux command as the create so it can't race it. An admin
        # opening an unowned session must not silently take it over.
        claim = sessions.clean_owner(ws_sess.get("username") or "")
        # Tags from the + New form ride the create the same way; a re-attach to
        # an existing name passes none, so its tags are never clobbered.
        claim_tags = ",".join(sessions.clean_tags((q.get("tags") or "").split(",")))

    # Remember an explicit start dir and extra agent args on the session itself,
    # so the file pane and a later recreate keep using them after the shell cds
    # elsewhere. clean_args quotes every token (invariant #3); an unparseable
    # value is dropped rather than guessed at -- an attach must not fail on it.
    try:
        start_dir = sessions.clean_dir(path) if path else ""
    except ValueError:
        start_dir = ""
    try:
        extra = sessions.clean_args(q.get("args") or "")
    except ValueError:
        extra = ""
    if exists:
        # Best effort, off the attach path. (A new session gets both inside its
        # create command instead: a separate set-option would race the create.)
        if start_dir:
            loop.run_in_executor(_pool, sessions.run_send,
                                 sessions.set_dir_argv(host, name, start_dir))
        if extra:
            loop.run_in_executor(_pool, sessions.run_send,
                                 sessions.set_args_argv(host, name, extra))
    else:
        argv = sessions.create_argv(host, name, kind, path, resume, history, extra,
                                    claim, claim_tags, cols, rows, start_dir)
        # A False here is usually a racing create of the same name (another tab):
        # the attach below finds it. A real failure shows up as the attach failing.
        await loop.run_in_executor(_pool, sessions.create_session, argv)

    client = tmuxctl.ControlClient(tmuxctl.control_argv(host, name), env=sessions.client_env())
    br = bridge.Bridge(client, ws.send_bytes, ws.send_text, cols=cols, rows=rows,
                       history=DEFAULT_HISTORY if history is None else history)
    try:
        await client.start()
        await br.start()
    except (tmuxctl.ControlClosed, tmuxctl.ControlError, ValueError, OSError) as exc:
        br.close()
        await client.close()
        # Unknown (ssh down) counts as alive: retry, never a final 4410.
        alive = await loop.run_in_executor(_pool, sessions.session_state, host, name) is not False
        try:
            await ws.send_text(json.dumps({"error": f"could not attach to {name}: {exc}"}))
        except RuntimeError:
            pass
        # Gone -> 4410 so the client does not recreate it in a loop; alive but
        # unattachable (ssh trouble) -> 1011, which the client retries with backoff.
        await _close_ws(ws, 1011 if alive else 4410)
        return

    pump = asyncio.create_task(_pump_browser(ws, br))
    tasks = [asyncio.create_task(br.writer()), asyncio.create_task(br.maintain()), pump]
    if WS_PING_INTERVAL > 0:
        tasks.append(asyncio.create_task(_ws_keepalive(br.ping)))
    tmux_gone = client.closed   # the client's own future; close() resolves it
    try:
        done, _ = await asyncio.wait([tmux_gone, *tasks], return_when=asyncio.FIRST_COMPLETED)
        tmux_ended = tmux_gone.done()      # before close() below resolves it anyway
        for t in done:
            if t is not tmux_gone and not t.cancelled() and t.exception() is not None:
                exc = t.exception()
                if not isinstance(exc, (WebSocketDisconnect, tmuxctl.ControlClosed, RuntimeError)):
                    print(f"[serai] attach {name}: {type(exc).__name__}: {exc}")
    finally:
        for t in tasks:
            t.cancel()
        br.close()
        await client.close()
    if tmux_ended:
        # The control client ended on tmux's side. Session gone (the program
        # exited, it was killed, or tmux moved us off it) -> 4410 so the client
        # does NOT auto-reattach, which would recreate it. Still alive, or we
        # can't tell (ssh dropped) -> a normal close, and the client reattaches.
        alive = (br.ended is None and
                 await loop.run_in_executor(_pool, sessions.session_state, host, name) is not False)
        await _close_ws(ws, 1000 if alive else 4410)
    elif pump not in done:
        # Our side broke (a bridge task failed) while the browser is still
        # there: say so with a retryable code rather than dropping the socket.
        await _close_ws(ws, 1011)
