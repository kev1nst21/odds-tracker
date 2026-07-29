"""Sends spike alerts to Telegram."""
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID


def _format_spike(s: dict) -> str:
    direction = "UP" if s["pct_change"] > 0 else "DOWN"
    tag = "SHARP (Asian)" if s["is_sharp_book"] else "public"
    line = s.get("label") or f"market {s['market_id']} / outcome {s['outcome_id']}"
    return (
        f"[{tag}] {s['bookmaker']} | fixture {s['fixture_id']}\n"
        f"{line}\n"
        f"{s['prev_price']:.2f} -> {s['price']:.2f} ({direction} {abs(s['pct_change']) * 100:.1f}%)"
    )


def send_telegram_message(text: str):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[notifier] Telegram not configured, skipping send:\n" + text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    resp = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text}, timeout=10)
    if resp.status_code != 200:
        print(f"[notifier] Telegram send failed: {resp.status_code} {resp.text[:300]}")


def notify_spikes(spikes: list, max_messages: int = 10):
    if not spikes:
        return
    header = f"⚡ {len(spikes)} odds move(s) detected\n\n"
    body = "\n\n".join(_format_spike(s) for s in spikes[:max_messages])
    footer = ""
    if len(spikes) > max_messages:
        footer = f"\n\n...and {len(spikes) - max_messages} more, see dashboard."
    send_telegram_message(header + body + footer)


def _format_divergence(d: dict) -> str:
    return (
        f"{d['label']} | fixture {d['fixture_id']}\n"
        f"sharp avg {d['sharp_avg']:.2f} ({', '.join(d['sharp_books'])}) vs "
        f"public avg {d['public_avg']:.2f} ({', '.join(d['public_books'])})\n"
        f"gap: {d['divergence_pct'] * 100:+.1f}%"
    )


def notify_digest(divergences: list, max_messages: int = 5):
    """Sharp-vs-public summary -- the 'big picture' view across every
    tracked bookmaker/tournament, not just single-line spikes."""
    if not divergences:
        return
    header = f"📊 Sharp vs public digest -- {len(divergences)} line(s) diverging\n\n"
    body = "\n\n".join(_format_divergence(d) for d in divergences[:max_messages])
    footer = ""
    if len(divergences) > max_messages:
        footer = f"\n\n...and {len(divergences) - max_messages} more, see dashboard."
    send_telegram_message(header + body + footer)


def _format_region(d: dict) -> str:
    return (
        f"{d['label']} | fixture {d['fixture_id']}\n"
        f"🌏 Азия avg {d['asia_avg']:.2f} ({', '.join(d['asia_books'])}) vs "
        f"🇪🇺 Европа avg {d['europe_avg']:.2f} ({', '.join(d['europe_books'])})\n"
        f"gap: {d['divergence_pct'] * 100:+.1f}%"
    )


def notify_region_digest(region_rows: list, max_messages: int = 5):
    """Pure geographic summary (Asia vs Europe), separate from the sharp/public
    digest above -- a clearer view when you just want to see where the two
    markets disagree, regardless of which side is 'sharper'."""
    if not region_rows:
        return
    header = f"🌏🇪🇺 Азия vs Европа -- {len(region_rows)} line(s) diverging\n\n"
    body = "\n\n".join(_format_region(d) for d in region_rows[:max_messages])
    footer = ""
    if len(region_rows) > max_messages:
        footer = f"\n\n...and {len(region_rows) - max_messages} more, see dashboard."
    send_telegram_message(header + body + footer)
