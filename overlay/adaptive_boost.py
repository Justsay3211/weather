"""
ADAPTIVE PER-STRATEGY BOOST.

A self-tuning size multiplier that leans capital toward whichever strategies are
ACTUALLY winning in this account's realized history, and trims those that are
bleeding — the "offline/adaptive ML boost by realized win/loss" idea.

How it works:
  * _close_position reports every realized outcome via record(strategy, pnl).
  * Per-strategy running (wins, losses, pnl) persist to data/strategy_stats.json.
  * multiplier(strategy) maps realized win-rate to a BOUNDED factor in
    [MIN, MAX] (default 0.6..1.4), centred on a neutral win-rate, and only after
    a minimum sample (default 15 closes) — so it is a pure no-op until there is
    enough evidence, and can never swing size wildly.

Held in memory, persisted on change (no per-trade disk reads in the hot path).
Master switch ADAPTIVE_BOOST_ENABLED (default ON). Fail-open.
"""

import json
import os

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

STATE_PATH = "data/strategy_stats.json"

SETTING_DEFAULTS = {
    "ADAPTIVE_BOOST_ENABLED": True,
    "ADAPTIVE_BOOST_MIN_SAMPLE": 15,
    "ADAPTIVE_BOOST_MIN_MULT": 0.6,
    "ADAPTIVE_BOOST_MAX_MULT": 1.4,
    "ADAPTIVE_BOOST_NEUTRAL_WR": 0.45,   # win-rate that maps to 1.0x
}

_STATE = None  # {strategy: {"w": int, "l": int, "pnl": float}}


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


def _load():
    global _STATE
    if _STATE is None:
        try:
            with open(STATE_PATH) as f:
                _STATE = json.load(f)
        except Exception:
            _STATE = {}
    return _STATE


def _save():
    try:
        os.makedirs("data", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(_STATE, f, indent=2)
    except Exception:
        pass


def record(strategy, pnl):
    tag = (strategy or "").strip().lower()
    if not tag:
        return
    st = _load()
    row = st.setdefault(tag, {"w": 0, "l": 0, "pnl": 0.0})
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        pnl = 0.0
    if pnl > 1e-9:
        row["w"] += 1
    elif pnl < -1e-9:
        row["l"] += 1
    row["pnl"] = float(row.get("pnl", 0.0)) + pnl
    _save()


def multiplier(strategy):
    if Config is None or not bool(getattr(Config, "ADAPTIVE_BOOST_ENABLED", True)):
        return 1.0
    tag = (strategy or "").strip().lower()
    st = _load()
    row = st.get(tag)
    if not row:
        return 1.0
    n = int(row.get("w", 0)) + int(row.get("l", 0))
    if n < int(_f("ADAPTIVE_BOOST_MIN_SAMPLE", 15)):
        return 1.0
    wr = row["w"] / n if n else 0.0
    neutral = _f("ADAPTIVE_BOOST_NEUTRAL_WR", 0.45)
    lo = _f("ADAPTIVE_BOOST_MIN_MULT", 0.6)
    hi = _f("ADAPTIVE_BOOST_MAX_MULT", 1.4)
    # Linear map: neutral WR -> 1.0; scale up toward hi as WR->1, down toward lo
    # as WR->0. Clamped to [lo, hi].
    if wr >= neutral:
        span = (1.0 - neutral) or 1.0
        mult = 1.0 + (wr - neutral) / span * (hi - 1.0)
    else:
        span = neutral or 1.0
        mult = 1.0 - (neutral - wr) / span * (1.0 - lo)
    return max(lo, min(hi, mult))


def stats():
    return dict(_load())


ensure_defaults()
