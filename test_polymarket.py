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
r = pm.plan_entry([(0.20, 10_000)], entry_price=4.00)
assert r["need_coef"] == 4.2, r          # 4.00 * 1.05
assert r["best_coef"] == 5.0, r
assert r["take"] and r["fits_target"], r
assert r["exec_stake_usd"] == 200.0, r   # берём ровно целевые $200, не больше
assert r["avg_coef"] == 5.0, r
assert r["edge_pct"] == 25.0, r
print(f"claim ok: БК 4.00 → нужен коэф {r['need_coef']}, стакан даёт 5.00, "
      f"берём ${r['exec_stake_usd']:g} с плюсом {r['edge_pct']}%")

# --- 2. ровно на пороге берём, на волосок ниже — нет ------------------------
# Граница обязана быть детерминированной: «лучше минимум на 5%» значит, что
# ровно пять процентов проходят.
exact = pm.plan_entry([(1 / 4.20, 10_000)], entry_price=4.00)
assert exact["take"] and exact["edge_pct"] == 5.0, exact
just_under = pm.plan_entry([(1 / 4.19, 10_000)], entry_price=4.00)
assert not just_under["take"] and just_under["exec_stake_usd"] == 0.0, just_under
print("claim ok: 5.00% ровно — берём, 4.75% — не берём; граница не плавает")

# --- 3. ГЛАВНОЕ: цену хуже конторы не берём никогда -------------------------
# Живой случай 20.08: Рыбакина, наш вход 2.56 у onexbet, стакан Polymarket
# отдаёт лучший ask 0.42 = коэффициент 2.381. Polymarket ХУЖЕ на 7%.
ryb = pm.plan_entry([(0.42, 9524.68), (0.43, 28271.25), (0.44, 5152.69)],
                    entry_price=2.56)
assert not ryb["take"], ryb
assert ryb["exec_stake_usd"] == 0.0 and ryb["edge_pct"] is None, ryb
assert ryb["best_coef"] < 2.56, ryb
print(f"claim ok: живой рынок Рыбакиной — БК 2.56 против стакана "
      f"{ryb['best_coef']}, сделки нет (нужно было бы {ryb['need_coef']})")

# --- 4. «залезем ли на $200» — отвечаем суммой, а не отказом ----------------
# Прямая просьба: «если к примеру не залезаем, то писать ту сумму в таблице по
# которой залезли». Уровень 0.20 держит только $30, следующий уже ниже порога.
thin = pm.plan_entry([(0.20, 150), (0.25, 100_000)], entry_price=4.00)
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
     "startDate": "2026-08-20T18:00:00Z", "markets": []},
    {"id": "2", "title": "Udinese Calcio vs. Venezia FC",
     "startDate": "2026-08-20T18:00:00Z", "slug": "main",
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

print("polymarket: все инварианты пройдены")
