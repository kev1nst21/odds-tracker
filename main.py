"""Single poll cycle: discover sports -> fetch odds -> store snapshot ->
detect spikes -> notify -> update dashboard -> (periodically) check results.
Run this once per interval (see README.md for how to schedule it)."""
from datetime import datetime, timezone

import odds_client
import storage
import detector
import consensus
import notifier
import dashboard
import results


def _warn_sport_error(sport_key, exc):
    # A single sport temporarily failing (e.g. no events right now) shouldn't
    # crash the whole run -- log it and keep going with everyone else.
    print(f"[main] skipping sport '{sport_key}': {exc}")


def run_once():
    storage.init_db()
    fetched_at = datetime.now(timezone.utc).isoformat()

    all_sports = odds_client.list_sports()  # free call, no quota cost
    sport_keys = odds_client.select_sport_keys(all_sports)

    raw = odds_client.fetch_odds_for_sports(sport_keys, on_error=_warn_sport_error)
    records = odds_client.flatten_odds(raw)

    spikes = detector.detect_spikes(records, fetched_at)
    divergences = consensus.sharp_vs_public(records)
    region_rows = consensus.region_breakdown(records)

    storage.save_snapshot(records, fetched_at)
    notifier.notify_spikes(spikes)
    notifier.notify_digest(divergences)
    notifier.notify_region_digest(region_rows)

    # Costs quota (scores call per sport with pending alerts), so this is
    # internally throttled to run at most once every RESULTS_CHECK_INTERVAL_HOURS.
    newly_resolved = results.check_pending_results()

    path = dashboard.render_dashboard(spikes, divergences, region_rows)

    print(
        f"[{fetched_at}] sports={sport_keys} fetched {len(records)} lines, "
        f"{len(spikes)} spikes, {len(divergences)} sharp/public divergences, "
        f"{len(region_rows)} Asia/Europe divergences, {newly_resolved} alerts resolved, "
        f"dashboard -> {path}"
    )
    return spikes, divergences, region_rows


if __name__ == "__main__":
    run_once()
