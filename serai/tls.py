"""TLS for serai: a self-signed cert by default, bring-your-own when you have one.

serai now carries a login, so it should not send passwords over plain http on a
network. This module resolves the cert/key pair to serve with:

  * SERAI_CERT + SERAI_KEY set  -> use them (your own cert -- a real domain, your
    homelab CA, whatever). Both are PEM paths and must exist.
  * otherwise                   -> a self-signed cert, generated once and cached
    under ~/.config/serai/ (cert.pem + key.pem). Its SANs cover localhost, this
    host, its primary LAN IP, and whatever you set in SERAI_HOST / SERAI_HOSTNAME
    (a comma-separated list of names/IPs) -- so reaching serai at a LAN name like
    serai.home.example doesn't throw a name-mismatch. It's regenerated
    automatically when those change. Browsers still warn it's untrusted (accept
    it once, or install it / drop in your own cert).

run.sh calls `python -m serai.tls`, which prints the cert path then the key path
(generating the self-signed pair on first run) for uvicorn's
--ssl-certfile/--ssl-keyfile. Set SERAI_TLS=off to serve plain http instead --
sane only on a trusted localhost, or when a reverse proxy terminates TLS for you
(in which case it should forward X-Forwarded-Proto so the session cookie still
gets its Secure flag).

Cert generation uses `cryptography`, which serai already pulls in via paramiko.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import socket
import sys
from pathlib import Path


def _config_dir() -> Path:
    override = os.environ.get("SERAI_CONFIG_DIR")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "serai"


def tls_enabled() -> bool:
    """TLS is on unless SERAI_TLS is explicitly an off-ish value."""
    return os.environ.get("SERAI_TLS", "").strip().lower() not in ("off", "0", "false", "no")


def resolve() -> tuple[Path, Path]:
    """Return (cert_path, key_path) to serve with, generating a self-signed pair
    on first run if the operator hasn't supplied their own."""
    cert = os.environ.get("SERAI_CERT")
    key = os.environ.get("SERAI_KEY")
    if cert or key:
        if not (cert and key):
            raise SystemExit("set both SERAI_CERT and SERAI_KEY (or neither)")
        c, k = Path(cert).expanduser(), Path(key).expanduser()
        missing = [str(p) for p in (c, k) if not p.exists()]
        if missing:
            raise SystemExit("SERAI_CERT/SERAI_KEY not found: " + ", ".join(missing))
        return c, k

    d = _config_dir()
    c, k = d / "cert.pem", d / "key.pem"
    entries = _desired_san_entries()
    # (re)generate if missing, or if the operator added a name/IP the existing
    # cert doesn't cover (e.g. set SERAI_HOST / SERAI_HOSTNAME and restart).
    if not (c.exists() and k.exists()) or not _cert_covers(c, entries):
        _generate_self_signed(c, k, entries)
    return c, k


def _classify(value: str) -> tuple[str, str] | None:
    """('ip', v) for an IP literal, ('dns', lowercased) for a name, None for blanks."""
    value = value.strip()
    if not value:
        return None
    try:
        ipaddress.ip_address(value)
        return ("ip", value)
    except ValueError:
        return ("dns", value.lower())


def _primary_ip() -> str | None:
    """Best-effort primary outbound IPv4 of this host (no packet is sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))   # TEST-NET-1; only selects a route
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        return None


def _desired_san_entries() -> list[tuple[str, str]]:
    """Names/IPs the self-signed cert should cover: always localhost + this host
    + its primary LAN IP, plus the bind host (SERAI_HOST) and anything in
    SERAI_HOSTNAME (comma-separated DNS names and/or IPs)."""
    raw = ["localhost", "127.0.0.1", "::1", socket.gethostname()]
    ip = _primary_ip()
    if ip:
        raw.append(ip)
    host = os.environ.get("SERAI_HOST", "").strip()
    if host and host not in ("0.0.0.0", "::", "*"):
        raw.append(host)
    raw += os.environ.get("SERAI_HOSTNAME", "").split(",")
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for v in raw:
        ent = _classify(v)
        if ent and ent not in seen:
            seen.add(ent)
            out.append(ent)
    return out


def cert_sans(cert_path: Path | None = None) -> list[str]:
    """The DNS names / IPs the active self-signed cert currently covers (for the
    UI to show what's validated). Empty if there's no readable cert."""
    if cert_path is None:
        cert_path = _config_dir() / "cert.pem"
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return ([str(d) for d in san.get_values_for_type(x509.DNSName)]
                + [str(ip) for ip in san.get_values_for_type(x509.IPAddress)])
    except Exception:
        return []


def primary_ip() -> str | None:
    """Public wrapper for the best-effort primary LAN IP (UI hint)."""
    return _primary_ip()


def _cert_covers(cert_path: Path, entries: list[tuple[str, str]]) -> bool:
    """True if the existing cert's SANs already include every desired entry."""
    try:
        from cryptography import x509
        cert = x509.load_pem_x509_certificate(cert_path.read_bytes())
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        have = {("dns", d.lower()) for d in san.get_values_for_type(x509.DNSName)}
        have |= {("ip", str(ip)) for ip in san.get_values_for_type(x509.IPAddress)}
        return all(e in have for e in entries)
    except Exception:
        return False


def _generate_self_signed(cert_path: Path, key_path: Path,
                          entries: list[tuple[str, str]] | None = None) -> None:
    """Write a long-lived self-signed cert+key, the key at mode 0600. SANs come
    from _desired_san_entries() (localhost + this host + SERAI_HOST/HOSTNAME) so
    name-checks pass once the cert is trusted; for a CA-signed cert, bring your
    own with SERAI_CERT/SERAI_KEY."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    if entries is None:
        entries = _desired_san_entries()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "serai")])
    sans: list[x509.GeneralName] = [
        x509.IPAddress(ipaddress.ip_address(v)) if kind == "ip" else x509.DNSName(v)
        for kind, v in entries
    ]
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    ktmp = key_path.with_name(key_path.name + ".tmp")
    ktmp.write_bytes(key_pem)
    os.chmod(ktmp, 0o600)
    ktmp.replace(key_path)
    ctmp = cert_path.with_name(cert_path.name + ".tmp")
    ctmp.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    ctmp.replace(cert_path)
    # to stderr: stdout is reserved for the two paths run.sh reads.
    names = ", ".join(v for _, v in entries)
    print(f"[serai] generated a self-signed TLS cert at {cert_path}\n"
          f"        covering: {names}\n"
          "        (untrusted -- accept it once, or set SERAI_CERT/SERAI_KEY to "
          "your own).", file=sys.stderr, flush=True)


if __name__ == "__main__":
    cert_path, key_path = resolve()
    # run.sh reads these two lines; keep stdout to exactly the paths.
    print(cert_path)
    print(key_path)
