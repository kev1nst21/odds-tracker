"""Turns a raw poll into ONE actionable card per event.

THE STRATEGY THIS IMPLEMENTS (stated by the user, 2026-07-29 -- read this
before changing anything here, because it is not the same as generic
"find positive expected value" logic):

    "Если коэффициент был 1.5, а где-то мы увидели, что просел до 1.3, то мы
     ставим на 1.5 и не рассматриваем вариант поставить в другую сторону
     из-за того, что коэффициент там подрос. То есть мы видим, что на какой-то
     конторе грузанули денег на какое-то событие, и нам нужно прогрузить на
     тех конторах, где этого не было."

So the whole product reduces to four questions per event:

    1. WHICH outcome did money go into?  -> the side whose price DROPPED.
    2. HOW FAR did it drop, and at how many books?  -> was 1.50, now 1.30,
       at N of M bookmakers. Breadth is the confidence signal: one book moving
       can be a single punter or a trader's typo, the same outcome dropping at
       many independent books inside half an hour is informed money.
    3. WHERE can that same outcome still be backed at the OLD price?  -> the
       bookmakers that haven't moved yet. THIS is the bet.
    4. If nobody is left offering the old price, say so plainly -- the entry
       is gone and there is nothing to do.

Two consequences that are easy to get wrong:

  * The opposite side drifting UP is not a signal. When money hits the home
    side, the away price mechanically rises; betting the away side because
    "its odds improved" is backing the side the market just moved against.
    Rises are therefore never turned into bets here.
  * A drop that has already reached every bookmaker is not actionable, however
    dramatic it looks. The card still shows it (it's real information about
    where money went) but it is explicitly marked as a closed entry.

The no-vig fair price is still computed and shown as a secondary reference --
it answers "is this price sane in absolute terms" -- but it does not drive the
recommendation. The recommendation comes from the money-flow logic above.

Fair price method, for reference: take a sharp book's full market, convert to
implied probabilities p_i = 1/odds_i, note they sum to S > 1 (the margin), and
remove it proportionally -> fair_odds_i = odds_i * S.

Limits worth stating plainly: a price lagging behind the market is not a
guarantee of anything -- sometimes the move is wrong, sometimes the lagging
book simply hasn't been asked for that bet yet and will refuse or void it.
Nothing here is advice, and none of it says how much to stake.
"""
from collections import defaultdict

from config import (
    ASIAN_SHARP_BOOKMAKERS,
    EXCHANGE_BOOKMAKERS,
    MAX_SIGNAL_PRICE,
    MIN_SIGNAL_PRICE,
    ENTRY_MIN_GAP_PCT,
    EXCLUDE_DRAW,
    SPIKE_THRESHOLD_PCT,
)

_SHARP_PRIORITY = list(ASIAN_SHARP_BOOKMAKERS)
_SIDE_ORDER = {"home": 0, "draw": 1, "away": 2}


def is_signal_book(bookmaker: str) -> bool:
    """Exchanges never generate signals -- their thin markets swing on one
    random user's order (confirmed live: betfair_ex_eu went 2.28 -> 9.20 in a
    single window while every real bookmaker barely moved)."""
    return (bookmaker or "").lower() not in EXCHANGE_BOOKMAKERS


def _usable(record) -> bool:
    """Must stay identical to the filter in detector.detect() -- if the two
    disagree, a bookmaker can appear in the movement list without appearing in
    the price list, which produced "просело у 8 из 4 контор" on 2026-07-29."""
    return (
        is_signal_book(record.get("bookmaker"))
        and record.get("price")
        and MIN_SIGNAL_PRICE <= float(record["price"]) <= MAX_SIGNAL_PRICE
    )


def _outcome_name(record, side):
    if side == "draw":
        return "Ничья"
    if side == "home":
        return record.get("home_team") or "П1"
    if side == "away":
        return record.get("away_team") or "П2"
    label = record.get("label") or ""
    return label.split(":", 1)[1].strip() if ":" in label else (label or str(side))


def _fair_prices(sharp_market: dict) -> dict:
    prices = {s: p for s, p in sharp_market.items() if p and p > 1}
    if len(prices) < 2:
        return {}
    overround = sum(1.0 / p for p in prices.values())
    if overround <= 0:
        return {}
    return {side: price * overround for side, price in prices.items()}


def _pick_sharp_market(by_book: dict) -> tuple:
    for book in _SHARP_PRIORITY:
        market = by_book.get(book)
        if market and len(market) >= 2:
            return book, market
    return None, {}


def _stars(down_books: set, sharp_moved: bool) -> int:
    """Confidence 0-3, from how BROAD the drop is rather than how big.

    One bookmaker shortening proves little. The same outcome shortening at many
    independent books inside one polling window is the classic "steam move" --
    informed money hitting the market everywhere at once. A sharp book joining
    counts extra, since those move on money rather than on copying rivals.
    """
    n = len(down_books)
    if n == 0:
        return 0
    stars = 3 if n >= 4 else (2 if n >= 2 else 1)
    if sharp_moved:
        stars = min(3, stars + 1)
    return stars


def build_event_summaries(records: list, spikes: list = None, movements: list = None) -> list:
    """One dict per event, most actionable first."""
    spikes = spikes or []
    movements = movements if movements is not None else spikes

    by_event = defaultdict(list)
    for r in records:
        if _usable(r):
            by_event[r["fixture_id"]].append(r)

    moves = defaultdict(list)
    for m in movements:
        price = float(m.get("price") or 0)
        if is_signal_book(m.get("bookmaker")) and MIN_SIGNAL_PRICE <= price <= MAX_SIGNAL_PRICE:
            moves[(m["fixture_id"], m["outcome_id"])].append(m)

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
            prices_by_book = {r["bookmaker"].lower(): float(r["price"]) for r in side_rows}
            prices = list(prices_by_book.values())

            move_list = moves.get((fixture_id, side)) or []
            # Belt and braces on top of the shared price filter: only count a
            # bookmaker as having moved if it is also currently quoting this
            # outcome, so "просело у N из M" can never report N > M whatever
            # else changes upstream.
            dropped = [m for m in move_list
                       if m["pct_change"] < 0 and m["bookmaker"].lower() in prices_by_book]
            down_books = {m["bookmaker"].lower() for m in dropped}
            sharp_down = any(b in ASIAN_SHARP_BOOKMAKERS for b in down_books)

            old_price = new_price = drop_pct = None
            entries = []
            if dropped:
                # "Было" = the best price on offer before money arrived; "стало"
                # = where the books that moved have settled. Using max(prev) and
                # min(current) frames the move the way it's actually acted on:
                # the widest gap between the old price and the new consensus.
                old_price = max(m["prev_price"] for m in dropped)
                new_price = min(m["price"] for m in dropped)
                if old_price:
                    drop_pct = (new_price - old_price) / old_price * 100

                # The entry: books that have NOT moved and still price this
                # outcome meaningfully above where the market went.
                threshold = new_price * (1 + ENTRY_MIN_GAP_PCT / 100.0)
                entries = sorted(
                    ((b, p) for b, p in prices_by_book.items()
                     if b not in down_books and p >= threshold),
                    key=lambda bp: -bp[1],
                )

            fair_price = fair.get(side)
            outcomes.append({
                "side": side,
                "name": _outcome_name(sample, side),
                "min_price": min(prices),
                "max_price": max(prices),
                "books_count": len(prices),
                "fair_price": fair_price,
                "old_price": old_price,
                "new_price": new_price,
                "drop_pct": drop_pct,
                "down_count": len(down_books),
                "sharp_moved": sharp_down,
                "stars": _stars(down_books, sharp_down),
                "spiked": (fixture_id, side) in spiked_sides,
                "entries": entries[:3],
                "entry_price": entries[0][1] if entries else None,
                "entry_book": entries[0][0] if entries else None,
                "entry_gap_pct": ((entries[0][1] / new_price - 1) * 100)
                                 if entries and new_price else None,
            })

        outcomes.sort(key=lambda o: _SIDE_ORDER.get(o["side"], 9))
        # The draw is dropped only AFTER the fair-price maths above, which needs
        # the full 3-way market to measure the bookmaker's margin correctly.
        if EXCLUDE_DRAW:
            outcomes = [o for o in outcomes if o["side"] != "draw"]

        # The bet is on the side money went INTO. Never on the side that merely
        # drifted up as a mechanical consequence -- see the module docstring.
        shortening = [o for o in outcomes if o["down_count"] > 0]
        bet = None
        if shortening:
            bet = max(shortening, key=lambda o: (o["spiked"], o["down_count"],
                                                 -(o["drop_pct"] or 0)))

        has_entry = bool(bet and bet["entry_price"])
        stars = bet["stars"] if bet else 0
        # Only a drop of at least the alert threshold is worth a notification.
        # Sub-threshold drift still shows on the site but must never reach the
        # bot -- the user explicitly does not want small moves pushed to them.
        big_enough = bool(bet and abs(bet["drop_pct"] or 0) >= SPIKE_THRESHOLD_PCT * 100)

        summaries.append({
            "fixture_id": fixture_id,
            "sport_key": sample.get("sport_key"),
            "start_time": sample.get("start_time"),
            "home_team": sample.get("home_team"),
            "away_team": sample.get("away_team"),
            "outcomes": outcomes,
            "bet": bet,
            "sharp_book": sharp_book,
            "has_entry": has_entry,
            "entry_closed": bool(bet and not has_entry),
            "big_move": big_enough,
            "alertable": big_enough and has_entry,
            "stars": stars,
            "verdict": _verdict(bet, has_entry),
        })

    # Sub-threshold drift is dropped entirely rather than shown greyed out.
    # The user's instruction is explicit: "мы не ищем меньше 10%... нам не
    # обязательно быстро найти, нам главное находить". Showing weak moves
    # anywhere invites acting on them, so an empty page is the correct output
    # when the market is quiet.
    summaries = [s for s in summaries if s["big_move"]]

    # Actionable first: a big move you can still get on, then confidence, then
    # how much room is left.
    summaries.sort(key=lambda s: (
        not s["alertable"],
        not s["has_entry"],
        -s["stars"],
        -((s["bet"] or {}).get("entry_gap_pct") or 0),
    ))
    return summaries


def find_live_anomalies(records: list, min_spread_pct: float = 25.0, limit: int = 25) -> list:
    """Odd things happening in matches that are already under way.

    These are NEVER bets. In-play prices move on what is happening on the pitch,
    so the money-flow logic doesn't apply. What IS informative live is
    DISAGREEMENT: when one bookmaker is offering 4.50 on an outcome the rest of
    the market has at 2.10, somebody has not repriced -- usually a book that
    missed a goal or a break of serve. That gap is the strange thing worth
    looking at, and unlike a price move it needs no history to detect, just one
    snapshot across several books.

    Returns rows sorted by how wide the disagreement is.
    """
    by_side = defaultdict(list)
    for r in records:
        if _usable(r):
            by_side[(r["fixture_id"], r["outcome_id"])].append(r)

    rows = []
    for (fixture_id, side), group in by_side.items():
        if len(group) < 3:
            continue  # two books disagreeing is not yet a market
        prices = sorted(float(x["price"]) for x in group)
        low, high = prices[0], prices[-1]
        if low <= 0:
            continue
        spread = (high / low - 1) * 100
        if spread < min_spread_pct:
            continue
        # The outlier is the single book away from the pack; compare it with
        # the median of the rest so one weird quote can't define "the market".
        mid = prices[len(prices) // 2]
        top = max(group, key=lambda x: float(x["price"]))
        sample = group[0]
        rows.append({
            "fixture_id": fixture_id,
            "sport_key": sample.get("sport_key"),
            "start_time": sample.get("start_time"),
            "home_team": sample.get("home_team"),
            "away_team": sample.get("away_team"),
            "name": _outcome_name(sample, side),
            "low": low,
            "high": high,
            "median": mid,
            "spread_pct": spread,
            "outlier_book": top["bookmaker"],
            "books_count": len(group),
        })

    rows.sort(key=lambda r: -r["spread_pct"])
    return rows[:limit]


def _verdict(bet, has_entry) -> str:
    """The conclusion, phrased the way the user actually reasons about the bet:
    'был коэффициент 3, просел до 2.1, желательно проставить за 3'."""
    if not bet:
        return "Движения нет — линия стоит на месте, входа нет."

    name = bet["name"]
    parts = [
        f"«{name}»: был коэффициент {bet['old_price']:.2f}, просел до "
        f"{bet['new_price']:.2f} у {bet['down_count']} из {bet['books_count']} контор"
        + (", включая шарпов" if bet["sharp_moved"] else "") + "."
    ]

    if has_entry:
        where = ", ".join(f"{b} — {p:.2f}" for b, p in bet["entries"])
        parts.append(
            f"Желательно проставить «{name}» за {bet['entry_price']:.2f}. "
            f"Ещё не просело у: {where}."
        )
    else:
        parts.append(
            "Проставить по старой цене уже негде — просело у всех контор, вход закрыт."
        )

    stars = bet["stars"]
    breadth = (
        "Движение подтверждено по всему рынку" if bet["down_count"] >= 4
        else "Движение подтвердили несколько контор" if bet["down_count"] >= 2
        else "Пока подвинулась только шарп-контора, а она обычно идёт первой"
        if bet["sharp_moved"] else "Подвинулась пока только одна контора"
    )
    strength = ("сигнал сильный" if stars >= 3
                else "сигнал средний" if stars == 2
                else "сигнал слабый, может быть и шум")
    parts.append(f"{breadth} — {strength}.")
    return " ".join(parts)
