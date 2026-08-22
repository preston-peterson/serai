"""Serai -- a single attach point for your terminal, SSH, and Claude Code
sessions across local and remote hosts.

Design notes:
  * Holds no credentials. Remote work goes through your ssh-agent and
    ~/.ssh/config; tmux gives every session persistence and resumability.
  * Binds to 127.0.0.1 by default -- put whatever access layer you like in
    front of it.
"""

from __future__ import annotations

import asyncio
import fcntl
import json
import os
import pty
import re
import signal
import struct
import termios
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, files, netcfg, sessions, settings, store, updates
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
    if auth.verify_token(request.cookies.get(auth.COOKIE)):
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

    # The snapshot and the live-tracking are serai's own bookkeeping about the
    # whole fleet, so they see everything regardless of who is asking. Only the
    # *response* is scoped to the caller.
    await loop.run_in_executor(_pool, store.upsert, flat)  # saved profiles; /exit keeps them
    store.mark_live([f"{s['host']}::{s['name']}" for s in flat])  # for resume-after-exit
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


@app.get("/api/sessions/exited")
async def api_sessions_exited(request: Request) -> JSONResponse:
    """Claude sessions seen live earlier this run but no longer live -- offered
    on the board as one-click `claude --resume`. Distinct from /saved (the full
    post-reboot snapshot): this is the narrow "you just /exited this, resume it?"
    case, recency-scoped so old exits don't pile up. Known hosts only, and only
    the caller's own."""
    sess = _session(request)
    loop = asyncio.get_event_loop()
    exited = await loop.run_in_executor(_pool, store.recently_exited)
    known = _known_hosts()
    return JSONResponse([r for r in exited
                         if r.get("host") in known and _owns(sess, r.get("owner", ""))])


@app.post("/api/sessions/restore")
async def api_sessions_restore(request: Request) -> JSONResponse:
    """Recreate saved sessions (detached) so they are back after a reboot.

    Body may carry ``{"targets": [{"host", "name", "resume"}, ...]}`` to resume a
    chosen subset; without it every saved session is restored. Targets *select*
    snapshot entries and may choose how a Claude session comes back (``continue``
    the last conversation, ``resume`` to open its picker, or ``""`` for a fresh
    one, defaulting to ``continue``). Everything else -- kind, path, tags -- comes
    from serai's own snapshot, never the request, the host is re-validated
    against the ssh config, and the resume choice is checked against a fixed set,
    so it all stays argv-safe (invariant #3). Sessions that are already running
    are skipped, and every session's tags are reapplied.
    """
    sess = _session(request)
    loop = asyncio.get_event_loop()
    saved = await loop.run_in_executor(_pool, store.saved)
    # Restore only what the caller owns. Done here, before any target selection,
    # so a client naming someone else's session can't reach it either.
    saved = [r for r in saved if _owns(sess, r.get("owner", ""))]
    try:
        body = await request.json()
    except Exception:
        body = {}
    targets = body.get("targets") if isinstance(body, dict) else None
    picks: dict[str, str] = {}  # host::name -> how that Claude session comes back
    arg_picks: dict[str, str] = {}   # host::name -> extra `claude` args, if edited
    if isinstance(targets, list):
        for t in targets:
            if isinstance(t, dict):
                key = f"{t.get('host')}::{t.get('name')}"
                picks[key] = str(t.get("resume", "continue"))
                # Only an explicitly-present key overrides the snapshot: a client
                # that doesn't send `args` must not be read as "clear them".
                if "args" in t:
                    arg_picks[key] = str(t.get("args") or "")
        saved = [r for r in saved if f"{r.get('host')}::{r.get('name')}" in picks]
    known = _known_hosts()

    async def recreate(r: dict) -> dict:
        host = r.get("host", "local")
        name = r.get("name") or ""
        if host not in known or not name:
            return {"host": host, "name": name, "ok": False, "error": "invalid target"}
        # Skip sessions that are already running (recreated by hand, or a remote
        # host that never rebooted). Without this, `tmux new -A -d` tries to
        # *attach* to the existing session and fails outside a terminal, so a
        # restore over a half-alive fleet reported spurious failures.
        if await loop.run_in_executor(_pool, sessions.session_exists, host, name):
            return {"host": host, "name": name, "ok": True, "skipped": True}
        # The one thing a target may choose (kind/path/tags still come only from
        # the snapshot): how a Claude session relaunches. Validated against a
        # fixed set, so an unrecognised value can't mean anything but "fresh".
        resume = picks.get(f"{host}::{name}", "continue")
        if resume not in sessions.RESUME_CHOICES:
            resume = "continue"
        # Extra `claude` args: the banner may edit them per session, otherwise
        # they come from the snapshot. Either way they are re-cleaned here, so
        # neither a client nor an old hand-edited record can smuggle anything
        # unquoted into the command (invariant #3).
        key = f"{host}::{name}"
        raw_args = arg_picks[key] if key in arg_picks else (r.get("args", "") or "")
        try:
            extra = sessions.clean_args(raw_args)
        except ValueError:
            extra = ""
        argv = sessions.restore_argv(host, name, r.get("kind", "shell"),
                                     r.get("path", "") or "", resume, extra)
        ok = await loop.run_in_executor(_pool, sessions.run_send, argv)
        tags = sessions.clean_tags(r.get("tags") or [])
        if ok and tags:
            await loop.run_in_executor(_pool, sessions.run_send,
                                       sessions.set_tags_argv(host, name, ",".join(tags)))
        # Re-apply the args to the recreated session so the next poll sees them
        # and the snapshot's sticky rule has something live to agree with.
        if ok and extra:
            await loop.run_in_executor(_pool, sessions.run_send,
                                       sessions.set_args_argv(host, name, extra))
        # An edit made in the banner is a deliberate change, including a clear --
        # record it so the sticky rule can't resurrect the old value.
        if ok and key in arg_picks:
            await loop.run_in_executor(_pool, store.set_args, host, name, extra)
        # Re-stamp the owner, or the restored session comes back unowned -- which
        # would make it admin-only and so vanish from the board of the very user
        # whose session it is.
        owner = sessions.clean_owner(r.get("owner", "") or "")
        if ok and owner:
            await loop.run_in_executor(_pool, sessions.run_send,
                                       sessions.set_owner_argv(host, name, owner))
        return {"host": host, "name": name, "ok": bool(ok)}

    results = await asyncio.gather(*[recreate(r) for r in saved])
    sessions.clear_cache()  # so the next poll shows the recreated sessions
    restored = sum(1 for r in results if r["ok"] and not r.get("skipped"))
    return JSONResponse({"restored": restored,
                         "skipped": sum(1 for r in results if r.get("skipped")),
                         "results": results})


@app.get("/api/files")
async def api_files(host: str = "local", path: str = "~") -> JSONResponse:
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
    body = await request.json()
    host = body.get("host", "local")
    name = body.get("name") or ""
    kind = body.get("kind", "shell")
    label = body.get("label") or ""
    if host not in _known_hosts() or not name:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
    new_name = sessions.session_name(kind, label)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.rename_argv(host, name, new_name))
    sessions.clear_cache(host)  # reflect the change on the next poll without waiting out the TTL
    return JSONResponse({"ok": bool(ok), "name": new_name})


@app.post("/api/kill")
async def api_kill(request: Request) -> JSONResponse:
    """Kill a session. Body: {host, name}."""
    body = await request.json()
    host = body.get("host", "local")
    name = body.get("name") or ""
    if host not in _known_hosts() or not name:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.kill_argv(host, name))
    sessions.clear_cache(host)
    await loop.run_in_executor(_pool, store.remove, host, name)  # forget the profile
    return JSONResponse({"ok": bool(ok)})


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
    body = await request.json()
    host = body.get("host", "local")
    name = body.get("name") or ""
    if host not in _known_hosts() or not name:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.kill_argv(host, name))
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok)})


@app.post("/api/args")
async def api_args(request: Request) -> JSONResponse:
    """Set a session's extra `claude` arguments (the @serai_args tmux option).
    Body: {host, name, args: "--chrome"}. Sanitised server-side by clean_args,
    which shlex-quotes every token -- see invariant #3.

    Takes effect on the session's *next* create, exactly like a start dir, so the
    caller normally follows with /api/sessions/restart.
    """
    body = await request.json()
    host = body.get("host", "local")
    name = body.get("name") or ""
    if host not in _known_hosts() or not name:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
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
    body = await request.json()
    host = body.get("host", "local")
    name = body.get("name") or ""
    tags = body.get("tags")
    if host not in _known_hosts() or not name or not isinstance(tags, list):
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
    clean = sessions.clean_tags(tags)
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send,
                                    sessions.set_tags_argv(host, name, ",".join(clean)))
    # Record the deliberate change (including a clear) straight into the restore
    # snapshot, so upsert's sticky-tags rule doesn't later resurrect old tags.
    await loop.run_in_executor(_pool, store.set_tags, host, name, clean)
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok), "tags": clean})


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
    body = await request.json()
    host = body.get("host", "local")
    name = body.get("name") or ""
    if host not in _known_hosts() or not name:
        return JSONResponse({"error": "invalid target"}, status_code=400)
    if not await _may_touch(_session(request), host, name):
        return JSONResponse({"error": "not your session"}, status_code=403)
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
    data = await file.read()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, files.write_file, host, path, data)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "path": path, "bytes": len(data)})


# --- terminal attach over websocket ---------------------------------------

# Keepalive so an idle attach (a TUI waiting for input: no PTY output, no
# keystrokes) is not dropped by a NAT / browser / idle timeout. Localhost
# never hits those; a second machine does. ASGI has no protocol ping, so this
# is a JSON control frame on the same channel as resize. 20s sits under the
# common 30s/60s idle cuts. 0 disables it (tests).
WS_PING_INTERVAL = 20.0


async def _ws_keepalive(ws: WebSocket) -> None:
    """Send a tiny ping every WS_PING_INTERVAL seconds until the socket dies."""
    try:
        while True:
            await asyncio.sleep(WS_PING_INTERVAL)
            await ws.send_text('{"ping":1}')
    except (WebSocketDisconnect, RuntimeError):
        return


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


@app.websocket("/ws/attach")
async def ws_attach(ws: WebSocket) -> None:
    await ws.accept()

    # The http auth middleware does not see websocket scopes, so gate here:
    # without a valid session, refuse before forking a PTY into the fleet.
    ws_sess = ({"username": None, "admin": True} if not auth.auth_enabled()
               else auth.verify_token(ws.cookies.get(auth.COOKIE)))
    if ws_sess is None:
        await ws.send_text("\r\n\x1b[31m[serai] not logged in -- refusing to attach\x1b[0m\r\n")
        await ws.close(code=4401)
        return

    host = ws.query_params.get("host", "local")
    kind = ws.query_params.get("kind", "shell")
    label = ws.query_params.get("label", "main")
    path = ws.query_params.get("path") or None
    resume = ws.query_params.get("resume", "")
    mouse = ws.query_params.get("mouse", "1") != "0"  # tmux mouse/scrollback (default on)
    try:
        history = ws.query_params.get("history")
        history = max(0, min(int(history), 1_000_000)) if history else None
    except ValueError:
        history = None  # tmux history-limit (lines of scrollback); None -> leave default
    name = ws.query_params.get("name") or sessions.session_name(kind, label)

    # Refuse hosts that aren't in the ssh config (invariant #3): an arbitrary
    # alias would otherwise reach ssh as a positional arg-injection vector. Close
    # with a distinct code so the client shows a clean error instead of looping.
    if host not in _known_hosts():
        await ws.send_text("\r\n\x1b[31m[serai] unknown host -- refusing to attach\x1b[0m\r\n")
        await ws.close(code=4404)
        return

    # Refuse someone else's live session, and claim a new one for whoever made it.
    # `tmux new -A` would otherwise happily attach to any name that was guessed:
    # filtering the *list* hides sessions, but only this stops them being opened.
    # (Still an organisational boundary, not a security one -- see _owns.)
    _loop = asyncio.get_event_loop()
    exists = await _loop.run_in_executor(_pool, sessions.session_exists, host, name)
    claim = ""
    if exists:
        if not _owns(ws_sess, await _loop.run_in_executor(_pool, sessions.get_owner, host, name)):
            await ws.send_text("\r\n\x1b[31m[serai] that session belongs to another "
                               "user -- refusing to attach\x1b[0m\r\n")
            await ws.close(code=4403)
            return
    else:
        # Only a session this attach *creates* is claimed, and the claim rides in
        # the same tmux command (after `new -A`) so it can't race the create. An
        # admin opening an unowned session must not silently take it over.
        claim = sessions.clean_owner(ws_sess.get("username") or "")

    # Remember an explicit start dir on the session itself, so the file pane and a
    # post-reboot restore keep using it even after the shell cds elsewhere. Best
    # effort: tmux `new -A` only applies it on create, and a failure here must
    # never stop the attach.
    if path:
        try:
            asyncio.get_event_loop().run_in_executor(
                _pool, sessions.run_send, sessions.set_dir_argv(host, name, sessions.clean_dir(path)))
        except ValueError:
            pass

    # Extra `claude` args, same deal: remembered on the session so a restore or a
    # later recreate keeps them. clean_args quotes every token, so nothing typed
    # here reaches the shell unquoted (invariant #3); an unparseable value is
    # dropped rather than guessed at, since an attach must not fail on it.
    try:
        extra = sessions.clean_args(ws.query_params.get("args") or "")
    except ValueError:
        extra = ""
    if extra:
        asyncio.get_event_loop().run_in_executor(
            _pool, sessions.run_send, sessions.set_args_argv(host, name, extra))

    argv = sessions.attach_argv(host, name, kind, path, resume, mouse, history, extra, claim)

    pid, master_fd = pty.fork()
    if pid == 0:  # child -> become the ssh/tmux process
        # The display is xterm.js, so advertise that terminal type. Without it,
        # tmux inherits whatever TERM serai was launched with -- and under
        # systemd there is none, which makes tmux abort with "open terminal
        # failed: terminal does not support clear". Forcing it here makes attach
        # work the same from a shell or a service (ssh -t forwards TERM, so this
        # fixes remote tmux too). COLORTERM enables 24-bit color in apps.
        os.environ["TERM"] = "xterm-256color"
        os.environ.setdefault("COLORTERM", "truecolor")
        # Don't hand serai's own working directory to tmux. A session created
        # with no start dir takes its cwd from the client -- us -- and would
        # otherwise open *inside serai's install tree*, which then got recorded
        # as that session's project dir. Home is the sane default; an explicit
        # start dir is applied by attach_argv itself.
        try:
            os.chdir(os.path.expanduser("~"))
        except OSError:
            pass
        os.execvp(argv[0], argv)
        os._exit(1)

    loop = asyncio.get_event_loop()
    # Non-blocking so a TUI that doesn't drain stdin (paste it doesn't
    # understand, a stuck CSI parse) can't freeze the event loop on os.write
    # — which is "paste, then typing dies".
    os.set_blocking(master_fd, False)
    write_buf = bytearray()
    watching_write = False

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, 65536)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(master_fd)
            asyncio.ensure_future(_close())
            return
        asyncio.ensure_future(ws.send_bytes(data))

    def _flush_write() -> None:
        nonlocal watching_write
        while write_buf:
            try:
                n = os.write(master_fd, write_buf)
            except BlockingIOError:
                break
            except OSError:
                write_buf.clear()
                break
            del write_buf[:n]
        if write_buf and not watching_write:
            loop.add_writer(master_fd, _flush_write)
            watching_write = True
        elif not write_buf and watching_write:
            loop.remove_writer(master_fd)
            watching_write = False

    async def _close() -> None:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        # The PTY (tmux/ssh client) exited. If the tmux session is gone -- the
        # program exited (e.g. claude /exit) or it was killed -- close with a
        # distinct code so the client does NOT auto-reattach (which would re-run
        # attach_argv and recreate the session). If the session is still alive
        # (a detach or network blip), close normally so the client reattaches.
        alive = await loop.run_in_executor(_pool, sessions.session_exists, host, name)
        try:
            await ws.close(code=1000 if alive else 4410)
        except RuntimeError:
            pass

    loop.add_reader(master_fd, _on_readable)

    ping_task = (asyncio.create_task(_ws_keepalive(ws))
                 if WS_PING_INTERVAL > 0 else None)
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            if msg.get("bytes") is not None:
                write_buf.extend(msg["bytes"])
                _flush_write()
            elif msg.get("text") is not None:
                # control frames: {"resize": {"rows": R, "cols": C}}
                #                 {"scroll": {"lines": N}}   (+ back, - forward)
                #                 {"ping": 1}                (keepalive; no-op)
                try:
                    payload = json.loads(msg["text"])
                except ValueError:
                    continue
                resize = payload.get("resize")
                if resize:
                    _set_winsize(master_fd, int(resize["rows"]), int(resize["cols"]))
                scroll = payload.get("scroll")
                if scroll:
                    # Touch drag -> an exact line count, which the wheel can't
                    # express (it lands on ~4-line notches). Runs off-loop: a
                    # slow tmux or ssh must not stall the PTY pump feeding the
                    # terminal. Best effort -- a dropped scroll is a lost gesture,
                    # never a broken session.
                    try:
                        lines = int(scroll.get("lines", 0))
                    except (TypeError, ValueError):
                        continue
                    if lines:
                        loop.run_in_executor(
                            _pool, sessions.run_send,
                            sessions.scroll_argv(host, name, lines))
    except WebSocketDisconnect:
        pass
    finally:
        if ping_task is not None:
            ping_task.cancel()
        if watching_write:
            loop.remove_writer(master_fd)
        loop.remove_reader(master_fd)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.close(master_fd)
