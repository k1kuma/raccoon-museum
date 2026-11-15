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
