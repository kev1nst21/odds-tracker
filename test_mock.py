"""Exercises flatten_odds -> storage -> detector -> analytics -> notifier ->
dashboard on synthetic The-Odds-API-shaped data, so the pipeline (and the
no-vig fair-price maths in particular) is proven correct without spending real
API quota."""
import os
from datetime import datetime, timedelta, timezone

# use a throwaway DB for this test run -- must patch config paths *before*
# storage/dashboard import them, since they bind the values at import time.
import config
config.DB_PATH = os.path.join(os.path.dirname(__file__), "data", "test_odds_history.db")
config.DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "data", "test_dashboard.html")
if os.path.exists(config.DB_PATH):
    os.remove(config.DB_PATH)

import storage
import odds_client
import detector
import analytics
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
        # Pinnacle: the sharp reference. 1/2.10 + 1/3.30 + 1/3.40 = 1.0700,
        # i.e. a 7% margin, so fair odds are each price x 1.0700.
        {"key": "pinnacle", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 2.10},
                {"name": "Draw", "price": 3.30},
                {"name": "Chelsea", "price": 3.40},
            ]},
        ]},
        # A public book pricing Arsenal generously -- this should surface as value.
        {"key": "bovada", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 2.45},
                {"name": "Draw", "price": 3.25},
                {"name": "Chelsea", "price": 3.35},
            ]},
        ]},
        # An exchange posting nonsense -- must be ignored everywhere.
        {"key": "betfair_ex_eu", "markets": [
            {"key": "h2h", "outcomes": [
                {"name": "Arsenal", "price": 9.20},
                {"name": "Draw", "price": 3.30},
                {"name": "Chelsea", "price": 3.40},
            ]},
        ]},
    ],
}

poll_1 = odds_client.flatten_odds([event])
detector.detect(poll_1, t0.isoformat())
storage.save_snapshot(poll_1, t0.isoformat())

# Pinnacle shortens Arsenal past the 10% threshold; the exchange swings wildly
# but must be ignored everywhere.
event["bookmakers"][0]["markets"][0]["outcomes"][0]["price"] = 1.85   # -11.9%
event["bookmakers"][2]["markets"][0]["outcomes"][0]["price"] = 2.28   # -75%, exchange
poll_2 = odds_client.flatten_odds([event])

spikes, movements = detector.detect(poll_2, t1.isoformat())
storage.save_snapshot(poll_2, t1.isoformat())

assert all(s["bookmaker"] != "betfair_ex_eu" for s in spikes), \
    f"exchange leaked into spikes: {[s['bookmaker'] for s in spikes]}"
assert all(m["bookmaker"] != "betfair_ex_eu" for m in movements), "exchange leaked into movements"
assert any(s["bookmaker"] == "pinnacle" for s in spikes), "expected the pinnacle move"

summaries = analytics.build_event_summaries(poll_2, spikes, movements)
assert len(summaries) == 1, f"expected one consolidated event, got {len(summaries)}"
s = summaries[0]
assert s["home_team"] == "Arsenal" and s["away_team"] == "Chelsea"
assert len(s["outcomes"]) == 3, "expected exactly one row per outcome, not per bookmaker"

arsenal = next(o for o in s["outcomes"] if o["name"] == "Arsenal")
# Exchange excluded => the market max is bovada's 2.45, not the exchange price.
assert abs(arsenal["max_price"] - 2.45) < 0.001, arsenal["max_price"]
assert abs(arsenal["min_price"] - 1.85) < 0.001, arsenal["min_price"]

# no-vig check: pinnacle now 1.85 / 3.30 / 3.40 -> overround 1.1236...
overround = 1 / 1.85 + 1 / 3.30 + 1 / 3.40
assert abs(arsenal["fair_price"] - 1.85 * overround) < 0.001, arsenal["fair_price"]
# Fair price must always be longer than the sharp book's own offered price.
assert arsenal["fair_price"] > 1.85

# 2.45 offered vs ~2.079 fair is a large edge -> value must be flagged.
assert s["has_value"], s["verdict"]
assert s["best_value"]["name"] == "Arsenal", s["best_value"]
assert s["has_move"], "the pinnacle shortening should register as movement"
assert "Ставить" in s["verdict"] and "от" in s["verdict"], s["verdict"]

# Only pinnacle shortened here, so confidence must stay low even though the
# percentage move was large -- breadth, not size, drives the stars.
assert s["stars"] <= 2, f"one book moving should not earn 3 stars, got {s['stars']}"
assert arsenal["down_count"] == 1, arsenal["down_count"]

msg = notifier._format_event(s)
assert "Arsenal" in msg and "Chelsea" in msg
assert "ИТОГ" in msg
assert "1.85–2.45" in msg, msg  # market range shown from-to
assert "просело у 1 из 2" in msg, msg

path = dashboard.render_dashboard(summaries)
assert os.path.exists(path)
with open(path, encoding="utf-8") as f:
    page = f.read()
assert "Arsenal" in page and "Сводка по рынку" in page
assert "betfair_ex_eu" not in page, "exchange must not appear on the dashboard"
assert "Азия" not in page and "Sharp vs Public" not in page, "region/sharp split should be merged away"
assert "Последний снимок" not in page and "Откуда данные" not in page

print("OK: flatten_odds + detector + analytics (no-vig) + notifier + dashboard verified.")
print(f"Verdict: {s['verdict']}")
print(f"Dashboard written to: {path}")
