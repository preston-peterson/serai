#!/usr/bin/env bash
# Launch serai on localhost. It serves HTTPS by default (self-signed cert, or
# your own via SERAI_CERT/SERAI_KEY); set SERAI_TLS=off for plain http.
set -euo pipefail

HOST="${SERAI_HOST:-127.0.0.1}"
PORT="${SERAI_PORT:-8022}"

cd "$(dirname "$0")"

# Recover the PATH a terminal would have. tmux takes a new session's environment
# from the *client* that creates it -- which is serai -- so serai's PATH is the
# PATH every session it starts will run under. As a lingering user service serai
# starts at boot, before the desktop pushes ~/.profile's PATH into the systemd
# user manager, and comes up with a bare PATH that lacks ~/.local/bin. Tools
# installed there (`claude` among them) then aren't found, and a Claude session
# dies the instant it is created -- tmux prints "claude: command not found",
# destroys the session, and clears the screen on the way out, so the reason is
# gone before you can read it. Asking the *interactive* login shell recovers the
# real PATH regardless of login ordering (~/.profile adds the login dirs).
# -i is load-bearing: dev-tool PATH dirs (~/.opencode/bin, ~/.grok/bin, ...) live
# in ~/.bashrc, which returns early when non-interactive, so a bare -l probe never
# sees them and those sessions die on "command not found" too.
# Best-effort by design: a shell that hangs, fails, or answers with something
# that isn't a PATH leaves ours untouched.
if [ -z "${SERAI_SKIP_PATH_PROBE:-}" ]; then
  login_path="$(timeout 5 "${SHELL:-/bin/sh}" -ilc 'printf %s "$PATH"' </dev/null 2>/dev/null | tail -n1)" || login_path=""
  case ":$login_path:" in
    *:/bin:*|*:/usr/bin:*) export PATH="$login_path" ;;
  esac
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -e .
fi

# tmux is required locally; ssh + tmux must exist on any remote hosts you use.
command -v tmux >/dev/null || { echo "tmux not found -- install it: sudo apt install tmux"; exit 1; }

if [ "${SERAI_TLS:-on}" != "off" ]; then
  # Resolve (and, first run, generate) the cert/key pair. serai.tls prints the
  # cert path then the key path on stdout; status/errors go to stderr.
  TLS_PATHS="$(./.venv/bin/python -m serai.tls)" || { echo "serai: TLS setup failed (see above)"; exit 1; }
  CERTFILE="${TLS_PATHS%$'\n'*}"   # first line
  KEYFILE="${TLS_PATHS##*$'\n'}"   # second line
  echo "serai: https://$HOST:$PORT"
  exec ./.venv/bin/uvicorn serai.main:app --host "$HOST" --port "$PORT" \
       --ssl-certfile "$CERTFILE" --ssl-keyfile "$KEYFILE" "$@"
fi

echo "serai: http://$HOST:$PORT  (SERAI_TLS=off)"
exec ./.venv/bin/uvicorn serai.main:app --host "$HOST" --port "$PORT" "$@"
