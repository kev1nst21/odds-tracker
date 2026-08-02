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

# --- the bot must announce moves it is NOT betting -------------------------
# Requested 2026-08-01: "эти движения тоже мы должны озвучивать, хоть и не
# поставили, может кто-то поставит."
import notifier  # noqa: E402

sent = []
notifier.send_telegram_message = lambda text, **k: sent.append(text)

solo[0]["move_is_new"] = True
notifier.notify_summaries(solo, dashboard_url="https://example.test")
assert sent, "a move with no bet produced no message at all"
msg = sent[-1]
assert "Движения без ставки" in msg, msg[:300]
assert "двинулась одна контора" in msg, msg[:400]
assert "Riga II" in msg and "2.90" in msg, msg[:400]
print("notify ok: a move we did not bet is still announced, with the reason")

# a move already announced once must not be repeated on the next cycle
sent.clear()
solo[0]["move_is_new"] = False
notifier.notify_summaries(solo, dashboard_url="https://example.test")
assert not sent, "the same move was announced twice"
print("notify ok: announced once, then silent")

# --- the suspicion flag ----------------------------------------------------
# Requested 2026-08-01: "надо еще придумать как выслеживать договорняки".
# Four patterns we already measure; three of them together get flagged.
quiet = analytics._suspicion(
    {"drop_pct": -22.0, "down_count": 4, "sharp_moved": True, "spiked": True},
    "soccer_latvia_2")
assert quiet[0] >= 3, quiet
print(f"suspicion ok: quiet-league collapse scored {quiet[0]}/4 — {quiet[1]}")

# the same move in a top league is NOT flagged: there it is news, not a fix
major = analytics._suspicion(
    {"drop_pct": -22.0, "down_count": 4, "sharp_moved": True, "spiked": False},
    "soccer_uefa_champs_league")
assert major[0] < 3, major
print(f"suspicion ok: identical move in the Champions League scored {major[0]}/4 — not flagged")

# an ordinary signal must never be flagged
plain = analytics._suspicion(
    {"drop_pct": -11.0, "down_count": 2, "sharp_moved": False, "spiked": False},
    "soccer_epl")
assert plain[0] == 0, plain
print("suspicion ok: an ordinary 11% move at two books is not flagged")
