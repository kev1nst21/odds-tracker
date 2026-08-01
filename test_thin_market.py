"""Reproduces the 2026-08-01 funnel reading and checks the fix.

Production, 24 hours, widened line: 6 drops cleared the threshold and the
"market must have 4+ bookmakers" rule rejected 6 of 6. Nothing else in the
funnel rejected anything. Two separate faults sat behind that single number
and both are asserted here:

  1. small leagues -- the entire point of the wide sweep -- are quoted by
     three books, so demanding four guaranteed zero signals forever;
  2. a rejected move produced no summary at all, so it never reached the
     movements log either, and the "Движения" page showed 0 while the funnel
     counted 6.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "thin.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import analytics  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)
start = (now + timedelta(hours=4)).isoformat()


def row(book, side, price):
    return {"fixture_id": "small1", "sport_key": "soccer_latvia_2", "start_time": start,
            "home_team": "Riga II", "away_team": "Valmiera II", "bookmaker": book,
            "market_id": "h2h", "outcome_id": side, "outcome_name": "Riga II",
            "player_key": "-", "price": price}


def move(book, prev, price, sharp=False):
    return {"fixture_id": "small1", "outcome_id": "home", "bookmaker": book,
            "price": price, "prev_price": prev,
            "pct_change": (price - prev) / prev, "is_sharp_book": sharp}


# A three-bookmaker lower-division market. Two of them shortened 2.90 -> 2.55,
# the third has not moved yet -- textbook steam, and previously invisible.
records = [row("pinnacle", "home", 2.55), row("unibet_eu", "home", 2.55),
           row("betsson", "home", 2.90),
           row("pinnacle", "away", 1.45), row("unibet_eu", "away", 1.45),
           row("betsson", "away", 1.40)]
movements = [move("pinnacle", 2.90, 2.55, sharp=True), move("unibet_eu", 2.90, 2.55)]

summaries = analytics.build_event_summaries(records, [], movements)
assert summaries, "a 3-book market with two books moving produced no summary at all"
s = summaries[0]
assert s["bet"], "no bet picked from a two-of-three steam move"
assert s["alertable"], f"three-book steam move still not alertable: {s['verdict']}"
assert s["bet"]["entry_book"] == "betsson", s["bet"]
print(f"thin market ok: 2.90 -> 2.55 at 2 of 3 books, entry {s['bet']['entry_price']:.2f} "
      f"at {s['bet']['entry_book']}, alertable={s['alertable']}")

f = analytics.LAST_FUNNEL
assert f["big_drop"] == 1 and f["signals"] == 1, f
assert f["thin_market"] == 0, f
print(f"funnel ok: {f}")

# One lone bookmaker moving is still not a market move -- it must be logged
# as a movement but never sent as a signal.
storage.init_db()
solo = analytics.build_event_summaries(
    [row("unibet_eu", "home", 2.55), row("betsson", "home", 2.90),
     row("williamhill", "home", 2.88),
     row("unibet_eu", "away", 1.45), row("betsson", "away", 1.40),
     row("williamhill", "away", 1.41)],
    [], [move("unibet_eu", 2.90, 2.55)])
assert solo, "a solo move must still be RECORDED, just not alerted"
assert not solo[0]["alertable"], "one bookmaker moving was treated as a signal"
assert analytics.LAST_FUNNEL["thin_market"] == 1, analytics.LAST_FUNNEL
print("evidence ok: a single bookmaker moving is recorded but never alerted")

# And the recorded-but-not-alerted move has to reach the movements table --
# that is the bug that left the Движения page at zero.
assert storage.save_movement(solo[0], now.isoformat()), "solo move never reached movements"
assert storage.movement_stats()["total"] == 1, storage.movement_stats()
print("movements ok: a move we would not bet is still logged and shown")
