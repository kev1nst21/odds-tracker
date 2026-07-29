"""Central configuration for the odds-movement tracker."""
import os
from dotenv import load_dotenv

load_dotenv()

ODDSPAPI_KEY = os.getenv("ODDSPAPI_KEY", "")
ODDSPAPI_BASE_URL = "https://api.oddspapi.io"

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

SPIKE_THRESHOLD_PCT = float(os.getenv("SPIKE_THRESHOLD_PCT", "0.08"))

# Sport IDs on OddsPapi -- confirmed live via GET /v4/sports on 2026-07-29
SPORTS = {
    "football": 10,  # slug: soccer
    "tennis": 12,
    "cs2": 17,       # slug: esport-counter-strike
    "lol": 18,       # slug: esport-league-of-legends
    "dota2": 16,     # slug: esport-dota
}

# Tournament IDs -- pulled live from GET /v4/tournaments?sportId=... on 2026-07-29.
# Extend/trim freely; run odds_client.list_tournaments(SPORTS["<sport>"]) to see more.
TOURNAMENT_IDS = {
    "football": [17, 8, 23, 35, 34, 7],       # Premier League, La Liga, Serie A, Bundesliga, Ligue 1, Champions League
    "tennis": [591, 2567, 2579, 2100],        # Wimbledon, Australian Open (MS), French Open (MS), Davis Cup
    "cs2": [2392, 2414, 16634],               # ESL Pro League, IEM, PGL Major
    "lol": [2450, 2452, 2454, 15490, 2549],   # LCS, LEC, LCK, LPL, Worlds
    "dota2": [13911, 24405, 28029],           # The International, ESL One Birmingham, PGL Bucharest Major
}
ALL_TOURNAMENT_IDS = [tid for ids in TOURNAMENT_IDS.values() for tid in ids]

# Asian / sharp bookmakers get checked first and weighted higher in alerts,
# since line moves there tend to reflect informed money rather than public money.
# Verified against the live OddsPapi bookmaker list on 2026-07-29 -- "crown" isn't
# a valid slug there, swapped for "maxbet" (IBCBET's rebrand, same Asian handicap desk).
ASIAN_SHARP_BOOKMAKERS = [
    "pinnacle",
    "sbobet",
    "singbet",
    "ibcbet",
    "maxbet",
    "1xbet",
]

# Softer / recreational books, tracked for comparison against the sharp side.
# All confirmed valid slugs on OddsPapi's own bookmaker list (2026-07-29) --
# one paid-free API key already covers all of these, no extra accounts needed.
PUBLIC_BOOKMAKERS = [
    "bet365", "williamhill", "unibet", "bwin", "betfair-ex", "betway",
    "ladbrokes", "coral", "betvictor", "sportingbet", "888sport",
    "betfred", "paddypower", "skybet", "tipico", "interwetten",
    "betsson", "marathonbet", "stake", "draftkings", "fanduel",
    "betmgm", "caesars", "dafabet",
]

ALL_BOOKMAKERS = ASIAN_SHARP_BOOKMAKERS + PUBLIC_BOOKMAKERS

# Purely geographic split -- separate axis from "sharp vs public" above.
# Sharp/public groups by how informed the money is; this groups by where the
# bookmaker's core market actually is, so the dashboard can show a clean
# "Asia vs Europe" view on request. Bookmakers not listed default to "europe".
BOOKMAKER_REGIONS = {
    "pinnacle": "asia", "sbobet": "asia", "singbet": "asia", "ibcbet": "asia",
    "maxbet": "asia", "1xbet": "asia", "dafabet": "asia",
    "stake": "us", "draftkings": "us", "fanduel": "us", "betmgm": "us", "caesars": "us",
}
REGION_LABELS = {"asia": "🌏 Азия", "europe": "🇪🇺 Европа", "us": "🇺🇸 США"}


def get_region(bookmaker: str) -> str:
    return BOOKMAKER_REGIONS.get(bookmaker.lower(), "europe")


DB_PATH = os.path.join(os.path.dirname(__file__), "data", "odds_history.db")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
