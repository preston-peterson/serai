#!/usr/bin/env bash
# Launch serai on localhost. It serves HTTPS by default (self-signed cert, or
# your own via SERAI_CERT/SERAI_KEY); set SERAI_TLS=off for plain http.
set -euo pipefail

HOST="${SERAI_HOST:-127.0.0.1}"
PORT="${SERAI_PORT:-8022}"

cd "$(dirname "$0")"

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
