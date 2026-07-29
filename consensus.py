"""Aggregate analysis across all tracked bookmakers: for every line, compares
the average price on Asian/sharp books vs public/soft books. A gap between
the two usually means sharp money has already moved and the public books
haven't caught up yet -- exactly the kind of signal worth surfacing beyond
single-bookmaker spikes."""
from collections import defaultdict

from config import ASIAN_SHARP_BOOKMAKERS, get_region

DIVERGENCE_THRESHOLD_PCT = 0.05  # 5% gap between sharp avg and public avg
REGION_DIVERGENCE_THRESHOLD_PCT = 0.05  # 5% gap between Asia avg and Europe avg


def sharp_vs_public(records: list) -> list:
    """records: flattened odds for one poll (output of odds_client.flatten_odds).
    Returns rows sorted by |divergence| desc:
    {fixture_id, market_id, outcome_id, label, sharp_avg, public_avg, divergence_pct,
     sharp_books, public_books}
    """
    groups = defaultdict(lambda: {"sharp": [], "public": []})
    for r in records:
        key = (r["fixture_id"], r["market_id"], r["outcome_id"], r["player_key"])
        bucket = "sharp" if r["bookmaker"].lower() in ASIAN_SHARP_BOOKMAKERS else "public"
        groups[key][bucket].append(r)

    rows = []
    for (fixture_id, market_id, outcome_id, player_key), g in groups.items():
        if not g["sharp"] or not g["public"]:
            continue  # need at least one book on each side to compare
        sharp_avg = sum(x["price"] for x in g["sharp"]) / len(g["sharp"])
        public_avg = sum(x["price"] for x in g["public"]) / len(g["public"])
        if public_avg == 0:
            continue
        divergence = (sharp_avg - public_avg) / public_avg
        if abs(divergence) < DIVERGENCE_THRESHOLD_PCT:
            continue
        sample = g["sharp"][0]
        rows.append({
            "fixture_id": fixture_id,
            "market_id": market_id,
            "outcome_id": outcome_id,
            "home_team": sample.get("home_team"),
            "away_team": sample.get("away_team"),
            "label": sample.get("label") or f"{market_id}/{outcome_id}",
            "sharp_avg": sharp_avg,
            "public_avg": public_avg,
            "divergence_pct": divergence,
            "sharp_books": sorted({x["bookmaker"] for x in g["sharp"]}),
            "public_books": sorted({x["bookmaker"] for x in g["public"]}),
        })

    rows.sort(key=lambda x: -abs(x["divergence_pct"]))
    return rows


def region_breakdown(records: list) -> list:
    """Same shape as sharp_vs_public() but split by pure geography (Asia vs
    Europe) instead of sharp/public status -- a separate, clearer lens the
    dashboard shows as its own card so the two views don't get mixed up."""
    groups = defaultdict(lambda: defaultdict(list))
    for r in records:
        key = (r["fixture_id"], r["market_id"], r["outcome_id"], r["player_key"])
        groups[key][get_region(r["bookmaker"])].append(r)

    rows = []
    for (fixture_id, market_id, outcome_id, player_key), by_region in groups.items():
        asia = by_region.get("asia", [])
        europe = by_region.get("europe", [])
        if not asia or not europe:
            continue  # need at least one book on each side to compare
        asia_avg = sum(x["price"] for x in asia) / len(asia)
        europe_avg = sum(x["price"] for x in europe) / len(europe)
        if europe_avg == 0:
            continue
        divergence = (asia_avg - europe_avg) / europe_avg
        if abs(divergence) < REGION_DIVERGENCE_THRESHOLD_PCT:
            continue
        sample = asia[0]
        rows.append({
            "fixture_id": fixture_id,
            "market_id": market_id,
            "outcome_id": outcome_id,
            "home_team": sample.get("home_team"),
            "away_team": sample.get("away_team"),
            "label": sample.get("label") or f"{market_id}/{outcome_id}",
            "asia_avg": asia_avg,
            "europe_avg": europe_avg,
            "divergence_pct": divergence,
            "asia_books": sorted({x["bookmaker"] for x in asia}),
            "europe_books": sorted({x["bookmaker"] for x in europe}),
        })

    rows.sort(key=lambda x: -abs(x["divergence_pct"]))
    return rows
