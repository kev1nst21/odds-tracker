"""The credit governor must never overspend, and never starve to zero.

This is the test that would have prevented both of this month's budget
failures, and it is written as two opposite assertions on purpose:

  * spend an entire period at the width the governor allows, and the total
    must come in UNDER the plan. That is the 8 August runaway;
  * hand it almost no credits, and it must still return a working tracker
    rather than zero. That is the 15 August starvation, where a cap set for a
    quiet week was still in force when the balance ran down.

The third property is the one the whole design rests on: MORE CREDITS MUST
WIDEN IT WITHOUT A CODE CHANGE. If that ever stops holding, buying a bigger
plan silently buys nothing, and the user would have paid for an upgrade that
never arrived -- a far worse failure than any of the above, because it looks
like success.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "budget.db")
os.environ.setdefault("DASHBOARD_DIR", tempfile.mkdtemp())

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import budget  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)
POLL = 20


def plan(remaining, poll=POLL):
    return budget.plan(remaining, now=now, poll_minutes=poll)


# --- 1. the cost of a sport key is markets x regions ------------------------
assert budget.credits_per_sport() == (
    len(config.MARKETS.split(",")) * len(config.REGIONS.split(","))
), budget.credits_per_sport()
print(f"инвариант ok: одна лига стоит {budget.credits_per_sport()} кр. "
      f"(рынки {config.MARKETS} × регионы {config.REGIONS})")

# --- 2. a whole period at the allowed width must fit inside the plan --------
# The governor is only worth having if this holds for balances of every size,
# so it is checked across four orders of magnitude rather than on one example.
for remaining in (1_200, 5_000, 20_000, 100_000, 5_000_000):
    p = plan(remaining)
    cycles = p["cycles_left"]
    spend = p["sports"] * p["credits_per_sport"] * cycles
    usable = remaining - config.QUOTA_RESERVE_CREDITS
    # The floor is allowed to overspend -- deliberately, because a tracker
    # narrowed to nothing is worse than one that runs the reserve down and
    # gets stopped by runner.py. Everywhere above the floor, it must fit.
    if p["sports"] > p["floor"]:
        assert spend <= usable, (remaining, spend, usable, p)
print("инвариант ok: за целый период ширина не выходит за остаток кредитов")

# --- 3. a starved plan still returns a working tracker, never zero ----------
p = plan(config.QUOTA_RESERVE_CREDITS + 1)
assert p["sports"] == p["floor"] >= 1, p
assert p["starved"], p
print(f"инвариант ok: на нуле сужается до {p['sports']} лиг и говорит об этом, а не до нуля")

# --- 4. MORE CREDITS MUST MEAN MORE MARKET ---------------------------------
widths = [plan(r)["sports"] for r in (2_000, 20_000, 100_000, 5_000_000)]
assert widths == sorted(widths), widths
assert widths[-1] > widths[0], widths
assert widths[-1] == config.MAX_SPORTS_PER_CYCLE, (widths, config.MAX_SPORTS_PER_CYCLE)
print(f"инвариант ok: рост кредитов расширяет охват {widths} и упирается "
      f"в потолок конфига ({config.MAX_SPORTS_PER_CYCLE}), а не выше")

# --- 5. a faster cadence buys itself out of breadth -------------------------
# Frequency is not free: at four times the polling rate the same balance has to
# cover four times the cycles. Stated as a test because it is the whole reason
# "давайте опрашивать каждые 5 минут" is not a free improvement.
wide_slow = plan(60_000, poll=20)["sports"]
wide_fast = plan(60_000, poll=5)["sports"]
assert wide_fast <= wide_slow, (wide_fast, wide_slow)
print(f"инвариант ok: на тех же кредитах 20 мин даёт {wide_slow} лиг/цикл, "
      f"а 5 мин — {wide_fast}; частота оплачивается охватом")

# --- 6. an extra region multiplies the bill for every league ----------------
before = budget.credits_per_sport()
config.REGIONS = "eu,uk"
budget.REGIONS = "eu,uk"
after = budget.credits_per_sport()
assert after == before * 2, (before, after)
narrow = plan(60_000)["sports"]
config.REGIONS = "eu"
budget.REGIONS = "eu"
assert narrow <= wide_slow, (narrow, wide_slow)
print(f"инвариант ok: второй регион удваивает цену лиги ({before} → {after}) "
      f"и сужает охват {wide_slow} → {narrow}, если кредиты не добавить")

# --- 7. a rollover is still noticed, even though the calendar now rules ----
# observe() keeps watching `used` fall, because a contradiction between the
# stated rule and observed reality is worth recording. It just is not what
# sizes the cycle any more.
storage.set_meta("quota_used_seen", "18000")
budget.observe({"used": 120, "remaining": 19_880}, now)
assert storage.get_meta("quota_period_start"), "откат плана не замечен"
# and it must NOT fire on the ordinary case of the counter climbing
storage.set_meta("quota_period_start", "")
budget.observe({"used": 500, "remaining": 19_500}, now)
budget.observe({"used": 700, "remaining": 19_300}, now)
assert not storage.get_meta("quota_period_start"), "обычный расход принят за откат"
print("инвариант ok: откат плана распознаётся по падению used, обычный расход — нет")

# --- 8. the reset is the 1st of the month, not an anniversary --------------
# Taken verbatim from the provider's own dashboard on 2026-08-15: "Monthly
# plans reset on the 1st of each month at 12AM UTC". Before this the code
# guessed a rolling 30 days and under-spent the balance by up to half.
for stamp, expect in ((datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc), 16.5),
                      (datetime(2026, 8, 31, 0, 0, tzinfo=timezone.utc), 1.0),
                      (datetime(2026, 12, 30, 0, 0, tzinfo=timezone.utc), 2.0),
                      (datetime(2026, 2, 10, 0, 0, tzinfo=timezone.utc), 19.0)):
    got = budget._period_days_left(stamp)
    assert abs(got - expect) < 0.01, (stamp, got, expect)
# the last hour of the month must not hand the whole balance to one cycle
assert budget._period_days_left(
    datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc)) >= 0.5
print("инвариант ok: горизонт считается до 1-го числа (в т.ч. через декабрь "
      "и февраль), и в последний час месяца не схлопывается в ноль")

# --- 8b. a balance is spent before it expires, not hoarded -----------------
mid = plan(60_000, poll=20)
end = budget.plan(60_000, now=datetime(2026, 8, 29, tzinfo=timezone.utc), poll_minutes=20)
assert end["sports"] >= mid["sports"], (end["sports"], mid["sports"])
print(f"инвариант ok: ближе к сбросу тот же остаток тратится шире "
      f"({mid['sports']} → {end['sports']}), а не сгорает")

# --- 9. no data yet must fall back DOWN, not up ----------------------------
# See invariant 13 for the live bug that rewrote this one. An unknown balance
# is not permission to spend the maximum.
storage.set_meta("quota_remaining_seen", "")
p = plan(None)
assert p["reason"] == "no-data", p
assert p["sports"] == min(config.MAX_SPORTS_PER_CYCLE, config.COLD_START_SPORTS), p
print(f"инвариант ok: до первого ответа API берём осторожные {p['sports']} лиг")

print("бюджетный регулятор: все инварианты пройдены")

# --- 10. a narrow budget must not starve the core out of the rotation -------
# Under a fixed cap the wide sweep could safely claim WIDE_MIN_SLOTS, because
# the cap was always more than twice that. With the governor the cap can fall
# to the floor, and the same reservation then took every slot -- dropping every
# tennis tournament exactly when settleable bets matter most.
import odds_client  # noqa: E402

fake = ([{"key": f"tennis_{i}", "group": "Tennis", "active": True} for i in range(6)]
        + [{"key": f"soccer_low_{i}", "group": "Soccer", "active": True} for i in range(40)])
for cap in (3, 6, 9, 15, 30):
    picked = odds_client._select_within(fake, cap)
    assert len(picked) <= cap, (cap, len(picked))
    tennis = [k for k in picked if k.startswith("tennis_")]
    assert tennis, f"при cap={cap} теннис выпал из цикла целиком: {picked}"
    wide = [k for k in picked if k.startswith("soccer_low_")]
    assert wide, f"при cap={cap} широкий обход выпал целиком: {picked}"
print("инвариант ok: при любом бюджете в цикле остаётся и теннис, и широкий обход")

print("охват под бюджетом: все инварианты пройдены")

# --- 11. the region ladder must climb with the balance, never starve it -----
# Bookmakers are what the funnel is short of, and region is the only lever that
# adds them. The promise this module makes is that paying for a bigger plan
# widens the product with no commit -- so the ladder is tested exactly like the
# league width: monotonic in credits, capped, and never bought by gutting the
# league list.
import importlib  # noqa: E402
importlib.reload(budget)

rungs = []
for remaining in (1_200, 20_000, 100_000, 5_000_000):
    p = plan(remaining)
    n = len(p["regions"].split(","))
    rungs.append(n)
    # whatever it picked, the arithmetic must still fit the plan
    if p["sports"] > p["floor"]:
        spend = p["sports"] * p["credits_per_sport"] * p["cycles_left"]
        assert spend <= remaining - config.QUOTA_RESERVE_CREDITS, (remaining, spend, p)
    # and a region is never taken at the price of a gutted league list
    if n > 1:
        assert p["sports"] >= config.REGION_STEP_MIN_SPORTS, (remaining, p)
assert rungs == sorted(rungs), rungs
assert rungs[0] == 1, rungs
assert rungs[-1] == len(config.REGION_LADDER.split(",")), rungs
print(f"инвариант ok: лестница регионов растёт с балансом {rungs} и упирается "
      f"в {config.REGION_LADDER}")

# the small plan we are on today must be left exactly as it is
p = plan(1_137)
assert p["regions"] == "eu", p["regions"]
assert p["credits_per_sport"] == 1, p
print("инвариант ok: на текущем балансе регион остаётся один — апгрейд не имитируется")

# and on the 5M plan at a 5-minute cadence the full scenario must be affordable
p = plan(5_000_000, poll=5)
month = p["sports"] * p["credits_per_sport"] * (30 * 24 * 60 / 5)
assert p["sports"] == config.MAX_SPORTS_PER_CYCLE and len(p["regions"].split(",")) == 4, p
assert month < 5_000_000, month
print(f"инвариант ok: план 5M при опросе раз в 5 мин даёт {p['sports']} лиг × "
      f"{p['regions']} = {month:,.0f} кр./мес, укладывается".replace(",", " "))

# --- 12. what the fetch actually asks for is what the plan paid for ---------
plan(5_000_000)
assert budget.active_regions() == "eu,uk,au,us", budget.active_regions()
plan(1_137)
assert budget.active_regions() == "eu", budget.active_regions()
print("инвариант ok: запрос к API идёт с теми регионами, которые посчитал бюджет")

print("лестница регионов: все инварианты пройдены")

# --- 13. an unknown balance must never authorise the largest spend ----------
# Shipped live and caught by the published ledger on 2026-08-15: the quota only
# arrives in a response header, but the width is chosen BEFORE the first
# request, and the sports list is served from cache -- so a whole cycle can
# complete without LAST_QUOTA ever being filled. The governor read None and
# politely handed back the full ambition, on a day the plan had 1 137 credits.
storage.set_meta("quota_remaining_seen", "")
p = plan(None)
assert p["sports"] == min(config.MAX_SPORTS_PER_CYCLE, config.COLD_START_SPORTS), p
assert p["sports"] < config.MAX_SPORTS_PER_CYCLE, p
print(f"инвариант ok: без данных берём осторожные {p['sports']} лиг, "
      f"а не максимальные {config.MAX_SPORTS_PER_CYCLE}")

# and once a cycle has seen the balance, the next one sizes itself properly
budget.remember({"remaining": 1_137})
p = plan(None)
assert p["reason"] == "budget", p
assert p["remaining"] == 1_137, p
assert p["sports"] == p["floor"], p
print(f"инвариант ok: баланс запоминается между циклами — {p['remaining']} кр. "
      f"→ {p['sports']} лиг ещё до первого запроса")

budget.remember({"remaining": 5_000_000})
p = plan(None)
assert p["sports"] == config.MAX_SPORTS_PER_CYCLE and p["regions"] == config.REGION_LADDER, p
print("инвариант ok: после апгрейда следующий же цикл берёт полный охват и все регионы")

print("холодный старт: все инварианты пройдены")
