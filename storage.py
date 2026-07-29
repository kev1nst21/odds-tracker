"""SQLite-backed history of odds snapshots, so we can diff each new poll
against the previous one per (fixture, bookmaker, market, outcome, player).

Schema v2 (2026-07-29, switch to The Odds API): added sport_key/home_team/
away_team everywhere so alerts and CLV analysis can reference real event
names and look up scores per sport; added alert_price/clv_pct/clv_continued
to tracked_alerts for Closing Line Value tracking (see results.py); added a
small meta key-value table to throttle results-checking without a separate
state file."""
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from config import DB_PATH, FLAT_STAKE

SCHEMA = """
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    sport_key TEXT,
    start_time TEXT,
    home_team TEXT,
    away_team TEXT,
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

-- One row per BET we would have placed, not per line that twitched. The three
-- prices are the whole point: what the odds were before the money arrived
-- (old_price), where the books that moved settled (new_price), and what we
-- actually recommended taking (entry_price, at entry_book). Scoring uses
-- entry_price, because that is the number a real bet would have got.
CREATE TABLE IF NOT EXISTS tracked_alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alert_type TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'prematch',  -- 'prematch' | 'live'
    fixture_id TEXT NOT NULL,
    sport_key TEXT,
    start_time TEXT,
    home_team TEXT,
    away_team TEXT,
    outcome_id TEXT NOT NULL,          -- 'home' / 'away' -- the side money went into
    outcome_name TEXT,                 -- team/player name as shown in the alert
    stars INTEGER,                     -- confidence at the time of the alert
    down_count INTEGER,                -- how many books had moved
    books_count INTEGER,
    old_price REAL,                    -- what the coefficient was before the drop
    new_price REAL,                    -- what it dropped to at the books that moved
    entry_price REAL,                  -- what we said to bet at
    entry_book TEXT,                   -- where that price was
    market_id TEXT,
    player_key TEXT,
    bookmaker TEXT,                    -- kept = entry_book, for the CLV price lookup
    label TEXT,
    direction TEXT NOT NULL DEFAULT 'down',
    alert_price REAL,                  -- alias of entry_price, kept for CLV code
    detected_at TEXT NOT NULL,
    resolved INTEGER NOT NULL DEFAULT 0,
    result TEXT,                       -- 'hit', 'miss', or 'n/a' once resolved
    clv_pct REAL,
    clv_continued INTEGER,
    resolved_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_tracked_alerts_lookup
    ON tracked_alerts (fixture_id, outcome_id, resolved);
-- One alert per (event, side): if the same move keeps showing up on later
-- polls we don't want to log it again and inflate the stats.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tracked_alerts_dedup
    ON tracked_alerts (kind, fixture_id, outcome_id);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def _conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def get_meta(key: str):
    with _conn() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None


def set_meta(key: str, value: str):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
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
        return (row["fetched_at"], row["price"]) if row else None


def get_closing_price(fixture_id, bookmaker, market_id, outcome_id, player_key, before_iso):
    """Most recent snapshot for this exact line at or before before_iso
    (normally the match's start_time) -- used as the 'closing line' for CLV.
    Falls back to the latest snapshot overall if none exist before that time
    (e.g. the match started before our next poll caught up)."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT fetched_at, price FROM odds_snapshots
            WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=? AND player_key=?
              AND fetched_at<=?
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (fixture_id, bookmaker, market_id, outcome_id, player_key, before_iso),
        ).fetchone()
        if row:
            return (row["fetched_at"], row["price"])
        return get_latest_price(fixture_id, bookmaker, market_id, outcome_id, player_key)


def save_snapshot(records, fetched_at):
    with _conn() as conn:
        conn.executemany(
            """
            INSERT INTO odds_snapshots
                (fetched_at, fixture_id, sport_key, start_time, home_team, away_team,
                 bookmaker, market_id, outcome_id, player_key, price, label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    fetched_at,
                    r["fixture_id"],
                    r.get("sport_key"),
                    r.get("start_time"),
                    r.get("home_team"),
                    r.get("away_team"),
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
            SELECT COUNT(*) AS n FROM spike_events
            WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=? AND player_key=?
              AND direction=? AND detected_at>=?
            """,
            (fixture_id, bookmaker, market_id, outcome_id, player_key, direction, since_iso),
        ).fetchone()
        return row["n"] if row else 0


def save_bet_alert(summary: dict, detected_at: str) -> bool:
    """Record the bet an alert actually recommends, so it can be scored later.

    Stores all three prices -- what the coefficient was, what it dropped to,
    and what we said to take -- because the only honest way to judge the tool
    is against the price a real bet would have got, not against whichever
    bookmaker happened to move first. Returns True if a new row was written
    (the same event/side is only ever logged once).
    """
    bet = summary.get("bet") or {}
    if not bet.get("entry_price"):
        return False
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO tracked_alerts
                (alert_type, kind, fixture_id, sport_key, start_time, home_team, away_team,
                 outcome_id, outcome_name, stars, down_count, books_count,
                 old_price, new_price, entry_price, entry_book,
                 market_id, player_key, bookmaker, label, direction,
                 alert_price, detected_at, resolved)
            VALUES (?, 'prematch', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'down', ?, ?, 0)
            """,
            (
                "bet",
                summary["fixture_id"],
                summary.get("sport_key"),
                summary.get("start_time"),
                summary.get("home_team"),
                summary.get("away_team"),
                bet["side"],
                bet["name"],
                summary.get("stars"),
                bet.get("down_count"),
                bet.get("books_count"),
                bet.get("old_price"),
                bet.get("new_price"),
                bet.get("entry_price"),
                bet.get("entry_book"),
                "h2h",
                "-",
                bet.get("entry_book"),
                bet.get("name"),
                bet.get("entry_price"),
                detected_at,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def get_unresolved_alerts(before_iso: str, limit: int = 200):
    """Unresolved tracked alerts whose event start_time is before before_iso
    (i.e. the match should be over by now), oldest first. Returns
    sqlite3.Row objects -- index by column name (row['sport_key']) or
    position, both work."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT id, alert_type, fixture_id, sport_key, start_time, home_team, away_team,
                   bookmaker, market_id, outcome_id, outcome_name, player_key, label,
                   direction, alert_price, entry_price, old_price, new_price, detected_at
            FROM tracked_alerts
            WHERE resolved=0 AND start_time IS NOT NULL AND start_time < ?
            ORDER BY start_time ASC LIMIT ?
            """,
            (before_iso, limit),
        ).fetchall()
        return rows


def mark_resolved(alert_id: int, result: str, resolved_at: str, clv_pct: float = None, clv_continued=None):
    with _conn() as conn:
        conn.execute(
            """
            UPDATE tracked_alerts
            SET resolved=1, result=?, resolved_at=?, clv_pct=?, clv_continued=?
            WHERE id=?
            """,
            (
                result,
                resolved_at,
                clv_pct,
                None if clv_continued is None else int(clv_continued),
                alert_id,
            ),
        )
        conn.commit()


def alert_stats(kind: str = "prematch"):
    """Summary for the dashboard: how many alerts we've fired, how many are
    resolved, the win rate among resolved 1X2-style alerts, and the average
    Closing Line Value -- a more statistically robust "are we beating the
    bookmakers" signal than win/loss alone, since CLV measures whether the
    market kept moving the way we called it, independent of the final score."""
    with _conn() as conn:
        k = (kind,)
        total = conn.execute("SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=?", k).fetchone()["n"]
        resolved = conn.execute("SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND resolved=1", k).fetchone()["n"]
        hits = conn.execute("SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND result='hit'", k).fetchone()["n"]
        misses = conn.execute("SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND result='miss'", k).fetchone()["n"]
        clv_row = conn.execute(
            "SELECT AVG(clv_pct) AS avg_clv, "
            "SUM(CASE WHEN clv_continued=1 THEN 1 ELSE 0 END) AS clv_wins, "
            "SUM(CASE WHEN clv_continued IS NOT NULL THEN 1 ELSE 0 END) AS clv_n "
            "FROM tracked_alerts WHERE kind=? AND clv_pct IS NOT NULL", k
        ).fetchone()
        recent = conn.execute(
            """
            SELECT fixture_id, home_team, away_team, outcome_name, stars,
                   old_price, new_price, entry_price, entry_book,
                   result, clv_pct, clv_continued, resolved_at
            FROM tracked_alerts WHERE kind=? AND resolved=1
            ORDER BY resolved_at DESC LIMIT 20
            """, k
        ).fetchall()
        # Flat-stake profit/loss over every graded bet, priced at the entry we
        # actually recommended. A win returns stake x (odds - 1), a loss costs
        # the stake. Bets graded 'n/a' are excluded rather than counted as
        # pushes -- we don't know what they did.
        graded = conn.execute(
            "SELECT result, entry_price FROM tracked_alerts "
            "WHERE kind=? AND resolved=1 AND result IN ('hit','miss') AND entry_price IS NOT NULL", k
        ).fetchall()
        clv_n = clv_row["clv_n"] or 0
        profit = 0.0
        for g in graded:
            if g["result"] == "hit":
                profit += FLAT_STAKE * (g["entry_price"] - 1)
            else:
                profit -= FLAT_STAKE
        staked = FLAT_STAKE * len(graded)
        return {
            "total": total,
            "resolved": resolved,
            "pending": total - resolved,
            "hits": hits,
            "misses": misses,
            "win_rate": (hits / (hits + misses) * 100) if (hits + misses) else None,
            "avg_clv_pct": clv_row["avg_clv"],
            "clv_continued_rate": (clv_row["clv_wins"] / clv_n * 100) if clv_n else None,
            "clv_n": clv_n,
            "kind": kind,
            "stake": FLAT_STAKE,
            "graded_n": len(graded),
            "staked": staked,
            "profit": profit,
            "roi_pct": (profit / staked * 100) if staked else None,
            "recent": recent,
        }


def save_live_alert(row: dict, detected_at: str) -> bool:
    """Log a live signal -- a bookmaker still offering a price the rest of the
    in-play market has already moved away from. Kept in the same table as
    pre-match bets but under kind='live', because the two have to be judged
    separately: they are different strategies with different hit profiles, and
    averaging them together would hide which one actually works."""
    if not row.get("high"):
        return False
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO tracked_alerts
                (alert_type, kind, fixture_id, sport_key, start_time, home_team, away_team,
                 outcome_id, outcome_name, stars, down_count, books_count,
                 old_price, new_price, entry_price, entry_book,
                 market_id, player_key, bookmaker, label, direction,
                 alert_price, detected_at, resolved)
            VALUES ('live', 'live', ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, 'h2h', '-', ?, ?, 'down', ?, ?, 0)
            """,
            (
                row["fixture_id"], row.get("sport_key"), row.get("start_time"),
                row.get("home_team"), row.get("away_team"),
                row["side"], row["name"],
                row.get("books_count"), row.get("books_count"),
                row.get("median"), row.get("median"),
                row["high"], row["outlier_book"],
                row["outlier_book"], row["name"],
                row["high"], detected_at,
            ),
        )
        conn.commit()
        return cur.rowcount > 0


def recent_bets(limit: int = 5, kind: str = "prematch"):
    """The last N bets we called, resolved or not. Unlike alert_stats()['recent']
    this deliberately includes pending ones -- right after a stats reset there
    are no finished matches yet, and a panel that renders empty for hours looks
    broken rather than new."""
    with _conn() as conn:
        return conn.execute(
            """
            SELECT fixture_id, home_team, away_team, outcome_name, stars,
                   down_count, books_count, old_price, new_price,
                   entry_price, entry_book, start_time, detected_at,
                   resolved, result, clv_pct, resolved_at
            FROM tracked_alerts WHERE kind=?
            ORDER BY detected_at DESC LIMIT ?
            """,
            (kind, limit),
        ).fetchall()


def top_books(limit: int = 10):
    """Bookmakers ranked by how often the entry landed with them.

    The entry book is the one still offering the old price after the rest of
    the market moved -- i.e. the place the value actually was. Counting them
    answers a genuinely useful question: which bookmakers are slowest to
    reprice, and therefore worth having an account with. (Also the natural
    place to hang affiliate links later.)
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT entry_book AS book, COUNT(*) AS n,
                   SUM(CASE WHEN kind='live' THEN 1 ELSE 0 END) AS live_n
            FROM tracked_alerts
            WHERE entry_book IS NOT NULL AND entry_book <> ''
            GROUP BY entry_book ORDER BY n DESC, book ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{"book": r["book"], "n": r["n"], "live_n": r["live_n"] or 0} for r in rows]


def coverage_stats(hours: int = 24):
    """Honest scale-of-operation numbers for the header.

    Everything here is counted from what actually landed in the database over
    the window -- no estimates, no multipliers. It reads impressively because
    the pipeline genuinely does this much work, not because the numbers were
    dressed up.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS lines, COUNT(DISTINCT fixture_id) AS events, "
            "COUNT(DISTINCT bookmaker) AS books, COUNT(DISTINCT sport_key) AS sports, "
            "COUNT(DISTINCT fetched_at) AS cycles "
            "FROM odds_snapshots WHERE fetched_at>=?", (since,),
        ).fetchone()
        moves = conn.execute(
            "SELECT COUNT(*) AS n FROM spike_events WHERE detected_at>=?", (since,),
        ).fetchone()["n"]
        return {
            "hours": hours,
            "lines": row["lines"] or 0,
            "events": row["events"] or 0,
            "books": row["books"] or 0,
            "sports": row["sports"] or 0,
            "cycles": row["cycles"] or 0,
            "moves": moves or 0,
        }


def snapshot_meta():
    """Describes the most recent poll: when it ran, how many odds lines it
    stored, and which bookmakers / sports / events actually came back. The
    dashboard shows this so a reader can see exactly where the numbers on the
    page came from, rather than having to trust an unlabelled table."""
    with _conn() as conn:
        row = conn.execute("SELECT MAX(fetched_at) AS last FROM odds_snapshots").fetchone()
        last = row["last"] if row else None
        if not last:
            return {"fetched_at": None, "lines": 0, "bookmakers": [], "sports": [], "events": 0}
        stats = conn.execute(
            "SELECT COUNT(*) AS lines, COUNT(DISTINCT fixture_id) AS events "
            "FROM odds_snapshots WHERE fetched_at=?",
            (last,),
        ).fetchone()
        books = [r["bookmaker"] for r in conn.execute(
            "SELECT DISTINCT bookmaker FROM odds_snapshots WHERE fetched_at=? ORDER BY bookmaker",
            (last,),
        ).fetchall()]
        sports = [r["sport_key"] for r in conn.execute(
            "SELECT DISTINCT sport_key FROM odds_snapshots WHERE fetched_at=? AND sport_key IS NOT NULL "
            "ORDER BY sport_key",
            (last,),
        ).fetchall()]
        return {
            "fetched_at": last,
            "lines": stats["lines"],
            "events": stats["events"],
            "bookmakers": books,
            "sports": sports,
        }


def recent_snapshots(limit=200):
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT fetched_at, fixture_id, home_team, away_team, bookmaker, market_id,
                   outcome_id, player_key, price, label
            FROM odds_snapshots ORDER BY fetched_at DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows
