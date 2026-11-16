"""NFL kickoff schedule → live-scrape windows.

`python -m museum.nfl_schedule fetch` saves the next ~9 days of kickoffs from ESPN's keyless public scoreboard
(no Yahoo requests) to data/nfl_schedule.json. `python -m museum.nfl_schedule gate` decides whether a live run
should scrape right now. Standard library only, so the gate runs in seconds before any `pip install`.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

SCHEDULE_FILE = Path(__file__).resolve().parent.parent / "data" / "nfl_schedule.json"
ESPN_URL = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={:%Y%m%d}"
ET = ZoneInfo("America/New_York")
BEFORE = timedelta(minutes=15)       # start scraping just before kickoff
AFTER = timedelta(hours=4)           # long enough for overtime and stat corrections during the broadcast
FINAL_REFRESH = timedelta(minutes=15)  # one more 15-minute tick after the last window closes
MAX_AGE = timedelta(days=8)
DAYS_BACK, DAYS_AHEAD = 1, 8


def _utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch(today: date | None = None, opener=urllib.request.urlopen) -> dict:
    """One ESPN request per day (date ranges aren't supported). Raises on any failure so a stale file is kept."""
    today = today or datetime.now(ET).date()
    days = [today + timedelta(days=d) for d in range(-DAYS_BACK, DAYS_AHEAD + 1)]
    games = {}
    for day in days:
        req = urllib.request.Request(ESPN_URL.format(day), headers={"User-Agent": "raccoon-museum"})
        with opener(req, timeout=20) as resp:
            data = json.load(resp)
        for e in data.get("events", []):
            games[e["id"]] = {"kickoff": _utc(e["date"]).strftime("%Y-%m-%dT%H:%MZ"), "name": e.get("shortName", "")}
    return {
        "fetched_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "from": days[0].isoformat(),
        "to": days[-1].isoformat(),
        "games": sorted(games.values(), key=lambda g: (g["kickoff"], g["name"])),
    }


def windows(games: list[dict]) -> list[tuple[datetime, datetime]]:
    """Kickoff-15min → kickoff+4h for every game, overlapping windows merged. All UTC, so DST can't shift them."""
    spans = sorted((k - BEFORE, k + AFTER) for k in (_utc(g["kickoff"]) for g in games))
    merged: list[list[datetime]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def fallback_live(now: datetime) -> bool:
    """The old fixed windows (ET), used only when the schedule file is missing or stale."""
    now = now.astimezone(ET)
    day = now - timedelta(minutes=30) if now.hour == 0 and now.minute < 30 else now
    mins = (now - day.replace(hour=0, minute=0, second=0, microsecond=0)).total_seconds() / 60
    dow = day.weekday()
    spans = []
    if dow == 6 or (dow == 5 and day.month in (12, 1)) or (day.month, day.day) == (12, 25):
        spans.append((12 * 60 + 45, 24 * 60 + 30))
    if day.month == 11 and dow == 3 and 22 <= day.day <= 28:
        spans.append((12 * 60 + 15, 24 * 60 + 30))
    if dow in (0, 3):
        spans.append((20 * 60, 24 * 60 + 30))
    return any(a <= mins < b for a, b in spans)


def usable(schedule: dict | None, now: datetime) -> str | None:
    """None if the schedule can be trusted at `now`, else the reason it can't."""
    if not schedule:
        return "no schedule file"
    if now - _utc(schedule["fetched_at"]) > MAX_AGE:
        return f"schedule fetched {schedule['fetched_at']} is older than {MAX_AGE.days} days"
    covered_until = datetime.fromisoformat(schedule["to"]).replace(tzinfo=ET) + timedelta(days=1)
    if now >= covered_until:
        return f"schedule only covers up to {schedule['to']}"
    return None


def decide(schedule: dict | None, now: datetime) -> tuple[bool, str, list[tuple[datetime, datetime]]]:
    problem = usable(schedule, now)
    if problem:
        live = fallback_live(now)
        return live, f"WARNING: {problem}; using fixed fallback windows → {'in' if live else 'outside'} a window", []
    spans = windows(schedule["games"])
    for start, end in spans:
        if start <= now < end:
            return True, f"in game window {_fmt(start)} – {_fmt(end)}", spans
        if end <= now < end + FINAL_REFRESH:
            return True, f"final refresh after window {_fmt(start)} – {_fmt(end)}", spans
    return False, "outside every game window", spans


def _fmt(t: datetime) -> str:
    return t.astimezone(ET).strftime("%a %b %d %I:%M %p ET")


def load(path: Path = SCHEDULE_FILE) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="museum.nfl_schedule")
    p.add_argument("cmd", choices=["fetch", "gate", "show"])
    p.add_argument("--file", type=Path, default=SCHEDULE_FILE)
    p.add_argument("--force", action="store_true", help="gate: treat as live (manual runs)")
    args = p.parse_args(argv)

    if args.cmd == "fetch":
        try:
            schedule = fetch()
        except Exception as e:  # keep the previous file; the gate falls back once it goes stale
            print(f"::warning::NFL schedule fetch failed, keeping the old file: {e}", file=sys.stderr)
            return 0
        old = load(args.file)
        # Only rewrite when kickoffs changed (or every few days) so the daily refresh doesn't commit noise.
        if old and old.get("games") == schedule["games"] and \
                datetime.now(timezone.utc) - _utc(old["fetched_at"]) < timedelta(days=3):
            print(f"NFL schedule unchanged ({len(schedule['games'])} games)")
        else:
            args.file.write_text(json.dumps(schedule, indent=1) + "\n")
            print(f"NFL schedule: {len(schedule['games'])} games {schedule['from']} → {schedule['to']}")
        return 0

    now = datetime.now(timezone.utc)
    schedule = load(args.file)
    live, reason, spans = decide(schedule, now)
    if args.force:
        live, reason = True, f"manual run ({reason})"
    print("Live-scrape windows this week:" if spans else "No schedule windows.")
    for start, end in spans:
        mark = " ◀ now" if start <= now < end + FINAL_REFRESH else ""
        print(f"  {_fmt(start)} → {_fmt(end)}{mark}")
    print(f"{now.astimezone(ET):%a %Y-%m-%d %H:%M} ET → {'LIVE' if live else 'skip'}: {reason}")
    if reason.startswith("WARNING") or "(WARNING" in reason:
        print(f"::warning::{reason}")
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a") as f:
            f.write(f"live={str(live).lower()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
