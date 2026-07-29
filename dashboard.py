"""Renders a self-contained HTML dashboard: latest odds snapshot + recent spikes.
Overwrites dashboard/index.html on every run so it always reflects the last poll."""
import html
from datetime import datetime, timezone

from config import DASHBOARD_PATH, ASIAN_SHARP_BOOKMAKERS, REGION_LABELS, get_region
import storage

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<title>Odds Tracker</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; padding: 24px;
          background: #0b0d12; color: #e6e8ec; }}
  h1 {{ font-size: 20px; margin: 0 0 4px; }}
  .meta {{ color: #8a919e; font-size: 13px; margin-bottom: 20px; }}
  .card {{ background: #151821; border: 1px solid #262b38; border-radius: 10px;
           padding: 16px; margin-bottom: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #262b38; }}
  th {{ color: #8a919e; font-weight: 500; }}
  .sharp {{ color: #f5a623; font-weight: 600; }}
  .up {{ color: #33c481; }}
  .down {{ color: #ef5b5b; }}
  .empty {{ color: #8a919e; font-style: italic; }}
  .region-tag {{ font-size: 11px; color: #8a919e; margin-left: 4px; }}
</style>
</head>
<body>
  <h1>📈 Odds Movement Tracker</h1>
  <div class="meta">Last updated: {updated_at}</div>

  <div class="card">
    <h2>🌏 Азия vs 🇪🇺 Европа</h2>
    {region_table}
  </div>

  <div class="card">
    <h2>Sharp vs public divergence</h2>
    {digest_table}
  </div>

  <div class="card">
    <h2>Recent spikes (≥ threshold)</h2>
    {spikes_table}
  </div>

  <div class="card">
    <h2>Latest snapshot (most recent {n} lines)</h2>
    {snapshot_table}
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
        direction_cls = "up" if s["pct_change"] > 0 else "down"
        line_label = s.get("label") or f"{s['market_id']}/{s['outcome_id']}"
        rows.append(
            f"<tr><td class='{cls}'>{html.escape(s['bookmaker'])}{_region_badge(s['bookmaker'])}</td>"
            f"<td>{html.escape(str(s['fixture_id']))}</td>"
            f"<td>{html.escape(line_label)}</td>"
            f"<td>{s['prev_price']:.2f} → {s['price']:.2f}</td>"
            f"<td class='{direction_cls}'>{s['pct_change'] * 100:+.1f}%</td></tr>"
        )
    return (
        "<table><tr><th>Bookmaker</th><th>Fixture</th><th>Line</th>"
        "<th>Odds</th><th>Change</th></tr>" + "".join(rows) + "</table>"
    )


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
        region_table=_region_table(region_rows or []),
        digest_table=_digest_table(divergences or []),
        spikes_table=_spikes_table(spikes),
        snapshot_table=_snapshot_table(rows),
        n=snapshot_limit,
    )
    with open(DASHBOARD_PATH, "w", encoding="utf-8") as f:
        f.write(html_out)
    return DASHBOARD_PATH
