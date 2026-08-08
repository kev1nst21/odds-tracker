"""The 2026-08-08 tightening: three stars, price band, publishing horizon.

Decided from the first 40 signals rather than from taste. Two-star signals had
gone 1 for 7 and lost $796; three-star had gone 7 for 16 and made $1 980. The
instruction was "будем работать на качество" -- publish fewer events, each one
confirmed by several independent books, at a price we would really take, for a
match close enough that the quoted price still means something.

Every gate here is asserted in BOTH directions. A filter that only ever gets
tested on what it rejects is how you end up shipping one that rejects
everything, and an empty page looks exactly like a quiet market.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "sel.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import analytics  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)


def build(books_moved, price_old, price_new, entry_price, lead_hours, sharp=False):
    """A market where `books_moved` books shortened and one still lags."""
    start = (now + timedelta(hours=lead_hours)).isoformat()

    def row(book, side, price):
        return {"fixture_id": "f1", "sport_key": "soccer_latvia_2", "start_time": start,
                "home_team": "Riga II", "away_team": "Valmiera II", "bookmaker": book,
                "market_id": "h2h", "outcome_id": side, "outcome_name": "Riga II",
                "player_key": "-", "price": price}

    movers = ["unibet_eu", "betsson", "williamhill", "bwin", "coolbet"][:books_moved]
    if sharp:
        movers[0] = "pinnacle"
    records, moves = [], []
    for b in movers:
        records += [row(b, "home", price_new), row(b, "away", 1.45)]
        moves.append({"fixture_id": "f1", "outcome_id": "home", "bookmaker": b,
                      "price": price_new, "prev_price": price_old,
                      "pct_change": (price_new - price_old) / price_old,
                      "is_sharp_book": b == "pinnacle"})
    # the laggard still showing the old price -- this is the entry
    records += [row("nordicbet", "home", entry_price), row("nordicbet", "away", 1.40)]
    return analytics.build_event_summaries(records, [], moves)


# --- the baseline: everything in order, must still publish -------------------
ok = build(books_moved=4, price_old=3.20, price_new=2.80, entry_price=3.15,
           lead_hours=6)
assert ok, "a clean four-book steam move produced no summary at all"
s = ok[0]
assert s["stars"] == 3, s["stars"]
assert s["alertable"], f"a textbook signal was rejected: {s['verdict']}"
assert s["funnel_bucket"] == "signal", s["funnel_bucket"]
print(f"baseline ok: 4 books, {s['stars']}★, вход {s['bet']['entry_price']:.2f}, публикуем")

# a sharp book counts double, so two books INCLUDING pinnacle is still 3 stars
sharp = build(books_moved=2, price_old=3.20, price_new=2.80, entry_price=3.15,
              lead_hours=6, sharp=True)
assert sharp[0]["stars"] == 3 and sharp[0]["alertable"], sharp[0]["stars"]
print("baseline ok: две конторы, но одна из них Pinnacle — это те же 3★")

# --- gate 1: fewer than three stars -----------------------------------------
weak = build(books_moved=2, price_old=3.20, price_new=2.80, entry_price=3.15,
             lead_hours=6)
assert weak, "a two-star move must still be RECORDED"
assert weak[0]["stars"] == 2, weak[0]["stars"]
assert not weak[0]["alertable"], "a two-star signal was published"
assert weak[0]["funnel_bucket"] == "low_stars", weak[0]["funnel_bucket"]
assert analytics.LAST_FUNNEL["low_stars"] == 1, analytics.LAST_FUNNEL
# and it must still reach the movements ledger, or we would be hiding it
assert storage.save_movement(weak[0], now.isoformat()), "a skipped tier vanished entirely"
print("звёзды ok: 2★ пишем в движения, но не публикуем как сигнал")

# --- gate 2: the price band -------------------------------------------------
# Above the band the market is filtered out upstream too, so the event does not
# even form -- belt and braces, and this proves the belt.
high = build(books_moved=4, price_old=6.40, price_new=5.60, entry_price=6.30,
             lead_hours=6)
assert not (high and high[0]["alertable"]), "a signal above the price band was published"
low = build(books_moved=4, price_old=1.62, price_new=1.42, entry_price=1.60,
            lead_hours=6)
assert not (low and low[0]["alertable"]), "a signal below the price band was published"
edge = build(books_moved=4, price_old=5.00, price_new=4.35, entry_price=4.95,
             lead_hours=6)
assert edge and edge[0]["alertable"], "4.95 sits inside the band and must publish"
print(f"полоса ok: {config.MIN_SIGNAL_PRICE:g}–{config.MAX_SIGNAL_PRICE:g}, "
      f"4.95 проходит, 6.30 и 1.60 нет")

# --- gate 3: the publishing horizon -----------------------------------------
far = build(books_moved=4, price_old=3.20, price_new=2.80, entry_price=3.15,
            lead_hours=config.MAX_LEAD_HOURS + 12)
assert far, "a distant move must still be recorded"
assert not far[0]["alertable"], "a match beyond the horizon was published"
assert far[0]["funnel_bucket"] == "too_far", far[0]["funnel_bucket"]
near = build(books_moved=4, price_old=3.20, price_new=2.80, entry_price=3.15,
             lead_hours=config.MAX_LEAD_HOURS - 12)
assert near[0]["alertable"], "a match inside the horizon was refused"
print(f"горизонт ok: {config.MAX_LEAD_HOURS:g} ч — "
      f"{config.MAX_LEAD_HOURS - 12:g} ч публикуем, {config.MAX_LEAD_HOURS + 12:g} ч нет")

# --- the funnel must name the new reasons -----------------------------------
import dashboard  # noqa: E402

block = dashboard._funnel_block(
    {"big_drop": 9, "thin_market": 1, "all_books_moved": 1, "entry_too_low": 1,
     "low_stars": 3, "off_band": 1, "too_far": 1, "signals": 1}, "24 часа")
for needle in ("звёзд", "вне полосы", "дальше"):
    assert needle in block, f"funnel does not explain «{needle}»: {block[:400]}"
print("воронка ok: новые причины отсева названы человеческим языком")
