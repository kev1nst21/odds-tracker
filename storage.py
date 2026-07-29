"""SQLite-backed history of odds snapshots, so we can diff each new poll
against the previous one per (fixture, bookmaker, market, outcome, player)."""
import os
import sqlite3
from contextlib import contextmanager

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    start_time TEXT,
    bookmaker TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    player_key TEXT NOT NULL,
    price REAL NOT NULL,
    label TEXT
);
CREATE INDEX IF NOT EXISTS idx_lookup
    ON odds_snapshots (fixture_id, bookmaker, market_id, outcome_id, player_key, fetched_at);
"""


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def get_latest_price(fixture_id, bookmaker, market_id, outcome_id, player_key):
    """Return (fetched_at, price) of the most recent stored snapshot for this
    exact odds line, or None if we've never seen it before."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT fetched_at, price FROM odds_snapshots
            WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=? AND player_key=?
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (fixture_id, bookmaker, market_id, outcome_id, player_key),
        ).fetchone()
        return row  # (fetched_at, price) or None


def save_snapshot(records, fetched_at):
    with _conn() as conn:
        conn.executemany(
            """
            INSERT INTO odds_snapshots
                (fetched_at, fixture_id, start_time, bookmaker, market_id, outcome_id, player_key, price, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    fetched_at,
                    r["fixture_id"],
                    r.get("start_time"),
                    r["bookmaker"],
                    r["market_id"],
                    r["outcome_id"],
                    r["player_key"],
                    r["price"],
                    r.get("label"),
                )
                for r in records
            ],
        )
        conn.commit()


def recent_snapshots(limit=200):
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT fetched_at, fixture_id, bookmaker, market_id, outcome_id, player_key, price, label
            FROM odds_snapshots ORDER BY fetched_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows
