#!/usr/bin/env bash
# One-line install + start (paper trading only; nothing here can place an order):
#
#   curl -fsSL https://raw.githubusercontent.com/chrisjeansonne1232-jpg/tarderz/HEAD/install.sh | bash
#
# Add  -s -- --ipad  after "bash" to also open the dashboard to your iPad.
# Re-run the same line to update: the code is replaced; your data/, .venv and
# config.toml are kept. Installs into ~/polybot (override with POLYBOT_DIR).
set -euo pipefail

REPO="chrisjeansonne1232-jpg/tarderz"
BRANCH="claude/polymarket-btc-paper-trading-nxqncp"
URL="${POLYBOT_TARBALL_URL:-https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz}"
DIR="${POLYBOT_DIR:-$HOME/polybot}"

say() { printf '\033[1;35m▸\033[0m %s\n' "$*"; }

say "downloading polybot into $DIR"
TMP=$(mktemp -d)
cleanup() { rm -rf "$TMP"; }
trap cleanup EXIT
if ! curl -fsSL "$URL" -o "$TMP/src.tar.gz"; then
  echo "Download failed from $URL"
  echo "If the GitHub repository is private, make it public or download the ZIP from GitHub instead."
  exit 1
fi
mkdir "$TMP/src"
tar -xzf "$TMP/src.tar.gz" -C "$TMP/src" --strip-components=1
[ -f "$TMP/src/start.sh" ] || { echo "The download doesn't look like polybot (no start.sh)."; exit 1; }

mkdir -p "$DIR"
if [ -f "$DIR/config.toml" ]; then
  say "keeping your config.toml (this version's defaults saved as config.default.toml)"
  mv "$TMP/src/config.toml" "$TMP/src/config.default.toml"
fi
cp -R "$TMP/src/." "$DIR/"
cleanup
trap - EXIT

say "installed. Next time just run:  bash $DIR/start.sh"
cd "$DIR"
# When piped from curl, stdin is the script itself; give start.sh the keyboard.
if (exec < /dev/tty) 2>/dev/null; then
  exec bash start.sh "$@" < /dev/tty
fi
exec bash start.sh "$@"
