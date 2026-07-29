"""Second data provider: OddsPapi (api.oddspapi.io), used ONLY for the lines
The Odds API doesn't carry at all -- esports and table tennis.

Why two providers instead of one: The Odds API (the main source, football and
tennis) has zero esports coverage -- confirmed live 2026-07-29 against the full
174-sport listing, no CS2 / Dota / LoL / Valorant keys anywhere. OddsPapi has
them, so a paid $102/mo "dev" plan was added on 2026-07-29 covering 4 books
(1xBet, 22Bet, 188BET, Betway) across CS2, Dota 2, League of Legends and table
tennis, with a 100,000 request/month allowance.

Everything here converts into the SAME flat record shape as
odds_client.flatten_odds(), so detector / analytics / storage stay provider-
agnostic and don't need to know where a price came from.

Request budget (measured live 2026-07-29): 33 tournaments actually have
fixtures scheduled across the four sports -> 7 chunks of 5 -> 28 odds calls per
cycle at 4 bookmakers. At a 30-minute cadence that's ~40,000/month against the
100,000 allowance. The participants and tournaments lookups are cached in
SQLite (see storage.get_meta) rather than re-fetched every cycle, which is what
keeps that number down.

Two quirks of this API that shaped the code:
  * One request carries ONE bookmaker and at most FIVE tournamentIds.
  * Fixtures identify teams by numeric participant id, not by name, so
    /v4/participants?sportId=N is needed to resolve them.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone

import requests

from config import (
    ODDSPAPI_KEY,
    ODDSPAPI_BASE_URL,
    ODDSPAPI_SPORTS,
    ODDSPAPI_BOOKMAKERS,
    PREMATCH_ONLY,
    PREMATCH_BUFFER_MINUTES,
)


class OddsPapiError(RuntimeError):
    pass


# The API throttles per-second even on paid plans; a shared gate keeps the
# combined rate sane while several worker threads are in flight.
MIN_REQUEST_INTERVAL_SEC = 0.15
MAX_RETRIES = 4
MAX_WORKERS = 6
MAX_TOURNAMENTS_PER_CALL = 5

# Moneyline market/outcome ids, confirmed live 2026-07-29 on a League of
# Legends fixture: market 181 is the match winner, outcome 181 is
# participant1 and 182 is participant2 (bookmakerOutcomeId "1" and "3").
MONEYLINE_MARKET = "181"
OUTCOME_P1 = "181"
OUTCOME_P2 = "182"

_throttle_lock = threading.Lock()
_next_allowed = 0.0

LAST_QUOTA = {"used": None, "remaining": None}


def _throttle():
    global _next_allowed
    with _throttle_lock:
        now = time.monotonic()
        wait = _next_allowed - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _next_allowed = now + MIN_REQUEST_INTERVAL_SEC


def _get(path: str, params: dict, treat_404_as_empty: bool = False):
    if not ODDSPAPI_KEY:
        raise OddsPapiError("ODDSPAPI_KEY is not set")
    params = {**params, "apiKey": ODDSPAPI_KEY}
    for attempt in range(MAX_RETRIES):
        _throttle()
        resp = requests.get(f"{ODDSPAPI_BASE_URL}{path}", params=params, timeout=25)
        if resp.status_code == 429:
            wait = MIN_REQUEST_INTERVAL_SEC * (attempt + 1)
            try:
                retry_ms = resp.json().get("error", {}).get("retryMs")
                if retry_ms:
                    wait = max(wait, float(retry_ms) / 1000)
            except (ValueError, json.JSONDecodeError, AttributeError):
                pass
            time.sleep(wait)
            continue
        # A tournament/bookmaker pair with no scheduled matches 404s with
        # FIXTURE_NOT_FOUND -- normal, not an error.
        if resp.status_code == 404 and treat_404_as_empty:
            return []
        if resp.status_code != 200:
            raise OddsPapiError(f"{resp.status_code} from {path}: {resp.text[:300]}")
        return resp.json()
    raise OddsPapiError(f"Gave up on {path} after {MAX_RETRIES} retries (rate limited)")


def list_participants(sport_id: int) -> dict:
    """{participant_id(str): name}. One call per sport; callers should cache."""
    data = _get("/v4/participants", {"sportId": sport_id})
    return data if isinstance(data, dict) else {}


def list_active_tournaments(sport_id: int) -> list:
    """Tournament ids that actually have fixtures scheduled.

    Filtering matters a lot: CS2 lists 329 tournaments but only 9 have any
    upcoming matches, and table tennis lists 605 with 3 active. Requesting the
    dead ones would burn the request allowance for nothing.
    """
    data = _get("/v4/tournaments", {"sportId": sport_id})
    if not isinstance(data, list):
        return []
    return [
        t["tournamentId"] for t in data
        if (t.get("upcomingFixtures") or 0) > 0 or (t.get("futureFixtures") or 0) > 0
    ]


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_odds(tournament_ids: list, bookmakers: list = None, on_error=None) -> list:
    """One request per (bookmaker, <=5 tournaments); merged fixture list back."""
    bookmakers = bookmakers or ODDSPAPI_BOOKMAKERS
    if not tournament_ids:
        return []
    jobs = [(bm, chunk) for bm in bookmakers
            for chunk in _chunk(tournament_ids, MAX_TOURNAMENTS_PER_CALL)]

    def _one(bookmaker, chunk):
        return _get(
            "/v4/odds-by-tournaments",
            {"tournamentIds": ",".join(str(t) for t in chunk), "bookmaker": bookmaker},
            treat_404_as_empty=True,
        )

    fixtures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_one, bm, ch): bm for bm, ch in jobs}
        for fut in as_completed(futures):
            bm = futures[fut]
            try:
                data = fut.result()
            except OddsPapiError as exc:
                if on_error:
                    on_error(bm, exc)
                continue
            if isinstance(data, list):
                fixtures.extend(data)
    return fixtures


def _is_prematch(start_time, now=None) -> bool:
    if not PREMATCH_ONLY:
        return True
    if not start_time:
        return False
    try:
        dt = datetime.fromisoformat(str(start_time).replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return dt > now + timedelta(minutes=PREMATCH_BUFFER_MINUTES)


def flatten_odds(fixtures: list, names_by_sport: dict, sport_keys: dict = None) -> list:
    """Convert to the shared record shape used by odds_client.flatten_odds().

    Only the moneyline market is kept. These sports are two-way (no draw), so
    outcomes map straight onto home/away, which is exactly what the rest of the
    pipeline already understands.
    """
    sport_keys = sport_keys or ODDSPAPI_SPORTS
    records = []
    skipped_live = 0
    for fx in fixtures:
        start_time = fx.get("startTime")
        if not _is_prematch(start_time):
            skipped_live += 1
            continue

        sport_id = fx.get("sportId")
        sport_key = sport_keys.get(sport_id, f"oddspapi_{sport_id}")
        names = names_by_sport.get(sport_id, {})
        home = names.get(str(fx.get("participant1Id"))) or f"#{fx.get('participant1Id')}"
        away = names.get(str(fx.get("participant2Id"))) or f"#{fx.get('participant2Id')}"

        for bookmaker, bdata in (fx.get("bookmakerOdds") or {}).items():
            if bdata.get("suspended"):
                continue
            market = (bdata.get("markets") or {}).get(MONEYLINE_MARKET)
            if not market or market.get("marketActive") is False:
                continue
            for outcome_id, odata in (market.get("outcomes") or {}).items():
                side = ("home" if outcome_id == OUTCOME_P1
                        else "away" if outcome_id == OUTCOME_P2 else None)
                if side is None:
                    continue
                for pdata in (odata.get("players") or {}).values():
                    price = pdata.get("price")
                    if price is None or pdata.get("active") is False:
                        continue
                    if pdata.get("mainLine") is False:
                        continue
                    name = home if side == "home" else away
                    records.append({
                        "fixture_id": str(fx.get("fixtureId")),
                        "sport_key": sport_key,
                        "sport_title": sport_key,
                        "start_time": start_time,
                        "home_team": home,
                        "away_team": away,
                        "bookmaker": bookmaker.lower(),
                        "market_id": "h2h",
                        "outcome_id": side,
                        "player_key": "-",
                        "price": float(price),
                        "label": f"{home} vs {away}: {name}",
                    })
                    break  # one price per outcome; alt lines are skipped above
    if skipped_live:
        print(f"[oddspapi] skipped {skipped_live} in-play fixture(s) -- pre-match only")
    return records


def collect(on_error=None, names_cache: dict = None, tournaments_cache: dict = None) -> tuple:
    """Full cycle for every configured sport.

    Returns (records, names_by_sport, tournaments_by_sport) so the caller can
    persist the two lookup tables and skip re-fetching them next run.
    """
    names_cache = names_cache or {}
    tournaments_cache = tournaments_cache or {}
    names_by_sport, tourneys_by_sport, all_fixtures = {}, {}, []

    for sport_id in ODDSPAPI_SPORTS:
        try:
            names = names_cache.get(sport_id) or list_participants(sport_id)
            tourneys = tournaments_cache.get(sport_id)
            if tourneys is None:
                tourneys = list_active_tournaments(sport_id)
        except OddsPapiError as exc:
            if on_error:
                on_error(f"sport {sport_id}", exc)
            continue
        names_by_sport[sport_id] = names
        tourneys_by_sport[sport_id] = tourneys
        if tourneys:
            all_fixtures.extend(fetch_odds(tourneys, on_error=on_error))

    records = flatten_odds(all_fixtures, names_by_sport)
    return records, names_by_sport, tourneys_by_sport
