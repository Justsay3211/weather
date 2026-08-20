"""Smoke test the MasterWeatherService single entry point (offline, stdlib).

Injects a fake http_get so no network is needed; verifies the service composes
pipeline -> master brain -> bridge and returns grade/edge features, a buy block,
a support score, a selection plan, and that learning feedback + save() work.
"""
import os, sys, json, tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


class FakeConfig:
    # pipeline on, brain on
    WEATHER_PIPELINE_ENABLED = True
    WEATHER_MASTER_ENABLED = True
    WEATHER_EXECUTION_MODE = "bot"
    WEATHER_SOURCE_LOCATION = ""
    WEATHER_PIPELINE_FORECAST_DAYS = 3
    WEATHER_FORECAST_CACHE_SECONDS = 300
    # only open-meteo (no keys needed) so we stay offline + deterministic
    WEATHER_SRC_OPEN_METEO_ENABLED = True
    WEATHER_SRC_OPENWEATHER_ENABLED = False
    WEATHER_SRC_WEATHERAPI_ENABLED = False
    WEATHER_SRC_VISUALCROSSING_ENABLED = False
    WEATHER_SRC_NWS_ENABLED = False
    WEATHER_SRC_ECMWF_DIRECT_ENABLED = False
    WEATHER_SRC_ECMWF_AIFS_ENABLED = False
    OPEN_METEO_MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless"]
    WEATHER_SELECT_REEVAL_DAYS = 3.0
    WEATHER_SELECT_MIN_INDEPENDENT = 3
    WEATHER_OPTIMIZER_EPSILON = 0.10

    def __init__(self, state_dir):
        self.WEATHER_MASTER_STATE_DIR = state_dir


def _fake_open_meteo(url, params=None, timeout=None):
    """Return an Open-Meteo-shaped multi-model hourly JSON."""
    hours = 72
    times = ["2026-08-20T%02d:00" % (h % 24) for h in range(hours)]
    def temp_curve(offset):
        return [15.0 + 8.0 * __import__("math").sin(__import__("math").pi * (h % 24) / 24.0) + offset
                for h in range(hours)]
    hourly = {"time": times}
    # multiple model suffixes so the pipeline sees several families
    for suf, off in (("ecmwf_ifs025", 0.0), ("gfs_seamless", 0.6), ("icon_seamless", -0.4)):
        hourly["temperature_2m_" + suf] = temp_curve(off)
        hourly["precipitation_" + suf] = [0.0] * hours
        hourly["precipitation_probability_" + suf] = [10.0] * hours
    return {"latitude": 40.7, "longitude": -74.0, "hourly": hourly}


def main():
    from data.weather.master_service import MasterWeatherService, build_master_service
    with tempfile.TemporaryDirectory() as td:
        cfg = FakeConfig(td)
        svc = build_master_service(cfg, bot_http_get=_fake_open_meteo,
                                   vps_available=False, vps_weather_enabled=False)
        check("service builds when pipeline enabled", svc is not None)
        if svc is None:
            print("\n==== %d passed, %d failed ====" % (PASS, FAIL))
            sys.exit(1 if FAIL else 0)

        res = svc.analyze(40.7, -74.0, city="nyc")
        check("analyze returns a result", res is not None)
        check("brain active", res.brain_active is True)
        check("has provenance", isinstance(res.provenance, dict))
        check("grade/edge features produced", isinstance(res.grade_edge_features, dict))
        check("buy block for temp_c", bool(res.buy_blocks.get("temp_c")))
        check("support score in 0..1",
              ("temp_c" not in res.support) or (0.0 <= res.support.get("temp_c", 0.0) <= 1.0))

        plan = svc.selection_plan()
        check("selection plan is a dict", isinstance(plan, dict))

        # learning feedback + persistence
        svc.ingest_observation("nyc", 40.7, "temp_c", 22.5)
        svc.ingest_precip_outcome("nyc", 24.0, 0.3, False)
        svc.save()
        check("skill state persisted", os.path.exists(os.path.join(td, "skill.json")))

        # brain OFF path still returns raw consensus
        cfg.WEATHER_MASTER_ENABLED = False
        svc2 = MasterWeatherService(cfg, bot_http_get=_fake_open_meteo)
        res2 = svc2.analyze(40.7, -74.0, city="nyc")
        check("brain-off degrades gracefully", res2 is not None and res2.brain_active is False)

        # build_master_service returns None when pipeline disabled
        cfg.WEATHER_PIPELINE_ENABLED = False
        check("no service when pipeline disabled",
              build_master_service(cfg, bot_http_get=_fake_open_meteo) is None)

    print("\n==== %d passed, %d failed ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
