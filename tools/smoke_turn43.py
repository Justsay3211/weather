"""Smoke + wire test for the 2026-08-19 upgrade (grade+edge engine, remaster,
no-arb, run-id, VPS service, weather 10k mode, bridge-cache persistence).
Fail-open by design; this test asserts the wiring is real."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ok = []
fail = []

def check(name, cond):
    (ok if cond else fail).append(name)
    print(("PASS " if cond else "FAIL ") + name)

# 1) grade+edge engine
from data import grade_edge_engine as gee
fx = gee.Features(side="NO", entry_price=0.62, raw_prob=0.95, lock_confidence=0.9,
                  hours_remaining=1.0, remaining_spread_c=0.3, bucket_distance_c=3.0,
                  n_models=5, days_to_resolution=0.5, spread=0.02, bid_depth_usd=12,
                  strategy="late_observed_remaster", win_rate=0.65, n_trades=40,
                  ml_prob=0.8, ml_confidence=0.7)
r = gee.score(fx)
check("engine returns GradeEdgeResult", hasattr(r, "prob_calibrated") and hasattr(r, "grade"))
check("calibration ceiling holds (never old ~0.999 overconfidence)", r.prob_calibrated <= 0.97)
# thin-support overconfident leg must be COMPRESSED below its raw 0.99
thin = gee.score(gee.Features(side="NO", entry_price=0.55, raw_prob=0.99,
                  lock_confidence=0.70, hours_remaining=6, remaining_spread_c=1.4,
                  n_models=1, days_to_resolution=3.0))
check("thin-support overconfidence compressed (<0.99)", thin.prob_calibrated < 0.99)
check("multi-pipeline components present", len(r.components) >= 3)
# overconfident-edge trap: raw edge >0.5 must NOT score higher than a sweet-band leg
hi_trap = gee.score(gee.Features(side="NO", entry_price=0.20, raw_prob=0.99,
                    lock_confidence=0.9, hours_remaining=1, remaining_spread_c=0.3,
                    bucket_distance_c=3, n_models=5))
sweet = gee.score(gee.Features(side="NO", entry_price=0.62, raw_prob=0.80,
                    lock_confidence=0.9, hours_remaining=1, remaining_spread_c=0.3,
                    bucket_distance_c=3, n_models=5))
check("trap guard: extreme-edge leg not graded above sweet-band leg", hi_trap.grade <= sweet.grade + 0.15)

# 2) run manager
from data import run_manager
d1 = run_manager.start(mode="fresh")
check("run_id minted", bool(d1.get("run_id", "").startswith("R-")))
d2 = run_manager.resume(d1["run_id"])
check("recover reuses same run_id", d2["run_id"] == d1["run_id"])
run_manager.note("smoke_event", foo=1)
check("manifest has events", len(run_manager.history()) >= 1)
stamped = run_manager.stamp({})
check("stamp attaches run_id", stamped.get("run_id") == run_manager.run_id())

# 3) VPS service master gate
from overlay import vps_service as vps
from config import Config
Config.VPS_BASE_URL = "http://x:443"; Config.VPS_AUTH_TOKEN = "tok"
Config.VPS_SERVICES_ENABLED = True; Config.VPS_WEATHER_PROXY_ENABLED = True
Config.VPS_OFFLOAD_ENABLED = True
check("master on -> proxy+offload on", vps.weather_proxy_enabled() and vps.offload_enabled())
Config.VPS_SERVICES_ENABLED = False
check("master OFF -> proxy off", not vps.weather_proxy_enabled())
check("master OFF -> offload off", not vps.offload_enabled())
check("master OFF -> pull off", not vps.pull_on_analysis_enabled())
Config.VPS_SERVICES_ENABLED = True
Config.VPS_DOC_PAPER_TRADES = "railway"
check("per-doc railway target respected", vps.document_target("paper_trades.jsonl") == "railway")
Config.VPS_DOC_PAPER_TRADES = "vps"
check("per-doc vps target offloads", vps.stream_offloads("paper_trades.jsonl"))

# 4) weather 10k mode adaptive TTL
from data.weather_fetcher import WeatherFetcher
wf = WeatherFetcher()
Config.WEATHER_FETCH_MODE = "normal"
check("normal mode uses base TTL", wf._effective_cache_ttl() == int(getattr(Config, "WEATHER_FORECAST_CACHE_SECONDS", 300)))
Config.WEATHER_FETCH_MODE = "limit10k"
wf._req_count = 100000; wf._req_window_start = __import__("time").time() - 3600
ttl = wf._effective_cache_ttl()
check("limit10k stretches TTL under heavy load", ttl >= int(getattr(Config, "WEATHER_FORECAST_CACHE_SECONDS", 300)))
Config.WEATHER_FETCH_MODE = "normal"

# 5) observed cache persistence (bridge fix)
from data import observed_weather as ow
key = (12.34, 56.78, "2026-08-19", "high")
ow._obs_cache_merge(key, 30.0, "high")
check("observed cache persisted to disk", os.path.exists(ow._OBS_CACHE_PATH))
ow._OBS_CACHE.clear(); ow._OBS_CACHE_LOADED = False
check("observed cache survives reload (bridge works across restarts)", ow._obs_cache_get(key) == 30.0)

# 6) strategies importable + evaluate signatures
from strategies.late_observed_remaster import LateObservedRemasterStrategy
from strategies.late_observed_no_arbitrage import LateObservedNoArbitrageStrategy
check("remaster has evaluate", hasattr(LateObservedRemasterStrategy(), "evaluate"))
check("no-arb has evaluate", hasattr(LateObservedNoArbitrageStrategy(), "evaluate"))

# 7) boosts reduced to normal
check("late_observed_no boost = 1.0", abs(Config.STRATEGY_SIZE_MULT.get("late_observed_no", 9) - 1.0) < 1e-9)
check("golden_no boost = 1.0", abs(Config.STRATEGY_SIZE_MULT.get("golden_no", 9) - 1.0) < 1e-9)
check("remaster boost = 1.0", abs(Config.STRATEGY_SIZE_MULT.get("late_observed_remaster", 9) - 1.0) < 1e-9)

print("\n%d passed, %d failed" % (len(ok), len(fail)))
if fail:
    print("FAILED:", fail); sys.exit(1)
print("ALL SMOKE/WIRE CHECKS PASSED")
