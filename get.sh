#!/usr/bin/env bash
# =============================================================================
# serai — one-line installer
# =============================================================================
#
#   bash <(curl -fsSL https://raw.githubusercontent.com/preston-peterson/serai/main/get.sh)
#
# Any flags are passed straight through to install.sh, so everything it
# understands works here too:
#
#   bash <(curl -fsSL .../get.sh) --hostname serai.lan,192.0.2.5
#   bash <(curl -fsSL .../get.sh) --system
#   bash <(curl -fsSL .../get.sh) --dry-run
#
# What it does:
#   1. Checks this machine can actually run serai (Linux, python3 >= 3.10 with
#      the venv module, tmux, ssh, systemd).
#   2. Finds the latest GitHub release.
#   3. Downloads that release's source tarball and verifies it against the
#      release's published sha256 — and refuses to continue if it doesn't match.
#   4. Extracts it to a temp directory and runs ./install.sh, passing your
#      arguments through.
#
# Installing from a clone stays a `git clone && ./install.sh` away; this exists
# so a fresh box needs one line and still gets a verified download.
#
# Forks: set SERAI_REPO=owner/name.
# =============================================================================

set -euo pipefail

REPO="${SERAI_REPO:-preston-peterson/serai}"

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
CYAN='\033[36m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'

say()  { echo -e "${CYAN}[get.sh]${RESET} $*"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
fail() { echo -e "${RED}[get.sh] Error:${RESET} $*" >&2; exit 1; }

CLONE_HINT="install from a clone instead:
  git clone https://github.com/${REPO}.git && cd serai && ./install.sh"

# --- preflight ---------------------------------------------------------------
# Fail here, with the fix, rather than three minutes into an install.

[ "$(uname -s)" = "Linux" ] \
    || fail "serai installs as a systemd service and runs on Linux (got: $(uname -s))"

for tool in curl tar sha256sum; do
    command -v "$tool" >/dev/null 2>&1 && continue
    pkg="$tool"; [ "$tool" = "sha256sum" ] && pkg="coreutils"
    fail "missing required tool: ${tool}
  sudo apt install ${pkg}      (Debian/Ubuntu)
  sudo dnf install ${pkg}      (Fedora/RHEL)"
done

command -v python3 >/dev/null 2>&1 \
    || fail "python3 not found — serai needs Python 3.10 or newer
  sudo apt install python3      (Debian/Ubuntu)
  sudo dnf install python3      (Fedora/RHEL)"

# `|| true` is load-bearing: under `set -o pipefail` a broken python3 makes this
# pipeline non-zero, and a failing assignment under `set -e` would abort the
# script with no message at all -- silently, right where we mean to explain.
PY_VER="$(python3 -V 2>&1 | awk '{print $2}' || true)" || true
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "serai needs Python 3.10 or newer${PY_VER:+ (this is ${PY_VER})}"

# Debian and Ubuntu ship venv/ensurepip in a separate package, and the failure
# without it happens inside install.sh with a much less obvious message.
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 \
    || fail "python3 is missing the venv module — install.sh cannot create its virtualenv
  sudo apt install python3-venv      (Debian/Ubuntu)
  sudo dnf install python3-libs      (Fedora/RHEL)"

command -v systemctl >/dev/null 2>&1 \
    || fail "systemd not found — install.sh installs and starts a systemd unit.
serai itself runs fine without it (./run.sh), but this installer needs it. ${CLONE_HINT}"

# tmux is the persistence substrate and ssh is how remote hosts are reached.
# Neither is needed to *install*, so warn rather than stop.
command -v tmux >/dev/null 2>&1 || warn "tmux not found — serai needs it to hold sessions: sudo apt install tmux"
command -v ssh  >/dev/null 2>&1 || warn "ssh not found — remote hosts will be unreachable: sudo apt install openssh-client"

ok "$(python3 -V 2>&1), $(uname -s) $(uname -m)"

# --- find the latest release -------------------------------------------------

say "looking up the latest release of ${REPO}…"
# Fetch fully, then parse. `curl | grep -m1` makes curl die on a closed pipe,
# which `set -o pipefail` then reports as a download failure.
RELEASE_JSON=$(curl -fsSL -H 'Accept: application/vnd.github+json' \
        "https://api.github.com/repos/${REPO}/releases/latest") \
    || fail "could not reach the GitHub API (offline, or ${REPO} has no releases yet)"

TAG=$(printf '%s' "$RELEASE_JSON" | grep -m1 '"tag_name"' \
        | sed -E 's/.*"tag_name"[^"]*"([^"]+)".*/\1/' || true)
[ -n "$TAG" ] || fail "could not determine the latest release tag of ${REPO}"
VER="${TAG#v}"
ok "latest release: ${TAG}"

# --- download and verify -----------------------------------------------------

WORKDIR=$(mktemp -d /tmp/serai-get.XXXXXX)
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"

DL="https://github.com/${REPO}/releases/download/${TAG}"
TARBALL="serai_${VER}.tar.gz"
SUMS="serai_${VER}_checksums.txt"

say "downloading ${TARBALL}…"
curl -fsSL -o "$TARBALL" "${DL}/${TARBALL}" || fail "download failed: ${DL}/${TARBALL}
That release may predate the verified tarballs. ${CLONE_HINT}"
curl -fsSL -o "$SUMS" "${DL}/${SUMS}" || fail "download failed: ${DL}/${SUMS}
The release is missing its checksums file, so the download cannot be verified.
Refusing to install unverified code. ${CLONE_HINT}"

say "verifying sha256…"
# Check the sums file actually covers this file, so a mismatch is a real
# failure rather than sha256sum finding nothing to do and exiting happy.
grep -q " ${TARBALL}\$" "$SUMS" \
    || fail "${SUMS} has no entry for ${TARBALL} — refusing to install"
grep " ${TARBALL}\$" "$SUMS" | sha256sum -c - >/dev/null 2>&1 \
    || fail "sha256 verification FAILED for ${TARBALL} — refusing to install.
The download was corrupted or tampered with. Try again; if it persists, open an
issue at https://github.com/${REPO}/issues"
ok "$(cut -d' ' -f1 "$SUMS")"

say "extracting…"
tar -xzf "$TARBALL"
SRC_DIR="${WORKDIR}/serai-${VER}"
[ -d "$SRC_DIR" ] || fail "unexpected layout in ${TARBALL} (no serai-${VER}/)"
[ -f "${SRC_DIR}/install.sh" ] || fail "install.sh missing from ${TARBALL}"
chmod +x "${SRC_DIR}/install.sh" 2>/dev/null || true
ok "serai ${VER}"

# --- hand off to install.sh --------------------------------------------------

echo
say "running install.sh${*:+ $*}…"
cd "$SRC_DIR"
if [ -t 0 ]; then
    # `bash <(curl ...)` -- stdin is still the terminal, prompts just work.
    ./install.sh "$@"
elif (exec < /dev/tty) 2>/dev/null; then
    # `curl | bash` -- stdin is the script itself, so reattach the terminal or
    # install.sh's questions (and any sudo prompt) would read the script body.
    ./install.sh "$@" < /dev/tty
else
    # No terminal at all (CI, cloud-init): fine, provided every answer it needs
    # was passed as a flag.
    ./install.sh "$@" || fail "install.sh needs a terminal for its prompts.
Run get.sh from a terminal, or pass the answers as flags (--hostname …)."
fi
