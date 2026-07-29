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
    CASCADE_WINDOW_MINUTES,
    EXCHANGE_BOOKMAKERS,
    MAX_SIGNAL_PRICE,
)
import storage


def _window_start_iso(fetched_at: str, minutes: int) -> str:
    try:
        ts = datetime.fromisoformat(fetched_at)
    except ValueError:
        return fetched_at
    return (ts - timedelta(minutes=minutes)).isoformat()


def detect(records, fetched_at):
    """records: output of odds_client.flatten_odds().
    Returns (spikes, movements). Spikes are sorted so cascades and sharp-book
    moves surface first."""
    spikes = []
    movements = []

    for r in records:
        # Exchanges and long-shot prices are excluded from signal generation
        # entirely -- not just at display time. A spike recorded here also
        # feeds the win-rate and CLV stats, so exchange noise would quietly
        # corrupt the "is this tool actually working" numbers.
        if r["bookmaker"].lower() in EXCHANGE_BOOKMAKERS:
            continue
        if not r["price"] or r["price"] > MAX_SIGNAL_PRICE:
            continue

        prev = storage.get_latest_price(
            r["fixture_id"], r["bookmaker"], r["market_id"], r["outcome_id"], r["player_key"]
        )
        if prev is None:
            continue  # first sighting of this line -- nothing to diff against
        prev_fetched_at, prev_price = prev
        if not prev_price:
            continue

        pct_change = (r["price"] - prev_price) / prev_price
        if abs(pct_change) < MIN_DRIFT_PCT:
            continue

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
        storage.save_tracked_alert(
            "cascade" if spike["is_cascade"] else "spike", r, direction, fetched_at
        )

    spikes.sort(key=lambda s: (not s["is_cascade"], not s["is_sharp_book"], -abs(s["pct_change"])))
    return spikes, movements


def detect_spikes(records, fetched_at):
    """Backwards-compatible wrapper -- spikes only."""
    return detect(records, fetched_at)[0]
