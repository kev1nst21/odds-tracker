"""Closing the two holes in the track record, found 2026-08-08 in the ledger.

Of 28 signals whose match had started, five had never been graded -- two of
them 60 and 70 hours past kick-off -- and every single one was football or
esports. Tennis closed 19 of 19. So the published record was effectively a
tennis record wearing a six-discipline badge, and any conclusion about football
rested on four matches.

Two distinct causes, both asserted here:

  1. esports and table tennis were NEVER gradeable. results.py skipped every
     OddsPapi sport because The Odds API scores endpoint has never heard of
     those keys, and no replacement was ever wired in. Now settled through
     OddsPapi's own /v4/settlements, which states the outcome outright instead
     of making us interpret a bo3 score.
  2. a bet missed for longer than the scores endpoint reaches back can never be
     graded, and used to sit in "ждут матча" for ever -- so the book always
     looked bigger than the part of it we had actually checked.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "grade.db")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import results  # noqa: E402
import oddspapi_client  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)


def signal(fid, sport, side, hours_ago, name="Gen.G"):
    return {
        "fixture_id": fid, "sport_key": sport,
        "start_time": (now - timedelta(hours=hours_ago)).isoformat(),
        "home_team": "Gen.G", "away_team": "T1", "stars": 3,
        "has_entry": True, "alertable": True, "strategy": "aggressive",
        "bet": {"side": side, "name": name, "old_price": 2.60, "new_price": 2.25,
                "drop_pct": 13.5, "down_count": 4, "books_count": 6,
                "entry_price": 2.55, "entry_book": "betway",
                "market_id": "h2h", "player_key": "-"},
        "optimal": None,
    }


won = signal("esp_win", "esports_lol", "home", 5)
lost = signal("esp_lose", "esports_lol", "away", 5, name="T1")
pending = signal("esp_open", "esports_cs2", "home", 5)
for s in (won, lost, pending):
    storage.save_bet_alert(s, (now - timedelta(hours=6)).isoformat(), 3)

# The provider settles the exact outcome we bet, per side.
SETTLEMENTS = {
    "esp_win":  {"home": "WIN",  "away": "LOSE"},
    "esp_lose": {"home": "WIN",  "away": "LOSE"},
    "esp_open": {"home": "UNDECIDED", "away": "UNDECIDED"},
}
calls = []
oddspapi_client.fetch_settlement = lambda fid, on_error=None: (
    calls.append(fid) or SETTLEMENTS.get(fid, {}))
oddspapi_client.fetch_score = lambda fid, on_error=None: (
    (2.0, 1.0) if fid in ("esp_win", "esp_lose") else (None, None))

n = results.check_pending_results(now)
assert n >= 2, f"esports bets still not graded: {n}"

rows = {r["fixture_id"]: r for r in storage.recent_bets(20, "prematch")}
assert rows["esp_win"]["result"] == "hit", rows["esp_win"]["result"]
assert rows["esp_lose"]["result"] == "miss", rows["esp_lose"]["result"]
assert rows["esp_open"]["resolved"] == 0, "an UNDECIDED fixture was graded anyway"
print(f"киберспорт ok: WIN → зашла, LOSE → не зашла, UNDECIDED остаётся ждать "
      f"({len(set(calls))} запроса settlements)")

# the score is only decoration -- a missing one must never block the verdict
storage.init_db()
noscore = signal("esp_noscore", "esports_dota2", "home", 5)
storage.save_bet_alert(noscore, (now - timedelta(hours=6)).isoformat(), 3)
SETTLEMENTS["esp_noscore"] = {"home": "WIN", "away": "LOSE"}
oddspapi_client.fetch_score = lambda fid, on_error=None: (None, None)
storage.set_meta("last_results_check_at", "")
results.check_pending_results(now + timedelta(hours=4))
got = {r["fixture_id"]: r for r in storage.recent_bets(20, "prematch")}
assert got["esp_noscore"]["result"] == "hit", "no score meant no verdict"
print("киберспорт ok: без счёта ставка всё равно рассчитана — счёт это украшение")

# --- bets nobody can ever check must not sit as "ждут матча" for ever -------
storage.init_db()
ancient = signal("old1", "soccer_epl", "home", config.RESULT_GIVE_UP_HOURS + 10)
storage.save_bet_alert(ancient, (now - timedelta(hours=200)).isoformat(), 3)
recent = signal("new1", "soccer_epl", "home", 5)
storage.save_bet_alert(recent, (now - timedelta(hours=6)).isoformat(), 3)

import odds_client  # noqa: E402
odds_client.fetch_scores_for_sport = lambda *a, **k: []
storage.set_meta("last_results_check_at", "")
results.check_pending_results(now + timedelta(hours=8))

final = {r["fixture_id"]: r for r in storage.recent_bets(20, "prematch")}
assert final["old1"]["resolved"] == 1 and final["old1"]["result"] == "n/a", final["old1"]["result"]
assert final["new1"]["resolved"] == 0, "gave up on a bet that is still checkable"
print(f"просрочка ok: старше {config.RESULT_GIVE_UP_HOURS:.0f} ч закрываем как «не проверено», "
      f"свежую не трогаем")
