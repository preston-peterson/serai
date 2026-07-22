"""Tell the operator when a newer serai has been released.

serai already notices when the *running server* changes under an open tab
(``main`` stamps ``X-Serai-Version`` and the UI offers a reload). This answers
the other question: whether a newer release exists upstream at all.

The check runs **server-side, once per instance, cached hard**. Every open tab
asking GitHub independently would be wasteful and would walk into the
unauthenticated rate limit (60 requests/hour per IP) on a tool people leave open
for days. The last result is persisted, so a restart does not re-poll.

It touches the network, so it can be switched off three ways: the operator's
interval choice (which includes ``off``), ``SERAI_UPDATE_CHECK=off`` for a whole
install, and simply being offline -- failures are recorded and swallowed, never
raised, so an air-gapped box reads "couldn't reach GitHub" rather than breaking
a page load.

Stored as JSON under the config dir (``~/.config/serai/updates.json``, matching
auth/settings/store). It holds the last check's result and nothing sensitive.
Forks: point it elsewhere with ``SERAI_UPDATE_REPO=owner/name``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
from . import netcfg
from . import settings

# How often to poll, in seconds. "off" is handled separately -- it is a valid
# stored choice, not an interval.
INTERVALS = {"daily": 86_400, "weekly": 604_800, "monthly": 2_592_000}
DEFAULT_INTERVAL = "weekly"

# The client mirrors localStorage keys prefixed "serai." into the settings blob
# (see app.js schedulePush), so the operator's choice arrives here for free.
SETTING_KEY = "serai.updates.interval"

_DEFAULT_REPO = "preston-peterson/serai"
_TIMEOUT = 6.0
_lock = threading.Lock()

# A release tag, validated before it is ever put in a URL or a path. The apply
# path builds a download URL and a filename from this, so it must be exactly a
# version and nothing that could traverse or inject (invariant #3).
_TAG_RE = re.compile(r"^v?\d+(\.\d+){0,3}$")
_apply_lock = threading.Lock()


def _config_dir() -> Path:
    override = os.environ.get("SERAI_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "serai"


def _state_path() -> Path:
    return _config_dir() / "updates.json"


def _load_state() -> dict:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(data: dict) -> None:
    """Write the cache atomically. Never raises -- a read-only config dir must
    degrade to "check every time", not to a broken settings panel."""
    try:
        path = _state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def interval() -> str:
    """The effective check interval: one of INTERVALS, or "off".

    An install-wide ``SERAI_UPDATE_CHECK=off`` wins over the stored preference,
    so an operator can disable the network call without touching the UI.
    """
    env = os.environ.get("SERAI_UPDATE_CHECK", "").strip().lower()
    if env in {"off", "0", "false", "no", "never"}:
        return "off"
    stored = settings.load().get(SETTING_KEY)
    if isinstance(stored, str):
        # the settings blob stores raw localStorage strings, so it may be JSON-quoted
        choice = stored.strip().strip('"').lower()
        if choice in INTERVALS or choice == "off":
            return choice
    return DEFAULT_INTERVAL


def _version_tuple(text: str) -> tuple:
    """A comparable tuple from a version string, tolerant of a "v" prefix and
    of suffixes like "2.15.0-rc1" (which sorts *below* the plain release)."""
    core = text.strip().lstrip("vV").split("+", 1)[0]
    core, _, pre = core.partition("-")
    parts = []
    for chunk in core.split("."):
        digits = "".join(c for c in chunk if c.isdigit())
        parts.append(int(digits) if digits else 0)
    # no pre-release marker sorts above one ("2.15.0" > "2.15.0-rc1")
    return (tuple(parts), 0 if pre else 1)


def is_newer(latest: str, current: str) -> bool:
    try:
        return _version_tuple(latest) > _version_tuple(current)
    except (ValueError, TypeError):
        return False


def _fetch_latest() -> dict:
    """Ask GitHub for the newest release. Returns a state fragment; never raises."""
    repo = os.environ.get("SERAI_UPDATE_REPO", _DEFAULT_REPO).strip() or _DEFAULT_REPO
    url = f"https://api.github.com/repos/{repo}/releases/latest"
    req = urllib.request.Request(url, headers={
        # GitHub rejects requests without a User-Agent
        "User-Agent": f"serai/{__version__}",
        "Accept": "application/vnd.github+json",
    })
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # the repo publishes no releases yet -- a real answer, not a failure
            return {"latest": None, "url": None, "error": None, "no_releases": True}
        return {"error": f"GitHub returned {exc.code}"}
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        return {"error": f"couldn't reach GitHub ({exc.__class__.__name__})"}

    tag = (payload.get("tag_name") or "").strip()
    if not tag:
        return {"error": "release had no tag"}
    return {
        "latest": tag.lstrip("vV"),
        "url": payload.get("html_url"),
        "error": None,
        "no_releases": False,
    }


def status(force: bool = False) -> dict:
    """Current update status, checking upstream only if a check is due.

    ``force`` is the settings panel's "check now": it polls even when the
    interval says otherwise, but still respects a hard ``off`` from the
    environment, since that exists to stop the call happening at all.
    """
    choice = interval()
    state = _load_state()
    now = time.time()
    env_off = os.environ.get("SERAI_UPDATE_CHECK", "").strip().lower() in {
        "off", "0", "false", "no", "never"}

    due = False
    if not env_off:
        if force:
            due = True
        elif choice != "off":
            last = state.get("checked_at") or 0
            due = (now - last) >= INTERVALS[choice]

    if due:
        with _lock:
            # re-read under the lock: a concurrent caller may have just checked
            state = _load_state()
            last = state.get("checked_at") or 0
            if force or (now - last) >= INTERVALS.get(choice, 0):
                fetched = _fetch_latest()
                state = {**state, **fetched, "checked_at": now}
                _save_state(state)

    latest = state.get("latest")
    return {
        "current": __version__,
        "latest": latest,
        "available": bool(latest) and is_newer(latest, __version__),
        "url": state.get("url"),
        "checked_at": state.get("checked_at"),
        "interval": "off" if env_off else choice,
        "env_locked": env_off,
        "no_releases": bool(state.get("no_releases")),
        "error": state.get("error"),
        # whether the running install can update itself in place (systemd unit);
        # a checkout or `./run.sh` can't, so the UI shows the command instead
        "can_apply": can_apply(),
    }


# --- applying an update ----------------------------------------------------
# Downloads the latest release, verifies it against the release's own sha256,
# and re-runs the installer over the running install. This is get.sh minus the
# preflight, driven from the UI: same download-verify-extract-install.sh path,
# so there is one way an install is laid out, not two. Admin-gated in main.py;
# nothing user-supplied reaches the command (invariant #3).


def can_apply() -> bool:
    """True when the running install can replace itself: it is under a systemd
    unit (so it can restart) and its code lives in a writable directory."""
    if netcfg.service_mode() is None:
        return False
    try:
        return os.access(_install_dir(), os.W_OK)
    except OSError:
        return False


def _install_dir() -> Path:
    """Where the running app lives -- the parent of the ``serai`` package. On an
    install that is ``~/.local/share/serai`` (or ``/opt/serai``); install.sh
    copies files here and the service runs from it."""
    return Path(__file__).resolve().parent.parent


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": f"serai/{__version__}"})
    with urllib.request.urlopen(req, timeout=30) as resp, open(dest, "wb") as fh:
        while True:
            chunk = resp.read(65536)
            if not chunk:
                break
            fh.write(chunk)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _safe_members(tar: tarfile.TarFile, root: str):
    """Yield only members that stay inside ``root/`` -- a defence against a
    path-traversal entry in a tarball, even though we build the tarball
    ourselves. Refuses absolute paths, ``..`` escapes, and non-file/dir types."""
    prefix = root + "/"
    for m in tar.getmembers():
        name = m.name
        if name != root and not name.startswith(prefix):
            raise ValueError(f"tarball entry escapes {root}/: {name!r}")
        if m.issym() or m.islnk() or m.isdev():
            raise ValueError(f"tarball has a non-regular entry: {name!r}")
        yield m


def apply() -> dict:
    """Update the running install to the latest release, then restart.

    Returns a result dict; never raises. The flow, all off the request thread:
    resolve the latest tag -> download the tarball + checksums -> verify sha256
    -> extract -> run ``install.sh --no-restart`` over the current install dir
    -> restart via netcfg. install.sh does the copy-out and dep install; we
    restart last so the installer isn't killed by its own service going down.
    """
    if not _apply_lock.acquire(blocking=False):
        return {"ok": False, "stage": "lock", "error": "an update is already running"}
    try:
        return _apply_locked()
    finally:
        _apply_lock.release()


def _apply_locked() -> dict:
    if netcfg.service_mode() is None:
        return {"ok": False, "stage": "capability",
                "error": "not running as a systemd service -- update from a shell instead"}

    repo = os.environ.get("SERAI_UPDATE_REPO", _DEFAULT_REPO).strip() or _DEFAULT_REPO
    st = status()
    tag = st.get("latest")
    if not tag:
        return {"ok": False, "stage": "resolve", "error": "no release to update to"}
    if not st.get("available"):
        return {"ok": False, "stage": "resolve",
                "error": f"already on the latest release ({__version__})"}

    # Validate before the tag touches a URL or a path. GitHub gave us this, but
    # it is still external input feeding a filesystem path and a command.
    if not _TAG_RE.match(tag):
        return {"ok": False, "stage": "resolve", "error": f"refusing suspicious tag {tag!r}"}
    ver = tag.lstrip("vV")
    gh_tag = f"v{ver}"

    base = f"https://github.com/{repo}/releases/download/{gh_tag}"
    tarball_name = f"serai_{ver}.tar.gz"
    sums_name = f"serai_{ver}_checksums.txt"

    with tempfile.TemporaryDirectory(prefix="serai-update-") as tmp:
        tmpd = Path(tmp)
        tarball = tmpd / tarball_name
        sums = tmpd / sums_name
        try:
            _download(f"{base}/{tarball_name}", tarball)
            _download(f"{base}/{sums_name}", sums)
        except (urllib.error.URLError, OSError) as exc:
            return {"ok": False, "stage": "download",
                    "error": f"download failed ({exc.__class__.__name__})"}

        # Verify exactly as get.sh does: the sums file must contain a line for
        # this tarball, and the digest must match. A missing line is a failure,
        # not a pass -- `sha256sum -c` with no matching entry succeeds trivially.
        want = None
        for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1].lstrip("*") == tarball_name:
                want = parts[0].lower()
                break
        if not want:
            return {"ok": False, "stage": "verify",
                    "error": f"{sums_name} has no entry for {tarball_name}"}
        got = _sha256(tarball)
        if got != want:
            return {"ok": False, "stage": "verify",
                    "error": "sha256 mismatch -- refusing to install"}

        # Extract the source tree from the verified tarball. _safe_members already
        # rejects traversal/symlink entries; also pass the stdlib "data" filter
        # where available (3.12+) as belt-and-braces and to silence 3.14's
        # deprecation of the unfiltered default.
        extract_root = tmpd / "src"
        extract_root.mkdir()
        try:
            with tarfile.open(tarball, "r:gz") as tar:
                top = f"serai-{ver}"
                members = list(_safe_members(tar, top))
                try:
                    tar.extractall(extract_root, members=members, filter="data")
                except TypeError:
                    tar.extractall(extract_root, members=members)  # Python < 3.12
        except (tarfile.TarError, ValueError, OSError) as exc:
            return {"ok": False, "stage": "extract", "error": f"bad tarball: {exc}"}
        src = extract_root / f"serai-{ver}"
        installer = src / "install.sh"
        if not installer.is_file():
            return {"ok": False, "stage": "extract",
                    "error": "install.sh missing from release tarball"}

        # Run the new install.sh over the current install dir, restart suppressed
        # so it doesn't kill itself when the service goes down. argv only, no
        # shell, no user input -- the prefix is our own resolved path.
        #
        # --no-restart was added in the same change as this updater, so a release
        # that predates it won't understand the flag. Detect support (it appears
        # in --help) and, if absent, run without it and skip our own restart --
        # that older install.sh restarts by itself. Either way the service comes
        # back on the new code.
        install_dir = _install_dir()
        mode = netcfg.service_mode()
        supports_no_restart = False
        try:
            help_out = subprocess.run(["bash", str(installer), "--help"],
                                      cwd=str(src), capture_output=True,
                                      text=True, timeout=15)
            supports_no_restart = "--no-restart" in (help_out.stdout + help_out.stderr)
        except (OSError, subprocess.SubprocessError):
            pass

        cmd = ["bash", str(installer), "--prefix", str(install_dir)]
        if mode == "system":
            cmd.append("--system")
        if supports_no_restart:
            cmd.append("--no-restart")
        try:
            proc = subprocess.run(cmd, cwd=str(src), capture_output=True,
                                  text=True, timeout=300)
        except subprocess.TimeoutExpired:
            # An installer that restarts on its own can kill us here; that is a
            # successful update, not a timeout. Only treat it as success when we
            # did NOT suppress the restart (i.e. we handed control to the old one).
            if not supports_no_restart:
                return {"ok": True, "stage": "done", "from_version": __version__,
                        "to_version": ver, "restart": {"ok": True, "self": True},
                        "error": None}
            return {"ok": False, "stage": "install", "error": "installer timed out"}
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            return {"ok": False, "stage": "install",
                    "error": "install.sh failed: " + " / ".join(tail)}

        # Old installer already restarted; nothing left to do.
        if not supports_no_restart:
            return {"ok": True, "stage": "done", "from_version": __version__,
                    "to_version": ver, "restart": {"ok": True, "self": True},
                    "error": None}

    # Files are in place; the running process still holds the old code in memory.
    # Restart last -- --no-block, so the HTTP response can flush before systemd
    # tears this process down. netcfg.restart() picks user vs system + sudo.
    restart = netcfg.restart()
    return {
        "ok": bool(restart.get("ok")),
        "stage": "restart" if not restart.get("ok") else "done",
        "from_version": __version__,
        "to_version": ver,
        "restart": restart,
        "error": None if restart.get("ok") else restart.get("reason", "restart failed"),
    }
