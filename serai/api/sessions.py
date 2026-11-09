"""Live sessions: enumerate, broadcast, rename, kill, restart, tags, start dir."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import config, sessions, store
from . import common
from .common import _pool

router = APIRouter()


@router.get("/api/kinds")
async def api_kinds() -> JSONResponse:
    """The session-kinds table (shell + every coding agent), straight from
    `sessions.kinds()` -- cmd, prefix, chip, glyph and resume flags in one place,
    so the UI doesn't keep a second copy of its own. Read-only and tiny; the
    ordering it returns is the ordering the UI should render."""
    return JSONResponse(sessions.kinds())


@router.get("/api/sessions")
async def api_sessions(request: Request) -> JSONResponse:
    """Enumerate live sessions on local + every configured host, concurrently."""
    sess = common._session(request)
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
    return JSONResponse([s for s in flat if common._owns(sess, s.get("owner", ""))])


@router.post("/api/broadcast")
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

    known = common._known_hosts()
    loop = asyncio.get_event_loop()

    sess = common._session(request)

    async def deliver(t: dict) -> dict:
        host = t.get("host", "local")
        name = t.get("name") or ""
        if host not in known or not name:
            return {"host": host, "name": name, "ok": False, "error": "invalid target"}
        # A broadcast types into a live session, so it needs the same ownership
        # check as attaching -- otherwise it would be the way around the others.
        if not await common._may_touch(sess, host, name):
            return {"host": host, "name": name, "ok": False, "error": "not your session"}
        argv = sessions.send_keys_argv(host, name, text)
        ok = await loop.run_in_executor(_pool, sessions.run_send, argv)
        return {"host": host, "name": name, "ok": bool(ok)}

    results = await asyncio.gather(*[deliver(t) for t in targets if isinstance(t, dict)])
    return JSONResponse({"sent": sum(r["ok"] for r in results), "results": results})


@router.post("/api/rename")
async def api_rename(request: Request) -> JSONResponse:
    """Rename a session. Body: {host, name, kind, label}. The new tmux name is
    built from kind+label so the cc-/shell- prefix is preserved (invariant #5)."""
    t = await common._target(request)
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


@router.post("/api/kill")
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
    t = await common._target(request)
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


@router.post("/api/sessions/restart")
async def api_restart(request: Request) -> JSONResponse:
    """Kill a session but *keep* its restore record. Body: {host, name}.

    tmux `new -A` only runs its command on create, so a change to a session's
    extra args (or its start dir) can't reach one that is already running -- the
    session has to be recreated. That is what this is for, and it is why it is
    not `/api/kill`: that one calls store.remove, which would throw away the very
    record the client needs to recreate the session from. The client attaches
    straight afterwards, which is what brings the session back.
    """
    t = await common._target(request)
    if isinstance(t, JSONResponse):
        return t
    _, host, name = t
    loop = asyncio.get_event_loop()
    ok = await loop.run_in_executor(_pool, sessions.run_send, sessions.kill_argv(host, name))
    sessions.clear_cache(host)
    return JSONResponse({"ok": bool(ok)})


@router.post("/api/sessions/relaunch")
async def api_relaunch(request: Request) -> JSONResponse:
    """Run an agent session's command again, in its pane, after the harness
    exited to the shell prompt (_EXIT_SHELL). Body: {host, name, launch?}.

    The one launch rule: the profile's effective launch (new / continue /
    resume) plus the args-win rule of ``resume_flag`` -- the same pieces a
    create uses. ``launch`` overrides the rule for this one run (the "start
    fresh" button sends "new"). The command is typed with send-keys as a single
    literal argument; args are already shlex-quoted tokens (clean_args), so the
    shell sees exactly what a create would have run (invariant #3).

    Refuses unless the session is an agent kind with a shell in the foreground:
    typing a command into a running TUI would be input to the agent, not a relaunch.
    """
    t = await common._target(request)
    if isinstance(t, JSONResponse):
        return t
    body, host, name = t
    mode = body.get("launch")
    if mode is not None and mode not in ("new", "continue", "resume"):
        return JSONResponse({"error": "launch must be new, continue or resume"}, status_code=400)
    loop = asyncio.get_event_loop()
    sessions.clear_cache(host)
    live = next((s for s in await loop.run_in_executor(_pool, sessions.list_sessions, host)
                 if s.name == name), None)
    if live is None:
        return JSONResponse({"error": "no such session"}, status_code=404)
    spec = sessions._AGENTS.get(live.kind)
    if not spec:
        return JSONResponse({"error": "not an agent session"}, status_code=400)
    busy = JSONResponse({"error": f"{spec['cmd']} is still running in that session"},
                        status_code=409)
    if not live.at_prompt:
        return busy
    # Belt and braces for a pre-v3 session on a remote host, where the listing
    # can't see past the wrapper shell: typing into a running agent would be a
    # prompt to it, not a relaunch. Unknown (no pid, ssh failed) refuses too.
    pid = await loop.run_in_executor(_pool, sessions.pane_pid, host, name)
    if pid is None or await loop.run_in_executor(
            _pool, sessions.pane_has_children, host, pid) is not False:
        return busy
    profile = await loop.run_in_executor(_pool, store.get, host, name) or {}
    if mode is None:
        mode = store.effective_launch({**profile, "kind": live.kind})
    # These are typed into a live shell, so re-clean them here rather than trust
    # every writer of @serai_args / the profile to have. Idempotent on a value
    # clean_args already produced, so a normal relaunch is unchanged.
    try:
        args = sessions.clean_args(live.args or profile.get("args") or "")
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    flag = sessions.resume_flag(live.kind, "" if mode == "new" else mode, args)
    command = f"{spec['cmd']}{flag}" + (f" {args}" if args else "")
    ok = await loop.run_in_executor(_pool, sessions.run_send,
                                    sessions.send_keys_argv(host, name, command))
    sessions.clear_cache(host)
    if not ok:
        return JSONResponse({"error": "tmux refused the keys"}, status_code=502)
    return JSONResponse({"ok": True, "command": command})


@router.post("/api/tags")
async def api_tags(request: Request) -> JSONResponse:
    """Set a session's tags (stored as the @serai_tags tmux user option).
    Body: {host, name, tags: [...]}. Tags are sanitized server-side."""
    t = await common._target(request)
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


@router.post("/api/dir")
async def api_dir(request: Request) -> JSONResponse:
    """Set a session's "start in" directory (the @serai_dir tmux user option).

    Body: {host, name, path}; an empty path clears it. This is where the file
    pane opens for the session and where a post-reboot restore starts it -- it
    does not move a shell that is already running.
    """
    t = await common._target(request)
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
