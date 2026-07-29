"""Sends consolidated event cards to Telegram.

Format follows the user's own reasoning about the bet (2026-07-29):

    "Был коэффициент 3, просел до 2.1, желательно проставить за 3."

So each card answers, in order: which outcome did money go into, what was the
price before and what is it now, and where can that same price still be taken.
Nothing else. Earlier versions listed every outcome with a fair-value figure
and a bookmaker list in parentheses, which buried the one line that matters.

Only events whose drop cleared the 10% threshold AND still have somewhere to
bet are sent -- a move you can no longer get on is not worth a notification,
and neither is a 1% drift.
"""
import html
from datetime import datetime

import requests

from config import (
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
    OPTIMAL_MAX_PRICE,
)

DISCLAIMER = (
    "<i>Это расчёт по движению рынка, а не рекомендация. "
    "Ставки — риск потерять деньги.</i>"
)


def _fmt_start(iso: str) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).strftime("%d.%m %H:%M UTC")
    except ValueError:
        return str(iso)


def _format_event(s: dict) -> str:
    bet = s.get("bet") or {}
    home = html.escape(s.get("home_team") or "?")
    away = html.escape(s.get("away_team") or "?")
    start = _fmt_start(s.get("start_time"))
    stars = "⭐" * s.get("stars", 0)
    name = html.escape(bet.get("name") or "")

    lines = [f"{stars} <b>{home} — {away}</b>".strip()]
    if start:
        lines.append(f"<i>старт {html.escape(start)}</i>")

    lines.append("")
    lines.append(f"💰 Деньги зашли на: <b>{name}</b>")
    lines.append(
        f"Был <b>{bet['old_price']:.2f}</b> → просел до <b>{bet['new_price']:.2f}</b>"
        f"  <i>({abs(bet['drop_pct']):.1f}%, у {bet['down_count']} из {bet['books_count']} контор)</i>"
    )

    if s.get("has_entry"):
        lines.append("")
        lines.append(f"✅ <b>СТАВИМ {name} за {bet['entry_price']:.2f}</b>")
        where = "\n".join(f"   • {html.escape(b)} — <b>{p:.2f}</b>" for b, p in bet["entries"])
        lines.append("Ещё не просело у:")
        lines.append(where)
    else:
        lines.append("")
        lines.append("⛔️ Вход закрыт — просело у всех, старую цену взять негде.")

    # Which strategy bucket this one counts towards, so the two win rates on
    # the site can be traced back to individual messages here.
    if s.get("strategy") == "optimal":
        lines.append(f"\n🟢 <i>Оптимальная — коэффициент не выше {OPTIMAL_MAX_PRICE:g}</i>")
    else:
        lines.append("\n🔴 <i>Агрессивная — коэффициент высокий</i>")

    safe = s.get("safe")
    if safe:
        lines.append("")
        if safe.get("price"):
            lines.append(f"🛡 <b>Безопасный вариант: {html.escape(safe['pick'])} "
                         f"≈ {safe['price']:.2f}</b>")
        else:
            lines.append(f"🛡 <b>Безопасный вариант: {html.escape(safe['pick'])}</b>")
        lines.append(f"<i>{html.escape(safe.get('note') or '')}</i>")

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
    """One digest per cycle, containing only actionable events: the drop
    cleared the threshold and there is still a bookmaker to take it at."""
    actionable = [s for s in summaries if s.get("alertable")]
    if not actionable:
        return

    strong = sum(1 for s in actionable if s.get("stars", 0) >= 3)
    opt = sum(1 for s in actionable if s.get("strategy") == "optimal")
    header = f"⚡ <b>Сигналы — {len(actionable)}</b>"
    if strong:
        header += f"\n⭐⭐⭐ подтверждено рынком: <b>{strong}</b>"
    if opt:
        header += f"\n🟢 из них оптимальных (коэф. ≤ {OPTIMAL_MAX_PRICE:g}): <b>{opt}</b>"

    body = "\n\n➖➖➖\n\n".join(_format_event(s) for s in actionable[:max_events])

    footer = ""
    if len(actionable) > max_events:
        footer += f"\n\n<i>...и ещё {len(actionable) - max_events} на сайте.</i>"
    if dashboard_url:
        footer += f"\n\n🌐 {dashboard_url}"
    footer += f"\n\n{DISCLAIMER}"

    send_telegram_message(f"{header}\n\n{body}{footer}")
