"""Shared paths and environment loading."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
LEAGUE_DB = DATA_DIR / "league.sqlite"
SAMPLE_DB = DATA_DIR / "sample.sqlite"
MANAGERS_YAML = DATA_DIR / "managers.yaml"
STATS_OUT = ROOT / "site" / "src" / "data"
# In-progress week from `scrape --live`; deployed with the site but never committed.
LIVE_JSON = DATA_DIR / "live.json"

LEAGUE_ID = os.environ.get("YAHOO_LEAGUE_ID", "705220")
LEAGUE_NAME = "City Slickin' Raccoons"
LEAGUE_URL = f"https://football.fantasysports.yahoo.com/f1/{LEAGUE_ID}"
# season:league_id for every season the scraper should read. Add the new pair each year the league renews
# (and update YAHOO_LEAGUE_ID to the new current id).
SEASONS = "2024:934670,2025:388631,2026:705220"


def load_dotenv() -> None:
    """Load a gitignored .env from the repo root without overriding real env vars."""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)
