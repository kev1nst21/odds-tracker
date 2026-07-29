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
import time

import requests

from config import ODDSPAPI_BASE_URL, ODDSPAPI_KEY


class OddsPapiError(RuntimeError):
    pass


# The free tier throttles hard (confirmed live 2026-07-29: 429 RATE_LIMITED,
# "Please wait 0.12 seconds before making another request"). We poll ~30
# bookmakers x several tournament chunks per run, so a fixed pause between
# every call plus honoring the API's own retryAfter/retryMs on 429 keeps us
# under the limit instead of failing the whole run on the first throttle hit.
MIN_REQUEST_INTERVAL_SEC = 0.25
MAX_RETRIES = 5


def _get(path: str, params: dict) -> dict:
    if not ODDSPAPI_KEY:
        raise OddsPapiError(
            "ODDSPAPI_KEY is not set. Copy .env.example to .env and fill it in."
        )
    params = {**params, "apiKey": ODDSPAPI_KEY}

    for attempt in range(MAX_RETRIES):
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
        time.sleep(MIN_REQUEST_INTERVAL_SEC)
        if resp.status_code != 200:
            raise OddsPapiError(f"{resp.status_code} from {path}: {resp.text[:500]}")
        return resp.json()

    raise OddsPapiError(f"Gave up on {path} after {MAX_RETRIES} retries (rate limited).")


def list_sports() -> list:
    return _get("/v4/sports", {})


def list_tournaments(sport_id: int) -> list:
    return _get("/v4/tournaments", {"sportId": sport_id})


def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def fetch_odds_by_tournaments(tournament_ids: list, bookmakers: list) -> list:
    """Fetch current odds for the given tournaments x bookmakers.

    The live API rejects >1 bookmaker and >5 tournamentIds per call (confirmed
    2026-07-29 -- not documented up front), so this loops one request per
    (bookmaker, <=5 tournamentIds chunk) and merges the raw fixture lists.
    Each fixture in the merged result only carries odds for the single
    bookmaker it was fetched with; flatten_odds() handles that fine since it
    iterates whatever bookmakers are present per fixture.
    """
    all_fixtures = []
    for bookmaker in bookmakers:
        for chunk in _chunk(tournament_ids, 5):
            params = {
                "tournamentIds": ",".join(str(t) for t in chunk),
                "bookmaker": bookmaker,
            }
            data = _get("/v4/odds-by-tournaments", params)
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
