"""The silent blindness of 2026-08-09, reproduced and fenced off.

The tracker went a full day with zero signals. Nothing was broken in any of the
filters that had just been tightened -- the funnel showed ONE movement in
seventeen hours, so nothing ever reached the star, price or horizon gates at
all. The fault was two config values that only interact through the calendar:

  * the wide sweep took 5 slots per cycle across ~40 obscure leagues, so a full
    lap took 8 cycles = FOUR HOURS at the 30-minute cadence;
  * a baseline older than 180 minutes was rejected as "history, not a
    baseline".

So every wide-list line was revisited an hour after its baseline had expired,
returned None, and was skipped without a word. Only the core leagues -- polled
every cycle -- could still be measured, which is why the single movement that
did appear came from the Belgian first division.

It hid for two reasons. On the 5-minute cadence the same lap took 40 minutes
and fitted easily; and after the cadence changed, the database still held dense
history from the fast era, so baselines kept resolving anyway. The v5 restart
wiped that history and the breakage became total.

The lesson this file encodes: the max-age rail is not a property of the
measurement, it is a CONSTRAINT ON THE SCHEDULE. Whenever cadence, slot counts
or the wide list change, the lap must still fit inside it -- and if it does
not, the run has to say so out loud instead of reporting a calm market.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "gap.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import detector  # noqa: E402
import odds_client  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)


def line(price, sport="soccer_latvia_2", fid="f1"):
    return {"fixture_id": fid, "sport_key": sport,
            "start_time": (now + timedelta(hours=10)).isoformat(),
            "home_team": "Riga II", "away_team": "Valmiera II",
            "bookmaker": "unibet_eu", "market_id": "h2h", "outcome_id": "home",
            "outcome_name": "Riga II", "player_key": "-", "price": price}


# --- 1. the rail must follow the schedule, whatever the schedule is ----------
# The arithmetic nobody checked. Two independent constants -- a 180-minute rail
# and a four-hour lap -- and the loser was silent. Now the rail is derived from
# the lap the sweep reports, so this can no longer be reopened by editing a
# cadence or a slot count in isolation.
for lap in (60, 150, 240, 600):
    storage.set_meta("wide_lap_minutes", str(lap))
    age = detector._baseline_max_age()
    assert age >= min(lap + config.POLL_INTERVAL_MINUTES,
                      config.BASELINE_ABSOLUTE_MAX_MINUTES), (lap, age)
    assert age <= config.BASELINE_ABSOLUTE_MAX_MINUTES, (lap, age)
    print(f"schedule ok: круг {lap:>3} мин → база живёт {age} мин")
# A lap so long that no honest comparison survives must be CAPPED, not
# indulged: past the ceiling we would be calling multi-day drift a signal.
storage.set_meta("wide_lap_minutes", "600")
assert detector._baseline_max_age() == config.BASELINE_ABSOLUTE_MAX_MINUTES
print("schedule ok: бесконечный круг не растягивает базу — упираемся в потолок")
storage.set_meta("wide_lap_minutes", "0")

# --- 2. the old spacing produced NOTHING, and now it produces a movement -----
# A price seen 4 hours ago and again now: the exact wide-league case.
four_hours_ago = (now - timedelta(hours=4)).isoformat()
storage.save_snapshot([line(2.90)], four_hours_ago)
spikes, moves = detector.detect([line(2.45)], now.isoformat())
assert not moves, "a 4-hour-old price must NOT be treated as a baseline -- that is drift"
assert detector.LAST_DIAG["no_history"] == 1, detector.LAST_DIAG
print("rail ok: цену четырёхчасовой давности за базу не берём — это дрейф, а не занос")

# The spacing the fixed rotation actually delivers: ~2 hours.
storage.init_db()
two_hours_ago = (now - timedelta(hours=2)).isoformat()
storage.save_snapshot([line(2.90)], two_hours_ago)
spikes, moves = detector.detect([line(2.45)], now.isoformat())
assert moves, "the spacing the new rotation delivers still produced no movement"
assert abs(moves[0]["pct_change"] + 0.155) < 0.01, moves[0]["pct_change"]
assert spikes, "a 15% drop cleared no threshold"
print(f"detect ok: при шаге 2 ч падение 2.90→2.45 видно как "
      f"{moves[0]['pct_change']*100:.1f}%")

# --- 3. blindness must be COUNTED, never silent ------------------------------
# The whole reason this cost a day: a line with no baseline was skipped with a
# bare `continue`, so total blindness and a calm market looked identical.
storage.init_db()
spikes, moves = detector.detect(
    [line(2.45, fid="a"), line(2.45, fid="b"), line(2.45, fid="c")],
    now.isoformat())
d = detector.LAST_DIAG
assert d["lines"] == 3 and d["no_history"] == 3 and d["compared"] == 0, d
assert d["by_sport_blind"].get("soccer_latvia_2") == 3, d["by_sport_blind"]
print(f"diag ok: слепые линии посчитаны и названы по лигам — {d['by_sport_blind']}")

# --- 4. dormant leagues leave the rotation ----------------------------------
# They cost credits for prices we could not publish anyway, and every one of
# them lengthens the lap for the leagues that DO matter.
soon = (now + timedelta(hours=20)).isoformat()
far = (now + timedelta(days=9)).isoformat()
horizon = {"soccer_a": {"next": soon}, "soccer_b": {"next": far},
           "soccer_c": {"next": far}}
kept = odds_client._drop_dormant(["soccer_a", "soccer_b", "soccer_c"], horizon)
assert kept == ["soccer_a"], kept
print("rotation ok: лиги без матчей в горизонте выпадают из круга")

# a league we have never fetched has no horizon and must always be tried
kept = odds_client._drop_dormant(["soccer_a", "soccer_new"], horizon)
assert "soccer_new" in kept, kept
print("rotation ok: незнакомую лигу всегда пробуем — фильтр не может себя запереть")

# and the filter can never empty the rotation, which would freeze the horizons
# that feed it and latch it shut for good
kept = odds_client._drop_dormant(["soccer_b", "soccer_c"], horizon)
assert kept == ["soccer_b", "soccer_c"], kept
print("rotation ok: пустой круг невозможен — иначе фильтр захлопнулся бы навсегда")

# --- 5. the horizon cache round-trips ---------------------------------------
storage.init_db()
storage.save_sport_horizon(
    [dict(line(2.5, sport="soccer_x"), start_time=far),
     dict(line(2.5, sport="soccer_x", fid="f2"), start_time=soon)],
    now.isoformat())
h = storage.sport_horizon()
assert h["soccer_x"]["next"] == soon, h
print("horizon ok: по лиге запоминается БЛИЖАЙШИЙ матч, а не первый попавшийся")

# --- 6. the page must SAY how much it could measure --------------------------
# The funnel explains what happened to movements once found; it is mute when
# the fault is upstream. This line is what turns "рынок спокоен" back into a
# checkable claim.
import json  # noqa: E402
import dashboard  # noqa: E402

storage.set_meta("detect_diag", json.dumps(
    {"lines": 1000, "no_history": 900, "compared": 100, "max_age": 240}))
warn = dashboard._detect_line()
assert "warn" in warn and "100" in warn and "1000" in warn, warn[:300]
assert "сравнивать не с чем" in warn, warn[:300]
print("page ok: слепота показана на сайте и подсвечена, а не спрятана")

storage.set_meta("detect_diag", json.dumps(
    {"lines": 1000, "no_history": 40, "compared": 960, "max_age": 150}))
calm = dashboard._detect_line()
assert "warn" not in calm and "960" in calm, calm[:300]
print("page ok: когда сверяем почти всё — просто строчка, без тревоги")

block = dashboard._funnel_block(
    {"big_drop": 3, "thin_market": 1, "all_books_moved": 1, "entry_too_low": 0,
     "low_stars": 0, "off_band": 0, "too_far": 0, "signals": 1}, "24 часа")
assert "сверено" in block, "the funnel lost the detection line"
print("page ok: строка сверки стоит над воронкой")
