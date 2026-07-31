"""Sends consolidated event cards to Telegram.

Format follows the user's own reasoning about the bet (2026-07-29):

    "Был коэффициент 3, просел до 2.1, желательно проставить за 3."

So each card answers, in order: which outcome did money go into, what was the
price before and what is it now, and where can that same price still be taken.
Nothing else. Earlier versions listed every outcome with a fair-value figure
and a bookmaker list in parentheses, which buried the one line that matters.

Only events whose drop cleared SPIKE_THRESHOLD_PCT AND still have somewhere to
bet are sent -- a move you can no longer get on is not worth a notification,
and neither is a 1% drift.
"""
import html
from datetime import datetime, timezone

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


def _left(iso: str) -> str:
    """" · через 3 ч 40 мин" -- how long until kick-off, or nothing if unknown."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return ""
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return " · матч уже начался"
    mins = int(secs // 60)
    if mins < 60:
        return f" · через {mins} мин"
    hours, mins = divmod(mins, 60)
    if hours < 24:
        return f" · через {hours} ч {mins:02d} мин"
    return f" · через {hours // 24} дн {hours % 24} ч"


def _format_event(s: dict) -> str:
    bet = s.get("bet") or {}
    home = html.escape(s.get("home_team") or "?")
    away = html.escape(s.get("away_team") or "?")
    start = _fmt_start(s.get("start_time"))
    stars = "⭐" * s.get("stars", 0)
    name = html.escape(bet.get("name") or "")

    lines = [f"{stars} <b>{home} — {away}</b>".strip()]
    if start:
        # Time left matters more than the clock time: it decides whether this
        # is something to act on now or to note for later.
        lines.append(f"<i>старт {html.escape(start)}{_left(s.get('start_time'))}</i>")

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

    # What the optimal line does with this same event. It is no longer just a
    # label saying "counts / doesn't count": above the cut-off that line enters
    # through the double chance or a handicap instead of skipping, so the
    # message has to name the actual second bet.
    opt = s.get("optimal")
    lines.append("")
    if not opt:
        lines.append("🔴 <i>Только агрессивная — мягкого входа в это событие нет.</i>")
    elif opt["kind"] == "straight":
        lines.append(f"🟢 <b>Оптимальная:</b> та же ставка за {opt['price']:.2f} "
                     f"<i>(в пределах {OPTIMAL_MAX_PRICE:g})</i>")
    elif opt.get("price"):
        lines.append(f"🟢 <b>Оптимальная:</b> {html.escape(opt['pick'])} ≈ {opt['price']:.2f}")
        if opt.get("note"):
            lines.append(f"<i>{html.escape(opt['note'])}</i>")
    elif opt.get("est_price"):
        # "~" because this price is derived from the moneyline, not quoted.
        lines.append(f"🟢 <b>Оптимальная:</b> {html.escape(opt['pick'])}")
        lines.append(f"<i>Расчётный коэффициент ~{opt['est_price']:.2f} — выведен из "
                     f"линии на матч, в конторе обычно на 5–10% ниже. Заходимость "
                     f"считаем, прибыль нет: цену мы не выкупаем.</i>")
    else:
        lines.append(f"🟡 <b>Оптимальная:</b> {html.escape(opt['pick'])}")
        lines.append("<i>Цену смотри в линии. В статистику этот вход не пойдёт — "
                     "форы мы не выкупаем и проверить её по счёту не можем.</i>")

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
    # "is_new" is set by main.py from the database insert. Absent (older
    # callers, tests) it defaults to True, so nothing can go silent by
    # accident -- the guard only ever suppresses a repeat we can prove is one.
    # It matters now because a move is measured against the price an hour ago,
    # so the same event stays actionable for many cycles in a row.
    actionable = [s for s in summaries
                  if s.get("alertable") and s.get("is_new", True)]
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
