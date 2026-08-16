"""
MAE / MFE intra-trade PATH LOGGER (backtest enabler).

The ledger only records entry and exit, so an exit rule (‒90%, −50%,
hedge-switch) can only be ESTIMATED, never proven — we can't see how far a
position dipped or spiked between entry and exit. This overlay closes that gap
without touching the core: on every scan it observes each open position and
tracks:

  * min_price / mae_pct  — Max Adverse Excursion (worst point)
  * max_price / mfe_pct  — Max Favorable Excursion (best point)
  * crossed_-20/-30/-50  — whether ROI ever broke each level
  * a per-scan snapshot appended to data/positions_timeseries.jsonl

At close it finalizes one summary row per position to
data/positions_mae_mfe.jsonl, including the final settlement, so every exit rule
can later be replayed EXACTLY against the recorded path.

Purely observational — it never changes a decision, size, or price. Guarded and
cheap (a dict update + one JSON line per scan). Master switch
MAE_MFE_LOGGING_ENABLED (default ON).
"""

import json
import os
import time

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

TS_PATH = "data/positions_timeseries.jsonl"
SUMMARY_PATH = "data/positions_mae_mfe.jsonl"

SETTING_DEFAULTS = {
    "MAE_MFE_LOGGING_ENABLED": True,
    "MAE_MFE_SNAPSHOT_EVERY_SCAN": True,
}

_TRACK = {}  # pos_id -> {min_price,max_price,mae_pct,mfe_pct,crossed:{...}}

# Per-scan snapshots are BUFFERED in memory and flushed in batches so the hot
# path (called once per open position per scan) never pays a file-open cost.
_BUF = []
_FLUSH_EVERY = 250      # flush after this many buffered snapshot lines
_BUF_CAP = 5000         # hard cap so a wedged disk can't grow memory unbounded


def ensure_defaults():
    if Config is None:
        return
    for key, default in SETTING_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _enabled():
    return Config is None or bool(getattr(Config, "MAE_MFE_LOGGING_ENABLED", True))


def _pid(pos):
    return (getattr(pos, "id", None) or getattr(pos, "token_id", None)
            or f"{getattr(pos, 'city', '')}:{getattr(pos, 'bucket_label', '')}")


def _num(pos, *names):
    """First present numeric attribute among names, else 0.0. Never raises."""
    for n in names:
        try:
            v = getattr(pos, n, None)
            if v is not None:
                return round(float(v), 6)
        except (TypeError, ValueError):
            continue
    return 0.0


def _append(path, row):
    try:
        os.makedirs("data", exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        pass


def _flush_ts():
    """Write all buffered per-scan snapshot lines in a single open/append."""
    global _BUF
    if not _BUF:
        return
    try:
        os.makedirs("data", exist_ok=True)
        with open(TS_PATH, "a") as f:
            f.write("".join(_BUF))
        _BUF = []
    except Exception:
        # Keep the most recent lines only; never let the buffer grow forever.
        if len(_BUF) > _BUF_CAP:
            _BUF = _BUF[-_BUF_CAP:]


def flush():
    """Public flush hook (safe to call on shutdown / between scans)."""
    _flush_ts()


def observe(pos):
    """Record one price observation for an open position (called each scan)."""
    if not _enabled():
        return
    try:
        price = float(getattr(pos, "current_price", 0.0) or 0.0)
        entry = float(getattr(pos, "entry_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        return
    if price <= 0 or entry <= 0:
        return
    pid = _pid(pos)
    roi = (price - entry) / entry * 100.0
    t = _TRACK.get(pid)
    if t is None:
        t = {"entry": entry, "min_price": price, "max_price": price,
             "mae_pct": roi, "mfe_pct": roi,
             "crossed": {"-20": False, "-30": False, "-50": False},
             "strategy": getattr(pos, "strategy", ""), "city": getattr(pos, "city", ""),
             # decision context captured ONCE at entry -> enables what-if research
             "signal": getattr(pos, "signal", "") or "",
             "bucket": getattr(pos, "bucket_label", "") or "",
             "edge": _num(pos, "edge_at_entry", "edge"),
             "grade": _num(pos, "grade"),
             "prob": _num(pos, "our_probability", "prob_win", "p_win"),
             "cost_usd": _num(pos, "cost_basis", "cost_usd", "cost"),
             "shares": _num(pos, "shares")}
        _TRACK[pid] = t
    if price < t["min_price"]:
        t["min_price"] = price
        t["mae_pct"] = roi
    if price > t["max_price"]:
        t["max_price"] = price
        t["mfe_pct"] = roi
    for lvl in (-20, -30, -50):
        if roi <= lvl:
            t["crossed"][str(lvl)] = True
    if Config is None or bool(getattr(Config, "MAE_MFE_SNAPSHOT_EVERY_SCAN", True)):
        _BUF.append(json.dumps({
            "t": time.time(), "id": pid, "strategy": t["strategy"], "city": t["city"],
            "price": round(price, 4), "roi_pct": round(roi, 2),
            "bid": round(float(getattr(pos, "current_bid", 0.0) or 0.0), 4),
        }) + "\n")
        if len(_BUF) >= _FLUSH_EVERY:
            _flush_ts()


def finalize(pos):
    """Emit one summary row when a position closes; clear its tracker."""
    if not _enabled():
        return
    pid = _pid(pos)
    t = _TRACK.pop(pid, None)
    if t is None:
        return
    _flush_ts()  # ensure this position's path is on disk before its summary
    try:
        _append(SUMMARY_PATH, {
            "t": time.time(), "id": pid,
            "strategy": t.get("strategy", ""), "city": t.get("city", ""),
            "entry": round(t.get("entry", 0.0), 4),
            "min_price": round(t.get("min_price", 0.0), 4),
            "max_price": round(t.get("max_price", 0.0), 4),
            "mae_pct": round(t.get("mae_pct", 0.0), 2),
            "mfe_pct": round(t.get("mfe_pct", 0.0), 2),
            "crossed": t.get("crossed", {}),
            "exit_reason": getattr(pos, "exit_reason", ""),
            "exit_price": round(float(getattr(pos, "exit_price", 0.0) or 0.0), 4),
            "realized_pnl": round(float(getattr(pos, "realized_pnl", 0.0) or 0.0), 4),
            "final_status": getattr(pos, "status", ""),
            # decision context (captured at entry) for what-if backtests
            "signal": t.get("signal", ""), "bucket": t.get("bucket", ""),
            "edge": t.get("edge", 0.0), "grade": t.get("grade", 0.0),
            "prob": t.get("prob", 0.0), "cost_usd": t.get("cost_usd", 0.0),
            "shares": t.get("shares", 0.0),
        })
    except Exception:
        pass


ensure_defaults()
