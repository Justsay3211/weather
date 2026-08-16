"""
RESERVE + TAKEOUT.

Two user-facing capital controls, both kept OUT of the trading engine's hot path
and exposed over Telegram (/reserve, /takeout):

1. RESERVE (TAKEOUT_RESERVE_USD): a flat dollar amount that is always held back
   from trading, on top of the existing percentage portfolio reserve. Adjustable
   live.

2. TAKEOUT: the user says e.g. "I want $100 out of the bot". Set a target with
   /takeout 100. From then on, every winning trade contributes a share
   (TAKEOUT_SKIM_PCT, default 50%) of its realized PROFIT into an untouchable
   "takeout pool" until the pool reaches the target. Pool money physically stays
   in the balance but is FENCED: the portfolio guard subtracts it from the
   deployable balance, so it is never used in a trade. /takeout withdraw cashes
   the pool out (paper: debits balance + deposited so the ledger identity holds;
   live: clears the fence and tells the user to move it on-chain).

State (pool + lifetime withdrawn) lives in data/reserve_takeout.json and is held
in memory so locked_total() — called by the portfolio guard on every candidate
trade — never hits disk.
"""

import json
import os

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

try:
    from logger import log
except Exception:  # pragma: no cover
    class _Nop:
        def __getattr__(self, _):
            return lambda *a, **k: None
    log = _Nop()

STATE_PATH = "data/reserve_takeout.json"

# Settings that live on Config (registered as tunables in settings_store via
# advanced_settings, so /set + persistence work automatically).
SETTING_DEFAULTS = {
    "TAKEOUT_RESERVE_USD": 0.0,
    "TAKEOUT_TARGET_USD": 0.0,
    "TAKEOUT_SKIM_PCT": 0.5,
}

_STATE = None  # in-memory cache: {"pool_usd": float, "withdrawn_total": float}


def ensure_defaults():
    if Config is None:
        return
    for key, default in SETTING_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _cfg(name, default):
    if Config is None:
        return default
    try:
        return float(getattr(Config, name, default) or 0.0)
    except (TypeError, ValueError):
        return default


def _load():
    global _STATE
    if _STATE is None:
        try:
            with open(STATE_PATH) as f:
                _STATE = json.load(f)
        except Exception:
            _STATE = {"pool_usd": 0.0, "withdrawn_total": 0.0}
        _STATE.setdefault("pool_usd", 0.0)
        _STATE.setdefault("withdrawn_total", 0.0)
    return _STATE


def _save(state):
    global _STATE
    _STATE = state
    try:
        os.makedirs("data", exist_ok=True)
        with open(STATE_PATH, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:  # pragma: no cover
        log.debug(f"reserve_takeout persist failed: {e}")


def pool_usd() -> float:
    return float(_load().get("pool_usd", 0.0))


def reserve_usd() -> float:
    return max(0.0, _cfg("TAKEOUT_RESERVE_USD", 0.0))


def locked_total() -> float:
    """Dollars fenced off from the deployable balance (reserve + takeout pool)."""
    return max(0.0, reserve_usd() + pool_usd())


def on_realized_pnl(pnl) -> float:
    """Fence a share of a realized WIN's profit toward the takeout target.
    Returns the amount skimmed (0.0 when disabled / target already met)."""
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return 0.0
    if pnl <= 0:
        return 0.0
    target = _cfg("TAKEOUT_TARGET_USD", 0.0)
    skim = _cfg("TAKEOUT_SKIM_PCT", 0.0)
    if target <= 0 or skim <= 0:
        return 0.0
    state = _load()
    pool = float(state.get("pool_usd", 0.0))
    room = target - pool
    if room <= 0:
        return 0.0
    amt = min(pnl * skim, room)
    if amt <= 0:
        return 0.0
    state["pool_usd"] = pool + amt
    _save(state)
    log.info(f"\U0001F3E6 TAKEOUT skim +${amt:.2f} -> pool ${state['pool_usd']:.2f}/${target:.2f}")
    return amt


def withdraw(pm):
    """Cash out the takeout pool. Returns (ok, message)."""
    state = _load()
    pool = float(state.get("pool_usd", 0.0))
    if pool <= 0:
        return False, "\U0001F3E6 Takeout pool is empty \u2014 set a target with /takeout USD first."
    is_paper = True
    try:
        is_paper = bool(Config.is_paper()) if Config is not None else True
    except Exception:
        is_paper = True
    if is_paper and pm is not None:
        try:
            pm.paper_balance = max(0.0, float(pm.paper_balance) - pool)
            if hasattr(pm, "total_deposited"):
                pm.total_deposited = max(0.0, float(pm.total_deposited) - pool)
            if hasattr(pm, "_save_state"):
                pm._save_state()
        except Exception as e:  # pragma: no cover
            log.debug(f"takeout withdraw balance update failed: {e}")
        state["pool_usd"] = 0.0
        state["withdrawn_total"] = float(state.get("withdrawn_total", 0.0)) + pool
        _save(state)
        bal = getattr(pm, "paper_balance", 0.0)
        return True, (f"\U0001F3E6 Withdrew ${pool:.2f} from the takeout pool (paper). "
                      f"New balance ${bal:.2f}. Lifetime withdrawn ${state['withdrawn_total']:.2f}.")
    # Live: we can't move on-chain funds here; clear the fence and instruct.
    state["pool_usd"] = 0.0
    state["withdrawn_total"] = float(state.get("withdrawn_total", 0.0)) + pool
    _save(state)
    return True, (f"\U0001F3E6 Cleared the ${pool:.2f} takeout fence. In LIVE mode, move that "
                  f"${pool:.2f} out of your wallet on-chain \u2014 it is no longer reserved by the bot.")


def status(pm=None) -> dict:
    state = _load()
    pool = float(state.get("pool_usd", 0.0))
    reserve = reserve_usd()
    bal = None
    deployable = None
    if pm is not None:
        try:
            bal = float(pm.get_balance())
            deployable = max(0.0, bal - locked_total())
        except Exception:
            bal = None
    return {
        "reserve_usd": reserve,
        "pool_usd": pool,
        "target_usd": _cfg("TAKEOUT_TARGET_USD", 0.0),
        "skim_pct": _cfg("TAKEOUT_SKIM_PCT", 0.0),
        "withdrawn_total": float(state.get("withdrawn_total", 0.0)),
        "locked_total": locked_total(),
        "balance": bal,
        "deployable": deployable,
    }


ensure_defaults()
