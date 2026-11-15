"""Scrape league history from Yahoo's logged-in web pages into the same SQLite schema as `ingest`.

Fallback for when the Fantasy Sports API is unavailable (Yahoo now gates API access behind an approval
process). Authenticates with browser cookies from YAHOO_COOKIES. Parsers are pure functions over HTML so
they can be tested without network access. Never reads or stores manager e-mail addresses.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup

from . import config, db

log = logging.getLogger("museum.scrape")

BASE = "https://football.fantasysports.yahoo.com"
EASTERN = ZoneInfo("America/New_York")
# Pages of 25 players fetched per position for season totals/positions (covers every realistically drafted player).
POSITION_PAGES = {"QB": 2, "RB": 4, "WR": 5, "TE": 2, "K": 2, "DEF": 2}
MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                      "dec"], start=1)}


class ScrapeError(RuntimeError):
    pass


class CookiesExpired(ScrapeError):
    pass


# ---------------------------------------------------------------- helpers

def soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def text(el) -> str:
    return el.get_text(" ", strip=True) if el is not None else ""


def num(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = value.replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def team_id_from(href: str | None, lid: str) -> int | None:
    m = re.search(rf"/f1/{lid}/(\d+)/?(?:[?#]|$)", href or "")
    return int(m.group(1)) if m else None


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def player_ref(a) -> tuple[str, str] | None:
    """(player_key, name) for a Yahoo player link. Defenses are keyed by nickname because past seasons link
    them to team pages rather than player ids."""
    if a is None:
        return None
    name = a.get("title") or text(a)
    href = a.get("href") or ""
    pid = a.get("data-ys-playerid")
    m = re.search(r"/nfl/players/(\d+)", href)
    pid = pid or (m.group(1) if m else None)
    if pid and int(pid) < 100000:
        return f"p.{pid}", name
    if pid or "/nfl/teams/" in href:
        return f"p.def.{slug(name.split()[-1])}" if name else None, name
    return None


def position_from(label: str) -> str | None:
    """'GB - QB' / '(Det - RB)' / '- DEF' -> 'QB' / 'RB' / 'DEF'."""
    m = re.search(r"-\s*([A-Z][A-Z/,]*)\s*\)?\s*$", label or "")
    return m.group(1).split(",")[0] if m else None


def week1_start(season: int) -> date:
    """Yahoo NFL weeks run Tuesday–Monday; week 1 starts the day after Labor Day."""
    first = date(season, 9, 1)
    labor_day = first + timedelta(days=(0 - first.weekday()) % 7)
    return labor_day + timedelta(days=1)


def week_start(season: int, week: int) -> str:
    return (week1_start(season) + timedelta(days=7 * (week - 1))).isoformat()


def week_of(season: int, when: datetime, end_week: int) -> int:
    days = (when.date() - week1_start(season)).days
    return max(1, min(end_week, days // 7 + 1))


def parse_timestamp(label: str, season: int) -> datetime | None:
    """'Nov 15, 3:12 am' (Eastern, no year) -> aware datetime. Jan–Jul belong to the following calendar year."""
    m = re.search(r"([A-Za-z]{3})\w*\s+(\d{1,2}),\s*(\d{1,2}):(\d{2})\s*([ap]m)", label or "", re.I)
    if not m:
        return None
    month = MONTHS.get(m.group(1).lower())
    if not month:
        return None
    hour = int(m.group(3)) % 12 + (12 if m.group(5).lower() == "pm" else 0)
    year = season if month >= 8 else season + 1
    return datetime(year, month, int(m.group(2)), hour, int(m.group(4)), tzinfo=EASTERN)


def header_index(row, *names: str) -> dict[str, int]:
    cells = [text(c) for c in row.find_all(["th", "td"], recursive=False)]
    out = {}
    for name in names:
        for i, c in enumerate(cells):
            if c.lower().startswith(name.lower()):
                out[name] = i
                break
    return out


# ---------------------------------------------------------------- parsers

def parse_settings(html: str) -> dict:
    s = soup(html)
    raw = {}
    for tr in s.select("#settings-table tr"):
        cells = tr.find_all(["td", "th"], recursive=False)
        if len(cells) >= 2:
            raw[text(cells[0]).rstrip(":").strip()] = text(cells[1])
    out: dict = {"name": raw.get("League Name")}
    if m := re.search(r"(\d+)", raw.get("Max Teams", "")):
        out["num_teams"] = int(m.group(1))
    if m := re.search(r"(\d+)", raw.get("Start Scoring on", "")):
        out["start_week"] = int(m.group(1))
    playoffs = raw.get("Playoffs", "")
    if m := re.search(r"(\d+)\s+teams", playoffs):
        out["num_playoff_teams"] = int(m.group(1))
    if m := re.search(r"Weeks?\s+([\d,\sand]+)", playoffs):
        weeks = [int(w) for w in re.findall(r"\d+", m.group(1))]
        if weeks:
            out["playoff_start_week"], out["end_week"] = min(weeks), max(weeks)
    positions = [p.strip() for p in raw.get("Roster Positions", "").split(",") if p.strip()]
    out["roster_positions"] = [p for p in positions if p not in ("BN", "IR", "IR+", "NA")]
    return out


def parse_standings(html: str, lid: str) -> list[dict]:
    s = soup(html)
    table = s.find(id="standingstable")
    if table is None:
        return []
    rows = table.select("tr")
    idx = header_index(rows[0], "Rank", "Team", "W-L-T", "PF", "PA", "Moves")
    out = []
    for tr in rows[1:]:
        cells = tr.find_all("td", recursive=False)
        link = next((a for a in tr.select("a[href]") if team_id_from(a["href"], lid) and text(a)), None)
        if not link or len(cells) <= max(idx.values()):
            continue
        wlt = [int(x) for x in re.findall(r"\d+", text(cells[idx["W-L-T"]]))] + [0, 0, 0]
        rank = re.search(r"\d+", text(cells[idx["Rank"]]))
        out.append({
            "team_id": team_id_from(link["href"], lid), "name": text(link),
            "rank": int(rank.group()) if rank else None,
            "wins": wlt[0], "losses": wlt[1], "ties": wlt[2],
            "points_for": num(text(cells[idx["PF"]])) or 0.0, "points_against": num(text(cells[idx["PA"]])) or 0.0,
            "moves": int(num(text(cells[idx["Moves"]])) or 0) if "Moves" in idx else None,
        })
    return out


def parse_teams(html: str, lid: str) -> list[dict]:
    """Team names and managers from /teams. The e-mail column is deliberately ignored."""
    s = soup(html)
    for table in s.select("table"):
        rows = table.select("tr")
        if not rows:
            continue
        idx = header_index(rows[0], "Team Name", "Manager", "Moves", "Trades")
        if "Team Name" not in idx or "Manager" not in idx:
            continue
        out = []
        for tr in rows[1:]:
            cells = tr.find_all("td", recursive=False)
            if len(cells) <= idx["Manager"]:
                continue
            link = next((a for a in cells[idx["Team Name"]].select("a[href]")
                         if team_id_from(a["href"], lid) and text(a)), None)
            if not link:
                continue
            mcell = cells[idx["Manager"]]
            profile = mcell.select_one("a[href*='/profile/']")
            guid = profile["href"].rstrip("/").rsplit("/", 1)[-1] if profile else None
            nickname = text(profile) if profile else text(mcell.select_one(".user-id") or mcell)

            def count(col):
                return int(num(text(cells[idx[col]])) or 0) if col in idx and len(cells) > idx[col] else None

            out.append({"team_id": team_id_from(link["href"], lid), "name": text(link), "manager_guid": guid,
                        "manager_nickname": nickname or None, "moves": count("Moves"), "trades": count("Trades")})
        return out
    return []


def parse_bracket(html: str, lid: str) -> dict[int, int]:
    """Playoff seeds {team_id: seed} from the bracket shown on a finished season's league home page."""
    seeds = {}
    for game in soup(html).select("div.bracket"):
        for side in game.find_all("div", recursive=False):
            rank = side.select_one(".yfa-rank")
            tid = next((team_id_from(a["href"], lid) for a in side.select("a[href]") if team_id_from(a["href"], lid)),
                       None)
            if rank and tid and (seed := re.search(r"\d+", text(rank))):
                seeds[tid] = int(seed.group())
    return seeds


@dataclass
class Scoreboard:
    week: int | None
    final: bool
    games: list[dict] = field(default_factory=list)


def parse_scoreboard(html: str, lid: str) -> Scoreboard:
    s = soup(html)
    section = s.find(id="matchupweek")
    if section is None:
        return Scoreboard(None, False)
    header = text(section.select_one(".Bdrbot") or section)
    wk = re.search(r"Week\s+(\d+)", header)
    board = Scoreboard(int(wk.group(1)) if wk else None, "final" in header.lower())
    label = ""
    for el in section.find_all(["p", "li"]):
        if el.name == "p":
            label = text(el)
            continue
        if "Linkable" not in (el.get("class") or []):
            continue
        teams = []
        for a in el.select("a.F-link[href]"):
            tid = team_id_from(a["href"], lid)
            if tid and tid not in teams:
                teams.append(tid)
        scores = el.select("div.Fz-lg")
        if len(teams) != 2 or len(scores) != 2:
            continue
        projs = [sc.find_next_sibling("div") for sc in scores]
        board.games.append({
            "teams": teams, "points": [num(text(sc)) or 0.0 for sc in scores],
            "projected": [num(text(p)) if p else None for p in projs],
            "label": label, "url": el.get("data-target") or (el.select_one("a[href*='matchup']") or {}).get("href"),
        })
    return board


def _slot_player(td) -> dict | None:
    a = td.select_one("a.name")
    ref = player_ref(a)
    if not ref or not ref[0]:
        return None
    pos_label = td.select_one(".ysf-player-name span.D-b")
    return {"player_key": ref[0], "player_name": ref[1], "position": position_from(text(pos_label))}


def parse_matchup(html: str, lid: str) -> tuple[list[int], dict[int, list[dict]]]:
    """Team ids (left, right) and each side's roster rows with selected slot and points."""
    s = soup(html)
    header = s.find(id="matchup-header") or s
    teams: list[int] = []
    for a in header.select("a[href]"):
        tid = team_id_from(a["href"], lid)
        if tid and tid not in teams:
            teams.append(tid)
    teams = teams[:2]
    rosters: dict[int, list[dict]] = {t: [] for t in teams}
    if not teams:
        return teams, rosters
    sides = [(teams[0], 1, 3)] + ([(teams[1], 9, 7)] if len(teams) == 2 else [])  # (team, player col, points col)
    for table in s.select("table[id^=statTable]"):
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            if len(cells) < 11:
                continue
            slot = text(cells[5])
            if not slot or slot.upper() == "TOTAL":
                continue
            for tid, pcol, ptscol in sides:
                p = _slot_player(cells[pcol])
                if p:
                    rosters[tid].append({**p, "selected_position": slot, "points": num(text(cells[ptscol])) or 0.0})
    return teams, rosters


def parse_draft(html: str, num_teams: int) -> list[dict]:
    picks = []
    for table in soup(html).select("table"):
        head = table.select_one("th")
        rnd = re.search(r"Round\s+(\d+)", text(head))
        if not rnd:
            continue
        r = int(rnd.group(1))
        for tr in table.select("tr"):
            first = tr.select_one("td.first")
            ref = player_ref(tr.select_one("td.player a"))
            team = tr.select_one("td.last")
            n = re.search(r"\d+", text(first))
            if not (first and ref and team and n):
                continue
            picks.append({"round": r, "pick": (r - 1) * num_teams + int(n.group()), "player_key": ref[0],
                          "player_name": ref[1], "position": position_from(text(tr.select_one("td.player span"))),
                          "team_name": team.get("title") or text(team)})
    return picks


def parse_transactions(html: str, lid: str) -> tuple[list[dict], bool]:
    """Transactions on one page (newest first) and whether a next page exists."""
    s = soup(html)
    out: list[dict] = []
    trade: dict | None = None
    for tr in s.select("table.Tst-transaction-table tr"):
        tds = tr.find_all("td", recursive=False)
        if not tds:
            continue
        team_link = next((a for a in tds[-1].select("a[href]") if team_id_from(a["href"], lid) and text(a)), None)
        stamp = text(tds[-1].select_one(".F-timestamp"))
        if tr.select_one(".F-trade") or "traded to" in text(tr).lower():
            if tr.select_one(".F-trade") or trade is None:
                trade = {"type": "trade", "stamp": stamp, "moves": [], "raw": []}
                out.append(trade)
            dest = team_id_from(team_link["href"], lid) if team_link else None
            for p in tr.select("p"):
                ref = player_ref(next((a for a in p.select("a[href*='/nfl/']") if text(a)), None))
                if ref and ref[0]:
                    trade["moves"].append({"player_key": ref[0], "player_name": ref[1], "action": "trade",
                                           "position": position_from(text(p.select_one(".F-position"))),
                                           "source": None, "dest": dest})
            trade["raw"].append(text(tr))
            continue
        trade = None
        icons = [sp.get("title", "") for sp in tds[0].select("span[title]")]
        blocks = tds[1].select("div.Pbot-xs") if len(tds) > 1 else []
        tid = team_id_from(team_link["href"], lid) if team_link else None
        moves = []
        for i, block in enumerate(blocks):
            ref = player_ref(next((a for a in block.select("a[href*='/nfl/']") if text(a)), None))
            if not ref or not ref[0]:
                continue
            icon = icons[i].lower() if i < len(icons) else ""
            dropped = "drop" in icon or text(block.select_one("h6")).lower().startswith("to ")
            moves.append({"player_key": ref[0], "player_name": ref[1],
                          "position": position_from(text(block.select_one(".F-position"))),
                          "action": "drop" if dropped else "add",
                          "source": tid if dropped else None, "dest": None if dropped else tid})
        if not moves:
            continue
        actions = {m["action"] for m in moves}
        out.append({"type": "add/drop" if actions == {"add", "drop"} else actions.pop(), "stamp": stamp,
                    "moves": moves, "raw": [text(tr)]})
    for tx in out:
        if tx["type"] == "trade":
            dests = {m["dest"] for m in tx["moves"] if m["dest"]}
            if len(dests) == 2:
                for m in tx["moves"]:
                    m["source"] = next(d for d in dests if d != m["dest"])
        tx["key"] = hashlib.sha1("|".join(tx["raw"]).encode()).hexdigest()[:16]
    has_next = any("next" in text(a).lower() for a in s.select("a[href*='count=']"))
    return out, has_next


def parse_players(html: str) -> tuple[list[dict], bool]:
    """Season fantasy points from a /players list page, plus whether a next page exists."""
    s = soup(html)
    table = s.select_one("table.Table-interactive") or s.select_one("table")
    out = []
    if table is not None:
        head_rows = table.select("thead tr")
        idx = header_index(head_rows[-1], "Fan Pts") if head_rows else {}
        for tr in table.select("tbody tr"):
            cells = tr.find_all("td", recursive=False)
            a = next((x for x in tr.select("a[href]") if re.search(r"/nfl/(players|teams)/", x["href"]) and text(x)),
                     None)
            ref = player_ref(a)
            if not ref or not ref[0] or "Fan Pts" not in idx or len(cells) <= idx["Fan Pts"]:
                continue
            out.append({"player_key": ref[0], "player_name": ref[1], "points": num(text(cells[idx["Fan Pts"]]))})
    has_next = any("next" in text(a).lower() for a in s.select("a[href*='count=']"))
    return out, has_next


# ---------------------------------------------------------------- fetching

class Fetcher:
    def __init__(self, cookies: str, delay: float, deadline: float):
        import requests

        self.session = requests.Session()
        self.session.headers.update({
            "Cookie": cookies.strip(),
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
                           "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"),
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        })
        self.delay, self.deadline, self.calls = delay, deadline, 0

    def get(self, path: str) -> str:
        if time.time() > self.deadline:
            raise TimeoutError("time budget exhausted")
        url = path if path.startswith("http") else BASE + path
        for attempt in range(4):
            if self.calls:
                time.sleep(self.delay + random.uniform(0, self.delay / 2))
            self.calls += 1
            resp = self.session.get(url, timeout=60)
            if "login.yahoo.com" in resp.url or "guce.yahoo.com" in resp.url:
                raise CookiesExpired("Yahoo redirected to the login page — YAHOO_COOKIES is missing or expired. "
                                     "Copy fresh cookies from a logged-in browser (see README).")
            if resp.status_code in (429, 999) or resp.status_code >= 500:
                wait = 30 * (attempt + 1)
                log.warning("HTTP %s for %s; backing off %ss", resp.status_code, url, wait)
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise ScrapeError(f"HTTP {resp.status_code} for {url}")
            return resp.text
        raise ScrapeError(f"Gave up on {url} after repeated throttling")


# ---------------------------------------------------------------- orchestration

def parse_seasons(spec: str) -> dict[int, str]:
    out = {}
    for part in spec.split(","):
        if ":" in part:
            year, lid = part.split(":", 1)
            out[int(year.strip())] = lid.strip()
    return out


class SeasonScraper:
    def __init__(self, f: Fetcher, conn, season: int, lid: str, current: bool, full: bool):
        self.f, self.conn, self.season, self.lid, self.full = f, conn, season, lid, full
        self.prefix = f"/f1/{lid}" if current else f"/{season}/f1/{lid}"
        self.current = current

    def key(self, tid: int | None) -> str | None:
        return f"{self.season}.l.{self.lid}.t.{tid}" if tid else None

    def run(self) -> None:
        s, conn = self.season, self.conn
        if not self.full and db.was_fetched(conn, f"season:{s}:complete"):
            log.info("%s: complete in cache, skipping", s)
            return
        settings = parse_settings(self.f.get(f"{self.prefix}/settings"))
        if not settings.get("end_week"):
            raise ScrapeError(f"{s}: couldn't read league settings — page layout may have changed")
        home = self.f.get(self.prefix)
        standings_html = self.f.get(f"{self.prefix}?lhst=stand")
        standings = parse_standings(standings_html, self.lid)
        teams = {t["team_id"]: t for t in parse_teams(self.f.get(f"{self.prefix}/teams"), self.lid)}
        if not standings:
            raise ScrapeError(f"{s}: no standings found — page layout may have changed")
        num_teams = len(standings)
        start, end, po_start = settings.get("start_week", 1), settings["end_week"], settings.get("playoff_start_week")
        board = parse_scoreboard(home, self.lid)
        if self.current:
            cur = board.week or start
            last_completed = cur if board.final else cur - 1
        else:
            cur, last_completed = end, end
        finished = last_completed >= end
        db.upsert(conn, "seasons", {
            "season": s, "league_key": f"{s}.l.{self.lid}", "league_id": self.lid, "game_id": None,
            "name": settings.get("name") or config.LEAGUE_NAME, "num_teams": num_teams, "start_week": start,
            "end_week": end, "playoff_start_week": po_start, "num_playoff_teams": settings.get("num_playoff_teams"),
            "current_week": min(cur, end), "last_completed_week": max(last_completed, start - 1),
            "is_finished": int(finished), "roster_positions": json.dumps(settings["roster_positions"]),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })
        for st in standings:
            t = teams.get(st["team_id"], {})
            db.upsert(conn, "teams", {
                "team_key": self.key(st["team_id"]), "season": s, "team_id": st["team_id"], "name": st["name"],
                "manager_guid": t.get("manager_guid"), "manager_nickname": t.get("manager_nickname"),
                "logo_url": None, "final_rank": st["rank"], "playoff_seed": None, "wins": st["wins"],
                "losses": st["losses"], "ties": st["ties"], "points_for": st["points_for"],
                "points_against": st["points_against"], "moves": t.get("moves", st["moves"]),
                "trades": t.get("trades"), "draft_grade": None,
            })
        conn.commit()
        log.info("%s: %s teams, weeks %s-%s, last completed %s", s, num_teams, start, end, last_completed)

        for week in range(start, last_completed + 1):
            refresh = self.current and week == last_completed  # pick up stat corrections
            if db.was_fetched(conn, f"week:{s}:{week}") and not (self.full or refresh):
                continue
            self.scrape_week(week, po_start)
        self.set_seeds(parse_bracket(home, self.lid), po_start, settings.get("num_playoff_teams"))
        positions, season_points = self.scrape_players()
        self.scrape_draft(num_teams, {t["name"]: t["team_id"] for t in standings}, positions, season_points)
        self.fill_positions(positions)
        self.scrape_transactions(end, finished)
        if finished:
            db.mark_fetched(conn, f"season:{s}:complete")
        conn.commit()

    def scrape_week(self, week: int, po_start: int | None) -> None:
        s = self.season
        board = parse_scoreboard(self.f.get(f"{self.prefix}?matchup_week={week}&module=matchups&lhst=matchups"),
                                 self.lid)
        if board.week not in (None, week):
            raise ScrapeError(f"{s}: asked for week {week} but Yahoo returned week {board.week}")
        playoffs = int(bool(po_start and week >= po_start))
        for idx, g in enumerate(board.games):
            (a, b), (pa, pb) = g["teams"], g["points"]
            for me, them, mp, tp, proj in ((a, b, pa, pb, g["projected"][0]), (b, a, pb, pa, g["projected"][1])):
                db.upsert(self.conn, "matchups", {
                    "season": s, "week": week, "matchup_idx": idx, "team_key": self.key(me),
                    "opponent_key": self.key(them), "points": mp, "opponent_points": tp, "projected": proj,
                    "is_playoffs": playoffs, "is_consolation": int(playoffs and "consolation" in g["label"].lower()),
                    "result": "W" if mp > tp else "L" if mp < tp else "T", "week_start": week_start(s, week),
                })
            url = f"{self.prefix}/matchup?week={week}&mid1={a}&mid2={b}"
            teams, rosters = parse_matchup(self.f.get(url), self.lid)
            for tid, rows in rosters.items():
                self.conn.execute("DELETE FROM roster_slots WHERE season=? AND week=? AND team_key=?",
                                  (s, week, self.key(tid)))
                for r in rows:
                    db.upsert(self.conn, "roster_slots", {
                        "season": s, "week": week, "team_key": self.key(tid), "player_key": r["player_key"],
                        "player_name": r["player_name"], "eligible_positions": r["position"] or "",
                        "primary_position": r["position"], "selected_position": r["selected_position"],
                        "points": r["points"],
                    })
        if board.final:
            db.mark_fetched(self.conn, f"week:{s}:{week}")
        self.conn.commit()
        log.info("%s: week %s stored (%s games, final=%s, %s requests so far)", s, week, len(board.games),
                 board.final, self.f.calls)

    def set_seeds(self, bracket: dict[int, int], po_start: int | None, num_playoff: int | None) -> None:
        """Bracket seeds where shown; otherwise regular-season order (wins, then points for)."""
        s, conn = self.season, self.conn
        if not po_start:
            return
        rec = conn.execute(
            """SELECT team_key, SUM(result='W') + 0.5 * SUM(result='T') AS w, SUM(points) AS pf FROM matchups
               WHERE season=? AND week < ? GROUP BY team_key ORDER BY w DESC, pf DESC""", (s, po_start)).fetchall()
        if not rec or conn.execute("SELECT MAX(week) FROM matchups WHERE season=?", (s,)).fetchone()[0] < po_start:
            return  # regular season still running; seeds aren't set yet
        seeds = {r["team_key"]: i + 1 for i, r in enumerate(rec)}
        seeds.update({self.key(tid): seed for tid, seed in bracket.items()})
        for team_key, seed in seeds.items():
            conn.execute("UPDATE teams SET playoff_seed=? WHERE team_key=?", (seed, team_key))
        if num_playoff:
            # Games involving a non-playoff team are consolation; 3rd/5th-place games between playoff teams aren't.
            conn.execute(
                """UPDATE matchups SET is_consolation = CASE WHEN
                       (SELECT playoff_seed FROM teams WHERE team_key = matchups.team_key) > :n OR
                       (SELECT playoff_seed FROM teams WHERE team_key = matchups.opponent_key) > :n THEN 1 ELSE 0 END
                   WHERE season = :s AND is_playoffs = 1""", {"n": num_playoff, "s": s})

    def scrape_players(self) -> tuple[dict[str, str], dict[str, float]]:
        positions: dict[str, str] = {}
        points: dict[str, float] = {}
        for pos, pages in POSITION_PAGES.items():
            for page in range(pages):
                html = self.f.get(f"{self.prefix}/players?status=ALL&pos={pos}&cut_type=9&stat1=S_S_{self.season}"
                                  f"&myteam=0&sort=PTS&sdir=1&count={page * 25}")
                rows, has_next = parse_players(html)
                for r in rows:
                    positions.setdefault(r["player_key"], pos)
                    if r["points"] is not None:
                        points.setdefault(r["player_key"], r["points"])
                if not has_next or not rows:
                    break
        log.info("%s: season totals for %s players", self.season, len(points))
        return positions, points

    def scrape_draft(self, num_teams: int, team_by_name: dict[str, int], positions, season_points) -> None:
        s, conn = self.season, self.conn
        names = dict(team_by_name)
        for r in conn.execute("SELECT team_id, name FROM teams WHERE season=?", (s,)):
            names.setdefault(r["name"], r["team_id"])
        if self.full or not conn.execute("SELECT 1 FROM draft_picks WHERE season=? LIMIT 1", (s,)).fetchone():
            for p in parse_draft(self.f.get(f"{self.prefix}/draftresults"), num_teams):
                db.upsert(conn, "draft_picks", {
                    "season": s, "pick": p["pick"], "round": p["round"], "team_key": self.key(names.get(p["team_name"])),
                    "player_key": p["player_key"], "player_name": p["player_name"], "position": p["position"],
                    "season_points": None, "cost": None,
                })
        rostered = {r["player_key"]: r["pts"] for r in conn.execute(
            "SELECT player_key, SUM(points) AS pts FROM roster_slots WHERE season=? GROUP BY player_key", (s,))}
        for p in conn.execute("SELECT pick, player_key, position FROM draft_picks WHERE season=?", (s,)).fetchall():
            pts = season_points.get(p["player_key"], rostered.get(p["player_key"], 0.0))
            conn.execute("UPDATE draft_picks SET season_points=?, position=? WHERE season=? AND pick=?",
                         (pts, p["position"] or positions.get(p["player_key"]), s, p["pick"]))
        conn.commit()

    def fill_positions(self, positions: dict[str, str]) -> None:
        """Past seasons don't print positions on matchup pages; fill from the position lists, else the slot."""
        conn, s = self.conn, self.season
        rows = conn.execute("SELECT DISTINCT player_key FROM roster_slots WHERE season=? AND "
                            "(primary_position IS NULL OR primary_position='')", (s,)).fetchall()
        for r in rows:
            pos = positions.get(r["player_key"])
            if not pos:
                slot = conn.execute(
                    """SELECT selected_position FROM roster_slots WHERE season=? AND player_key=? AND
                       selected_position IN ('QB','RB','WR','TE','K','DEF') LIMIT 1""", (s, r["player_key"])).fetchone()
                pos = slot[0] if slot else ("DEF" if r["player_key"].startswith("p.def.") else None)
            if pos:
                conn.execute("UPDATE roster_slots SET primary_position=?, eligible_positions=? WHERE season=? AND "
                             "player_key=? AND (primary_position IS NULL OR primary_position='')",
                             (pos, pos, s, r["player_key"]))
        conn.commit()

    def scrape_transactions(self, end_week: int, finished: bool) -> None:
        s, conn = self.season, self.conn
        if not self.full and db.was_fetched(conn, f"tx:{s}:final"):
            return
        offset = 0
        while True:
            txs, has_next = parse_transactions(
                self.f.get(f"{self.prefix}/transactions?transactionsfilter=all&count={offset}"), self.lid)
            known = 0
            for tx in txs:
                tkey = f"{s}.tx.{tx['key']}"
                if conn.execute("SELECT 1 FROM transactions WHERE transaction_key=?", (tkey,)).fetchone():
                    known += 1
                when = parse_timestamp(tx["stamp"], s)
                week = week_of(s, when, end_week) if when else None
                db.upsert(conn, "transactions", {
                    "transaction_key": tkey, "season": s, "type": tx["type"], "status": "successful",
                    "timestamp": int(when.timestamp()) if when else None, "week": week,
                })
                for m in tx["moves"]:
                    src = m["source"]
                    if tx["type"] == "trade" and src is None and week:
                        row = conn.execute(
                            """SELECT team_key FROM roster_slots WHERE season=? AND player_key=? AND week<=?
                               ORDER BY week DESC LIMIT 1""", (s, m["player_key"], week)).fetchone()
                        src_key = row[0] if row and row[0] != self.key(m["dest"]) else None
                    else:
                        src_key = self.key(src)
                    db.upsert(conn, "transaction_players", {
                        "transaction_key": tkey, "player_key": m["player_key"], "player_name": m["player_name"],
                        "position": m["position"], "action": m["action"], "source_team_key": src_key,
                        "destination_team_key": self.key(m["dest"]),
                    })
            conn.commit()
            # Newest first: once a whole page is already stored, older pages are too.
            if not has_next or not txs or (known == len(txs) and not self.full):
                break
            offset += 25
        if finished:
            db.mark_fetched(conn, f"tx:{s}:final")


def run(args) -> int:
    cookies = os.environ.get("YAHOO_COOKIES", "").strip()
    if not cookies:
        log.error("YAHOO_COOKIES is not set. See README → 'Scraping with browser cookies'.")
        return 1
    seasons = parse_seasons(args.seasons)
    current_lid = args.league_id
    if not seasons:
        log.error("No seasons configured (expected YAHOO_SEASONS like '2024:934670,2025:388631').")
        return 1
    f = Fetcher(cookies, delay=args.delay, deadline=time.time() + args.time_budget * 60)
    # Probe one page before touching the DB so bad cookies never leave an empty league.sqlite behind.
    current_season = max((y for y, lid in seasons.items() if lid == current_lid), default=max(seasons))
    try:
        f.get(f"/f1/{current_lid}/settings" if seasons.get(current_season) == current_lid else
              f"/{current_season}/f1/{seasons[current_season]}/settings")
    except ScrapeError as exc:
        log.error("%s", exc)
        return 1
    conn = db.connect(args.db)
    status = 0
    try:
        for season in sorted(seasons, reverse=True):  # newest first so this week's data lands quickly
            if args.season and season not in args.season:
                continue
            try:
                SeasonScraper(f, conn, season, seasons[season], seasons[season] == current_lid, args.full).run()
            except (CookiesExpired, TimeoutError):
                raise
            except Exception:  # noqa: BLE001 - one bad season shouldn't sink the run
                log.exception("Season %s failed; continuing", season)
                conn.rollback()
                status = 1
    except TimeoutError as exc:
        log.warning("Stopping early (%s); progress is saved and the next run resumes.", exc)
    except CookiesExpired as exc:
        log.error("%s", exc)
        status = 1
    if conn.execute("SELECT COUNT(*) FROM teams").fetchone()[0]:
        db.set_meta(conn, "is_sample", "0")
        db.set_meta(conn, "last_ingest", datetime.now(timezone.utc).isoformat())
        db.set_meta(conn, "source", "scrape")
    conn.commit()
    conn.close()
    log.info("Done. %s page requests.", f.calls)
    return status
