"""Exercises odds_client.flatten_odds -> storage -> detector -> consensus ->
notifier -> dashboard with synthetic The-Odds-API-shaped data, so the
pipeline is proven correct before/without spending a real API key's quota."""
import os
from datetime import datetime, timedelta, timezone

# use a throwaway DB for this test run -- must patch config.DB_PATH *before*
# storage/dashboard import it, since they bind the value at import time.
import config
config.DB_PATH = os.path.join(os.path.dirname(__file__), "data", "test_odds_history.db")
config.DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "data", "test_dashboard.html")
if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)

import storage
import odds_client
import detector
import consensus
import notifier
import dashboard

storage.init_db()

t0 = datetime.now(timezone.utc)
t1 = t0 + timedelta(minutes=5)

event = {
    "id": "evt_test_1",
    "sport_key": "soccer_epl",
    "sport_title": "EPL",
    "commence_time": "2026-08-01T18:00:00Z",
    "home_team": "Arsenal",
    "away_team": "Chelsea",
    "bookmakers": [
        {"key": "pinnacle", "title": "Pinnacle", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 2.10},
                {"name": "Chelsea", "price": 3.40},
                {"name": "Draw", "price": 3.30},
            ]},
        ]},
        {"key": "bovada", "title": "Bovada", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 2.05},
                {"name": "Chelsea", "price": 3.50},
                {"name": "Draw", "price": 3.25},
            ]},
        ]},
    ],
}

poll_1 = odds_client.flatten_odds([event])

# pinnacle (sharp/Asian) jumps hard, bovada (public) barely moves
event["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.85  # -11.9%
event["bookmakers"][1]["markets"][0]["outcomes"][0]["price"] = 2.03  # -1.0%
poll_2 = odds_client.flatten_odds([event])

# poll 1: nothing to diff against yet
spikes_1 = detector.detect_spikes(poll_1, t0.isoformat())
storage.save_snapshot(poll_1, t0.isoformat())
assert spikes_1 == [], f"expected no spikes on first poll, got {spikes_1}"

# poll 2: pinnacle should trip the default 8% threshold, bovada should not
spikes_2 = detector.detect_spikes(poll_2, t1.isoformat())
storage.save_snapshot(poll_2, t1.isoformat())

assert len(spikes_2) == 1, f"expected exactly 1 spike, got {len(spikes_2)}: {spikes_2}"
assert spikes_2[0]["bookmaker"] == "pinnacle"
assert spikes_2[0]["is_sharp_book"] is True
assert spikes_2[0]["home_team"] == "Arsenal" and spikes_2[0]["away_team"] == "Chelsea"
assert abs(spikes_2[0]["pct_change"] + 0.1190) < 0.001, spikes_2[0]["pct_change"]

divergences = consensus.sharp_vs_public(poll_2)
assert len(divergences) == 1, f"expected 1 sharp/public divergence, got {divergences}"

msg = notifier._format_spike(spikes_2[0])
assert "Arsenal" in msg and "Chelsea" in msg
assert "ИТОГ" in msg  # explicit user requirement: every alert ends with a clear summary line

path = dashboard.render_dashboard(spikes_2, divergences, [])
assert os.path.exists(path)
with open(path, encoding="utf-8") as f:
    html_out = f.read()
assert "pinnacle" in html_out
assert "Arsenal" in html_out and "Chelsea" in html_out
assert "-11.9%" in html_out

print("OK: flatten_odds + storage + detector + consensus + notifier + dashboard verified with mock data.")
print(f"Dashboard written to: {path}")
