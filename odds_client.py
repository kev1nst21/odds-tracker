"""
Thin client around The Odds API (the-odds-api.com).

Docs: https://the-odds-api.com/liveapi/guides/v4/
Auth: query param `apiKey`
Base: https://api.the-odds-api.com

Switched from OddsPapi on 2026-07-29 (see config.py for the why). Key
differences from the old client:
  - GET /v4/sports/ is FREE (no quota cost) and lists every sport currently
    in season -- used for dynamic discovery instead of a hardcoded list,
    since e.g. tennis tournaments rotate in and out of season.
  - GET /v4/sports/{sport}/odds/ returns EVERY bookmaker for that sport in a
    single call. Quota cost = (markets count) x (regions count) credits per
    call, confirmed live 2026-07-29 via the response's x-requests-* headers.
  - GET /v4/sports/{sport}/scores/ returns recent + live results for that
    sport; daysFrom<=1 costs 1 credit, daysFrom 2-3 costs 2 credits.
"""
from datetime import datetime, timedelta, timezone

import requests

from config import (
    THEODDSAPI_BASE_URL,
    THEODDSAPI_KEY,
    REGIONS,
    MARKETS,
    SOCCER_LEAGUE_KEYS,
    TENNIS_GROUP,
    PREMATCH_ONLY,
    PREMATCH_BUFFER_MINUTES,
    WIDE_COVERAGE,
    WIDE_GROUPS,
    MAX_SPORTS_PER_CYCLE,
    ROTATE_WIDE_COVERAGE,
    WIDE_MIN_SLOTS,
    SPORTS_LIST_TTL_MINUTES,
    SKIP_DORMANT_SPORTS,
    DORMANT_MARGIN_HOURS,
    MAX_LEAD_HOURS,
    POLL_INTERVAL_MINUTES,
)


class TheOddsApiError(RuntimeError):
    pass


# Last quota figures reported by the API (x-requests-used / x-requests-remaining
# response headers). Kept module-level so the dashboard can show how much of the
# monthly credit budget is left without spending an extra call to look it up.
LAST_QUOTA = {"used": None, "remaining": None}


def _get(path: str, params: dict):
    if not THEODDSAPI_KEY:
        raise TheOddsApiError(
            "THEODDSAPI_KEY is not set. Copy .env.example to .env and fill it in."
        )
    params = {**params, "apiKey": THEODDSAPI_KEY}
    resp = requests.get(f"{THEODDSAPI_BASE_URL}{path}", params=params, timeout=20)
    if resp.status_code != 200:
        raise TheOddsApiError(f"{resp.status_code} from {path}: {resp.text[:500]}")
    remaining = resp.headers.get("x-requests-remaining")
    used = resp.headers.get("x-requests-used")
    if remaining is not None:
        try:
            LAST_QUOTA["used"] = int(float(used))
            LAST_QUOTA["remaining"] = int(float(remaining))
        except (TypeError, ValueError):
            pass
        print(f"[odds_client] quota: used={used} remaining={remaining} (call: {path})")
    return resp.json()


def list_sports() -> list:
    """Every sport currently in season, each with 'key', 'group', 'title',
    'active'.

    The docs call this endpoint free. It is not: production logs show it
    billing a credit per call ("used=1454 remaining=18546 (call: /v4/sports/)").
    At 100+ cycles a day that is an entire league's worth of budget spent on a
    list that changes about twice a day, so the answer is cached in the meta
    table for SPORTS_LIST_TTL_MINUTES and refetched only when it goes stale.
    """
    import json
    import storage  # local import: storage imports config, not this module

    try:
        stamp = storage.get_meta("sports_list_at")
        cached = storage.get_meta("sports_list")
        if stamp and cached:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(stamp)
            if age < timedelta(minutes=SPORTS_LIST_TTL_MINUTES):
                data = json.loads(cached)
                if data:
                    return data
    except Exception:  # noqa: BLE001 -- a cache miss must never break the run
        pass

    data = _get("/v4/sports/", {})
    try:
        storage.set_meta("sports_list", json.dumps(data))
        storage.set_meta("sports_list_at", datetime.now(timezone.utc).isoformat())
    except Exception:  # noqa: BLE001
        pass
    return data


def _obscurity_rank(key: str) -> tuple:
    """Sort key that puts the quiet corners of the market FIRST.

    The whole premise of the tracker is catching money that knows something.
    In the Premier League that money is competing with every trading desk on
    earth and the price is right again within seconds. In a Latvian second
    division or a reserve-team cup tie, one informed bet can move a line and
    leave it moved for an hour, and the genuinely dirty games live there
    too -- nobody fixes a Champions League match. So famous leagues are polled
    because they are cheap to include, not because they are where the edge is,
    and they go last in the rotation.
    """
    k = key.lower()
    famous = ("_epl", "la_liga", "serie_a", "bundesliga", "ligue_one",
              "champs_league", "uefa_europa", "usa_mls", "_atp_", "_wta_")
    quiet = ("_2", "_3", "_reserve", "_youth", "u19", "u20", "u21", "_amateur",
             "_women", "_cup", "friendl", "_challenger", "_itf", "_qualif")
    return (0 if any(q in k for q in quiet) else (2 if any(f in k for f in famous) else 1), k)


def select_sport_keys(all_sports: list = None) -> list:
    """Which sport keys this cycle actually pays for.

    The core soccer leagues and every in-season tennis tournament are always
    included. On top of that, WIDE_COVERAGE sweeps the rest of the configured
    groups -- the lower divisions, cups and small federations where a line
    move is still worth something (see _obscurity_rank).

    That list is far bigger than one cycle's credit budget, so it is walked in
    slices of MAX_SPORTS_PER_CYCLE with the cursor kept in the database. This
    is only safe because the detector compares against a price from an hour
    ago rather than against the previous cycle, so a league being polled every
    fourth cycle still gets its moves measured correctly.

    2026-08-15: the slice width is no longer MAX_SPORTS_PER_CYCLE itself.
    budget.plan() turns that constant into an AMBITION and returns what the
    remaining credits can actually pay for between now and the plan's rollover.
    A cap the balance cannot fund is not a cap; it is an outage with a
    two-week fuse, which is exactly what this month demonstrated.
    """
    import budget
    cap = budget.plan(LAST_QUOTA.get("remaining")).get("sports") or MAX_SPORTS_PER_CYCLE
    return _select_within(all_sports, max(1, int(cap)))


def _select_within(all_sports, cap: int) -> list:
    """The selection proper, with this cycle's affordable width already fixed.

    Split out from select_sport_keys so the rotation logic can be tested
    against an explicit width instead of against whatever the live credit
    balance happens to be.
    """
    if all_sports is None:
        all_sports = list_sports()
    live = [s for s in all_sports if s.get("active", True)]
    live_keys = {s["key"] for s in live}

    # Tennis first: in-season tournaments are few, they rotate constantly, and
    # a tennis line that moves is the single most gradeable thing we track.
    core = sorted(s["key"] for s in live if s.get("group") == TENNIS_GROUP)
    core += [k for k in SOCCER_LEAGUE_KEYS if k in live_keys]
    core = list(dict.fromkeys(core))

    if not WIDE_COVERAGE:
        return core[:cap]

    wide = sorted((s["key"] for s in live
                   if s.get("group") in WIDE_GROUPS and s["key"] not in core),
                  key=_obscurity_rank)
    if not wide:
        return core[:cap]

    # The wide sweep gets its slots reserved BEFORE the core list is trimmed.
    # Otherwise a busy tennis week fills the whole budget with famous names and
    # the tracker quietly goes back to watching only the efficient markets --
    # which is the exact failure this was written to fix.
    #
    # ...but the reservation has to cut both ways once the cap can shrink. With
    # a fixed cap of 15 the old `min(WIDE_MIN_SLOTS, len(wide), cap)` always
    # left seven slots for the core. Under the credit governor the cap can fall
    # to six, and that same expression then claimed ALL of them -- silently
    # dropping every tennis tournament, which is the single most gradeable
    # thing tracked here, at precisely the moment the tracker could least
    # afford to stop producing settleable bets. So the core now keeps a third
    # of a narrow cycle. At the ordinary cap this changes nothing.
    core_floor = max(1, cap // 3)
    wide_slots = min(WIDE_MIN_SLOTS, len(wide), max(0, cap - core_floor))
    kept_core = core[:max(0, cap - wide_slots)]
    wide_slots = max(wide_slots, cap - len(kept_core))
    wide_slots = min(wide_slots, len(wide))

    if not ROTATE_WIDE_COVERAGE:
        return kept_core + wide[:wide_slots]

    # Imported here rather than at module scope: storage imports config, and a
    # top-level import would make the two modules circular at startup.
    import storage
    wide = _drop_dormant(wide, storage.sport_horizon())
    wide_slots = min(wide_slots, len(wide))
    if not wide_slots:
        return kept_core
    # Record how long a full lap of the wide list now takes. detector.py reads
    # this and refuses to let a baseline expire sooner -- the two numbers were
    # set independently before, contradicted each other, and the result was a
    # day of silence with nothing anywhere saying why.
    import math
    lap = math.ceil(len(wide) / max(1, wide_slots)) * max(1, POLL_INTERVAL_MINUTES)
    storage.set_meta("wide_lap_minutes", str(lap))

    start = storage.next_rotation_offset(wide_slots, len(wide))
    return kept_core + [wide[(start + i) % len(wide)] for i in range(wide_slots)]


def _drop_dormant(wide: list, horizon: dict) -> list:
    """Leagues with nothing inside the publishing horizon leave the rotation.

    The lap length is the whole ball game. A wide list of forty keys read five
    at a time is a four-hour lap, and a line revisited every four hours cannot
    be compared against an hour-old price -- so it is never measured at all,
    which is precisely how the tracker went a full day without a signal on
    2026-08-09. Dropping dormant leagues shortens the lap directly, and costs
    nothing: the nearest fixture per key was already recorded on the last fetch.

    A key we have never fetched has no horizon recorded and is always kept. The
    filter must never be able to exclude a league it has not looked at, or a
    cold start would lock itself out permanently.
    """
    if not SKIP_DORMANT_SPORTS or not horizon:
        return wide
    cutoff = (datetime.now(timezone.utc)
              + timedelta(hours=MAX_LEAD_HOURS + DORMANT_MARGIN_HOURS)).isoformat()
    kept = [k for k in wide
            if not (horizon.get(k) or {}).get("next")
            or str(horizon[k]["next"]) <= cutoff]
    dropped = len(wide) - len(kept)
    if dropped:
        print(f"[coverage] {dropped} of {len(wide)} wide leagues have nothing within "
              f"{MAX_LEAD_HOURS + DORMANT_MARGIN_HOURS:g}h -- skipped, lap gets shorter")
    # Never return an empty rotation: if every league looked dormant we would
    # stop refreshing the horizons that say so, and the filter would latch shut.
    return kept or wide


def fetch_odds_for_sport(sport_key: str, on_error=None) -> list:
    """GET /v4/sports/{sport_key}/odds/ -- every event for this sport, with
    every bookmaker's prices nested inside. Costs len(MARKETS split by comma)
    x len(REGIONS split by comma) credits (confirmed live 2026-07-29)."""
    try:
        # Not the REGIONS constant: budget.plan() already decided this cycle how
        # many regions the balance can carry, and paying for four while the
        # governor sized the league list for one is how a plan gets emptied in
        # a weekend.
        import budget
        return _get(f"/v4/sports/{sport_key}/odds/",
                    {"regions": budget.active_regions(), "markets": MARKETS})
    except TheOddsApiError as exc:
        if on_error:
            on_error(sport_key, exc)
            return []
        raise


def fetch_odds_for_sports(sport_keys: list, on_error=None) -> list:
    """Fetch + concatenate raw event lists across every selected sport key.
    Sequential -- 7-10 calls/cycle is small enough that OddsPapi's old
    rate-limit/threading machinery isn't needed for this provider."""
    all_events = []
    for sport_key in sport_keys:
        events = fetch_odds_for_sport(sport_key, on_error=on_error)
        for e in events:
            e["_sport_key"] = sport_key  # tag for flatten_odds() / results.py
        all_events.extend(events)
    return all_events


def fetch_scores_for_sport(sport_key: str, days_from: int = 1, on_error=None) -> list:
    """GET /v4/sports/{sport_key}/scores/ -- recent + live scores for this
    sport, used by results.py to grade past alerts. daysFrom=1 costs 1
    credit, daysFrom 2-3 costs 2 credits (confirmed live via docs 2026-07-29)."""
    try:
        return _get(f"/v4/sports/{sport_key}/scores/", {"daysFrom": days_from})
    except TheOddsApiError as exc:
        if on_error:
            on_error(sport_key, exc)
            return []
        raise


_SIDE_ALIASES = {"draw": "draw", "tie": "draw"}


def _side_for_outcome(name: str, home_team: str, away_team: str):
    """Map an outcome name to 'home' / 'away' / 'draw' where possible, so
    detector/results/notifier can reason about direction generically instead
    of matching literal team-name strings everywhere downstream."""
    if not name:
        return None
    key = name.strip().lower()
    if home_team and key == home_team.strip().lower():
        return "home"
    if away_team and key == away_team.strip().lower():
        return "away"
    return _SIDE_ALIASES.get(key)


def _is_prematch(commence_time, now=None) -> bool:
    """True if the event is worth storing: not started, and not so far away
    that we could never publish it.

    The near edge was always here -- an in-play price reacts to goals and
    breaks of serve, not to money, so it can only produce false signals.

    The FAR edge was added 2026-08-09 on the user's observation: "ты мониторишь
    события которые будут позже чем 60 часов, а их быть нигде не должно". He is
    right, and it was worse than untidy. MAX_LEAD_HOURS already refuses to
    publish those events, so every line we kept for a match a week out was
    stored, diffed and counted -- padding the "сверено N из M" denominator,
    bloating the snapshot table and the CI cache, and diluting the rotation
    with fixtures that cannot become a signal under our own rules. One of the
    only two movements found during the blind day was exactly this: a match on
    14.08 caught on 09.08, unpublishable the moment it was found.

    Note what this does and does not save. The Odds API bills per SPORT KEY,
    not per event, so dropping distant events costs nothing extra and saves no
    credit directly. It pays indirectly and steadily: a league whose fixtures
    are all beyond the horizon now yields no usable records, which is what
    marks it dormant, and dormant leagues leave the rotation -- and THAT is
    what shortens the lap and cuts the bill.
    """
    if not PREMATCH_ONLY:
        return True
    if not commence_time:
        return False  # unknown start time -- safer to skip than to guess
    try:
        start = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    if start <= now + timedelta(minutes=PREMATCH_BUFFER_MINUTES):
        return False
    # The margin matches the rotation's: a fixture enters the window slightly
    # before it becomes publishable, so a baseline already exists by the time
    # the first move on it would matter.
    return start <= now + timedelta(hours=MAX_LEAD_HOURS + DORMANT_MARGIN_HOURS)


def _already_started(commence_time, now=None) -> bool:
    """Distinguishes the two ways an event fails _is_prematch, so the log can
    say which one happened rather than blaming everything on in-play."""
    if not commence_time:
        return False
    try:
        start = datetime.fromisoformat(str(commence_time).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return start <= now + timedelta(minutes=PREMATCH_BUFFER_MINUTES)


def flatten_odds(raw_events: list) -> list:
    """Turn the nested The Odds API response into flat records:
    {fixture_id, sport_key, sport_title, start_time, home_team, away_team,
     bookmaker, market_id, outcome_id, player_key, price, label}

    outcome_id is normalized to 'home'/'away'/'draw' when the outcome name
    matches a team name (h2h market), so downstream code (detector, results,
    notifier) doesn't need to special-case sports or bookmaker naming.
    player_key has no real meaning for this provider (no player-prop markets
    requested) but is kept as a constant '-' for schema compatibility with
    the rest of the pipeline (detector/storage key on 5 fields).
    """
    return _flatten(raw_events)


def _flatten(raw_events: list) -> list:
    records = []
    skipped_live = skipped_far = 0
    for event in raw_events:
        fixture_id = event.get("id")
        start_time = event.get("commence_time")
        if not _is_prematch(start_time):
            # Two different reasons, counted apart: one says the market is
            # already live, the other says we would never publish this match.
            # Lumping them together once made a horizon skip read as "in-play".
            if _already_started(start_time):
                skipped_live += 1
            else:
                skipped_far += 1
            continue
        home_team = event.get("home_team")
        away_team = event.get("away_team")
        sport_key = event.get("_sport_key") or event.get("sport_key")
        sport_title = event.get("sport_title") or sport_key
        for bm in event.get("bookmakers", []):
            bookmaker = bm.get("key")
            for market in bm.get("markets", []):
                market_id = market.get("key")
                for outcome in market.get("outcomes", []):
                    price = outcome.get("price")
                    name = outcome.get("name")
                    if price is None or not bookmaker or not market_id:
                        continue
                    side = _side_for_outcome(name, home_team, away_team)
                    outcome_id = side or (name or "").strip().lower() or "-"
                    label = f"{home_team} vs {away_team}: {name}"
                    records.append({
                        "fixture_id": fixture_id,
                        "sport_key": sport_key,
                        "sport_title": sport_title,
                        "start_time": start_time,
                        "home_team": home_team,
                        "away_team": away_team,
                        "bookmaker": bookmaker,
                        "market_id": market_id,
                        "outcome_id": outcome_id,
                        "player_key": "-",
                        "price": float(price),
                        "label": label,
                    })
    if skipped_live:
        print(f"[odds_client] skipped {skipped_live} in-play event(s) -- pre-match only")
    if skipped_far:
        print(f"[odds_client] skipped {skipped_far} event(s) beyond the "
              f"{MAX_LEAD_HOURS + DORMANT_MARGIN_HOURS:g}h horizon -- unpublishable anyway")
    return records
