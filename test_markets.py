"""Buying spreads and totals must add lines, never invent movements.

Written 20.08.2026, the same hour MARKETS went from "h2h" to
"h2h,spreads,totals" on Vladislav's instruction to stop wasting 83% of a plan
that expires on the 1st.

The change looks like one config string. It is not. Until today every price we
stored had no line attached, because a moneyline has none -- so the identity of
a series was (fixture, book, market, outcome) and player_key was the literal
'-'. A total has a line. "Over 2.5" and "Over 3.5" are the SAME market and the
SAME outcome name at the same bookmaker, and they are different bets at very
different prices.

Collapse them and the detector does not lose a signal, which would be
survivable. It diffs 2.60 against 1.90, calls it a 37% move, and publishes it.
A fabricated signal is worse than a missing one: it enters the book, it enters
the by-stars table, and nothing downstream can tell it from a real one.

So this file pins two things that a config string cannot express on its own:
the line is part of a series' identity, and a market we merely BUY is not
thereby a market we ACT on.
"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "markets.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
storage.DB_PATH = config.DB_PATH
storage.init_db()

import odds_client  # noqa: E402
import detector  # noqa: E402
import budget  # noqa: E402

# Inside the publication horizon, or _flatten drops the event before it ever
# reaches the code under test.
from datetime import datetime, timedelta, timezone  # noqa: E402
_NOW = datetime.now(timezone.utc)
FAR = (_NOW + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
T0 = (_NOW - timedelta(minutes=90)).isoformat()
T1 = _NOW.isoformat()


def event(markets):
    return {
        "id": "evt-1", "commence_time": FAR,
        "_sport_key": "soccer_epl", "sport_title": "EPL",
        "home_team": "Arsenal", "away_team": "Chelsea",
        "bookmakers": [{"key": "pinnacle", "markets": markets}],
    }


def key(r):
    return (r["fixture_id"], r["bookmaker"], r["market_id"],
            r["outcome_id"], r["player_key"])


# --- 1. the line is part of the identity ------------------------------------
recs = odds_client.flatten_odds([event([
    {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.10},
                                {"name": "Chelsea", "price": 3.40}]},
    {"key": "totals", "outcomes": [{"name": "Over", "price": 1.90, "point": 2.5},
                                   {"name": "Under", "price": 1.95, "point": 2.5},
                                   {"name": "Over", "price": 2.60, "point": 3.5},
                                   {"name": "Under", "price": 1.48, "point": 3.5}]},
    {"key": "spreads", "outcomes": [{"name": "Arsenal", "price": 1.90, "point": -0.5},
                                    {"name": "Arsenal", "price": 2.70, "point": -1.5}]},
])])
keys = [key(r) for r in recs]
assert len(keys) == len(set(keys)), (
    "две котировки схлопнулись в один ключ — детектор увидит между ними "
    f"движение, которого не было: {[k for k in keys if keys.count(k) > 1]}")

overs = [r for r in recs if r["market_id"] == "totals" and r["outcome_id"] == "over"]
assert len(overs) == 2, overs
assert {r["player_key"] for r in overs} == {"2.5", "3.5"}, overs
assert {r["price"] for r in overs} == {1.90, 2.60}, overs
print("claim ok: Over 2.5 и Over 3.5 — две разные серии, а не одна прыгающая цена")

spr = sorted((r["player_key"], r["price"]) for r in recs if r["market_id"] == "spreads")
assert spr == [("-0.5", 1.90), ("-1.5", 2.70)], spr
print("claim ok: фора -0.5 и -1.5 у одной конторы тоже не смешиваются")

# --- 2. h2h history must survive the change ---------------------------------
# Every row already in the book was written with player_key '-'. If a
# moneyline started carrying anything else, every baseline lookup would miss
# and the tracker would go blind on its own history the moment this shipped.
h2h = [r for r in recs if r["market_id"] == "h2h"]
assert h2h and all(r["player_key"] == "-" for r in h2h), h2h
assert {r["outcome_id"] for r in h2h} == {"home", "away"}, h2h
print("claim ok: у moneyline player_key остаётся '-' — старая история не осиротела")

# --- 3. the phantom move that this test exists to prevent -------------------
# The real sequence: a book holds Over 2.5 at 1.90, then moves its line to 3.5
# and quotes 2.60. Same book, same market, same outcome name, no price move at
# all -- the 2.5 line simply stopped being offered.
storage.save_snapshot(
    odds_client.flatten_odds([event([
        {"key": "totals", "outcomes": [{"name": "Over", "price": 1.90, "point": 2.5}]}])]),
    T0)
later = odds_client.flatten_odds([event([
    {"key": "totals", "outcomes": [{"name": "Over", "price": 2.60, "point": 3.5}]}])])
_, moves = detector.detect(later, T1)
assert not moves, (
    "сдвиг линии 2.5 → 3.5 показан как движение цены 1.90 → 2.60 (+37%) — "
    f"это выдуманный сигнал: {moves}")
# kept here, not read at the end: LAST_DIAG describes the most recent detect()
# call, and the next one below is h2h-only.
DIAG_TOTALS = dict(detector.LAST_DIAG)
print("claim ok: перенос тотала 2.5 → 3.5 не выдаётся за движение цены")

# and the same series really does still report a real move
storage.save_snapshot(
    odds_client.flatten_odds([event([
        {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.60}]}])]),
    T0)
_, real = detector.detect(
    odds_client.flatten_odds([event([
        {"key": "h2h", "outcomes": [{"name": "Arsenal", "price": 2.10}]}])]),
    T1)
assert real and real[0]["prev_price"] == 2.60 and real[0]["price"] == 2.10, real
print("claim ok: настоящее движение по той же линии по-прежнему ловится")

# --- 4. bought is not the same as acted upon --------------------------------
diag = DIAG_TOTALS
assert "totals" in diag["by_market"], diag
assert "totals" not in diag.get("by_market_signal", {}), (
    "тотал попал в генерацию сигналов — analytics строит ставку из стороны "
    "home/away и на «Over» выдаст бессмысленную строку в журнал", diag)
assert set(diag.get("by_market_signal", {})) <= set(
    m.strip() for m in config.SIGNAL_MARKETS.split(",")), diag
print(f"claim ok: покупаем {config.MARKETS}, сигналы делаем только из "
      f"{config.SIGNAL_MARKETS} — счётчик by_market показывает разницу")

# --- 5. and the price of that decision must be visible to the governor ------
n_markets = len([m for m in config.MARKETS.split(",") if m.strip()])
assert budget.credits_per_sport("eu") == n_markets, budget.credits_per_sport("eu")
assert budget.credits_per_sport("eu,uk,au,us") == n_markets * 4
print(f"claim ok: губернатор знает цену — лига стоит {n_markets} кр. в одном "
      f"регионе и {n_markets * 4} в четырёх, а не осталась на старой цифре")

print("рынки: все инварианты пройдены")
