"""Operator-facing settings: network, the UI preference blob, updates, hosts."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, netcfg, sessions, settings, updates
from . import common
from .common import _pool

router = APIRouter()


@router.get("/api/network")
async def api_network(request: Request) -> JSONResponse:
    """Current listen address + cert hostnames (admin only)."""
    sess = common._session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, netcfg.read))


@router.post("/api/network")
async def api_network_save(request: Request) -> JSONResponse:
    """Set the listen address + cert hostnames, then restart to apply (admin).
    serai reissues its self-signed cert on restart to cover the new names."""
    sess = common._session(request)
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


# --- read APIs -------------------------------------------------------------

@router.get("/api/settings")
async def api_get_settings() -> JSONResponse:
    """The UI's persisted preference blob (durable across browsers/devices)."""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, settings.load))


@router.put("/api/settings")
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


@router.get("/api/updates")
async def api_updates() -> JSONResponse:
    """Whether a newer serai has been released. Polls upstream only when the
    operator's chosen interval says a check is due -- see serai.updates."""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, updates.status))


@router.post("/api/updates/check")
async def api_updates_check() -> JSONResponse:
    """The settings panel's "check now" -- poll regardless of the interval."""
    loop = asyncio.get_event_loop()
    return JSONResponse(await loop.run_in_executor(_pool, updates.status, True))


@router.post("/api/updates/apply")
async def api_updates_apply(request: Request) -> JSONResponse:
    """Download the latest release, verify its sha256, install it, and restart.

    Admin only -- this is the one endpoint that runs the installer over the
    running code (invariant #3: the command is argv-built from our own paths and
    a GitHub-supplied tag validated against a version regex; nothing from the
    request reaches it). Runs off the event loop; on success the service
    restarts and the UI's version-change detector prompts a reload."""
    sess = common._session(request)
    if not (sess and sess["admin"]):
        return JSONResponse({"error": "admin required"}, status_code=403)
    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(_pool, updates.apply)
    return JSONResponse(result, status_code=200 if result.get("ok") else 409)


@router.get("/api/hosts")
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


@router.post("/api/hosts")
async def api_hosts_add(request: Request) -> JSONResponse:
    """Add a host to ~/.ssh/config (admin only). Stores connection metadata only
    -- no credentials; ssh config stays the single source of truth (invariants
    #1/#6). Fields are validated to a tight charset to prevent config injection."""
    sess = common._session(request)
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
