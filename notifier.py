"""Sends spike alerts to Telegram.

2026-07-29: rewritten per explicit user request -- event names now show in
**bold** (Telegram HTML parse_mode), and every alert ends with a clear
"ИТОГ:" line explaining what the move likely means and how much to trust it,
instead of a bare price-change line the user has to interpret themselves."""
import html

import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def _event_line(s: dict) -> str:
    home = html.escape(s.get("home_team") or "?")
    away = html.escape(s.get("away_team") or "?")
    return f"<b>{home} vs {away}</b>"


def _outcome_text(s: dict) -> str:
    label = s.get("label") or f"{s.get('market_id')}/{s.get('outcome_id')}"
    # label is "Home vs Away: Outcome" -- keep only the outcome part here,
    # the event name is already shown in bold above it.
    if ":" in label:
        return label.split(":", 1)[1].strip()
    return label


def _itog_for_spike(s: dict) -> str:
    """One clear, plain-language line explaining what this move likely means
    and how much weight to give it -- the "so what" the user explicitly
    asked for, not just raw numbers."""
    who = "Шарпы (Азия)" if s["is_sharp_book"] else "Паблик-контора"
    outcome = _outcome_text(s)
    if s["pct_change"] < 0:
        meaning = f"{who} двигает деньги НА «{outcome}» — котировка укорачивается, рынок сильнее верит в этот исход."
    else:
        meaning = f"{who} уходит ОТ «{outcome}» — котировка растёт, рынок теряет веру в этот исход."
    if s["is_sharp_book"] and s.get("is_cascade"):
        confidence = "Надёжность: ВЫСОКАЯ — это шарп-букмекер, и линия уже второй раз подряд идёт в одну сторону."
    elif s["is_sharp_book"]:
        confidence = "Надёжность: высокая — движение пришло со стороны шарп-букмекера (обычно значит, что деньги информированные)."
    elif s.get("is_cascade"):
        confidence = "Надёжность: средняя — движение повторное, но с публичной конторы, стоит подтвердить у шарпов."
    else:
        confidence = "Надёжность: средняя/низкая — публичная контора, может быть шум или реакция на ставки любителей."
    return f"{meaning} {confidence}"


def _format_spike(s: dict) -> str:
    direction = "⬆️ РОСТ" if s["pct_change"] > 0 else "⬇️ ПАДЕНИЕ"
    tag = "🌏 ШАРП (Азия)" if s["is_sharp_book"] else "публичная контора"
    prefix = f"🚨 СУПЕР-АЛЕРТ (x{s['cascade_count']} подряд) " if s.get("is_cascade") else "⚡ "
    return (
        f"{prefix}{_event_line(s)}\n"
        f"[{tag}] {html.escape(s['bookmaker'])} · {html.escape(_outcome_text(s))}\n"
        f"{s['prev_price']:.2f} → {s['price']:.2f} ({direction} {abs(s['pct_change']) * 100:.1f}%)\n"
        f"<b>ИТОГ:</b> {html.escape(_itog_for_spike(s))}"
    )


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


def notify_spikes(spikes: list, max_messages: int = 10):
    if not spikes:
        return

    # Cascades (repeated same-direction moves within the window) go out as
    # their own urgent message first, separate from the regular digest --
    # these are the ones most likely to actually mean something.
    cascades = [s for s in spikes if s.get("is_cascade")]
    if cascades:
        header = f"🚨🚨 <b>{len(cascades)} СУПЕР-АЛЕРТ(ов)</b> — повторное движение линии в одну сторону\n\n"
        body = "\n\n".join(_format_spike(s) for s in cascades[:max_messages])
        send_telegram_message(header + body)

    header = f"⚡ <b>{len(spikes)} движение(й) котировок</b>\n\n"
    body = "\n\n".join(_format_spike(s) for s in spikes[:max_messages])
    footer = ""
    if len(spikes) > max_messages:
        footer = f"\n\n...и ещё {len(spikes) - max_messages}, см. дашборд."
    send_telegram_message(header + body + footer)


def _divergence_event_line(d: dict) -> str:
    home = html.escape(d.get("home_team") or "?")
    away = html.escape(d.get("away_team") or "?")
    return f"<b>{home} vs {away}</b>"


def _divergence_outcome(d: dict) -> str:
    label = d.get("label") or f"{d.get('market_id')}/{d.get('outcome_id')}"
    if ":" in label:
        return label.split(":", 1)[1].strip()
    return label


def _format_divergence(d: dict) -> str:
    gap_pct = d["divergence_pct"] * 100
    leading = "шарпы дают ЛУЧШУЮ котировку" if gap_pct > 0 else "паблик даёт ЛУЧШУЮ котировку"
    itog = (
        f"Разрыв {gap_pct:+.1f}% между шарпами и паблик-конторами — "
        f"{leading}. Обычно паблик подтягивается к шарпам, а не наоборот, "
        f"так что стоит смотреть в сторону шарп-цены, пока разрыв не закроется."
    )
    return (
        f"{_divergence_event_line(d)}\n"
        f"{html.escape(_divergence_outcome(d))}\n"
        f"шарп avg {d['sharp_avg']:.2f} ({html.escape(', '.join(d['sharp_books']))}) vs "
        f"паблик avg {d['public_avg']:.2f} ({html.escape(', '.join(d['public_books']))})\n"
        f"<b>ИТОГ:</b> {itog}"
    )


def notify_digest(divergences: list, max_messages: int = 5):
    """Sharp-vs-public summary -- the 'big picture' view across every
    tracked bookmaker/tournament, not just single-line spikes."""
    if not divergences:
        return
    header = f"📊 <b>Sharp vs Public — {len(divergences)} расхождение(й)</b>\n\n"
    body = "\n\n".join(_format_divergence(d) for d in divergences[:max_messages])
    footer = ""
    if len(divergences) > max_messages:
        footer = f"\n\n...и ещё {len(divergences) - max_messages}, см. дашборд."
    send_telegram_message(header + body + footer)


def _format_region(d: dict) -> str:
    gap_pct = d["divergence_pct"] * 100
    leading = "Азия впереди рынка" if gap_pct > 0 else "Европа впереди рынка"
    itog = f"Разрыв {gap_pct:+.1f}% между регионами — {leading}. Возможен сигнал раннего движения, который ещё не отыгран в другом регионе."
    return (
        f"{_divergence_event_line(d)}\n"
        f"{html.escape(_divergence_outcome(d))}\n"
        f"🌏 Азия avg {d['asia_avg']:.2f} ({html.escape(', '.join(d['asia_books']))}) vs "
        f"🇪🇺 Европа avg {d['europe_avg']:.2f} ({html.escape(', '.join(d['europe_books']))})\n"
        f"<b>ИТОГ:</b> {itog}"
    )


def notify_region_digest(region_rows: list, max_messages: int = 5):
    """Pure geographic summary (Asia vs Europe), separate from the sharp/public
    digest above -- a clearer view when you just want to see where the two
    markets disagree, regardless of which side is 'sharper'."""
    if not region_rows:
        return
    header = f"🌏🇪🇺 <b>Азия vs Европа — {len(region_rows)} расхождение(й)</b>\n\n"
    body = "\n\n".join(_format_region(d) for d in region_rows[:max_messages])
    footer = ""
    if len(region_rows) > max_messages:
        footer = f"\n\n...и ещё {len(region_rows) - max_messages}, см. дашборд."
    send_telegram_message(header + body + footer)
