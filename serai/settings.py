"""Server-side persistence for the UI's settings blob.

The web UI keeps its preferences (terminal theme/font/size, file-column sort and
widths, splitter sizes, fleet selection) in localStorage. To make them durable
across browsers and devices, the client mirrors that blob here and pulls it back
on load. This stores only UI preferences -- never credentials, hosts, or keys
(those still come from the ssh-agent and ~/.ssh/config), so the "stores no
credentials" guarantee holds.

One JSON file, single operator, last-write-wins. It lives in serai's one config
dir (see serai.paths), with its own ``SERAI_SETTINGS`` override for the file:
  $SERAI_SETTINGS, else $SERAI_CONFIG_DIR/settings.json, else
  $XDG_CONFIG_HOME/serai/settings.json, else ~/.config/serai/settings.json.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from . import paths

# Serialises read-modify-write in merge(); two tabs saving at once would
# otherwise interleave load and save and lose one of the writes.
_lock = threading.Lock()


def _path() -> Path:
    override = os.environ.get("SERAI_SETTINGS")
    if override:
        return Path(override)
    return paths.config_dir() / "settings.json"


def load() -> dict:
    """Return the saved settings dict, or {} if none/unreadable."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    """Write the settings dict atomically. 0644 as before: UI preferences, not
    credentials (the invariant-1 guarantee is that nothing sensitive is here)."""
    paths.atomic_write(_path(), json.dumps(data, indent=2), 0o644)


def merge(patch: dict) -> dict:
    """Apply ``patch`` over the stored settings and return the result.

    The UI mirrors its whole localStorage into one blob, so a plain replace made
    the sync last-writer-wins *across tabs*: a tab opened before some preference
    existed doesn't have that key, and the next time it saved anything at all it
    silently dropped the key for everyone. Merging keeps a tab's write to the
    keys it actually knows about.

    Nothing in the UI deletes a ``serai.*`` key, so no key ever needs to be
    removed by a write -- if that changes, this needs an explicit tombstone
    rather than going back to replace.
    """
    with _lock:
        data = load()
        data.update(patch)
        save(data)
        return data
