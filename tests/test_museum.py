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


def test_game_type_comes_from_settings_and_seeds_not_yahoo_flags(tmp_path):
    import sqlite3
    db_path = sample.generate(tmp_path / "s.sqlite", today=date(2026, 11, 14))
    stats.build(db_path, tmp_path / "before")
    with sqlite3.connect(db_path) as conn:  # Yahoo flags every game from the playoff start week as a playoff game
        assert conn.execute("""UPDATE matchups SET is_playoffs = 1, is_consolation = 0 WHERE week >= (
            SELECT playoff_start_week FROM seasons s WHERE s.season = matchups.season)""").rowcount
    stats.build(db_path, tmp_path / "after")
    load = lambda d, f: json.loads((tmp_path / d / f).read_text())  # noqa: E731
    assert load("after", "managers.json") == load("before", "managers.json")
    shame = {c["slug"]: c for c in load("after", "shame.json")}
    assert shame["the-basement"] == {c["slug"]: c for c in load("before", "shame.json")}["the-basement"]
    assert shame["toilet-bowl"]["entries"] and shame["toilet-bowl"]["segment"] == "consolation"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    museum = stats.Museum(conn, stats.load_config(stats.config.DATA_DIR / "sample_managers.yaml"))
    for r in museum.rows:
        s = museum.seasons[r["season"]]
        seeds = museum.teams[r["team_key"]]["playoff_seed"], museum.teams[r["opponent_key"]]["playoff_seed"]
        if r["week"] < s["playoff_start_week"]:
            assert r["game_type"] == "regular"
        elif all(seeds):
            expect = "playoff" if max(seeds) <= s["num_playoff_teams"] else "consolation"
            assert r["game_type"] == expect, (r["season"], r["week"], seeds)
    finished = [r for r in museum.games("playoffs") if museum.finished(r["season"])]
    assert finished and all(r["round_label"] for r in finished)
    assert {r["round_label"] for r in finished} >= {"Championship", "Semifinal", "3rd place"}
    records = {c["slug"]: c for c in load("after", "records.json")}
    playoff_weeks = {(r["season"], r["week"], r["manager"]) for r in museum.games("playoffs")}
    for e in records["highest-scores"]["segments"]["playoffs"]:
        assert (e["season"], e["week"], e["manager"]) in playoff_weeks
    for e in records["highest-scores"]["entries"]:
        assert e["week"] < museum.seasons[e["season"]]["playoff_start_week"]


def test_all_play_and_rivalry_split(tmp_path):
    db_path = sample.generate(tmp_path / "s.sqlite", today=date(2026, 11, 14))
    stats.build(db_path, tmp_path)
    managers = json.loads((tmp_path / "managers.json").read_text())
    for m in managers:
        c = m["career"]
        ap = c["allPlay"]
        if ap["pct"] is not None:
            assert 0 <= ap["pct"] <= 1
        assert c["playoffApps"] >= c["playoffByes"]
    rivalries = json.loads((tmp_path / "rivalries.json").read_text())
    for r in rivalries.values():
        assert r["meetings"] + r["playoff"]["meetings"] == r["totalMeetings"] == len(r["games"])
        assert r["aWins"] + r["bWins"] + r["ties"] == r["meetings"]


def test_week_marks_and_record_vs_median_and_average():
    # Even count: median is the mean of the middle two; skewed scores pull the average away from it.
    assert stats.week_marks([100, 90, 80, 70]) == (85, 85)
    assert stats.week_marks([150, 90, 80, 70]) == (85, 97.5)
    assert stats.week_marks([90, 80, 70]) == (80, 80)
    assert [stats.vs_mark(p, 85) for p in (100, 85, 70)] == ["W", "T", "L"]


def test_carry_share_concentration():
    c = stats.carry_share({"a": 60, "b": 20, "c": 10, "d": 10, "e": -4})
    assert c["total"] == 100  # negative scores count as zero, not as negative share
    assert c["top1"] == 0.6 and c["top3"] == 0.9
    assert c["hhi"] == round(0.36 + 0.04 + 0.01 + 0.01, 4)
    even = stats.carry_share({k: 10 for k in "abcde"})
    assert even["top1"] == 0.2 and even["effective"] == 5
    assert stats.carry_share({})["total"] == 0


def test_benchmarks_and_carry_use_only_final_regular_weeks(tmp_path):
    db_path = sample.generate(tmp_path / "s.sqlite", today=date(2026, 11, 14))
    stats.build(db_path, tmp_path)
    managers = json.loads((tmp_path / "managers.json").read_text())
    archives = {a["season"]: a for a in json.loads((tmp_path / "archives.json").read_text())}
    for m in managers:
        for s in m["seasons"]:
            if not s["trend"]:
                continue
            weeks = s["trend"]["weeks"]
            last = archives[s["season"]]["lastCompletedWeek"]
            assert all(w["week"] <= last for w in weeks)
            med = s["vsMedian"].split("-")
            assert int(med[0]) == sum(w["pts"] > w["med"] for w in weeks) == s["trend"]["aboveMedian"]
            # True record = head-to-head + median games, i.e. two games per week.
            assert sum(map(int, s["trueRecord"].split("-"))) == 2 * len(weeks)
            c = s["carry"]
            assert 0 < c["top1"] <= c["top3"] <= 1
            assert abs(sum(p["share"] for p in c["players"]) + c["other"]["share"] - 1) < 0.01
    lobby = json.loads((tmp_path / "lobby.json").read_text())
    assert any(h["kicker"] == "Carry job" for h in lobby["headlines"])


def test_live_week_shows_in_lobby_but_never_touches_records(tmp_path):
    import sqlite3
    from datetime import datetime, timezone
    db_path = sample.generate(tmp_path / "s.sqlite", today=date(2026, 11, 14))
    with sqlite3.connect(db_path) as conn:
        pairs = conn.execute("SELECT team_key, opponent_key FROM matchups WHERE season=2026 AND week=9 "
                             "AND team_key < opponent_key").fetchall()
    live = {"season": 2026, "week": 10, "final": False,
            "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "games": [{"label": "", "teams": [{"teamKey": a, "points": 999.0, "projected": 1000.0},
                                              {"teamKey": b, "points": 1.0, "projected": 90.0}]} for a, b in pairs]}
    live_path = tmp_path / "live.json"
    live_path.write_text(json.dumps(live))
    stats.build(db_path, tmp_path / "base", live_path=tmp_path / "missing.json")
    stats.build(db_path, tmp_path / "live", live_path=live_path)
    read = lambda d, f: json.loads((tmp_path / d / f).read_text())  # noqa: E731
    board = read("live", "lobby.json")["live"]
    assert board["week"] == 10 and not board["final"] and len(board["games"]) == len(pairs)
    assert board["games"][0]["leader"] == "a" and read("live", "meta.json")["liveUpdatedAt"]
    for f in ("records.json", "shame.json", "managers.json"):
        assert read("live", f) == read("base", f), f
    live["week"] = 9  # already final in the database -> ignored
    live_path.write_text(json.dumps(live))
    stats.build(db_path, tmp_path / "stale", live_path=live_path)
    assert read("stale", "lobby.json")["live"] is None

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
    <tr><td>Roster&nbsp;Positions:</td><td>QB, WR, W/R/T, K, DEF, BN, BN, IR</td></tr></table>"""
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


def test_clean_cookies_tolerates_pasted_quotes_and_prefix():
    assert scrape.clean_cookies("'A=1; B=2'\n") == "A=1; B=2"
    assert scrape.clean_cookies("Cookie: A=1; B=2") == "A=1; B=2"
    assert scrape.clean_cookies("-b 'A=1'") == "A=1"


def test_scrape_logos_only_from_team_links_or_single_team_rows():
    lid = "705220"
    html = f"""<table>
    <tr><td><a href="/f1/{lid}/3"><img src="https://yahoofantasysports-res.cloudinary.com/image/upload/t_s32sq/fantasy-logos/a.png"></a>
      <a href="/f1/{lid}/3">Roomba Raiders</a></td></tr>
    <tr><td><img src="//s.yimg.com/cv/apiv2/default/nfl/nfl_4.png"></td><td><a href="/f1/{lid}/4">Dyson</a></td></tr>
    <tr><td><a href="/f1/{lid}/players/123"><img src="https://s.yimg.com/iu/headshot.png"></a></td></tr>
    </table>
    <ul><li><img src="https://s.yimg.com/x/y.png"><a href="/f1/{lid}/5">A</a><a href="/f1/{lid}/6">B</a></li></ul>"""
    logos = scrape.parse_logos(html, lid)
    assert logos == {3: "https://yahoofantasysports-res.cloudinary.com/image/upload/t_s32sq/fantasy-logos/a.png",
                     4: "https://s.yimg.com/cv/apiv2/default/nfl/nfl_4.png"}
    assert scrape.is_default_logo(logos[4]) and not scrape.is_default_logo(logos[3])


def test_logos_latest_by_default_and_per_season(tmp_path, monkeypatch):
    import sqlite3
    db_path = sample.generate(tmp_path / "s.sqlite", today=date(2026, 11, 14))
    with sqlite3.connect(db_path) as conn:
        key = conn.execute("SELECT team_key FROM teams WHERE season=2026 ORDER BY team_id LIMIT 1").fetchone()[0]
        tid = int(key.rsplit(".", 1)[1])
        guid = conn.execute("SELECT manager_guid FROM teams WHERE team_key=?", (key,)).fetchone()[0]
        old = conn.execute("SELECT season, team_id FROM teams WHERE manager_guid=? AND season=2025", (guid,)).fetchone()
        conn.execute("UPDATE teams SET logo_url='https://x/new.png' WHERE team_key=?", (key,))
        conn.execute("UPDATE teams SET logo_url='https://x/old.png' WHERE season=2025 AND team_id=?", (old[1],))
        conn.execute("UPDATE teams SET logo_url='https://s.yimg.com/default/nfl.png' WHERE season=2026 AND team_id!=?",
                     (tid,))
    logos = tmp_path / "logos"
    for season, team, url in ((2026, tid, "https://x/new.png"), (2025, old[1], "https://x/old.png")):
        (logos / str(season)).mkdir(parents=True, exist_ok=True)
        (logos / str(season) / f"{scrape.logo_stem(team, url)}.png").write_bytes(b"png")
    monkeypatch.setattr(stats.config, "LOGOS_DIR", logos)
    stats.build(db_path, tmp_path / "out")
    meta = json.loads((tmp_path / "out" / "meta.json").read_text())
    with_logo = [m for m in meta["managers"] if m["logo"]]
    assert len(with_logo) == 1 and with_logo[0]["logo"].startswith(f"logos/2026/{tid}-")
    mid = with_logo[0]["id"]
    assert meta["logos"]["2025"][mid].startswith(f"logos/2025/{old[1]}-")
    assert len(meta["logos"]["2026"]) == 1  # default Yahoo avatars fall back to initials


def test_live_windows_follow_the_nfl_schedule():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from museum import nfl_schedule as ns

    et = ZoneInfo("America/New_York")
    at = lambda *a: datetime(*a, tzinfo=et)  # noqa: E731
    kick = lambda t: {"kickoff": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "name": ""}  # noqa: E731
    games = [
        kick(at(2026, 10, 25, 9, 30)),   # London, EDT
        kick(at(2026, 10, 25, 13, 0)), kick(at(2026, 10, 25, 16, 25)),
        kick(at(2026, 10, 25, 20, 20)),  # SNF runs past midnight
        kick(at(2026, 11, 1, 9, 30)),    # London on the day DST ends (EST)
        kick(at(2026, 11, 4, 20, 15)),   # Wednesday game
    ]
    sched = {"fetched_at": "2026-10-28T12:00:00Z", "from": "2026-10-23", "to": "2026-11-05", "games": games}
    spans = ns.windows(games)
    assert spans[0] == (at(2026, 10, 25, 9, 15), at(2026, 10, 26, 0, 20))  # all of Sunday merged
    assert spans[1][0] == at(2026, 11, 1, 9, 15) and spans[1][0].astimezone(timezone.utc).hour == 14
    live = lambda *a: ns.decide(sched, at(*a))[0]  # noqa: E731
    assert not live(2026, 10, 25, 9, 0) and live(2026, 10, 25, 9, 20)       # early London kickoff
    assert live(2026, 10, 26, 0, 10)                                          # past midnight ET
    assert live(2026, 10, 26, 0, 30) and not live(2026, 10, 26, 0, 40)       # one final refresh, then stop
    assert live(2026, 11, 1, 9, 20) and not live(2026, 11, 1, 9, 0)          # DST change doesn't shift it
    assert live(2026, 11, 4, 21, 0) and not live(2026, 11, 2, 21, 0)         # Wednesday yes, bye-less Monday no
    # Missing or stale schedule → fixed fallback windows (Sunday 1 PM is live, Wednesday isn't).
    assert ns.decide(None, at(2026, 11, 8, 14, 0))[0] and not ns.decide(None, at(2026, 11, 4, 21, 0))[0]
    stale = ns.decide(sched, at(2026, 11, 8, 14, 0))
    assert stale[0] and stale[1].startswith("WARNING")
