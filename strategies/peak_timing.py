"""Peak-timing engine for the peaker (and other time-aware strategies).

The user's spec: the peaker must FIND the peak 2-3 days OR ~2 hours before it
happens, ENTER while the market is still cheap (buy low), then DECIDE from the
time-data whether to HOLD to resolution or SELL at a profit.

This module is pure/stdlib and offline-testable. It consumes a normalized
hourly forecast series (list of (datetime, value)) — e.g. the master pipeline's
corrected temperature track — plus the current time and current market price,
and returns a PeakPlan the strategy can act on.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple


@dataclass
class PeakPlan:
    found: bool = False
    peak_value: Optional[float] = None
    peak_time: Optional[datetime] = None
    hours_to_peak: Optional[float] = None
    horizon: str = "none"          # 'multiday' | 'intraday' | 'imminent' | 'none'
    entry_window: bool = False     # True => inside a buy-low window now
    action: str = "wait"           # 'buy' | 'hold' | 'sell' | 'wait'
    reason: str = ""
    confidence: float = 0.0        # 0..1 timing confidence (sharpness of the peak)


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def find_daily_peak(series: List[Tuple[datetime, float]],
                    now: datetime,
                    day_offset: int = 0) -> Optional[Tuple[datetime, float]]:
    """Return (time, value) of the max on the calendar day now+day_offset (UTC)."""
    now = _as_utc(now)
    target_day = (now + timedelta(days=day_offset)).date()
    best = None
    for dt, val in series:
        if val is None:
            continue
        dt = _as_utc(dt)
        if dt.date() != target_day:
            continue
        if best is None or val > best[1]:
            best = (dt, float(val))
    return best


def _sharpness(series, peak_time, peak_value, window_h=6):
    """How pronounced the peak is vs its neighbourhood (0..1). A sharp, clearly
    defined peak is more tradeable than a flat plateau."""
    peak_time = _as_utc(peak_time)
    lo = peak_time - timedelta(hours=window_h)
    hi = peak_time + timedelta(hours=window_h)
    neigh = [v for (t, v) in series if v is not None and lo <= _as_utc(t) <= hi]
    if len(neigh) < 3:
        return 0.3
    mean = sum(neigh) / len(neigh)
    drop = peak_value - mean
    return max(0.0, min(1.0, drop / 3.0))   # ~3C above local mean = very sharp


def plan_peak(series: List[Tuple[datetime, float]],
              now: datetime,
              market_price: Optional[float] = None,
              held: bool = False,
              entry_price: Optional[float] = None,
              *,
              lookahead_days: int = 3,
              intraday_entry_hours: float = 2.0,
              multiday_min_hours: float = 24.0,
              multiday_max_hours: float = 72.0,
              cheap_price: float = 0.45,
              take_profit: float = 0.20,
              hold_to_resolution_hours: float = 6.0) -> PeakPlan:
    """Build a PeakPlan.

    Entry (buy-low) windows:
      * MULTIDAY: peak is multiday_min..multiday_max hours away (2-3 days) AND the
        market is still cheap (price < cheap_price) => buy low before the crowd.
      * INTRADAY: peak is within intraday_entry_hours (~2h) and not yet reached
        => last-chance cheap entry just before the peak locks.

    Exit (when held):
      * SELL if we have >= take_profit unrealized profit and the peak has passed
        (value now declining) OR the peak is imminent and price already rich.
      * HOLD if within hold_to_resolution_hours of resolution (let it settle) or
        still climbing toward an unrealised peak.
    """
    now = _as_utc(now)
    if not series:
        return PeakPlan(found=False, reason="no forecast series")

    # find the best peak across today..+lookahead_days
    best = None
    for d in range(0, max(1, lookahead_days) + 1):
        cand = find_daily_peak(series, now, day_offset=d)
        if cand and (best is None or cand[1] > best[1]):
            best = cand
    if best is None:
        return PeakPlan(found=False, reason="no peak in horizon")
    peak_time, peak_value = best
    hours_to_peak = (peak_time - now).total_seconds() / 3600.0
    sharp = _sharpness(series, peak_time, peak_value)

    plan = PeakPlan(found=True, peak_value=peak_value, peak_time=peak_time,
                    hours_to_peak=hours_to_peak, confidence=sharp)

    if hours_to_peak >= multiday_max_hours:
        plan.horizon = "multiday"
    elif hours_to_peak >= multiday_min_hours:
        plan.horizon = "multiday"
    elif hours_to_peak > intraday_entry_hours:
        plan.horizon = "intraday"
    else:
        plan.horizon = "imminent"

    # ---- exit logic when already holding -----------------------------
    if held:
        profit = None
        if market_price is not None and entry_price is not None:
            profit = market_price - entry_price
        # peak passed => value now < peak (declining) => lock profit
        past_peak = hours_to_peak <= 0
        if profit is not None and profit >= take_profit and (past_peak or (market_price or 0) >= 0.85):
            plan.action = "sell"
            plan.reason = "take profit %.2f (peak %s)" % (
                profit, "passed" if past_peak else "rich")
            return plan
        if 0 <= hours_to_peak <= hold_to_resolution_hours:
            plan.action = "hold"
            plan.reason = "near peak/resolution — hold to settle"
            return plan
        plan.action = "hold"
        plan.reason = "still climbing to peak (%.1fh out)" % hours_to_peak
        return plan

    # ---- entry logic when flat ---------------------------------------
    cheap = (market_price is None) or (market_price < cheap_price)
    if plan.horizon == "multiday" and multiday_min_hours <= hours_to_peak <= multiday_max_hours and cheap:
        plan.entry_window = True
        plan.action = "buy"
        plan.reason = "multiday peak in %.0fh, price still cheap — buy low" % hours_to_peak
        return plan
    if plan.horizon == "imminent" and 0 < hours_to_peak <= intraday_entry_hours and cheap:
        plan.entry_window = True
        plan.action = "buy"
        plan.reason = "peak in %.1fh, last cheap entry — buy low" % hours_to_peak
        return plan

    plan.action = "wait"
    if not cheap:
        plan.reason = "peak in %.0fh but price already rich (%.2f)" % (
            hours_to_peak, market_price or 0)
    else:
        plan.reason = "peak in %.0fh — outside entry window" % hours_to_peak
    return plan
