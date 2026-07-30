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
import os
from datetime import datetime, timedelta, timezone
from string import Template

import storage
from config import (
    DASHBOARD_PATH,
    POLL_INTERVAL_MINUTES,
    PUBLISH_INTERVAL_MINUTES,
    CADENCE_LABEL,
    SPIKE_THRESHOLD_PCT,
    OPTIMAL_MAX_PRICE,
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
# feed
# --------------------------------------------------------------------------

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
        tags.append(f"<span class='tag safe' title='{html.escape(opt.get('note') or '')}'>"
                    f"🛡 ОПТИМАЛЬНАЯ — {html.escape(opt['pick'])[:44]}</span>")

    return (
        # Deliberately NOT a .reveal element: the feed is the one thing on the
        # page that must render even if the script never runs, so it is never
        # hidden behind an animation.
        f"<tr class='row' data-stars='{stars}' data-open='{1 if has_entry else 0}' "
        f"data-strat='{strategy}'>"
        f"<td class='c-stars'>{'★' * stars}<span class='sr'>{stars} из 3</span></td>"
        f"<td class='c-ev'><b>{name}</b><small>{_fmt_start(s.get('start_time'))} UTC</small></td>"
        f"<td class='c-out'>{outcome}<div class='tags'>{''.join(tags)}</div></td>"
        f"<td class='c-move'><span class='old'>{bet['old_price']:.2f}</span>"
        f"<span class='arr'>→</span><span class='new'>{bet['new_price']:.2f}</span>"
        f"<span class='pct'>−{abs(bet['drop_pct']):.0f}%</span></td>"
        f"<td class='c-books'>{bet['down_count']}<span class='of'>/{bet['books_count']}</span></td>"
        f"{bet_cell}</tr>"
    )


def _summaries_html(summaries: list, limit: int = 120) -> str:
    shown = [s for s in summaries if s.get("bet")][:limit]
    if not shown:
        return ("<div class='empty'><div class='empty-ico'>◎</div>"
                "<p><b>Сейчас рынок стоит.</b></p>"
                "<p>Ни одного падения от "
                f"{SPIKE_THRESHOLD_PCT * 100:.0f}% за последний срез. "
                "Пустая страница здесь — это честный ответ, а не поломка: "
                "мы не придумываем сигналы, чтобы заполнить место.</p></div>")

    n3 = sum(1 for s in shown if s["stars"] >= 3)
    n2 = sum(1 for s in shown if s["stars"] == 2)
    n1 = sum(1 for s in shown if s["stars"] == 1)
    nopen = sum(1 for s in shown if s.get("has_entry"))
    nopt = sum(1 for s in shown if s.get("strategy") == "optimal")

    filters = (
        "<div class='toolbar'>"
        f"<button class='f active' data-f='all'>Все<span class='n'>{len(shown)}</span></button>"
        f"<button class='f' data-f='opt'>Оптимальная<span class='n'>{nopt}</span></button>"
        f"<button class='f' data-f='3'>★★★<span class='n'>{n3}</span></button>"
        f"<button class='f' data-f='2'>★★<span class='n'>{n2}</span></button>"
        f"<button class='f' data-f='1'>★<span class='n'>{n1}</span></button>"
        f"<button class='f' data-f='open'>Есть вход<span class='n'>{nopen}</span></button>"
        "</div>"
    )

    head = ("<tr><th><span class='sr'>Звёзды</span></th><th>Событие</th><th>Деньги зашли на</th>"
            "<th>Был → стал</th><th>Контор</th><th>Ставим</th></tr>")
    body = "".join(_event_row(s) for s in shown)
    table = f"<div class='feed-wrap'><table class='feed'>{head}{body}</table></div>"
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
        tag = ("<span class='tag opt'>ОПТИМАЛЬНАЯ</span>"
               if r["strategy"] == "optimal" else "<span class='tag agg'>АГРЕССИВНАЯ</span>")
        safe = ""
        if r["safe_pick"]:
            price = f" {r['safe_price']:.2f}" if r["safe_price"] else ""
            safe = f"<span class='tag safe'>🛡{html.escape(str(r['safe_pick']))[:34]}{price}</span>"
        items.append(
            f"<tr class='row'><td class='c-stars'>{'★' * (r['stars'] or 0)}</td>"
            f"<td class='c-ev'><b>{html.escape(event)}</b>"
            f"<small>старт {_fmt_start(r['start_time'])} UTC</small></td>"
            f"<td class='c-out'>{html.escape(r['outcome_name'] or '')}"
            f"<div class='tags'>{tag}{safe}</div></td>"
            f"<td class='c-move'><span class='old'>{old_p}</span><span class='arr'>→</span>"
            f"<span class='new'>{new_p}</span></td>"
            f"<td class='c-books'>{r['down_count'] or 0}<span class='of'>/{r['books_count'] or 0}</span></td>"
            f"<td class='c-bet'><span class='price'>{entry}</span>"
            f"<small>{html.escape(r['entry_book'] or '')}</small></td></tr>"
        )
    return ("<div class='feed-wrap'><table class='feed'>"
            "<tr><th></th><th>Событие</th><th>Ставим на</th><th>Был → стал</th>"
            "<th>Контор</th><th>Взяли по</th></tr>" + "".join(items) + "</table></div>")


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
        f"<div class='bank-sub'>За {n} {_plural(n, 'сыгравшую ставку', 'сыгравшие ставки', 'сыгравших ставок')} "
        f"вы бы {word} <b>{sign}${abs(profit):,.0f}</b>. "
        f"Оборот ${staked:,.0f}, доходность {roi:+.1f}%.</div>"
        f"<div class='bank-note'>Считается по уже сыгравшим сигналам и по той цене, "
        f"которую мы называли. Это не обещание будущего результата.</div>"
        f"</div>"
    ).replace(",", " ")


def _mini_signals(rows) -> str:
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
        entry = f"{r['entry_price']:.2f}" if r["entry_price"] else "—"
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
            f"<small>{html.escape(r['outcome_name'] or '')} · {old_p} → {new_p} · "
            f"взяли {entry} у {html.escape(r['entry_book'] or '—')}</small></span>{st}</li>"
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
        <div class="stat"><button class="stat-btn" type="button" data-open="sig-{cls}"
             aria-expanded="false" aria-controls="sig-{cls}"
             title="Показать последние сигналы">{stats['total']}</button><span>сигналов ▾</span></div>
        <div class="stat"><b>{stats['resolved']}</b><span>проверено</span></div>
        <div class="stat"><b>{stats['pending']}</b><span>ждут матча</span></div>
        <div class="stat"><b>{win_rate_html}</b><span>заходимость</span></div>
        <div class="stat"><b>{avg_clv_html}</b><span>средний CLV</span></div>
      </div>
      {_unverifiable_note(stats)}
      <div class="sig-list" id="sig-{cls}" hidden>
        <div class="sig-cap">Последние сигналы этой стратегии</div>
        {_mini_signals(recent)}
      </div>
      {_bankroll_block(stats)}
    </div>
    """


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
            f"<tr><td class='c-stars'>{'★' * (r['stars'] or 0)}</td>"
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


def _last_bets(bets, limit: int = 6) -> str:
    if not bets:
        return "<p class='empty small'>Ставок пока нет — появятся с первым сигналом.</p>"
    items = []
    for b in bets[:limit]:
        home, away = b["home_team"], b["away_team"]
        event = f"{home} — {away}" if home and away else str(b["fixture_id"])
        stars = "★" * (b["stars"] or 0)
        entry = f"{b['entry_price']:.2f}" if b["entry_price"] else "—"
        if b["resolved"]:
            status = {"hit": "<span class='hit'>✅ зашла</span>",
                      "miss": "<span class='miss'>❌ не зашла</span>",
                      "n/a": "<span class='pending'>— н/д</span>"}.get(
                          b["result"], f"<span class='pending'>{html.escape(str(b['result']))}</span>")
        else:
            status = "<span class='pending'>⏳ ждём матч</span>"
        clv = f"{b['clv_pct'] * 100:+.1f}%" if b["clv_pct"] is not None else "—"
        old_p = f"{b['old_price']:.2f}" if b["old_price"] else "—"
        new_p = f"{b['new_price']:.2f}" if b["new_price"] else "—"
        items.append(
            "<details class='bet'><summary>"
            f"<span class='b-left'><span class='c-stars'>{stars}</span>"
            f"<span class='b-name'>{html.escape(event)}</span>"
            f"<span class='b-pick'>{html.escape(b['outcome_name'] or '')} @ {entry}</span></span>"
            f"{status}</summary>"
            "<div class='b-body'><table>"
            f"<tr><td>Ставили на</td><td><b>{html.escape(b['outcome_name'] or '')}</b></td></tr>"
            f"<tr><td>Коэффициент был</td><td class='mono'>{old_p}</td></tr>"
            f"<tr><td>Просел до</td><td class='mono'>{new_p}</td></tr>"
            f"<tr><td>Поставили по</td><td class='mono'><b>{entry}</b> — "
            f"{html.escape(b['entry_book'] or '')}</td></tr>"
            f"<tr><td>Просело у контор</td><td class='mono'>{b['down_count'] or 0} "
            f"из {b['books_count'] or 0}</td></tr>"
            f"<tr><td>Старт матча</td><td class='mono'>{_fmt_dt(b['start_time'])}</td></tr>"
            f"<tr><td>Сигнал зафиксирован</td><td class='mono'>{_fmt_dt(b['detected_at'])}</td></tr>"
            f"<tr><td>Результат</td><td>{status}</td></tr>"
            f"<tr><td>CLV</td><td class='mono'>{clv}</td></tr>"
            "</table></div></details>"
        )
    return "<div class='last5'>" + "".join(items) + "</div>"


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
        "count": len(rows),
        "signals": rows,
    }
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)


# --------------------------------------------------------------------------
# page
# --------------------------------------------------------------------------

PAGE = Template(r"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KEWA · VILKA · TRACKER — ловим движение коэффициентов</title>
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
.pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;
  padding:6px 11px;border-radius:999px;border:1px solid var(--line2);white-space:nowrap}
.pill.live{color:var(--lime);border-color:rgba(200,255,46,.35);background:rgba(200,255,46,.07)}
.pill.stale{color:var(--warn);border-color:rgba(255,197,49,.35);background:rgba(255,197,49,.07)}
.pill .dot{width:7px;height:7px;border-radius:50%;background:currentColor;animation:beat 1.9s infinite}
@keyframes beat{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.7)}}
#snd{cursor:pointer;background:none;font:inherit;font-size:12px;font-weight:700;color:var(--ink2);
  border:1px solid var(--line2);border-radius:999px;padding:6px 11px}
#snd:hover{color:var(--lime);border-color:rgba(200,255,46,.4)}
#snd.on{color:var(--lime);border-color:rgba(200,255,46,.5);background:rgba(200,255,46,.08)}

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
.books{list-style:none;margin:10px 0 0;padding:0;display:grid;gap:8px}
.books li{display:grid;grid-template-columns:20px 1fr 64px 30px;align-items:center;gap:9px;font-size:13.5px}
.books .rk{color:var(--ink3);font-family:var(--mono);font-size:12px}
.books .bk{color:var(--ink);font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.books .bar{height:6px;background:var(--line);border-radius:99px;overflow:hidden}
.books .bar i{display:block;height:100%;background:linear-gradient(90deg,var(--lime),var(--cy))}
.books .ct{text-align:right;font-family:var(--mono);color:var(--ink2);font-size:12.5px}
.none,.kpi-note{color:var(--ink3);font-size:13px}

/* ------------------------------------------------------------------ feed */
.toolbar{display:flex;flex-wrap:wrap;gap:8px;margin:0 0 14px}
.f{cursor:pointer;font:inherit;font-size:13px;font-weight:600;color:var(--ink2);
  background:var(--card);border:1px solid var(--line);border-radius:999px;padding:8px 13px;
  display:inline-flex;align-items:center;gap:7px;transition:.16s}
.f:hover{border-color:var(--line2);color:var(--ink)}
.f.active{background:var(--lime);color:#0b0b06;border-color:var(--lime)}
.f .n{font-family:var(--mono);font-size:11px;opacity:.75}
.feed-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:var(--r);background:var(--card)}
table{border-collapse:collapse;width:100%;min-width:720px}
th{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--ink3);font-weight:700;
  text-align:left;padding:13px 14px;border-bottom:1px solid var(--line);background:var(--card2)}
td{padding:13px 14px;border-bottom:1px solid var(--line);vertical-align:middle;font-size:14.5px}
tr:last-child td{border-bottom:0}
tbody tr:hover td,table tr.row:hover td{background:rgba(255,255,255,.022)}
.c-stars{color:var(--warn);white-space:nowrap;letter-spacing:1px;font-size:13px}
.c-ev b{display:block;font-weight:600}
.c-ev small{display:block;color:var(--ink3);font-size:11.5px;font-family:var(--mono)}
.c-out{font-weight:600}
.tags{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px}
.tag{font-size:9.5px;font-weight:800;letter-spacing:.09em;padding:3px 7px;border-radius:6px;
  text-transform:uppercase;white-space:nowrap}
.tag.opt{background:rgba(61,220,132,.13);color:var(--good)}
.tag.agg{background:rgba(255,61,129,.13);color:var(--mag)}
.tag.safe{background:rgba(74,217,255,.13);color:var(--cy)}
.c-move{white-space:nowrap;font-family:var(--mono);font-size:13.5px}
.c-move .old{color:var(--ink3);text-decoration:line-through}
.c-move .arr{color:var(--ink3);margin:0 5px}
.c-move .new{color:var(--ink);font-weight:700}
.c-move .pct{color:var(--mag);font-weight:700;margin-left:8px}
.c-books{font-family:var(--mono);color:var(--ink);font-weight:700}
.c-books .of{color:var(--ink3);font-weight:400}
.c-bet .price{display:block;font-family:Unbounded,sans-serif;font-weight:800;font-size:19px;color:var(--lime)}
.c-bet small{display:block;color:var(--ink3);font-size:11.5px}
.chip.shut{color:var(--ink3);font-size:12px;font-weight:600}
.mono{font-family:var(--mono);font-size:13px}
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
.ms-ev small{display:block;color:var(--ink3);font-size:11.5px;font-family:var(--mono)}
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
.bank-sub{font-size:13px;color:var(--ink2)} .bank-note{font-size:11.5px;color:var(--ink3);margin-top:8px}

.last5{display:grid;gap:8px;margin-top:14px}
.bet{background:var(--card);border:1px solid var(--line);border-radius:13px;overflow:hidden}
.bet summary{cursor:pointer;list-style:none;padding:13px 15px;display:flex;align-items:center;
  justify-content:space-between;gap:12px;font-size:14px}
.bet summary::-webkit-details-marker{display:none}
.bet[open]{border-color:var(--line2)}
.b-left{display:flex;align-items:center;gap:10px;min-width:0}
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
      <svg viewBox="0 0 64 64" aria-hidden="true"><rect width="64" height="64" rx="16" fill="#c8ff2e"/>
      <path d="M20 14v14M27 12v16M34 14v14M18 28h18a2 2 0 012 2v2a11 11 0 01-8 10v12a3 3 0 01-6 0V42a11 11 0 01-8-10v-2a2 2 0 012-2z" fill="none" stroke="#08080b" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
      KEWA<span style="color:#c8ff2e">/</span>VILKA
    </span>
    <span class="sp"></span>
    <a class="link" href="#feed">Сигналы</a>
    <a class="link" href="#active">Открытые</a>
    <a class="link" href="#proof">Проверка</a>
    <a class="link" href="#how">Как это работает</a>
    <span class="pill $freshness_class"><span class="dot"></span>$freshness_label</span>
    <button id="snd" type="button" aria-pressed="false" title="Короткая заставка со звуком">♪ звук</button>
  </div>
</nav>
$ticker

<div class="wrap">

  <header class="hero">
    <div>
      <svg class="logo" viewBox="0 0 64 64" aria-label="Логотип KEWA Vilka Tracker">
        <defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stop-color="#c8ff2e"/><stop offset=".55" stop-color="#8ff06a"/>
          <stop offset="1" stop-color="#4ad9ff"/></linearGradient></defs>
        <rect x="1" y="1" width="62" height="62" rx="17" fill="url(#g)"/>
        <g stroke="#08080b" stroke-width="3.2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M20 15v11" fill="none"/><path d="M27 13v13" fill="none"/><path d="M34 15v11" fill="none"/>
          <path d="M18 26h18a2 2 0 012 2v2a11 11 0 01-8 10v10a3 3 0 01-6 0V40a11 11 0 01-8-10v-2a2 2 0 012-2z" fill="#08080b"/>
        </g>
        <circle cx="23.5" cy="32" r="2.4" fill="#c8ff2e"/><circle cx="31.5" cy="32" r="2.4" fill="#c8ff2e"/>
        <path d="M23 37.6c1.7 2 6.3 2 8 0" fill="none" stroke="#c8ff2e" stroke-width="2.2" stroke-linecap="round"/>
        <path d="M49 15l-9 14h6l-3 12 10-15h-6z" fill="#ff3d81" stroke="#08080b" stroke-width="2.4" stroke-linejoin="round"/>
      </svg>
      <div class="tag">трекер движения линии · $sports_n $sports_word</div>
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
    <div class="kpi lime reveal"><b data-count="$cov_books">$cov_books_txt</b><span>контор в опросе</span></div>
    <div class="kpi reveal"><b data-count="$cov_events">$cov_events_txt</b><span>событий за $span_label</span></div>
    <div class="kpi reveal"><b data-count="$cov_lines">$cov_lines_txt</b><span>котировок сверено</span></div>
    <div class="kpi reveal"><b data-count="$cov_cycles">$cov_cycles_txt</b><span>срезов рынка</span></div>
    <div class="kpi mag reveal"><b data-count="$cov_moves">$cov_moves_txt</b><span>движений от $threshold_pct%</span></div>
    <div class="kpi cy reveal"><b data-count="$cov_signals">$cov_signals_txt</b><span>сигналов со входом</span></div>
  </section>
  <p class="kpi-note">Всё посчитано по тому, что реально легло в базу за $span_label — без оценок
  и множителей. Прямо сейчас открытых входов: <b>$hero_open</b>, из них на три звезды: <b>$hero_stars</b>.</p>

  <h2 id="feed"><span class="hash">#</span>Сигналы</h2>
  <p class="lead">Одна строка — одно событие. «Был → стал» это цена до денег и после,
  «конторы» — сколько из них уже подвинулось, «ставим» — где старую цену ещё дают.</p>
  $summaries_html

  <h2 id="active"><span class="hash">#</span>Открытые ставки</h2>
  <p class="lead">Сигналы, по которым матч ещё не начался — $active_n $active_word.
  Блок выше показывает только то, что шевельнулось в последнем срезе (это окно в
  $poll_interval мин, и чаще всего оно пустое). Здесь — всё, на чём мы сейчас стоим.</p>
  $active_signals

  <h2 id="how"><span class="hash">#</span>Как это работает</h2>
  <div class="bento">
    <div class="card how reveal">
      <ul>
        <li><i>01</i><span>Раз в $poll_interval минут снимаем линию у всех контор сразу —
          футбол, теннис, CS2, Dota&nbsp;2, LoL, настольный теннис.</span></li>
        <li><i>02</i><span>Ищем исход, у которого цена <b>упала</b>. Падение — это деньги.
          Противоположную сторону не трогаем никогда: она подорожала механически,
          просто потому что деньги пошли против неё.</span></li>
        <li><i>03</i><span>Считаем, <b>у скольких контор</b> просело. Одна контора — может быть
          чья-то разовая ставка или ошибка трейдера. Много независимых контор за один
          срез — это уже информированные деньги. Отсюда звёзды.</span></li>
        <li><i>04</i><span>Находим конторы, которые <b>ещё не подвинулись</b>, и показываем
          цену там. Это и есть ставка. Если не подвинулась ни одна — честно пишем,
          что вход закрыт.</span></li>
        <li><i>05</i><span>Если коэффициент выше $safe_trigger, отдельно считаем
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
  Всё считается по цене, которую мы называли, и по уже сыгравшим матчам.</p>
  <div class="strats">
    $stats_aggressive
    $stats_optimal
  </div>

  <h3 style="font-family:Unbounded,sans-serif;font-size:15px;margin:26px 0 8px;text-transform:uppercase">Сыгравшие сигналы</h3>
  $resolved_table

  <h3 style="font-family:Unbounded,sans-serif;font-size:15px;margin:26px 0 8px;text-transform:uppercase">Последние сигналы — $last_n из $total_n</h3>
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
    страница перевыпускается раз в $publish_interval мин · <a href="ledger.json">ledger.json</a><br>
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
  var buttons = document.querySelectorAll('.f'),
      rows = document.querySelectorAll('tr.row'),
      norows = document.getElementById('norows');
  function apply(mode) {
    var shown = 0;
    rows.forEach(function (r) {
      var ok;
      if (mode === 'all') ok = true;
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

  /* -------------------------------------------------------------- sound */
  /* Browsers will not let any page make unmuted sound without a gesture --
     that is policy, not a bug, and trying to fight it is exactly the kind of
     thing that makes a site feel like malware. So: the sting is armed on the
     first tap anywhere, and there is an always-visible toggle to disarm it.
     Everything is synthesised in the browser -- oscillators for the sting,
     speechSynthesis for the line -- so the page ships no audio files at all. */
  var btn = document.getElementById('snd'), armed = true, played = false;

  function sting() {
    if (played) return;
    played = true;
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) return;
      var ctx = new AC();
      if (ctx.state === 'suspended') ctx.resume();
      var t = ctx.currentTime + 0.02;

      /* sub drop */
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.type = 'sine'; o.frequency.setValueAtTime(150, t);
      o.frequency.exponentialRampToValueAtTime(38, t + 1.1);
      g.gain.setValueAtTime(0.0001, t);
      g.gain.exponentialRampToValueAtTime(0.5, t + 0.05);
      g.gain.exponentialRampToValueAtTime(0.0001, t + 1.5);
      o.connect(g).connect(ctx.destination); o.start(t); o.stop(t + 1.6);

      /* two detuned stabs */
      [0, 0.001].forEach(function (d, i) {
        var s = ctx.createOscillator(), sg = ctx.createGain(),
            f = ctx.createBiquadFilter();
        s.type = 'sawtooth';
        s.frequency.setValueAtTime(110 * (1 + d) * (i ? 1.5 : 1), t + 0.06);
        f.type = 'lowpass';
        f.frequency.setValueAtTime(400, t + 0.06);
        f.frequency.exponentialRampToValueAtTime(2600, t + 0.5);
        sg.gain.setValueAtTime(0.0001, t + 0.06);
        sg.gain.exponentialRampToValueAtTime(0.11, t + 0.14);
        sg.gain.exponentialRampToValueAtTime(0.0001, t + 1.0);
        s.connect(f).connect(sg).connect(ctx.destination);
        s.start(t + 0.06); s.stop(t + 1.1);
      });

      /* noise riser */
      var len = Math.floor(ctx.sampleRate * 0.9),
          buf = ctx.createBuffer(1, len, ctx.sampleRate), ch = buf.getChannelData(0);
      for (var i = 0; i < len; i++) ch[i] = (Math.random() * 2 - 1) * (i / len);
      var n = ctx.createBufferSource(), ng = ctx.createGain(), nf = ctx.createBiquadFilter();
      n.buffer = buf; nf.type = 'highpass'; nf.frequency.value = 1400;
      ng.gain.setValueAtTime(0.0001, t);
      ng.gain.exponentialRampToValueAtTime(0.06, t + 0.8);
      ng.gain.exponentialRampToValueAtTime(0.0001, t + 1.05);
      n.connect(nf).connect(ng).connect(ctx.destination); n.start(t);
    } catch (e) { /* no audio available -- the page works fine silent */ }

    /* The spoken line is a bonus layered on top: voice lists load
       asynchronously and on some platforms never populate at all, so nothing
       depends on it existing. */
    try {
      if (!('speechSynthesis' in window)) return;
      var say = function () {
        var u = new SpeechSynthesisUtterance("Bookmaker. I am coming for you. Give me my money back.");
        u.lang = 'en-US'; u.rate = 0.86; u.pitch = 0.55; u.volume = 0.95;
        var v = speechSynthesis.getVoices().filter(function (x) {
          return (x.lang || '').toLowerCase().indexOf('en') === 0;
        });
        if (v.length) u.voice = v[0];
        speechSynthesis.speak(u);
      };
      setTimeout(function () {
        if (speechSynthesis.getVoices().length) { say(); }
        else { speechSynthesis.addEventListener('voiceschanged', say, { once: true }); }
      }, 700);
    } catch (e) { /* ignore */ }
  }

  function firstGesture() { if (armed) sting(); }
  document.addEventListener('pointerdown', firstGesture, { once: true });
  document.addEventListener('keydown', firstGesture, { once: true });

  if (btn) {
    btn.classList.add('on');
    btn.setAttribute('aria-pressed', 'true');
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (!played) { armed = true; sting(); btn.textContent = '♪ звук'; return; }
      armed = !armed;
      btn.classList.toggle('on', armed);
      btn.setAttribute('aria-pressed', armed ? 'true' : 'false');
      btn.textContent = armed ? '♪ звук' : '✕ без звука';
      if (!armed) { try { speechSynthesis.cancel(); } catch (err) {} }
    });
  }
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
    last_bets = storage.recent_bets(10, "prematch")

    now = datetime.now(timezone.utc)
    fetched = _parse_iso(meta.get("fetched_at"))
    # More than two publish cycles without a refresh means the scheduler is
    # stuck. Say so rather than showing a green "в эфире" badge over stale data
    # -- a live badge that lies is worse than no badge.
    fresh = bool(fetched and (now - fetched) < timedelta(minutes=PUBLISH_INTERVAL_MINUTES * 2 + 5))

    span = cov.get("span_hours") or 24
    span_label = f"{span} {_hours_word(span)}"

    _write_ledger(aggressive)

    html_out = PAGE.safe_substitute(
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
        # The formatted value is rendered server-side and the count-up merely
        # animates up to it -- so a browser that never runs the script still
        # shows the real figure instead of a row of zeroes.
        cov_books=cov["books"], cov_books_txt=_num(cov["books"]),
        cov_events=cov["events"], cov_events_txt=_num(cov["events"]),
        cov_lines=cov["lines"], cov_lines_txt=_num(cov["lines"]),
        cov_cycles=cov["cycles"], cov_cycles_txt=_num(cov["cycles"]),
        cov_moves=cov["moves"], cov_moves_txt=_num(cov["moves"]),
        cov_signals=cov["signals"], cov_signals_txt=_num(cov["signals"]),
        ticker=_ticker(summaries),
        summaries_html=_summaries_html(summaries),
        top_books=_top_books(storage.top_books(10)),
        stats_aggressive=_strategy_card(
            aggressive, "Агрессивная",
            "Все сигналы подряд, какой бы ни был коэффициент.", "agg",
            storage.recent_bets(5, "prematch", "aggressive")),
        stats_optimal=_strategy_card(
            optimal, "Оптимальная",
            f"До {OPTIMAL_MAX_PRICE:g} — та же ставка. Выше — вход мягче: двойной шанс в футболе, фора там, где ничьей нет.", "opt",
            storage.recent_bets(5, "prematch", "optimal")),
        resolved_table=_resolved_table(aggressive),
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
