"""SQLite-backed history of odds snapshots, so we can diff each new poll
against the previous one per (fixture, bookmaker, market, outcome, player).

Schema v2 (2026-07-29, switch to The Odds API): added sport_key/home_team/
away_team everywhere so alerts and CLV analysis can reference real event
names and look up scores per sport; added alert_price/clv_pct/clv_continued
to tracked_alerts for Closing Line Value tracking (see results.py); added a
small meta key-value table to throttle results-checking without a separate
state file.

2026-07-29: the database is no longer thrown away when a column is added.
Four schema versions in one day each wiped the track record, which is the one
asset this product has -- so new columns now arrive through _migrate() below
and the history survives. If a change ever genuinely invalidates past rows,
bump DB_PATH deliberately and say so on the site; don't do it by accident.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from config import (
    DB_PATH,
    FLAT_STAKE,
    OPTIMAL_MAX_PRICE,
    SNAPSHOT_RETENTION_HOURS,
    ENTRY_MIN_GAP_PCT,
    ENTRY_MIN_CAPTURE_PCT,
    ENTRY_MAX_OVER_OLD_PCT,
    POLYMARKET_TARGET_STAKE,
    POLYMARKET_MIN_EDGE_PCT,
)

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

-- One row per MOVEMENT -- every drop that cleared the threshold, whether or
-- not a bookmaker was still offering the old price. tracked_alerts only holds
-- the ones we could actually bet; this table holds all of them, priced at
-- old_price, i.e. "what if we always caught the coefficient before it fell".
-- That number is a ceiling, not money: it is what the money-flow thesis is
-- worth when execution is free. Comparing it with tracked_alerts is how we
-- tell "the idea is wrong" apart from "the idea is right but we're too slow".
CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    sport_key TEXT,
    start_time TEXT,
    home_team TEXT,
    away_team TEXT,
    outcome_id TEXT NOT NULL,
    outcome_name TEXT,
    stars INTEGER,
    old_price REAL,          -- the coefficient we would have caught
    new_price REAL,
    drop_pct REAL,
    down_count INTEGER,
    books_count INTEGER,
    had_entry INTEGER,       -- was it also a real signal?
    entry_price REAL,
    resolved INTEGER NOT NULL DEFAULT 0,
    result TEXT,
    resolved_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_movements_dedup
    ON movements (fixture_id, outcome_id);
CREATE INDEX IF NOT EXISTS idx_movements_open
    ON movements (resolved, start_time);

-- Polymarket. One row per LOOK, not one per signal.
--
-- Added 20.08.2026, when Polymarket became the instrument rather than a
-- curiosity. A signal is checked against Polymarket over and over from the
-- moment it fires until kick-off, because the market there often does not
-- exist yet when we detect the move (measured: their whole open football
-- listing spans three days, our signals fire up to 44 hours out) and because
-- the line moves after it appears. Vladislav's instruction was to wait for our
-- price rather than ask once: "мы будем за ней следить и ставить когда нам
-- будет подходить".
--
-- So this table is a time series, and that is the point: it answers not only
-- "was there an edge" but "when did it appear and how long did it live" --
-- the open question from the 09.08 plan that nothing has been able to answer
-- because nothing was recorded.
CREATE TABLE IF NOT EXISTS pm_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT NOT NULL,
    fixture_id TEXT NOT NULL,
    outcome_name TEXT NOT NULL,
    sport_key TEXT,
    start_time TEXT,
    lead_hours REAL,           -- how far from kick-off this look was
    matched INTEGER NOT NULL,  -- did we find the event at all
    reason TEXT,               -- why not, when not
    event_title TEXT,
    event_slug TEXT,
    token_id TEXT,
    entry_price REAL,          -- OUR bookmaker price: the base of the rule
    need_coef REAL,            -- entry_price * 1.05
    best_coef REAL,            -- top of their book
    avg_coef REAL,             -- what we would actually average on our size
    exec_stake_usd REAL,       -- how much fits at or above need_coef
    fits_target INTEGER,       -- did the full target fit
    edge_pct REAL,             -- (avg_coef / entry_price - 1) * 100
    take INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_pm_fixture
    ON pm_quotes (fixture_id, outcome_name, checked_at);
CREATE INDEX IF NOT EXISTS idx_pm_take ON pm_quotes (take, checked_at);

-- One row per poll: how many events the market moved, and where each one
-- stopped being a signal. Without this the header can only say "22 движения"
-- and "1 сигнал", which reads as a bug rather than as a filter doing its job.
CREATE TABLE IF NOT EXISTS funnel_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    big_drop INTEGER, thin_market INTEGER, all_books_moved INTEGER,
    entry_too_low INTEGER, signals INTEGER
);
CREATE INDEX IF NOT EXISTS idx_funnel_at ON funnel_log (at);

-- Live score for a match that has already kicked off. Kept in its own table
-- rather than as columns on tracked_alerts because one fixture can carry a
-- signal AND a movement row, and both want the same score.
CREATE TABLE IF NOT EXISTS live_scores (
    fixture_id TEXT PRIMARY KEY,
    sport_key TEXT,
    home_team TEXT,
    away_team TEXT,
    home_score REAL,
    away_score REAL,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

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


# Columns added after the table first shipped. CREATE TABLE IF NOT EXISTS does
# nothing to an existing table, so without this an older database would keep
# running with the old shape and every INSERT naming a new column would fail.
# Additive only -- nothing here can destroy a row.
_MIGRATIONS = {
    "pm_quotes": {
        # Which of the two bets on the same event this row is about. Added
        # 20.08.2026: "бот в таком случае будет делать две ставки с разным
        # кофом, если такое возможно". One event can now produce an aggressive
        # leg (straight win) and an optimal leg (the double chance, bought on
        # Polymarket as "No" on the opponent), each with its own book, its own
        # limit and its own size.
        "leg": "TEXT NOT NULL DEFAULT 'aggressive'",
        # Where the candidate came from: a published signal, or a raw movement
        # that our own filters rejected. The second pool is several times the
        # first, and on Polymarket a rejected move can still be a good trade --
        # our filters were tuned for bookmaker limits, not for an order book.
        "source": "TEXT NOT NULL DEFAULT 'signal'",
        "question": "TEXT",
        "means": "TEXT",
        "markets_total": "INTEGER",
        # Насколько Polymarket ещё НЕ отыграл движение контор: 1.0 -- стоит на
        # доviженческой цене, 0.0 -- отыграл полностью. Это, а не размер
        # зазора в процентах, и есть наш эдж, и на этом строится оценка сделки.
        "pm_lag": "REAL",
        "down_count": "INTEGER",
        "books_count": "INTEGER",
    },
    "movements": {
        # "had_entry" used to mean "a bookmaker was still offering the old
        # price", and the site printed it as "был вход" -- which readers
        # correctly took to mean "we bet this". Those are different things: a
        # move can have a takeable price and still be refused as a signal
        # because only one bookmaker moved. This column records the second,
        # narrower fact, so the two can be shown apart instead of conflated.
        "was_signal": "INTEGER NOT NULL DEFAULT 0",
        # Which funnel bucket this move landed in: 'signal' | 'thin_market' |
        # 'all_books_moved' | 'entry_too_low'. Stored here so the funnel can be
        # counted off deduplicated rows instead of summed per poll -- see
        # funnel_stats() for why the old sum double-counted.
        "bucket": "TEXT",
        # The best price still on offer when we REFUSED the entry.
        #
        # Added 2026-08-09 because a question could not be answered without it.
        # The single biggest killer of signals is ENTRY_MIN_CAPTURE_PCT -- the
        # entry has to give back at least half the drop, or we would be
        # announcing "было 3.20" and sending you to bet 2.40. Reasonable rule,
        # but is 50% the right number? To know what a 40% or 30% threshold
        # would have produced, you need the price that was actually available
        # and got refused. We never stored it, so every rejected move was
        # unrecoverable and the threshold was unfalsifiable.
        "best_left_price": "REAL",
    },
    "tracked_alerts": {
        # 'aggressive' | 'optimal' -- which strategy bucket this signal falls
        # into. Stored rather than derived so a later change to the 2.8 cut-off
        # can't silently rewrite history that was already published.
        "strategy": "TEXT",
        # The safer alternative offered alongside a long-shot pick, if one was
        # computable: what market it is, what to back, and at what price.
        "safe_market": "TEXT",
        "safe_pick": "TEXT",
        "safe_price": "REAL",
        "safe_book": "TEXT",
        # Cadence in force when the signal fired. The whole point of the
        # 3/5/10-minute experiment is to compare buckets, and that is only
        # possible if each row remembers which bucket it belongs to.
        "poll_interval_minutes": "INTEGER",
        # What the ОПТИМАЛЬНАЯ strategy actually did with this signal, which
        # since 2026-07-30 is no longer "the same bet or nothing": at or below
        # the cut-off it backs the straight pick, above it the double chance or
        # a handicap. Stored separately from the aggressive bet because the two
        # strategies now place DIFFERENT bets at DIFFERENT prices on the same
        # event, and one set of columns cannot describe both.
        "opt_kind": "TEXT",        # 'straight' | 'double_chance' | 'handicap'
        "opt_pick": "TEXT",
        "opt_price": "REAL",
        "opt_book": "TEXT",
        # 0 for handicaps: we know neither the line taken nor the price paid,
        # so there is no honest way to settle one. Ungradeable rows are shown
        # on the site but never enter a win rate.
        "opt_gradeable": "INTEGER",
        # Derived from the match odds rather than quoted anywhere -- see
        # analytics._set_handicap_price. Kept in its own column so it can never
        # be mistaken for a price we actually took: the win rate uses it, the
        # profit maths deliberately does not.
        "opt_est_price": "REAL",
        # Graded separately from `result` -- a double chance also wins on a draw.
        "opt_result": "TEXT",
    },
}


def _migrate(conn):
    for table, columns in _MIGRATIONS.items():
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue  # table doesn't exist yet; SCHEMA just created it correctly
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_db():
    with _conn() as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()


def save_movement(summary: dict, detected_at: str) -> bool:
    """Log a market move, signal or not. One row per event+side, ever."""
    bet = summary.get("bet") or {}
    if not bet.get("old_price"):
        return False
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO movements
                (detected_at, fixture_id, sport_key, start_time, home_team, away_team,
                 outcome_id, outcome_name, stars, old_price, new_price, drop_pct,
                 down_count, books_count, had_entry, entry_price, was_signal, bucket,
                 best_left_price)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (detected_at, summary["fixture_id"], summary.get("sport_key"),
             summary.get("start_time"), summary.get("home_team"), summary.get("away_team"),
             bet["side"], bet["name"], summary.get("stars"),
             bet.get("old_price"), bet.get("new_price"), bet.get("drop_pct"),
             bet.get("down_count"), bet.get("books_count"),
             1 if summary.get("has_entry") else 0, bet.get("entry_price"),
             1 if summary.get("alertable") else 0, summary.get("funnel_bucket"),
             bet.get("best_left_price")),
        )
        conn.commit()
        return cur.rowcount > 0


def get_unresolved_movements(before_iso: str, limit: int = 300):
    with _conn() as conn:
        return conn.execute(
            "SELECT id, fixture_id, sport_key, home_team, away_team, outcome_id "
            "FROM movements WHERE resolved=0 AND start_time IS NOT NULL AND start_time < ? "
            "ORDER BY start_time ASC LIMIT ?", (before_iso, limit),
        ).fetchall()


def mark_movement_resolved(mid: int, result: str, resolved_at: str):
    with _conn() as conn:
        conn.execute("UPDATE movements SET resolved=1, result=?, resolved_at=? WHERE id=?",
                     (result, resolved_at, mid))
        conn.commit()


def movement_stats():
    """Flat-stake result of backing EVERY move at the pre-drop coefficient.

    Deliberately priced at old_price, which is frequently a price nobody was
    still offering by the time we saw the move. So this is an upper bound on
    the idea, not a claim about achievable money -- and the site says so next
    to the number.
    """
    with _conn() as conn:
        total = conn.execute("SELECT COUNT(*) n FROM movements").fetchone()["n"]
        with_entry = conn.execute("SELECT COUNT(*) n FROM movements WHERE was_signal=1").fetchone()["n"]
        resolved = conn.execute("SELECT COUNT(*) n FROM movements WHERE resolved=1").fetchone()["n"]
        hits = conn.execute("SELECT COUNT(*) n FROM movements WHERE result='hit'").fetchone()["n"]
        misses = conn.execute("SELECT COUNT(*) n FROM movements WHERE result='miss'").fetchone()["n"]
        graded = conn.execute(
            "SELECT result, old_price FROM movements "
            "WHERE resolved=1 AND result IN ('hit','miss') AND old_price IS NOT NULL"
        ).fetchall()
    profit = 0.0
    for g in graded:
        profit += FLAT_STAKE * (g["old_price"] - 1) if g["result"] == "hit" else -FLAT_STAKE
    staked = FLAT_STAKE * len(graded)
    return {
        "total": total, "with_entry": with_entry, "resolved": resolved,
        "hits": hits, "misses": misses,
        "win_rate": (hits / (hits + misses) * 100) if (hits + misses) else None,
        "graded_n": len(graded), "staked": staked, "profit": profit,
        "roi_pct": (profit / staked * 100) if staked else None,
        "stake": FLAT_STAKE,
    }


def recent_movements(limit: int = 30):
    with _conn() as conn:
        return conn.execute(
            "SELECT detected_at, fixture_id, sport_key, start_time, home_team, away_team, "
            "       outcome_name, stars, old_price, new_price, drop_pct, down_count, "
            "       books_count, had_entry, was_signal, entry_price, resolved, result "
            "FROM movements ORDER BY detected_at DESC LIMIT ?", (limit,),
        ).fetchall()


def save_funnel(counts: dict, at: str):
    """Persist one cycle's rejection breakdown."""
    if not counts:
        return
    with _conn() as conn:
        conn.execute(
            "INSERT INTO funnel_log (at, big_drop, thin_market, all_books_moved,"
            " entry_too_low, signals) VALUES (?,?,?,?,?,?)",
            (at, counts.get("big_drop", 0), counts.get("thin_market", 0),
             counts.get("all_books_moved", 0), counts.get("entry_too_low", 0),
             counts.get("signals", 0)),
        )
        conn.commit()


# A movements row written before the bucket column existed still has to land
# somewhere. Two of the three rejection reasons are recoverable from what was
# already stored -- a takeable price that never became a signal can only mean
# the evidence was too thin, and no takeable price at all means the market had
# already moved everywhere. "entry_too_low" is not recoverable and folds into
# the latter, which is the honest place for it: either way there was nothing
# worth taking.
_BUCKET_FALLBACK = (
    "COALESCE(bucket, CASE WHEN was_signal=1 THEN 'signal'"
    " WHEN had_entry=1 THEN 'thin_market' ELSE 'all_books_moved' END)"
)


def funnel_stats(hours: int = 24):
    """Where the last day's market moves stopped being signals.

    This is the answer to "у нас 22 движения, где они?" -- every one of them
    is in exactly one of these buckets, and the buckets sum to the total.

    2026-08-04: counted off the movements table, not summed from funnel_log.
    funnel_log holds one row per POLL, and a drop is measured against the
    price an hour ago -- so the same event was counted again on every cycle
    inside that hour and the block claimed six movements where the ledger
    listed three. movements is deduplicated on (fixture_id, outcome_id), so
    counting there makes the header and the list agree by construction rather
    than by luck. funnel_log is still written; it is the per-poll diagnostic.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    out = {"big_drop": 0, "thin_market": 0, "all_books_moved": 0,
           "entry_too_low": 0, "low_stars": 0, "off_band": 0, "too_far": 0,
           "signals": 0}
    key = {"signal": "signals", "thin_market": "thin_market",
           "all_books_moved": "all_books_moved", "entry_too_low": "entry_too_low",
           "low_stars": "low_stars", "off_band": "off_band", "too_far": "too_far"}
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT {_BUCKET_FALLBACK} AS b, COUNT(*) AS n FROM movements"
            " WHERE detected_at>=? GROUP BY b", (since,),
        ).fetchall()
    for r in rows:
        out["big_drop"] += r["n"]
        if r["b"] in key:
            out[key[r["b"]]] += r["n"]
    return out


def prune_snapshots(hours: int = None) -> int:
    """Drop raw price history older than the retention window.

    Snapshots are only needed for two things: diffing against the previous
    poll, and finding the closing line for CLV once a match is over. Both are
    satisfied by a few days of history. At a 3-minute cadence the table grows
    by millions of rows a day, and an oversized database makes the CI cache
    slow to save -- which would eventually cost us whole runs. Alerts are never
    touched: they ARE the track record.
    """
    hours = SNAPSHOT_RETENTION_HOURS if hours is None else hours
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        deleted = conn.execute(
            "DELETE FROM odds_snapshots WHERE fetched_at < ?", (cutoff,)
        ).rowcount
        conn.execute("DELETE FROM spike_events WHERE detected_at < ?", (cutoff,))
        conn.execute("DELETE FROM funnel_log WHERE at < ?", (cutoff,))
        conn.commit()
        return deleted or 0


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


def get_baseline_price(fixture_id, bookmaker, market_id, outcome_id, player_key,
                       since_iso, floor_iso=None):
    """The price this line was showing `since_iso` ago -- what a move is
    measured against.

    Prefers the newest snapshot at or before since_iso, so the comparison is
    genuinely "an hour ago" rather than "the oldest thing we happen to hold".
    When the line is younger than the window (a fixture that only just opened)
    it falls back to the oldest snapshot inside the window, because a line
    that appeared 20 minutes ago and has already moved 12% is exactly the kind
    of thing worth seeing. Returns None when the only history is older than
    floor_iso -- see BASELINE_MAX_AGE_MULT.
    """
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT fetched_at, price FROM odds_snapshots
            WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=?
              AND player_key=? AND fetched_at<=?
            ORDER BY fetched_at DESC LIMIT 1
            """,
            (fixture_id, bookmaker, market_id, outcome_id, player_key, since_iso),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT fetched_at, price FROM odds_snapshots
                WHERE fixture_id=? AND bookmaker=? AND market_id=? AND outcome_id=?
                  AND player_key=? AND fetched_at>?
                ORDER BY fetched_at ASC LIMIT 1
                """,
                (fixture_id, bookmaker, market_id, outcome_id, player_key, since_iso),
            ).fetchone()
    if row is None:
        return None
    if floor_iso and row["fetched_at"] < floor_iso:
        return None  # too stale to be a baseline -- that is drift, not a move
    return (row["fetched_at"], row["price"])


def next_rotation_offset(step: int, span: int) -> int:
    """Advance the wide-coverage cursor and return where this cycle starts.

    Kept in the meta table rather than derived from the clock so a skipped or
    doubled run can't make the sweep jump a league permanently.
    """
    if span <= 0 or step <= 0:
        return 0
    try:
        cur = int(get_meta("wide_rotation_offset") or 0)
    except (TypeError, ValueError):
        cur = 0
    cur %= span
    set_meta("wide_rotation_offset", str((cur + step) % span))
    return cur


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


def save_bet_alert(summary: dict, detected_at: str, poll_interval_minutes: int = None) -> bool:
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
    safe = summary.get("safe") or {}
    opt = summary.get("optimal") or {}
    with _conn() as conn:
        cur = conn.execute(
            """
            INSERT OR IGNORE INTO tracked_alerts
                (alert_type, kind, fixture_id, sport_key, start_time, home_team, away_team,
                 outcome_id, outcome_name, stars, down_count, books_count,
                 old_price, new_price, entry_price, entry_book,
                 market_id, player_key, bookmaker, label, direction,
                 alert_price, detected_at, resolved,
                 strategy, safe_market, safe_pick, safe_price, safe_book,
                 poll_interval_minutes,
                 opt_kind, opt_pick, opt_price, opt_book, opt_gradeable, opt_est_price)
            VALUES (?, 'prematch', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'down', ?, ?, 0,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?)
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
                summary.get("strategy"),
                safe.get("market"),
                safe.get("pick"),
                safe.get("price"),
                safe.get("book"),
                poll_interval_minutes,
                opt.get("kind"),
                opt.get("pick"),
                opt.get("price"),
                opt.get("book"),
                1 if opt.get("gradeable") else 0,
                opt.get("est_price"),
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
                   direction, alert_price, entry_price, old_price, new_price, detected_at,
                   opt_kind, opt_price, opt_gradeable
            FROM tracked_alerts
            WHERE resolved=0 AND start_time IS NOT NULL AND start_time < ?
            ORDER BY start_time ASC LIMIT ?
            """,
            (before_iso, limit),
        ).fetchall()
        return rows


def mark_resolved(alert_id: int, result: str, resolved_at: str, clv_pct: float = None,
                  clv_continued=None, opt_result: str = None):
    """Settle an alert. `opt_result` is stored separately because the optimal
    strategy may have placed a different bet on the same event -- a double
    chance also wins when the match ends level, so it cannot share the straight
    bet's verdict."""
    with _conn() as conn:
        conn.execute(
            """
            UPDATE tracked_alerts
            SET resolved=1, result=?, resolved_at=?, clv_pct=?, clv_continued=?, opt_result=?
            WHERE id=?
            """,
            (
                result,
                resolved_at,
                clv_pct,
                None if clv_continued is None else int(clv_continued),
                opt_result,
                alert_id,
            ),
        )
        conn.commit()


def _strategy_clause(strategy: str):
    """SQL fragment + params restricting rows to one strategy bucket.

    'aggressive' is every signal, so it adds nothing. 'optimal' is every signal
    the optimal line found a way into -- straight below the cut-off, double
    chance or handicap above it. Rows written before opt_kind existed fall back
    to their entry price so old history still lands somewhere sensible instead
    of vanishing.
    """
    if strategy != "optimal":
        return "", ()
    return (
        " AND (opt_kind IS NOT NULL OR (opt_kind IS NULL AND entry_price IS NOT NULL"
        " AND entry_price<=?))",
        (OPTIMAL_MAX_PRICE,),
    )


def _strategy_columns(strategy: str):
    """Which result and price columns describe THIS strategy's bet.

    The two lines no longer place the same bet: on a 4.00 pick the aggressive
    line backs the winner at 4.00 while the optimal line backs the double
    chance at ~1.90. Scoring both off `result` and `entry_price` would credit
    the optimal line with a bet it never made, so each strategy is settled and
    priced from its own columns.
    """
    if strategy == "optimal":
        # The price falls back to entry_price ONLY when the optimal line took
        # the identical bet. It used to fall back always, and that produced a
        # flatly false number: the Cocciaretto set handicap won, the fallback
        # paid it at the 5.70 moneyline, and the bank claimed +$940 from a bet
        # worth about 1.60. A handicap we never bought a price for now returns
        # NULL and drops out of the money entirely -- it still counts in the
        # win rate, which is exactly what we promised: "заходимость считаем,
        # прибыль нет: цену мы не выкупаем".
        return ("COALESCE(opt_result, result)",
                "CASE WHEN opt_kind='straight' THEN entry_price ELSE opt_price END")
    return "result", "entry_price"


def breakdown_stats(kind: str = "prematch"):
    """The same book, cut three ways: by discipline, by how big the drop was,
    and by confidence.

    Added 2026-08-08. The headline numbers had been hiding the only things in
    the ledger that actually differed. Cut by sport, tennis was running +5.1%
    CLV and football MINUS 4.6% -- opposite signs, averaged into one bland
    figure. Cut by drop size, the relationship ran BACKWARDS: 10-12% drops made
    +$2 184 while everything above 15% lost every single time, which is the
    fingerprint of news we do not have rather than of money we can follow.

    None of that is visible on a single average, and none of it can be acted on
    until it is on the page. Counts come with every row on purpose: most of
    these buckets are far too small to mean anything yet, and a percentage
    without its denominator is how a fluke gets promoted to a strategy.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT sport_key, stars, old_price, new_price, entry_price, "
            "       result, clv_pct "
            "FROM tracked_alerts WHERE kind=? AND result IN ('hit','miss')",
            (kind,),
        ).fetchall()

    def blank():
        return {"n": 0, "hits": 0, "profit": 0.0, "clv_sum": 0.0, "clv_n": 0}

    by_sport, by_drop, by_stars = {}, {}, {}
    for r in rows:
        price = r["entry_price"]
        drop = None
        if r["old_price"] and r["new_price"]:
            drop = (r["old_price"] - r["new_price"]) / r["old_price"] * 100
        buckets = [
            (by_sport, _sport_family(r["sport_key"])),
            (by_drop, "10–12%" if drop is None or drop < 12
             else "12–15%" if drop < 15 else "15%+"),
            (by_stars, f"{r['stars'] or 0}★"),
        ]
        for target, key in buckets:
            b = target.setdefault(key, blank())
            b["n"] += 1
            if r["result"] == "hit":
                b["hits"] += 1
                b["profit"] += FLAT_STAKE * ((price or 1) - 1)
            else:
                b["profit"] -= FLAT_STAKE
            if r["clv_pct"] is not None:
                b["clv_sum"] += r["clv_pct"] * 100
                b["clv_n"] += 1

    def finish(d):
        out = {}
        for k, b in d.items():
            out[k] = {
                "n": b["n"], "hits": b["hits"], "profit": b["profit"],
                "win_rate": (b["hits"] / b["n"] * 100) if b["n"] else None,
                "clv": (b["clv_sum"] / b["clv_n"]) if b["clv_n"] else None,
            }
        return dict(sorted(out.items(), key=lambda kv: -kv[1]["n"]))

    return {"by_sport": finish(by_sport), "by_drop": finish(by_drop),
            "by_stars": finish(by_stars), "graded": len(rows),
            "stake": FLAT_STAKE}


def save_sport_horizon(records, at: str):
    """Remember the nearest fixture we saw for each sport key.

    Cheap bookkeeping with an outsized payoff: it lets the rotation skip
    leagues that have nothing coming up, which both saves credits and -- far
    more importantly -- shortens the rotation lap so the remaining leagues are
    revisited often enough to be diffable at all.
    """
    nearest = {}
    for r in records:
        key, start = r.get("sport_key"), r.get("start_time")
        if not key or not start:
            continue
        cur = nearest.get(key)
        if cur is None or str(start) < cur:
            nearest[key] = str(start)
    if not nearest:
        return
    existing = json.loads(get_meta("sport_horizon") or "{}")
    existing.update({k: {"next": v, "seen": at} for k, v in nearest.items()})
    set_meta("sport_horizon", json.dumps(existing))


def sport_horizon() -> dict:
    """{sport_key: {"next": iso, "seen": iso}} from the last time each was fetched."""
    try:
        return json.loads(get_meta("sport_horizon") or "{}")
    except (ValueError, TypeError):
        return {}


def counterfactual_stats():
    """What the rules we DIDN'T adopt would have returned.

    This exists because of a specific temptation. On 2026-08-08 the ledger held
    23 settled bets, and cutting it by stars, by drop size and by sport all
    produced flattering splits. Tuning the filters until those 23 look good is
    not improvement, it is fitting the noise -- and the fit would be invisible
    afterwards, because the rejected bets simply stop being recorded.

    So instead of trusting the split, we keep scoring the roads not taken. The
    movements table logs EVERY drop we saw, including the ones the current
    rules refuse, together with the price that was actually on offer. That
    makes each rule below a live, running experiment rather than a one-off
    reading, and in a few hundred bets the answer will be worth something.

    Priced at entry_price, not at the pre-drop price the movements P&L uses:
    the question here is "would we have made money BETTING this rule", so it
    has to be the price a person could have taken.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT sport_key, stars, old_price, new_price, entry_price, "
            "       was_signal, result "
            "FROM movements WHERE resolved=1 AND result IN ('hit','miss') "
            "  AND entry_price IS NOT NULL"
        ).fetchall()
    return _score_rules(rows)


def capture_threshold_preview(pct_options=(50, 40, 30)):
    """How many MORE signals a softer entry rule would have produced.

    The entry has to give back at least ENTRY_MIN_CAPTURE_PCT of the drop, and
    that single rule is currently the biggest killer of signals -- most refused
    moves die there rather than on stars, price band or horizon. The rule is
    sound: without it we would announce "было 3.20" and send you to bet 2.40.
    The open question is whether half is the right share.

    This answers it from the ledger instead of by taste, by replaying the same
    floor arithmetic against the best price that was ACTUALLY still on offer
    when we said no. Counts only -- deliberately not money, because a bet we
    never made has no result, and pretending otherwise is how a loosened filter
    gets justified by a number it invented.

    Movements logged before 2026-08-09 have no stored refused price and are
    excluded rather than guessed at; `sample` says how many rows the answer
    actually rests on.
    """
    with _conn() as conn:
        rows = conn.execute(
            # Only moves refused BY THIS RULE. A move blocked for too few stars
            # or a price outside the band would still be blocked after
            # loosening the capture threshold, so counting it here would credit
            # the change with signals it does not produce -- exactly the kind of
            # flattering arithmetic this preview exists to prevent.
            "SELECT old_price, new_price, best_left_price "
            "FROM movements "
            "WHERE best_left_price IS NOT NULL AND old_price IS NOT NULL "
            "  AND new_price IS NOT NULL AND was_signal=0 "
            "  AND bucket='entry_too_low'"
        ).fetchall()

    out = []
    for pct in pct_options:
        extra = 0
        for r in rows:
            old, new, best = r["old_price"], r["new_price"], r["best_left_price"]
            floor = max(new * (1 + ENTRY_MIN_GAP_PCT / 100.0),
                        new + (old - new) * (pct / 100.0))
            ceiling = old * (1 + ENTRY_MAX_OVER_OLD_PCT / 100.0)
            if floor <= best <= ceiling:
                extra += 1
        out.append({"capture_pct": pct, "extra_signals": extra})
    return {"rules": out, "sample": len(rows)}


def _score_rules(rows):

    def drop_of(r):
        if not r["old_price"] or not r["new_price"]:
            return None
        return (r["old_price"] - r["new_price"]) / r["old_price"] * 100

    rules = [
        ("Как сейчас: три звезды, полоса, горизонт",
         lambda r: r["was_signal"] == 1),
        ("Если бы вернули две звезды",
         lambda r: (r["stars"] or 0) >= 2),
        ("Без падений свыше 15%",
         lambda r: (drop_of(r) or 0) < 15),
        ("Только умеренные падения 10–12%",
         lambda r: (drop_of(r) or 0) < 12),
        ("Только теннис",
         lambda r: (r["sport_key"] or "").startswith("tennis")),
        ("Всё подряд, что было чем взять",
         lambda _r: True),
    ]

    out = []
    for label, keep in rules:
        picked = [r for r in rows if keep(r)]
        hits = sum(1 for r in picked if r["result"] == "hit")
        profit = sum(FLAT_STAKE * (r["entry_price"] - 1) if r["result"] == "hit"
                     else -FLAT_STAKE for r in picked)
        out.append({
            "label": label, "n": len(picked), "hits": hits, "profit": profit,
            "win_rate": (hits / len(picked) * 100) if picked else None,
            "roi": (profit / (FLAT_STAKE * len(picked)) * 100) if picked else None,
        })
    return {"rules": out, "pool": len(rows), "stake": FLAT_STAKE}


def _sport_family(sport_key: str) -> str:
    key = (sport_key or "").lower()
    if key.startswith("soccer"):
        return "Футбол"
    if key.startswith("tennis"):
        return "Теннис"
    if key.startswith("esports"):
        return "Киберспорт"
    if key.startswith("table_tennis"):
        return "Наст. теннис"
    if key.startswith("basketball"):
        return "Баскетбол"
    return key.split("_")[0].title() or "—"


def alert_stats(kind: str = "prematch", strategy: str = "aggressive"):
    """Summary for the dashboard: how many alerts we've fired, how many are
    resolved, the win rate among resolved 1X2-style alerts, and the average
    Closing Line Value -- a more statistically robust "are we beating the
    bookmakers" signal than win/loss alone, since CLV measures whether the
    market kept moving the way we called it, independent of the final score.

    strategy: 'aggressive' counts every signal; 'optimal' counts only those at
    or below OPTIMAL_MAX_PRICE. Both are computed from the SAME logged rows,
    so the comparison is between two ways of playing one signal stream rather
    than between two unrelated samples.
    """
    sf, sp = _strategy_clause(strategy)
    res_col, price_col = _strategy_columns(strategy)
    # Handicap plays carry no price and no line, so they can never be settled.
    # They are counted in the signal total but excluded from anything that
    # claims to measure performance.
    # 2026-08-10: this clause used to gate the COUNTS as well as the money, and
    # that made the optimal card contradict the aggressive one -- "10 сыгравших"
    # on one, "За 5 сыгравших ставок" on the other, from the same signal stream.
    # Reported by the user, and he was right: both strategies bet the same
    # events, so the number of matches PLAYED cannot differ between them. Only
    # the money can, because a handicap we never bought a price for is
    # settleable from the score but not payable. So the clause now restricts
    # the bank alone; everything that answers "how many did we check" counts
    # every resolved signal.
    checkable = " AND opt_gradeable=1" if strategy == "optimal" else ""
    with _conn() as conn:
        k = (kind,) + sp

        def one(sql):
            return conn.execute(sql, k).fetchone()

        total = one(f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=?{sf}")["n"]
        unverifiable = one(
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=?{sf} AND opt_gradeable=0"
        )["n"] if strategy == "optimal" else 0
        resolved = one(
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND resolved=1{sf}"
        )["n"]
        hits = one(
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND {res_col}='hit'{sf}"
        )["n"]
        misses = one(
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND {res_col}='miss'{sf}"
        )["n"]
        clv_row = one(
            "SELECT AVG(clv_pct) AS avg_clv, "
            "SUM(CASE WHEN clv_continued=1 THEN 1 ELSE 0 END) AS clv_wins, "
            "SUM(CASE WHEN clv_continued IS NOT NULL THEN 1 ELSE 0 END) AS clv_n "
            f"FROM tracked_alerts WHERE kind=? AND clv_pct IS NOT NULL{sf}"
        )
        recent = conn.execute(
            "SELECT fixture_id, sport_key, home_team, away_team, outcome_name, stars, "
            f"       old_price, new_price, {price_col} AS entry_price, entry_book, "
            f"       {res_col} AS result, clv_pct, clv_continued, resolved_at, "
            "       opt_kind, opt_price, opt_est_price, opt_gradeable "
            f"FROM tracked_alerts WHERE kind=? AND resolved=1{sf} "
            "ORDER BY resolved_at DESC LIMIT 20", k
        ).fetchall()
        # Flat-stake profit/loss over every graded bet, priced at the entry THIS
        # strategy actually took. A win returns stake x (odds - 1), a loss costs
        # the stake. Bets graded 'n/a' are excluded rather than counted as
        # pushes -- we don't know what they did.
        graded = conn.execute(
            f"SELECT {res_col} AS result, {price_col} AS entry_price FROM tracked_alerts "
            f"WHERE kind=? AND resolved=1 AND {res_col} IN ('hit','miss') "
            f"AND {price_col} IS NOT NULL{sf}{checkable}",
            k,
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
            # Resolved bets that carry a price we could pay out on. Differs
            # from "resolved" only for the optimal line, where a handicap is
            # settleable from the score but was never bought.
            "priced_n": len(graded),
            "win_rate": (hits / (hits + misses) * 100) if (hits + misses) else None,
            "avg_clv_pct": clv_row["avg_clv"],
            "clv_continued_rate": (clv_row["clv_wins"] / clv_n * 100) if clv_n else None,
            "clv_n": clv_n,
            "kind": kind,
            "strategy": strategy,
            # Handicap entries: real recommendations, impossible to settle.
            "unverifiable": unverifiable,
            "max_price": OPTIMAL_MAX_PRICE if strategy == "optimal" else None,
            "stake": FLAT_STAKE,
            "graded_n": len(graded),
            "staked": staked,
            "profit": profit,
            "roi_pct": (profit / staked * 100) if staked else None,
            "recent": recent,
        }


def recent_bets(limit: int = 5, kind: str = "prematch", strategy: str = None,
                resolved_only: bool = False):
    """The last N bets we called, resolved or not. Unlike alert_stats()['recent']
    this deliberately includes pending ones -- right after a stats reset there
    are no finished matches yet, and a panel that renders empty for hours looks
    broken rather than new."""
    sf, sp = _strategy_clause(strategy)
    with _conn() as conn:
        return conn.execute(
            "SELECT fixture_id, sport_key, home_team, away_team, outcome_name, stars, "
            "       down_count, books_count, old_price, new_price, "
            "       entry_price, entry_book, start_time, detected_at, "
            "       resolved, result, clv_pct, resolved_at, "
            "       opt_kind, opt_pick, opt_price, opt_book, opt_gradeable, opt_result, opt_est_price "
            f"FROM tracked_alerts WHERE kind=?{sf}"
            + (" AND resolved=1 " if resolved_only else " ") +
            "ORDER BY detected_at DESC LIMIT ?",
            (kind,) + sp + (limit,),
        ).fetchall()


def save_live_score(fixture_id, sport_key, home_team, away_team,
                    home_score, away_score, completed, at):
    with _conn() as conn:
        conn.execute(
            "INSERT INTO live_scores (fixture_id, sport_key, home_team, away_team,"
            " home_score, away_score, completed, updated_at) VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(fixture_id) DO UPDATE SET home_score=excluded.home_score,"
            " away_score=excluded.away_score, completed=excluded.completed,"
            " updated_at=excluded.updated_at",
            (fixture_id, sport_key, home_team, away_team,
             home_score, away_score, 1 if completed else 0, at),
        )
        conn.commit()


def live_scores_map(max_age_minutes: int = 90) -> dict:
    """{fixture_id: row} for matches currently in play.

    Age-limited on purpose: a score we last refreshed two hours ago is not a
    live score, it is a stale one, and printing it next to "матч идёт" would
    be worse than printing nothing.
    """
    since = (datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            "SELECT fixture_id, home_score, away_score, completed, updated_at "
            "FROM live_scores WHERE updated_at>=?", (since,),
        ).fetchall()
    return {r["fixture_id"]: r for r in rows}


def final_scores_map(limit: int = 400) -> dict:
    """{fixture_id: row} for matches that have finished.

    Unlike live_scores_map this has no age limit: a final score does not go
    stale, and the track record needs to show what a match ended, however long
    ago it was played.
    """
    with _conn() as conn:
        rows = conn.execute(
            "SELECT fixture_id, home_score, away_score, completed, updated_at "
            "FROM live_scores WHERE completed=1 ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return {r["fixture_id"]: r for r in rows}


def inplay_fixtures(now_iso: str, limit: int = 60):
    """Events we are standing on whose match has started but not been graded.

    Union of the two tables that hold them: a signal we bet, and a movement we
    only logged. Both appear on the site with a "матч идёт" badge, so both need
    a score to put next to it.
    """
    with _conn() as conn:
        return conn.execute(
            """
            SELECT fixture_id, sport_key, home_team, away_team FROM (
                SELECT fixture_id, sport_key, home_team, away_team, start_time
                  FROM tracked_alerts WHERE resolved=0 AND start_time IS NOT NULL
                UNION
                SELECT fixture_id, sport_key, home_team, away_team, start_time
                  FROM movements WHERE resolved=0 AND start_time IS NOT NULL
            ) WHERE start_time <= ? ORDER BY start_time DESC LIMIT ?
            """,
            (now_iso, limit),
        ).fetchall()


def recent_signals(hours: int = 24, limit: int = 60, kind: str = "prematch"):
    """Every signal we called in the last N hours, finished or not.

    Added 2026-07-31. The feed used to render only the current poll's
    summaries -- a three-minute window -- so a signal sent an hour ago was
    nowhere on the page under the heading "Сигналы", even though it was in
    the database, in the bot and in the open-bets block. That reads as a lost
    signal. This is the list the word "сигналы" actually means to a reader.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        return conn.execute(
            """
            SELECT fixture_id, sport_key, home_team, away_team, outcome_id, outcome_name,
                   stars, down_count, books_count, old_price, new_price, entry_price,
                   entry_book, start_time, detected_at, strategy, resolved, result,
                   opt_kind, opt_pick, opt_price, opt_book, opt_gradeable, opt_est_price,
                   opt_result
            FROM tracked_alerts
            WHERE kind=? AND detected_at >= ?
            ORDER BY detected_at DESC LIMIT ?
            """,
            (kind, since, limit),
        ).fetchall()


def active_signals(limit: int = 40, kind: str = "prematch"):
    """Signals whose match hasn't kicked off yet -- the ones still live.

    The feed on the site shows what moved in the LAST poll, which is a
    three-minute window and is empty most of the time. That made the page look
    dead while several logged bets were sitting there waiting for their
    matches, which is the opposite of the truth. This is the list a reader
    actually means by "какие сигналы сейчас живые".
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        return conn.execute(
            """
            SELECT fixture_id, sport_key, home_team, away_team, outcome_name, stars,
                   down_count, books_count, old_price, new_price, entry_price,
                   entry_book, start_time, detected_at, strategy,
                   safe_market, safe_pick, safe_price,
                   opt_kind, opt_pick, opt_price, opt_book, opt_gradeable, opt_est_price
            FROM tracked_alerts
            WHERE kind=? AND resolved=0 AND start_time IS NOT NULL AND start_time > ?
            ORDER BY start_time ASC LIMIT ?
            """,
            (kind, now, limit),
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
            SELECT entry_book AS book, COUNT(*) AS n
            FROM tracked_alerts
            WHERE entry_book IS NOT NULL AND entry_book <> ''
            GROUP BY entry_book ORDER BY n DESC, book ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [{"book": r["book"], "n": r["n"]} for r in rows]


def export_ledger(limit: int = 5000):
    """Every logged signal as plain dicts, oldest first, for publication.

    This is deliberately the raw table with nothing removed or reordered:
    misses sit next to hits, unresolved rows are included, and the file can be
    downloaded and recounted by anyone. For an audience that assumes every
    betting site is fake, an ugly machine-readable file is worth more than any
    claim made on the page.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT detected_at, sport_key, start_time, home_team, away_team,
                   outcome_name, stars, down_count, books_count,
                   old_price, new_price, entry_price, entry_book,
                   strategy, safe_market, safe_pick, safe_price,
                   opt_kind, opt_pick, opt_price, opt_book, opt_gradeable, opt_result,
                   opt_est_price, poll_interval_minutes, resolved, result, clv_pct, resolved_at
            FROM tracked_alerts WHERE kind='prematch'
            ORDER BY detected_at ASC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def coverage_stats(hours: int = 24):
    """Honest scale-of-operation numbers for the header.

    Everything is counted from what actually landed in the database -- no
    estimates, no multipliers. Three things were wrong with the first version
    and are fixed here, because a header full of numbers that don't add up is
    worse than no header at all:

      * It always said "за 24 ч" even when the database was three hours old,
        so a freshly-started tracker looked like a dead one. `span_hours` now
        reports the window that genuinely has data, and the label follows it.
      * "Движений поймано" counted raw spike rows, so one event moving at six
        bookmakers read as six movements. It now counts distinct
        (event, outcome) pairs -- the number of actual market moves, which is
        also the number a reader assumes it is.
      * Nothing counted the signals themselves. That is the output of the
        whole pipeline, so it belongs in the header next to the inputs.

    `books` is deliberately measured over the window rather than over the last
    poll: bookmaker lists differ per sport, so a single cycle sees fewer books
    than the tracker actually covers.
    """
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS lines, COUNT(DISTINCT fixture_id) AS events, "
            "COUNT(DISTINCT bookmaker) AS books, COUNT(DISTINCT sport_key) AS sports, "
            "COUNT(DISTINCT fetched_at) AS cycles, MIN(fetched_at) AS first_at, "
            "MAX(fetched_at) AS last_at "
            "FROM odds_snapshots WHERE fetched_at>=?", (since,),
        ).fetchone()
        # Counted from the movements table, which is exactly the list the
        # "Движения" section on the site prints. It used to come from
        # spike_events, and the two disagreed -- the header claimed 22 moves
        # while the page could show none of them, which reads as invented.
        moves = conn.execute(
            "SELECT COUNT(*) AS n FROM movements WHERE detected_at>=?", (since,),
        ).fetchone()["n"]
        signals = conn.execute(
            "SELECT COUNT(*) AS n FROM tracked_alerts WHERE detected_at>=?", (since,),
        ).fetchone()["n"]

        # How long we have genuinely been observing, rounded up, capped at the
        # requested window. Used for the label so it can never overstate.
        span_hours = hours
        if row["first_at"]:
            try:
                first = datetime.fromisoformat(row["first_at"])
                if first.tzinfo is None:
                    first = first.replace(tzinfo=timezone.utc)
                observed = (now - first).total_seconds() / 3600.0
                span_hours = max(1, min(hours, int(observed + 0.5)))
            except ValueError:
                pass

        return {
            "hours": hours,
            "span_hours": span_hours,
            "lines": row["lines"] or 0,
            "events": row["events"] or 0,
            "books": row["books"] or 0,
            "sports": row["sports"] or 0,
            "cycles": row["cycles"] or 0,
            "moves": moves or 0,
            "signals": signals or 0,
            "first_at": row["first_at"],
            "last_at": row["last_at"],
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


# ---------------------------------------------------------------------------
# Polymarket
# ---------------------------------------------------------------------------

def save_pm_quote(row: dict) -> None:
    """One look at Polymarket for one signal. Never raises."""
    try:
        with _conn() as conn:
            conn.execute(
                """
                INSERT INTO pm_quotes
                    (checked_at, fixture_id, outcome_name, sport_key, start_time,
                     lead_hours, matched, reason, event_title, event_slug, token_id,
                     entry_price, need_coef, best_coef, avg_coef, exec_stake_usd,
                     fits_target, edge_pct, take, leg, source, question, means,
                     markets_total, pm_lag, down_count, books_count)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (row.get("checked_at"), row.get("fixture_id"), row.get("outcome_name"),
                 row.get("sport_key"), row.get("start_time"), row.get("lead_hours"),
                 1 if row.get("matched") else 0, row.get("reason"),
                 row.get("event_title"), row.get("event_slug"), row.get("token_id"),
                 row.get("entry_price"), row.get("need_coef"), row.get("best_coef"),
                 row.get("avg_coef"), row.get("exec_stake_usd"),
                 1 if row.get("fits_target") else 0, row.get("edge_pct"),
                 1 if row.get("take") else 0, row.get("leg") or "aggressive",
                 row.get("source") or "signal", row.get("question"),
                 row.get("means"), row.get("markets_total"), row.get("pm_lag"),
                 row.get("down_count"), row.get("books_count")),
            )
            conn.commit()
    except sqlite3.Error:
        pass


def pm_candidates(limit: int = 250):
    """Everything worth asking Polymarket about, not just what we published.

    Widened 20.08.2026 on instruction: bet "от сигналов... а так же из
    движений, которые мы видим и из той аналитики, которую мы можем сделать
    исходя из этих движений".

    The reasoning is sound and worth stating. Our filters -- three books
    minimum, a price band, an entry that still captures half the move -- were
    tuned for BOOKMAKERS, where a bad entry means a limited or voided bet. On
    an order book none of that applies: there is a visible price and a visible
    depth, and a move we refused to publish can still be sitting in front of a
    Polymarket line that has not woken up. Refusing to even LOOK at it because
    of a rule written for a different venue would be leaving money on the table
    for a reason that no longer holds.

    Signals come first and keep their optimal-leg price; movements follow with
    whatever price was still takeable when we saw them.
    """
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        rows = [dict(r) | {"source": "signal"} for r in conn.execute(
            """SELECT fixture_id, sport_key, home_team, away_team, outcome_name,
                      stars, entry_price, opt_price, start_time,
                      old_price, new_price, down_count, books_count
               FROM tracked_alerts
               WHERE kind='prematch' AND resolved=0
                 AND start_time IS NOT NULL AND start_time > ?
               ORDER BY start_time ASC LIMIT ?""",
            (now, limit)).fetchall()]
        seen = {(r["fixture_id"], r["outcome_name"]) for r in rows}
        for r in conn.execute(
            """SELECT fixture_id, sport_key, home_team, away_team, outcome_name,
                      stars, entry_price, best_left_price, start_time,
                      old_price, new_price, down_count, books_count
               FROM movements
               WHERE resolved=0 AND start_time IS NOT NULL AND start_time > ?
               ORDER BY start_time ASC LIMIT ?""",
                (now, limit)):
            key = (r["fixture_id"], r["outcome_name"])
            if key in seen:
                continue
            seen.add(key)
            d = dict(r) | {"source": "movement", "opt_price": None}
            # A rejected movement has no logged entry, but it does have the best
            # price still standing when we looked -- which is exactly the number
            # the rule needs.
            d["entry_price"] = d.get("entry_price") or d.get("best_left_price")
            if d["entry_price"]:
                rows.append(d)
    return rows


def pm_last_check(fixture_id: str, outcome_name: str):
    """When we last looked, and whether we had found the market by then."""
    with _conn() as conn:
        r = conn.execute(
            """SELECT checked_at, MAX(matched) AS matched FROM pm_quotes
               WHERE fixture_id=? AND outcome_name=?
               GROUP BY checked_at ORDER BY checked_at DESC LIMIT 1""",
            (fixture_id, outcome_name),
        ).fetchone()
    return (r["checked_at"], bool(r["matched"])) if r else (None, False)


def pm_best(fixture_id: str, outcome_name: str):
    """The best look we have ever had at this signal -- the moment to have bet."""
    with _conn() as conn:
        return conn.execute(
            """SELECT * FROM pm_quotes
               WHERE fixture_id=? AND outcome_name=? AND take=1
               ORDER BY edge_pct DESC LIMIT 1""",
            (fixture_id, outcome_name),
        ).fetchone()


def pm_opportunities(limit: int = 60):
    """Signals where Polymarket has, at any point, beaten the bookmaker."""
    with _conn() as conn:
        return conn.execute(
            """
            SELECT fixture_id, outcome_name, sport_key, start_time,
                   MAX(edge_pct) AS best_edge,
                   MAX(exec_stake_usd) AS best_stake,
                   MIN(lead_hours) AS closest_look,
                   COUNT(*) AS looks,
                   MAX(event_title) AS event_title,
                   MAX(event_slug) AS event_slug,
                   MAX(entry_price) AS entry_price,
                   MAX(avg_coef) AS avg_coef
            FROM pm_quotes WHERE take=1
            GROUP BY fixture_id, outcome_name
            ORDER BY start_time DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def pm_stats(hours: int = 24 * 30) -> dict:
    """Coverage and edge, counted rather than assumed.

    The two numbers that decide whether this whole direction works: what share
    of our signals exist on Polymarket at all, and on what share of those the
    price there ever beat the bookmaker by the required margin.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        sig = conn.execute(
            """SELECT fixture_id, outcome_name,
                      MAX(matched) AS ever_matched,
                      MAX(take) AS ever_take,
                      MAX(edge_pct) AS best_edge,
                      MAX(exec_stake_usd) AS best_stake,
                      COUNT(*) AS looks
               FROM pm_quotes WHERE checked_at >= ?
               GROUP BY fixture_id, outcome_name""",
            (since,),
        ).fetchall()
    n = len(sig)
    matched = sum(1 for r in sig if r["ever_matched"])
    took = sum(1 for r in sig if r["ever_take"])
    edges = [r["best_edge"] for r in sig if r["ever_take"] and r["best_edge"] is not None]
    stakes = [r["best_stake"] for r in sig if r["ever_take"] and r["best_stake"]]
    return {
        "signals": n,
        "matched": matched,
        "match_pct": round(matched / n * 100, 1) if n else 0.0,
        "opportunities": took,
        "take_pct": round(took / matched * 100, 1) if matched else 0.0,
        "looks": sum(r["looks"] for r in sig),
        "avg_edge_pct": round(sum(edges) / len(edges), 2) if edges else None,
        "max_edge_pct": round(max(edges), 2) if edges else None,
        "avg_stake_usd": round(sum(stakes) / len(stakes), 2) if stakes else None,
        "full_size": sum(1 for r in sig if r["ever_take"]
                         and (r["best_stake"] or 0) >= POLYMARKET_TARGET_STAKE - 0.01),
    }


def pm_counterfactual():
    """«Что бы дали другие правила» — но на Polymarket, а не на конторах.

    Развёрнуто сюда 20.08 по прямой просьбе: «эта вся вкладка у нас по сути
    может работать... очень важную аналитику теперь только по полику, по бк уже
    её можешь не вести».

    Считается по журналу КОТИРОВОК, а не по ставкам. Каждый открытый сигнал
    опрашивается на Polymarket десятки раз от срабатывания до стартового
    свистка, и каждый взгляд лежит строкой. Поэтому здесь можно спросить то,
    чего нельзя спросить про конторы: а если бы порог был 3%? а если бы мы
    ждали последнего часа? а если бы брали только полный размер?

    Важно: деньги считаются по ФАКТИЧЕСКОМУ размеру и ФАКТИЧЕСКОМУ среднему
    коэффициенту, а не по флэту. На Polymarket размер определяется стаканом, и
    флэт здесь врал бы: сделка на $30 и сделка на $200 — не одна ставка.

    «Первый подходящий» против «лучшего за всё время» — не academic: первый
    отвечает на «что бы мы взяли, если бы жали сразу», второй на «сколько
    стоило подождать». Разница между строками и есть цена терпения.
    """
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.result AS result, a.stars AS base_stars
            FROM pm_quotes q
            JOIN tracked_alerts a
              ON a.fixture_id = q.fixture_id
             AND a.outcome_name = q.outcome_name
             AND a.kind = 'prematch'
            WHERE q.take = 1 AND a.resolved = 1 AND a.result IN ('hit','miss')
            ORDER BY q.checked_at ASC
            """
        ).fetchall()

    by_sig: dict[tuple, list] = {}
    for r in rows:
        by_sig.setdefault((r["fixture_id"], r["outcome_name"]), []).append(r)

    def score(label, keep, choose="first"):
        n = hits = 0
        staked = profit = 0.0
        for looks in by_sig.values():
            ok = [r for r in looks if keep(r)]
            if not ok:
                continue
            r = ok[0] if choose == "first" else max(ok, key=lambda x: x["edge_pct"] or 0)
            size = r["exec_stake_usd"] or 0.0
            coef = r["avg_coef"] or 0.0
            if size <= 0 or coef <= 1.0:
                continue
            n += 1
            staked += size
            if r["result"] == "hit":
                hits += 1
                profit += size * (coef - 1.0)
            else:
                profit -= size
        return {
            "label": label, "n": n, "hits": hits, "profit": profit,
            "staked": staked,
            "win_rate": (hits / n * 100) if n else None,
            "roi": (profit / staked * 100) if staked else None,
        }

    full = POLYMARKET_TARGET_STAKE - 0.01
    rules = [
        (f"Как сейчас: зазор от {POLYMARKET_MIN_EDGE_PCT:g}%, берём сразу",
         lambda r: True, "first"),
        (f"То же, но ждём лучшую цену до старта",
         lambda r: True, "best"),
        ("Порог мягче: зазор от 3%",
         lambda r: (r["edge_pct"] or 0) >= 3, "first"),
        ("Порог жёстче: зазор от 8%",
         lambda r: (r["edge_pct"] or 0) >= 8, "first"),
        ("Только зазор от 12%",
         lambda r: (r["edge_pct"] or 0) >= 12, "first"),
        ("Только когда влезает полный размер",
         lambda r: (r["exec_stake_usd"] or 0) >= full, "first"),
        ("Только в последние 3 часа до старта",
         lambda r: (r["lead_hours"] or 99) <= 3, "first"),
        ("Только заранее, дальше 12 часов",
         lambda r: (r["lead_hours"] or 0) > 12, "first"),
        ("Только теннис",
         lambda r: (r["sport_key"] or "").startswith("tennis"), "first"),
        ("Только футбол",
         lambda r: (r["sport_key"] or "").startswith("soccer"), "first"),
        ("Только под наш сигнал в 3★ и выше",
         lambda r: (r["base_stars"] or 0) >= 3, "first"),
    ]
    return {
        "rules": [score(l, k, c) for l, k, c in rules],
        "pool": len(by_sig),
        "looks": len(rows),
        "min_edge": POLYMARKET_MIN_EDGE_PCT,
        "target": POLYMARKET_TARGET_STAKE,
    }


def pm_live_feed(max_age_minutes: int = 30):
    """The machine-readable answer to "what should a bot do right now".

    Written 20.08.2026 because Vladislav has a Polymarket bot that is already
    registered, verified and trading, and wants it acting on our signals rather
    than on nothing. The clean way to join two systems is a contract, not a
    merge: we publish what we believe, his bot decides what to do about it.

    One row per signal that, at its most recent look, cleared the rule. Stale
    looks are excluded on purpose -- an order book quote from an hour ago is
    not an offer, and a feed that serves one invites a bot to take a price
    that is gone. If the latest look did not clear, the signal is simply
    absent: silence means "not now", never "no data".
    """
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=max_age_minutes)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT q.*, a.stars AS base_stars, a.entry_book AS entry_book,
                   a.home_team AS home_team, a.away_team AS away_team
            FROM pm_quotes q
            JOIN tracked_alerts a
              ON a.fixture_id = q.fixture_id
             AND a.outcome_name = q.outcome_name
             AND a.kind = 'prematch'
            WHERE q.checked_at >= ?
              AND q.id IN (
                  SELECT MAX(id) FROM pm_quotes
                  GROUP BY fixture_id, outcome_name
              )
              AND q.take = 1
              AND a.resolved = 0
              AND q.start_time > ?
            ORDER BY q.edge_pct DESC
            """,
            (cutoff, now.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


def pm_settled(limit: int = 500):
    """Наши сделки на Polymarket, у которых матч уже сыгран.

    Одна строка на (событие, исход, нога) — берётся ЛУЧШИЙ прошедший порог
    взгляд, потому что именно его бот и мог взять: строка живёт в фиде, пока
    зазор открыт, и бот заходит в неё один раз.
    """
    with _conn() as conn:
        return conn.execute(
            """
            SELECT q.fixture_id, q.outcome_name, q.leg, q.source, q.sport_key,
                   q.start_time, q.lead_hours, q.event_title, q.event_slug,
                   q.entry_price, q.avg_coef, q.exec_stake_usd, q.edge_pct,
                   q.fits_target, q.means, q.pm_lag, q.down_count, q.books_count,
                   a.result AS result, a.stars AS base_stars,
                   a.home_team AS home_team, a.away_team AS away_team,
                   a.opt_result AS opt_result
            FROM pm_quotes q
            JOIN tracked_alerts a
              ON a.fixture_id = q.fixture_id AND a.outcome_name = q.outcome_name
             AND a.kind = 'prematch'
            WHERE q.take = 1 AND a.resolved = 1
              AND q.id IN (
                  SELECT id FROM pm_quotes p2
                  WHERE p2.fixture_id = q.fixture_id
                    AND p2.outcome_name = q.outcome_name
                    AND p2.leg = q.leg AND p2.take = 1
                  ORDER BY p2.edge_pct DESC LIMIT 1)
            ORDER BY q.start_time DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()


def _pm_outcome(row) -> str:
    """Which verdict applies to this leg.

    The aggressive leg is the straight win, so it takes the signal's own
    result. The optimal leg is the double chance, which wins on a draw too --
    that is exactly what opt_result already records for the bookmaker version
    of the same bet, so it is reused rather than re-derived.
    """
    if row["leg"] == "optimal" and row["opt_result"] in ("hit", "miss"):
        return row["opt_result"]
    return row["result"]


def pm_results() -> dict:
    """Deньги по Polymarket: по ногам, по звёздам и целиком.

    Считается по ФАКТИЧЕСКОМУ исполнимому размеру и фактическому среднему
    коэффициенту, а не по флэту. На ордербуке размер определяет стакан, и
    сделка на $30 не равна сделке на $200 -- усреднять их одним флэтом значило
    бы придумать доходность, которой не было.
    """
    import polymarket
    rows = [r for r in pm_settled() if _pm_outcome(r) in ("hit", "miss")]

    def agg(sel):
        n = len(sel)
        hits = sum(1 for r in sel if _pm_outcome(r) == "hit")
        staked = sum(r["exec_stake_usd"] or 0 for r in sel)
        profit = sum((r["exec_stake_usd"] or 0) * ((r["avg_coef"] or 1) - 1)
                     if _pm_outcome(r) == "hit" else -(r["exec_stake_usd"] or 0)
                     for r in sel)
        edges = [r["edge_pct"] for r in sel if r["edge_pct"] is not None]
        return {
            "n": n, "hits": hits, "staked": round(staked, 2),
            "profit": round(profit, 2),
            "win_rate": round(hits / n * 100, 1) if n else None,
            "roi": round(profit / staked * 100, 1) if staked else None,
            "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
        }

    by_stars = {}
    for r in rows:
        k = polymarket.pm_stars(r["pm_lag"], r["down_count"] or 0,
                                r["books_count"] or 0, r["exec_stake_usd"] or 0,
                                edge_pct=r["edge_pct"])
        by_stars.setdefault(k, []).append(r)

    return {
        "total": agg(rows),
        "aggressive": agg([r for r in rows if r["leg"] == "aggressive"]),
        "optimal": agg([r for r in rows if r["leg"] == "optimal"]),
        "by_stars": {k: agg(v) for k, v in sorted(by_stars.items(), reverse=True)},
        "by_source": {s: agg([r for r in rows if r["source"] == s])
                      for s in ("signal", "movement")},
        "pending": len([r for r in pm_settled() if _pm_outcome(r) not in ("hit", "miss")]),
    }


def pm_coverage_by_sport(hours: int = 24 * 30):
    """Где Polymarket вообще нас котирует. Считаем, а не предполагаем."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        rows = conn.execute(
            """SELECT sport_key,
                      COUNT(DISTINCT fixture_id || '|' || outcome_name) AS sigs,
                      SUM(matched) AS matched_looks,
                      MAX(take) AS any_take
               FROM pm_quotes WHERE checked_at >= ? AND sport_key IS NOT NULL
               GROUP BY sport_key""", (since,)).fetchall()
        seen = conn.execute(
            """SELECT sport_key,
                      COUNT(DISTINCT CASE WHEN matched=1
                            THEN fixture_id || '|' || outcome_name END) AS matched,
                      COUNT(DISTINCT fixture_id || '|' || outcome_name) AS total,
                      COUNT(DISTINCT CASE WHEN take=1
                            THEN fixture_id || '|' || outcome_name END) AS took
               FROM pm_quotes WHERE checked_at >= ? AND sport_key IS NOT NULL
               GROUP BY sport_key ORDER BY total DESC""", (since,)).fetchall()
    return [dict(r) for r in seen]


def pm_alert_is_new(fixture_id: str, outcome_name: str, leg: str) -> bool:
    """Звонить ли по этому зазору. Один раз на (событие, исход, ногу).

    Без этого красное уведомление приходило бы каждые пять минут, пока зазор
    открыт, и красный цвет перестал бы что-либо значить к вечеру первого дня.
    Ключ ровно тот же, по которому дедуплицируется бот, — чтобы «нам позвонили»
    и «бот вошёл» означали одно и то же событие, а не два разных.
    """
    key = f"pm_alert:{fixture_id}|{outcome_name}|{leg}"
    if get_meta(key):
        return False
    set_meta(key, datetime.now(timezone.utc).isoformat())
    return True


def pm_gap_history(limit: int = 300):
    """Жизнь каждого зазора: когда открылся, где был лучшим, когда закрылся.

    Добавлено 20.08.2026 по прямой просьбе — «собирай историю зазоров, веди по
    ней детальную статистику чтобы мы потом от неё оттолкнулись».

    Одна строка на (событие, исход, ногу). Мы смотрим на каждый открытый сигнал
    снова и снова до самого стартового свистка, и все взгляды лежат в журнале —
    значит можно спросить не только «был ли зазор», но и то, чего раньше нельзя
    было спросить ни у кого:

      сколько живёт зазор -- от первого появления до последнего взгляда, когда
        он ещё держался. От этого зависит, обязан ли бот быть быстрым или может
        быть ленивым, а это уже вопрос про инфраструктуру, а не про ставки;
      где он был лучшим -- за сколько часов до старта площадка отставала
        сильнее всего. Если окажется, что лучший момент устойчиво один и тот
        же, вход можно ждать, а не хватать первый попавшийся;
      закрылся сам или мы просто перестали смотреть -- разные вещи, и путать
        их значит выдумывать себе упущенную выгоду.
    """
    with _conn() as conn:
        rows = conn.execute(
            """SELECT fixture_id, outcome_name, leg, sport_key, source,
                      start_time, checked_at, lead_hours, take, edge_pct,
                      pm_lag, exec_stake_usd, avg_coef, entry_price,
                      event_title, down_count, books_count
               FROM pm_quotes
               WHERE matched = 1
               ORDER BY fixture_id, outcome_name, leg, checked_at ASC"""
        ).fetchall()

    groups: dict[tuple, list] = {}
    for r in rows:
        groups.setdefault((r["fixture_id"], r["outcome_name"], r["leg"]), []).append(r)

    out = []
    for (fid, pick, leg), looks in groups.items():
        takes = [r for r in looks if r["take"]]
        if not takes:
            continue
        best = max(takes, key=lambda r: r["edge_pct"] or -999)
        first, last = takes[0], takes[-1]
        # Жил ли зазор до старта или закрылся раньше: если после последнего
        # прошедшего взгляда были ещё взгляды, значит он реально закрылся, а не
        # просто кончились наблюдения.
        closed = looks[-1]["checked_at"] != last["checked_at"]
        try:
            life = (datetime.fromisoformat(last["checked_at"])
                    - datetime.fromisoformat(first["checked_at"])).total_seconds() / 60
        except (TypeError, ValueError):
            life = None
        out.append({
            "fixture_id": fid, "pick": pick, "leg": leg,
            "sport_key": looks[0]["sport_key"], "source": looks[0]["source"],
            "event_title": looks[0]["event_title"],
            "start_time": looks[0]["start_time"],
            "looks": len(looks), "takes": len(takes),
            "first_lead_h": first["lead_hours"], "first_edge": first["edge_pct"],
            "best_lead_h": best["lead_hours"], "best_edge": best["edge_pct"],
            "best_lag": best["pm_lag"], "best_stake": best["exec_stake_usd"],
            "last_lead_h": last["lead_hours"],
            "life_minutes": round(life, 1) if life is not None else None,
            "closed_before_start": closed,
            "entry_price": first["entry_price"],
            "down_count": best["down_count"], "books_count": best["books_count"],
        })
    out.sort(key=lambda d: d["start_time"] or "", reverse=True)
    return out[:limit]


# Часовые корзины до старта. Границы не круглые ради красоты: рынок Polymarket
# выставляется примерно за трое суток, наши сигналы приходят за 26-44 часа, а
# последние три часа -- это когда линия там оживает. Корзины нарезаны так,
# чтобы эти три режима не смешивались в одну кашу.
LEAD_BUCKETS = ((36, 999, "больше 36 ч"), (12, 36, "12–36 ч"),
                (3, 12, "3–12 ч"), (1, 3, "1–3 ч"), (0, 1, "меньше часа"))


def pm_lag_profile():
    """Насколько Polymarket отстаёт в зависимости от того, за сколько до старта.

    Это и есть карта нашего эджа во времени. Если отставание устойчиво больше
    вдали от матча -- входить надо рано и ждать не имеет смысла. Если наоборот,
    ближе к старту -- значит площадка просыпается поздно и лучший вход впереди.
    Без этой картинки выбор момента входа остаётся вкусовщиной.
    """
    with _conn() as conn:
        rows = conn.execute(
            """SELECT lead_hours, pm_lag, take, edge_pct, exec_stake_usd,
                      fixture_id, outcome_name, leg
               FROM pm_quotes WHERE matched=1 AND pm_lag IS NOT NULL"""
        ).fetchall()
    out = []
    for lo, hi, label in LEAD_BUCKETS:
        sel = [r for r in rows if r["lead_hours"] is not None
               and lo <= r["lead_hours"] < hi]
        if not sel:
            out.append({"label": label, "n": 0})
            continue
        lags = sorted(r["pm_lag"] for r in sel)
        mid = len(lags) // 2
        med = lags[mid] if len(lags) % 2 else (lags[mid - 1] + lags[mid]) / 2
        takes = [r for r in sel if r["take"]]
        edges = [r["edge_pct"] for r in takes if r["edge_pct"] is not None]
        # ЧИСЛО СОБЫТИЙ, А НЕ ЧИСЛО ВЗГЛЯДОВ. Разница здесь не косметическая.
        # Мы смотрим на каждый открытый сигнал заново каждые пять минут, и в
        # корзине «3-12 ч» на 21.08 лежало 127 взглядов -- но всего на ДВА
        # события. Медиана по взглядам выглядит как выборка из 127 наблюдений,
        # хотя это одно и то же событие, посчитанное шестьдесят раз. Решать по
        # такой цифре -- значит принять уверенность за знание. Поэтому наружу
        # идут оба числа, и «событий» стоит первым.
        events = {(r["fixture_id"], r["outcome_name"], r["leg"]) for r in sel}
        take_events = {(r["fixture_id"], r["outcome_name"], r["leg"]) for r in takes}
        out.append({
            "label": label,
            "events": len(events),
            "looks": len(sel),
            "n": len(events),          # то, на что смотрят -- события
            "median_lag": round(med, 3),
            "share_behind": round(sum(1 for r in sel if r["pm_lag"] >= 0.5)
                                  / len(sel) * 100, 1),
            "takes": len(take_events),
            "take_pct": round(len(take_events) / len(events) * 100, 1),
            "avg_edge": round(sum(edges) / len(edges), 2) if edges else None,
        })
    return out


def pm_gap_summary() -> dict:
    """Свод по истории зазоров — то, от чего можно оттолкнуться."""
    h = pm_gap_history()
    if not h:
        return {"gaps": 0, "profile": pm_lag_profile()}
    lives = [g["life_minutes"] for g in h if g["life_minutes"] is not None]
    lives.sort()
    best_leads = [g["best_lead_h"] for g in h if g["best_lead_h"] is not None]
    return {
        "gaps": len(h),
        "median_life_min": (lives[len(lives) // 2] if lives else None),
        "closed_before_start": sum(1 for g in h if g["closed_before_start"]),
        "median_best_lead_h": (sorted(best_leads)[len(best_leads) // 2]
                               if best_leads else None),
        "avg_best_edge": round(sum(g["best_edge"] or 0 for g in h) / len(h), 2),
        "full_size": sum(1 for g in h
                         if (g["best_stake"] or 0) >= POLYMARKET_TARGET_STAKE - 0.01),
        "by_leg": {leg: sum(1 for g in h if g["leg"] == leg)
                   for leg in ("aggressive", "optimal")},
        "profile": pm_lag_profile(),
    }
