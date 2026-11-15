"""SQLite schema and helpers. The same schema is used for real Yahoo data and sample fixtures."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS seasons (
    season INTEGER PRIMARY KEY,
    league_key TEXT NOT NULL,
    league_id TEXT,
    game_id TEXT,
    name TEXT,
    num_teams INTEGER,
    start_week INTEGER,
    end_week INTEGER,
    playoff_start_week INTEGER,
    num_playoff_teams INTEGER,
    current_week INTEGER,
    last_completed_week INTEGER,
    is_finished INTEGER DEFAULT 0,
    roster_positions TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_key TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    name TEXT,
    manager_guid TEXT,
    manager_nickname TEXT,
    logo_url TEXT,
    final_rank INTEGER,
    playoff_seed INTEGER,
    wins INTEGER DEFAULT 0,
    losses INTEGER DEFAULT 0,
    ties INTEGER DEFAULT 0,
    points_for REAL DEFAULT 0,
    points_against REAL DEFAULT 0,
    moves INTEGER,
    trades INTEGER,
    draft_grade TEXT
);

-- One row per team per week (every game appears twice, once from each side).
CREATE TABLE IF NOT EXISTS matchups (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    matchup_idx INTEGER NOT NULL,
    team_key TEXT NOT NULL,
    opponent_key TEXT,
    points REAL,
    opponent_points REAL,
    projected REAL,
    is_playoffs INTEGER DEFAULT 0,
    is_consolation INTEGER DEFAULT 0,
    result TEXT,
    week_start TEXT,
    PRIMARY KEY (season, week, team_key)
);

CREATE TABLE IF NOT EXISTS roster_slots (
    season INTEGER NOT NULL,
    week INTEGER NOT NULL,
    team_key TEXT NOT NULL,
    player_key TEXT NOT NULL,
    player_name TEXT,
    eligible_positions TEXT,
    primary_position TEXT,
    selected_position TEXT,
    points REAL,
    PRIMARY KEY (season, week, team_key, player_key)
);

CREATE TABLE IF NOT EXISTS draft_picks (
    season INTEGER NOT NULL,
    pick INTEGER NOT NULL,
    round INTEGER,
    team_key TEXT,
    player_key TEXT,
    player_name TEXT,
    position TEXT,
    season_points REAL,
    cost INTEGER,
    PRIMARY KEY (season, pick)
);

CREATE TABLE IF NOT EXISTS transactions (
    transaction_key TEXT PRIMARY KEY,
    season INTEGER NOT NULL,
    type TEXT,
    status TEXT,
    timestamp INTEGER,
    week INTEGER
);

CREATE TABLE IF NOT EXISTS transaction_players (
    transaction_key TEXT NOT NULL,
    player_key TEXT NOT NULL,
    player_name TEXT,
    position TEXT,
    action TEXT,
    source_team_key TEXT,
    destination_team_key TEXT,
    PRIMARY KEY (transaction_key, player_key, action)
);

CREATE TABLE IF NOT EXISTS fetch_log (
    key TEXT PRIMARY KEY,
    fetched_at TEXT
);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def upsert(conn: sqlite3.Connection, table: str, row: dict) -> None:
    cols = list(row.keys())
    placeholders = ",".join("?" for _ in cols)
    conn.execute(
        f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) VALUES ({placeholders})",
        [row[c] for c in cols],
    )


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    upsert(conn, "meta", {"key": key, "value": value})


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def was_fetched(conn: sqlite3.Connection, key: str) -> bool:
    return conn.execute("SELECT 1 FROM fetch_log WHERE key = ?", (key,)).fetchone() is not None


def mark_fetched(conn: sqlite3.Connection, key: str) -> None:
    upsert(conn, "fetch_log", {"key": key, "fetched_at": datetime.now(timezone.utc).isoformat()})
