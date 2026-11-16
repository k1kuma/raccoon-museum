"""Draft grades: value over expected (VOE) per pick, a letter grade per team-season and a career draft GPA.

Expected points for a pick come from the league's own drafts: the average season total of the n-th player taken at
that position (smoothed over neighbours n-1..n+1), across finished seasons. In-progress seasons scale the
expectation by the share of the season played. "Kept value" counts only the points a player scored while he was
still on the drafting team's roster."""
from __future__ import annotations

from collections import defaultdict
from statistics import mean, pstdev

GRADES = [(1.5, "A+", 4.3), (1.0, "A", 4.0), (0.6, "A-", 3.7), (0.25, "B+", 3.3), (-0.1, "B", 3.0),
          (-0.45, "B-", 2.7), (-0.8, "C+", 2.3), (-1.15, "C", 2.0), (-1.5, "D", 1.0)]
F_GRADE = ("F", 0.0)


def grade_for(z: float) -> tuple[str, float]:
    for cut, letter, gpa in GRADES:
        if z >= cut:
            return letter, gpa
    return F_GRADE


def positional_ranks(picks: list[dict]) -> dict[int, int]:
    """pick number -> n where the pick was the n-th player taken at his position that draft."""
    seen: dict[str, int] = defaultdict(int)
    out = {}
    for p in sorted(picks, key=lambda p: p["pick"]):
        seen[p["position"] or "?"] += 1
        out[p["pick"]] = seen[p["position"] or "?"]
    return out


def expected_curve(drafts: dict[int, list[dict]]) -> dict[tuple[str, int], float]:
    """(position, n) -> expected full-season points, averaged over the given (finished) drafts."""
    samples: dict[tuple[str, int], list[float]] = defaultdict(list)
    for picks in drafts.values():
        ranks = positional_ranks(picks)
        by_pos: dict[str, dict[int, float]] = defaultdict(dict)
        for p in picks:
            by_pos[p["position"] or "?"][ranks[p["pick"]]] = p["season_points"] or 0.0
        for pos, pts in by_pos.items():
            for n in pts:
                near = [pts[k] for k in (n - 1, n, n + 1) if k in pts]
                samples[(pos, n)].append(mean(near))
    return {k: mean(v) for k, v in samples.items()}


def lookup(curve: dict, pos: str, n: int) -> float | None:
    if (pos, n) in curve:
        return curve[(pos, n)]
    lower = [k[1] for k in curve if k[0] == pos and k[1] < n]
    return curve[(pos, max(lower))] if lower else None


def grade_drafts(picks: list[dict], finished: dict[int, bool], fraction: dict[int, float],
                 kept: dict[tuple[int, str, str], float]) -> dict:
    """picks: draft_picks rows with 'manager'. finished: season -> bool. fraction: season -> share of the
    season's weeks already played (1.0 when finished). kept: (season, team_key, player_key) -> points scored
    while on that team. Returns {"picks": {(season, pick): {...}}, "teams": {team_key: {...}}}."""
    drafts: dict[int, list[dict]] = defaultdict(list)
    for p in picks:
        drafts[p["season"]].append(p)
    done = {s: d for s, d in drafts.items() if finished.get(s)}
    pick_out, team_out = {}, {}
    for season, ps in drafts.items():
        curve = expected_curve(done or {season: ps})
        ranks = positional_ranks(ps)
        scale = fraction.get(season, 1.0)
        voes = []
        for p in ps:
            pos = p["position"] or "?"
            exp = lookup(curve, pos, ranks[p["pick"]])
            exp = round((exp or 0.0) * scale, 2)
            pts = p["season_points"] or 0.0
            voe = round(pts - exp, 2)
            voes.append(voe)
            pick_out[(season, p["pick"])] = {"posRank": f"{pos}{ranks[p['pick']]}", "expected": exp, "voe": voe,
                                             "kept": round(kept.get((season, p["team_key"], p["player_key"]), 0), 2)}
        sd = pstdev(voes) if len(voes) > 1 else 1.0
        for p in ps:
            e = pick_out[(season, p["pick"])]
            z = e["voe"] / sd if sd else 0
            e["tier"] = ("steal" if z >= 1.0 else "good" if z >= 0.35 else "bust" if z <= -1.0
                         else "poor" if z <= -0.35 else "par")
        by_team: dict[str, list[dict]] = defaultdict(list)
        for p in ps:
            by_team[p["team_key"]].append(p)
        totals = {t: sum(pick_out[(season, p["pick"])]["voe"] for p in tp) for t, tp in by_team.items()}
        mu = mean(totals.values())
        tsd = pstdev(totals.values()) if len(totals) > 1 else 1.0
        for t, tp in by_team.items():
            z = (totals[t] - mu) / tsd if tsd else 0
            letter, gpa = grade_for(z)
            rated = sorted(tp, key=lambda p: -pick_out[(season, p["pick"])]["voe"])

            def brief(p):
                e = pick_out[(season, p["pick"])]
                return {"player": p["player_name"], "pos": p["position"], "pick": p["pick"], "round": p["round"],
                        "pts": round(p["season_points"] or 0, 2), "expected": e["expected"], "voe": e["voe"],
                        "posRank": e["posRank"]}
            team_out[t] = {"season": season, "manager": tp[0]["manager"], "grade": letter, "gpa": gpa,
                           "z": round(z, 2), "voe": round(totals[t], 2), "picks": len(tp),
                           "kept": round(sum(pick_out[(season, p["pick"])]["kept"] for p in tp), 2),
                           "points": round(sum(p["season_points"] or 0 for p in tp), 2),
                           "best": brief(rated[0]), "worst": brief(rated[-1]), "final": bool(finished.get(season))}
        for i, t in enumerate(sorted(by_team, key=lambda t: -totals[t])):
            team_out[t]["rank"] = i + 1
    return {"picks": pick_out, "teams": team_out}


def career_gpa(teams: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for t in teams.values():
        o = out.setdefault(t["manager"], {"gpas": [], "voe": 0.0, "grades": []})
        o["gpas"].append(t["gpa"])
        o["voe"] += t["voe"]
        o["grades"].append({"season": t["season"], "grade": t["grade"]})
    for o in out.values():
        o["gpa"] = round(mean(o["gpas"]), 2)
        o["voe"] = round(o["voe"], 2)
        o["grades"].sort(key=lambda g: g["season"])
        del o["gpas"]
    return out
