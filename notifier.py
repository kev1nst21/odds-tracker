"""Sends consolidated event summaries to Telegram.

2026-07-29, second pass. What changed and why:
  - One message block per EVENT, not per odds line. Before, one football match
    could fire four separate alerts describing the same market move from
    different sides ("+16.7% here, -15.4% there"), which read as contradictory
    noise. Now every event appears once.
  - Bookmaker name lists in parentheses are gone. Only the single book holding
    the best price is named, because that's the only one worth acting on.
  - Prices are shown as a market range (from-to) instead of a single book's
    number, so the spread across the market is visible at a glance.
  - Each event ends with the analyst line: the price to bet from (no-vig fair
    price) and how much room is left over it.
  - The separate Asia-vs-Europe digest was removed entirely; that split is
    merged into the single summary, which cuts the message count per cycle
    from three to one.
"""
import html

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

DISCLAIMER = (
    "<i>Расчёт по модели справедливой цены, а не рекомендация. "
    "Ставки — это риск потерять деньги.</i>"
)


def _fmt_start(iso: str) -> str:
    if not iso:
        return ""
    text = str(iso).replace("Z", "+00:00")
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(text)
        return dt.strftime("%d.%m %H:%M UTC")
    except ValueError:
        return str(iso)


def _outcome_line(o: dict) -> str:
    """One outcome: name, market range, how many books moved it, fair price."""
    name = html.escape(o["name"])
    if abs(o["max_price"] - o["min_price"]) < 0.01:
        price = f"{o['max_price']:.2f}"
    else:
        price = f"{o['min_price']:.2f}–{o['max_price']:.2f}"

    marks = []
    if o.get("down_count"):
        marks.append(f"↓ просело у {o['down_count']} из {o['books_count']}")
        if o.get("avg_down_pct") is not None:
            marks.append(f"{o['avg_down_pct']:.1f}%")
    if o.get("fair_price"):
        marks.append(f"справедливо {o['fair_price']:.2f}")
    tail = f"  <i>{html.escape(' · '.join(marks))}</i>" if marks else ""
    return f"• {name} — <b>{price}</b>{tail}"


def _format_event(s: dict) -> str:
    home = html.escape(s.get("home_team") or "?")
    away = html.escape(s.get("away_team") or "?")
    start = _fmt_start(s.get("start_time"))
    stars = "⭐" * s.get("stars", 0)
    flag = "💎" if s.get("has_value") else "📈"
    title = f"{stars} {flag}".strip() if stars else flag

    lines = [f"{title} <b>{home} — {away}</b>"]
    if start:
        lines.append(f"<i>старт {html.escape(start)}</i>")
    lines += [_outcome_line(o) for o in s["outcomes"]]

    bv = s.get("best_value")
    if bv:
        lines.append(
            f"<b>ИТОГ:</b> {html.escape(s['verdict'])}\n"
            f"Лучшая цена сейчас у <b>{html.escape(bv['best_book'])}</b>."
        )
    else:
        lines.append(f"<b>ИТОГ:</b> {html.escape(s['verdict'])}")
    return "\n".join(lines)


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[notifier] Telegram not configured, skipping send:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=10,
    )
    if resp.status_code != 200:
        print(f"[notifier] Telegram send failed: {resp.status_code} {resp.text[:300]}")


def notify_summaries(summaries: list, max_events: int = 6, dashboard_url: str = None):
    """Single digest for the whole cycle. Only events that actually have
    something to say (value found, or the line moved) are sent -- a list of
    events where nothing happened is not worth a notification."""
    interesting = [s for s in summaries if s.get("has_value") or s.get("has_move")]
    if not interesting:
        return

    starred = sum(1 for s in interesting if s.get("stars", 0) >= 3)
    value_count = sum(1 for s in interesting if s.get("has_value"))
    header = f"⚡ <b>Сводка по рынку — {len(interesting)} событий</b>"
    if starred:
        header += f"\n⭐⭐⭐ движение подтверждено рынком: <b>{starred}</b>"
    if value_count:
        header += f"\n💎 есть запас над справедливой ценой: <b>{value_count}</b>"

    body = "\n\n".join(_format_event(s) for s in interesting[:max_events])

    footer = ""
    if len(interesting) > max_events:
        footer += f"\n\n<i>...и ещё {len(interesting) - max_events} событий на сайте.</i>"
    if dashboard_url:
        footer += f"\n\n🌐 {dashboard_url}"
    footer += f"\n\n{DISCLAIMER}"

    send_telegram_message(f"{header}\n\n{body}{footer}")
