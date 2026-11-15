import json
from datetime import date

from museum import ingest, lineup, sample, stats


def test_optimal_lineup_uses_flex_and_ignores_ir():
    slots = ["QB", "RB", "WR", "W/R/T"]
    players = [
        {"primary_position": "QB", "selected_position": "QB", "points": 20},
        {"primary_position": "RB", "selected_position": "RB", "points": 5},
        {"primary_position": "RB", "selected_position": "BN", "points": 15},
        {"primary_position": "WR", "selected_position": "WR", "points": 10},
        {"primary_position": "TE", "selected_position": "W/R/T", "points": 2},
        {"primary_position": "WR", "selected_position": "IR", "points": 40},
    ]
    # QB 20 + RB 15 + WR 10 + FLEX (RB 5) = 50
    assert lineup.optimal_points(slots, players) == 50


def test_as_list_unwraps_yahoo_shapes():
    assert ingest.as_list([{"team": {"a": 1}}, {"team": {"a": 2}}], "team") == [{"a": 1}, {"a": 2}]
    assert ingest.as_list({"position": "WR"}, "position") == ["WR"]
    assert ingest.as_list({"a": 1}, "team") == [{"a": 1}]
    assert ingest.as_list(None, "team") == []


def test_sample_to_stats_end_to_end(tmp_path):
    db_path = sample.generate(tmp_path / "s.sqlite", today=date(2026, 11, 14))
    out = tmp_path / "out"
    stats.build(db_path, out)
    meta = json.loads((out / "meta.json").read_text())
    assert meta["isSample"] is True
    assert meta["latest"] == {"season": 2026, "week": 9}
    records = json.loads((out / "records.json").read_text())
    shame = json.loads((out / "shame.json").read_text())
    for cat in records + shame:
        assert cat["slug"] and cat["entries"], cat["slug"]
        assert all(e["caption"] for e in cat["entries"])
    managers = json.loads((out / "managers.json").read_text())
    ids = {m["id"] for m in managers}
    rivalries = json.loads((out / "rivalries.json").read_text())
    for key in rivalries:
        a, b = key.split("--")
        assert a < b and a in ids and b in ids
    champions = json.loads((out / "champions.json").read_text())
    assert len(champions["banners"]) == 12  # 2014-2025 finished


# --- web scraper parsers (synthetic HTML mirroring Yahoo's markup) ---

from museum import scrape  # noqa: E402

LID = "111"


def _row(left_player, left_pts, slot, right_player, right_pts):
    return (f"<tr><td></td><td>{left_player}</td><td></td><td>{left_pts}</td><td></td><td>{slot}</td>"
            f"<td></td><td>{right_pts}</td><td></td><td>{right_player}</td><td></td></tr>")


def _player(pid, name, pos, href=None):
    href = href or f"https://sports.yahoo.com/nfl/players/{pid}"
    return (f'<div class="ysf-player-name"><a class="name" href="{href}">{name}</a>'
            f'<span class="D-b">X - {pos}</span></div>')


def test_scrape_settings_playoffs_and_roster():
    html = """<table id="settings-table"><tr><td>Max Teams:</td><td>12</td></tr>
    <tr><td>Playoffs:</td><td>6 teams - Week 15, 16 and 17 (ends Jan 4)</td></tr>
    <tr><td>Roster Positions:</td><td>QB, WR, W/R/T, K, DEF, BN, BN, IR</td></tr></table>"""
    s = scrape.parse_settings(html)
    assert (s["num_teams"], s["num_playoff_teams"], s["playoff_start_week"], s["end_week"]) == (12, 6, 15, 17)
    assert s["roster_positions"] == ["QB", "WR", "W/R/T", "K", "DEF"]


def test_scrape_matchup_rows_defense_and_empty_slots():
    html = (f'<div id="matchup-header"><a href="/f1/{LID}/4">A</a><a href="/f1/{LID}/9">B</a></div>'
            '<table id="statTable0"><tbody>'
            + _row(_player(30123, "Joe QB", "QB"), "21.5", "QB", "(Empty)", "")
            + _row(_player(0, "Chicago Bears", "DEF", "https://sports.yahoo.com/nfl/teams/chi/"), "7", "DEF",
                   _player(100018, "Detroit Lions", "DEF"), "-2")
            + _row("", "", "TOTAL", "", "")
            + "</tbody></table>")
    teams, rosters = scrape.parse_matchup(html, LID)
    assert teams == [4, 9]
    assert [(p["player_key"], p["selected_position"], p["points"]) for p in rosters[4]] == [
        ("p.30123", "QB", 21.5), ("p.def.bears", "DEF", 7.0)]
    assert [(p["player_key"], p["position"], p["points"]) for p in rosters[9]] == [("p.def.lions", "DEF", -2.0)]


def test_scrape_trade_groups_infer_source_team():
    def side(dest, players):
        ps = "".join(f'<p><a href="https://sports.yahoo.com/nfl/players/{pid}">{n}</a>'
                     f'<span class="F-position">X - WR</span><span title="Injured">Q</span></p>' for pid, n in players)
        return (f'<td>{ps} Traded to</td><td><a class="Tst-team-name" href="/f1/{LID}/{dest}">T{dest}</a>'
                '<span class="F-timestamp">Nov 15, 3:12 am</span></td>')
    html = (f'<table class="Tst-transaction-table"><tr><td><span class="F-trade"></span></td>{side(2, [(1, "A"), (3, "C")])}</tr>'
            f'<tr><td></td>{side(5, [(2, "B")])}</tr></table>')
    txs, has_next = scrape.parse_transactions(html, LID)
    assert len(txs) == 1 and not has_next
    assert [(m["player_name"], m["source"], m["dest"]) for m in txs[0]["moves"]] == [
        ("A", 5, 2), ("C", 5, 2), ("B", 2, 5)]


def test_scrape_draft_overall_pick_and_timestamp_rollover():
    html = ('<table><tr><th>Round 2</th></tr><tr><td class="first">3.</td>'
            '<td class="player"><a href="https://sports.yahoo.com/nfl/players/123">P</a><span>(GB - RB)</span></td>'
            '<td class="last" title="Team X">Team X</td></tr></table>')
    [pick] = scrape.parse_draft(html, 12)
    assert (pick["round"], pick["pick"], pick["position"], pick["team_name"]) == (2, 15, "RB", "Team X")
    assert scrape.parse_timestamp("Jan 2, 1:05 pm", 2025).date() == date(2026, 1, 2)
    assert scrape.parse_timestamp("Sep 9, 12:30 am", 2025).hour == 0
