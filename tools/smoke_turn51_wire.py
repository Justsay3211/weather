#!/usr/bin/env python3
"""Turn-51 WIRE test: prove the advanced pipeline is correctly wired into
data/weather_fetcher.WeatherFetcher.fetch_all as an OPT-IN path, converts to
the legacy ForecastPoint contract, and falls back cleanly when disabled or on
error. Runs fully OFFLINE via a stubbed requests session (no network).
"""
import os
import sys
import json

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import Config
import data.weather_fetcher as wf

_PASS = 0
_FAIL = 0


def check(name, cond):
    global _PASS, _FAIL
    if cond:
        _PASS += 1
        print("  PASS  %s" % name)
    else:
        _FAIL += 1
        print("  FAIL  %s" % name)


class _Resp(object):
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


def _open_meteo_payload():
    """Minimal multi-model Open-Meteo hourly+daily payload the OpenMeteoAdapter
    can parse. Covers EVERY model id the live Config lists so the ensemble is
    realistic (seamless variants included)."""
    times = ["2026-08-20T12:00", "2026-08-20T13:00", "2026-08-20T14:00"]
    hourly = {"time": times}
    daily = {"time": ["2026-08-20"]}
    models = list(getattr(Config, "OPEN_METEO_MODELS", None) or
                  ["ecmwf_ifs025", "gfs_global", "icon_global"])
    for m in models:
        hourly["temperature_2m_" + m] = [20.0, 21.0, 22.0]
        hourly["relative_humidity_2m_" + m] = [60, 61, 62]
        hourly["precipitation_" + m] = [0.0, 0.1, 0.0]
        hourly["cloud_cover_" + m] = [10, 20, 30]
        hourly["wind_speed_10m_" + m] = [5, 6, 7]
        hourly["precipitation_probability_" + m] = [10, 20, 30]
        daily["temperature_2m_max_" + m] = [24.0]
        daily["temperature_2m_min_" + m] = [16.0]
    return {"hourly": hourly, "daily": daily,
            "latitude": 40.7, "longitude": -74.0, "utc_offset_seconds": 0}


class _StubSession(object):
    """Records GET calls and returns canned Open-Meteo payloads. Any non
    Open-Meteo URL raises to prove those providers are NOT hit in this test."""
    def __init__(self):
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}, "headers": headers or {}})
        if "open-meteo.com" in url or "/v1/forecast" in url:
            return _Resp(_open_meteo_payload())
        # WeatherAPI/OWM/VC/NWS not configured with keys in this test -> should
        # never be called. Return an error-ish empty payload if they are.
        return _Resp({"error": True})


def main():
    print("[1] pipeline delegation ON (bot-routed, no VPS)")
    # Force pipeline ON, no VPS so Open-Meteo routes to the BOT (direct).
    Config.WEATHER_PIPELINE_ENABLED = True
    Config.WEATHER_EXECUTION_MODE = "bot"
    Config.VPS_BASE_URL = ""
    # keep only Open-Meteo enabled (others need keys anyway)
    Config.WEATHER_SRC_OPEN_METEO_ENABLED = True
    Config.WEATHER_SRC_OPENWEATHER_ENABLED = False
    Config.WEATHER_SRC_WEATHERAPI_ENABLED = False
    Config.WEATHER_SRC_VISUALCROSSING_ENABLED = False
    Config.WEATHER_SRC_NWS_ENABLED = False

    f = wf.WeatherFetcher()
    stub = _StubSession()
    f.session = stub

    pts = f.fetch_all(40.7, -74.0, "New York")
    check("pipeline returned ForecastPoints", isinstance(pts, list) and len(pts) > 0)
    check("points carry temp_c", all(getattr(p, "temp_c", None) is not None for p in pts))
    check("points carry source+model", all(p.source and p.model for p in pts))
    check("legacy ForecastPoint type", all(isinstance(p, wf.ForecastPoint) for p in pts))
    check("only open-meteo endpoints hit",
          all(("open-meteo.com" in c["url"] or "/v1/forecast" in c["url"]) for c in stub.calls))
    fams = {p.model for p in pts}
    check("multiple model families present (ECMWF/GFS/ICON)", len(fams) >= 2)

    print("[2] pipeline OFF -> legacy path used")
    Config.WEATHER_PIPELINE_ENABLED = False
    f2 = wf.WeatherFetcher()
    stub2 = _StubSession()
    f2.session = stub2
    pts2 = f2.fetch_all(40.7, -74.0, "New York")
    # legacy path also hits Open-Meteo via _open_meteo_request; should get points
    check("legacy path returns points", isinstance(pts2, list) and len(pts2) > 0)
    check("legacy did not build pipeline", f2._pipeline is None)

    print("[3] pipeline ON but run raises -> graceful fallback (no crash)")
    Config.WEATHER_PIPELINE_ENABLED = True
    f3 = wf.WeatherFetcher()
    f3.session = _StubSession()

    class _BoomPipe(object):
        def run(self, *a, **k):
            raise RuntimeError("boom")
    f3._pipeline = _BoomPipe()
    # should fall through to legacy and still return a list without raising
    pts3 = f3.fetch_all(40.7, -74.0, "New York")
    check("fallback returns a list on pipeline error", isinstance(pts3, list))

    print("=" * 60)
    print("RESULT: %d passed, %d failed" % (_PASS, _FAIL))
    print("=" * 60)
    return 1 if _FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
