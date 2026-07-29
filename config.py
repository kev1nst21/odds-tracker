"""Central configuration for the odds-movement tracker.

Provider: The Odds API (the-odds-api.com), switched from OddsPapi on 2026-07-29
after OddsPapi's free plan turned out to be capped at 250 requests/month total
(confirmed live via GET /v4/account) -- nowhere near enough for real polling.
The Odds API bills differently: 1 request = 1 sport key, and returns EVERY
bookmaker for that sport in a single call, so cost = markets x regions per
call instead of 1-bookmaker-per-call. Confirmed live 2026-07-29 that the paid
$30/mo "20K" tier includes "All bookmakers" (Pinnacle and 1xBet included) --
bookmaker access is NOT gated by plan tier, only the monthly credit total is.
"""
import os
from dotenv import load_dotenv

load_dotenv()

THEODDSAPI_KEY = os.getenv("THEODDSAPI_KEY", "")
THEODDSAPI_BASE_URL = "https://api.the-odds-api.com"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SPIKE_THRESHOLD_PCT = float(os.getenv("SPIKE_THRESHOLD_PCT", "0.10"))

# A line drifting by at least this much counts as "this bookmaker moved too"
# when scoring how broad a move is (see analytics._stars). Deliberately far
# below SPIKE_THRESHOLD_PCT: the point is not whether each book spiked, it's
# how many books agree on the direction. Rounding jitter is under 1%.
MIN_DRIFT_PCT = float(os.getenv("MIN_DRIFT_PCT", "0.01"))

# "Super alert": two (or more) same-direction spikes on the exact same line
# within this many minutes get flagged as a cascade -- e.g. price drops 5%,
# then drops another 5% within half an hour. Usually means the move isn't
# noise, it's a real developing situation (injury news, lineup leak, etc.).
CASCADE_WINDOW_MINUTES = int(os.getenv("CASCADE_WINDOW_MINUTES", "30"))

# How long after a match's scheduled start we wait before trying to look up
# its result and score our alerts against it (matches can run long / start late).
RESULT_CHECK_DELAY_HOURS = int(os.getenv("RESULT_CHECK_DELAY_HOURS", "3"))

# GET /v4/sports/{sport}/scores/ costs quota too (1-2 credits per sport per
# call), so results.py doesn't check on every single poll cycle -- only once
# per this many hours (tracked via storage's meta table). Keeps the
# quota budget dominated by the odds-polling cadence, not results-checking.
RESULTS_CHECK_INTERVAL_HOURS = int(os.getenv("RESULTS_CHECK_INTERVAL_HOURS", "3"))

# Region parameter for The Odds API -- determines which bookmakers come back.
# Confirmed live (2026-07-29) via the bookmakers-by-region page: Pinnacle and
# 1xBet (our two Asian/sharp reference books) both live under the "eu" region
# key on this API. SBOBET, Singbet and Maxbet (available on OddsPapi) are NOT
# covered by The Odds API at all -- a real coverage loss, but Pinnacle is
# still the single most important sharp reference book, so this is an
# acceptable trade for going from a 250/month quota to a usable one.
REGIONS = os.getenv("THEODDSAPI_REGIONS", "eu")

# h2h = moneyline/1X2. Extra markets (spreads, totals) each multiply the
# per-call quota cost by the number of regions -- keep to h2h only to stay
# inside the $30/mo "20K credits" plan at a reasonable polling frequency.
# See README.md for the quota math.
MARKETS = os.getenv("THEODDSAPI_MARKETS", "h2h")

# Soccer leagues to track, matched by exact sport key from GET /v4/sports.
# NOTE: The Odds API's esports coverage is confirmed live (2026-07-29, full
# GET /v4/sports listing) to be NON-EXISTENT -- no CS2, League of Legends or
# Dota 2 keys anywhere. That's a real scope cut vs. the original OddsPapi
# setup; cybersport tracking is not possible on this provider. Documented
# here as a known gap, not a TODO -- there's no fix short of adding a second,
# esports-specific provider on top of this one.
SOCCER_LEAGUE_KEYS = [
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_uefa_champs_league_qualification",
]

# Tennis has no single stable "ATP tour" key -- confirmed live (2026-07-29)
# that /v4/sports only lists whichever tournament is currently in season
# (e.g. "tennis_atp_washington_open" in July). main.py fetches the live
# /v4/sports list each run (that call is free, no quota cost) and includes
# every sport whose group is "Tennis" rather than hardcoding a tournament key
# that would go stale the moment the tournament ends.
TENNIS_GROUP = "Tennis"

# Asian / sharp bookmakers get checked first and weighted higher in alerts,
# since line moves there tend to reflect informed money rather than public
# money. Bookmaker keys per The Odds API's own naming (confirmed live
# 2026-07-29): Pinnacle is "pinnacle", 1xBet is "onexbet".
ASIAN_SHARP_BOOKMAKERS = [
    "pinnacle",
    "onexbet",
]

# Betting EXCHANGES, not bookmakers. Their prices come from whatever one
# random user happened to post, so a thin market swings wildly for reasons that
# have nothing to do with information -- confirmed live 2026-07-29, betfair_ex_eu
# showed a tennis line going 2.28 -> 9.20 (+303%) in a single 30-minute window
# while every real bookmaker barely moved. Left out of signal generation
# entirely (they'd drown the alerts in noise); still stored in history.
EXCHANGE_BOOKMAKERS = ["betfair_ex_eu", "betfair_ex_uk", "betfair_ex_au", "matchbook", "smarkets"]

# Long-shot outcomes (e.g. 26.00) move several percent on rounding alone, so an
# 8% "spike" there is meaningless. Lines priced above this are ignored for
# signals -- nobody is acting on informed money at 30-to-1 anyway.
MAX_SIGNAL_PRICE = float(os.getenv("MAX_SIGNAL_PRICE", "12.0"))

# A decimal price at or below this is not a real market -- 1.00 pays nothing
# back, and anything under ~1.05 is a settled or suspended line rather than a
# quote you could take. Confirmed live 2026-07-29: a tennis line showed
# "просел до 1.00", and because detector and analytics applied DIFFERENT lower
# bounds, the same event reported "просело у 8 из 4 контор" -- more books moving
# than were quoting. Both modules now filter on this one constant so the two
# counts can never disagree again.
MIN_SIGNAL_PRICE = float(os.getenv("MIN_SIGNAL_PRICE", "1.05"))

# Drop events that have already kicked off. The odds endpoint keeps returning
# in-play matches, and their prices move on what is happening ON THE PITCH --
# a goal or a break of serve repositions the line instantly, which has nothing
# to do with money arriving before the event. Confirmed live 2026-07-29: an
# already-started tennis match showed "просел до 1.00" simply because it was
# nearly decided. Those are not signals and are excluded everywhere.
PREMATCH_ONLY = os.getenv("PREMATCH_ONLY", "1") not in ("0", "false", "False")

# Small cushion so a match starting in the next minute or two -- where the
# market is already effectively live -- doesn't sneak through.
PREMATCH_BUFFER_MINUTES = int(os.getenv("PREMATCH_BUFFER_MINUTES", "3"))

# How much better than the computed fair (no-vig) price a bookmaker has to be
# before the analyst calls it value, in percent. Below this the edge is inside
# the model's own error bars.
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "2.0"))

# A bookmaker counts as "hasn't moved yet" -- i.e. still worth betting into --
# only if its price is at least this much above where the books that DID move
# have settled. Anything tighter is not a real entry, just rounding.
ENTRY_MIN_GAP_PCT = float(os.getenv("ENTRY_MIN_GAP_PCT", "3.0"))

# The draw is never bet (user decision, 2026-07-29) -- only the two match
# winners are actionable. Draw prices are still FETCHED and still feed the
# no-vig fair-price calculation, because removing a leg from a 3-way market
# would make the margin maths wrong; they are only hidden from the cards and
# excluded from being picked as a bet.
EXCLUDE_DRAW = os.getenv("EXCLUDE_DRAW", "1") not in ("0", "false", "False")


# Purely informational -- how often .github/workflows/poll.yml is scheduled to
# run. Nothing enforces this in code; it's shown on the dashboard so a reader
# can tell how fresh the data is and when the next refresh is due. Keep in
# sync with the cron expression in poll.yml.
POLL_INTERVAL_MINUTES = int(os.getenv("POLL_INTERVAL_MINUTES", "30"))

# Public URL of the dashboard, linked at the bottom of each Telegram digest.
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://kev1nst21.github.io/odds-tracker/")

# Flat stake used for the "what would the balance be" line on the dashboard.
# A flat stake is the only honest way to present this: varying the stake would
# let the number be tuned after the fact. $200 is a size most bookmakers accept
# without cutting limits.
FLAT_STAKE = float(os.getenv("FLAT_STAKE", "200"))

# v3 (2026-07-29): clean slate. The earlier database held alerts recorded under
# the old per-line logic, where the stored "alert price" was whatever the book
# that spiked was showing -- not the price we would actually have bet at. Those
# rows can't be re-scored meaningfully, so statistics start fresh from here and
# every alert now records was-price, dropped-to price and the entry price we
# recommended.
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "odds_history_v3.db")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
