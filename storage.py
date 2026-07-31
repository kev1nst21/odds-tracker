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
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

from config import (
    DB_PATH,
    FLAT_STAKE,
    OPTIMAL_MAX_PRICE,
    SNAPSHOT_RETENTION_HOURS,
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


def funnel_stats(hours: int = 24):
    """Where the last day's market moves stopped being signals.

    This is the answer to "у нас 22 движения, где они?" -- every one of them
    is in exactly one of these buckets, and the buckets sum to the total.
    """
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with _conn() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(big_drop),0) AS big_drop,"
            " COALESCE(SUM(thin_market),0) AS thin_market,"
            " COALESCE(SUM(all_books_moved),0) AS all_books_moved,"
            " COALESCE(SUM(entry_too_low),0) AS entry_too_low,"
            " COALESCE(SUM(signals),0) AS signals"
            " FROM funnel_log WHERE at>=?", (since,),
        ).fetchone()
        return dict(row) if row else {}


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
        return "COALESCE(opt_result, result)", "COALESCE(opt_price, entry_price)"
    return "result", "entry_price"


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
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND resolved=1{sf}{checkable}"
        )["n"]
        hits = one(
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND {res_col}='hit'{sf}{checkable}"
        )["n"]
        misses = one(
            f"SELECT COUNT(*) AS n FROM tracked_alerts WHERE kind=? AND {res_col}='miss'{sf}{checkable}"
        )["n"]
        clv_row = one(
            "SELECT AVG(clv_pct) AS avg_clv, "
            "SUM(CASE WHEN clv_continued=1 THEN 1 ELSE 0 END) AS clv_wins, "
            "SUM(CASE WHEN clv_continued IS NOT NULL THEN 1 ELSE 0 END) AS clv_n "
            f"FROM tracked_alerts WHERE kind=? AND clv_pct IS NOT NULL{sf}{checkable}"
        )
        recent = conn.execute(
            "SELECT fixture_id, home_team, away_team, outcome_name, stars, "
            f"       old_price, new_price, {price_col} AS entry_price, entry_book, "
            f"       {res_col} AS result, clv_pct, clv_continued, resolved_at "
            f"FROM tracked_alerts WHERE kind=? AND resolved=1{sf}{checkable} "
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


def recent_bets(limit: int = 5, kind: str = "prematch", strategy: str = None):
    """The last N bets we called, resolved or not. Unlike alert_stats()['recent']
    this deliberately includes pending ones -- right after a stats reset there
    are no finished matches yet, and a panel that renders empty for hours looks
    broken rather than new."""
    sf, sp = _strategy_clause(strategy)
    with _conn() as conn:
        return conn.execute(
            "SELECT fixture_id, home_team, away_team, outcome_name, stars, "
            "       down_count, books_count, old_price, new_price, "
            "       entry_price, entry_book, start_time, detected_at, "
            "       resolved, result, clv_pct, resolved_at, "
            "       opt_kind, opt_pick, opt_price, opt_book, opt_gradeable, opt_result, opt_est_price "
            f"FROM tracked_alerts WHERE kind=?{sf} "
            "ORDER BY detected_at DESC LIMIT ?",
            (kind,) + sp + (limit,),
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
        moves = conn.execute(
            "SELECT COUNT(*) AS n FROM ("
            "  SELECT DISTINCT fixture_id, outcome_id FROM spike_events"
            "  WHERE detected_at>=? AND direction='down')", (since,),
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
