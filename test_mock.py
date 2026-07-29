"""Exercises flatten_odds -> detector -> analytics -> notifier -> dashboard on
synthetic The-Odds-API-shaped data.

The key thing under test is the strategy itself: money goes into one outcome,
its price drops at several books, and the bet is that SAME outcome at whichever
book hasn't moved yet -- never the opposite side just because its price rose.
"""
import os
from datetime import datetime, timedelta, timezone

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

SLOW_BOOK = "bovada"          # never moves -- this is where the bet lands
MOVERS = ["pinnacle", "betclic_fr", "betsson", "coolbet", "sport888"]


def build(home_price):
    """Full 3-way market. home_price is applied to the movers; SLOW_BOOK keeps
    the original 3.00 so there is somewhere left to bet."""
    books = {b: {"Arsenal": home_price, "Draw": 3.40, "Chelsea": 2.30} for b in MOVERS}
    books[SLOW_BOOK] = {"Arsenal": 3.00, "Draw": 3.40, "Chelsea": 2.30}
    books["betfair_ex_eu"] = {"Arsenal": 9.20, "Draw": 3.40, "Chelsea": 2.30}  # exchange noise
    return {
        "id": "evt_test_1", "sport_key": "soccer_epl", "sport_title": "EPL",
        "commence_time": "2026-08-01T18:00:00Z",
        "home_team": "Arsenal", "away_team": "Chelsea",
        "bookmakers": [
            {"key": b, "markets": [{"key": "h2h", "outcomes": [
                {"name": n, "price": p} for n, p in o.items()]}]}
            for b, o in books.items()
        ],
    }


# Poll 1: Arsenal is 3.00 everywhere.
poll_1 = odds_client.flatten_odds([build(3.00)])
detector.detect(poll_1, t0.isoformat())
storage.save_snapshot(poll_1, t0.isoformat())

# Poll 2: money hits Arsenal -- 3.00 -> 2.10 (-30%) at five books. bovada lags.
poll_2 = odds_client.flatten_odds([build(2.10)])
spikes, movements = detector.detect(poll_2, t1.isoformat())
storage.save_snapshot(poll_2, t1.isoformat())

assert all(s["bookmaker"] != "betfair_ex_eu" for s in spikes), "exchange leaked into spikes"
assert all(m["outcome_id"] != "draw" for m in movements), "draw should never produce a signal"

summaries = analytics.build_event_summaries(poll_2, spikes, movements)
assert len(summaries) == 1
s = summaries[0]

assert [o["name"] for o in s["outcomes"]] == ["Arsenal", "Chelsea"], \
    f"draw must not be shown, got {[o['name'] for o in s['outcomes']]}"

bet = s["bet"]
assert bet["name"] == "Arsenal", f"bet must follow the money, got {bet['name']}"
assert abs(bet["old_price"] - 3.00) < 0.001, bet["old_price"]
assert abs(bet["new_price"] - 2.10) < 0.001, bet["new_price"]
assert bet["down_count"] == 5, bet["down_count"]

# The entry is the book that did NOT move, still offering the old 3.00.
assert s["has_entry"], s["verdict"]
assert bet["entry_book"] == SLOW_BOOK, bet["entry_book"]
assert abs(bet["entry_price"] - 3.00) < 0.001, bet["entry_price"]

# Chelsea drifted nowhere here, but the rule is absolute: the opposite side is
# never the bet, whatever its price did.
assert bet["side"] == "home"

assert s["alertable"], "a 30% drop with an open entry must reach the bot"
assert s["stars"] == 3, s["stars"]
assert "был коэффициент 3.00" in s["verdict"], s["verdict"]
assert "просел до 2.10" in s["verdict"], s["verdict"]
assert "проставить" in s["verdict"], s["verdict"]

msg = notifier._format_event(s)
assert "СТАВИМ Arsenal за 3.00" in msg, msg
assert "Ничья" not in msg, "draw must not appear in the alert"
assert "3.00" in msg and "2.10" in msg

# Sub-threshold moves must not be pushed to the bot.
small = dict(s)
small["bet"] = dict(bet, drop_pct=-4.0)
small["alertable"] = False
sent = []
notifier.send_telegram_message = lambda t: sent.append(t)
notifier.notify_summaries([small])
assert not sent, "a 4% move must never be sent to the bot"
notifier.notify_summaries([s])
assert sent and "СТАВИМ" in sent[0], "a 30% move with an entry must be sent"

path = dashboard.render_dashboard(summaries)
with open(path, encoding="utf-8") as f:
    page = f.read()
assert "Arsenal" in page and "СТАВИМ" in page
assert "betfair_ex_eu" not in page
# "Ничья" is allowed in the explanatory note, but never inside an event card.
cards = page.split("<div class='ev ")[1:]
assert cards and all("Ничья" not in c for c in cards), "draw must not appear in any card"

print("OK: money-flow strategy, draw exclusion, 10% gate, exchange filter all verified.")
print(f"Verdict: {s['verdict']}")
print(f"Dashboard: {path}")
