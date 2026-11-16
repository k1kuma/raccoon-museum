"""Weekly awards, generated from each final week (regular season and winners' bracket; consolation excluded)."""
from __future__ import annotations

from collections import defaultdict

from .stats import f2, ordinal  # noqa: F401  (f2 used in captions)

AWARDS = [
    {"slug": "manager-of-the-week", "title": "Manager of the Week", "icon": "🏆",
     "blurb": "Highest score of the week."},
    {"slug": "benchwarmer", "title": "Benchwarmer of the Week", "icon": "🪑",
     "blurb": "Most points left on the bench (best possible lineup minus the one played)."},
    {"slug": "heartbreaker", "title": "Heartbreaker", "icon": "💔", "blurb": "Narrowest loss of the week."},
    {"slug": "lucky-win", "title": "Lucky Win", "icon": "🍀",
     "blurb": "Won despite scoring below the league median that week (lowest such winning score)."},
    {"slug": "blowout", "title": "Blowout of the Week", "icon": "💥", "blurb": "Biggest margin of victory."},
    {"slug": "dud", "title": "Dud of the Week", "icon": "🥔", "blurb": "Lowest score of the week."},
    {"slug": "one-man-show", "title": "One-Man Show", "icon": "🎤",
     "blurb": "The starter who scored the biggest share of his team's points."},
]
BY_SLUG = {a["slug"]: a for a in AWARDS}


def median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


def week_awards(m, rows: list[dict], bench: list[dict], carry: dict | None) -> list[dict]:
    """rows: one game row per team for a single final week. bench: bench_rows for that week."""
    if not rows:
        return []
    name = m.name
    s, w = rows[0]["season"], rows[0]["week"]
    out = []

    def add(slug, r, value, display, caption, **extra):
        out.append({"slug": slug, **{k: BY_SLUG[slug][k] for k in ("title", "icon")}, "manager": r["manager"],
                    "opponent": r.get("opponent"), "team": r.get("team_name"), "value": value, "display": display,
                    "caption": caption, **extra})

    top = max(rows, key=lambda r: r["points"])
    add("manager-of-the-week", top, top["points"], f"{f2(top['points'])} pts",
        f"{name(top['manager'])} put up the week's best score, {f2(top['points'])} points"
        + (f", {'beating' if top['result'] == 'W' else 'and still lost to'} {name(top['opponent'])}." if top['opponent']
           else "."))
    wk_bench = [b for b in bench if b["left"] > 0]
    if wk_bench:
        b = max(wk_bench, key=lambda b: b["left"])
        tail = " That would have flipped the result." if b["would_have_won"] else ""
        add("benchwarmer", b, b["left"], f"{f2(b['left'])} pts on bench",
            f"{name(b['manager'])} left {f2(b['left'])} points on the bench: the best possible lineup scored "
            f"{f2(b['optimal'])} against the {f2(b['actual'])} played.{tail}")
    losses = [r for r in rows if r["result"] == "L" and r["opponent"]]
    if losses:
        r = min(losses, key=lambda r: abs(r["margin"]))
        add("heartbreaker", r, abs(r["margin"]), f"lost by {f2(abs(r['margin']))}",
            f"{name(r['manager'])} lost to {name(r['opponent'])} by just {f2(abs(r['margin']))} points, "
            f"{f2(r['points'])} to {f2(r['opponent_points'])}.")
    med = median([r["points"] for r in rows])
    lucky = [r for r in rows if r["result"] == "W" and r["points"] < med]
    if lucky:
        r = min(lucky, key=lambda r: r["points"])
        add("lucky-win", r, r["points"], f"won with {f2(r['points'])}",
            f"{name(r['manager'])} scored {f2(r['points'])}, below the week's median of {f2(med)}, and still beat "
            f"{name(r['opponent'])} ({f2(r['opponent_points'])}).")
    wins = [r for r in rows if r["result"] == "W" and r["opponent"]]
    if wins:
        r = max(wins, key=lambda r: r["margin"])
        add("blowout", r, r["margin"], f"won by {f2(r['margin'])}",
            f"{name(r['manager'])} beat {name(r['opponent'])} by {f2(r['margin'])} points, "
            f"{f2(r['points'])} to {f2(r['opponent_points'])}.")
    low = min(rows, key=lambda r: r["points"])
    add("dud", low, low["points"], f"{f2(low['points'])} pts",
        f"{name(low['manager'])} managed only {f2(low['points'])} points, the week's lowest"
        + (f", and {'somehow won' if low['result'] == 'W' else 'lost'} against {name(low['opponent'])}."
           if low["opponent"] else "."))
    if carry:
        r = next(x for x in rows if x["team_key"] == carry["team_key"])
        pct = round(100 * carry["share"])
        add("one-man-show", r, round(carry["share"], 4), f"{pct}% of points",
            f"{carry['player']} ({carry['pos']}) scored {f2(carry['pts'])} of {name(carry['manager'])}'s "
            f"{f2(carry['total'])} starting points ({pct}%)"
            + (" — and it was enough to win." if carry["result"] == "W" else "."), player=carry["player"])
    for a in out:
        a["season"], a["week"] = s, w
    return out


def build_fun(m, bench_rows: list[dict]) -> dict:
    counted = {}
    for r in m.games("all"):
        st = m.seasons.get(r["season"], {})
        lc = st.get("last_completed_week")
        if not st.get("is_finished") and lc and r["week"] > lc:
            continue
        counted.setdefault((r["season"], r["week"]), []).append(r)
    bench_by = defaultdict(list)
    for b in bench_rows:
        if b["game_type"] != "consolation":
            bench_by[(b["season"], b["week"])].append(b)
    weeks = []
    for key in sorted(counted):
        rows = counted[key]
        awards = week_awards(m, rows, bench_by.get(key, []), m.week_carry(*key))
        label = m.week_label(rows[0]) if rows[0]["game_type"] != "regular" else f"Week {key[1]}, {key[0]}"
        label = label.replace(f" ({rows[0].get('round_label')})", f" · {m.round_name(*key)}") \
            if rows[0]["game_type"] != "regular" else label
        weeks.append({"season": key[0], "week": key[1], "id": f"{key[0]}-{key[1]}", "label": label,
                      "segment": "playoffs" if rows[0]["game_type"] == "playoff" else "regular",
                      "isNew": m.is_new(*key), "awards": awards})
    cabinet: dict[str, dict] = defaultdict(lambda: defaultdict(int))
    history: dict[str, list] = defaultdict(list)
    for wk in weeks:
        for a in wk["awards"]:
            cabinet[a["manager"]][a["slug"]] += 1
            history[a["slug"]].append({k: a[k] for k in ("season", "week", "manager", "opponent", "display",
                                                          "caption", "value")} | {"weekId": wk["id"]})
    leaders = {}
    for slug, hist in history.items():
        tally = defaultdict(int)
        for h in hist:
            tally[h["manager"]] += 1
        leaders[slug] = sorted(({"manager": k, "count": v} for k, v in tally.items()), key=lambda x: -x["count"])
    return {"awards": AWARDS, "weeks": list(reversed(weeks)),
            "latest": weeks[-1]["id"] if weeks else None,
            "cabinet": {k: dict(v) for k, v in cabinet.items()},
            "history": {k: list(reversed(v)) for k, v in history.items()}, "leaders": leaders}
