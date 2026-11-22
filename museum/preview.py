"""Build one featured matchup per week from scoreboard pairings and completed scores."""
from collections import defaultdict
from statistics import mean


def point_spread(a_scores, b_scores):
    """Return A's expected margin, rounded to a half point, from five completed games per side."""
    if len(a_scores) < 5 or len(b_scores) < 5:
        return None
    return round((mean(a_scores[-5:]) - mean(b_scores[-5:])) * 2) / 2


def matchup_previews(museum, live):
    fixtures = defaultdict(dict)
    results = defaultdict(list)
    for row in museum.rows:
        week = (row["season"], row["week"])
        results[week].append(row)
        if row["opponent_key"] and row["opponent_points"] is not None:
            pair = tuple(sorted((row["team_key"], row["opponent_key"])))
            scores = (row["points"], row["opponent_points"])
            fixtures[week][pair] = scores if pair[0] == row["team_key"] else scores[::-1]

    if live:
        week = (live["season"], live["week"])
        for game in live["games"]:
            if len(game["teams"]) != 2:
                continue
            sides = sorted(game["teams"], key=lambda team: team["teamKey"])
            fixtures[week].setdefault(tuple(team["teamKey"] for team in sides), None)

    recent_scores = defaultdict(list)
    meetings = defaultdict(int)
    previews = []
    for season, week in sorted(fixtures.keys() | results.keys()):
        options = []
        for pair, scores in fixtures[season, week].items():
            managers = tuple(museum.mgr(key) for key in pair)
            if not all(managers) or managers[0] == managers[1]:
                continue
            spread = point_spread(*(recent_scores[key] for key in pair))
            if spread is None:
                continue
            rivalry = tuple(sorted(managers))
            count = meetings[rivalry]
            options.append((count, pair, managers, spread, scores))

        if options:
            count, pair, managers, spread, scores = min(options, key=lambda option: (-option[0], option[1]))
            cover = None
            if scores is not None:
                adjusted_margin = round(scores[0] - scores[1] - spread, 2)
                cover = "push" if adjusted_margin == 0 else ("a" if adjusted_margin > 0 else "b")
            previews.append({
                "id": f"{season}-{week}-{'--'.join(managers)}",
                "season": season,
                "week": week,
                "teams": [
                    {"manager": manager, "name": museum.teams[key]["name"]}
                    for key, manager in zip(pair, managers)
                ],
                "meetings": count,
                "spread": spread,
                "scores": scores,
                "cover": cover,
            })

        for row in results[season, week]:
            recent_scores[row["team_key"]].append(row["points"])
        for pair, scores in fixtures[season, week].items():
            managers = [museum.mgr(key) for key in pair]
            if scores is not None and all(managers):
                meetings[tuple(sorted(managers))] += 1

    return previews
