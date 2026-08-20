"""Provider adapters. Each adapter knows how to (a) declare its source identity,
(b) build its request URL(s), and (c) parse a raw JSON response into normalized
ForecastSeries. Adapters do NOT perform IO themselves — the pipeline injects an
http_get callable — so every adapter is unit-testable offline with a captured
response.

Schemas below were validated against live responses (Open-Meteo, WeatherAPI,
OpenWeather) and against provider documentation (Visual Crossing, NWS).
"""

from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

from .schema import SourceIdentity, ForecastSeries, ForecastPoint, Category

HttpGet = Callable[[str, Optional[dict], Optional[dict]], dict]


def _parse_iso(s: str) -> Optional[datetime]:
    if not s:
        return None
    txt = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        # WeatherAPI hourly "YYYY-MM-DD HH:MM"
        try:
            dt = datetime.strptime(s.strip(), "%Y-%m-%d %H:%M")
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


class BaseAdapter(object):
    provider = "base"
    daily_budget = 1000        # per-source default request budget / day

    def identities(self) -> List[SourceIdentity]:
        raise NotImplementedError

    def build_requests(self, lat: float, lon: float, city: str) -> List[Tuple[str, dict]]:
        """Return list of (url, params). Multi-step adapters (NWS) return one and
        follow-up inside fetch_and_parse."""
        raise NotImplementedError

    def parse(self, payload: dict, lat: float, lon: float, city: str) -> List[ForecastSeries]:
        raise NotImplementedError

    def fetch_and_parse(self, http_get: HttpGet, lat: float, lon: float,
                        city: str) -> List[ForecastSeries]:
        out: List[ForecastSeries] = []
        for url, params in self.build_requests(lat, lon, city):
            payload = http_get(url, params, None)
            out.extend(self.parse(payload, lat, lon, city))
        return out


# --------------------------------------------------------------------------
# Open-Meteo (multi-model in a single call). model_family per underlying model
# so downstream de-dup treats Open-Meteo-ECMWF == direct ECMWF.
# --------------------------------------------------------------------------
class OpenMeteoAdapter(BaseAdapter):
    provider = "open_meteo"
    daily_budget = 10000

    # open-meteo model id -> (model_family, category, prior_weight, res)
    # NOTE: Open-Meteo exposes each model under several IDs (e.g. the raw
    # 'gfs_global' AND the blended 'gfs_seamless'). We map BOTH families of IDs
    # to the same model_family so whatever the live config lists
    # (OPEN_METEO_MODELS uses the *_seamless / ecmwf_ifs names) still yields a
    # full multi-model ensemble instead of collapsing to a single model. IDs
    # sharing a model_family are de-duplicated downstream (one independent vote).
    MODELS = {
        # ECMWF
        "ecmwf_ifs025": ("ECMWF_IFS", Category.RAW_MODEL, 0.80, "0.25deg"),
        "ecmwf_ifs04": ("ECMWF_IFS", Category.RAW_MODEL, 0.80, "0.4deg"),
        "ecmwf_ifs": ("ECMWF_IFS", Category.RAW_MODEL, 0.80, ""),
        # NOAA GFS
        "gfs_global": ("NOAA_GFS", Category.RAW_MODEL, 0.65, "0.25deg"),
        "gfs_seamless": ("NOAA_GFS", Category.RAW_MODEL, 0.65, "seamless"),
        # DWD ICON
        "icon_global": ("DWD_ICON", Category.RAW_MODEL, 0.70, "0.11deg"),
        "icon_seamless": ("DWD_ICON", Category.RAW_MODEL, 0.70, "seamless"),
        # CMC GEM
        "gem_global": ("CMC_GEM", Category.RAW_MODEL, 0.55, "0.15deg"),
        "gem_seamless": ("CMC_GEM", Category.RAW_MODEL, 0.55, "seamless"),
        # JMA
        "jma_gsm": ("JMA_GSM", Category.RAW_MODEL, 0.50, "0.5deg"),
        "jma_seamless": ("JMA_GSM", Category.RAW_MODEL, 0.50, "seamless"),
    }

    def __init__(self, base_url: str = "https://api.open-meteo.com/v1/forecast",
                 models: Optional[List[str]] = None, forecast_days: int = 3):
        self.base_url = base_url
        self.models = models or ["ecmwf_ifs025", "gfs_global", "icon_global"]
        self.forecast_days = forecast_days

    def identities(self) -> List[SourceIdentity]:
        out = []
        for m in self.models:
            fam, cat, w, res = self.MODELS.get(m, (m.upper(), Category.RAW_MODEL, 0.5, ""))
            out.append(SourceIdentity(
                source="open_meteo:" + m, provider=self.provider, model_family=fam,
                category=cat, product="normalized_hourly", resolution=res,
                prior_weight=w, license="CC-BY-4.0", attribution="Open-Meteo",
                commercial_ok=True))
        return out

    def build_requests(self, lat, lon, city):
        params = {
            "latitude": lat, "longitude": lon,
            "hourly": "temperature_2m,precipitation,relative_humidity_2m,"
                      "cloud_cover,wind_speed_10m,wind_direction_10m,"
                      "precipitation_probability",
            "daily": "temperature_2m_max,temperature_2m_min",
            "models": ",".join(self.models),
            "forecast_days": self.forecast_days,
            "timezone": "GMT",
        }
        return [(self.base_url, params)]

    def parse(self, payload, lat, lon, city):
        out: List[ForecastSeries] = []
        hourly = (payload or {}).get("hourly") or {}
        times = hourly.get("time") or []
        for m in self.models:
            fam, cat, w, res = self.MODELS.get(m, (m.upper(), Category.RAW_MODEL, 0.5, ""))
            idn = SourceIdentity(
                source="open_meteo:" + m, provider=self.provider, model_family=fam,
                category=cat, product="normalized_hourly", resolution=res,
                prior_weight=w, license="CC-BY-4.0", attribution="Open-Meteo")
            temp = hourly.get("temperature_2m_" + m) or []
            precip = hourly.get("precipitation_" + m) or []
            hum = hourly.get("relative_humidity_2m_" + m) or []
            cloud = hourly.get("cloud_cover_" + m) or []
            wind = hourly.get("wind_speed_10m_" + m) or []
            wdir = hourly.get("wind_direction_10m_" + m) or []
            pprob = hourly.get("precipitation_probability_" + m) or []
            pts: List[ForecastPoint] = []
            for i, t in enumerate(times):
                vt = _parse_iso(t)
                if vt is None:
                    continue
                pts.append(ForecastPoint(
                    valid_time=vt,
                    temp_c=_at(temp, i), precip_mm=_at(precip, i),
                    humidity_pct=_at(hum, i), cloud_cover_pct=_at(cloud, i),
                    wind_speed_kmh=_at(wind, i), wind_dir_deg=_at(wdir, i),
                    precip_prob_pct=_at(pprob, i)))
            if pts:
                out.append(ForecastSeries(identity=idn, points=pts, location=city or ("%s,%s" % (lat, lon))))
        return out


class WeatherApiAdapter(BaseAdapter):
    provider = "weatherapi"
    daily_budget = 900   # free tier ~1M/month; keep conservative

    def __init__(self, api_key: str,
                 base_url: str = "https://api.weatherapi.com/v1/forecast.json",
                 days: int = 3):
        self.api_key = api_key
        self.base_url = base_url
        self.days = days

    def identities(self):
        return [SourceIdentity(
            source="weatherapi", provider=self.provider, model_family="PROVIDER_WEATHERAPI",
            category=Category.PROVIDER, product="forecast_json", prior_weight=0.45,
            license="WeatherAPI-ToS", attribution="WeatherAPI.com", commercial_ok=True)]

    def build_requests(self, lat, lon, city):
        params = {"key": self.api_key, "q": "%s,%s" % (lat, lon),
                  "days": self.days, "aqi": "no", "alerts": "no"}
        return [(self.base_url, params)]

    def parse(self, payload, lat, lon, city):
        idn = self.identities()[0]
        pts: List[ForecastPoint] = []
        fc = ((payload or {}).get("forecast") or {}).get("forecastday") or []
        for day in fc:
            for h in day.get("hour") or []:
                vt = _parse_iso(h.get("time"))
                if vt is None:
                    continue
                pts.append(ForecastPoint(
                    valid_time=vt, temp_c=h.get("temp_c"), precip_mm=h.get("precip_mm"),
                    humidity_pct=h.get("humidity"), cloud_cover_pct=h.get("cloud"),
                    wind_speed_kmh=h.get("wind_kph"), wind_dir_deg=h.get("wind_degree"),
                    precip_prob_pct=h.get("chance_of_rain"), dew_point_c=h.get("dewpoint_c"),
                    pressure_hpa=h.get("pressure_mb")))
        if not pts:
            return []
        return [ForecastSeries(identity=idn, points=pts, location=city or ("%s,%s" % (lat, lon)))]


class OpenWeatherAdapter(BaseAdapter):
    provider = "openweather"
    daily_budget = 900

    def __init__(self, api_key: str,
                 base_url: str = "https://api.openweathermap.org/data/2.5/forecast"):
        self.api_key = api_key
        self.base_url = base_url

    def identities(self):
        return [SourceIdentity(
            source="openweather", provider=self.provider, model_family="PROVIDER_OPENWEATHER",
            category=Category.PROVIDER, product="forecast_3h", prior_weight=0.40,
            license="OWM-ToS", attribution="OpenWeather", commercial_ok=True)]

    def build_requests(self, lat, lon, city):
        params = {"lat": lat, "lon": lon, "appid": self.api_key, "units": "metric"}
        return [(self.base_url, params)]

    def parse(self, payload, lat, lon, city):
        idn = self.identities()[0]
        pts: List[ForecastPoint] = []
        for row in (payload or {}).get("list") or []:
            ts = row.get("dt")
            if ts is None:
                continue
            vt = datetime.fromtimestamp(int(ts), tz=timezone.utc)
            main = row.get("main") or {}
            wind = row.get("wind") or {}
            clouds = row.get("clouds") or {}
            rain = row.get("rain") or {}
            pts.append(ForecastPoint(
                valid_time=vt, temp_c=main.get("temp"), temp_min_c=main.get("temp_min"),
                temp_max_c=main.get("temp_max"), humidity_pct=main.get("humidity"),
                dew_point_c=main.get("dew_point"), pressure_hpa=main.get("pressure"),
                wind_speed_kmh=(wind.get("speed") * 3.6) if wind.get("speed") is not None else None,
                wind_dir_deg=wind.get("deg"), cloud_cover_pct=clouds.get("all"),
                precip_mm=rain.get("3h"),
                precip_prob_pct=(row.get("pop") * 100.0) if row.get("pop") is not None else None))
        if not pts:
            return []
        return [ForecastSeries(identity=idn, points=pts, location=city or ("%s,%s" % (lat, lon)))]


class VisualCrossingAdapter(BaseAdapter):
    provider = "visualcrossing"
    daily_budget = 900   # free tier ~1000/day

    def __init__(self, api_key: str,
                 base_url: str = "https://weather.visualcrossing.com/VisualCrossing/rest/services/timeline"):
        self.api_key = api_key
        self.base_url = base_url

    def identities(self):
        return [SourceIdentity(
            source="visualcrossing", provider=self.provider, model_family="PROVIDER_VISUALCROSSING",
            category=Category.PROVIDER, product="timeline", prior_weight=0.40,
            license="VC-ToS", attribution="Visual Crossing", commercial_ok=True)]

    def build_requests(self, lat, lon, city):
        loc = "%s,%s" % (lat, lon)
        url = self.base_url + "/" + loc + "/next3days"
        params = {"key": self.api_key, "unitGroup": "metric",
                  "include": "hours", "contentType": "json"}
        return [(url, params)]

    def parse(self, payload, lat, lon, city):
        idn = self.identities()[0]
        pts: List[ForecastPoint] = []
        for day in (payload or {}).get("days") or []:
            date = day.get("datetime") or ""
            for h in day.get("hours") or []:
                t = h.get("datetime") or ""
                vt = _parse_iso(date + "T" + t) if len(t) <= 8 else _parse_iso(t)
                if vt is None:
                    continue
                pts.append(ForecastPoint(
                    valid_time=vt, temp_c=h.get("temp"), humidity_pct=h.get("humidity"),
                    precip_mm=h.get("precip"), precip_prob_pct=h.get("precipprob"),
                    cloud_cover_pct=h.get("cloudcover"), wind_speed_kmh=h.get("windspeed"),
                    wind_dir_deg=h.get("winddir"), dew_point_c=h.get("dew"),
                    pressure_hpa=h.get("pressure")))
        if not pts:
            return []
        return [ForecastSeries(identity=idn, points=pts, location=city or ("%s,%s" % (lat, lon)))]


class NwsAdapter(BaseAdapter):
    """US National Weather Service (api.weather.gov). Two-step: /points -> hourly
    forecast URL. category=RAW_MODEL family NWS_NDFD (US only, no key)."""
    provider = "nws"
    daily_budget = 2000

    def __init__(self, base_url: str = "https://api.weather.gov"):
        self.base_url = base_url

    def identities(self):
        return [SourceIdentity(
            source="nws", provider=self.provider, model_family="NWS_NDFD",
            category=Category.RAW_MODEL, product="forecast_hourly", prior_weight=0.60,
            license="US-PublicDomain", attribution="NOAA/NWS", commercial_ok=True)]

    def build_requests(self, lat, lon, city):
        return [(self.base_url + ("/points/%s,%s" % (round(lat, 4), round(lon, 4))), {})]

    def fetch_and_parse(self, http_get, lat, lon, city):
        pt = http_get(self.base_url + ("/points/%s,%s" % (round(lat, 4), round(lon, 4))), {}, None)
        hourly_url = (((pt or {}).get("properties") or {}).get("forecastHourly"))
        if not hourly_url:
            return []
        payload = http_get(hourly_url, {}, None)
        return self.parse(payload, lat, lon, city)

    def parse(self, payload, lat, lon, city):
        idn = self.identities()[0]
        pts: List[ForecastPoint] = []
        for per in (((payload or {}).get("properties") or {}).get("periods") or []):
            vt = _parse_iso(per.get("startTime"))
            if vt is None:
                continue
            temp = per.get("temperature")
            if temp is not None and str(per.get("temperatureUnit", "F")).upper() == "F":
                temp = (temp - 32.0) * 5.0 / 9.0
            pop = ((per.get("probabilityOfPrecipitation") or {}).get("value"))
            rh = ((per.get("relativeHumidity") or {}).get("value"))
            pts.append(ForecastPoint(
                valid_time=vt, temp_c=temp, humidity_pct=rh, precip_prob_pct=pop))
        if not pts:
            return []
        return [ForecastSeries(identity=idn, points=pts, location=city or ("%s,%s" % (lat, lon)))]


def _at(arr, i):
    if arr is None or i >= len(arr):
        return None
    return arr[i]
