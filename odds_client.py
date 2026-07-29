"""
Thin client around the OddsPapi REST API.

Docs: https://oddspapi.io/docs
Auth: query param `apiKey`
Base: https://api.oddspapi.io

NOTE: the exact market/outcome ID scheme (e.g. market "101" for 1X2) should
be double-checked against a live response once a real API key is wired in --
market IDs can vary per sport. flatten_odds() below is written defensively
so it won't crash on an unfamiliar shape, but confirm the mapping of
market/outcome IDs to human-readable labels before trusting alert text.
"""
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from config import ODDSPAPI_BASE_URL, ODDSPAPI_KEY


class OddsPapiError(RuntimeError):
    pass


# The free tier throttles hard (confirmed live 2026-07-29: 429 RATE_LIMITED,
# "Please wait 0.12 seconds before making another request"). A poll cycle
# makes on the order of a hundred+ calls (bookmakers x tournament chunks) --
# run sequentially with a per-call sleep that took several minutes, way past
# the 5-minute cron interval. _throttle() is a small shared rate gate so
# many worker threads (see fetch_odds_by_tournaments) can fire concurrently
# while still respecting one global "no more than ~1 request per
# MIN_REQUEST_INTERVAL_SEC" ceiling, instead of one thread's sleep blocking
# nothing but itself.
MIN_REQUEST_INTERVAL_SEC = 0.15
MAX_RETRIES = 5
MAX_WORKERS = 8

_throttle_lock = threading.Lock()
_next_allowed_time = 0.0


def _throttle():
    global _next_allowed_time
    with _throttle_lock:
        now = time.monotonic()
        wait = _next_allowed_time - now
        if wait > 0:
            time.sleep(wait)
            now += wait
        _next_allowed_time = now + MIN_REQUEST_INTERVAL_SEC


def _get(path: str, params: dict, treat_404_as_empty: bool = False):
    if not ODDSPAPI_KEY:
        raise OddsPapiError(
            "ODDSPAPI_KEY is not set. Copy .env.example to .env and fill it in."
        )
    params = {**params, "apiKey": ODDSPAPI_KEY}

    for attempt in range(MAX_RETRIES):
        _throttle()
        resp = requests.get(f"{ODDSPAPI_BASE_URL}{path}", params=params, timeout=20)
        if resp.status_code == 429:
            wait_sec = MIN_REQUEST_INTERVAL_SEC * (attempt + 1)
            try:
                body = resp.json()
                retry_ms = body.get("error", {}).get("retryMs")
                if retry_ms:
                    wait_sec = max(wait_sec, float(retry_ms) / 1000)
            except (ValueError, json.JSONDecodeError):
                pass
            time.sleep(wait_sec)
            continue
        # Confirmed live (2026-07-29): odds-by-tournaments 404s with
        # FIXTURE_NOT_FOUND whenever a given tournament/bookmaker combo simply
        # has no matches scheduled right now -- completely normal, not an
        # error, so callers that expect this (see fetch_odds_by_tournaments)
        # get an empty list back instead of a crash.
        if resp.status_code == 404 and treat_404_as_empty:
            return []
        if resp.status_code != 200:
            raise OddsPapiError(f"{resp.status_code} from {path}: {resp.text[:500]}")
        return resp.json()

    raise OddsPapiError(f"Gave up on {path} after {MAX_RETRIES} retries (rate limited).")


def list_sports() -> list:
    return _get("/v4/sports", {})


def list_tournaments(sport_id: int) -> list:
    return _get("/v4/tournaments", {"sportId": sport_id})


def get_fixture(fixture_id) -> dict:
    """Fetch fixture details -- used post-match to look up the final result
    for scoring alerts (see results.py). Exact shape of the result/score
    fields hasn't been confirmed against a finished live fixture yet, so
    results.py parses this defensively and leaves anything it can't
    confidently read as unresolved rather than guessing."""
    return _get("/v4/fixture", {"fixtureId": fixture_id})


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_odds_by_tournaments(tournament_ids: list, bookmakers: list, on_bookmaker_error=None) -> list:
    """Fetch current odds for the given tournaments x bookmakers.

    The live API rejects >1 bookmaker and >5 tournamentIds per call (confirmed
    2026-07-29 -- not documented up front), so this fires one request per
    (bookmaker, <=5 tournamentIds chunk) and merges the raw fixture lists.
    Each fixture in the merged result only carries odds for the single
    bookmaker it was fetched with; flatten_odds() handles that fine since it
    iterates whatever bookmakers are present per fixture.

    A poll cycle is on the order of a hundred+ of these calls (confirmed
    live 2026-07-29: running them one at a time, even with only a ~0.25s
    sleep each, took several minutes -- longer than the 5-minute cron
    interval this is meant to run on). MAX_WORKERS requests run concurrently
    through a shared ThreadPoolExecutor; _get()'s _throttle() still caps the
    combined request rate globally, so this doesn't hit the API any harder
    per second, it just stops one slow round-trip from blocking the next.

    A single bad/unsupported bookmaker slug (confirmed live: OddsPapi will
    400 with INVALID_PARAMETER if a bookmaker isn't supported, separate from
    the 404-when-no-fixtures case) shouldn't take down the whole poll cycle --
    that bookmaker's chunk is skipped and reported via on_bookmaker_error
    instead of raising, so one stale entry in ASIAN_SHARP_BOOKMAKERS /
    PUBLIC_BOOKMAKERS doesn't block everything else.
    """
    jobs = [
        (bookmaker, chunk)
        for bookmaker in bookmakers
        for chunk in _chunk(tournament_ids, 5)
    ]

    def _fetch_one(bookmaker, chunk):
        params = {
            "tournamentIds": ",".join(str(t) for t in chunk),
            "bookmaker": bookmaker,
        }
        return _get("/v4/odds-by-tournaments", params, treat_404_as_empty=True)

    all_fixtures = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_bookmaker = {
            pool.submit(_fetch_one, bookmaker, chunk): bookmaker
            for bookmaker, chunk in jobs
        }
        for future in as_completed(future_to_bookmaker):
            bookmaker = future_to_bookmaker[future]
            try:
                data = future.result()
            except OddsPapiError as exc:
                if on_bookmaker_error:
                    on_bookmaker_error(bookmaker, exc)
                continue
            if isinstance(data, list):
                all_fixtures.extend(data)
    return all_fixtures


def flatten_odds(raw_fixtures: list, main_lines_only: bool = True) -> list:
    """Turn the nested OddsPapi response into flat records:
    {fixture_id, start_time, bookmaker, market_id, outcome_id, player_key, price, label}
    so downstream storage/diffing doesn't need to know the nesting.

    Confirmed live (2026-07-29): a single fixture can carry hundreds of alt-line
    markets (spreads/totals at every increment) alongside the main line, which
    blows up record count and Telegram noise fast. By default we keep only
    entries where `mainLine` is true or absent (moneyline-style markets don't
    always set it) -- pass main_lines_only=False to keep everything.
    """
    records = []
    for fx in raw_fixtures:
        fixture_id = fx.get("fixtureId")
        start_time = fx.get("startTime")
        bookmaker_odds = fx.get("bookmakerOdds", {})
        for bookmaker, bdata in bookmaker_odds.items():
            markets = bdata.get("markets", {})
            for market_id, mdata in markets.items():
                outcomes = mdata.get("outcomes", {})
                for outcome_id, odata in outcomes.items():
                    players = odata.get("players", {})
                    for player_key, pdata in players.items():
                        price = pdata.get("price")
                        if price is None:
                            continue
                        if main_lines_only and pdata.get("mainLine") is False:
                            continue
                        records.append({
                            "fixture_id": fixture_id,
                            "start_time": start_time,
                            "bookmaker": bookmaker,
                            "market_id": str(market_id),
                            "outcome_id": str(outcome_id),
                            "player_key": str(player_key),
                            "price": float(price),
                            "label": pdata.get("bookmakerOutcomeId") or str(outcome_id),
                        })
    return records
