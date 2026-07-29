"""Compares a fresh batch of odds records against the last stored price for
each line and flags anything that moved more than SPIKE_THRESHOLD_PCT."""
from config import SPIKE_THRESHOLD_PCT, ASIAN_SHARP_BOOKMAKERS
import storage


def detect_spikes(records, fetched_at):
    """records: output of odds_client.flatten_odds().
    Returns a list of spike dicts, sorted so Asian sharp-book moves surface first."""
    spikes = []
    for r in records:
        prev = storage.get_latest_price(
            r["fixture_id"], r["bookmaker"], r["market_id"], r["outcome_id"], r["player_key"]
        )
        if prev is None:
            continue  # first time we've seen this line -- nothing to diff against yet
        prev_fetched_at, prev_price = prev
        if prev_price == 0:
            continue
        pct_change = (r["price"] - prev_price) / prev_price
        if abs(pct_change) >= SPIKE_THRESHOLD_PCT:
            spikes.append({
                **r,
                "prev_price": prev_price,
                "prev_fetched_at": prev_fetched_at,
                "pct_change": pct_change,
                "is_sharp_book": r["bookmaker"].lower() in ASIAN_SHARP_BOOKMAKERS,
            })

    spikes.sort(key=lambda s: (not s["is_sharp_book"], -abs(s["pct_change"])))
    return spikes
