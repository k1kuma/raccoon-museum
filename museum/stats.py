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
from .managers import Resolver, load_config

TOP_N = 25
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

        for r in self.rows:
            # Yahoo flags consolation games as playoffs too; here "playoffs" means the championship bracket only.
            if r["is_consolation"]:
                r["is_playoffs"] = 0
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
        if r.get("is_playoffs"):
            return base + f" ({self.round_name(r['season'], r['week'])})"
        if r.get("is_consolation"):
            return base + " (consolation)"
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

    def games(self, include_consolation: bool = True) -> list[dict]:
        return [r for r in self.rows if include_consolation or not r["is_consolation"]]

    # ---------- streaks ----------
    def streaks(self) -> tuple[list[dict], list[dict], dict[str, dict]]:
        by_mgr = defaultdict(list)
        for r in self.games(include_consolation=False):
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
                         "active": g is None}
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
        return self.entry(s["length"], f"{s['length']} games",
                          f"{self.name(s['manager'])} {word} {s['length']} straight from {span}{tail}",
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
        for s in self.slots:
            if s["selected_position"] not in lineup.BENCH_SLOTS:
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

    # ---------- categories ----------
    def record_book(self, win_streaks, loss_streaks) -> list[dict]:
        games = self.games()
        cats = []

        def game_caption(r, lead):
            res = {"W": "won", "L": "lost", "T": "tied"}[r["result"]]
            return (f"{lead} {self.name(r['manager'])} ({r['team_name']}) scored {f2(r['points'])} against "
                    f"{self.name(r['opponent'])} in {self.week_label(r)} — and {res} "
                    f"{f2(r['points'])}–{f2(r['opponent_points'])}.")

        top = sorted(games, key=lambda r: -r["points"])[:TOP_N]
        cats.append(self.category("highest-scores", "Highest Single-Week Scores", "The league's offensive explosions.",
                                  [self.entry(r["points"], f2(r["points"]), game_caption(r, "Boom."),
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in top]))
        low = sorted([r for r in games if r["points"] > 0], key=lambda r: r["points"])[:TOP_N]
        cats.append(self.category("lowest-scores", "Lowest Single-Week Scores", "When the lineup just… didn't.",
                                  [self.entry(r["points"], f2(r["points"]), game_caption(r, "Oof."),
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in low]))
        winners = [r for r in games if r["result"] == "W"]
        blow = sorted(winners, key=lambda r: -r["margin"])[:TOP_N]
        cats.append(self.category("biggest-blowouts", "Biggest Blowouts", "Margins of victory that should be illegal.",
                                  [self.entry(r["margin"], f"+{f2(r['margin'])}",
                                              f"{self.name(r['manager'])} flattened {self.name(r['opponent'])} by "
                                              f"{f2(r['margin'])} points ({f2(r['points'])}–{f2(r['opponent_points'])}) "
                                              f"in {self.week_label(r)}.",
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in blow]))
        close = sorted([r for r in winners if r["margin"] > 0], key=lambda r: r["margin"])[:TOP_N]
        cats.append(self.category("closest-games", "Closest Games", "Decided by a stat correction and a prayer.",
                                  [self.entry(r["margin"], f"+{f2(r['margin'])}",
                                              f"{self.name(r['manager'])} edged {self.name(r['opponent'])} by just "
                                              f"{f2(r['margin'])} ({f2(r['points'])}–{f2(r['opponent_points'])}) in "
                                              f"{self.week_label(r)}.",
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in close]))
        cats.append(self.category("win-streaks", "Longest Winning Streaks", "Regular season and playoffs, across seasons.",
                                  [self.streak_entry(s) for s in sorted(
                                      win_streaks, key=lambda s: (-s["length"], s["end"]["season"]))[:15]]))
        cats.append(self.category("losing-streaks", "Longest Losing Streaks", "Somebody check on them.",
                                  [self.streak_entry(s) for s in sorted(
                                      loss_streaks, key=lambda s: (-s["length"], s["end"]["season"]))[:15]]))
        playoff = sorted([r for r in games if r["is_playoffs"]], key=lambda r: -r["points"])[:15]
        cats.append(self.category("playoff-explosions", "Biggest Playoff Performances", "Clutch, defined.",
                                  [self.entry(r["points"], f2(r["points"]), game_caption(r, "When it mattered most,"),
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in playoff]))
        seasons = [t for t in self.teams.values() if self.finished(t["season"])]
        pf = sorted(seasons, key=lambda t: -(t["points_for"] or 0))[:TOP_N]
        cats.append(self.category("most-points-season", "Most Points in a Season", "Regular-season points for.",
                                  [self.entry(t["points_for"], f2(t["points_for"]),
                                              f"{self.name(self.mgr(t['team_key']))}'s {t['name']} piled up "
                                              f"{f2(t['points_for'])} points in {t['season']} on the way to a "
                                              f"{record_str(t['wins'], t['losses'], t['ties'])} record and a "
                                              f"{ordinal(t['final_rank'] or 0)}-place finish.",
                                              self.mgr(t["team_key"]), t["season"]) for t in pf]))

        def pct(t):
            g = (t["wins"] or 0) + (t["losses"] or 0) + (t["ties"] or 0)
            return ((t["wins"] or 0) + 0.5 * (t["ties"] or 0)) / g if g else 0

        best = sorted(seasons, key=lambda t: (-pct(t), -(t["points_for"] or 0)))[:15]
        cats.append(self.category("best-records", "Best Regular-Season Records", "Dominance, by the numbers.",
                                  [self.entry(round(pct(t), 3), record_str(t["wins"], t["losses"], t["ties"]),
                                              f"{self.name(self.mgr(t['team_key']))} went "
                                              f"{record_str(t['wins'], t['losses'], t['ties'])} in {t['season']} "
                                              f"with {t['name']}"
                                              + (" — and won it all." if t["final_rank"] == 1 else
                                                 f", finishing {ordinal(t['final_rank'] or 0)}."),
                                              self.mgr(t["team_key"]), t["season"]) for t in best]))
        return cats

    def hall_of_shame(self, bench, drafts, trades) -> list[dict]:
        cats = []
        top_bench = sorted(bench, key=lambda r: -r["left"])[:TOP_N]

        def bench_caption(r):
            tail = ""
            if r["would_have_won"]:
                tail = f" The optimal lineup ({f2(r['optimal'])}) would have beaten {self.name(r['opponent'])}."
            elif r["result"] == "W":
                tail = " Luckily, it didn't cost them the game."
            return (f"{self.name(r['manager'])} left {f2(r['left'])} points on the bench in {self.week_label(r)}, "
                    f"scoring {f2(r['actual'])} when {f2(r['optimal'])} was possible.{tail}")

        cats.append(self.category("bench-points", "Most Points Left on the Bench",
                                  "Optimal lineup minus actual lineup, in a single week.",
                                  [self.entry(r["left"], f2(r["left"]), bench_caption(r), r["manager"], r["season"],
                                              r["week"], r["opponent"], wouldHaveWon=r["would_have_won"])
                                   for r in top_bench]))
        costly = sorted([r for r in bench if r["would_have_won"]], key=lambda r: -r["left"])[:TOP_N]
        cats.append(self.category("bench-cost-a-win", "Benched Their Way to a Loss",
                                  "Losses where the optimal lineup would have won.",
                                  [self.entry(r["left"], f2(r["left"]), bench_caption(r), r["manager"], r["season"],
                                              r["week"], r["opponent"]) for r in costly]))
        season_bench = defaultdict(lambda: {"left": 0.0, "costly": 0})
        for r in bench:
            k = (r["manager"], r["season"], r["team_name"])
            season_bench[k]["left"] += r["left"]
            season_bench[k]["costly"] += int(r["would_have_won"])
        sb = sorted(season_bench.items(), key=lambda kv: -kv[1]["left"])[:15]
        cats.append(self.category("bench-season", "Most Bench Points in a Season", "A full season of second-guessing.",
                                  [self.entry(round(v["left"], 2), f2(v["left"]),
                                              f"{self.name(m)} ({team}) stranded {f2(v['left'])} points on the bench "
                                              f"across {s}" + (f", costing {v['costly']} win"
                                                               f"{'s' if v['costly'] != 1 else ''}." if v["costly"]
                                                               else "."),
                                              m, s) for (m, s, team), v in sb]))

        # Losses with the 2nd-highest score of the week.
        by_week = defaultdict(list)
        for r in self.games():
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
        losses = sorted([r for r in self.games() if r["result"] == "L"], key=lambda r: -r["points"])[:TOP_N]
        cats.append(self.category("heartbreaks", "Most Points in a Loss", "Did everything right. Lost anyway.",
                                  [self.entry(r["points"], f2(r["points"]),
                                              f"{self.name(r['manager'])} dropped {f2(r['points'])} in "
                                              f"{self.week_label(r)} and lost to {self.name(r['opponent'])} "
                                              f"({f2(r['opponent_points'])}).",
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in losses]))
        stolen = sorted([r for r in self.games() if r["result"] == "W"], key=lambda r: r["points"])[:TOP_N]
        cats.append(self.category("ugliest-wins", "Ugliest Wins", "Fewest points in a victory. A W is a W.",
                                  [self.entry(r["points"], f2(r["points"]),
                                              f"{self.name(r['manager'])} beat {self.name(r['opponent'])} with just "
                                              f"{f2(r['points'])} points ({f2(r['opponent_points'])} against) in "
                                              f"{self.week_label(r)}.",
                                              r["manager"], r["season"], r["week"], r["opponent"]) for r in stolen]))
        busts = sorted([p for p in drafts if p["round"] and p["round"] <= 5 and p["final"]],
                       key=lambda p: p["value"])[:TOP_N]
        cats.append(self.category("worst-draft-picks", "Worst Draft Picks",
                                  "Early-round picks (Rounds 1–5) that finished furthest below their draft slot.",
                                  [self.entry(p["value"], f"{p['value']:+d} spots", self.pick_caption(p),
                                              p["manager"], p["season"], player=p["player_name"], pick=p["pick"])
                                   for p in busts]))
        worst = sorted(trades, key=lambda t: t["net"])[:15]
        cats.append(self.category("worst-trades", "Most Lopsided Trades (Losing Side)",
                                  "Starter points gained minus starter points given away, rest of season.",
                                  [self.entry(t["net"], f"{f2(t['net'])}", self.trade_caption(t), t["manager"],
                                              t["season"], t["week"],
                                              t["partners"][0] if t["partners"] else None) for t in worst]))
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
        return cats

    def category(self, slug, title, blurb, entries, ranked=True) -> dict:
        for i, e in enumerate(entries):
            e["rank"] = i + 1
        return {"slug": slug, "title": title, "blurb": blurb, "entries": entries, "ranked": ranked,
                "hasNew": any(e["isNew"] for e in entries)}

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
        for r in self.games():
            a, b = r["manager"], r["opponent"]
            if not a or not b or a > b:
                continue
            p = pairs.setdefault(f"{a}--{b}", {"a": a, "b": b, "games": []})
            p["games"].append({"season": r["season"], "week": r["week"], "aPoints": r["points"],
                               "bPoints": r["opponent_points"], "result": r["result"],
                               "kind": "playoffs" if r["is_playoffs"] else
                               "consolation" if r["is_consolation"] else "regular",
                               "label": self.week_label(r), "isNew": self.is_new(r["season"], r["week"])})
        out = {}
        mids = sorted(self.managers)
        for a, b in combinations(mids, 2):
            p = pairs.get(f"{a}--{b}", {"a": a, "b": b, "games": []})
            g = sorted(p["games"], key=lambda x: (x["season"], x["week"]), reverse=True)
            aw = sum(1 for x in g if x["result"] == "W")
            bw = sum(1 for x in g if x["result"] == "L")
            ties = sum(1 for x in g if x["result"] == "T")
            apf = round(sum(x["aPoints"] for x in g), 2)
            bpf = round(sum(x["bPoints"] for x in g), 2)
            playoff = [x for x in g if x["kind"] == "playoffs"]
            big_a = max((x for x in g if x["result"] == "W"), key=lambda x: x["aPoints"] - x["bPoints"], default=None)
            big_b = max((x for x in g if x["result"] == "L"), key=lambda x: x["bPoints"] - x["aPoints"], default=None)
            # current streak
            streak_owner, streak = None, 0
            for x in g:
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
                leader = na if aw > bw else nb if bw > aw else None
                hi, lo = max(aw, bw), min(aw, bw)
                caption = (f"{leader} leads the all-time series {hi}-{lo}" + (f"-{ties}" if ties else "")
                           if leader else f"The series is dead even at {aw}-{bw}" + (f"-{ties}" if ties else ""))
                caption += f" over {len(g)} meeting{'s' if len(g) != 1 else ''}"
                if playoff:
                    pa = sum(1 for x in playoff if x["result"] == "W")
                    caption += f", including {len(playoff)} playoff clash{'es' if len(playoff) != 1 else ''} " \
                               f"({na} {pa}-{len(playoff) - pa})"
                caption += "."
                if streak_owner and streak >= 2:
                    caption += f" {self.name(streak_owner)} has won the last {streak}."
            out[f"{a}--{b}"] = {
                "a": a, "b": b, "aWins": aw, "bWins": bw, "ties": ties, "aPoints": apf, "bPoints": bpf,
                "meetings": len(g), "playoffMeetings": len(playoff), "caption": caption,
                "streak": {"manager": streak_owner, "length": streak} if streak_owner else None,
                "biggestA": big_a, "biggestB": big_b, "games": g,
                "hasNew": any(x["isNew"] for x in g),
            }
        return out

    # ---------- managers ----------
    def manager_pages(self, bench, drafts, trades, h2h, unlucky_cat, active_streaks) -> list[dict]:
        pages = []
        champs = self.champions()["trophyCase"]
        trophy = {c["manager"]: c for c in champs}
        for mid, m in sorted(self.managers.items(), key=lambda kv: kv[1]["name"].lower()):
            teams = sorted([t for t in self.teams.values() if self.mgr(t["team_key"]) == mid],
                           key=lambda t: t["season"])
            seasons = []
            for t in teams:
                s = self.seasons[t["season"]]
                seasons.append({"season": t["season"], "team": t["name"], "wins": t["wins"], "losses": t["losses"],
                                "ties": t["ties"], "pointsFor": t["points_for"], "pointsAgainst": t["points_against"],
                                "rank": t["final_rank"], "seed": t["playoff_seed"], "finished": bool(s["is_finished"]),
                                "madePlayoffs": bool(t["playoff_seed"] and s.get("num_playoff_teams")
                                                     and t["playoff_seed"] <= s["num_playoff_teams"]
                                                     and s["is_finished"])})
            w = sum(t["wins"] or 0 for t in teams)
            l = sum(t["losses"] or 0 for t in teams)
            ti = sum(t["ties"] or 0 for t in teams)
            my_games = [r for r in self.rows if r["manager"] == mid]
            po = [r for r in my_games if r["is_playoffs"]]
            done = [s for s in seasons if s["finished"] and s["rank"]]
            hi = max(my_games, key=lambda r: r["points"], default=None)
            lo = min((r for r in my_games if r["points"] > 0), key=lambda r: r["points"], default=None)
            my_picks = [p for p in drafts if p["manager"] == mid and p["final"]]
            my_trades = [t for t in trades if t["manager"] == mid]
            my_bench = [r for r in bench if r["manager"] == mid]
            tc = trophy.get(mid, {"titles": [], "runnerUps": [], "thirds": []})
            rivals = []
            for key, h in h2h.items():
                if mid not in (h["a"], h["b"]) or not h["meetings"]:
                    continue
                me_a = h["a"] == mid
                rivals.append({"opponent": h["b"] if me_a else h["a"], "pair": key,
                               "wins": h["aWins"] if me_a else h["bWins"],
                               "losses": h["bWins"] if me_a else h["aWins"], "ties": h["ties"],
                               "meetings": h["meetings"]})
            rivals.sort(key=lambda r: (-r["meetings"], r["opponent"]))
            game_view = lambda r: r and {"points": r["points"], "opponent": r["opponent"],  # noqa: E731
                                         "opponentPoints": r["opponent_points"], "season": r["season"],
                                         "week": r["week"], "label": self.week_label(r), "result": r["result"]}
            pct = (w + 0.5 * ti) / (w + l + ti) if (w + l + ti) else 0
            first, last_season = (teams[0]["season"], teams[-1]["season"]) if teams else (None, None)
            title_txt = (f" with {len(tc['titles'])} championship{'s' if len(tc['titles']) != 1 else ''} "
                         f"({', '.join(map(str, tc['titles']))})" if tc["titles"] else "")
            caption = (f"{m['name']} has managed {len(teams)} season{'s' if len(teams) != 1 else ''} "
                       f"({first}–{last_season}), going {record_str(w, l, ti)} ({pct:.3f}){title_txt}.")
            streak = active_streaks.get(mid)
            pages.append({
                **{k: m[k] for k in ("id", "name", "nickname", "bio", "lore", "color", "initials")},
                "caption": caption, "firstSeason": first, "lastSeason": last_season,
                "active": m.get("active") if m.get("active") is not None else last_season == self.latest["season"],
                "career": {"wins": w, "losses": l, "ties": ti, "pct": round(pct, 3),
                           "pointsFor": round(sum(t["points_for"] or 0 for t in teams), 2),
                           "pointsAgainst": round(sum(t["points_against"] or 0 for t in teams), 2),
                           "playoffWins": sum(1 for r in po if r["result"] == "W"),
                           "playoffLosses": sum(1 for r in po if r["result"] == "L"),
                           "playoffApps": sum(1 for s in seasons if s["madePlayoffs"]),
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
                "streak": self.streak_entry(streak) if streak and streak["length"] >= 2 else None,
            })
        return pages

    def _pick_view(self, p):
        return {"season": p["season"], "pick": p["pick"], "round": p["round"], "player": p["player_name"],
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
            teams = sorted([t for t in self.teams.values() if t["season"] == season],
                           key=lambda t: (t["final_rank"] or 99, -(t["points_for"] or 0)))
            standings = [{"rank": t["final_rank"], "manager": self.mgr(t["team_key"]), "team": t["name"],
                          "wins": t["wins"], "losses": t["losses"], "ties": t["ties"], "pointsFor": t["points_for"],
                          "pointsAgainst": t["points_against"], "seed": t["playoff_seed"], "moves": t["moves"],
                          "trades": t["trades"], "draftGrade": t["draft_grade"]} for t in teams]
            weeks = defaultdict(list)
            for r in self.rows:
                if r["season"] == season and r["team_key"] < (r["opponent_key"] or ""):
                    weeks[r["week"]].append({"a": r["manager"], "b": r["opponent"], "aTeam": r["team_name"],
                                             "bTeam": r["opp_team_name"], "aPoints": r["points"],
                                             "bPoints": r["opponent_points"], "playoffs": bool(r["is_playoffs"]),
                                             "consolation": bool(r["is_consolation"])})
            bracket = []
            for week in sorted(w for w, gs in weeks.items() if any(g["playoffs"] for g in gs)):
                games = [g for g in weeks[week] if g["playoffs"]]
                if self.round_name(season, week) == "Championship week" and s["is_finished"]:
                    top2 = {st["manager"] for st in standings[:2]}
                    for g in games:
                        g["label"] = "Championship" if {g["a"], g["b"]} == top2 else "3rd place"
                    games.sort(key=lambda g: g.get("label") != "Championship")
                bracket.append({"week": week, "name": self.round_name(season, week), "games": games})
            picks = sorted(draft_by.get(season, []), key=lambda p: p["pick"])
            order = []
            for p in picks:
                if p["round"] == 1:
                    order.append(p["manager"])
            board = defaultdict(list)
            for p in picks:
                board[p["round"]].append({**self._pick_view(p), "manager": p["manager"],
                                          "tag": "steal" if p["value"] >= 40 else "bust" if p["value"] <= -40 and
                                          (p["round"] or 99) <= 6 else None})
            for rnd, row in board.items():
                row.sort(key=lambda c: order.index(c["manager"]) if c["manager"] in order else 99)
            champion = standings[0] if s["is_finished"] and standings else None
            caption = (f"{self.name(champion['manager'])} ({champion['team']}) won the {season} title."
                       if champion else f"The {season} season is in progress — through Week "
                                        f"{s.get('last_completed_week') or 0}.")
            out.append({"season": season, "name": s["name"], "finished": bool(s["is_finished"]),
                        "lastCompletedWeek": s.get("last_completed_week"), "numTeams": s["num_teams"],
                        "caption": caption, "standings": standings, "bracket": bracket,
                        "draftOrder": order, "draftBoard": [{"round": r, "picks": board[r]} for r in sorted(board)],
                        "weeks": [{"week": w, "games": weeks[w]} for w in sorted(weeks, reverse=True)],
                        "trades": [self._trade_view(t) for t in trades if t["season"] == season and t["net"] >= 0]})
        return out

    # ---------- lobby ----------
    def lobby(self, cats, bench, active_streaks, h2h) -> dict:
        L = self.latest
        week_rows = [r for r in self.rows if r["season"] == L["season"] and r["week"] == L["week"]]
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
            wb = [r for r in bench if r["season"] == L["season"] and r["week"] == L["week"]]
            if wb:
                bb = max(wb, key=lambda r: r["left"])
                heads.append({"kicker": "Bench blunder", "manager": bb["manager"],
                              "text": f"{self.name(bb['manager'])} left {f2(bb['left'])} points on the bench"
                                      + (" — enough to flip the result." if bb["would_have_won"] else ".")})
            ranked = sorted(week_rows, key=lambda r: -r["points"])
            if len(ranked) > 1 and ranked[1]["result"] == "L":
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
                                       "rank": e["rank"], "caption": e["caption"], "manager": e["manager"]})
        broken.sort(key=lambda b: b["rank"])
        streaks = [self.streak_entry(s) for s in active_streaks.values()
                   if s["length"] >= 2 and s["end"]["season"] == L["season"]]
        streaks.sort(key=lambda e: -e["value"])
        # "This week in history": same week number in past seasons.
        history = []
        for r in self.rows:
            if r["week"] == L["week"] + 1 and r["season"] < L["season"] and r["result"] == "W":
                history.append(r)
        if not history:
            history = [r for r in self.rows if r["week"] == L["week"] and r["season"] < L["season"]
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
                "totals": {"seasons": len(self.seasons), "games": len(self.rows) // 2,
                           "points": round(sum(r["points"] for r in self.rows), 2),
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
            games.append({"label": g.get("label", ""), "a": a, "b": b, "leader": lead, "projectedWinner": proj})
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
        win_streaks, loss_streaks, active = self.streaks()
        records = self.record_book(win_streaks, loss_streaks)
        shame = self.hall_of_shame(bench, drafts, trades)
        h2h = self.head_to_head()
        unlucky = next(c for c in shame if c["slug"] == "unlucky-losses")
        managers = self.manager_pages(bench, drafts, trades, h2h, unlucky, active)
        lobby = self.lobby({"records": records, "shame": shame}, bench, active, h2h)
        lobby["live"] = self.live_board(live)
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
        return {"meta": meta, "lobby": lobby, "records": records, "shame": shame, "managers": managers,
                "rivalries": h2h, "champions": self.champions(), "archives": self.archives(drafts, trades)}


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
