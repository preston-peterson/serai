"""Directory listing and transfer for the file pane.

Local hosts use the ordinary filesystem. Remote hosts use paramiko's SFTP,
connecting through your ssh-agent and ~/.ssh/config -- no passwords are
collected or stored. Connections are cached per host for the process lifetime.
"""

from __future__ import annotations

import io
import os
import shlex
import shutil
import stat
import subprocess
import tarfile
from dataclasses import dataclass, asdict

import paramiko


@dataclass
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime: float = 0.0  # unix seconds; 0 if unknown

    def as_dict(self) -> dict:
        return asdict(self)


def _sort(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (not e.is_dir, e.name.lower()))


# --- local ----------------------------------------------------------------

def list_local(path: str) -> list[Entry]:
    path = os.path.expanduser(path or "~")
    entries: list[Entry] = []
    with os.scandir(path) as it:
        for de in it:
            try:
                is_dir = de.is_dir()
                st = de.stat()
                size = 0 if is_dir else st.st_size
                mtime = st.st_mtime
            except OSError:
                continue
            entries.append(Entry(name=de.name, is_dir=is_dir, size=size, mtime=mtime))
    return _sort(entries)


# --- remote (sftp) ---------------------------------------------------------

_clients: dict[str, paramiko.SSHClient] = {}


def _ssh_for(host: str) -> paramiko.SSHClient:
    client = _clients.get(host)
    if client is not None and client.get_transport() and client.get_transport().is_active():
        return client

    cfg = paramiko.SSHConfig()
    cfg_path = os.path.expanduser("~/.ssh/config")
    if os.path.exists(cfg_path):
        with open(cfg_path) as fh:
            cfg.parse(fh)
    opts = cfg.lookup(host)

    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=opts.get("hostname", host),
        username=opts.get("user"),
        port=int(opts.get("port", 22)),
        key_filename=opts.get("identityfile"),
        allow_agent=True,
        look_for_keys=True,
        timeout=6,
    )
    _clients[host] = client
    return client


def _remote_path(path: str) -> str:
    """Map the UI's ~-style paths onto SFTP, which has no tilde expansion.

    SFTP paths are relative to the login home directory, so "~" (what the file
    pane requests by default) becomes "." and "~/sub" becomes "sub"; absolute
    and already-relative paths pass through unchanged. Without this every remote
    browse failed with FileNotFoundError -- the "no files on this remote server"
    bug. (expanduser only ever applies to the *local* branch.)
    """
    if not path or path in ("~", "~/"):
        return "."
    if path.startswith("~/"):
        return path[2:]
    return path


def list_remote(host: str, path: str) -> list[Entry]:
    sftp = _ssh_for(host).open_sftp()
    path = _remote_path(path)
    entries: list[Entry] = []
    for attr in sftp.listdir_attr(path):
        is_dir = stat.S_ISDIR(attr.st_mode or 0)
        entries.append(Entry(name=attr.filename, is_dir=is_dir,
                             size=0 if is_dir else (attr.st_size or 0),
                             mtime=float(getattr(attr, "st_mtime", 0) or 0)))
    return _sort(entries)


def list_dir(host: str, path: str) -> list[Entry]:
    if host == "local":
        return list_local(path)
    return list_remote(host, path)


def read_file(host: str, path: str) -> bytes:
    if host == "local":
        with open(os.path.expanduser(path), "rb") as fh:
            return fh.read()
    sftp = _ssh_for(host).open_sftp()
    with sftp.open(_remote_path(path), "rb") as fh:
        return fh.read()


def write_file(host: str, path: str, data: bytes) -> None:
    if host == "local":
        with open(os.path.expanduser(path), "wb") as fh:
            fh.write(data)
        return
    sftp = _ssh_for(host).open_sftp()
    with sftp.open(_remote_path(path), "wb") as fh:
        fh.write(data)


# --- file operations (rename / delete / copy / mkdir) ------------------------

def _remote_exec(host: str, argv: list[str]) -> None:
    """Run a short command on the remote host over the cached SSH connection.

    Every argv element is shlex-quoted before joining, so client-supplied paths
    can never break out into the remote shell (invariant #3). Used only for the
    operations SFTP has no primitive for (recursive copy/delete).
    """
    cmd = " ".join(shlex.quote(a) for a in argv)
    _, stdout, stderr = _ssh_for(host).exec_command(cmd, timeout=30)
    rc = stdout.channel.recv_exit_status()
    if rc != 0:
        err = stderr.read().decode(errors="replace").strip()
        raise RuntimeError(err or f"remote command failed (exit {rc})")


def _guard_op_path(path: str) -> str:
    """Refuse operation targets that are empty or obviously catastrophic."""
    p = (path or "").strip()
    if p.rstrip("/") in ("", "~", ".", "..") or p == "/":
        raise ValueError("refusing to operate on that path")
    return p


def rename_path(host: str, path: str, dest: str) -> None:
    """Rename/move a file or directory (dest = full new path, same host)."""
    path, dest = _guard_op_path(path), _guard_op_path(dest)
    if host == "local":
        shutil.move(os.path.expanduser(path), os.path.expanduser(dest))
        return
    _ssh_for(host).open_sftp().rename(_remote_path(path), _remote_path(dest))


def delete_path(host: str, path: str) -> None:
    """Delete a file or directory (recursive). Permanent -- the UI confirms."""
    path = _guard_op_path(path)
    if host == "local":
        full = os.path.expanduser(path)
        if os.path.isdir(full) and not os.path.islink(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        return
    # `rm -rf --` covers files and directories in one quoted, argv-built command
    _remote_exec(host, ["rm", "-rf", "--", _remote_path(path)])


def copy_path(host: str, path: str, dest: str) -> None:
    """Copy a file or directory (recursive) to dest on the same host."""
    path, dest = _guard_op_path(path), _guard_op_path(dest)
    if host == "local":
        src = os.path.expanduser(path)
        dst = os.path.expanduser(dest)
        if os.path.isdir(src) and not os.path.islink(src):
            shutil.copytree(src, dst)
        else:
            shutil.copy2(src, dst)
        return
    _remote_exec(host, ["cp", "-a", "--", _remote_path(path), _remote_path(dest)])


def make_dir(host: str, path: str) -> None:
    path = _guard_op_path(path)
    if host == "local":
        os.makedirs(os.path.expanduser(path), exist_ok=False)
        return
    _ssh_for(host).open_sftp().mkdir(_remote_path(path))


def _is_dir(host: str, path: str) -> bool:
    if host == "local":
        return os.path.isdir(os.path.expanduser(path))
    try:
        st = _ssh_for(host).open_sftp().stat(_remote_path(path))
        return stat.S_ISDIR(st.st_mode or 0)
    except FileNotFoundError:
        return False


def transfer_path(src_host: str, src_path: str, dst_host: str, dst_path: str,
                  on_progress=None) -> int:
    """Copy a file OR folder between hosts, relayed through the server (which
    holds an agent-authenticated connection to both sides -- the hosts never
    talk to each other). Files relay as raw bytes; folders relay as a tar
    stream (see transfer_tree). Returns bytes moved.

    `on_progress(bytes_so_far)` is called as the relay runs, throttled to every
    _PROGRESS_EVERY bytes. It runs on the calling (worker) thread, so callers
    marshalling to an event loop must do that themselves.
    """
    src_path, dst_path = _guard_op_path(src_path), _guard_op_path(dst_path)
    if _is_dir(src_host, src_path):
        return transfer_tree(src_host, src_path, dst_host, dst_path, on_progress)
    data = read_file(src_host, src_path)
    write_file(dst_host, dst_path, data)
    if on_progress:
        on_progress(len(data))  # a single file lands in one go -- one final tick
    return len(data)


# --- folder relay (tar stream) ----------------------------------------------
# A folder crosses hosts as one tar, *streamed* rather than buffered: the source
# packs, the destination unpacks, and bytes move through a pipe a chunk at a
# time. Nothing holds the whole tree, so a multi-GB relay costs a fixed buffer
# instead of its own size in RAM (twice over, in the old shape -- packed bytes
# plus the extracted copy).
#
# Remote ends run `tar` over the cached SSH connection with every argv element
# shlex-quoted (invariant #3). A *local destination* still extracts with Python's
# tarfile rather than system tar: the archive arrives from another host, and
# tarfile is where the traversal guard lives. tar preserves modes/mtimes and
# beats per-file round trips for trees of many small files. Remote hosts need
# `tar` on PATH (they already need tmux).
#
# No timeout on the relay execs, deliberately: the old 120s cap would have killed
# exactly the large transfers this streaming is for.

_RELAY_CHUNK = 256 * 1024
_PROGRESS_EVERY = 512 * 1024  # report this often, not once per chunk


class _CountingReader:
    """Wraps a readable so we learn how much moved -- tarfile pulls at its own pace."""

    def __init__(self, src, on_progress=None) -> None:
        self._src = src
        self._on = on_progress
        self._last = 0
        self.count = 0

    def read(self, n: int = -1) -> bytes:
        b = self._src.read(n)
        self.count += len(b)
        if self._on and self.count - self._last >= _PROGRESS_EVERY:
            self._last = self.count
            self._on(self.count)
        return b


def _tar_producer(host: str, path: str):
    """(readable tar stream, finish) for `path`.

    finish() raises if the packer exited non-zero, so a missing path or a
    permission error surfaces instead of quietly relaying an empty tree.
    """
    p = (os.path.expanduser(path) if host == "local" else _remote_path(path)).rstrip("/")
    parent, name = os.path.split(p)
    argv = ["tar", "-C", parent or ".", "-cf", "-", "--", name]

    if host == "local":
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def finish() -> None:
            err = proc.stderr.read()
            if proc.wait() != 0:
                raise RuntimeError(err.decode(errors="replace").strip() or "tar failed")

        return proc.stdout, finish

    _, stdout, stderr = _ssh_for(host).exec_command(" ".join(shlex.quote(a) for a in argv))

    def finish() -> None:
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(stderr.read().decode(errors="replace").strip() or "remote tar failed")

    return stdout, finish


def _untar_local_stream(src, parent: str, on_progress=None) -> int:
    """Extract a tar stream into a local dir, refusing members that escape it."""
    dest = os.path.expanduser(parent)
    counted = _CountingReader(src, on_progress)
    # "r|" is tarfile's streaming reader: members are handled as they arrive, so a
    # non-seekable pipe works and the archive is never materialised.
    with tarfile.open(fileobj=counted, mode="r|") as tf:
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, filter="data")  # py>=3.12: blocks traversal/devices
        else:
            # 3.10/3.11: one pass, checking each member as it comes off the stream
            base = os.path.realpath(dest)
            for m in tf:
                target = os.path.realpath(os.path.join(dest, m.name))
                if target != base and not target.startswith(base + os.sep):
                    raise ValueError(f"refusing tar member outside destination: {m.name}")
                tf.extract(m, dest)
    return counted.count


def _untar_remote_stream(host: str, parent: str, src, on_progress=None) -> int:
    """Pipe a tar stream into `tar -xf -` on a remote host."""
    argv = ["tar", "-C", _remote_path(parent) or ".", "-xf", "-"]
    stdin, stdout, stderr = _ssh_for(host).exec_command(" ".join(shlex.quote(a) for a in argv))
    moved = last = 0
    while True:
        chunk = src.read(_RELAY_CHUNK)
        if not chunk:
            break
        stdin.write(chunk)
        moved += len(chunk)
        if on_progress and moved - last >= _PROGRESS_EVERY:
            last = moved
            on_progress(moved)
    stdin.channel.shutdown_write()
    if stdout.channel.recv_exit_status() != 0:
        raise RuntimeError(stderr.read().decode(errors="replace").strip() or "remote untar failed")
    return moved


def transfer_tree(src_host: str, src_path: str, dst_host: str, dst_path: str,
                  on_progress=None) -> int:
    """Relay a whole folder between hosts as a streamed tar. Returns bytes moved."""
    src_path, dst_path = _guard_op_path(src_path), _guard_op_path(dst_path)
    src_name = os.path.basename(src_path.rstrip("/"))
    if os.path.basename(dst_path.rstrip("/")) != src_name:
        raise ValueError("cross-host folder paste keeps the folder name")
    parent = os.path.dirname(dst_path.rstrip("/"))
    if not parent:
        parent = "/" if dst_path.startswith("/") else "~"

    stream, finish = _tar_producer(src_host, src_path)
    try:
        moved = (_untar_local_stream(stream, parent, on_progress) if dst_host == "local"
                 else _untar_remote_stream(dst_host, parent, stream, on_progress))
        finish()  # a source-side failure surfaces here, not as a half-copied tree
        return moved
    finally:
        try:
            stream.close()
        except Exception:  # closing a spent pipe must never mask the real error
            pass


def file_op(op: str, host: str, path: str, dest: str | None = None) -> None:
    """Dispatch a file operation; `op` is validated by the API layer."""
    if op == "rename":
        rename_path(host, path, dest or "")
    elif op == "copy":
        copy_path(host, path, dest or "")
    elif op == "delete":
        delete_path(host, path)
    elif op == "mkdir":
        make_dir(host, path)
    else:
        raise ValueError(f"unknown op: {op}")
