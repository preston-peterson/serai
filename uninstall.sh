#!/usr/bin/env bash
# =============================================================================
# serai — Uninstall Script
# =============================================================================
#
# Reverses what install.sh set up.
#
# Usage:
#   ./uninstall.sh           interactive — prompts before removing the venv/config
#   ./uninstall.sh --purge   remove everything (service + venv + config), no prompts
#   ./uninstall.sh --keep    remove the service only; keep the venv + config, no prompts
#   ./uninstall.sh --dry-run show what would be removed; change nothing
#   ./uninstall.sh --help
#
# Always removed:
#   - the systemd service (user and/or system): stopped, disabled, unit deleted
#   - the sudoers rule (/etc/sudoers.d/serai), for a --system install
#
# Prompted (or driven by --purge / --keep):
#   - the install directory (the copied app + venv, e.g. ~/.local/share/serai):
#     removed by default; --keep leaves it. A git checkout is never deleted.
#   - your serai config + CREDENTIALS (~/.config/serai): login users, TLS cert
#     + key, the cookie secret, serai.env, and saved UI settings. Kept by default.
#
# Never touched:
#   - your tmux sessions (they live in tmux, not serai), ~/.ssh/config, any git
#     checkout you installed from, or system packages.
# =============================================================================

set -euo pipefail

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
CYAN='\033[36m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

PURGE=false
KEEP=false
DRY=false
for arg in "$@"; do
    case "$arg" in
        --purge) PURGE=true ;;
        --keep)  KEEP=true ;;
        -n|--dry-run) DRY=true ;;
        -h|--help)
            cat <<'EOF'
serai uninstall script. Reverses install.sh.

Usage:
  ./uninstall.sh           interactive — prompts before removing the venv/config
  ./uninstall.sh --purge   remove everything (service + venv + config), no prompts
  ./uninstall.sh --keep    remove the service only; keep the venv + config
  ./uninstall.sh --dry-run show what would be removed; change nothing
  ./uninstall.sh --help

Always removes the systemd service (+ the /etc/sudoers.d/serai rule for a system
install). The venv and ~/.config/serai (login users, TLS cert, settings) are kept
by default — use --purge to remove them. Your tmux sessions are never touched.
EOF
            exit 0 ;;
        *)
            echo -e "${RED}Unknown option: $arg${RESET}" >&2
            echo "Run ./uninstall.sh --help for usage." >&2
            exit 2 ;;
    esac
done

if $PURGE && $KEEP; then
    echo -e "${RED}Cannot use --purge and --keep together.${RESET}" >&2
    exit 1
fi

USER_NAME="${SUDO_USER:-$(id -un)}"
if [ "$USER_NAME" = "root" ]; then
    echo -e "${RED}Please run as a regular user (not root).${RESET}"
    echo "A user service can only be removed by its own user; sudo is used only for"
    echo "a --system install's bits."
    exit 1
fi
USER_HOME="$(eval echo "~$USER_NAME")"
CONFIG_DIR="${XDG_CONFIG_HOME:-$USER_HOME/.config}/serai"
USER_UNIT="$USER_HOME/.config/systemd/user/serai.service"
SYS_UNIT="/etc/systemd/system/serai.service"

# The install location is whatever the unit runs from. Read it back from the
# unit (most robust) so we remove the real copy regardless of where it landed.
INSTALL_DIR=""
for u in "$USER_UNIT" "$SYS_UNIT"; do
    [ -f "$u" ] && INSTALL_DIR="$(sed -n 's/^WorkingDirectory=//p' "$u" | tail -1)"
done

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║               serai — Uninstall              ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Run as user:  ${USER_NAME}"
echo "  Install dir:  ${INSTALL_DIR:-<none found>}"
echo "  Config dir:   ${CONFIG_DIR}"
$DRY && echo -e "  ${YELLOW}Mode:         dry-run — nothing will be removed${RESET}"
echo ""

if ! $PURGE && ! $KEEP && ! $DRY; then
    read -r -p "Uninstall serai (remove the service)? [y/N] " reply || reply=""
    case "$reply" in
        [Yy]*) ;;
        *) echo "Cancelled."; exit 0 ;;
    esac
fi

# --- [1/3] remove the service(s) --------------------------------------------
echo -e "${CYAN}[1/3]${RESET} Removing the systemd service..."
removed_any=false

if [ -f "$USER_UNIT" ] && command -v systemctl >/dev/null; then
    removed_any=true
    if $DRY; then
        echo -e "  ${YELLOW}(dry-run)${RESET} would: systemctl --user stop/disable serai; rm ${USER_UNIT}; daemon-reload"
    else
        systemctl --user stop serai.service 2>/dev/null || true
        systemctl --user disable serai.service 2>/dev/null || true
        rm -f "$USER_UNIT"
        systemctl --user daemon-reload 2>/dev/null || true
        systemctl --user reset-failed serai.service 2>/dev/null || true
        echo -e "  ${GREEN}✓${RESET} user service stopped, disabled, and removed"
    fi
fi

if [ -f "$SYS_UNIT" ]; then
    removed_any=true
    if $DRY; then
        echo -e "  ${YELLOW}(dry-run)${RESET} would: sudo systemctl stop/disable serai; sudo rm ${SYS_UNIT}"
        [ -f /etc/sudoers.d/serai ] && echo -e "  ${YELLOW}(dry-run)${RESET} would: sudo rm /etc/sudoers.d/serai"
    else
        sudo systemctl stop serai.service 2>/dev/null || true
        sudo systemctl disable serai.service 2>/dev/null || true
        sudo rm -f "$SYS_UNIT"
        [ -f /etc/sudoers.d/serai ] && { sudo rm -f /etc/sudoers.d/serai; echo -e "  ${GREEN}✓${RESET} sudoers rule removed"; }
        sudo systemctl daemon-reload 2>/dev/null || true
        sudo systemctl reset-failed serai.service 2>/dev/null || true
        echo -e "  ${GREEN}✓${RESET} system service stopped, disabled, and removed"
    fi
fi

$removed_any || echo -e "  ${YELLOW}·${RESET} no serai service found (already removed, or you ran it manually with run.sh)"

# --- [2/3] install directory (copied app + venv) ----------------------------
echo -e "${CYAN}[2/3]${RESET} Install directory..."
if [ -z "$INSTALL_DIR" ] || [ ! -d "$INSTALL_DIR" ]; then
    echo -e "  ${YELLOW}·${RESET} no install directory found"
elif [ -e "$INSTALL_DIR/.git" ]; then
    # A source/git checkout, not a clean install -- never delete it.
    echo -e "  ${YELLOW}·${RESET} ${INSTALL_DIR} is a git checkout — left in place (service unregistered only)"
else
    remove_install=true                         # it's only copied code + venv (no user data)
    if $KEEP; then
        remove_install=false
    elif ! $PURGE && ! $DRY; then
        read -r -p "  Remove the install dir ${INSTALL_DIR} (copied code + venv)? [Y/n] " r || r=""
        [[ "$r" =~ ^[Nn] ]] && remove_install=false
    fi
    if $DRY; then
        $remove_install && echo -e "  ${YELLOW}(dry-run)${RESET} would remove ${INSTALL_DIR}" \
                        || echo -e "  ${YELLOW}(dry-run)${RESET} would keep ${INSTALL_DIR}"
    elif $remove_install; then
        if [ -w "$(dirname "$INSTALL_DIR")" ]; then rm -rf "$INSTALL_DIR"; else sudo rm -rf "$INSTALL_DIR"; fi
        echo -e "  ${GREEN}✓${RESET} ${INSTALL_DIR} removed"
    else
        echo -e "  ${YELLOW}·${RESET} kept ${INSTALL_DIR}"
    fi
fi

# --- [3/3] config + credentials ---------------------------------------------
echo -e "${CYAN}[3/3]${RESET} Config + credentials (${CONFIG_DIR})..."
remove_config=false
if $PURGE; then
    remove_config=true
elif ! $KEEP && ! $DRY && [ -d "$CONFIG_DIR" ]; then
    echo -e "  ${YELLOW}This holds your login users (password hashes), TLS cert/key, the"
    echo -e "  cookie secret, serai.env, and saved UI settings.${RESET}"
    read -r -p "  Remove it? [y/N] " r || r=""
    [[ "$r" =~ ^[Yy] ]] && remove_config=true
fi
if ! [ -d "$CONFIG_DIR" ]; then
    echo -e "  ${YELLOW}·${RESET} no config dir present"
elif $DRY; then
    $remove_config && echo -e "  ${YELLOW}(dry-run)${RESET} would remove ${CONFIG_DIR}" \
                   || echo -e "  ${YELLOW}(dry-run)${RESET} would keep ${CONFIG_DIR} (use --purge to remove credentials too)"
elif $remove_config; then
    rm -rf "$CONFIG_DIR"
    echo -e "  ${GREEN}✓${RESET} ${CONFIG_DIR} removed"
else
    echo -e "  ${YELLOW}·${RESET} kept ${CONFIG_DIR}"
fi

echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
$DRY && echo -e "${GREEN}  Dry run — nothing removed${RESET}" \
     || echo -e "${GREEN}  serai uninstalled${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo "  Your tmux sessions and ~/.ssh/config are untouched; the source checkout"
echo "  in ${SOURCE_DIR} is left in place. Reinstall any time with ./install.sh"
if command -v loginctl >/dev/null; then
    echo "  (linger is left enabled — 'loginctl disable-linger ${USER_NAME}' if no"
    echo "   other user services need it)."
fi
echo ""
