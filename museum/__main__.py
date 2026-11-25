"""CLI: python -m museum {sample,ingest,scrape,stats,managers}"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="museum")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="generate the sample/fixture league database")
    p_sample.add_argument("--db", type=Path, default=config.SAMPLE_DB)

    p_ingest = sub.add_parser("ingest", help="pull data from Yahoo into SQLite (incremental)")
    p_ingest.add_argument("--db", type=Path, default=config.LEAGUE_DB)
    p_ingest.add_argument("--league-id", default=config.LEAGUE_ID)
    p_ingest.add_argument("--full", action="store_true", help="re-fetch everything, ignoring the cache")
    p_ingest.add_argument("--season", type=int, action="append", help="only ingest these season(s)")
    p_ingest.add_argument("--live", action="store_true",
                          help="only read the current week's scoreboard into data/live.json; the database is not touched")
    p_ingest.add_argument("--time-budget", type=float, default=330, help="minutes before stopping gracefully")
    p_ingest.add_argument("--delay", type=float, default=0.35, help="seconds between API calls")

    p_scrape = sub.add_parser("scrape", help="pull data from Yahoo's web pages using browser cookies (YAHOO_COOKIES)")
    p_scrape.add_argument("--db", type=Path, default=config.LEAGUE_DB)
    p_scrape.add_argument("--league-id", help="current season's league id (default: YAHOO_LEAGUE_ID)")
    p_scrape.add_argument("--seasons", help="season:league_id pairs (default: YAHOO_SEASONS or config.SEASONS)")
    p_scrape.add_argument("--full", action="store_true", help="re-fetch everything, ignoring the cache")
    p_scrape.add_argument("--season", type=int, action="append", help="only scrape these season(s)")
    p_scrape.add_argument("--time-budget", type=float, default=300, help="minutes before stopping gracefully")
    p_scrape.add_argument("--delay", type=float, default=2.0, help="seconds between page requests")
    p_scrape.add_argument("--live", action="store_true",
                          help="only read the current week's scoreboard (1 request) into data/live.json; "
                               "the database is not touched")

    p_stats = sub.add_parser("stats", help="compute stats JSON for the site")
    p_stats.add_argument("--db", type=Path, help="defaults to league.sqlite if present, else sample.sqlite")
    p_stats.add_argument("--out", type=Path, default=config.STATS_OUT)

    p_mgr = sub.add_parser("managers", help="list teams/GUIDs to help fill in data/managers.yaml")
    p_mgr.add_argument("--db", type=Path, default=config.LEAGUE_DB)

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    config.load_dotenv()

    if args.cmd == "sample":
        from .sample import generate

        path = generate(args.db)
        print(f"Sample league written to {path}")
    elif args.cmd == "ingest":
        from .ingest import run

        return run(args)
    elif args.cmd == "scrape":
        import os

        from .scrape import run

        args.league_id = args.league_id or os.environ.get("YAHOO_LEAGUE_ID") or config.LEAGUE_ID
        args.seasons = args.seasons or os.environ.get("YAHOO_SEASONS") or config.SEASONS
        return run(args)
    elif args.cmd == "stats":
        from .stats import build

        from .db import has_league_data

        db_path = args.db or (config.LEAGUE_DB if has_league_data(config.LEAGUE_DB) else config.SAMPLE_DB)
        if not db_path.exists():
            print(f"No database at {db_path}. Run `python -m museum sample` or `ingest` first.", file=sys.stderr)
            return 1
        build(db_path, args.out)
        print(f"Stats from {db_path.name} written to {args.out}")
    elif args.cmd == "managers":
        from . import db

        if not db.has_league_data(args.db):
            print(f"No league data in {args.db} yet — `python -m museum scrape` (or `ingest`) has to succeed first.\n"
                  "(The sample league's managers are already mapped in data/sample_managers.yaml.)", file=sys.stderr)
            return 1
        conn = db.connect(args.db)
        rows = conn.execute(
            "SELECT season, team_id, name, manager_guid, manager_nickname FROM teams ORDER BY manager_guid, season"
        ).fetchall()
        for r in rows:
            print(f"{r['season']}:{r['team_id']:<3} guid={r['manager_guid'] or '-':<28} "
                  f"nick={r['manager_nickname'] or '-':<16} team={r['name']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
