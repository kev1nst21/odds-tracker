"""Offline check for the ДВИЖЕНИЯ block and the 24h signal feed.

Runs against a throwaway database so it can be executed anywhere, including
CI, without touching real history. Verifies the two things that are easy to
get silently wrong: the flat-stake P&L must be priced at the PRE-drop
coefficient (that is the whole point of the movements table), and a signal
logged an hour ago must still appear in the feed even though the current poll
returned nothing.
"""
import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ.setdefault("DASHBOARD_DIR", tempfile.mkdtemp())

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import dashboard  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

now = datetime.now(timezone.utc)
soon = (now + timedelta(hours=3)).isoformat()
past = (now - timedelta(hours=6)).isoformat()


def summary(fid, home, away, side, name, old, new, entry, book, stars, start, opt=None):
    return {
        "fixture_id": fid, "sport_key": "tennis_atp", "start_time": start,
        "home_team": home, "away_team": away, "stars": stars,
        "has_entry": entry is not None, "alertable": entry is not None,
        "strategy": "optimal" if opt and opt["kind"] == "straight" else "aggressive",
        "bet": {"side": side, "name": name, "old_price": old, "new_price": new,
                "drop_pct": (old - new) / old * 100, "down_count": 5, "books_count": 9,
                "entry_price": entry, "entry_book": book,
                "market_id": "h2h", "player_key": None},
        "optimal": opt,
    }


# 1. a move that became a real signal, match already played
won = summary("f1", "Cocciaretto", "Osaka", "away", "Naomi Osaka",
              5.50, 4.75, 5.70, "winamax_de", 2, past,
              {"kind": "set_handicap", "pick": "фора по сетам +1.5 ≈ 1.62",
               "price": None, "est_price": 1.62, "gradeable": True, "note": "теннис"})
# 2. a move where the old price was gone everywhere -- movement only, no signal
shut = summary("f2", "Alcaraz", "Rune", "home", "Carlos Alcaraz",
               2.40, 2.05, None, None, 3, past)
# 3. a signal logged an hour ago whose match has NOT started -- the Osaka case
open_ = summary("f3", "Hapoel", "Red Star", "home", "Hapoel Be'er Sheva",
                3.45, 3.10, 3.35, "unibet_se", 2, soon,
                {"kind": "double_chance", "pick": "1X двойной шанс", "price": 1.68,
                 "est_price": None, "gradeable": True, "note": "двойной шанс"})

detected = (now - timedelta(hours=1)).isoformat()
for s in (won, shut, open_):
    if s["alertable"]:
        storage.save_bet_alert(s, detected, 3)
    storage.save_movement(s, detected)

# dedup: the same move seen again next poll must not create a second row
assert storage.save_movement(won, now.isoformat()) is False, "movement dedup broken"

# grade: #1 won at 5.50, #2 lost at 2.40
mv = {r["fixture_id"]: r["id"] for r in storage.get_unresolved_movements(now.isoformat())}
storage.mark_movement_resolved(mv["f1"], "hit", now.isoformat())
storage.mark_movement_resolved(mv["f2"], "miss", now.isoformat())

m = storage.movement_stats()
stake = m["stake"]
expected = stake * (5.50 - 1) - stake
assert m["total"] == 3, m
assert m["with_entry"] == 2, m
assert m["graded_n"] == 2, m
assert abs(m["profit"] - expected) < 1e-6, (m["profit"], expected)
assert abs(m["win_rate"] - 50.0) < 1e-6, m
print(f"movements ok: {m['total']} moves, P&L {m['profit']:+.0f} at ${stake:.0f} "
      f"(priced at the pre-drop coefficient, expected {expected:+.0f})")

# the feed must still show the hour-old signals when the current poll is empty
feed = dashboard._summaries_html([], storage.recent_signals(24))
assert "Osaka" in feed, "an hour-old signal fell out of the feed"
assert "Hapoel" in feed, "an hour-old signal fell out of the feed"
assert "только что" not in feed, "nothing is fresh in this poll"
print("feed ok: hour-old signals visible with an empty poll")

# and a live move must be marked fresh and not duplicated by its stored copy
live = dashboard._summaries_html([open_], storage.recent_signals(24))
assert live.count("Red Star") == 1, "live row duplicated by its database copy"
assert "только что" in live, "live row not marked fresh"
print("feed ok: live row deduplicated against the database and tagged fresh")

html = dashboard._movements_table(storage.recent_movements(30))
assert "Osaka" in html and "5.50" in html, "movements table missing the caught price"
print("movements table ok")
print(dashboard._movement_stats(m)[:160].replace("<", " <"))

# and the whole page must build with these rows in it
path = dashboard.render_dashboard([open_], quota={"remaining": 1234, "used": 10})
page = open(path, encoding="utf-8").read()
for needle in ("Движения", "только что", "feedtable", "Osaka",
               "коэф. до падения", "Все за сутки", "Hapoel"):
    assert needle in page, f"page is missing: {needle}"
print(f"page ok: {len(page)} bytes, all blocks present -> {path}")

# --- a coefficient must never be truncated into nonsense -------------------
# Reported 2026-08-01: "Terence Atmane — Jack Draper ... коф на +1.5 указано 1".
# The pick text carried the price ("... +1.5 ≈ 1.60 (взять ...)") and every
# table cut the label to fit, landing mid-number.
legacy = "Terence Atmane — фора по сетам +1.5 ≈ 1.60 (взять хотя бы один сет)"
short = dashboard._short(legacy, 40)
assert "≈" not in short, short          # the price never rides inside the label
assert "+1.5" in short, short           # but the handicap LINE must survive
assert short.startswith("Terence Atmane — фора по сетам"), short
assert not short.rstrip("…").endswith((".", ",", "≈")), short
print(f"truncation ok: {short!r} — no dangling number")

atmane = summary("f4", "Terence Atmane", "Jack Draper", "home", "Terence Atmane",
                 3.16, 2.80, 3.05, "coolbet", 3, soon,
                 {"kind": "set_handicap", "pick": legacy, "price": None,
                  "est_price": 1.60, "gradeable": True, "note": "теннис"})
atmane["fresh"] = True
row = dashboard._event_row(atmane)
assert "~1.60" in row, row
assert "≈ 1." not in row and "≈ 1<" not in row, row
print("truncation ok: the row shows ~1.60 in full, never a bare '1'")

# and a clean modern pick keeps its price exactly once
clean = dict(atmane)
clean["optimal"] = dict(atmane["optimal"], pick="Terence Atmane — фора по сетам +1.5 (взять хотя бы один сет)")
assert dashboard._event_row(clean).count("1.60") == 1, "price rendered twice"
print("truncation ok: price appears exactly once")

# --- both strategies get their own verdict, and the money is honest --------
# Reported 2026-08-01: Cocciaretto — Osaka showed only "не зашла", though the
# straight bet lost and the set handicap won. And the optimal bank claimed
# +$940 from that one bet -- it paid a ~1.60 handicap at the 5.70 moneyline.
row = {"result": "miss", "opt_result": "hit", "opt_kind": "set_handicap",
       "opt_pick": "Cocciaretto — фора по сетам +1.5 ≈ 1.62 (взять хотя бы один сет)",
       "opt_price": None, "opt_est_price": 1.62}
chips = dashboard._both_results(row)
assert "агрессивная" in chips and "не зашла" in chips, chips
assert "оптимальная" in chips and "зашла" in chips, chips
detail = dashboard._opt_detail_row(row)
assert "~1.62" in detail and "≈" not in detail, detail
print("verdicts ok: both strategies reported separately, with what optimal bet")

# identical bets must not be reported twice
same = dashboard._both_results({"result": "hit", "opt_result": "hit", "opt_kind": "straight"})
assert same.count("зашла") == 1 and "обе стратегии" in same, same
print("verdicts ok: a straight optimal play reports once, not twice")

# the money: a handicap with no bought price stays OUT of the bank
storage.mark_resolved(1, "miss", now.isoformat(), None, None, "hit")
agg = storage.alert_stats("prematch", "aggressive")
opt = storage.alert_stats("prematch", "optimal")
assert opt["profit"] <= 0, f"optimal bank paid an unbought handicap: {opt['profit']}"
assert opt["hits"] >= 1, "the handicap win vanished from the win rate too"
print(f"bank ok: optimal win counted in заходимость ({opt['hits']} hit), "
      f"profit {opt['profit']:+.0f} — the 5.70 moneyline is no longer paid for a ~1.6 фора")

# --- one merged track record, with the score --------------------------------
# Requested 2026-08-01: "в сыгравших матчах ... счет давай указывать будем",
# and "Сыгравшие сигналы и Разбор каждой ставки — эти блоки объедини в один".
storage.save_live_score("f1", "tennis_atp", "Cocciaretto", "Osaka",
                        1, 2, True, now.isoformat())
dashboard._FINAL.clear()
dashboard._FINAL.update(storage.final_scores_map())
assert dashboard._score_text("f1") == "1:2", dashboard._score_text("f1")

played = storage.recent_bets(10, "prematch", resolved_only=True)
assert played, "no resolved bets to render"
block = dashboard._last_bets(played, 10)
assert "1:2" in block, "the final score is missing from the track record"
assert "агрессивная" in block and "оптимальная" in block, "verdicts missing"
print("track record ok: one block, final score 1:2 and both verdicts on the row")

# "проверено" opens the last finished bets for that strategy
opt = storage.alert_stats("prematch", "optimal")
card = dashboard._strategy_card(opt, "ОПТИМАЛЬНАЯ", "sub", "opt",
                                storage.recent_bets(5, "prematch", "optimal"))
assert 'id="res-opt"' in card and 'data-open="res-opt"' in card, "проверено is not clickable"
assert "Cocciaretto" in card, "the expanded list has no rows"
assert "1:2" in card, "the expanded list has no score"
print("card ok: «проверено» expands into the last finished bets, with scores")

# the whole page still builds
p2 = dashboard.render_dashboard([], quota={"remaining": 19978, "used": 22})
page2 = open(p2, encoding="utf-8").read()
assert "Сыгравшие сигналы" in page2 and "Разбор каждой ставки" not in page2, "blocks not merged"
print(f"page ok: {len(page2)} bytes, single track-record block")

# --- every row must name its discipline ------------------------------------
# Requested 2026-08-01: "Boostgate eSports — Su eSports ... указывай что это за
# дисциплина, например дота или контер страйк или лол".
assert dashboard._sport_label("esports_dota2") == "Dota 2"
assert dashboard._sport_label("esports_cs2") == "CS2"
assert dashboard._sport_label("esports_lol") == "LoL"
assert dashboard._sport_label("soccer_latvia_2") == "Футбол"
assert dashboard._sport_label("tennis_atp_washington") == "Теннис"
assert dashboard._sport_label("table_tennis") == "Наст. теннис"
assert dashboard._sport_label("basketball_nba") == "Баскетбол"
assert dashboard._sport_label(None) == ""
esp = summary("f9", "Boostgate eSports", "Su eSports", "home", "Boostgate eSports",
              2.90, 2.55, 2.88, "1xbet", 3, soon)
esp["sport_key"] = "esports_dota2"
assert "Dota 2" in dashboard._event_row(esp), "the discipline is missing from the feed row"
print("discipline ok: Dota 2 / CS2 / LoL / Футбол / Теннис labelled on the row")

# --- the funnel must count each move once, not once per poll ----------------
# Reported 2026-08-03: the block said 6 movements where the list underneath it
# held 3. It summed funnel_log, which has a row per POLL -- and since a drop is
# measured against the price an hour ago, the same event was re-counted on
# every cycle inside that hour. Counting off the deduplicated movements table
# is the fix, and this asserts the two numbers now agree.
for cycle in range(4):                       # four polls, same three events
    at = (now - timedelta(minutes=45 - cycle * 10)).isoformat()
    storage.save_funnel({"big_drop": 3, "thin_market": 0, "all_books_moved": 1,
                         "entry_too_low": 0, "signals": 2}, at)
    for s in (won, shut, open_):
        storage.save_movement(s, at)

fn = storage.funnel_stats(24)
ledger = len(storage.recent_movements(100))
assert fn["big_drop"] == ledger == 3, (fn, ledger)
assert fn["signals"] == 2, fn                 # won + open_ had an entry
assert fn["all_books_moved"] == 1, fn         # shut had none
assert fn["big_drop"] == fn["thin_market"] + fn["all_books_moved"] \
    + fn["entry_too_low"] + fn["signals"], fn
print(f"funnel ok: 4 polls over the same 3 moves report {fn['big_drop']}, "
      f"matching the {ledger} rows in the ledger (was 12)")

# and a bucket recorded explicitly by analytics beats the legacy fallback:
# this row HAS a takeable price, so the fallback would call it thin_market
# anyway -- what is being checked is that a stored 'entry_too_low' survives
# instead of being re-derived into the wrong bucket.
low = summary("f7", "Riga II", "Valmiera II", "home", "Riga II",
              2.90, 2.55, 2.88, "betsson", 1, soon)
low["alertable"] = False
low["funnel_bucket"] = "entry_too_low"
assert storage.save_movement(low, now.isoformat())
fn2 = storage.funnel_stats(24)
assert fn2["entry_too_low"] == 1, fn2
assert fn2["big_drop"] == fn["big_drop"] + 1, (fn2, fn)
assert fn2["signals"] == fn["signals"], "an unbet move was counted as a signal"
print("funnel ok: an explicit bucket is stored and counted, not re-derived")

# every "проверено" counter must open into what exactly was checked
mv_block = dashboard._movement_stats(storage.movement_stats())
assert "res-moves" in mv_block and "stat-btn" in mv_block, "movements «проверено» not clickable"
mini_mv = dashboard._mini_movements()
assert "Cocciaretto" in mini_mv and "1:2" in mini_mv, mini_mv[:300]
print("movements ok: «проверено» opens the graded moves, with score and P&L")

# --- CLV first, plus the two analysis blocks --------------------------------
# Requested 2026-08-08 after the ledger showed CLV separating outcomes almost
# cleanly (+12.4% on winners, -1.4% on losers) while win rate on 23 bets was
# still noise. And the breakdown block exists because a single average hid the
# only real differences: tennis +5.1% CLV against football -4.6%, and drops
# above 15% losing every time while 10-12% drops made money.
agg = storage.alert_stats("prematch", "aggressive")
card = dashboard._strategy_card(agg, "АГРЕССИВНАЯ", "sub", "agg",
                                storage.recent_bets(5, "prematch", "aggressive"))
i_clv, i_wr = card.index("средний CLV"), card.index("заходимость")
assert i_clv < i_wr, "CLV must be rendered before win rate, it is the headline now"
assert "clv-good" in card or "clv-bad" in card or "clv-flat" in card, "CLV not colour-coded"
print("карточка ok: CLV стоит первым и раскрашен по знаку")

bd = storage.breakdown_stats("prematch")
block = dashboard._breakdown_block(bd)
if bd["graded"]:
    assert "ГДЕ РЕЗУЛЬТАТ РАЗЛИЧАЕТСЯ" in block and "ставок" in block, block[:300]
    print(f"разбивки ok: {bd['graded']} сыгравших, разрезаны по спорту/падению/звёздам")

# The counterfactual tab was repointed from bookmakers to Polymarket on
# 20.08.2026 ("очень важную аналитику теперь только по полику"). The bookmaker
# scorer is kept and still checked -- it is the history of how we got here --
# but the block that renders on the page is now the Polymarket one, and it has
# to survive an EMPTY journal, because that is the state it ships in.
cf = storage.counterfactual_stats()
assert cf["rules"] and "вернули две звезды" in {r["label"] for r in cf["rules"]} \
    or any("две звезды" in r["label"] for r in cf["rules"]), cf["rules"]

pmcf = storage.pm_counterfactual()
pmb = dashboard._pm_counterfactual_block(pmcf)
assert "POLYMARKET" in pmb, pmb[:300]
if not pmcf["pool"]:
    # An empty tab must SAY it is empty and why, not render a table of zeroes
    # that reads like a measurement.
    assert "начал вестись сегодня" in pmb, pmb[:400]
assert len(pmcf["rules"]) >= 8, pmcf["rules"]
labels = " ".join(r["label"] for r in pmcf["rules"])
for must in ("зазор от 3%", "зазор от 8%", "полный размер", "последние 3 часа"):
    assert must in labels, (must, labels)
print(f"контрфактические ok: {len(pmcf['rules'])} правил по Polymarket, "
      f"{pmcf['pool']} сыгравших сигналов в журнале котировок")

# --- the bot feed is a contract, so its shape is pinned ----------------------
# A trading bot reads this file. A silently renamed field is a bot placing
# orders on stale or missing data, so the keys are asserted, not assumed.
import json as _jsonf  # noqa: E402
feed = _jsonf.load(open(dashboard.PM_FEED_PATH, encoding="utf-8"))
assert feed["version"] == 1, feed
assert set(feed) >= {"version", "generated_at", "rule", "count", "signals"}, feed
assert feed["rule"]["min_edge_pct"] == config.POLYMARKET_MIN_EDGE_PCT, feed["rule"]
assert isinstance(feed["signals"], list) and feed["count"] == len(feed["signals"])
print(f"фид для бота ok: версия {feed['version']}, правило "
      f"+{feed['rule']['min_edge_pct']:g}%, сигналов сейчас {feed['count']}")

p3 = dashboard.render_dashboard([], quota={"remaining": 9999, "used": 5})
page3 = open(p3, encoding="utf-8").read()
assert "$breakdown_block" not in page3 and "$counterfactual_block" not in page3, \
    "a template placeholder was left unsubstituted"
print(f"страница ok: {len(page3)} байт, плейсхолдеров не осталось")

# --- the ledger must carry the numbers decisions rest on ---------------------
# Added 2026-08-10 after a check ran with the browser down: the funnel and the
# entry-threshold preview existed only inside the poll job, so the one question
# that mattered ("what would a 40% threshold give?") could not be answered
# without reading a CI log by hand. Anything a decision depends on belongs in
# the published file.
import json as _json  # noqa: E402

dashboard.render_dashboard([], quota={"remaining": 4321, "used": 9})
led = _json.load(open(dashboard.LEDGER_PATH, encoding="utf-8"))
for key in ("funnel_24h", "entry_threshold_preview", "detect", "signals"):
    assert key in led, f"ledger.json is missing {key}: {list(led)}"
prev = led["entry_threshold_preview"]
assert "rules" in prev and "sample" in prev, prev
assert {r["capture_pct"] for r in prev["rules"]} == {50, 40, 30}, prev
print(f"ledger ok: воронка, охват и превью порога входа опубликованы "
      f"(выборка {prev['sample']})")
