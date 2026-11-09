"""Saved session profiles: the restore inventory, extra args, favorite, launch."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import sessions, store
from . import common
from .common import _pool

router = APIRouter()


@router.get("/api/sessions/saved")
async def api_sessions_saved(request: Request) -> JSONResponse:
    """Saved session profiles (known hosts only, and only the caller's own --
    see _owns). Not-live entries are what + New and the palette offer to resume."""
    sess = common._session(request)
    loop = asyncio.get_event_loop()
    saved = await loop.run_in_executor(_pool, store.saved)
    known = common._known_hosts()
    return JSONResponse([r for r in saved
                         if r.get("host") in known and common._owns(sess, r.get("owner", ""))])


@router.post("/api/args")
async def api_args(request: Request) -> JSONResponse:
    """Set a session's extra agent arguments (the @serai_args tmux option).
    Body: {host, name, args: "--chrome"}. Sanitised server-side by clean_args,
    which shlex-quotes every token -- see invariant #3.

    Takes effect on the session's *next* create, exactly like a start dir, so the
    caller normally follows with /api/sessions/restart.
    """
    t = await common._target(request)
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


@router.post("/api/favorite")
async def api_favorite(request: Request) -> JSONResponse:
    """Set a saved profile's favorite flag (the header favorites list).
    Body: {host, name, favorite: true|false}. Store-side only -- there is no
    tmux option to set. Owner-gated like every other per-session mutation, so a
    guessed name can't favorite or unfavorite someone else's session. A no-op on
    a name with no profile yet (the create path re-applies once it exists)."""
    t = await common._target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    favorite = bool(body.get("favorite"))
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_pool, store.set_favorite, host, name, favorite)
    return JSONResponse({"ok": True, "favorite": favorite})


@router.post("/api/launch")
async def api_launch(request: Request) -> JSONResponse:
    """Set a saved profile's launch rule: "new", "continue" or "resume" ("" clears
    it, falling back to the per-kind default). Body: {host, name, launch}.
    Store-side only -- like favorite there is no tmux option to set; the value is
    read back out of the profile when the session is next created. Owner-gated
    like every other per-session mutation, so a guessed name can't change someone
    else's profile. Takes effect on the session's *next* create.

    Absent is not cleared: the key is required, so only an explicit "" reverts a
    profile to the default. Coercing a missing key (or a null, or a 0) to "" would
    let any body that omitted the field silently wipe a deliberate choice."""
    t = await common._target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    if "launch" not in body:
        return JSONResponse({"error": "launch required"}, status_code=400)
    launch = body["launch"]
    if not isinstance(launch, str):
        return JSONResponse({"error": "launch must be a string"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, store.set_launch, host, name, launch)
    except ValueError as exc:                      # not one of the three modes
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"ok": True, "launch": launch})


@router.post("/api/profiles/forget")
async def api_profile_forget(request: Request) -> JSONResponse:
    """Forget a saved profile and leave the running session alone. Body: {host, name}.

    The edit form's "Forget profile" (v3 plan 3.4: one meaning per action).
    ✕ / Close ends the tmux session and forgets the profile unless it is a
    favorite; this is the other half -- forget without killing. Owner-gated by
    ``_target`` (a profile that isn't live is checked against its stored owner),
    so a guessed name can't forget someone else's. The next poll re-saves a
    profile for a session that is still running, minus the favorite flag and
    launch choice -- which is what "forget" means.
    """
    t = await common._target(request)
    if isinstance(t, JSONResponse):
        return t
    _, host, name = t
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_pool, store.remove, host, name)
    return JSONResponse({"ok": True})

