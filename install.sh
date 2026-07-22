#!/usr/bin/env bash
# =============================================================================
# serai — Install Script
# =============================================================================
#
# Usage:
#   cd ~/git/serai && ./install.sh
#
# This script will:
#   - Create a Python virtual environment in ./.venv and install serai
#   - Write an editable env file at ~/.config/serai/serai.env (listen
#     address, port, TLS, ssh-agent socket, and the DNS name(s)/IP(s)
#     you'll reach serai by — prompted for here, changeable later in the
#     web UI under the account menu → Network)
#   - Install and enable a systemd service that runs as you, so it keeps
#     your tmux sessions, ssh-agent, and ~/.ssh/config
#   - Start the service and print the URL(s)
#
# serai runs as YOU (not a service account) because it attaches to your
# tmux sessions and authenticates remote work through your ssh-agent.
#
#   ./install.sh                       interactive (prompts for the DNS name)
#   ./install.sh --hostname NAME[,IP]  set the name(s)/IP(s) up front (no prompt)
#   ./install.sh --bind 0.0.0.0        listen address (default: 0.0.0.0 when a
#                                      hostname is given, else 127.0.0.1)
#   ./install.sh --port 8022           listen port (default: 8022)
#   ./install.sh --system              install a system unit (sudo) instead of
#                                      a user service
#   ./install.sh --help
#
# Idempotent — re-running refreshes the venv and unit without disturbing an
# existing serai.env (change settings in the web UI, or edit that file).
# =============================================================================

set -euo pipefail

GREEN='\033[32m\033[1m'
RED='\033[31m\033[1m'
CYAN='\033[36m\033[1m'
YELLOW='\033[33m\033[1m'
RESET='\033[0m'

SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

ARG_HOSTNAME=""
ARG_BIND=""
ARG_PORT=""
ARG_PREFIX=""
MODE="user"
DRY=false
DETECT_ONLY=false
NO_RESTART=false

while [ $# -gt 0 ]; do
    case "$1" in
        --hostname) ARG_HOSTNAME="${2:-}"; shift 2 ;;
        --hostname=*) ARG_HOSTNAME="${1#*=}"; shift ;;
        --bind) ARG_BIND="${2:-}"; shift 2 ;;
        --bind=*) ARG_BIND="${1#*=}"; shift ;;
        --port) ARG_PORT="${2:-}"; shift 2 ;;
        --port=*) ARG_PORT="${1#*=}"; shift ;;
        --prefix) ARG_PREFIX="${2:-}"; shift 2 ;;
        --prefix=*) ARG_PREFIX="${1#*=}"; shift ;;
        --system) MODE="system"; shift ;;
        -n|--dry-run) DRY=true; shift ;;
        --no-restart) NO_RESTART=true; shift ;;  # copy + deps, but leave the restart to the caller
        --detect-only) DETECT_ONLY=true; shift ;;  # print backend-changed|frontend-only, change nothing
        -h|--help)
            cat <<'EOF'
serai install script. Sets serai up to run as a systemd service (as you, so it
keeps your tmux sessions + ssh-agent), with TLS and a login.

Usage:
  ./install.sh                       interactive (prompts for the DNS name)
  ./install.sh --hostname NAME[,IP]  set the name(s)/IP(s) up front (no prompt)
  ./install.sh --bind 0.0.0.0        listen address (default: 0.0.0.0 when a
                                     hostname is given, else 127.0.0.1)
  ./install.sh --port 8022           listen port (default: 8022)
  ./install.sh --system              install a system unit (sudo) instead of
                                     a user service
  ./install.sh --prefix DIR          install location (default: ~/.local/share/
                                     serai, or /opt/serai with --system)
  ./install.sh --dry-run             show exactly what it would write/run and
                                     change nothing (safe to test with)
  ./install.sh --no-restart          copy files + deps but don't restart the
                                     service (the web UI's self-update uses this,
                                     then restarts itself)
  ./install.sh --help

serai is COPIED to its install location (out of your source/git checkout) and
the service runs from there. Config + credentials live in ~/.config/serai. The
hostname goes into the self-signed TLS cert so a LAN name validates; change it
later in the web UI (account menu -> Network) or in ~/.config/serai/serai.env.
Idempotent — re-running refreshes the install but keeps your serai.env; if only
web/ changed the service is NOT restarted (live sessions keep running and
browsers pick the new frontend up on reload).
EOF
            exit 0 ;;
        *)
            echo "Unknown flag: $1" >&2
            echo "Run ./install.sh --help for usage." >&2
            exit 2 ;;
    esac
done

USER_NAME="${SUDO_USER:-$(id -un)}"
if [ "$USER_NAME" = "root" ]; then
    echo -e "${RED}Please run as a regular user (not root).${RESET}"
    echo "serai must run as you so it can reach your tmux + ssh-agent. The"
    echo "script uses sudo only for a --system install."
    exit 1
fi
USER_NAME_GROUP="$(id -gn "$USER_NAME")"
USER_HOME="$(eval echo "~$USER_NAME")"
CONFIG_DIR="${XDG_CONFIG_HOME:-$USER_HOME/.config}/serai"
ENV_FILE="$CONFIG_DIR/serai.env"

# Where the app code + venv live -- a clean copy, never the git checkout.
if [ -n "$ARG_PREFIX" ]; then
    INSTALL_DIR="$ARG_PREFIX"
elif [ "$MODE" = "system" ]; then
    INSTALL_DIR="/opt/serai"
else
    INSTALL_DIR="${XDG_DATA_HOME:-$USER_HOME/.local/share}/serai"
fi

# Run privileged file ops only for a system-dir install; user dirs need no sudo.
priv() { if [ "$MODE" = "system" ]; then sudo "$@"; else "$@"; fi; }

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════╗${RESET}"
echo -e "${CYAN}║                serai — Install               ║${RESET}"
echo -e "${CYAN}╚══════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Source:       ${SOURCE_DIR}"
echo "  Install to:   ${INSTALL_DIR}"
echo "  Run as user:  ${USER_NAME}"
echo "  Service:      serai.service (${MODE})"
echo "  Env file:     ${ENV_FILE}"
$DRY && echo -e "  ${YELLOW}Mode:         dry-run — nothing will be written, started, or changed${RESET}"
echo ""

# --- [1/5] copy the app to its install location + venv ----------------------
# Copy ONLY the app, never .git / .venv / tests / the private dev notes
# (CLAUDE.md, HANDOFF.md) -- the install location must be free of PII and of
# anything that could be committed/pushed.
echo -e "${CYAN}[1/5]${RESET} Installing serai to ${INSTALL_DIR}..."
SRC_REAL="$(realpath "$SOURCE_DIR")"
DEST_REAL="$(realpath -m "$INSTALL_DIR")"
APP_DIRS=(serai web)
APP_FILES=(run.sh pyproject.toml install.sh uninstall.sh README.md LICENSE)

# Graceful deploys: a frontend-only change needs no service restart -- browsers
# pick up new web/ assets on a plain reload (no-cache), and the service reads
# its version from disk, so even the status-bar version and the "updated —
# reload" prompt refresh live. Detect BEFORE copying by diffing the backend
# surface (serai/ minus the version-only __init__.py, run.sh, pyproject.toml)
# against what's already installed; anything else (web/, docs) rides along
# without dropping the live websockets.
BACKEND_CHANGED=true
if [ "$SRC_REAL" != "$DEST_REAL" ] && [ -d "$INSTALL_DIR/serai" ] \
   && diff -rq -x '__pycache__' -x '__init__.py' "$SOURCE_DIR/serai" "$INSTALL_DIR/serai" >/dev/null 2>&1 \
   && diff -q "$SOURCE_DIR/run.sh" "$INSTALL_DIR/run.sh" >/dev/null 2>&1 \
   && diff -q "$SOURCE_DIR/pyproject.toml" "$INSTALL_DIR/pyproject.toml" >/dev/null 2>&1; then
    BACKEND_CHANGED=false
fi
if $DETECT_ONLY; then
    $BACKEND_CHANGED && echo "backend-changed" || echo "frontend-only"
    exit 0
fi

if $DRY; then
    echo -e "  ${YELLOW}(dry-run)${RESET} would copy ${APP_DIRS[*]} ${APP_FILES[*]} -> ${INSTALL_DIR}"
    echo -e "  ${YELLOW}(dry-run)${RESET} (excludes .git, .venv, tests/, CLAUDE.md, HANDOFF.md)"
    echo -e "  ${YELLOW}(dry-run)${RESET} would create ${INSTALL_DIR}/.venv and 'pip install -e .'"
else
    if [ "$SRC_REAL" != "$DEST_REAL" ]; then
        priv mkdir -p "$INSTALL_DIR"
        for d in "${APP_DIRS[@]}"; do
            priv rm -rf "$INSTALL_DIR/$d"
            priv cp -a "$SOURCE_DIR/$d" "$INSTALL_DIR/"
        done
        for f in "${APP_FILES[@]}"; do
            [ -e "$SOURCE_DIR/$f" ] && priv cp -a "$SOURCE_DIR/$f" "$INSTALL_DIR/"
        done
        priv find "$INSTALL_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
        [ "$MODE" = "system" ] && sudo chown -R "$USER_NAME:$USER_NAME_GROUP" "$INSTALL_DIR"
        echo -e "  ${GREEN}✓${RESET} copied app to ${INSTALL_DIR}"
    else
        echo -e "  ${GREEN}✓${RESET} already installed at ${INSTALL_DIR}"
    fi
    cd "$INSTALL_DIR"
    [ -d .venv ] || python3 -m venv .venv
    ./.venv/bin/pip install -q -e .
    echo -e "  ${GREEN}✓${RESET} serai installed in ${INSTALL_DIR}/.venv"
fi
command -v tmux >/dev/null || echo -e "  ${YELLOW}⚠${RESET}  tmux not found — install it: sudo apt install tmux"

# --- [2/5] env file (with the DNS prompt) -----------------------------------
echo -e "${CYAN}[2/5]${RESET} Network + environment..."
$DRY || mkdir -p "$CONFIG_DIR"

# Primary LAN IP, used as a hint in the prompt and as a fallback bind/URL.
DETECTED_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '/src/{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
[ -z "$DETECTED_IP" ] && DETECTED_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SCHEME="https"

if [ -f "$ENV_FILE" ]; then
    # Preserve operator config on a re-run; read current values for the report.
    BIND="$(sed -n 's/^SERAI_HOST=//p' "$ENV_FILE" | tail -1)"
    PORT="$(sed -n 's/^SERAI_PORT=//p' "$ENV_FILE" | tail -1)"
    HOSTNAMES="$(sed -n 's/^SERAI_HOSTNAME=//p' "$ENV_FILE" | tail -1)"
    grep -qE '^SERAI_TLS=(off|0|false|no)$' "$ENV_FILE" && SCHEME="http"
    echo -e "  ${GREEN}✓${RESET} Keeping existing ${ENV_FILE} (bind ${BIND:-127.0.0.1}:${PORT:-8022}${HOSTNAMES:+, names: ${HOSTNAMES}})"
    echo "      (change the hostname later in the web UI → Network)"
else
    HOSTNAMES="$ARG_HOSTNAME"
    if [ -z "$HOSTNAMES" ] && [ -t 0 ]; then
        echo ""
        echo "  How will you reach serai from your other devices? Enter the DNS"
        echo "  name(s) and/or IP(s) — comma-separated — so the TLS cert covers"
        echo "  them and the browser doesn't flag a name mismatch."
        [ -n "$DETECTED_IP" ] && echo -e "  (this host's LAN IP ${CYAN}${DETECTED_IP}${RESET} is included automatically)"
        echo "  Leave blank to serve on localhost only."
        echo ""
        read -r -p "  Hostname(s)/IP(s): " HOSTNAMES || HOSTNAMES=""
        echo ""
    fi

    BIND="$ARG_BIND"
    PORT="${ARG_PORT:-8022}"
    # A hostname implies you want LAN reach -> listen on all interfaces unless
    # the operator pinned a specific bind address.
    if [ -z "$BIND" ]; then
        if [ -n "$HOSTNAMES" ]; then BIND="0.0.0.0"; else BIND="127.0.0.1"; fi
    fi

    ENV_CONTENT="$(cat <<EOF
# serai service environment. Change these in the web UI (account menu →
# Network), or edit here and: systemctl --user restart serai

# Listen address. 127.0.0.1 = localhost only; 0.0.0.0 = all interfaces (reach
# it from the LAN). The login + TLS are what make LAN exposure safe -- keep
# serai off the public internet.
SERAI_HOST=${BIND}
SERAI_PORT=${PORT}

# DNS name(s)/IP(s) you reach serai by, comma-separated. Put in the self-signed
# cert so the name validates (the cert also auto-covers localhost, this host,
# and its LAN IP). The cert regenerates when this changes.
SERAI_HOSTNAME=${HOSTNAMES}

# TLS is on by default (self-signed under ~/.config/serai). Override:
#   SERAI_TLS=off                 # plain http (localhost / behind a TLS proxy)
#   SERAI_CERT=/path/cert.pem
#   SERAI_KEY=/path/key.pem

# Built-in login is on by default (SERAI_AUTH=off disables it).

# ssh-agent socket serai uses for remote hosts.
SSH_AUTH_SOCK=${SSH_AUTH_SOCK:-/run/user/$(id -u "$USER_NAME")/gcr/ssh}
EOF
)"
    if $DRY; then
        echo -e "  ${YELLOW}(dry-run)${RESET} would write ${ENV_FILE}:"
        printf '%s\n' "$ENV_CONTENT" | sed 's/^/        | /'
    else
        ( umask 077; printf '%s\n' "$ENV_CONTENT" > "$ENV_FILE" )
        echo -e "  ${GREEN}✓${RESET} Wrote ${ENV_FILE} (bind ${BIND}:${PORT}${HOSTNAMES:+, names: ${HOSTNAMES}})"
    fi
fi

# --- [3/5] systemd unit -----------------------------------------------------
echo -e "${CYAN}[3/5]${RESET} Installing the systemd ${MODE} service..."
UNIT_BODY="[Unit]
Description=serai — one attach point for terminal, SSH, and Claude Code sessions
Documentation=https://github.com/preston-peterson/serai
After=network-online.target
Wants=network-online.target

[Service]
Type=exec
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
ExecStart=${INSTALL_DIR}/run.sh
Restart=on-failure
RestartSec=2"

if [ "$MODE" = "user" ]; then
    UNIT_PATH="$USER_HOME/.config/systemd/user/serai.service"
    UNIT_FULL="$(printf '%s\n\n[Install]\nWantedBy=default.target\n' "$UNIT_BODY")"
    SYSTEMCTL="systemctl --user"
    if $DRY; then
        echo -e "  ${YELLOW}(dry-run)${RESET} would write ${UNIT_PATH}:"
        printf '%s\n' "$UNIT_FULL" | sed 's/^/        | /'
        echo -e "  ${YELLOW}(dry-run)${RESET} would: loginctl enable-linger ${USER_NAME}; ${SYSTEMCTL} daemon-reload"
    else
        mkdir -p "$(dirname "$UNIT_PATH")"
        printf '%s' "$UNIT_FULL" > "$UNIT_PATH"
        if command -v loginctl >/dev/null; then
            loginctl enable-linger "$USER_NAME" >/dev/null 2>&1 \
                && echo -e "  ${GREEN}✓${RESET} Enabled linger (starts at boot without a login)" \
                || echo -e "  ${YELLOW}·${RESET} Could not enable linger (sudo loginctl enable-linger ${USER_NAME})"
        fi
        systemctl --user daemon-reload
        echo -e "  ${GREEN}✓${RESET} serai.service installed"
    fi
else
    UNIT_FULL="$(printf '%s\nUser=%s\nGroup=%s\n\n[Install]\nWantedBy=multi-user.target\n' \
        "$UNIT_BODY" "$USER_NAME" "$(id -gn "$USER_NAME")")"
    SYSTEMCTL="sudo systemctl"
    if $DRY; then
        echo -e "  ${YELLOW}(dry-run)${RESET} would write /etc/systemd/system/serai.service (+ /etc/sudoers.d/serai):"
        printf '%s\n' "$UNIT_FULL" | sed 's/^/        | /'
        echo -e "  ${YELLOW}(dry-run)${RESET} would: sudo systemctl daemon-reload"
    else
        printf '%s' "$UNIT_FULL" | sudo tee /etc/systemd/system/serai.service >/dev/null
        sudo systemctl daemon-reload
        # A scoped sudoers rule so the web UI's Network panel can restart serai
        # (to apply a hostname change) without the operator opening a terminal.
        SUDOERS_TMP="$(mktemp)"
        cat > "$SUDOERS_TMP" <<EOF
# SUDOERS_VERSION: 1
# serai (system install) — lets ${USER_NAME} restart the service from the web UI
# without a password. Exact commands only. Written by install.sh; do not edit.
${USER_NAME} ALL=(ALL) NOPASSWD: /bin/systemctl restart serai, /usr/bin/systemctl restart serai, /bin/systemctl restart --no-block serai, /usr/bin/systemctl restart --no-block serai
EOF
        if command -v visudo >/dev/null && ! visudo -cf "$SUDOERS_TMP" >/dev/null 2>&1; then
            echo -e "  ${RED}✗${RESET} generated sudoers rule failed validation — skipping (UI restart will need a manual sudo)"
        else
            sudo install -m 0440 -o root -g root "$SUDOERS_TMP" /etc/sudoers.d/serai
            echo -e "  ${GREEN}✓${RESET} sudoers rule installed (UI can restart serai)"
        fi
        rm -f "$SUDOERS_TMP"
        echo -e "  ${GREEN}✓${RESET} serai.service installed"
    fi
fi

# --- [4/5] enable + (re)start -----------------------------------------------
echo -e "${CYAN}[4/5]${RESET} Starting serai..."
if $DRY; then
    echo -e "  ${YELLOW}(dry-run)${RESET} would: ${SYSTEMCTL} enable serai.service; then restart it"
    echo -e "  ${YELLOW}(dry-run)${RESET} (frontend-only changes skip the restart to keep live sessions)"
    $NO_RESTART && echo -e "  ${YELLOW}(dry-run)${RESET} --no-restart: would copy + install deps but leave the restart to the caller"
else
    $SYSTEMCTL enable serai.service >/dev/null 2>&1 || true
    if $NO_RESTART; then
        # The web UI's "Update now" runs install.sh as a child of the serai
        # service; restarting here would kill this process mid-copy. The caller
        # copies first, then restarts itself last. Report which it needs to do.
        $BACKEND_CHANGED && echo -e "  ${GREEN}✓${RESET} files updated — caller will restart (backend changed)" \
                         || echo -e "  ${GREEN}✓${RESET} files updated — no restart needed (frontend-only)"
    elif ! $BACKEND_CHANGED && $SYSTEMCTL is-active --quiet serai.service; then
        # only web/ (or docs) changed: the running process serves the new files
        # as-is (no-cache), so keep every live websocket attached
        echo -e "  ${GREEN}✓${RESET} frontend-only change — restart skipped, live sessions kept"
        echo    "      (open tabs pick it up on their next reload)"
    else
        $SYSTEMCTL restart serai.service
    fi
fi

# --- [5/5] verify + report --------------------------------------------------
echo -e "${CYAN}[5/5]${RESET} Verifying..."
if $DRY; then
    echo -e "  ${YELLOW}(dry-run)${RESET} no changes were made."
else
    sleep 2
    if $SYSTEMCTL is-active --quiet serai.service; then
        echo -e "  ${GREEN}✓${RESET} serai is running"
    else
        echo -e "  ${RED}✗${RESET} serai failed to start. Check the logs:"
        [ "$MODE" = "user" ] && echo "      journalctl --user -u serai -n 50" \
                             || echo "      sudo journalctl -u serai -n 50"
        exit 1
    fi
fi

PORT="${PORT:-8022}"
HOST0="$(printf '%s' "${HOSTNAMES:-}" | cut -d, -f1)"
[ -z "$HOST0" ] && HOST0="${DETECTED_IP:-127.0.0.1}"

echo ""
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
$DRY && echo -e "${GREEN}  Dry run — no changes made${RESET}" \
     || echo -e "${GREEN}  Install complete${RESET}"
echo -e "${GREEN}══════════════════════════════════════════════${RESET}"
echo ""
echo -e "  Open:  ${CYAN}${SCHEME}://${HOST0}:${PORT}${RESET}"
echo "         (first run: open it and create your admin account — do it now,"
echo "          before others can reach serai; self-signed cert — accept once.)"
echo ""
echo "  Manage:"
if [ "$MODE" = "user" ]; then
    echo "    systemctl --user status serai"
    echo "    systemctl --user restart serai      # or do it from the web UI → Network"
    echo "    journalctl --user -u serai -f"
else
    echo "    sudo systemctl status serai"
    echo "    sudo systemctl restart serai"
    echo "    journalctl -u serai -f"
fi
echo ""
echo "  Env:   ${ENV_FILE}"
echo ""
