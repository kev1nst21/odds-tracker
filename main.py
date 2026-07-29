"""Single poll cycle: discover sports -> fetch odds -> detect movement ->
build one summary per event -> notify -> update dashboard -> (periodically)
check results. Run this once per interval (see README.md)."""
from datetime import datetime, timezone

from config import DASHBOARD_URL
import odds_client
import storage
import detector
import analytics
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

    spikes, movements = detector.detect(records, fetched_at)
    summaries = analytics.build_event_summaries(records, spikes, movements)

    storage.save_snapshot(records, fetched_at)
    notifier.notify_summaries(summaries, dashboard_url=DASHBOARD_URL)

    # Costs quota (one scores call per sport with pending alerts), so this is
    # internally throttled to run at most once every RESULTS_CHECK_INTERVAL_HOURS.
    newly_resolved = results.check_pending_results()

    path = dashboard.render_dashboard(summaries, quota=odds_client.LAST_QUOTA)

    with_value = sum(1 for s in summaries if s.get("has_value"))
    with_move = sum(1 for s in summaries if s.get("has_move"))
    starred = sum(1 for s in summaries if s.get("stars", 0) >= 3)
    print(
        f"[{fetched_at}] sports={sport_keys} fetched {len(records)} lines, "
        f"{len(summaries)} events, {with_move} with movement, {with_value} with value, "
        f"{starred} with 3 stars, {newly_resolved} alerts resolved, dashboard -> {path}"
    )
    return summaries


if __name__ == "__main__":
    run_once()
