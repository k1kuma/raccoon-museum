"""'This season' analytics: playoff odds (Monte Carlo), power rankings and strength of schedule.

Everything here reads only final regular-season weeks (Museum.final_regular), so live in-progress scores never
move the odds, rankings or SOS. Simulations use a fixed RNG seed per season/week so builds are reproducible."""
from __future__ import annotations

import random
from collections import defaultdict
from statistics import mean, pstdev

SIMS = 10_000          # current snapshot
HISTORY_SIMS = 2_000   # each earlier week in the odds-over-time chart
SHRINK_GAMES = 3       # prior strength (in games) pulling each team's mean toward the league mean
POWER_WEIGHTS = {"allPlay": 0.40, "form": 0.25, "pointsFor": 0.20, "median": 0.15}


def bracket_order(size: int) -> list[int]:
    """Standard seeded bracket positions, e.g. 8 -> [1, 8, 4, 5, 2, 7, 3, 6] (1 meets 8, winner meets 4/5 winner)."""
    order = [1]
    while len(order) < size:
        n = len(order) * 2
        order = [x for s in order for x in (s, n + 1 - s)]
    return order


def playoff_shape(num_teams: int, num_playoff: int | None, po_start: int | None, end_week: int | None) -> dict:
    rounds = max(1, (end_week or 0) - (po_start or 0) + 1) if po_start and end_week else 3
    n = min(num_playoff or 6, num_teams)
    size = 2 ** rounds
    while size < n:
        size *= 2
    return {"teams": n, "rounds": rounds, "size": size, "byes": size - n}


def rank_teams(teams: list[str], wins: dict[str, float], pf: dict[str, float]) -> list[str]:
    """Regular-season order: win total (ties count half), then the league's tiebreaker, points for."""
    return sorted(teams, key=lambda t: (-wins[t], -pf[t], t))


def team_models(scores: dict[str, list[float]]) -> dict[str, tuple[float, float]]:
    """(mean, sd) per team: the team's mean shrunk toward the league mean, and a pooled within-team sd."""
    allpts = [p for xs in scores.values() for p in xs]
    league_mu = mean(allpts) if allpts else 100.0
    resid = [p - mean(xs) for xs in scores.values() if len(xs) > 1 for p in xs]
    sd = pstdev(resid) if len(resid) > 2 else (pstdev(allpts) if len(allpts) > 2 else 20.0)
    sd = max(sd, 8.0)
    out = {}
    for t, xs in scores.items():
        n = len(xs)
        mu = (sum(xs) + SHRINK_GAMES * league_mu) / (n + SHRINK_GAMES)
        out[t] = (mu, sd)
    return out


def simulate(teams: list[str], played: list[dict], remaining: list[list[tuple[str, str]] | None],
             shape: dict, sims: int, seed: int, known_playoffs: dict | None = None) -> dict[str, dict]:
    """Monte Carlo the rest of a regular season and the winners' bracket.

    played: final regular-season games [{team, opp, pts, opp_pts}] (one row per team per game).
    remaining: one entry per remaining regular-season week: a list of pairings, or None when the schedule is
    unknown (random pairings are drawn). known_playoffs: {(round, frozenset(teams)): winner} for bracket games
    already final. Returns per team: playoff/bye/title/last probabilities, average seed and seed distribution."""
    rng = random.Random(seed)
    gauss = rng.gauss
    wins0, pf0 = defaultdict(float), defaultdict(float)
    scores = {t: [] for t in teams}
    for g in played:
        wins0[g["team"]] += 1 if g["pts"] > g["opp_pts"] else 0.5 if g["pts"] == g["opp_pts"] else 0
        pf0[g["team"]] += g["pts"]
        scores[g["team"]].append(g["pts"])
    model = team_models(scores)
    order = bracket_order(shape["size"])
    n_po, byes = shape["teams"], shape["byes"]
    known_playoffs = known_playoffs or {}
    acc = {t: {"playoff": 0, "bye": 0, "title": 0, "last": 0, "seedSum": 0, "seeds": [0] * len(teams)}
           for t in teams}
    for _ in range(sims):
        wins, pf = dict(wins0), dict(pf0)
        for t in teams:
            wins.setdefault(t, 0.0)
            pf.setdefault(t, 0.0)
        for week in remaining:
            pairs = week
            if pairs is None:
                shuffled = teams[:]
                rng.shuffle(shuffled)
                pairs = list(zip(shuffled[::2], shuffled[1::2]))
            for a, b in pairs:
                ma, sa = model[a]
                mb, sb = model[b]
                pa, pb = gauss(ma, sa), gauss(mb, sb)
                pf[a] += pa
                pf[b] += pb
                if pa > pb:
                    wins[a] += 1
                else:
                    wins[b] += 1
        ranked = rank_teams(teams, wins, pf)
        for i, t in enumerate(ranked):
            a = acc[t]
            a["seedSum"] += i + 1
            a["seeds"][i] += 1
            if i < n_po:
                a["playoff"] += 1
            if i < byes:
                a["bye"] += 1
        acc[ranked[-1]]["last"] += 1
        # Winners' bracket on fixed seeds (no reseeding), byes for the top seeds.
        alive = [ranked[s - 1] if s <= n_po else None for s in order]
        rnd = 0
        while len(alive) > 1:
            nxt = []
            for a, b in zip(alive[::2], alive[1::2]):
                if a is None or b is None:
                    nxt.append(a or b)
                    continue
                won = known_playoffs.get((rnd, frozenset((a, b))))
                if won is None:
                    won = a if gauss(*model[a]) > gauss(*model[b]) else b
                nxt.append(won)
            alive = nxt
            rnd += 1
        if alive and alive[0]:
            acc[alive[0]]["title"] += 1
    out = {}
    for t, a in acc.items():
        out[t] = {"playoff": a["playoff"] / sims, "bye": a["bye"] / sims, "title": a["title"] / sims,
                  "last": a["last"] / sims, "avgSeed": round(a["seedSum"] / sims, 2),
                  "seeds": [round(x / sims, 4) for x in a["seeds"]]}
    return out


def power_scores(rows: dict[str, dict]) -> dict[str, float]:
    """Composite 0–100 from all-play %, recent form, points for and median %. Each component is min-max scaled
    across the league that week, then weighted by POWER_WEIGHTS."""
    def scaled(key):
        xs = [r[key] for r in rows.values()]
        lo, hi = min(xs), max(xs)
        return {t: (r[key] - lo) / (hi - lo) if hi > lo else 0.5 for t, r in rows.items()}

    parts = {k: scaled(k) for k in POWER_WEIGHTS}
    return {t: round(100 * sum(POWER_WEIGHTS[k] * parts[k][t] for k in POWER_WEIGHTS), 1) for t in rows}


class SeasonLab:
    """Builds season.json for one season (the latest) from a Museum instance."""

    def __init__(self, museum, season: int):
        self.m, self.season = museum, season
        self.s = museum.seasons[season]
        self.teams = sorted(k for k, t in museum.teams.items() if t["season"] == season)
        self.games = [r for r in museum.final_regular() if r["season"] == season]
        self.po_start = self.s.get("playoff_start_week") or ((self.s.get("end_week") or 17) - 2)
        self.reg_weeks = list(range(self.s.get("start_week") or 1, self.po_start))
        self.done_weeks = sorted({r["week"] for r in self.games})
        self.shape = playoff_shape(len(self.teams), self.s.get("num_playoff_teams"), self.po_start,
                                   self.s.get("end_week"))
        all_rows = [r for r in museum.rows if r["season"] == season]
        self.pairs_by_week: dict[int, list[tuple[str, str]]] = defaultdict(list)
        for r in all_rows:
            if r["game_type"] == "regular" and r["opponent_key"] and r["team_key"] < r["opponent_key"]:
                self.pairs_by_week[r["week"]].append((r["team_key"], r["opponent_key"]))
        for row in museum.conn.execute("SELECT week, team_key, opponent_key FROM schedule WHERE season=?",
                                       (season,)).fetchall():
            if row["week"] not in self.done_weeks and row["team_key"] < (row["opponent_key"] or ""):
                if (row["team_key"], row["opponent_key"]) not in self.pairs_by_week[row["week"]]:
                    self.pairs_by_week[row["week"]].append((row["team_key"], row["opponent_key"]))

    # ---- helpers
    def played_through(self, week: int) -> list[dict]:
        return [{"team": r["team_key"], "opp": r["opponent_key"], "pts": r["points"],
                 "opp_pts": r["opponent_points"] or 0} for r in self.games if r["week"] <= week]

    def remaining_after(self, week: int) -> list[list[tuple[str, str]] | None]:
        out = []
        for w in self.reg_weeks:
            if w <= week:
                continue
            pairs = self.pairs_by_week.get(w)
            out.append(pairs if pairs and len(pairs) * 2 == len(self.teams) else None)
        return out

    def known_bracket(self) -> dict:
        """Winners'-bracket games already final, keyed by (round index, the two teams)."""
        known = {}
        lc = self.s.get("last_completed_week") or 0
        for r in self.m.games("playoffs"):
            if r["season"] != self.season or not r.get("title_path") or r["result"] != "W":
                continue
            if not self.s.get("is_finished") and r["week"] > lc:
                continue
            known[(r["week"] - self.po_start, frozenset((r["team_key"], r["opponent_key"])))] = r["team_key"]
        return known

    # ---- playoff odds
    def odds(self) -> dict:
        last = max(self.done_weeks, default=0)
        remaining = self.remaining_after(last)
        known = self.known_bracket()
        current = simulate(self.teams, self.played_through(last), remaining, self.shape, SIMS,
                           seed=self.season * 100 + last, known_playoffs=known)
        history = []
        for w in self.done_weeks:
            res = current if w == last else simulate(self.teams, self.played_through(w), self.remaining_after(w),
                                                     self.shape, HISTORY_SIMS, seed=self.season * 100 + w)
            history.append({"week": w, "teams": {self.m.mgr(t): {k: round(res[t][k], 4)
                                                                 for k in ("playoff", "bye", "title", "last")}
                                                 for t in self.teams}})
        wins, pf, rec = defaultdict(float), defaultdict(float), defaultdict(lambda: [0, 0, 0])
        for g in self.played_through(last):
            res = "W" if g["pts"] > g["opp_pts"] else "T" if g["pts"] == g["opp_pts"] else "L"
            wins[g["team"]] += {"W": 1, "T": 0.5, "L": 0}[res]
            pf[g["team"]] += g["pts"]
            rec[g["team"]]["WLT".index(res)] += 1
        ranked = rank_teams(self.teams, wins, pf)
        unknown = sum(1 for wk in remaining if wk is None)
        rows = []
        for i, t in enumerate(ranked):
            o = current[t]
            rows.append({"manager": self.m.mgr(t), "team": self.m.teams[t]["name"], "seedNow": i + 1,
                         "record": "-".join(map(str, rec[t][:2])) + (f"-{rec[t][2]}" if rec[t][2] else ""),
                         "pointsFor": round(pf[t], 2), **{k: round(o[k], 4) for k in ("playoff", "bye", "title",
                                                                                       "last")},
                         "avgSeed": o["avgSeed"], "seeds": o["seeds"]})
        rows.sort(key=lambda r: (-r["playoff"], r["avgSeed"]))
        return {"asOfWeek": last, "sims": SIMS, "historySims": HISTORY_SIMS,
                "remainingWeeks": len(remaining), "unknownWeeks": unknown, "shape": self.shape,
                "rows": rows, "history": history}

    # ---- power rankings
    def power(self) -> dict:
        by_week = defaultdict(dict)
        for r in self.games:
            by_week[r["week"]][r["team_key"]] = r
        hist = []
        acc = {t: {"pts": [], "apW": 0.0, "apN": 0, "medW": 0.0, "medN": 0} for t in self.teams}
        for w in self.done_weeks:
            week_rows = by_week[w]
            pts = sorted(r["points"] for r in week_rows.values())
            n = len(pts)
            med = pts[n // 2] if n % 2 else (pts[n // 2 - 1] + pts[n // 2]) / 2
            for t, r in week_rows.items():
                a = acc[t]
                a["pts"].append(r["points"])
                others = [x["points"] for k, x in week_rows.items() if k != t]
                a["apW"] += sum(1 for p in others if p < r["points"]) + 0.5 * sum(1 for p in others
                                                                                  if p == r["points"])
                a["apN"] += len(others)
                a["medW"] += 1 if r["points"] > med else 0.5 if r["points"] == med else 0
                a["medN"] += 1
            rows = {t: {"allPlay": a["apW"] / a["apN"] if a["apN"] else 0,
                        "form": mean(a["pts"][-3:]) if a["pts"] else 0, "pointsFor": sum(a["pts"]),
                        "median": a["medW"] / a["medN"] if a["medN"] else 0} for t, a in acc.items()}
            score = power_scores(rows)
            order = sorted(self.teams, key=lambda t: (-score[t], -rows[t]["pointsFor"]))
            hist.append({"week": w, "rank": {t: i + 1 for i, t in enumerate(order)}, "score": score, "rows": rows})
        if not hist:
            return {"week": None, "rows": [], "weights": POWER_WEIGHTS}
        cur, prev = hist[-1], (hist[-2] if len(hist) > 1 else None)
        out = []
        for t in sorted(self.teams, key=lambda t: cur["rank"][t]):
            r = cur["rows"][t]
            out.append({"rank": cur["rank"][t], "prev": prev["rank"][t] if prev else None,
                        "move": (prev["rank"][t] - cur["rank"][t]) if prev else 0,
                        "manager": self.m.mgr(t), "team": self.m.teams[t]["name"], "score": cur["score"][t],
                        "allPlay": round(r["allPlay"], 3), "form": round(r["form"], 2),
                        "pointsFor": round(r["pointsFor"], 2), "median": round(r["median"], 3),
                        "path": [h["rank"][t] for h in hist]})
        return {"week": cur["week"], "weights": POWER_WEIGHTS, "rows": out, "weeks": [h["week"] for h in hist]}

    # ---- strength of schedule
    def sos(self, allplay: dict) -> dict[str, dict]:
        """Past SOS: opponents' average score against you and their season all-play %. Remaining SOS: the
        all-play % and points per game of opponents still to come. Rank 1 = toughest."""
        ppg = defaultdict(list)
        for r in self.games:
            ppg[r["team_key"]].append(r["points"])
        team_ppg = {t: mean(v) for t, v in ppg.items() if v}
        ap = {t: allplay.get(t, {}).get("pct", 0.5) for t in self.teams}
        out = {}
        last = max(self.done_weeks, default=0)
        future = defaultdict(list)
        for w, pairs in self.pairs_by_week.items():
            if w > last and w < self.po_start:
                for a, b in pairs:
                    future[a].append(b)
                    future[b].append(a)
        for t in self.teams:
            mine = [r for r in self.games if r["team_key"] == t and r["opponent_key"]]
            opps = [r["opponent_key"] for r in mine]
            out[t] = {"pastOppPoints": round(mean(r["opponent_points"] or 0 for r in mine), 2) if mine else None,
                      "pastOppAllPlay": round(mean(ap[o] for o in opps), 3) if opps else None,
                      "remainingOppAllPlay": round(mean(ap[o] for o in future[t]), 3) if future[t] else None,
                      "remainingOppPpg": round(mean(team_ppg.get(o, 0) for o in future[t]), 2) if future[t] else None,
                      "remainingGames": len(future[t])}
        for key, rank_key in (("pastOppAllPlay", "pastRank"), ("remainingOppAllPlay", "remainingRank")):
            vals = sorted((v[key], t) for t, v in out.items() if v[key] is not None)
            for i, (_, t) in enumerate(reversed(vals)):
                out[t][rank_key] = i + 1
        return out


def sos_for_all(museum, allplay: dict) -> dict[str, dict]:
    """Past SOS for every team-season (finished seasons have no 'remaining')."""
    out = {}
    for season in museum.seasons:
        out.update(SeasonLab(museum, season).sos(allplay))
    return out
