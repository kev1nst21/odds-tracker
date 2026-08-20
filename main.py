"""Single poll cycle: discover sports -> fetch odds -> detect movement ->
build one summary per event -> notify -> update dashboard -> (periodically)
check results. Run this once per interval (see README.md)."""
import json
import traceback
from datetime import datetime, timedelta, timezone

from config import (
    POLYMARKET_ENABLED,
    DASHBOARD_URL,
    SPIKE_THRESHOLD_PCT,
    ODDSPAPI_KEY,
    ODDSPAPI_LOOKUP_TTL_HOURS,
    POLL_INTERVAL_MINUTES,
    SNAPSHOT_RETENTION_HOURS,
    BASELINE_MAX_AGE_MINUTES,
    DIGEST_INTERVAL_HOURS,
    QUOTA_WARN_CREDITS,
    QUOTA_WARN_INTERVAL_HOURS,
)
import budget
import odds_client
import oddspapi_client
import storage
import detector
import analytics
import notifier
import dashboard
import results


def _warn_sport_error(sport_key, exc):
    # A single sport temporarily failing (e.g. no events right now) shouldn't
    # crash the whole run -- log it and keep going with everyone else.
    print(f"[main] skipping sport '{sport_key}': {exc}")


def _load_lookup_cache(now):
    """Participant names and active tournament ids, reused between runs.

    These change slowly, so refetching them every cycle would spend ~12,000
    OddsPapi requests a month for data that barely moves.
    """
    stamp = storage.get_meta("oddspapi_lookup_at")
    if stamp:
        try:
            if (now - datetime.fromisoformat(stamp)) < timedelta(hours=ODDSPAPI_LOOKUP_TTL_HOURS):
                names = json.loads(storage.get_meta("oddspapi_names") or "{}")
                tourneys = json.loads(storage.get_meta("oddspapi_tournaments") or "{}")
                # JSON turns the int sport ids into strings on the way in.
                return ({int(k): v for k, v in names.items()},
                        {int(k): v for k, v in tourneys.items()})
        except (ValueError, TypeError):
            pass
    return {}, {}


def _save_lookup_cache(names, tourneys, now):
    if not names:
        return
    storage.set_meta("oddspapi_names", json.dumps(names))
    storage.set_meta("oddspapi_tournaments", json.dumps(tourneys))
    storage.set_meta("oddspapi_lookup_at", now.isoformat())


def _fetch_oddspapi(now):
    """Esports + table tennis. Never allowed to break the run: if this provider
    is down or unpaid, the football/tennis pipeline must still complete."""
    if not ODDSPAPI_KEY:
        return []
    try:
        names_cache, tourneys_cache = _load_lookup_cache(now)
        records, names, tourneys = oddspapi_client.collect(
            on_error=_warn_sport_error,
            names_cache=names_cache,
            tournaments_cache=tourneys_cache,
        )
        if not names_cache:
            _save_lookup_cache(names, tourneys, now)
        return records
    except Exception as exc:  # noqa: BLE001 -- second provider is best-effort
        print(f"[main] OddsPapi provider failed, continuing without it: {exc}")
        return []


def _soonest(active) -> str:
    """When the nearest open bet kicks off, in the same format the cards use."""
    starts = sorted(r["start_time"] for r in active if r["start_time"])
    if not starts:
        return ""
    try:
        dt = datetime.fromisoformat(str(starts[0]).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return dt.strftime("%d.%m %H:%M UTC")


def _maybe_digest(now, fetched_at):
    """Send the periodic heartbeat, at most once per DIGEST_INTERVAL_HOURS.

    Deliberately built from what is already in the database rather than from
    this cycle's variables: the digest covers a window of several hours, and a
    single poll is not that window. Any failure here is swallowed -- a report
    that cannot be sent must never take the poll down with it.
    """
    if not DIGEST_INTERVAL_HOURS:
        return
    last = storage.get_meta("last_digest_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < timedelta(hours=DIGEST_INTERVAL_HOURS):
                return
        except ValueError:
            pass
    try:
        f = storage.funnel_stats(int(DIGEST_INTERVAL_HOURS))
        diag = json.loads(storage.get_meta("detect_diag") or "{}")
        active = storage.active_signals(60)
        quota = odds_client.LAST_QUOTA or {}
        remaining = quota.get("remaining")
        payload = {
            "hours": DIGEST_INTERVAL_HOURS,
            "threshold": SPIKE_THRESHOLD_PCT * 100,
            "lines_watched": diag.get("lines"),
            "lines_blind": diag.get("no_history"),
            "lines_moved": diag.get("moved"),
            "movements": f.get("big_drop"),
            "signals": f.get("signals"),
            "thin_market": f.get("thin_market"),
            "all_books_moved": f.get("all_books_moved"),
            "entry_too_low": f.get("entry_too_low"),
            "low_stars": f.get("low_stars"),
            "off_band": f.get("off_band"),
            "too_far": f.get("too_far"),
            "open_bets": len(active),
            "next_start": _soonest(active),
            "credits": remaining,
            "poll_minutes": POLL_INTERVAL_MINUTES,
        }
        if remaining:
            burn = storage.get_meta("burn_per_day")
            try:
                payload["days_left"] = max(0, remaining - 800) / float(burn) if burn else None
            except (TypeError, ValueError):
                payload["days_left"] = None
        notifier.notify_digest(payload, dashboard_url=DASHBOARD_URL)
        storage.set_meta("last_digest_at", fetched_at)
        print(f"[digest] sent: {payload['movements']} moves, {payload['signals']} signals")
    except Exception:  # noqa: BLE001 -- a report must never break the poll
        print("[digest] failed, continuing:")
        traceback.print_exc()


def _days_left(remaining) -> float:
    """How long the balance lasts at the burn rate we have actually measured."""
    try:
        burn = float(storage.get_meta("burn_per_day") or 0)
        usable = max(0, int(remaining) - 800)
        return usable / burn if burn > 0 else None
    except (TypeError, ValueError):
        return None


def _maybe_quota_alarm(now, fetched_at):
    """Warn to Telegram before the credits run out, not after.

    Throttled the same way the digest is, and swallowed the same way: a
    warning that cannot be delivered must not be able to take down the poll it
    is warning about.
    """
    remaining = (odds_client.LAST_QUOTA or {}).get("remaining")
    if remaining is None or remaining > QUOTA_WARN_CREDITS:
        return
    last = storage.get_meta("last_quota_warn_at")
    if last:
        try:
            if (now - datetime.fromisoformat(last)) < timedelta(hours=QUOTA_WARN_INTERVAL_HOURS):
                return
        except ValueError:
            pass
    try:
        p = budget.LAST_PLAN or {}
        notifier.notify_credits({
            "remaining": int(remaining),
            "days_left": _days_left(remaining),
            "width": p.get("sports") if p.get("capped") else None,
            "plan_hint": budget.describe(p),
        }, dashboard_url=DASHBOARD_URL)
        storage.set_meta("last_quota_warn_at", fetched_at)
        print(f"[budget] отправлено предупреждение: осталось {remaining} кредитов")
    except Exception:  # noqa: BLE001 -- a warning must never break the poll
        print("[budget] предупреждение не ушло, продолжаем:")
        traceback.print_exc()


def _sweep_polymarket(now):
    """Look at Polymarket for every candidate that is due for a look.

    "Candidate" is deliberately wider than "signal": published signals plus the
    raw movements our own filters rejected. Those filters were written for
    bookmakers -- three books minimum, a price band, an entry that still
    captures half the move -- and none of that reasoning transfers to an order
    book with a visible price and visible depth. Instruction, 20.08: bet "от
    сигналов... а так же из движений, которые мы видим".

    "Due" is decided per candidate by polymarket.due_in_minutes: far from
    kick-off the market there usually does not exist yet, so hammering a free
    API every five minutes is noise; in the last hours the line moves and every
    skipped cycle is a possibly-missed edge.

    Each look can now produce TWO rows -- the straight win and the double
    chance -- because the bot can take both: "бот в таком случае будет делать
    две ставки с разным кофом, если такое возможно".

    Returns (looks, takes) for the cycle log.
    """
    import polymarket
    from datetime import datetime as _dt

    cands = storage.pm_candidates()
    if not cands:
        return 0, 0

    index = polymarket.build_index()
    looks = takes = 0
    alerts = []
    for row in cands:
        start = row.get("start_time")
        try:
            lead_h = (_dt.fromisoformat(str(start).replace("Z", "+00:00")) - now
                      ).total_seconds() / 3600.0
        except (TypeError, ValueError):
            continue
        if lead_h < 0:
            continue
        last_at, was_matched = storage.pm_last_check(row["fixture_id"], row["outcome_name"])
        wait = polymarket.due_in_minutes(lead_h, was_matched)
        # A movement we never published is checked less eagerly than a signal:
        # same ladder, one notch slower, because the signal is the thing we
        # actually believe in and the movement is the wider net.
        if row.get("source") == "movement":
            wait = max(wait, 15)
        if last_at:
            try:
                age_min = (now - _dt.fromisoformat(last_at)).total_seconds() / 60.0
            except (TypeError, ValueError):
                age_min = 1e9
            if age_min < wait:
                continue

        for res in polymarket.check(
                row["home_team"], row["away_team"], str(start),
                row["outcome_name"], row.get("entry_price"),
                opt_price=row.get("opt_price"), events=index,
                old_price=row.get("old_price"), new_price=row.get("new_price"),
                down_count=row.get("down_count") or 0,
                books_count=row.get("books_count") or 0):
            looks += 1
            res.update({
                "fixture_id": row["fixture_id"], "outcome_name": row["outcome_name"],
                "sport_key": row.get("sport_key"), "start_time": start,
                "lead_hours": round(lead_h, 2), "source": row.get("source", "signal"),
            })
            storage.save_pm_quote(res)
            if res.get("take"):
                takes += 1
                if storage.pm_alert_is_new(row["fixture_id"], row["outcome_name"],
                                           res.get("leg") or "aggressive"):
                    res["pm_stars"] = polymarket.pm_stars(
                        res.get("pm_lag"), row.get("down_count") or 0,
                        row.get("books_count") or 0, res.get("exec_stake_usd") or 0)
                    res["max_price"] = (round(1.0 / res["need_coef"], 4)
                                        if res.get("need_coef") else None)
                    res["home_team"] = row.get("home_team")
                    res["away_team"] = row.get("away_team")
                    alerts.append(res)
                print(f"[polymarket] ЗАЗОР ({res.get('leg')}): {row['outcome_name']} — "
                      f"БК {res.get('entry_price')}, стакан {res.get('avg_coef')}, "
                      f"+{res.get('edge_pct')}% на ${res.get('exec_stake_usd')}")
    if alerts:
        # Красным и немедленно: зазор на ордербуке живёт минутами, и сводка
        # через час равносильна неотправленному сообщению.
        try:
            notifier.notify_polymarket(alerts, dashboard_url=DASHBOARD_URL)
        except Exception as e:                                # noqa: BLE001
            print(f"[polymarket] уведомление не ушло: {e!r}")
    return looks, takes


def run_once():
    storage.init_db()
    now = datetime.now(timezone.utc)
    fetched_at = now.isoformat()

    # Notice a plan rollover before anything asks how many credits are left,
    # so the governor divides by the right number of days remaining.
    budget.observe(odds_client.LAST_QUOTA, now)

    all_sports = odds_client.list_sports()  # free call, no quota cost
    sport_keys = odds_client.select_sport_keys(all_sports)

    raw = odds_client.fetch_odds_for_sports(sport_keys, on_error=_warn_sport_error)
    records = odds_client.flatten_odds(raw)

    # Esports + table tennis ride on a second provider but produce identical
    # records, so everything downstream treats them the same as football.
    esports_records = _fetch_oddspapi(now)
    records += esports_records

    spikes, movements = detector.detect(records, fetched_at)
    summaries = analytics.build_event_summaries(records, spikes, movements)

    storage.save_snapshot(records, fetched_at)
    # Which leagues have something coming up, so the next lap can skip the
    # dormant ones and stay short enough for baselines to resolve.
    storage.save_sport_horizon(records, fetched_at)

    # Record the bets we're actually recommending, with all three prices, so
    # results.py can score them later against the price a real bet would have
    # got. Only alertable ones -- if there's nowhere left to bet, there is no
    # bet to score.
    # save_bet_alert() returns False for a bet we already logged. That flag is
    # what stops the bot repeating itself: since 2026-07-31 a move is measured
    # against the price an hour ago, so the same event stays "alertable" for
    # cycle after cycle. Without this it would be sent to Telegram every few
    # minutes for an hour.
    logged = 0
    for s in summaries:
        if not s.get("alertable"):
            continue
        s["is_new"] = storage.save_bet_alert(s, fetched_at, POLL_INTERVAL_MINUTES)
        logged += 1 if s["is_new"] else 0

    # Every move that cleared the threshold, signal or not. Priced at the
    # pre-drop coefficient, this is the ceiling the strategy is worth when
    # execution is free -- the benchmark the real, bettable numbers are
    # measured against.
    moves_logged = 0
    for s in summaries:
        # Same "is this the first time we've seen it" flag as the alerts use,
        # so the bot can announce a move once and then stay quiet about it
        # even though an hour-long baseline keeps it visible for many cycles.
        s["move_is_new"] = storage.save_movement(s, fetched_at)
        moves_logged += 1 if s["move_is_new"] else 0

    notifier.notify_summaries(summaries, dashboard_url=DASHBOARD_URL)

    # Polymarket. NOT a one-shot enrichment of the signals we just found -- a
    # sweep over every signal still standing, however long ago it fired.
    #
    # Measured 20.08: their entire open football listing spans three days,
    # while our signals fire up to 44 hours before kick-off. So at the moment
    # a signal is born the market there frequently does not exist yet, and
    # asking once would answer "no" for the majority of them forever. The
    # instruction was to keep looking instead: "мы будем за ней следить и
    # ставить когда нам будет подходить".
    #
    # Cost is time, not credits -- both Polymarket endpoints are free and do
    # not touch the odds quota this whole file is built to ration.
    pm_looks = pm_takes = 0
    if POLYMARKET_ENABLED:
        try:
            pm_looks, pm_takes = _sweep_polymarket(now)
        except Exception as e:                                # noqa: BLE001
            # Never let the side quest kill the poll it rides on.
            print(f"[polymarket] пропуск цикла: {e!r}")

    # Costs quota (one scores call per sport with pending alerts), so this is
    # internally throttled to run at most once every RESULTS_CHECK_INTERVAL_HOURS.
    newly_resolved = results.check_pending_results()

    # Current score for anything we're standing on that has already kicked
    # off. Free when nothing is in play -- it asks about no sports at all.
    live = results.refresh_live_scores(now)

    # Drop price history we no longer need. At a 3-minute cadence the snapshot
    # table grows by millions of rows a day; left alone it would eventually make
    # the CI cache too slow to save, which costs whole runs. Alerts are never
    # pruned -- they are the track record.
    pruned = storage.prune_snapshots(SNAPSHOT_RETENTION_HOURS)

    # Where the market's activity stopped being a signal. Printed every cycle
    # because "0 сигналов" on its own is indistinguishable from a broken
    # pipeline, and the only way to tune a filter honestly is to see how many
    # events each one is actually eating.
    # BEFORE the funnel, because the funnel only describes what happened to
    # movements that were found -- and on 2026-08-09 the fault was that almost
    # none were. A line with no usable baseline is skipped in silence, so this
    # is the only place that failure can ever be seen. If "не с чем сравнить"
    # is most of the lines, nothing downstream means anything.
    d = detector.LAST_DIAG
    storage.set_meta("detect_diag", json.dumps(d))
    if d.get("lines"):
        blind_pct = d["no_history"] / d["lines"] * 100
        print(f"[detect] линий {d['lines']} → сравнили {d['compared']} "
              f"→ дрогнуло {d['moved']} → от порога {d['spiked']}; "
              f"не с чем сравнить {d['no_history']} ({blind_pct:.0f}%)")
        if blind_pct >= 50:
            worst = sorted(d["by_sport_blind"].items(), key=lambda kv: -kv[1])[:5]
            print("[detect] ВНИМАНИЕ: половина линий без базы — ротация длиннее "
                  f"окна сравнения ({BASELINE_MAX_AGE_MINUTES} мин). Хуже всего: "
                  + ", ".join(f"{k} ({n})" for k, n in worst))

    f = analytics.LAST_FUNNEL
    storage.save_funnel(f, fetched_at)
    print(f"[funnel] событий {f.get('events',0)} → просело {f.get('with_drop',0)} → "
          f"от {SPIKE_THRESHOLD_PCT*100:.0f}% {f.get('big_drop',0)} → отсев: рынок мал {f.get('thin_market',0)}, "
          f"просело у всех {f.get('all_books_moved',0)}, вход не дотянул "
          f"{f.get('entry_too_low',0)} → сигналов {f.get('signals',0)}")

    # What the governor allowed this cycle, recorded before rendering so the
    # page can state the width it is actually running at rather than the width
    # someone once typed into config.
    # Remember the balance for the NEXT cycle. The quota only ever arrives in a
    # response header, but the width has to be chosen before the first request
    # is made, so without this the governor is asked a question it cannot
    # answer and stands aside -- which is exactly what it did for one afternoon
    # on 2026-08-15, publishing "remaining": null next to the full ambition.
    budget.remember(odds_client.LAST_QUOTA)
    if budget.LAST_PLAN:
        storage.set_meta("budget_plan", json.dumps(budget.LAST_PLAN))
        line = budget.describe()
        if line:
            print(f"[budget] {line}")

    path = dashboard.render_dashboard(summaries, quota=odds_client.LAST_QUOTA)

    _maybe_digest(now, fetched_at)
    _maybe_quota_alarm(now, fetched_at)

    actionable = sum(1 for s in summaries if s.get("alertable"))
    starred = sum(1 for s in summaries if s.get("stars", 0) >= 3)
    optimal = sum(1 for s in summaries
                  if s.get("alertable") and s.get("strategy") == "optimal")
    print(
        f"[{fetched_at}] every {POLL_INTERVAL_MINUTES} min · "
        f"{len(sport_keys)} sports via TheOddsAPI + "
        f"{len(esports_records)} esports/table-tennis lines via OddsPapi, "
        f"{len(records)} lines total, {len(summaries)} events moved {SPIKE_THRESHOLD_PCT*100:.0f}%+, "
        f"{actionable} with an open entry ({optimal} optimal), {starred} at 3 stars, "
        f"{logged} new bets logged, {moves_logged} moves logged, {newly_resolved} resolved, "
        f"{live} live scores, "
        f"{pruned} old rows pruned, dashboard -> {path}"
    )
    return summaries


if __name__ == "__main__":
    run_once()

