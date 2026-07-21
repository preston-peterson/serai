"""Read/write serai's network settings (listen address + cert hostnames) in the
service env file, and restart the service to apply them.

The web UI (admin only) edits these so an operator can change the DNS name/IP
they reach serai by -- and have the TLS cert reissued to cover it -- without
touching a terminal. The flow: write SERAI_HOST / SERAI_HOSTNAME into the env
file the systemd unit sources, then restart; on start, tls.py regenerates the
self-signed cert to cover the new names (see serai.tls).

Values are validated to a tight charset before they're written: they land in an
env file the service sources, so an unchecked newline or '=' could otherwise
inject another SERAI_* setting (e.g. flip auth off). The env file path mirrors
the rest of ~/.config/serai/ and is overridable with SERAI_ENV_FILE (tests).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from . import tls

# DNS label / IP charset. No spaces, '=', or newlines -> can't inject env lines.
# fullmatch (not ^..$) so a trailing newline can't slip through the $ anchor.
_NAME_RE = re.compile(r"[A-Za-z0-9._:-]{1,253}")
_BIND_SPECIALS = {"0.0.0.0", "::", "::1", "127.0.0.1", "localhost"}


def env_path() -> Path:
    override = os.environ.get("SERAI_ENV_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "serai" / "serai.env"


def _parse() -> dict:
    out: dict[str, str] = {}
    try:
        for line in env_path().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except (FileNotFoundError, OSError):
        pass
    return out


def valid_host(host: str) -> bool:
    return bool(host) and (host in _BIND_SPECIALS or _NAME_RE.fullmatch(host) is not None)


def clean_hostnames(raw: str) -> list[str]:
    """Split a comma list into validated names/IPs, dropping anything unsafe."""
    out: list[str] = []
    for part in (raw or "").replace(" ", "").split(","):
        if part and _NAME_RE.fullmatch(part) and part not in out:
            out.append(part)
    return out


def read() -> dict:
    """Current settings + context for the UI."""
    env = _parse()
    return {
        "host": env.get("SERAI_HOST", "127.0.0.1"),
        "hostnames": env.get("SERAI_HOSTNAME", ""),
        "port": env.get("SERAI_PORT", "8022"),
        "tls": env.get("SERAI_TLS", "on"),
        "detected_ip": tls.primary_ip(),
        "cert_sans": tls.cert_sans(),
        "service": service_mode(),
        "env_file": str(env_path()),
    }


def write(host: str, hostnames: str) -> list[str]:
    """Update SERAI_HOST / SERAI_HOSTNAME in the env file, preserving everything
    else. Returns the cleaned hostname list. Raises ValueError on a bad host."""
    if not valid_host(host):
        raise ValueError("invalid listen address")
    names = clean_hostnames(hostnames)
    path = env_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        lines = []

    set_host = set_names = False
    out: list[str] = []
    for line in lines:
        s = line.strip()
        if s.startswith("SERAI_HOST="):
            out.append(f"SERAI_HOST={host}")
            set_host = True
        elif s.startswith("SERAI_HOSTNAME="):
            out.append("SERAI_HOSTNAME=" + ",".join(names))
            set_names = True
        else:
            out.append(line)
    if not set_host:
        out.append(f"SERAI_HOST={host}")
    if not set_names:
        out.append("SERAI_HOSTNAME=" + ",".join(names))

    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return names


def service_mode() -> str | None:
    """'user' / 'system' / None -- how serai is being managed, so the UI knows
    whether it can restart itself to apply changes."""
    if (Path(os.path.expanduser("~/.config/systemd/user/serai.service"))).exists():
        return "user"
    if Path("/etc/systemd/system/serai.service").exists():
        return "system"
    return None


def restart() -> dict:
    """Restart the service so new settings + cert take effect. --no-block so the
    HTTP response can flush before systemd tears this process down."""
    mode = service_mode()
    if mode == "user":
        cmd = ["systemctl", "--user", "restart", "--no-block", "serai.service"]
    elif mode == "system":
        cmd = ["sudo", "-n", "systemctl", "restart", "--no-block", "serai.service"]
    else:
        return {"ok": False, "mode": None,
                "reason": "serai isn't running as a systemd service -- restart it yourself to apply."}
    if not shutil.which(cmd[0]):
        return {"ok": False, "mode": mode, "reason": f"{cmd[0]} not found"}
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=10)
        return {"ok": True, "mode": mode}
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or b"").decode(errors="replace").strip()
        return {"ok": False, "mode": mode, "reason": msg or "restart command failed"}
    except Exception as exc:  # timeout, etc.
        return {"ok": False, "mode": mode, "reason": str(exc)}
