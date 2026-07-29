"""Renders a self-contained HTML dashboard: latest odds snapshot + recent spikes.
Overwrites dashboard/index.html on every run so it always reflects the last poll."""
import html
import os
from datetime import datetime, timezone

from config import DASHBOARD_PATH, ASIAN_SHARP_BOOKMAKERS, REGION_LABELS, get_region
import storage

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ODDS//TRACKER</title>
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
  .meta {{ color: var(--dim); font-size: 12px; font-family: 'Share Tech Mono', monospace; text-align: right; }}
  .meta .live {{ color: var(--green); }}
  .live::before {{ content: '● '; }}
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
    margin: 0 0 14px; color: var(--text); display: flex; align-items: center; gap: 8px;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13.5px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--panel-border); }}
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
  .region-tag {{ font-size: 11px; color: var(--dim); margin-left: 4px; }}
  .cascade {{ background: rgba(255, 59, 92, 0.10); }}
  .cascade-tag {{
    color: #fff; background: linear-gradient(90deg, var(--red), var(--magenta));
    font-weight: 700; font-size: 11px; padding: 2px 7px; border-radius: 3px; margin-right: 4px;
    box-shadow: 0 0 10px rgba(255, 59, 92, 0.55);
  }}
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
  footer {{ text-align: center; color: var(--dim); font-size: 11px; font-family: 'Share Tech Mono', monospace; margin-top: 30px; letter-spacing: 1px; }}
</style>
</head>
<body>
<div class="wrap">
  <div class="banner">
    <h1>⚡ ODDS // TRACKER<span>SHARP MONEY SIGNAL SYSTEM</span></h1>
    <div class="meta"><span class="live">LIVE</span> · обновлено {updated_at}</div>
  </div>

  <div class="card">
    <h2>📊 Статистика алертов</h2>
    {stats_card}
  </div>

  <div class="card">
    <h2>🌏 Азия vs 🇪🇺 Европа</h2>
    {region_table}
  </div>

  <div class="card">
    <h2>🎯 Sharp vs Public divergence</h2>
    {digest_table}
  </div>

  <div class="card">
    <h2>🚨 Recent spikes</h2>
    {spikes_table}
  </div>

  <div class="card">
    <h2>📡 Latest snapshot <span style="text-transform:none;color:var(--dim);font-weight:500;">(последние {n} линий)</span></h2>
    {snapshot_table}
  </div>

  <footer>ODDS-TRACKER · Pinnacle / SBOBET / IBCBET / Maxbet / 1xBet и др. · auto-refresh every poll cycle</footer>
</div>
</body>
</html>
"""


def _region_badge(bookmaker: str) -> str:
    label = REGION_LABELS.get(get_region(bookmaker), "")
    return f"<span class='region-tag'>{html.escape(label)}</span>" if label else ""


def _spikes_table(spikes):
    if not spikes:
        return '<p class="empty">Пока нет зафиксированных скачков.</p>'
    rows = []
    for s in spikes:
        cls = "sharp" if s["is_sharp_book"] else ""
        row_cls = "cascade" if s.get("is_cascade") else ""
        direction_cls = "up" if s["pct_change"] > 0 else "down"
        line_label = s.get("label") or f"{s['market_id']}/{s['outcome_id']}"
        cascade_mark = (
            f"<span class='cascade-tag'>🚨 x{s['cascade_count']}</span> " if s.get("is_cascade") else ""
        )
        rows.append(
            f"<tr class='{row_cls}'><td class='{cls}'>{cascade_mark}{html.escape(s['bookmaker'])}{_region_badge(s['bookmaker'])}</td>"
            f"<td>{html.escape(str(s['fixture_id']))}</td>"
            f"<td>{html.escape(line_label)}</td>"
            f"<td>{s['prev_price']:.2f} → {s['price']:.2f}</td>"
            f"<td class='{direction_cls}'>{s['pct_change'] * 100:+.1f}%</td></tr>"
        )
    return (
        "<table><tr><th>Bookmaker</th><th>Fixture</th><th>Line</th>"
        "<th>Odds</th><th>Change</th></tr>" + "".join(rows) + "</table>"
    )


def _stats_card(stats: dict):
    win_rate = stats["win_rate"]
    win_rate_html = f"{win_rate:.0f}%" if win_rate is not None else "—"
    summary = f"""
    <div class="stat-row">
      <div class="stat"><b>{stats['total']}</b>алертов всего</div>
      <div class="stat"><b>{stats['resolved']}</b>матч завершён и проверен</div>
      <div class="stat"><b>{stats['pending']}</b>ждут завершения матча</div>
      <div class="stat"><b>{win_rate_html}</b>win rate (по 1X2-рынкам)</div>
    </div>
    """
    if not stats["recent"]:
        return summary + '<p class="empty">Пока нет проверенных алертов -- появятся, как только завершится первый отслеживаемый матч.</p>'
    rows = []
    for fixture_id, bookmaker, label, direction, alert_type, result, resolved_at in stats["recent"]:
        cls = "hit" if result == "hit" else ("miss" if result == "miss" else "")
        result_label = {"hit": "✅ сработал", "miss": "❌ не сработал", "n/a": "н/д (не 1X2)"}.get(result, result)
        rows.append(
            f"<tr><td>{html.escape(str(fixture_id))}</td><td>{html.escape(bookmaker)}</td>"
            f"<td>{html.escape(label or '')}</td><td>{html.escape(alert_type)}</td>"
            f"<td class='{cls}'>{result_label}</td></tr>"
        )
    table = (
        "<table><tr><th>Fixture</th><th>Bookmaker</th><th>Line</th>"
        "<th>Type</th><th>Result</th></tr>" + "".join(rows) + "</table>"
    )
    return summary + table


def _region_table(rows):
    if not rows:
        return '<p class="empty">Пока нет расхождений Азия vs Европа ≥ порога.</p>'
    out = []
    for d in rows:
        cls = "up" if d["divergence_pct"] > 0 else "down"
        out.append(
            f"<tr><td>{html.escape(str(d['fixture_id']))}</td><td>{html.escape(d['label'])}</td>"
            f"<td>{d['asia_avg']:.2f} ({html.escape(', '.join(d['asia_books']))})</td>"
            f"<td>{d['europe_avg']:.2f} ({html.escape(', '.join(d['europe_books']))})</td>"
            f"<td class='{cls}'>{d['divergence_pct'] * 100:+.1f}%</td></tr>"
        )
    return (
        "<table><tr><th>Fixture</th><th>Line</th><th>🌏 Азия avg</th>"
        "<th>🇪🇺 Европа avg</th><th>Gap</th></tr>" + "".join(out) + "</table>"
    )


def _digest_table(divergences):
    if not divergences:
        return '<p class="empty">Пока нет расхождений sharp vs public ≥ порога.</p>'
    rows = []
    for d in divergences:
        cls = "up" if d["divergence_pct"] > 0 else "down"
        rows.append(
            f"<tr><td>{html.escape(str(d['fixture_id']))}</td><td>{html.escape(d['label'])}</td>"
            f"<td>{d['sharp_avg']:.2f} ({html.escape(', '.join(d['sharp_books']))})</td>"
            f"<td>{d['public_avg']:.2f} ({html.escape(', '.join(d['public_books']))})</td>"
            f"<td class='{cls}'>{d['divergence_pct'] * 100:+.1f}%</td></tr>"
        )
    return (
        "<table><tr><th>Fixture</th><th>Line</th><th>Sharp avg</th>"
        "<th>Public avg</th><th>Gap</th></tr>" + "".join(rows) + "</table>"
    )


def _snapshot_table(rows):
    if not rows:
        return '<p class="empty">Нет данных — запусти первый poll.</p>'
    out = []
    for fetched_at, fixture_id, bookmaker, market_id, outcome_id, player_key, price, label in rows:
        cls = "sharp" if bookmaker.lower() in ASIAN_SHARP_BOOKMAKERS else ""
        out.append(
            f"<tr><td>{html.escape(fetched_at)}</td><td class='{cls}'>{html.escape(bookmaker)}{_region_badge(bookmaker)}</td>"
            f"<td>{html.escape(str(fixture_id))}</td><td>{html.escape(label or f'{market_id}/{outcome_id}')}</td>"
            f"<td>{price:.2f}</td></tr>"
        )
    return (
        "<table><tr><th>Fetched</th><th>Bookmaker</th><th>Fixture</th>"
        "<th>Line</th><th>Price</th></tr>" + "".join(out) + "</table>"
    )


def render_dashboard(spikes: list, divergences: list = None, region_rows: list = None, snapshot_limit: int = 200):
    rows = storage.recent_snapshots(limit=snapshot_limit)
    html_out = PAGE_TEMPLATE.format(
        updated_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
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
