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

SPIKE_THRESHOLD_PCT = float(os.getenv("SPIKE_THRESHOLD_PCT", "0.08"))

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

# Purely geographic split -- separate axis from "sharp vs public" above.
# Bookmakers not listed default to "europe" (see get_region()).
BOOKMAKER_REGIONS = {
    "pinnacle": "asia",
    "onexbet": "asia",
    "betonlineag": "us", "betmgm": "us", "betrivers": "us", "betus": "us",
    "bovada": "us", "williamhill_us": "us", "draftkings": "us", "fanatics": "us",
    "fanduel": "us", "lowvig": "us", "mybookieag": "us",
}
REGION_LABELS = {"asia": "🌏 Азия", "europe": "🇪🇺 Европа", "us": "🇺🇸 США"}


def get_region(bookmaker: str) -> str:
    return BOOKMAKER_REGIONS.get(bookmaker.lower(), "europe")


DB_PATH = os.path.join(os.path.dirname(__file__), "data", "odds_history_v2.db")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
