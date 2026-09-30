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
sha() { if command -v shasum >/dev/null 2>&1; then shasum -a 256 "$1"; else sha256sum "$1"; fi | cut -c1-64; }
# config.toml: replace it if it is an unmodified copy of an earlier release,
# keep it (and save the new defaults next to it) if you've edited it.
SHIPPED="2536e702e6aa5518fe4ee785793415a92f340ea19eba59f36ac53a2f110715ca 423bddbe5dc5c398757ac3685e71b570387f52a1b59914445f767ea52b757d96 28f8344f62acc3f04ab7c5e215df3427eb903fe880c5f93373f77fac3a659e74 071340332ce3dd87563729c54784a1176743c9cc367fa17d75c36e55363fe788 78ed7a464ce53ee4f5a82e73fb2d04780570d51f9c705ccf14fd395e781069a8 70711e73c3c3c219f07423a5bd0a0d6221422d06cc9b43daacb9bb83c93156d6 c75d98666829b6b35d7aa7ca70a84c00e0d010939f3c42c82b954e515a7fc078 c6dab9cf9ac51dfab969397b4e8a2bf8e12365b2bcd8269c49848e19c17e0fcf"
[ -f "$DIR/.config.shipped.sha256" ] && SHIPPED="$SHIPPED $(cat "$DIR/.config.shipped.sha256")"
NEW_HASH=$(sha "$TMP/src/config.toml")
if [ -f "$DIR/config.toml" ]; then
  if printf '%s\n' $SHIPPED | grep -qx "$(sha "$DIR/config.toml")"; then
    say "updating config.toml to this version's defaults (you hadn't changed it)"
  else
    say "keeping your edited config.toml (this version's defaults saved as config.default.toml)"
    mv "$TMP/src/config.toml" "$TMP/src/config.default.toml"
  fi
fi
cp -R "$TMP/src/." "$DIR/"
echo "$NEW_HASH" > "$DIR/.config.shipped.sha256"
cleanup
trap - EXIT

say "installed. Next time just run:  bash $DIR/start.sh"
[ -n "${POLYBOT_NO_START:-}" ] && exit 0  # for testing the installer alone
cd "$DIR"
# When piped from curl, stdin is the script itself; give start.sh the keyboard.
if (exec < /dev/tty) 2>/dev/null; then
  exec bash start.sh "$@" < /dev/tty
fi
exec bash start.sh "$@"
