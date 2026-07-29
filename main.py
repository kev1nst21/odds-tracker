"""Single poll cycle: discover sports -> fetch odds -> detect movement ->
build one summary per event -> notify -> update dashboard -> (periodically)
check results. Run this once per interval (see README.md)."""
import json
from datetime import datetime, timedelta, timezone

from config import DASHBOARD_URL, ODDSPAPI_KEY, ODDSPAPI_LOOKUP_TTL_HOURS
import odds_client
import oddspapi_client
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


def _load_lookup_cache(now):
    """Participant names and active tournament ids, reused between runs.

    These change slowly, so refetching them every cycle would spend ~12,000
    OddsPapi requests a month for data that barely moves.
    """
    stamp = storage.get_meta("oddspapi_lookup_at")
    if stamp:
        try:
            if (now - datetime.fromisoformat(stamp)) < timedelta(hours=ODDSPAPI_LOOKUP_TTL_HOURS):
                names = json.loads(storage.get_meta("oddspapi_names") or "{}")
                tourneys = json.loads(storage.get_meta("oddspapi_tournaments") or "{}")
                # JSON turns the int sport ids into strings on the way in.
                return ({int(k): v for k, v in names.items()},
                        {int(k): v for k, v in tourneys.items()})
        except (ValueError, TypeError):
            pass
    return {}, {}


def _save_lookup_cache(names, tourneys, now):
    if not names:
        return
    storage.set_meta("oddspapi_names", json.dumps(names))
    storage.set_meta("oddspapi_tournaments", json.dumps(tourneys))
    storage.set_meta("oddspapi_lookup_at", now.isoformat())


def _fetch_oddspapi(now):
    """Esports + table tennis. Never allowed to break the run: if this provider
    is down or unpaid, the football/tennis pipeline must still complete."""
    if not ODDSPAPI_KEY:
        return [], []
    try:
        names_cache, tourneys_cache = _load_lookup_cache(now)
        records, live, names, tourneys = oddspapi_client.collect(
            on_error=_warn_sport_error,
            names_cache=names_cache,
            tournaments_cache=tourneys_cache,
        )
        if not names_cache:
            _save_lookup_cache(names, tourneys, now)
        return records, live
    except Exception as exc:  # noqa: BLE001 -- second provider is best-effort
        print(f"[main] OddsPapi provider failed, continuing without it: {exc}")
        return [], []


def run_once():
    storage.init_db()
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()

    all_sports = odds_client.list_sports()  # free call, no quota cost
    sport_keys = odds_client.select_sport_keys(all_sports)

    raw = odds_client.fetch_odds_for_sports(sport_keys, on_error=_warn_sport_error)
    records = odds_client.flatten_odds(raw)
    live_records = odds_client.flatten_live(raw)

    # Esports + table tennis ride on a second provider but produce identical
    # records, so everything downstream treats them the same as football.
    esports_records, esports_live = _fetch_oddspapi(now)
    records += esports_records
    live_records += esports_live

    spikes, movements = detector.detect(records, fetched_at)
    summaries = analytics.build_event_summaries(records, spikes, movements)

    storage.save_snapshot(records, fetched_at)

    # Record the bets we're actually recommending, with all three prices, so
    # results.py can score them later against the price a real bet would have
    # got. Only alertable ones -- if there's nowhere left to bet, there is no
    # bet to score.
    logged = sum(1 for s in summaries
                 if s.get("alertable") and storage.save_bet_alert(s, fetched_at))

    notifier.notify_summaries(summaries, dashboard_url=DASHBOARD_URL)

    # Costs quota (one scores call per sport with pending alerts), so this is
    # internally throttled to run at most once every RESULTS_CHECK_INTERVAL_HOURS.
    newly_resolved = results.check_pending_results()

    live_rows = analytics.find_live_anomalies(live_records)
    logged_live = sum(1 for r in live_rows if storage.save_live_alert(r, fetched_at))
    path = dashboard.render_dashboard(summaries, quota=odds_client.LAST_QUOTA,
                                      live_rows=live_rows)

    actionable = sum(1 for s in summaries if s.get("alertable"))
    starred = sum(1 for s in summaries if s.get("stars", 0) >= 3)
    print(
        f"[{fetched_at}] {len(sport_keys)} sports via TheOddsAPI + "
        f"{len(esports_records)} esports/table-tennis lines via OddsPapi, "
        f"{len(records)} lines total, {len(summaries)} events moved 10%+, "
        f"{actionable} with an open entry, {starred} at 3 stars, "
        f"{logged} prematch + {logged_live} live bets logged, "
        f"{newly_resolved} resolved, dashboard -> {path}"
    )
    return summaries


if __name__ == "__main__":
    run_once()
