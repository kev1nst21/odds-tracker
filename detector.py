"""Diffs a fresh batch of odds against the last stored price for each line.

Returns two things, deliberately separated:

  * movements -- EVERY line that changed at all since the previous poll, however
    slightly. These are not alerts; they exist so analytics can count how many
    bookmakers agree on a direction. Breadth of agreement is what separates a
    real informed-money move ("steam") from one book fixing a typo, and you
    can't measure breadth if you only keep the big jumps.
  * spikes -- the subset that moved at least SPIKE_THRESHOLD_PCT. These are
    what get recorded as tracked alerts and scored later for win rate / CLV.

Also flags "cascades": repeated same-direction spikes on one line inside
CASCADE_WINDOW_MINUTES.
"""
from datetime import datetime, timedelta

from config import (
    SPIKE_THRESHOLD_PCT,
    MIN_DRIFT_PCT,
    ASIAN_SHARP_BOOKMAKERS,
    BASELINE_WINDOW_MINUTES,
    BASELINE_MAX_AGE_MINUTES,
    BASELINE_ABSOLUTE_MAX_MINUTES,
    POLL_INTERVAL_MINUTES,
    CASCADE_WINDOW_MINUTES,
    EXCHANGE_BOOKMAKERS,
    MAX_SIGNAL_PRICE,
    MIN_SIGNAL_PRICE,
    EXCLUDE_DRAW,
)
import storage


def _window_start_iso(fetched_at: str, minutes: int) -> str:
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return fetched_at
    return (ts - timedelta(minutes=minutes)).isoformat()


# Why a line produced nothing. Filled on every call and read by main.py.
#
# This did not exist until 2026-08-09, and its absence cost a full day of
# silence. The funnel measured everything that happens AFTER a movement is
# found -- how many books moved, whether an entry survived -- so when detection
# itself stopped producing movements, every counter downstream read zero and
# the page said "рынок спокоен". It was not: we were blind on most of what we
# were paying to watch, and nothing anywhere said so.
LAST_DIAG = {}


def _baseline_max_age() -> int:
    """How old a price may be and still count as a baseline.

    Never shorter than one full rotation lap plus a cycle. The rail and the
    polling schedule used to be two independent constants that happened to
    contradict each other -- a 180-minute rail against a four-hour lap -- and
    the losing side was silent: every line on the wide list returned no
    baseline and was skipped without a word for a full day.

    Tying the rail to the lap the sweep is actually walking means that can no
    longer happen by arithmetic. It is still capped: past
    BASELINE_ABSOLUTE_MAX_MINUTES a comparison stops describing a market move
    at all, and being blind and loud beats publishing drift as a signal.
    """
    try:
        lap = int(storage.get_meta("wide_lap_minutes") or 0)
    except (TypeError, ValueError):
        lap = 0
    need = lap + POLL_INTERVAL_MINUTES if lap else 0
    return max(BASELINE_MAX_AGE_MINUTES,
               min(need, BASELINE_ABSOLUTE_MAX_MINUTES))


def detect(records, fetched_at):
    """records: output of odds_client.flatten_odds().
    Returns (spikes, movements). Spikes are sorted so cascades and sharp-book
    moves surface first.

    Every price is diffed against where that same line stood
    BASELINE_WINDOW_MINUTES ago, not against the previous poll. See the long
    note on BASELINE_WINDOW_MINUTES in config.py for why -- in short, diffing
    against the previous poll meant a faster cadence made the tool blinder.
    """
    spikes = []
    movements = []
    baseline_iso = _window_start_iso(fetched_at, BASELINE_WINDOW_MINUTES)
    max_age = _baseline_max_age()
    floor_iso = _window_start_iso(fetched_at, max_age)
    diag = {"lines": 0, "no_history": 0, "compared": 0, "flat": 0,
            "moved": 0, "spiked": 0, "max_age": max_age, "by_sport_blind": {}}

    for r in records:
        # Exchanges and long-shot prices are excluded from signal generation
        # entirely -- not just at display time. A spike recorded here also
        # feeds the win-rate and CLV stats, so exchange noise would quietly
        # corrupt the "is this tool actually working" numbers.
        if r["bookmaker"].lower() in EXCHANGE_BOOKMAKERS:
            continue
        # Must match analytics._usable() exactly -- see MIN_SIGNAL_PRICE in
        # config.py for what went wrong when these two drifted apart.
        if not r["price"] or not (MIN_SIGNAL_PRICE <= r["price"] <= MAX_SIGNAL_PRICE):
            continue
        # The draw is never bet, so tracking draw moves would only add rows to
        # tracked_alerts that can never be acted on and would skew the win-rate
        # stats. Draw prices are still stored in snapshots for the fair-price
        # maths -- this only stops them becoming signals.
        if EXCLUDE_DRAW and r["outcome_id"] == "draw":
            continue

        diag["lines"] += 1
        prev = storage.get_baseline_price(
            r["fixture_id"], r["bookmaker"], r["market_id"], r["outcome_id"],
            r["player_key"], baseline_iso, floor_iso,
        )
        if prev is None:
            # Either a first sighting, or -- the case that bit us -- history
            # that exists but is older than the floor because this league is
            # only polled every few hours. Counted per sport so the log names
            # exactly which leagues we are blind on instead of leaving it to be
            # inferred from an absence.
            diag["no_history"] += 1
            key = r.get("sport_key") or "?"
            diag["by_sport_blind"][key] = diag["by_sport_blind"].get(key, 0) + 1
            continue
        prev_fetched_at, prev_price = prev
        if not prev_price:
            continue

        diag["compared"] += 1
        pct_change = (r["price"] - prev_price) / prev_price
        if abs(pct_change) < MIN_DRIFT_PCT:
            diag["flat"] += 1
            continue
        diag["moved"] += 1

        is_sharp = r["bookmaker"].lower() in ASIAN_SHARP_BOOKMAKERS
        movements.append({
            "fixture_id": r["fixture_id"],
            "outcome_id": r["outcome_id"],
            "bookmaker": r["bookmaker"],
            "price": r["price"],
            "prev_price": prev_price,
            "pct_change": pct_change,
            "is_sharp_book": is_sharp,
        })

        if abs(pct_change) < SPIKE_THRESHOLD_PCT:
            continue
        diag["spiked"] += 1

        direction = "up" if pct_change > 0 else "down"
        window_start = _window_start_iso(fetched_at, CASCADE_WINDOW_MINUTES)
        prior_same_direction = storage.count_recent_same_direction_spikes(
            r["fixture_id"], r["bookmaker"], r["market_id"], r["outcome_id"], r["player_key"],
            direction, window_start,
        )
        spike = {
            **r,
            "prev_price": prev_price,
            "prev_fetched_at": prev_fetched_at,
            "pct_change": pct_change,
            "direction": direction,
            "is_sharp_book": is_sharp,
            "is_cascade": prior_same_direction > 0,
            "cascade_count": prior_same_direction + 1,
        }
        spikes.append(spike)
        storage.save_spike_event(spike, fetched_at)
        # Tracked alerts are NOT written here any more. This function only sees
        # one bookmaker at a time, so it cannot know the price we'd actually
        # bet at -- that needs the whole market. main.py records the bet once
        # analytics has picked the entry (see storage.save_bet_alert).

    spikes.sort(key=lambda s: (not s["is_cascade"], not s["is_sharp_book"], -abs(s["pct_change"])))
    LAST_DIAG.clear()
    LAST_DIAG.update(diag)
    return spikes, movements


def detect_spikes(records, fetched_at):
    """Backwards-compatible wrapper -- spikes only."""
    return detect(records, fetched_at)[0]
