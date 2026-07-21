"""Parse ~/.ssh/config into a list of hosts, reading optional grouping metadata
from structured comments so the sidebar can organize by project or tag.

Grouping lives in the ssh config itself, as comments ssh ignores:

    # @group web-stack
    # @tags prod,docker
    Host proj-web
        HostName 192.0.2.21
        User youruser

The annotation may sit directly above the `Host` line or inside the block.
This keeps one source of truth shared with your terminal and Gigolo -- they
just skip the comments.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, asdict

DEFAULT_PATH = os.path.expanduser("~/.ssh/config")

_GROUP_RE = re.compile(r"#\s*@group\s+(.+)", re.IGNORECASE)
_TAGS_RE = re.compile(r"#\s*@tags\s+(.+)", re.IGNORECASE)


@dataclass
class Host:
    alias: str
    hostname: str = ""
    user: str = ""
    port: int = 22
    group: str = "ungrouped"
    tags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def parse_ssh_config(path: str = DEFAULT_PATH) -> list[Host]:
    if not os.path.exists(path):
        return []

    hosts: list[Host] = []
    current: Host | None = None
    pending_group: str | None = None
    pending_tags: list[str] = []

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.strip()
            if not line:
                # A blank line ends the current stanza, so an annotation after it
                # is held for the next Host rather than the one above. (Keep your
                # Host blocks free of internal blank lines, as ssh configs usually
                # are.)
                current = None
                continue

            if line.startswith("#"):
                gm = _GROUP_RE.match(line)
                if gm:
                    value = gm.group(1).strip()
                    if current is not None:
                        current.group = value
                    else:
                        pending_group = value
                tm = _TAGS_RE.match(line)
                if tm:
                    tags = [t.strip() for t in tm.group(1).split(",") if t.strip()]
                    if current is not None:
                        current.tags = tags
                    else:
                        pending_tags = tags
                continue

            key, _, value = line.partition(" ")
            key = key.lower().strip()
            value = value.strip()

            if key == "host":
                # A Host line can list several patterns; take the first concrete
                # alias and skip wildcards like `*` or `web-*`.
                alias = next(
                    (a for a in value.split() if "*" not in a and "?" not in a),
                    None,
                )
                if alias is None:
                    current = None
                else:
                    current = Host(
                        alias=alias,
                        group=pending_group or "ungrouped",
                        tags=list(pending_tags),
                    )
                    hosts.append(current)
                pending_group = None
                pending_tags = []
            elif current is not None:
                if key == "hostname":
                    current.hostname = value
                elif key == "user":
                    current.user = value
                elif key == "port":
                    try:
                        current.port = int(value)
                    except ValueError:
                        pass

    return hosts


# Tight charsets for add_host(). fullmatch (not ^..$) so a trailing newline
# can't slip past a `$` anchor. These values are written into ~/.ssh/config, so
# an unchecked newline could smuggle another ssh directive (e.g. ProxyCommand ->
# local command execution) -- reject anything off-charset.
_ALIAS_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_HOST_RE = re.compile(r"[A-Za-z0-9._:-]{1,253}")   # hostname or IP (IPv6 has ':')
_USER_RE = re.compile(r"[A-Za-z0-9._-]{1,63}")
_GROUP_OK = re.compile(r"[A-Za-z0-9 ._-]{1,64}")
_TAG_OK = re.compile(r"[A-Za-z0-9._-]{1,32}")


def add_host(alias, hostname="", user="", port=22, group="", tags=None,
             path: str = DEFAULT_PATH) -> Host:
    """Append a `Host` block to the ssh config and return the new Host.

    Every field is validated to a tight charset so an injected newline can't
    write another ssh directive into the file. No credentials are stored -- only
    connection metadata; auth stays the ssh-agent (invariant #1) and the ssh
    config remains the single source of truth (invariant #6). Raises ValueError
    on bad input or a duplicate alias.
    """
    alias = (alias or "").strip()
    hostname = (hostname or "").strip()
    user = (user or "").strip()
    group = (group or "").strip()
    tags = tags or []

    if not _ALIAS_RE.fullmatch(alias):
        raise ValueError("alias must be letters/digits/._- (no spaces or wildcards)")
    if hostname and not _HOST_RE.fullmatch(hostname):
        raise ValueError("invalid hostname")
    if user and not _USER_RE.fullmatch(user):
        raise ValueError("invalid user")
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("port must be a number")
    if not 1 <= port <= 65535:
        raise ValueError("port must be 1-65535")
    if group and not _GROUP_OK.fullmatch(group):
        raise ValueError("invalid group")
    clean_tags = [t for t in (str(t).strip() for t in tags) if _TAG_OK.fullmatch(t)]

    if any(h.alias == alias for h in parse_ssh_config(path)):
        raise ValueError(f"host '{alias}' already exists")

    lines: list[str] = []
    if group:
        lines.append(f"# @group {group}")
    if clean_tags:
        lines.append(f"# @tags {','.join(clean_tags)}")
    lines.append(f"Host {alias}")
    if hostname:
        lines.append(f"    HostName {hostname}")
    if user:
        lines.append(f"    User {user}")
    if port != 22:
        lines.append(f"    Port {port}")
    block = "\n".join(lines) + "\n"

    p = os.path.expanduser(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    existing = ""
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8", errors="replace") as fh:
            existing = fh.read()
    with open(p, "a", encoding="utf-8") as fh:
        if existing:
            if not existing.endswith("\n"):
                fh.write("\n")
            fh.write("\n")  # blank line so the @group/@tags bind to THIS host
        fh.write(block)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass

    return Host(alias=alias, hostname=hostname, user=user, port=port,
                group=group or "ungrouped", tags=clean_tags)


if __name__ == "__main__":
    for h in parse_ssh_config():
        print(f"{h.group:<14} {h.alias:<16} tags={','.join(h.tags)}")
