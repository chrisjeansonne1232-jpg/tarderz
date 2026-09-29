#!/usr/bin/env bash
# One command to run everything:
#   bash start.sh            start paper trading + dashboard, open it in your browser
#   bash start.sh --ipad     same, and make the dashboard reachable from your iPad (same wifi)
#   bash start.sh --config other.toml
#
# First run: checks for Python 3.11+ (offers to install it with Homebrew on a
# Mac), creates .venv, installs dependencies, and checks the live markets.
# Later runs skip straight to starting. Ctrl+C stops the bot cleanly.
# Paper trading only: nothing here can place an order.
set -euo pipefail
cd "$(dirname "$0")"

HOST=""
CONFIG=config.toml
while [ $# -gt 0 ]; do
  case "$1" in
    --ipad|--lan) HOST=0.0.0.0 ;;
    --config) CONFIG="$2"; shift ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $1 (try --help)"; exit 2 ;;
  esac
  shift
done

say() { printf '\033[1;35m▸\033[0m %s\n' "$*"; }

# --- 1. Python 3.11+ ---------------------------------------------------------
find_python() {
  for c in python3.13 python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}
PY=$(find_python || true)
if [ -z "$PY" ]; then
  if [ "$(uname)" = "Darwin" ] && command -v brew >/dev/null 2>&1; then
    read -r -p "Python 3.11 or newer is needed. Install Python 3.12 with Homebrew now? [Y/n] " answer
    case "$answer" in [nN]*) exit 1 ;; esac
    brew install python@3.12
    PY=$(find_python) || { echo "Python still not found. Get it from https://www.python.org/downloads/"; exit 1; }
  else
    echo "Python 3.11 or newer is needed."
    echo "Install it from https://www.python.org/downloads/ (the big yellow button), then run: bash start.sh"
    exit 1
  fi
fi

# --- 2. Private environment + dependencies (reinstalled only when they change) --
VPY=.venv/bin/python
if [ ! -x "$VPY" ]; then
  say "setting up a private Python environment in .venv ($("$PY" --version))"
  "$PY" -m venv .venv
fi
STAMP=.venv/.requirements.sha256
SUM=$("$VPY" -c 'import hashlib; print(hashlib.sha256(open("requirements.txt", "rb").read()).hexdigest())')
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$SUM" ]; then
  say "installing dependencies (first run only, ~30 s)"
  "$VPY" -m pip install --quiet --disable-pip-version-check --upgrade pip
  "$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt
  echo "$SUM" > "$STAMP"
fi

# --- 3. One-shot check of the live markets and fee parameters -----------------
mkdir -p data
say "checking the live Polymarket markets (full output: data/discover.txt)"
if "$VPY" -m polybot --config "$CONFIG" discover > data/discover.txt 2>&1; then
  grep -E "^=== |rules verified|fee model used|NOT FOUND|failed|NOTE" data/discover.txt | sed 's/^/  /' || true
else
  echo "  market check failed (the bot will keep retrying on its own):"
  tail -5 data/discover.txt | sed 's/^/  /'
fi

# --- 4. Start the bot + dashboard; open the browser once it's listening --------
PORT=$("$VPY" -c "from polybot.config import load_config; print(load_config('$CONFIG').dashboard.dashboard_port)")
URL="http://127.0.0.1:$PORT"
open_when_ready() {
  for _ in $(seq 1 120); do
    if "$VPY" -c "import socket, sys; s = socket.socket(); s.settimeout(0.3); sys.exit(s.connect_ex(('127.0.0.1', $PORT)))" 2>/dev/null; then
      if command -v open >/dev/null 2>&1; then open "$URL"
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL" >/dev/null 2>&1 || true
      fi
      if [ "$HOST" = 0.0.0.0 ]; then
        ip=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || true)
        say "on your iPad (same wifi) open:  http://${ip:-YOUR-COMPUTER-IP}:$PORT"
      fi
      return 0
    fi
    sleep 0.5
  done
}
open_when_ready &

say "starting paper trading — dashboard at $URL — press Ctrl+C to stop"
ARGS=(-m polybot --config "$CONFIG" run --dashboard)
[ -n "$HOST" ] && ARGS+=(--host "$HOST")
if [ "$(uname)" = "Darwin" ]; then
  caffeinate -is -w $$ &   # keep the Mac awake for as long as the bot runs
fi
exec "$VPY" "${ARGS[@]}"
