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

# Signed, so a forwarded message still says where it came from.
BRAND = "STEAMLINE"

DISCLAIMER = (
    f"<b>{BRAND}</b> · видим деньги раньше рынка\n"
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

    # Moves we are NOT betting -- the old price is gone, or only one book
    # shifted. Requested 2026-08-01: "эти движения тоже мы должны озвучивать,
    # хоть и не поставили, может кто-то поставит." Someone with an account we
    # don't cover may still get on, and staying silent about a move we saw is
    # hiding information rather than protecting anyone. They go in a separate
    # block, once each, clearly marked as not-a-bet.
    seen_only = [s for s in summaries
                 if s.get("move_is_new", False) and s not in actionable]

    if not actionable and not seen_only:
        return

    strong = sum(1 for s in actionable if s.get("stars", 0) >= 3)
    opt = sum(1 for s in actionable if s.get("strategy") == "optimal")
    header = f"⚡ <b>Сигналы — {len(actionable)}</b>"
    if strong:
        header += f"\n⭐⭐⭐ подтверждено рынком: <b>{strong}</b>"
    if opt:
        header += f"\n🟢 из них оптимальных (коэф. ≤ {OPTIMAL_MAX_PRICE:g}): <b>{opt}</b>"

    body = "\n\n➖➖➖\n\n".join(_format_event(s) for s in actionable[:max_events])
    if not actionable:
        header, body = "👀 <b>Движения без ставки</b>", ""

    footer = ""
    if len(actionable) > max_events:
        footer += f"\n\n<i>...и ещё {len(actionable) - max_events} на сайте.</i>"
    if seen_only:
        footer += _format_seen_only(seen_only, max_events)
    if dashboard_url:
        footer += f"\n\n🌐 {dashboard_url}"
    footer += f"\n\n{DISCLAIMER}"

    send_telegram_message(f"{header}\n\n{body}{footer}".replace("\n\n\n", "\n\n"))


def notify_digest(d: dict, dashboard_url: str = None):
    """The "still here, here is what the market did" heartbeat.

    Requested 2026-08-10: "чтобы отчеты были в бота, а то сижу втыкаю". The bot
    used to speak only when a signal fired, which meant a quiet market and a
    dead poller produced exactly the same thing -- nothing. That ambiguity is
    not theoretical: it is what let a 46-hour outage and then a full day of
    detector blindness pass unnoticed this week.

    So this reports the MEASUREMENT, not only the outcome. "Сверили 380 линий,
    до порога дошли 2" is a claim that can be checked and argued with; silence
    is not. When nothing fired it says so plainly instead of dressing the gap
    up -- an empty market is a legitimate answer, a broken one is not, and
    these numbers are what tells the two apart.
    """
    lines = [f"📊 <b>{BRAND} · сводка за {d.get('hours', 3):g} ч</b>"]

    watched = d.get("lines_watched") or 0
    if watched:
        blind = d.get("lines_blind") or 0
        moved = f", шевельнулось {d['lines_moved']}" if d.get("lines_moved") else ""
        lines.append(f"Сверено <b>{watched - blind}</b> из <b>{watched}</b> "
                     f"линий в последнем срезе{moved}")

    lines.append("")
    moves = d.get("movements") or 0
    sigs = d.get("signals") or 0
    thr = d.get("threshold", 10)
    if sigs:
        lines.append(f"⚡ Сигналов: <b>{sigs}</b> · движений от {thr:g}%: {moves}")
    elif moves:
        lines.append(f"Движений от {thr:g}%: <b>{moves}</b>, но ни одно не дошло до ставки.")
    else:
        lines.append(f"Ни одного падения от {thr:g}% — рынок стоял.")

    # Only the buckets that actually caught something, so a clean funnel stays
    # one short line instead of six zeroes.
    reasons = [(label, d.get(key) or 0) for key, label in (
        ("thin_market", "двинулась одна контора"),
        ("all_books_moved", "просело у всех, брать негде"),
        ("entry_too_low", "вход не дотянул по цене"),
        ("low_stars", "меньше трёх звёзд"),
        ("off_band", "коэффициент вне полосы"),
        ("too_far", "матч слишком далеко"),
    ) if (d.get(key) or 0) > 0]
    if reasons:
        lines.append("<i>не взяли: " + ", ".join(f"{lbl} — {n}" for lbl, n in reasons) + "</i>")

    if d.get("open_bets"):
        nxt = f", ближайший старт {html.escape(str(d['next_start']))}" if d.get("next_start") else ""
        lines.append("")
        lines.append(f"🎯 Открытых ставок: <b>{d['open_bets']}</b>{nxt}")

    if d.get("graded"):
        lines.append(f"✅ Рассчитано за период: {d['graded']}")

    if d.get("credits") is not None:
        days = f", хватит на ~{d['days_left']:.0f} дн." if d.get("days_left") else ""
        lines.append("")
        lines.append(f"<i>Кредитов {d['credits']:,}".replace(",", " ") + days
                     + f" · опрос раз в {d.get('poll_minutes', 20):g} мин</i>")

    if dashboard_url:
        lines.append(f"\n🌐 {dashboard_url}")

    send_telegram_message("\n".join(lines))


def _format_seen_only(moves: list, limit: int) -> str:
    """Compact list of moves we spotted but did not bet.

    Deliberately terse and deliberately honest about WHY each one is not a
    signal -- a line here that looked like a recommendation would be the
    worst possible outcome, since the price it quotes is usually already gone.
    """
    lines = []
    for s in sorted(moves, key=lambda x: -abs((x.get("bet") or {}).get("drop_pct") or 0))[:limit]:
        bet = s.get("bet") or {}
        who = html.escape(bet.get("name") or "")
        home = html.escape(s.get("home_team") or "?")
        away = html.escape(s.get("away_team") or "?")
        if not s.get("well_evidenced", True):
            why = "двинулась одна контора"
        elif not s.get("has_entry"):
            why = "старую цену уже нигде не дают"
        else:
            why = "вход не дотянул по цене"
        lines.append(
            f"• <b>{home} — {away}</b> · {who} "
            f"{bet.get('old_price', 0):.2f}→{bet.get('new_price', 0):.2f} "
            f"(−{abs(bet.get('drop_pct') or 0):.0f}%, {bet.get('down_count', 0)}"
            f"/{bet.get('books_count', 0)} контор) — <i>{why}</i>"
        )
    extra = ""
    if len(moves) > limit:
        extra = f"\n<i>...и ещё {len(moves) - limit} на сайте.</i>"
    return ("\n\n👀 <b>Движения без ставки</b>\n"
            "<i>Мы сюда не заходим — но деньги там были. Если у тебя есть контора,"
            " которая ещё не подвинулась, смотри сам.</i>\n"
            + "\n".join(lines) + extra)
