"""How much market this cycle is allowed to pay for.

WHY THIS EXISTS. Until now breadth was a constant: MAX_SPORTS_PER_CYCLE sat in
config.py and the poller spent whatever that implied, month in and month out.
That is fine while the number is right and catastrophic the moment it is not,
and it has been wrong in both directions inside a fortnight:

  * 2026-08-08 the chain ran away and spent 3 052 credits in fifty minutes --
    a month's income in under two days;
  * 2026-08-15, at the opposite extreme, the plan is down to 1 137 credits with
    two weeks of the month left, because a cap set for a quiet Tuesday was
    still in force when the weekend fixtures arrived and every cycle got
    dearer.

Both are the same bug: a fixed cap cannot know what the plan can afford. So the
cap stops being a constant and becomes an ANSWER -- recomputed every cycle from
three things we actually measure: credits the API says are left, how long until
the plan rolls over, and what one sport currently costs.

    per_cycle = (remaining - reserve) / cycles_left_in_the_period
    sports    = per_cycle / credits_per_sport

The number in config.py becomes an AMBITION -- the most we would ever want --
and this module hands back the most we can currently have. Two consequences,
and they are the whole point:

  * over-spend becomes structurally impossible. Ambition can be set to the
    entire fixture list and a small plan simply throttles it, instead of
    burning out on the 9th and going dark for three weeks.
  * an upgrade needs no code. Buy a bigger plan and the next cycle sees the
    larger `remaining`, computes a larger allowance and widens by itself --
    within the hour, without a commit, without me.

The floor matters as much as the ceiling. When credits run low this narrows to
MIN_SPORTS_PER_CYCLE rather than to zero: a tracker watching six leagues is
still a tracker, and runner.py's reserve guard is the thing that stops polling
outright. Degrade, don't die.
"""
import math
from datetime import datetime, timedelta, timezone

from config import (
    MARKETS,
    REGIONS,
    MAX_SPORTS_PER_CYCLE,
    MIN_SPORTS_PER_CYCLE,
    QUOTA_RESERVE_CREDITS,
    QUOTA_PERIOD_DAYS,
    AUTO_BUDGET,
    COLD_START_SPORTS,
    AUTO_REGIONS,
    REGION_LADDER,
    REGION_STEP_MIN_SPORTS,
)

# What the last cycle worked out, for the dashboard and the digest to read
# without recomputing it. Same pattern as detector.LAST_DIAG.
LAST_PLAN = {}


def _split(spec) -> list:
    return [x.strip() for x in str(spec or "").split(",") if x.strip()]


def credits_per_sport(regions=None) -> int:
    """The Odds API bills markets x regions per sport key, per call.

    Confirmed against the published rule ("cost = [number of markets] x
    [number of regions]"). This is the single number that makes adding a region
    expensive: every extra region multiplies EVERY sport we poll, so going from
    eu to eu,uk doubles the bill for the whole cycle rather than adding a line
    item.
    """
    markets = len(_split(MARKETS))
    regions = len(_split(regions if regions is not None else REGIONS))
    return max(1, markets) * max(1, regions)


def _afford_regions(per_cycle: float, markets: int) -> str:
    """How many bookmaker regions this balance can carry.

    Region is the only lever that adds bookmakers, and bookmakers are what the
    funnel says we are short of: the two biggest reasons a movement fails to
    become a bet are "every book moved, nowhere left to back it" and "the best
    remaining price missed the entry rule". Both are a shortage of books. More
    books means more laggards still holding the old price, and a laggard IS the
    bet.

    So regions climb the ladder on their own as credits allow, exactly like
    breadth does -- which is the whole promise of this module: pay for a bigger
    plan and the product widens by itself, within the hour, with no commit and
    nobody remembering to flip a switch.

    The guard is REGION_STEP_MIN_SPORTS. A second region must never be bought
    by starving the league list: doubling the price per league while the cycle
    can only afford eight of them would trade away more market than it buys
    books. So a region is only added once the budget can still keep a
    respectable number of leagues WHILE paying the higher per-league price.
    """
    ladder = _split(REGION_LADDER) or _split(REGIONS) or ["eu"]
    afford = 1
    for n in range(2, len(ladder) + 1):
        if per_cycle >= REGION_STEP_MIN_SPORTS * n * max(1, markets):
            afford = n
    return ",".join(ladder[:afford])


def _remembered_balance():
    """The last credit balance any cycle actually saw.

    Written by remember() at the end of every poll. Reading it here is what
    lets the governor decide a width before the first request of a cycle has
    been made -- which is every time it is asked.
    """
    import storage
    try:
        v = storage.get_meta("quota_remaining_seen")
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def remember(quota: dict) -> None:
    """Persist the balance so the next cycle can size itself before spending."""
    import storage
    try:
        remaining = int((quota or {}).get("remaining"))
    except (TypeError, ValueError):
        return
    storage.set_meta("quota_remaining_seen", str(remaining))


def active_regions() -> str:
    """The region string this cycle is actually paying for."""
    return (LAST_PLAN.get("regions") if LAST_PLAN else None) or REGIONS


def _period_days_left(now: datetime) -> float:
    """Days until the plan's credits reset.

    This used to be guesswork. The allowance is monthly, nothing in the API
    says when the month turns over, so the code inferred it from `used`
    falling and otherwise assumed a full period ahead -- deliberately
    conservative, and wrong by up to 2x in the direction of under-spending.

    2026-08-15 removed the guess. The provider's own dashboard states the rule
    outright: "Monthly plans reset on the 1st of each month at 12AM UTC". It is
    a CALENDAR month, not a subscription anniversary, so the answer is simply
    the time until the next 1st and no observation is needed at all.

    The inferred marker is still honoured if something ever contradicts this --
    observe() keeps writing it -- but the calendar is the primary answer,
    because a measured fact beats an inference every time.
    """
    if now.month == 12:
        nxt = now.replace(year=now.year + 1, month=1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    else:
        nxt = now.replace(month=now.month + 1, day=1,
                          hour=0, minute=0, second=0, microsecond=0)
    left = (nxt - now).total_seconds() / 86400.0
    # Never zero: dividing by it would ask for an infinite per-cycle allowance
    # in the last hour of the month and hand the whole balance to one cycle.
    return min(float(QUOTA_PERIOD_DAYS), max(0.5, left))


def observe(quota: dict, now: datetime) -> None:
    """Record what the API just told us, and notice a rollover when it happens.

    `used` only ever climbs inside a billing period, so a fall is the one
    unambiguous signal that a new period has begun. A small tolerance guards
    against the counter jittering between two calls in flight.
    """
    import storage
    try:
        used = int((quota or {}).get("used"))
    except (TypeError, ValueError):
        return
    prev = storage.get_meta("quota_used_seen")
    try:
        prev = int(prev) if prev is not None else None
    except (TypeError, ValueError):
        prev = None
    if prev is not None and used < prev - 50:
        storage.set_meta("quota_period_start", now.isoformat())
        print(f"[budget] план обновился: использовано было {prev}, стало {used} — "
              f"новый период начался {now:%d.%m}")
    elif prev is None and not storage.get_meta("quota_period_start"):
        # First sighting ever. We do not know where in the month we are, so we
        # assume the worst (a full period ahead) by leaving the marker unset.
        pass
    storage.set_meta("quota_used_seen", str(used))


def plan(remaining, now=None, poll_minutes=None) -> dict:
    """How many sport keys this cycle may buy.

    Returns the whole calculation, not just the number, because a cap that
    cannot be recounted is exactly the kind of silent constraint that cost us
    two days in August -- the dashboard and the Telegram digest both print
    these fields.
    """
    from config import POLL_INTERVAL_MINUTES
    now = now or datetime.now(timezone.utc)
    poll_minutes = max(1, int(poll_minutes or POLL_INTERVAL_MINUTES))
    per_sport = credits_per_sport()
    ambition = max(1, int(MAX_SPORTS_PER_CYCLE or 1))
    floor = max(1, min(int(MIN_SPORTS_PER_CYCLE or 1), ambition))

    out = {
        "ambition": ambition,
        "floor": floor,
        "credits_per_sport": per_sport,
        "regions": REGIONS,
        "poll_minutes": poll_minutes,
        "remaining": remaining,
        "reserve": QUOTA_RESERVE_CREDITS,
    }

    # THE BALANCE IS ALMOST NEVER KNOWN WHEN THIS IS ASKED, and pretending
    # otherwise shipped a live bug on 2026-08-15. The quota only arrives in a
    # response header, but the width has to be decided BEFORE the first request
    # of the cycle -- and the sports list is served from cache, so a whole run
    # can go by without odds_client.LAST_QUOTA ever being filled in. The first
    # published ledger under the governor duly read
    # `"remaining": null, "sports": 60, "reason": "no-data"` -- the governor
    # politely standing aside at the exact moment it was supposed to be
    # governing, on the day the plan had a thousand credits left.
    #
    # So the balance is remembered across cycles. Last cycle's number is a fine
    # estimate of this cycle's: it can be at most one cycle's spend stale.
    if remaining is None:
        remaining = _remembered_balance()

    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        remaining = None

    if not AUTO_BUDGET or remaining is None:
        # Genuinely nothing to go on: no governor, or a database so fresh that
        # no cycle has ever completed. Fall back to the width that was in force
        # before the governor existed rather than to the ambition -- an unknown
        # balance must never authorise the LARGEST possible spend, which is
        # precisely the mistake described above.
        out.update({"sports": min(ambition, COLD_START_SPORTS),
                    "capped": ambition > COLD_START_SPORTS, "reason": "no-data"})
        LAST_PLAN.update(out)
        return out
    out["remaining"] = remaining

    days_left = _period_days_left(now)
    usable = max(0, remaining - QUOTA_RESERVE_CREDITS)
    cycles_left = max(1.0, days_left * 24 * 60 / poll_minutes)
    per_cycle = usable / cycles_left

    # Depth before breadth is decided: how many bookmaker regions the balance
    # can carry. On a small plan this stays at "eu" and nothing changes; on a
    # large one it climbs to eu,uk,au,us and the number of books roughly
    # triples, which is the change the funnel actually asks for.
    if AUTO_REGIONS:
        regions = _afford_regions(per_cycle, len(_split(MARKETS)))
        per_sport = credits_per_sport(regions)
        out["regions"] = regions
        out["credits_per_sport"] = per_sport
        out["region_count"] = len(_split(regions))
    # One credit of the cycle goes to overheads that are not sport keys (the
    # results check, the odd retry), so the division is deliberately floor().
    affordable = int(per_cycle // per_sport)
    sports = max(floor, min(ambition, affordable))

    out.update({
        "days_left": round(days_left, 2),
        "usable": usable,
        "cycles_left": int(cycles_left),
        "per_cycle": round(per_cycle, 1),
        "affordable": affordable,
        "sports": sports,
        "capped": sports < ambition,
        "starved": affordable < floor,
        "reason": "budget",
    })
    LAST_PLAN.clear()
    LAST_PLAN.update(out)
    return out


def describe(p: dict = None) -> str:
    """One line a human can check the arithmetic of."""
    p = p or LAST_PLAN
    if not p:
        return ""
    if p.get("reason") != "budget":
        return f"бюджет: {p.get('sports')} лиг/цикл (по конфигу)"
    line = (f"бюджет: {p['sports']} лиг/цикл из {p['ambition']} желаемых · "
            f"регионы {p.get('regions')} · "
            f"{p['credits_per_sport']} кр. за лигу · {p['per_cycle']:.0f} кр. на цикл "
            f"({p['usable']} доступно на {p['days_left']:.1f} дн.)")
    if p.get("starved"):
        line += " · КРЕДИТЫ НА ИСХОДЕ, ширина на минимуме"
    return line


def monthly_need(sports: int, poll_minutes: int, per_sport: int = None) -> int:
    """What a given ambition would actually cost per month, for the menu."""
    per_sport = per_sport or credits_per_sport()
    cycles = QUOTA_PERIOD_DAYS * 24 * 60 / max(1, poll_minutes)
    return int(math.ceil(sports * per_sport * cycles))
