"""Turn the SQLite league history into precomputed JSON for the Astro site.

Every stat entry carries a story-style caption and an `isNew` flag (set when it happened in the latest week).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

from . import config, db, lineup
from . import draft as draft_mod
from . import season as season_mod
from .managers import Resolver, load_config
from .preview import matchup_previews, point_spread

TOP_N = 25
# Record Book segments, and the game types each one includes. Consolation games only appear in the Toilet Bowl.
SEGMENTS = ("regular", "playoffs", "all")
SEGMENT_TYPES = {"regular": {"regular"}, "playoffs": {"playoff"}, "all": {"regular", "playoff"},
                 "consolation": {"consolation"}}
PALETTE = ["#c2410c", "#0f766e", "#7c3aed", "#b91c1c", "#1d4ed8", "#a16207", "#15803d", "#be185d",
           "#4338ca", "#0e7490", "#9333ea", "#65a30d", "#ea580c", "#475569", "#db2777", "#0369a1"]


def f2(x: float | None) -> str:
    return "—" if x is None else f"{x:,.2f}"


def ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def record_str(w: int, l: int, t: int = 0) -> str:
    return f"{w}-{l}" + (f"-{t}" if t else "")


def week_marks(scores: list[float]) -> tuple[float, float]:
    """(median, average) of one week's scores across the whole league."""
    xs = sorted(scores)
    n = len(xs)
    if not n:
        return 0.0, 0.0
    med = xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2
    return round(med, 2), round(sum(xs) / n, 2)


def vs_mark(points: float, mark: float) -> str:
    """Result of 'playing' the league median or average: W, L or T (exact tie)."""
    return "W" if points > mark else "L" if points < mark else "T"


def carry_share(player_points: dict[str, float]) -> dict:
    """How concentrated a team's starting points are.

    shares: each player's fraction of the starters' total; top1/top3: fraction from the best 1 and 3 players;
    hhi: Herfindahl index (sum of squared shares; 1 = one player scored everything); effective: 1 / hhi, the
    number of equally productive starters that would give the same concentration."""
    pos = {k: max(0.0, v) for k, v in player_points.items()}
    total = sum(pos.values())
    if total <= 0:
        return {"total": 0.0, "shares": {}, "top1": 0.0, "top3": 0.0, "hhi": 0.0, "effective": 0.0}
    shares = {k: v / total for k, v in pos.items()}
    ranked = sorted(shares.values(), reverse=True)
    hhi = sum(x * x for x in ranked)
    return {"total": round(total, 2), "shares": shares, "top1": round(ranked[0], 4),
            "top3": round(sum(ranked[:3]), 4), "hhi": round(hhi, 4), "effective": round(1 / hhi, 2)}


class Museum:
    def __init__(self, conn: sqlite3.Connection, manager_config: dict, logos_dir: Path | None = None):
        self.conn = conn
        q = lambda sql: [dict(r) for r in conn.execute(sql).fetchall()]  # noqa: E731
        self.seasons = {r["season"]: r for r in q("SELECT * FROM seasons ORDER BY season")}
        self.teams = {r["team_key"]: r for r in q("SELECT * FROM teams")}
        self.rows = q("SELECT * FROM matchups WHERE points IS NOT NULL ORDER BY season, week, matchup_idx")
        self.slots = q("SELECT * FROM roster_slots")
        self.picks = q("SELECT * FROM draft_picks ORDER BY season, pick")
        self.txs = q("SELECT * FROM transactions ORDER BY season, timestamp")
        self.tx_players = q("SELECT * FROM transaction_players")
        self.is_sample = db.get_meta(conn, "is_sample") == "1"

        self.resolver = Resolver(manager_config, list(self.teams.values()))
        self.lore = manager_config.get("lore", [])
        used = {self.resolver.manager_for(k) for k in self.teams}
        self.managers = {mid: m for mid, m in self.resolver.managers.items() if mid in used}
        for i, mid in enumerate(sorted(self.managers)):
            m = self.managers[mid]
            m["color"] = PALETTE[i % len(PALETTE)]
            m["initials"] = "".join(w[0] for w in m["name"].replace("-", " ").split()[:2]).upper() or "?"

        self.logos = self.find_logos(logos_dir or config.LOGOS_DIR)
        for mid, m in self.managers.items():
            m["logo"] = next((self.logos[y][mid] for y in sorted(self.logos, reverse=True) if mid in self.logos[y]),
                             None)

        self.classify_games()
        for r in self.rows:
            r["manager"] = self.mgr(r["team_key"])
            r["opponent"] = self.mgr(r["opponent_key"]) if r["opponent_key"] else None
            r["margin"] = round((r["points"] or 0) - (r["opponent_points"] or 0), 2)
            r["team_name"] = self.teams.get(r["team_key"], {}).get("name", "")
            r["opp_team_name"] = self.teams.get(r["opponent_key"], {}).get("name", "") if r["opponent_key"] else ""

        if self.rows:
            last = max((r["season"], r["week"]) for r in self.rows)
        else:
            last = (max(self.seasons) if self.seasons else datetime.now().year, 0)
        self.latest = {"season": last[0], "week": last[1]}

    # ---------- game types ----------
    def classify_games(self) -> None:
        """Tag every game with exactly one game_type: regular, playoff (winners' bracket) or consolation.

        Uses the league settings (playoff start week, number of playoff teams) and the final seeds, because Yahoo
        flags consolation games as playoffs too. Falls back to Yahoo's flags when settings or seeds are missing.
        Playoff games also get title_path (neither team eliminated yet) and a round_label such as "Semifinal",
        "Championship" or "5th place"."""
        seed = {k: t.get("playoff_seed") for k, t in self.teams.items()}
        for r in self.rows:
            s = self.seasons.get(r["season"], {})
            start, n = s.get("playoff_start_week"), s.get("num_playoff_teams")
            a, b = seed.get(r["team_key"]), seed.get(r["opponent_key"])
            if start and r["week"] < start:
                kind = "regular"
            elif start and n and a and b:
                kind = "playoff" if a <= n and b <= n else "consolation"
            elif r["is_consolation"]:
                kind = "consolation"
            elif r["is_playoffs"]:
                kind = "playoff"
            else:
                kind = "regular"
            r["game_type"] = kind
            r["is_playoffs"] = int(kind == "playoff")
            r["is_consolation"] = int(kind == "consolation")
            r["title_path"] = False
            r["round_label"] = None

        by_season = defaultdict(list)
        last_week: dict[str, int] = {}
        for r in self.rows:
            last_week[r["team_key"]] = max(last_week.get(r["team_key"], 0), r["week"])
            if r["game_type"] != "regular":
                by_season[r["season"]].append(r)
        for season, games in by_season.items():
            s = self.seasons.get(season, {})
            end = s.get("end_week")
            eliminated: set[str] = set()
            for week in sorted({g["week"] for g in games}):
                losers = set()
                for g in (g for g in games if g["week"] == week):
                    g["title_path"] = (g["game_type"] == "playoff" and g["team_key"] not in eliminated
                                       and g["opponent_key"] not in eliminated)
                    if g["title_path"] and g["result"] == "L":
                        losers.add(g["team_key"])
                eliminated |= losers
            for g in games:
                ra = self.teams.get(g["team_key"], {}).get("final_rank")
                rb = self.teams.get(g["opponent_key"], {}).get("final_rank")
                placement = (self.finished(season) and ra and rb and abs(ra - rb) == 1
                             and last_week.get(g["team_key"]) == g["week"] == last_week.get(g["opponent_key"]))
                if g["title_path"]:
                    g["round_label"] = {0: "Championship", 1: "Semifinal", 2: "Quarterfinal"}.get(
                        (end or g["week"]) - g["week"], "Playoffs")
                elif placement:
                    g["round_label"] = f"{ordinal(min(ra, rb))} place"
                else:
                    g["round_label"] = "Consolation" if g["game_type"] == "consolation" else "Placement"

    def games(self, segment: str = "regular") -> list[dict]:
        types = SEGMENT_TYPES[segment]
        return [r for r in self.rows if r["game_type"] in types]

    # ---------- helpers ----------
    def mgr(self, team_key: str | None) -> str | None:
        return self.resolver.manager_for(team_key) if team_key else None

    def name(self, mid: str | None) -> str:
        return self.managers.get(mid, {}).get("name", "Unknown") if mid else "Unknown"

    def is_new(self, season: int | None, week: int | None) -> bool:
        return season == self.latest["season"] and week == self.latest["week"]

    def finished(self, season: int) -> bool:
        return bool(self.seasons.get(season, {}).get("is_finished"))

    def week_label(self, r: dict) -> str:
        base = f"Week {r['week']}, {r['season']}"
        if r.get("round_label"):
            return base + f" ({r['round_label']})"
        return base

    def round_name(self, season: int, week: int) -> str:
        s = self.seasons.get(season, {})
        end = s.get("end_week") or week
        offset = end - week
        return {0: "Championship week", 1: "Semifinals", 2: "Quarterfinals"}.get(offset, "Playoffs")

    def entry(self, value, display, caption, manager=None, season=None, week=None, opponent=None, **extra) -> dict:
        e = {"value": value, "display": display, "caption": caption, "manager": manager, "season": season,
             "week": week, "opponent": opponent, "isNew": self.is_new(season, week)}
        e.update(extra)
        return e

    # ---------- streaks ----------
    def streaks(self, segment: str = "regular") -> tuple[list[dict], list[dict], dict[str, dict]]:
        by_mgr = defaultdict(list)
        for r in self.games(segment):
            by_mgr[r["manager"]].append(r)
        wins, losses, active = [], [], {}
        for mid, games in by_mgr.items():
            games.sort(key=lambda r: (r["season"], r["week"]))
            run, kind = [], None
            for g in games + [None]:
                res = g["result"] if g else None
                if g and res == kind and res in ("W", "L"):
                    run.append(g)
                    continue
                if run and kind in ("W", "L"):
                    s = {"manager": mid, "kind": kind, "length": len(run), "start": run[0], "end": run[-1],
                         "active": g is None and (segment == "regular" or not self.finished(run[-1]["season"])),
                         "segment": segment}
                    (wins if kind == "W" else losses).append(s)
                    if g is None:
                        active[mid] = s
                run, kind = ([g], res) if g else ([], None)
        return wins, losses, active

    def streak_entry(self, s: dict) -> dict:
        word = "won" if s["kind"] == "W" else "lost"
        a, b = s["start"], s["end"]
        span = f"Week {a['week']}, {a['season']} through Week {b['week']}, {b['season']}"
        tail = " — and the streak is still alive." if s["active"] else "."
        what = {"playoffs": " playoff games", "all": " (regular season and playoffs)"}.get(s.get("segment"), "")
        return self.entry(s["length"], f"{s['length']} games",
                          f"{self.name(s['manager'])} {word} {s['length']} straight{what} from {span}{tail}",
                          manager=s["manager"], season=b["season"], week=b["week"], active=s["active"],
                          span=span)

    # ---------- bench ----------
    def bench_rows(self) -> list[dict]:
        starters_by_season = {s: json.loads(v.get("roster_positions") or "[]") for s, v in self.seasons.items()}
        grouped = defaultdict(list)
        for p in self.slots:
            grouped[(p["season"], p["week"], p["team_key"])].append(p)
        game = {(r["season"], r["week"], r["team_key"]): r for r in self.rows}
        out = []
        for key, players in grouped.items():
            slots = [s for s in starters_by_season.get(key[0], []) if s not in lineup.BENCH_SLOTS]
            if not slots or key not in game:
                continue
            actual = lineup.actual_points(players)
            best = lineup.optimal_points(slots, players)
            g = game[key]
            out.append({**g, "actual": actual, "optimal": best, "left": round(max(0.0, best - actual), 2),
                        "would_have_won": g["result"] != "W" and best > (g["opponent_points"] or 0)})
        return out

    # ---------- draft ----------
    def draft_values(self) -> list[dict]:
        by_season = defaultdict(list)
        for p in self.picks:
            by_season[p["season"]].append(p)
        out = []
        for season, picks in by_season.items():
            ranked = sorted(picks, key=lambda p: -(p["season_points"] or 0))
            prank = {p["pick"]: i + 1 for i, p in enumerate(ranked)}
            for p in picks:
                out.append({**p, "manager": self.mgr(p["team_key"]), "points_rank": prank[p["pick"]],
                            "value": p["pick"] - prank[p["pick"]], "final": self.finished(season)})
        return out

    def pick_caption(self, p: dict) -> str:
        pts = f2(p["season_points"])
        when = "" if p["final"] else " so far"
        return (f"{self.name(p['manager'])} used the #{p['pick']} overall pick (Round {p['round']}) on "
                f"{p['player_name']}{f' ({p["position"]})' if p['position'] else ''} in {p['season']}. {p['player_name']} finished {ordinal(p['points_rank'])} "
                f"among drafted players with {pts} points{when}.")

    # ---------- trades ----------
    def trades(self) -> list[dict]:
        started = defaultdict(float)  # (season, team_key, player_key, from_week) -> points as starter
        slots_by = defaultdict(list)
        counted = {(r["season"], r["week"], r["team_key"]) for r in self.games("all")}  # no consolation weeks
        for s in self.slots:
            if s["selected_position"] not in lineup.BENCH_SLOTS and (s["season"], s["week"], s["team_key"]) in counted:
                slots_by[(s["season"], s["team_key"], s["player_key"])].append((s["week"], s["points"] or 0))

        def pts_after(season, team_key, player_key, week):
            return sum(p for w, p in slots_by.get((season, team_key, player_key), []) if w >= week)

        players_by_tx = defaultdict(list)
        for p in self.tx_players:
            players_by_tx[p["transaction_key"]].append(p)
        out = []
        for tx in self.txs:
            if tx["type"] != "trade" or tx["status"] not in (None, "", "successful"):
                continue
            moves = [m for m in players_by_tx[tx["transaction_key"]] if m["destination_team_key"]]
            sides = defaultdict(lambda: {"received": [], "sent": []})
            for m in moves:
                sides[m["destination_team_key"]]["received"].append(m)
                if m["source_team_key"]:
                    sides[m["source_team_key"]]["sent"].append(m)
            week = tx["week"] or 1
            for team_key, side in sides.items():
                got = sum(pts_after(tx["season"], team_key, m["player_key"], week) for m in side["received"])
                gave = sum(pts_after(tx["season"], m["destination_team_key"], m["player_key"], week)
                           for m in side["sent"])
                partners = sorted({self.mgr(m["destination_team_key"]) for m in side["sent"]} |
                                  {self.mgr(m["source_team_key"]) for m in side["received"] if m["source_team_key"]})
                out.append({"transaction_key": tx["transaction_key"], "season": tx["season"], "week": week,
                            "manager": self.mgr(team_key), "partners": [p for p in partners if p],
                            "received": [m["player_name"] for m in side["received"]],
                            "sent": [m["player_name"] for m in side["sent"]],
                            "got": round(got, 2), "gave": round(gave, 2), "net": round(got - gave, 2)})
        return out

    def trade_caption(self, t: dict) -> str:
        partner = ", ".join(self.name(p) for p in t["partners"]) or "a league-mate"
        sent = ", ".join(t["sent"]) or "nothing"
        got = ", ".join(t["received"]) or "nothing"
        return (f"In Week {t['week']}, {t['season']}, {self.name(t['manager'])} sent {sent} to {partner} for {got}. "
                f"The incoming players started for {f2(t['got'])} points; the outgoing ones put up "
                f"{f2(t['gave'])} for their new team.")

    # ---------- derived season stats ----------
    def all_play(self) -> dict[str, dict]:
        """Per team-season: record against every other team every regular-season week, and luck."""
        by_week = defaultdict(list)
        for r in self.games("regular"):
            by_week[(r["season"], r["week"])].append(r)
        out: dict[str, dict] = {}
        for rows in by_week.values():
            for r in rows:
                o = out.setdefault(r["team_key"], {"team_key": r["team_key"], "season": r["season"],
                                                   "manager": r["manager"], "team": r["team_name"], "w": 0, "l": 0,
                                                   "t": 0, "expected": 0.0, "wins": 0.0, "games": 0, "points": 0.0,
                                                   "last_week": 0})
                others = [x["points"] for x in rows if x is not r]
                if not others:
                    continue
                w = sum(1 for p in others if p < r["points"])
                t = sum(1 for p in others if p == r["points"])
                o["w"] += w
                o["t"] += t
                o["l"] += len(others) - w - t
                o["expected"] += (w + 0.5 * t) / len(others)
                o["wins"] += 1 if r["result"] == "W" else 0.5 if r["result"] == "T" else 0
                o["games"] += 1
                o["points"] += r["points"]
                o["last_week"] = max(o["last_week"], r["week"])
        for o in out.values():
            n = o["w"] + o["l"] + o["t"]
            o["pct"] = (o["w"] + 0.5 * o["t"]) / n if n else 0
            o["luck"] = round(o["wins"] - o["expected"], 2)
            o["expected"] = round(o["expected"], 2)
        return out

    def playoff_profile(self) -> dict[str, dict]:
        """Per manager: playoff appearances, byes, appearance rate, regular vs playoff points per game."""
        prof = {mid: {"apps": 0, "byes": 0, "seasons": 0, "regPts": 0.0, "regGames": 0, "poPts": 0.0, "poGames": 0,
                      "poWins": 0, "poLosses": 0, "appSeasons": [], "byeSeasons": []} for mid in self.managers}
        po_week_teams = defaultdict(set)
        for r in self.games("playoffs"):
            po_week_teams[(r["season"], r["week"])].add(r["team_key"])
        for t in self.teams.values():
            s = self.seasons.get(t["season"], {})
            mid = self.mgr(t["team_key"])
            if mid not in prof or not self.finished(t["season"]):
                continue
            p = prof[mid]
            p["seasons"] += 1
            n, start = s.get("num_playoff_teams"), s.get("playoff_start_week")
            if t["playoff_seed"] and n and t["playoff_seed"] <= n:
                p["apps"] += 1
                p["appSeasons"].append(t["season"])
                first = po_week_teams.get((t["season"], start))
                if first and t["team_key"] not in first:
                    p["byes"] += 1
                    p["byeSeasons"].append(t["season"])
        for r in self.games("all"):
            p = prof.get(r["manager"])
            if not p:
                continue
            if r["game_type"] == "regular":
                p["regPts"] += r["points"]
                p["regGames"] += 1
            else:
                p["poPts"] += r["points"]
                p["poGames"] += 1
                p["poWins"] += r["result"] == "W"
                p["poLosses"] += r["result"] == "L"
        for p in prof.values():
            p["regPpg"] = round(p["regPts"] / p["regGames"], 2) if p["regGames"] else None
            p["poPpg"] = round(p["poPts"] / p["poGames"], 2) if p["poGames"] else None
            p["clutch"] = round(p["poPpg"] - p["regPpg"], 2) if p["poPpg"] is not None and p["regPpg"] else None
            p["rate"] = round(p["apps"] / p["seasons"], 3) if p["seasons"] else None
        return prof

    # ---------- median / average benchmarks ----------
    def final_regular(self) -> list[dict]:
        """Regular-season games from completed weeks only (in-progress weeks never count)."""
        out = []
        for r in self.games("regular"):
            s = self.seasons.get(r["season"], {})
            lc = s.get("last_completed_week")
            if not s.get("is_finished") and lc and r["week"] > lc:
                continue
            out.append(r)
        return out

    def benchmarks(self) -> tuple[dict, dict]:
        """League median/average per week, and per team-season: record vs the median, vs the average, the
        'true record' (head-to-head plus median) and a weekly trend."""
        by_week = defaultdict(list)
        for r in self.final_regular():
            by_week[(r["season"], r["week"])].append(r)
        marks = {k: week_marks([r["points"] for r in rows]) for k, rows in by_week.items()}
        idx = {"W": 0, "L": 1, "T": 2}
        teams: dict[str, dict] = {}
        for key in sorted(by_week):
            med, avg = marks[key]
            for r in by_week[key]:
                o = teams.setdefault(r["team_key"], {"team_key": r["team_key"], "season": r["season"],
                                                     "manager": r["manager"], "team": r["team_name"],
                                                     "med": [0, 0, 0], "avg": [0, 0, 0], "h2h": [0, 0, 0],
                                                     "trend": []})
                o["med"][idx[vs_mark(r["points"], med)]] += 1
                o["avg"][idx[vs_mark(r["points"], avg)]] += 1
                if r["result"] in idx:
                    o["h2h"][idx[r["result"]]] += 1
                o["trend"].append({"week": key[1], "pts": r["points"], "med": med, "avg": avg,
                                   "res": r["result"], "opp": r["opponent"]})
        for o in teams.values():
            o["true"] = [a + b for a, b in zip(o["h2h"], o["med"])]
            o["weeks"] = len(o["trend"])
            o["last_week"] = o["trend"][-1]["week"] if o["trend"] else 0
            for k in ("med", "avg", "true"):
                w, l, t = o[k]
                o[k + "Pct"] = round((w + 0.5 * t) / (w + l + t), 3) if (w + l + t) else 0
        return marks, teams

    def carry(self) -> dict[str, dict]:
        """Per team-season (regular season, completed weeks, starters only): who scored the points."""
        games = {(r["season"], r["week"], r["team_key"]): r for r in self.final_regular()}
        grouped = defaultdict(list)
        for p in self.slots:
            key = (p["season"], p["week"], p["team_key"])
            if key in games:
                grouped[key].append(p)
        per_team: dict[str, dict] = defaultdict(lambda: {"players": {}, "weeks": []})
        for key in sorted(grouped):
            players = grouped[key]
            starters = [p for p in players if p["selected_position"] not in lineup.BENCH_SLOTS]
            bench = [p for p in players if p["selected_position"] == "BN"]
            T = per_team[key[2]]
            T["weeks"].append((games[key], starters, bench))
            for p in starters:
                e = T["players"].setdefault(p["player_key"], {"key": p["player_key"], "name": p["player_name"],
                                                              "pos": p["primary_position"], "pts": 0.0,
                                                              "starts": 0})
                e["pts"] += p["points"] or 0
                e["starts"] += 1
        out = {}
        for tk, T in per_team.items():
            c = carry_share({k: e["pts"] for k, e in T["players"].items()})
            if not c["total"]:
                continue
            ranked = sorted(T["players"].values(), key=lambda e: -e["pts"])
            top = ranked[0]
            actual, without = [0, 0, 0], [0, 0, 0]
            idx = {"W": 0, "L": 1, "T": 2}
            for g, starters, bench in T["weeks"]:
                pts = g["points"]
                x = next((p for p in starters if p["player_key"] == top["key"]), None)
                if x:
                    allowed = lineup.slot_allows(x["selected_position"])
                    repl = max((b["points"] or 0 for b in bench
                                if lineup.player_positions(b["eligible_positions"], b["primary_position"]) & allowed),
                               default=0.0)
                    pts = pts - (x["points"] or 0) + repl
                if g["result"] in idx:
                    actual[idx[g["result"]]] += 1
                without[idx[vs_mark(round(pts, 2), g["opponent_points"] or 0)]] += 1
            view = lambda e: {"name": e["name"], "pos": e["pos"], "pts": round(e["pts"], 2),  # noqa: E731
                              "share": round(c["shares"][e["key"]], 4), "starts": e["starts"]}
            shown = [view(e) for e in ranked[:8]]
            g0 = T["weeks"][0][0]
            out[tk] = {"team_key": tk, "season": g0["season"], "manager": g0["manager"], "team": g0["team_name"],
                       "weeks": len(T["weeks"]), "last_week": T["weeks"][-1][0]["week"], "total": c["total"],
                       "top1": c["top1"], "top3": c["top3"], "hhi": c["hhi"], "effective": c["effective"],
                       "top": view(top), "players": shown,
                       "other": {"pts": round(c["total"] - sum(p["pts"] for p in shown if p["pts"] > 0), 2),
                                 "share": round(max(0.0, 1 - sum(p["share"] for p in shown)), 4)},
                       "actual": record_str(*actual), "without": record_str(*without),
                       "lostWins": actual[0] - without[0]}
        return out

    def week_carry(self, season: int, week: int) -> dict | None:
        """The starter with the biggest share of his team's points in one week (non-consolation games)."""
        games = {r["team_key"]: r for r in self.games("all") if r["season"] == season and r["week"] == week}
        best = None
        by_team = defaultdict(list)
        for p in self.slots:
            if p["season"] == season and p["week"] == week and p["team_key"] in games \
                    and p["selected_position"] not in lineup.BENCH_SLOTS:
                by_team[p["team_key"]].append(p)
        for tk, starters in by_team.items():
            total = sum(max(0.0, p["points"] or 0) for p in starters)
            if total <= 0:
                continue
            p = max(starters, key=lambda p: p["points"] or 0)
            share = (p["points"] or 0) / total
            if not best or share > best["share"]:
                best = {"team_key": tk, "manager": games[tk]["manager"], "player": p["player_name"],
                        "pos": p["primary_position"], "pts": p["points"] or 0, "total": total, "share": share,
                        "result": games[tk]["result"]}
        return best

    def season_bench(self, team_key: str) -> dict:
        o, c = self.bm.get(team_key), self.carries.get(team_key)
        out = {"vsMedian": None, "vsAverage": None, "trueRecord": None, "trend": None, "carry": None}
        if o:
            above = sum(1 for w in o["trend"] if w["pts"] > w["med"])
            out.update({"vsMedian": record_str(*o["med"]), "vsAverage": record_str(*o["avg"]),
                        "trueRecord": record_str(*o["true"]),
                        "trend": {"weeks": [{k: w[k] for k in ("week", "pts", "med", "avg", "res")}
                                            | {"opp": self.name(w["opp"]) if w["opp"] else None} for w in o["trend"]],
                                  "aboveMedian": above, "aboveAverage": o["avg"][0],
                                  "summary": f"Above the median in {above} of {o['weeks']} regular-season weeks"
                                             + ("" if self.finished(o["season"]) else " so far") + "."}})
        if c:
            out["carry"] = {k: c[k] for k in ("top1", "top3", "hhi", "effective", "total", "players", "other",
                                              "actual", "without", "lostWins", "weeks")} | {
                "topName": c["top"]["name"], "caption": self.carry_caption(c)}
        return out

    def career_bench(self, mid: str) -> dict:
        mine = [o for o in self.bm.values() if o["manager"] == mid]
        tot = {k: [sum(o[k][i] for o in mine) for i in range(3)] for k in ("med", "avg", "true")}
        return {"vsMedian": record_str(*tot["med"]) if mine else None,
                "vsAverage": record_str(*tot["avg"]) if mine else None,
                "trueRecord": record_str(*tot["true"]) if mine else None}

    def mark_caption(self, o: dict, kind: str) -> str:
        done = self.finished(o["season"])
        when = f"in {o['season']}" if done else f"in {o['season']} so far (through Week {o['last_week']})"
        label = {"med": "the league median", "avg": "the league average"}[kind]
        w, l, t = o[kind]
        return (f"{self.name(o['manager'])} ({o['team']}) beat {label} in {w} of {o['weeks']} weeks {when}: "
                f"{record_str(w, l, t)} against {label}. Head-to-head they went "
                f"{record_str(*o['h2h'])}; counting both, their true record is {record_str(*o['true'])}.")

    def carry_caption(self, c: dict, lead: str = "") -> str:
        done = self.finished(c["season"])
        when = f"in {c['season']}" if done else f"in {c['season']} so far (through Week {c['last_week']})"
        top = c["top"]
        txt = (f"{lead}{top['name']} ({top['pos']}) scored {f2(top['pts'])} of the {f2(c['total'])} starting "
               f"points for {self.name(c['manager'])} ({c['team']}) {when} — {round(top['share'] * 100)}%. "
               f"The top three starters combined for {round(c['top3'] * 100)}%.")
        if c["actual"] != c["without"]:
            txt += (f" Replace {top['name']} with the best eligible bench player each week and {c['actual']} "
                    f"becomes roughly {c['without']}.")
        return txt

    # ---------- categories ----------
    def seg_category(self, slug, title, blurb, build) -> dict:
        """A category with Regular / Playoffs / All variants; `entries` is the regular-season default."""
        lists = {s: build(s) for s in SEGMENTS}
        return self.category(slug, title, blurb, lists["regular"],
                             segments={s: lists[s] for s in SEGMENTS if s != "regular"})

    def record_book(self, streaks: dict, allplay: dict, profile: dict) -> list[dict]:
        cats = []

        def game_caption(r, lead):
            res = {"W": "won", "L": "lost", "T": "tied"}[r["result"]]
            return (f"{lead} {self.name(r['manager'])} ({r['team_name']}) scored {f2(r['points'])} against "
                    f"{self.name(r['opponent'])} in {self.week_label(r)} — and {res} "
                    f"{f2(r['points'])}–{f2(r['opponent_points'])}.")

        def game_entry(r, value, display, caption):
            return self.entry(value, display, caption, r["manager"], r["season"], r["week"], r["opponent"])

        cats.append(self.seg_category(
            "highest-scores", "Highest Single-Week Scores", "The league's offensive explosions.",
            lambda seg: [game_entry(r, r["points"], f2(r["points"]), game_caption(r, "Boom."))
                         for r in sorted(self.games(seg), key=lambda r: -r["points"])[:TOP_N]]))
        cats.append(self.seg_category(
            "lowest-scores", "Lowest Single-Week Scores", "When the lineup just… didn't.",
            lambda seg: [game_entry(r, r["points"], f2(r["points"]), game_caption(r, "Oof."))
                         for r in sorted([r for r in self.games(seg) if r["points"] > 0],
                                         key=lambda r: r["points"])[:TOP_N]]))
        cats.append(self.seg_category(
            "biggest-blowouts", "Biggest Blowouts", "Margins of victory that should be illegal.",
            lambda seg: [game_entry(r, r["margin"], f"+{f2(r['margin'])}",
                                    f"{self.name(r['manager'])} flattened {self.name(r['opponent'])} by "
                                    f"{f2(r['margin'])} points ({f2(r['points'])}–{f2(r['opponent_points'])}) "
                                    f"in {self.week_label(r)}.")
                         for r in sorted([r for r in self.games(seg) if r["result"] == "W"],
                                         key=lambda r: -r["margin"])[:TOP_N]]))
        cats.append(self.seg_category(
            "closest-games", "Closest Games", "Decided by a stat correction and a prayer.",
            lambda seg: [game_entry(r, r["margin"], f"+{f2(r['margin'])}",
                                    f"{self.name(r['manager'])} edged {self.name(r['opponent'])} by just "
                                    f"{f2(r['margin'])} ({f2(r['points'])}–{f2(r['opponent_points'])}) in "
                                    f"{self.week_label(r)}.")
                         for r in sorted([r for r in self.games(seg) if r["result"] == "W" and r["margin"] > 0],
                                         key=lambda r: r["margin"])[:TOP_N]]))
        cats.append(self.seg_category(
            "win-streaks", "Longest Winning Streaks", "Consecutive wins, carried across seasons.",
            lambda seg: [self.streak_entry(s) for s in sorted(
                streaks[seg][0], key=lambda s: (-s["length"], s["end"]["season"]))[:15]]))
        cats.append(self.seg_category(
            "losing-streaks", "Longest Losing Streaks", "Somebody check on them.",
            lambda seg: [self.streak_entry(s) for s in sorted(
                streaks[seg][1], key=lambda s: (-s["length"], s["end"]["season"]))[:15]]))

        # Regular season only.
        seasons = [t for t in self.teams.values() if self.finished(t["season"])]
        pf = sorted(seasons, key=lambda t: -(t["points_for"] or 0))[:TOP_N]
        cats.append(self.category("most-points-season", "Most Points in a Season", "Regular-season points for.",
                                  [self.entry(t["points_for"], f2(t["points_for"]),
                                              f"{self.name(self.mgr(t['team_key']))}'s {t['name']} piled up "
                                              f"{f2(t['points_for'])} regular-season points in {t['season']} on the "
                                              f"way to a {record_str(t['wins'], t['losses'], t['ties'])} record "
                                              f"and the No. {t['playoff_seed'] or '?'} seed.",
                                              self.mgr(t["team_key"]), t["season"]) for t in pf]))

        def pct(t):
            g = (t["wins"] or 0) + (t["losses"] or 0) + (t["ties"] or 0)
            return ((t["wins"] or 0) + 0.5 * (t["ties"] or 0)) / g if g else 0

        def finish(t):
            return " — and won it all." if t["final_rank"] == 1 else \
                f", then finished {ordinal(t['final_rank'] or 0)} after the playoffs."

        best = sorted(seasons, key=lambda t: (-pct(t), -(t["points_for"] or 0)))[:15]
        cats.append(self.category("best-records", "Best Regular-Season Records", "Dominance, by the numbers.",
                                  [self.entry(round(pct(t), 3), record_str(t["wins"], t["losses"], t["ties"]),
                                              f"{self.name(self.mgr(t['team_key']))} went "
                                              f"{record_str(t['wins'], t['losses'], t['ties'])} in the {t['season']} "
                                              f"regular season with {t['name']}" + finish(t),
                                              self.mgr(t["team_key"]), t["season"]) for t in best]))
        ringless = sorted([t for t in seasons if t["final_rank"] and t["final_rank"] != 1],
                          key=lambda t: (-pct(t), -(t["points_for"] or 0)))[:10]
        cats.append(self.category("best-record-no-title", "Best Record Without a Title",
                                  "Owned the regular season. Didn't get the ring.",
                                  [self.entry(round(pct(t), 3), record_str(t["wins"], t["losses"], t["ties"]),
                                              f"{self.name(self.mgr(t['team_key']))} ({t['name']}) went "
                                              f"{record_str(t['wins'], t['losses'], t['ties'])} with "
                                              f"{f2(t['points_for'])} points in {t['season']} as the No. "
                                              f"{t['playoff_seed'] or '?'} seed, and finished "
                                              f"{ordinal(t['final_rank'])}.",
                                              self.mgr(t["team_key"]), t["season"]) for t in ringless]))

        crowns = defaultdict(list)
        for season in sorted(self.seasons):
            if not self.finished(season):
                continue
            ts = [t for t in self.teams.values() if t["season"] == season and t["points_for"]]
            if ts:
                top = max(ts, key=lambda t: t["points_for"])
                crowns[self.mgr(top["team_key"])].append((season, top))
        cats.append(self.category("points-crowns", "Points-For Crowns",
                                  "Seasons leading the league in regular-season points.",
                                  [self.entry(len(v), f"{len(v)} crown{'s' if len(v) != 1 else ''}",
                                              f"{self.name(m)} led the league in regular-season scoring in "
                                              + ", ".join(f"{y} ({f2(t['points_for'])})" for y, t in v) + ".",
                                              m, v[-1][0]) for m, v in sorted(
                                      crowns.items(), key=lambda kv: (-len(kv[1]), -kv[1][-1][0]))]))

        def ap_caption(o, lead=""):
            done = self.finished(o["season"])
            when = f"in {o['season']}" if done else f"in {o['season']} so far (through Week {o['last_week']})"
            actual = (f"{int(o['wins'])}" if o["wins"] == int(o["wins"]) else f"{o['wins']}")
            return (f"{lead}Against every team, every week {when}, {self.name(o['manager'])} ({o['team']}) would "
                    f"have gone {record_str(o['w'], o['l'], o['t'])} ({o['pct']:.3f}). That's {o['expected']} "
                    f"expected wins; they actually won {actual}.")

        ap = list(allplay.values())
        cats.append(self.category("all-play", "Best All-Play Records",
                                  "Record against every team, every regular-season week — schedule luck removed.",
                                  [self.entry(round(o["pct"], 3), record_str(o["w"], o["l"], o["t"]), ap_caption(o),
                                              o["manager"], o["season"])
                                   for o in sorted(ap, key=lambda o: -o["pct"])[:15]]))
        cats.append(self.category("luckiest-seasons", "Luckiest Seasons",
                                  "Actual regular-season wins minus all-play expected wins.",
                                  [self.entry(o["luck"], f"+{o['luck']:.2f} wins", ap_caption(o, "Blessed. "),
                                              o["manager"], o["season"])
                                   for o in sorted(ap, key=lambda o: -o["luck"])[:15] if o["luck"] > 0]))
        bm = [o for o in self.bm.values() if o["weeks"] >= 3]
        cats.append(self.category("median-records", "Best Records vs the Median",
                                  "Every week, a bonus game against the league's median score. Beat it and you win.",
                                  [self.entry(o["medPct"], record_str(*o["med"]), self.mark_caption(o, "med"),
                                              o["manager"], o["season"])
                                   for o in sorted(bm, key=lambda o: (-o["medPct"], -o["med"][0]))[:15]]))
        cats.append(self.category("average-records", "Best Records vs the Average",
                                  "The same idea, against the week's league-average score.",
                                  [self.entry(o["avgPct"], record_str(*o["avg"]), self.mark_caption(o, "avg"),
                                              o["manager"], o["season"])
                                   for o in sorted(bm, key=lambda o: (-o["avgPct"], -o["avg"][0]))[:15]]))
        cats.append(self.category("true-records", "Best True Records",
                                  "Head-to-head record plus record vs the median — two games a week.",
                                  [self.entry(o["truePct"], record_str(*o["true"]), self.mark_caption(o, "med"),
                                              o["manager"], o["season"])
                                   for o in sorted(bm, key=lambda o: (-o["truePct"], -o["true"][0]))[:15]]))
        cv = [c for c in self.carries.values() if c["weeks"] >= 3]
        cats.append(self.category("most-carried", "Most Carried Teams",
                                  "Biggest share of a team's regular-season starting points from one player.",
                                  [self.entry(c["top1"], f"{round(c['top1'] * 100)}%",
                                              self.carry_caption(c, "One-man show. "), c["manager"], c["season"],
                                              player=c["top"]["name"])
                                   for c in sorted(cv, key=lambda c: -c["top1"])[:15]]))
        cats.append(self.category("most-balanced", "Most Balanced Teams",
                                  "Smallest share of regular-season starting points from the top three players.",
                                  [self.entry(c["top3"], f"Top 3: {round(c['top3'] * 100)}%",
                                              self.carry_caption(c, "Everybody eats. "), c["manager"], c["season"])
                                   for c in sorted(cv, key=lambda c: (c["top3"], c["top1"]))[:15]]))

        # Playoffs only.
        cats.append(self.category("playoff-explosions", "Biggest Playoff Performances", "Clutch, defined.",
                                  [game_entry(r, r["points"], f2(r["points"]), game_caption(r, "When it mattered most,"))
                                   for r in sorted(self.games("playoffs"), key=lambda r: -r["points"])[:15]],
                                  segment="playoffs"))
        clutch = [(m, p) for m, p in profile.items() if p["clutch"] is not None and p["poGames"] >= 2]
        cats.append(self.category("clutch-score", "Clutch Score",
                                  "Playoff points per game minus regular-season points per game (min. 2 playoff games).",
                                  [self.entry(p["clutch"], f"{p['clutch']:+.2f}",
                                              f"{self.name(m)} averages {f2(p['poPpg'])} in the playoffs "
                                              f"({p['poGames']} games, {p['poWins']}-{p['poLosses']}) against "
                                              f"{f2(p['regPpg'])} in the regular season"
                                              + (" — built for January." if p["clutch"] > 0 else " — the lights get bright."),
                                              m) for m, p in sorted(clutch, key=lambda mp: -mp[1]["clutch"])],
                                  segment="playoffs"))
        upsets = []
        for r in self.games("playoffs"):
            a = self.teams.get(r["team_key"], {}).get("playoff_seed")
            b = self.teams.get(r["opponent_key"], {}).get("playoff_seed")
            if r["title_path"] and r["result"] == "W" and a and b and a > b:
                upsets.append((a - b, r, a, b))
        upsets.sort(key=lambda u: (-u[0], -u[1]["margin"]))
        cats.append(self.category("playoff-upsets", "Playoff Upsets", "Lower seeds knocking out higher ones.",
                                  [game_entry(r, d, f"#{a} over #{b}",
                                              f"The No. {a} seed {self.name(r['manager'])} knocked off the No. {b} "
                                              f"seed {self.name(r['opponent'])} {f2(r['points'])}–"
                                              f"{f2(r['opponent_points'])} in {self.week_label(r)}.")
                                   for d, r, a, b in upsets], segment="playoffs"))
        rate = [(m, p) for m, p in profile.items() if p["seasons"]]
        cats.append(self.category("playoff-rate", "Playoff Appearance Rate", "How often they make the dance.",
                                  [self.entry(p["rate"], f"{round(p['rate'] * 100)}%",
                                              f"{self.name(m)} has made the playoffs in {p['apps']} of "
                                              f"{p['seasons']} completed season{'s' if p['seasons'] != 1 else ''}"
                                              + (f" ({', '.join(map(str, p['appSeasons']))})" if p["apps"] else "")
                                              + (f", with {p['byes']} first-round bye{'s' if p['byes'] != 1 else ''}"
                                                 if p["byes"] else "") + ".", m)
                                   for m, p in sorted(rate, key=lambda mp: (-mp[1]["rate"], -mp[1]["apps"],
                                                                             -mp[1]["byes"]))],
                                  segment="playoffs"))
        return cats

    def hall_of_shame(self, bench, drafts, trades, allplay) -> list[dict]:
        cats = []

        def seg_bench(seg):
            return [r for r in bench if r["game_type"] in SEGMENT_TYPES[seg]]

        def bench_caption(r):
            tail = ""
            if r["would_have_won"]:
                tail = f" The optimal lineup ({f2(r['optimal'])}) would have beaten {self.name(r['opponent'])}."
            elif r["result"] == "W":
                tail = " Luckily, it didn't cost them the game."
            return (f"{self.name(r['manager'])} left {f2(r['left'])} points on the bench in {self.week_label(r)}, "
                    f"scoring {f2(r['actual'])} when {f2(r['optimal'])} was possible.{tail}")

        def bench_entry(r, **extra):
            return self.entry(r["left"], f2(r["left"]), bench_caption(r), r["manager"], r["season"], r["week"],
                              r["opponent"], **extra)

        cats.append(self.seg_category(
            "bench-points", "Most Points Left on the Bench", "Optimal lineup minus actual lineup, in a single week.",
            lambda seg: [bench_entry(r, wouldHaveWon=r["would_have_won"])
                         for r in sorted(seg_bench(seg), key=lambda r: -r["left"])[:TOP_N]]))
        cats.append(self.seg_category(
            "bench-cost-a-win", "Benched Their Way to a Loss", "Losses where the optimal lineup would have won.",
            lambda seg: [bench_entry(r) for r in sorted([r for r in seg_bench(seg) if r["would_have_won"]],
                                                        key=lambda r: -r["left"])[:TOP_N]]))
        season_bench = defaultdict(lambda: {"left": 0.0, "costly": 0})
        for r in seg_bench("regular"):
            k = (r["manager"], r["season"], r["team_name"])
            season_bench[k]["left"] += r["left"]
            season_bench[k]["costly"] += int(r["would_have_won"])
        sb = sorted(season_bench.items(), key=lambda kv: -kv[1]["left"])[:15]
        cats.append(self.category("bench-season", "Most Bench Points in a Season",
                                  "A full regular season of second-guessing.",
                                  [self.entry(round(v["left"], 2), f2(v["left"]),
                                              f"{self.name(m)} ({team}) stranded {f2(v['left'])} points on the bench "
                                              f"across the {s} regular season" + (
                                                  f", costing {v['costly']} win{'s' if v['costly'] != 1 else ''}."
                                                  if v["costly"] else "."),
                                              m, s) for (m, s, team), v in sb]))

        # Losses with the 2nd-highest score of the week (regular season: every team plays).
        by_week = defaultdict(list)
        for r in self.games("regular"):
            by_week[(r["season"], r["week"])].append(r)
        unlucky = []
        for rows in by_week.values():
            ranked = sorted(rows, key=lambda r: -r["points"])
            if len(ranked) > 1 and ranked[1]["result"] == "L":
                unlucky.append(ranked[1])
        unlucky.sort(key=lambda r: -r["points"])
        cats.append(self.category("unlucky-losses", "Second-Best Score, Still Lost",
                                  "Put up the 2nd-highest score of the week and drew the only team that beat it.",
                                  [self.entry(r["points"], f2(r["points"]),
                                              f"{self.name(r['manager'])} scored {f2(r['points'])} in "
                                              f"{self.week_label(r)} — the 2nd-best score in the league — and still "
                                              f"lost to {self.name(r['opponent'])}'s {f2(r['opponent_points'])}.",
                                              r["manager"], r["season"], r["week"], r["opponent"])
                                   for r in unlucky]))
        cats.append(self.seg_category(
            "heartbreaks", "Most Points in a Loss", "Did everything right. Lost anyway.",
            lambda seg: [self.entry(r["points"], f2(r["points"]),
                                    f"{self.name(r['manager'])} dropped {f2(r['points'])} in {self.week_label(r)} "
                                    f"and lost to {self.name(r['opponent'])} ({f2(r['opponent_points'])}).",
                                    r["manager"], r["season"], r["week"], r["opponent"])
                         for r in sorted([r for r in self.games(seg) if r["result"] == "L"],
                                         key=lambda r: -r["points"])[:TOP_N]]))
        cats.append(self.seg_category(
            "ugliest-wins", "Ugliest Wins", "Fewest points in a victory. A W is a W.",
            lambda seg: [self.entry(r["points"], f2(r["points"]),
                                    f"{self.name(r['manager'])} beat {self.name(r['opponent'])} with just "
                                    f"{f2(r['points'])} points ({f2(r['opponent_points'])} against) in "
                                    f"{self.week_label(r)}.",
                                    r["manager"], r["season"], r["week"], r["opponent"])
                         for r in sorted([r for r in self.games(seg) if r["result"] == "W"],
                                         key=lambda r: r["points"])[:TOP_N]]))

        def ap_caption(o):
            when = o["season"] if self.finished(o["season"]) else f"{o['season']} so far"
            actual = int(o["wins"]) if o["wins"] == int(o["wins"]) else o["wins"]
            return (f"Snakebitten. {self.name(o['manager'])} ({o['team']}) would have gone "
                    f"{record_str(o['w'], o['l'], o['t'])} against every team, every week in {when} — "
                    f"{o['expected']} expected wins — but actually won {actual}.")

        cats.append(self.category("unluckiest-seasons", "Unluckiest Seasons",
                                  "Actual regular-season wins minus all-play expected wins.",
                                  [self.entry(o["luck"], f"{o['luck']:.2f} wins", ap_caption(o), o["manager"],
                                              o["season"])
                                   for o in sorted(allplay.values(), key=lambda o: o["luck"])[:15] if o["luck"] < 0]))
        busts = sorted([p for p in drafts if p["round"] and p["round"] <= 5 and p["final"]],
                       key=lambda p: p["value"])[:TOP_N]
        cats.append(self.category("worst-draft-picks", "Worst Draft Picks",
                                  "Early-round picks (Rounds 1–5) that finished furthest below their draft slot.",
                                  [self.entry(p["value"], f"{p['value']:+d} spots", self.pick_caption(p),
                                              p["manager"], p["season"], player=p["player_name"], pick=p["pick"])
                                   for p in busts], segment="season"))
        worst = sorted(trades, key=lambda t: t["net"])[:15]
        cats.append(self.category("worst-trades", "Most Lopsided Trades (Losing Side)",
                                  "Starter points gained minus starter points given away, rest of season "
                                  "(regular season and playoffs).",
                                  [self.entry(t["net"], f"{f2(t['net'])}", self.trade_caption(t), t["manager"],
                                              t["season"], t["week"],
                                              t["partners"][0] if t["partners"] else None) for t in worst],
                                  segment="season"))
        basement = []
        for season in sorted(self.seasons, reverse=True):
            if not self.finished(season):
                continue
            season_teams = [t for t in self.teams.values() if t["season"] == season]
            if not season_teams:
                continue
            # Last place = worst regular-season record. Yahoo's final seeds already apply the league's
            # tiebreaker (points for); fall back to win% then points for when seeds are missing.
            if all(t["playoff_seed"] for t in season_teams):
                last = max(season_teams, key=lambda t: t["playoff_seed"])
            else:
                last = min(season_teams, key=lambda t: ((t["wins"] or 0) + 0.5 * (t["ties"] or 0),
                                                        t["points_for"] or 0))
            rec = record_str(last["wins"], last["losses"], last["ties"])
            n = len(season_teams)
            tied = [t for t in season_teams if t is not last and (t["wins"], t["losses"], t["ties"]) ==
                    (last["wins"], last["losses"], last["ties"])]
            who = self.name(self.mgr(last["team_key"]))
            team = last["name"] if last["name"].lower().startswith(who.lower()) else f"{who}'s {last['name']}"
            caption = (f"{team} finished dead last in {season} "
                       f"with the league's worst regular-season record, {rec}, and {f2(last['points_for'])} points.")
            if tied:
                caption += " Lost the points-for tiebreaker to " + ", ".join(
                    self.name(self.mgr(t["team_key"])) for t in tied) + "."
            if last["final_rank"] and last["final_rank"] != n:
                caption += f" (Yahoo's final standings, after the consolation bracket, have them {ordinal(last['final_rank'])}.)"
            basement.append(self.entry(season, rec, caption, self.mgr(last["team_key"]), season))
        cats.append(self.category("the-basement", "The Basement", "Worst regular-season record, season by season.",
                                  basement, ranked=False))

        toilet = []
        for season in sorted(self.seasons, reverse=True):
            if not self.finished(season):
                continue
            ranked = [t for t in self.teams.values() if t["season"] == season and t["final_rank"]]
            if not ranked:
                continue
            worst_t = max(ranked, key=lambda t: t["final_rank"])
            cons = sorted([r for r in self.games("consolation") if r["team_key"] == worst_t["team_key"]],
                          key=lambda r: r["week"])
            mid = self.mgr(worst_t["team_key"])
            rec = f"{sum(r['result'] == 'W' for r in cons)}-{sum(r['result'] == 'L' for r in cons)}"
            if cons:
                g = cons[-1]
                caption = (f"{self.name(mid)} ({worst_t['name']}) lost the {season} Toilet Bowl, falling to "
                           f"{self.name(g['opponent'])} {f2(g['points'])}–{f2(g['opponent_points'])} in Week "
                           f"{g['week']} and finishing {ordinal(worst_t['final_rank'])} of {len(ranked)}"
                           f" (consolation record {rec}).")
                toilet.append(self.entry(season, f2(g["points"]), caption, mid, season, g["week"], g["opponent"]))
            else:
                toilet.append(self.entry(season, ordinal(worst_t["final_rank"]),
                                         f"{self.name(mid)} ({worst_t['name']}) finished {ordinal(worst_t['final_rank'])}"
                                         f" of {len(ranked)} in {season} after the consolation bracket.",
                                         mid, season))
        cats.append(self.category("toilet-bowl", "The Toilet Bowl",
                                  "The consolation bracket's final loser, season by season.", toilet, ranked=False,
                                  segment="consolation"))
        return cats

    def category(self, slug, title, blurb, entries, ranked=True, segment="regular", segments=None) -> dict:
        for lst in [entries, *(segments or {}).values()]:
            for i, e in enumerate(lst):
                e["rank"] = i + 1
        cat = {"slug": slug, "title": title, "blurb": blurb, "entries": entries, "ranked": ranked,
               "segment": segment,
               "hasNew": any(e["isNew"] for lst in [entries, *(segments or {}).values()] for e in lst)}
        if segments:
            cat["segments"] = segments
        return cat

    # ---------- champions ----------
    def champions(self) -> dict:
        banners = []
        for season in sorted(self.seasons, reverse=True):
            if not self.finished(season):
                continue
            ranked = sorted([t for t in self.teams.values() if t["season"] == season and t["final_rank"]],
                            key=lambda t: t["final_rank"])
            if not ranked:
                continue
            final = None
            champ_key = ranked[0]["team_key"]
            end = self.seasons[season]["end_week"]
            for r in self.rows:
                if r["season"] == season and r["team_key"] == champ_key and r["is_playoffs"] and r["week"] == end:
                    final = r
            podium = []
            for t in ranked[:3]:
                podium.append({"manager": self.mgr(t["team_key"]), "team": t["name"], "rank": t["final_rank"],
                               "record": record_str(t["wins"], t["losses"], t["ties"]),
                               "pointsFor": t["points_for"], "seed": t["playoff_seed"]})
            caption = f"{self.name(podium[0]['manager'])} ({podium[0]['team']}) won the {season} title"
            if final:
                caption += (f", beating {self.name(final['opponent'])} {f2(final['points'])}–"
                            f"{f2(final['opponent_points'])} in the championship")
            if podium[0]["seed"]:
                caption += f" as the No. {podium[0]['seed']} seed"
            caption += "."
            banners.append({"season": season, "podium": podium, "caption": caption,
                            "final": {"points": final["points"], "opponentPoints": final["opponent_points"]}
                            if final else None})
        trophy = defaultdict(lambda: {"titles": [], "runnerUps": [], "thirds": []})
        for b in banners:
            for p in b["podium"]:
                key = {1: "titles", 2: "runnerUps", 3: "thirds"}[p["rank"]]
                trophy[p["manager"]][key].append(b["season"])
        case = [{"manager": m, **v} for m, v in trophy.items()]
        case.sort(key=lambda c: (-len(c["titles"]), -len(c["runnerUps"]), -len(c["thirds"])))
        return {"banners": banners, "trophyCase": case}

    # ---------- h2h ----------
    def head_to_head(self) -> dict:
        pairs = {}
        for r in self.games("all"):
            a, b = r["manager"], r["opponent"]
            if not a or not b or a > b:
                continue
            p = pairs.setdefault(f"{a}--{b}", {"a": a, "b": b, "games": []})
            p["games"].append({"season": r["season"], "week": r["week"], "aPoints": r["points"],
                               "bPoints": r["opponent_points"], "result": r["result"],
                               "kind": "playoffs" if r["game_type"] == "playoff" else "regular",
                               "round": r.get("round_label"),
                               "label": self.week_label(r), "isNew": self.is_new(r["season"], r["week"])})
        out = {}
        mids = sorted(self.managers)
        for a, b in combinations(mids, 2):
            p = pairs.get(f"{a}--{b}", {"a": a, "b": b, "games": []})
            g = sorted(p["games"], key=lambda x: (x["season"], x["week"]), reverse=True)
            reg = [x for x in g if x["kind"] == "regular"]
            playoff = [x for x in g if x["kind"] == "playoffs"]
            aw = sum(1 for x in reg if x["result"] == "W")
            bw = sum(1 for x in reg if x["result"] == "L")
            ties = sum(1 for x in reg if x["result"] == "T")
            apf = round(sum(x["aPoints"] for x in reg), 2)
            bpf = round(sum(x["bPoints"] for x in reg), 2)
            pa = sum(1 for x in playoff if x["result"] == "W")
            pb = sum(1 for x in playoff if x["result"] == "L")
            big_a = max((x for x in g if x["result"] == "W"), key=lambda x: x["aPoints"] - x["bPoints"], default=None)
            big_b = max((x for x in g if x["result"] == "L"), key=lambda x: x["bPoints"] - x["aPoints"], default=None)
            # current regular-season streak
            streak_owner, streak = None, 0
            for x in reg:
                owner = a if x["result"] == "W" else b if x["result"] == "L" else None
                if streak == 0:
                    streak_owner, streak = owner, 1 if owner else 0
                    if not owner:
                        break
                elif owner == streak_owner:
                    streak += 1
                else:
                    break
            na, nb = self.name(a), self.name(b)
            if not g:
                caption = f"{na} and {nb} have never met. The museum awaits their first chapter."
            else:
                if reg:
                    leader = na if aw > bw else nb if bw > aw else None
                    hi, lo = max(aw, bw), min(aw, bw)
                    caption = (f"{leader} leads the regular-season series {hi}-{lo}" + (f"-{ties}" if ties else "")
                               if leader else f"The regular-season series is dead even at {aw}-{bw}"
                               + (f"-{ties}" if ties else ""))
                    caption += f" over {len(reg)} meeting{'s' if len(reg) != 1 else ''}."
                else:
                    caption = f"{na} and {nb} have never met in the regular season."
                if playoff:
                    pl = na if pa > pb else nb if pb > pa else None
                    caption += (f" In the playoffs, {pl} leads {max(pa, pb)}-{min(pa, pb)}." if pl
                                else f" In the playoffs, they're even at {pa}-{pb}.")
                if streak_owner and streak >= 2:
                    caption += f" {self.name(streak_owner)} has won the last {streak} regular-season meetings."
            out[f"{a}--{b}"] = {
                "a": a, "b": b, "aWins": aw, "bWins": bw, "ties": ties, "aPoints": apf, "bPoints": bpf,
                "meetings": len(reg), "playoffMeetings": len(playoff), "totalMeetings": len(g),
                "playoff": {"aWins": pa, "bWins": pb, "meetings": len(playoff),
                            "aPoints": round(sum(x["aPoints"] for x in playoff), 2),
                            "bPoints": round(sum(x["bPoints"] for x in playoff), 2)},
                "caption": caption,
                "streak": {"manager": streak_owner, "length": streak} if streak_owner else None,
                "biggestA": big_a, "biggestB": big_b, "games": g,
                "hasNew": any(x["isNew"] for x in g),
            }
        return out

    # ---------- managers ----------
    def manager_pages(self, bench, drafts, trades, h2h, unlucky_cat, active_streaks, allplay, profile) -> list[dict]:
        pages = []
        champs = self.champions()["trophyCase"]
        trophy = {c["manager"]: c for c in champs}
        po_by_team = defaultdict(list)
        for r in self.games("playoffs"):
            po_by_team[r["team_key"]].append(r)
        for mid, m in sorted(self.managers.items(), key=lambda kv: kv[1]["name"].lower()):
            prof = profile.get(mid, {})
            teams = sorted([t for t in self.teams.values() if self.mgr(t["team_key"]) == mid],
                           key=lambda t: t["season"])
            seasons = []
            for t in teams:
                s = self.seasons[t["season"]]
                tpo = po_by_team.get(t["team_key"], [])
                seasons.append({"playoffRecord": record_str(sum(r["result"] == "W" for r in tpo),
                                                            sum(r["result"] == "L" for r in tpo),
                                                            sum(r["result"] == "T" for r in tpo)) if tpo else None,
                                "bye": t["season"] in prof.get("byeSeasons", []),
                                "allPlay": (lambda o: o and {"record": record_str(o["w"], o["l"], o["t"]),
                                                             "luck": o["luck"]})(allplay.get(t["team_key"])),
                                **self.season_bench(t["team_key"]),
                                "draft": self._grade_view(t["team_key"]), "sos": self.sos.get(t["team_key"]),
                                "season": t["season"], "team": t["name"], "wins": t["wins"], "losses": t["losses"],
                                "ties": t["ties"], "pointsFor": t["points_for"], "pointsAgainst": t["points_against"],
                                "rank": t["final_rank"], "seed": t["playoff_seed"], "finished": bool(s["is_finished"]),
                                "madePlayoffs": bool(t["playoff_seed"] and s.get("num_playoff_teams")
                                                     and t["playoff_seed"] <= s["num_playoff_teams"]
                                                     and s["is_finished"])})
            w = sum(t["wins"] or 0 for t in teams)
            l = sum(t["losses"] or 0 for t in teams)
            ti = sum(t["ties"] or 0 for t in teams)
            my_games = [r for r in self.games("regular") if r["manager"] == mid]
            po = [r for r in self.games("playoffs") if r["manager"] == mid]
            my_ap = [o for o in allplay.values() if o["manager"] == mid]
            apw, apl, apt = (sum(o[k] for o in my_ap) for k in ("w", "l", "t"))
            done = [s for s in seasons if s["finished"] and s["rank"]]
            hi = max(my_games, key=lambda r: r["points"], default=None)
            lo = min((r for r in my_games if r["points"] > 0), key=lambda r: r["points"], default=None)
            my_picks = [p for p in drafts if p["manager"] == mid and p["final"]]
            my_trades = [t for t in trades if t["manager"] == mid]
            my_bench = [r for r in bench if r["manager"] == mid and r["game_type"] == "regular"]
            tc = trophy.get(mid, {"titles": [], "runnerUps": [], "thirds": []})
            rivals = []
            for key, h in h2h.items():
                if mid not in (h["a"], h["b"]) or not h["totalMeetings"]:
                    continue
                me_a = h["a"] == mid
                hp = h["playoff"]
                rivals.append({"opponent": h["b"] if me_a else h["a"], "pair": key,
                               "wins": h["aWins"] if me_a else h["bWins"],
                               "losses": h["bWins"] if me_a else h["aWins"], "ties": h["ties"],
                               "meetings": h["meetings"],
                               "playoffWins": hp["aWins"] if me_a else hp["bWins"],
                               "playoffLosses": hp["bWins"] if me_a else hp["aWins"],
                               "playoffMeetings": hp["meetings"]})
            rivals.sort(key=lambda r: (-r["meetings"], r["opponent"]))
            game_view = lambda r: r and {"points": r["points"], "opponent": r["opponent"],  # noqa: E731
                                         "opponentPoints": r["opponent_points"], "season": r["season"],
                                         "week": r["week"], "label": self.week_label(r), "result": r["result"]}
            pct = (w + 0.5 * ti) / (w + l + ti) if (w + l + ti) else 0
            first, last_season = (teams[0]["season"], teams[-1]["season"]) if teams else (None, None)
            title_txt = (f" with {len(tc['titles'])} championship{'s' if len(tc['titles']) != 1 else ''} "
                         f"({', '.join(map(str, tc['titles']))})" if tc["titles"] else "")
            caption = (f"{m['name']} has managed {len(teams)} season{'s' if len(teams) != 1 else ''} "
                       f"({first}–{last_season}) with a {record_str(w, l, ti)} ({pct:.3f}) regular-season "
                       f"record{title_txt}.")
            pw, pl = sum(1 for r in po if r["result"] == "W"), sum(1 for r in po if r["result"] == "L")
            if prof.get("seasons"):
                caption += (f" Playoffs: {prof['apps']} appearance{'s' if prof['apps'] != 1 else ''} in "
                            f"{prof['seasons']} completed season{'s' if prof['seasons'] != 1 else ''}"
                            + (f", {pw}-{pl} in playoff games." if po else "."))
            streak = active_streaks.get(mid)
            pages.append({
                **{k: m[k] for k in ("id", "name", "nickname", "bio", "lore", "color", "initials")},
                "caption": caption, "firstSeason": first, "lastSeason": last_season,
                "active": m.get("active") if m.get("active") is not None else last_season == self.latest["season"],
                "career": {"wins": w, "losses": l, "ties": ti, "pct": round(pct, 3),
                           "pointsFor": round(sum(t["points_for"] or 0 for t in teams), 2),
                           "pointsAgainst": round(sum(t["points_against"] or 0 for t in teams), 2),
                           "playoffWins": pw, "playoffLosses": pl,
                           "playoffApps": sum(1 for s in seasons if s["madePlayoffs"]),
                           "playoffByes": prof.get("byes", 0), "playoffRate": prof.get("rate"),
                           "completedSeasons": prof.get("seasons", 0),
                           "playoffPpg": prof.get("poPpg"), "regularPpg": prof.get("regPpg"),
                           "clutch": prof.get("clutch"),
                           "allPlay": {"wins": apw, "losses": apl, "ties": apt,
                                       "pct": round((apw + 0.5 * apt) / (apw + apl + apt), 3)
                                       if (apw + apl + apt) else None},
                           "luck": round(sum(o["luck"] for o in my_ap), 2),
                           **self.career_bench(mid),
                           "avgFinish": round(sum(s["rank"] for s in done) / len(done), 1) if done else None,
                           "bestFinish": min((s["rank"] for s in done), default=None),
                           "benchPoints": round(sum(r["left"] for r in my_bench), 2),
                           "benchCostWins": sum(1 for r in my_bench if r["would_have_won"]),
                           "unluckyLosses": sum(1 for e in unlucky_cat["entries"] if e["manager"] == mid),
                           "trades": len({t["transaction_key"] for t in my_trades}),
                           "moves": sum(t["moves"] or 0 for t in teams)},
                "titles": tc["titles"], "runnerUps": tc["runnerUps"], "thirds": tc["thirds"],
                "seasons": seasons, "highestScore": game_view(hi), "lowestScore": game_view(lo),
                "bestPicks": [{**self._pick_view(p)} for p in sorted(my_picks, key=lambda p: -p["value"])[:5]],
                "worstPicks": [{**self._pick_view(p)} for p in sorted(
                    [p for p in my_picks if p["round"] and p["round"] <= 5], key=lambda p: p["value"])[:5]],
                "bestTrades": [self._trade_view(t) for t in sorted(my_trades, key=lambda t: -t["net"])[:3]
                               if t["net"] > 0],
                "worstTrades": [self._trade_view(t) for t in sorted(my_trades, key=lambda t: t["net"])[:3]
                                if t["net"] < 0],
                "rivals": rivals,
                "draftCareer": self.grades["careers"].get(mid),
                "cabinet": self.cabinet.get(mid, {}),
                "streak": self.streak_entry(streak) if streak and streak["length"] >= 2 else None,
            })
        return pages

    def _grade_view(self, team_key):
        g = self.grades["teams"].get(team_key)
        return g and {k: g[k] for k in ("grade", "gpa", "voe", "kept", "points", "rank", "best", "worst", "final")}

    def _pick_view(self, p):
        e = self.grades["picks"].get((p["season"], p["pick"]), {})
        return {**{k: e.get(k) for k in ("voe", "expected", "kept", "tier", "posRank")},"season": p["season"], "pick": p["pick"], "round": p["round"], "player": p["player_name"],
                "position": p["position"], "points": p["season_points"], "pointsRank": p["points_rank"],
                "value": p["value"], "caption": self.pick_caption(p)}

    def _trade_view(self, t):
        return {"season": t["season"], "week": t["week"], "net": t["net"], "partners": t["partners"],
                "sent": t["sent"], "received": t["received"], "caption": self.trade_caption(t)}

    # ---------- archives ----------
    def archives(self, drafts, trades) -> list[dict]:
        out = []
        draft_by = defaultdict(list)
        for p in drafts:
            draft_by[p["season"]].append(p)
        for season, s in sorted(self.seasons.items(), reverse=True):
            teams = [t for t in self.teams.values() if t["season"] == season]
            if teams and all(t["playoff_seed"] for t in teams):
                teams.sort(key=lambda t: t["playoff_seed"])
            else:
                teams.sort(key=lambda t: (-((t["wins"] or 0) + 0.5 * (t["ties"] or 0)) /
                                          max(1, (t["wins"] or 0) + (t["losses"] or 0) + (t["ties"] or 0)),
                                          -(t["points_for"] or 0)))
            standings = [{"rank": t["final_rank"], "finalRank": t["final_rank"] if s["is_finished"] else None, "manager": self.mgr(t["team_key"]), "team": t["name"],
                          "wins": t["wins"], "losses": t["losses"], "ties": t["ties"], "pointsFor": t["points_for"],
                          "pointsAgainst": t["points_against"], "seed": t["playoff_seed"], "moves": t["moves"],
                          "trades": t["trades"],
                          "draftGrade": (self.grades["teams"].get(t["team_key"]) or {}).get("grade"),
                          **{k: (self.sos.get(t["team_key"]) or {}).get(k) for k in
                             ("pastOppAllPlay", "pastRank", "pastOppPoints")},
                          **{k: v for k, v in self.season_bench(t["team_key"]).items() if k in
                             ("vsMedian", "vsAverage", "trueRecord")}} for t in teams]
            weeks = defaultdict(list)
            for r in self.rows:
                if r["season"] == season and r["team_key"] < (r["opponent_key"] or ""):
                    weeks[r["week"]].append({"a": r["manager"], "b": r["opponent"], "aTeam": r["team_name"],
                                             "bTeam": r["opp_team_name"], "aPoints": r["points"],
                                             "bPoints": r["opponent_points"], "type": r["game_type"],
                                             "playoffs": r["game_type"] == "playoff",
                                             "consolation": r["game_type"] == "consolation",
                                             "label": r.get("round_label"), "titlePath": bool(r.get("title_path"))})
            bracket = []
            for week in sorted(w for w, gs in weeks.items() if any(g["playoffs"] for g in gs)):
                games = sorted([g for g in weeks[week] if g["playoffs"]],
                               key=lambda g: (not g["titlePath"], g["label"] or ""))
                bracket.append({"week": week, "name": self.round_name(season, week), "games": games})
            picks = sorted(draft_by.get(season, []), key=lambda p: p["pick"])
            order = []
            for p in picks:
                if p["round"] == 1:
                    order.append(p["manager"])
            board = defaultdict(list)
            for p in picks:
                view = self._pick_view(p)
                board[p["round"]].append({**view, "manager": p["manager"],
                                          "tag": view["tier"] if view["tier"] in ("steal", "bust") else None})
            for rnd, row in board.items():
                row.sort(key=lambda c: order.index(c["manager"]) if c["manager"] in order else 99)
            champion = next((st for st in standings if st["finalRank"] == 1), None) if s["is_finished"] else None
            caption = (f"{self.name(champion['manager'])} ({champion['team']}) won the {season} title."
                       if champion else f"The {season} season is in progress — through Week "
                                        f"{s.get('last_completed_week') or 0}.")
            out.append({"season": season, "name": s["name"], "finished": bool(s["is_finished"]),
                        "lastCompletedWeek": s.get("last_completed_week"), "numTeams": s["num_teams"],
                        "caption": caption, "standings": standings, "bracket": bracket,
                        "draftOrder": order,
                        "draftGrades": sorted(({"manager": g["manager"], **self._grade_view(tk)}
                                               for tk, g in self.grades["teams"].items() if g["season"] == season),
                                              key=lambda g: g["rank"]), "draftBoard": [{"round": r, "picks": board[r]} for r in sorted(board)],
                        "weeks": [{"week": w, "games": weeks[w]} for w in sorted(weeks, reverse=True)],
                        "trades": [self._trade_view(t) for t in trades if t["season"] == season and t["net"] >= 0]})
        return out

    # ---------- lobby ----------
    def lobby(self, cats, bench, active_streaks, h2h) -> dict:
        L = self.latest
        week_rows = [r for r in self.games("all") if r["season"] == L["season"] and r["week"] == L["week"]]
        heads = []
        if week_rows:
            hi = max(week_rows, key=lambda r: r["points"])
            heads.append({"kicker": "Top score", "manager": hi["manager"],
                          "text": f"{self.name(hi['manager'])} led the league with {f2(hi['points'])} points "
                                  f"against {self.name(hi['opponent'])}."})
            lo = min(week_rows, key=lambda r: r["points"])
            heads.append({"kicker": "Basement dweller", "manager": lo["manager"],
                          "text": f"{self.name(lo['manager'])} limped to {f2(lo['points'])}, the week's lowest."})
            wins = [r for r in week_rows if r["result"] == "W"]
            if wins:
                big = max(wins, key=lambda r: r["margin"])
                heads.append({"kicker": "Blowout", "manager": big["manager"],
                              "text": f"{self.name(big['manager'])} beat {self.name(big['opponent'])} by "
                                      f"{f2(big['margin'])}."})
                close = min(wins, key=lambda r: r["margin"])
                heads.append({"kicker": "Nail-biter", "manager": close["manager"],
                              "text": f"{self.name(close['manager'])} survived {self.name(close['opponent'])} by "
                                      f"{f2(close['margin'])}."})
            wb = [r for r in bench if r["season"] == L["season"] and r["week"] == L["week"]
                  and r["game_type"] != "consolation"]
            if wb:
                bb = max(wb, key=lambda r: r["left"])
                heads.append({"kicker": "Bench blunder", "manager": bb["manager"],
                              "text": f"{self.name(bb['manager'])} left {f2(bb['left'])} points on the bench"
                                      + (" — enough to flip the result." if bb["would_have_won"] else ".")})
            cj = self.week_carry(L["season"], L["week"])
            if cj and cj["share"] >= 0.25:
                heads.append({"kicker": "Carry job", "manager": cj["manager"],
                              "text": f"{cj['player']} ({cj['pos']}) put up {f2(cj['pts'])} of "
                                      f"{self.name(cj['manager'])}'s {f2(cj['total'])} points — "
                                      f"{round(cj['share'] * 100)}% of the team"
                                      + (" — and it still wasn't enough." if cj["result"] == "L" else ".")})
            ranked = sorted(week_rows, key=lambda r: -r["points"])
            if len(ranked) > 1 and ranked[1]["result"] == "L" and ranked[1]["game_type"] == "regular":
                u = ranked[1]
                heads.append({"kicker": "Cursed", "manager": u["manager"],
                              "text": f"{self.name(u['manager'])} posted the 2nd-best score ({f2(u['points'])}) "
                                      f"and still lost."})
        broken = []
        for section, catlist in cats.items():
            for c in catlist:
                for e in c["entries"]:
                    if e["isNew"] and c["ranked"] and e["rank"] <= 10:
                        broken.append({"section": section, "slug": c["slug"], "title": c["title"],
                                       "segment": c["segment"],
                                       "rank": e["rank"], "caption": e["caption"], "manager": e["manager"]})
        broken.sort(key=lambda b: b["rank"])
        streaks = [self.streak_entry(s) for s in active_streaks.values()
                   if s["length"] >= 2 and s["end"]["season"] == L["season"]]
        streaks.sort(key=lambda e: -e["value"])
        # "This week in history": same week number in past seasons.
        history = []
        for r in self.games("all"):
            if r["week"] == L["week"] + 1 and r["season"] < L["season"] and r["result"] == "W":
                history.append(r)
        if not history:
            history = [r for r in self.games("all") if r["week"] == L["week"] and r["season"] < L["season"]
                       and r["result"] == "W"]
        notable = sorted(history, key=lambda r: -(r["points"] + r["margin"]))[:6]
        on_this_week = [{"season": r["season"], "week": r["week"], "manager": r["manager"],
                         "text": f"{r['season']}: {self.name(r['manager'])} scored {f2(r['points'])} to beat "
                                 f"{self.name(r['opponent'])} by {f2(r['margin'])}."}
                        for r in sorted(notable, key=lambda r: -r["season"])]
        cur = [t for t in self.teams.values() if t["season"] == L["season"]]
        cur.sort(key=lambda t: (-(t["wins"] or 0) - 0.5 * (t["ties"] or 0), -(t["points_for"] or 0)))
        standings = [{"rank": i + 1, "manager": self.mgr(t["team_key"]), "team": t["name"],
                      "record": record_str(t["wins"], t["losses"], t["ties"]), "pointsFor": t["points_for"]}
                     for i, t in enumerate(cur)]
        upcoming_week = L["week"] + 1
        fiercest = sorted([h for h in h2h.values() if h["meetings"] >= 6],
                          key=lambda h: (abs(h["aWins"] - h["bWins"]), -h["meetings"]))[:4]
        return {"headlines": heads, "recordsBroken": broken[:12], "streaks": streaks[:8],
                "thisWeekInHistory": on_this_week, "historyWeek": upcoming_week if any(
                    r["week"] == upcoming_week for r in history) else L["week"],
                "standings": standings, "fiercestRivalries": [
                    {"pair": f"{h['a']}--{h['b']}", "a": h["a"], "b": h["b"], "aWins": h["aWins"],
                     "bWins": h["bWins"], "ties": h["ties"], "caption": h["caption"]} for h in fiercest],
                "totals": {"seasons": len(self.seasons), "games": len(self.games("all")) // 2,
                           "points": round(sum(r["points"] for r in self.games("all")), 2),
                           "managers": len(self.managers), "trades": len({t["transaction_key"] for t in self.txs
                                                                         if t["type"] == "trade"})}}

    def live_board(self, live: dict | None) -> dict | None:
        """In-progress week from `scrape --live`. Display-only: it never feeds records, streaks or standings."""
        if not live or live.get("season") != self.latest["season"] or live.get("week", 0) <= self.latest["week"]:
            return None  # missing, or already final in the database
        try:
            fetched = datetime.fromisoformat(live["fetchedAt"])
        except (KeyError, ValueError):
            return None
        if (datetime.now(timezone.utc) - fetched).days >= 7:
            return None
        completed = defaultdict(list)
        for row in self.rows:
            if row["season"] == live["season"] and row["week"] < live["week"]:
                completed[row["team_key"]].append(row["points"])

        games = []
        for g in live.get("games", []):
            sides = [{"manager": self.mgr(t["teamKey"]), "team": self.teams.get(t["teamKey"], {}).get("name", ""),
                      "points": t["points"], "projected": t["projected"]} for t in g["teams"]]
            if len(sides) != 2:
                continue
            a, b = sides
            lead = "a" if a["points"] > b["points"] else "b" if b["points"] > a["points"] else None
            proj = None
            if a["projected"] is not None and b["projected"] is not None and a["projected"] != b["projected"]:
                proj = "a" if a["projected"] > b["projected"] else "b"
            spread = point_spread(*(completed[t["teamKey"]] for t in g["teams"]))
            games.append({"label": g.get("label", ""), "a": a, "b": b, "leader": lead,
                          "projectedWinner": proj, "spread": spread})
        if not games:
            return None
        started = any(g[s]["points"] for g in games for s in ("a", "b"))
        return {"season": live["season"], "week": live["week"], "final": bool(live.get("final")),
                "started": started, "updatedAt": live["fetchedAt"], "games": games}

    def find_logos(self, folder: Path) -> dict[int, dict[str, str]]:
        """{season: {manager: site-relative path}} for every team logo downloaded by the scraper."""
        from .scrape import is_default_logo, logo_stem

        out: dict[int, dict[str, str]] = defaultdict(dict)
        for t in self.teams.values():
            if is_default_logo(t.get("logo_url")):
                continue
            hit = next(iter(sorted((folder / str(t["season"])).glob(f"{logo_stem(t['team_id'], t['logo_url'])}.*"))),
                       None)
            if hit:
                out[t["season"]][self.mgr(t["team_key"])] = f"logos/{t['season']}/{hit.name}"
        return dict(out)

    # ---------- build ----------
    def build(self, live: dict | None = None) -> dict[str, object]:
        bench = self.bench_rows()
        drafts = self.draft_values()
        trades = self.trades()
        streaks = {seg: self.streaks(seg) for seg in SEGMENTS}
        active = streaks["regular"][2]
        allplay = self.all_play()
        profile = self.playoff_profile()
        self.marks, self.bm = self.benchmarks()
        self.carries = self.carry()
        from . import awards as awards_mod  # awards imports helpers from this module
        self.grades = self.draft_grades()
        self.sos = season_mod.sos_for_all(self, allplay)
        fun = awards_mod.build_fun(self, bench)
        self.cabinet = fun["cabinet"]
        records = self.record_book(streaks, allplay, profile)
        shame = self.hall_of_shame(bench, drafts, trades, allplay)
        h2h = self.head_to_head()
        unlucky = next(c for c in shame if c["slug"] == "unlucky-losses")
        managers = self.manager_pages(bench, drafts, trades, h2h, unlucky, active, allplay, profile)
        lobby = self.lobby({"records": records, "shame": shame}, bench, active, h2h)
        lobby["live"] = self.live_board(live)
        lobby["previews"] = matchup_previews(self, live if lobby["live"] else None)
        latest_season = self.seasons.get(self.latest["season"], {})
        meta = {
            "leagueName": config.LEAGUE_NAME, "leagueUrl": config.LEAGUE_URL, "isSample": self.is_sample,
            "latest": self.latest, "latestFinished": bool(latest_season.get("is_finished")),
            "generatedAt": datetime.now(timezone.utc).isoformat(timespec="minutes"),
            "liveUpdatedAt": lobby["live"]["updatedAt"] if lobby["live"] else None,
            "seasons": sorted(self.seasons, reverse=True),
            "managers": [{"id": m["id"], "name": m["name"], "color": m["color"], "initials": m["initials"],
                          "logo": self.managers[m["id"]]["logo"], "active": m["active"]} for m in managers],
            "logos": {str(y): v for y, v in self.logos.items()},
            "lore": self.lore,
        }
        lobby["awards"] = next((w for w in fun["weeks"] if w["id"] == fun["latest"]), None)
        return {"meta": meta, "lobby": lobby, "records": records, "shame": shame, "managers": managers,
                "rivalries": h2h, "champions": self.champions(), "archives": self.archives(drafts, trades),
                "season": self.this_season(allplay), "fun": fun}

    # ---------- this season / draft grades ----------
    def this_season(self, allplay) -> dict:
        season = self.latest["season"]
        lab = season_mod.SeasonLab(self, season)
        sos = lab.sos(allplay)
        sos_rows = sorted(({"manager": self.mgr(t), "team": self.teams[t]["name"], **v} for t, v in sos.items()),
                          key=lambda r: r.get("pastRank") or 99)
        return {"season": season, "finished": self.finished(season), "odds": lab.odds(), "power": lab.power(),
                "sos": sos_rows, "playoffStart": lab.po_start}

    def draft_grades(self) -> dict:
        kept = defaultdict(float)
        for p in self.slots:
            kept[(p["season"], p["team_key"], p["player_key"])] += p["points"] or 0
        fraction = {}
        for season, s in self.seasons.items():
            end = s.get("end_week") or 17
            fraction[season] = 1.0 if s.get("is_finished") else min(1.0, (s.get("last_completed_week") or 0) / end)
        picks = [{**p, "manager": self.mgr(p["team_key"])} for p in self.picks]
        g = draft_mod.grade_drafts(picks, {s: self.finished(s) for s in self.seasons}, fraction, kept)
        g["careers"] = draft_mod.career_gpa(g["teams"])
        return g


def build(db_path: Path, out_dir: Path, live_path: Path | None = None) -> None:
    live_path = live_path or config.LIVE_JSON
    live = None
    if live_path.exists():
        try:
            live = json.loads(live_path.read_text())
        except ValueError:
            live = None
    conn = db.connect(db_path)
    is_sample = db.get_meta(conn, "is_sample") == "1"
    cfg_path = config.DATA_DIR / ("sample_managers.yaml" if is_sample else "managers.yaml")
    museum = Museum(conn, load_config(cfg_path))
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, payload in museum.build(live).items():
        (out_dir / f"{name}.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
