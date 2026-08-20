"""Deploy-time live verification of every weather provider + API key.

Run this ON THE SERVER/VPS (where there IS network) after deploy:
    python tools/weather_source_selftest.py --lat 40.7 --lon -74.0

It performs ONE real request per configured provider through the exact adapter
the pipeline uses, then prints a PASS/FAIL table with point counts + a sample
value so you can confirm keys work end-to-end. Uses only the stdlib (urllib);
no third-party deps. The bot sandbox has no network, so this is intentionally a
separate operator tool rather than an offline unit test.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.weather.sources import (  # noqa: E402
    OpenMeteoAdapter, OpenWeatherAdapter, WeatherApiAdapter,
    VisualCrossingAdapter, NwsAdapter,
)


def http_get(url, params, headers):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers or {
        "User-Agent": "weather-pol-selftest/1.0 (contact: ops@weather-pol)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _get_config():
    try:
        from config import Config
        return Config
    except Exception:
        class _Env(object):
            OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY", "")
            WEATHERAPI_API_KEY = os.getenv("WEATHERAPI_API_KEY", "")
            VISUALCROSSING_API_KEY = os.getenv("VISUALCROSSING_API_KEY", "")
        return _Env()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lat", type=float, default=40.7128)
    ap.add_argument("--lon", type=float, default=-74.0060)
    ap.add_argument("--city", default="New York")
    args = ap.parse_args()
    cfg = _get_config()

    adapters = []
    adapters.append(("open_meteo", OpenMeteoAdapter(
        models=["ecmwf_ifs025", "gfs_global", "icon_global"])))
    if getattr(cfg, "OPENWEATHER_API_KEY", ""):
        adapters.append(("openweather", OpenWeatherAdapter(api_key=cfg.OPENWEATHER_API_KEY)))
    if getattr(cfg, "WEATHERAPI_API_KEY", ""):
        adapters.append(("weatherapi", WeatherApiAdapter(api_key=cfg.WEATHERAPI_API_KEY)))
    if getattr(cfg, "VISUALCROSSING_API_KEY", ""):
        adapters.append(("visualcrossing", VisualCrossingAdapter(api_key=cfg.VISUALCROSSING_API_KEY)))
    adapters.append(("nws", NwsAdapter()))

    print("=" * 66)
    print("WEATHER SOURCE LIVE SELF-TEST  @ %.4f,%.4f (%s)" % (args.lat, args.lon, args.city))
    print("=" * 66)
    ok = 0
    fail = 0
    for name, adapter in adapters:
        try:
            series = adapter.fetch_and_parse(http_get, args.lat, args.lon, args.city)
            npts = sum(len(s.points) for s in series)
            if series and npts:
                sample = next((p.temp_c for s in series for p in s.points
                               if p.temp_c is not None), None)
                fams = ",".join(sorted({s.identity.model_family for s in series}))
                print("  PASS  %-14s series=%d pts=%d temp0=%s fams=[%s]" % (
                    name, len(series), npts, ("%.1fC" % sample) if sample is not None else "n/a", fams))
                ok += 1
            else:
                print("  WARN  %-14s connected but 0 usable points (schema/coverage?)" % name)
                fail += 1
        except Exception as exc:  # noqa: BLE001
            print("  FAIL  %-14s %s" % (name, str(exc)[:90]))
            fail += 1
    print("=" * 66)
    print("RESULT: %d ok, %d fail/warn" % (ok, fail))
    print("=" * 66)
    sys.exit(1 if ok == 0 else 0)


if __name__ == "__main__":
    main()
