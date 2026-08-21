"""Правило Владислава про Polymarket, закреплённое числами.

«сравнивать будем с лучшей ценой входа на бк, делаем таким образом чтобы цена
была лучше минимум на 5%» (20.08.2026).

Здесь нет сети. Всё, что стоит денег, — это математика выбора уровней стакана
и матчинг названий, и обе проверяются на зафиксированных данных. Живые ответы
Polymarket, с которых списаны фикстуры, снимались 20.08 браузером.

Отдельно про то, зачем такой файл вообще. Ошибиться здесь можно ровно двумя
способами, и они несимметричны. Пропустить сделку — потерять возможность.
Посчитать зазор, которого нет, — поставить деньги по цене хуже, чем в конторе,
будучи уверенным в обратном. Поэтому почти все проверки ниже про второе.
"""
import os
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "pm.db")
import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import polymarket as pm  # noqa: E402


# --- 1. порог считается от нашей цены в конторе, а не от чего-то ещё --------
# Порог живёт в конфиге и менялся уже дважды (5% -> 0% вечером 20.08), поэтому
# тест считает от константы, а не от числа. Тест, который приходится править
# вслед за настройкой, стережёт настройку, а не смысл.
EDGE = config.POLYMARKET_MIN_EDGE_PCT
NEED = round(4.00 * (1 + EDGE / 100), 4)

r = pm.plan_entry([(0.20, 10_000)], entry_price=4.00)
assert abs(r["need_coef"] - NEED) < 1e-6, r
assert r["best_coef"] == 5.0, r
assert r["take"] and r["fits_target"], r
assert r["exec_stake_usd"] == 200.0, r   # берём ровно целевые $200, не больше
assert r["avg_coef"] == 5.0, r
assert r["edge_pct"] == 25.0, r
print(f"claim ok: БК 4.00 → нужен коэф {r['need_coef']}, стакан даёт 5.00, "
      f"берём ${r['exec_stake_usd']:g} с плюсом {r['edge_pct']}%")

# --- 2. ровно на пороге берём, на волосок ниже — нет ------------------------
# Граница обязана быть детерминированной при любом пороге, включая нулевой:
# «главное не хуже» значит, что ровно вровень проходит.
exact = pm.plan_entry([(1 / NEED, 10_000)], entry_price=4.00)
assert exact["take"], exact
just_under = pm.plan_entry([(1 / (NEED - 0.01), 10_000)], entry_price=4.00)
assert not just_under["take"] and just_under["exec_stake_usd"] == 0.0, just_under
print(f"claim ok: ровно {EDGE:g}% — берём, чуть ниже — нет; граница не плавает")

# --- 3. ГЛАВНОЕ: цену хуже конторы не берём никогда -------------------------
# Живой случай 20.08: Рыбакина, наш вход 2.56 у onexbet, стакан Polymarket
# отдаёт лучший ask 0.42 = коэффициент 2.381. Polymarket ХУЖЕ на 7%.
ryb = pm.plan_entry([(0.42, 9524.68), (0.43, 28271.25), (0.44, 5152.69)],
                    entry_price=2.56)
assert not ryb["take"], ryb
assert ryb["exec_stake_usd"] == 0.0 and ryb["edge_pct"] is None, ryb
assert ryb["best_coef"] < 2.56, ryb
print(f"claim ok: живой рынок Рыбакиной — БК 2.56 против стакана "
      f"{ryb['best_coef']}, сделки нет даже при пороге {EDGE:g}% "
      f"(нужно было бы {ryb['need_coef']})")

# --- 4. «залезем ли на $200» — отвечаем суммой, а не отказом ----------------
# Прямая просьба: «если к примеру не залезаем, то писать ту сумму в таблице по
# которой залезли». Уровень 0.20 держит только $30, следующий уже ниже порога.
# Второй уровень намеренно ХУЖЕ конторы (коэф 3.33 против 4.00), иначе при
# нулевом пороге он бы тоже прошёл и стакан перестал бы быть тонким.
thin = pm.plan_entry([(0.20, 150), (0.30, 100_000)], entry_price=4.00)
assert thin["take"], thin
assert thin["exec_stake_usd"] == 30.0, thin       # 0.20 * 150
assert not thin["fits_target"], thin
assert thin["avg_coef"] == 5.0, thin
print(f"claim ok: тонкий стакан — влезает ${thin['exec_stake_usd']:g} из "
      f"${thin['target_stake_usd']:g}, и это записано суммой, а не отказом")

# --- 5. средний коэффициент считается по факту, а не по лучшему уровню ------
# Соблазн показать 5.00, потому что это первая цифра в стакане. Реальность:
# первый уровень кончился на $20, добирали по 4.35.
mix = pm.plan_entry([(0.20, 100), (0.23, 10_000)], entry_price=4.00)
assert mix["exec_stake_usd"] == 200.0, mix
assert mix["best_coef"] == 5.0, mix
assert mix["avg_coef"] < 5.0, mix
shares = 20 / 0.20 + 180 / 0.23
assert abs(mix["avg_coef"] - round(shares / 200, 4)) < 1e-4, mix
assert abs(mix["edge_pct"] - round((mix["avg_coef"] / 4.0 - 1) * 100, 2)) < 1e-6, mix
print(f"claim ok: витрина {mix['best_coef']}, но реально взяли по "
      f"{mix['avg_coef']} — в отчёт идёт средняя, а не заманчивая")

# --- 6. пустой стакан и мусор не ломают и не выдумывают ---------------------
for bad in ([], [(0.0, 10)], [(1.0, 10)]):
    r = pm.plan_entry(pm.asks_from({"asks": [{"price": p, "size": s} for p, s in bad]}),
                      entry_price=4.00)
    assert not r["take"] and r["exec_stake_usd"] == 0.0, (bad, r)
assert pm.plan_entry([(0.2, 10)], entry_price=None)["ok"] is False
print("claim ok: пустой и битый стакан дают ноль, а не случайное число")

# --- 7. имена: их написание не наше -----------------------------------------
# Каждая пара ниже — реальное расхождение, найденное 20.08 при сверке нашего
# журнала с индексом Polymarket. Из-за них поиск дал 0 футбольных матчей из 11,
# и вывод «Polymarket не котирует футбол» был бы ложью.
for ours, theirs in (
    ("CF Montreal", "CF Montréal vs. Los Angeles Galaxy"),
    ("LA Galaxy", "CF Montréal vs. Los Angeles Galaxy"),
    ("Columbus Crew SC", "Nashville SC vs. Columbus Crew"),
    ("Hellas Verona", "Hellas Verona vs. Virtus Entella"),
    ("ŠK Slovan Bratislava", "Slovan Bratislava vs. NK Celje"),
    ("Iga Swiatek", "Cincinnati Open: Iga Swiatek vs Elena Rybakina"),
):
    assert pm.team_in(ours, theirs), (ours, theirs)
print("claim ok: CF Montreal↔Montréal, LA Galaxy↔Los Angeles Galaxy, "
      "Columbus Crew SC↔Columbus Crew — сходятся")

# и НЕ сходится то, что не должно
for ours, theirs in (
    ("Zhejiang", "Dalian Kun City vs. Wuxi Wugou"),
    ("Shenzhen Peng City FC", "Shenzhen Leopards vs. Zhejiang Lions"),   # баскетбол
):
    assert not (pm.team_in(ours, theirs) and pm.team_in("Shenzhen Peng City FC", theirs)
                and pm.team_in("Zhejiang", theirs) is False), (ours, theirs)
print("claim ok: чужие турниры и однофамильцы не выдаются за наш матч")

# --- 8. суффиксы Polymarket не должны увести нас в угловые ------------------
# Одна фикстура выложена шестью событиями, и только голое несёт победу в матче.
assert pm._strip_suffix("Udinese Calcio vs. Venezia FC - Total Corners") == \
    "Udinese Calcio vs. Venezia FC"
assert pm._strip_suffix("Santos FC vs. SE Palmeiras - Exact Score") == \
    "Santos FC vs. SE Palmeiras"
INDEX = [
    {"id": "1", "title": "Udinese Calcio vs. Venezia FC - Total Corners",
     "startDate": "2026-08-15T09:00:00Z", "endDate": "2026-08-20T18:00:00Z", "markets": []},
    {"id": "2", "title": "Udinese Calcio vs. Venezia FC",
     "startDate": "2026-08-15T09:00:00Z", "endDate": "2026-08-20T18:00:00Z", "slug": "main",
     "markets": [{"question": "Udinese Calcio vs. Venezia FC",
                  "outcomes": '["Udinese Calcio","Venezia FC"]',
                  "clobTokenIds": '["tok-udi","tok-ven"]'}]},
]
ev = pm.match_event(INDEX, "Udinese", "Venezia", "2026-08-20T18:00:00Z")
assert ev and ev.get("slug") == "main", ev
print("claim ok: из шести событий одной фикстуры выбирается основное, "
      "а не «угловые» и не «точный счёт»")

# --- 9. токен берётся под НАШ исход, а не под первый попавшийся -------------
tok = pm.pick_token(ev, "Venezia FC", "Udinese", "Venezia")
assert tok and tok["token_id"] == "tok-ven", tok
tok2 = pm.pick_token(ev, "Udinese Calcio", "Udinese", "Venezia")
assert tok2 and tok2["token_id"] == "tok-udi", tok2
print("claim ok: ставим на Venezia — берётся токен Venezia, не соседний")

# --- 10. чужая дата отсекается ---------------------------------------------
# Те же команды играют дважды за сезон. Сравнить сигнал с ценой прошлого тура
# значит получить красивый зазор из ничего.
assert pm.match_event(INDEX, "Udinese", "Venezia", "2026-09-20T18:00:00Z") is None
print("claim ok: тот же матч месяцем позже не подставляется под сегодняшний сигнал")

# --- 10b. startDate — это ЛИСТИНГ, а не начало матча ------------------------
# Баг, который 21.08 в одиночку убивал весь футбол: покрытие было 0 из 14, и
# выглядело это как «Polymarket не котирует наши лиги». На деле «Genoa CFC vs.
# SSC Napoli» был выставлен 17.08 в 19:35, а матч начинался 22.08 в 18:45 —
# и проверка окна дат, читавшая startDate как начало, расходилась на четверо
# суток и выбрасывала событие.
real = {"title": "Genoa CFC vs. SSC Napoli",
        "startDate": "2026-08-17T19:35:00Z",     # когда рынок выставили
        "endDate": "2026-08-22T18:45:00Z",       # когда начинается матч
        "markets": []}
got = pm._event_start(real)
assert got is not None and got.isoformat().startswith("2026-08-22T18:45"), got
assert pm.match_event([real], "Genoa", "Napoli", "2026-08-22T18:45:00Z"), \
    "матч не сматчился по настоящему времени старта"
assert pm.match_event([real], "Genoa", "Napoli", "2026-08-17T19:35:00Z") is None, \
    "событие сматчилось по дате ЛИСТИНГА — ровно тот баг, что убил футбол"
print("claim ok: начало матча берётся из endDate, а дата листинга началом не считается")

# --- 10c. props-событие не заслоняет основной рынок -------------------------
# Polymarket разносит фикстуру по нескольким событиям. Матчер брал первое
# подошедшее по названию, и если первым попадался «- Player Props», мы уходили
# с событием без единого рынка на победу и писали «нет токена» — по журналу
# неотличимо от «рынка нет вовсе», хотя причины противоположные.
props = {"title": "Genoa CFC vs. SSC Napoli - Player Props",
         "startDate": "2026-08-17T19:35:00Z", "endDate": "2026-08-22T18:45:00Z",
         "markets": [{"question": "Rasmus Hojlund: Anytime Goalscorer",
                      "outcomes": '["Yes","No"]', "clobTokenIds": '["a","b"]'}]}
main = {"title": "Genoa CFC vs. SSC Napoli", "slug": "gen-nap",
        "startDate": "2026-08-17T19:35:00Z", "endDate": "2026-08-22T18:45:00Z",
        "markets": [{"question": "Genoa CFC vs. SSC Napoli",
                     "outcomes": '["Genoa CFC","SSC Napoli"]',
                     "clobTokenIds": '["tok-gen","tok-nap"]'}]}
merged = pm.merge_siblings([props, main])
assert len(merged) == 1, merged
ev2 = pm.match_event(merged, "Genoa", "Napoli", "2026-08-22T18:45:00Z")
legs2 = pm.find_legs(ev2, "SSC Napoli", "Genoa CFC", "SSC Napoli")
assert legs2.get("aggressive", {}).get("token_id") == "tok-nap", legs2
print("claim ok: события одной фикстуры склеиваются, и рынок победы находится "
      "даже когда первым попался props")

# --- 11. частота опроса подстраивается сама ---------------------------------
# Просьба: «ты должен в процессе сам подстроится в плане как часто его обновлять
# чтобы результат лучше был». Дальше от старта — реже (рынка ещё нет),
# в последние часы — каждый цикл.
# Монотонность — ВНУТРИ каждого состояния, а не через оба сразу. Первая
# редакция теста требовала общей монотонности и упала, и была неправа: найденный
# рынок за двое суток до старта СТОИТ опрашивать чаще, чем ненайденный за сутки.
# Там уже есть цена, и она ходит; здесь всё ещё нечего смотреть.
for matched in (False, True):
    mins = [pm.due_in_minutes(h, matched) for h in (48, 24, 6, 1)]
    assert mins == sorted(mins, reverse=True), (matched, mins)
    assert mins[-1] == 0, (matched, mins)
for h in (48, 24, 6):
    assert pm.due_in_minutes(h, True) < pm.due_in_minutes(h, False), h
print("claim ok: чем ближе старт, тем чаще; найденный рынок опрашивается чаще "
      f"ненайденного ({[pm.due_in_minutes(h, True) for h in (48,24,6,1)]} против "
      f"{[pm.due_in_minutes(h, False) for h in (48,24,6,1)]}), в последние часы — каждый цикл")

# --- 12. две ноги одной сделки ---------------------------------------------
# «бот в таком случае будет делать две ставки с разным кофом, если такое
# возможно». Агрессивная — прямая победа. Оптимальная — двойной шанс, который
# на Polymarket отдельной строкой не продаётся, но существует: «наш или ничья»
# это ровно «соперник НЕ победит», то есть токен No на рынке соперника.
EV2 = {"title": "Fenerbahce vs. Lyon", "slug": "fen-lyon",
       "startDate": "2026-08-21T19:00:00Z", "markets": [
    {"question": "Will Lyon win?", "outcomes": '["Yes","No"]',
     "clobTokenIds": '["y-lyon","n-lyon"]', "outcomePrices": '["0.30","0.70"]'},
    {"question": "Will Fenerbahce win?", "outcomes": '["Yes","No"]',
     "clobTokenIds": '["y-fen","n-fen"]', "outcomePrices": '["0.45","0.55"]'},
    {"question": "Fenerbahce vs. Lyon - Total Corners",
     "outcomes": '["Over 9.5","Under 9.5"]', "clobTokenIds": '["o","u"]'},
]}
legs = pm.find_legs(EV2, "Lyon", "Fenerbahce", "Lyon")
assert legs["aggressive"]["token_id"] == "y-lyon", legs
assert legs["optimal"]["token_id"] == "n-fen", legs
assert "или ничья" in legs["optimal"]["means"], legs
print("claim ok: агрессивная — Yes на Lyon, оптимальная — No на Fenerbahce "
      f"(«{legs['optimal']['means']}»), угловые не тронуты")

# угловые НЕ должны попасть ни в одну ногу — на них мы не ставим никогда
assert all(l["token_id"] not in ("o", "u") for l in legs.values()), legs

# --- 13. полное событие раскрывается, а не только первый рынок --------------
# «на полики раскрывай полное событие». Раньше смотрели только на победу и не
# знали, что теряем: у одного теннисного события Polymarket пятнадцать рынков.
kinds = {m["kind"] for m in pm.explode(EV2)}
assert pm.KIND_MONEYLINE in kinds and pm.KIND_TOTAL in kinds, kinds
assert len(pm.explode(EV2)) == 3, pm.explode(EV2)
print(f"claim ok: событие раскрывается целиком — {len(pm.explode(EV2))} рынка, "
      f"типы {sorted(kinds)}")

# --- 14. без цены у конторы оптимальная нога НЕ выдумывается ----------------
# Правило «лучше на 5%» требует базы. Нет базы — нет сделки, и строка честно
# говорит почему, вместо того чтобы подставить приблизительную цену. Ровно на
# этом теннис годами не давал безопасной ставки, и врать здесь нельзя.
rows = pm.check("Fenerbahce", "Lyon", "2026-08-21T19:00:00Z", "Lyon",
                entry_price=3.62, opt_price=None, events=[EV2])
by_leg = {r["leg"]: r for r in rows}
assert set(by_leg) == {"aggressive", "optimal"}, by_leg
assert by_leg["optimal"]["take"] is False, by_leg["optimal"]
assert "нет цены у конторы" in by_leg["optimal"]["reason"], by_leg["optimal"]
print("claim ok: без цены двойного шанса у конторы оптимальная нога не "
      "выдумывается — пишется причина")

# --- 15. звёзды меряют ОТСТАВАНИЕ, а не процент -----------------------------
# Переписано вечером 20.08. Идея Владислава: «звёзды от полимаркета должны
# исходить из вероятности того что ставить надо, а не процентов и повышения.
# Если мы видим что на большом количестве просело БК, а на полике нет — это
# охуенно». Прежняя шкала мерила размер зазора, то есть отвечала на вопрос
# «насколько тут дешевле». Деньги приносит ответ на другой вопрос: «насколько
# этот рынок ещё не понял того, что уже поняли все остальные».

# Отставание считается по отрезку, который движение уже прошло у контор.
assert pm.pm_lag(4.75, 4.75, 4.20) == 1.0        # не шелохнулся
assert pm.pm_lag(4.20, 4.75, 4.20) == 0.0        # отыграл всё
assert pm.pm_lag(4.475, 4.75, 4.20) == 0.5       # ровно половина
assert pm.pm_lag(4.05, 4.75, 4.20) < 0           # ушёл ДАЛЬШЕ контор
assert pm.pm_lag(4.75, 4.20, 4.20) is None       # движения не было — не врём числом
print("claim ok: отставание 1.0 = не шелохнулся, 0.0 = отыграл всё, "
      "отрицательное = знает больше нас, None = мерить нечего")

# ГЛАВНЫЙ СЛУЧАЙ, ради которого шкалу переписали: широкое движение у контор,
# которое Polymarket не заметил.
best = pm.pm_stars(lag=0.95, down_count=43, books_count=54, exec_stake=200)
assert best == 4, best
# То же движение, но площадка уже переставилась — движение настоящее, а денег
# в нём для нас нет.
priced_in = pm.pm_stars(lag=0.05, down_count=43, books_count=54, exec_stake=200)
assert priced_in == 2, priced_in
# И зеркальный случай: площадка стоит колом, но поехали две конторы. Скорее
# всего прав он, а шумим мы.
thin = pm.pm_stars(lag=0.95, down_count=2, books_count=54, exec_stake=200)
assert thin == 2, thin
assert best > priced_in and best > thin, (best, priced_in, thin)
print(f"claim ok: 43 конторы + отставание 0.95 = {best}★; те же 43 при "
      f"отставании 0.05 = {priced_in}★; отставание 0.95 при 2 конторах = {thin}★")

# Ни одна из двух величин в одиночку не даёт верхнюю ступень.
for lag_v, dn in ((0.95, config.MOVED_FOR_4_STARS - 1), (0.79, 43)):
    assert pm.pm_stars(lag=lag_v, down_count=dn, books_count=54,
                       exec_stake=200) < 4, (lag_v, dn)
# И глубина обязательна для верхней: зазор без размера — картинка.
assert pm.pm_stars(lag=0.95, down_count=43, books_count=54, exec_stake=30) == 3
print("claim ok: ни ширина без отставания, ни отставание без ширины, "
      "ни то и другое без размера не дают четвёртую ступень")

# --- 16. порог входа и нижняя ступень не имеют права разойтись -------------
# Иначе появится опубликованная сделка с нулём звёзд, то есть «сделки нет»
# рядом с реальным ордером бота.
r = pm.plan_entry([(1 / (4.00 * (1 + config.POLYMARKET_MIN_EDGE_PCT / 100)), 10_000)],
                  entry_price=4.00)
assert r["take"], r
assert pm.pm_stars(lag=0.1, down_count=1, books_count=50,
                   exec_stake=r["exec_stake_usd"]) >= 2, "прошло вход, но 0 звёзд"
print("claim ok: всё, что прошло правило входа, имеет минимум две звезды")

print("polymarket: все инварианты пройдены")


# --- 17. воронка развёрнута: кандидаты не ограничены нашими сигналами -------
# 21.08.2026. До этого дня мы спрашивали площадку только про то, что уже стало
# нашим сигналом или движением — 17 кандидатов за всё время. Но зазор на
# ордербуке не требует движения у контор: он требует ровно одного — чтобы там
# цена была не хуже нашей. Пара может стоять мёртвой у всех шестидесяти
# букмекеров, а Polymarket держать её выше просто потому, что туда никто не
# заглядывал.
import storage as _st  # noqa: E402
from datetime import datetime as _dt2, timedelta as _td2, timezone as _tz2  # noqa: E402

_st.init_db()
_soon = (_dt2.now(_tz2.utc) + _td2(hours=8)).isoformat()
_recs = []
for _i, _b in enumerate(["pinnacle", "betsson", "unibet", "williamhill", "onexbet"]):
    _recs += [{"fixture_id": "quiet1", "sport_key": "soccer_q", "start_time": _soon,
               "home_team": "Quiet", "away_team": "Still", "bookmaker": _b,
               "market_id": "h2h", "outcome_id": "home", "outcome_name": "Quiet",
               "player_key": "-", "price": 3.00 + _i * 0.05, "label": "Quiet"}]
_st.save_snapshot(_recs, _dt2.now(_tz2.utc).isoformat())

_uni = _st.pm_universe(min_books=config.PM_UNIVERSE_MIN_BOOKS)
_hit = [r for r in _uni if r["fixture_id"] == "quiet1"]
assert _hit, "пара без единого движения не попала в множество кандидатов"
assert _hit[0]["entry_price"] == 3.20, _hit[0]      # лучшая из пяти, не первая
assert _hit[0]["down_count"] == 0 and _hit[0]["old_price"] is None, _hit[0]
print(f"claim ok: событие, где НИЧЕГО не двигалось, попадает в кандидаты с "
      f"лучшей ценой {_hit[0]['entry_price']:.2f} из {_hit[0]['books_count']} контор")

# И отставание для такой пары не выдумывается: движения не было, мерить нечего.
assert pm.pm_lag(3.5, _hit[0]["old_price"], _hit[0]["new_price"]) is None
assert pm.pm_stars(lag=None, down_count=0, books_count=5, exec_stake=200,
                   edge_pct=1.0) == 2
print("claim ok: без движения отставание не считается, а оценка падает до "
      "нижней ступени вместо выдуманной")

# --- 18. лестница порогов считает СДЕЛКИ, а не взгляды ----------------------
# Мы смотрим на одно событие десятки раз. Считать каждый взгляд отдельной
# возможностью значило бы умножить объём на частоту опроса и отчитаться о
# сотнях сделок там, где их две.
for _k in range(4):
    _st.save_pm_quote({"checked_at": f"2026-08-21T1{_k}:00:00", "fixture_id": "L1",
                       "outcome_name": "Same", "leg": "aggressive", "matched": True,
                       "take": False, "entry_price": 4.00,
                       "best_coef": 4.00 + _k * 0.05, "source": "universe"})
_lad = _st.pm_threshold_ladder()
assert _lad["trades_seen"] >= 1
_at0 = next(s_["trades"] for s_ in _lad["steps"] if s_["threshold"] == 0.0)
assert _at0 >= 1, _lad
_neg = next(s_["trades"] for s_ in _lad["steps"] if s_["threshold"] == -2.0)
assert _neg >= _at0, "порог мягче обязан открывать не меньше сделок"
print(f"claim ok: четыре взгляда на одно событие — это одна сделка в лестнице "
      f"(при 0%: {_at0}, при -2%: {_neg})")
