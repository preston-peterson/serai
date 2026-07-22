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
# EVERY requirement is checked before ANY is reported. Failing on the first
# missing package turns a fresh box into a loop: install one thing, re-run,
# discover the next. One report, one command, one re-run.

[ "$(uname -s)" = "Linux" ] \
    || fail "serai installs as a systemd service and runs on Linux (got: $(uname -s))"

PKG_FAMILY="unknown"; PKG_REFRESH=""; PKG_INSTALL=""
pkg_detect() {
    local id="" like=""
    if [ -r /etc/os-release ]; then
        id="$(. /etc/os-release 2>/dev/null && printf '%s' "${ID:-}")" || true
        like="$(. /etc/os-release 2>/dev/null && printf '%s' "${ID_LIKE:-}")" || true
    fi
    case "$id" in
        debian|ubuntu|raspbian|linuxmint|pop|elementary|devuan) PKG_FAMILY=debian ;;
        fedora|rhel|centos|rocky|almalinux)                     PKG_FAMILY=fedora ;;
        arch|manjaro|endeavouros)                               PKG_FAMILY=arch ;;
        opensuse*|sles|sled)                                    PKG_FAMILY=opensuse ;;
        *) case " $like " in
               *" debian "*|*" ubuntu "*)                 PKG_FAMILY=debian ;;
               *" fedora "*|*" rhel "*|*" centos "*)      PKG_FAMILY=fedora ;;
               *" arch "*)                                PKG_FAMILY=arch ;;
               *" suse "*|*" opensuse "*)                 PKG_FAMILY=opensuse ;;
           esac ;;
    esac
    case "$PKG_FAMILY" in
        debian)   PKG_REFRESH="sudo apt-get update -qq"
                  PKG_INSTALL="sudo apt-get install -y" ;;
        fedora)   PKG_INSTALL="sudo dnf install -y" ;;
        arch)     PKG_REFRESH="sudo pacman -Sy --noconfirm"
                  PKG_INSTALL="sudo pacman -S --needed --noconfirm" ;;
        opensuse) PKG_REFRESH="sudo zypper --non-interactive refresh"
                  PKG_INSTALL="sudo zypper --non-interactive install" ;;
    esac
}

# Package names differ per family. An empty answer means "already part of
# python3 on this distro" -- only Debian-likes split venv/ensurepip out.
pkg_for() {
    case "$1:$PKG_FAMILY" in
        venv:debian)      echo "python3-venv" ;;
        venv:*)           echo "" ;;
        python3:arch)     echo "python" ;;
        python3:*)        echo "python3" ;;
        ssh:debian)       echo "openssh-client" ;;
        ssh:arch)         echo "openssh" ;;
        ssh:*)            echo "openssh-clients" ;;
        sha256sum:*)      echo "coreutils" ;;
        *)                echo "$1" ;;
    esac
}

NEED_PKGS=(); MISSING=(); BLOCKERS=()

run_checks() {
    NEED_PKGS=(); MISSING=(); BLOCKERS=()
    local t pkg
    for t in curl tar sha256sum; do
        command -v "$t" >/dev/null 2>&1 && continue
        MISSING+=("$t"); pkg="$(pkg_for "$t")"; [ -n "$pkg" ] && NEED_PKGS+=("$pkg")
    done

    if ! command -v python3 >/dev/null 2>&1; then
        MISSING+=("python3 (>= 3.10)")
        NEED_PKGS+=("$(pkg_for python3)")
        pkg="$(pkg_for venv)"; [ -n "$pkg" ] && NEED_PKGS+=("$pkg")   # ships apart on Debian
    else
        # `|| true` twice is load-bearing: under `set -o pipefail` a broken
        # python3 makes this pipeline non-zero, and a failing assignment under
        # `set -e` aborts the script silently -- exactly where it should explain.
        local py_ver; py_ver="$(python3 -V 2>&1 | awk '{print $2}' || true)" || true
        if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
            # installing a package won't fix this -- the distro's python3 IS this
            BLOCKERS+=("Python 3.10 or newer is required${py_ver:+ (this system has ${py_ver})}")
        elif ! python3 -c 'import venv, ensurepip' >/dev/null 2>&1; then
            MISSING+=("the python3 venv module")
            pkg="$(pkg_for venv)"
            if [ -n "$pkg" ]; then NEED_PKGS+=("$pkg")
            else BLOCKERS+=("python3 cannot create virtualenvs and this distro has no separate venv package"); fi
        fi
    fi

    # tmux holds every session -- serai does nothing useful without it.
    command -v tmux >/dev/null 2>&1 || { MISSING+=("tmux"); NEED_PKGS+=("$(pkg_for tmux)"); }
    # ssh is only needed to reach REMOTE hosts; local-only installs work without.
    command -v ssh  >/dev/null 2>&1 || { MISSING+=("ssh (for remote hosts)"); NEED_PKGS+=("$(pkg_for ssh)"); }

    command -v systemctl >/dev/null 2>&1 \
        || BLOCKERS+=("systemd is required: install.sh writes and starts a unit")

    # de-duplicate, preserving order
    if [ ${#NEED_PKGS[@]} -gt 0 ]; then
        local seen=" " out=() p
        for p in "${NEED_PKGS[@]}"; do
            [ -z "$p" ] && continue
            case "$seen" in *" $p "*) continue ;; esac
            seen="${seen}${p} "; out+=("$p")
        done
        NEED_PKGS=(${out[@]+"${out[@]}"})
    fi
}

pkg_detect
run_checks

if [ ${#BLOCKERS[@]} -gt 0 ] || [ ${#MISSING[@]} -gt 0 ]; then
    echo -e "${YELLOW}[get.sh]${RESET} this machine isn't ready yet:"
    for b in ${BLOCKERS[@]+"${BLOCKERS[@]}"}; do echo -e "  ${RED}✗${RESET} $b"; done
    for m in ${MISSING[@]+"${MISSING[@]}"};  do echo -e "  ${RED}✗${RESET} missing: $m"; done
    echo
fi

# Things no package install can fix -- stop, don't offer.
if [ ${#BLOCKERS[@]} -gt 0 ]; then
    fail "the above can't be fixed by installing a package. ${CLONE_HINT}"
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    if [ ${#NEED_PKGS[@]} -eq 0 ] || [ -z "$PKG_INSTALL" ]; then
        fail "couldn't recognise this distro's package manager (apt, dnf, pacman or zypper).
Install the above with your package manager, then run this again."
    fi
    INSTALL_LINE="${PKG_INSTALL} ${NEED_PKGS[*]}"
    echo -e "  all of it installs in one go:"
    echo -e "    ${CYAN}${INSTALL_LINE}${RESET}"
    echo

    REPLY_YN="n"
    if [ "${SERAI_ASSUME_YES:-}" = "1" ]; then
        REPLY_YN="y"
    elif [ -t 0 ]; then
        read -r -p "Run that now? [Y/n] " REPLY_YN || REPLY_YN="n"
        REPLY_YN="${REPLY_YN:-y}"
    elif (exec < /dev/tty) 2>/dev/null; then
        read -r -p "Run that now? [Y/n] " REPLY_YN < /dev/tty || REPLY_YN="n"
        REPLY_YN="${REPLY_YN:-y}"
    else
        fail "run the command above, then run this again (or set SERAI_ASSUME_YES=1)"
    fi

    case "$REPLY_YN" in
        [Yy]*)
            say "installing prerequisites…"
            [ -n "$PKG_REFRESH" ] && { $PKG_REFRESH || warn "package list refresh failed — continuing"; }
            $PKG_INSTALL "${NEED_PKGS[@]}" || fail "installing prerequisites failed. Run this by hand and try again:
    ${INSTALL_LINE}"
            run_checks
            if [ ${#MISSING[@]} -gt 0 ] || [ ${#BLOCKERS[@]} -gt 0 ]; then
                echo
                for b in ${BLOCKERS[@]+"${BLOCKERS[@]}"}; do echo -e "  ${RED}✗${RESET} $b"; done
                for m in ${MISSING[@]+"${MISSING[@]}"};  do echo -e "  ${RED}✗${RESET} still missing: $m"; done
                fail "prerequisites are still not satisfied after installing. ${CLONE_HINT}"
            fi
            ok "prerequisites installed" ;;
        *)  fail "nothing was installed. Run the command above, then try again." ;;
    esac
fi

ok "$(python3 -V 2>&1), $(uname -s) $(uname -m), ${PKG_FAMILY}"

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
