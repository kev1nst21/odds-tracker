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
    ENTRY_MIN_CAPTURE_PCT,
    ENTRY_MAX_OVER_OLD_PCT,
    MIN_MARKET_BOOKS,
    MIN_MOVED_BOOKS,
    OUTLIER_MAX_DEVIATION_PCT,
)


def _median(values):
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _drop_outliers(prices_by_book: dict) -> dict:
    """Throw away quotes that are too far from the rest of the market.

    Bookmakers disagree by 10-20%. A quote 70% away from every other book is
    not a disagreement, it is a stale or mis-keyed line -- and those are
    exactly what produced the "был 3.20, просел до 1.73" nonsense: one book
    carrying a wrong number, then correcting it, looked identical to money
    arriving. With fewer than three quotes there is no market to compare
    against, so nothing is trimmed.
    """
    if len(prices_by_book) < 3:
        return prices_by_book
    med = _median(prices_by_book.values())
    if not med:
        return prices_by_book
    limit = OUTLIER_MAX_DEVIATION_PCT / 100.0
    return {b: p for b, p in prices_by_book.items() if abs(p - med) / med <= limit}

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
    # Tennis is fixed to the SET handicap by user decision (2026-07-30):
    # "по теннису ставим фору по сетам всегда если большие кофы". Unlike a
    # games handicap this one is also settleable from the score The Odds API
    # already returns for tennis -- sets won -- so these entries can be counted
    # in the win rate instead of sitting outside the statistics forever.
    ("tennis_", "фора по сетам +1.5 (наш игрок берёт хотя бы один сет)"),
    ("table_tennis", "фора по сетам +1.5 (взять хотя бы одну партию)"),
    ("esports_", "фора по картам (−1.5 / +1.5)"),
    ("basketball_", "фора по очкам либо фора на четверть"),
    ("icehockey_", "фора по шайбам (−1.5 / +1.5)"),
    ("baseball_", "фора по раннам (−1.5 / +1.5)"),
)

# Every other two-way sport still gets an answer rather than silence -- the
# market always exists, we just can't name its unit generically.
_HANDICAP_DEFAULT = "фора (минусовая на фаворита либо плюсовая на аутсайдера)"


def _handicap_hint(sport_key: str):
    key = (sport_key or "").lower()
    for prefix, hint in _HANDICAP_HINTS:
        if key.startswith(prefix):
            return hint
    return _HANDICAP_DEFAULT


# --- estimating the price of a +1.5 set handicap ---------------------------
# We do not buy the handicap market, but for tennis its price can be DERIVED
# from the match-winner odds we already have, because tennis has a rigid
# structure: a match is won by taking sets, and sets are close enough to
# independent for this purpose.
#
# Take our player's no-vig match probability M and let s be his chance of
# winning any one set. In a best-of-3 he wins the match by taking two sets:
#
#     M = s^2 + 2*s^2*(1-s) = s^2 * (3 - 2s)
#
# and in a best-of-5 by taking three:
#
#     M = s^3 * (1 + 3*(1-s) + 6*(1-s)^2)
#
# Both are strictly increasing in s, so s is recovered by bisection. Then
# "+1.5 sets" -- our player takes at least one set -- is simply the complement
# of being whitewashed:
#
#     best-of-3:  P = 1 - (1-s)^2        best-of-5:  P = 1 - (1-s)^3
#
# and the fair decimal price is 1/P. Worked example, an underdog at 4.60
# against 1.20: M = 0.207, s = 0.293, P = 0.50, price = 2.00.
#
# This is an ESTIMATE and is labelled as one everywhere it appears. A real
# bookmaker adds margin, so expect 5-10% less on screen. It never enters the
# profit maths -- only the win rate, which needs no price.
_SLAMS = ("wimbledon", "us_open", "french_open", "australian_open", "roland")


def _best_of(sport_key: str) -> int:
    """Men's Grand Slam singles are best-of-5; everything else here is
    best-of-3. Women play best-of-3 even at the slams, hence the atp check."""
    key = (sport_key or "").lower()
    if "atp" in key and any(slam in key for slam in _SLAMS):
        return 5
    return 3


def _match_prob_to_set_prob(m: float, best_of: int = 3) -> float:
    """Invert the match-win formula by bisection."""
    if not (0 < m < 1):
        return None

    def match_prob(s):
        if best_of == 5:
            return s ** 3 * (1 + 3 * (1 - s) + 6 * (1 - s) ** 2)
        return s ** 2 * (3 - 2 * s)

    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if match_prob(mid) < m:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def _set_handicap_price(pick_odds: float, other_odds: float, sport_key: str):
    """Fair decimal price for '+1.5 sets' on our pick, or None if the two
    moneyline prices don't give us a usable probability."""
    if not pick_odds or not other_odds or pick_odds <= 1 or other_odds <= 1:
        return None
    # Strip the bookmaker's margin the simple way: normalise the two implied
    # probabilities so they sum to one.
    ours = (1 / pick_odds) / ((1 / pick_odds) + (1 / other_odds))
    bo = _best_of(sport_key)
    s = _match_prob_to_set_prob(ours, bo)
    if s is None:
        return None
    p_at_least_one = 1 - (1 - s) ** (2 if bo == 3 else 3)
    if p_at_least_one <= 0.01:
        return None
    return round(1 / p_at_least_one, 2)


# Typical two-way bookmaker margin. Only used by the fallback below, where the
# opponent's price isn't available and the vig has to be assumed rather than
# measured. 5% is the usual figure for a mainstream tennis moneyline.
ASSUMED_TWO_WAY_MARGIN = 0.05


def set_handicap_price_from_one(pick_odds: float, sport_key: str = ""):
    """Estimate '+1.5 sets' from OUR price alone.

    The proper version (_set_handicap_price) normalises both sides to strip the
    bookmaker's margin exactly. This one exists for rows logged before that
    calculation shipped, where only our own price was stored -- it assumes a
    typical margin instead of measuring it. Less precise, still far better than
    printing "по линии" and leaving the reader to guess.
    """
    if not pick_odds or pick_odds <= 1:
        return None
    ours = (1.0 / pick_odds) / (1.0 + ASSUMED_TWO_WAY_MARGIN)
    bo = _best_of(sport_key)
    s = _match_prob_to_set_prob(ours, bo)
    if s is None:
        return None
    p = 1 - (1 - s) ** (2 if bo == 3 else 3)
    return round(1 / p, 2) if p > 0.01 else None


def _safe_variant(bet: dict, by_book: dict, sport_key: str, trigger: float = None):
    """A lower-risk way to back the same opinion when the straight price is
    high (user decision, 2026-07-29: above 3.5 offer a "безопасный" variant
    landing around 1.7-2.5).

    Gradeability differs by sport and that difference is load-bearing. The
    football double chance below is computed from prices we already hold AND
    can be settled from the final score -- our side won or it ended level.
    A handicap cannot: we know neither the line nor the price, so it is
    returned as an instruction and marked ungradeable, and nothing ungradeable
    is ever allowed into a win rate.

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
    trigger = SAFE_TRIGGER_PRICE if trigger is None else trigger
    if bet["entry_price"] <= trigger:
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
            "gradeable": True,
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
        key = (sport_key or "").lower()
        # For tennis the price is not a guess -- it follows from the match
        # odds, see _set_handicap_price above. Median across books so one
        # stale quote can't move it.
        est = None
        if key.startswith("tennis_"):
            other = "away" if side == "home" else "home"
            est = _set_handicap_price(
                _median([m.get(side) for m in by_book.values() if m.get(side)]),
                _median([m.get(other) for m in by_book.values() if m.get(other)]),
                sport_key,
            )
            # The number deliberately does NOT go into this string. It used to
            # ("фора по сетам +1.5 ≈ 1.60 (взять хотя бы один сет)"), and every
            # display site truncates the label to fit a table cell -- which cut
            # straight through the price and showed the coefficient as "1".
            # One number, one place: est_price below, rendered on its own.
            if est:
                hint = "фора по сетам +1.5 (взять хотя бы один сет)"
        # A +1.5 SET handicap is the one handicap we can settle without buying
        # the market: it wins whenever our player takes at least one set, and
        # the score endpoint already reports sets for tennis. So it counts
        # towards the win rate -- but not towards profit, because we still do
        # not know the price, and inventing one would be worse than a gap.
        set_handicap = key.startswith("tennis_") or key.startswith("table_tennis")
        return {
            "market": "set_handicap" if set_handicap else "handicap",
            "pick": f"{name} — {hint}",
            "price": None,
            "book": None,
            "legs": [],
            "in_band": None,
            "gradeable": set_handicap,
            # Derived from the moneyline, NOT quoted by anybody. Shown with a
            # "~" everywhere and deliberately kept out of the profit maths --
            # a win rate needs no price, a P&L does, and we don't have a real
            # one. A bookmaker's own line is usually 5-10% worse than this.
            "est_price": est,
            "note": (
                f"Прямой коэффициент {bet['entry_price']:.2f} высокий, а ничьей в этом "
                f"виде спорта нет, поэтому двойной шанс не собрать. Безопасный вариант — "
                f"{hint} на «{name}»: этот рынок мы не выкупаем, цену нужно посмотреть "
                f"в линии и взять что-то в районе 1.70–2.50."
            ),
        }
    return None


def _optimal_play(bet: dict, safe: dict):
    """What the ОПТИМАЛЬНАЯ strategy actually does with this signal.

    The first version simply threw away every signal priced above the cut-off,
    which is not what the strategy is for. The user's rule (2026-07-30) is that
    the optimal line does not skip the event, it enters it more softly:

        "если это футбол к примеру и у нас победа коф 3.5 и он не проходит по
         критериям на победу, то мы ставим соответственно с форой и иксом...
         в теннисе так же с форой, в баскетболе, киберспорте тоже фора"

    So there are three outcomes here, and exactly one of them is a skip:

      * price at or below the cut-off -> back the straight pick, same as the
        aggressive line. Gradeable.
      * above it, football -> back the double chance instead. Gradeable, and
        priced from data we already hold.
      * above it, no draw in this sport -> name the handicap. NOT gradeable,
        so it is published as a recommendation and never counted in the win
        rate. Inventing a settled result for a bet whose line and price we
        never knew would be the single fastest way to make this statistic
        worthless.
    """
    if not bet or not bet.get("entry_price"):
        return None

    if bet["entry_price"] <= OPTIMAL_MAX_PRICE:
        return {
            "kind": "straight",
            "pick": bet["name"],
            "price": bet["entry_price"],
            "book": bet["entry_book"],
            "gradeable": True,
            "note": f"Коэффициент {bet['entry_price']:.2f} в пределах "
                    f"{OPTIMAL_MAX_PRICE:g} — берём прямую победу.",
        }

    if not safe:
        return None

    return {
        "kind": safe["market"],
        "pick": safe["pick"],
        "price": safe.get("price"),
        "est_price": safe.get("est_price"),
        "book": safe.get("book"),
        "gradeable": bool(safe.get("gradeable")),
        "note": safe.get("note"),
    }


# Filled in by every call to build_event_summaries(). Answers the question
# that matters most when the bot is quiet: of everything the market did this
# cycle, where exactly did it stop being a signal? Without this the only
# visible fact is "0 сигналов", which is indistinguishable from a broken
# pipeline -- and guessing which filter is too strict is how you end up
# loosening the wrong one.
LAST_FUNNEL = {}


def build_event_summaries(records: list, spikes: list = None, movements: list = None) -> list:
    """One dict per event, most actionable first."""
    funnel = {"events": 0, "with_drop": 0, "big_drop": 0,
              "thin_market": 0, "all_books_moved": 0, "entry_too_low": 0,
              "price_out_of_range": 0, "signals": 0}
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
            prices_by_book = _drop_outliers(
                {r["bookmaker"].lower(): float(r["price"]) for r in side_rows}
            )
            if not prices_by_book:
                continue
            prices = list(prices_by_book.values())
            # Breadth is this product's entire confidence signal, and breadth
            # needs a crowd. Two bookmakers cannot form a consensus for a third
            # to lag behind, so a "move" there is unreadable -- see
            # MIN_MARKET_BOOKS in config.py for the case that proved it.
            thin_market = len(prices) < MIN_MARKET_BOOKS

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
            entry_floor = best_left = None
            left_count = 0
            entries = []
            if dropped:
                # MEDIANS, not max-of-old and min-of-new.
                #
                # The old code took the highest pre-drop price across every
                # book that moved and the lowest post-drop price across every
                # book that moved -- two numbers that generally come from two
                # DIFFERENT bookmakers. That manufactures a drop nobody
                # experienced: one book carrying a stale 3.20 alongside a
                # market at 1.90 turned an ordinary correction into a
                # headline "-46%". Medians describe what actually happened to
                # a typical bookmaker, and one broken quote cannot move them.
                old_price = _median([m["prev_price"] for m in dropped])
                new_price = _median([m["price"] for m in dropped])
                drop_pct = _median([m["pct_change"] for m in dropped]) * 100

                # The entry has to be worth taking, which means two things.
                # It must beat where the market went by more than rounding
                # (ENTRY_MIN_GAP_PCT), AND it must give back a real share of
                # the move (ENTRY_MIN_CAPTURE_PCT) -- otherwise we would be
                # announcing "был 3.20" and sending you to bet 1.87. It also
                # must not sit far above the pre-drop price: a book offering
                # more than the market ever showed is broken, not slow.
                floor = max(
                    new_price * (1 + ENTRY_MIN_GAP_PCT / 100.0),
                    new_price + (old_price - new_price) * (ENTRY_MIN_CAPTURE_PCT / 100.0),
                )
                ceiling = old_price * (1 + ENTRY_MAX_OVER_OLD_PCT / 100.0)
                left = {b: p for b, p in prices_by_book.items() if b not in down_books}
                entries = sorted(
                    ((b, p) for b, p in left.items() if floor <= p <= ceiling),
                    key=lambda bp: -bp[1],
                )
                # Kept so the funnel below can say WHY an entry was refused
                # rather than just that there wasn't one.
                entry_floor, left_count = floor, len(left)
                best_left = max(left.values()) if left else None

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
                "thin_market": thin_market,
                # Breadth, asked properly: did several independent books move
                # the same way, or is this one trader? A sharp book moving
                # alone counts -- Pinnacle shortening a price is the reference
                # the rest of the market follows, not a typo.
                "well_evidenced": bool(len(down_books) >= MIN_MOVED_BOOKS or sharp_down),
                "entries": entries[:3],
                "entry_price": entries[0][1] if entries else None,
                "entry_book": entries[0][0] if entries else None,
                "entry_gap_pct": ((entries[0][1] / new_price - 1) * 100)
                                 if entries and new_price else None,
                "entry_floor": entry_floor,
                "left_count": left_count,
                "best_left_price": best_left,
            })

        outcomes.sort(key=lambda o: _SIDE_ORDER.get(o["side"], 9))
        # The draw is dropped only AFTER the fair-price maths above, which needs
        # the full 3-way market to measure the bookmaker's margin correctly.
        if EXCLUDE_DRAW:
            outcomes = [o for o in outcomes if o["side"] != "draw"]

        # The bet is on the side money went INTO. Never on the side that merely
        # drifted up as a mechanical consequence -- see the module docstring.
        #
        # 2026-08-01: thin markets are no longer excluded here. They used to
        # be, and the effect was that a drop in a small league produced no
        # summary at all -- so it never reached the movements log either, and
        # the "Движения" page sat at zero while the funnel counted six. Weak
        # evidence now blocks the ALERT (see well_evidenced below), not the
        # record of what happened. We log everything we see and are honest
        # about which ones we would actually bet.
        shortening = [o for o in outcomes if o["down_count"] > 0]
        bet = None
        if shortening:
            bet = max(shortening, key=lambda o: (o["spiked"], o["down_count"],
                                                 -(o["drop_pct"] or 0)))

        # --- funnel bookkeeping -------------------------------------------
        funnel["events"] += 1
        dropping = [o for o in outcomes if o["down_count"] > 0]
        if dropping:
            funnel["with_drop"] += 1
        big = [o for o in dropping
               if abs(o["drop_pct"] or 0) >= SPIKE_THRESHOLD_PCT * 100]
        if big:
            funnel["big_drop"] += 1
            lead = max(big, key=lambda o: abs(o["drop_pct"] or 0))
            if not lead["well_evidenced"]:
                # Key name kept so the stored funnel history stays comparable
                # across this change; the label on the site is what the reader
                # sees, and that now says what this bucket really is.
                funnel["thin_market"] += 1
            elif lead["left_count"] == 0:
                funnel["all_books_moved"] += 1
            elif not lead["entries"]:
                funnel["entry_too_low"] += 1
            else:
                funnel["signals"] += 1

        has_entry = bool(bet and bet["entry_price"])
        stars = bet["stars"] if bet else 0

        # The safe variant is computed from OPTIMAL_MAX_PRICE upwards, not from
        # SAFE_TRIGGER_PRICE, because the optimal line needs one as soon as the
        # straight price stops qualifying -- a 3.00 pick has no safe
        # alternative to offer otherwise. Whether it is also SHOWN as a
        # "безопасный вариант" on the card still follows the 3.5 threshold.
        safe = (_safe_variant(bet, by_book, sample.get("sport_key"),
                              trigger=min(OPTIMAL_MAX_PRICE, SAFE_TRIGGER_PRICE))
                if has_entry else None)
        optimal = _optimal_play(bet, safe)
        # Both strategies are fed by the SAME signal stream, so the comparison
        # answers "does entering softly beat entering straight" rather than
        # comparing two unrelated sets of bets. A signal only falls out of the
        # optimal line when there is no softer way in at all.
        strategy = "optimal" if optimal else "aggressive"
        # Only a drop of at least the alert threshold is worth a notification.
        # Sub-threshold drift still shows on the site but must never reach the
        # bot -- the user explicitly does not want small moves pushed to them.
        big_enough = bool(bet and abs(bet["drop_pct"] or 0) >= SPIKE_THRESHOLD_PCT * 100)
        # One book moving is not a market move, whatever the size of it. This
        # is the guard that used to be MIN_MARKET_BOOKS, asked about the thing
        # that actually matters. It gates the alert only: the movement is
        # still recorded and still shown.
        well_evidenced = bool(bet and bet["well_evidenced"])

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
            "alertable": big_enough and has_entry and well_evidenced,
            "well_evidenced": well_evidenced,
            "stars": stars,
            "strategy": strategy,
            "safe": safe if (has_entry and bet["entry_price"] > SAFE_TRIGGER_PRICE) else None,
            "optimal": optimal,
            "verdict": _verdict(bet, has_entry, safe, optimal),
        })

    LAST_FUNNEL.clear()
    LAST_FUNNEL.update(funnel)

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


def _verdict(bet, has_entry, safe=None, optimal=None) -> str:
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

    if optimal and optimal["kind"] != "straight":
        if optimal.get("price"):
            parts.append(f"Оптимальная стратегия входит через «{optimal['pick']}» "
                         f"за {optimal['price']:.2f}.")
        else:
            parts.append(f"Оптимальная стратегия входит через «{optimal['pick']}» — "
                         f"цену смотреть в линии, в статистику она не попадёт, "
                         f"потому что проверить её по счёту невозможно.")
    return " ".join(parts)
