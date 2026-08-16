"""
Fine-grained STRATEGY TRADE GATE.

Problem this fixes
------------------
The strategies used to be bundled behind coarse master switches: disabling
PEAKER_ENABLED killed the solo peaker AND the peaker cool/warm baskets together,
and there was no way to run late_observed's NO side without its YES side (or vice
versa). The user wants each sub-strategy independently switchable, and — crucially
— when a sub-strategy is OFF it must NOT place a trade even if the scan still
GENERATES a signal for it.

How it works
------------
`trade_allowed(strategy)` is consulted once inside dashboard._place() — the single
placement path for EVERY strategy — right after the global TRADING_ENABLED check.
It reads the fine-grained sub-toggles LIVE from Config (so Telegram /toggle takes
effect on the very next scan) and returns (ok, reason). When ok is False the leg
is dropped before any order is built.

The sub-toggles are INDEPENDENT of each other. The pre-existing master run
switches (LATE_OBSERVED_ENABLED / PEAKER_ENABLED / PEAK_CLUSTER_ENABLED) still
gate whether the engine RUNS in the scan loop; these sub-toggles decide which of
its outputs are allowed to trade. Turning one sub-toggle off never affects the
others.

Fail-open: any strategy tag we don't manage is always allowed, and every toggle
defaults to True, so this gate can only ever STOP a strategy the user explicitly
switched off — it never silently disables a working one.
"""

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None


# New independent sub-toggles owned by this overlay (default ON).
GATE_TOGGLES = {
    "LATE_OBSERVED_YES_ENABLED": True,
    "LATE_OBSERVED_NO_ENABLED": True,
    "PEAKER_SOLO_ENABLED": True,
    "PEAKER_COOL_BASKET_ENABLED": True,
    "PEAKER_WARM_BASKET_ENABLED": True,
}

# strategy tag (lower-case) -> list of sub-toggles that must ALL be ON.
_TAG_TOGGLES = {
    "late_observed_yes": ["LATE_OBSERVED_YES_ENABLED"],
    "late_observed_no": ["LATE_OBSERVED_NO_ENABLED"],
    "peaker": ["PEAKER_SOLO_ENABLED"],
    "peaker_cool_basket": ["PEAKER_COOL_BASKET_ENABLED"],
    "peaker_warm_basket": ["PEAKER_WARM_BASKET_ENABLED"],
    "peak_cluster": ["PEAK_CLUSTER_ENABLED"],
}


def ensure_defaults():
    """Materialise the sub-toggles on Config so the settings snapshot (which
    reads getattr(Config, k, False)) shows them ON by default rather than OFF.
    Only sets a key when it is missing, so persisted overrides always win."""
    if Config is None:
        return
    for key, default in GATE_TOGGLES.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _on(key, default=True):
    if Config is None:
        return default
    return bool(getattr(Config, key, default))


def trade_allowed(strategy):
    """Return (ok, reason). ok=False means: signal may exist, but DON'T trade."""
    tag = (strategy or "").strip().lower()
    toggles = _TAG_TOGGLES.get(tag)
    if not toggles:
        return True, "ok"
    for key in toggles:
        if not _on(key):
            return False, f"{key} is OFF"
    return True, "ok"


# Apply defaults as soon as the module is imported.
ensure_defaults()
