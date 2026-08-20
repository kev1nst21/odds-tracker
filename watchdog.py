"""Watches the poller itself and unsticks it.

Written 2026-08-08, after the site sat frozen for 46 hours and nobody knew.

What happened: run #695 was dispatched at 06.08 16:00 UTC and went into the
"waiting" state -- the job never started at all. Because poll.yml uses

    concurrency: {group: poll-odds, cancel-in-progress: false}

that run held the group. Every scheduled run after it queued behind the stuck
head and was cancelled by the next one arriving: #696 through #710, fourteen in
a row, one to nine hours each. The last successful deploy was 06.08 16:01, so
the dashboard kept serving a two-day-old snapshot in which matches from the 5th
and 6th were still "матч идёт".

Two separate failures, and this file addresses both:

  1. NOTHING CLEARED THE QUEUE. `timeout-minutes` in poll.yml only starts
     counting once a job begins executing, so a run stuck BEFORE that is
     unbounded. Cancelling the head is all it takes -- the moment #695 was
     cancelled by hand, the next scheduled run started on its own. So: cancel
     any poll run that has sat queued or waiting longer than STUCK_AFTER_MIN.

  2. NOTHING RAISED ITS HAND. A frozen dashboard looks exactly like a quiet
     market, which is the failure mode that hides longest. So: if no poll run
     has succeeded in STALE_AFTER_MIN, say so in Telegram. One message per
     incident, not one per hour -- a watchdog that cries every hour gets muted,
     and a muted watchdog is worse than none.

Runs from its own workflow, in its own concurrency group, so it cannot be
blocked by the very queue it is meant to clear.
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

REPO = os.getenv("GITHUB_REPOSITORY", "kev1nst21/odds-tracker")
TOKEN = os.getenv("GH_TOKEN") or ""
# Optional. GITHUB_TOKEN is allowed to cancel runs but NOT to start a workflow
# (GitHub blocks that deliberately, to stop workflows spawning workflows). So
# restarting the chain needs the same PAT poll.yml already uses; without it we
# fall back to waiting for the next cron slot, which is at most 30 minutes.
PAT = os.getenv("GH_PAT") or ""
WORKFLOW = "poll.yml"

# A poll run takes ~30 minutes and the cron fires every 30. Anything still
# queued after 40 has missed its window and is now blocking its successors,
# which is strictly worse than not running at all.
STUCK_AFTER_MIN = int(os.getenv("STUCK_AFTER_MIN", "40"))
# Two missed windows. One missed window is normal -- GitHub's scheduler is
# openly unreliable and we measured it skipping slots long ago.
STALE_AFTER_MIN = int(os.getenv("STALE_AFTER_MIN", "100"))

# ЦЕПОЧКА ОБОРВАЛАСЬ, А ОПРОС ИДЁТ. Самая коварная поломка из возможных, и до
# 20.08 её не ловило ничто.
#
# poll.yml запускается двумя способами сразу: расписанием "7,37 * * * *", то
# есть дважды в час, и самозапуском в последнем шаге через WORKFLOW_PAT,
# который и даёт пятиминутный цикл. Если PAT протухнет или потеряет права,
# самозапуск молча перестанет работать -- а расписание останется. Опрос не
# встанет, он ПРОРЕДИТСЯ с пяти минут до тридцати.
#
# Проверка на "давно не было удачного прогона" такого не увидит: тридцать
# минут это далеко не сто. Сайт будет живой, цифры свежие, всё зелёное -- и
# только цена входа тихо станет хуже, потому что движение мы будем замечать в
# среднем на четверть часа позже. Поэтому меряем ФАКТИЧЕСКИЙ интервал между
# удачными прогонами и сравниваем с обещанным.
CHAIN_SLACK = float(os.getenv("CHAIN_SLACK", "2.5"))   # во сколько раз можно разойтись
CHAIN_MIN_RUNS = int(os.getenv("CHAIN_MIN_RUNS", "6"))

# СРОК ЖИЗНИ PAT. Дата известна заранее и вводится руками, потому что GitHub не
# отдаёт срок годности токена по API тому, кто им пользуется. Предупредить
# заранее -- единственный способ не узнать об этом постфактум.
PAT_EXPIRES = os.getenv("PAT_EXPIRES", "2026-08-28")
PAT_WARN_DAYS = int(os.getenv("PAT_WARN_DAYS", "10"))

API = "https://api.github.com"
HEAD = {"Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}"}


def _now():
    return datetime.now(timezone.utc)


def _age_min(iso: str) -> float:
    return (_now() - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 60


def _runs(limit: int = 40):
    r = requests.get(f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs",
                     headers=HEAD, params={"per_page": limit}, timeout=20)
    r.raise_for_status()
    return r.json().get("workflow_runs", [])


def _cancel(run: dict) -> bool:
    r = requests.post(f"{API}/repos/{REPO}/actions/runs/{run['id']}/cancel",
                      headers=HEAD, timeout=20)
    ok = r.status_code in (202, 409)  # 409 = already finishing, fine either way
    print(f"[watchdog] cancel #{run['run_number']} ({run['status']}, "
          f"{_age_min(run['created_at']):.0f} min old) -> HTTP {r.status_code}")
    return ok


def _dispatch() -> bool:
    """Ask for a fresh run now instead of waiting for the next cron slot."""
    if not PAT:
        print("[watchdog] no PAT -- leaving the restart to the cron schedule")
        return False
    r = requests.post(
        f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        headers={"Accept": "application/vnd.github+json",
                 "Authorization": f"Bearer {PAT}"},
        json={"ref": os.getenv("GITHUB_REF_NAME", "main")}, timeout=20)
    print(f"[watchdog] restart requested -> HTTP {r.status_code}")
    return r.status_code == 204


def _telegram(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("[watchdog] Telegram not configured:\n" + text)
        return
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat, "text": text, "parse_mode": "HTML",
                            "disable_web_page_preview": True}, timeout=15)
    if r.status_code != 200:
        print(f"[watchdog] Telegram failed: {r.status_code} {r.text[:200]}")


# One message per incident. Kept in its OWN cache, not alongside the odds
# database: a watchdog that can write into the file it is guarding is one bad
# line away from being the outage. If the state is missing we assume "healthy",
# which errs towards one extra alert rather than towards silence.
STATE = os.getenv("WATCHDOG_STATE", "wdstate/watchdog.json")


def _state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def _save_state(s: dict):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as fh:
        json.dump(s, fh)


def _cadence_gap_min(runs) -> float:
    """Медиана фактического интервала между удачными прогонами, в минутах.

    Медиана, а не среднее: один пропущенный слот (планировщик GitHub открыто
    ненадёжен) сдвинул бы среднее и заставил бы watchdog кричать на здоровой
    системе. Медиана переживает одиночный выброс и ломается только тогда,
    когда изменился сам режим -- а это ровно то, что мы хотим поймать.
    """
    stamps = sorted(
        (w["updated_at"] for w in runs if w.get("conclusion") == "success"),
        reverse=True)[:CHAIN_MIN_RUNS + 1]
    if len(stamps) < CHAIN_MIN_RUNS:
        return 0.0
    gaps = []
    for a, b in zip(stamps, stamps[1:]):
        ta = datetime.fromisoformat(a.replace("Z", "+00:00"))
        tb = datetime.fromisoformat(b.replace("Z", "+00:00"))
        gaps.append((ta - tb).total_seconds() / 60)
    gaps.sort()
    mid = len(gaps) // 2
    return gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2


def _expected_gap_min() -> float:
    """Сколько минут между прогонами обещает конфиг."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import config
        return float(config.POLL_INTERVAL_MINUTES)
    except Exception:                                          # noqa: BLE001
        return 5.0


def _pat_days_left() -> float:
    try:
        exp = datetime.fromisoformat(PAT_EXPIRES).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return 10 ** 6
    return (exp - _now()).total_seconds() / 86400


def main() -> int:
    if not TOKEN:
        print("[watchdog] GH_TOKEN missing -- nothing to do")
        return 0

    runs = _runs()
    if not runs:
        print("[watchdog] no runs returned")
        return 0

    running = any(w["status"] == "in_progress" for w in runs)

    # 1. clear the queue -- but ONLY when nothing is executing.
    #
    # A queued run is not evidence of a jam by itself: poll runs take about
    # half an hour and the cron fires every half hour, so a successor waiting
    # its turn is the design working. It is a jam precisely when the queue is
    # long and nothing is moving. Cancelling a legitimately-waiting run would
    # make this file a cause of outages instead of a cure for them.
    stuck = []
    if not running:
        stuck = [w for w in runs
                 if w["status"] in ("queued", "waiting", "pending", "requested")
                 and _age_min(w["created_at"]) > STUCK_AFTER_MIN]
        for w in stuck:
            _cancel(w)

    # 2. is the site actually being published?
    ok = [w for w in runs if w["conclusion"] == "success"]
    last_ok_min = _age_min(ok[0]["updated_at"]) if ok else 10 ** 6

    state = _state()
    was_down = bool(state.get("down"))
    is_down = last_ok_min > STALE_AFTER_MIN

    print(f"[watchdog] последний удачный прогон {last_ok_min:.0f} мин назад, "
          f"в работе сейчас: {running}, зависших снято: {len(stuck)}")

    if stuck:
        _dispatch()

    if is_down and not was_down:
        _telegram(
            "🔴 <b>STEAMLINE — опрос встал</b>\n"
            f"Последняя удачная выкладка была <b>{last_ok_min/60:.1f} ч</b> назад.\n"
            + (f"Снял {len(stuck)} зависших прогонов из очереди, "
               "перезапустил.\n" if stuck else
               "Зависших прогонов в очереди нет — причина другая, смотрю.\n")
            + "Сайт до перезапуска показывает старый срез — цифрам на нём "
              "сейчас верить нельзя."
        )
    elif was_down and not is_down:
        _telegram("🟢 <b>STEAMLINE — опрос снова идёт</b>\n"
                  "Выкладка прошла, данные на сайте свежие.")

    # 3. цепочка жива? Отдельный вопрос от "сайт жив".
    want = _expected_gap_min()
    got = _cadence_gap_min(runs)
    thinned = bool(got and want and got > want * CHAIN_SLACK)
    was_thinned = bool(state.get("thinned"))
    if got:
        print(f"[watchdog] фактический интервал {got:.1f} мин против "
              f"обещанных {want:.0f}")
    if thinned and not was_thinned:
        _telegram(
            "🟠 <b>STEAMLINE — цепочка оборвалась, опрос проредился</b>\n"
            f"Прогоны идут раз в <b>{got:.0f} мин</b> вместо обещанных "
            f"<b>{want:.0f}</b>.\n"
            "Сайт при этом живой и цифры свежие, поэтому обычная проверка "
            "молчит — но движение мы теперь замечаем позже, и цена входа "
            "становится хуже.\n"
            "Самая частая причина — истёк или потерял права WORKFLOW_PAT. "
            "Расписание дважды в час продолжает работать, так что это не "
            "остановка, а деградация."
        )
    elif was_thinned and not thinned:
        _telegram("🟢 <b>STEAMLINE — цепочка восстановилась</b>\n"
                  f"Прогоны снова раз в {got:.0f} мин.")
    state["thinned"] = thinned

    # 4. PAT протухнет раньше, чем мы это заметим по последствиям
    days = _pat_days_left()
    warned = state.get("pat_warned_at_days")
    if days <= PAT_WARN_DAYS:
        step = int(days) if days > 0 else -1
        if warned is None or step < warned:
            if days > 0:
                _telegram(
                    f"🟠 <b>WORKFLOW_PAT истекает через {days:.0f} дн.</b> "
                    f"({PAT_EXPIRES})\n"
                    "Когда истечёт, самозапуск прекратится и опрос проредится "
                    "с 5 минут до 30 — молча, без единой красной ошибки.\n"
                    "Продлить: GitHub → Settings → Developer settings → "
                    "Personal access tokens → выпустить новый с правом "
                    "Actions: read and write на репозиторий odds-tracker → "
                    "положить в Settings → Secrets and variables → Actions → "
                    "WORKFLOW_PAT."
                )
            else:
                _telegram(
                    f"🔴 <b>WORKFLOW_PAT ИСТЁК</b> ({PAT_EXPIRES})\n"
                    "Пятиминутный цикл больше не запускается. Опрос идёт по "
                    "расписанию раз в 30 минут — данные собираются, но вход "
                    "мы берём позже и хуже."
                )
            state["pat_warned_at_days"] = step

    state["down"] = is_down
    _save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
