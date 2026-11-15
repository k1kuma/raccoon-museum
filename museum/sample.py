"""Deterministic, realistic sample league so the site can be built before Yahoo credentials exist.

The current season advances with the real calendar, so the weekly pipeline visibly moves forward.
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from . import db

FIRST_SEASON = 2014
NUM_TEAMS = 12
REG_WEEKS = 14
END_WEEK = 17
PLAYOFF_START = 15
STARTERS = ["QB", "WR", "WR", "RB", "RB", "TE", "W/R/T", "K", "DEF"]
BENCH = 6
POS_TALENT = {"QB": (17, 4.5), "RB": (10, 4.5), "WR": (10, 4.0), "TE": (7, 3.0), "K": (8, 1.5), "DEF": (7, 2.5)}
POS_SD = {"QB": 7, "RB": 7, "WR": 7, "TE": 5, "K": 4, "DEF": 5}
POS_COUNTS = {"QB": 36, "RB": 80, "WR": 95, "TE": 36, "K": 32, "DEF": 32}
DRAFT_CAPS = {"QB": 2, "RB": 6, "WR": 6, "TE": 2, "K": 1, "DEF": 1}

MANAGERS = [
    ("G-RACCOON-01", "Rocky"), ("G-RACCOON-02", "Bandit"), ("G-RACCOON-03", "Mags"),
    ("G-RACCOON-04", "Dumpster Dave"), ("G-RACCOON-05", "Trash Queen"), ("G-RACCOON-06", "Ringo"),
    ("G-RACCOON-07", "Sly"), ("G-RACCOON-08", "--hidden--"), ("G-RACCOON-09", "Moonpie"),
    ("G-RACCOON-10", "Coach Klepto"), ("G-RACCOON-11", "Tex"), ("G-RACCOON-12", "Pickles"),
    ("G-RACCOON-13", "Rookie Ricky"), ("G-RACCOON-14", "Big Lou"),
]
TEAM_WORDS_A = ["Midnight", "Alley", "Dumpster", "Garbage Day", "Masked", "Back Alley", "Curbside", "Rooftop",
                "Downtown", "Lid Lifting", "Compost", "Streetlight", "Recycling", "Uptown", "Bin Night", "Porch"]
TEAM_WORDS_B = ["Bandits", "Scavengers", "Divers", "Gremlins", "Marauders", "Rascals", "Raiders", "Burglars",
                "Snackers", "Prowlers", "Trash Pandas", "Hustlers", "Ringtails", "Moochers", "Lid Poppers"]
FIRST = ["Jaylen", "Marcus", "Tyler", "Derrick", "Cam", "Bo", "Trey", "Malik", "Austin", "Devin", "Chris", "Jordan",
         "Brandon", "Kenny", "Isaiah", "Darius", "Josh", "Zach", "Tre", "Nate", "Rashad", "Cole", "Drew", "Eli",
         "Myles", "Quincy", "Rex", "Sammy", "Terrell", "Vince", "Wes", "Xavier", "Brock", "Dak", "Jalen", "Kyler"]
LAST = ["Washington", "Brooks", "Carter", "Mitchell", "Hayes", "Coleman", "Barnes", "Fields", "Grant", "Holloway",
        "Ingram", "Jennings", "Kelce", "Lockett", "Mayfield", "Norwood", "Owens", "Prescott", "Quarles", "Ridley",
        "Sanders", "Thielen", "Underwood", "Vaughn", "Waller", "Young", "Zeller", "Becton", "Chubb", "Diggs",
        "Ekeler", "Fournette", "Godwin", "Henry", "Irving", "Jefferson", "Kamara", "Lamb", "McCaffrey", "Nacua"]
CITIES = ["Gotham", "Metro City", "Bayport", "Riverton", "Ironwood", "Port Lake", "Summit", "Harbor City",
          "Capital", "Lakeside", "Northfield", "Pine Valley", "Redrock", "Seaside", "Twin Peaks", "Westbrook",
          "Ashford", "Brookhaven", "Cedar Falls", "Dunmore", "Elmwood", "Fairview", "Glenwood", "Hillcrest",
          "Kingsport", "Lynnfield", "Maplewood", "Oakdale", "Parkview", "Queensbury", "Rosedale", "Stonebridge"]


def week1_start(season: int) -> date:
    """Tuesday after Labor Day (first Monday of September)."""
    d = date(season, 9, 1)
    while d.weekday() != 0:
        d += timedelta(days=1)
    return d + timedelta(days=1)


def last_completed_week(season: int, today: date) -> int:
    start = week1_start(season)
    done = 0
    for w in range(1, END_WEEK + 1):
        if start + timedelta(days=7 * (w - 1) + 6) < today:
            done = w
    return done


class Player:
    __slots__ = ("pid", "name", "pos", "talent")

    def __init__(self, pid: int, name: str, pos: str, talent: float):
        self.pid, self.name, self.pos, self.talent = pid, name, pos, talent


def make_pool(rng: random.Random) -> list[Player]:
    pool, pid, used = [], 1000, set()
    for pos, count in POS_COUNTS.items():
        mean, spread = POS_TALENT[pos]
        for i in range(count):
            if pos == "DEF":
                name = f"{CITIES[i % len(CITIES)]} Defense"
            else:
                while True:
                    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
                    if name not in used:
                        used.add(name)
                        break
            # A few true studs per position, long tail of replacement-level guys.
            talent = max(1.0, rng.gauss(mean, spread) + (spread * 1.6 if i < 4 else 0))
            pool.append(Player(pid, name, pos, talent))
            pid += 1
    return pool


def round_robin(team_ids: list[int], weeks: int, rng: random.Random) -> list[list[tuple[int, int]]]:
    ids = team_ids[:]
    rng.shuffle(ids)
    rounds = []
    n = len(ids)
    for _ in range(n - 1):
        rounds.append([(ids[i], ids[n - 1 - i]) for i in range(n // 2)])
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    sched = []
    while len(sched) < weeks:
        sched.extend(rounds)
    return sched[:weeks]


def generate(db_path: Path, today: date | None = None, first_season: int = FIRST_SEASON) -> Path:
    today = today or datetime.now(timezone.utc).date()
    if db_path.exists():
        db_path.unlink()
    conn = db.connect(db_path)
    current = today.year if today >= week1_start(today.year) else today.year - 1
    for season in range(first_season, current + 1):
        _season(conn, season, min(END_WEEK, last_completed_week(season, today)))
    db.set_meta(conn, "is_sample", "1")
    db.set_meta(conn, "generated_at", datetime.now(timezone.utc).isoformat())
    conn.commit()
    conn.close()
    return db_path


def _season(conn, season: int, visible_weeks: int) -> None:
    rng = random.Random(f"raccoons-{season}")
    game_id = 300 + season - 2000
    league_key = f"{game_id}.l.{70000 + season}"
    pkey = lambda p: f"{game_id}.p.{p.pid}"  # noqa: E731
    tkey = lambda t: f"{league_key}.t.{t}"  # noqa: E731

    # Manager churn: #13 replaces #8 from 2018, #14 replaces #12 from 2021.
    roster_mgrs = list(MANAGERS[:12])
    if season >= 2018:
        roster_mgrs[7] = MANAGERS[12]
    if season >= 2021:
        roster_mgrs[11] = MANAGERS[13]
    rng.shuffle(roster_mgrs)
    team_ids = list(range(1, NUM_TEAMS + 1))
    names = set()
    teams = {}
    for tid, (guid, nick) in zip(team_ids, roster_mgrs):
        while True:
            name = f"{rng.choice(TEAM_WORDS_A)} {rng.choice(TEAM_WORDS_B)}"
            if name not in names:
                names.add(name)
                break
        # Each manager has a persistent "skill" that nudges their drafting.
        skill = (int(guid[-2:]) * 37 % 11) / 10.0
        teams[tid] = {"guid": guid, "nick": nick, "name": name, "skill": skill, "roster": []}

    pool = make_pool(rng)
    by_pid = {p.pid: p for p in pool}

    # Snake draft.
    order = team_ids[:]
    rng.shuffle(order)
    available = sorted(pool, key=lambda p: -(p.talent + rng.gauss(0, 2.2)))
    picks = []
    rounds = STARTERS.__len__() + BENCH
    pick_no = 0
    for rnd in range(1, rounds + 1):
        seq = order if rnd % 2 == 1 else order[::-1]
        for tid in seq:
            team = teams[tid]
            counts = {}
            for pid in team["roster"]:
                counts[by_pid[pid].pos] = counts.get(by_pid[pid].pos, 0) + 1
            late = rnd >= rounds - 2
            choice = None
            for p in available[: 25]:
                if counts.get(p.pos, 0) >= DRAFT_CAPS[p.pos]:
                    continue
                if p.pos in ("K", "DEF") and not late:
                    continue
                if rng.random() < 0.25 - team["skill"] * 0.15:
                    continue  # a reach / panic pick
                choice = p
                break
            if choice is None:
                choice = next(p for p in available if counts.get(p.pos, 0) < DRAFT_CAPS[p.pos])
            available.remove(choice)
            team["roster"].append(choice.pid)
            pick_no += 1
            picks.append((pick_no, rnd, tid, choice))

    # Weekly player scoring for the whole pool.
    scores: dict[int, dict[int, float]] = {}
    for w in range(1, END_WEEK + 1):
        bye = set(rng.sample(range(len(pool)), len(pool) // 10)) if 5 <= w <= 13 else set()
        scores[w] = {}
        for i, p in enumerate(pool):
            if i in bye:
                scores[w][p.pid] = 0.0
            else:
                boom = rng.random() < 0.06
                pts = rng.gauss(p.talent * (1.8 if boom else 1.0), POS_SD[p.pos])
                scores[w][p.pid] = round(max(-2.0 if p.pos == "DEF" else 0.0, pts), 2)

    schedule = round_robin(team_ids, REG_WEEKS, rng)
    w1 = week1_start(season)
    results: dict[int, list[tuple]] = {}
    standings = {tid: {"w": 0, "l": 0, "t": 0, "pf": 0.0, "pa": 0.0} for tid in team_ids}
    tx_counter = 0
    transactions = []
    team_points: dict[tuple[int, int], float] = {}

    def set_lineup(tid: int, w: int) -> list[tuple[Player, str]]:
        roster = [by_pid[pid] for pid in teams[tid]["roster"]]
        est = {p.pid: (p.talent + rng.gauss(0, 2.0)) if scores[w][p.pid] > 0 else -5 for p in roster}
        roster.sort(key=lambda p: -est[p.pid])
        used, lineup = set(), []
        for slot in sorted(STARTERS, key=lambda s: 3 if "/" in s else 1):
            allowed = {"WR", "RB", "TE"} if slot == "W/R/T" else {slot}
            for p in roster:
                if p.pid not in used and p.pos in allowed:
                    used.add(p.pid)
                    lineup.append((p, slot))
                    break
        lineup.extend((p, "BN") for p in roster if p.pid not in used)
        return lineup

    def play_week(w: int, pairs: list[tuple[int, int]], playoffs: dict[int, bool]) -> None:
        lineups = {tid: set_lineup(tid, w) for pair in pairs for tid in pair}
        for tid, lineup in lineups.items():
            team_points[(w, tid)] = round(sum(scores[w][p.pid] for p, s in lineup if s != "BN"), 2)
        results[w] = []
        for idx, (a, b) in enumerate(pairs):
            results[w].append((idx, a, b, lineups[a], lineups[b], playoffs.get(a, False), playoffs.get(b, False)))

    def waiver_moves(w: int) -> None:
        nonlocal tx_counter
        rostered = {pid for t in teams.values() for pid in t["roster"]}
        free = [p for p in pool if p.pid not in rostered]
        for tid in team_ids:
            if rng.random() < 0.45:
                roster = [by_pid[pid] for pid in teams[tid]["roster"]]
                recent = {p.pid: sum(scores[x][p.pid] for x in range(max(1, w - 2), w + 1)) for p in roster}
                drop = min(roster, key=lambda p: recent[p.pid] + p.talent * 0.5)
                cands = [p for p in free if p.pos == drop.pos]
                if not cands:
                    continue
                add = max(cands[:40], key=lambda p: sum(scores[x][p.pid] for x in range(max(1, w - 2), w + 1)))
                free.remove(add)
                teams[tid]["roster"].remove(drop.pid)
                teams[tid]["roster"].append(add.pid)
                tx_counter += 1
                transactions.append((f"{league_key}.tr.{tx_counter}", "add/drop", w + 1,
                                     [(add, "add", None, tid), (drop, "drop", tid, None)]))
        if REG_WEEKS - 4 >= w >= 2 and rng.random() < 0.35:
            a, b = rng.sample(team_ids, 2)
            pa = rng.choice(teams[a]["roster"])
            pb = rng.choice([pid for pid in teams[b]["roster"] if by_pid[pid].pos not in ("K", "DEF")] or teams[b]["roster"])
            teams[a]["roster"].remove(pa)
            teams[b]["roster"].remove(pb)
            teams[a]["roster"].append(pb)
            teams[b]["roster"].append(pa)
            tx_counter += 1
            transactions.append((f"{league_key}.tr.{tx_counter}", "trade", w + 1,
                                 [(by_pid[pa], "trade", a, b), (by_pid[pb], "trade", b, a)]))

    for w in range(1, REG_WEEKS + 1):
        play_week(w, schedule[w - 1], {})
        for idx, a, b, *_ in results[w]:
            pa, pb = team_points[(w, a)], team_points[(w, b)]
            if w <= visible_weeks:
                for me, them, mp, tp in ((a, b, pa, pb), (b, a, pb, pa)):
                    s = standings[me]
                    s["pf"] += mp
                    s["pa"] += tp
                    s["w" if mp > tp else "l" if mp < tp else "t"] += 1
        waiver_moves(w)

    # Full-season standings decide the bracket (it is only shown once those weeks are visible).
    full = {tid: {"w": 0, "pf": 0.0} for tid in team_ids}
    for w in range(1, REG_WEEKS + 1):
        for idx, a, b, *_ in results[w]:
            pa, pb = team_points[(w, a)], team_points[(w, b)]
            full[a]["pf"] += pa
            full[b]["pf"] += pb
            full[a if pa > pb else b]["w"] += 1
    seeds = sorted(team_ids, key=lambda t: (-full[t]["w"], -full[t]["pf"]))
    seed_of = {tid: i + 1 for i, tid in enumerate(seeds)}

    def winner(w, a, b):
        return a if team_points[(w, a)] >= team_points[(w, b)] else b

    s = seeds
    # Week 15: 3v6, 4v5 (1, 2 on bye); consolation for 7-12.
    play_week(15, [(s[2], s[5]), (s[3], s[4]), (s[6], s[11]), (s[7], s[10]), (s[8], s[9])],
              {t: True for t in s[2:6]})
    qf_w = [winner(15, s[2], s[5]), winner(15, s[3], s[4])]
    qf_l = [t for t in s[2:6] if t not in qf_w]
    semis = [(s[0], max(qf_w, key=lambda t: seed_of[t])), (s[1], min(qf_w, key=lambda t: seed_of[t]))]
    play_week(16, semis + [(qf_l[0], qf_l[1]), (s[6], s[7]), (s[8], s[9]), (s[10], s[11])],
              {t: True for pair in semis for t in pair})
    finalists = [winner(16, a, b) for a, b in semis]
    semi_losers = [a if winner(16, a, b) == b else b for a, b in semis]
    play_week(17, [tuple(finalists), tuple(semi_losers), (s[6], s[7]), (s[8], s[9]), (s[10], s[11]),
                   tuple(qf_l)], {t: True for t in finalists + semi_losers})
    champ = winner(17, *finalists)
    third = winner(17, *semi_losers)
    final_order = [champ, finalists[0] if finalists[1] == champ else finalists[1], third,
                   semi_losers[0] if semi_losers[1] == third else semi_losers[1]]
    final_order += sorted(qf_l, key=lambda t: seed_of[t]) + s[6:]

    finished = visible_weeks >= END_WEEK
    current_rank = sorted(team_ids, key=lambda t: (-standings[t]["w"], -standings[t]["pf"]))

    db.upsert(conn, "seasons", {
        "season": season, "league_key": league_key, "league_id": str(70000 + season), "game_id": str(game_id),
        "name": "City Slickin' Raccoons", "num_teams": NUM_TEAMS, "start_week": 1, "end_week": END_WEEK,
        "playoff_start_week": PLAYOFF_START, "num_playoff_teams": 6,
        "current_week": min(END_WEEK, visible_weeks + 1), "last_completed_week": visible_weeks,
        "is_finished": int(finished), "roster_positions": json.dumps(STARTERS),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    })
    for tid, t in teams.items():
        st = standings[tid]
        rank = final_order.index(tid) + 1 if finished else current_rank.index(tid) + 1
        db.upsert(conn, "teams", {
            "team_key": tkey(tid), "season": season, "team_id": tid, "name": t["name"],
            "manager_guid": t["guid"], "manager_nickname": t["nick"], "logo_url": None,
            "final_rank": rank, "playoff_seed": seed_of[tid] if visible_weeks >= REG_WEEKS else None,
            "wins": st["w"], "losses": st["l"], "ties": st["t"],
            "points_for": round(st["pf"], 2), "points_against": round(st["pa"], 2),
            "moves": sum(1 for tx in transactions if tx[2] <= visible_weeks and any(x[3] == tid for x in tx[3])),
            "trades": sum(1 for tx in transactions if tx[1] == "trade" and tx[2] <= visible_weeks
                          and any(tid in (x[2], x[3]) for x in tx[3])),
            "draft_grade": rng.choice(["A", "A-", "B+", "B", "B-", "C+", "C", "D"]),
        })

    for w, games in results.items():
        if w > visible_weeks:
            continue
        ws = (w1 + timedelta(days=7 * (w - 1))).isoformat()
        for idx, a, b, la, lb, pa_flag, pb_flag in games:
            for me, them, lineup, playoff in ((a, b, la, pa_flag), (b, a, lb, pb_flag)):
                mp, tp = team_points[(w, me)], team_points[(w, them)]
                proj = round(sum(p.talent for p, s_ in lineup if s_ != "BN") + rng.gauss(0, 6), 2)
                db.upsert(conn, "matchups", {
                    "season": season, "week": w, "matchup_idx": idx, "team_key": tkey(me),
                    "opponent_key": tkey(them), "points": mp, "opponent_points": tp, "projected": proj,
                    "is_playoffs": int(playoff), "is_consolation": int(w >= PLAYOFF_START and not playoff),
                    "result": "W" if mp > tp else "L" if mp < tp else "T", "week_start": ws,
                })
                for p, slot in lineup:
                    db.upsert(conn, "roster_slots", {
                        "season": season, "week": w, "team_key": tkey(me), "player_key": pkey(p),
                        "player_name": p.name, "eligible_positions": p.pos, "primary_position": p.pos,
                        "selected_position": slot, "points": scores[w][p.pid],
                    })

    for pick_no, rnd, tid, p in picks:
        season_pts = round(sum(scores[w][p.pid] for w in range(1, min(visible_weeks, END_WEEK) + 1)), 2)
        db.upsert(conn, "draft_picks", {
            "season": season, "pick": pick_no, "round": rnd, "team_key": tkey(tid), "player_key": pkey(p),
            "player_name": p.name, "position": p.pos, "season_points": season_pts, "cost": None,
        })

    for key, ttype, week, moves in transactions:
        if week > visible_weeks:
            continue
        ts = int(datetime.combine(w1 + timedelta(days=7 * (week - 1) + 1), datetime.min.time(),
                                  tzinfo=timezone.utc).timestamp())
        db.upsert(conn, "transactions", {"transaction_key": key, "season": season, "type": ttype,
                                         "status": "successful", "timestamp": ts, "week": week})
        for p, action, src, dst in moves:
            db.upsert(conn, "transaction_players", {
                "transaction_key": key, "player_key": pkey(p), "player_name": p.name, "position": p.pos,
                "action": action, "source_team_key": tkey(src) if src else None,
                "destination_team_key": tkey(dst) if dst else None,
            })
