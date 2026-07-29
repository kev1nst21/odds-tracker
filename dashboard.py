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
    MIN_EDGE_PCT,
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
  .oc {{ width: 100%; border-collapse: collapse; font-size: 13.5px; margin-bottom: 10px; }}
  .oc td {{ padding: 6px 8px; border-bottom: 1px solid rgba(28,35,51,0.6); }}
  .oc td:first-child {{ color: var(--text); font-weight: 600; }}
  .oc .price {{ font-family: 'Share Tech Mono', monospace; color: var(--cyan); font-size: 14px; white-space: nowrap; }}
  .oc .fair {{ font-family: 'Share Tech Mono', monospace; color: var(--dim); font-size: 12.5px; white-space: nowrap; }}
  .oc .mv {{ font-family: 'Share Tech Mono', monospace; font-size: 12.5px; white-space: nowrap; }}
  .verdict {{
    font-size: 14px; line-height: 1.6; padding: 10px 12px; border-radius: 3px;
    background: rgba(0,240,255,0.05); border-left: 2px solid var(--cyan); color: var(--text);
  }}
  .verdict.good {{ background: rgba(43,255,168,0.07); border-left-color: var(--green); }}
  .verdict b {{ color: var(--cyan); }}
  .verdict.good b {{ color: var(--green); }}
  .badge {{
    font-size: 11px; font-family: 'Share Tech Mono', monospace; padding: 3px 8px;
    border-radius: 3px; letter-spacing: 0.5px;
  }}
  .badge.v {{ background: rgba(43,255,168,0.15); color: var(--green); border: 1px solid rgba(43,255,168,0.4); }}
  .badge.m {{ background: rgba(123,91,255,0.15); color: #b4a1ff; border: 1px solid rgba(123,91,255,0.4); }}
  .badge.s {{ background: rgba(255,176,32,0.15); color: var(--amber); border: 1px solid rgba(255,176,32,0.45); letter-spacing: 0; }}
  .ev.starred {{ border-left: 3px solid var(--amber); }}
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
    <h1>⚡ ODDS // TRACKER<span>ТРЕКЕР ДВИЖЕНИЯ КОЭФФИЦИЕНТОВ</span></h1>
    <div class="meta">
      <span class="{freshness_class}">{freshness_label}</span><br>
      обновлено {updated_ago}<br>
      обновление каждые {poll_interval} мин
    </div>
  </div>

  <div class="card intro">
    <h2>❓ Что это за продукт</h2>
    <p>Автоматический трекер: каждые {poll_interval} минут снимает коэффициенты по всему рынку,
    сводит их в <b>одну карточку на событие</b> и считает, есть ли смысл в ставке. Всё
    важное дублируется алертом в Telegram.</p>
    <p>Как считается цена входа: у «шарп»-контор (Pinnacle, 1xBet) берётся линия и из
    неё убирается маржа букмекера — получается <b>справедливый коэффициент</b>, то есть
    цена, при которой ставка выходит в ноль. Если где-то на рынке дают <b>выше</b>
    справедливой — это и есть запас, ради которого всё работает.</p>
    <p>Каждый сигнал потом проверяется по факту: <b>сработал ли исход</b> и <b>CLV</b> —
    продолжила ли линия двигаться в ту же сторону до старта матча. CLV честнее: исход
    можно угадать на удаче, движение рынка — нет.</p>
  </div>

  <div class="card">
    <h2>💎 Сводка по рынку</h2>
    <p class="note">Одна карточка на событие. <b>Коэффициент</b> — разброс по всему рынку
    от минимума до максимума. <b>Справедливо</b> — расчётная цена без маржи; если где-то
    дают выше неё, есть запас (💎). <b>Просело у N из M</b> — у скольких контор из
    котирующих линия пошла вниз.<br>
    <b>⭐ Звёзды — это уверенность в сигнале, и считаются они по числу контор, а не по
    величине скачка.</b> Одна контора подвинула цену — это может быть чья-то одиночная
    ставка или ошибка трейдера. Когда одно и то же просело сразу у многих независимых
    контор за полчаса — это заходят информированные деньги, и такое движение отрабатывает
    заметно чаще. ⭐ — просело у одной конторы, ⭐⭐ — у двух-трёх, ⭐⭐⭐ — у четырёх и больше
    либо при участии шарп-конторы. Порог для алерта — движение от {threshold_pct}%.</p>
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


def _outcome_row(o: dict) -> str:
    if abs(o["max_price"] - o["min_price"]) < 0.01:
        price = f"{o['max_price']:.2f}"
    else:
        price = f"{o['min_price']:.2f} – {o['max_price']:.2f}"

    fair = f"справедливо {o['fair_price']:.2f}" if o.get("fair_price") else ""
    if o.get("down_count"):
        pct = f" {o['avg_down_pct']:.1f}%" if o.get("avg_down_pct") is not None else ""
        move = (f"<span class='down'>↓ просело у {o['down_count']} "
                f"из {o['books_count']}{pct}</span>")
    else:
        move = ""
    return (
        f"<tr><td>{html.escape(o['name'])}</td>"
        f"<td class='price'>{price}</td>"
        f"<td class='fair'>{fair}</td>"
        f"<td class='mv'>{move}</td></tr>"
    )


def _event_card(s: dict) -> str:
    stars = s.get("stars", 0)
    cls = "starred" if stars >= 3 else ("value" if s.get("has_value") else ("move" if s.get("has_move") else ""))
    badges = ""
    if stars:
        badges += f"<span class='badge s'>{'⭐' * stars}</span> "
    if s.get("has_value"):
        badges += f"<span class='badge v'>💎 запас {s['best_value']['edge_pct']:+.1f}%</span> "
    if s.get("has_move"):
        badges += "<span class='badge m'>📈 линия двигается</span>"

    name = f"{html.escape(s.get('home_team') or '?')} — {html.escape(s.get('away_team') or '?')}"
    rows = "".join(_outcome_row(o) for o in s["outcomes"])
    vcls = "verdict good" if s.get("has_value") else "verdict"
    extra = ""
    if s.get("best_value"):
        extra = f" Лучшая цена у <b>{html.escape(s['best_value']['best_book'])}</b>."
    return (
        f"<div class='ev {cls}'>"
        f"<div class='ev-head'><div><div class='ev-name'>{name}</div>"
        f"<div class='ev-when'>старт {_fmt_start(s.get('start_time'))}</div></div>"
        f"<div>{badges}</div></div>"
        f"<table class='oc'>{rows}</table>"
        f"<div class='{vcls}'><b>ИТОГ:</b> {html.escape(s['verdict'])}{extra}</div>"
        f"</div>"
    )


def _summaries_html(summaries: list, limit: int = 40) -> str:
    interesting = [s for s in summaries if s.get("has_value") or s.get("has_move")]
    shown = interesting or summaries
    if not shown:
        return '<p class="empty">Данных пока нет — дождитесь первого опроса.</p>'
    out = "".join(_event_card(s) for s in shown[:limit])
    if len(shown) > limit:
        out += (f"<p class='empty'>...и ещё {len(shown) - limit} "
                f"{_plural(len(shown) - limit, 'событие', 'события', 'событий')}.</p>")
    if not interesting:
        out = ('<p class="note">Сейчас ни по одному событию нет ни движения линии, '
               'ни запаса над справедливой ценой — показаны текущие котировки.</p>' + out)
    return out


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
        min_edge=f"{MIN_EDGE_PCT:.0f}",
        summaries_html=_summaries_html(summaries or []),
        stats_card=_stats_card(storage.alert_stats()),
    )
    # git does not track empty directories, so a fresh CI checkout has no
    # dashboard/ folder yet -- make sure it exists before writing.
    os.makedirs(os.path.dirname(DASHBOARD_PATH), exist_ok=True)
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return DASHBOARD_PATH
