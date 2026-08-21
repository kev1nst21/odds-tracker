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


# 21.08.2026 -- фикстура расширена с трёх контор до восьми.
#
# Не потому, что прежний тест был неправ: 01.08 он ловил настоящую поломку,
# когда требование «минимум 4 конторы» отсеивало 6 падений из 6 и широкий
# обход не давал ничего. Но с тех пор поменялась сама задача. Тогда малые лиги
# были точкой роста на плане в 20 тысяч кредитов; сейчас у нас 126 лиг за цикл
# и рынки по 40-54 конторы, а основой продукта стал Polymarket, который
# латвийский второй дивизион и предварительные раунды Кубка Англии не котирует
# в принципе.
#
# Поэтому порог глубины поднят до восьми, и фикстура приведена к тому, что
# суть теста -- «малую лигу нельзя выбрасывать молча» -- проверяет на рынке,
# который сегодня считается рынком. Само правило осталось: движение в малой
# лиге обязано доходить и до сигнала, и до журнала движений.
BOOKS = ["pinnacle", "betsson", "williamhill", "onexbet",
         "marathonbet", "unibet", "betfair", "coolbet"]


def row(book, side, price):
    return {"fixture_id": "small1", "sport_key": "soccer_latvia_2", "start_time": start,
            "home_team": "Riga II", "away_team": "Valmiera II", "bookmaker": book,
            "market_id": "h2h", "outcome_id": side, "outcome_name": "Riga II",
            "player_key": "-", "price": price}


def move(book, prev, price, sharp=False):
    return {"fixture_id": "small1", "outcome_id": "home", "bookmaker": book,
            "price": price, "prev_price": prev,
            "pct_change": (price - prev) / prev, "is_sharp_book": sharp}


# Рынок нижнего дивизиона: восемь контор, три из них укоротили 2.90 -> 2.55,
# остальные ещё стоят. Классический steam, который раньше был невидим.
MOVERS = BOOKS[:3]
records = ([row(b, "home", 2.55) for b in MOVERS]
           + [row(b, "home", 2.90) for b in BOOKS[3:]]
           + [row(b, "away", 1.45) for b in MOVERS]
           + [row(b, "away", 1.40) for b in BOOKS[3:]])
movements = [move(MOVERS[0], 2.90, 2.55, sharp=True)] + \
            [move(b, 2.90, 2.55) for b in MOVERS[1:]]

summaries = analytics.build_event_summaries(records, [], movements)
assert summaries, "рынок малой лиги не дал сводки вообще"
s = summaries[0]
assert s["bet"], "из движения трёх контор не выбрана ставка"
assert s["alertable"], f"движение в малой лиге не стало сигналом: {s['verdict']}"
assert s["bet"]["entry_book"] in BOOKS[3:], s["bet"]
print(f"thin market ok: 2.90 -> 2.55 у {len(MOVERS)} из {len(BOOKS)} контор, "
      f"вход {s['bet']['entry_price']:.2f} у {s['bet']['entry_book']}, "
      f"alertable={s['alertable']}")

f = analytics.LAST_FUNNEL
assert f["big_drop"] == 1 and f["signals"] == 1, f
assert f["thin_market"] == 0, f
print(f"funnel ok: {f}")

# One lone bookmaker moving is still not a market move -- it must be logged
# as a movement but never sent as a signal.
storage.init_db()
solo = analytics.build_event_summaries(
    [row(BOOKS[0], "home", 2.55)] + [row(b, "home", 2.90) for b in BOOKS[1:]]
    + [row(BOOKS[0], "away", 1.45)] + [row(b, "away", 1.40) for b in BOOKS[1:]],
    [], [move(BOOKS[0], 2.90, 2.55, sharp=True)])
assert solo, "одиночное движение обязано попасть в журнал, даже не став сигналом"
assert not solo[0]["alertable"], (
    "одна контора была принята за сигнал: " + str(solo[0]["verdict"])[:200])
# Ведро называется thin_market, но ловит оно здесь другое: в нём оказывается
# всё, что не набрало доказательности, а не только мелкий рынок. Имя ведра
# осталось от 01.08, когда эти два случая совпадали. Проверяем то, что есть, а
# не то, что хотелось бы прочитать по названию.
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

# --- "был вход" must not mean "we bet it" ----------------------------------
# Reported 2026-08-07: a movement row read "был вход" for Nongshim Redforce
# Challengers — KT Rolster Challengers, but the event was in neither strategy.
# A takeable price and an actual signal are different facts.
import dashboard  # noqa: E402

storage.init_db()
solo[0]["move_is_new"] = True
assert solo[0]["has_entry"] and not solo[0]["alertable"], (
    "fixture must have a price on offer but no signal", solo[0].get("verdict"))
storage.save_movement(solo[0], now.isoformat())
rows = storage.recent_movements(10)
row = [r for r in rows if r["fixture_id"] == "small1"][0]
assert row["had_entry"] == 1, "the price was on offer"
assert row["was_signal"] == 0, "but it was never a signal"
html = dashboard._movements_table(rows)
assert "цена была, но не ставили" in html, html[:400]
assert "поставили</span>" not in html.replace("но не ставили", ""), "claimed a bet it never made"
print("movements ok: a takeable price that never became a signal says so plainly")

# --- the bot must report even when nothing fired -----------------------------
# Requested 2026-08-10: "чтобы отчеты были в бота, а то сижу втыкаю". Until now
# the bot spoke only on a signal, so a quiet market and a dead poller looked
# identical from outside — which is precisely how a 46-hour outage and a day of
# detector blindness went unnoticed this week. The digest reports the
# MEASUREMENT, so silence stops being ambiguous.
sent.clear()
notifier.notify_digest({
    "hours": 3, "threshold": 10,
    "lines_watched": 380, "lines_blind": 0, "lines_moved": 108,
    "movements": 0, "signals": 0,
    "open_bets": 0, "credits": 6524, "days_left": 12.4, "poll_minutes": 20,
}, dashboard_url="https://example.test")
assert sent, "тихий рынок не дал вообще никакого отчёта"
quiet = sent[-1]
assert "380" in quiet and "108" in quiet, quiet
assert "рынок стоял" in quiet, quiet
assert "6 524" in quiet, quiet          # credits, space-grouped
print("сводка ok: при нуле сигналов бот всё равно отчитывается измерением")

# with signals and a funnel, the reasons must be named -- and only the ones
# that actually caught something, so a clean funnel stays one short line
sent.clear()
notifier.notify_digest({
    "hours": 3, "threshold": 10,
    "lines_watched": 400, "lines_blind": 12, "lines_moved": 90,
    "movements": 5, "signals": 2,
    "all_books_moved": 2, "entry_too_low": 1, "low_stars": 0, "too_far": 0,
    "open_bets": 3, "next_start": "11.08 18:00 UTC",
    "credits": 6000, "poll_minutes": 20,
}, dashboard_url="https://example.test")
busy = sent[-1]
assert "Сигналов: <b>2</b>" in busy, busy
assert "просело у всех" in busy and "вход не дотянул" in busy, busy
assert "меньше трёх звёзд" not in busy, "пустой бакет попал в отчёт"
assert "11.08 18:00 UTC" in busy and "388" in busy, busy   # 400 - 12 compared
print("сводка ok: причины отсева названы, пустые бакеты не печатаются")
