"""Renders a self-contained HTML dashboard.

2026-07-29: reworked so the page explains itself. Previously it was a wall of
unlabelled tables -- a reader had no way to tell what the product does, which
bookmakers/sports the numbers came from, how fresh they were, or how to read
any given column. Now every card carries a plain-language note, and there are
dedicated "what is this / where does the data come from / when was it updated"
sections driven by real values from the last poll (not hardcoded prose).
"""
import html
import os
from datetime import datetime, timedelta, timezone

from config import (
    DASHBOARD_PATH,
    ASIAN_SHARP_BOOKMAKERS,
    REGION_LABELS,
    get_region,
    REGIONS,
    MARKETS,
    SPIKE_THRESHOLD_PCT,
    CASCADE_WINDOW_MINUTES,
    POLL_INTERVAL_MINUTES,
    RESULTS_CHECK_INTERVAL_HOURS,
)
import storage

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ODDS//TRACKER — трекер движения коэффициентов</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;800;900&family=Rajdhani:wght@500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root {{
    color-scheme: dark;
    --bg: #05060a;
    --bg-grid: rgba(0, 255, 220, 0.045);
    --panel: #0c0f18;
    --panel-border: #1c2333;
    --cyan: #00f0ff;
    --magenta: #ff2ec4;
    --violet: #7b5bff;
    --green: #2bffa8;
    --red: #ff3b5c;
    --amber: #ffb020;
    --text: #d7e2f2;
    --dim: #6c7a94;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    font-family: 'Rajdhani', -apple-system, Segoe UI, Roboto, sans-serif;
    margin: 0; padding: 28px 20px 60px; color: var(--text);
    background:
      radial-gradient(circle at 15% 0%, rgba(123, 91, 255, 0.16), transparent 45%),
      radial-gradient(circle at 85% 10%, rgba(0, 240, 255, 0.10), transparent 40%),
      linear-gradient(var(--bg-grid) 1px, transparent 1px), linear-gradient(90deg, var(--bg-grid) 1px, transparent 1px),
      var(--bg);
    background-size: auto, auto, 42px 42px, 42px 42px, auto;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}
  .banner {{
    display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 10px;
    padding: 18px 22px; margin-bottom: 22px; border-radius: 4px;
    background: linear-gradient(120deg, rgba(0,240,255,0.08), rgba(255,46,196,0.08));
    border: 1px solid var(--panel-border); border-left: 3px solid var(--cyan);
    box-shadow: 0 0 24px rgba(0, 240, 255, 0.08);
  }}
  h1 {{
    font-family: 'Orbitron', sans-serif; font-weight: 900; letter-spacing: 2px;
    font-size: 24px; margin: 0; text-transform: uppercase;
    background: linear-gradient(90deg, var(--cyan), var(--violet) 55%, var(--magenta));
    -webkit-background-clip: text; background-clip: text; color: transparent;
  }}
  h1 span {{ color: var(--dim); -webkit-text-fill-color: var(--dim); font-size: 13px; letter-spacing: 3px; display: block; font-family: 'Share Tech Mono', monospace; margin-top: 4px; }}
  .meta {{ color: var(--dim); font-size: 12px; font-family: 'Share Tech Mono', monospace; text-align: right; line-height: 1.7; }}
  .meta .live {{ color: var(--green); }}
  .live::before {{ content: '● '; }}
  .stale {{ color: var(--amber); }}
  .stale::before {{ content: '● '; }}
  .card {{
    position: relative; background: var(--panel); border: 1px solid var(--panel-border);
    border-radius: 4px; padding: 18px 20px; margin-bottom: 18px; overflow: hidden;
  }}
  .card::before {{
    content: ''; position: absolute; top: 0; left: 0; right: 0; height: 2px;
    background: linear-gradient(90deg, var(--cyan), var(--violet), var(--magenta));
    opacity: 0.7;
  }}
  .card h2 {{
    font-family: 'Orbitron', sans-serif; font-size: 13px; letter-spacing: 1.5px; text-transform: uppercase;
    margin: 0 0 6px; color: var(--text); display: flex; align-items: center; gap: 8px;
  }}
  .note {{
    color: var(--dim); font-size: 13.5px; line-height: 1.65; margin: 0 0 14px;
    border-left: 2px solid var(--panel-border); padding-left: 12px;
  }}
  .note b {{ color: var(--text); font-weight: 600; }}
  .note code {{ font-family: 'Share Tech Mono', monospace; color: var(--cyan); font-size: 12.5px; }}
  .intro p {{ color: var(--text); font-size: 14.5px; line-height: 1.7; margin: 0 0 10px; }}
  .intro p:last-child {{ margin-bottom: 0; }}
  .intro b {{ color: var(--cyan); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--panel-border); vertical-align: top; }}
  th {{
    color: var(--dim); font-weight: 600; font-size: 11px; letter-spacing: 1px;
    text-transform: uppercase; font-family: 'Share Tech Mono', monospace;
  }}
  tbody tr {{ transition: background 0.15s ease; }}
  tbody tr:hover {{ background: rgba(0, 240, 255, 0.05); }}
  .sharp {{ color: var(--amber); font-weight: 700; }}
  .up {{ color: var(--green); font-family: 'Share Tech Mono', monospace; }}
  .down {{ color: var(--red); font-family: 'Share Tech Mono', monospace; }}
  .empty {{ color: var(--dim); font-style: italic; font-size: 13px; }}
  .mono {{ font-family: 'Share Tech Mono', monospace; font-size: 12px; color: var(--dim); }}
  .region-tag {{ font-size: 11px; color: var(--dim); margin-left: 4px; }}
  .cascade {{ background: rgba(255, 59, 92, 0.10); }}
  .cascade-tag {{
    color: #fff; background: linear-gradient(90deg, var(--red), var(--magenta));
    font-weight: 700; font-size: 11px; padding: 2px 7px; border-radius: 3px; margin-right: 4px;
    box-shadow: 0 0 10px rgba(255, 59, 92, 0.55);
  }}
  .chip {{
    display: inline-block; font-size: 12px; font-family: 'Share Tech Mono', monospace;
    padding: 3px 9px; margin: 0 6px 6px 0; border-radius: 3px;
    border: 1px solid var(--panel-border); background: rgba(255,255,255,0.02); color: var(--text);
  }}
  .chip.sharpchip {{ border-color: rgba(255,176,32,0.5); color: var(--amber); }}
  .facts {{ width: 100%; font-size: 14px; }}
  .facts td {{ padding: 7px 10px 7px 0; border-bottom: 1px solid var(--panel-border); }}
  .facts td:first-child {{ color: var(--dim); width: 220px; white-space: nowrap; }}
  .stat-row {{ display: flex; gap: 14px; flex-wrap: wrap; margin-bottom: 14px; }}
  .stat {{
    flex: 1 1 140px; font-size: 12px; color: var(--dim); text-transform: uppercase; letter-spacing: 0.5px;
    background: rgba(255,255,255,0.02); border: 1px solid var(--panel-border); border-radius: 4px;
    padding: 10px 12px;
  }}
  .stat b {{
    display: block; font-size: 26px; color: var(--cyan); font-family: 'Orbitron', sans-serif;
    text-shadow: 0 0 12px rgba(0, 240, 255, 0.35); margin-bottom: 2px;
  }}
  .hit {{ color: var(--green); font-weight: 700; }}
  .miss {{ color: var(--red); font-weight: 700; }}
  .warn {{ color: var(--amber); }}
  footer {{ text-align: center; color: var(--dim); font-size: 11px; font-family: 'Share Tech Mono', monospace; margin-top: 30px; letter-spacing: 1px; line-height: 1.8; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="banner">
    <h1>⚡ ODDS // TRACKER<span>ТРЕКЕР ДВИЖЕНИЯ КОЭФФИЦИЕНТОВ</span></h1>
    <div class="meta">
      <span class="{freshness_class}">{freshness_label}</span><br>
      обновлено {updated_at}<br>
      {updated_ago}
    </div>
  </div>

  <div class="card intro">
    <h2>❓ Что это за продукт</h2>
    <p>Это автоматический трекер, который каждые {poll_interval} минут снимает коэффициенты
    у букмекеров и ищет три вещи: <b>резкие движения линии</b>, <b>расхождения между
    «умными» и публичными конторами</b> и <b>расхождения между регионами</b>. Всё
    найденное уходит алертом в Telegram и попадает на эту страницу.</p>
    <p>Логика в основе: когда информированные деньги заходят в матч, первыми линию
    двигают «шарп»-конторы (в первую очередь <b>Pinnacle</b>) — они не режут лимиты и
    закладывают поток ставок прямо в цену. Публичные конторы подтягиваются позже. Разрыв
    между ними и есть окно, ради которого всё это работает.</p>
    <p>Чтобы понимать, есть ли от инструмента реальный толк, каждый алерт потом
    проверяется двумя способами: <b>по результату матча</b> (угадал/не угадал) и по
    <b>CLV</b> — продолжила ли линия двигаться в ту же сторону до самого старта матча.
    CLV честнее: исход можно угадать на удаче, а движение рынка — нет.</p>
  </div>

  <div class="card">
    <h2>📡 Откуда данные</h2>
    <p class="note">Всё на этой странице получено из одного источника — ниже указано,
    что именно и в каком объёме пришло в <b>последнем</b> опросе.</p>
    {source_facts}
  </div>

  <div class="card">
    <h2>🕐 Когда обновляется</h2>
    <p class="note">Опрос запускается автоматически через GitHub Actions. Страница
    перезаписывается целиком на каждом прогоне — то, что вы видите, это всегда
    последний снимок, а не накопленное среднее.</p>
    {timing_facts}
  </div>

  <div class="card">
    <h2>📊 Статистика алертов</h2>
    <p class="note">Считается только по алертам, чьи матчи уже закончились.
    <b>Win rate</b> — доля угаданных исходов на рынке 1X2 (спреды и тоталы сюда не
    попадают, они помечаются «н/д»). <b>CLV</b> — насколько цена ушла от нашей точки
    входа к моменту старта матча: плюс по столбцу CLV означает, что рынок продолжил
    двигаться туда же, куда указывал алерт.</p>
    {stats_card}
  </div>

  <div class="card">
    <h2>🚨 Резкие движения (скачки)</h2>
    <p class="note">Линия сравнивается с её же ценой в предыдущем опросе. В таблицу
    попадает всё, что сдвинулось минимум на <b>{threshold_pct}%</b>. Падение
    коэффициента = рынок сильнее верит в исход, рост = теряет веру. Пометка
    <span class="cascade-tag">🚨 xN</span> — «каскад»: движение в одну и ту же сторону
    повторилось N раз за {cascade_window} минут, это самый сильный сигнал.
    Жёлтым выделены шарп-конторы.</p>
    {spikes_table}
  </div>

  <div class="card">
    <h2>🎯 Sharp vs Public</h2>
    <p class="note">Для каждого исхода считается средняя цена у шарп-контор и у
    публичных, и берётся разрыв между ними. В таблицу попадают расхождения от
    <b>5%</b>. Смысл: если шарпы уже переоценили исход, а паблик ещё нет —
    обычно подтягивается паблик, а не наоборот.</p>
    {digest_table}
  </div>

  <div class="card">
    <h2>🌏 Азия vs 🇪🇺 Европа</h2>
    <p class="note">То же сравнение, но чисто по географии, без деления на «умные» и
    «глупые» деньги — отдельный срез, чтобы видеть, какой рынок двигается первым.
    Порог тоже <b>5%</b>.</p>
    {region_table}
  </div>

  <div class="card">
    <h2>📋 Последний снимок</h2>
    <p class="note">Сырые данные последнего опроса, как они пришли из API —
    до {n} строк. Это то, на чём построены все таблицы выше.</p>
    {snapshot_table}
  </div>

  <footer>
    ODDS-TRACKER · источник данных: The Odds API (the-odds-api.com)<br>
    страница генерируется автоматически на каждом прогоне · время везде UTC<br>
    это аналитический инструмент, а не советы по ставкам
  </footer>
</div>
</body>
</html>
"""


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: 1 событие / 2-4 события / 5+ событий.
    Without this the page reads '9 строк по 1 событиям', which looks sloppy on
    a page whose whole point is being clearly written."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _region_badge(bookmaker: str) -> str:
    label = REGION_LABELS.get(get_region(bookmaker), "")
    return f"<span class='region-tag'>{html.escape(label)}</span>" if label else ""


def _get(row_like, key):
    """Read a field from either a dict (spike/divergence rows) or a
    sqlite3.Row (rows straight out of storage)."""
    if isinstance(row_like, dict):
        return row_like.get(key)
    try:
        return row_like[key]
    except (IndexError, KeyError):
        return None


def _event_name(row_like) -> str:
    home, away = _get(row_like, "home_team"), _get(row_like, "away_team")
    if home and away:
        return f"{home} vs {away}"
    return str(_get(row_like, "fixture_id") or "—")


def _parse_iso(value):
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _fmt_dt(value) -> str:
    dt = _parse_iso(value)
    return dt.strftime("%Y-%m-%d %H:%M UTC") if dt else "—"


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


def _source_facts(meta: dict) -> str:
    books = meta.get("bookmakers") or []
    sports = meta.get("sports") or []
    book_chips = "".join(
        f"<span class='chip{' sharpchip' if b.lower() in ASIAN_SHARP_BOOKMAKERS else ''}'>"
        f"{html.escape(b)}{' ★ шарп' if b.lower() in ASIAN_SHARP_BOOKMAKERS else ''}</span>"
        for b in books
    ) or "<span class='empty'>нет данных — опрос ещё не проходил</span>"
    sport_chips = "".join(
        f"<span class='chip'>{html.escape(s)}</span>" for s in sports
    ) or "<span class='empty'>нет данных</span>"
    lines_n = meta.get("lines") or 0
    events_n = meta.get("events") or 0
    lines_word = _plural(lines_n, "строка", "строки", "строк")
    events_word = _plural(events_n, "событию", "событиям", "событиям")
    return f"""
    <table class="facts">
      <tr><td>Источник</td><td>The Odds API (the-odds-api.com), платный тариф 20K — 20&nbsp;000 запросов/мес</td></tr>
      <tr><td>Регион запроса</td><td><span class="mono">{html.escape(REGIONS)}</span> — определяет, какие конторы возвращает API</td></tr>
      <tr><td>Рынок</td><td><span class="mono">{html.escape(MARKETS)}</span> — победитель матча (1X2 / moneyline)</td></tr>
      <tr><td>Конторы в снимке<br><span class="mono">{len(books)} шт</span></td><td>{book_chips}</td></tr>
      <tr><td>Виды спорта<br><span class="mono">{len(sports)} шт</span></td><td>{sport_chips}</td></tr>
      <tr><td>Объём снимка</td><td><b>{lines_n}</b> {lines_word} котировок по <b>{events_n}</b> {events_word}</td></tr>
      <tr><td class="warn">Чего здесь нет</td><td class="warn">Киберспорт (CS2, LoL, Dota&nbsp;2) этим API не покрывается вообще.
        Из азиатских контор недоступны SBOBET, Maxbet, Singbet; bet365 тоже нет.
        Из «шарпов» остались Pinnacle и 1xBet.</td></tr>
    </table>
    """


def _timing_facts(meta: dict, now=None) -> str:
    now = now or datetime.now(timezone.utc)
    fetched = _parse_iso(meta.get("fetched_at"))
    next_run = (fetched + timedelta(minutes=POLL_INTERVAL_MINUTES)).strftime("%H:%M UTC") if fetched else "—"
    quota_used = meta.get("quota_used")
    quota_left = meta.get("quota_remaining")
    if quota_left is not None:
        quota = f"израсходовано <b>{quota_used}</b>, осталось <b>{quota_left}</b> из 20&nbsp;000 на месяц"
    else:
        quota = "<span class='empty'>нет данных за этот прогон</span>"
    return f"""
    <table class="facts">
      <tr><td>Последний опрос</td><td><b>{_fmt_dt(meta.get('fetched_at'))}</b> ({_ago(meta.get('fetched_at'), now)})</td></tr>
      <tr><td>Частота опроса</td><td>каждые <b>{POLL_INTERVAL_MINUTES} минут</b>, круглосуточно</td></tr>
      <tr><td>Следующий опрос</td><td>ориентировочно в <b>{next_run}</b> (GitHub может задержать запуск на несколько минут)</td></tr>
      <tr><td>Проверка результатов</td><td>раз в {RESULTS_CHECK_INTERVAL_HOURS} ч — счёт матча и расчёт CLV по завершённым событиям</td></tr>
      <tr><td>Расход квоты API</td><td>{quota}</td></tr>
      <tr><td>Страница собрана</td><td>{now.strftime('%Y-%m-%d %H:%M:%S UTC')}</td></tr>
    </table>
    """


def _spikes_table(spikes):
    if not spikes:
        return ('<p class="empty">В последнем опросе резких движений не найдено. '
                'Это нормально: скачки появляются только когда линия реально дёрнулась '
                'между двумя соседними опросами.</p>')
    rows = []
    for s in spikes:
        cls = "sharp" if s["is_sharp_book"] else ""
        row_cls = "cascade" if s.get("is_cascade") else ""
        direction_cls = "up" if s["pct_change"] > 0 else "down"
        arrow = "рост" if s["pct_change"] > 0 else "падение"
        outcome = s.get("label") or f"{s['market_id']}/{s['outcome_id']}"
        if ":" in outcome:
            outcome = outcome.split(":", 1)[1].strip()
        cascade_mark = (
            f"<span class='cascade-tag'>🚨 x{s['cascade_count']}</span> " if s.get("is_cascade") else ""
        )
        rows.append(
            f"<tr class='{row_cls}'>"
            f"<td><b>{html.escape(_event_name(s))}</b><br>"
            f"<span class='mono'>старт {_fmt_dt(s.get('start_time'))}</span></td>"
            f"<td>{html.escape(outcome)}</td>"
            f"<td class='{cls}'>{cascade_mark}{html.escape(s['bookmaker'])}{_region_badge(s['bookmaker'])}</td>"
            f"<td>{s['prev_price']:.2f} → {s['price']:.2f}</td>"
            f"<td class='{direction_cls}'>{s['pct_change'] * 100:+.1f}%<br>"
            f"<span class='mono'>{arrow}</span></td></tr>"
        )
    return (
        "<table><tr><th>Событие</th><th>Исход</th><th>Контора</th>"
        "<th>Коэффициент</th><th>Изменение</th></tr>" + "".join(rows) + "</table>"
    )


def _stats_card(stats: dict):
    win_rate = stats["win_rate"]
    win_rate_html = f"{win_rate:.0f}%" if win_rate is not None else "—"
    avg_clv = stats.get("avg_clv_pct")
    avg_clv_html = f"{avg_clv * 100:+.1f}%" if avg_clv is not None else "—"
    clv_rate = stats.get("clv_continued_rate")
    clv_rate_html = f"{clv_rate:.0f}%" if clv_rate is not None else "—"
    total_word = _plural(stats['total'], "алерт отправлен", "алерта отправлено", "алертов отправлено")
    resolved_word = _plural(stats['resolved'], "матч сыгран и проверен", "матча сыграно и проверено", "матчей сыграно и проверено")
    pending_word = _plural(stats['pending'], "алерт ждёт", "алерта ждут", "алертов ждут") + " окончания матча"
    summary = f"""
    <div class="stat-row">
      <div class="stat"><b>{stats['total']}</b>{total_word}</div>
      <div class="stat"><b>{stats['resolved']}</b>{resolved_word}</div>
      <div class="stat"><b>{stats['pending']}</b>{pending_word}</div>
      <div class="stat"><b>{win_rate_html}</b>win rate по рынку 1X2</div>
    </div>
    <div class="stat-row">
      <div class="stat"><b>{avg_clv_html}</b>средний CLV</div>
      <div class="stat"><b>{clv_rate_html}</b>линия пошла дальше в нашу сторону
        <span class="mono">({stats.get('clv_n') or 0} алертов с данными)</span></div>
    </div>
    """
    if not stats["recent"]:
        return summary + ('<p class="empty">Проверенных алертов пока нет — они появятся здесь, '
                          'как только закончится первый матч, по которому был алерт.</p>')
    rows = []
    for r in stats["recent"]:
        result = r["result"]
        cls = "hit" if result == "hit" else ("miss" if result == "miss" else "")
        result_label = {"hit": "✅ сработал", "miss": "❌ не сработал",
                        "n/a": "н/д (не 1X2)"}.get(result, result)
        clv_pct = r["clv_pct"]
        clv_html = f"{clv_pct * 100:+.1f}%" if clv_pct is not None else "—"
        clv_cls = "hit" if r["clv_continued"] == 1 else ("miss" if r["clv_continued"] == 0 else "")
        label = r["label"] or ""
        if ":" in label:
            label = label.split(":", 1)[1].strip()
        rows.append(
            f"<tr><td><b>{html.escape(_event_name(r))}</b></td>"
            f"<td>{html.escape(label)}</td>"
            f"<td>{html.escape(r['bookmaker'])}</td>"
            f"<td>{html.escape(r['alert_type'])}</td>"
            f"<td class='{cls}'>{result_label}</td>"
            f"<td class='{clv_cls}'>{clv_html}</td>"
            f"<td class='mono'>{_fmt_dt(r['resolved_at'])}</td></tr>"
        )
    table = (
        "<table><tr><th>Событие</th><th>Исход</th><th>Контора</th>"
        "<th>Тип</th><th>Результат</th><th>CLV</th><th>Проверено</th></tr>"
        + "".join(rows) + "</table>"
    )
    return summary + table


def _region_table(rows):
    if not rows:
        return '<p class="empty">Расхождений между Азией и Европой выше 5% сейчас нет.</p>'
    out = []
    for d in rows:
        cls = "up" if d["divergence_pct"] > 0 else "down"
        outcome = d.get("label") or ""
        if ":" in outcome:
            outcome = outcome.split(":", 1)[1].strip()
        out.append(
            f"<tr><td><b>{html.escape(_event_name(d))}</b></td><td>{html.escape(outcome)}</td>"
            f"<td>{d['asia_avg']:.2f}<br><span class='mono'>{html.escape(', '.join(d['asia_books']))}</span></td>"
            f"<td>{d['europe_avg']:.2f}<br><span class='mono'>{html.escape(', '.join(d['europe_books']))}</span></td>"
            f"<td class='{cls}'>{d['divergence_pct'] * 100:+.1f}%</td></tr>"
        )
    return (
        "<table><tr><th>Событие</th><th>Исход</th><th>🌏 Азия, средняя</th>"
        "<th>🇪🇺 Европа, средняя</th><th>Разрыв</th></tr>" + "".join(out) + "</table>"
    )


def _digest_table(divergences):
    if not divergences:
        return '<p class="empty">Расхождений sharp vs public выше 5% сейчас нет.</p>'
    rows = []
    for d in divergences:
        cls = "up" if d["divergence_pct"] > 0 else "down"
        outcome = d.get("label") or ""
        if ":" in outcome:
            outcome = outcome.split(":", 1)[1].strip()
        rows.append(
            f"<tr><td><b>{html.escape(_event_name(d))}</b></td><td>{html.escape(outcome)}</td>"
            f"<td>{d['sharp_avg']:.2f}<br><span class='mono'>{html.escape(', '.join(d['sharp_books']))}</span></td>"
            f"<td>{d['public_avg']:.2f}<br><span class='mono'>{html.escape(', '.join(d['public_books']))}</span></td>"
            f"<td class='{cls}'>{d['divergence_pct'] * 100:+.1f}%</td></tr>"
        )
    return (
        "<table><tr><th>Событие</th><th>Исход</th><th>Шарпы, средняя</th>"
        "<th>Паблик, средняя</th><th>Разрыв</th></tr>" + "".join(rows) + "</table>"
    )


def _snapshot_table(rows):
    if not rows:
        return '<p class="empty">Данных нет — опрос ещё ни разу не проходил.</p>'
    out = []
    for row in rows:
        bookmaker = row["bookmaker"]
        cls = "sharp" if bookmaker.lower() in ASIAN_SHARP_BOOKMAKERS else ""
        label = row["label"] or f"{row['market_id']}/{row['outcome_id']}"
        if ":" in label:
            label = label.split(":", 1)[1].strip()
        out.append(
            f"<tr><td class='mono'>{_fmt_dt(row['fetched_at'])}</td>"
            f"<td><b>{html.escape(_event_name(row))}</b></td>"
            f"<td>{html.escape(label)}</td>"
            f"<td class='{cls}'>{html.escape(bookmaker)}{_region_badge(bookmaker)}</td>"
            f"<td>{row['price']:.2f}</td></tr>"
        )
    return (
        "<table><tr><th>Снято</th><th>Событие</th><th>Исход</th>"
        "<th>Контора</th><th>Коэффициент</th></tr>" + "".join(out) + "</table>"
    )


def render_dashboard(spikes: list, divergences: list = None, region_rows: list = None,
                     snapshot_limit: int = 200, quota: dict = None):
    rows = storage.recent_snapshots(limit=snapshot_limit)
    meta = storage.snapshot_meta()
    if quota:
        meta["quota_used"] = quota.get("used")
        meta["quota_remaining"] = quota.get("remaining")

    now = datetime.now(timezone.utc)
    fetched = _parse_iso(meta.get("fetched_at"))
    # More than two poll intervals without a refresh means the scheduler is
    # stuck -- say so on the page instead of showing a green "LIVE" badge over
    # data that's hours old.
    fresh = bool(fetched and (now - fetched) < timedelta(minutes=POLL_INTERVAL_MINUTES * 2 + 5))

    html_out = PAGE_TEMPLATE.format(
        updated_at=_fmt_dt(meta.get("fetched_at")),
        updated_ago=_ago(meta.get("fetched_at"), now),
        freshness_class="live" if fresh else "stale",
        freshness_label="ДАННЫЕ АКТУАЛЬНЫ" if fresh else "ДАННЫЕ УСТАРЕЛИ",
        poll_interval=POLL_INTERVAL_MINUTES,
        threshold_pct=f"{SPIKE_THRESHOLD_PCT * 100:.0f}",
        cascade_window=CASCADE_WINDOW_MINUTES,
        source_facts=_source_facts(meta),
        timing_facts=_timing_facts(meta, now),
        stats_card=_stats_card(storage.alert_stats()),
        region_table=_region_table(region_rows or []),
        digest_table=_digest_table(divergences or []),
        spikes_table=_spikes_table(spikes),
        snapshot_table=_snapshot_table(rows),
        n=snapshot_limit,
    )
    # git does not track empty directories, so a fresh checkout on CI has no
    # dashboard/ folder at all yet (confirmed live 2026-07-29: FileNotFoundError
    # on the very first run after checkout) -- make sure it exists before writing.
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return DASHBOARD_PATH
