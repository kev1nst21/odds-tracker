"""Checks on the two changes that decide how many signals the tool can ever
produce: what a move is measured against, and which leagues get polled.

Both are easy to break silently -- a wrong baseline just makes the tracker
quiet, and a wrong rotation just makes it watch the wrong markets -- so they
get asserted rather than eyeballed.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "cov.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import detector  # noqa: E402
import odds_client  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)


def rec(price, fid="fx1", book="pinnacle", outcome="home"):
    return {"fixture_id": fid, "sport_key": "soccer_x", "start_time":
            (now + timedelta(hours=5)).isoformat(), "home_team": "A", "away_team": "B",
            "bookmaker": book, "market_id": "h2h", "outcome_id": outcome,
            "outcome_name": "A", "player_key": "-", "price": price}


# A line that slides 2.60 -> 2.34 over an hour in small steps. Every single
# step is under the 1% drift floor, so the old "diff against the previous
# poll" logic saw absolutely nothing -- this is the production bug.
steps = [2.60, 2.57, 2.54, 2.50, 2.46, 2.42, 2.38, 2.33, 2.29, 2.25]
for i, p in enumerate(steps):
    at = (now - timedelta(minutes=(len(steps) - i) * 7)).isoformat()
    storage.save_snapshot([rec(p)], at)

fetched_at = now.isoformat()
spikes, movements = detector.detect([rec(2.25)], fetched_at)
assert movements, "an hour-long slide produced no movement at all"
drop = movements[0]["pct_change"]
assert drop < -0.12, f"expected a ~13% drop against the hour-old price, got {drop:.3%}"
assert spikes, f"the slide did not clear the {config.SPIKE_THRESHOLD_PCT:.0%} threshold"
print(f"baseline ok: gradual slide 2.60 -> 2.25 seen as {drop:.1%} "
      f"(step-by-step it is under the 1% floor and was invisible before)")

# A baseline older than the safety rail must be refused rather than reported
# as a fresh move.
storage.save_snapshot([rec(9.00, fid="fx_old")], (now - timedelta(days=3)).isoformat())
_, mv_old = detector.detect([rec(2.00, fid="fx_old")], fetched_at)
assert not mv_old, "a three-day-old price was accepted as a baseline"
print("baseline ok: three-day-old history refused, drift is not a move")

# --------------------------------------------------------------- coverage
sports = ([{"key": f"soccer_tier2_{i}", "group": "Soccer", "active": True} for i in range(40)]
          + [{"key": k, "group": "Soccer", "active": True} for k in config.SOCCER_LEAGUE_KEYS]
          + [{"key": "tennis_atp_x", "group": "Tennis", "active": True},
             {"key": "basketball_nba", "group": "Basketball", "active": True},
             {"key": "soccer_dead", "group": "Soccer", "active": False}])

picked = odds_client.select_sport_keys(sports)
assert len(picked) <= config.MAX_SPORTS_PER_CYCLE, picked
assert "soccer_dead" not in picked, "polled a league that is out of season"
assert "basketball_nba" not in picked, "polled a group outside WIDE_GROUPS"
wide_in_first = [k for k in picked if k.startswith("soccer_tier2_")]
assert len(wide_in_first) >= config.WIDE_MIN_SLOTS, (
    f"wide sweep got only {len(wide_in_first)} slots", picked)

# Rotation must eventually reach every league rather than re-polling the same
# slice forever -- that is the whole point of paying for breadth.
seen = set(picked)
for _ in range(20):
    seen.update(odds_client.select_sport_keys(sports))
tier2 = {f"soccer_tier2_{i}" for i in range(40)}
assert tier2 <= seen, f"rotation never reached {len(tier2 - seen)} of the small leagues"
print(f"coverage ok: {config.MAX_SPORTS_PER_CYCLE} sports per cycle, "
      f"{len(wide_in_first)} of them small leagues, all 40 covered within 21 cycles")

cost_per_day = (24 * 60 / config.POLL_INTERVAL_MINUTES) * config.MAX_SPORTS_PER_CYCLE
print(f"budget: {config.POLL_INTERVAL_MINUTES} min cadence x "
      f"{config.MAX_SPORTS_PER_CYCLE} sports = {cost_per_day:.0f} credits/day")
