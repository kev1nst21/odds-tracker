"""Consolidates a raw poll into ONE summary per event, plus the analyst verdict.

Why this module exists (2026-07-29, user request): the previous output was
per-line, so a single football match produced four separate alerts -- "Larne FC
+16.7%", "Draw +13.3%", "Draw -15.4%", "Larne FC +11.1%" -- which is the same
market move seen from different sides at different bookmakers. Unreadable, and
it made the alert feed look busier than the market actually was. Everything
about one event now collapses into a single row/message.

The analyst part is the standard no-vig fair-price method used in professional
betting:

  1. Take a sharp book's full market for the event (Pinnacle first, 1xBet as
     fallback). Sharp books price closest to true probability because they
     accept large bets and let the money correct them.
  2. Convert its decimal odds to implied probabilities: p_i = 1 / odds_i.
  3. Those sum to more than 1 -- the excess is the bookmaker's margin (vig).
     S = sum(p_i), typically 1.02-1.08.
  4. Remove it proportionally: fair_p_i = p_i / S, so fair_odds_i = odds_i * S.

fair_odds is the break-even price: bet above it and the wager has positive
expected value *under this model*, bet below and it doesn't. That is exactly
the "от какого коэффициента можно ставить" number.

Hard limits worth stating plainly: this assumes the sharp book is correctly
priced, which is an assumption and not a fact; it says nothing about how much
to stake; and a positive edge is a long-run statistical statement, not a
prediction that any single bet wins. The output is a calculation, not advice.
"""
from collections import defaultdict

from config import (
    ASIAN_SHARP_BOOKMAKERS,
    EXCHANGE_BOOKMAKERS,
    MAX_SIGNAL_PRICE,
    MIN_EDGE_PCT,
)

# Order matters: whichever appears first and has a full market gets used as the
# fair-price reference.
_SHARP_PRIORITY = list(ASIAN_SHARP_BOOKMAKERS)

_SIDE_ORDER = {"home": 0, "draw": 1, "away": 2}


def is_signal_book(bookmaker: str) -> bool:
    """Exchanges are excluded from anything that generates a signal."""
    return (bookmaker or "").lower() not in EXCHANGE_BOOKMAKERS


def _usable(record) -> bool:
    return (
        is_signal_book(record.get("bookmaker"))
        and record.get("price")
        and 1.01 < float(record["price"]) <= MAX_SIGNAL_PRICE
    )


def _outcome_name(record, side):
    """Human label for an outcome: the team/player name where we have it,
    'Ничья' for the draw."""
    if side == "draw":
        return "Ничья"
    if side == "home":
        return record.get("home_team") or "П1"
    if side == "away":
        return record.get("away_team") or "П2"
    label = record.get("label") or ""
    return label.split(":", 1)[1].strip() if ":" in label else (label or str(side))


def _fair_prices(sharp_market: dict) -> dict:
    """no-vig fair odds per side. sharp_market: {side: decimal_odds} from ONE
    bookmaker. Needs at least 2 sides or the margin can't be identified."""
    prices = {s: p for s, p in sharp_market.items() if p and p > 1}
    if len(prices) < 2:
        return {}
    overround = sum(1.0 / p for p in prices.values())
    if overround <= 0:
        return {}
    return {side: price * overround for side, price in prices.items()}


def _pick_sharp_market(by_book: dict) -> tuple:
    """Return (bookmaker, {side: price}) for the highest-priority sharp book
    that quotes a full-enough market, or (None, {})."""
    for book in _SHARP_PRIORITY:
        market = by_book.get(book)
        if market and len(market) >= 2:
            return book, market
    return None, {}


def _stars(down_books: set, sharp_moved: bool) -> int:
    """Confidence, 0-3, from how BROAD the move is rather than how big.

    One bookmaker shortening a price proves nothing -- it could be a trader
    correcting an error, or a single large recreational bet. The same outcome
    shortening at many independent books within one 30-minute window is the
    classic "steam move": informed money hitting the market everywhere at once,
    which is the pattern actually worth acting on. A sharp book joining the move
    counts extra, since those books move on money rather than on copying others.
    """
    n = len(down_books)
    if n == 0:
        return 0
    stars = 3 if n >= 4 else (2 if n >= 2 else 1)
    if sharp_moved:
        stars = min(3, stars + 1)
    return stars


def build_event_summaries(records: list, spikes: list = None, movements: list = None) -> list:
    """One dict per event, sorted so the most actionable comes first.

    Each summary carries every outcome with its price range across the market
    (min-max), the best price available, the computed fair price, the edge over
    fair, how many bookmakers moved the line and in which direction, and a
    0-3 star confidence score derived from that breadth.
    """
    spikes = spikes or []
    # Fall back to spikes when no full movement list is supplied (older callers
    # and tests) -- breadth scoring is then coarser but still works.
    movements = movements if movements is not None else spikes

    by_event = defaultdict(list)
    for r in records:
        if _usable(r):
            by_event[r["fixture_id"]].append(r)

    # Consolidate movement per (event, side). The previous per-line output
    # double-counted the same market move across bookmakers; here every book
    # that moved is collected once so breadth can be measured.
    moves = defaultdict(list)
    for m in movements:
        if is_signal_book(m.get("bookmaker")) and float(m.get("price") or 0) <= MAX_SIGNAL_PRICE:
            moves[(m["fixture_id"], m["outcome_id"])].append(
                (m["bookmaker"].lower(), m["pct_change"])
            )
    # Which sides actually spiked past the alert threshold this cycle.
    spiked_sides = {(s["fixture_id"], s["outcome_id"]) for s in spikes
                    if is_signal_book(s.get("bookmaker"))}

    summaries = []
    for fixture_id, rows in by_event.items():
        sample = rows[0]

        by_side = defaultdict(list)
        by_book = defaultdict(dict)
        for r in rows:
            by_side[r["outcome_id"]].append(r)
            by_book[r["bookmaker"].lower()][r["outcome_id"]] = float(r["price"])

        sharp_book, sharp_market = _pick_sharp_market(by_book)
        fair = _fair_prices(sharp_market)

        outcomes = []
        for side, side_rows in by_side.items():
            prices = [float(x["price"]) for x in side_rows]
            best = max(prices)
            best_book = max(side_rows, key=lambda x: float(x["price"]))["bookmaker"]
            fair_price = fair.get(side)
            edge_pct = ((best / fair_price) - 1) * 100 if fair_price else None
            move_list = moves.get((fixture_id, side)) or []
            down_books = {b for b, p in move_list if p < 0}
            up_books = {b for b, p in move_list if p > 0}
            down_moves = [p for _, p in move_list if p < 0]
            avg_move = (sum(p for _, p in move_list) / len(move_list) * 100) if move_list else None
            avg_down = (sum(down_moves) / len(down_moves) * 100) if down_moves else None
            sharp_down = any(b in ASIAN_SHARP_BOOKMAKERS for b in down_books)
            outcomes.append({
                "side": side,
                "name": _outcome_name(sample, side),
                "min_price": min(prices),
                "max_price": best,
                "best_book": best_book,
                "books_count": len(prices),
                "sharp_price": sharp_market.get(side),
                "fair_price": fair_price,
                "edge_pct": edge_pct,
                "move_pct": avg_move,
                "down_count": len(down_books),
                "up_count": len(up_books),
                "avg_down_pct": avg_down,
                "sharp_moved": sharp_down,
                "stars": _stars(down_books, sharp_down),
                "spiked": (fixture_id, side) in spiked_sides,
            })

        outcomes.sort(key=lambda o: _SIDE_ORDER.get(o["side"], 9))

        # Where the market went, as ONE statement: the side backed by the most
        # bookmakers wins, with the size of the drop as the tie-break. Counting
        # books first (rather than picking the single biggest percentage) is
        # what stops one outlier book from defining the event's direction.
        shortening = [o for o in outcomes if o["down_count"] > 0]
        if shortening:
            # Prefer an outcome that actually crossed the alert threshold, then
            # the one most bookmakers agree on, then the biggest drop. Without
            # the first key the headline can land on a 1.6% drift while the
            # outcome that genuinely spiked goes unmentioned.
            lead = max(shortening, key=lambda o: (o["spiked"], o["down_count"], -(o["avg_down_pct"] or 0)))
            movement = {
                "name": lead["name"],
                "pct": lead["avg_down_pct"] or 0.0,
                "toward": True,
                "books": lead["down_count"],
                "total_books": lead["books_count"],
                "sharp": lead["sharp_moved"],
                "stars": lead["stars"],
                "spiked": lead["spiked"],
            }
        else:
            movement = None

        value = [o for o in outcomes if o["edge_pct"] is not None and o["edge_pct"] >= MIN_EDGE_PCT]
        value.sort(key=lambda o: -o["edge_pct"])
        best_value = value[0] if value else None

        # The badge must describe the move the verdict actually talks about, so
        # take the lead outcome's score rather than the best score anywhere on
        # the event.
        stars = movement["stars"] if movement else 0
        # Only report an event as "moving" once at least one book crossed the
        # alert threshold -- broad sub-threshold drift still sets the stars and
        # the direction, but on its own it isn't news.
        has_move = bool(movement and any(o["spiked"] for o in outcomes))

        summaries.append({
            "fixture_id": fixture_id,
            "sport_key": sample.get("sport_key"),
            "start_time": sample.get("start_time"),
            "home_team": sample.get("home_team"),
            "away_team": sample.get("away_team"),
            "outcomes": outcomes,
            "movement": movement,
            "sharp_book": sharp_book,
            "best_value": best_value,
            "has_value": bool(best_value),
            "has_move": has_move,
            "stars": stars,
            "verdict": _verdict(movement, best_value, outcomes),
        })

    # Most trustworthy first: confidence stars, then value + movement, then the
    # size of the edge.
    summaries.sort(key=lambda s: (
        -s["stars"],
        not (s["has_value"] and s["has_move"]),
        not s["has_value"],
        not s["has_move"],
        -(s["best_value"]["edge_pct"] if s["best_value"] else 0),
    ))
    return summaries


def _verdict(movement, best_value, outcomes) -> str:
    """One plain-language conclusion per event. States the entry price when the
    model finds one, and says so directly when it doesn't -- silence here would
    read as 'no opinion' rather than 'no edge'."""
    parts = []
    if movement:
        books = movement.get("books", 0)
        total = movement.get("total_books", 0)
        stars = movement.get("stars", 0)
        parts.append(
            f"Линия просела на «{movement['name']}» у {books} из {total} контор "
            f"(в среднем {abs(movement['pct']):.1f}%)"
            + (", включая шарпов" if movement.get("sharp") else "") + "."
        )
        # The factual count above and the strength label below must never
        # disagree, so the label is driven by the same star score shown on the
        # card -- previously the count decided the wording and the stars were
        # computed separately, which produced "3 из 13 ... сигнал средний" on
        # an event badged three stars.
        breadth = (
            "Движение подтверждено по всему рынку" if books >= 4
            else "Движение подтвердили несколько контор" if books >= 2
            else "Просело пока только у шарп-конторы, а она обычно идёт первой" if movement.get("sharp")
            else "Просело только у одной конторы"
        )
        strength = (
            "сигнал сильный" if stars >= 3
            else "сигнал средний" if stars == 2
            else "сигнал слабый, может быть и шум"
        )
        parts.append(f"{breadth} — {strength}.")

    if best_value:
        parts.append(
            f"Ставить «{best_value['name']}» можно от {best_value['fair_price']:.2f}. "
            f"Сейчас на рынке дают до {best_value['max_price']:.2f} — "
            f"запас {best_value['edge_pct']:+.1f}%."
        )
    else:
        priced = [o for o in outcomes if o["fair_price"]]
        if priced:
            closest = max(priced, key=lambda o: o["edge_pct"] if o["edge_pct"] is not None else -99)
            parts.append(
                f"Ценности нет: справедливый коэффициент «{closest['name']}» — "
                f"{closest['fair_price']:.2f}, а лучший на рынке всего {closest['max_price']:.2f}. "
                f"Ждём цену выше."
            )
        else:
            parts.append("Расчёт невозможен: шарп-контора не дала полную линию по этому событию.")

    return " ".join(parts)
