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

import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import __version__
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
    }
