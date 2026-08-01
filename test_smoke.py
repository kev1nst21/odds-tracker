"""End-to-end smoke test: one full poll cycle against a fake provider.

Exists because of a real outage. On 2026-07-31 detector.py shipped with a call
to storage.get_baseline_price() while storage.py itself was left out of the
upload. Every module imported fine, every unit test passed, and every poll in
production then died on AttributeError for five hours -- runner.py catches
per-poll exceptions so the job stayed green, and the site simply froze at its
last good render while looking perfectly healthy.

Nothing short of actually running a cycle catches that. So this walks the real
path -- discover sports, fetch odds, detect, summarise, store, render -- with
only the network stubbed, and fails loudly if any wiring between modules is
missing.
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "smoke.db")
os.environ["DASHBOARD_DIR"] = tempfile.mkdtemp()
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "")
os.environ.setdefault("THEODDSAPI_KEY", "smoke-test")
os.environ.setdefault("ODDSPAPI_KEY", "")

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import odds_client  # noqa: E402
import main  # noqa: E402

storage.DB_PATH = config.DB_PATH

now = datetime.now(timezone.utc)
BOOKS = ["pinnacle", "unibet_eu", "betsson", "williamhill", "onexbet"]

SPORTS = [{"key": "soccer_epl", "group": "Soccer", "title": "EPL", "active": True},
          {"key": "soccer_latvia_2", "group": "Soccer", "title": "Latvia 2", "active": True},
          {"key": "tennis_atp_x", "group": "Tennis", "title": "ATP", "active": True}]

# Two books shorten the home side between the two cycles; the rest hold.
CYCLE = 0
DRIFTED = {"pinnacle": [2.90, 2.50], "unibet_eu": [2.90, 2.52]}


def _event(sport_key):
    return {
        "id": f"fx_{sport_key}", "sport_key": sport_key,
        "commence_time": (now + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "home_team": "Alpha", "away_team": "Beta",
        "bookmakers": [{
            "key": b, "title": b, "last_update": now.isoformat(),
            "markets": [{"key": "h2h", "outcomes": [
                {"name": "Alpha", "price": DRIFTED.get(b, [2.90, 2.90])[CYCLE]},
                {"name": "Beta", "price": 1.42},
            ]}],
        } for b in BOOKS],
    }


def fake_fetch(sport_keys, on_error=None):
    odds_client.LAST_QUOTA.update({"used": 1500, "remaining": 18500})
    return [_event(k) for k in sport_keys]


odds_client.list_sports = lambda: SPORTS
odds_client.fetch_odds_for_sports = fake_fetch
odds_client.fetch_scores_for_sport = lambda *a, **k: []

# First cycle seeds the baseline, second cycle should see the drop. The
# baseline lookup wants an hour of distance, so the seed is backdated.
main.run_once()
with storage._conn() as conn:
    conn.execute("UPDATE odds_snapshots SET fetched_at=?",
                 ((now - timedelta(minutes=75)).isoformat(),))
    conn.commit()

CYCLE = 1
summaries = main.run_once()

assert summaries, "a 14% drop at two books produced no summary"
s = summaries[0]
assert s["bet"]["down_count"] == 2, s["bet"]
assert s["alertable"], f"not alertable: {s.get('verdict')}"
print(f"smoke ok: {len(summaries)} event(s), "
      f"{s['bet']['old_price']:.2f} -> {s['bet']['new_price']:.2f} at "
      f"{s['bet']['down_count']}/{s['bet']['books_count']} books, "
      f"entry {s['bet']['entry_price']:.2f} at {s['bet']['entry_book']}")

assert storage.movement_stats()["total"] >= 1, "movement never logged"
assert storage.alert_stats("prematch", "aggressive")["total"] >= 1, "alert never logged"

page = config.DASHBOARD_PATH
assert os.path.exists(page), f"no dashboard was rendered at {page}"
html = open(page, encoding="utf-8").read()
for needle in ("Движения", "Сигналы", "Открытые ставки", "кредитов осталось"):
    assert needle in html, f"rendered page is missing: {needle}"
print(f"smoke ok: dashboard rendered, {len(html)} bytes, all sections present")

picked = odds_client.select_sport_keys(SPORTS)
print(f"smoke ok: sports selected this cycle -> {picked}")
sys.exit(0)
