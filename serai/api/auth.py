"""Login, setup, and the user store (the /api/auth, /api/login, /api/users routes)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from .. import auth
from . import common
from .common import _pool

router = APIRouter()


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


@router.get("/api/auth/status")
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


@router.post("/api/login")
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


@router.post("/api/logout")
async def api_logout() -> JSONResponse:
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.post("/api/setup")
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


@router.get("/api/users")
async def api_users(request: Request) -> JSONResponse:
    sess = common._session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    loop = asyncio.get_event_loop()
    users = await loop.run_in_executor(_pool, auth.list_users)
    return JSONResponse({"users": users, "me": sess["username"]})


@router.post("/api/users")
async def api_users_add(request: Request) -> JSONResponse:
    sess = common._session(request)
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


@router.delete("/api/users/{username}")
async def api_users_remove(username: str, request: Request) -> JSONResponse:
    sess = common._session(request)
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


@router.post("/api/users/{username}/password")
async def api_users_password(username: str, request: Request) -> JSONResponse:
    """Set a password. Admins may reset anyone else's; changing your *own*
    (admin or not) needs the current one in ``current``, so a session cookie
    left in an unattended browser is not enough to take the account over."""
    sess = common._session(request)
    if not sess:
        return JSONResponse({"error": "auth required"}, status_code=401)
    own = username == sess["username"]
    if not (sess["admin"] or own):
        return JSONResponse({"error": "not allowed"}, status_code=403)
    body = await request.json()
    password = body.get("password") or ""
    loop = asyncio.get_event_loop()
    if own:
        current = body.get("current")
        ok = isinstance(current, str) and await loop.run_in_executor(
            _pool, auth.verify_credentials, username, current)
        if not ok:
            return JSONResponse({"error": "your current password is not right"}, status_code=403)
    try:
        await loop.run_in_executor(_pool, auth.set_password, username, password)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True})
