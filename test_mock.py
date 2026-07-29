"""Exercises storage -> detector -> notifier -> dashboard with synthetic data,
so the pipeline is proven correct before a real OddsPapi key is wired in."""
import os
from datetime import datetime, timedelta, timezone

# use a throwaway DB for this test run -- must patch config.DB_PATH *before*
# storage/dashboard import it, since they bind the value at import time.
import config
config.DB_PATH = os.path.join(os.path.dirname(__file__), "data", "test_odds_history.db")
if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)

import storage
import detector
import dashboard
storage.DB_PATH = config.DB_PATH  # re-bind in case storage was already imported earlier in the process

storage.init_db()

t0 = datetime.now(timezone.utc)
t1 = t0 + timedelta(minutes=5)

poll_1 = [
    {"fixture_id": "fx1", "start_time": "2026-08-01T18:00:00Z", "bookmaker": "pinnacle",
     "market_id": "101", "outcome_id": "home", "player_key": "0", "price": 2.10},
    {"fixture_id": "fx1", "start_time": "2026-08-01T18:00:00Z", "bookmaker": "bet365",
     "market_id": "101", "outcome_id": "home", "player_key": "0", "price": 2.05},
]

# pinnacle (sharp/asian) jumps hard, bet365 (public) barely moves
poll_2 = [
    {"fixture_id": "fx1", "start_time": "2026-08-01T18:00:00Z", "bookmaker": "pinnacle",
     "market_id": "101", "outcome_id": "home", "player_key": "0", "price": 1.85},  # -11.9%
    {"fixture_id": "fx1", "start_time": "2026-08-01T18:00:00Z", "bookmaker": "bet365",
     "market_id": "101", "outcome_id": "home", "player_key": "0", "price": 2.03},  # -1.0%
]

# poll 1: nothing to diff against yet
spikes_1 = detector.detect_spikes(poll_1, t0.isoformat())
storage.save_snapshot(poll_1, t0.isoformat())
assert spikes_1 == [], f"expected no spikes on first poll, got {spikes_1}"

# poll 2: pinnacle should trip the default 8% threshold, bet365 should not
spikes_2 = detector.detect_spikes(poll_2, t1.isoformat())
storage.save_snapshot(poll_2, t1.isoformat())

assert len(spikes_2) == 1, f"expected exactly 1 spike, got {len(spikes_2)}: {spikes_2}"
assert spikes_2[0]["bookmaker"] == "pinnacle"
assert spikes_2[0]["is_sharp_book"] is True
assert abs(spikes_2[0]["pct_change"] + 0.1190) < 0.001, spikes_2[0]["pct_change"]

path = dashboard.render_dashboard(spikes_2)
assert os.path.exists(path)
with open(path, encoding="utf-8") as f:
    html_out = f.read()
assert "pinnacle" in html_out
assert "-11.9%" in html_out

print("OK: storage + detector + dashboard pipeline verified with mock data.")
print(f"Dashboard written to: {path}")
