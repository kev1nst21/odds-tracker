"""Renders the whole public site as one self-contained static HTML file.

There is no backend, no build step and no framework: main.py calls
render_dashboard() at the end of every poll and the result is published to
GitHub Pages. Everything therefore has to be inline -- CSS, JS, SVG, even the
sound -- and it has to stay small enough to open fast on a phone.

Two rules shape every design decision here:

  * Motion is only allowed if it proves something. A count-up on a figure, a
    pulse on a row that just changed, a countdown to the next poll -- these are
    evidence that the system is running. Decorative motion (confetti on a new
    signal, a custom cursor, parallax) reads as a slot machine to an audience
    that assumes every betting site is a scam, so none of it is here.
  * Colour never carries meaning alone. Red and green sit only ~4.1 ΔE apart
    under deuteranopia, so every state also gets an icon and a word.

The page is built with string.Template rather than str.format, because the
whole file is full of CSS and JS braces and doubling every one of them was how
the countdown script got silently corrupted once already.
"""
import html
import json
import re
import os
from datetime import datetime, timedelta, timezone
from string import Template

import analytics
import storage
from config import (
    POLYMARKET_MIN_EDGE_PCT,
    POLYMARKET_TARGET_STAKE,
    PM_LAG_3_STARS,
    PM_LAG_4_STARS,
    MOVED_FOR_3_STARS,
    MOVED_FOR_4_STARS,
    MOVED_FOR_2_STARS,
    MOVED_FOR_3_STARS,
    MOVED_FOR_4_STARS,
    MAX_STARS,
    STAR_LABELS,
    DASHBOARD_PATH,
    FLAT_STAKE,
    MATCH_MAX_DURATION_HOURS,
    MAX_SPORTS_PER_CYCLE,
    POLL_INTERVAL_MINUTES,
    PUBLISH_INTERVAL_MINUTES,
    CADENCE_LABEL,
    SPIKE_THRESHOLD_PCT,
    OPTIMAL_MAX_PRICE,
    MIN_SIGNAL_PRICE,
    MAX_SIGNAL_PRICE,
    MIN_SIGNAL_STARS,
    MAX_LEAD_HOURS,
    SAFE_TRIGGER_PRICE,
)

LEDGER_PATH = os.path.join(os.path.dirname(DASHBOARD_PATH), "ledger.json")


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: 1 событие / 2-4 события / 5+ событий."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _hours_word(n: int) -> str:
    return _plural(n, "час", "часа", "часов")


def _num(n) -> str:
    """Thin-space thousands separator -- 7 302 rather than 7,302."""
    try:
        return f"{int(n):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "0"


def _parse_iso(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_dt(value) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


def _fmt_start(value) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%d.%m %H:%M") if dt else "—"


def _ago(value, now=None) -> str:
    dt = _parse_iso(value)
    if not dt:
        return "—"
    now = now or datetime.now(timezone.utc)
    mins = int((now - dt).total_seconds() // 60)
    if mins < 0:
        return f"через {abs(mins)} мин"
    if mins < 1:
        return "только что"
    if mins < 60:
        return f"{mins} мин назад"
    hours = mins // 60
    if hours < 24:
        return f"{hours} ч {mins % 60} мин назад"
    return f"{hours // 24} дн назад"


# --------------------------------------------------------------------------
# the confidence ladder
# --------------------------------------------------------------------------
# Until 2026-08-15 only the top rung was ever published, so the stars in a row
# said nothing a reader had to act on -- every row carried the same three of
# them. Now three rungs are published side by side and the rung is the first
# judgement the reader makes, which means ★★ must not land on the eye with the
# same weight as ★★★★.
#
# Three cues carry it, deliberately redundant, per the rule at the top of this
# module that colour never means anything on its own:
#
#   * the unlit positions are always drawn, so the mark is a meter -- ★★☆☆ next
#     to ★★★★ compares itself even in black and white, and the column keeps a
#     constant width instead of jumping between rows;
#   * each rung has its own ink -- lime, amber, muted grey;
#   * the word from STAR_LABELS is printed with it, so the rung can be READ
#     rather than counted or matched against a legend.
#
# The track is markup rather than a CSS ::before, because its length has to
# follow MAX_STARS and the stylesheet is a static template with no way to
# interpolate it.

# How many bookmakers each rung needs, for the tooltips. Read from config so a
# threshold change cannot leave the hint on the chip claiming the old number.
MOVED_FOR = {2: MOVED_FOR_2_STARS, 3: MOVED_FOR_3_STARS, 4: MOVED_FOR_4_STARS}


def _star_mark(stars, *, word: bool = False, stacked: bool = False) -> str:
    """The rung as a fixed-width star meter, optionally with its label.

    `stacked` puts the word under the stars (the feed's own column, where the
    cell is narrow and tall); otherwise it sits beside them.
    """
    n = max(0, min(MAX_STARS, int(stars or 0)))
    label = STAR_LABELS.get(n, "")
    read = f"{n} из {MAX_STARS}" + (f" — {label}" if label else "")
    lab = f"<span class='st-lab'>{html.escape(label)}</span>" if (word and label) else ""
    cls = f"stars s{n}" + (" stack" if stacked else "")
    return (
        f"<span class='{cls}' title='{read}'>"
        f"<span class='st' aria-hidden='true'><span class='trk'>{'★' * MAX_STARS}</span>"
        f"<span class='fill'>{'★' * n}</span></span>{lab}"
        f"<span class='sr'>{read}</span></span>"
    )


def _stars_cell(stars) -> str:
    """First column of every table that lists signals."""
    return f"<td class='c-stars'>{_star_mark(stars, word=True, stacked=True)}</td>"


def _rung_of(label: str) -> int:
    """Rung number out of a stored bucket key like "3★"."""
    m = re.match(r"\s*(\d+)", str(label))
    return int(m.group(1)) if m else 0


# --------------------------------------------------------------------------
# feed
# --------------------------------------------------------------------------

# Scores of matches currently in play, refreshed once per render. Module-level
# rather than threaded through five row builders: every table on the page wants
# the same map, and it is read-only for the duration of a render.
_LIVE = {}

# Final scores of finished matches. Separate from _LIVE because a final
# score never goes stale, while a live one does.
_FINAL = {}


def _live_badge(fixture_id, live=None) -> str:
    """Score of a match in progress.

    "матч идёт" on its own tells you the position is no longer actionable but
    nothing about how it is going, which is the one thing worth knowing once
    the whistle has blown. Only rendered from a score refreshed in the last
    hour and a half (see storage.live_scores_map) -- a stale number here would
    be worse than none.
    """
    row = (live if live is not None else _LIVE).get(fixture_id)
    if not row or row["home_score"] is None or row["away_score"] is None:
        return ""
    hs, as_ = row["home_score"], row["away_score"]
    fmt = lambda v: f"{v:.0f}" if float(v).is_integer() else f"{v:g}"  # noqa: E731
    cls = "score done" if row["completed"] else "score"
    label = "финал" if row["completed"] else "счёт"
    return (f"<span class='{cls}' title='обновлено {_fmt_start(row['updated_at'])} UTC'>"
            f"{label} {fmt(hs)}:{fmt(as_)}</span>")


def _countdown(start_iso, fixture_id=None) -> str:
    """A live 'until kick-off' badge.

    Rendered server-side with a sensible value AND given the raw timestamp so
    the script can keep counting -- the page is a static file that may be
    minutes old, and a frozen "через 2 ч" on a match starting in 40 minutes is
    worse than no timer at all. If the script never runs, the server-rendered
    text is still correct as of publication.
    """
    dt = _parse_iso(start_iso)
    if not dt:
        return ""
    left = (dt - datetime.now(timezone.utc)).total_seconds()
    if left > 0:
        return (f"<span class='cd-to' data-start='{dt.isoformat()}'>"
                f"{_left_words(left)}</span>")

    # Past kick-off is not the same as "in play", and the page used to claim
    # it was -- a match that finished yesterday still read "матч идёт" until
    # the results pass got round to grading it, which could be hours. Now the
    # badge only says that while the match can plausibly still be running:
    # either the score feed confirms it is unfinished, or not enough time has
    # passed for any sport to have ended.
    row = _LIVE.get(fixture_id) if fixture_id else None
    if row is not None and row["completed"]:
        return "<span class='cd-to done' data-start=''>матч завершён</span>"
    if row is None and -left > MATCH_MAX_DURATION_HOURS * 3600:
        return "<span class='cd-to done' data-start=''>матч завершён · ждём результат</span>"
    return "<span class='cd-to live' data-start=''>матч идёт</span>"


def _left_words(seconds: float) -> str:
    mins = int(seconds // 60)
    if mins < 60:
        return f"через {mins} мин"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f"через {hours} ч {mins:02d} мин"
    days, hours = divmod(hours, 24)
    return f"через {days} {_plural(days, 'день', 'дня', 'дней')} {hours} ч"


def _detect_line() -> str:
    """One line saying how much of the market we could actually MEASURE.

    The funnel below explains what happened to movements once they were found.
    It has nothing to say when the fault is upstream -- and on 2026-08-09 it
    was: a scheduling bug meant most lines had no price to be compared against,
    every counter downstream read zero, and the page reported a calm market for
    a full day. This is the number that makes that visible instead.
    """
    try:
        d = json.loads(storage.get_meta("detect_diag") or "{}")
    except (ValueError, TypeError):
        return ""
    lines = d.get("lines") or 0
    if not lines:
        return ""
    blind = d.get("no_history") or 0
    pct = blind / lines * 100
    tone = "warn" if pct >= 50 else ""
    tail = ""
    if pct >= 50:
        tail = (" — столько линий мы видим впервые или слишком давно, "
                "сравнивать не с чем. Пока эта доля высокая, сигналов "
                "будет мало не потому, что рынок спокоен.")
    return (f"<p class='detect {tone}'>В последнем срезе сверено "
            f"<b>{lines - blind}</b> из <b>{lines}</b> линий "
            f"(сравнение с ценой не старше {d.get('max_age', '?')} мин){tail}</p>"
            + _markets_line(d))


def _markets_line(d: dict) -> str:
    """How many lines each bought market actually returned.

    Added 20.08.2026 with the purchase of spreads and totals. The provider's
    documentation says those two are "mainly available for US sports and
    bookmakers", which is an adjective, not a number -- and we are now paying
    three times as much per league on the strength of it. This line turns the
    adjective into a count, on the page, next to everything else that can be
    recounted.

    It also marks which markets may currently produce a signal, because
    buying a market and acting on one are deliberately different things here
    and a reader has no other way to tell.
    """
    by = d.get("by_market") or {}
    if len(by) <= 1:
        return ""
    acting = set((d.get("by_market_signal") or {}).keys())
    names = {"h2h": "исход", "spreads": "фора", "totals": "тотал",
             "outrights": "аутрайт"}
    parts = []
    for mk, n in sorted(by.items(), key=lambda kv: -kv[1]):
        label = names.get(mk, mk)
        mark = "" if mk in acting else " <i>(только собираем)</i>"
        parts.append(f"<b>{_num(n)}</b> {label}{mark}")
    return ("<p class='detect'>Из них по рынкам: " + ", ".join(parts)
            + ". Рынки без пометки дают сигналы; помеченные мы пока только "
              "копим, чтобы посмотреть на них данными, а не на слово.</p>")


def _funnel_block(f: dict, span: str) -> str:
    """Where the day's market moves went.

    A header that says "22 движения" next to "1 сигнал" looks broken. It is
    not -- the other 21 were each rejected by a specific rule, and every one
    of them is in exactly one bucket below. Showing the breakdown turns a
    number that invites suspicion into a number that explains itself, and it
    is also how we decide which filter to loosen when we want more volume.
    """
    total = (f or {}).get("big_drop") or 0
    if not total:
        return ""
    parts = [
        ("двинулась только одна контора — это не рынок", f.get("thin_market") or 0),
        ("просело уже у всех — брать негде", f.get("all_books_moved") or 0),
        ("вход вернул меньше половины падения", f.get("entry_too_low") or 0),
        (f"меньше {MIN_SIGNAL_STARS} звёзд — мало контор подтвердило",
         f.get("low_stars") or 0),
        (f"коэффициент вне полосы {MIN_SIGNAL_PRICE:g}–{MAX_SIGNAL_PRICE:g}",
         f.get("off_band") or 0),
        (f"матч дальше {MAX_LEAD_HOURS:g} ч — цена ещё десять раз изменится",
         f.get("too_far") or 0),
    ]
    rows = "".join(
        f"<li><span>не ставили: {label}</span><b>{n}</b></li>" for label, n in parts if n
    )
    return (
        _detect_line()
        + f"<div class='funnel'><div class='fn-head'>Движения за {span}</div>"
        f"<ul><li class='fn-top'><span>Всего поймали движений</span><b>{total}</b></li>"
        f"{rows}"
        f"<li class='fn-ok'><span>Из них поставили</span><b>{f.get('signals') or 0}</b></li>"
        f"</ul></div>"
    )


def _mini_movements(limit: int = 10) -> str:
    """Movements that have been graded, opened from the "проверено" counter.

    Same idea as the strategy cards: a bare count is not checkable. Here it
    matters more, because these are mostly moves we did NOT bet -- the only
    way to judge whether the thesis holds is to see how they finished.
    """
    rows = [r for r in storage.recent_movements(80) if r["resolved"]][:limit]
    if not rows:
        return "<p class='none'>Проверенных движений пока нет — ждём, пока сыграют матчи.</p>"
    items = []
    for r in rows:
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        caught = f"{r['old_price']:.2f}" if r["old_price"] else "—"
        score = _score_text(r["fixture_id"])
        score_html = f" · <b>{score}</b>" if score else ""
        cls, label = _RESULT_LABEL.get(r["result"], ("pending", "⏳ ждём"))
        pnl = ""
        if r["result"] == "hit" and r["old_price"]:
            pnl = f" · <span class='pnl good'>+${FLAT_STAKE * (r['old_price'] - 1):.0f}</span>"
        elif r["result"] == "miss":
            pnl = f" · <span class='pnl bad'>−${FLAT_STAKE:.0f}</span>"
        items.append(
            f"<li><span class='ms-ev'><b>{html.escape(event)}</b>"
            f"{_sport_badge(r['sport_key'])}"
            f"<small>{html.escape(r['outcome_name'] or '')} · до падения {caught}"
            f"{score_html}{pnl}</small></span><span class='{cls}'>{label}</span></li>"
        )
    return "<ul class='mini'>" + "".join(items) + "</ul>"


def _movements_table(rows) -> str:
    """Every detected move, priced at the coefficient before it fell."""
    if not rows:
        return ("<p class='empty small'>Движений пока не зафиксировано. "
                "Здесь будет каждое падение от порога — и те, что стали сигналом, "
                "и те, где взять цену было уже негде.</p>")
    items = []
    for r in rows:
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        caught = f"{r['old_price']:.2f}" if r["old_price"] else "—"
        newp = f"{r['new_price']:.2f}" if r["new_price"] else "—"
        drop = f"−{abs(r['drop_pct']):.0f}%" if r["drop_pct"] else ""
        if r["resolved"]:
            st = {"hit": "<span class='hit'>✅ зашла</span>",
                  "miss": "<span class='miss'>❌ не зашла</span>"}.get(
                      r["result"], "<span class='pending'>— н/д</span>")
            pnl = ""
            if r["result"] == "hit" and r["old_price"]:
                pnl = f"<small class='pnl good'>+${FLAT_STAKE * (r['old_price'] - 1):,.0f}</small>"
            elif r["result"] == "miss":
                pnl = f"<small class='pnl bad'>−${FLAT_STAKE:,.0f}</small>"
            st += pnl.replace(",", " ")
        else:
            st = ("<span class='pending'>⏳ ждём</span>" + _countdown(r["start_time"], r["fixture_id"])
                  + _live_badge(r["fixture_id"]))
        # Three states, not two. "был вход" used to mean only that a price was
        # still on offer, and readers reasonably took it as "we bet this" --
        # then found the event in no strategy at all. A takeable price and an
        # actual signal are different things: a move with one bookmaker behind
        # it gets refused however good the price looks.
        keys = r.keys() if hasattr(r, "keys") else r
        was_signal = ("was_signal" in keys) and r["was_signal"]
        if was_signal:
            mark = "<span class='tag opt'>поставили</span>"
        elif r["had_entry"]:
            mark = ("<span class='tag warnish' title='Цену ещё давали, но движение "
                    "не подтвердилось — двинулась одна контора'>цена была, "
                    "но не ставили</span>")
        else:
            mark = "<span class='tag agg'>брать было негде</span>"
        items.append(
            f"<tr class='row'>{_stars_cell(r['stars'])}"
            f"<td class='c-ev'><b>{html.escape(event)}</b>"
            f"{_sport_badge(r['sport_key'])}"
            f"<small>{_fmt_start(r['start_time'])} UTC</small></td>"
            f"<td class='c-out'>{html.escape(r['outcome_name'] or '')}"
            f"<div class='tags'>{mark}</div></td>"
            f"<td class='c-move'><span class='new'>{newp}</span>"
            f"<span class='pct'>{drop}</span></td>"
            f"<td class='c-books'>{r['down_count'] or 0}<span class='of'>/{r['books_count'] or 0}</span></td>"
            f"<td class='c-bet'><span class='price'>{caught}</span><small>поймали бы</small></td>"
            f"<td>{st}</td></tr>"
        )
    return ("<div class='feed-wrap'><table class='feed'>"
            "<tr><th></th><th>Событие</th><th>Деньги на</th><th>Стал</th>"
            "<th>Контор</th><th>Коэф. до падения</th><th>Итог</th></tr>"
            + "".join(items) + "</table></div>")


def _movement_stats(m: dict) -> str:
    """The ceiling: what backing every move at the pre-drop price would return.

    Stated as a ceiling on purpose. old_price is frequently a number nobody was
    still offering by the time the move was visible -- that is precisely why
    many of these never became signals. Presenting it as achievable profit
    would be the most flattering lie this page could tell, so the caveat sits
    directly under the figure rather than in a footnote.
    """
    wr = f"{m['win_rate']:.0f}%" if m.get("win_rate") is not None else "—"
    profit = m.get("profit") or 0
    sign = "+" if profit >= 0 else "−"
    cls = "good" if profit > 0 else ("bad" if profit < 0 else "neutral")
    n = m.get("graded_n") or 0
    body = (f"<div class='bank-num'>{sign}${abs(profit):,.0f}</div>"
            f"<div class='bank-sub'>По {n} {_plural(n, 'сыгравшему движению', 'сыгравшим движениям', 'сыгравшим движениям')}, "
            f"флэтом по ${int(m.get('stake') or 0)}. Оборот ${m.get('staked') or 0:,.0f}, "
            f"доходность {m['roi_pct']:+.1f}%.</div>") if n else (
            "<div class='bank-sub'>Ни одно движение ещё не сыграло — считать нечего.</div>")
    return (
        f"<div class='stat-row'>"
        f"<div class='stat'><b>{m.get('total', 0)}</b><span>движений</span></div>"
        f"<div class='stat'><b>{m.get('with_entry', 0)}</b><span>из них поставили</span></div>"
        f"<div class='stat'><button class='stat-btn' type='button' data-open='res-moves'"
        f" aria-expanded='false' aria-controls='res-moves'"
        f" title='Показать проверенные движения'>{m.get('resolved', 0)}</button>"
        f"<span>проверено ▾</span></div>"
        f"<div class='stat'><b>{wr}</b><span>заходимость</span></div>"
        f"<div class='stat'><b>{m.get('total', 0) - (m.get('with_entry') or 0)}</b><span>не ставили</span></div>"
        f"</div>"
        f"<div class='sig-list' id='res-moves' hidden>"
        f"<div class='sig-cap'>Проверенные движения — с итогом и счётом</div>"
        f"{_mini_movements()}</div>"
        f"<div class='bank {cls}'><div class='bank-head'>Если бы мы <b>всегда</b> успевали "
        f"взять коэффициент до падения, по ${int(m.get('stake') or 0)} на движение</div>{body}"
        f"<div class='bank-note'>Это потолок, а не деньги. Цена до падения часто уже никем "
        f"не даётся — именно поэтому такие движения и не стали сигналами. Смысл цифры в другом: "
        f"если она в плюсе, значит логика «деньги зашли — ставим туда же» работает, и вопрос "
        f"только в скорости. Если в минусе — ускоряться бессмысленно.</div></div>"
    ).replace(",", " ")


def _event_row(s: dict) -> str:
    """One compact row per event. Everything needed to act on it -- which side
    money went into, what the price was and is, how broad the move was, and
    where to still take it -- fits on a single line, so the feed stays
    scannable on a phone without horizontal scrolling."""
    bet = s.get("bet") or {}
    stars = s.get("stars", 0)
    has_entry = s.get("has_entry")
    strategy = s.get("strategy") or "aggressive"
    safe = s.get("safe")

    name = f"{html.escape(s.get('home_team') or '?')} — {html.escape(s.get('away_team') or '?')}"
    outcome = html.escape(bet.get("name") or "—")

    # A row can arrive here two ways: straight out of the poll that just ran,
    # or out of the database because it was called earlier today. They look
    # identical otherwise, so the badge is the only thing telling the reader
    # which one is fresh.
    badge = "<span class='chip fresh'>только что</span>" if s.get("fresh") else ""
    # Flagged only at 3+ of the 4 patterns -- see analytics._suspicion. The
    # title carries the actual reasons so it is a claim you can check, not a
    # vibe.
    if (s.get("suspicion") or 0) >= 3:
        why = html.escape(", ".join(s.get("suspicion_reasons") or []))
        badge += f"<span class='chip flag' title='{why}'>🚩 странное движение</span>"
    res = {"hit": "<span class='chip win'>зашла</span>",
           "miss": "<span class='chip lose'>не зашла</span>",
           "n/a": "<span class='chip na'>не проверить</span>"}.get(s.get("result") or "", "")

    if has_entry:
        bet_cell = (f"<td class='c-bet'><span class='price'>{bet['entry_price']:.2f}</span>"
                    f"<small>{html.escape(bet['entry_book'])}</small></td>")
    else:
        bet_cell = "<td class='c-bet'><span class='chip shut'>⛔ закрыт</span></td>"

    # The tag names the optimal line's ACTUAL bet, not a bucket. Since
    # 2026-07-30 that line does not skip a long shot, it enters it softly --
    # so "ОПТИМАЛЬНАЯ" alone would hide which bet is meant.
    tags = [f"<span class='tag agg' title='Прямая победа за "
            f"{(bet.get('entry_price') or 0):.2f}'>АГРЕССИВНАЯ</span>"]
    opt = s.get("optimal")
    if not opt:
        pass
    elif opt["kind"] == "straight":
        tags.append("<span class='tag opt' title='Коэффициент входа не выше "
                    f"{OPTIMAL_MAX_PRICE:g}'>ОПТИМАЛЬНАЯ — та же ставка</span>")
    elif opt.get("price"):
        tags.append(f"<span class='tag opt' title='{html.escape(opt.get('note') or '')}'>"
                    f"ОПТИМАЛЬНАЯ — двойной шанс {opt['price']:.2f}</span>")
    else:
        # The label is truncated to fit the cell, so the price is rendered
        # separately and never lives inside the truncated text -- that is
        # exactly how a 1.60 once displayed as "1.".
        est = opt.get("est_price")
        shown = f" ~{est:.2f}" if est else ""
        tags.append(f"<span class='tag safe' title='{html.escape(opt.get('note') or '')}'>"
                    f"🛡 ОПТИМАЛЬНАЯ — {html.escape(_short(opt['pick'], 44))}{shown}</span>")

    return (
        # Deliberately NOT a .reveal element: the feed is the one thing on the
        # page that must render even if the script never runs, so it is never
        # hidden behind an animation.
        f"<tr class='row' data-stars='{stars}' data-open='{1 if has_entry else 0}' "
        f"data-strat='{strategy}' data-fresh='{1 if s.get('fresh') else 0}'>"
        f"{_stars_cell(stars)}"
        f"<td class='c-ev'><b>{name}</b>{_sport_badge(s.get('sport_key'))}"
        f"<small>{_fmt_start(s.get('start_time'))} UTC</small>"
        f"{_countdown(s.get('start_time'), s.get('fixture_id'))}{_live_badge(s.get('fixture_id'))}{badge}{res}</td>"
        f"<td class='c-out'>{outcome}<div class='tags'>{''.join(tags)}</div></td>"
        f"<td class='c-move'><span class='old'>{bet['old_price']:.2f}</span>"
        f"<span class='arr'>→</span><span class='new'>{bet['new_price']:.2f}</span>"
        f"<span class='pct'>−{abs(bet['drop_pct']):.0f}%</span></td>"
        f"<td class='c-books'>{bet['down_count']}<span class='of'>/{bet['books_count']}</span></td>"
        f"{bet_cell}</tr>"
    )


def _row_to_summary(r) -> dict:
    """Turn a stored tracked_alerts row back into the shape _event_row eats.

    Cheaper and safer than a second row renderer: one template, one set of
    columns, so the live rows and the ones read back from the database can
    never drift apart visually.
    """
    old_p, new_p = r["old_price"], r["new_price"]
    drop = ((old_p - new_p) / old_p * 100) if (old_p and new_p) else 0.0
    opt = None
    if r["opt_kind"]:
        opt = {
            "kind": r["opt_kind"],
            "pick": r["opt_pick"] or "",
            "price": r["opt_price"],
            "est_price": r["opt_est_price"],
            "note": r["opt_pick"] or "",
        }
    return {
        "fixture_id": r["fixture_id"],
        "sport_key": r["sport_key"],
        "home_team": r["home_team"],
        "away_team": r["away_team"],
        "start_time": r["start_time"],
        "stars": r["stars"] or 0,
        "strategy": r["strategy"] or "aggressive",
        "has_entry": bool(r["entry_price"]),
        "result": r["result"] if r["resolved"] else None,
        "optimal": opt,
        "bet": {
            "name": r["outcome_name"], "side": r["outcome_id"],
            "old_price": old_p or 0.0, "new_price": new_p or 0.0, "drop_pct": drop,
            "down_count": r["down_count"] or 0, "books_count": r["books_count"] or 0,
            "entry_price": r["entry_price"], "entry_book": r["entry_book"] or "",
        },
    }


def _merge_feed(summaries: list, recent_rows, limit: int = 120) -> list:
    """Current poll first, then everything else called in the last 24 hours.

    Deduplicated on (event, side): a move that is still moving appears in both
    sources, and the live copy wins because it carries the newer prices.
    """
    shown, seen = [], set()
    for s in summaries:
        if not s.get("bet"):
            continue
        key = (s.get("fixture_id"), (s.get("bet") or {}).get("side"))
        if key in seen:
            continue
        seen.add(key)
        s = dict(s)
        s["fresh"] = True
        shown.append(s)
    for r in recent_rows or []:
        key = (r["fixture_id"], r["outcome_id"])
        if key in seen:
            continue
        seen.add(key)
        shown.append(_row_to_summary(r))
    return shown[:limit]


def _summaries_html(summaries: list, recent_rows=None, limit: int = 120) -> str:
    shown = _merge_feed(summaries, recent_rows, limit)
    if not shown:
        return ("<div class='empty'><div class='empty-ico'>◎</div>"
                "<p><b>За сутки ни одного сигнала.</b></p>"
                "<p>Ни одно падение от "
                f"{SPIKE_THRESHOLD_PCT * 100:.0f}% не дошло до входа за последние 24 часа. "
                "Пустая страница здесь — это честный ответ, а не поломка: "
                "мы не придумываем сигналы, чтобы заполнить место.</p></div>")

    nfresh = sum(1 for s in shown if s.get("fresh"))
    # One rung per published confidence level. Before 2026-08-15 only three
    # stars was ever published, so "★★★" meant "every signal" and the chips
    # sorted nothing. Now each rung is a real, separately-scored population.
    n4 = sum(1 for s in shown if s["stars"] >= 4)
    n3 = sum(1 for s in shown if s["stars"] == 3)
    n2 = sum(1 for s in shown if s["stars"] == 2)
    nopen = sum(1 for s in shown if s.get("has_entry"))
    nopt = sum(1 for s in shown if s.get("strategy") == "optimal")

    # The three rungs are one question ("насколько уверенно") and the rest are
    # another, so they are fenced off into their own group instead of sitting
    # in one undifferentiated row of seven identical pills. Each rung chip
    # carries the same meter and the same ink as the rows it filters to, so
    # the chip and the column teach each other.
    def rung_chip(rung: int, n: int) -> str:
        return (f"<button class='f fs s{rung}' data-f='{rung}' "
                f"title='Просело у {MOVED_FOR[rung]} контор и больше'>"
                f"<span class='st' aria-hidden='true'>"
                f"<span class='trk'>{'★' * MAX_STARS}</span>"
                f"<span class='fill'>{'★' * rung}</span></span>"
                f"{STAR_LABELS[rung]}<span class='n'>{n}</span></button>")

    # The rungs live inside one recessed group rather than being fenced off by
    # separators: a divider that lands at the end of a wrapped line on a phone
    # reads as a stray mark, while a group moves and wraps as a single unit at
    # any width and states outright that these three are one choice.
    filters = (
        "<div class='toolbar'>"
        f"<button class='f active' data-f='all'>Все за сутки<span class='n'>{len(shown)}</span></button>"
        f"<button class='f' data-f='fresh'>Только что<span class='n'>{nfresh}</span></button>"
        "<span class='f-group' role='group' aria-label='Ступень доверия'>"
        + rung_chip(4, n4) + rung_chip(3, n3) + rung_chip(2, n2)
        + "</span>"
        + f"<button class='f' data-f='opt'>Оптимальная<span class='n'>{nopt}</span></button>"
        f"<button class='f' data-f='open'>Есть вход<span class='n'>{nopen}</span></button>"
        "</div>"
    )

    head = ("<tr><th><span class='sr'>Звёзды</span></th><th>Событие</th><th>Деньги зашли на</th>"
            "<th>Был → стал</th><th>Контор</th><th>Ставим</th></tr>")
    body = "".join(_event_row(s) for s in shown)
    table = (f"<div class='feed-wrap'><table class='feed' id='feedtable'>"
             f"{head}{body}</table></div>")
    empty = "<p class='norows' id='norows' hidden>Под этот фильтр ничего не подошло.</p>"
    return filters + table + empty


def _ticker(summaries: list) -> str:
    """Marquee across the top. Deliberately only rendered when it has real
    movements to carry -- a decorative ticker looping fake rows is exactly the
    kind of theatre this audience is trained to spot."""
    items = [s for s in summaries if s.get("bet")][:14]
    if not items:
        return ""
    chunks = []
    for s in items:
        bet = s["bet"]
        who = html.escape(bet.get("name") or "")
        chunks.append(
            f"<span class='ti'><i class='dot'></i>{who} "
            f"<b>{bet['old_price']:.2f}→{bet['new_price']:.2f}</b> "
            f"<u>−{abs(bet['drop_pct']):.0f}%</u></span>"
        )
    track = "".join(chunks)
    # Duplicated track is what makes a pure-CSS marquee loop seamlessly.
    return (f"<div class='ticker' aria-hidden='true'><div class='tr'>"
            f"<div class='tt'>{track}</div><div class='tt'>{track}</div></div></div>")


# --------------------------------------------------------------------------
# statistics
# --------------------------------------------------------------------------

def _active_signals(rows) -> str:
    """Signals whose match hasn't started yet.

    Added 2026-07-30 because the page genuinely misled: the feed above shows
    only what moved in the LAST poll -- a three-minute window that is empty
    most of the time -- so the site looked dead while several logged bets were
    sitting there waiting to play. Those are two different questions ("что
    шевельнулось только что" vs "на что мы сейчас стоим") and they now get two
    different blocks.
    """
    if not rows:
        return ("<p class='empty small'>Открытых ставок нет — все сигналы уже сыграли "
                "либо новых пока не было.</p>")
    items = []
    for r in rows:
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        entry = f"{r['entry_price']:.2f}" if r["entry_price"] else "—"
        old_p = f"{r['old_price']:.2f}" if r["old_price"] else "—"
        new_p = f"{r['new_price']:.2f}" if r["new_price"] else "—"
        # The two strategies place DIFFERENT bets here, so the row shows both.
        # Printing one price under an "ОПТИМАЛЬНАЯ" label was actively
        # misleading: a 3.45 pick that the optimal line actually enters as a
        # 1.68 double chance read as if we were recommending 3.35 twice.
        opt_cell = "<span class='chip shut'>— не входим</span>"
        kind = r["opt_kind"]
        if kind == "straight":
            opt_cell = (f"<span class='price'>{entry}</span>"
                        f"<small>та же ставка</small>")
        elif kind and r["opt_price"]:
            opt_cell = (f"<span class='price'>{r['opt_price']:.2f}</span>"
                        f"<small>{html.escape(_short(str(r['opt_pick']), 34))}</small>")
        elif kind:
            # "~" is load-bearing: this price is derived from the moneyline,
            # not quoted by a bookmaker, and the reader has to be able to tell.
            txt, est = _opt_price_text(r)
            cls = "price est" if est else "price"
            opt_cell = (f"<span class='{cls}'>{txt}</span>"
                        f"<small>{html.escape(_short(str(r['opt_pick']), 34))}</small>")
        items.append(
            f"<tr class='row'>{_stars_cell(r['stars'])}"
            f"<td class='c-ev'><b>{html.escape(event)}</b>"
            f"{_sport_badge(r['sport_key'])}"
            f"<small>старт {_fmt_start(r['start_time'])} UTC</small>"
            f"{_countdown(r['start_time'], r['fixture_id'])}{_live_badge(r['fixture_id'])}</td>"
            f"<td class='c-out'>{html.escape(r['outcome_name'] or '')}</td>"
            f"<td class='c-move'><span class='old'>{old_p}</span><span class='arr'>→</span>"
            f"<span class='new'>{new_p}</span></td>"
            f"<td class='c-bet'><span class='price'>{entry}</span>"
            f"<small>{html.escape(r['entry_book'] or '')}</small></td>"
            f"<td class='c-bet'>{opt_cell}</td></tr>"
        )
    return ("<div class='feed-wrap'><table class='feed'>"
            "<tr><th></th><th>Событие</th><th>Деньги на</th><th>Был → стал</th>"
            "<th>Агрессивная</th><th>Оптимальная</th></tr>" + "".join(items) + "</table></div>")


def _bankroll_block(stats: dict) -> str:
    """Plain-language flat-stake result. Percentages are easy to misread; a
    balance in dollars is not."""
    stake = int(stats.get("stake") or 0)
    n = stats.get("graded_n") or 0
    if not n:
        return (f"<div class='bank neutral'><div class='bank-head'>Если ставить "
                f"по ${stake} на каждый сигнал</div>"
                f"<div class='bank-sub'>Считать пока нечего — ни один матч с сигналом "
                f"ещё не сыгран. Как только сыграет, здесь появится баланс.</div></div>")

    profit, staked, roi = stats["profit"], stats["staked"], stats["roi_pct"]
    sign = "+" if profit >= 0 else "−"
    cls = "good" if profit > 0 else ("bad" if profit < 0 else "neutral")
    word = "заработали" if profit > 0 else ("потеряли" if profit < 0 else "вышли в ноль")
    return (
        f"<div class='bank {cls}'>"
        f"<div class='bank-head'>Если бы вы ставили по <b>${stake}</b> на каждый сигнал</div>"
        f"<div class='bank-num' data-count='{profit:.0f}' data-prefix='{sign}$'>{sign}${abs(profit):,.0f}</div>"
        f"<div class='bank-sub'>{_played_note(stats, n)}"
        f"вы бы {word} <b>{sign}${abs(profit):,.0f}</b>. "
        f"Оборот ${staked:,.0f}, доходность {roi:+.1f}%.</div>"
        f"<div class='bank-note'>Считается по уже сыгравшим сигналам и по той цене, "
        f"которую мы называли. Это не обещание будущего результата.{_unpriced_note(stats)}</div>"
        f"</div>"
    ).replace(",", " ")


def _played_note(stats: dict, priced: int) -> str:
    """Opening of the bank sentence, naming BOTH counts when they differ.

    Reported 2026-08-10: the optimal card said "За 5 сыгравших ставок" while
    the same page showed 10 played, and the user was right to call it a bug --
    both strategies bet the same events, so the number of matches played cannot
    differ. What differs is how many of them carry a price we could pay out on:
    a handicap is settleable from the score but was never bought. Saying "из 10
    сыгравших 5 с известной ценой" states the gap instead of quietly reporting
    the smaller number as if it were the whole book.
    """
    resolved = stats.get("resolved") or priced
    played = _plural(resolved, "сыгравшую ставку", "сыгравшие ставки", "сыгравших ставок")
    if resolved and priced and resolved != priced:
        return (f"За {resolved} {played}, из которых {priced} "
                f"{_plural(priced, 'с известной ценой', 'с известными ценами', 'с известными ценами')}, ")
    return f"За {resolved} {played} "


def _unpriced_note(stats: dict) -> str:
    """Says out loud how many settled bets are missing from the money.

    A handicap we never bought a price for is settleable from the score but
    not payable, so it counts in the win rate and not in the bank. Without
    saying so the two numbers silently disagree -- which is how "+$940 from
    one bet" happened: a set handicap worth about 1.60 got paid at the 5.70
    moneyline because the price fell back to the wrong column.
    """
    if stats.get("strategy") != "optimal":
        return ""
    gap = (stats.get("resolved") or 0) - (stats.get("graded_n") or 0)
    if gap <= 0:
        return ""
    return (f" Ещё {gap} {_plural(gap, 'ставка сыграла', 'ставки сыграли', 'ставок сыграли')}"
            f" по форе — они считаются в заходимости, но не в деньгах: цену форы мы не"
            f" выкупаем, а выдумывать её нечестно.")


_EMBEDDED_PRICE = re.compile(r"\s*≈\s*\d+[.,]?\d*")


def _short(text: str, limit: int) -> str:
    """Truncate a label without ever cutting through a number.

    The bug this exists for: a pick read "Terence Atmane — фора по сетам +1.5
    ≈ 1.60 (взять хотя бы один сет)", the table cut it at 40 characters, and
    the coefficient rendered as "1." -- a wrong price is worse than no price.
    Any embedded "≈ N.NN" is stripped first (rows logged before the price
    moved out of this string still carry one), then the cut lands on a word
    boundary.
    """
    text = _EMBEDDED_PRICE.sub("", text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut:
        cut = cut[:cut.rindex(" ")]
    return cut.rstrip(" ,.—-") + "…"


def _opt_price_text(row):
    """The price the optimal line took, as a string, never "по линии".

    Order matters: a real quoted price first, then the estimate computed from
    both sides of the market, then -- for rows logged before that estimate
    existed -- one derived from our own price with an assumed margin. Anything
    estimated carries a "~" so it can never be mistaken for a price we took.
    """
    if row["opt_price"]:
        return f"{row['opt_price']:.2f}", False
    est = row["opt_est_price"] if "opt_est_price" in row.keys() else None
    if not est and row["opt_kind"] == "set_handicap" and row["entry_price"]:
        est = analytics.set_handicap_price_from_one(row["entry_price"], row["sport_key"]
                                                    if "sport_key" in row.keys() else "")
    return (f"~{est:.2f}", True) if est else ("по линии", True)


def _mini_signals(rows, strategy: str = "aggressive") -> str:
    """The last few signals in a bucket, shown when the count is clicked.

    A total on its own is not checkable. Being able to open it and see the
    actual events behind it -- with the price we named and what happened --
    is what turns a number into something a reader can argue with.
    """
    if not rows:
        return "<p class='none'>В этой стратегии сигналов пока нет.</p>"
    items = []
    for r in rows:
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        # Each card must quote the price ITS OWN strategy took. The optimal
        # card showing the aggressive entry is how "коэффициент 3.45 в
        # оптимальной за 3.35" ended up on the page.
        if strategy == "optimal" and r["opt_kind"] and r["opt_kind"] != "straight":
            entry = _opt_price_text(r)[0]
            pick = str(r["opt_pick"] or "")
        else:
            entry = f"{r['entry_price']:.2f}" if r["entry_price"] else "—"
            pick = r["outcome_name"] or ""
        old_p = f"{r['old_price']:.2f}" if r["old_price"] else "—"
        new_p = f"{r['new_price']:.2f}" if r["new_price"] else "—"
        if r["resolved"]:
            st = {"hit": "<span class='hit'>✅ зашла</span>",
                  "miss": "<span class='miss'>❌ не зашла</span>"}.get(
                      r["result"], "<span class='pending'>— н/д</span>")
        else:
            st = "<span class='pending'>⏳ ждём</span>"
        items.append(
            f"<li><span class='ms-ev'><b>{html.escape(event)}</b>"
            f"<small>{html.escape(pick)} · {old_p} → {new_p} · "
            f"взяли <b>{entry}</b></small>{_countdown(r['start_time'], r['fixture_id'])}"
            f"{_live_badge(r['fixture_id'])}</span>{st}</li>"
        )
    return "<ul class='mini'>" + "".join(items) + "</ul>"


def _unverifiable_note(stats: dict) -> str:
    """Say out loud how many of this strategy's bets can never be settled.

    Handicaps are a real instruction the analyst can act on, but we buy neither
    their line nor their price, so there is no way to check afterwards whether
    one won. They are therefore counted as signals and excluded from the win
    rate -- and that exclusion is stated on the card rather than left for
    someone to discover by adding the numbers up.
    """
    n = stats.get("unverifiable") or 0
    if not n:
        return ""
    return (f"<p class='note-warn'>Из них {n} "
            f"{_plural(n, 'вход', 'входа', 'входов')} через фору — "
            f"мы не выкупаем этот рынок, поэтому проверить их по счёту "
            f"нельзя, и в заходимость они не попадают.</p>")


def _clv_class(avg_clv) -> str:
    """Green above zero, magenta below. A negative CLV is not a small blemish
    -- it says the market moved AGAINST the price we took, i.e. we were late or
    reading noise -- so it should not be printed in the same ink as a win."""
    if avg_clv is None:
        return "clv-flat"
    return "clv-good" if avg_clv > 0 else ("clv-bad" if avg_clv < 0 else "clv-flat")


def _breakdown_block(bd: dict) -> str:
    """Where the record actually differs: discipline, size of drop, confidence.

    Kept deliberately plain and always showing n, because every one of these
    buckets is currently far too small to carry a conclusion. The point is not
    to declare a winner -- it is to make the differences visible at all, since
    on a single average they cancel out and disappear.
    """
    if not bd or not bd.get("graded"):
        return ""

    def table(title, data, note, *, stars=False, key=False, intro="", flag=""):
        if not data:
            return ""
        rows = []
        for label, v in data.items():
            clv = f"{v['clv']:+.1f}%" if v["clv"] is not None else "—"
            wr = f"{v['win_rate']:.0f}%" if v["win_rate"] is not None else "—"
            money = f"{v['profit']:+,.0f}$".replace(",", " ")
            cls = "pos" if v["profit"] > 0 else ("neg" if v["profit"] < 0 else "")
            # storage keys the rungs as "2★"/"3★"/"4★" -- plain text that says
            # nothing about which rung is which. Rendered through the same mark
            # as the feed, a reader can match a row here to the rows up there
            # without decoding anything.
            head = _star_mark(_rung_of(label), word=True) if stars else html.escape(label)
            rows.append(
                f"<tr><td>{head}</td><td class='num'>{v['n']}</td>"
                f"<td class='num'>{wr}</td>"
                f"<td class='num {_clv_class(v['clv'] / 100 if v['clv'] is not None else None)}'>{clv}</td>"
                f"<td class='num {cls}'>{money}</td></tr>"
            )
        return (f"<div class='bd-card{' key' if key else ''}'>"
                f"<h4>{title}{flag}</h4>{intro}"
                # Every table on the page inherits min-width:720px, and these
                # three had no scroll box around them -- so on a phone the CLV
                # and money columns were simply clipped off the side. The one
                # table that has to answer "стоит ли брать ★★" cannot be the
                # one a reader on a phone sees only half of.
                f"<div class='bd-scroll'>"
                f"<table class='bd'><thead><tr><th>{note}</th><th class='num'>ставок</th>"
                f"<th class='num'>заход.</th><th class='num'>CLV</th>"
                f"<th class='num'>флэт ${bd['stake']:.0f}</th></tr></thead>"
                f"<tbody>{''.join(rows)}</tbody></table></div></div>")

    # Sorted strongest rung first rather than by sample size, like every other
    # place the ladder is drawn -- a table meant to be read as a ladder should
    # not shuffle its rungs whenever a bucket gets one more bet.
    by_stars = dict(sorted((bd.get("by_stars") or {}).items(),
                           key=lambda kv: -_rung_of(kv[0])))

    return (
        "<div class='breakdown reveal'>"
        "<h3>ГДЕ РЕЗУЛЬТАТ РАЗЛИЧАЕТСЯ</h3>"
        f"<p class='bd-cap'>Те же {bd['graded']} сыгравших ставки, разрезанные три раза — "
        "и первый разрез, по ступеням доверия, теперь главный. Средняя цифра по всему "
        "журналу прячет именно то, что стоит знать: разные ступени, разные виды спорта и "
        "разные по величине падения ведут себя по-разному и в среднем гасят друг друга. "
        "Смотри на колонку «ставок» — почти везде её пока слишком "
        "мало, чтобы делать вывод, и это честная часть картины.</p>"
        + table("По звёздам", by_stars, "ступень", stars=True, key=True,
                flag="<span class='bd-flag'>главный разрез</span>",
                intro="<p class='bd-why'>Единственная таблица, которая отвечает, "
                      "стоит ли вообще брать слабые сигналы. Все ступени считаются "
                      f"<b>одинаковой суммой</b> — по ${bd['stake']:.0f} на сигнал, "
                      "ставка не растёт вместе с уверенностью, — поэтому строки "
                      "сравнимы между собой напрямую: разница в последней колонке "
                      "это разница сигналов, а не разного размера ставок.</p>")
        + table("По дисциплине", bd.get("by_sport"), "спорт")
        + table("По величине падения", bd.get("by_drop"), "падение")
        + "</div>"
    )


def _pm_gap_block() -> str:
    """История зазоров: сколько живут, где были лучшими, как затухают.

    Добавлено 20.08.2026: «собирай историю зазоров, веди по ней детальную
    статистику чтобы мы потом от неё оттолкнулись».

    Две таблицы отвечают на два разных вопроса, и оба до сегодняшнего дня были
    без ответа. КАРТА ВО ВРЕМЕНИ говорит, за сколько часов до старта площадка
    отстаёт сильнее всего — то есть когда вообще имеет смысл входить. ЖИЗНЬ
    КАЖДОГО ЗАЗОРА говорит, сколько минут у нас есть на решение — а это уже не
    про ставки, а про то, каким должен быть бот: быстрым или спокойным.
    """
    g = storage.pm_gap_summary()
    prof = [b for b in g.get("profile", []) if b.get("n")]
    if not prof and not g.get("gaps"):
        return ("<h3 class='pm-h3'>История зазоров</h3>"
                "<p class='lead small'>Журнал только начал наполняться. Каждый "
                "открытый сигнал опрашивается на Polymarket снова и снова до "
                "стартового свистка, и каждый взгляд ложится строкой — поэтому "
                "через сутки здесь появится то, чего нельзя посмотреть нигде "
                "больше: сколько живёт зазор и за сколько часов до матча эта "
                "площадка отстаёт сильнее всего.</p>")

    out = ["<h3 class='pm-h3'>История зазоров</h3>"]
    if prof:
        rows = []
        for b in prof:
            edge = f"+{b['avg_edge']:.1f}%" if b.get("avg_edge") is not None else "—"
            rows.append(
                f"<tr><td>{html.escape(b['label'])}</td>"
                f"<td class='num'>{b.get('events', b['n'])}</td>"
                f"<td class='num dim'>{b.get('looks', '—')}</td>"
                f"<td class='num'>{b['median_lag']:.2f}</td>"
                f"<td class='num'>{b['share_behind']:.0f}%</td>"
                f"<td class='num'>{b['take_pct']:.0f}%</td>"
                f"<td class='num'>{edge}</td></tr>")
        out.append(
            "<p class='lead small'>Карта эджа во времени. Считать надо по "
            "<b>событиям</b>, а не по взглядам: на каждый открытый сигнал мы "
            "смотрим заново каждые пять минут, поэтому сотня взглядов может "
            "оказаться двумя событиями, посчитанными по полсотни раз. Колонка "
            "взглядов оставлена только чтобы это было видно. "
            "<b>Отставание</b> — "
            "медиана по всем взглядам в этой корзине: единица значит, что "
            "площадка стоит на цене до движения, ноль — что отыграла всё. "
            "Если отставание устойчиво выше вдали от матча, входить надо рано "
            "и ждать бессмысленно; если наоборот — площадка просыпается "
            "поздно, и лучший вход ещё впереди.</p>"
            "<div class='bd-scroll'><table class='bd'><thead><tr>"
            "<th>до старта</th><th class='num'>событий</th>"
            "<th class='num'>взглядов</th>"
            "<th class='num'>отставание</th><th class='num'>отстаёт ≥0.5</th>"
            "<th class='num'>проходит вход</th><th class='num'>средний зазор</th>"
            f"</tr></thead><tbody>{''.join(rows)}</tbody></table></div>")

    if g.get("gaps"):
        life = g.get("median_life_min")
        life_s = ("—" if life is None else
                  (f"{life:.0f} мин" if life < 120 else f"{life/60:.1f} ч"))
        lead = g.get("median_best_lead_h")
        out.append(
            "<p class='lead small' style='margin-top:14px'>"
            f"Зазоров в истории: <b>{g['gaps']}</b>. "
            f"Медианная жизнь одного: <b>{life_s}</b> — столько времени есть у "
            f"бота на решение. Лучшая цена в среднем встречалась за "
            f"<b>{('—' if lead is None else format(lead, '.0f') + ' ч')}</b> до "
            f"старта. Закрылись до матча сами: <b>{g['closed_before_start']}</b> "
            f"из {g['gaps']} — остальные дожили до свистка. Полный размер "
            f"влезал в <b>{g['full_size']}</b>. По ногам: прямая "
            f"{g['by_leg']['aggressive']}, двойной шанс {g['by_leg']['optimal']}."
            "</p>")
    return "".join(out)


def _pm_rule_note() -> str:
    """Почему порог именно такой. Одно предложение, но без него правило голое."""
    if POLYMARKET_MIN_EDGE_PCT <= 0:
        return ("Хуже — не берём. Ровно вровень — берём: у букмекера выигрышный "
                "счёт режут лимитами и закрывают, а здесь ту же цену можно взять "
                "в размере и повторить завтра, и это само по себе перевес.")
    return "Ровно вровень или хуже — сделки нет."


def _pm_rule_phrase() -> str:
    """Как назвать правило входа словами, чтобы это была правда при любом пороге.

    При пороге 5% фраза «минимум на 5% выше» читалась естественно. При пороге
    ноль та же шаблонная фраза выдала бы «минимум на 0% выше» — формально
    верно, на слух бессмыслица, и на странице, которая живёт тем, что её можно
    пересчитать, такое читается как недосмотр. Так что порог называется тем
    словом, которым он на самом деле является.
    """
    if POLYMARKET_MIN_EDGE_PCT <= 0:
        return "не хуже лучшей цены"
    return f"минимум на {POLYMARKET_MIN_EDGE_PCT:g}% выше лучшей цены"


def _pm_section() -> str:
    """Polymarket на витрине: покрытие, живые зазоры, звёзды, результаты.

    Добавлено 20.08.2026. К этому моменту весь конвейер уже искал зазоры и
    писал их в журнал, но на сайте этого не было видно вообще — а Polymarket
    стал основой продукта, а не приложением к нему. Страница, которая не
    показывает главного, врёт молчанием.

    Порядок разделов не декоративный. Сначала ПОКРЫТИЕ, потому что если наших
    событий там нет, всё остальное бессмысленно. Потом ЖИВЫЕ ЗАЗОРЫ — то, на
    что можно нажать прямо сейчас. Потом ЗВЁЗДЫ, потому что читателю надо
    объяснить, чем оценка сделки отличается от оценки движения. И только потом
    РЕЗУЛЬТАТЫ: они появляются последними и в реальности тоже приходят
    последними.
    """
    st = storage.pm_stats()
    live = storage.pm_live_feed()
    res = storage.pm_results()
    cov = storage.pm_coverage_by_sport()

    def n(v, suf=""):
        return f"{v}{suf}" if v is not None else "—"

    kpis = (
        "<section class='kpis'>"
        f"<div class='kpi cy reveal'><b>{st['signals']}</b><span>"
        f"{_plural(st['signals'], 'сигнал проверен', 'сигнала проверено', 'сигналов проверено')}</span></div>"
        f"<div class='kpi reveal'><b>{st['matched']}</b><span>нашлись на Polymarket</span></div>"
        f"<div class='kpi reveal'><b>{n(st['match_pct'], '%')}</b><span>покрытие</span></div>"
        f"<div class='kpi lime reveal'><b>{st['opportunities']}</b><span>зазоров найдено</span></div>"
        f"<div class='kpi mag reveal'><b>{n(st['avg_edge_pct'], '%')}</b><span>средний зазор</span></div>"
        f"<div class='kpi reveal'><b>{st['looks']}</b><span>снятий стакана</span></div>"
        "</section>"
    )

    if live:
        rows = []
        for r in live:
            leg = "прямая" if r["leg"] == "aggressive" else "двойной шанс"
            size = f"${r['exec_stake_usd']:,.0f}".replace(",", " ")
            full = "" if r["fits_target"] else " <i>частично</i>"
            rows.append(
                f"<tr>{_stars_cell(_pm_stars_of(r))}"
                f"<td>{html.escape(str(r['outcome_name']))}"
                f"<span class='sub'>{html.escape(str(r['home_team']))} — "
                f"{html.escape(str(r['away_team']))}</span></td>"
                f"<td>{leg}</td>"
                f"<td class='num'>{r['entry_price']:.2f}</td>"
                f"<td class='num'>{r['avg_coef']:.2f}</td>"
                f"<td class='num pos'>+{r['edge_pct']:.1f}%</td>"
                f"<td class='num'>{('—' if r['pm_lag'] is None else format(r['pm_lag'], '.2f'))}</td>"
                f"<td class='num'>{size}{full}</td></tr>")
        live_html = (
            "<div class='bd-scroll'><table class='bd'><thead><tr>"
            "<th>оценка</th><th>ставка</th><th>вариант</th><th class='num'>контора</th>"
            "<th class='num'>Polymarket</th><th class='num'>лучше на</th>"
            "<th class='num'>отставание</th><th class='num'>влезает</th></tr></thead>"
            f"<tbody>{''.join(rows)}</tbody></table></div>")
    else:
        live_html = ("<p class='lead small'>Прямо сейчас открытых зазоров нет. "
                     "Это нормальное состояние, а не поломка: чаще всего "
                     "Polymarket стоит хуже контор, и тогда мы просто не ставим. "
                     "Строка появляется здесь ровно в тот момент, когда цена там "
                     "перестаёт быть хуже нашей.</p>")

    cov_html = ""
    if cov:
        crows = []
        for c in sorted(cov, key=lambda x: -x["total"]):
            pct = c["matched"] / c["total"] * 100 if c["total"] else 0
            crows.append(
                f"<tr><td>{html.escape(_sport_label(c['sport_key']))}</td>"
                f"<td class='num'>{c['total']}</td>"
                f"<td class='num'>{c['matched']}</td>"
                f"<td class='num'>{pct:.0f}%</td>"
                f"<td class='num'>{c['took']}</td></tr>")
        cov_html = (
            "<h3 class='pm-h3'>Где Polymarket нас вообще котирует</h3>"
            "<p class='lead small'>Считается, а не предполагается. Совпадение "
            "события — главная инженерная сложность всей затеи: их названия не "
            "наши. «CF Montreal» против «CF Montréal», «LA Galaxy» против "
            "«Los Angeles Galaxy». Низкий процент по виду спорта означает не "
            "отсутствие рынка, а то, что наш сопоставитель его не нашёл.</p>"
            "<div class='bd-scroll'><table class='bd'><thead><tr><th>вид</th>"
            "<th class='num'>сигналов</th><th class='num'>нашлись</th>"
            "<th class='num'>покрытие</th><th class='num'>с зазором</th>"
            "</tr></thead>"
            f"<tbody>{''.join(crows)}</tbody></table></div>")

    # --- результаты -------------------------------------------------------
    t = res["total"]
    if t["n"]:
        def money(v):
            return f"{v:+,.0f}$".replace(",", " ")
        def line(label, a, note=""):
            if not a["n"]:
                return (f"<tr><td>{html.escape(label)}</td><td class='num'>0</td>"
                        "<td class='num'>—</td><td class='num'>—</td>"
                        "<td class='num'>—</td><td class='num'>—</td></tr>")
            cls = "pos" if a["profit"] > 0 else ("neg" if a["profit"] < 0 else "")
            return (f"<tr><td>{html.escape(label)}{note}</td>"
                    f"<td class='num'>{a['n']}</td>"
                    f"<td class='num'>{a['win_rate']:.0f}%</td>"
                    f"<td class='num'>{a['staked']:,.0f}$".replace(",", " ") + "</td>"
                    f"<td class='num {cls}'>{money(a['profit'])}</td>"
                    f"<td class='num {cls}'>{a['roi']:+.0f}%</td></tr>")
        rrows = [line("Всего по Polymarket", t)]
        rrows.append(line("— прямая ставка", res["aggressive"]))
        rrows.append(line("— двойной шанс", res["optimal"]))
        for k in sorted(res["by_stars"], reverse=True):
            rrows.append(line("★" * k + " " + STAR_LABELS.get(k, ""), res["by_stars"][k]))
        rrows.append(line("из опубликованных сигналов", res["by_source"]["signal"]))
        rrows.append(line("из отклонённых движений", res["by_source"]["movement"]))
        results_html = (
            "<h3 class='pm-h3'>Результаты по Polymarket</h3>"
            "<p class='lead small'>Деньги считаются по <b>фактическому размеру</b>, "
            "который держал стакан, и по фактическому среднему коэффициенту — не по "
            "флэту. На ордербуке размер решает стакан, и сделка на $30 не равна "
            f"сделке на ${POLYMARKET_TARGET_STAKE:.0f}; усреднить их одним флэтом "
            "значило бы придумать доходность, которой не было. "
            f"Ждут матча: {res['pending']}.</p>"
            "<div class='bd-scroll'><table class='bd'><thead><tr><th>разрез</th>"
            "<th class='num'>сделок</th><th class='num'>заход.</th>"
            "<th class='num'>оборот</th><th class='num'>итог</th>"
            "<th class='num'>доходность</th></tr></thead>"
            f"<tbody>{''.join(rrows)}</tbody></table></div>")
    else:
        results_html = (
            "<h3 class='pm-h3'>Результаты по Polymarket</h3>"
            "<p class='lead small'>Ни одной сделки ещё не рассчитано. Строка "
            "появится здесь после первого сыгравшего матча, по которому зазор "
            "был найден. Пока сравнивать нечего, и рисовать таблицу нулей "
            "вместо этого было бы враньём: пустая таблица читается как "
            "измерение, а измерения ещё нет.</p>")

    return f'''
  <h2 id="polymarket"><span class="hash">#</span>Polymarket</h2>
  <p class="lead">Основной инструмент. Логика простая: у букмекера выигрышный счёт
  быстро упирается в лимиты и блокировки, а на Polymarket есть видимая глубина стакана,
  нет банов и нет проблем с выводом. Мы приносим туда цену, которую нашли снаружи —
  и ставим <b>только там, где Polymarket даёт коэффициент {_pm_rule_phrase()}</b>,
  которую можно взять у конторы на тот же исход. {_pm_rule_note()}</p>
  {kpis}
  <p class="kpi-note">Каждый открытый сигнал опрашивается на Polymarket снова и снова
  до самого стартового свистка, а не один раз при срабатывании. Причина замерена: весь
  их открытый список матчей укладывается в трое суток, а наши сигналы приходят за
  26–44 часа до старта, так что в момент сигнала рынка там часто ещё нет. Частота
  подстраивается сама — за сутки до матча раз в пару часов, в последние часы каждый цикл.</p>

  <h3 class="pm-h3">Открытые зазоры прямо сейчас</h3>
  {live_html}

  <h3 class="pm-h3">Звёзды Polymarket — насколько эта площадка отстала</h3>
  <p class="lead small">Оценка здесь отвечает не на вопрос «насколько тут дешевле»,
  а на другой: <b>насколько этот рынок ещё не понял того, что уже поняли все
  остальные</b>. Это разные величины, и совпадают они только случайно.</p>
  <p class="lead small">Считаем две вещи, и обе обязательны. <b>Ширина</b> — у скольких
  контор поехала линия: одна может ошибиться, сорок независимых не могут, это и есть
  вероятность того, что движение настоящее. <b>Отставание</b> — какую долю этого
  движения Polymarket ещё не отыграл: единица значит, что он стоит на цене
  <i>до</i> движения и не шелохнулся, ноль — что отыграл всё и стоит там же, где
  конторы.</p>
  <p class="lead small">Ни одна из двух в одиночку не стоит ничего, и в этом весь смысл.
  Сорок контор поехали, а Polymarket уже переставился — движение настоящее, но денег
  в нём для нас нет. Polymarket стоит колом, а поехали две конторы — он, скорее всего,
  просто прав, и шумим мы, а не он. Деньги живут ровно на пересечении:
  <b>рынок уверен, а эта площадка ещё не знает</b>.</p>
  <ul class="rungs">
    <li>{_star_mark(4, word=True)}— от {MOVED_FOR_4_STARS} контор <b>и</b> отставание от {PM_LAG_4_STARS:g} <b>и</b> влезает полный размер</li>
    <li>{_star_mark(3, word=True)}— от {MOVED_FOR_3_STARS} контор <b>и</b> отставание от {PM_LAG_3_STARS:g}</li>
    <li>{_star_mark(2, word=True)}— цена прошла правило входа, но одно из двух условий не выполнено</li>
  </ul>
  <p class="lead small">Ноль звёзд означает «сделки нет», а не «плохая сделка»: ниже
  порога мы просто не ставим, и смешивать эти два состояния нельзя.</p>

  <h3 class="pm-h3">Две ставки на одно событие</h3>
  <p class="lead small"><b>Прямая</b> — та же ставка, что мы поставили бы у конторы.
  <b>Двойной шанс</b> — «наш побеждает или ничья»: отдельной строкой Polymarket его не
  продаёт, но он там есть, потому что это ровно «соперник не победит», то есть токен
  No на рынке победы соперника. Обе ноги смотрят в одну сторону и проигрывают вместе —
  это не страховка, а два входа по разной цене. В теннисе ничьей нет, поэтому там
  бывает только прямая.</p>

  {cov_html}

  {_pm_gap_block()}

  {results_html}
'''


def _sport_label(key: str) -> str:
    fam = storage._sport_family(key or "")
    return fam if fam else (key or "—")


def _pm_counterfactual_block(cf: dict) -> str:
    """The Polymarket rules we did not adopt, scored on the quote journal.

    Repointed 20.08.2026 from bookmakers to Polymarket, on instruction: "эта
    вся вкладка у нас по сути может работать... очень важную аналитику теперь
    только по полику, по бк уже её можешь не вести".

    It gains a power the bookmaker version never had. There, a rejected rule
    could only be replayed against movements we happened to log once. Here
    every open signal is re-quoted dozens of times between firing and kick-off,
    so the journal holds the whole price path -- which means "what if the
    threshold were 3%", "what if we always waited for the last hour", and "what
    if we only took full size" are answerable from data that already exists,
    not from an experiment somebody has to run with money.

    The two head rows are the ones that matter most and cost nothing to
    compare: taking the first quote that clears the rule, against waiting for
    the best one before kick-off. The gap between them is the price of
    patience, in dollars, measured rather than argued.
    """
    if not cf or not cf.get("pool"):
        return (
            "<div class='breakdown reveal'>"
            "<h3>ЧТО БЫ ДАЛИ ДРУГИЕ ПРАВИЛА НА POLYMARKET</h3>"
            "<p class='bd-cap'>Пока пусто, и это честное состояние, а не ошибка: "
            "журнал котировок Polymarket начал вестись сегодня, а строка "
            "появляется здесь только после того, как матч сыгран и результат "
            "известен. Каждый открытый сигнал опрашивается на Polymarket "
            "заново каждые несколько минут до самого старта, так что данные "
            "копятся сами. Первые строки — после первых расчётов.</p></div>")
    rows = []
    for r in cf["rules"]:
        money = f"{r['profit']:+,.0f}$".replace(",", " ")
        cls = "pos" if r["profit"] > 0 else ("neg" if r["profit"] < 0 else "")
        wr = f"{r['win_rate']:.0f}%" if r["win_rate"] is not None else "—"
        roi = f"{r['roi']:+.0f}%" if r["roi"] is not None else "—"
        staked = f"{r['staked']:,.0f}$".replace(",", " ") if r["staked"] else "—"
        rows.append(
            f"<tr><td>{html.escape(r['label'])}</td><td class='num'>{r['n']}</td>"
            f"<td class='num'>{wr}</td><td class='num'>{staked}</td>"
            f"<td class='num {cls}'>{money}</td><td class='num {cls}'>{roi}</td></tr>"
        )
    return (
        "<div class='breakdown reveal'>"
        "<h3>ЧТО БЫ ДАЛИ ДРУГИЕ ПРАВИЛА НА POLYMARKET</h3>"
        f"<p class='bd-cap'>Считается по журналу котировок Polymarket: "
        f"{cf['looks']} снятий стакана по {cf['pool']} сыгравшим сигналам. "
        f"Деньги — по ФАКТИЧЕСКОМУ размеру, который держал стакан, и по "
        f"фактическому среднему коэффициенту, а не по флэту: на Polymarket "
        f"размер решает стакан, и сделка на $30 не равна сделке на "
        f"${cf['target']:.0f}. Текущее правило — зазор от {cf['min_edge']:g}% "
        f"к лучшей цене в конторе. Первые две строки отвечают на самый дорогой "
        f"вопрос: сколько стоит подождать лучшую цену вместо того, чтобы брать "
        f"первую подходящую.</p>"
        "<div class='bd-scroll'>"
        "<table class='bd'><thead><tr><th>правило</th><th class='num'>сделок</th>"
        "<th class='num'>заход.</th><th class='num'>оборот</th>"
        "<th class='num'>итог</th><th class='num'>доходность</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )


def _counterfactual_block_bookmakers(cf: dict) -> str:
    """The rules we did not adopt, scored on the same live data.

    The honest counterweight to any filter change. When we tightened to three
    stars the alternative stopped producing signals -- and a rule that stops
    producing evidence can never be shown to have been wrong. This block keeps
    scoring it from the movements ledger, so "мы правильно отрезали 2★" stays a
    measurement instead of quietly becoming folklore.
    """
    if not cf or not cf.get("pool"):
        return ""
    rows = []
    for r in cf["rules"]:
        money = f"{r['profit']:+,.0f}$".replace(",", " ")
        cls = "pos" if r["profit"] > 0 else ("neg" if r["profit"] < 0 else "")
        wr = f"{r['win_rate']:.0f}%" if r["win_rate"] is not None else "—"
        roi = f"{r['roi']:+.0f}%" if r["roi"] is not None else "—"
        rows.append(
            f"<tr><td>{html.escape(r['label'])}</td><td class='num'>{r['n']}</td>"
            f"<td class='num'>{wr}</td><td class='num {cls}'>{money}</td>"
            f"<td class='num {cls}'>{roi}</td></tr>"
        )
    return (
        "<div class='breakdown reveal'>"
        "<h3>ЧТО БЫ ДАЛИ ДРУГИЕ ПРАВИЛА</h3>"
        f"<p class='bd-cap'>Считается по журналу движений — там лежит каждое падение, "
        f"включая те, что нынешние правила не публикуют. Всего пригодных для проверки "
        f"движений: {cf['pool']}. Флэт ${cf['stake']:.0f} по цене, которую реально "
        "давали. Это не предложение поменять правила: на такой выборке любая строка "
        "может оказаться случайностью. Смысл в том, чтобы отрезанное продолжало "
        "считаться — иначе решение «2★ не нужны» уже никогда не проверить.</p>"
        "<div class='bd-scroll'>"
        "<table class='bd'><thead><tr><th>правило</th><th class='num'>ставок</th>"
        "<th class='num'>заход.</th><th class='num'>итог</th>"
        "<th class='num'>доходность</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )


def _strategy_card(stats: dict, title: str, subtitle: str, cls: str, recent=None) -> str:
    win_rate = stats["win_rate"]
    win_rate_html = f"{win_rate:.0f}%" if win_rate is not None else "—"
    avg_clv = stats.get("avg_clv_pct")
    avg_clv_html = f"{avg_clv * 100:+.1f}%" if avg_clv is not None else "—"
    return f"""
    <div class="strat {cls} reveal">
      <div class="strat-head">
        <h3>{title}</h3>
        <p>{subtitle}</p>
      </div>
      <div class="stat-row">
        <div class="stat lead"><b class="{_clv_class(avg_clv)}">{avg_clv_html}</b><span>средний CLV</span></div>
        <div class="stat"><b>{win_rate_html}</b><span>заходимость</span></div>
        <div class="stat"><button class="stat-btn" type="button" data-open="sig-{cls}"
             aria-expanded="false" aria-controls="sig-{cls}"
             title="Показать последние сигналы">{stats['total']}</button><span>сигналов ▾</span></div>
        <div class="stat"><button class="stat-btn" type="button" data-open="res-{cls}"
             aria-expanded="false" aria-controls="res-{cls}"
             title="Показать сыгравшие">{stats['resolved']}</button><span>проверено ▾</span></div>
        <div class="stat"><b>{stats['pending']}</b><span>ждут матча</span></div>
      </div>
      <p class="lead-note">CLV стоит первым не для красоты. Он говорит, взяли ли мы
      цену раньше рынка, и по нашим же данным делит исходы почти начисто:
      у зашедших ставок он в среднем сильно выше нуля, у незашедших около нуля
      или ниже. Заходимость на такой выборке — ещё шум, CLV набирает смысл
      в разы быстрее.</p>
      {_unverifiable_note(stats)}
      <div class="sig-list" id="sig-{cls}" hidden>
        <div class="sig-cap">Последние сигналы этой стратегии</div>
        {_mini_signals(recent, stats.get('strategy'))}
      </div>
      <div class="sig-list" id="res-{cls}" hidden>
        <div class="sig-cap">Последние сыгравшие — по этой стратегии</div>
        {_mini_resolved(stats)}
      </div>
      {_bankroll_block(stats)}
    </div>
    """


def _settled_price(r) -> str:
    """The coefficient a finished bet is judged by — never a bare dash.

    Reported 2026-08-10 on Rafael Jodar — Brandon Nakashima: a settled row
    showed no price at all. It was a handicap play, where the bought price is
    null and only the derived one exists, so the cell rendered "—" while the
    result still counted in the win rate. Showing a verdict without the number
    it was reached on is the one thing this page must never do -- the whole
    claim of the site is that every figure can be recounted.

    A derived price is marked with "~" and stays out of the money; that
    distinction is made in the bank block, not hidden by blanking the cell.
    """
    def _num(key):
        try:
            v = r[key]
        except (KeyError, IndexError):
            return None
        return v

    price = _num("entry_price")
    if price:
        return f"{price:.2f}"
    opt = _num("opt_price")
    if opt:
        return f"{opt:.2f}"
    est = _num("opt_est_price")
    if est:
        return f"~{est:.2f}"
    return "—"


def _mini_resolved(stats: dict, limit: int = 10) -> str:
    """The last finished bets for ONE strategy, opened from its "проверено".

    Deliberately settled with that strategy's own columns (alert_stats already
    selects them), so the optimal card shows the optimal verdict and price --
    not the straight bet's.
    """
    rows = (stats.get("recent") or [])[:limit]
    if not rows:
        return "<p class='none'>По этой стратегии сыгравших ставок пока нет.</p>"
    items = []
    for r in rows:
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        price = _settled_price(r)
        score = _score_text(r["fixture_id"])
        score_html = f" · <b>{score}</b>" if score else ""
        cls, label = _RESULT_LABEL.get(r["result"], ("pending", "⏳ ждём"))
        clv = (f" · CLV {r['clv_pct'] * 100:+.1f}%") if r["clv_pct"] is not None else ""
        items.append(
            f"<li><span class='ms-ev'><b>{html.escape(event)}</b>"
            f"{_sport_badge(r['sport_key'])}"
            f"<small>{html.escape(r['outcome_name'] or '')} @ {price}{score_html}{clv}</small>"
            f"</span><span class='{cls}'>{label}</span></li>"
        )
    return "<ul class='mini'>" + "".join(items) + "</ul>"


def _resolved_table(stats: dict) -> str:
    if not stats["recent"]:
        return ("<p class='empty small'>Проверенных сигналов пока нет — первая строка "
                "появится, когда закончится первый матч с алертом. "
                "Мы показываем и заходы, и промахи: журнал без промахов "
                "не стоит ничего.</p>")
    rows = []
    for r in stats["recent"]:
        result = r["result"]
        cls = "hit" if result == "hit" else ("miss" if result == "miss" else "")
        label = {"hit": "✅ зашла", "miss": "❌ не зашла", "n/a": "— н/д"}.get(result, str(result))
        clv_pct = r["clv_pct"]
        clv_html = f"{clv_pct * 100:+.1f}%" if clv_pct is not None else "—"
        clv_cls = "hit" if r["clv_continued"] == 1 else ("miss" if r["clv_continued"] == 0 else "")
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        old_p = f"{r['old_price']:.2f}" if r["old_price"] else "—"
        new_p = f"{r['new_price']:.2f}" if r["new_price"] else "—"
        entry = f"{r['entry_price']:.2f}" if r["entry_price"] else "—"
        rows.append(
            f"<tr>{_stars_cell(r['stars'])}"
            f"<td><b>{html.escape(event)}</b></td>"
            f"<td>{html.escape(r['outcome_name'] or '')}</td>"
            f"<td class='mono'>{old_p} → {new_p}</td>"
            f"<td class='mono'><b>{entry}</b> <small>{html.escape(r['entry_book'] or '')}</small></td>"
            f"<td class='{cls}'>{label}</td><td class='mono {clv_cls}'>{clv_html}</td></tr>"
        )
    return ("<div class='feed-wrap'><table class='plain'>"
            "<tr><th></th><th>Событие</th><th>Ставили на</th>"
            "<th>Был → стал</th><th>Поставили по</th>"
            "<th>Результат</th><th>CLV</th></tr>"
            + "".join(rows) + "</table></div>")


def _top_books(rows) -> str:
    """Leaderboard of the bookmakers where the entry most often survives -- i.e.
    the ones slowest to reprice, which is exactly where it's worth holding an
    account."""
    if not rows:
        return ("<p class='none'>Рейтинг наберётся с первыми сигналами.</p>")
    top = max((r["n"] for r in rows), default=1) or 1
    items = []
    for i, r in enumerate(rows, 1):
        pct = max(6, int(r["n"] / top * 100))
        items.append(
            f"<li><span class='rk'>{i}</span>"
            f"<span class='bk'>{html.escape(r['book'])}</span>"
            f"<span class='bar'><i style='width:{pct}%'></i></span>"
            f"<span class='ct'>{r['n']}</span></li>"
        )
    return "<ol class='books'>" + "".join(items) + "</ol>"


_RESULT_LABEL = {"hit": ("hit", "✅ зашла"), "miss": ("miss", "❌ не зашла"),
                 "n/a": ("pending", "— н/д")}


def _verdict_chip(result, prefix: str) -> str:
    cls, label = _RESULT_LABEL.get(result, ("pending", "⏳ ждём"))
    return f"<span class='{cls}'>{prefix}: {label}</span>"


def _both_results(row) -> str:
    """Aggressive and optimal side by side.

    They are not the same bet -- on Cocciaretto — Osaka the straight 5.70 lost
    while the +1.5 set handicap won -- so a single verdict on the row can only
    ever be right about one of them.
    """
    keys = row.keys() if hasattr(row, "keys") else row
    agg = row["result"] if "result" in keys else None
    opt = row["opt_result"] if "opt_result" in keys else None
    kind = row["opt_kind"] if "opt_kind" in keys else None
    if not kind:
        return _verdict_chip(agg, "агрессивная")
    if kind == "straight":
        # Identical bet, identical verdict -- two chips would be noise.
        return _verdict_chip(agg, "обе стратегии")
    return (f"<span class='verdicts'>{_verdict_chip(agg, 'агрессивная')}"
            f"{_verdict_chip(opt, 'оптимальная')}</span>")


def _opt_detail_row(b) -> str:
    """What the optimal line actually bet, spelled out under the result.

    Otherwise "оптимальная: зашла" sits there with no indication of WHICH bet
    won -- and it is usually not the one named at the top of the card.
    """
    keys = b.keys() if hasattr(b, "keys") else b
    kind = b["opt_kind"] if "opt_kind" in keys else None
    if not kind or kind == "straight":
        return ""
    pick = _short(str(b["opt_pick"] or ""), 60) if "opt_pick" in keys else ""
    price = None
    if "opt_price" in keys and b["opt_price"]:
        price = f"{b['opt_price']:.2f}"
    elif "opt_est_price" in keys and b["opt_est_price"]:
        price = f"~{b['opt_est_price']:.2f}"
    tail = f" @ <b>{price}</b>" if price else ""
    return (f"<tr><td>Оптимальная ставила</td><td>{html.escape(pick)}{tail}</td></tr>")


# Which sport a fixture belongs to, in words. Requested 2026-08-01: a row
# reading "Boostgate eSports — Su eSports" tells you nothing about whether you
# are looking at Dota, CS or football, and the discipline changes how you read
# the price entirely.
_SPORT_NAMES = {
    "esports_cs2": "CS2",
    "esports_dota2": "Dota 2",
    "esports_lol": "LoL",
    "table_tennis": "Наст. теннис",
}
_SPORT_PREFIXES = (
    ("soccer_", "Футбол"),
    ("tennis_", "Теннис"),
    ("basketball_", "Баскетбол"),
    ("icehockey_", "Хоккей"),
    ("baseball_", "Бейсбол"),
    ("americanfootball_", "Ам. футбол"),
    ("mma_", "MMA"),
    ("boxing", "Бокс"),
    ("cricket_", "Крикет"),
    ("rugby", "Регби"),
    ("golf_", "Гольф"),
    ("aussierules_", "Австр. футбол"),
    ("esports_", "Киберспорт"),
)


def _sport_label(sport_key) -> str:
    key = (sport_key or "").lower()
    if not key:
        return ""
    if key in _SPORT_NAMES:
        return _SPORT_NAMES[key]
    for prefix, name in _SPORT_PREFIXES:
        if key.startswith(prefix):
            return name
    return key.split("_")[0].capitalize()


def _sport_badge(sport_key) -> str:
    label = _sport_label(sport_key)
    if not label:
        return ""
    cls = "sport esp" if (sport_key or "").startswith("esports_") else "sport"
    return f"<span class='{cls}'>{html.escape(label)}</span>"


def _score_text(fixture_id) -> str:
    """Final score of a match that is over, as plain text."""
    row = _FINAL.get(fixture_id)
    if not row or row["home_score"] is None or row["away_score"] is None:
        return ""
    fmt = lambda v: f"{v:.0f}" if float(v).is_integer() else f"{v:g}"  # noqa: E731
    return f"{fmt(row['home_score'])}:{fmt(row['away_score'])}"


def _last_bets(bets, limit: int = 10) -> str:
    """The track record: one card per finished bet, openable for the detail.

    Merged 2026-08-01 from two blocks that showed the same rows twice -- a
    compact table and a detail list. One row, closed by default, carrying
    everything you need to judge it at a glance: who, what we backed and at
    what price, how the match ended, and how EACH strategy settled. The rest
    opens on click.
    """
    if not bets:
        return ("<p class='empty small'>Сыгравших ставок пока нет — первая строка "
                "появится, когда закончится матч с сигналом. Промахи показываем "
                "наравне с заходами: журнал без промахов не стоит ничего.</p>")
    items = []
    for b in bets[:limit]:
        home, away = b["home_team"], b["away_team"]
        event = f"{home} — {away}" if home and away else str(b["fixture_id"])
        stars = _star_mark(b["stars"], word=True)
        entry = _settled_price(b)
        score = _score_text(b["fixture_id"])
        score_html = f"<span class='b-score'>{score}</span>" if score else ""
        status = _both_results(b)
        clv = f"{b['clv_pct'] * 100:+.1f}%" if b["clv_pct"] is not None else "—"
        old_p = f"{b['old_price']:.2f}" if b["old_price"] else "—"
        new_p = f"{b['new_price']:.2f}" if b["new_price"] else "—"
        score_row = (f"<tr><td>Счёт матча</td><td class='mono'><b>{score}</b></td></tr>"
                     if score else "")
        items.append(
            "<details class='bet'><summary>"
            f"<span class='b-left'>{stars}"
            f"<span class='b-name'>{html.escape(event)}{_sport_badge(b['sport_key'])}{score_html}</span>"
            f"<span class='b-pick'>{html.escape(b['outcome_name'] or '')} @ {entry}</span></span>"
            f"{status}</summary>"
            "<div class='b-body'><table>"
            f"<tr><td>Ставили на</td><td><b>{html.escape(b['outcome_name'] or '')}</b></td></tr>"
            f"<tr><td>Коэффициент был</td><td class='mono'>{old_p}</td></tr>"
            f"<tr><td>Просел до</td><td class='mono'>{new_p}</td></tr>"
            f"<tr><td>Поставили по</td><td class='mono'><b>{entry}</b> — "
            f"{html.escape(b['entry_book'] or '')}</td></tr>"
            f"{_opt_detail_row(b)}"
            f"{score_row}"
            f"<tr><td>Просело у контор</td><td class='mono'>{b['down_count'] or 0} "
            f"из {b['books_count'] or 0}</td></tr>"
            f"<tr><td>Старт матча</td><td class='mono'>{_fmt_dt(b['start_time'])}</td></tr>"
            f"<tr><td>Сигнал зафиксирован</td><td class='mono'>{_fmt_dt(b['detected_at'])}</td></tr>"
            f"<tr><td>Результат</td><td>{status}</td></tr>"
            f"<tr><td>CLV</td><td class='mono'>{clv}</td></tr>"
            "</table></div></details>"
        )
    return "<div class='last5'>" + "".join(items) + "</div>"


def _detect_diag() -> dict:
    """The last poll's measurement coverage, as stored by main.py."""
    try:
        return json.loads(storage.get_meta("detect_diag") or "{}")
    except (ValueError, TypeError):
        return {}


def _budget_plan() -> dict:
    """What the credit governor allowed this cycle, as stored by main.py."""
    try:
        return json.loads(storage.get_meta("budget_plan") or "{}")
    except (ValueError, TypeError):
        return {}


def _grading_state() -> dict:
    """When bets were last scored, and when they will be scored next.

    Added 2026-08-16, the morning after the book was reset, because the very
    first bet of the new record raised a question the site could not answer.
    The Danish match kicked off at 12:00, and at 16:30 it still showed no
    result. Nothing anywhere said why. From outside, "not graded yet" and
    "grading is broken" look identical -- and this project has already lost
    days to exactly that kind of ambiguity twice.

    The answer turned out to be mundane: scores cost quota, so results.py runs
    at most once every RESULTS_CHECK_INTERVAL_HOURS and only looks at matches
    that started RESULT_CHECK_DELAY_HOURS ago. The bet was simply between two
    windows. But working that out took reading the source and reconstructing a
    schedule by hand, which is not a thing a reader should ever have to do
    about their own statistics.

    So the schedule is published. A reader who wonders where their result is
    gets a time instead of a silence.
    """
    from config import RESULTS_CHECK_INTERVAL_HOURS, RESULT_CHECK_DELAY_HOURS
    out = {"interval_hours": RESULTS_CHECK_INTERVAL_HOURS,
           "delay_hours": RESULT_CHECK_DELAY_HOURS}
    stamp = storage.get_meta("last_results_check_at")
    if not stamp:
        return out
    try:
        last = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return out
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    nxt = last + timedelta(hours=RESULTS_CHECK_INTERVAL_HOURS)
    out["last_check_at"] = last.isoformat()
    out["next_check_at"] = nxt.isoformat()
    try:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(hours=RESULT_CHECK_DELAY_HOURS)).isoformat()
        out["awaiting"] = len(storage.get_unresolved_alerts(cutoff))
    except Exception:  # noqa: BLE001 -- a diagnostic must never break the page
        pass
    return out


def _grading_line() -> str:
    """One sentence on the page answering 'where is my result'."""
    g = _grading_state()
    nxt = g.get("next_check_at")
    if not nxt:
        return ""
    try:
        when = datetime.fromisoformat(nxt).strftime("%H:%M")
    except (TypeError, ValueError):
        return ""
    waiting = g.get("awaiting") or 0
    who = (f"{waiting} {_plural(waiting, 'ставка ждёт', 'ставки ждут', 'ставок ждут')} расчёта"
           if waiting else "ставок в очереди нет")
    return (f"<p class='detect ok'>Результаты сверяем раз в "
            f"{g['interval_hours']:g} ч и не раньше чем через {g['delay_hours']:g} ч "
            f"после старта матча — {who}, следующая сверка в {when} UTC.</p>")


def _write_ledger(agg: dict) -> None:
    """Dump every logged signal next to index.html as plain JSON.

    An audience that assumes every betting site is fake trusts an ugly,
    unstyled data file more than any polished claim on the page: it can be
    diffed, scraped and checked against a bookmaker's own history. Publishing
    it also makes cherry-picking impossible -- misses are in the same file as
    the hits, and anyone can count them.
    """
    rows = storage.export_ledger()
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "poll_interval_minutes": POLL_INTERVAL_MINUTES,
        "threshold_pct": SPIKE_THRESHOLD_PCT * 100,
        "optimal_max_price": OPTIMAL_MAX_PRICE,
        "flat_stake": agg.get("stake"),
        # The funnel and the entry-threshold preview travel WITH the ledger on
        # purpose. Both were previously computable only inside the poll job, so
        # answering "where did the signals go" or "what would a 40% threshold
        # give" meant reading a CI log by hand -- which turned out to be
        # impossible on a day the browser was down. Numbers that decisions rest
        # on belong in the published file, where anyone can recount them.
        "funnel_24h": storage.funnel_stats(24),
        "entry_threshold_preview": storage.capture_threshold_preview(),
        "detect": _detect_diag(),
        # The width this cycle could afford, and the arithmetic behind it. A
        # constraint nobody can see is how the tracker spent a day silent in
        # August; a constraint that now decides how much market we watch had
        # better be published rather than inferred from a CI log.
        "budget": _budget_plan(),
        # When the record gets scored. Published so "где мой результат"
        # has an answer that is a timestamp rather than a silence.
        "grading": _grading_state(),
        # История зазоров едет вместе с журналом намеренно: решения по
        # порогам будут приниматься по ней, а число, на котором стоит
        # решение, обязано лежать там, где его может пересчитать кто угодно.
        "polymarket": {
            **storage.pm_stats(),
            "gaps": storage.pm_gap_summary(),
            "results": storage.pm_results(),
            "coverage_by_sport": storage.pm_coverage_by_sport(),
        },
        "count": len(rows),
        "signals": rows,
    }
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
    _write_pm_feed()


PM_FEED_PATH = os.path.join(os.path.dirname(DASHBOARD_PATH), "pm_signals.json")


def _write_pm_feed() -> None:
    """A separate, small, stable file for machines. Not for humans.

    ledger.json is the record: everything that ever happened, growing forever.
    A trading bot needs the opposite -- a short list of what is actionable in
    the next few minutes, in a shape that will not change under it. Mixing the
    two would mean either a bot parsing a hundred historical rows to find two
    live ones, or a record shaped by the convenience of a consumer.

    The contract is deliberately narrow and deliberately boring:
      version      bumped only on a BREAKING change to this shape
      rule         what "actionable" currently means, in machine-readable form
      signals[]    zero or more rows, each with the token to buy, the price
                   floor below which the trade is off, and the size the book
                   actually held at that floor when we looked
      checked_at   per row: how fresh the look is. Stale is not tradeable.

    max_stake_usd is the executable size, not a wish. If the book held $37 at
    or above the floor, this says 37 -- that was the explicit instruction:
    "если к примеру не залезаем, то писать ту сумму по которой залезли".
    """
    rows = storage.pm_live_feed()
    out = []
    for r in rows:
        out.append({
            "fixture_id": r["fixture_id"],
            "event": f"{r['home_team']} vs {r['away_team']}",
            "polymarket_event": r["event_title"],
            "polymarket_slug": r["event_slug"],
            "pick": r["outcome_name"],
            "leg": r["leg"],
            "leg_means": r["means"] or ("прямая победа" if r["leg"] == "aggressive"
                                        else "двойной шанс"),
            "source": r["source"],
            "token_id": r["token_id"],
            "side": "BUY",
            "starts_at": r["start_time"],
            "lead_hours": r["lead_hours"],
            "bookmaker_price": r["entry_price"],
            "bookmaker_book": r["entry_book"],
            "min_coef": r["need_coef"],
            "max_price": round(1.0 / r["need_coef"], 4) if r["need_coef"] else None,
            "seen_coef": r["avg_coef"],
            "edge_pct": r["edge_pct"],
            "max_stake_usd": r["exec_stake_usd"],
            "full_size_available": bool(r["fits_target"]),
            "polymarket_question": r["question"],
            "signal_stars": r["base_stars"],
            "pm_stars": _pm_stars_of(r),
            "checked_at": r["checked_at"],
        })
    payload = {
        "version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "rule": {
            "compare_against": "best bookmaker entry price for the same pick",
            "min_edge_pct": POLYMARKET_MIN_EDGE_PCT,
            "target_stake_usd": POLYMARKET_TARGET_STAKE,
            "note": ("buy only at or below max_price; max_stake_usd is what the "
                     "book held at that limit at checked_at, not a promise"),
            "legs": ("aggressive = the straight win, same bet as at the "
                     "bookmaker; optimal = the double chance, bought here as "
                     "No on the opponent. Both may appear for one event and "
                     "may be taken together -- they are different prices on "
                     "the same view, not a hedge"),
            "dedupe_on": ["fixture_id", "pick", "leg"],
        },
        "count": len(out),
        "signals": out,
    }
    with open(PM_FEED_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


def _pm_stars_of(r) -> int:
    try:
        import polymarket
        return polymarket.pm_stars(r["pm_lag"], r["down_count"] or 0,
                                   r["books_count"] or 0,
                                   r["exec_stake_usd"] or 0,
                                   edge_pct=r["edge_pct"])
    except Exception:                                          # noqa: BLE001
        return 0


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

PAGE = Template(r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>STEAMLINE — видим деньги раньше рынка</title>
<meta name="description" content="Трекер движения коэффициентов: ловим момент, когда на исход занесли деньги, и показываем конторы, где старая цена ещё стоит.">
<meta name="theme-color" content="#08080b">
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'><rect width='64' height='64' rx='16' fill='%23c8ff2e'/><path d='M20 14v14M27 12v16M34 14v14M18 28h18a2 2 0 012 2v2a11 11 0 01-8 10v12a3 3 0 01-6 0V42a11 11 0 01-8-10v-2a2 2 0 012-2z' fill='none' stroke='%2308080b' stroke-width='3.4' stroke-linecap='round' stroke-linejoin='round'/></svg>">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Unbounded:wght@400;600;800;900&family=Golos+Text:wght@400;500;600;700&family=JetBrains+Mono:wght@500;700&display=swap" rel="stylesheet">
<style>
/* ---------------------------------------------------------------- tokens */
:root{
  --bg:#08080b; --bg2:#0e0e13; --card:#121218; --card2:#16161d;
  --line:#24242e; --line2:#2f2f3c;
  --ink:#f4f4f1; --ink2:#a8a8a0; --ink3:#7b7b86;
  --lime:#c8ff2e; --mag:#ff3d81; --cy:#4ad9ff;
  --good:#3ddc84; --bad:#ff6b6b; --warn:#ffc531;
  --r:18px; --mono:"JetBrains Mono",ui-monospace,Menlo,monospace;
}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font:16px/1.6 "Golos Text",system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
  -webkit-font-smoothing:antialiased; overflow-x:hidden;
}
/* Drifting colour blobs. Fixed + blurred so they never reflow anything, and
   frozen entirely for anyone who asked for reduced motion. */
.mesh{position:fixed;inset:-20vmax;z-index:0;pointer-events:none;filter:blur(90px);opacity:.5}
.mesh i{position:absolute;display:block;border-radius:50%;mix-blend-mode:screen}
.mesh i:nth-child(1){width:46vmax;height:46vmax;left:-6vmax;top:-4vmax;background:#243b00;animation:drift1 34s ease-in-out infinite alternate}
.mesh i:nth-child(2){width:38vmax;height:38vmax;right:-4vmax;top:8vmax;background:#3a0a26;animation:drift2 41s ease-in-out infinite alternate}
.mesh i:nth-child(3){width:42vmax;height:42vmax;left:24vmax;bottom:-10vmax;background:#04283a;animation:drift3 47s ease-in-out infinite alternate}
@keyframes drift1{to{transform:translate3d(9vmax,6vmax,0) scale(1.12)}}
@keyframes drift2{to{transform:translate3d(-8vmax,10vmax,0) scale(1.08)}}
@keyframes drift3{to{transform:translate3d(6vmax,-8vmax,0) scale(1.15)}}
/* Film grain: one inline SVG turbulence, painted once, never re-rendered. */
body::after{
  content:"";position:fixed;inset:0;z-index:1;pointer-events:none;opacity:.05;
  background-image:url("data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg'><filter id='n'><feTurbulence type='fractalNoise' baseFrequency='.85' numOctaves='3'/></filter><rect width='100%25' height='100%25' filter='url(%23n)'/></svg>");
}
.wrap{position:relative;z-index:2;max-width:1180px;margin:0 auto;padding:0 20px 90px}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}

/* ------------------------------------------------------------------- nav */
.nav{
  position:sticky;top:0;z-index:40;backdrop-filter:blur(14px);
  background:rgba(8,8,11,.72);border-bottom:1px solid var(--line);
}
.nav-in{max-width:1180px;margin:0 auto;padding:10px 20px;display:flex;align-items:center;gap:14px}
.nav .mark{display:flex;align-items:center;gap:10px;font-family:Unbounded,sans-serif;font-weight:800;
  font-size:14px;letter-spacing:.06em;text-transform:uppercase}
.nav .mark svg{width:30px;height:30px;flex:none}
.nav .sp{flex:1}
.nav a.link{color:var(--ink2);text-decoration:none;font-size:14px;font-weight:600;padding:6px 10px;border-radius:9px}
.nav a.link:hover{color:var(--lime);background:rgba(200,255,46,.08)}
/* Polymarket стал основой продукта, и в шапке он выделен ровно поэтому --
   не украшением, а признанием того, где теперь центр тяжести. */
.nav a.link.pm{color:var(--lime);background:rgba(200,255,46,.10);
  border:1px solid rgba(200,255,46,.28)}
.nav a.link.pm:hover{background:rgba(200,255,46,.18)}
/* Подзаголовки внутри раздела Polymarket: он длинный, и без ритма читается
   как одна простыня. */
.pm-h3{font-family:Unbounded,sans-serif;font-size:15px;text-transform:uppercase;
  letter-spacing:.02em;margin:30px 0 8px;color:var(--ink)}
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;
  padding:6px 11px;border-radius:999px;border:1px solid var(--line2);white-space:nowrap}
.pill.live{color:var(--lime);border-color:rgba(200,255,46,.35);background:rgba(200,255,46,.07)}
.pill.stale{color:var(--warn);border-color:rgba(255,197,49,.35);background:rgba(255,197,49,.07)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor;animation:beat 1.9s infinite}
@keyframes beat{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}

/* ---------------------------------------------------------------- ticker */
.ticker{overflow:hidden;border-bottom:1px solid var(--line);background:rgba(18,18,24,.55);
  position:relative;z-index:3}
.ticker .tr{display:flex;width:max-content;animation:slide 46s linear infinite}
.ticker .tt{display:flex;gap:26px;padding:8px 13px}
.ticker .ti{display:inline-flex;align-items:center;gap:7px;font-size:12.5px;color:var(--ink2);white-space:nowrap}
.ticker .ti b{font-family:var(--mono);color:var(--ink)}
.ticker .ti u{text-decoration:none;color:var(--lime);font-weight:700}
.ticker .dot{width:5px;height:5px;border-radius:50%;background:var(--mag);display:inline-block}
@keyframes slide{to{transform:translateX(-50%)}}

/* ----------------------------------------------------------------- hero */
.hero{padding:40px 0 22px;display:grid;grid-template-columns:1.15fr .85fr;gap:30px;align-items:center}
.logo{width:84px;height:84px;filter:drop-shadow(0 10px 30px rgba(200,255,46,.22))}
/* Deliberately smaller than the first version: at 74px the headline filled
   most of a laptop screen and read as shouting rather than as a title. */
.hero h1{
  font-family:Unbounded,sans-serif;font-weight:800;line-height:1.06;letter-spacing:-.015em;
  font-size:clamp(27px,3.4vw,42px);margin:14px 0 0;text-transform:uppercase;
}
.hero h1 em{font-style:normal;background:linear-gradient(96deg,var(--lime),var(--cy) 55%,var(--mag));
  -webkit-background-clip:text;background-clip:text;color:transparent}
.hero .tag{font-family:Unbounded,sans-serif;font-size:12px;font-weight:600;letter-spacing:.22em;
  text-transform:uppercase;color:var(--ink3)}
.hero .sub{font-size:16px;color:var(--ink2);margin:14px 0 0;max-width:44ch}
.hero .sub b{color:var(--ink)}
.cta{display:flex;flex-wrap:wrap;gap:10px;margin-top:22px}
.btn{display:inline-flex;align-items:center;gap:8px;padding:12px 20px;border-radius:999px;
  font-weight:700;font-size:15px;text-decoration:none;border:1px solid transparent;transition:.18s}
.btn.primary{background:var(--lime);color:#0b0b06}
.btn.primary:hover{transform:translateY(-2px);box-shadow:0 10px 26px rgba(200,255,46,.28)}
.btn.ghost{border-color:var(--line2);color:var(--ink)}
.btn.ghost:hover{border-color:var(--lime);color:var(--lime)}

.clock{background:linear-gradient(160deg,var(--card),rgba(18,18,24,.4));border:1px solid var(--line);
  border-radius:var(--r);padding:22px}
.clock .cl-lab{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--ink3);font-weight:700}
.clock .cl-time{font-family:Unbounded,sans-serif;font-weight:800;font-size:clamp(34px,4.6vw,46px);
  line-height:1;margin:8px 0 12px;font-variant-numeric:tabular-nums}
.cd-bar{height:5px;border-radius:99px;background:var(--line);overflow:hidden}
.cd-bar i{display:block;height:100%;width:0;border-radius:99px;
  background:linear-gradient(90deg,var(--lime),var(--cy));transition:width .9s linear}
.clock .cl-meta{margin-top:12px;font-size:13px;color:var(--ink3);display:grid;gap:4px}
.clock .cl-meta b{color:var(--ink2);font-weight:600}

/* --------------------------------------------------------------- numbers */
.kpis{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin:8px 0 6px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:15px;padding:15px 14px;
  transition:.2s;position:relative;overflow:hidden}
.kpi:hover{border-color:var(--line2);transform:translateY(-3px)}
.kpi b{display:block;font-family:Unbounded,sans-serif;font-weight:800;font-size:26px;line-height:1.1;
  font-variant-numeric:tabular-nums}
.kpi span{display:block;font-size:11.5px;color:var(--ink3);margin-top:5px;line-height:1.35}
.kpi.lime b{color:var(--lime)} .kpi.mag b{color:var(--mag)} .kpi.cy b{color:var(--cy)}
.kpi-note{font-size:12.5px;color:var(--ink3);margin:6px 0 0}
.funnel{margin:14px 0 0;background:var(--card);border:1px solid var(--line);border-radius:15px;padding:15px 17px}
.fn-head{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);font-weight:700;margin-bottom:10px}
.funnel ul{list-style:none;margin:0;padding:0;display:grid;gap:7px}
.funnel li{display:flex;justify-content:space-between;align-items:baseline;gap:14px;font-size:13.5px;color:var(--ink2)}
.funnel li b{font-family:var(--mono);font-size:16px;font-weight:800;color:var(--ink)}
.funnel li.fn-top{border-bottom:1px solid var(--line);padding-bottom:8px}
.funnel li.fn-top b{color:var(--mag)}
.funnel li.fn-ok{border-top:1px solid var(--line);padding-top:8px}
.funnel li.fn-ok b{color:var(--lime);font-size:19px}
.detect{margin:14px 0 0;font-size:12.5px;line-height:1.55;color:var(--ink2)}
.detect b{font-family:var(--mono);color:var(--ink)}
.detect.warn{background:rgba(255,45,149,.08);border:1px solid rgba(255,45,149,.35);
  border-radius:12px;padding:11px 13px;color:var(--ink)}
.stat.lead b{font-size:26px}
.clv-good{color:var(--lime)}
.clv-bad{color:var(--mag)}
.clv-flat{color:var(--ink2)}
.lead-note{margin:6px 0 0;font-size:12px;line-height:1.5;color:var(--ink2)}
.breakdown{margin:22px 0 0;background:var(--card);border:1px solid var(--line);border-radius:15px;padding:16px 18px}
.breakdown h3{font-family:Unbounded,sans-serif;font-size:14px;margin:0 0 6px;text-transform:uppercase}
.bd-cap{margin:0 0 14px;font-size:12.5px;line-height:1.55;color:var(--ink2)}
.bd-card{margin:0 0 14px}
.bd-card h4{font-size:12px;letter-spacing:.06em;text-transform:uppercase;color:var(--ink2);margin:0 0 6px}
/* The by-stars cut is not one of three equals any more: it is the table that
   answers whether the two-star rung is worth taking at all, so it is lifted
   out of the stack instead of being third in a list of look-alikes. */
.bd-card.key{background:var(--card2);border:1px solid rgba(200,255,46,.24);border-radius:13px;
  padding:14px 15px 6px;margin:0 0 18px}
.bd-card.key h4{color:var(--lime);font-size:13.5px;display:flex;flex-wrap:wrap;align-items:center;gap:9px}
.bd-flag{padding:2px 7px;border-radius:6px;background:rgba(200,255,46,.13);color:var(--lime);
  border:1px solid rgba(200,255,46,.35);font-size:9px;font-weight:800;letter-spacing:.08em}
.bd-why{margin:0 0 11px;font-size:12.5px;line-height:1.55;color:var(--ink2);max-width:78ch}
.bd-why b{color:var(--ink)}
.bd-card.key table.bd td{padding-top:9px;padding-bottom:9px}
.bd-card.key table.bd td:first-child{width:1%;white-space:nowrap;padding-right:16px}
.bd-scroll{overflow-x:auto}
/* Overrides the page-wide table min-width of 720px, which these narrow
   five-column tables never needed and which used to push them off a phone. */
table.bd{width:100%;min-width:470px;border-collapse:collapse;font-size:13px}
table.bd th{text-align:left;font-weight:600;color:var(--ink2);font-size:11.5px;
  text-transform:uppercase;letter-spacing:.04em;padding:0 8px 6px 0;border-bottom:1px solid var(--line)}
table.bd td{padding:7px 8px 7px 0;border-bottom:1px solid var(--line)}
table.bd td.num,table.bd th.num{text-align:right;font-family:var(--mono)}
table.bd td.pos{color:var(--lime)}
table.bd td.neg{color:var(--mag)}

/* ----------------------------------------------------------------- bento */
h2{font-family:Unbounded,sans-serif;font-weight:800;font-size:clamp(22px,3vw,30px);
  letter-spacing:-.01em;margin:46px 0 6px;text-transform:uppercase}
h2 .hash{color:var(--lime);margin-right:8px}
.lead{color:var(--ink2);margin:0 0 18px;max-width:72ch}
.card{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:20px}
.bento{display:grid;grid-template-columns:1.55fr 1fr;gap:14px}
.how ul{margin:10px 0 0;padding:0;list-style:none;display:grid;gap:12px}
.how li{display:grid;grid-template-columns:30px 1fr;gap:11px;align-items:start;color:var(--ink2);font-size:14.6px}
.how li i{font-style:normal;width:28px;height:28px;border-radius:9px;display:grid;place-items:center;
  background:rgba(200,255,46,.1);color:var(--lime);font-weight:800;font-size:13px;font-family:var(--mono)}
.how li b{color:var(--ink)}
/* The ladder is spelled out with the same marks the rows carry, so the method
   section is where a reader learns the code rather than a second place that
   describes it in words alone. */
.rungs{list-style:none;display:grid;gap:8px;margin:10px 0 10px;padding:0}
.rungs li{display:flex;align-items:center;gap:10px;font-size:13.5px;color:var(--ink3)}
.rungs li b{font-family:var(--mono);font-weight:700;color:var(--ink2)}
.rungs .stars{width:158px;flex:none}
.books{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:8px}
.books li{display:grid;grid-template-columns:20px 1fr 64px 30px;align-items:center;gap:9px;font-size:13.5px}
.books .rk{color:var(--ink3);font-family:var(--mono);font-size:12px}
.books .bk{color:var(--ink);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.books .bar{height:6px;background:var(--line);border-radius:99px;overflow:hidden}
.books .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--lime),var(--cy))}
.books .ct{text-align:right;font-family:var(--mono);color:var(--ink2);font-size:12.5px}
.none,.kpi-note{color:var(--ink3);font-size:13px}

/* ------------------------------------------------------ confidence ladder */
/* A meter, not a string of glyphs: the unlit track is always drawn, so two
   stars visibly occupy half the ladder and the column keeps one width no
   matter which rung a row is on. Colour is the second cue and the word from
   STAR_LABELS the third -- the mark still ranks itself printed in grey. */
.stars{display:inline-flex;align-items:center;gap:7px;line-height:1;position:relative;white-space:nowrap}
.stars.stack{display:inline-block}
.st{position:relative;display:inline-block;font-size:13px;letter-spacing:1.6px;line-height:1;
  vertical-align:middle;flex:none}
.st .trk{color:var(--line2)}
.st .fill{position:absolute;left:0;top:0;letter-spacing:inherit;overflow:hidden;white-space:nowrap}
.st-lab{display:inline-block;padding:2px 6px;border-radius:6px;vertical-align:middle;
  font-size:9px;font-weight:800;letter-spacing:.07em;text-transform:uppercase;line-height:1.3;
  white-space:nowrap;border:1px solid transparent}
.stars.stack .st-lab{display:block;margin-top:6px}
.s4 .fill{color:var(--lime);text-shadow:0 0 13px rgba(200,255,46,.5)}
.s4 .st-lab{background:rgba(200,255,46,.13);color:var(--lime);border-color:rgba(200,255,46,.4)}
.s3 .fill{color:var(--warn)}
.s3 .st-lab{background:rgba(255,197,49,.11);color:var(--warn);border-color:rgba(255,197,49,.34)}
.s2 .fill{color:var(--ink2)}
.s2 .st-lab{background:rgba(255,255,255,.045);color:var(--ink3);border-color:var(--line2)}
.s1 .fill,.s0 .fill{color:var(--ink3)}

/* ------------------------------------------------------------------ feed */
.toolbar{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:0 0 14px}
.f{cursor:pointer;font:inherit;font-size:13px;font-weight:600;color:var(--ink2);
  background:var(--card);border:1px solid var(--line);border-radius:999px;padding:8px 13px;
  display:inline-flex;align-items:center;gap:7px;transition:.16s}
.f:hover{border-color:var(--line2);color:var(--ink)}
.f.active{background:var(--lime);color:#0b0b06;border-color:var(--lime)}
.f .n{font-family:var(--mono);font-size:11px;opacity:.75}
/* The rung chips sit in one recessed group: they answer a single question
   ("насколько уверенно"), the others answer different ones, and seven
   identical pills in a row made that impossible to see. */
/* 22px rather than 999px: on one line it still reads as a pill, and when the
   group wraps on a phone it stays a tidy rounded box instead of a stadium. */
.f-group{display:inline-flex;flex-wrap:wrap;gap:6px;padding:4px;border-radius:22px;
  background:rgba(255,255,255,.028);border:1px solid var(--line)}
.f.fs{padding-left:11px}
.f.fs .st{font-size:12px}
.f.s4{border-color:rgba(200,255,46,.3)}
.f.s3{border-color:rgba(255,197,49,.28)}
.f.s2{border-color:var(--line2)}
.f.s4:hover{border-color:var(--lime);color:var(--ink)}
.f.s3:hover{border-color:var(--warn);color:var(--ink)}
.f.s4.active{background:var(--lime);border-color:var(--lime);color:#0b0b06}
.f.s3.active{background:var(--warn);border-color:var(--warn);color:#0b0b06}
.f.s2.active{background:var(--ink2);border-color:var(--ink2);color:#0b0b06}
.f.active .fill{color:#0b0b06;text-shadow:none}
.f.active .trk{color:rgba(11,11,6,.26)}
.feed-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r);background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:720px}
th{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);font-weight:700;
  text-align:left;padding:13px 14px;border-bottom:1px solid var(--line);background:var(--card2)}
td{padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:middle;font-size:14.5px}
tr:last-child td{border-bottom:0}
tbody tr:hover td,table tr.row:hover td{background:rgba(255,255,255,.022)}
/* width:1% is the auto-layout way of saying "as narrow as the content allows":
   the cell then sizes itself to four stars plus the longest label instead of
   to a hard-coded number that a fifth rung or a longer word would break. The
   old rule sized it by three bare glyphs and nothing else. */
.c-stars{white-space:nowrap;width:1%;padding-right:8px;vertical-align:top;padding-top:15px}
.c-ev b{display:block;font-weight:600}
.c-ev small{display:block;color:var(--ink3);font-size:11.5px;font-family:var(--mono)}
.cd-to{display:inline-block;margin-top:4px;font-size:11px;font-weight:700;font-family:var(--mono);padding:2px 7px;border-radius:6px;background:rgba(74,217,255,.1);color:var(--cy);white-space:nowrap}
.cd-to.soon{background:rgba(255,197,49,.13);color:var(--warn)}
.cd-to.live{background:rgba(255,61,129,.13);color:var(--mag)}
.cd-to.done{background:rgba(255,255,255,.05);color:var(--ink3)}
.verdicts{display:flex;flex-direction:column;gap:3px;align-items:flex-end;font-size:12.5px;white-space:nowrap}
.tag.warnish{background:rgba(255,197,49,.13);color:var(--warn);border-color:rgba(255,197,49,.35)}
.sport{display:inline-block;margin-left:8px;padding:1px 7px;border-radius:5px;background:rgba(74,217,255,.12);color:var(--cy);font-size:10.5px;font-weight:800;letter-spacing:.05em;text-transform:uppercase;vertical-align:middle}
.sport.esp{background:rgba(255,61,129,.13);color:var(--mag)}
.b-score{margin-left:9px;padding:1px 8px;border-radius:999px;background:rgba(255,255,255,.07);color:var(--ink);font-family:var(--mono);font-size:12.5px;font-weight:800}
.c-out{font-weight:600}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.tag{font-size:9.5px;font-weight:800;letter-spacing:.09em;padding:3px 7px;border-radius:6px;
  text-transform:uppercase;white-space:nowrap}
.tag.opt{background:rgba(61,220,132,.13);color:var(--good)}
.tag.agg{background:rgba(255,61,129,.13);color:var(--mag)}
.tag.safe{background:rgba(74,217,255,.13);color:var(--cy)}
.c-move{white-space:nowrap;font-family:var(--mono);font-size:16px;font-weight:700}
.c-move .old{color:var(--ink3);text-decoration:line-through;font-size:14px;font-weight:500}
.c-move .arr{color:var(--ink3);margin:0 5px}
.c-move .new{color:var(--warn);font-weight:800;font-size:17px}
.c-move .pct{color:var(--mag);font-weight:800;margin-left:8px;font-size:15px}
.c-books{font-family:var(--mono);color:var(--ink);font-weight:700}
.c-books .of{color:var(--ink3);font-weight:400}
.c-bet .price{display:block;font-family:Unbounded,sans-serif;font-weight:900;font-size:24px;color:var(--lime);letter-spacing:-.01em;line-height:1.1}
.c-bet small{display:block;color:var(--ink3);font-size:11.5px}
.chip.shut{color:var(--ink3);font-size:12px;font-weight:600}
.c-ev .chip{display:inline-block;margin-top:5px;margin-right:6px;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.04em;text-transform:uppercase}
.c-ev .chip.fresh{background:rgba(200,255,46,.14);color:var(--lime);border:1px solid rgba(200,255,46,.35)}
.c-ev .chip.win{background:rgba(60,220,130,.13);color:var(--good);border:1px solid rgba(60,220,130,.32)}
.c-ev .chip.lose{background:rgba(255,61,129,.12);color:var(--bad);border:1px solid rgba(255,61,129,.3)}
.c-ev .chip.flag{background:rgba(255,197,49,.15);color:var(--warn);border:1px solid rgba(255,197,49,.4)}
.c-ev .chip.na{background:rgba(255,255,255,.05);color:var(--ink3);border:1px solid var(--line)}
.score{display:inline-block;margin-left:8px;padding:2px 9px;border-radius:999px;font-family:var(--mono);font-size:13px;font-weight:800;color:#0b0b06;background:var(--mag);letter-spacing:.02em}
.score.done{background:rgba(255,255,255,.08);color:var(--ink2)}
.c-bet .price.est{color:var(--cy)}
.mono{font-family:var(--mono);font-size:15px;font-weight:700;color:var(--cy)}
.hit{color:var(--good);font-weight:600}
.miss{color:var(--bad);font-weight:600}
.pending{color:var(--ink3)}
.norows{color:var(--ink3);padding:16px 2px}

/* ------------------------------------------------------------- strategy */
.strats{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.strat{background:var(--card);border:1px solid var(--line);border-radius:var(--r);padding:20px}
.strat.opt{border-color:rgba(61,220,132,.26)}
.strat.agg{border-color:rgba(255,61,129,.24)}
.strat-head h3{font-family:Unbounded,sans-serif;font-size:16px;font-weight:800;margin:0;
  letter-spacing:.02em;text-transform:uppercase}
.strat.opt h3{color:var(--good)} .strat.agg h3{color:var(--mag)}
.strat-head p{margin:5px 0 14px;font-size:13.4px;color:var(--ink3)}
.stat-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px}
.stat{background:var(--card2);border:1px solid var(--line);border-radius:12px;padding:11px 9px;text-align:center}
.stat-btn{display:block;width:100%;cursor:pointer;background:none;border:0;padding:0;
  font-family:Unbounded,sans-serif;font-weight:800;font-size:18px;color:var(--ink);
  font-variant-numeric:tabular-nums;border-bottom:1px dashed var(--line2)}
.stat-btn:hover{color:var(--lime);border-bottom-color:var(--lime)}
.sig-list{margin:0 0 14px;border:1px solid var(--line2);border-radius:13px;padding:13px;
  background:var(--card2)}
.sig-cap{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);
  font-weight:700;margin-bottom:9px}
ul.mini{list-style:none;margin:0;padding:0;display:grid;gap:9px}
ul.mini li{display:flex;justify-content:space-between;align-items:center;gap:11px;
  font-size:13px;border-bottom:1px solid var(--line);padding-bottom:8px}
ul.mini li:last-child{border-bottom:0;padding-bottom:0}
.ms-ev{min-width:0}
.ms-ev b{display:block;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ms-ev small{display:block;color:var(--ink3);font-size:12.5px;font-family:var(--mono)}
.ms-ev small b{color:var(--lime);font-size:14px;font-weight:800}
.stat b{display:block;font-family:Unbounded,sans-serif;font-weight:800;font-size:18px;
  font-variant-numeric:tabular-nums}
.stat span{display:block;font-size:10px;color:var(--ink3);margin-top:3px}
.note-warn{font-size:12px;color:var(--warn);background:rgba(255,197,49,.07);border:1px solid rgba(255,197,49,.22);border-radius:11px;padding:9px 11px;margin:0 0 12px}
.bank{border-radius:14px;padding:15px;border:1px solid var(--line);background:var(--card2)}
.bank.good{border-color:rgba(61,220,132,.3)} .bank.bad{border-color:rgba(255,107,107,.3)}
.bank-head{font-size:13px;color:var(--ink2)}
.bank-num{font-family:Unbounded,sans-serif;font-weight:900;font-size:34px;margin:6px 0 4px;
  font-variant-numeric:tabular-nums}
.bank.good .bank-num{color:var(--good)} .bank.bad .bank-num{color:var(--bad)}
.bank-sub{font-size:13px;color:var(--ink2)}
.pnl{display:block;font-family:var(--mono);font-weight:800;font-size:13px;margin-top:3px}
.pnl.good{color:var(--good)} .pnl.bad{color:var(--bad)} .bank-note{font-size:11.5px;color:var(--ink3);margin-top:8px}

.last5{display:grid;gap:8px;margin-top:14px}
.bet{background:var(--card);border:1px solid var(--line);border-radius:13px;overflow:hidden}
.bet summary{cursor:pointer;list-style:none;padding:13px 15px;display:flex;align-items:center;
  justify-content:space-between;gap:12px;font-size:14px}
.bet summary::-webkit-details-marker{display:none}
.bet[open]{border-color:var(--line2)}
.b-left{display:flex;align-items:center;gap:10px;min-width:0}
.b-left .stars{flex:none}
.b-name{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.b-pick{color:var(--ink3);font-size:12.5px;white-space:nowrap}
.b-body{padding:0 15px 14px}
.b-body table{min-width:0}
.b-body td{padding:6px 0;border:0;font-size:13px;color:var(--ink2)}
.b-body td:first-child{color:var(--ink3);width:46%}

.empty{text-align:center;padding:36px 18px;color:var(--ink2);border:1px dashed var(--line2);
  border-radius:var(--r);background:rgba(18,18,24,.4)}
.empty-ico{font-size:30px;color:var(--ink3);margin-bottom:6px}
.empty p{margin:5px 0;max-width:56ch;margin-inline:auto;font-size:14.5px}
.empty.small{padding:20px;font-size:13.5px;text-align:left}

.honest{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px}
.honest .card h3{font-family:Unbounded,sans-serif;font-size:14px;margin:0 0 8px;text-transform:uppercase;
  letter-spacing:.04em}
.honest ul{margin:0;padding-left:18px;color:var(--ink2);font-size:14px;display:grid;gap:7px}
.honest .no h3{color:var(--bad)} .honest .yes h3{color:var(--good)}

footer{margin-top:56px;padding-top:22px;border-top:1px solid var(--line);color:var(--ink3);font-size:12.5px}
footer a{color:var(--ink2)}

/* Reveal-on-scroll: opacity+transform only, so it stays GPU-composited.
   Gated behind .js -- if the script never runs, nothing is left invisible. */
.js .reveal{opacity:0;transform:translateY(14px)}
.js .reveal.in{opacity:1;transform:none;transition:opacity .5s ease,transform .5s cubic-bezier(.2,.7,.3,1)}

@media (max-width:1000px){
  .hero{grid-template-columns:1fr;gap:22px}
  .kpis{grid-template-columns:repeat(3,1fr)}
  .bento,.strats,.honest{grid-template-columns:1fr}
  .stat-row{grid-template-columns:repeat(3,1fr)}
}
@media (max-width:560px){
  .kpis{grid-template-columns:repeat(2,1fr)}
  .wrap{padding:0 14px 70px}
  .nav a.link{display:none}
  /* The settled-bets row is one line on a phone and the event name needs it:
     the meter alone still ranks the rung, and the word stays in the title. */
  .b-left .st-lab{display:none}
  /* Stacking the word under the stars saves ~60px in the first column, which
     is exactly what the by-stars table needs to fit a phone outright instead
     of hiding its money column behind a sideways scroll nobody discovers. */
  .bd-card .stars{display:inline-block}
  .bd-card .st-lab{display:block;margin-top:5px}
  .breakdown{padding:14px 12px}
  .bd-card.key{padding:12px 11px 6px}
  table.bd{min-width:0;font-size:12px}
  table.bd th{font-size:10.5px}
}
@media (prefers-reduced-motion:reduce){
  *{animation:none!important;transition:none!important}
  .reveal{opacity:1;transform:none}
  .mesh{opacity:.35}
}
</style>
</head>
<body data-updated="$updated_iso" data-interval="$poll_interval">
<script>document.documentElement.className+=" js";</script>
<div class="mesh" aria-hidden="true"><i></i><i></i><i></i></div>

<nav class="nav">
  <div class="nav-in">
    <span class="mark">
      <svg viewBox="0 0 64 64" aria-hidden="true">
      <rect width="64" height="64" rx="16" fill="#08080b"/>
      <path d="M8 24h16l10 22" fill="none" stroke="#ff3d81" stroke-width="5"
            stroke-linecap="round" stroke-linejoin="round"/>
      <path d="M8 24h48" fill="none" stroke="#c8ff2e" stroke-width="5"
            stroke-linecap="round" opacity=".95"/>
      <circle cx="24" cy="24" r="6.5" fill="#c8ff2e"/>
      <circle cx="24" cy="24" r="2.6" fill="#08080b"/></svg>
      STEAM<span style="color:#c8ff2e">LINE</span>
    </span>
    <span class="sp"></span>
    <a class="link pm" href="#polymarket">Polymarket</a>
    <a class="link" href="#feed">Сигналы</a>
    <a class="link" href="#active">Открытые</a>
    <a class="link" href="#moves">Движения</a>
    <a class="link" href="#proof">Проверка</a>
    <a class="link" href="#how">Как это работает</a>
    <span class="pill $freshness_class"><span class="dot"></span>$freshness_label</span>
  </div>
</nav>
$ticker

<div class="wrap">

  <header class="hero">
    <div>
      <svg class="logo" viewBox="0 0 64 64" aria-label="STEAMLINE — трекер движения линии">
        <defs>
          <linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
            <stop offset="0" stop-color="#1a1a20"/><stop offset="1" stop-color="#08080b"/>
          </linearGradient>
          <linearGradient id="hold" x1="0" y1="0" x2="1" y2="0">
            <stop offset="0" stop-color="#c8ff2e"/><stop offset="1" stop-color="#8ff06a"/>
          </linearGradient>
        </defs>
        <rect x="1" y="1" width="62" height="62" rx="17" fill="url(#g)"
              stroke="rgba(200,255,46,.25)" stroke-width="1.5"/>
        <!-- the market: holds, then collapses -->
        <path d="M9 25h15l11 24" fill="none" stroke="#ff3d81" stroke-width="4.6"
              stroke-linecap="round" stroke-linejoin="round"/>
        <!-- the one price that has not moved yet: this is the bet -->
        <path d="M9 25h46" fill="none" stroke="url(#hold)" stroke-width="4.6"
              stroke-linecap="round"/>
        <!-- the moment between them -->
        <circle cx="24" cy="25" r="6.2" fill="#c8ff2e"/>
        <circle cx="24" cy="25" r="2.4" fill="#08080b"/>
      </svg>
      <div class="tag">STEAMLINE · видим деньги раньше рынка · $sports_n $sports_word</div>
      <h1>Ловим <em>деньги</em><br>раньше рынка</h1>
      <p class="sub">Когда на исход <b>заносят крупные деньги</b>, коэффициент проседает —
      но не у всех сразу. Мы ловим этот момент и показываем конторы,
      где <b>старая цена ещё стоит</b>.</p>
      <div class="cta">
        <a class="btn primary" href="#feed">Смотреть сигналы →</a>
        <a class="btn ghost" href="ledger.json">Сырой журнал (JSON)</a>
      </div>
    </div>

    <aside class="clock">
      <div class="cl-lab">Следующий срез рынка через</div>
      <div class="cl-time" id="cd">--:--</div>
      <div class="cd-bar"><i id="cdbar"></i></div>
      <div class="cl-meta">
        <span id="cdago">обновлено $updated_ago</span>
        <span><b>Опрос:</b> каждые $poll_interval мин$cadence_note</span>
        <span><b>Страница:</b> перевыпуск раз в $publish_interval мин · бот пишет сразу</span>
      </div>
    </aside>
  </header>

  <section class="kpis">
    <div class="kpi lime reveal"><b data-count="$cov_books">$cov_books_txt</b><span>$cov_books_word в опросе</span></div>
    <div class="kpi reveal"><b data-count="$cov_events">$cov_events_txt</b><span>$cov_events_word за $span_label</span></div>
    <div class="kpi reveal"><b data-count="$cov_lines">$cov_lines_txt</b><span>$cov_lines_word</span></div>
    <div class="kpi reveal"><b data-count="$cov_cycles">$cov_cycles_txt</b><span>$cov_cycles_word</span></div>
    <div class="kpi mag reveal"><b data-count="$cov_moves">$cov_moves_txt</b><span>$cov_moves_word от $threshold_pct%</span></div>
    <div class="kpi cy reveal"><b data-count="$cov_signals">$cov_signals_txt</b><span>$cov_signals_word</span></div>
  </section>
  <p class="kpi-note">Всё посчитано по тому, что реально легло в базу за $span_label — без оценок
  и множителей. Прямо сейчас открытых входов: <b>$hero_open</b>, из них на ступени
  «уверенно» (★★★) и выше: <b>$hero_stars</b> — остальные помечены «осторожно» и стоят тут
  на тех же правах, чтобы их можно было пересчитать.</p>
  $funnel_block

  <h2 id="feed"><span class="hash">#</span>Сигналы</h2>
  <p class="lead">Все сигналы за <b>последние 24 часа</b> — то же, что ушло в бот.
  Пришедшие в последнем срезе помечены <b>«только что»</b>, их можно отфильтровать
  кнопкой. Строка попадает сюда, даже если входа уже нет: в колонке «ставим» тогда
  стоит <b>⛔ закрыт</b>, и в открытые ставки такое событие не уходит — поэтому
  в двух блоках бывают разные числа.</p>
  $summaries_html

  <h2 id="active"><span class="hash">#</span>Открытые ставки</h2>
  <p class="lead">Сигналы, по которым матч ещё не начался — $active_n $active_word.
  Блок выше показывает только то, что шевельнулось в последнем срезе (это окно в
  $poll_interval мин, и чаще всего оно пустое). Здесь — всё, на чём мы сейчас стоим.</p>
  $active_signals

  <h2 id="moves"><span class="hash">#</span>Движения</h2>
  <p class="lead">Каждое падение от $threshold_pct%, которое мы поймали — включая те,
  где взять старую цену было уже негде. Колонка <b>«коэф. до падения»</b> — это цифра,
  которую мы бы забрали, если бы успевали всегда.</p>
  $movement_stats
  $movements_table

  <h2 id="how"><span class="hash">#</span>Как это работает</h2>
  <div class="bento">
    <div class="card how reveal">
      <ul>
        <li><i>01</i><span>Раз в $poll_interval минут снимаем линию у всех контор сразу —
          футбол, теннис, CS2, Dota&nbsp;2, LoL, настольный теннис.</span></li>
        <li><i>02</i><span>Ищем исход, у которого цена <b>упала</b>. Падение — это деньги.
          Противоположную сторону не трогаем никогда: она подорожала механически,
          просто потому что деньги пошли против неё.</span></li>
        <li><i>03</i><span>Считаем, <b>сколько независимых контор</b> просело за один срез.
          Одна — может быть чья-то разовая ставка или ошибка трейдера. Отсюда звёзды,
          и это же уровень доверия — ровно те метки, которыми помечена каждая строка выше:
          <ul class="rungs">
            <li><span class="stars s4"><span class="st" aria-hidden="true"><span class="trk">★★★★</span><span
              class="fill">★★★★</span></span><span class="st-lab">максимум</span></span>— от <b>$moved_4</b> контор</li>
            <li><span class="stars s3"><span class="st" aria-hidden="true"><span class="trk">★★★★</span><span
              class="fill">★★★</span></span><span class="st-lab">уверенно</span></span>— от <b>$moved_3</b> контор</li>
            <li><span class="stars s2"><span class="st" aria-hidden="true"><span class="trk">★★★★</span><span
              class="fill">★★</span></span><span class="st-lab">осторожно</span></span>— от <b>$moved_2</b> контор</li>
          </ul>
          Ширина рынка правит эту оценку, но мягко: если подвинулась заметно меньшая
          доля от всех, кто котирует событие, ступень опускается <b>ровно на одну</b> —
          и никогда больше. Шесть контор из пятидесяти — это ★★, а не ★★★: рынок
          среагировал ещё не весь, мы рано, поэтому осторожнее. Но это по-прежнему
          сигнал. До 18.08 доля могла перечеркнуть любое число подтверждений, и
          двенадцать контор из семидесяти уходили в корзину молча — это была ошибка,
          и она исправлена.
          Шарп-контора среди них добавляет звезду: такие двигают линию на деньгах,
          а не переписывая соседей. Одна звезда не публикуется вовсе.</span></li>
        <li><i>04</i><span>Публикуем <b>все три уровня</b>, а не только верхний, и считаем
          каждый отдельно — <b>одинаковой суммой</b>. Ставить на сильные сигналы больше
          выглядит логично, но убило бы измерение: прибыль показывала бы схему ставок,
          а не силу сигнала. Сначала честно меряем, потом решаем, кому давать больше.
          Таблица «по звёздам» ниже и есть этот ответ.</span></li>
        <li><i>05</i><span>Находим конторы, которые <b>ещё не подвинулись</b>, и показываем
          цену там. Это и есть ставка. Если не подвинулась ни одна — честно пишем,
          что вход закрыт.</span></li>
        <li><i>06</i><span>Если коэффициент выше $safe_trigger, отдельно считаем
          <b>безопасный вариант</b>: в футболе это двойной шанс, собранный из той же линии,
          в теннисе и киберспорте — фора.</span></li>
      </ul>
    </div>
    <div class="card">
      <h3 style="font-family:Unbounded,sans-serif;font-size:14px;margin:0 0 4px;text-transform:uppercase">Где чаще всего остаётся вход</h3>
      <p class="none">Конторы, которые медленнее всех переставляют цену — там, где ставка реально проходит.</p>
      $top_books
    </div>
  </div>

  <h2 id="proof"><span class="hash">#</span>Проверка сигналов</h2>
  <p class="lead">Обе стратегии считаются по <b>одним и тем же</b> сигналам: оптимальная —
  это просто подмножество с коэффициентом не выше $optimal_max. Так видно, стоит ли
  отсекать длинные коэффициенты, а не сравниваются две разные выборки.
  Здесь <b>только сыгравшие матчи</b>, у которых уже есть результат — ставки, которые
  ещё ждут своего матча, лежат выше, в «Открытых». Считаем по цене, которую называли.
  У двух стратегий бывают <b>разные исходы на одном матче</b>: прямая ставка может не
  зайти, а фора по тому же событию — зайти. Поэтому в каждой строке стоят оба вердикта.</p>
  $grading_line
  <div class="strats">
    $stats_aggressive
    $stats_optimal
  </div>

  $polymarket_section

  $breakdown_block

  $counterfactual_block

  <h3 style="font-family:Unbounded,sans-serif;font-size:15px;margin:26px 0 8px;text-transform:uppercase">Сыгравшие сигналы — $last_n</h3>
  <p class="lead small">Нажми на строку, чтобы раскрыть: обе цены, что ставила оптимальная,
  счёт матча и CLV.</p>
  $last_bets

  <div class="honest">
    <div class="card yes reveal">
      <h3>Что мы делаем</h3>
      <ul>
        <li>Показываем, куда уже зашли деньги, и где старая цена ещё стоит.</li>
        <li>Пишем и заходы, и промахи — журнал без промахов не стоит ничего.</li>
        <li>Отдаём <a href="ledger.json">сырой JSON</a> со всеми сигналами: можно скачать и пересчитать самому.</li>
        <li>Считаем CLV — успели ли мы взять цену до того, как её срезал весь рынок.</li>
      </ul>
    </div>
    <div class="card no reveal">
      <h3>Чего мы не делаем</h3>
      <ul>
        <li>Не предсказываем победителя. Мы видим движение денег, а не будущее.</li>
        <li>Не обещаем процент захода и не продаём «гарантированный профит».</li>
        <li>Не показываем скриншоты купонов вместо данных.</li>
        <li>Не прячем сигналы, которые не сыграли, и не подчищаем статистику.</li>
      </ul>
    </div>
  </div>

  <footer>
    Время везде UTC · порог алерта $threshold_pct% · опрос каждые $poll_interval мин ·
    страница перевыпускается раз в $publish_interval мин · $quota_note<a href="ledger.json">ledger.json</a><br>
    Это расчёт по движению рынка, а не рекомендация. Ставки — риск потерять деньги.
    Материал не адресован лицам младше 18 лет.
  </footer>
</div>
$scripts
</body>
</html>
""")


SCRIPTS = Template(r"""<script>
(function () {
  var reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------------------------------------------------------- countdown */
  /* The page is a static file regenerated every cycle, so without this the
     "обновлено только что" line silently goes stale while looking fresh.
     The next-poll time is rolled FORWARD past any polls that happened after
     this file was written, so the clock stays right even when the page is
     several minutes behind the data. */
  var cd = document.getElementById('cd'),
      bar = document.getElementById('cdbar'),
      ago = document.getElementById('cdago'),
      last = Date.parse(document.body.dataset.updated || ''),
      span = (parseInt(document.body.dataset.interval, 10) || 30) * 60000;

  function words(min) {
    if (min < 1) return 'обновлено только что';
    if (min < 60) return 'обновлено ' + min + ' мин назад';
    var h = Math.floor(min / 60);
    return 'обновлено ' + h + ' ч ' + (min % 60) + ' мин назад';
  }
  function tick() {
    if (!cd || isNaN(last)) return;
    var now = Date.now(), next = last + span;
    while (next <= now) next += span;
    var left = next - now, s = Math.floor(left / 1000);
    cd.textContent = Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    bar.style.width = (100 - (left / span) * 100).toFixed(1) + '%';
    ago.textContent = words(Math.floor((now - last) / 60000));
  }
  tick(); setInterval(tick, 1000);

  /* A tab left open all evening should not keep showing one old snapshot. */
  setTimeout(function () { location.reload(); }, 5 * 60000);

  /* ------------------------------------------------------------ filters */
  /* Scoped to the signal feed on purpose: 'tr.row' also matches the open-bets
     and movements tables, and an unscoped filter used to blank those too. */
  var buttons = document.querySelectorAll('.f'),
      rows = document.querySelectorAll('#feedtable tr.row'),
      norows = document.getElementById('norows');
  function apply(mode) {
    var shown = 0;
    rows.forEach(function (r) {
      var ok;
      if (mode === 'all') ok = true;
      else if (mode === 'fresh') ok = r.dataset.fresh === '1';
      else if (mode === 'open') ok = r.dataset.open === '1';
      else if (mode === 'opt') ok = r.dataset.strat === 'optimal';
      else ok = r.dataset.stars === mode;
      r.hidden = !ok;
      if (ok) shown++;
    });
    if (norows) norows.hidden = shown > 0;
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      buttons.forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      apply(b.dataset.f);
    });
  });

  /* ------------------------------------------------------------ reveals */
  var targets = document.querySelectorAll('.reveal');
  if (reduce || !('IntersectionObserver' in window)) {
    targets.forEach(function (t) { t.classList.add('in'); });
  } else {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e, i) {
        if (!e.isIntersecting) return;
        setTimeout(function () { e.target.classList.add('in'); }, Math.min(i * 22, 180));
        io.unobserve(e.target);
      });
    }, { rootMargin: '0px 0px -40px 0px' });
    targets.forEach(function (t) { io.observe(t); });
    /* Failsafe: nothing on this page is allowed to stay invisible because an
       observer misfired, a tab was restored in the background, or the page was
       printed. After a few seconds everything shows regardless. */
    setTimeout(function () {
      targets.forEach(function (t) { t.classList.add('in'); });
    }, 2500);
  }

  /* ----------------------------------------------------------- count-up */
  /* Not decoration: a figure that animates from zero every time the file is
     rebuilt is visible proof the page really was regenerated. */
  function countUp(el) {
    var end = parseFloat(el.dataset.count || '0'), prefix = el.dataset.prefix || '';
    if (reduce || !isFinite(end)) { return; }
    var dur = 900, t0 = performance.now(), neg = end < 0, abs = Math.abs(end);
    function fmt(v) {
      return prefix + Math.round(v).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ' ');
    }
    function frame(t) {
      var p = Math.min(1, (t - t0) / dur), e = 1 - Math.pow(1 - p, 3);
      el.textContent = (prefix ? '' : (neg ? '−' : '')) + fmt(abs * e);
      if (p < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }
  var counted = new WeakSet();
  var numbers = document.querySelectorAll('[data-count]');
  if ('IntersectionObserver' in window) {
    var io2 = new IntersectionObserver(function (es) {
      es.forEach(function (e) {
        if (e.isIntersecting && !counted.has(e.target)) { counted.add(e.target); countUp(e.target); }
      });
    });
    numbers.forEach(function (n) { io2.observe(n); });
  } else {
    numbers.forEach(countUp);
  }

  /* ------------------------------------------------- expandable counters */
  document.querySelectorAll('.stat-btn').forEach(function (b) {
    b.addEventListener('click', function () {
      var box = document.getElementById(b.dataset.open);
      if (!box) return;
      box.hidden = !box.hidden;
      b.setAttribute('aria-expanded', box.hidden ? 'false' : 'true');
    });
  });


  /* --------------------------------------------------- time to kick-off */
  /* Server-rendered text is right at publication time; this keeps it right
     afterwards. Under an hour the badge turns amber, because that is when
     "still open" stops being a comfortable assumption. */
  var cds = document.querySelectorAll('.cd-to[data-start]');
  function words(sec) {
    var m = Math.floor(sec / 60);
    if (m < 60) return 'через ' + m + ' мин';
    var h = Math.floor(m / 60); m = m % 60;
    if (h < 24) return 'через ' + h + ' ч ' + String(m).padStart(2, '0') + ' мин';
    var d = Math.floor(h / 24); h = h % 24;
    var word = d % 10 === 1 && d % 100 !== 11 ? 'день'
             : (d % 10 >= 2 && d % 10 <= 4 && !(d % 100 >= 12 && d % 100 <= 14)) ? 'дня' : 'дней';
    return 'через ' + d + ' ' + word + ' ' + h + ' ч';
  }
  function ticks() {
    var now = Date.now();
    cds.forEach(function (el) {
      var t = Date.parse(el.dataset.start || '');
      if (isNaN(t)) return;
      var left = (t - now) / 1000;
      if (left <= 0) { el.textContent = 'матч идёт'; el.className = 'cd-to live'; return; }
      el.textContent = words(left);
      el.className = 'cd-to' + (left < 3600 ? ' soon' : '');
    });
  }
  if (cds.length) { ticks(); setInterval(ticks, 1000); }

  /* The audio intro that used to live here was removed on request. Nothing
     on this page makes noise now, and nothing should: a betting site that
     starts talking at you reads as spam, however good the joke was. */

})();
</script>
""")


def render_dashboard(summaries: list, quota: dict = None):
    summaries = summaries or []
    meta = storage.snapshot_meta()
    cov = storage.coverage_stats(24)
    aggressive = storage.alert_stats("prematch", "aggressive")
    optimal = storage.alert_stats("prematch", "optimal")
    active = storage.active_signals(40)
    # Only matches that are actually over. "Проверка сигналов" is the
    # track record; a pending bet there is not proof of anything, and it
    # already has its own block in "Открытые ставки".
    last_bets = storage.recent_bets(10, "prematch", resolved_only=True)

    now = datetime.now(timezone.utc)
    fetched = _parse_iso(meta.get("fetched_at"))
    # More than two publish cycles without a refresh means the scheduler is
    # stuck. Say so rather than showing a green "в эфире" badge over stale data
    # -- a live badge that lies is worse than no badge.
    fresh = bool(fetched and (now - fetched) < timedelta(minutes=PUBLISH_INTERVAL_MINUTES * 2 + 5))

    span = cov.get("span_hours") or 24
    span_label = f"{span} {_hours_word(span)}"

    _LIVE.clear()
    _LIVE.update(storage.live_scores_map())
    _FINAL.clear()
    _FINAL.update(storage.final_scores_map())

    _write_ledger(aggressive)

    # Printed on the page, not just in the CI log. The credit balance decides
    # how wide the line and how fast the cadence can be, and reading it used
    # to mean opening a workflow run by hand -- which is exactly the sort of
    # number that quietly goes unwatched until the API stops answering.
    q = quota or {}
    quota_note = ""
    if q.get("remaining") is not None:
        # The burn rate must be built from the width we are ACTUALLY running
        # at, not from MAX_SPORTS_PER_CYCLE. Since 2026-08-15 that constant is
        # an ambition the credit governor clamps, so reading it here would
        # divide the balance by several times the real spend and tell the
        # reader the plan had hours left when it had days. The dashboard's one
        # job is that its numbers can be recounted.
        import budget as _budget
        plan = _budget.LAST_PLAN or _budget.plan(q.get("remaining"))
        width = plan.get("sports") or MAX_SPORTS_PER_CYCLE
        per_sport = plan.get("credits_per_sport") or 1
        per_day = (24 * 60 / max(1, POLL_INTERVAL_MINUTES)) * width * per_sport
        usable = max(0, int(q["remaining"]) - (plan.get("reserve") or 0))
        days = usable / per_day if per_day else 0
        starved = " — охват на минимуме, план пора расширять" if plan.get("starved") else ""
        quota_note = (f"кредитов осталось {int(q['remaining']):,} "
                      f"(≈{days:.0f} дн. при текущем темпе){starved} · "
                      f"{width} лиг за цикл по {per_sport} кр. · ").replace(",", " ")

    html_out = PAGE.safe_substitute(
        quota_note=quota_note,
        updated_iso=(fetched or now).isoformat(),
        updated_ago=_ago(meta.get("fetched_at"), now),
        freshness_class="live" if fresh else "stale",
        freshness_label="в эфире" if fresh else "данные устарели",
        poll_interval=POLL_INTERVAL_MINUTES,
        publish_interval=PUBLISH_INTERVAL_MINUTES,
        cadence_note=(f" · {html.escape(CADENCE_LABEL)}" if CADENCE_LABEL else ""),
        threshold_pct=f"{SPIKE_THRESHOLD_PCT * 100:.0f}",
        optimal_max=f"{OPTIMAL_MAX_PRICE:g}",
        safe_trigger=f"{SAFE_TRIGGER_PRICE:g}",
        span_label=span_label,
        sports_n=cov.get("sports") or 0,
        sports_word=_plural(cov.get("sports") or 0, "дисциплина", "дисциплины", "дисциплин"),
        hero_open=sum(1 for s in summaries if s.get("has_entry")),
        hero_stars=sum(1 for s in summaries if s.get("stars", 0) >= 3),
        moved_2=MOVED_FOR_2_STARS,
        moved_3=MOVED_FOR_3_STARS,
        moved_4=MOVED_FOR_4_STARS,
        # The formatted value is rendered server-side and the count-up merely
        # animates up to it -- so a browser that never runs the script still
        # shows the real figure instead of a row of zeroes.
        cov_books=cov["books"], cov_books_txt=_num(cov["books"]),
        cov_events=cov["events"], cov_events_txt=_num(cov["events"]),
        cov_lines=cov["lines"], cov_lines_txt=_num(cov["lines"]),
        cov_cycles=cov["cycles"], cov_cycles_txt=_num(cov["cycles"]),
        cov_moves=cov["moves"], cov_moves_txt=_num(cov["moves"]),
        cov_signals=cov["signals"], cov_signals_txt=_num(cov["signals"]),
        # 2026-08-15. The six labels under the headline numbers were fixed
        # strings in the plural-many form, so they only read correctly for
        # counts of five and up. On a reset book -- exactly the state the site
        # is in tonight -- they came out as "101 событий", "2 срезов рынка",
        # "32 841 котировок сверено". Every one of those is wrong Russian, and
        # on a page whose whole pitch is that the numbers can be checked, a
        # number that does not agree with its own noun reads as carelessness
        # about the numbers themselves.
        cov_books_word=_plural(cov["books"], "контора", "конторы", "контор"),
        cov_events_word=_plural(cov["events"], "событие", "события", "событий"),
        cov_lines_word=_plural(cov["lines"], "котировка сверена",
                               "котировки сверены", "котировок сверено"),
        cov_cycles_word=_plural(cov["cycles"], "срез рынка", "среза рынка",
                                "срезов рынка"),
        cov_moves_word=_plural(cov["moves"], "движение", "движения", "движений"),
        cov_signals_word=_plural(cov["signals"], "сигнал со входом",
                                 "сигнала со входом", "сигналов со входом"),
        funnel_block=_funnel_block(storage.funnel_stats(24), span_label),
        ticker=_ticker(summaries),
        summaries_html=_summaries_html(summaries, storage.recent_signals(24)),
        top_books=_top_books(storage.top_books(10)),
        grading_line=_grading_line(),
        stats_aggressive=_strategy_card(
            aggressive, "Агрессивная",
            "Все сигналы подряд, какой бы ни был коэффициент.", "agg",
            storage.recent_bets(5, "prematch", "aggressive")),
        stats_optimal=_strategy_card(
            optimal, "Оптимальная",
            f"До {OPTIMAL_MAX_PRICE:g} — та же ставка. Выше — вход мягче: двойной шанс в футболе, фора там, где ничьей нет.", "opt",
            storage.recent_bets(5, "prematch", "optimal")),
        breakdown_block=_breakdown_block(storage.breakdown_stats("prematch")),
        counterfactual_block=_pm_counterfactual_block(storage.pm_counterfactual()),
        polymarket_section=_pm_section(),
        movements_table=_movements_table(storage.recent_movements(30)),
        movement_stats=_movement_stats(storage.movement_stats()),
        active_signals=_active_signals(active),
        active_n=len(active),
        active_word=_plural(len(active), "ставка", "ставки", "ставок"),
        # Spelled out rather than left implicit: the list below is capped at 10
        # while the counter above shows every signal ever logged, and without
        # both numbers on screen that looks like a bug.
        last_n=len(last_bets),
        total_n=aggressive["total"],
        last_bets=_last_bets(last_bets, 10),
        scripts=SCRIPTS.template,
    )
    # git does not track empty directories, so a fresh CI checkout has no
    # dashboard/ folder yet -- make sure it exists before writing.
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return DASHBOARD_PATH
