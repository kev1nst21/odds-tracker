"""Follows up on past alerts once their match should be over, so the
dashboard can show whether the tool's signals actually predicted anything --
not just "we sent N alerts" but "of the ones we could check, how many were
right", plus Closing Line Value (CLV): did the market keep moving the way we
called it, right up to kickoff? CLV is the professional sports-betting
standard for judging a signal, and it's more statistically robust than
win/loss alone -- a "correct" alert can lose on a fluke, and a "wrong" alert
can still have called the market move correctly. We already store every
snapshot in odds_snapshots, so CLV needs no extra API calls: it's just the
last stored price before the match started vs. the price we alerted at.

2026-07-29: rewritten for The Odds API. Results come from
GET /v4/sports/{sport}/scores/ -- one call per sport (not per fixture), so
pending alerts are grouped by sport_key first to keep quota cost down. This
endpoint also costs credits, so main.py only calls check_pending_results()
every RESULTS_CHECK_INTERVAL_HOURS (throttled via storage's meta table),
not on every poll cycle.
"""
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from config import RESULT_CHECK_DELAY_HOURS, RESULTS_CHECK_INTERVAL_HOURS
import odds_client
import storage

_VALID_SIDES = {"home", "away", "draw"}


def _extract_scores(score_event: dict, home_team: str, away_team: str):
    """The Odds API scores shape: {"scores": [{"name": team, "score": "2"}, ...],
    "completed": true/false}. Match by team name since there's no home/away
    tag on each entry."""
    scores = score_event.get("scores")
    if not scores:
        return None, None
    by_name = {s.get("name"): s.get("score") for s in scores if s.get("name")}
    home_score, away_score = by_name.get(home_team), by_name.get(away_team)
    if home_score is None or away_score is None:
        return None, None
    try:
        return float(home_score), float(away_score)
    except (TypeError, ValueError):
        return None, None


def _winner_from_scores(home_score, away_score):
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


def _compute_clv(row):
    """CLV = (closing_price - alert_price) / alert_price, using the last
    snapshot we stored before the match's start_time as the 'closing line'.
    clv_continued: did the closing line keep moving the same direction we
    alerted on (down => stayed <= alert price; up => stayed >= alert price)?
    Returns (clv_pct, clv_continued), either possibly None if we don't have
    enough data (e.g. only ever saw one snapshot for this line)."""
    if not row["start_time"] or row["alert_price"] is None:
        return None, None
    closing = storage.get_closing_price(
        row["fixture_id"], row["bookmaker"], row["market_id"], row["outcome_id"], row["player_key"],
        row["start_time"],
    )
    if not closing:
        return None, None
    _, closing_price = closing
    alert_price = row["alert_price"]
    if not closing_price or not alert_price:
        return None, None
    clv_pct = (closing_price - alert_price) / alert_price
    if row["direction"] == "down":
        continued = closing_price <= alert_price
    else:
        continued = closing_price >= alert_price
    return clv_pct, continued


def _warn_sport_error(sport_key, exc):
    print(f"[results] skipping sport '{sport_key}' scores lookup: {exc}")


def check_pending_results(now: datetime = None) -> int:
    """Look at unresolved tracked alerts whose match started long enough ago
    that it should be over, fetch scores per sport, and mark hit/miss/n-a
    plus CLV. Throttled to run at most once every RESULTS_CHECK_INTERVAL_HOURS
    (scores calls cost quota too). Returns how many alerts got resolved."""
    now = now or datetime.now(timezone.utc)

    last_check = storage.get_meta("last_results_check_at")
    if last_check:
        try:
            last_dt = datetime.fromisoformat(last_check)
            if (now - last_dt) < timedelta(hours=RESULTS_CHECK_INTERVAL_HOURS):
                return 0  # too soon since the last check -- save quota
        except ValueError:
            pass

    cutoff = (now - timedelta(hours=RESULT_CHECK_DELAY_HOURS)).isoformat()
    pending = storage.get_unresolved_alerts(cutoff)
    storage.set_meta("last_results_check_at", now.isoformat())
    if not pending:
        return 0

    by_sport = defaultdict(list)
    for row in pending:
        by_sport[row["sport_key"]].append(row)

    scores_by_fixture = {}
    for sport_key, rows in by_sport.items():
        if not sport_key:
            continue
        events = odds_client.fetch_scores_for_sport(sport_key, days_from=3, on_error=_warn_sport_error)
        for ev in events:
            scores_by_fixture[ev.get("id")] = ev

    resolved_count = 0
    for row in pending:
        ev = scores_by_fixture.get(row["fixture_id"])
        if not ev or not ev.get("completed"):
            continue  # not finished yet, or we have no score data for it

        home_score, away_score = _extract_scores(ev, row["home_team"], row["away_team"])
        winner = _winner_from_scores(home_score, away_score)
        side = row["outcome_id"]

        if winner is None or side not in _VALID_SIDES:
            result = "n/a"
        elif row["direction"] == "down":
            result = "hit" if side == winner else "miss"
        else:  # direction == "up" -- alert was betting against this side
            result = "hit" if side != winner else "miss"

        clv_pct, clv_continued = _compute_clv(row)
        storage.mark_resolved(row["id"], result, now.isoformat(), clv_pct, clv_continued)
        resolved_count += 1

    return resolved_count
