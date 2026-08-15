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

# --- Second provider: OddsPapi, esports + table tennis only ---
# The Odds API carries no esports whatsoever (verified live 2026-07-29 against
# its full 174-sport listing), so those lines come from a separate paid plan
# here: 4 bookmakers x 4 sports, 100,000 requests/month.
ODDSPAPI_KEY = os.getenv("ODDSPAPI_KEY", "")
ODDSPAPI_BASE_URL = "https://api.oddspapi.io"

# sportId -> the sport_key we store it under. Ids confirmed live via
# GET /v4/sports on 2026-07-29.
ODDSPAPI_SPORTS = {
    17: "esports_cs2",
    16: "esports_dota2",
    18: "esports_lol",
    25: "table_tennis",
}

# Exactly the books the subscription covers -- asking for others just wastes
# requests and returns nothing.
ODDSPAPI_BOOKMAKERS = ["1xbet", "22bet", "188bet", "betway"]

# The participants and tournaments lookups change slowly, so they're cached in
# SQLite between runs and only refreshed this often. Without this, eight extra
# calls per cycle would burn roughly 12,000 requests a month for nothing.
ODDSPAPI_LOOKUP_TTL_HOURS = int(os.getenv("ODDSPAPI_LOOKUP_TTL_HOURS", "6"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Minimum drop that counts as a signal.
#
# 2026-07-31: lowered from 0.10 to 0.08 by user decision. Measured over the
# preceding 23 hours the market produced only 19 drops of 10%+ across 146
# events, and the entry rules then cut that to a single signal -- too thin to
# learn anything from. 8% widens the top of the funnel without touching the
# rules that decide whether an entry is real, which are the ones that actually
# protect quality.
# 2026-08-01, back to 0.10 by user decision, now that signals actually flow:
# "поднимаем снова процент до 10 процентов минимум чтобы нас тригерило, меньше
# вообще не рассматриваем". The 8% experiment did its job -- it widened the
# top of the funnel while we were debugging why nothing came through. The real
# blocker turned out to be the breadth rule, not the threshold, so the
# threshold can go back to where a move is genuinely worth acting on.
SPIKE_THRESHOLD_PCT = float(os.getenv("SPIKE_THRESHOLD_PCT", "0.10"))

# How far back a price has to be compared against.
#
# 2026-07-31, and this is the single most important number in the project.
# The detector used to diff each line against the IMMEDIATELY PRECEDING
# snapshot, which silently tied the sensitivity of the whole tool to the
# polling cadence: at a 3-minute cadence an alert required an 8% move inside
# three minutes, which almost never happens. Production proved it -- one cycle
# saw 113 events, 3,690 quotes and "просело 0": not a single line had moved
# even 1% since the poll five minutes earlier. Polling FASTER was making the
# tool blinder, which is the exact opposite of the intention.
#
# Now every line is compared against its price this many minutes ago. Cadence
# becomes what it should always have been -- how EARLY we notice a move, not
# whether we notice it at all.
BASELINE_WINDOW_MINUTES = int(os.getenv("BASELINE_WINDOW_MINUTES", "60"))

# Safety rail on the above. If the only price we hold for a line is much older
# than the window (a league we poll rarely, or one that vanished for a while),
# comparing against it would report slow multi-day drift as a sudden move.
# Anything older than this is not a baseline, it's history.
#
# 2026-08-09: THIS NUMBER SILENTLY BLINDED THE PRODUCT FOR A DAY, and the way
# it did so is worth writing down because the mistake was structural, not
# arithmetic.
#
# It used to be expressed as a multiplier (window x 3 = 180 minutes), which
# reads like a property of the measurement. It is not. It is a constraint on
# the POLLING SCHEDULE: a line can only be compared if we looked at it again
# within this many minutes. The wide sweep rotates 5 slots per cycle across
# ~40 obscure leagues, so at a 30-minute cadence each of those leagues came
# round every ~4 hours -- comfortably past 180 minutes. Every single wide-list
# line therefore failed the floor, returned no baseline, and was skipped in
# silence. Only the core leagues, polled every cycle, could still be measured.
#
# It was invisible for two reasons. The rotation used to run on a 5-minute
# cadence, where the same walk takes 40 minutes and fits easily; and after the
# cadence moved to 30 minutes the database still held dense history from the
# fast era, so baselines kept resolving. The v5 restart wiped that history and
# the breakage became total: one movement in seventeen hours.
#
# The fix is NOT to pick a bigger number here and hope. Two things now hold it
# together, and both matter:
#
#   * this is a FLOOR, not the whole rule. detector.py stretches it at runtime
#     to at least one full rotation lap plus a cycle, measured from the list
#     the sweep is actually walking (odds_client records it every cycle). A
#     line can then never expire before it comes round again, whatever the
#     list size, cadence or slot split happen to be. The cost is honest and
#     bounded: a rarely-polled league is measured at its own coarser
#     resolution rather than not at all.
#   * the horizon filter below drops leagues with no fixture we could publish,
#     which shortens the lap so the stretch is rarely needed.
BASELINE_MAX_AGE_MINUTES = int(os.getenv("BASELINE_MAX_AGE_MINUTES", "150"))

# Hard ceiling on that stretch. Past this a "move" is slow multi-day drift and
# calling it informed money would be a lie, so we would rather be blind and say
# so in the log than publish a signal we cannot stand behind.
BASELINE_ABSOLUTE_MAX_MINUTES = int(os.getenv("BASELINE_ABSOLUTE_MAX_MINUTES", "240"))

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

# Live score for matches already in play, shown next to "матч идёт" on the
# site and in the bot. Each sport asked about costs one credit per cycle, so
# this is capped: only sports we actually hold an in-play position in get
# asked, and never more than this many at once. On a quiet night it spends
# nothing; on a busy one, a couple of credits rather than the whole card.
LIVE_SCORE_MAX_SPORTS = int(os.getenv("LIVE_SCORE_MAX_SPORTS", "2"))

# After this long, no match in any sport we track is still being played, so
# the site stops calling it "матч идёт" and says it is over and waiting to be
# graded. Covers the longest realistic case -- a five-set tennis match with
# rain delays -- rather than the average one, because being early with
# "завершён" would be the same class of lie in the other direction.
MATCH_MAX_DURATION_HOURS = float(os.getenv("MATCH_MAX_DURATION_HOURS", "4"))

# Region parameter for The Odds API -- determines which bookmakers come back.
# Confirmed live (2026-07-29) via the bookmakers-by-region page: Pinnacle and
# 1xBet (our two Asian/sharp reference books) both live under the "eu" region
# key on this API. SBOBET, Singbet and Maxbet (available on OddsPapi) are NOT
# covered by The Odds API at all -- a real coverage loss, but Pinnacle is
# still the single most important sharp reference book, so this is an
# acceptable trade for going from a 250/month quota to a usable one.
#
# 2026-08-15. Region is the ONLY lever that adds bookmakers, and the funnel
# says bookmakers are now the binding constraint: of the ten movements that
# failed to become signals in 24 hours, four died as "просело у всех" (no book
# left holding the old price) and three as "вход не дотянул" (the best
# remaining price was below the entry rule). Both are shortages of books, not
# of movement. More books means more laggards, and a laggard IS the bet.
#
# What each region is worth here, from the published bookmaker list
# (checked 2026-08-15). Cost is multiplicative: every region multiplies the
# bill for EVERY sport key polled, so this is the expensive lever.
#   eu  ~25 books, incl. Pinnacle, 1xBet, Marathon Bet, Betsson, Unibet -- the
#       sharp reference books live here. Non-negotiable, always first.
#   uk  ~20 books, of which ~15 exist nowhere else: Sky Bet, Paddy Power,
#       Coral, Ladbrokes, William Hill, Betfred, BoyleSports, Bet Victor.
#       Serious soccer and tennis books. Best value after eu.
#   au  ~13 books and the only place BET365 appears -- the largest book in the
#       world by turnover, and absent from eu entirely. Also SportsBet, Neds,
#       TAB. Worth a region on bet365 alone.
#   us  ~17 books (DraftKings, FanDuel, BetMGM, Caesars). US-shaped: deep on
#       US sports, thinner and slower on European soccer. Buy last.
#   se, fr  heavy overlap with eu (Betsson, NordicBet, LeoVegas, Unibet,
#       Winamax). Poor value -- they cost a full multiple for a handful of
#       books we already see.
#
# Stake, BetBoom, Fonbet and SBOBET are NOT on this provider at any price --
# confirmed against the full published list. Asking for them here is not a
# configuration problem, it is a provider problem; see README for the
# OddsPapi route.
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

# Everything above is the CORE list: it is polled every single cycle.
# Everything else in season is polled too, just on rotation -- see below.
#
# 2026-07-31. Seven top-flight European leagues is the worst possible hunting
# ground for what this tool looks for. Those markets are the most watched,
# most liquid and most efficiently priced in the world; a genuinely
# informed-money move there gets arbitraged away in seconds, and there is
# nothing dirty to find. Suspicious money shows up where nobody is looking:
# second and third divisions, reserve and youth sides, small federations,
# out-of-season friendlies, minor cups. So the tracker now polls the whole
# soccer group, not a shortlist -- and deliberately gives the obscure end of
# it priority over the famous end.
WIDE_COVERAGE = os.getenv("WIDE_COVERAGE", "1") not in ("0", "false", "False")

# Sport groups (as named by GET /v4/sports) that the wide sweep pulls from.
WIDE_GROUPS = [g.strip() for g in
               os.getenv("WIDE_GROUPS", "Soccer,Tennis").split(",") if g.strip()]

# Hard budget per cycle. Each sport key costs len(MARKETS) x len(REGIONS)
# credits, i.e. 1 credit on the current settings, so this is literally "how
# many credits a cycle may spend on odds".
#
# The arithmetic that forces a cap: the plan is 20,000 credits a month, about
# 666 a day. Measured in production, a cycle costs 1 + (number of sports), and
# the tracker was running ~102 cycles a day over 8 sports = ~918 credits a
# day. It was already spending 1.4x its income before any widening. Since
# breadth is now worth more than frequency (see BASELINE_WINDOW_MINUTES), the
# cap buys breadth and the cadence pays for it.
# 2026-08-10, raised 10 -> 15 by user decision after the funnel finally read
# clean and showed the real bottleneck. With every filter bucket at zero, the
# scarcity of signals was not selectivity at all: of 380 lines watched, 108
# twitched and only TWO cleared 10% in a poll -- one movement in 24 hours
# across the whole market. We had built a careful mill and were feeding it a
# handful of grain. Coverage is the one lever that adds signals without
# touching what counts as one: same threshold, same stars, same price band,
# just a bigger field. Costs credits linearly, which the user accepted
# ("не бойся, мы если что докупим кредитов").
# 2026-08-15, raised 15 -> 60 and simultaneously demoted from a cap to an
# AMBITION. It is now safe to ask for more than we can pay for, because
# budget.py hands back what the balance actually affords and this number is
# only the ceiling it may not exceed. Sixty covers essentially the whole
# in-season soccer + tennis list in one or two cycles instead of a four-hour
# lap, which also puts every league comfortably inside the baseline window --
# breadth and measurability improving together for once. On today's balance
# the governor will still hand back six; on a bigger plan it hands back sixty
# the same hour, with no commit.
MAX_SPORTS_PER_CYCLE = int(os.getenv("MAX_SPORTS_PER_CYCLE", "60"))

# The wide list is bigger than one cycle's budget, so it is walked in slices:
# each cycle takes the next MAX_SPORTS_PER_CYCLE - len(core) keys and the
# offset advances. Every league still gets seen regularly, just not every
# cycle -- which the baseline window above makes harmless, because a move is
# measured against the last price we hold for that line, not against "the
# previous cycle".
ROTATE_WIDE_COVERAGE = os.getenv("ROTATE_WIDE_COVERAGE", "1") not in ("0", "false", "False")

# Slots reserved for the wide sweep before the core list is allowed to spend
# the budget. Without a reservation, a busy tennis week fills every slot with
# famous tournaments and the tracker silently reverts to watching only the
# efficient markets -- the exact failure the wide sweep exists to fix.
#
# 2026-08-09: the reservation was never the problem, the WALK LENGTH was --
# five slots over ~40 wide keys is a lap every eight cycles, four hours at this
# cadence, and nothing on that list could be diffed against an hour-old price.
# Deliberately NOT raised to paper over it: taking slots from core would starve
# tennis, which is the one segment the ledger showed actually working (19 of 19
# graded, +5.1% CLV). The lap is shortened by dropping dormant leagues instead,
# and whatever lap remains is covered by stretching the baseline rail to match.
# 2026-08-10, 5 -> 8 alongside the wider cycle. Two reasons, and the second is
# not obvious: the wide list is where the informed money hides, so it should
# take most of the new slots; and a longer slice means a SHORTER lap, which is
# what keeps the baseline resolvable (see BASELINE_MAX_AGE_MINUTES for the day
# this went wrong). Core still keeps 7 of the 15, so tennis -- the only segment
# the ledger has shown working -- is not starved.
WIDE_MIN_SLOTS = int(os.getenv("WIDE_MIN_SLOTS", "8"))

# Skip leagues whose next fixture is beyond the publishing horizon.
#
# Two things pay for this. It stops us buying prices we could not publish even
# if they moved -- MAX_LEAD_HOURS means a match five days out is not a signal,
# and the one movement found during the blind day was exactly that, a fixture
# on 14.08 caught on 09.08. And by dropping dormant leagues from the walk it
# shortens the lap, which is what makes the baseline resolvable at all.
#
# The nearest fixture per sport is recorded from the previous fetch, so this
# costs nothing extra: a league we have never fetched is always tried once.
SKIP_DORMANT_SPORTS = os.getenv("SKIP_DORMANT_SPORTS", "1") not in ("0", "false", "False")

# Margin on top of MAX_LEAD_HOURS, so a league enters the rotation a little
# before its fixtures become publishable and a baseline already exists by the
# time the first move matters.
DORMANT_MARGIN_HOURS = float(os.getenv("DORMANT_MARGIN_HOURS", "12"))

# GET /v4/sports/ is documented as free but is NOT -- production logs show it
# billing 1 credit per call ("used=1454 remaining=18546 (call: /v4/sports/)").
# At 100+ cycles a day that is a whole league's worth of budget spent on a
# list that changes maybe twice a day, so it gets cached.
SPORTS_LIST_TTL_MINUTES = int(os.getenv("SPORTS_LIST_TTL_MINUTES", "180"))

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
# 2026-07-30: lowered from 12.0. A live signal recommended backing an 11.00
# outsider because it had "dropped" from 12.00 -- technically a move, but at
# those odds the price swings on rounding and nobody is loading informed money
# onto a 12-to-1 shot anyway.
# 2026-08-01, tightened to 5.5 by user decision: "чтобы не было огромных и
# супер маленьких кофов". Above this the price is a lottery ticket -- the
# money that moves it is not necessarily informed, and one win in six flatters
# the record without meaning anything.
# 2026-08-08, tightened again to 5.0 by user decision: "максимальный коф
# который будем использовать это 5". The ledger supported it -- across 23
# settled bets nothing above the band had produced a win, and long prices are
# where a single lucky result flatters a small sample most.
MAX_SIGNAL_PRICE = float(os.getenv("MAX_SIGNAL_PRICE", "5.0"))

# A decimal price at or below this is not a real market -- 1.00 pays nothing
# back, and anything under ~1.05 is a settled or suspended line rather than a
# quote you could take. Confirmed live 2026-07-29: a tennis line showed
# "просел до 1.00", and because detector and analytics applied DIFFERENT lower
# bounds, the same event reported "просело у 8 из 4 контор" -- more books moving
# than were quoting. Both modules now filter on this one constant so the two
# counts can never disagree again.
# 2026-08-01, raised from 1.05 to 1.55 by the same decision. Below this there
# is nothing to win: a 10% drop on a 1.20 favourite moves it to 1.08, and no
# amount of being right pays for the losses at that price.
MIN_SIGNAL_PRICE = float(os.getenv("MIN_SIGNAL_PRICE", "1.55"))

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

# --- how selective the product is -------------------------------------------
#
# Set 2026-08-08 from the ledger rather than from taste. Across the first 40
# signals (23 settled) the split by confidence was stark: two-star signals went
# 1 for 7 and lost $796, three-star signals went 7 for 16 and made $1 980. The
# user's call was to stop publishing anything below three stars -- "будем
# работать на качество" -- and take fewer, better events.
#
# Worth being clear about what this does NOT claim: 23 settled bets cannot
# prove a two-star signal is worthless. What it does is stop spending attention
# and credibility on the tier that has never paid, while COUNTERFACTUALS below
# keep measuring the tier we dropped, so the decision stays checkable instead
# of becoming folklore.
# --- The confidence ladder (2026-08-15, second revision) -------------------
# Earlier the same day breadth was briefly made proportional (a share of the
# quoting books) so that widening from 25 bookmakers to 75 could not inflate
# ratings while the OLD statistics were being continued. Vladislav then chose
# the other branch of that trade, and it is a coherent one:
#
#   "не процент от количества контор, а фактически столько, сколько мы считаем
#    достаточно... всё-таки сбрасывать всю статистику с нуля... давай придумаем
#    разрезы по 2-3-4 звезды с уровнем доверия к ставке... их должно быть
#    намного больше"
#
# Once the old book is thrown away, comparability with it stops being a
# constraint, and that is exactly what made the share rule necessary. Absolute
# counts are the more honest primitive anyway: "eight independent bookmakers
# moved the same way inside an hour" is a statement about the world, while
# "11% of the ones we happened to poll" is partly a statement about our
# subscription.
#
# So the ladder is counts, and it now goes to FOUR rungs instead of three. The
# point of the fourth is not decoration: publishing only the top rung meant
# every signal carried the same implied confidence, so nothing could ever be
# learned about whether the weak ones were worth taking. Now every rung is
# published and TRACKED SEPARATELY, and in a few days the by-stars table
# answers the question with data instead of opinion.
MOVED_FOR_2_STARS = int(os.getenv("MOVED_FOR_2_STARS", "3"))
MOVED_FOR_3_STARS = int(os.getenv("MOVED_FOR_3_STARS", "6"))
MOVED_FOR_4_STARS = int(os.getenv("MOVED_FOR_4_STARS", "12"))
MAX_STARS = 4

# ...and a CEILING by share, which is what stops the ladder from measuring the
# size of a league instead of the strength of a move.
#
# The counts above are the rungs Vladislav asked for and they are what the page
# shows. But a count alone does not survive the feed widening from ~25 books to
# ~75: a move is an event on an OUTCOME, so when three times as many books
# quote it, three times as many move, and the same real-world move climbs the
# ladder for free. Checked against the twenty signals in the ledger on
# 2026-08-15, tripled at constant share, a pure-count rule collapses five of
# six into four stars. With the ceiling below, not one of the twenty changes
# rating. That is the whole justification, and it is a testable claim --
# test_stars_invariance.py asserts it directly.
#
# The division of labour is exact. On a thin market (5 books) the ceiling never
# binds and the count decides, so "4 of 5" cannot masquerade as a market-wide
# move. On a wide market (60 books) the count is always saturated and the share
# decides, so "12 of 60" is honestly a two-star event rather than a four.
SHARE_CAP_2_STARS = float(os.getenv("SHARE_CAP_2_STARS", "0.20"))
SHARE_CAP_3_STARS = float(os.getenv("SHARE_CAP_3_STARS", "0.33"))
SHARE_CAP_4_STARS = float(os.getenv("SHARE_CAP_4_STARS", "0.50"))

# What each rung is called, and how much trust the page and the bot should
# express. Deliberately worded as evidence strength, never as advice.
STAR_LABELS = {
    2: "осторожно",
    3: "уверенно",
    4: "максимум",
}

# IMPORTANT, AND EASY TO GET WRONG LATER: the notional stake is the SAME on
# every rung. Sizing bets by confidence would be the obvious move and it would
# also destroy the measurement -- profit per rung would then reflect the stake
# schedule as much as the edge, and the by-stars table could no longer answer
# "is two stars worth taking". Measure first on equal stakes; let the data
# decide the sizing afterwards. See storage.FLAT_STAKE.

# 2026-08-15: lowered 3 -> 2 with the four-rung ladder. Publishing only the
# top rung is what kept the sample tiny (twenty signals in a week) and made
# every confidence claim unfalsifiable. Two stars now publishes, clearly
# labelled "осторожно", and is scored in its own row.
MIN_SIGNAL_STARS = int(os.getenv("MIN_SIGNAL_STARS", "2"))

# Do not publish a signal for a match that is further away than this.
#
# Two reasons, and the second matters more. A price three days out is not the
# price you will get: the line is thin, limits are low, and it will be reissued
# several times before kick-off, so "мы взяли 4.40" is a claim we cannot stand
# behind. And a signal that sits on the page for three days is read as a
# standing recommendation rather than as a moment -- the whole product is about
# a window that is open now.
#
# 2026-08-08: set to 48 and raised to 60 the same day at the user's request --
# far enough to keep weekend fixtures spotted on a Friday, still short of the
# multi-day window where the quoted price stops being real.
MAX_LEAD_HOURS = float(os.getenv("MAX_LEAD_HOURS", "60"))

# How much better than the computed fair (no-vig) price a bookmaker has to be
# before the analyst calls it value, in percent. Below this the edge is inside
# the model's own error bars.
MIN_EDGE_PCT = float(os.getenv("MIN_EDGE_PCT", "2.0"))

# Minimum disagreement between bookmakers on an in-play outcome before it
# counts as a live signal. Live bets are rarer than pre-match ones by design,
# so this is the one number that decides how many appear at all.
LIVE_MIN_SPREAD_PCT = float(os.getenv("LIVE_MIN_SPREAD_PCT", "15.0"))

# Sport keys served by OddsPapi rather than The Odds API. Results for these
# cannot be looked up through The Odds API scores endpoint -- it has never
# heard of them -- so grading skips them instead of erroring every cycle.
ODDSPAPI_SPORT_KEYS = {"esports_cs2", "esports_dota2", "esports_lol", "table_tennis"}

# Esports results are settled one fixture at a time (OddsPapi has no per-sport
# scores sweep), so this is a spend cap per results check rather than a limit
# on how many we care about. Twelve is comfortably more than the esports
# signals a three-star filter produces in a day, and it stops a backlog from
# turning into a burst of requests after an outage.
ODDSPAPI_SETTLEMENTS_PER_CYCLE = int(os.getenv("ODDSPAPI_SETTLEMENTS_PER_CYCLE", "12"))

# Live scores are for READING, not for deciding anything, so they do not need
# to keep pace with the poll.
#
# 2026-08-09: found while checking why a cycle cost 13 credits instead of the
# 10 the odds calls account for. Live scores ran once per CYCLE, capped at two
# sports -- fine at half-hourly polling, but the moment the cadence tripled so
# did this, purely to redraw a number nobody acts on. Throttling it by the
# clock instead means the displayed score is at most this stale while the cost
# stops scaling with how fast we poll.
LIVE_SCORE_MIN_INTERVAL_MINUTES = int(os.getenv("LIVE_SCORE_MIN_INTERVAL_MINUTES", "30"))

# How often the bot sends a "still here, this is what the market did" digest,
# even when there is nothing to bet.
#
# Requested 2026-08-10: "чтобы отчеты были в бота, а то сижу втыкаю". Until now
# the bot only spoke when a signal fired, so a quiet market and a broken poller
# were indistinguishable from the outside -- which is exactly the failure mode
# that cost two days this week. A heartbeat with real numbers makes silence
# informative instead of ambiguous.
DIGEST_INTERVAL_HOURS = float(os.getenv("DIGEST_INTERVAL_HOURS", "3"))

# When to stop asking about a match and record that we could not check it.
#
# The Odds API scores endpoint only reaches three days back, so a bet missed
# for longer than that can never be graded -- and until now those rows sat in
# "ждут матча" for ever, quietly inflating the pending count and making the
# record look bigger than the part of it we had actually verified. After the
# 46-hour outage on 2026-08-06 five bets were in exactly that state, two of
# them 60 and 70 hours past kick-off. Past this age we mark them n/a and say
# so, because an honest gap is worth more than a number that never resolves.
RESULT_GIVE_UP_HOURS = float(os.getenv("RESULT_GIVE_UP_HOURS", "96"))

# A bookmaker counts as "hasn't moved yet" -- i.e. still worth betting into --
# only if its price is at least this much above where the books that DID move
# have settled. Anything tighter is not a real entry, just rounding.
ENTRY_MIN_GAP_PCT = float(os.getenv("ENTRY_MIN_GAP_PCT", "3.0"))

# How much of the drop the entry has to give back, in percent of the whole
# move. THIS IS THE RULE THAT MAKES A SIGNAL MEAN WHAT IT SAYS.
#
# Found live 2026-07-30 on Furia Esports - Keyd Stars. One bookmaker had Keyd
# Stars at 3.20, corrected itself to 1.73, and the only other book on that
# fixture was sitting at 1.87 -- where it had been all along. The old code
# only asked "is the entry at least 3% above where the market went", 1.87 is
# 8% above 1.73, so it happily announced "был 3.20, ставим за 1.87". Nonsense:
# 3.20 was never a price anyone could have taken, and 1.87 gives back barely a
# tenth of the supposed move.
#
# The user's own logic is the fix -- "если коэффициент был 3, ставим за 3".
# So the entry now has to recover at least half the distance between the old
# price and the new one. Take the Keyd Stars numbers: the entry would have had
# to be 2.46 or better, and 1.87 is thrown out.
ENTRY_MIN_CAPTURE_PCT = float(os.getenv("ENTRY_MIN_CAPTURE_PCT", "50.0"))

# ...and nothing priced far ABOVE where the market was before the move counts
# either. A bookmaker offering more than the pre-drop price is not a slow
# bookmaker, it is a stale or mis-keyed line, and those get voided rather than
# paid. A little headroom is allowed because bookmakers genuinely disagree.
ENTRY_MAX_OVER_OLD_PCT = float(os.getenv("ENTRY_MAX_OVER_OLD_PCT", "10.0"))

# How many bookmakers must be quoting an outcome before a move in it counts as
# a market move at all.
#
# Same incident: that esports fixture was priced by exactly TWO bookmakers, so
# "просело у 1 из 2" was arithmetically true and completely meaningless. With
# two quotes there is no consensus for anyone to lag behind -- one of them
# fixing a typo is indistinguishable from money arriving. Breadth is the whole
# confidence signal in this product, and breadth needs a crowd.
#
# Consequence worth knowing: esports fixtures that only two of the four
# OddsPapi books cover will now produce no signal. That is the correct
# outcome. No signal beats a fake one.
# 2026-08-01: lowered from 4 to 3, and it stopped being an alert filter.
# Measured over 24 hours with the widened line: the market produced 6 drops
# past the threshold, and this rule rejected SIX OF SIX. Nothing else in the
# funnel rejected anything -- the entry rules never even got a look. The
# reason is structural, not a fluke: the whole point of the wide sweep is
# lower divisions and small federations, and a Latvian second-division match
# is quoted by three bookmakers, not eight. Demanding a crowd in exactly the
# markets chosen for having no crowd guarantees zero signals forever.
#
# Breadth is still the confidence signal, but it is now measured by how many
# books MOVED (MIN_MOVED_BOOKS) rather than by how many exist. That is the
# thing the original incident was actually about: "просело у 1 из 2" is
# meaningless because ONE book moved, not because only two were quoting.
MIN_MARKET_BOOKS = int(os.getenv("MIN_MARKET_BOOKS", "3"))

# How many independent bookmakers must have moved the same way before a drop
# is allowed to become a signal. This replaces MIN_MARKET_BOOKS as the guard
# against reading one trader's typo as informed money -- it asks the question
# that matters (do several books agree?) instead of a proxy for it (is this a
# big league?). A sharp book moving on its own also clears it: Pinnacle
# shortening a price is not a typo, it is the reference the rest of the
# market follows.
MIN_MOVED_BOOKS = int(os.getenv("MIN_MOVED_BOOKS", "2"))

# A single quote further than this from the median of all quotes on the same
# outcome is treated as broken rather than as an opinion. Bookmakers disagree
# by 10-20%; they do not disagree by 70%.
OUTLIER_MAX_DEVIATION_PCT = float(os.getenv("OUTLIER_MAX_DEVIATION_PCT", "45.0"))

# The draw is never bet (user decision, 2026-07-29) -- only the two match
# winners are actionable. Draw prices are still FETCHED and still feed the
# no-vig fair-price calculation, because removing a leg from a 3-way market
# would make the margin maths wrong; they are only hidden from the cards and
# excluded from being picked as a bet.
EXCLUDE_DRAW = os.getenv("EXCLUDE_DRAW", "1") not in ("0", "false", "False")


# --- Polling cadence -------------------------------------------------------
# How often the market is actually snapshotted.
#
# 2026-07-29: this stopped being a constant. The cadence now comes from
# cadence.json -- ONE source of truth read at import time, so runner.py's loop,
# the Telegram cadence and the countdown on the site are always the same
# number. They used to be three separate values and they drifted: the site
# advertised one figure while the code ran another.
#
# Why a file of date ranges instead of just editing the cron: GitHub Actions
# will not schedule anything more often than every 5 minutes, and runs it late
# routinely. So the workflow fires every 30 minutes and runner.py does its own
# timed loop inside that window -- the only way to get a real 3-minute cadence
# out of GitHub. The cron in poll.yml never has to change when the cadence does.
CADENCE_PATH = os.path.join(os.path.dirname(__file__), "cadence.json")


def _load_cadence(at=None):
    """(minutes, label) for the phase covering `at` (default: now, UTC)."""
    from datetime import datetime, timezone
    import json

    at = at or datetime.now(timezone.utc)
    try:
        with open(CADENCE_PATH, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return 30, ""

    def _dt(value):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    for phase in cfg.get("phases", []):
        try:
            if _dt(phase["from"]) <= at < _dt(phase["to"]):
                return int(phase["minutes"]), phase.get("label", "")
        except (KeyError, ValueError, TypeError):
            continue
    return int(cfg.get("default_minutes", 30)), ""


# An explicit env var still wins, so a one-off manual run can override the
# schedule without editing the file.
_forced = os.getenv("POLL_INTERVAL_MINUTES")
if _forced:
    POLL_INTERVAL_MINUTES, CADENCE_LABEL = int(_forced), ""
else:
    POLL_INTERVAL_MINUTES, CADENCE_LABEL = _load_cadence()

# How often the GitHub workflow itself fires. The dashboard is a static file
# republished once per run, so THIS -- not POLL_INTERVAL_MINUTES -- is how
# often the page changes. Stated separately on the site rather than blurred
# together, because they are genuinely different numbers: the bot fires on
# every poll, the page catches up once per run.
#
# 2026-08-09: this now DEFAULTS to the poll interval instead of a hardcoded 30.
# The two being independent was fine while both happened to be 30, but the
# moment cadence.json says 20 the run window still spans 30 minutes, so a run
# polls at :00 and :20 and the next one at :30 and :50 -- an advertised
# "каждые 20 минут" delivered as an uneven 20/10/20/10 sawtooth. Tying them
# together means changing cadence.json alone stays the only lever, which is the
# whole point of that file.
PUBLISH_INTERVAL_MINUTES = int(os.getenv("PUBLISH_INTERVAL_MINUTES")
                               or POLL_INTERVAL_MINUTES)

# Stop polling inside a run if the provider's monthly credit balance falls
# below this. A 3-minute cadence burns credits ten times faster than a
# 30-minute one, and running the account to zero mid-month would take the whole
# product offline -- much worse than a few missed cycles.
# 2026-08-09: lowered from 1500. The reserve exists so the account never hits
# zero mid-month and loses the ability to grade results -- but at 1500 it was
# holding back a fifth of the remaining budget, which at the current burn is
# several days of coverage. 800 still leaves room to settle every open bet.
# 2026-08-15, briefly lowered to 250 while the balance sat at 650 and a plan
# upgrade was being paid for -- a narrow live tracker beats a wide frozen one.
# Restored the same day: the 5M plan landed, and on five million credits a
# reserve of 800 is a rounding error while the protection it buys (always
# enough left to grade every open bet) is exactly as valuable as before.
QUOTA_RESERVE_CREDITS = int(os.getenv("QUOTA_RESERVE_CREDITS", "800"))

# --- The credit governor (2026-08-15) --------------------------------------
# MAX_SPORTS_PER_CYCLE above stopped being a promise and became an AMBITION:
# budget.py recomputes the real cap every cycle from the credits the API says
# are left, so a number set here can no longer overspend the plan. See the
# module docstring there for why a fixed cap failed in both directions inside
# a single fortnight -- a runaway on the 8th, a starved plan on the 15th.
#
# This is also what makes buying a bigger plan a purchase rather than a
# release: the next cycle reads a larger `remaining`, computes a larger
# allowance, and widens by itself. No commit, no redeploy.
AUTO_BUDGET = os.getenv("AUTO_BUDGET", "1") not in ("0", "false", "False")

# Never narrow below this, however little is left. A tracker watching six
# leagues still detects, still grades, still publishes; one watching zero is
# indistinguishable from a broken deploy. The hard stop is runner.py's reserve
# guard, and it should stay the ONLY thing that stops us.
MIN_SPORTS_PER_CYCLE = int(os.getenv("MIN_SPORTS_PER_CYCLE", "6"))

# The width to use when the credit balance is genuinely unknown -- a brand new
# database, or AUTO_BUDGET switched off. Deliberately the pre-governor value
# rather than MAX_SPORTS_PER_CYCLE: an unknown balance must never authorise the
# largest possible spend. It did exactly that for one afternoon on 2026-08-15,
# because the sports list is served from cache and a whole cycle can therefore
# complete without any response header ever carrying a quota figure.
COLD_START_SPORTS = int(os.getenv("COLD_START_SPORTS", "15"))

# The provider's allowance is monthly. The exact reset date is never sent in
# any header, so budget.observe() infers it: `used` only climbs inside a
# period, so the moment it falls, a new one has begun.
QUOTA_PERIOD_DAYS = float(os.getenv("QUOTA_PERIOD_DAYS", "30"))

# Warn to Telegram while there is still time to act. Running out of credits
# must never first be noticed by a human wondering why the site looks quiet --
# that is precisely the failure mode that cost two days earlier this month.
QUOTA_WARN_CREDITS = int(os.getenv("QUOTA_WARN_CREDITS", "2500"))
QUOTA_WARN_INTERVAL_HOURS = float(os.getenv("QUOTA_WARN_INTERVAL_HOURS", "6"))

# --- The region ladder (2026-08-15) ----------------------------------------
# Bookmakers are the binding constraint on signal count -- see the note above
# REGIONS -- and region is the only lever that adds them. So regions climb on
# their own as the balance allows, exactly the way breadth does. The point is
# that buying a bigger plan needs no code: the next cycle sees more credits,
# affords another region, and the number of books roughly doubles the same
# hour.
#
# Order matters and is by value per credit, not alphabetical:
#   eu  the sharp reference books (Pinnacle, 1xBet, Marathon). Never dropped.
#   uk  ~15 books that exist nowhere else, all serious on soccer and tennis.
#   au  the only region carrying bet365, the largest book in the world.
#   us  DraftKings, FanDuel, BetMGM -- deep on US sports, thinner on European
#       soccer, so it is bought last.
# se and fr are deliberately absent: they overlap eu almost entirely and would
# cost a full multiple for a handful of books already visible.
AUTO_REGIONS = os.getenv("AUTO_REGIONS", "1") not in ("0", "false", "False")
REGION_LADDER = os.getenv("REGION_LADDER", "eu,uk,au,us")

# A region must never be bought by starving the league list. Doubling the price
# per league while the cycle can only afford eight of them trades away more
# market than it buys books, so the next rung is only taken once the budget can
# still keep this many leagues at the HIGHER per-league price.
REGION_STEP_MIN_SPORTS = int(os.getenv("REGION_STEP_MIN_SPORTS", "20"))

# --- Strategy split (user decision, 2026-07-29) ----------------------------
# Every signal is logged once and then counted under BOTH headings, so the two
# win rates are measured on the same events rather than on two different
# samples:
#   АГРЕССИВНАЯ -- every signal the tracker fires, whatever the price.
#   ОПТИМАЛЬНАЯ -- only those whose entry price is at or below this line.
# The point is to find out empirically whether skipping the long shots
# actually improves the bottom line, instead of assuming it does.
OPTIMAL_MAX_PRICE = float(os.getenv("OPTIMAL_MAX_PRICE", "2.8"))

# Above this price the straight bet is treated as risky enough to be worth
# offering a "безопасный" alternative alongside it (double chance in football,
# a handicap in tennis) -- see analytics._safe_variant().
SAFE_TRIGGER_PRICE = float(os.getenv("SAFE_TRIGGER_PRICE", "3.5"))

# The band a safe alternative has to land in to be worth naming. Below the
# floor you are risking a lot to win very little; above the ceiling it is not
# meaningfully safer than the straight bet it was supposed to replace.
SAFE_TARGET_MIN = float(os.getenv("SAFE_TARGET_MIN", "1.7"))
SAFE_TARGET_MAX = float(os.getenv("SAFE_TARGET_MAX", "2.5"))

# Raw price history is only needed to diff against the previous poll and to
# find the closing line for CLV; beyond that it is dead weight. At a 3-minute
# cadence the table grows by roughly 3.5 million rows a day, which would make
# the CI cache slow to save and eventually impossible. Alerts are never
# pruned -- they are the actual track record.
SNAPSHOT_RETENTION_HOURS = int(os.getenv("SNAPSHOT_RETENTION_HOURS", "96"))

# Public URL of the dashboard, linked at the bottom of each Telegram digest.
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://kev1nst21.github.io/odds-tracker/")

# Flat stake used for the "what would the balance be" line on the dashboard.
# A flat stake is the only honest way to present this: varying the stake would
# let the number be tuned after the fact. $200 is a size most bookmakers accept
# without cutting limits.
FLAT_STAKE = float(os.getenv("FLAT_STAKE", "200"))

# v5 (2026-07-29): third reset -- the live experiment was dropped entirely
# (in-play disagreement turned out to be noise, not signal), so the counters
# start clean again and only pre-match signals are ever recorded.
# v4 (2026-07-29): second reset. v3 mixed pre-match and live bets into one
# set of numbers and logged esports alerts that could never be graded (their
# scores live on a different provider), so the win rate was meaningless.
# Stats are now split by kind and start clean again.
# v3 (2026-07-29): clean slate. The earlier database held alerts recorded under
# the old per-line logic, where the stored "alert price" was whatever the book
# that spiked was showing -- not the price we would actually have bet at. Those
# rows can't be re-scored meaningfully, so statistics start fresh from here and
# every alert now records was-price, dropped-to price and the entry price we
# recommended.
DB_PATH = os.path.join(os.path.dirname(__file__), "data", "odds_history_v5.db")
DASHBOARD_PATH = os.path.join(os.path.dirname(__file__), "dashboard", "index.html")
