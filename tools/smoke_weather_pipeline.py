"""Offline smoke + wire test for the data/weather pipeline package.

No network: an injected http_get returns captured/synthetic payloads so every
layer (adapters, QC, dedup, consensus, confidence, execution routing, budgets,
circuit breaker) is exercised deterministically.

Run: python tools/smoke_weather_pipeline.py
"""

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.weather import (  # noqa: E402
    WeatherPipeline, ExecutionRouter, Location, effective_independent_count,
)
from data.weather.sources import (  # noqa: E402
    OpenMeteoAdapter, WeatherApiAdapter, OpenWeatherAdapter,
    VisualCrossingAdapter, NwsAdapter,
)
from data.weather.schema import ForecastSeries, ForecastPoint, SourceIdentity, Category

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


BASE = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _hours(n):
    return [(BASE + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(n)]


def fake_open_meteo():
    t = _hours(6)
    def series(base):
        return [base + i * 0.1 for i in range(6)]
    return {
        "hourly": {
            "time": t,
            "temperature_2m_ecmwf_ifs025": series(20.0),
            "precipitation_ecmwf_ifs025": [0, 0, 1.0, 0, 0, 0],
            "temperature_2m_gfs_global": series(21.0),
            "precipitation_gfs_global": [0, 0, 0.5, 0, 0, 0],
            "temperature_2m_icon_global": series(20.5),
            "precipitation_icon_global": [0, 0, 0.8, 0, 0, 0],
        }
    }


def fake_weatherapi():
    hrs = [{"time": (BASE + timedelta(hours=i)).strftime("%Y-%m-%d %H:%M"),
            "temp_c": 25.0 + i * 0.1, "precip_mm": 0.0, "humidity": 55,
            "cloud": 10, "wind_kph": 12, "wind_degree": 200,
            "chance_of_rain": 20} for i in range(6)]
    return {"forecast": {"forecastday": [{"hour": hrs}]}}


def fake_openweather():
    lst = [{"dt": int((BASE + timedelta(hours=3 * i)).timestamp()),
            "main": {"temp": 19.0 + i, "humidity": 60, "pressure": 1005},
            "wind": {"speed": 4.0, "deg": 250}, "clouds": {"all": 20},
            "pop": 0.1} for i in range(3)]
    return {"list": lst}


def make_http(mapping):
    def http_get(url, params, headers):
        for key, payload in mapping.items():
            if key in url:
                return payload
        raise RuntimeError("no mock for " + url)
    return http_get


def test_dedup_and_consensus():
    print("[1] identity dedup + consensus")
    om = OpenMeteoAdapter(models=["ecmwf_ifs025", "gfs_global", "icon_global"])
    wapi = WeatherApiAdapter(api_key="x")
    ow = OpenWeatherAdapter(api_key="y")
    http = make_http({"open-meteo": fake_open_meteo(),
                      "weatherapi": fake_weatherapi(),
                      "openweathermap": fake_openweather()})
    router = ExecutionRouter(mode="bot")
    pipe = WeatherPipeline([om, wapi, ow], router, bot_http_get=http,
                           now_fn=lambda: BASE)
    res = pipe.run(52.5, 13.4, "Berlin", when=BASE + timedelta(hours=2))
    # 3 raw families (ECMWF/GFS/ICON) + 2 provider families = 5 independent
    check("5 independent model families", res.n_independent == 5)
    check("temp consensus present", res.consensus["temp_c"].median is not None)
    check("temp uses 5 family votes", res.consensus["temp_c"].n_independent == 5)
    check("confidence computed", 0 <= res.confidence["temp_c"].score <= 100)
    print("     temp median=%.2f conf=%d (%s)" % (
        res.consensus["temp_c"].median, res.confidence["temp_c"].score,
        res.confidence["temp_c"].label))


def test_provider_dedup_collapse():
    print("[2] duplicate model family collapses to one vote")
    # two Open-Meteo adapters both exposing ECMWF -> must count once
    om1 = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    s1 = om1.parse(fake_open_meteo(), 52.5, 13.4, "Berlin")
    dup = ForecastSeries(identity=SourceIdentity(
        source="ecmwf:direct", provider="ecmwf", model_family="ECMWF_IFS",
        category=Category.RAW_MODEL), points=s1[0].points)
    n = effective_independent_count(s1 + [dup])
    check("ECMWF provider+direct == 1 family", n == 1)


def test_execution_routing():
    print("[3] execution routing (vps / bot / off / vps-only no-fallback)")
    # off
    r = ExecutionRouter(mode="bot", source_overrides={"weatherapi": "off"})
    check("weatherapi off", r.resolve("weatherapi") == Location.OFF)
    check("open_meteo bot", r.resolve("open_meteo") == Location.BOT)
    # vps master off -> vps falls back to bot
    r2 = ExecutionRouter(mode="vps", vps_weather_enabled=False)
    check("vps master off -> bot", r2.resolve("open_meteo") == Location.BOT)
    # vps enabled but unreachable -> stays vps (no unoptimal direct)
    r3 = ExecutionRouter(mode="vps", vps_weather_enabled=True, vps_available=False)
    check("vps-only stays vps when unreachable", r3.resolve("open_meteo") == Location.VPS)
    # auto prefers vps when usable
    r4 = ExecutionRouter(mode="auto", vps_weather_enabled=True, vps_available=True)
    check("auto -> vps when usable", r4.resolve("open_meteo") == Location.VPS)
    r5 = ExecutionRouter(mode="auto", vps_weather_enabled=False, vps_available=False)
    check("auto -> bot when vps off", r5.resolve("open_meteo") == Location.BOT)


def test_vps_only_no_direct_call():
    print("[4] vps-only never makes a direct bot call")
    calls = {"n": 0}
    def http(url, params, headers):
        calls["n"] += 1
        return fake_open_meteo()
    om = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    router = ExecutionRouter(mode="vps", vps_weather_enabled=True, vps_available=False)
    # vps_fetch is None -> should skip, NOT call http
    pipe = WeatherPipeline([om], router, bot_http_get=http, vps_fetch=None,
                           now_fn=lambda: BASE)
    res = pipe.run(52.5, 13.4, "Berlin", when=BASE)
    check("zero direct http calls under vps-only", calls["n"] == 0)
    check("source excluded with skip reason",
          "open_meteo" in res.excluded and "skipped" in res.excluded["open_meteo"])


def test_vps_delegation():
    print("[5] vps delegation path returns series via proxy")
    om = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    def vps_fetch(provider, lat, lon, city):
        return om.parse(fake_open_meteo(), lat, lon, city)
    router = ExecutionRouter(mode="vps", vps_weather_enabled=True, vps_available=True)
    pipe = WeatherPipeline([om], router, vps_fetch=vps_fetch, now_fn=lambda: BASE)
    res = pipe.run(52.5, 13.4, "Berlin", when=BASE)
    check("series fetched via vps", len(res.series) == 1)
    check("executed_on == vps", res.series[0].executed_on == Location.VPS)
    check("quota counts vps request", pipe.quota_snapshot()["by_location"]["vps"] == 1)


def test_budget_and_breaker():
    print("[6] budget + circuit breaker + cache")
    om = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    om.daily_budget = 1
    calls = {"n": 0}
    def http(url, params, headers):
        calls["n"] += 1
        return fake_open_meteo()
    router = ExecutionRouter(mode="bot")
    pipe = WeatherPipeline([om], router, bot_http_get=http, cache_ttl_s=0,
                           now_fn=lambda: BASE)
    pipe.run(52.5, 13.4, "Berlin", when=BASE)
    r2 = pipe.run(52.5, 13.4, "Berlin", when=BASE)   # budget exhausted (ttl=0)
    check("budget stops 2nd fetch", calls["n"] == 1)
    check("budget exclude reason", r2.excluded.get("open_meteo") == "daily budget exhausted")
    # breaker
    def bad_http(url, params, headers):
        raise RuntimeError("boom")
    om2 = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    pipe2 = WeatherPipeline([om2], ExecutionRouter(mode="bot"),
                            bot_http_get=bad_http, cache_ttl_s=0, now_fn=lambda: BASE)
    for _ in range(3):
        pipe2.run(52.5, 13.4, "Berlin", when=BASE)
    check("breaker open after 3 fails", pipe2.health.is_open("open_meteo"))


def test_qc_rejects_bad():
    print("[7] QC rejects impossible values")
    om = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    bad = fake_open_meteo()
    bad["hourly"]["temperature_2m_ecmwf_ifs025"] = [999.0] * 6
    def http(url, params, headers):
        return bad
    pipe = WeatherPipeline([om], ExecutionRouter(mode="bot"), bot_http_get=http,
                           now_fn=lambda: BASE)
    res = pipe.run(52.5, 13.4, "Berlin", when=BASE)
    check("impossible temps removed", len(res.series) == 0)


if __name__ == "__main__":
    print("=" * 60)
    print("WEATHER PIPELINE SMOKE TEST")
    print("=" * 60)
    test_dedup_and_consensus()
    test_provider_dedup_collapse()
    test_execution_routing()
    test_vps_only_no_direct_call()
    test_vps_delegation()
    test_budget_and_breaker()
    test_qc_rejects_bad()
    print("=" * 60)
    print("RESULT: %d passed, %d failed" % (PASS, FAIL))
    print("=" * 60)
    sys.exit(1 if FAIL else 0)
