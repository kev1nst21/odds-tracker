"""Renders a self-contained HTML dashboard.

2026-07-29, second pass. Restructured to match the Telegram output: one row per
EVENT instead of separate "spikes" / "sharp vs public" / "Asia vs Europe"
tables. Those three views were three different angles on the same market move,
which meant a single match appeared in all three and the page read as more
crowded than the market actually was. They are now one table: price range
across the market, where the line moved, the computed fair price, and the entry
price the analyst derives from it.

Removed at the user's request: the "where the data comes from" card and the raw
"last snapshot" table (200 rows of unaggregated prices nobody reads). Source
provenance moved to a single compact footer line so it isn't lost entirely.
"""
import html
import os
from datetime import datetime, timedelta, timezone

from config import (
    DASHBOARD_PATH,
    SPIKE_THRESHOLD_PCT,
    POLL_INTERVAL_MINUTES,

)
import storage

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KEWA / Vilka / Tracker — трекер движения коэффициентов</title>
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
  .intro p {{ color: var(--text); font-size: 14.5px; line-height: 1.7; margin: 0 0 10px; }}
  .intro p:last-child {{ margin-bottom: 0; }}
  .intro b {{ color: var(--cyan); }}
  .facts {{ width: 100%; font-size: 14px; }}
  .facts td {{ padding: 7px 10px 7px 0; border-bottom: 1px solid var(--panel-border); }}
  .facts td:first-child {{ color: var(--dim); width: 220px; white-space: nowrap; }}
  .ev {{
    border: 1px solid var(--panel-border); border-radius: 4px; padding: 14px 16px;
    margin-bottom: 12px; background: rgba(255,255,255,0.015);
  }}
  .ev.value {{ border-left: 3px solid var(--green); }}
  .ev.move {{ border-left: 3px solid var(--violet); }}
  .ev-head {{ display: flex; justify-content: space-between; flex-wrap: wrap; gap: 8px; margin-bottom: 10px; }}
  .ev-name {{ font-size: 16px; font-weight: 700; color: var(--text); }}
  .ev-when {{ font-family: 'Share Tech Mono', monospace; font-size: 12px; color: var(--dim); }}
  /* Compact feed: one row per event so many fit on a screen without scrolling. */
  .filters {{ display: flex; gap: 8px; flex-wrap: wrap; margin: 0 0 14px; }}
  .f {{
    font-family: 'Share Tech Mono', monospace; font-size: 12.5px; cursor: pointer;
    padding: 7px 14px; border-radius: 3px; color: var(--dim);
    background: rgba(255,255,255,0.03); border: 1px solid var(--panel-border);
    transition: all 0.15s ease;
  }}
  .f:hover {{ color: var(--text); border-color: var(--cyan); }}
  .f.active {{ color: #05060a; background: var(--cyan); border-color: var(--cyan); font-weight: 700; }}
  .f.active[data-f="3"] {{ background: var(--amber); border-color: var(--amber); }}
  .f.active[data-f="open"] {{ background: var(--green); border-color: var(--green); }}
  .feed-wrap {{ overflow-x: auto; }}
  table.feed {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  table.feed th {{
    text-align: left; padding: 8px 10px; color: var(--dim); font-weight: 600;
    font-size: 10.5px; letter-spacing: 1px; text-transform: uppercase;
    font-family: 'Share Tech Mono', monospace; border-bottom: 1px solid var(--panel-border);
    white-space: nowrap;
  }}
  table.feed td {{ padding: 9px 10px; border-bottom: 1px solid rgba(28,35,51,0.7); vertical-align: middle; }}
  table.feed tr.row:hover {{ background: rgba(0,240,255,0.05); }}
  table.feed tr.s3 {{ background: rgba(255,176,32,0.05); }}
  table.feed tr.shut {{ opacity: 0.5; }}
  .c-stars {{ white-space: nowrap; font-size: 13px; letter-spacing: -1px; }}
  .c-ev {{ font-weight: 700; color: var(--text); line-height: 1.35; }}
  .c-ev small {{ display: block; font-family: 'Share Tech Mono', monospace; font-size: 11px; color: var(--dim); font-weight: 400; }}
  .c-out {{ color: var(--cyan); font-weight: 600; white-space: nowrap; }}
  .c-move {{ font-family: 'Share Tech Mono', monospace; white-space: nowrap; font-size: 14px; }}
  .c-move .old {{ color: var(--green); }}
  .c-move .new {{ color: var(--red); }}
  .c-move .pct {{ color: var(--red); font-size: 12px; }}
  .c-books {{ font-family: 'Share Tech Mono', monospace; font-size: 12.5px; color: var(--dim); white-space: nowrap; }}
  .c-bet {{ white-space: nowrap; }}
  .c-bet b {{ color: var(--green); font-family: 'Share Tech Mono', monospace; font-size: 15px; }}
  .c-bet small {{ display: block; font-family: 'Share Tech Mono', monospace; font-size: 11px; color: var(--dim); }}
  .c-bet.shut {{ color: var(--red); font-size: 12px; font-family: 'Share Tech Mono', monospace; }}
  .norows {{ color: var(--dim); font-style: italic; font-size: 13px; padding: 16px 4px; }}
  .badge {{
    font-size: 11px; font-family: 'Share Tech Mono', monospace; padding: 3px 8px;
    border-radius: 3px; letter-spacing: 0.5px;
  }}
  .badge.v {{ background: rgba(43,255,168,0.15); color: var(--green); border: 1px solid rgba(43,255,168,0.4); }}
  .badge.m {{ background: rgba(123,91,255,0.15); color: #b4a1ff; border: 1px solid rgba(123,91,255,0.4); }}
  .badge.s {{ background: rgba(255,176,32,0.15); color: var(--amber); border: 1px solid rgba(255,176,32,0.45); letter-spacing: 0; }}
  .badge.c {{ background: rgba(255,59,92,0.12); color: var(--red); border: 1px solid rgba(255,59,92,0.35); }}
  .ev.starred {{ border-left: 3px solid var(--amber); }}
  .ev.closed {{ border-left: 3px solid var(--panel-border); opacity: 0.75; }}
  .up {{ color: var(--green); }}
  .down {{ color: var(--red); }}
  .empty {{ color: var(--dim); font-style: italic; font-size: 13px; }}
  .mono {{ font-family: 'Share Tech Mono', monospace; font-size: 12px; color: var(--dim); }}
  table.plain {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  table.plain th, table.plain td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--panel-border); }}
  table.plain th {{ color: var(--dim); font-weight: 600; font-size: 11px; letter-spacing: 1px; text-transform: uppercase; font-family: 'Share Tech Mono', monospace; }}
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
  footer {{ text-align: center; color: var(--dim); font-size: 11px; font-family: 'Share Tech Mono', monospace; margin-top: 30px; letter-spacing: 1px; line-height: 1.9; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="banner">
    <h1>⚡ KEWA / VILKA / TRACKER<span>ТРЕКЕР ДВИЖЕНИЯ КОЭФФИЦИЕНТОВ</span></h1>
    <div class="meta">
      <span class="{freshness_class}">{freshness_label}</span><br>
      обновлено {updated_ago}<br>
      обновление каждые {poll_interval} мин
    </div>
  </div>

  <div class="card intro">
    <h2>❓ Что это за продукт</h2>
    <p>Автоматический трекер: каждые {poll_interval} минут снимает коэффициенты по всему
    рынку и ищет момент, когда на какой-то исход <b>занесли деньги</b>. Всё важное
    дублируется алертом в Telegram.</p>
    <p>Логика простая. Если коэффициент был <b>3.00</b> и у нескольких контор просел
    до <b>2.10</b> — значит, в этот исход зашли деньги. Ставим мы <b>на тот же исход</b>,
    но там, где цена ещё не успела упасть: забираем старые 3.00, пока их дают. Обратную
    сторону не трогаем никогда — она подорожала механически, просто потому что деньги
    пошли против неё.</p>
    <p>Главный фильтр — <b>сколько контор подвинулось</b>, а не насколько сильно. Одна
    контора может дёрнуть цену из-за чьей-то одиночной ставки или ошибки трейдера. Когда
    один и тот же исход просел сразу у многих независимых контор за полчаса — это уже
    информированные деньги. Отсюда и звёзды.</p>
    <p>Работаем только по матчам <b>до старта</b>: в лайве цена скачет от голов, а не от
    денег. Ничья в футболе не рассматривается. Биржи в расчёт не берём — там цену двигает
    один случайный человек.</p>
  </div>

  <div class="card">
    <h2>💎 Сводка по рынку</h2>
    <p class="note"><b>Как это читать.</b> Если коэффициент был 3.00 и где-то просел
    до 2.10 — значит, на этот исход загрузили деньги. Ставим мы <b>на тот же исход</b>
    и там, где цена ещё не упала, то есть забираем старые 3.00. Обратную сторону не
    рассматриваем никогда: она подорожала просто механически, потому что деньги пошли
    против неё.<br>
    <b>⭐ Звёзды — уверенность, и считаются по числу контор, а не по величине скачка.</b>
    Одна контора подвинула цену — это может быть чья-то одиночная ставка или ошибка
    трейдера. Когда то же самое просело сразу у многих независимых контор за полчаса —
    это заходят информированные деньги. ⭐ одна контора, ⭐⭐ две-три, ⭐⭐⭐ четыре и больше
    либо с участием шарп-конторы.<br>
    В бота уходят только события с падением от {threshold_pct}%, где ещё есть где
    поставить. Ничья в футболе не рассматривается.</p>
    {summaries_html}
  </div>

  <div class="card">
    <h2>📊 Проверка сигналов</h2>
    <p class="note">Считается только по алертам, чьи матчи уже закончились.
    <b>Win rate</b> — доля угаданных исходов. <b>CLV</b> — насколько цена ушла от нашей
    точки входа к старту матча; плюс означает, что рынок продолжил двигаться туда же,
    куда указывал сигнал.</p>
    {stats_card}
  </div>

  <footer>
    страница обновляется автоматически · время везде UTC<br>
    расчёт по модели справедливой цены, а не рекомендация · ставки — риск потерять деньги
  </footer>
</div>
</body>
</html>
"""


def _plural(n: int, one: str, few: str, many: str) -> str:
    """Russian noun agreement: 1 событие / 2-4 события / 5+ событий."""
    n = abs(int(n or 0))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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
    return dt.strftime("%d.%m %H:%M UTC") if dt else "—"


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


def _event_row(s: dict) -> str:
    """One compact table row per event. Everything needed to act on it -- which
    side money went into, what the price was and is, how broad the move was, and
    where to still take it -- has to fit on a single line, so the whole feed is
    scannable without scrolling."""
    bet = s.get("bet") or {}
    stars = s.get("stars", 0)
    has_entry = s.get("has_entry")
    big = s.get("big_move")

    row_cls = "row" + (" s3" if stars >= 3 and has_entry else "") + ("" if has_entry else " shut")
    name = f"{html.escape(s.get('home_team') or '?')} — {html.escape(s.get('away_team') or '?')}"
    outcome = html.escape(bet.get("name") or "—")

    if has_entry:
        bet_cell = (f"<td class='c-bet'><b>{bet['entry_price']:.2f}</b>"
                    f"<small>{html.escape(bet['entry_book'])}</small></td>")
    else:
        bet_cell = "<td class='c-bet shut'>⛔️ вход закрыт</td>"

    return (
        f"<tr class='{row_cls}' data-stars='{stars}' data-open='{1 if has_entry else 0}' "
        f"data-big='{1 if big else 0}'>"
        f"<td class='c-stars'>{'⭐' * stars}</td>"
        f"<td class='c-ev'>{name}<small>{_fmt_start(s.get('start_time'))}</small></td>"
        f"<td class='c-out'>{outcome}</td>"
        f"<td class='c-move'><span class='old'>{bet['old_price']:.2f}</span> → "
        f"<span class='new'>{bet['new_price']:.2f}</span> "
        f"<span class='pct'>({abs(bet['drop_pct']):.0f}%)</span></td>"
        f"<td class='c-books'>{bet['down_count']}/{bet['books_count']}</td>"
        f"{bet_cell}</tr>"
    )


FILTER_JS = """
<script>
(function () {
  var buttons = document.querySelectorAll('.f');
  var rows = document.querySelectorAll('tr.row');
  var empty = document.getElementById('norows');
  function apply(mode) {
    var shown = 0;
    rows.forEach(function (r) {
      var ok;
      if (mode === 'all') ok = true;
      else if (mode === 'open') ok = r.dataset.open === '1';
      else if (mode === 'big') ok = r.dataset.big === '1';
      else ok = r.dataset.stars === mode;
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    });
    empty.style.display = shown ? 'none' : '';
  }
  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      buttons.forEach(function (x) { x.classList.remove('active'); });
      b.classList.add('active');
      apply(b.dataset.f);
    });
  });
})();
</script>
"""


def _summaries_html(summaries: list, limit: int = 120) -> str:
    shown = [s for s in summaries if s.get("bet")][:limit]
    if not shown:
        return ('<p class="empty">Сейчас движений нет — линии стоят на месте. '
                'Строки появятся, как только рынок начнёт двигаться.</p>')

    n3 = sum(1 for s in shown if s["stars"] >= 3)
    n2 = sum(1 for s in shown if s["stars"] == 2)
    n1 = sum(1 for s in shown if s["stars"] == 1)
    nopen = sum(1 for s in shown if s.get("has_entry"))
    nbig = sum(1 for s in shown if s.get("big_move"))

    filters = (
        "<div class='filters'>"
        f"<button class='f active' data-f='all'>Все · {len(shown)}</button>"
        f"<button class='f' data-f='3'>⭐⭐⭐ · {n3}</button>"
        f"<button class='f' data-f='2'>⭐⭐ · {n2}</button>"
        f"<button class='f' data-f='1'>⭐ · {n1}</button>"
        f"<button class='f' data-f='open'>✅ есть вход · {nopen}</button>"
        f"<button class='f' data-f='big'>📈 от 10% · {nbig}</button>"
        "</div>"
    )

    head = ("<tr><th></th><th>Событие</th><th>Деньги на</th>"
            "<th>Был → стал</th><th>Контор</th><th>Ставим</th></tr>")
    body = "".join(_event_row(s) for s in shown)
    table = f"<div class='feed-wrap'><table class='feed'>{head}{body}</table></div>"
    empty = "<p class='norows' id='norows' style='display:none'>Под этот фильтр ничего не подошло.</p>"
    return filters + table + empty + FILTER_JS


def _stats_card(stats: dict):
    win_rate = stats["win_rate"]
    win_rate_html = f"{win_rate:.0f}%" if win_rate is not None else "—"
    avg_clv = stats.get("avg_clv_pct")
    avg_clv_html = f"{avg_clv * 100:+.1f}%" if avg_clv is not None else "—"
    clv_rate = stats.get("clv_continued_rate")
    clv_rate_html = f"{clv_rate:.0f}%" if clv_rate is not None else "—"
    total_word = _plural(stats['total'], "сигнал отправлен", "сигнала отправлено", "сигналов отправлено")
    resolved_word = _plural(stats['resolved'], "матч проверен", "матча проверено", "матчей проверено")
    pending_word = _plural(stats['pending'], "ждёт", "ждут", "ждут") + " результата"
    summary = f"""
    <div class="stat-row">
      <div class="stat"><b>{stats['total']}</b>{total_word}</div>
      <div class="stat"><b>{stats['resolved']}</b>{resolved_word}</div>
      <div class="stat"><b>{stats['pending']}</b>{pending_word}</div>
      <div class="stat"><b>{win_rate_html}</b>win rate</div>
      <div class="stat"><b>{avg_clv_html}</b>средний CLV</div>
    </div>
    """
    if not stats["recent"]:
        return summary + ('<p class="empty">Проверенных сигналов пока нет — появятся, '
                          'как только закончится первый матч с алертом.</p>')
    rows = []
    for r in stats["recent"]:
        result = r["result"]
        cls = "hit" if result == "hit" else ("miss" if result == "miss" else "")
        result_label = {"hit": "✅ сработал", "miss": "❌ не сработал",
                        "n/a": "н/д"}.get(result, result)
        clv_pct = r["clv_pct"]
        clv_html = f"{clv_pct * 100:+.1f}%" if clv_pct is not None else "—"
        clv_cls = "hit" if r["clv_continued"] == 1 else ("miss" if r["clv_continued"] == 0 else "")
        home, away = r["home_team"], r["away_team"]
        event = f"{home} — {away}" if home and away else str(r["fixture_id"])
        label = r["label"] or ""
        if ":" in label:
            label = label.split(":", 1)[1].strip()
        rows.append(
            f"<tr><td><b>{html.escape(event)}</b></td><td>{html.escape(label)}</td>"
            f"<td class='{cls}'>{result_label}</td><td class='{clv_cls}'>{clv_html}</td>"
            f"<td class='mono'>{_fmt_dt(r['resolved_at'])}</td></tr>"
        )
    table = ("<table class='plain'><tr><th>Событие</th><th>Исход</th>"
             "<th>Результат</th><th>CLV</th><th>Проверено</th></tr>"
             + "".join(rows) + "</table>")
    return summary + table


def render_dashboard(summaries: list, quota: dict = None):
    meta = storage.snapshot_meta()
    if quota:
        meta["quota_used"] = quota.get("used")
        meta["quota_remaining"] = quota.get("remaining")

    now = datetime.now(timezone.utc)
    fetched = _parse_iso(meta.get("fetched_at"))
    # More than two poll intervals without a refresh means the scheduler is
    # stuck -- say so instead of showing a green "LIVE" badge over stale data.
    fresh = bool(fetched and (now - fetched) < timedelta(minutes=POLL_INTERVAL_MINUTES * 2 + 5))

    html_out = PAGE_TEMPLATE.format(
        updated_at=_fmt_dt(meta.get("fetched_at")),
        updated_ago=_ago(meta.get("fetched_at"), now),
        freshness_class="live" if fresh else "stale",
        freshness_label="ДАННЫЕ АКТУАЛЬНЫ" if fresh else "ДАННЫЕ УСТАРЕЛИ",
        poll_interval=POLL_INTERVAL_MINUTES,
        threshold_pct=f"{SPIKE_THRESHOLD_PCT * 100:.0f}",
        summaries_html=_summaries_html(summaries or []),
        stats_card=_stats_card(storage.alert_stats()),
    )
    # git does not track empty directories, so a fresh CI checkout has no
    # dashboard/ folder yet -- make sure it exists before writing.
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return DASHBOARD_PATH
