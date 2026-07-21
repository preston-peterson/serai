"""Serai authentication -- a small local user store and signed session cookies.

This is the ONE credential serai stores of its own, and it exists because serai
is a single attach point into your whole fleet: an unauthenticated port would be
a shell on every host you can reach. So, under ~/.config/serai/, this module
holds:

  * users.json   -- per-user password *hashes* (stdlib scrypt + a random salt);
                    never the plaintext, never your remote secrets.
  * auth_secret  -- a random key generated once, used to HMAC-sign the session
                    cookie so a logged-in session can't be forged.

It still never stores your ssh keys or remote passwords -- remote auth stays
ssh-agent-only (the remote half of invariant #1 is intact). Set SERAI_AUTH=off
to disable auth entirely, which is only sane on a trusted localhost.

Storage dir mirrors settings.py:
  $SERAI_CONFIG_DIR, else $XDG_CONFIG_HOME/serai, else ~/.config/serai.

stdlib only -- no new runtime dependency (scrypt is memory-hard and ships with
hashlib; cookie signing is hmac/sha256).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from pathlib import Path

COOKIE = "serai_session"

try:
    SESSION_TTL = int(os.environ.get("SERAI_SESSION_TTL") or 30 * 24 * 3600)
except ValueError:
    SESSION_TTL = 30 * 24 * 3600

# scrypt cost. n=2**14,r=8,p=1 needs ~16 MB and a few ms -- comfortably under
# OpenSSL's 32 MB default maxmem, and interactive-fast.
_SCRYPT = {"n": 1 << 14, "r": 8, "p": 1, "dklen": 32}
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,32}$")
_MIN_PASSWORD = 8

# One-time bootstrap code, printed to the server console while no users exist so
# the first (admin) user can be created from the web UI without a CLI. In-memory
# only: it rotates on every restart and never touches disk.
_setup_token: str | None = None


# --- paths -----------------------------------------------------------------

def _config_dir() -> Path:
    override = os.environ.get("SERAI_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "serai"


def _users_path() -> Path:
    return _config_dir() / "users.json"


def _secret_path() -> Path:
    return _config_dir() / "auth_secret"


def auth_enabled() -> bool:
    """Auth is on unless SERAI_AUTH is explicitly an off-ish value."""
    return os.environ.get("SERAI_AUTH", "").strip().lower() not in ("off", "0", "false", "no")


# --- user store ------------------------------------------------------------

def _load() -> dict:
    try:
        data = json.loads(_users_path().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return {"users": {}}
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        return {"users": {}}
    return data


def _save(data: dict) -> None:
    """Atomic write (temp + rename), 0600 -- the file holds password hashes."""
    path = _users_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def _hash(password: str, salt: bytes, params: dict | None = None) -> bytes:
    p = params or _SCRYPT
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=int(p["n"]), r=int(p["r"]), p=int(p["p"]), dklen=int(p["dklen"]),
    )


def _make_record(password: str, admin: bool) -> dict:
    salt = secrets.token_bytes(16)
    return {
        "salt": salt.hex(),
        "hash": _hash(password, salt).hex(),
        "n": _SCRYPT["n"], "r": _SCRYPT["r"], "p": _SCRYPT["p"],
        "admin": bool(admin),
        "created": int(time.time()),
    }


def _verify(password: str, rec: dict) -> bool:
    try:
        salt = bytes.fromhex(rec["salt"])
        expected = bytes.fromhex(rec["hash"])
        got = _hash(password, salt, {
            "n": rec.get("n", _SCRYPT["n"]), "r": rec.get("r", _SCRYPT["r"]),
            "p": rec.get("p", _SCRYPT["p"]), "dklen": len(expected),
        })
    except (KeyError, ValueError):
        return False
    return hmac.compare_digest(got, expected)


def list_users() -> list[dict]:
    """Usernames + admin flag only -- never the hashes."""
    users = _load()["users"]
    return sorted(
        ({"username": u, "admin": bool(r.get("admin"))} for u, r in users.items()),
        key=lambda d: d["username"],
    )


def user_count() -> int:
    return len(_load()["users"])


def is_configured() -> bool:
    return user_count() > 0


def get_user(username: str) -> dict | None:
    return _load()["users"].get(username)


def add_user(username: str, password: str, admin: bool = False) -> None:
    if not _USERNAME_RE.match(username or ""):
        raise ValueError("username must be 1-32 chars of letters, digits, dot, dash, underscore")
    if len(password or "") < _MIN_PASSWORD:
        raise ValueError(f"password must be at least {_MIN_PASSWORD} characters")
    data = _load()
    if username in data["users"]:
        raise ValueError("user already exists")
    data["users"][username] = _make_record(password, admin)
    _save(data)


def set_password(username: str, password: str) -> None:
    if len(password or "") < _MIN_PASSWORD:
        raise ValueError(f"password must be at least {_MIN_PASSWORD} characters")
    data = _load()
    rec = data["users"].get(username)
    if not rec:
        raise ValueError("no such user")
    new = _make_record(password, rec.get("admin", False))
    new["created"] = rec.get("created", new["created"])
    data["users"][username] = new
    _save(data)


def remove_user(username: str) -> None:
    data = _load()
    if username not in data["users"]:
        raise ValueError("no such user")
    # Never leave the system with zero admins -- that would lock everyone out of
    # user management with no in-UI way back.
    others_admin = [u for u, r in data["users"].items() if u != username and r.get("admin")]
    if data["users"][username].get("admin") and not others_admin:
        raise ValueError("cannot remove the last admin")
    del data["users"][username]
    _save(data)


def verify_credentials(username: str, password: str) -> dict | None:
    """Return {username, admin} on a correct password, else None."""
    rec = get_user(username)
    if rec is None:
        # Run a throwaway hash so a missing user isn't measurably faster than a
        # wrong password (don't leak which usernames exist).
        _hash(password, b"\0" * 16)
        return None
    if _verify(password, rec):
        return {"username": username, "admin": bool(rec.get("admin"))}
    return None


# --- session cookie (HMAC-signed, stateless) -------------------------------

def _secret() -> bytes:
    path = _secret_path()
    try:
        return bytes.fromhex(path.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        pass
    key = secrets.token_bytes(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(key.hex(), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return key


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: str) -> str:
    return _b64e(hmac.new(_secret(), payload.encode("ascii"), hashlib.sha256).digest())


def issue_token(username: str, ttl: int = SESSION_TTL) -> str:
    payload = _b64e(json.dumps(
        {"u": username, "exp": int(time.time()) + ttl}, separators=(",", ":"),
    ).encode("utf-8"))
    return f"{payload}.{_sign(payload)}"


def verify_token(token: str | None) -> dict | None:
    """Return {username, admin} for a valid, unexpired cookie, else None.

    Rejects on a bad/forged signature, expiry, or a user that no longer exists
    (so removing a user immediately invalidates their outstanding cookies).
    """
    if not token or "." not in token:
        return None
    payload, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    try:
        data = json.loads(_b64d(payload))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or int(data.get("exp", 0)) < int(time.time()):
        return None
    username = data.get("u")
    rec = get_user(username) if isinstance(username, str) else None
    if rec is None:
        return None
    return {"username": username, "admin": bool(rec.get("admin"))}


# --- first-run bootstrap ---------------------------------------------------

def setup_requires_code() -> bool:
    """Whether creating the first admin needs the printed setup code.

    Off by default: the first person to open serai creates the admin account
    (trust-on-first-use, like most self-hosted apps). The only window that opens
    is between the service starting with no users and you creating that account
    -- so set it up promptly. Opt into the stricter code-gated flow with
    SERAI_SETUP_CODE=1 when serai sits exposed and that window is a concern. The
    permanent guard is unaffected: once any user exists, /api/setup is closed.
    """
    return os.environ.get("SERAI_SETUP_CODE", "").strip().lower() in (
        "1", "on", "true", "yes", "code", "require")


def get_setup_token() -> str:
    global _setup_token
    if _setup_token is None:
        _setup_token = secrets.token_urlsafe(18)
    return _setup_token


def check_setup_token(token: str) -> bool:
    return bool(token) and hmac.compare_digest(token, get_setup_token())


def startup_banner() -> None:
    """Print auth state on boot; the one-time setup code only in code mode."""
    if not auth_enabled():
        print("[serai] auth DISABLED (SERAI_AUTH=off) -- the app is OPEN. "
              "Only do this on a trusted localhost.", flush=True)
        return
    if is_configured():
        return
    if setup_requires_code():
        print(
            "\n[serai] No users yet. Open serai and create the first (admin) user\n"
            "        with this one-time setup code:\n\n"
            f"            {get_setup_token()}\n\n"
            "        (rotates each restart; finish setup before exposing serai.)\n",
            flush=True,
        )
    else:
        print("[serai] No users yet -- open serai in a browser to create the "
              "first (admin) account. Do it promptly if serai is reachable on "
              "your network.", flush=True)


# --- recovery CLI ----------------------------------------------------------
# Management is meant to happen in the web UI; this is a break-glass tool for a
# lockout (e.g. reset a forgotten password). `python -m serai.auth`.

def _cli(argv: list[str]) -> int:
    import getpass

    def _prompt_pw() -> str:
        pw = getpass.getpass("password: ")
        if pw != getpass.getpass("confirm:  "):
            print("passwords do not match")
            raise SystemExit(1)
        return pw

    cmd = argv[0] if argv else "list"
    try:
        if cmd == "list":
            for u in list_users():
                print(f"{'admin' if u['admin'] else 'user '}  {u['username']}")
        elif cmd in ("add", "passwd", "rm") and len(argv) >= 2:
            user = argv[1]
            if cmd == "add":
                add_user(user, _prompt_pw(), admin="--admin" in argv)
            elif cmd == "passwd":
                set_password(user, _prompt_pw())
            else:
                remove_user(user)
            print(f"ok: {cmd} {user}")
        else:
            print("usage: python -m serai.auth [list | add <user> [--admin] | "
                  "passwd <user> | rm <user>]")
            return 2
    except ValueError as exc:
        print(f"error: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_cli(sys.argv[1:]))
