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
    OPTIMAL_MAX_PRICE,
    SAFE_TRIGGER_PRICE,
    SAFE_TARGET_MIN,
    SAFE_TARGET_MAX,
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


# What a safer alternative looks like in sports with no draw. These cannot be
# computed from the moneyline data we buy -- a handicap is a separate market --
# so the honest thing is to name the market and hand it to the analyst rather
# than invent a number. The user's own instruction for tennis was exactly this:
# "безопасный вариант это фора по геймам... это уже задача аналитика".
_HANDICAP_HINTS = (
    ("tennis_", "фора по геймам (обычно −3.5 / +3.5) либо тотал геймов"),
    ("table_tennis", "фора по очкам"),
    ("esports_", "фора по картам (−1.5 / +1.5)"),
)


def _handicap_hint(sport_key: str):
    key = (sport_key or "").lower()
    for prefix, hint in _HANDICAP_HINTS:
        if key.startswith(prefix):
            return hint
    return None


def _safe_variant(bet: dict, by_book: dict, sport_key: str):
    """A lower-risk way to back the same opinion when the straight price is
    high (user decision, 2026-07-29: above 3.5 offer a "безопасный" variant
    landing around 1.7-2.5).

    In football the maths is exact and needs no extra data. Backing "our side
    OR the draw" is the same as splitting a stake across those two outcomes,
    and the combined decimal price of doing that is

        1 / (1/pick + 1/draw)

    which is what a bookmaker's own double-chance market is derived from. We
    take the best such combination available at a single bookmaker, since both
    legs have to be placed in the same place for the stake split to work.
    Bookmakers' own 1X markets are usually a little worse than this figure --
    they add margin on top -- so the number is stated as what the split
    achieves, not as a price you will see on a screen.

    In two-way sports (tennis, esports, table tennis) there is no draw and
    therefore no double chance. A handicap is the equivalent, but it lives in a
    market we don't buy, so the variant returned there is a named instruction
    rather than a computed price.
    """
    if not bet or not bet.get("entry_price"):
        return None
    if bet["entry_price"] <= SAFE_TRIGGER_PRICE:
        return None

    side, name = bet["side"], bet["name"]

    best = None
    for book, market in by_book.items():
        pick, draw = market.get(side), market.get("draw")
        if not pick or not draw or pick <= 1 or draw <= 1:
            continue
        price = 1.0 / (1.0 / pick + 1.0 / draw)
        if best is None or price > best["price"]:
            best = {"price": price, "book": book, "pick_odds": pick, "draw_odds": draw}

    if best:
        # Below the floor the alternative stops being worth the trade -- you'd
        # be risking a lot to win very little -- so it's reported but flagged.
        in_band = SAFE_TARGET_MIN <= best["price"] <= SAFE_TARGET_MAX
        return {
            "market": "double_chance",
            "pick": f"{name} или ничья",
            "price": round(best["price"], 2),
            "book": best["book"],
            "legs": [(name, round(best["pick_odds"], 2)),
                     ("Ничья", round(best["draw_odds"], 2))],
            "in_band": in_band,
            "note": (
                f"Двойной шанс собирается на {best['book']}: делим ставку между "
                f"«{name}» ({best['pick_odds']:.2f}) и ничьей ({best['draw_odds']:.2f}) "
                f"обратно пропорционально коэффициентам — получается "
                f"{best['price']:.2f} на то, что мы не проиграем."
                + ("" if in_band else " Коридор 1.70–2.50 не выдержан, "
                                       "так что выигрыш здесь скромный.")
            ),
        }

    hint = _handicap_hint(sport_key)
    if hint:
        return {
            "market": "handicap",
            "pick": f"{name} — {hint}",
            "price": None,
            "book": None,
            "legs": [],
            "in_band": None,
            "note": (
                f"Прямой коэффициент {bet['entry_price']:.2f} высокий, а ничьей в этом "
                f"виде спорта нет, поэтому двойной шанс не собрать. Безопасный вариант — "
                f"{hint} на «{name}»: этот рынок мы не выкупаем, цену нужно посмотреть "
                f"в линии и взять что-то в районе 1.70–2.50."
            ),
        }
    return None


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

        # Which strategy bucket this signal falls into. Both buckets are fed by
        # the same signal stream -- ОПТИМАЛЬНАЯ is simply the subset priced at
        # or below the cut-off -- so comparing their win rates later actually
        # answers "is skipping the long shots worth it", rather than comparing
        # two unrelated sets of bets.
        strategy = ("optimal" if has_entry and bet["entry_price"] <= OPTIMAL_MAX_PRICE
                    else "aggressive")
        safe = _safe_variant(bet, by_book, sample.get("sport_key")) if has_entry else None
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
            "strategy": strategy,
            "safe": safe,
            "verdict": _verdict(bet, has_entry, safe),
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


def _verdict(bet, has_entry, safe=None) -> str:
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

    if safe and safe.get("note"):
        parts.append("Безопасный вариант: " + safe["note"])
    return " ".join(parts)
