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

# --- the share ceiling, and exactly how much it is allowed to take ----------
# Added 2026-08-18, the same day the rule itself was fixed. Until that morning
# breadth was scored twice -- by count and by share -- and the SMALLER of the
# two won, so a low share could overrule any amount of independent
# confirmation: twelve books out of seventy is 17%, which collapsed a four-star
# move to one star and deleted it. The page never mentioned this, because the
# page described the ladder purely in counts. Prose and code disagreed and
# nothing complained, which is the exact failure this file exists to prevent.
#
# So the sentence is now on the page, and it is pinned here in two ways: the
# words must be present, and the function must actually behave the way the
# words promise. Either one drifting fails the suite.
import analytics  # noqa: E402

if config.SHARE_DOCKS_ONE_RUNG:
    says("ровно на одну",
         "страница обязана признать, что доля рынка может опустить ступень — "
         "и обязана сказать, что ровно на одну, иначе читатель не сможет "
         "пересчитать оценку сам")

    # the promise, checked against the function rather than against itself
    for moved, quoting in ((6, 50), (8, 50), (10, 60), (12, 70), (43, 54), (4, 70)):
        books = {f"b{i}" for i in range(moved)}
        got = analytics._stars(books, False, quoting)
        by_count = (4 if moved >= config.MOVED_FOR_4_STARS
                    else 3 if moved >= config.MOVED_FOR_3_STARS
                    else 2 if moved >= config.MOVED_FOR_2_STARS
                    else 1)
        assert by_count - got <= 1, (
            f"{moved} из {quoting}: по счёту {by_count}★, а выдано {got}★ — "
            "страница обещает падение ровно на одну ступень, код отнял больше")
        assert got <= by_count, (
            f"{moved} из {quoting}: доля НАБАВИЛА ступень ({by_count}★ → {got}★) — "
            "это не потолок, а подарок, и на странице такого не обещано")
    print("claim ok: доля опускает ступень максимум на одну — проверено на "
          "6/50, 8/50, 10/60, 12/70, 43/54, 4/70")

    # and the reason must be stated, not just the mechanic: a docked rung is a
    # statement about US being early, not about the signal being weak
    says("мы рано",
         "страница обязана объяснить, ПОЧЕМУ низкая доля снижает ступень — "
         "иначе правило выглядит произвольным")
    print("claim ok: страница объясняет смысл понижения — рынок среагировал не весь")
else:
    assert "ровно на одну" not in text, (
        "SHARE_DOCKS_ONE_RUNG выключен, а страница всё ещё обещает понижение "
        "ровно на одну ступень")
    print("claim ok: понижение по доле отключено, и страница про него молчит")

# --- and the flat-stake promise must be on the page, not just in the code ---
# It is the reason the by-stars table can be trusted, so a reader has to be
# able to see it stated.
says("одинаковой суммой",
     "страница обязана говорить, что все ступени считаются одной суммой — "
     "иначе таблица «по звёздам» ничего не доказывает")
print("claim ok: страница объясняет, что ступени считаются одинаковой суммой")

# --- Polymarket: the page must state the rule the trader actually follows ---
# Added 20.08.2026, when Polymarket became the product rather than a side
# experiment. The stakes here are higher than anywhere else on this page: a
# real bot places real orders on the numbers this section describes, so a
# stale sentence is not a cosmetic problem, it is a wrong trade.
# Порог опущен до нуля 20.08 вечером, и фраза обязана следовать за числом, а
# не за первой редакцией: «минимум на 0% выше» было бы формально верно и
# бессмысленно на слух.
says(dashboard._pm_rule_phrase(),
     "страница обязана называть настоящий порог входа на Polymarket — по нему "
     "торгует бот, и разойтись здесь дороже, чем где-либо ещё")
if config.POLYMARKET_MIN_EDGE_PCT <= 0:
    says("не хуже лучшей цены", "при нулевом пороге правило формулируется словами")
    says("Хуже — не берём", "страница обязана сказать, что хуже конторы мы не ставим")
# Шкала переписана вечером 20.08: она измеряет не размер зазора, а ОТСТАВАНИЕ
# площадки от движения, помноженное на ширину этого движения. Идея Владислава:
# «если мы видим что на большом количестве просело БК, а на полике нет — это
# охуенно». Страница обязана описывать ту шкалу, которая работает, а не ту,
# которая была утром.
for n_books in (config.MOVED_FOR_3_STARS, config.MOVED_FOR_4_STARS):
    says(f"от {n_books} контор", f"страница обязана называть ширину для ступени")
for lag in (config.PM_LAG_3_STARS, config.PM_LAG_4_STARS):
    says(f"отставание от {lag:g}", f"страница обязана называть порог отставания {lag:g}")
says("отставание", "страница обязана вводить понятие отставания площадки")
assert "зазор от" not in text, (
    "на странице осталась старая шкала по проценту зазора — она перестала быть "
    "правдой, когда оценка стала считаться по отставанию и ширине")
print(f"claim ok: Polymarket — вход {dashboard._pm_rule_phrase()}, ступени по "
      f"ширине {config.MOVED_FOR_3_STARS}/{config.MOVED_FOR_4_STARS} контор и "
      f"отставанию {config.PM_LAG_3_STARS:g}/{config.PM_LAG_4_STARS:g}")

# And the honest disclaimer about the two legs must survive any redesign: they
# are NOT a hedge, and a reader who thinks otherwise is mispricing their risk.
says("проигрывают вместе",
     "страница обязана сказать, что две ноги не страхуют друг друга — иначе "
     "читатель посчитает риск вдвое меньше настоящего")
assert "страховка" in text, "нет явного отрицания страховки"
print("claim ok: страница честно говорит, что две ноги — не хедж")

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

# --- every headline number must agree with its own noun ---------------------
# Russian noun agreement, checked on the page rather than on the helper. The
# six KPI labels were fixed plural-many strings until 2026-08-15, so they only
# read correctly for counts of five and up -- and a reset book, which is
# exactly the state the tracker starts every experiment in, produced "101
# событий", "2 срезов рынка", "32 841 котировок сверено".
#
# It is a small thing that is not a small thing: the entire claim of this page
# is that its numbers can be recounted, and a number that does not agree with
# the word next to it says nobody looked.
KPI_FORMS = {
    "контор": ("контора", "конторы", "контор"),
    "событ": ("событие", "события", "событий"),
    "котировк": ("котировка сверена", "котировки сверены", "котировок сверено"),
    "срез": ("срез рынка", "среза рынка", "срезов рынка"),
    "движен": ("движение", "движения", "движений"),
    "сигнал": ("сигнал со входом", "сигнала со входом", "сигналов со входом"),
}
for n in (0, 1, 2, 3, 5, 11, 21, 22, 101, 1002, 32_841):
    for stem, (one, few, many) in KPI_FORMS.items():
        got = dashboard._plural(n, one, few, many)
        # the rule itself, restated independently of the implementation
        last, last2 = n % 10, n % 100
        want = (one if last == 1 and last2 != 11
                else few if 2 <= last <= 4 and not 12 <= last2 <= 14
                else many)
        assert got == want, (n, stem, got, want)
print("claim ok: подписи под числами согласуются с ними для 0, 1, 2, 5, 11, 21, 101, 32 841")

# and the rendered page must actually USE the agreeing forms, not a frozen one
import re as _re  # noqa: E402
for bad in (r"\b1 событий\b", r"\b2 срезов\b", r"\b1 контор\b", r"\b2 движений\b"):
    hit = _re.search(bad, text)
    assert not hit, f"на странице несогласованная подпись: {hit.group(0)}"
print("claim ok: на странице нет несогласованных пар «число + слово»")
