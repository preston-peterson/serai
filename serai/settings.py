"""Server-side persistence for the UI's settings blob.

The web UI keeps its preferences (terminal theme/font/size, file-column sort and
widths, splitter sizes, fleet selection) in localStorage. To make them durable
across browsers and devices, the client mirrors that blob here and pulls it back
on load. This stores only UI preferences -- never credentials, hosts, or keys
(those still come from the ssh-agent and ~/.ssh/config), so the "stores no
credentials" guarantee holds.

One JSON file, single operator, last-write-wins. Path:
  $SERAI_SETTINGS, else $XDG_CONFIG_HOME/serai/settings.json, else
  ~/.config/serai/settings.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path


def _path() -> Path:
    override = os.environ.get("SERAI_SETTINGS")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "serai" / "settings.json"


def load() -> dict:
    """Return the saved settings dict, or {} if none/unreadable."""
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {}
    return data if isinstance(data, dict) else {}


def save(data: dict) -> None:
    """Write the settings dict atomically (temp file + rename)."""
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
