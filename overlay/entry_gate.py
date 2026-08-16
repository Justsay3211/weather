"""
ENTRY-BAND GATE.

The ledger analysis is unambiguous about WHERE each strategy makes and loses
money by ENTRY PRICE:

  late_observed_no  — the engine, but ONLY in the 0.50–0.85 band (69–79% WR,
                      +$271). The <0.50 band is a net loser (-$96, 0–45% WR).
  late_observed_yes — a lottery that only pays when bought CHEAP (<~$0.10
                      band +$109). Above ~$0.20 it bleeds.

Neither of these bands can be gated from Telegram (LATE_OBSERVED_NO_MIN_PRICE
caps at 0.20), so this overlay adds a hard entry-price gate at the single
placement path. It is consulted alongside strategy_gate.trade_allowed():
a blocked leg is dropped BEFORE any order is built.

All thresholds are live-tunable (registered in settings). Master switch
ENTRY_BAND_GATE_ENABLED (default ON). Fail-open on any error.
"""

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

SETTING_DEFAULTS = {
    "ENTRY_BAND_GATE_ENABLED": True,
    "LATE_OBS_NO_MIN_ENTRY": 0.50,   # skip late_observed_no below this price
    "LATE_OBS_NO_MAX_ENTRY": 0.97,   # skip the near-1.0 no-edge tail
    "LATE_OBS_YES_MIN_ENTRY": 0.20,  # skip late_observed_yes below this price (cheap-longshot junk band)
    "LATE_OBS_YES_MAX_ENTRY": 0.50,  # skip late_observed_yes above this price (pays too much for the win)
    # --- GLOBAL LONGSHOT FLOOR (added 2026-07-27) ---------------------------
    # The 3-month audit is blunt: single-leg longshots (<~0.35) bleed across
    # every directional strategy while favourites (>0.60) print. This is a
    # LAST-LINE floor applied to ALL strategies EXCEPT the multi-leg basket
    # strategies (peak_cluster / peaker baskets), where cheap legs are the
    # whole point (basket cost gate handles those). A real forecast edge can
    # still punch through the floor (see GLOBAL_MIN_ENTRY_EDGE below).
    "GLOBAL_MIN_ENTRY_GATE_ENABLED": True,
    "GLOBAL_MIN_ENTRY_PRICE": 0.35,  # directional longshots below this are junk unless a strong edge exists
    "GLOBAL_MIN_ENTRY_EDGE": 0.15,   # model_prob - price must beat this to override the floor
    # --- late_observed_yes forecast-edge gate (added 2026-07-27) ------------
    # Keep the user's [0.20, 0.50] band, but ALSO require the model to actually
    # like the YES side: only fire when model_prob - price >= this edge. This
    # is what turns late_observed_yes from a 0% WR lottery into a real signal
    # without touching the band the user set. Needs a model_prob to apply;
    # if none is passed it fails-open (band gate still applies).
    "LATE_OBS_YES_MIN_EDGE": 0.10,
}

# Basket strategies are EXEMPT from the global longshot floor: their cheap
# legs are intentional and are governed by their own basket-cost gate.
_GLOBAL_FLOOR_EXEMPT = frozenset({
    "peak_cluster", "peaker", "peaker_cool", "peaker_warm",
    "peaker_cool_basket", "peaker_warm_basket",
})


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


def entry_allowed(strategy, entry_price, model_prob=None):
    """Return (ok, reason). ok=False => price outside the profitable band.

    model_prob (optional): the model's estimated win probability for this leg.
    When provided it powers two edge checks: the late_observed_yes edge gate
    and the global-floor override. When omitted, both fail-open gracefully.
    """
    if Config is None or not bool(getattr(Config, "ENTRY_BAND_GATE_ENABLED", True)):
        return True, "ok"
    try:
        p = float(entry_price)
    except (TypeError, ValueError):
        return True, "ok"
    try:
        mp = float(model_prob) if model_prob is not None else None
    except (TypeError, ValueError):
        mp = None
    tag = (strategy or "").strip().lower()
    edge = (mp - p) if mp is not None else None

    if tag == "late_observed_no":
        lo = _f("LATE_OBS_NO_MIN_ENTRY", 0.50)
        hi = _f("LATE_OBS_NO_MAX_ENTRY", 0.97)
        if p < lo:
            return False, f"late_observed_no @ {p:.2f} < {lo:.2f} (junk band)"
        if p > hi:
            return False, f"late_observed_no @ {p:.2f} > {hi:.2f} (no-edge tail)"
        return True, "ok"
    if tag == "late_observed_yes":
        lo = _f("LATE_OBS_YES_MIN_ENTRY", 0.20)
        hi = _f("LATE_OBS_YES_MAX_ENTRY", 0.50)
        if p < lo:
            return False, f"late_observed_yes @ {p:.2f} < {lo:.2f} (cheap-longshot junk band)"
        if p > hi:
            return False, f"late_observed_yes @ {p:.2f} > {hi:.2f} (pays too much for the win)"
        # Forecast-edge gate: the band alone let a 0% WR lottery through. Only
        # trade YES when the model genuinely favours it over the price.
        min_edge = _f("LATE_OBS_YES_MIN_EDGE", 0.10)
        if min_edge > 0 and edge is not None and edge < min_edge:
            return False, (f"late_observed_yes @ {p:.2f} edge {edge:+.2f} < "
                           f"{min_edge:.2f} (model doesn't favour YES enough)")
        return True, "ok"

    # ---- GLOBAL LONGSHOT FLOOR (all other directional strategies) ----------
    if bool(getattr(Config, "GLOBAL_MIN_ENTRY_GATE_ENABLED", True)) and tag not in _GLOBAL_FLOOR_EXEMPT:
        floor = _f("GLOBAL_MIN_ENTRY_PRICE", 0.35)
        if floor > 0 and p < floor:
            # A strong forecast edge can still punch through the floor.
            need = _f("GLOBAL_MIN_ENTRY_EDGE", 0.15)
            if edge is not None and need > 0 and edge >= need:
                return True, f"{tag or 'trade'} @ {p:.2f} below floor but edge {edge:+.2f} >= {need:.2f}"
            return False, (f"{tag or 'trade'} @ {p:.2f} < {floor:.2f} "
                           f"(global longshot floor)")
    return True, "ok"


ensure_defaults()
