#!/usr/bin/env bash
set -euo pipefail
LABEL=com.k1kuma.raccoon-museum.cookies
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Removed $LABEL. (The YAHOO_COOKIES secret is unchanged.)"
