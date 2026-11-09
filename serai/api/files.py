"""The file pane: browse, file operations, download, cross-host transfer, upload."""

from __future__ import annotations

import asyncio
import json
import os

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .. import files
from . import common
from .common import _pool

router = APIRouter()


@router.get("/api/files")
async def api_files(host: str = "local", path: str = "~") -> JSONResponse:
    # Same allowlist as every other host-taking route: files._ssh_for connects to
    # whatever string it is handed, so an unchecked alias is hostile input (#1/#3).
    if host not in common._known_hosts():
        return JSONResponse({"error": "invalid target"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        entries = await loop.run_in_executor(_pool, files.list_dir, host, path)
    except Exception as exc:  # surface a usable message rather than a 500 blob
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"host": host, "path": path, "entries": [e.as_dict() for e in entries]})


_FILE_OPS = {"rename", "delete", "copy", "mkdir", "transfer"}


@router.post("/api/files/op")
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
    if (op not in _FILE_OPS or host not in common._known_hosts()
            or not isinstance(path, str) or not path):
        return JSONResponse({"error": "invalid request"}, status_code=400)
    if op in ("rename", "copy") and (not isinstance(dest, str) or not dest):
        return JSONResponse({"error": "dest required"}, status_code=400)
    loop = asyncio.get_event_loop()
    try:
        if op == "transfer":
            # cross-host paste: {op, host: dst, path: dst path, src_host, src_path}
            src_host = body.get("src_host", "")
            src_path = body.get("src_path") or ""
            if src_host not in common._known_hosts() or not isinstance(src_path, str) or not src_path:
                return JSONResponse({"error": "invalid source"}, status_code=400)
            await loop.run_in_executor(_pool, files.transfer_path, src_host, src_path, host, path)
        else:
            await loop.run_in_executor(_pool, files.file_op, op, host, path, dest)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True})


@router.get("/api/file")
async def api_file(host: str = "local", path: str = "") -> Response:
    if host not in common._known_hosts():
        return JSONResponse({"error": "invalid target"}, status_code=400)
    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(_pool, files.read_file, host, path)
    name = os.path.basename(path) or "download"
    return Response(
        content=data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.post("/api/files/transfer")
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
    if (host not in common._known_hosts() or src_host not in common._known_hosts()
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


@router.post("/api/upload")
async def api_upload(host: str = "local", path: str = "", file: UploadFile = File(...)) -> JSONResponse:
    if host not in common._known_hosts():
        return JSONResponse({"error": "invalid target"}, status_code=400)
    data = await file.read()
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(_pool, files.write_file, host, path, data)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    return JSONResponse({"ok": True, "path": path, "bytes": len(data)})
