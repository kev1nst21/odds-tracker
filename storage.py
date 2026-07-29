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

CREATE TABLE IF NOT EXISTS spike_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    bookmaker TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    player_key TEXT NOT NULL,
    direction TEXT NOT NULL,
    pct_change REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spike_lookup
    ON spike_events (fixture_id, bookmaker, market_id, outcome_id, player_key, direction, detected_at);

CREATE TABLE IF NOT EXISTS tracked_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,          -- 'spike', 'cascade', or 'digest'
    fixture_id TEXT NOT NULL,
    start_time TEXT,
    bookmaker TEXT NOT NULL,
    market_id TEXT NOT NULL,
    outcome_id TEXT NOT NULL,
    player_key TEXT NOT NULL,
    label TEXT,
    direction TEXT NOT NULL,           -- 'up' or 'down' (odds shortening = 'down' = market favors this outcome more)
    detected_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    result TEXT,                       -- 'hit', 'miss', or 'n/a' once resolved
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracked_alerts_lookup
    ON tracked_alerts (fixture_id, bookmaker, market_id, outcome_id, player_key, resolved);
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_alerts_dedup
    ON tracked_alerts (alert_type, fixture_id, bookmaker, market_id, outcome_id, player_key);
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


def save_spike_event(spike: dict, detected_at: str):
    """Record a detected single-step spike so future polls can tell whether
    this exact line is on a streak of same-direction moves (see
    count_recent_same_direction_spikes)."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT INTO spike_events
                (detected_at, fixture_id, bookmaker, market_id, outcome_id, player_key, direction, pct_change)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                detected_at,
                spike["fixture_id"],
                spike["bookmaker"],
                spike["market_id"],
                spike["outcome_id"],
                spike["player_key"],
                "up" if spike["pct_change"] > 0 else "down",
                spike["pct_change"],
            ),
        )
        conn.commit()


def count_recent_same_direction_spikes(fixture_id, bookmaker, market_id, outcome_id, player_key, direction, since_iso):
    """How many earlier spikes on this exact line, in the same direction,
    happened at or after since_iso. Used to flag cascades (e.g. -5% then
    another -5% within 30 min) with a super-alert."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM spike_events
            WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=? AND player_key=?
              AND direction=? AND detected_at>=?
            """,
            (fixture_id, bookmaker, market_id, outcome_id, player_key, direction, since_iso),
        ).fetchone()
        return row[0] if row else 0


def save_tracked_alert(alert_type: str, r: dict, direction: str, detected_at: str):
    """Record that we alerted on this line, so we can later check whether the
    match result agreed with the move (see results.py). One row per
    (alert_type, exact line) -- repeat spikes on the same line before it
    resolves just get ignored, we only need the first alert timestamp."""
    with _conn() as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO tracked_alerts
                (alert_type, fixture_id, start_time, bookmaker, market_id, outcome_id,
                 player_key, label, direction, detected_at, resolved)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
            """,
            (
                alert_type,
                r["fixture_id"],
                r.get("start_time"),
                r["bookmaker"],
                r["market_id"],
                r["outcome_id"],
                r["player_key"],
                r.get("label"),
                direction,
                detected_at,
            ),
        )
        conn.commit()


def get_unresolved_alerts(before_iso: str, limit: int = 50):
    """Unresolved tracked alerts whose event start_time is before before_iso
    (i.e. the match should be over by now), oldest first."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, alert_type, fixture_id, start_time, bookmaker, market_id,
                   outcome_id, player_key, label, direction, detected_at
            FROM tracked_alerts
            WHERE resolved=0 AND start_time IS NOT NULL AND start_time < ?
            ORDER BY start_time ASC LIMIT ?
            """,
            (before_iso, limit),
        ).fetchall()
        return rows


def mark_resolved(alert_id: int, result: str, resolved_at: str):
    with _conn() as conn:
        conn.execute(
            "UPDATE tracked_alerts SET resolved=1, result=?, resolved_at=? WHERE id=?",
            (result, resolved_at, alert_id),
        )
        conn.commit()


def alert_stats():
    """Summary for the dashboard: how many alerts we've fired, how many are
    resolved yet, and the hit rate among resolved ones where a result could
    actually be determined (simple moneyline-style markets only -- spreads
    and totals are recorded but counted as 'n/a', not wins or losses)."""
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) FROM tracked_alerts").fetchone()[0]
        resolved = conn.execute("SELECT COUNT(*) FROM tracked_alerts WHERE resolved=1").fetchone()[0]
        hits = conn.execute("SELECT COUNT(*) FROM tracked_alerts WHERE result='hit'").fetchone()[0]
        misses = conn.execute("SELECT COUNT(*) FROM tracked_alerts WHERE result='miss'").fetchone()[0]
        recent = conn.execute(
            """
            SELECT fixture_id, bookmaker, label, direction, alert_type, result, resolved_at
            FROM tracked_alerts WHERE resolved=1
            ORDER BY resolved_at DESC LIMIT 20
            """
        ).fetchall()
        return {
            "total": total,
            "resolved": resolved,
            "pending": total - resolved,
            "hits": hits,
            "misses": misses,
            "win_rate": (hits / (hits + misses) * 100) if (hits + misses) else None,
            "recent": recent,
        }


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
