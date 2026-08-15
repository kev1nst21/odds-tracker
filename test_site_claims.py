"""Every rule the site STATES must be the rule the code FOLLOWS.

Requested 2026-08-15: "пусть наши тестировщики прогоняют логику написанного на
сайте и логику вообще проекта".

The other suites check that functions compute the right thing. This one checks
something they structurally cannot: that the prose is still true. A page can be
arithmetically perfect and still lie, simply by describing a rule that was
changed months ago -- and prose has no compiler, so nothing complains.

It is not a hypothetical failure. Two of them shipped this month. The footer
promised a 20-minute cadence while a bug ran two cycles per window. Step 03 of
"как это работает" said we count HOW MANY bookmakers moved, which stopped being
true the moment breadth became a share -- and on a site whose entire pitch is
that every number can be recounted, a stale sentence is not a typo, it is the
product failing at the only thing it claims.

So each check below pins a sentence on the page to the constant that governs
it. Change the constant without changing the sentence, or the other way round,
and this fails.
"""
import os
import re
import tempfile

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "claims.db")
os.environ.setdefault("DASHBOARD_DIR", tempfile.mkdtemp())

import config  # noqa: E402
config.DB_PATH = os.environ["DB_PATH"]

import storage  # noqa: E402
import dashboard  # noqa: E402

storage.DB_PATH = config.DB_PATH
storage.init_db()

page = open(dashboard.render_dashboard([], quota={"remaining": 4_900_000, "used": 100}),
            encoding="utf-8").read()
text = re.sub(r"<[^>]+>", " ", page)
text = re.sub(r"\s+", " ", text)


def says(fragment, why):
    assert fragment in text, f"на странице нет утверждения «{fragment}» — {why}"


# --- the alert threshold -----------------------------------------------------
says(f"порог алерта {config.SPIKE_THRESHOLD_PCT * 100:.0f}%",
     "футер обязан называть тот же порог, по которому детектор ловит движение")
print(f"claim ok: порог алерта на странице = {config.SPIKE_THRESHOLD_PCT*100:.0f}% = SPIKE_THRESHOLD_PCT")

# --- the cadence -------------------------------------------------------------
# Both the footer and step 01 of the method quote it, and they have disagreed
# before, so both are pinned.
says(f"опрос каждые {config.POLL_INTERVAL_MINUTES:g} мин",
     "футер обязан называть реальную частоту опроса из cadence.json")
says(f"Раз в {config.POLL_INTERVAL_MINUTES:g} минут",
     "шаг 01 методики обязан называть ту же частоту, что и футер")
print(f"claim ok: частота на странице = {config.POLL_INTERVAL_MINUTES:g} мин в обоих местах")

# --- the rejection reasons ---------------------------------------------------
# Rendered only for buckets that actually caught something, so a funnel with
# one movement in every bucket is fed in rather than asserting against an empty
# page. That also proves the labels survive real data, not just a happy path.
every_bucket = {"big_drop": 6, "thin_market": 1, "all_books_moved": 1,
                "entry_too_low": 1, "low_stars": 1, "off_band": 1, "too_far": 1}
reasons = re.sub(r"<[^>]+>", " ", dashboard._funnel_block(every_bucket, "24 ч"))
reasons = re.sub(r"\s+", " ", reasons)

for fragment, why in (
    (f"коэффициент вне полосы {config.MIN_SIGNAL_PRICE:g}–{config.MAX_SIGNAL_PRICE:g}",
     "полоса коэффициентов обязана совпадать с конфигом"),
    (f"меньше {config.MIN_SIGNAL_STARS} звёзд",
     "порог по звёздам обязан совпадать с MIN_SIGNAL_STARS"),
    (f"матч дальше {config.MAX_LEAD_HOURS:g} ч",
     "горизонт публикации обязан совпадать с MAX_LEAD_HOURS"),
):
    assert fragment in reasons, f"в причинах отсева нет «{fragment}» — {why}\n{reasons[:400]}"
print(f"claim ok: причины отсева называют реальные пороги — полоса "
      f"{config.MIN_SIGNAL_PRICE:g}–{config.MAX_SIGNAL_PRICE:g}, "
      f"{config.MIN_SIGNAL_STARS} звезды, горизонт {config.MAX_LEAD_HOURS:g} ч")

# Every bucket must be nameable: a movement rejected into a bucket with no
# label would vanish from the explanation and the numbers would stop adding up.
for bucket in ("thin_market", "all_books_moved", "entry_too_low",
               "low_stars", "off_band", "too_far"):
    one = {"big_drop": 1, bucket: 1}
    block = dashboard._funnel_block(one, "24 ч")
    assert "не ставили:" in block, f"причина «{bucket}» не имеет подписи на странице"
print("claim ok: у каждой причины отсева есть человеческая подпись")

# --- the confidence ladder must be spelled out, rung by rung ----------------
# This sentence has now gone stale twice in one day -- first when breadth
# became a share, then when it went back to counts and grew a fourth rung.
# Pinned to the constants so it cannot drift a third time.
says("сколько независимых контор", "шаг 03 обязан описывать число контор")
for rung, threshold in ((2, config.MOVED_FOR_2_STARS),
                        (3, config.MOVED_FOR_3_STARS),
                        (4, config.MOVED_FOR_4_STARS)):
    label = config.STAR_LABELS[rung]
    says(f"{'★' * rung} {label}", f"страница обязана называть ступень {rung}★ словами")
    says(f"от {threshold}", f"страница обязана называть порог ступени {rung}★")
assert "какая доля контор" not in text, (
    "на странице осталась формулировка про ДОЛЮ рынка — она перестала быть "
    "правдой, когда шкала вернулась к абсолютному счёту контор")
print("claim ok: методика описывает лестницу доверия числами контор — "
      + ", ".join(f"{'★'*k} {config.STAR_LABELS[k]} от {t}"
                  for k, t in ((2, config.MOVED_FOR_2_STARS),
                               (3, config.MOVED_FOR_3_STARS),
                               (4, config.MOVED_FOR_4_STARS))))

# --- and the flat-stake promise must be on the page, not just in the code ---
# It is the reason the by-stars table can be trusted, so a reader has to be
# able to see it stated.
says("одинаковой суммой",
     "страница обязана говорить, что все ступени считаются одной суммой — "
     "иначе таблица «по звёздам» ничего не доказывает")
print("claim ok: страница объясняет, что ступени считаются одинаковой суммой")

# --- the two strategies ------------------------------------------------------
says(f"коэффициентом не выше {config.OPTIMAL_MAX_PRICE:g}",
     "описание оптимальной стратегии обязано называть настоящий потолок цены")
says(f"коэффициент выше {config.SAFE_TRIGGER_PRICE:g}",
     "шаг 05 обязан называть настоящий порог безопасного варианта")
print(f"claim ok: оптимальная ≤{config.OPTIMAL_MAX_PRICE:g}, "
      f"безопасный вариант от {config.SAFE_TRIGGER_PRICE:g}")

# --- the disclaimer must survive any redesign -------------------------------
# Not a rule about odds, a rule about us. It has to be on the page whatever
# else changes.
for must in ("не рекомендация", "риск потерять деньги", "18"):
    assert must in text, f"с футера пропала обязательная оговорка: «{must}»"
print("claim ok: дисклеймер на месте — не рекомендация, риск, 18+")

# --- and nothing may claim a certainty the project does not have ------------
# A guardrail against wording drift in future edits.
for banned in ("гарантируем", "гарантия выигрыша", "беспроигрыш", "100% точность"):
    assert banned not in text.lower(), f"на странице появилось обещание «{banned}»"
print("claim ok: на странице нет обещаний выигрыша")

print(f"логика написанного на сайте: все утверждения совпали с кодом ({len(page)} байт)")
