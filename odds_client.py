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
import requests

from config import (
    THEODDSAPI_BASE_URL,
    THEODDSAPI_KEY,
    REGIONS,
    MARKETS,
    SOCCER_LEAGUE_KEYS,
    TENNIS_GROUP,
)


class TheOddsApiError(RuntimeError):
    pass


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
        print(f"[odds_client] quota: used={used} remaining={remaining} (call: {path})")
    return resp.json()


def list_sports() -> list:
    """Free call -- every sport currently in season, each with
    'key', 'group', 'title', 'active'."""
    return _get("/v4/sports/", {})


def select_sport_keys(all_sports: list = None) -> list:
    """Dynamic sport-key selection: the fixed soccer leagues (only if they're
    currently listed -- a league can briefly vanish between seasons) plus
    every currently in-season tennis tournament (group == TENNIS_GROUP), so
    tennis coverage doesn't go stale the moment one tournament ends."""
    if all_sports is None:
        all_sports = list_sports()
    live_keys = {s["key"] for s in all_sports if s.get("active", True)}
    selected = [k for k in SOCCER_LEAGUE_KEYS if k in live_keys]
    selected += sorted(
        s["key"] for s in all_sports
        if s.get("group") == TENNIS_GROUP and s.get("key") in live_keys
    )
    return selected


def fetch_odds_for_sport(sport_key: str, on_error=None) -> list:
    """GET /v4/sports/{sport_key}/odds/ -- every event for this sport, with
    every bookmaker's prices nested inside. Costs len(MARKETS split by comma)
    x len(REGIONS split by comma) credits (confirmed live 2026-07-29)."""
    try:
        return _get(f"/v4/sports/{sport_key}/odds/", {"regions": REGIONS, "markets": MARKETS})
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
    records = []
    for event in raw_events:
        fixture_id = event.get("id")
        start_time = event.get("commence_time")
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
    return records
