"""Serai -- a single attach point for your terminal, SSH, and coding-agent
(Claude / Grok / OpenCode) sessions across local and remote hosts.

Design notes:
  * Holds no credentials. Remote work goes through your ssh-agent and
    ~/.ssh/config; tmux gives every session persistence and resumability.
  * Binds to 127.0.0.1 by default -- put whatever access layer you like in
    front of it.

This module is the app itself: the FastAPI instance, the two middlewares, the
static shell, and the routers. The routes live in serai/api/ (one module per
area) and serai/ws/attach.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import auth, config, sessions
from .api import admin as admin_api
from .api import auth as auth_api
from .api import files as files_api
from .api import profiles as profiles_api
from .api import sessions as sessions_api
from .api.common import _current_version
from .ws import attach as ws_api

# `config` and `sessions` have no user left in this module after the split: the
# tests patch them through it (serai.main.sessions.list_sessions,
# serai.main.config.parse_ssh_config), which only resolves while the modules are
# bound here. The routers hold these same module objects.

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


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

# --- routes ----------------------------------------------------------------
#
# Included in the order the routes were declared before the split, so path
# matching is unchanged.

app.include_router(auth_api.router)
app.include_router(admin_api.router)
app.include_router(sessions_api.router)
app.include_router(profiles_api.router)
app.include_router(files_api.router)
app.include_router(ws_api.router)
