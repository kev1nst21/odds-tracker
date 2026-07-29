"""Single poll cycle: fetch odds -> store snapshot -> detect spikes -> notify -> update dashboard.
Run this once per interval (see README.md for how to schedule it)."""
from datetime import datetime, timezone

from config import SPORTS, ALL_BOOKMAKERS, ALL_TOURNAMENT_IDS
import odds_client
import storage
import detector
import consensus
import notifier
import dashboard
import results


def _warn_bookmaker_error(bookmaker, exc):
    # A single unsupported/renamed bookmaker slug shouldn't crash the whole
    # run -- log it and keep going with everyone else.
    print(f"[main] skipping bookmaker '{bookmaker}': {exc}")


def run_once(tournament_ids: list):
    storage.init_db()
    fetched_at = datetime.now(timezone.utc).isoformat()

    raw = odds_client.fetch_odds_by_tournaments(
        tournament_ids, ALL_BOOKMAKERS, on_bookmaker_error=_warn_bookmaker_error
    )
    records = odds_client.flatten_odds(raw)

    spikes = detector.detect_spikes(records, fetched_at)
    divergences = consensus.sharp_vs_public(records)
    region_rows = consensus.region_breakdown(records)

    storage.save_snapshot(records, fetched_at)
    notifier.notify_spikes(spikes)
    notifier.notify_digest(divergences)
    notifier.notify_region_digest(region_rows)

    newly_resolved = results.check_pending_results()

    path = dashboard.render_dashboard(spikes, divergences, region_rows)

    print(
        f"[{fetched_at}] fetched {len(records)} lines across {len(ALL_BOOKMAKERS)} bookmakers, "
        f"{len(spikes)} spikes, {len(divergences)} sharp/public divergences, "
        f"{len(region_rows)} Asia/Europe divergences, {newly_resolved} alerts resolved, "
        f"dashboard -> {path}"
    )
    return spikes, divergences, region_rows


if __name__ == "__main__":
    run_once(ALL_TOURNAMENT_IDS)
