"""
Weather Trading Bot - Configuration

Polymarket weather market sniper with multi-source forecasts.
Supports paper (dry-run) and live trading modes.

OVERHAUL NOTE: defaults now favor the observation-driven edge - the
Late Observed-Temperature strategy is the PRIMARY strategy, fee-aware EV gating
is on, liquidity awareness adapts to thin books (no hard blocking by default),
and the old forecast-only strategies (PeakBasket / Confident) are demoted to
opt-in.
"""

import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    """Central configuration for weather trading bot."""

    VERSION = "2.1.0"
    VERSION_NAME = "Weather Sniper Pro - Observed Edge"

    # ===================================================================
    # TRADING MODE - paper = dry-run (no real orders), live = real money
    # ===================================================================
    TRADING_MODE = os.getenv('TRADING_MODE', 'paper')  # 'paper' or 'live'
    STARTING_BALANCE = float(os.getenv('STARTING_BALANCE', '3.0'))
    # Master trading switch - toggled live via Telegram Start/Stop buttons. When
    # False the bot keeps scanning/monitoring but places NO new trades.
    # Req-28: default OFF so a fresh Railway deploy does NOT auto-trade. The bot
    # boots, posts "initialized successfully" + [Start Trading][Settings][Restart]
    # buttons, and only begins trading when the user presses Start / types 'start'.
    # (dashboard startup ALSO force-resets this to False on every boot so a
    # previously-persisted True never silently auto-resumes a deploy.)
    TRADING_ENABLED = os.getenv('TRADING_ENABLED', '0') == '1'

    # ===================================================================
    # POLYMARKET WALLET (reused from polymarket-bot-v2)
    # ===================================================================
    POLY_PRIVATE_KEY = os.getenv('POLY_PRIVATE_KEY', '')
    POLY_FUNDER_ADDRESS = os.getenv('POLY_FUNDER_ADDRESS', '')
    POLY_PROXY_WALLET = os.getenv('POLY_PROXY_WALLET', '')
    POLY_API_KEY = os.getenv('POLY_API_KEY', '')
    POLY_API_SECRET = os.getenv('POLY_API_SECRET', '')
    POLY_PASSPHRASE = os.getenv('POLY_PASSPHRASE', '')
    POLY_SIGNATURE_TYPE = int(os.getenv('POLY_SIGNATURE_TYPE', '3'))
    POLY_CHAIN_ID = int(os.getenv('POLY_CHAIN_ID', '137'))

    # ===================================================================
    # BUILDER RELAYER (required for live V2 trading)
    # ===================================================================
    POLY_BUILDER_API_KEY = os.getenv('POLY_BUILDER_API_KEY', '')
    POLY_BUILDER_SECRET = os.getenv('POLY_BUILDER_SECRET', '')
    POLY_BUILDER_PASSPHRASE = os.getenv('POLY_BUILDER_PASSPHRASE', '')
    POLY_BUILDER_CODE = os.getenv('POLY_BUILDER_CODE', '')
    AUTO_REDEEM_INTERVAL = int(os.getenv('AUTO_REDEEM_INTERVAL', '120'))

    # ===================================================================
    # V2 CONTRACT ADDRESSES (Polygon mainnet)
    # ===================================================================
    PUSD_CONTRACT = '0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB'
    CTF_EXCHANGE_V2 = '0xE111180000d2663C0091e4f400237545B87B996B'
    NEG_RISK_CTF_EXCHANGE = '0xe2222d279d744050d28e00520010520000310F59'

    # ===================================================================
    # API ENDPOINTS (V2 - same as polymarket-bot-v2)
    # ===================================================================
    GAMMA_API_URL = 'https://gamma-api.polymarket.com'
    CLOB_API_URL = 'https://clob.polymarket.com'
    POLYMARKET_WS_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market'
    POLYGON_RPC_URL = os.getenv('POLYGON_RPC_URL', '')
    CLOB_RELAY_URL = os.getenv('CLOB_RELAY_URL', '')

    # Fee: weather = 0% maker (GTC), 5% taker (FOK)
    MAKER_FEE_RATE = 0.0
    TAKER_FEE_RATE = 0.05
    MAKER_PREFERRED = True  # always GTC limit = 0% fee
    # Fee-aware EV gating: when 1, EV/break-even checks assume the conservative
    # TAKER fee (5% x p x (1-p)). Keep on unless you are confident every fill is
    # a resting maker order.
    ASSUME_TAKER_FILLS = os.getenv('ASSUME_TAKER_FILLS', '1') == '1'

    # ===================================================================
    # WEATHER API KEYS
    # ===================================================================
    OPENWEATHER_API_KEY = os.getenv('OPENWEATHER_API_KEY', '')
    # Open-Meteo: no key needed (free, 10k calls/day)
    # weather.gov: no key needed (US gov free)
    # Open-Meteo endpoints to round-robin across. The free tier allows ~10k
    # calls/day; alternating across mirrors (or a self-hosted instance) spreads
    # the budget and reduces the chance of a single-IP rate limit. Comma-sep.
    # Add a second URL here and the fetcher alternates automatically.
    OPEN_METEO_ENDPOINTS = [e.strip() for e in os.getenv(
        'OPEN_METEO_ENDPOINTS',
        'https://api.open-meteo.com/v1/forecast'
    ).split(',') if e.strip()]
    # How long a forecast fetch is cached (seconds). Lower = fresher data + more
    # API calls; keep within the daily budget.
    WEATHER_FORECAST_CACHE_SECONDS = int(os.getenv('WEATHER_FORECAST_CACHE_SECONDS', '300'))

    # Open-Meteo model members (comma-sep, highest confidence first). Default now
    # leads with ECMWF IFS *HRES* (`ecmwf_ifs`, native ~9 km, finest + freshest,
    # real-time) then IFS 0.25 (`ecmwf_ifs025`), the GFS/ICON/JMA/GEM seamless
    # members, and finally the old 0.4 deg `ecmwf_ifs04` kept ONLY as a fallback
    # member (it is the coarsest ECMWF grid and frequently returns all-null; the
    # null-tolerant parser simply skips it when empty). HRES carries no 2m
    # relative humidity field -> that column may be null for the HRES member,
    # which the null-tolerant helpers already handle; temperature is solid.
    OPEN_METEO_MODELS = [m.strip() for m in os.getenv(
        'OPEN_METEO_MODELS',
        'ecmwf_ifs,ecmwf_ifs025,gfs_seamless,icon_seamless,jma_seamless,gem_seamless'
    ).split(',') if m.strip()]

    # ===================================================================
    # WEATHER-API FAILOVER (Req-25 fix #5) - survive Open-Meteo rate / IP limits.
    # When the primary forecast provider returns a rate/IP-limit response
    # (HTTP status in WEATHER_RATELIMIT_STATUS - e.g. 429/403 - or an Open-Meteo
    # JSON error whose reason mentions "limit"), that endpoint is put on a
    # COOLDOWN and the fetchers transparently fail over to the next available
    # source: another OPEN_METEO_ENDPOINTS mirror (plus any
    # OPEN_METEO_FAILOVER_ENDPOINTS), then OpenWeatherMap, then weather.gov. The
    # cooled endpoint is retried automatically once its cooldown expires, so the
    # primary recovers on its own.
    # ===================================================================
    WEATHER_FAILOVER_ENABLED = os.getenv('WEATHER_FAILOVER_ENABLED', '1') == '1'
    # Seconds to rest a forecast endpoint after a rate/IP-limit hit before retrying it.
    WEATHER_PROVIDER_COOLDOWN_SECONDS = int(os.getenv('WEATHER_PROVIDER_COOLDOWN_SECONDS', '600'))
    # HTTP status codes treated as rate/IP-limited (comma-sep) -> cooldown + failover.
    WEATHER_RATELIMIT_STATUS = [int(s.strip()) for s in os.getenv('WEATHER_RATELIMIT_STATUS', '429,403').split(',') if s.strip()]
    # Extra Open-Meteo mirror endpoints used as failover ONLY when the primary
    # endpoints are cooling down (comma-sep; supplements OPEN_METEO_ENDPOINTS).
    OPEN_METEO_FAILOVER_ENDPOINTS = [e.strip() for e in os.getenv('OPEN_METEO_FAILOVER_ENDPOINTS', '').split(',') if e.strip()]
    # -- VPS Edge Node (weather proxy + data store) -----------------------
    # When VPS_BASE_URL is set the bot routes Open-Meteo through the VPS
    # (fast, self-warming cache) and AUTOMATICALLY keeps DIRECT Open-Meteo as
    # a failover, so if the proxy is offline the bot silently falls back to
    # normal Railway->Open-Meteo polling. The token is sent ONLY to the VPS.
    VPS_BASE_URL = os.getenv('VPS_BASE_URL', '').strip().rstrip('/')
    VPS_AUTH_TOKEN = os.getenv('VPS_AUTH_TOKEN', '') or os.getenv('PROXY_AUTH_TOKEN', '')
    VPS_FORECAST_PATH = os.getenv('VPS_FORECAST_PATH', '/v1/forecast')
    VPS_TIMEOUT_SECONDS = float(os.getenv('VPS_TIMEOUT_SECONDS', '8'))
    VPS_OFFLOAD_ENABLED = os.getenv('VPS_OFFLOAD_ENABLED', '0') == '1'
    VPS_OFFLOAD_INTERVAL_HOURS = float(os.getenv('VPS_OFFLOAD_INTERVAL_HOURS', '12'))
    VPS_OFFLOAD_BATCH_LINES = int(os.getenv('VPS_OFFLOAD_BATCH_LINES', '2000'))
    if VPS_BASE_URL:
        _vps_forecast = VPS_BASE_URL + VPS_FORECAST_PATH
        _eps = [_vps_forecast]
        for _e in OPEN_METEO_ENDPOINTS:
            if _e != _vps_forecast:
                _eps.append(_e)
        OPEN_METEO_ENDPOINTS = _eps
        _direct = 'https://api.open-meteo.com/v1/forecast'
        _fbs = list(OPEN_METEO_FAILOVER_ENDPOINTS)
        if (_direct not in OPEN_METEO_ENDPOINTS) and (_direct not in _fbs):
            _fbs.append(_direct)
        OPEN_METEO_FAILOVER_ENDPOINTS = _fbs

    # WEATHER-DATA BUY GUARD (Req-30) - NEVER place a buy without enough live
    # weather data. fetch_all() returns nothing when every provider failed / is
    # cooling down (the "Open-Meteo cooling down" / "observed fetch returned no
    # data" errors). Require at least this many forecast models before a market
    # is evaluated for buys; this single choke point protects EVERY strategy.
    WEATHER_BUY_GUARD_ENABLED = os.getenv('WEATHER_BUY_GUARD_ENABLED', '1') == '1'
    WEATHER_MIN_FORECAST_MODELS = int(os.getenv('WEATHER_MIN_FORECAST_MODELS', '1'))

    # LAST-GOOD OBSERVED CACHE (overlay in data/observed_weather.py). Remembers
    # the highest observed extreme (high mode) / lowest (low mode) seen so far
    # today per city, and (1) CLAMPS a fresh read so a glitchy LOWER value can
    # never weaken a lock we already earned, and (2) bridges a momentary
    # Open-Meteo starvation (fresh=None) with the last-good value so a
    # near-certain trade is not missed. It can ONLY raise (high) / lower (low)
    # the extreme -> it can never manufacture false confidence. TTL bounds
    # staleness within the local day (key already includes the date).
    OBSERVED_CACHE_ENABLED = os.getenv('OBSERVED_CACHE_ENABLED', '1') == '1'
    OBSERVED_CACHE_TTL_SECONDS = int(os.getenv('OBSERVED_CACHE_TTL_SECONDS', '10800'))  # 3h

    # WEATHER TRACE / HEALTH WATCHER (overlay/weather_trace.py). Fail-open,
    # append-only JSONL side-car that records, per decision, exactly which
    # weather data drove it: per-source point counts + peak temp (provider
    # agreement), the observed lock state (locked vs starved, spread, hours
    # left, models with data vs null, which source produced the used number),
    # city/market/strategy/timestamp. Purely observational (never changes a
    # decision); shipped in /exportdata and summarised by /weatherhealth.
    WEATHER_TRACE_ENABLED = os.getenv('WEATHER_TRACE_ENABLED', '1') == '1'

    # ===================================================================
    # TRADING PARAMETERS
    # ===================================================================
    # Sniper strategy: buy buckets priced below this when forecast is strong
    SNIPER_MAX_ENTRY_PRICE = float(os.getenv('SNIPER_MAX_ENTRY_PRICE', '0.15'))
    # SELLABILITY FLOOR (early-exit strategies): a leg that plans to SELL before
    # resolution needs a real bid to exit into. Below ~5c the book often has no
    # bid, so we only require this floor for strategies that intend to flip.
    # Hold-to-resolution legs (e.g. late-observed) bypass it - their EV is
    # already fee-cleared and they never need to sell.
    MIN_ENTRY_PRICE = float(os.getenv('MIN_ENTRY_PRICE', '0.05'))
    # HARD DUST FLOOR (all strategies): below this a leg can't even rest on the
    # 1c-tick venue. This is the only absolute price block now - cheap EV+ tails
    # above it are allowed when held to resolution (the 90%-WR wallet's edge).
    ABS_PRICE_FLOOR = float(os.getenv('ABS_PRICE_FLOOR', '0.01'))
    # Trade BOTH daily-high and daily-low markets. The observation-driven
    # strategy locks the high in the afternoon and the low overnight, and trades
    # the NO side of dead buckets either way. Set to 1 to restrict to highs only.
    HIGHEST_TEMP_ONLY = os.getenv('HIGHEST_TEMP_ONLY', '0') == '1'
    # Minimum edge (our probability - market price) to enter
    MIN_EDGE_TO_ENTER = float(os.getenv('MIN_EDGE_TO_ENTER', '0.10'))
    # Kelly criterion fraction (conservative)
    KELLY_FRACTION = float(os.getenv('KELLY_FRACTION', '0.15'))
    # Maximum bet as % of balance
    MAX_BET_PCT = float(os.getenv('MAX_BET_PCT', '0.20'))
    # Minimum order size on Polymarket
    MIN_ORDER_SIZE = 1.0
    # Maximum concurrent positions
    MAX_POSITIONS = int(os.getenv('MAX_POSITIONS', '10'))
    # Maximum exposure per single market (% of balance)
    MAX_SINGLE_MARKET_PCT = float(os.getenv('MAX_SINGLE_MARKET_PCT', '0.30'))
    # Risk Management (weather markets are BINARY -> no traditional SL/TP)
    # Instead: hold to resolution OR sell early at profit
    # "Stop-loss" only applies to LOCK-IN trades that went wrong
    STOP_LOSS_PCT = float(os.getenv('STOP_LOSS_PCT', '-95'))  # almost never triggers
    TRAILING_STOP_PCT = float(os.getenv('TRAILING_STOP_PCT', '30'))
    # Only arm the trailing stop after a BIG run-up (peak >= this multiple of
    # entry). Higher = give winners more room before a trailing exit can fire.
    # The user flagged trailing/stop exits cutting good positions too early.
    TRAILING_MIN_PEAK_MULT = float(os.getenv('TRAILING_MIN_PEAK_MULT', '3.0'))
    # PROFIT-TAKE: sell if price rises above this BEFORE resolution (early profit)
    EARLY_PROFIT_THRESHOLD = float(os.getenv('EARLY_PROFIT_THRESHOLD', '0.60'))
    # For confident strategy: never sell (hold to resolution)
    CONFIDENT_NEVER_SELL = os.getenv('CONFIDENT_NEVER_SELL', '1') == '1'

    # ===================================================================
    # BEST-KELLY FACTOR SIZING (trading/sizing.py) - multi-factor allocation.
    # Replaces the flat %-Kelly that dumped ~25% of balance on the first signal
    # and starved later (better) markets. Stake now scales with a composite
    # signal-strength score (edge + win-probability + grade + realized win-rate)
    # onto an ABSOLUTE-USD tier ladder: base $3, good $5, very good $10, perfect
    # $15 MAX per position. A portfolio RESERVE + per-scan deploy caps keep cash
    # for opportunities that appear later in the same scan.
    # ===================================================================
    KELLY_FACTOR_SIZING_ENABLED = os.getenv('KELLY_FACTOR_SIZING_ENABLED', '1') == '1'
    KELLY_TIER_BASE_USD = float(os.getenv('KELLY_TIER_BASE_USD', '3.0'))       # weakest valid signal
    KELLY_TIER_GOOD_USD = float(os.getenv('KELLY_TIER_GOOD_USD', '5.0'))       # good signal
    KELLY_TIER_VGOOD_USD = float(os.getenv('KELLY_TIER_VGOOD_USD', '10.0'))    # very good signal
    KELLY_TIER_PERFECT_USD = float(os.getenv('KELLY_TIER_PERFECT_USD', '15.0'))  # perfect signal (HARD MAX)
    KELLY_GOOD_STRENGTH = float(os.getenv('KELLY_GOOD_STRENGTH', '0.40'))      # strength >= -> good tier
    KELLY_VGOOD_STRENGTH = float(os.getenv('KELLY_VGOOD_STRENGTH', '0.65'))    # strength >= -> very-good tier
    KELLY_PERFECT_STRENGTH = float(os.getenv('KELLY_PERFECT_STRENGTH', '0.85'))  # strength >= -> perfect tier
    KELLY_W_EDGE = float(os.getenv('KELLY_W_EDGE', '0.35'))                    # weight: post-fee edge
    KELLY_W_PROB = float(os.getenv('KELLY_W_PROB', '0.25'))                    # weight: P(win)
    KELLY_W_GRADE = float(os.getenv('KELLY_W_GRADE', '0.20'))                  # weight: stability grade
    KELLY_W_WINRATE = float(os.getenv('KELLY_W_WINRATE', '0.20'))             # weight: realized strategy win-rate
    KELLY_EDGE_FULL = float(os.getenv('KELLY_EDGE_FULL', '0.25'))              # edge counted as max strength
    KELLY_WINRATE_PRIOR = float(os.getenv('KELLY_WINRATE_PRIOR', '0.45'))      # win-rate prior before enough trades
    KELLY_WINRATE_FULL_TRUST_N = int(os.getenv('KELLY_WINRATE_FULL_TRUST_N', '20'))  # trades to fully trust observed WR
    KELLY_MAX_FRACTION = float(os.getenv('KELLY_MAX_FRACTION', '0.25'))        # per-trade safety cap vs live balance

    # PORTFOLIO GUARD - stop the "allocates all funds immediately" drain. Keep a
    # cash reserve, cap how much of the portfolio is deployed, and cap how much
    # NEW money + how many NEW buys a single scan may place, so capital is left
    # for the good markets that appear later in the same scan.
    PORTFOLIO_GUARD_ENABLED = os.getenv('PORTFOLIO_GUARD_ENABLED', '1') == '1'
    PORTFOLIO_RESERVE_PCT = float(os.getenv('PORTFOLIO_RESERVE_PCT', '0.15'))  # always keep >= this % of portfolio in cash
    # Absolute-dollar cash floor: the bot never trades the balance below this
    # many dollars. Effective reserve = max(PORTFOLIO_RESERVE_PCT * portfolio,
    # BALANCE_RESERVE_USD). 0 = disabled (percentage reserve only).
    BALANCE_RESERVE_USD = float(os.getenv('BALANCE_RESERVE_USD', '0.0'))
    PORTFOLIO_MAX_DEPLOY_PCT = float(os.getenv('PORTFOLIO_MAX_DEPLOY_PCT', '0.85'))  # never deploy beyond this % of portfolio
    MAX_DEPLOY_PER_SCAN_PCT = float(os.getenv('MAX_DEPLOY_PER_SCAN_PCT', '0.30'))  # new $ per scan <= this % of portfolio
    MAX_BUYS_PER_SCAN = int(os.getenv('MAX_BUYS_PER_SCAN', '6'))               # max NEW buys placed in one scan

    # PER-STRATEGY SIZE MULTIPLIER - lean toward what wins, away from what loses.
    # UPDATED 2026-07-27 from the full 3-month trade audit (positions_mae_mfe):
    #   late_observed_no = +$13.69 over 23 pos (65% WR) -> the ONLY real winner,
    #     keep the 1.3 lean; peaker_cool basket also net-positive (untouched).
    #   peak_cluster was actually -$12.08 (14% WR) NOT positive -> the old 1.25
    #     "only net-positive" comment was STALE/WRONG; drop to 1.0 neutral so a
    #     losing basket is no longer up-sized. The prune + outer-TP fixes below
    #     do the real work on peak_cluster quality.
    #   late_observed_yes = -$9.62 (0% WR) -> keep it small at 0.6 (the new
    #     forecast-edge gate in overlay/entry_gate.py is what makes it "good").
    # 1.0 = neutral. Env vars still override every value.
    STRATEGY_SIZE_MULT = {
        'peak_cluster': float(os.getenv('SIZE_MULT_PEAK_CLUSTER', '1.0')),
        'late_observed_yes': float(os.getenv('SIZE_MULT_LATE_OBSERVED_YES', '0.6')),
        'late_observed_no': float(os.getenv('SIZE_MULT_LATE_OBSERVED_NO', '1.3')),
        'quick_flip': float(os.getenv('SIZE_MULT_QUICK_FLIP', '1.0')),
        # golden_no = audited fusion strategy (overlay/golden_gate.py). Skims the
        # Golden-Filter subset of late_observed_no + peaker_cool_basket into a
        # separately-named, boosted strategy. Same 1.3 lean as late_observed_no
        # so the bot up-sizes toward the +$332 / 68% WR core. Env overrides.
        'golden_no': float(os.getenv('SIZE_MULT_GOLDEN_NO', '1.3')),
    }

    # === GOLDEN FILTER + LATE-OBSERVED TIMING (overlay/golden_gate.py) =====
    # Audited fusion strategy: skims the durable winning core out of
    # late_observed_no + peaker_cool_basket into a separately-named
    # 'golden_no' strategy. Live-tunable from /settings; env overrides.
    # GOLDEN_NO_ENABLED off => identical pre-golden behaviour. Timing toggles
    # gate late_observed by days-to-resolution (same-day loses -> OFF).
    GOLDEN_NO_ENABLED = os.getenv('GOLDEN_NO_ENABLED', '1') == '1'
    GOLDEN_NO_SIDE_ONLY = os.getenv('GOLDEN_NO_SIDE_ONLY', '1') == '1'
    GOLDEN_MIN_DAYS_TO_RES = float(os.getenv('GOLDEN_MIN_DAYS_TO_RES', '1.0'))
    GOLDEN_ENTRY_MIN = float(os.getenv('GOLDEN_ENTRY_MIN', '0.50'))
    GOLDEN_ENTRY_MAX = float(os.getenv('GOLDEN_ENTRY_MAX', '0.80'))
    GOLDEN_EDGE_MIN = float(os.getenv('GOLDEN_EDGE_MIN', '0.10'))
    GOLDEN_EDGE_MAX = float(os.getenv('GOLDEN_EDGE_MAX', '0.50'))
    GOLDEN_GRADE_MAX = float(os.getenv('GOLDEN_GRADE_MAX', '0.90'))
    LATE_OBS_TIMING_SAMEDAY_ENABLED = os.getenv('LATE_OBS_TIMING_SAMEDAY_ENABLED', '0') == '1'
    LATE_OBS_TIMING_1D_ENABLED = os.getenv('LATE_OBS_TIMING_1D_ENABLED', '1') == '1'
    LATE_OBS_TIMING_2D_ENABLED = os.getenv('LATE_OBS_TIMING_2D_ENABLED', '1') == '1'
    LATE_OBS_TIMING_3D_ENABLED = os.getenv('LATE_OBS_TIMING_3D_ENABLED', '1') == '1'
    LATE_OBS_FETCH_AFTER_NEXT_DAY = os.getenv('LATE_OBS_FETCH_AFTER_NEXT_DAY', '1') == '1'

    # ML SIZING/VETO INFLUENCE - the ML engine (ml/decision_engine.py) is now
    # actually consulted on the trade path (it was wired but never called).
    # A SKIP with high confidence vetoes the buy; otherwise its confidence
    # scales size between MIN and MAX mult. Safe no-op when ML_API_KEY is unset
    # (the engine returns a local BUY@0.7 fallback, so nothing is blocked).
    ML_DECISION_ENABLED = os.getenv('ML_DECISION_ENABLED', '1') == '1'
    ML_VETO_CONF = float(os.getenv('ML_VETO_CONF', '0.66'))                    # SKIP with conf >= this vetoes the buy
    ML_SIZE_MIN_MULT = float(os.getenv('ML_SIZE_MIN_MULT', '0.7'))            # size mult at low ML confidence
    ML_SIZE_MAX_MULT = float(os.getenv('ML_SIZE_MAX_MULT', '1.2'))            # size mult at high ML confidence

    # ===================================================================
    # FEATURE TOGGLES (enable/disable without breaking anything)
    # ===================================================================
    SNIPER_ENABLED = os.getenv('SNIPER_ENABLED', '0') == '1'
    SPREAD_ENABLED = os.getenv('SPREAD_STRATEGY_ENABLED', '0') == '1'
    SELECTIVE_SNIPER_ENABLED = os.getenv('SELECTIVE_SNIPER_ENABLED', '0') == '1'
    # Early-mispricing / forecast-change sniper: buy a freshly mispriced bucket
    # before the book adjusts and flip on the correction (or hold if structural).
    # Enabled by default now that it is wired into the scan loop.
    QUICK_FLIP_ENABLED = os.getenv('QUICK_FLIP_ENABLED', '1') == '1'
    CORRELATION_ARB_ENABLED = os.getenv('CORRELATION_ARB_ENABLED', '0') == '1'
    # Demoted: forecast-only single-bucket bet. Off by default - the observed
    # strategy supersedes it. Flip to 1 to run it as a second opinion.
    CONFIDENT_ENABLED = os.getenv('CONFIDENT_ENABLED', '0') == '1'
    # Enabled by default: stability is a GRADE applied across strategies.
    STABILITY_ENABLED = os.getenv('STABILITY_ENABLED', '1') == '1'
    LOCKIN_ENABLED = os.getenv('LOCKIN_ENABLED', '1') == '1'
    ML_ENABLED = os.getenv('ML_ENABLED', '1') == '1'
    TELEGRAM_ENABLED = os.getenv('TELEGRAM_ENABLED', '1') == '1'
    COPY_TRADING_ENABLED = os.getenv('COPY_TRADING_ENABLED', '0') == '1'
    ADAPTIVE_EXIT_ENABLED = os.getenv('ADAPTIVE_EXIT_ENABLED', '1') == '1'
    AUTO_REDEEM_ENABLED = os.getenv('AUTO_REDEEM_ENABLED', '1') == '1'
    DRAWDOWN_GATE_ENABLED = os.getenv('DRAWDOWN_GATE_ENABLED', '1') == '1'

    # ===================================================================
    # LATE OBSERVED-TEMPERATURE STRATEGY (PRIMARY) - trade the locked extreme
    # Once the local day's peak/trough has passed, the observed max/min is a
    # hard floor/ceiling on the settled value while the book still prices stale
    # forecast uncertainty. YES the bucket that's locked in; NO the buckets that
    # observed data has made impossible. All gating is fee-aware.
    # NOTE (Req-28): late_observed (esp. the NO side) is the GOOD strategy - do
    # NOT retune it. Knobs below are left exactly as-is on purpose.
    # ===================================================================
    LATE_OBSERVED_ENABLED = os.getenv('LATE_OBSERVED_ENABLED', '1') == '1'
    LATE_OBSERVED_NO_SIDE = os.getenv('LATE_OBSERVED_NO_SIDE', '1') == '1'      # also buy NO on dead buckets
    LATE_OBSERVED_MIN_LOCK = float(os.getenv('LATE_OBSERVED_MIN_LOCK', '0.70'))  # min lock-confidence to trade
    LATE_OBSERVED_MIN_EDGE = float(os.getenv('LATE_OBSERVED_MIN_EDGE', '0.10'))  # post-fee probability cushion
    LATE_OBSERVED_MAX_YES_PRICE = float(os.getenv('LATE_OBSERVED_MAX_YES_PRICE', '0.95'))
    # Cheap-tail allowance for the HOLD-to-resolution primary strategy. Lower
    # than the global sellability floor because these legs never need to sell -
    # this is exactly the sub-5c tail the reference 90%-WR wallet lives in.
    LATE_OBSERVED_MIN_ENTRY_PRICE = float(os.getenv('LATE_OBSERVED_MIN_ENTRY_PRICE', '0.02'))
    LATE_OBSERVED_NO_MIN_PRICE = float(os.getenv('LATE_OBSERVED_NO_MIN_PRICE', '0.04'))
    LATE_OBSERVED_NO_MAX_PRICE = float(os.getenv('LATE_OBSERVED_NO_MAX_PRICE', '0.97'))
    # entry-band gate (overlay) -- data-driven profitable price windows
    LATE_OBS_YES_MIN_ENTRY = float(os.getenv('LATE_OBS_YES_MIN_ENTRY', '0.20'))
    LATE_OBS_YES_MAX_ENTRY = float(os.getenv('LATE_OBS_YES_MAX_ENTRY', '0.50'))
    LATE_OBS_NO_MIN_ENTRY = float(os.getenv('LATE_OBS_NO_MIN_ENTRY', '0.50'))
    LATE_OBS_NO_MAX_ENTRY = float(os.getenv('LATE_OBS_NO_MAX_ENTRY', '0.97'))
    ENTRY_BAND_GATE_ENABLED = os.getenv('ENTRY_BAND_GATE_ENABLED', 'true').lower() == 'true'
    LATE_OBSERVED_BASE_FRACTION = float(os.getenv('LATE_OBSERVED_BASE_FRACTION', '0.06'))
    LATE_OBSERVED_MAX_FRACTION = float(os.getenv('LATE_OBSERVED_MAX_FRACTION', '0.25'))
    LATE_OBSERVED_MAX_LEGS = int(os.getenv('LATE_OBSERVED_MAX_LEGS', '4'))
    # --- Signal-strength -> absolute-USD allocation ladder (replaces flat %-Kelly) ---
    LATE_OBSERVED_SIZE_FLOOR_USD = float(os.getenv('LATE_OBSERVED_SIZE_FLOOR_USD', '3.0'))   # weakest valid signal (~$3-4)
    LATE_OBSERVED_SIZE_MAX_USD = float(os.getenv('LATE_OBSERVED_SIZE_MAX_USD', '15.0'))      # strongest signal (fixed-$ HARD MAX)
    # BALANCE-AWARE MAX (added 2026-07-27): late_observed_no is the only proven
    # winner, so as the balance compounds we let its hard cap grow WITH the
    # balance instead of being frozen at a flat $15. Effective max per late-
    # observed leg = max(LATE_OBSERVED_SIZE_MAX_USD, LATE_OBSERVED_SIZE_MAX_PCT *
    # balance). Set LATE_OBSERVED_SIZE_MAX_PCT = 0 to disable and keep the flat
    # fixed cap only. 0.05 = up to 5% of balance for the strongest signal.
    LATE_OBSERVED_SIZE_MAX_PCT = float(os.getenv('LATE_OBSERVED_SIZE_MAX_PCT', '0.05'))
    LATE_OBSERVED_EDGE_FULL = float(os.getenv('LATE_OBSERVED_EDGE_FULL', '0.25'))            # post-fee edge counted as max strength
    LATE_OBSERVED_W_EDGE = float(os.getenv('LATE_OBSERVED_W_EDGE', '0.6'))                   # weight of edge in the strength score
    LATE_OBSERVED_W_GRADE = float(os.getenv('LATE_OBSERVED_W_GRADE', '0.4'))                 # weight of grade in the strength score
    LATE_OBSERVED_YES_MIN_LOCK = float(os.getenv('LATE_OBSERVED_YES_MIN_LOCK', '0.80'))      # YES leg needs this lock-confidence (vs NO)
    LATE_OBSERVED_YES_MIN_EDGE = float(os.getenv('LATE_OBSERVED_YES_MIN_EDGE', '0.14'))      # YES leg needs this post-fee edge (vs NO)

    # ===================================================================
    # CITY FILTER (which cities to trade - empty = all)
    # ===================================================================
    ENABLED_CITIES = [c.strip() for c in os.getenv('ENABLED_CITIES', '').split(',') if c.strip()]

    # ===================================================================
    # DRAWDOWN GATE - pause trading if drawdown exceeds threshold
    # ===================================================================
    MAX_DAILY_DRAWDOWN_PCT = float(os.getenv('MAX_DAILY_DRAWDOWN_PCT', '30'))
    MAX_WEEKLY_DRAWDOWN_PCT = float(os.getenv('MAX_WEEKLY_DRAWDOWN_PCT', '50'))
    DRAWDOWN_COOLDOWN_MINUTES = int(os.getenv('DRAWDOWN_COOLDOWN_MINUTES', '60'))

    # ===================================================================
    # LOCK-IN STRATEGY (buy near-certain outcomes at $0.90+ for safe profit)
    # ===================================================================
    LOCKIN_MIN_PRICE = float(os.getenv('LOCKIN_MIN_PRICE', '0.90'))
    LOCKIN_MIN_CONFIDENCE = float(os.getenv('LOCKIN_MIN_CONFIDENCE', '0.85'))
    LOCKIN_MAX_BET_PCT = float(os.getenv('LOCKIN_MAX_BET_PCT', '0.40'))

    # ===================================================================
    # STABILITY STRATEGY - trade only predictable city-days, adjacent buckets
    # ===================================================================
    STABILITY_MIN_SCORE = float(os.getenv('STABILITY_MIN_SCORE', '0.62'))        # predictable threshold
    STABILITY_NEIGHBOR_SPAN = int(os.getenv('STABILITY_NEIGHBOR_SPAN', '1'))     # +/-1 -> buy 3 buckets
    STABILITY_MAX_LEG_PRICE = float(os.getenv('STABILITY_MAX_LEG_PRICE', '0.60'))
    STABILITY_MIN_EDGE = float(os.getenv('STABILITY_MIN_EDGE', '0.08'))          # basket edge vs cost
    STABILITY_MAX_FRACTION = float(os.getenv('STABILITY_MAX_FRACTION', '0.25'))  # max % of balance per basket
    STABILITY_EARLY_EXIT_PRICE = float(os.getenv('STABILITY_EARLY_EXIT_PRICE', '0.85'))
    STABILITY_EXIT_HOURS_BEFORE = float(os.getenv('STABILITY_EXIT_HOURS_BEFORE', '1.0'))

    # ===================================================================
    # PEAK BASKET - forecast-only directional-peak strategy (DEMOTED/DEAD).
    # Superseded by PEAKER. Off by default; knobs kept so old envs don't break.
    # ===================================================================
    PEAK_BASKET_ENABLED = os.getenv('PEAK_BASKET_ENABLED', '0') == '1'
    PEAK_MIN_STABILITY = float(os.getenv('PEAK_MIN_STABILITY', '0.45'))
    PEAK_MAX_PEAK_PRICE = float(os.getenv('PEAK_MAX_PEAK_PRICE', '0.85'))
    PEAK_MAX_NEIGHBOR_PRICE = float(os.getenv('PEAK_MAX_NEIGHBOR_PRICE', '0.60'))
    PEAK_MAX_BASKET_COST = float(os.getenv('PEAK_MAX_BASKET_COST', '0.95'))
    PEAK_MIN_EDGE = float(os.getenv('PEAK_MIN_EDGE', '0.03'))
    PEAK_BASE_FRACTION = float(os.getenv('PEAK_BASE_FRACTION', '0.05'))
    PEAK_MAX_FRACTION = float(os.getenv('PEAK_MAX_FRACTION', '0.25'))
    PEAK_MIN_MODELS = int(os.getenv('PEAK_MIN_MODELS', '2'))
    PEAK_FEE_BUFFER = float(os.getenv('PEAK_FEE_BUFFER', '0.02'))
    PEAK_MIN_NET_PROFIT = float(os.getenv('PEAK_MIN_NET_PROFIT', '0.03'))

    # ===================================================================
    # BASKET QUALITY - predict the max temp, then buy an adjacent basket whose
    # COMBINED cost < BASKET_MAX_COST so ANY single winning leg nets profit.
    # ===================================================================
    BASKET_MAX_COST = float(os.getenv('BASKET_MAX_COST', '0.85'))            # sum(leg prices) must be below this
    BASKET_TIGHT_GRADE = float(os.getenv('BASKET_TIGHT_GRADE', '0.80'))      # grade >= this (with high conf) -> tight 2-leg basket
    BASKET_TIGHT_CONFIDENCE = float(os.getenv('BASKET_TIGHT_CONFIDENCE', '0.70'))  # center-bucket confidence >= this for tight basket

    # ===================================================================
    # SNIPER GATE - the lone cheap-tail sniper only fires on high-conviction,
    # stable city-days. Require a strong grade AND high model confidence.
    # ===================================================================
    SNIPER_MIN_GRADE = float(os.getenv('SNIPER_MIN_GRADE', '0.70'))         # stability grade required for a lone sniper buy
    SNIPER_MIN_CONFIDENCE = float(os.getenv('SNIPER_MIN_CONFIDENCE', '0.60'))  # model confidence required for a lone sniper buy
    SNIPER_MIN_PROBABILITY = float(os.getenv('SNIPER_MIN_PROBABILITY', '0.12'))

    # ===================================================================
    # QUICK_FLIP v3 (Req-28) - HIGH-confidence mispricing flip with a 10% profit
    # target and a PROFIT-ONLY exit (never cut at a loss/breakeven on the timer).
    # Now ALSO hunts mispriced NO tokens (QUICK_FLIP_NO_SIDE). Defaults tightened
    # so it triggers LESS and only on real, high-conviction mispricings: higher
    # confidence floor, smaller size, fewer per market. quick_flip.py reads each
    # knob via getattr with these same values.
    # ===================================================================
    QUICK_FLIP_MIN_DELTA_C = float(os.getenv('QUICK_FLIP_MIN_DELTA_C', '1.0'))            # min ensemble-mean move (C) across runs to signal
    QUICK_FLIP_MIN_EDGE = float(os.getenv('QUICK_FLIP_MIN_EDGE', '0.18'))                 # early-mispricing entry: model prob - price >= this (strict -> only real mispricings)
    QUICK_FLIP_MAX_PER_MARKET = int(os.getenv('QUICK_FLIP_MAX_PER_MARKET', '2'))          # cap flip candidates per market per scan
    QUICK_FLIP_MIN_CONFIDENCE = float(os.getenv('QUICK_FLIP_MIN_CONFIDENCE', '0.72'))     # Req-28: high-confidence only after boosts
    QUICK_FLIP_MAX_ENTRY = float(os.getenv('QUICK_FLIP_MAX_ENTRY', '0.85'))               # don't chase an already-priced bucket
    QUICK_FLIP_MIN_ENTRY = float(os.getenv('QUICK_FLIP_MIN_ENTRY', '0.03'))               # Req-28: skip sub-3c dust that can't be flipped
    QUICK_FLIP_MAX_HOLD_MIN = int(os.getenv('QUICK_FLIP_MAX_HOLD_MIN', '120'))            # convert to hold-to-resolution after this many minutes (never loss-cut)
    QUICK_FLIP_TARGET_ROI = float(os.getenv('QUICK_FLIP_TARGET_ROI', '10.0'))             # initial take-profit ROI% target (first profit rung)
    QUICK_FLIP_SIZE_PCT = float(os.getenv('QUICK_FLIP_SIZE_PCT', '0.03'))                 # Req-28: smaller base size (it was eating capital)
    QUICK_FLIP_MAX_SIZE_USD = float(os.getenv('QUICK_FLIP_MAX_SIZE_USD', '6.0'))          # Req-28: hard $ cap per flip lowered
    QUICK_FLIP_SIGNAL_COOLDOWN_MIN = int(os.getenv('QUICK_FLIP_SIGNAL_COOLDOWN_MIN', '30'))  # dedup: don't re-signal same bucket within N min
    QUICK_FLIP_WINDOW_MIN = int(os.getenv('QUICK_FLIP_WINDOW_MIN', '20'))                 # publish-window length (min) for the boost
    QUICK_FLIP_WINDOW_BOOST = float(os.getenv('QUICK_FLIP_WINDOW_BOOST', '0.10'))         # confidence boost inside the publish window
    QUICK_FLIP_STALE_BOOST = float(os.getenv('QUICK_FLIP_STALE_BOOST', '0.0'))            # confidence boost when the market price is stale
    QUICK_FLIP_STALE_EPS = float(os.getenv('QUICK_FLIP_STALE_EPS', '0.01'))               # |price-prev| below this counts as stale
    QUICK_FLIP_MAX_CONCURRENT = int(os.getenv('QUICK_FLIP_MAX_CONCURRENT', '3'))          # max simultaneous open flips (fewer, higher-quality)
    QUICK_FLIP_TIME_EXIT = os.getenv('QUICK_FLIP_TIME_EXIT', '1') == '1'                  # enforce the (profit-only) exit at the hold cap
    # --- Req-28 NO-side flips ---
    QUICK_FLIP_NO_SIDE = os.getenv('QUICK_FLIP_NO_SIDE', '1') == '1'                      # also hunt mispriced NO tokens for the 10% flip
    QUICK_FLIP_NO_MIN_EDGE = float(os.getenv('QUICK_FLIP_NO_MIN_EDGE', '0.10'))           # min edge for a NO-side flip candidate
    # --- Req-30 NEW-MARKET hunting: catch freshly-appeared mispricings early ---
    QUICK_FLIP_NEW_MARKET_BOOST = float(os.getenv('QUICK_FLIP_NEW_MARKET_BOOST', '0.10')) # confidence boost while a market is still "new"
    QUICK_FLIP_NEW_MARKET_WINDOW_MIN = float(os.getenv('QUICK_FLIP_NEW_MARKET_WINDOW_MIN', '60'))  # minutes a market counts as new
    # --- Req-27 PROFIT-ONLY LADDERED EXIT (trading/exit_policies.check_flip_exits) ---
    QUICK_FLIP_PROFIT_ONLY_EXIT = os.getenv('QUICK_FLIP_PROFIT_ONLY_EXIT', '1') == '1'    # never book a flip at a loss/breakeven on the timer
    QUICK_FLIP_USE_ML_EXIT = os.getenv('QUICK_FLIP_USE_ML_EXIT', '1') == '1'              # let the ML decide sell-small vs run-more
    QUICK_FLIP_MIN_BOOK_ROI_PCT = float(os.getenv('QUICK_FLIP_MIN_BOOK_ROI_PCT', '10.0')) # lowest profit rung we will book
    QUICK_FLIP_LADDER_MID_ROI_PCT = float(os.getenv('QUICK_FLIP_LADDER_MID_ROI_PCT', '20.0'))  # mid profit rung (flip_book_mid)
    QUICK_FLIP_LADDER_RUN_ROI_PCT = float(os.getenv('QUICK_FLIP_LADDER_RUN_ROI_PCT', '30.0'))  # let strong flips run toward this
    QUICK_FLIP_FORCE_BOOK_ROI_PCT = float(os.getenv('QUICK_FLIP_FORCE_BOOK_ROI_PCT', '30.0'))  # always book at/above this (don't round-trip a winner)
    QUICK_FLIP_STOP_LOSS_PCT = float(os.getenv('QUICK_FLIP_STOP_LOSS_PCT', '-5.0'))      # Req-29: book a quick-flip LOSS at this ROI% (quick +10% up / -5% down)
    QUICK_FLIP_BOOK_OR_CUT = os.getenv('QUICK_FLIP_BOOK_OR_CUT', '1') == '1'             # Req-30: cut a FLAT flip at the hold cap (OFF = let it ride to resolution, don't cut opportunities)
    QUICK_FLIP_USE_ML_PROFIT = os.getenv('QUICK_FLIP_USE_ML_PROFIT', '1') == '1'         # Req-30: at the +profit target, let ML decide BOOK vs HOLD-for-more (no ML -> book at target)

    # ===================================================================
    # Req-30 GLOBAL PROFIT CAP (any strategy). A position once ran 500% -> 0.
    # Above the cap, ML decides HOLD-to-settle vs BOOK now. If ML unavailable,
    # let it settle (ride to resolution) per spec.
    # ===================================================================
    PROFIT_CAP_ENABLED = os.getenv('PROFIT_CAP_ENABLED', '1') == '1'
    PROFIT_CAP_ROI_PCT = float(os.getenv('PROFIT_CAP_ROI_PCT', '300.0'))                 # ROI% ceiling (set 350 in Telegram if you want)
    # DETERMINISTIC by default: at the cap the position is BOOKED instantly to
    # LOCK the gain (fixes "350% cap never fired" round-trips). Applies to normal
    # singles incl. late_observed_no (harmless safety net -- NO tops out ~100% ROI
    # so it never actually fires) and late_observed_yes (books the round-trippers).
    # Baskets stay EXEMPT via PROFIT_CAP_EXEMPT_BASKETS. Flip ON to instead hand
    # capped winners to the ML, which may ride them past the cap.
    PROFIT_CAP_ML_OVERRIDE = os.getenv('PROFIT_CAP_ML_OVERRIDE', '0') == '1'

    # ===================================================================
    # PEAK_CLUSTER - parallel any-one-wins basket. Estimate the peak bucket
    # (argmax model probability), buy a window of adjacent buckets whose
    # COMBINED per-share cost stays below PEAK_CLUSTER_MAX_COST, so ANY single
    # winning leg pays $1 > cost = net profit after fees. HOLDS TO RESOLUTION.
    # MIN_LEGS is hard-floored to 3 in code so it is NEVER a 1-leg "cluster".
    # ===================================================================
    PEAK_CLUSTER_ENABLED = os.getenv('PEAK_CLUSTER_ENABLED', '1') == '1'
    PEAK_CLUSTER_SPAN = int(os.getenv('PEAK_CLUSTER_SPAN', '3'))                          # +/- buckets around the estimated peak
    PEAK_CLUSTER_MAX_COST = float(os.getenv('PEAK_CLUSTER_MAX_COST', '0.95'))             # combined per-share cost ceiling (the <95c mispricing gate: any single winning leg pays $1 > basket cost, covering the losers + profit)
    PEAK_CLUSTER_MIN_LEGS = int(os.getenv('PEAK_CLUSTER_MIN_LEGS', '3'))                  # minimum legs for a valid basket (never a 1-leg cluster)
    PEAK_CLUSTER_MAX_LEGS = int(os.getenv('PEAK_CLUSTER_MAX_LEGS', '7'))                  # 3-7 neighbouring buckets per the design
    PEAK_CLUSTER_MIN_EDGE = float(os.getenv('PEAK_CLUSTER_MIN_EDGE', '0.03'))             # combined prob - cost minimum
    PEAK_CLUSTER_MIN_CONF = float(os.getenv('PEAK_CLUSTER_MIN_CONF', '0.55'))             # min center-bucket confidence
    PEAK_CLUSTER_MAX_CENTER_PRICE = float(os.getenv('PEAK_CLUSTER_MAX_CENTER_PRICE', '0.85'))  # skip if the peak is already fully priced
    PEAK_CLUSTER_BASE_FRACTION = float(os.getenv('PEAK_CLUSTER_BASE_FRACTION', '0.05'))   # base % of balance per basket
    PEAK_CLUSTER_MAX_FRACTION = float(os.getenv('PEAK_CLUSTER_MAX_FRACTION', '0.20'))     # max % of balance per basket
    PEAK_CLUSTER_MAX_USD = float(os.getenv('PEAK_CLUSTER_MAX_USD', '15.0'))               # hard $ cap per basket
    PEAK_CLUSTER_TRADE_DECIDED = os.getenv('PEAK_CLUSTER_TRADE_DECIDED', '0') == '1'      # run inside the lock window? off by default
    # peak_cluster-ONLY selection toggles (never affect the peaker cool/warm
    # baskets). CONTIGUOUS = rebuild the basket as an unbroken temperature ladder
    # around the peak (proven winner, ON). PROB_BASED = also pull in any window
    # bucket with model prob >= PEAK_CLUSTER_PROB_MIN even if non-adjacent, to
    # catch a high-prob winner the contiguous run would skip (opt-in, OFF).
    PEAK_CLUSTER_CONTIGUOUS_ENABLED = os.getenv('PEAK_CLUSTER_CONTIGUOUS_ENABLED', '1') == '1'
    PEAK_CLUSTER_PROB_BASED_ENABLED = os.getenv('PEAK_CLUSTER_PROB_BASED_ENABLED', '1') == '1'
    PEAK_CLUSTER_PROB_MIN = float(os.getenv('PEAK_CLUSTER_PROB_MIN', '0.10'))
    # --- peak_cluster QUALITY (added 2026-07-27) --------------------------------
    # The 3-month audit shows peak_cluster at -$12.08 / 14% WR: it buys too many
    # dead ~1-3c legs that never win but still cost money. Two fixes, both
    # peak_cluster-ONLY (never touch the peaker cool/warm baskets):
    #  (1) PRUNE junk legs: drop any leg priced below PEAK_CLUSTER_LEG_MIN_PRICE
    #      from the basket before sizing (a 1-2c leg is lottery dust that just
    #      pads basket cost). The basket must still keep >= PEAK_CLUSTER_MIN_LEGS
    #      legs or it's skipped entirely.
    PEAK_CLUSTER_LEG_MIN_PRICE = float(os.getenv('PEAK_CLUSTER_LEG_MIN_PRICE', '0.12'))
    #  (2) OUTER-LEG partial take-profit (OPT-IN, default OFF): you can't ID the
    #      single winning leg mid-flight, and selling the winner caps the $1
    #      payout -- so this ONLY ever trims the *outer* (non-peak) legs of a
    #      basket once they've run, banking some cost back while the core/peak
    #      legs still hold to resolution for the full $1. Never sells the peak
    #      bucket. See trading/exit_policies.check_peak_cluster_outer_tp().
    PEAK_CLUSTER_OUTER_TP_ENABLED = os.getenv('PEAK_CLUSTER_OUTER_TP_ENABLED', '0') == '1'
    PEAK_CLUSTER_OUTER_TP_ROI = float(os.getenv('PEAK_CLUSTER_OUTER_TP_ROI', '0.60'))   # trim an outer leg once its ROI >= this (0.60 = +60%)
    PEAK_CLUSTER_OUTER_TP_FRACTION = float(os.getenv('PEAK_CLUSTER_OUTER_TP_FRACTION', '0.50'))  # sell this fraction of the outer leg

    # ===================================================================
    # SAFETY PEAK - DEAD (merged into PEAKER). Knobs kept so old envs don't break.
    # ===================================================================
    SAFETY_PEAK_ENABLED = os.getenv('SAFETY_PEAK_ENABLED', '0') == '1'
    SAFETY_PEAK_MIN_GRADE = float(os.getenv('SAFETY_PEAK_MIN_GRADE', '0.65'))
    SAFETY_PEAK_MIN_MODELS = int(os.getenv('SAFETY_PEAK_MIN_MODELS', '3'))
    SAFETY_PEAK_MAX_STD = float(os.getenv('SAFETY_PEAK_MAX_STD', '1.2'))
    SAFETY_PEAK_MIN_CONFIDENCE = float(os.getenv('SAFETY_PEAK_MIN_CONFIDENCE', '0.70'))
    SAFETY_PEAK_MAX_PEAK_PRICE = float(os.getenv('SAFETY_PEAK_MAX_PEAK_PRICE', '0.85'))
    SAFETY_PEAK_MAX_NEIGHBOR_PRICE = float(os.getenv('SAFETY_PEAK_MAX_NEIGHBOR_PRICE', '0.60'))
    SAFETY_PEAK_FEE_BUFFER = float(os.getenv('SAFETY_PEAK_FEE_BUFFER', '0.02'))
    SAFETY_PEAK_MIN_NET_PROFIT = float(os.getenv('SAFETY_PEAK_MIN_NET_PROFIT', '0.05'))
    SAFETY_PEAK_MIN_EDGE = float(os.getenv('SAFETY_PEAK_MIN_EDGE', '0.05'))
    SAFETY_PEAK_BASE_FRACTION = float(os.getenv('SAFETY_PEAK_BASE_FRACTION', '0.05'))
    SAFETY_PEAK_MAX_FRACTION = float(os.getenv('SAFETY_PEAK_MAX_FRACTION', '0.20'))
    SAFETY_PEAK_MAX_USD = float(os.getenv('SAFETY_PEAK_MAX_USD', '15.0'))
    SAFETY_PEAK_TRADE_DECIDED = os.getenv('SAFETY_PEAK_TRADE_DECIDED', '0') == '1'

    # ===================================================================
    # PEAKER (Req-28 REDESIGN) - MARKET-ANCHORED peak strategy. The market itself
    # prices the winning bucket (~>=60c implies ~60% win, ~40% upside). PEAKER
    # ANCHORS on the market's high-probability (favourite) bucket, then
    # CROSS-VALIDATES it with our model and only buys on CONFIRMATION. The edge
    # is the cool/warm BASKET: when our model peak == the market favourite AND
    # the trend is cooling, check the -1C neighbour; if peak + (-1C) combined
    # cost < PEAKER_MAX_COST (under 95c) buy BOTH as a grouped "peaker cool
    # basket". Same warming -> +1C "peaker warm basket". A bare-favourite SOLO buy
    # is ~breakeven (why peaker was "always losing"), so solo only fires on a
    # genuine model edge (PEAKER_SOLO_MIN_EDGE). All combined cost < PEAKER_MAX_COST
    # so any single winning leg nets profit after fees; HELD to resolution.
    # peaker.py reads every knob via getattr with these same defaults.
    # ===================================================================
    PEAKER_ENABLED = os.getenv('PEAKER_ENABLED', '1') == '1'
    PEAKER_MIN_GRADE = float(os.getenv('PEAKER_MIN_GRADE', '0.60'))                       # min stability grade to trade
    PEAKER_MIN_MODELS = int(os.getenv('PEAKER_MIN_MODELS', '3'))                          # min ensemble models agreeing
    PEAKER_MAX_STD = float(os.getenv('PEAKER_MAX_STD', '1.4'))                            # max ensemble spread (C)
    PEAKER_MIN_CONFIDENCE = float(os.getenv('PEAKER_MIN_CONFIDENCE', '0.62'))             # min peak-bucket confidence for a basket
    PEAKER_SOLO_MIN_CONFIDENCE = float(os.getenv('PEAKER_SOLO_MIN_CONFIDENCE', '0.80'))   # higher bar to buy the favourite SOLO (1 leg)
    # --- Req-28 market-anchoring + cross-validation ---
    PEAKER_MARKET_MIN_PRICE = float(os.getenv('PEAKER_MARKET_MIN_PRICE', '0.40'))         # market's favourite must be priced >= this to anchor on
    PEAKER_ALIGN_BUCKETS = int(os.getenv('PEAKER_ALIGN_BUCKETS', '1'))                    # our model peak must be within N buckets of the market favourite
    PEAKER_CONFIRM_RATIO = float(os.getenv('PEAKER_CONFIRM_RATIO', '0.85'))               # our model prob must be >= this x market price to CONFIRM
    PEAKER_SOLO_MIN_EDGE = float(os.getenv('PEAKER_SOLO_MIN_EDGE', '0.08'))               # solo (bare-favourite) buy needs a genuine model edge
    PEAKER_PEAK_BIAS_BUCKETS = int(os.getenv('PEAKER_PEAK_BIAS_BUCKETS', '1'))            # hot-bias correction: nudge the peak estimate down N buckets
    PEAKER_MAX_PEAK_PRICE = float(os.getenv('PEAKER_MAX_PEAK_PRICE', '0.85'))             # don't chase an already-fully-priced peak
    PEAKER_MAX_NEIGHBOR_PRICE = float(os.getenv('PEAKER_MAX_NEIGHBOR_PRICE', '0.60'))     # neighbour price cap
    PEAKER_MAX_COST = float(os.getenv('PEAKER_MAX_COST', '0.95'))                         # combined per-share cost ceiling (under 95c)
    PEAKER_FEE_BUFFER = float(os.getenv('PEAKER_FEE_BUFFER', '0.02'))                     # taker-fee headroom on the winning leg
    PEAKER_MIN_NET_PROFIT = float(os.getenv('PEAKER_MIN_NET_PROFIT', '0.03'))             # min net profit after fees (any-one-wins)
    PEAKER_MIN_EDGE = float(os.getenv('PEAKER_MIN_EDGE', '0.04'))                         # combined prob - basket cost minimum
    PEAKER_BASE_FRACTION = float(os.getenv('PEAKER_BASE_FRACTION', '0.05'))               # base % of balance per basket
    PEAKER_MAX_FRACTION = float(os.getenv('PEAKER_MAX_FRACTION', '0.20'))                 # max % of balance per basket
    PEAKER_MAX_USD = float(os.getenv('PEAKER_MAX_USD', '15.0'))                           # hard $ cap per basket
    PEAKER_PREFER_COOL = os.getenv('PEAKER_PREFER_COOL', '1') == '1'                      # bias to the winning COOL (-1) neighbour basket
    PEAKER_COOL_SIZE_MULT = float(os.getenv('PEAKER_COOL_SIZE_MULT', '1.35'))             # size the cool side up (it wins more)
    PEAKER_COOL_EDGE_RELAX = float(os.getenv('PEAKER_COOL_EDGE_RELAX', '0.02'))           # relax the cool side's edge gate slightly
    PEAKER_WARM_SIZE_MULT = float(os.getenv('PEAKER_WARM_SIZE_MULT', '0.7'))              # shrink the warm side (it loses more)
    PEAKER_TRADE_DECIDED = os.getenv('PEAKER_TRADE_DECIDED', '0') == '1'                  # run inside the lock window? off (forecast-based edge)

    # ===================================================================
    # THESIS-INVALIDATION EXIT - STRICT early exit. Most positions HOLD to
    # resolution; only a non-tail position whose ROI has COLLAPSED exits early.
    # ===================================================================
    THESIS_EXIT_ENABLED = os.getenv('THESIS_EXIT_ENABLED', '1') == '1'
    THESIS_EXIT_MAX_ROI_PCT = float(os.getenv('THESIS_EXIT_MAX_ROI_PCT', '-85.0'))        # exit ONLY if ROI <= this (very bad)
    THESIS_EXIT_MIN_ENTRY_PRICE = float(os.getenv('THESIS_EXIT_MIN_ENTRY_PRICE', '0.10')) # tails below this are HELD, never thesis-exited
    THESIS_EXIT_MIN_BID = float(os.getenv('THESIS_EXIT_MIN_BID', '0.02'))                 # need a real bid to exit into
    THESIS_EXIT_MIN_MINUTES_TO_CLOSE = float(os.getenv('THESIS_EXIT_MIN_MINUTES_TO_CLOSE', '60.0'))  # near-close positions are HELD

    # ===================================================================
    # LIQUIDITY-SWEEP EXIT (trading/exit_policies.check_liquidity_sweep_exits).
    # When a NON-basket position's thesis breaks and ROI collapses to <= -50%,
    # cut it FAST at the best immediately-available liquidity instead of letting
    # it bleed to -90%+. Evidence from the ledger: only ~4% of positions recover
    # once they hit -50% (so cutting there is correct), while ~43% recover at
    # -20% (so we deliberately do NOT cut early on noise). Baskets are EXEMPT
    # (any-one-wins structures ride to settlement, same as the thesis/profit-cap
    # exits). Fires earlier than THESIS_EXIT (-85%) so it is the primary cut.
    # ===================================================================
    LIQ_SWEEP_EXIT_ENABLED = os.getenv('LIQ_SWEEP_EXIT_ENABLED', '1') == '1'
    LIQ_SWEEP_TRIGGER_ROI_PCT = float(os.getenv('LIQ_SWEEP_TRIGGER_ROI_PCT', '-50.0'))    # cut at/below this ROI (thesis broken)
    LIQ_SWEEP_MIN_ENTRY_PRICE = float(os.getenv('LIQ_SWEEP_MIN_ENTRY_PRICE', '0.10'))     # tails below this are HELD (lottery legs)
    LIQ_SWEEP_MIN_BID = float(os.getenv('LIQ_SWEEP_MIN_BID', '0.02'))                     # need a real bid to sweep into
    LIQ_SWEEP_MIN_MINUTES_TO_CLOSE = float(os.getenv('LIQ_SWEEP_MIN_MINUTES_TO_CLOSE', '30.0'))  # near-close positions are HELD (settle)
    LIQ_SWEEP_EXEMPT_BASKETS = os.getenv('LIQ_SWEEP_EXEMPT_BASKETS', '1') == '1'          # baskets ride to settlement

    # ===================================================================
    # OUTCOME-DECIDED GATE - only HARD-skip a market once its measurement day is
    # FULLY OVER in the city's local time (value recorded, just awaiting UMA).
    # ===================================================================
    SKIP_DECIDED_MARKETS = os.getenv('SKIP_DECIDED_MARKETS', '1') == '1'
    HIGH_TEMP_LOCK_HOUR = int(os.getenv('HIGH_TEMP_LOCK_HOUR', '18'))       # local hour after which the day's HIGH is considered set
    LATE_OBSERVED_TRADE_DECIDED = os.getenv('LATE_OBSERVED_TRADE_DECIDED', '1') == '1'
    QUICK_FLIP_TRADE_DECIDED = os.getenv('QUICK_FLIP_TRADE_DECIDED', '1') == '1'
    PEAK_BASKET_TRADE_DECIDED = os.getenv('PEAK_BASKET_TRADE_DECIDED', '0') == '1'
    CONFIDENT_TRADE_DECIDED = os.getenv('CONFIDENT_TRADE_DECIDED', '0') == '1'

    # ===================================================================
    # STABILITY GRADE - stability is a GRADE (not a strategy): it scales
    # size and sets the exit for EVERY strategy.
    # ===================================================================
    GRADE_SIZING_ENABLED = os.getenv('GRADE_SIZING_ENABLED', '1') == '1'
    GRADE_NEUTRAL = float(os.getenv('GRADE_NEUTRAL', '0.60'))          # default grade when stability data is missing
    GRADE_MIN_TO_TRADE = float(os.getenv('GRADE_MIN_TO_TRADE', '0.35'))  # hard skip below this grade
    GRADE_SIZE_MIN_MULT = float(os.getenv('GRADE_SIZE_MIN_MULT', '0.30'))  # size multiplier at grade 0
    GRADE_SIZE_MAX_MULT = float(os.getenv('GRADE_SIZE_MAX_MULT', '1.25'))  # size multiplier at grade 1

    # ===================================================================
    # LIQUIDITY AWARENESS - weather books are thin & asymmetric BY DESIGN.
    # ===================================================================
    LIQUIDITY_GUARD_ENABLED = os.getenv('LIQUIDITY_GUARD_ENABLED', '1') == '1'   # read & adapt to the book
    LIQUIDITY_STRICT_BLOCK = os.getenv('LIQUIDITY_STRICT_BLOCK', '0') == '1'     # 0 = adapt (default), 1 = hard-skip failing books
    LIQUIDITY_THIN_SIZE_MULT = float(os.getenv('LIQUIDITY_THIN_SIZE_MULT', '0.7'))  # keep ~70% size on thin/wide books (trim ~30%)
    LIQUIDITY_BOOK_CACHE_SECONDS = int(os.getenv('LIQUIDITY_BOOK_CACHE_SECONDS', '30'))

    # ===================================================================
    # ADAPTIVE EXIT - analyze unfavorable markets and exit properly
    # ===================================================================
    ADAPTIVE_CHECK_INTERVAL = int(os.getenv('ADAPTIVE_CHECK_INTERVAL', '120'))
    ADAPTIVE_SELL_IF_EDGE_LOST = os.getenv('ADAPTIVE_SELL_IF_EDGE_LOST', '1') == '1'
    ADAPTIVE_MIN_HOLD_MINUTES = int(os.getenv('ADAPTIVE_MIN_HOLD_MINUTES', '10'))

    # ===================================================================
    # COPY TRADING (mirror reference wallet trades)
    # ===================================================================
    COPY_WALLET = os.getenv('COPY_WALLET', '0x594edb9112f526fa6a80b8f858a6379c8a2c1c11')
    COPY_SCALE_FACTOR = float(os.getenv('COPY_SCALE_FACTOR', '0.01'))
    COPY_POLL_INTERVAL = int(os.getenv('COPY_POLL_INTERVAL', '30'))

    # ===================================================================
    # MULTI-OUTCOME SPREAD STRATEGY
    # ===================================================================
    SPREAD_NEIGHBOR_DECAY = float(os.getenv('SPREAD_NEIGHBOR_DECAY', '0.4'))
    SPREAD_MAX_COST = float(os.getenv('SPREAD_MAX_COST', '1.50'))

    # ===================================================================
    # SCAN SETTINGS
    # ===================================================================
    SCAN_INTERVAL_SECONDS = int(os.getenv('SCAN_INTERVAL_SECONDS', '60'))
    SCAN_DAYS_AHEAD = int(os.getenv('SCAN_DAYS_AHEAD', '3'))
    # Req-30 SUMMARY TIMER - push a periodic Telegram status summary every N
    # minutes (0 = off). Set live from /settings (e.g. 15 / 30 / 60).
    SUMMARY_INTERVAL_MIN = int(os.getenv('SUMMARY_INTERVAL_MIN', '0'))

    # ===================================================================
    # PAPER-REALISM - make the dry run behave like real trading
    # ===================================================================
    PAPER_REALISTIC_FILL = os.getenv('PAPER_REALISTIC_FILL', '1') == '1'         # walk the real ask ladder for paper buys
    PAPER_SETTLE_BY_WEATHER = os.getenv('PAPER_SETTLE_BY_WEATHER', '0') == '1'   # weather is CONFIRMATION-only, never the source of truth
    PAPER_PRECLOSE_LOCK_PCT = float(os.getenv('PAPER_PRECLOSE_LOCK_PCT', '0.95'))  # venue price >= this in the final minutes => 'win likely'
    PAPER_PRECLOSE_WINDOW_MIN = float(os.getenv('PAPER_PRECLOSE_WINDOW_MIN', '2'))  # how many minutes before close to flag the lock
    PAPER_TRADE_LOG = os.getenv('PAPER_TRADE_LOG', 'data/paper_trades.jsonl')    # structured per-trade audit log
    PAPER_FREEZE_ON_BAD_PRICE = os.getenv('PAPER_FREEZE_ON_BAD_PRICE', '1') == '1'  # keep last good price instead of writing 0

    # ===================================================================
    # RESOLUTION-STATION VERIFICATION
    # ===================================================================
    RESOLUTION_VERIFY_ENABLED = os.getenv('RESOLUTION_VERIFY_ENABLED', '1') == '1'
    RESOLUTION_VERIFY_MIN_CONF = float(os.getenv('RESOLUTION_VERIFY_MIN_CONF', '0.6'))
    RESOLUTION_SKIP_ON_UNKNOWN = os.getenv('RESOLUTION_SKIP_ON_UNKNOWN', '0') == '1'
    ML_RESPONSES_URL = os.getenv('ML_RESPONSES_URL', 'https://api.freemodel.dev/v1')
    ML_VERIFY_MODEL = os.getenv('ML_VERIFY_MODEL', 'gpt-5.4-mini')

    # ===================================================================
    # TELEGRAM (optional notifications)
    # ===================================================================
    TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID', '')

    # ===================================================================
    # ML DECISION ENGINE (GPT-5.5 via Freemodel)
    # ===================================================================
    ML_API_URL = os.getenv('ML_API_URL', 'https://api.freemodel.dev/v1')
    ML_API_KEY = os.getenv('ML_API_KEY', '')
    # Decision model: used for the FREQUENT per-market / per-position calls. Default
    # gpt-5.4-mini = fast + cheap. The heavy reasoning model (gpt-5.5) is reserved
    # for the occasional /mlanalysis narrative (ML_ANALYSIS_MODEL) so the scan loop
    # stays snappy and the bot isn't waiting ~7s on every market.
    ML_MODEL = os.getenv('ML_MODEL', 'gpt-5.4-mini')
    ML_ANALYSIS_MODEL = os.getenv('ML_ANALYSIS_MODEL', 'gpt-5.5')
    # PROVIDER PROFILE (2026-07-27): selects the wire-format used to talk to the
    # endpoint at ML_API_URL. 'openai_compatible' works for OpenAI, Groq,
    # Together, OpenRouter, Fireworks, vLLM, LM Studio, etc. Other presets:
    # 'openai', 'anthropic', 'google_gemini', 'cohere', 'ollama'. Set it in the
    # env or via Telegram /mlsetup. ML_LOCKED_PROFILE (set by /mlsetup) pins the
    # choice and wins over ML_PROVIDER_PROFILE; /mlreset clears the lock. This is
    # why the old default endpoint silently never fired: api.freemodel.dev is
    # dead -- point ML_API_URL at a live provider and pick the matching profile.
    ML_PROVIDER_PROFILE = os.getenv('ML_PROVIDER_PROFILE', 'openai_compatible')
    ML_LOCKED_PROFILE = os.getenv('ML_LOCKED_PROFILE', '')
    # /mlanalysis on/off (the LLM narrative report). When OFF a local heuristic
    # report is used instead (no API call).
    ML_ANALYSIS_ENABLED = os.getenv('ML_ANALYSIS_ENABLED', '1') == '1'
    # The Freemodel gpt-5.x models are REASONING models: they emit a <think>...</think>
    # block (~7s) BEFORE the JSON answer. Give them enough tokens + time, otherwise
    # the JSON truncates / the call times out and the bot silently falls back to the
    # local rules even though the key is set.
    ML_QUERY_TIMEOUT = float(os.getenv('ML_QUERY_TIMEOUT', '30'))
    ML_DECISION_MAX_TOKENS = int(os.getenv('ML_DECISION_MAX_TOKENS', '700'))
    ML_ANALYSIS_MAX_TOKENS = int(os.getenv('ML_ANALYSIS_MAX_TOKENS', '1200'))
    # Extra ML wiring (Req-31): let ML review OPEN positions for an early HOLD/SELL,
    # and prioritise which cities get evaluated first each scan (ordering only).
    ML_REVIEW_POSITIONS = os.getenv('ML_REVIEW_POSITIONS', '1') == '1'
    ML_REVIEW_SELL_CONF = float(os.getenv('ML_REVIEW_SELL_CONF', '0.72'))   # only a CONFIDENT SELL acts
    ML_REVIEW_MIN_HOLD_MIN = float(os.getenv('ML_REVIEW_MIN_HOLD_MIN', '20'))
    ML_REVIEW_MIN_MTC_MIN = float(os.getenv('ML_REVIEW_MIN_MTC_MIN', '45'))
    ML_REVIEW_MAX_PER_SCAN = int(os.getenv('ML_REVIEW_MAX_PER_SCAN', '6'))
    ML_SELECT_MARKETS = os.getenv('ML_SELECT_MARKETS', '1') == '1'

    # ===================================================================
    # LOGGING
    # ===================================================================
    LOG_FILE = os.getenv('LOG_FILE', 'data/weather_bot.log')
    LOG_LEVEL = os.getenv('LOG_LEVEL', 'INFO')

    # ===================================================================
    # REFERENCE TRADERS (for analysis)
    # ===================================================================
    REFERENCE_TRADERS = [
        '0x594edb9112f526fa6a80b8f858a6379c8a2c1c11',
        '0x331bf91c132af9d921e1908ca0979363fc47193f',
        '0x15ceffed7bf820cd2d90f90ea24ae9909f5cd5fa',
    ]

    # ===================================================================
    # HELPERS
    # ===================================================================
    @classmethod
    def is_paper(cls) -> bool:
        return cls.TRADING_MODE.lower() == 'paper'

    @classmethod
    def is_live(cls) -> bool:
        return cls.TRADING_MODE.lower() == 'live'

    @classmethod
    def is_live_ready(cls) -> bool:
        """Check if all credentials for live trading are set."""
        pk = cls.POLY_PRIVATE_KEY.strip() if cls.POLY_PRIVATE_KEY else ''
        return bool(pk)

    @classmethod
    def get_clob_url(cls) -> str:
        if cls.CLOB_RELAY_URL:
            return cls.CLOB_RELAY_URL.rstrip('/')
        return cls.CLOB_API_URL

    @classmethod
    def get_funder_address(cls) -> str:
        """Resolve funder address (same logic as polymarket-bot-v2)."""
        if cls.POLY_FUNDER_ADDRESS and cls.POLY_FUNDER_ADDRESS.strip():
            return cls.POLY_FUNDER_ADDRESS.strip()
        if cls.POLY_SIGNATURE_TYPE == 2:
            if cls.POLY_PROXY_WALLET and cls.POLY_PROXY_WALLET.strip():
                return cls.POLY_PROXY_WALLET.strip()
            return ''
        if cls.POLY_SIGNATURE_TYPE == 0:
            return cls.derive_wallet_address()
        return ''

    @classmethod
    def derive_wallet_address(cls) -> str:
        pk = cls.POLY_PRIVATE_KEY.strip() if cls.POLY_PRIVATE_KEY else ''
        if not pk:
            return ''
        try:
            from eth_account import Account
            if not pk.startswith('0x'):
                pk = '0x' + pk
            return Account.from_key(pk).address
        except Exception:
            return ''

    @classmethod
    def print_status(cls):
        mode = 'PAPER (DRY-RUN)' if cls.is_paper() else 'LIVE'
        print(f"\n{'='*60}")
        print(f"WEATHER SNIPER v{cls.VERSION} - {cls.VERSION_NAME}")
        print(f"{'='*60}")
        print(f"Mode:        {mode}")
        print(f"Balance:     ${cls.STARTING_BALANCE:.2f} pUSD")
        print(f"Trading:     {'ENABLED' if cls.TRADING_ENABLED else 'DISABLED (press Start)'}")
        print(f"Primary:     LateObserved {'ON' if cls.LATE_OBSERVED_ENABLED else 'OFF'} "
              f"(NO-side {'ON' if cls.LATE_OBSERVED_NO_SIDE else 'OFF'}, "
              f"size ${cls.LATE_OBSERVED_SIZE_FLOOR_USD:.0f}-${cls.LATE_OBSERVED_SIZE_MAX_USD:.0f})")
        print(f"Kelly:       factor-sizing {'ON' if cls.KELLY_FACTOR_SIZING_ENABLED else 'OFF'} "
              f"(tiers ${cls.KELLY_TIER_BASE_USD:.0f}/${cls.KELLY_TIER_GOOD_USD:.0f}/"
              f"${cls.KELLY_TIER_VGOOD_USD:.0f}/${cls.KELLY_TIER_PERFECT_USD:.0f})")
        print(f"QuickFlip:   {'ON' if cls.QUICK_FLIP_ENABLED else 'OFF'} "
              f"(edge>={cls.QUICK_FLIP_MIN_EDGE:.0%} conf>={cls.QUICK_FLIP_MIN_CONFIDENCE:.0%} "
              f"target {cls.QUICK_FLIP_TARGET_ROI:.0f}% NO-side={'Y' if cls.QUICK_FLIP_NO_SIDE else 'N'} "
              f"max {cls.QUICK_FLIP_MAX_CONCURRENT}@${cls.QUICK_FLIP_MAX_SIZE_USD:.0f})")
        print(f"PeakCluster: {'ON' if cls.PEAK_CLUSTER_ENABLED else 'OFF'} "
              f"(span+/-{cls.PEAK_CLUSTER_SPAN}, {cls.PEAK_CLUSTER_MIN_LEGS}-{cls.PEAK_CLUSTER_MAX_LEGS} legs, cost<{cls.PEAK_CLUSTER_MAX_COST}, HOLD)")
        print(f"Peaker:      {'ON' if getattr(cls, 'PEAKER_ENABLED', True) else 'OFF'} "
              f"(market-anchored>={getattr(cls, 'PEAKER_MARKET_MIN_PRICE', 0.40):.0%}, confirm x{getattr(cls, 'PEAKER_CONFIRM_RATIO', 0.85)}, "
              f"cool/warm basket<{getattr(cls, 'PEAKER_MAX_COST', 0.95)}, HOLD)")
        print(f"ThesisExit:  {'ON' if cls.THESIS_EXIT_ENABLED else 'OFF'} "
              f"(only ROI<={cls.THESIS_EXIT_MAX_ROI_PCT:.0f}% & entry>={cls.THESIS_EXIT_MIN_ENTRY_PRICE})")
        print(f"Min Edge:    {cls.MIN_EDGE_TO_ENTER*100:.0f}% | fee-aware taker={cls.ASSUME_TAKER_FILLS}")
        print(f"Liquidity:   {'STRICT' if cls.LIQUIDITY_STRICT_BLOCK else 'adaptive'} (thin x{cls.LIQUIDITY_THIN_SIZE_MULT})")
        print(f"Scan:        every {cls.SCAN_INTERVAL_SECONDS}s")
        print(f"{'='*60}\n")
