"""The confidence ladder: four rungs, and each must mean something distinct.

Rewritten 2026-08-15 when Vladislav chose counts over shares and asked for the
book to be reset: "не процент от количества контор, а фактически столько,
сколько мы считаем достаточно... давай придумаем разрезы по 2-3-4 звезды с
уровнем доверия к ставке... их должно быть намного больше".

"Намного больше" and "не поставить хуёвую ставку" pull in opposite directions,
and the resolution is not a compromise between them -- it is that every rung is
published, labelled, and scored SEPARATELY at the same stake. Volume comes from
publishing two stars; safety comes from two stars being visibly two stars, and
from its own row in the by-stars table telling the truth about it within days.

That only works if the ladder has real structure, which is what this file
pins down. A scale whose rungs overlap, or invert, or all collapse into one
population, would look like a confidence system while measuring nothing.
"""
import os
import tempfile

os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "stars.db"))
os.environ.setdefault("DASHBOARD_DIR", tempfile.mkdtemp())

import config  # noqa: E402
import analytics  # noqa: E402

stars = analytics._stars


def books(n):
    return {f"book{i}" for i in range(n)}


# --- 1. the rungs are where the config says they are ------------------------
assert stars(books(config.MOVED_FOR_2_STARS), False) == 2
assert stars(books(config.MOVED_FOR_3_STARS), False) == 3
assert stars(books(config.MOVED_FOR_4_STARS), False) == 4
assert stars(books(config.MOVED_FOR_2_STARS - 1), False) == 1
print(f"инвариант ok: ступени стоят там, где написано — "
      f"{config.MOVED_FOR_2_STARS}→★★, {config.MOVED_FOR_3_STARS}→★★★, "
      f"{config.MOVED_FOR_4_STARS}→★★★★")

# --- 2. the ladder must be strictly ordered, with no gaps -------------------
# A rung nobody can land on is a rung that will never accumulate a sample, and
# a confidence level with no sample is decoration.
reachable = {stars(books(n), False) for n in range(1, config.MOVED_FOR_4_STARS + 5)}
assert reachable == {1, 2, 3, 4}, reachable
seen = [stars(books(n), False) for n in range(1, 40)]
assert seen == sorted(seen), seen
print(f"инвариант ok: все четыре ступени достижимы {sorted(reachable)}, "
      f"шкала монотонна")

# --- 3. the sharp bonus lifts exactly one rung and cannot overflow ----------
for n in range(1, 30):
    plain = stars(books(n), False)
    sharp = stars(books(n), True)
    assert sharp - plain in (0, 1), (n, plain, sharp)
    assert sharp <= config.MAX_STARS, (n, sharp)
assert stars(books(config.MOVED_FOR_4_STARS), True) == config.MAX_STARS
print(f"инвариант ok: шарп-контора поднимает ровно на ступень и не выше "
      f"{config.MAX_STARS}")

# --- 4. what we publish, and what we must never publish ---------------------
# The floor moved from three to two deliberately; one star must stay unpublished
# whatever else changes, because a single bookmaker is not evidence of anything.
assert config.MIN_SIGNAL_STARS == 2, config.MIN_SIGNAL_STARS
assert stars(books(1), False) < config.MIN_SIGNAL_STARS, (
    "одна контора не должна попадать в публикацию")
published = [n for n in range(1, 30) if stars(books(n), False) >= config.MIN_SIGNAL_STARS]
assert min(published) == config.MOVED_FOR_2_STARS, published[:5]
print(f"инвариант ok: публикуем от {config.MIN_SIGNAL_STARS} звёзд "
      f"(от {config.MOVED_FOR_2_STARS} контор), одна контора — никогда")

# --- 5. every published rung has a human name -------------------------------
# The whole point of publishing weak signals is that the reader can tell they
# are weak. An unlabelled rung would turn "more signals" into "noisier bot".
for rung in range(config.MIN_SIGNAL_STARS, config.MAX_STARS + 1):
    label = config.STAR_LABELS.get(rung)
    assert label, f"у ступени {rung}★ нет словесного уровня доверия"
assert len(set(config.STAR_LABELS.values())) == len(config.STAR_LABELS), (
    "две ступени названы одинаково — читатель их не различит")
print("инвариант ok: у каждой публикуемой ступени свой уровень доверия: "
      + ", ".join(f"{'★'*k} {v}" for k, v in sorted(config.STAR_LABELS.items())))

# --- 6. the ladder must not collapse into one population --------------------
# If the rungs sat too close together almost every signal would land on one of
# them and the by-stars table would compare nothing. Stated as spacing so a
# future edit that quietly narrows the gaps fails here rather than in the data
# three weeks later.
assert config.MOVED_FOR_3_STARS >= config.MOVED_FOR_2_STARS * 2, (
    config.MOVED_FOR_2_STARS, config.MOVED_FOR_3_STARS)
assert config.MOVED_FOR_4_STARS >= config.MOVED_FOR_3_STARS * 2, (
    config.MOVED_FOR_3_STARS, config.MOVED_FOR_4_STARS)
print(f"инвариант ok: ступени разнесены вдвое "
      f"({config.MOVED_FOR_2_STARS}/{config.MOVED_FOR_3_STARS}/"
      f"{config.MOVED_FOR_4_STARS}) — они соберут разные выборки, а не одну")

# --- 7. the stake must stay flat across rungs -------------------------------
# The measurement only answers "is two stars worth taking" while every rung is
# scored on the same money. Sizing by confidence is the obvious next move and
# it must not happen before the data is in -- profit would then reflect the
# stake schedule rather than the edge.
import storage  # noqa: E402
assert isinstance(storage.FLAT_STAKE, (int, float)) and storage.FLAT_STAKE > 0
assert not hasattr(storage, "STAKE_BY_STARS"), (
    "появилось разное плечо по звёздам — это уничтожит сравнимость ступеней")
print(f"инвариант ok: все ступени считаются одинаковой суммой ${storage.FLAT_STAKE:g}, "
      f"поэтому таблица «по звёздам» сравнивает силу сигнала, а не схему ставок")

print("лестница доверия: все инварианты пройдены")

# --- 8. width alone must not lift a rating -----------------------------------
# The property the share ceiling exists for, restated on 2026-08-18 when the
# ceiling was softened from min() to a one-rung dock.
#
# The ORIGINAL statement was "the same share at any feed width scores the
# same". That is no longer true, and pretending otherwise would be dishonest:
# under a one-rung dock, four books out of seventeen score two stars while
# twelve out of fifty-one -- the identical share -- score three. That is a
# deliberate choice. Twelve independent bookmakers confirming a move IS
# stronger evidence than four, and a rule that refuses to see the difference
# throws away the most reliable thing we measure.
#
# What must remain impossible is the thing that actually motivated the
# ceiling: a FIXED handful of books buying a rating simply because we bought a
# wider subscription. So the invariant is monotonicity in the denominator --
# holding the number of movers fixed, a wider feed can only lower the rating,
# never raise it.
for moved in (2, 3, 4, 5, 6, 8, 12):
    seen = [stars(books(moved), False, q) for q in (12, 17, 25, 40, 60, 90, 150)]
    assert seen == sorted(seen, reverse=True), (moved, seen)
print("инвариант ok: при том же числе двинувшихся контор более широкий фид "
      "оценку не повышает — ширину подписки нельзя обменять на звёзды")

# and the specific case that started it all: a handful on a wide market
assert stars(books(4), False, 70) < config.MIN_SIGNAL_STARS, (
    "4 конторы из 70 (6%) не должны публиковаться сами по себе")
# With a sharp book among them it becomes the LOWEST published rung -- two
# stars, "осторожно" -- and that is unchanged by the 2026-08-18 softening. It
# is a deliberate call, not an oversight: Pinnacle moving is worth a rung, and
# the reader is told plainly that this is the cautious tier.
assert stars(books(4), True, 70) == config.MIN_SIGNAL_STARS, stars(books(4), True, 70)
print("инвариант ok: 4 из 70 не сигнал; с острой конторой — минимальная "
      "ступень «осторожно», не выше")

# --- 9. a sharp book lifts, but never publishes on its own ------------------
# Until 2026-08-15 `well_evidenced` had an "or sharp_down" escape hatch, so one
# Pinnacle move became a signal and landed in the same population as genuine
# multi-book moves, spoiling the measurement of both.
import inspect  # noqa: E402
# Comments in that function legitimately mention the old expression, so the
# check is on the CODE: strip comment bodies before looking.
src = "\n".join(l.split("#")[0] for l in
                inspect.getsource(analytics.build_event_summaries).splitlines())
assert "or sharp_down" not in src, (
    "вернулась лазейка: одна шарп-контора снова открывает публикацию в одиночку")
print(f"инвариант ok: шарп поднимает ступень, но публикацию в одиночку не открывает "
      f"(нужно минимум {config.MIN_MOVED_BOOKS} контор)")

# --- 10. the quality gate must be REACHABLE --------------------------------
# MIN_MOVED_BOOKS and MOVED_FOR_2_STARS coinciding made low_stars a dead
# bucket: everything that passed "at least two books" was automatically
# publishable, so there was no quality filter left at all. The gap between
# "we record it" and "we bet it" has to exist.
assert config.MIN_MOVED_BOOKS < config.MOVED_FOR_2_STARS, (
    f"MIN_MOVED_BOOKS={config.MIN_MOVED_BOOKS} и MOVED_FOR_2_STARS="
    f"{config.MOVED_FOR_2_STARS} совпали — корзина low_stars недостижима, "
    f"фильтра по качеству нет")
gap = [n for n in range(config.MIN_MOVED_BOOKS, config.MOVED_FOR_2_STARS)]
print(f"инвариант ok: движения у {gap} контор пишем в журнал, но не публикуем — "
      f"фильтр по качеству живой")

# --- 11. the share ceiling may dock one rung, never two ---------------------
# The most destructive line in the project, live from 2026-08-15 to 08-18.
# min(by_count, by_share) meant a single low share overruled any amount of
# independent confirmation: twelve bookmakers out of seventy moving the same
# way scored 17% share and collapsed from four stars to ONE, which is below the
# publishing floor -- so it never reached the page at all.
#
# Vladislav spotted the symptom without seeing the code: "если даже у 5-6
# контор линия тронулась, то мы это событие должны рассматривать".
#
# The deeper error was treating share purely as conviction. It is also a
# measure of HOW LATE WE ARE: a steam move begins with a few books and ends
# with all of them, and by the time share is high the entry is usually gone
# (that is literally the all_books_moved bucket). A hard share cap therefore
# punished catching moves early, which is the one thing this product is for.
for moved, quoting, floor in ((6, 50, 2), (8, 50, 2), (10, 60, 2), (12, 70, 3)):
    got = stars(books(moved), False, quoting)
    assert got >= floor, (
        f"{moved} контор из {quoting} ({moved/quoting:.0%}) дали {got}★ — "
        f"широкая независимая поддержка снова схлопывается долей")
    assert got >= config.MIN_SIGNAL_STARS, (moved, quoting, got)
print("инвариант ok: 6/50, 8/50, 10/60 и 12/70 публикуются, а не исчезают")

# ...and the thing the ceiling exists for must still hold
for moved, quoting in ((4, 70), (5, 50), (3, 20), (2, 60)):
    got = stars(books(moved), False, quoting)
    assert got < config.MIN_SIGNAL_STARS, (moved, quoting, got)
print("инвариант ok: горстка контор на широком рынке по-прежнему не сигнал")

# the dock is exactly one rung, never more
for quoting in (20, 45, 70, 120):
    for moved in range(1, quoting + 1):
        share = moved / quoting
        by_count = (4 if moved >= config.MOVED_FOR_4_STARS
                    else 3 if moved >= config.MOVED_FOR_3_STARS
                    else 2 if moved >= config.MOVED_FOR_2_STARS else 1)
        got = stars(books(moved), False, quoting)
        assert by_count - got <= 1, (moved, quoting, by_count, got)
        assert got <= by_count, (moved, quoting, by_count, got)
print("инвариант ok: доля снимает ровно одну ступень и никогда не добавляет")
