"""ADVANCED SETTINGS registry (v2.1.0).

Exposes every extra tunable to the Telegram /settings panel with a
human-readable INFO string and a safe min/max/step range (or free text).
Adjustable live via /set, /toggle, tab buttons, and documented by /info KEY.

install(store) is called once at the end of bot/settings_store import. Guarded
and idempotent.
"""

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None


BOOL_DEFAULTS = {
    "LATE_OBSERVED_YES_ENABLED": True,
    "LATE_OBSERVED_NO_ENABLED": True,
    "PEAKER_SOLO_ENABLED": True,
    "PEAKER_COOL_BASKET_ENABLED": True,
    "PEAKER_WARM_BASKET_ENABLED": True,
    "BASKET_THESIS_EXEMPT": True,
    "SIZING_OVERLAY_ENABLED": True,
    "PROFIT_CAP_EXEMPT_BASKETS": True,
    "ENTRY_BAND_GATE_ENABLED": True,
    "CITY_THROTTLE_ENABLED": True,
    "ADAPTIVE_BOOST_ENABLED": True,
    "MAE_MFE_LOGGING_ENABLED": True,
    "MAE_MFE_SNAPSHOT_EVERY_SCAN": True,
    "CLUSTER_CONTIGUOUS_ENABLED": True,
    "CLUSTER_CONTIGUOUS_FILL_HOLES": True,
}

NUM_DEFAULTS = {
    "THESIS_EXIT_MIN_ENTRY_PRICE": (0.10, (0.01, 0.50, 0.01, False)),
    "THESIS_EXIT_MIN_BID": (0.02, (0.00, 0.20, 0.01, False)),
    "THESIS_EXIT_MIN_MINUTES_TO_CLOSE": (60.0, (0, 600, 15, False)),
    "ML_REVIEW_SELL_CONF": (0.72, (0.50, 0.99, 0.02, False)),
    "ML_REVIEW_MIN_HOLD_MIN": (20.0, (0, 240, 5, False)),
    "ML_REVIEW_MIN_MTC_MIN": (45.0, (0, 360, 5, False)),
    "ML_REVIEW_MAX_PER_SCAN": (6, (1, 30, 1, True)),
    "LATE_OBSERVED_MIN_ENTRY_PRICE": (0.02, (0.01, 0.20, 0.01, False)),
    "LATE_OBSERVED_BASE_FRACTION": (0.06, (0.01, 0.30, 0.01, False)),
    "LATE_OBSERVED_MAX_FRACTION": (0.25, (0.05, 0.50, 0.05, False)),
    "LATE_OBSERVED_W_EDGE": (0.60, (0.00, 1.00, 0.05, False)),
    "LATE_OBSERVED_W_GRADE": (0.40, (0.00, 1.00, 0.05, False)),
    "SIZING_OVERLAY_MIN_MULT": (0.85, (0.30, 1.00, 0.05, False)),
    "SIZING_OVERLAY_MAX_MULT": (1.20, (1.00, 2.00, 0.05, False)),
    "SIZING_OVERLAY_NO_LOW_BAND": (0.50, (0.00, 1.00, 0.05, False)),
    "SIZING_OVERLAY_NO_HIGH_BAND": (0.85, (0.00, 1.00, 0.05, False)),
    "LATE_OBS_NO_MIN_ENTRY": (0.50, (0.00, 1.00, 0.05, False)),
    "LATE_OBS_NO_MAX_ENTRY": (0.97, (0.50, 1.00, 0.01, False)),
    "LATE_OBS_YES_MAX_ENTRY": (0.15, (0.00, 1.00, 0.05, False)),
    "CITY_UNDERWEIGHT_MULT": (0.50, (0.00, 1.00, 0.05, False)),
    "CITY_OVERWEIGHT_MULT": (1.30, (1.00, 2.00, 0.05, False)),
    "ADAPTIVE_BOOST_MIN_SAMPLE": (15, (1, 200, 1, True)),
    "ADAPTIVE_BOOST_MIN_MULT": (0.60, (0.10, 1.00, 0.05, False)),
    "ADAPTIVE_BOOST_MAX_MULT": (1.40, (1.00, 3.00, 0.05, False)),
    "ADAPTIVE_BOOST_NEUTRAL_WR": (0.45, (0.00, 1.00, 0.05, False)),
    "TAKEOUT_RESERVE_USD": (0.0, (0, 1000000, 10, False)),
    "TAKEOUT_TARGET_USD": (0.0, (0, 1000000, 10, False)),
    "TAKEOUT_SKIM_PCT": (0.50, (0.00, 1.00, 0.05, False)),
}

STR_DEFAULTS = {
    "CITY_BLOCKLIST": "",
    "CITY_UNDERWEIGHT": "madrid,ankara,houston,chicago,tokyo,lucknow",
    "CITY_OVERWEIGHT": "paris,seoul,shanghai,london,hong kong,moscow",
}

INFO = {
    "LATE_OBSERVED_YES_ENABLED": "Allow late_observed YES legs to TRADE. Independent of NO. Weaker side historically; turn OFF to keep only NO.",
    "LATE_OBSERVED_NO_ENABLED": "Allow late_observed NO legs to TRADE. Independent of YES. Top earner (+$182, ~73% WR) - keep ON.",
    "PEAKER_SOLO_ENABLED": "Allow the single-bucket peaker to TRADE. Independent of cool/warm baskets.",
    "PEAKER_COOL_BASKET_ENABLED": "Allow peaker COOL baskets to TRADE. Independent of solo and warm.",
    "PEAKER_WARM_BASKET_ENABLED": "Allow peaker WARM baskets to TRADE. Independent of solo and cool.",
    "BASKET_THESIS_EXEMPT": "ON = baskets never cut by the thesis exit; they ride to resolution so the winning leg pays for losers. Fixes the -$819 leak. Keep ON.",
    "SIZING_OVERLAY_ENABLED": "Master switch for the late_observed_no band size multiplier. OFF = strategy sizing unchanged.",
    "PROFIT_CAP_EXEMPT_BASKETS": "ON = the +300% profit cap never books a basket leg early. A cheap winning leg must ride to $1.00 to cover losers. Keep ON.",
    "ENTRY_BAND_GATE_ENABLED": "Master switch for entry-band gate. late_observed_no below MIN or above MAX is skipped; late_observed_yes only trades at/below YES max.",
    "LATE_OBS_NO_MIN_ENTRY": "Lowest entry price a late_observed_no leg may trade at. Below is the proven losing band (<0.50 lost -$96).",
    "LATE_OBS_NO_MAX_ENTRY": "Highest entry price a late_observed_no leg may trade at (too rich = no edge).",
    "LATE_OBS_YES_MAX_ENTRY": "Highest price a late_observed_yes leg may trade at. YES only paid cheap (<$0.10 made +$109).",
    "CITY_THROTTLE_ENABLED": "Master switch for the per-city throttle. Blocklisted cities skipped; over/under-weight cities get a size multiplier.",
    "CITY_BLOCKLIST": "Comma-separated cities to NEVER trade. Free text: /set CITY_BLOCKLIST madrid,tokyo. Empty = block none.",
    "CITY_UNDERWEIGHT": "Comma-separated cities sized DOWN by CITY_UNDERWEIGHT_MULT. Free text via /set.",
    "CITY_OVERWEIGHT": "Comma-separated cities sized UP by CITY_OVERWEIGHT_MULT. Free text via /set.",
    "CITY_UNDERWEIGHT_MULT": "Size multiplier for CITY_UNDERWEIGHT cities (<1.0 trims).",
    "CITY_OVERWEIGHT_MULT": "Size multiplier for CITY_OVERWEIGHT cities (>1.0 boosts).",
    "ADAPTIVE_BOOST_ENABLED": "Master switch for the adaptive per-strategy boost from realized win-rate once enough trades exist.",
    "ADAPTIVE_BOOST_MIN_SAMPLE": "Closed trades a strategy needs before the boost activates (no-op until then).",
    "ADAPTIVE_BOOST_MIN_MULT": "Floor of the adaptive multiplier (worst win-rate).",
    "ADAPTIVE_BOOST_MAX_MULT": "Ceiling of the adaptive multiplier (best win-rate).",
    "ADAPTIVE_BOOST_NEUTRAL_WR": "Win-rate mapped to x1.0 (below trims, above boosts).",
    "MAE_MFE_LOGGING_ENABLED": "Log each position's worst/best price (MAE/MFE) to data/ for backtesting. Observational.",
    "MAE_MFE_SNAPSHOT_EVERY_SCAN": "ON = also append a per-scan price snapshot per open position (full path).",
    "CLUSTER_CONTIGUOUS_ENABLED": "Master switch for the peak-cluster contiguity fix: baskets rebuilt as an unbroken ladder around the peak (no gaps). Gapped lost -$67; contiguous made +$40.",
    "CLUSTER_CONTIGUOUS_FILL_HOLES": "ON = fill an interior missing bucket from the live market (within cost cap) instead of truncating there.",
    "THESIS_EXIT_MAX_ROI_PCT": "Thesis exit fires only when ROI is at/below this %. More negative = more patient. Try -80..-85.",
    "THESIS_EXIT_MIN_ENTRY_PRICE": "Only thesis-exit legs entered at/above this price. Cheap lottery legs left alone.",
    "THESIS_EXIT_MIN_BID": "Skip the thesis exit when the bid is below this (can't sell cleanly).",
    "THESIS_EXIT_MIN_MINUTES_TO_CLOSE": "Skip the thesis exit inside this many minutes of close.",
    "ML_REVIEW_SELL_CONF": "ML confidence needed before the ML review early-SELLs. Higher = fewer sells.",
    "ML_REVIEW_MIN_HOLD_MIN": "Minimum minutes held before the ML review can sell.",
    "ML_REVIEW_MIN_MTC_MIN": "Minimum minutes-to-close before the ML review can act.",
    "ML_REVIEW_MAX_PER_SCAN": "Cap on ML-review early sells per scan.",
    "LATE_OBSERVED_MIN_ENTRY_PRICE": "Lowest price a late_observed leg may be entered at.",
    "LATE_OBSERVED_BASE_FRACTION": "Base fraction of balance for a late_observed leg before weighting.",
    "LATE_OBSERVED_MAX_FRACTION": "Hard cap on fraction of balance a single late_observed leg may use.",
    "LATE_OBSERVED_W_EDGE": "Weight of post-fee edge in late_observed sizing.",
    "LATE_OBSERVED_W_GRADE": "Weight of grade in late_observed sizing.",
    "SIZING_OVERLAY_MIN_MULT": "Lower bound of the sizing-overlay multiplier (trim).",
    "SIZING_OVERLAY_MAX_MULT": "Upper bound of the sizing-overlay multiplier (boost).",
    "SIZING_OVERLAY_NO_LOW_BAND": "Bottom of the proven late_observed_no band that gets boosted.",
    "SIZING_OVERLAY_NO_HIGH_BAND": "Top of the proven late_observed_no band that gets boosted.",
    "TAKEOUT_RESERVE_USD": "Flat $ always held out of trading. Set with /reserve USD.",
    "TAKEOUT_TARGET_USD": "Profit to fence into the untouchable takeout pool. Set with /takeout USD.",
    "TAKEOUT_SKIM_PCT": "Share of each winning trade's PROFIT moved into the pool until target met.",
}

_TAB_HINTS = {
    "lateobs": ["LATE_OBSERVED_YES_ENABLED", "LATE_OBSERVED_NO_ENABLED"],
    "peaker": ["PEAKER_SOLO_ENABLED", "PEAKER_COOL_BASKET_ENABLED", "PEAKER_WARM_BASKET_ENABLED"],
    "exits": ["BASKET_THESIS_EXEMPT", "PROFIT_CAP_EXEMPT_BASKETS", "THESIS_EXIT_MIN_ENTRY_PRICE",
              "THESIS_EXIT_MIN_BID", "THESIS_EXIT_MIN_MINUTES_TO_CLOSE"],
    "cluster": ["CLUSTER_CONTIGUOUS_ENABLED", "CLUSTER_CONTIGUOUS_FILL_HOLES"],
}

_ADVANCED_KEYS = [
    "SIZING_OVERLAY_ENABLED", "SIZING_OVERLAY_MIN_MULT", "SIZING_OVERLAY_MAX_MULT",
    "SIZING_OVERLAY_NO_LOW_BAND", "SIZING_OVERLAY_NO_HIGH_BAND",
    "ADAPTIVE_BOOST_ENABLED", "ADAPTIVE_BOOST_MIN_SAMPLE", "ADAPTIVE_BOOST_MIN_MULT",
    "ADAPTIVE_BOOST_MAX_MULT", "ADAPTIVE_BOOST_NEUTRAL_WR",
    "MAE_MFE_LOGGING_ENABLED", "MAE_MFE_SNAPSHOT_EVERY_SCAN",
    "ML_REVIEW_SELL_CONF", "ML_REVIEW_MIN_HOLD_MIN", "ML_REVIEW_MIN_MTC_MIN",
    "ML_REVIEW_MAX_PER_SCAN",
    "LATE_OBSERVED_MIN_ENTRY_PRICE", "LATE_OBSERVED_BASE_FRACTION",
    "LATE_OBSERVED_MAX_FRACTION", "LATE_OBSERVED_W_EDGE", "LATE_OBSERVED_W_GRADE",
]

_BANDS_KEYS = [
    "ENTRY_BAND_GATE_ENABLED", "LATE_OBS_NO_MIN_ENTRY", "LATE_OBS_NO_MAX_ENTRY",
    "LATE_OBS_YES_MAX_ENTRY",
]

_CITY_KEYS = [
    "CITY_THROTTLE_ENABLED", "CITY_UNDERWEIGHT_MULT", "CITY_OVERWEIGHT_MULT",
    "CITY_BLOCKLIST", "CITY_UNDERWEIGHT", "CITY_OVERWEIGHT",
]

_RESERVE_KEYS = ["TAKEOUT_RESERVE_USD", "TAKEOUT_TARGET_USD", "TAKEOUT_SKIM_PCT"]


def ensure_defaults():
    if Config is None:
        return
    for key, default in BOOL_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)
    for key, (default, _spec) in NUM_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)
    for key, default in STR_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _add_group(store, gid, tab, title, keys):
    groups = getattr(store, "GROUPS", None)
    if not isinstance(groups, list):
        return
    present = [
        k for k in keys
        if k in getattr(store, "BOOL_KEYS", [])
        or k in getattr(store, "NUM_KEYS", {})
        or k in getattr(store, "STR_KEYS", {})
    ]
    if not present:
        return
    for g in groups:
        if isinstance(g, dict) and g.get("id") == gid:
            existing = g.setdefault("keys", [])
            for k in present:
                if k not in existing:
                    existing.append(k)
            return
    groups.append({"id": gid, "tab": tab, "title": title, "keys": list(present)})


def _extend_group(store, gid, keys):
    groups = getattr(store, "GROUPS", None)
    if not isinstance(groups, list):
        return
    for g in groups:
        if isinstance(g, dict) and g.get("id") == gid:
            existing = g.setdefault("keys", [])
            for k in keys:
                if k not in existing:
                    existing.insert(0, k)
            return


def install(store):
    ensure_defaults()

    bool_keys = getattr(store, "BOOL_KEYS", None)
    if isinstance(bool_keys, list):
        for k in BOOL_DEFAULTS:
            if k not in bool_keys:
                bool_keys.append(k)

    num_keys = getattr(store, "NUM_KEYS", None)
    if isinstance(num_keys, dict):
        for k, (_default, spec) in NUM_DEFAULTS.items():
            num_keys.setdefault(k, spec)

    str_keys = getattr(store, "STR_KEYS", None)
    if isinstance(str_keys, dict):
        for k, default in STR_DEFAULTS.items():
            str_keys.setdefault(k, [default])
    free_text = getattr(store, "FREE_TEXT_STR_KEYS", None)
    if isinstance(free_text, set):
        free_text.update(STR_DEFAULTS.keys())

    info = getattr(store, "INFO", None)
    if not isinstance(info, dict):
        info = {}
        setattr(store, "INFO", info)
    info.update(INFO)

    for gid, keys in _TAB_HINTS.items():
        _extend_group(store, gid, keys)
    _add_group(store, "advanced", "Advanced", "Advanced tunables", _ADVANCED_KEYS)
    _add_group(store, "bands", "Bands", "Entry bands", _BANDS_KEYS)
    _add_group(store, "cities", "Cities", "City throttle", _CITY_KEYS)
    _add_group(store, "reserve", "Reserve", "Reserve / Takeout", _RESERVE_KEYS)


ensure_defaults()
