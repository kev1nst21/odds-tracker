"""Cross-block invariants: the page must never contradict itself.

Every other test file here checks that one function is right. This one checks
that the numbers a READER sees add up, which is a different and easier thing to
get wrong -- each block can be individually correct while the page as a whole
tells two stories.

Both bugs reported on 2026-08-10 were exactly that shape, and neither could
have been caught by testing a function in isolation:

  * the optimal card said "За 5 сыгравших ставок" while the aggressive card
    said 10, from the same signal stream. Both numbers were correct answers to
    slightly different questions, and nothing compared them;
  * a settled bet (Rafael Jodar — Brandon Nakashima) showed no coefficient at
    all, while its result still counted in the win rate -- a verdict with no
    number behind it, on a site whose whole claim is that every figure can be
    recounted.

So the rules below are stated as relationships between blocks, not as expected
values. They hold whatever the market does, which is what makes them worth
running on every poll.
"""
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "cons.db")
os.environ.setdefault("DASHBOARD_DIR", tempfile.mkdtemp())

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import dashboard  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)
past = (now - timedelta(hours=8)).isoformat()


def signal(fid, name, entry, opt):
    return {
        "fixture_id": fid, "sport_key": "tennis_atp", "start_time": past,
        "home_team": name, "away_team": "Opponent", "stars": 3,
        "has_entry": True, "alertable": True,
        "strategy": "optimal" if opt else "aggressive",
        "bet": {"side": "home", "name": name, "old_price": entry + 0.4,
                "new_price": entry - 0.3, "drop_pct": 12.0, "down_count": 5,
                "books_count": 9, "entry_price": entry, "entry_book": "pinnacle",
                "market_id": "h2h", "player_key": "-"},
        "optimal": opt,
    }


# A straight optimal play (priced, payable) and a set handicap (settleable from
# the score, never bought) -- the exact mix that made the two cards disagree.
straight = signal("s1", "Player A", 2.40,
                  {"kind": "straight", "pick": "Player A", "price": 2.40,
                   "est_price": None, "gradeable": True, "note": ""})
handicap = signal("s2", "Rafael Jodar", 4.10,
                  {"kind": "set_handicap", "pick": "Rafael Jodar — фора по сетам +1.5",
                   "price": None, "est_price": 1.62, "gradeable": True,
                   "note": "теннис"})
for s in (straight, handicap):
    storage.save_bet_alert(s, past, 20)

rows = {r["fixture_id"]: r["id"] for r in storage.get_unresolved_alerts(now.isoformat())}
storage.mark_resolved(rows["s1"], "hit", now.isoformat(), 0.05, 1, "hit")
storage.mark_resolved(rows["s2"], "miss", now.isoformat(), -0.02, 0, "hit")

agg = storage.alert_stats("prematch", "aggressive")
opt = storage.alert_stats("prematch", "optimal")

# --- 1. the two strategies bet the same events, so the same number played ----
assert agg["resolved"] == opt["resolved"] == 2, (agg["resolved"], opt["resolved"])
print(f"инвариант ok: обе стратегии показывают {agg['resolved']} сыгравших, а не разные числа")

# --- 2. money may cover fewer bets, and the wording must SAY so -------------
assert opt["priced_n"] <= opt["resolved"], (opt["priced_n"], opt["resolved"])
bank = dashboard._bankroll_block(opt)
if opt["priced_n"] != opt["resolved"]:
    assert str(opt["resolved"]) in bank and str(opt["priced_n"]) in bank, bank[:400]
    assert "известной ценой" in bank or "известными ценами" in bank, bank[:400]
    print(f"инвариант ok: банк честно пишет «из {opt['resolved']} сыгравших "
          f"{opt['priced_n']} с известной ценой»")

# --- 3. a settled bet ALWAYS shows the coefficient it was judged by ----------
for stats, label in ((agg, "агрессивная"), (opt, "оптимальная")):
    block = dashboard._mini_resolved(stats)
    for chunk in re.findall(r"@ ([^ <·]+)", block):
        assert chunk != "—", f"{label}: сыгравшая ставка без коэффициента\n{block[:500]}"
    print(f"инвариант ok: в «{label}» у каждой сыгравшей ставки есть коэффициент")

played = dashboard._last_bets(storage.recent_bets(10, "prematch", resolved_only=True), 10)
assert "@ —" not in played, f"в сыгравших есть строка без коэффициента:\n{played[:600]}"
# and the handicap's derived price must be visible rather than blanked
assert "1.62" in played, "расчётная цена форы не показана"
print("инвариант ok: фора показывает расчётный коэффициент ~1.62, а не прочерк")

# --- 4. the funnel buckets must add up to the movements total ---------------
f = storage.funnel_stats(24)
parts = sum(f.get(k) or 0 for k in ("thin_market", "all_books_moved", "entry_too_low",
                                    "low_stars", "off_band", "too_far", "signals"))
assert parts == (f.get("big_drop") or 0), (parts, f)
print(f"инвариант ok: причины отсева в сумме дают {f.get('big_drop') or 0} движений")

# --- 5. win rate must be computed from hits and misses only -----------------
for stats, label in ((agg, "агрессивная"), (opt, "оптимальная")):
    h, m = stats["hits"], stats["misses"]
    if h + m:
        expected = h / (h + m) * 100
        assert abs(stats["win_rate"] - expected) < 1e-9, (label, stats["win_rate"], expected)
assert opt["hits"] >= 1, "выигрыш форы пропал из заходимости"
print("инвариант ok: заходимость считается по зашедшим и незашедшим, форы учтены")

# --- 6. the page must not leave a template placeholder or an empty number ----
page = open(dashboard.render_dashboard([], quota={"remaining": 5000, "used": 3}),
            encoding="utf-8").read()
assert "$" + "{" not in page and not re.search(r"\$[a-z_]{4,}\b", page), \
    "на странице остался неподставленный плейсхолдер"
# Deliberately narrow patterns: a bare "nan" matches ordinary words (Nantes,
# финансы) and would cry wolf on every poll. What must never appear is a
# FORMATTED NUMBER that came out as None/NaN/Infinity.
for bad in (r">\s*None\s*<", r">\s*nan\s*<", r">\s*NaN\s*<",
            r"\bNone%", r"\bnan%", r"\$None", r"\$nan", r"Infinity"):
    hit = re.search(bad, page)
    assert not hit, f"на странице напечатано нечисло ({bad}): ...{page[max(0,hit.start()-80):hit.end()+80]}..."
print(f"инвариант ok: страница собрана без плейсхолдеров, None и nan ({len(page)} байт)")
