"""Follows up on past alerts once their match should be over, so the
dashboard can show whether the tool's signals actually predicted anything --
not just "we sent N alerts" but "of the ones we could check, how many were
right". This is the piece that turns the tracker from "noisy notifications"
into something you can judge.

IMPORTANT CAVEAT: OddsPapi's exact fixture-result schema, and what a 1X2
market's outcome_id/player_key/label look like on a real finished match,
haven't been confirmed live yet (that needs an actual completed fixture to
check against). This module is written defensively: if it can't confidently
read a final score or map our tracked outcome to a side, it marks the alert
'n/a' rather than guessing. Once a few real matches finish, it's worth
checking storage.alert_stats()['recent'] and adjusting _get_winner /
_label_side to match whatever OddsPapi actually returns.
"""
from datetime import datetime, timedelta, timezone

from config import RESULT_CHECK_DELAY_HOURS
import odds_client
import storage

# Labels commonly used for 1X2 markets across bookmakers -- if a tracked
# alert's label doesn't match one of these (e.g. it's a spread/total line),
# we don't try to grade it.
_HOME_LABELS = {"home", "1", "h"}
_AWAY_LABELS = {"away", "2", "a"}
_DRAW_LABELS = {"draw", "x"}

_FINISHED_STATUSES = {"finished", "ft", "completed", "closed", "ended"}


def _label_side(label):
    if not label:
        return None
    key = str(label).strip().lower()
    if key in _HOME_LABELS:
        return "home"
    if key in _AWAY_LABELS:
        return "away"
    if key in _DRAW_LABELS:
        return "draw"
    return None


def _match_finished(fixture: dict) -> bool:
    status = str(fixture.get("status") or fixture.get("state") or "").strip().lower()
    return status in _FINISHED_STATUSES


def _get_winner(fixture: dict):
    """Best-effort extraction of the winning side from a finished fixture.
    Returns 'home' / 'away' / 'draw', or None if the shape isn't recognized."""
    home_score = fixture.get("homeScore", fixture.get("home_score"))
    away_score = fixture.get("awayScore", fixture.get("away_score"))
    if home_score is None or away_score is None:
        score = fixture.get("score") or {}
        if isinstance(score, dict):
            home_score = score.get("home")
            away_score = score.get("away")
    if home_score is None or away_score is None:
        return None
    try:
        home_score, away_score = float(home_score), float(away_score)
    except (TypeError, ValueError):
        return None
    if home_score > away_score:
        return "home"
    if away_score > home_score:
        return "away"
    return "draw"


def check_pending_results(now: datetime = None) -> int:
    """Look at unresolved tracked alerts whose match started long enough ago
    that it should be over, fetch the fixture, and mark hit/miss/n-a.
    Returns how many alerts got resolved this run."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=RESULT_CHECK_DELAY_HOURS)).isoformat()

    resolved_count = 0
    for row in storage.get_unresolved_alerts(cutoff):
        alert_id, alert_type, fixture_id, start_time, bookmaker, market_id, outcome_id, player_key, label, direction, detected_at = row
        try:
            fixture = odds_client.get_fixture(fixture_id)
        except odds_client.OddsPapiError:
            continue  # transient/lookup failure -- try again next run

        if not isinstance(fixture, dict) or not _match_finished(fixture):
            continue  # not over yet (or shape we don't recognize as "finished")

        winner = _get_winner(fixture)
        side = _label_side(label)
        if winner is None or side is None:
            result = "n/a"
        elif direction == "down":
            result = "hit" if side == winner else "miss"
        else:  # direction == "up" -- alert bet against this side
            result = "hit" if side != winner else "miss"

        storage.mark_resolved(alert_id, result, now.isoformat())
        resolved_count += 1

    return resolved_count
