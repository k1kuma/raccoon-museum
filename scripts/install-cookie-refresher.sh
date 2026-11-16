#!/usr/bin/env bash
# Schedules scripts/refresh_cookies.py with launchd: daily at 9am and 9pm, and at login.
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=com.k1kuma.raccoon-museum.cookies
DEST="$HOME/Library/LaunchAgents/$LABEL.plist"
[ -x "$REPO/.venv/bin/python" ] || { echo "Create the venv first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-cookies.txt"; exit 1; }
"$REPO/.venv/bin/python" -c "import browser_cookie3" 2>/dev/null || { echo "Run: .venv/bin/pip install -r requirements-cookies.txt"; exit 1; }
command -v gh >/dev/null || [ -x /opt/homebrew/bin/gh ] || { echo "Install the GitHub CLI (brew install gh) and run gh auth login"; exit 1; }
mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
sed -e "s#__REPO__#$REPO#g" -e "s#__HOME__#$HOME#g" "$REPO/scripts/launchd/$LABEL.plist" > "$DEST"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DEST"
echo "Installed $DEST (runs now, at 9:00 and 21:00, and at login)."
echo "Log: ~/Library/Logs/raccoon-museum-cookies.log"
