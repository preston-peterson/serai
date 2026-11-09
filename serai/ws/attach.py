"""Terminal attach over a websocket.

v3: a tmux control-mode client per attached browser terminal, pumped by
serai/bridge.py (see its docstring for the wire protocol). The session is
created first, detached, when it doesn't exist; then the control client
attaches to it. tmux stays the persistence substrate (invariant #4): closing
the socket only ends the control client.
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .. import auth, bridge, sessions, tmuxctl
from ..api import common
from ..api.common import _pool

router = APIRouter()

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


# One lock per remote host, held while a control client starts. With
# ControlMaster=auto, attaches that start together (two panes on page load, or
# every pane reconnecting after the master died) each find no master yet: one
# becomes it and the rest open their own TCP connections. Measured against a
# real host (tests/browser/test_remote.py): 2 connections for 2 panes. Holding
# the lock until the first client's tmux has answered means the master is up and
# listening, so the next one joins it. Costs one handshake of waiting, per host.
_MUX_LOCKS: dict[str, asyncio.Lock] = {}


async def _start_control(host: str, client) -> None:
    if not tmuxctl.uses_mux(host):
        await client.start()
        return
    async with _MUX_LOCKS.setdefault(host, asyncio.Lock()):
        await client.start()


@router.websocket("/ws/attach")
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
    if host not in common._known_hosts():
        await ws.send_text("\r\n\x1b[31m[serai] unknown host -- refusing to attach\x1b[0m\r\n")
        await ws.close(code=4404)
        return
    # A name ending in ';' would split the tmux create command (see valid_name).
    if not sessions.valid_name(name):
        await ws.send_text("\r\n\x1b[31m[serai] invalid session name -- refusing to attach\x1b[0m\r\n")
        await ws.close(code=4400)
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
        if not common._owns(ws_sess, await loop.run_in_executor(_pool, sessions.get_owner, host, name)):
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
        await _start_control(host, client)
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
