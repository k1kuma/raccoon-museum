"""Incremental Yahoo Fantasy ingestion via yfpy -> SQLite.

Design:
- Walk the league's renew/renewed chain to discover every season.
- Finished seasons are fetched once and then marked complete in fetch_log (never re-fetched unless --full).
- In-progress seasons refresh standings/transactions every run, and only fetch newly completed weeks.
- Every yfpy result is converted to plain JSON and parsed defensively (Yahoo's shapes vary by endpoint/year).
"""
from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any

from . import db, yahoo_auth

log = logging.getLogger("museum.ingest")


class StopIngest(Exception):
    pass


# ---------- defensive JSON helpers ----------
def unwrap(item: Any, key: str) -> Any:
    if isinstance(item, dict) and len(item) == 1 and key in item:
        return item[key]
    return item


def as_list(value: Any, key: str) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, dict):
        if key in value:
            inner = value[key]
            return [unwrap(i, key) for i in inner] if isinstance(inner, list) else [inner]
        plural = key + "s"
        if plural in value:
            return as_list(value[plural], key)
        return [value]
    if isinstance(value, list):
        return [unwrap(i, key) for i in value]
    return []


def dig(obj: Any, *path: str, default: Any = None) -> Any:
    cur = obj
    for p in path:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return default
        if cur is None:
            return default
    return cur


def num(value: Any, default: float | None = None) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def intval(value: Any, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def player_name(p: dict) -> str:
    name = p.get("name")
    if isinstance(name, dict):
        return name.get("full") or " ".join(filter(None, [name.get("first"), name.get("last")]))
    return str(name or p.get("player_key") or "Unknown")


def positions(p: dict) -> list[str]:
    raw = p.get("eligible_positions")
    out = []
    for item in as_list(raw, "position"):
        if isinstance(item, dict):
            item = item.get("position")
        if isinstance(item, str):
            out.append(item)
    return out


# ---------- Yahoo client ----------
class Yahoo:
    def __init__(self, delay: float, deadline: float):
        self.client_id = os.environ.get("YAHOO_CLIENT_ID", "")
        self.client_secret = os.environ.get("YAHOO_CLIENT_SECRET", "")
        self.refresh_token = os.environ.get("YAHOO_REFRESH_TOKEN", "")
        token_json = os.environ.get("YAHOO_ACCESS_TOKEN_JSON")
        if token_json and not self.refresh_token:
            self.refresh_token = json.loads(token_json).get("refresh_token", "")
        if not (self.client_id and self.client_secret and self.refresh_token):
            raise SystemExit("Missing YAHOO_CLIENT_ID / YAHOO_CLIENT_SECRET / YAHOO_REFRESH_TOKEN. "
                             "Run scripts/auth.py first (see README).")
        self.delay = delay
        self.deadline = deadline
        self.token: dict | None = None
        self.queries: dict[tuple, Any] = {}
        self.calls = 0

    def _ensure_token(self) -> None:
        if self.token and time.time() - self.token["token_time"] < 40 * 60:
            return
        log.info("Refreshing Yahoo access token")
        resp = yahoo_auth.refresh(self.client_id, self.client_secret, self.refresh_token)
        if resp.get("refresh_token") and resp["refresh_token"] != self.refresh_token:
            log.warning("Yahoo issued a new refresh token; update the YAHOO_REFRESH_TOKEN secret if runs start failing.")
            self.refresh_token = resp["refresh_token"]
        resp.setdefault("refresh_token", self.refresh_token)
        self.token = yahoo_auth.yfpy_token_dict(self.client_id, self.client_secret, resp)
        self.queries.clear()

    def query(self, league_id: str, game_id: str | None):
        from yfpy.query import YahooFantasySportsQuery

        self._ensure_token()
        key = (league_id, game_id)
        if key not in self.queries:
            self.queries[key] = YahooFantasySportsQuery(
                league_id=str(league_id), game_code="nfl", game_id=int(game_id) if game_id else None,
                yahoo_consumer_key=self.client_id, yahoo_consumer_secret=self.client_secret,
                yahoo_access_token_json=dict(self.token), env_var_fallback=False, browser_callback=False,
                retries=4, backoff=2,
            )
        return self.queries[key]

    def call(self, league_id: str, game_id: str | None, method: str, *args) -> Any:
        from yfpy.utils import jsonify_data

        if time.time() > self.deadline:
            raise StopIngest("time budget exhausted")
        q = self.query(league_id, game_id)
        time.sleep(self.delay)
        self.calls += 1
        result = getattr(q, method)(*args)
        return json.loads(jsonify_data(result))


# ---------- ingestion ----------
def discover_chain(y: Yahoo, league_id: str, game_id: str | None) -> list[tuple[str, str | None, dict]]:
    """Returns [(league_id, game_id, metadata)] for every reachable season, oldest first."""
    seen: dict[str, tuple[str, str | None, dict]] = {}

    def fetch(lid: str, gid: str | None, errors: list[str] | None = None) -> dict | None:
        try:
            return y.call(lid, gid, "get_league_metadata")
        except StopIngest:
            raise
        except Exception as exc:  # noqa: BLE001 - Yahoo returns many error shapes
            if errors is not None:
                errors.append(str(exc))
            log.warning("Cannot access league %s (game %s): %s", lid, gid, exc)
            return None

    errors: list[str] = []
    start = fetch(league_id, game_id, errors)
    if not start:
        if any("not authorized" in e.lower() for e in errors):
            raise SystemExit(
                "Yahoo rejected the request: 'This application is not authorized to perform this action'.\n"
                "Login worked, but your Yahoo app has no Fantasy Sports API access. Since mid-2026 Yahoo requires\n"
                "approval: apply with your app's Client ID at https://sports.yahoo.com/developer/access/ \n"
                "(see https://github.com/uberfastman/yfpy/issues/84)."
            )
        raise SystemExit(f"Could not read league {league_id}. If it is not this year's league, set YAHOO_GAME_ID.")
    gid = str(start.get("game_id") or (start.get("league_key", "").split(".")[0]) or game_id)
    seen[str(start["season"])] = (league_id, gid, start)

    for direction in ("renew", "renewed"):
        meta = start
        while meta and meta.get(direction):
            prev_gid, prev_lid = str(meta[direction]).split("_", 1)
            meta = fetch(prev_lid, prev_gid)
            if meta and str(meta.get("season")) not in seen:
                seen[str(meta["season"])] = (prev_lid, prev_gid, meta)
            else:
                break
    return [seen[s] for s in sorted(seen)]


def ingest_season(y: Yahoo, conn, league_id: str, game_id: str, meta: dict, full: bool) -> None:
    season = intval(meta.get("season"))
    league_key = meta.get("league_key") or f"{game_id}.l.{league_id}"
    finished = intval(meta.get("is_finished"), 0) == 1
    if finished and not full and db.was_fetched(conn, f"season:{season}:complete"):
        log.info("%s: complete in cache, skipping", season)
        return
    log.info("%s: ingesting %s (finished=%s)", season, league_key, finished)

    settings = y.call(league_id, game_id, "get_league_settings")
    roster_positions = []
    for rp in as_list(settings.get("roster_positions"), "roster_position"):
        pos = rp.get("position") if isinstance(rp, dict) else None
        count = intval(rp.get("count"), 1) if isinstance(rp, dict) else 1
        if pos and pos not in ("BN", "IR", "IR+", "NA"):
            roster_positions.extend([pos] * count)

    start_week = intval(meta.get("start_week"), 1)
    end_week = intval(meta.get("end_week"), 17)
    current_week = intval(meta.get("current_week"), end_week)
    last_completed = end_week if finished else max(start_week - 1, current_week - 1)

    db.upsert(conn, "seasons", {
        "season": season, "league_key": league_key, "league_id": str(league_id), "game_id": str(game_id),
        "name": meta.get("name"), "num_teams": intval(meta.get("num_teams")), "start_week": start_week,
        "end_week": end_week, "playoff_start_week": intval(settings.get("playoff_start_week")),
        "num_playoff_teams": intval(settings.get("num_playoff_teams")), "current_week": current_week,
        "last_completed_week": last_completed, "is_finished": int(finished),
        "roster_positions": json.dumps(roster_positions), "updated_at": datetime.now(timezone.utc).isoformat(),
    })

    standings = y.call(league_id, game_id, "get_league_standings")
    for t in as_list(dig(standings, "teams") or standings, "team"):
        ts = t.get("team_standings") or {}
        totals = ts.get("outcome_totals") or {}
        managers = as_list(t.get("managers"), "manager")
        m = managers[0] if managers else {}
        logos = as_list(t.get("team_logos"), "team_logo")
        db.upsert(conn, "teams", {
            "team_key": t.get("team_key"), "season": season, "team_id": intval(t.get("team_id")),
            "name": t.get("name"), "manager_guid": m.get("guid"), "manager_nickname": m.get("nickname"),
            "logo_url": logos[0].get("url") if logos and isinstance(logos[0], dict) else None,
            "final_rank": intval(ts.get("rank")), "playoff_seed": intval(ts.get("playoff_seed")),
            "wins": intval(totals.get("wins"), 0), "losses": intval(totals.get("losses"), 0),
            "ties": intval(totals.get("ties"), 0), "points_for": num(ts.get("points_for"), 0.0),
            "points_against": num(ts.get("points_against"), 0.0),
            "moves": intval(t.get("number_of_moves")), "trades": intval(t.get("number_of_trades")),
            "draft_grade": t.get("draft_grade") or None,
        })
    conn.commit()
    team_ids = {r["team_key"]: r["team_id"] for r in conn.execute(
        "SELECT team_key, team_id FROM teams WHERE season = ?", (season,))}

    week_starts: dict[int, str] = {}
    for week in range(start_week, last_completed + 1):
        key = f"week:{season}:{week}"
        if db.was_fetched(conn, key) and not full:
            continue
        matchups = y.call(league_id, game_id, "get_league_matchups_by_week", week)
        for idx, mu in enumerate(as_list(matchups, "matchup")):
            teams = as_list(mu.get("teams"), "team")
            if len(teams) != 2:
                continue
            tied = intval(mu.get("is_tied"), 0) == 1
            week_starts[week] = mu.get("week_start") or ""
            for me, them in ((teams[0], teams[1]), (teams[1], teams[0])):
                mp = num(dig(me, "team_points", "total"), 0.0)
                tp = num(dig(them, "team_points", "total"), 0.0)
                winner = mu.get("winner_team_key")
                result = "T" if tied else ("W" if winner == me.get("team_key") else "L") if winner else \
                    ("W" if mp > tp else "L" if mp < tp else "T")
                db.upsert(conn, "matchups", {
                    "season": season, "week": week, "matchup_idx": idx, "team_key": me.get("team_key"),
                    "opponent_key": them.get("team_key"), "points": mp, "opponent_points": tp,
                    "projected": num(dig(me, "team_projected_points", "total")),
                    "is_playoffs": intval(mu.get("is_playoffs"), 0),
                    "is_consolation": intval(mu.get("is_consolation"), 0), "result": result,
                    "week_start": mu.get("week_start"),
                })
        for team_key, team_id in team_ids.items():
            try:
                players = y.call(league_id, game_id, "get_team_roster_player_stats_by_week", team_id, week)
            except StopIngest:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("%s wk%s team %s roster unavailable: %s", season, week, team_id, exc)
                continue
            for p in as_list(players, "player"):
                db.upsert(conn, "roster_slots", {
                    "season": season, "week": week, "team_key": team_key, "player_key": p.get("player_key"),
                    "player_name": player_name(p), "eligible_positions": ",".join(positions(p)),
                    "primary_position": p.get("primary_position") or p.get("display_position"),
                    "selected_position": dig(p, "selected_position", "position"),
                    "points": num(dig(p, "player_points", "total"), 0.0),
                })
        db.mark_fetched(conn, key)
        conn.commit()
        log.info("%s: week %s stored (%s API calls so far)", season, week, y.calls)

    ingest_draft(y, conn, league_id, game_id, season, finished, last_completed, full)
    ingest_transactions(y, conn, league_id, game_id, season)

    if finished:
        db.mark_fetched(conn, f"season:{season}:complete")
    conn.commit()


def ingest_draft(y: Yahoo, conn, league_id, game_id, season, finished, last_completed, full) -> None:
    key = f"draft:{season}:{'final' if finished else last_completed}"
    if db.was_fetched(conn, key) and not full:
        return
    results = y.call(league_id, game_id, "get_league_draft_results")
    for d in as_list(results, "draft_result"):
        player_key = d.get("player_key")
        if not player_key:
            continue
        name, position, points = None, None, None
        try:
            info = y.call(league_id, game_id, "get_player_stats_for_season", player_key)
            name = player_name(info)
            position = info.get("display_position") or info.get("primary_position")
            points = num(dig(info, "player_points", "total"), 0.0)
        except StopIngest:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("player %s season stats unavailable: %s", player_key, exc)
        db.upsert(conn, "draft_picks", {
            "season": season, "pick": intval(d.get("pick")), "round": intval(d.get("round")),
            "team_key": d.get("team_key"), "player_key": player_key, "player_name": name or player_key,
            "position": position, "season_points": points, "cost": intval(d.get("cost")),
        })
    db.mark_fetched(conn, key)
    conn.commit()


def ingest_transactions(y: Yahoo, conn, league_id, game_id, season) -> None:
    weeks = [(r["week"], r["week_start"]) for r in conn.execute(
        "SELECT DISTINCT week, week_start FROM matchups WHERE season = ? AND week_start IS NOT NULL ORDER BY week",
        (season,))]

    def week_for(ts: int | None) -> int | None:
        if not ts:
            return None
        day = datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
        current = weeks[0][0] if weeks else None
        for w, start in weeks:
            if start and start <= day:
                current = w
        return current

    txs = y.call(league_id, game_id, "get_league_transactions")
    for tx in as_list(txs, "transaction"):
        tkey = tx.get("transaction_key")
        if not tkey:
            continue
        ts = intval(tx.get("timestamp"))
        db.upsert(conn, "transactions", {
            "transaction_key": tkey, "season": season, "type": tx.get("type"), "status": tx.get("status"),
            "timestamp": ts, "week": week_for(ts),
        })
        for p in as_list(tx.get("players"), "player"):
            data = p.get("transaction_data")
            data = as_list(data, "transaction_data")[0] if data else {}
            db.upsert(conn, "transaction_players", {
                "transaction_key": tkey, "player_key": p.get("player_key"), "player_name": player_name(p),
                "position": p.get("display_position"), "action": data.get("type") or tx.get("type"),
                "source_team_key": data.get("source_team_key") or None,
                "destination_team_key": data.get("destination_team_key") or None,
            })
    conn.commit()


def live_snapshot_from_matchups(matchups: list[dict], season: int, week: int) -> dict:
    """Convert the API's current-week scoreboard into the site's small live payload."""
    games = []
    statuses = []
    for matchup in as_list(matchups, "matchup"):
        teams = as_list(matchup.get("teams"), "team")
        if len(teams) != 2:
            continue
        statuses.append(str(matchup.get("status") or "").lower())
        games.append({"label": matchup.get("matchup_recap_title") or "", "teams": [
            {"teamKey": team.get("team_key"),
             "points": num(dig(team, "team_points", "total"), 0.0),
             "projected": num(dig(team, "team_projected_points", "total"))}
            for team in teams
        ]})
    if not games:
        raise SystemExit("Yahoo returned no matchups for the current week.")
    return {
        "season": season,
        "week": week,
        # Yahoo reports a matchup as postevent once its scoring is final.
        "final": bool(statuses) and all(status == "postevent" for status in statuses),
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "games": games,
    }


def run_live(args) -> int:
    """Fetch the current scoreboard via the approved Yahoo Fantasy API."""
    y = Yahoo(delay=args.delay, deadline=time.time() + args.time_budget * 60)
    game_id = os.environ.get("YAHOO_GAME_ID") or None
    metadata = y.call(args.league_id, game_id, "get_league_metadata")
    season = intval(metadata.get("season"))
    week = intval(metadata.get("current_week"))
    resolved_game_id = str(metadata.get("game_id") or game_id or "") or None
    if not season or not week:
        raise SystemExit("Yahoo did not return the current season/week for this league.")
    matchups = y.call(args.league_id, resolved_game_id, "get_league_matchups_by_week", week)
    out = getattr(args, "live_out", None) or config.LIVE_JSON
    snapshot = live_snapshot_from_matchups(matchups, season, week)
    out.write_text(json.dumps(snapshot, ensure_ascii=False))
    log.info("Live: %s week %s, %s games, final=%s → %s", season, week, len(snapshot["games"]),
             snapshot["final"], out)
    return 0


def run(args) -> int:
    if getattr(args, "live", False):
        return run_live(args)
    deadline = time.time() + args.time_budget * 60
    y = Yahoo(delay=args.delay, deadline=deadline)
    # Discover before touching the DB so a failed login never leaves an empty league.sqlite behind.
    chain = discover_chain(y, args.league_id, os.environ.get("YAHOO_GAME_ID") or None)
    conn = db.connect(args.db)
    try:
        log.info("Discovered seasons: %s", ", ".join(str(m.get("season")) for _, _, m in chain))
        for league_id, game_id, meta in reversed(chain):  # newest first so this week's data lands quickly
            if args.season and intval(meta.get("season")) not in args.season:
                continue
            try:
                ingest_season(y, conn, league_id, game_id, meta, args.full)
            except StopIngest:
                raise
            except Exception:  # noqa: BLE001 - one bad season shouldn't sink the run
                log.exception("Season %s failed; continuing", meta.get("season"))
                conn.rollback()
    except StopIngest as exc:
        log.warning("Stopping early (%s); progress is saved and the next run resumes.", exc)
    db.set_meta(conn, "is_sample", "0")
    db.set_meta(conn, "last_ingest", datetime.now(timezone.utc).isoformat())
    conn.commit()
    conn.close()
    log.info("Done. %s API calls.", y.calls)
    return 0
