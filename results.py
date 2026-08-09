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

from config import (
    RESULT_CHECK_DELAY_HOURS,
    RESULTS_CHECK_INTERVAL_HOURS,
    ODDSPAPI_SPORT_KEYS,
    LIVE_SCORE_MAX_SPORTS,
    LIVE_SCORE_MIN_INTERVAL_MINUTES,
    ODDSPAPI_SETTLEMENTS_PER_CYCLE,
    RESULT_GIVE_UP_HOURS,
)
import odds_client
import oddspapi_client
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
    """CLV measured against the price WE would have bet at, not against
    whichever bookmaker moved first.

    We always back a side whose price is falling, so the bet is good if the
    closing line ended up BELOW our entry: it means we got in before the rest
    of the market caught up. clv_pct is expressed so that positive = we beat
    the close.

    Returns (clv_pct, beat_the_close), either possibly None when there isn't
    enough snapshot history (e.g. the match started before the next poll).
    """
    entry = row["entry_price"] if row["entry_price"] is not None else row["alert_price"]
    if not row["start_time"] or entry is None:
        return None, None
    closing = storage.get_closing_price(
        row["fixture_id"], row["bookmaker"], row["market_id"], row["outcome_id"],
        row["player_key"], row["start_time"],
    )
    if not closing:
        return None, None
    _, closing_price = closing
    if not closing_price or not entry:
        return None, None
    clv_pct = (entry - closing_price) / closing_price
    return clv_pct, clv_pct > 0


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
        # Esports and table tennis come from OddsPapi; The Odds API scores
        # endpoint has never heard of those sport keys, so asking it would
        # error on every cycle and the alerts would never clear. They stay
        # pending until a results source for that provider is wired in.
        if row["sport_key"] in ODDSPAPI_SPORT_KEYS:
            continue
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
        if row["sport_key"] in ODDSPAPI_SPORT_KEYS:
            continue
        ev = scores_by_fixture.get(row["fixture_id"])
        if not ev or not ev.get("completed"):
            continue  # not finished yet, or we have no score data for it

        home_score, away_score = _extract_scores(ev, row["home_team"], row["away_team"])
        winner = _winner_from_scores(home_score, away_score)
        side = row["outcome_id"]

        # Keep the final score. Grading is the last moment we hold it -- once
        # the bet is resolved we stop asking about that fixture, so if it is
        # not stored here the site can only ever say "зашла" without saying
        # what the match actually finished.
        if home_score is not None and away_score is not None:
            storage.save_live_score(row["fixture_id"], row["sport_key"],
                                    row["home_team"], row["away_team"],
                                    home_score, away_score, True, now.isoformat())

        # We only ever back the side money went into, so the bet wins exactly
        # when that side wins. A draw is never bet, so it always loses the bet.
        if winner is None or side not in _VALID_SIDES:
            result = "n/a"
        else:
            result = "hit" if side == winner else "miss"

        clv_pct, clv_continued = _compute_clv(row)
        storage.mark_resolved(row["id"], result, now.isoformat(), clv_pct, clv_continued,
                              _optimal_result(row, winner, side, result,
                                              (home_score, away_score)))
        resolved_count += 1

    resolved_count += _resolve_oddspapi(pending, now)
    resolved_count += _give_up_on_stale(pending, now)
    resolved_count += _resolve_movements(scores_by_fixture, now)
    return resolved_count


# The provider's settlement vocabulary, mapped onto ours. HALFWIN/HALFLOSS
# belong to handicap markets we do not bet here, but they are listed so an
# unexpected one is handled rather than silently treated as a loss.
_SETTLEMENT = {
    "WIN": "hit", "HALFWIN": "hit",
    "LOSE": "miss", "HALFLOSS": "miss",
    "PUSH": "n/a", "CANCELLED": "n/a",
}


def _warn_fixture_error(fixture_id, exc):
    print(f"[results] settlement lookup failed for {fixture_id}: {exc}")


def _resolve_oddspapi(pending, now: datetime) -> int:
    """Grade esports and table tennis, which The Odds API cannot see.

    One settlement call per fixture, capped per cycle. UNDECIDED and unknown
    fixtures are left alone: a match that has not been settled yet must stay
    pending, not become a loss because we asked too early.
    """
    rows = [r for r in pending if r["sport_key"] in ODDSPAPI_SPORT_KEYS]
    if not rows:
        return 0

    resolved = 0
    seen = {}
    for row in rows[:ODDSPAPI_SETTLEMENTS_PER_CYCLE]:
        fid = row["fixture_id"]
        if fid not in seen:
            seen[fid] = oddspapi_client.fetch_settlement(fid, on_error=_warn_fixture_error)
        settlement = seen[fid]
        side = row["outcome_id"]
        raw = settlement.get(side)
        if not raw or raw == "UNDECIDED":
            continue

        result = _SETTLEMENT.get(raw, "n/a")
        # The score is only for display, so a missing one must never stop the
        # bet being graded -- that was the whole failure mode being fixed here.
        hs, as_ = oddspapi_client.fetch_score(fid, on_error=_warn_fixture_error)
        if hs is not None and as_ is not None:
            storage.save_live_score(fid, row["sport_key"], row["home_team"],
                                    row["away_team"], hs, as_, True, now.isoformat())

        clv_pct, clv_continued = _compute_clv(row)
        winner = _winner_from_scores(hs, as_)
        storage.mark_resolved(row["id"], result, now.isoformat(), clv_pct, clv_continued,
                              _optimal_result(row, winner, side, result, (hs, as_)))
        resolved += 1

    if resolved:
        print(f"[results] settled {resolved} esports/table-tennis bet(s) via OddsPapi")
    return resolved


def _give_up_on_stale(pending, now: datetime) -> int:
    """Close bets whose result can no longer be looked up.

    Not a cosmetic tidy-up. A row that never resolves is counted in "ждут
    матча" for ever, so the page shows a bigger book than the one we have
    actually checked -- and the longer the outage, the more flattering the
    error. Marking them n/a keeps them visible and countable as what they are:
    signals we failed to verify.
    """
    cutoff = now - timedelta(hours=RESULT_GIVE_UP_HOURS)
    stale = []
    for row in pending:
        if not row["start_time"]:
            continue
        try:
            started = datetime.fromisoformat(str(row["start_time"]).replace("Z", "+00:00"))
        except ValueError:
            continue
        if started < cutoff:
            stale.append(row)

    for row in stale:
        clv_pct, clv_continued = _compute_clv(row)
        storage.mark_resolved(row["id"], "n/a", now.isoformat(), clv_pct, clv_continued, "n/a")
    if stale:
        print(f"[results] gave up on {len(stale)} bet(s) older than "
              f"{RESULT_GIVE_UP_HOURS:.0f}h -- scores no longer available")
    return len(stale)


def refresh_live_scores(now: datetime = None) -> int:
    """Pull the current score for every match we are standing on that has
    already kicked off.

    Requested 2026-08-01: "если у нас матч идет, то пиши актуальный счет".

    Budget-aware, because scores cost credits like everything else. It only
    asks about sports that actually have an in-play position, and never more
    than LIVE_SCORE_MAX_SPORTS of them per cycle -- so on a quiet night it
    spends nothing at all, and on a busy one a couple of credits rather than
    sweeping the whole card.
    """
    now = now or datetime.now(timezone.utc)

    # Clock-throttled, not cycle-throttled. See LIVE_SCORE_MIN_INTERVAL_MINUTES:
    # tying this to the cycle made a faster cadence cost proportionally more
    # for a number that is only ever read, never acted on.
    last = storage.get_meta("last_live_scores_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < timedelta(minutes=LIVE_SCORE_MIN_INTERVAL_MINUTES):
                return 0
        except ValueError:
            pass

    rows = storage.inplay_fixtures(now.isoformat())
    if not rows:
        return 0
    storage.set_meta("last_live_scores_at", now.isoformat())

    wanted = {}
    for r in rows:
        if r["sport_key"] and r["sport_key"] not in ODDSPAPI_SPORT_KEYS:
            wanted.setdefault(r["sport_key"], []).append(r)
    if not wanted:
        return 0

    # Most positions first: if the cap bites, spend the credits where the most
    # of the page is waiting on an answer.
    sports = sorted(wanted, key=lambda k: -len(wanted[k]))[:LIVE_SCORE_MAX_SPORTS]

    saved, at = 0, now.isoformat()
    for sport_key in sports:
        # daysFrom=1 is the cheapest form of this call (1 credit) and already
        # covers everything currently running.
        for ev in odds_client.fetch_scores_for_sport(sport_key, days_from=1,
                                                     on_error=_warn_sport_error):
            home, away = ev.get("home_team"), ev.get("away_team")
            hs, as_ = _extract_scores(ev, home, away)
            if hs is None or as_ is None:
                continue  # listed but not started -- nothing to show yet
            storage.save_live_score(ev.get("id"), sport_key, home, away,
                                    hs, as_, ev.get("completed"), at)
            saved += 1
    return saved


def _resolve_movements(scores_by_fixture, now):
    """Grade the movements table -- every detected move, not just the ones we
    could bet. Same rule as a signal: we back the side money went into, so it
    wins exactly when that side wins."""
    cutoff = (now - timedelta(hours=RESULT_CHECK_DELAY_HOURS)).isoformat()
    done = 0
    for row in storage.get_unresolved_movements(cutoff):
        if row["sport_key"] in ODDSPAPI_SPORT_KEYS:
            continue
        ev = scores_by_fixture.get(row["fixture_id"])
        if not ev or not ev.get("completed"):
            continue
        hs, as_ = _extract_scores(ev, row["home_team"], row["away_team"])
        winner = _winner_from_scores(hs, as_)
        side = row["outcome_id"]
        if hs is not None and as_ is not None:
            storage.save_live_score(row["fixture_id"], row["sport_key"],
                                    row["home_team"], row["away_team"],
                                    hs, as_, True, now.isoformat())
        if winner is None or side not in _VALID_SIDES:
            result = "n/a"
        else:
            result = "hit" if side == winner else "miss"
        storage.mark_movement_resolved(row["id"], result, now.isoformat())
        done += 1
    return done


def _optimal_result(row, winner, side, straight_result, scores=None):
    """Settle the bet the ОПТИМАЛЬНАЯ strategy placed, which since 2026-07-30
    is not always the same bet as the aggressive one.

      * 'straight'      -- identical bet, identical verdict.
      * 'double_chance' -- backing "our side OR the draw", so it also wins when
                           the match ends level. This is settleable from the
                           final score alone, which is exactly why football
                           long shots are allowed into the optimal statistics.
      * 'handicap'      -- returns None. We never knew the line or the price,
                           so there is no honest verdict to record. Leaving it
                           unsettled keeps it out of the win rate instead of
                           quietly counting as a loss (or, worse, a win).
    """
    kind = row["opt_kind"] if "opt_kind" in row.keys() else None
    if not kind:
        return None
    if kind == "straight":
        return straight_result
    if kind == "double_chance":
        if winner is None or side not in _VALID_SIDES:
            return "n/a"
        return "hit" if winner in (side, "draw") else "miss"
    if kind == "set_handicap":
        return _grade_set_handicap(scores, side)
    return None


def _grade_set_handicap(scores, side):
    """+1.5 sets: our player wins the bet by taking at least one set.

    Deliberately paranoid about what the numbers mean. The Odds API reports
    tennis as sets won, but nothing in the response says so, and if a provider
    ever returned games instead ("6-4") this would silently grade nonsense. So
    the score is only trusted when it actually looks like a set score -- small
    integers, no more than five between the two players. Anything else is
    recorded as 'n/a' rather than guessed at.
    """
    if not scores:
        return "n/a"
    home_score, away_score = scores
    if home_score is None or away_score is None:
        return "n/a"
    if not (0 <= home_score <= 5 and 0 <= away_score <= 5 and home_score + away_score <= 5):
        return "n/a"  # these are not sets -- refuse to guess
    ours = home_score if side == "home" else away_score
    return "hit" if ours >= 1 else "miss"
