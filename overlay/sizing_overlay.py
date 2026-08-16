"""
SIZING OVERLAY — a bounded, opt-in size multiplier applied on top of the
strategy's own stake (never replaces it).

The post-change analysis (paper_trades (10).csv) shows late_observed_no is the
engine of the bot: +$182 realized at ~73% win rate. Within it, the NO legs
bought in the mid price band pay far more than the cheap-noise band. So this
overlay leans size INTO the proven band and TRIMS the weak band — without
touching the strategy's Kelly math, its hard $ cap, or any other strategy.

Guarantees so it can't "demote" the bot:
  * Only ever multiplies an already-computed size by a factor in
    [SIZING_OVERLAY_MIN_MULT, SIZING_OVERLAY_MAX_MULT] (default 0.85..1.20).
  * Only touches late_observed_no; every other strategy gets 1.0 (no change).
  * The strategy's own hard $ cap still applies downstream (factor Kelly caps at
    LATE_OBSERVED_SIZE_MAX_USD), so a boost can never blow past the ceiling.
  * Master switch SIZING_OVERLAY_ENABLED (default ON) turns it into a pure no-op.
"""

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

SETTING_DEFAULTS = {
    "SIZING_OVERLAY_ENABLED": True,
    "SIZING_OVERLAY_MIN_MULT": 0.85,
    "SIZING_OVERLAY_MAX_MULT": 1.20,
    "SIZING_OVERLAY_NO_LOW_BAND": 0.50,
    "SIZING_OVERLAY_NO_HIGH_BAND": 0.85,
}


def ensure_defaults():
    if Config is None:
        return
    for key, default in SETTING_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _f(name, default):
    if Config is None:
        return default
    try:
        return float(getattr(Config, name, default))
    except (TypeError, ValueError):
        return default


def size_multiplier(strategy, price, edge=0.0) -> float:
    """Return a bounded multiplier for this leg (1.0 = no change)."""
    if Config is None or not bool(getattr(Config, "SIZING_OVERLAY_ENABLED", True)):
        return 1.0
    tag = (strategy or "").strip().lower()
    lo = _f("SIZING_OVERLAY_MIN_MULT", 0.85)
    hi = _f("SIZING_OVERLAY_MAX_MULT", 1.20)
    if tag == "late_observed_no":
        try:
            p = float(price)
        except (TypeError, ValueError):
            return 1.0
        band_lo = _f("SIZING_OVERLAY_NO_LOW_BAND", 0.50)
        band_hi = _f("SIZING_OVERLAY_NO_HIGH_BAND", 0.85)
        if band_lo <= p < band_hi:
            return max(1.0, min(hi, hi))   # proven profitable band -> boost
        if p < band_lo:
            return max(lo, min(1.0, lo))   # weak/cheap band -> trim
        return 1.0                          # near-1.0 no-edge zone -> neutral
    return 1.0


ensure_defaults()
