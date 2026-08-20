"""Smoke test: bridge (master -> grade/edge + buy message) and peak_timing.
Run: python3 tools/smoke_bridge_timing.py
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.weather.bridge import support_score, features_from_master, buy_message_block
from data.weather.master import MasterForecastResult, VariableForecast
from strategies.peak_timing import plan_peak, find_daily_peak
import data.grade_edge_engine as gee

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name)


NOW = datetime(2026, 8, 20, 6, 0, tzinfo=timezone.utc)


def _mk_master(conf, n_ind, prob=0.7, method="gbm", warns=0):
    mf = MasterForecastResult(location="NYC", when=NOW)
    vf = VariableForecast(variable="temp_c", estimate=31.2, low=29.8, high=32.6,
                          probability=prob, confidence=conf,
                          confidence_label="HIGH", correction_method=method,
                          n_independent=n_ind, reasons=["models agree", "strong skill"])
    mf.variables["temp_c"] = vf
    mf.n_independent = n_ind
    mf.warnings = ["stale feed"] * warns
    mf.model_log = ["USED  ECMWF_IFS    <- open_meteo", "USED  NOAA_GFS     <- open_meteo",
                    "USED  DWD_ICON     <- open_meteo"]
    return mf


def test_bridge():
    print("\n[bridge] master -> support / features / buy message")
    hi = _mk_master(88, 4)
    lo = _mk_master(35, 1, prob=0.5, method="raw", warns=2)
    s_hi = support_score(hi)
    s_lo = support_score(lo)
    check("high-quality forecast scores higher support", s_hi > s_lo)
    check("support bounded 0..1", 0.0 <= s_hi <= 1.0 and 0.0 <= s_lo <= 1.0)

    feats = features_from_master(hi)
    check("features expose wx_confidence", feats.get("wx_confidence") == 88.0)
    check("features expose wx_n_independent", feats.get("wx_n_independent") == 4)

    # feed into grade_edge and confirm high master confidence lifts grade
    base = gee.Features(side="NO", entry_price=0.62, raw_prob=0.7,
                        lock_confidence=0.75, remaining_spread_c=0.8, n_models=4)
    r_no_wx = gee.score(base)
    wx = gee.Features(side="NO", entry_price=0.62, raw_prob=0.7,
                      lock_confidence=0.75, remaining_spread_c=0.8, n_models=4,
                      **features_from_master(hi))
    r_hi = gee.score(wx)
    wx_lo = gee.Features(side="NO", entry_price=0.62, raw_prob=0.7,
                         lock_confidence=0.75, remaining_spread_c=0.8, n_models=4,
                         **features_from_master(lo))
    r_lo = gee.score(wx_lo)
    check("weather pipeline appears in components", "weather" in r_hi.components)
    check("high master confidence lifts grade vs low", r_hi.grade > r_lo.grade)
    check("high master confidence lifts edge vs low", r_hi.edge > r_lo.edge)
    check("grade still bounded <=1", r_hi.grade <= 1.0)

    msg = buy_message_block(hi)
    check("buy message shows confidence", "Confidence:" in msg and "88/100" in msg)
    check("buy message shows calibrated chance", "Calibrated chance:" in msg)
    check("buy message shows models", "ECMWF_IFS" in msg)
    lo_msg = buy_message_block(lo)
    check("low-quality buy message surfaces warning", "⚠" in lo_msg)


def _peak_series(peak_day_offset, peak_hour=20, base=20.0, amp=10.0, days=4):
    """Diurnal series peaking at peak_hour; the chosen day gets the highest amp."""
    import math
    series = []
    for d in range(days):
        for h in range(24):
            t = (NOW + timedelta(days=d)).replace(hour=h, minute=0)
            day_amp = amp + (3.0 if d == peak_day_offset else 0.0)
            val = base + day_amp * math.sin(math.pi * max(0, min(24, h)) / 24.0)
            if h == peak_hour and d == peak_day_offset:
                val += 2.0
            series.append((t, val))
    return series


def test_peak_timing():
    print("\n[peak_timing] find peak, buy-low window, hold/sell")
    series = _peak_series(peak_day_offset=2, peak_hour=20)
    dp = find_daily_peak(series, NOW, day_offset=2)
    check("daily peak found on target day", dp is not None and dp[0].date() == (NOW + timedelta(days=2)).date())

    # multiday: peak ~2 days out, market cheap -> BUY
    plan = plan_peak(series, NOW, market_price=0.30)
    check("multiday horizon detected", plan.horizon == "multiday")
    check("multiday cheap -> buy-low", plan.action == "buy" and plan.entry_window)

    # multiday but price rich -> WAIT (no no-edge chase)
    plan_rich = plan_peak(series, NOW, market_price=0.80)
    check("rich price -> wait", plan_rich.action == "wait")

    # the synthetic diurnal curve peaks at local noon; sample relative to that.
    # imminent: ~1.5h before the noon peak, cheap -> BUY
    near = (NOW + timedelta(days=2)).replace(hour=10, minute=30)
    plan_intra = plan_peak(series, near, market_price=0.40)
    check("imminent (~2h) last-cheap-entry -> buy", plan_intra.action == "buy" and plan_intra.horizon == "imminent")

    # held, past peak, in profit -> SELL
    after = (NOW + timedelta(days=2)).replace(hour=14, minute=0)
    plan_sell = plan_peak(series, after, market_price=0.90, held=True, entry_price=0.40)
    check("held + past peak + profit -> sell", plan_sell.action == "sell")

    # held, climbing to peak -> HOLD
    before = (NOW + timedelta(days=2)).replace(hour=9, minute=0)
    plan_hold = plan_peak(series, before, market_price=0.55, held=True, entry_price=0.40)
    check("held + climbing -> hold", plan_hold.action == "hold")


def main():
    test_bridge()
    test_peak_timing()
    print("\n==== %d passed, %d failed ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
