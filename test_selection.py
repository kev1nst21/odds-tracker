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

    pool = ["unibet_eu", "betsson", "williamhill", "bwin", "coolbet",
            "leovegas", "betclic", "tipico", "winamax", "marathonbet",
            "codere", "suprabets", "gtbets", "everygame", "888sport"]
    movers = pool[:books_moved]
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
ok = build(books_moved=config.MOVED_FOR_3_STARS, price_old=3.20, price_new=2.80,
           entry_price=3.15, lead_hours=6)
assert ok, "a clean steam move produced no summary at all"
s = ok[0]
assert s["stars"] == 3, s["stars"]
assert s["alertable"], f"a textbook signal was rejected: {s['verdict']}"
assert s["funnel_bucket"] == "signal", s["funnel_bucket"]
print(f"baseline ok: {config.MOVED_FOR_3_STARS} контор, {s['stars']}★, "
      f"вход {s['bet']['entry_price']:.2f}, публикуем")

# each rung must be reachable end-to-end, not just inside _stars()
for moved, want in ((config.MOVED_FOR_2_STARS, 2),
                    (config.MOVED_FOR_3_STARS, 3),
                    (config.MOVED_FOR_4_STARS, 4)):
    r = build(books_moved=moved, price_old=3.20, price_new=2.80,
              entry_price=3.15, lead_hours=6)
    assert r[0]["stars"] == want, (moved, want, r[0]["stars"])
    assert r[0]["alertable"], (moved, r[0]["verdict"])
print("ступени ok: 2★/3★/4★ доходят до публикации через весь конвейер")

# a sharp book lifts by one rung -- but on its own it must NOT open publication
sharp = build(books_moved=config.MOVED_FOR_2_STARS, price_old=3.20,
              price_new=2.80, entry_price=3.15, lead_hours=6, sharp=True)
assert sharp[0]["stars"] == 3, sharp[0]["stars"]
alone = build(books_moved=1, price_old=3.20, price_new=2.80, entry_price=3.15,
              lead_hours=6, sharp=True)
assert not alone[0]["alertable"], "одна шарп-контора открыла публикацию в одиночку"
print("baseline ok: шарп поднимает на ступень, но в одиночку не публикует")

# --- gate 1: below the publishing floor -------------------------------------
# 2026-08-15: the floor moved from three stars to two. Two stars is now a
# PUBLISHED tier, labelled "осторожно" and scored in its own row, so the gate
# this guards is one star -- a single bookmaker moving, which is not evidence
# of anything and must never reach the bot however the ladder is retuned.
solo = build(books_moved=1, price_old=3.20, price_new=2.80, entry_price=3.15,
             lead_hours=6)
assert solo, "a single-book move must still be RECORDED"
assert not solo[0]["alertable"], "a single-book move was published"
assert solo[0]["funnel_bucket"] == "thin_market", solo[0]["funnel_bucket"]
print("звёзды ok: одна контора — не рынок, в публикацию не идёт")

# THE QUALITY GATE ITSELF. Between MIN_MOVED_BOOKS and MOVED_FOR_2_STARS lies
# the band where a move is real enough to write down and too thin to bet. Until
# the analyst's recalibration these two numbers coincided, which made the
# low_stars bucket unreachable -- everything that survived "at least two books"
# published automatically, so there was no quality filter left anywhere in the
# pipeline. This assertion is what keeps that gap open.
assert config.MIN_MOVED_BOOKS < config.MOVED_FOR_2_STARS
thin = build(books_moved=config.MIN_MOVED_BOOKS, price_old=3.20, price_new=2.80,
             entry_price=3.15, lead_hours=6)
assert thin[0]["stars"] == 1, thin[0]["stars"]
assert not thin[0]["alertable"], "движение ниже нижней ступени было опубликовано"
assert thin[0]["funnel_bucket"] == "low_stars", thin[0]["funnel_bucket"]
assert analytics.LAST_FUNNEL["low_stars"] == 1, analytics.LAST_FUNNEL
# and it must still reach the movements ledger, or we would be hiding it
assert storage.save_movement(thin[0], now.isoformat()), "a skipped tier vanished entirely"
print(f"звёзды ok: {config.MIN_MOVED_BOOKS} конторы пишем в движения, но не "
      f"публикуем — фильтр по качеству работает")

# ...and the rung immediately above MUST publish. This is the change that is
# supposed to multiply the sample, so it gets its own assertion.
careful = build(books_moved=config.MOVED_FOR_2_STARS, price_old=3.20,
                price_new=2.80, entry_price=3.15, lead_hours=6)
assert careful[0]["stars"] == 2, careful[0]["stars"]
assert careful[0]["alertable"], "2★ обязаны публиковаться после 15.08"
assert careful[0]["funnel_bucket"] == "signal", careful[0]["funnel_bucket"]
print("звёзды ok: 2★ «осторожно» публикуем — это и есть рост объёма")

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

# --- the entry threshold must be falsifiable ---------------------------------
# ENTRY_MIN_CAPTURE_PCT is the single biggest killer of signals: most refused
# moves die there, not on stars, price band or horizon. The rule is sound --
# without it we would announce "было 3.20" and send someone to bet 2.40 -- but
# whether HALF is the right share was unanswerable, because the price we
# refused was never stored. Now it is, and a softer threshold can be scored
# instead of argued about.
storage.init_db()


def refused(fid, old, new, best):
    """A move where a price WAS still on offer but did not clear the floor."""
    return {
        "fixture_id": fid, "sport_key": "soccer_x",
        "start_time": (now + timedelta(hours=5)).isoformat(),
        "home_team": "A", "away_team": "B", "stars": 3,
        "has_entry": False, "alertable": False, "strategy": "aggressive",
        "funnel_bucket": "entry_too_low",
        "bet": {"side": "home", "name": "A", "old_price": old, "new_price": new,
                "drop_pct": (old - new) / old * 100, "down_count": 4,
                "books_count": 9, "entry_price": None, "entry_book": None,
                "best_left_price": best, "market_id": "h2h", "player_key": "-"},
        "optimal": None,
    }


# drop 4.00 -> 3.00. Floors: 50% needs 3.50, 40% needs 3.40, 30% needs 3.30.
storage.save_movement(refused("c1", 4.00, 3.00, 3.35), now.isoformat())  # only 30% takes it
storage.save_movement(refused("c2", 4.00, 3.00, 3.45), now.isoformat())  # 40% and 30%
storage.save_movement(refused("c3", 4.00, 3.00, 3.10), now.isoformat())  # nobody takes it

prev = storage.capture_threshold_preview((50, 40, 30))
by = {r["capture_pct"]: r["extra_signals"] for r in prev["rules"]}
assert prev["sample"] == 3, prev
assert by[50] == 0, by      # these were refused under the live rule by construction
assert by[40] == 1, by      # 3.45 clears a 40% floor
assert by[30] == 2, by      # 3.35 and 3.45 clear a 30% floor
print(f"порог входа ok: смягчение до 40% дало бы +{by[40]}, до 30% +{by[30]} "
      f"на выборке из {prev['sample']} отказов")

# and the refused price must actually survive the round-trip, or the whole
# preview is computed from nothing
row = [r for r in storage.recent_movements(10) if r["fixture_id"] == "c2"][0]
assert row["had_entry"] == 0 and row["was_signal"] == 0, dict(row)
print("порог входа ok: отказ записан вместе с ценой, которую мы отвергли")
