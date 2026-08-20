"""
Runtime settings store -- change the bot's behavior live (from Telegram) without
editing .env or restarting. Overrides are applied as attributes on `Config`
(read at call-time across the code) and persisted to data/runtime_settings.json
so they survive restarts (load_into_config() re-applies them at startup, so they
also reach strategy objects that cache their gates in __init__).

Three things are exposed:
  BOOL_KEYS -- on/off toggles (every strategy enable + operational switches)
  NUM_KEYS  -- numeric gates with (min, max, step, is_int)
  GROUPS    -- ordered categories the Telegram /settings panel renders as tabs

Req-27: expanded from the original ~10 toggles / ~12 gates to cover EVERY tunable
knob (all strategies incl. PEAKER + the profit-only flip ladder + YES gates),
grouped into tabs so the panel is browsable instead of one giant +/- wall.
"""

import json
import os
from typing import Tuple, Dict, List

from config import Config
from logger import log

# Req-33: ensure newer toggles default sensibly on Config even before any
# persisted override is loaded, so the Telegram panel shows the correct state
# AND the code's getattr() default matches it. Done here to avoid a full
# config.py rewrite -- mirror into config.py when convenient.
if not hasattr(Config, 'QUICK_FLIP_HARD_STOP_ENABLED'):
    Config.QUICK_FLIP_HARD_STOP_ENABLED = os.getenv('QUICK_FLIP_HARD_STOP_ENABLED', '1') == '1'

# Req-P3/P2b/P1/P2: default the new tunables on Config so the panel + getattr()
# defaults agree even before any persisted override loads.
_NEW_DEFAULTS = {
    'EXPORT_PERIODIC_ENABLED': os.getenv('EXPORT_PERIODIC_ENABLED', '0') == '1',
    'EXPORT_PERIODIC_HOURS': int(os.getenv('EXPORT_PERIODIC_HOURS', '6')),
    'EXPORT_DISK_GUARD_MB': int(os.getenv('EXPORT_DISK_GUARD_MB', '400')),
    'P2B_ENTRY_CEILING': float(os.getenv('P2B_ENTRY_CEILING', '0.90')),
    'P2B_MIN_ROOM_USD': float(os.getenv('P2B_MIN_ROOM_USD', '0.10')),
    'P2B_FORCE_EXIT_ENABLED': os.getenv('P2B_FORCE_EXIT_ENABLED', '1') == '1',
    'PAPER_EXIT_SLIPPAGE_PCT': float(os.getenv('PAPER_EXIT_SLIPPAGE_PCT', '1.5')),
    'PHANTOM_GUARD_GOLDEN_ONLY': os.getenv('PHANTOM_GUARD_GOLDEN_ONLY', '1') == '1',
    'GOLDEN_NO_SAMEDAY_ENABLED': os.getenv('GOLDEN_NO_SAMEDAY_ENABLED', '0') == '1',
    'GOLDEN_MIN_DAYS_TO_RES': float(os.getenv('GOLDEN_MIN_DAYS_TO_RES', '1.0')),
    'GOLDEN_ENTRY_MIN': float(os.getenv('GOLDEN_ENTRY_MIN', '0.50')),
    'GOLDEN_ENTRY_MAX': float(os.getenv('GOLDEN_ENTRY_MAX', '0.80')),
    'GOLDEN_EDGE_MIN': float(os.getenv('GOLDEN_EDGE_MIN', '0.10')),
    'GOLDEN_EDGE_MAX': float(os.getenv('GOLDEN_EDGE_MAX', '0.50')),
    'GOLDEN_GRADE_MAX': float(os.getenv('GOLDEN_GRADE_MAX', '0.90')),
    'VPS_OFFLOAD_ENABLED': os.getenv('VPS_OFFLOAD_ENABLED', '0') == '1',
    'VPS_OFFLOAD_INTERVAL_HOURS': float(os.getenv('VPS_OFFLOAD_INTERVAL_HOURS', '12')),
    'VPS_OFFLOAD_BATCH_LINES': int(os.getenv('VPS_OFFLOAD_BATCH_LINES', '2000')),
    'NOTIFY_WEATHER_SOURCE': os.getenv('NOTIFY_WEATHER_SOURCE', '1') == '1',
    'NOTIFY_VPS_HEALTH': os.getenv('NOTIFY_VPS_HEALTH', '1') == '1',
    'NOTIFY_VPS_OFFLOAD': os.getenv('NOTIFY_VPS_OFFLOAD', '1') == '1',
    'NOTIFY_ENDPOINT_COOLDOWN': os.getenv('NOTIFY_ENDPOINT_COOLDOWN', '0') == '1',
    'NOTIFY_ML_STATUS': os.getenv('NOTIFY_ML_STATUS', '0') == '1',
    # Req-27 #6: per-model Open-Meteo fetch toggles (default ON = green tick).
    'WX_MODEL_ECMWF_IFS_ENABLED': os.getenv('WX_MODEL_ECMWF_IFS_ENABLED', '1') == '1',
    'WX_MODEL_ECMWF_IFS025_ENABLED': os.getenv('WX_MODEL_ECMWF_IFS025_ENABLED', '1') == '1',
    'WX_MODEL_ECMWF_IFS04_ENABLED': os.getenv('WX_MODEL_ECMWF_IFS04_ENABLED', '1') == '1',
    'WX_MODEL_GFS_SEAMLESS_ENABLED': os.getenv('WX_MODEL_GFS_SEAMLESS_ENABLED', '1') == '1',
    'WX_MODEL_ICON_SEAMLESS_ENABLED': os.getenv('WX_MODEL_ICON_SEAMLESS_ENABLED', '1') == '1',
    'WX_MODEL_JMA_SEAMLESS_ENABLED': os.getenv('WX_MODEL_JMA_SEAMLESS_ENABLED', '1') == '1',
    'WX_MODEL_GEM_SEAMLESS_ENABLED': os.getenv('WX_MODEL_GEM_SEAMLESS_ENABLED', '1') == '1',
    # Req-27 #g: ML veto batching (aggregate N legs into one veto call).
    'ML_VETO_BATCH_ENABLED': os.getenv('ML_VETO_BATCH_ENABLED', '0') == '1',
    'ML_VETO_GROUP_SIZE': int(os.getenv('ML_VETO_GROUP_SIZE', '1')),
    # Req-27 #c/#2: per-stream VPS data handling + extra VPS controls.
    'VPS_HANDLING_PAPER_TRADES': os.getenv('VPS_HANDLING_PAPER_TRADES', 'full'),
    'VPS_HANDLING_MAE': os.getenv('VPS_HANDLING_MAE', 'full'),
    'VPS_HANDLING_TIMESERIES': os.getenv('VPS_HANDLING_TIMESERIES', 'full'),
    'VPS_HANDLING_WEATHER_TRACE': os.getenv('VPS_HANDLING_WEATHER_TRACE', 'full'),
    'VPS_PULL_ON_OFFLOAD': os.getenv('VPS_PULL_ON_OFFLOAD', '0') == '1',
    'VPS_ANALYSIS_MERGE_ENABLED': os.getenv('VPS_ANALYSIS_MERGE_ENABLED', '1') == '1',
    # 2026-08-20: advanced weather pipeline (data/weather/) customisation.
    'WEATHER_PIPELINE_ENABLED': os.getenv('WEATHER_PIPELINE_ENABLED', '0') == '1',
    'WEATHER_EXECUTION_MODE': os.getenv('WEATHER_EXECUTION_MODE', 'auto'),
    'WEATHER_SOURCE_LOCATION': os.getenv('WEATHER_SOURCE_LOCATION', ''),
    'WEATHER_SRC_OPEN_METEO_ENABLED': os.getenv('WEATHER_SRC_OPEN_METEO_ENABLED', '1') == '1',
    'WEATHER_SRC_OPENWEATHER_ENABLED': os.getenv('WEATHER_SRC_OPENWEATHER_ENABLED', '1') == '1',
    'WEATHER_SRC_WEATHERAPI_ENABLED': os.getenv('WEATHER_SRC_WEATHERAPI_ENABLED', '1') == '1',
    'WEATHER_SRC_VISUALCROSSING_ENABLED': os.getenv('WEATHER_SRC_VISUALCROSSING_ENABLED', '1') == '1',
    'WEATHER_SRC_NWS_ENABLED': os.getenv('WEATHER_SRC_NWS_ENABLED', '1') == '1',
    'WEATHER_PIPELINE_FORECAST_DAYS': int(os.getenv('WEATHER_PIPELINE_FORECAST_DAYS', '3')),
}
for _k, _v in _NEW_DEFAULTS.items():
    if not hasattr(Config, _k):
        setattr(Config, _k, _v)

SETTINGS_PATH = 'data/runtime_settings.json'

# -- On/off toggles exposed to Telegram (tick boxes) ----------------------
BOOL_KEYS = [
    # master + per-strategy enables
    'TRADING_ENABLED',
    'LATE_OBSERVED_ENABLED', 'LATE_OBSERVED_NO_SIDE',
    'QUICK_FLIP_ENABLED', 'PEAK_CLUSTER_ENABLED', 'PEAKER_ENABLED',
    'CONFIDENT_ENABLED', 'SNIPER_ENABLED', 'SPREAD_ENABLED', 'STABILITY_ENABLED',
    # ml / ops
    'ML_ENABLED', 'ML_DECISION_ENABLED', 'ML_ANALYSIS_ENABLED',
    'ML_REVIEW_POSITIONS', 'ML_SELECT_MARKETS', 'AUTO_REDEEM_ENABLED',
    'PORTFOLIO_GUARD_ENABLED', 'DRAWDOWN_GATE_ENABLED',
    # quick-flip exit behaviour
    'QUICK_FLIP_PROFIT_ONLY_EXIT', 'QUICK_FLIP_USE_ML_EXIT',
    'QUICK_FLIP_BOOK_OR_CUT', 'QUICK_FLIP_USE_ML_PROFIT',
    'QUICK_FLIP_HARD_STOP_ENABLED',
    # global ML profit cap
    'PROFIT_CAP_ENABLED', 'PROFIT_CAP_ML_OVERRIDE',
    # peaker / cluster behaviour
    'PEAKER_PREFER_COOL', 'PEAKER_TRADE_DECIDED', 'PEAK_CLUSTER_TRADE_DECIDED',
    'PEAK_CLUSTER_CONTIGUOUS_ENABLED', 'PEAK_CLUSTER_PROB_BASED_ENABLED',
    'PEAK_CLUSTER_OUTER_TP_ENABLED',
    # advanced weather pipeline / master brain
    'WEATHER_SRC_ECMWF_DIRECT_ENABLED', 'WEATHER_SRC_ECMWF_AIFS_ENABLED',
    'WEATHER_MASTER_ENABLED', 'WEATHER_SELECT_SUPPRESS_ENABLED',
    # exits / liquidity / gating
    'THESIS_EXIT_ENABLED', 'LIQUIDITY_GUARD_ENABLED', 'LIQUIDITY_STRICT_BLOCK',
    'GRADE_SIZING_ENABLED', 'SKIP_DECIDED_MARKETS',
    # entry-band gate + research data capture (overlays)
    'ENTRY_BAND_GATE_ENABLED', 'MAE_MFE_LOGGING_ENABLED',
    # global longshot floor (overlay/entry_gate)
    'GLOBAL_MIN_ENTRY_GATE_ENABLED',
    # weather overlays: last-good observed cache + health watcher
    'OBSERVED_CACHE_ENABLED', 'WEATHER_TRACE_ENABLED',
    # liquidity-sweep exit (fast cut at -50% into available liquidity)
    'LIQ_SWEEP_EXIT_ENABLED', 'LIQ_SWEEP_EXEMPT_BASKETS',
    # golden_no fusion strategy + late-observed timing (overlay/golden_gate.py)
    'GOLDEN_NO_ENABLED', 'GOLDEN_NO_SIDE_ONLY',
    'LATE_OBS_TIMING_SAMEDAY_ENABLED', 'LATE_OBS_TIMING_1D_ENABLED',
    'LATE_OBS_TIMING_2D_ENABLED', 'LATE_OBS_TIMING_3D_ENABLED',
    'LATE_OBS_FETCH_AFTER_NEXT_DAY',
    # P3 export + P2b/P1 per-strategy + fill-model toggles
    'EXPORT_PERIODIC_ENABLED',
    'P2B_FORCE_EXIT_ENABLED', 'PHANTOM_GUARD_GOLDEN_ONLY',
    'GOLDEN_NO_SAMEDAY_ENABLED',
    # VPS edge-node data offload
    'VPS_OFFLOAD_ENABLED',
    # Telegram event notifications (state-change only, rate-limited)
    'NOTIFY_WEATHER_SOURCE', 'NOTIFY_VPS_HEALTH', 'NOTIFY_VPS_OFFLOAD',
    'NOTIFY_ENDPOINT_COOLDOWN', 'NOTIFY_ML_STATUS',
    # Req-27 #6: per-model weather fetch toggles (green tick ON / red X OFF)
    'WX_MODEL_ECMWF_IFS_ENABLED', 'WX_MODEL_ECMWF_IFS025_ENABLED',
    'WX_MODEL_ECMWF_IFS04_ENABLED', 'WX_MODEL_GFS_SEAMLESS_ENABLED',
    'WX_MODEL_ICON_SEAMLESS_ENABLED', 'WX_MODEL_JMA_SEAMLESS_ENABLED',
    'WX_MODEL_GEM_SEAMLESS_ENABLED',
    # Req-27 #g ML veto batching + extra VPS controls
    'ML_VETO_BATCH_ENABLED', 'VPS_PULL_ON_OFFLOAD', 'VPS_ANALYSIS_MERGE_ENABLED',
    # 2026-08-19 upgrade: VPS master switch + sub-toggles (master OFF = ALL VPS
    # services off, incl. weather proxy -> fetch goes direct to Open-Meteo)
    'VPS_SERVICES_ENABLED', 'VPS_WEATHER_PROXY_ENABLED', 'VPS_PULL_ON_ANALYSIS',
    # 2026-08-19: new grade+edge engine, run-id, phantom guard, new strategies
    'GRADE_EDGE_ENGINE_ENABLED', 'RUN_ID_ENABLED', 'PHANTOM_GUARD_ALL_STRATEGIES',
    'LATE_OBSERVED_REMASTER_ENABLED', 'LATE_OBS_NO_ARB_ENABLED',
    # 2026-08-20: advanced weather pipeline enable + per-provider toggles
    'WEATHER_PIPELINE_ENABLED',
    'WEATHER_SRC_OPEN_METEO_ENABLED', 'WEATHER_SRC_OPENWEATHER_ENABLED',
    'WEATHER_SRC_WEATHERAPI_ENABLED', 'WEATHER_SRC_VISUALCROSSING_ENABLED',
    'WEATHER_SRC_NWS_ENABLED',
]

# -- Numeric gates: key -> (min, max, step, is_int) ---------------------
NUM_KEYS: Dict[str, tuple] = {
    # account
    'STARTING_BALANCE':            (10, 1000000, 50, False),
    # ops
    'SUMMARY_INTERVAL_MIN':        (0, 240, 15, True),
    'WEATHER_PIPELINE_FORECAST_DAYS': (1, 7, 1, True),
    # master weather brain knobs
    'WEATHER_SELECT_REEVAL_DAYS':   (0.5, 30, 0.5, False),
    'WEATHER_SELECT_MIN_INDEPENDENT': (1, 8, 1, True),
    'WEATHER_OPTIMIZER_EPSILON':    (0.0, 1.0, 0.05, False),
    'GRADE_EDGE_WEATHER_W':         (0.0, 3.0, 0.1, False),
    'PEAKER_LEAD_MULTIDAY_MIN_H':   (0, 120, 6, False),
    'PEAKER_LEAD_MULTIDAY_MAX_H':   (0, 168, 6, False),
    'PEAKER_LEAD_INTRADAY_H':       (0, 12, 0.5, False),
    'PEAKER_LEAD_CHEAP_PRICE':      (0.0, 1.0, 0.05, False),
    'PEAKER_LEAD_TAKE_PROFIT':      (0.0, 1.0, 0.05, False),
    # risk & sizing
    'MAX_BET_PCT':                 (0.05, 1.00, 0.05, False),
    'MAX_POSITIONS':               (1, 50, 1, True),
    'MAX_SINGLE_MARKET_PCT':       (0.05, 1.00, 0.05, False),
    'KELLY_MAX_FRACTION':          (0.05, 0.50, 0.05, False),
    'KELLY_TIER_BASE_USD':         (1, 20, 1, False),
    'KELLY_TIER_GOOD_USD':         (1, 30, 1, False),
    'KELLY_TIER_VGOOD_USD':        (2, 50, 1, False),
    'KELLY_TIER_PERFECT_USD':      (3, 100, 1, False),
    'PORTFOLIO_RESERVE_PCT':       (0.00, 0.50, 0.05, False),
    'PORTFOLIO_MAX_DEPLOY_PCT':    (0.10, 1.00, 0.05, False),
    'MAX_DEPLOY_PER_SCAN_PCT':     (0.05, 1.00, 0.05, False),
    'MAX_BUYS_PER_SCAN':           (1, 20, 1, True),
    'MAX_DAILY_DRAWDOWN_PCT':      (5, 90, 5, False),
    'MAX_WEEKLY_DRAWDOWN_PCT':     (5, 95, 5, False),
    'DRAWDOWN_COOLDOWN_MINUTES':   (0, 480, 15, True),
    'MIN_EDGE_TO_ENTER':           (0.00, 0.50, 0.02, False),
    'GRADE_MIN_TO_TRADE':          (0.00, 1.00, 0.05, False),
    # late-observed (primary)
    'LATE_OBSERVED_MIN_LOCK':      (0.50, 0.95, 0.05, False),
    'LATE_OBSERVED_MIN_EDGE':      (0.00, 0.40, 0.02, False),
    'LATE_OBSERVED_YES_MIN_LOCK':  (0.50, 0.99, 0.05, False),
    'LATE_OBSERVED_YES_MIN_EDGE':  (0.00, 0.40, 0.02, False),
    'LATE_OBSERVED_MAX_LEGS':      (1, 8, 1, True),
    'LATE_OBSERVED_SIZE_FLOOR_USD': (1, 20, 1, False),
    'LATE_OBSERVED_SIZE_MAX_USD':  (3, 50, 1, False),
    'LATE_OBSERVED_SIZE_MAX_PCT':  (0.00, 0.25, 0.01, False),
    'LATE_OBSERVED_EDGE_FULL':     (0.05, 0.50, 0.05, False),
    'LATE_OBSERVED_NO_MIN_PRICE':  (0.01, 0.20, 0.01, False),
    'LATE_OBSERVED_NO_MAX_PRICE':  (0.80, 0.99, 0.01, False),
    # entry-band gate (overlay) -- data-driven profitable price windows
    'LATE_OBS_YES_MIN_ENTRY':      (0.01, 0.90, 0.01, False),
    'LATE_OBS_YES_MAX_ENTRY':      (0.05, 0.95, 0.05, False),
    'LATE_OBS_NO_MIN_ENTRY':       (0.01, 0.95, 0.01, False),
    'LATE_OBS_NO_MAX_ENTRY':       (0.50, 0.99, 0.01, False),
    'LATE_OBS_YES_MIN_EDGE':       (0.00, 0.40, 0.01, False),
    # global longshot floor (overlay/entry_gate) -- applies to all directional
    # strategies except the multi-leg basket strategies
    'GLOBAL_MIN_ENTRY_PRICE':      (0.00, 0.60, 0.01, False),
    'GLOBAL_MIN_ENTRY_EDGE':       (0.00, 0.40, 0.01, False),
    # quick-flip
    'QUICK_FLIP_MIN_EDGE':         (0.00, 0.40, 0.02, False),
    'QUICK_FLIP_MAX_PER_MARKET':   (1, 5, 1, True),
    'QUICK_FLIP_MAX_CONCURRENT':   (1, 10, 1, True),
    'QUICK_FLIP_MAX_HOLD_MIN':     (15, 360, 15, True),
    'QUICK_FLIP_TARGET_ROI':       (5, 50, 5, False),
    'QUICK_FLIP_MAX_SIZE_USD':     (1, 30, 1, False),
    'QUICK_FLIP_MIN_BOOK_ROI_PCT': (5, 50, 5, False),
    'QUICK_FLIP_LADDER_MID_ROI_PCT': (5, 60, 5, False),
    'QUICK_FLIP_LADDER_RUN_ROI_PCT': (10, 80, 5, False),
    'QUICK_FLIP_FORCE_BOOK_ROI_PCT': (10, 100, 5, False),
    'QUICK_FLIP_STOP_LOSS_PCT':    (-30, -1, 1, False),
    'QUICK_FLIP_STALE_BOOST':      (0.00, 0.50, 0.05, False),
    'QUICK_FLIP_NEW_MARKET_BOOST': (0.00, 0.50, 0.05, False),
    # peaker (merged peak + safety)
    'PEAKER_MIN_GRADE':            (0.00, 1.00, 0.05, False),
    'PEAKER_MIN_CONFIDENCE':       (0.00, 1.00, 0.02, False),
    'PEAKER_SOLO_MIN_CONFIDENCE':  (0.50, 1.00, 0.02, False),
    'PEAKER_MAX_STD':              (0.50, 3.00, 0.10, False),
    'PEAKER_MIN_MODELS':           (1, 6, 1, True),
    'PEAKER_MAX_PEAK_PRICE':       (0.50, 0.99, 0.05, False),
    'PEAKER_MAX_NEIGHBOR_PRICE':   (0.20, 0.90, 0.05, False),
    'PEAKER_MAX_COST':             (0.70, 0.99, 0.01, False),
    'PEAKER_MIN_EDGE':             (0.00, 0.30, 0.01, False),
    'PEAKER_MIN_NET_PROFIT':       (0.00, 0.20, 0.01, False),
    'PEAKER_MAX_USD':              (3, 50, 1, False),
    'PEAKER_COOL_SIZE_MULT':       (0.50, 2.50, 0.05, False),
    'PEAKER_COOL_EDGE_RELAX':      (0.00, 0.10, 0.01, False),
    'PEAKER_WARM_SIZE_MULT':       (0.30, 1.50, 0.05, False),
    'PEAKER_PEAK_BIAS_BUCKETS':    (0, 3, 1, True),
    # peak-cluster
    'PEAK_CLUSTER_SPAN':           (1, 5, 1, True),
    'PEAK_CLUSTER_MIN_LEGS':       (1, 7, 1, True),
    'PEAK_CLUSTER_MAX_LEGS':       (2, 10, 1, True),
    'PEAK_CLUSTER_MAX_COST':       (0.70, 0.99, 0.01, False),
    'PEAK_CLUSTER_MIN_EDGE':       (0.00, 0.30, 0.01, False),
    'PEAK_CLUSTER_MIN_CONF':       (0.00, 1.00, 0.05, False),
    'PEAK_CLUSTER_MAX_CENTER_PRICE': (0.50, 0.99, 0.05, False),
    'PEAK_CLUSTER_MAX_USD':        (3, 50, 1, False),
    'PEAK_CLUSTER_LEG_MIN_PRICE':  (0.00, 0.40, 0.01, False),
    'PEAK_CLUSTER_OUTER_TP_ROI':   (0.10, 3.00, 0.10, False),
    'PEAK_CLUSTER_OUTER_TP_FRACTION': (0.10, 1.00, 0.10, False),
    # weather overlays
    'OBSERVED_CACHE_TTL_SECONDS':  (900, 43200, 900, True),
    # liquidity-sweep exit
    'LIQ_SWEEP_TRIGGER_ROI_PCT':   (-90, -20, 5, False),
    'LIQ_SWEEP_MIN_ENTRY_PRICE':   (0.01, 0.50, 0.01, False),
    'LIQ_SWEEP_MIN_BID':           (0.01, 0.20, 0.01, False),
    'LIQ_SWEEP_MIN_MINUTES_TO_CLOSE': (0, 240, 15, False),
    # exits & liquidity
    'PROFIT_CAP_ROI_PCT':          (100, 1000, 25, False),
    'BALANCE_RESERVE_USD':         (0, 1000000, 5, False),
    'PEAK_CLUSTER_PROB_MIN':       (0.01, 0.50, 0.01, False),
    'THESIS_EXIT_MAX_ROI_PCT':     (-99, -50, 5, False),
    'TRAILING_STOP_PCT':           (5, 90, 5, False),
    'TRAILING_MIN_PEAK_MULT':      (1.0, 10.0, 0.5, False),
    'EARLY_PROFIT_THRESHOLD':      (0.50, 0.99, 0.05, False),
    'LIQUIDITY_THIN_SIZE_MULT':    (0.10, 1.00, 0.10, False),
    'HIGH_TEMP_LOCK_HOUR':         (0, 23, 1, True),
    # sniper / basket / spread / stability
    'SNIPER_MIN_GRADE':            (0.00, 1.00, 0.05, False),
    'SNIPER_MIN_CONFIDENCE':       (0.00, 1.00, 0.05, False),
    'SNIPER_MIN_PROBABILITY':      (0.00, 1.00, 0.02, False),
    'SNIPER_MAX_ENTRY_PRICE':      (0.05, 0.50, 0.05, False),
    'BASKET_MAX_COST':             (0.50, 0.99, 0.05, False),
    'BASKET_TIGHT_GRADE':          (0.00, 1.00, 0.05, False),
    'BASKET_TIGHT_CONFIDENCE':     (0.00, 1.00, 0.05, False),
    'SPREAD_MAX_COST':             (0.50, 2.00, 0.10, False),
    'STABILITY_MIN_SCORE':         (0.00, 1.00, 0.02, False),
    'STABILITY_EARLY_EXIT_PRICE':  (0.50, 0.99, 0.05, False),
    # golden_no fusion strategy gates (overlay/golden_gate.py)
    'GOLDEN_MIN_DAYS_TO_RES':       (0.0, 7.0, 0.5, False),
    'GOLDEN_ENTRY_MIN':             (0.05, 0.95, 0.05, False),
    'GOLDEN_ENTRY_MAX':             (0.05, 0.99, 0.05, False),
    'GOLDEN_EDGE_MIN':              (0.0, 1.0, 0.05, False),
    'GOLDEN_EDGE_MAX':              (0.0, 1.0, 0.05, False),
    'GOLDEN_GRADE_MAX':             (0.0, 1.0, 0.05, False),
    # P3 data export + disk guard
    'EXPORT_PERIODIC_HOURS':        (1, 48, 1, True),
    'EXPORT_DISK_GUARD_MB':         (50, 5000, 50, True),
    # P2b late_observed_no entry gate + P2 exit slippage model
    'P2B_ENTRY_CEILING':            (0.50, 0.99, 0.01, False),
    'P2B_MIN_ROOM_USD':             (0.00, 0.50, 0.01, False),
    'PAPER_EXIT_SLIPPAGE_PCT':      (0.0, 10.0, 0.5, False),
    # golden_no own knobs (split from late-observed so each is tunable)
    'GOLDEN_MIN_DAYS_TO_RES':       (0.0, 7.0, 0.5, False),
    'GOLDEN_ENTRY_MIN':             (0.01, 0.99, 0.01, False),
    'GOLDEN_ENTRY_MAX':             (0.05, 0.99, 0.01, False),
    'GOLDEN_EDGE_MIN':              (0.00, 0.50, 0.01, False),
    'GOLDEN_EDGE_MAX':              (0.00, 0.90, 0.01, False),
    'GOLDEN_GRADE_MAX':             (0.00, 1.00, 0.05, False),
    # VPS edge-node data offload
    'VPS_OFFLOAD_INTERVAL_HOURS':   (1, 48, 1, False),
    'VPS_OFFLOAD_BATCH_LINES':      (200, 20000, 200, True),
    # Req-27 #g: ML veto batch size (1 = per-call/normal, 2-4 = batch N legs)
    'ML_VETO_GROUP_SIZE':           (1, 4, 1, True),
    # 2026-08-19: grade+edge engine calibration knobs
    'GRADE_EDGE_RAW_ANCHOR_W':      (0.0, 1.0, 0.05, False),
    'GRADE_EDGE_CALIB_TEMPERATURE': (0.5, 4.0, 0.1, False),
    'GRADE_EDGE_CALIB_BIAS':        (-1.0, 1.0, 0.05, False),
    'GRADE_EDGE_MAX_PROB':          (0.50, 0.99, 0.01, False),
    # 2026-08-19: late_observed_remaster gates
    'REMASTER_MIN_EDGE':            (0.00, 0.40, 0.01, False),
    'REMASTER_MIN_GRADE':           (0.00, 1.00, 0.05, False),
    'REMASTER_MIN_LOCK':            (0.50, 0.99, 0.05, False),
    'REMASTER_SAMEDAY_MIN_LOCK':    (0.50, 0.99, 0.05, False),
    # 2026-08-19: late_observed_no_arbitrage gates
    'LATE_OBS_NO_ARB_MIN_LOCK':     (0.50, 0.99, 0.05, False),
    'LATE_OBS_NO_ARB_MIN_AVG_PROB': (0.50, 0.99, 0.01, False),
    'LATE_OBS_NO_ARB_MIN_GRADE':    (0.00, 1.00, 0.05, False),
    'LATE_OBS_NO_ARB_MIN_PRICE':    (0.01, 0.50, 0.01, False),
    'LATE_OBS_NO_ARB_MAX_PRICE':    (0.50, 0.99, 0.01, False),
    'LATE_OBS_NO_ARB_MIN_LEGS':     (2, 8, 1, True),
    'LATE_OBS_NO_ARB_MAX_LEGS':     (2, 10, 1, True),
    'LATE_OBS_NO_ARB_MAX_BASKET_USD': (3, 50, 1, False),
    # 2026-08-19: weather 10k-limit adaptive fetch planning
    'WEATHER_DAILY_BUDGET':         (1000, 30000, 500, True),
    'WEATHER_FETCH_SAFETY':         (0.30, 1.00, 0.05, False),
    'WEATHER_MIN_CACHE_SECONDS':    (30, 1800, 30, True),
    'WEATHER_MAX_CACHE_SECONDS':    (300, 7200, 60, True),
}

# -- String/choice settings: key -> list of allowed values (first = default) --
STR_KEYS: Dict[str, List[str]] = {
    'ML_MODEL':          ['gpt-5.4-mini', 'gpt-5.4', 'gpt-5.5', 'gpt-5.3-codex'],
    'ML_ANALYSIS_MODEL': ['gpt-5.5', 'gpt-5.4', 'gpt-5.4-mini', 'gpt-5.3-codex'],
    # Provider wire-format (2026-07-27). First value = safe default. Also
    # settable/lockable via /mlsetup. ML_LOCKED_PROFILE is FREE_TEXT (below) so
    # it can hold '' (unlocked) as well as any profile name.
    'ML_PROVIDER_PROFILE': ['openai_compatible', 'openai', 'anthropic',
                            'google_gemini', 'cohere', 'ollama'],
    # Locked profile: free text ('' = unlocked). Needs a STR_KEYS entry so
    # set_value accepts it; FREE_TEXT_STR_KEYS below makes _coerce store the
    # raw string (including '') rather than snapping to a choice.
    'ML_LOCKED_PROFILE': [''],
    # ML endpoint base URL -- free text, set via the /mlsetup wizard. 1-item
    # list keeps the v[0] default-fallback in _persist()/str_snapshot() safe.
    'ML_API_URL': [''],
    # Req-27 #c: per-stream VPS data handling. full = one continuous file per
    # stream; weekly / monthly = one file per period. Applies to ALL streams.
    'VPS_HANDLING_PAPER_TRADES': ['full', 'weekly', 'monthly'],
    'VPS_HANDLING_MAE': ['full', 'weekly', 'monthly'],
    'VPS_HANDLING_TIMESERIES': ['full', 'weekly', 'monthly'],
    'VPS_HANDLING_WEATHER_TRACE': ['full', 'weekly', 'monthly'],
    # 2026-08-19: weather fetch mode -- normal vs 10k-limit adaptive polling
    'WEATHER_FETCH_MODE': ['normal', 'limit10k'],
    # 2026-08-19: per-document VPS target -- 'vps' = offload+delete-on-Railway,
    # 'railway' = keep in bot's own memory (Railway disk)
    'VPS_DOC_DEFAULT': ['vps', 'railway'],
    'VPS_DOC_PAPER_TRADES': ['vps', 'railway'],
    'VPS_DOC_MAE': ['vps', 'railway'],
    'VPS_DOC_TIMESERIES': ['vps', 'railway'],
    'VPS_DOC_WEATHER_TRACE': ['vps', 'railway'],
    'VPS_DOC_BOOK_TRACE': ['vps', 'railway'],
    'VPS_DOC_MANIFEST': ['vps', 'railway'],
    # 2026-08-20: where the whole weather pipeline runs by default.
    'WEATHER_EXECUTION_MODE': ['auto', 'vps', 'bot', 'off'],
    # 2026-08-20: per-source execution override string, e.g.
    # 'open_meteo:vps,openweather:bot,weatherapi:off'. Free text (see below).
    'WEATHER_SOURCE_LOCATION': [''],
    # 2026-08-20: master brain state dir + direct ECMWF endpoint (free text).
    'WEATHER_MASTER_STATE_DIR': ['data/weather_state'],
    'ECMWF_API_URL': ['https://ecds.ecmwf.int/api'],
}

# STR keys that accept FREE TEXT (not a fixed choice list) — e.g. the overlay
# city lists. For these, /set stores the raw string. The advanced-settings
# overlay registers such keys here and gives them a 1-item choices list so the
# v[0] default fallback in str_snapshot()/_persist() stays safe.
FREE_TEXT_STR_KEYS = {'ML_LOCKED_PROFILE', 'ML_API_URL', 'ML_MODEL', 'ML_ANALYSIS_MODEL',
                      'WEATHER_SOURCE_LOCATION', 'WEATHER_MASTER_STATE_DIR', 'ECMWF_API_URL'}

# -- Tabs for the Telegram /settings panel. Each group lists the keys (toggles
# and/or gates) shown when that tab is active, in display order. ------------
GROUPS: List[dict] = [
    {'id': 'main', 'tab': 'Strat', 'title': 'Master & Strategies', 'keys': [
        'TRADING_ENABLED', 'STARTING_BALANCE',
        'LATE_OBSERVED_ENABLED', 'LATE_OBSERVED_NO_SIDE',
        'QUICK_FLIP_ENABLED', 'PEAK_CLUSTER_ENABLED', 'PEAKER_ENABLED',
        'CONFIDENT_ENABLED', 'SNIPER_ENABLED', 'SPREAD_ENABLED', 'STABILITY_ENABLED',
        'ML_ENABLED', 'ML_DECISION_ENABLED', 'AUTO_REDEEM_ENABLED',
        'SUMMARY_INTERVAL_MIN',
    ]},
    {'id': 'ml', 'tab': 'ML', 'title': 'ML / AI Engine', 'keys': [
        'ML_ENABLED', 'ML_DECISION_ENABLED', 'ML_ANALYSIS_ENABLED',
        'ML_REVIEW_POSITIONS', 'ML_SELECT_MARKETS',
        'ML_MODEL', 'ML_ANALYSIS_MODEL', 'ML_PROVIDER_PROFILE',
        'ML_VETO_BATCH_ENABLED', 'ML_VETO_GROUP_SIZE',
    ]},
    {'id': 'risk', 'tab': 'Risk', 'title': 'Risk, Drawdown & Sizing', 'keys': [
        'PORTFOLIO_GUARD_ENABLED', 'DRAWDOWN_GATE_ENABLED', 'QUICK_FLIP_BOOK_OR_CUT',
        'MAX_BET_PCT', 'MAX_POSITIONS', 'MAX_SINGLE_MARKET_PCT',
        'KELLY_MAX_FRACTION', 'KELLY_TIER_BASE_USD', 'KELLY_TIER_GOOD_USD',
        'KELLY_TIER_VGOOD_USD', 'KELLY_TIER_PERFECT_USD',
        'PORTFOLIO_RESERVE_PCT', 'PORTFOLIO_MAX_DEPLOY_PCT',
        'MAX_DEPLOY_PER_SCAN_PCT', 'MAX_BUYS_PER_SCAN',
        'MAX_DAILY_DRAWDOWN_PCT', 'MAX_WEEKLY_DRAWDOWN_PCT', 'DRAWDOWN_COOLDOWN_MINUTES',
        'MIN_EDGE_TO_ENTER', 'GRADE_MIN_TO_TRADE', 'BALANCE_RESERVE_USD',
    ]},
    # 2026-08-20: THE consolidated late-observed control tab. Previously the
    # late-observed knobs were scattered across LateObs / Entry / Golden /
    # Remaster / Observed-No-fix tabs. This single "main" tab gathers ALL of
    # them, arranged: master toggles -> primary gates -> entry bands ->
    # timing -> remaster -> no-arbitrage -> observed-no fill fix. The detailed
    # per-area tabs are kept below for focused editing.
    {'id': 'lateobsall', 'tab': 'Late-Obs \u2b50', 'title': 'Late-Observed \u2014 ALL settings (the main one)', 'keys': [
        # master toggles
        'LATE_OBSERVED_ENABLED', 'LATE_OBSERVED_NO_SIDE',
        'LATE_OBSERVED_REMASTER_ENABLED', 'LATE_OBS_NO_ARB_ENABLED',
        # primary gates / sizing
        'LATE_OBSERVED_MIN_LOCK', 'LATE_OBSERVED_MIN_EDGE',
        'LATE_OBSERVED_YES_MIN_LOCK', 'LATE_OBSERVED_YES_MIN_EDGE',
        'LATE_OBSERVED_MAX_LEGS', 'LATE_OBSERVED_SIZE_FLOOR_USD',
        'LATE_OBSERVED_SIZE_MAX_USD', 'LATE_OBSERVED_SIZE_MAX_PCT', 'LATE_OBSERVED_EDGE_FULL',
        'LATE_OBSERVED_NO_MIN_PRICE', 'LATE_OBSERVED_NO_MAX_PRICE',
        # entry bands
        'LATE_OBS_YES_MIN_ENTRY', 'LATE_OBS_YES_MAX_ENTRY', 'LATE_OBS_YES_MIN_EDGE',
        'LATE_OBS_NO_MIN_ENTRY', 'LATE_OBS_NO_MAX_ENTRY',
        # timing windows
        'LATE_OBS_TIMING_SAMEDAY_ENABLED', 'LATE_OBS_TIMING_1D_ENABLED',
        'LATE_OBS_TIMING_2D_ENABLED', 'LATE_OBS_TIMING_3D_ENABLED',
        'LATE_OBS_FETCH_AFTER_NEXT_DAY',
        # remaster gates
        'REMASTER_MIN_EDGE', 'REMASTER_MIN_GRADE', 'REMASTER_MIN_LOCK', 'REMASTER_SAMEDAY_MIN_LOCK',
        # no-arbitrage basket gates
        'LATE_OBS_NO_ARB_MIN_LOCK', 'LATE_OBS_NO_ARB_MIN_AVG_PROB', 'LATE_OBS_NO_ARB_MIN_GRADE',
        'LATE_OBS_NO_ARB_MIN_PRICE', 'LATE_OBS_NO_ARB_MAX_PRICE',
        'LATE_OBS_NO_ARB_MIN_LEGS', 'LATE_OBS_NO_ARB_MAX_LEGS', 'LATE_OBS_NO_ARB_MAX_BASKET_USD',
        # observed-no fill fix
        'P2B_ENTRY_CEILING', 'P2B_MIN_ROOM_USD', 'P2B_FORCE_EXIT_ENABLED',
    ]},
    {'id': 'lateobs', 'tab': 'LateObs', 'title': 'Late-Observed (primary)', 'keys': [
        'LATE_OBSERVED_NO_SIDE',
        'LATE_OBSERVED_MIN_LOCK', 'LATE_OBSERVED_MIN_EDGE',
        'LATE_OBSERVED_YES_MIN_LOCK', 'LATE_OBSERVED_YES_MIN_EDGE',
        'LATE_OBSERVED_MAX_LEGS', 'LATE_OBSERVED_SIZE_FLOOR_USD',
        'LATE_OBSERVED_SIZE_MAX_USD', 'LATE_OBSERVED_SIZE_MAX_PCT', 'LATE_OBSERVED_EDGE_FULL',
        'LATE_OBSERVED_NO_MIN_PRICE', 'LATE_OBSERVED_NO_MAX_PRICE',
    ]},
    {'id': 'entry', 'tab': 'Entry', 'title': 'Entry Bands (price gates)', 'keys': [
        'ENTRY_BAND_GATE_ENABLED',
        'LATE_OBS_YES_MIN_ENTRY', 'LATE_OBS_YES_MAX_ENTRY', 'LATE_OBS_YES_MIN_EDGE',
        'LATE_OBS_NO_MIN_ENTRY', 'LATE_OBS_NO_MAX_ENTRY',
        'GLOBAL_MIN_ENTRY_GATE_ENABLED', 'GLOBAL_MIN_ENTRY_PRICE', 'GLOBAL_MIN_ENTRY_EDGE',
    ]},
    {'id': 'weather', 'tab': 'Weather', 'title': 'Weather Data & Health', 'keys': [
        'OBSERVED_CACHE_ENABLED', 'OBSERVED_CACHE_TTL_SECONDS',
        'WEATHER_TRACE_ENABLED',
        # Req-27 #6: per-model Open-Meteo fetch toggles (green tick ON / red X OFF)
        'WX_MODEL_ECMWF_IFS_ENABLED', 'WX_MODEL_ECMWF_IFS025_ENABLED',
        'WX_MODEL_ECMWF_IFS04_ENABLED', 'WX_MODEL_GFS_SEAMLESS_ENABLED',
        'WX_MODEL_ICON_SEAMLESS_ENABLED', 'WX_MODEL_JMA_SEAMLESS_ENABLED',
        'WX_MODEL_GEM_SEAMLESS_ENABLED',
    ]},
    # 2026-08-20: advanced multi-layer weather PIPELINE (data/weather/). Fully
    # customisable execution: run the whole thing on VPS, bot, or off, and pick
    # per-source locations. Legacy fetcher stays available (old vs new).
    {'id': 'wxpipeline', 'tab': 'WX Pipeline', 'title': 'Advanced Weather Pipeline (source identity, consensus, VPS/bot routing)', 'keys': [
        'WEATHER_PIPELINE_ENABLED', 'WEATHER_EXECUTION_MODE', 'WEATHER_SOURCE_LOCATION',
        'WEATHER_PIPELINE_FORECAST_DAYS',
        'WEATHER_SRC_OPEN_METEO_ENABLED', 'WEATHER_SRC_OPENWEATHER_ENABLED',
        'WEATHER_SRC_WEATHERAPI_ENABLED', 'WEATHER_SRC_VISUALCROSSING_ENABLED',
        'WEATHER_SRC_NWS_ENABLED',
        'WEATHER_SRC_ECMWF_DIRECT_ENABLED', 'WEATHER_SRC_ECMWF_AIFS_ENABLED',
        'ECMWF_API_URL',
    ]},
    # 2026-08-20: the MASTER weather intelligence brain (skill/learning/
    # calibration/selection/optimizer). Separate settings so every part of the
    # data pipeline is independently customisable, per the master prompt.
    {'id': 'wxmaster', 'tab': 'WX Master', 'title': 'Master Weather Brain (skill, learning, calibration, smart source selection, optimizer)', 'keys': [
        'WEATHER_MASTER_ENABLED', 'WEATHER_MASTER_STATE_DIR',
        'WEATHER_SELECT_SUPPRESS_ENABLED', 'WEATHER_SELECT_REEVAL_DAYS',
        'WEATHER_SELECT_MIN_INDEPENDENT', 'WEATHER_OPTIMIZER_EPSILON',
        'GRADE_EDGE_WEATHER_W',
        'PEAKER_LEAD_MULTIDAY_MIN_H', 'PEAKER_LEAD_MULTIDAY_MAX_H',
        'PEAKER_LEAD_INTRADAY_H', 'PEAKER_LEAD_CHEAP_PRICE', 'PEAKER_LEAD_TAKE_PROFIT',
    ]},
    {'id': 'quickflip', 'tab': 'Flip', 'title': 'Quick-Flip', 'keys': [
        'QUICK_FLIP_PROFIT_ONLY_EXIT', 'QUICK_FLIP_USE_ML_EXIT',
        'QUICK_FLIP_BOOK_OR_CUT', 'QUICK_FLIP_USE_ML_PROFIT',
        'QUICK_FLIP_HARD_STOP_ENABLED',
        'QUICK_FLIP_MIN_EDGE', 'QUICK_FLIP_MAX_PER_MARKET',
        'QUICK_FLIP_MAX_CONCURRENT', 'QUICK_FLIP_MAX_HOLD_MIN',
        'QUICK_FLIP_TARGET_ROI', 'QUICK_FLIP_STOP_LOSS_PCT', 'QUICK_FLIP_MAX_SIZE_USD',
        'QUICK_FLIP_MIN_BOOK_ROI_PCT', 'QUICK_FLIP_LADDER_MID_ROI_PCT',
        'QUICK_FLIP_LADDER_RUN_ROI_PCT', 'QUICK_FLIP_FORCE_BOOK_ROI_PCT',
        'QUICK_FLIP_STALE_BOOST', 'QUICK_FLIP_NEW_MARKET_BOOST',
    ]},
    {'id': 'peaker', 'tab': 'Peaker', 'title': 'Peaker (merged peak+safety)', 'keys': [
        'PEAKER_PREFER_COOL', 'PEAKER_TRADE_DECIDED',
        'PEAKER_MIN_GRADE', 'PEAKER_MIN_CONFIDENCE', 'PEAKER_SOLO_MIN_CONFIDENCE',
        'PEAKER_MAX_STD', 'PEAKER_MIN_MODELS', 'PEAKER_MAX_PEAK_PRICE',
        'PEAKER_MAX_NEIGHBOR_PRICE', 'PEAKER_MAX_COST', 'PEAKER_MIN_EDGE',
        'PEAKER_MIN_NET_PROFIT', 'PEAKER_MAX_USD', 'PEAKER_COOL_SIZE_MULT',
        'PEAKER_COOL_EDGE_RELAX', 'PEAKER_WARM_SIZE_MULT', 'PEAKER_PEAK_BIAS_BUCKETS',
    ]},
    {'id': 'cluster', 'tab': 'Cluster', 'title': 'Peak-Cluster', 'keys': [
        'PEAK_CLUSTER_TRADE_DECIDED',
        'PEAK_CLUSTER_SPAN', 'PEAK_CLUSTER_MIN_LEGS', 'PEAK_CLUSTER_MAX_LEGS',
        'PEAK_CLUSTER_MAX_COST', 'PEAK_CLUSTER_MIN_EDGE', 'PEAK_CLUSTER_MIN_CONF',
        'PEAK_CLUSTER_MAX_CENTER_PRICE', 'PEAK_CLUSTER_MAX_USD',
        'PEAK_CLUSTER_CONTIGUOUS_ENABLED', 'PEAK_CLUSTER_PROB_BASED_ENABLED',
        'PEAK_CLUSTER_PROB_MIN', 'PEAK_CLUSTER_LEG_MIN_PRICE',
        'PEAK_CLUSTER_OUTER_TP_ENABLED', 'PEAK_CLUSTER_OUTER_TP_ROI',
        'PEAK_CLUSTER_OUTER_TP_FRACTION',
    ]},
    {'id': 'exits', 'tab': 'Exits', 'title': 'Exits & Liquidity', 'keys': [
        'THESIS_EXIT_ENABLED', 'LIQUIDITY_GUARD_ENABLED', 'LIQUIDITY_STRICT_BLOCK',
        'GRADE_SIZING_ENABLED', 'SKIP_DECIDED_MARKETS', 'PROFIT_CAP_ENABLED',
        'PROFIT_CAP_ML_OVERRIDE',
        'THESIS_EXIT_MAX_ROI_PCT', 'PROFIT_CAP_ROI_PCT', 'TRAILING_STOP_PCT', 'TRAILING_MIN_PEAK_MULT',
        'EARLY_PROFIT_THRESHOLD', 'LIQUIDITY_THIN_SIZE_MULT', 'HIGH_TEMP_LOCK_HOUR',
        # liquidity-sweep exit (fast cut at -50% into available liquidity)
        'LIQ_SWEEP_EXIT_ENABLED', 'LIQ_SWEEP_EXEMPT_BASKETS', 'LIQ_SWEEP_TRIGGER_ROI_PCT',
        'LIQ_SWEEP_MIN_ENTRY_PRICE', 'LIQ_SWEEP_MIN_BID', 'LIQ_SWEEP_MIN_MINUTES_TO_CLOSE',
    ]},
    {'id': 'sniper', 'tab': 'Sniper', 'title': 'Sniper / Basket', 'keys': [
        'SNIPER_MIN_GRADE', 'SNIPER_MIN_CONFIDENCE', 'SNIPER_MIN_PROBABILITY',
        'SNIPER_MAX_ENTRY_PRICE', 'BASKET_MAX_COST', 'BASKET_TIGHT_GRADE',
        'BASKET_TIGHT_CONFIDENCE', 'SPREAD_MAX_COST', 'STABILITY_MIN_SCORE',
        'STABILITY_EARLY_EXIT_PRICE',
    ]},
    {'id': 'golden', 'tab': 'Golden', 'title': 'Golden Filter + Late-Obs Timing', 'keys': [
        'GOLDEN_NO_ENABLED', 'GOLDEN_NO_SIDE_ONLY',
        'GOLDEN_MIN_DAYS_TO_RES', 'GOLDEN_ENTRY_MIN', 'GOLDEN_ENTRY_MAX',
        'GOLDEN_EDGE_MIN', 'GOLDEN_EDGE_MAX', 'GOLDEN_GRADE_MAX',
        'LATE_OBS_TIMING_SAMEDAY_ENABLED', 'LATE_OBS_TIMING_1D_ENABLED',
        'LATE_OBS_TIMING_2D_ENABLED', 'LATE_OBS_TIMING_3D_ENABLED',
        'LATE_OBS_FETCH_AFTER_NEXT_DAY',
    ]},
    {'id': 'export', 'tab': 'Export', 'title': 'Data Export & Disk', 'keys': [
        'EXPORT_PERIODIC_ENABLED', 'EXPORT_PERIODIC_HOURS', 'EXPORT_DISK_GUARD_MB',
    ]},
    {'id': 'goldentune', 'tab': 'Golden No', 'title': 'Golden-No strategy (own knobs)', 'keys': [
        'GOLDEN_NO_ENABLED', 'GOLDEN_NO_SIDE_ONLY', 'GOLDEN_NO_SAMEDAY_ENABLED',
        'GOLDEN_MIN_DAYS_TO_RES', 'GOLDEN_ENTRY_MIN', 'GOLDEN_ENTRY_MAX',
        'GOLDEN_EDGE_MIN', 'GOLDEN_EDGE_MAX', 'GOLDEN_GRADE_MAX',
    ]},
    {'id': 'p2btune', 'tab': 'Observed-No fix', 'title': 'Late-observed-no WR->profit + fill model', 'keys': [
        'P2B_ENTRY_CEILING', 'P2B_MIN_ROOM_USD', 'P2B_FORCE_EXIT_ENABLED',
        'PAPER_EXIT_SLIPPAGE_PCT', 'PHANTOM_GUARD_GOLDEN_ONLY',
    ]},
    {'id': 'vps', 'tab': 'VPS', 'title': 'VPS edge-node data offload', 'keys': [
        'VPS_OFFLOAD_ENABLED', 'VPS_OFFLOAD_INTERVAL_HOURS', 'VPS_OFFLOAD_BATCH_LINES',
        'VPS_PULL_ON_OFFLOAD', 'VPS_ANALYSIS_MERGE_ENABLED',
        # Req-27 #c: per-stream data handling (full=one file / weekly / monthly)
        'VPS_HANDLING_PAPER_TRADES', 'VPS_HANDLING_MAE',
        'VPS_HANDLING_TIMESERIES', 'VPS_HANDLING_WEATHER_TRACE',
    ]},
    {'id': 'vpsmaster', 'tab': 'VPS Master', 'title': 'VPS master switch + services (OFF = ALL VPS off, weather fetches direct)', 'keys': [
        'VPS_SERVICES_ENABLED', 'VPS_WEATHER_PROXY_ENABLED',
        'VPS_OFFLOAD_ENABLED', 'VPS_PULL_ON_ANALYSIS',
        'WEATHER_FETCH_MODE', 'WEATHER_DAILY_BUDGET', 'WEATHER_FETCH_SAFETY',
        'WEATHER_MIN_CACHE_SECONDS', 'WEATHER_MAX_CACHE_SECONDS',
    ]},
    {'id': 'vpsdocs', 'tab': 'VPS Docs', 'title': 'Per-document VPS target (vps = offload+delete on Railway / railway = keep in bot memory)', 'keys': [
        'VPS_DOC_DEFAULT', 'VPS_DOC_PAPER_TRADES', 'VPS_DOC_MAE',
        'VPS_DOC_TIMESERIES', 'VPS_DOC_WEATHER_TRACE', 'VPS_DOC_BOOK_TRACE',
        'VPS_DOC_MANIFEST',
    ]},
    {'id': 'engine', 'tab': 'Grade+Edge', 'title': 'Grade & Edge engine (multi-pipeline, calibrated, ML-assisted)', 'keys': [
        'GRADE_EDGE_ENGINE_ENABLED', 'GRADE_EDGE_RAW_ANCHOR_W',
        'GRADE_EDGE_CALIB_TEMPERATURE', 'GRADE_EDGE_CALIB_BIAS', 'GRADE_EDGE_MAX_PROB',
        'RUN_ID_ENABLED', 'PHANTOM_GUARD_ALL_STRATEGIES',
    ]},
    {'id': 'remaster', 'tab': 'Remaster', 'title': 'Late-Observed Remaster + NO-Arbitrage (new strategies)', 'keys': [
        'LATE_OBSERVED_REMASTER_ENABLED',
        'REMASTER_MIN_EDGE', 'REMASTER_MIN_GRADE', 'REMASTER_MIN_LOCK', 'REMASTER_SAMEDAY_MIN_LOCK',
        'LATE_OBS_NO_ARB_ENABLED',
        'LATE_OBS_NO_ARB_MIN_LOCK', 'LATE_OBS_NO_ARB_MIN_AVG_PROB', 'LATE_OBS_NO_ARB_MIN_GRADE',
        'LATE_OBS_NO_ARB_MIN_PRICE', 'LATE_OBS_NO_ARB_MAX_PRICE',
        'LATE_OBS_NO_ARB_MIN_LEGS', 'LATE_OBS_NO_ARB_MAX_LEGS', 'LATE_OBS_NO_ARB_MAX_BASKET_USD',
    ]},
    {'id': 'notify', 'tab': 'Notifications', 'title': 'Telegram event notifications (state-change only, rate-limited, no disk logs)', 'keys': [
        'NOTIFY_WEATHER_SOURCE', 'NOTIFY_VPS_HEALTH', 'NOTIFY_VPS_OFFLOAD',
        'NOTIFY_ENDPOINT_COOLDOWN', 'NOTIFY_ML_STATUS',
    ]},
]


def group_keys(group_id: str) -> Tuple[List[str], List[str]]:
    """Return (bool_keys, num_keys) for a group, preserving display order and
    silently dropping any key not registered in BOOL_KEYS / NUM_KEYS."""
    g = next((x for x in GROUPS if x['id'] == group_id), None)
    if not g:
        return [], []
    bkeys = [k for k in g['keys'] if k in BOOL_KEYS]
    nkeys = [k for k in g['keys'] if k in NUM_KEYS]
    return bkeys, nkeys


def group_str_keys(group_id: str) -> List[str]:
    """Return the string/choice keys for a group (e.g. ML model selectors)."""
    g = next((x for x in GROUPS if x['id'] == group_id), None)
    if not g:
        return []
    return [k for k in g['keys'] if k in STR_KEYS]


def _coerce(key: str, value):
    if key in BOOL_KEYS:
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ('1', 'true', 'on', 'yes', 'y')
    spec = NUM_KEYS.get(key)
    if spec:
        lo, hi, _step, is_int = spec
        v = int(round(float(value))) if is_int else float(value)
        return max(lo, min(hi, v))
    if key in STR_KEYS:
        choices = STR_KEYS[key]
        s = str(value).strip()
        if key in FREE_TEXT_STR_KEYS:
            return s
        for c in choices:
            if c.lower() == s.lower():
                return c
        return getattr(Config, key, choices[0] if choices else s)
    return value


def set_value(key: str, value) -> Tuple[bool, str]:
    """Set a tunable to an explicit value (used by /set KEY VALUE)."""
    key = key.upper()
    if key not in BOOL_KEYS and key not in NUM_KEYS and key not in STR_KEYS:
        return False, f"unknown setting '{key}'"
    try:
        v = _coerce(key, value)
    except (ValueError, TypeError):
        return False, f"invalid value for {key}: {value!r}"
    setattr(Config, key, v)
    _persist()
    return True, f"{key} = {v}"


def toggle(key: str) -> Tuple[bool, str]:
    """Flip a boolean toggle (used by tick-box buttons / /toggle KEY)."""
    key = key.upper()
    if key not in BOOL_KEYS:
        return False, f"'{key}' is not a toggle"
    cur = bool(getattr(Config, key, False))
    setattr(Config, key, not cur)
    _persist()
    return True, f"{key} = {not cur}"


def bump(key: str, direction: int) -> Tuple[bool, str]:
    """Step a numeric gate up (+1) or down (-1) by its configured step."""
    key = key.upper()
    spec = NUM_KEYS.get(key)
    if not spec:
        return False, f"'{key}' is not a numeric gate"
    lo, hi, step, is_int = spec
    cur = float(getattr(Config, key, lo))
    nxt = cur + direction * step
    nxt = max(lo, min(hi, nxt))
    if is_int:
        nxt = int(round(nxt))
    else:
        nxt = round(nxt, 4)
    setattr(Config, key, nxt)
    _persist()
    return True, f"{key} = {nxt}"


def cycle(key: str) -> Tuple[bool, str]:
    """Advance a string/choice setting to its next allowed value (tap button)."""
    key = key.upper()
    choices = STR_KEYS.get(key)
    if not choices:
        return False, f"'{key}' is not a choice setting"
    cur = str(getattr(Config, key, choices[0]))
    try:
        idx = next(i for i, c in enumerate(choices) if c.lower() == cur.lower())
    except StopIteration:
        idx = -1
    nxt = choices[(idx + 1) % len(choices)]
    setattr(Config, key, nxt)
    _persist()
    return True, f"{key} = {nxt}"


def snapshot() -> Tuple[Dict[str, bool], Dict[str, float]]:
    bools = {k: bool(getattr(Config, k, False)) for k in BOOL_KEYS}
    nums = {k: getattr(Config, k, None) for k in NUM_KEYS}
    return bools, nums


def str_snapshot() -> Dict[str, str]:
    """Current values of all string/choice settings."""
    return {k: str(getattr(Config, k, v[0])) for k, v in STR_KEYS.items()}


def _persist():
    try:
        os.makedirs('data', exist_ok=True)
        data = {k: bool(getattr(Config, k, False)) for k in BOOL_KEYS}
        data.update({k: getattr(Config, k, None) for k in NUM_KEYS})
        data.update({k: str(getattr(Config, k, v[0])) for k, v in STR_KEYS.items()})
        with open(SETTINGS_PATH, 'w') as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log.debug(f"settings persist failed: {e}")


def load_into_config():
    """Apply persisted overrides onto Config at startup."""
    try:
        if not os.path.exists(SETTINGS_PATH):
            return
        with open(SETTINGS_PATH) as f:
            data = json.load(f)
        n = 0
        for k, v in data.items():
            if k in BOOL_KEYS or k in NUM_KEYS or k in STR_KEYS:
                setattr(Config, k, _coerce(k, v))
                n += 1
        if n:
            log.info(f"Loaded {n} runtime settings overrides from {SETTINGS_PATH}")
    except Exception as e:
        log.debug(f"settings load failed: {e}")


# --- ADVANCED SETTINGS OVERLAY (overlay/advanced_settings.py) ----------------
# Register every extra tunable (exit thresholds, ML-review gates, late_observed
# sizing internals, the decoupled sub-strategy toggles, the sizing overlay, and
# the reserve/takeout controls) into BOOL_KEYS / NUM_KEYS / GROUPS, and fill the
# INFO registry consumed by the Telegram /info command. Guarded so a problem in
# the overlay can never break the settings store.
INFO: Dict[str, str] = {}
try:
    import sys as _sys
    from overlay import advanced_settings as _adv
    _adv.install(_sys.modules[__name__])
except Exception as _e:  # pragma: no cover
    log.debug(f"advanced settings install skipped: {_e}")
