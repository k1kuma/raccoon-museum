#!/usr/bin/env bash
# Run the cookie refresher immediately (e.g. after an expired-cookies alert).
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
exec "$REPO/.venv/bin/python" "$REPO/scripts/refresh_cookies.py" --force "$@"
