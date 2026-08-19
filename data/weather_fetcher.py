"""
Multi-source weather forecast fetcher.

Sources:
1. Open-Meteo (free, no key, global coverage, multiple models)
2. OpenWeatherMap (key required, good hourly forecasts)
3. weather.gov (free, US only, NWS forecasts)
4. WeatherAPI.com (key required, global, hourly + daily max/min)
5. Visual Crossing (key required, global, hourly + daily max/min)

Each source returns standardized forecast data that feeds into
the probability engine. More independent members = a tighter, more accurate
ensemble and automatic failover when any single provider is down/limited.

Open-Meteo's free tier allows ~10k calls/day. To spread that budget and reduce
single-IP rate-limit risk, the fetcher round-robins across the endpoints in
Config.OPEN_METEO_ENDPOINTS (default = the single public endpoint). Add a second
mirror / self-hosted instance there and calls alternate automatically.
"""

import os
import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from config import Config
from logger import log


@dataclass
class ForecastPoint:
    """A single forecast data point."""
    source: str           # 'open_meteo', 'openweather', 'weather_gov', 'weatherapi', 'visualcrossing'
    model: str            # 'ECMWF', 'GFS', 'ICON', 'WAPI', 'VC', etc.
    location: str         # city or lat,lon
    timestamp: datetime   # forecast valid time (UTC)
    temp_c: float         # temperature in Celsius
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    precip_mm: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    confidence: float = 0.5  # 0-1 how much we trust this source
    fetched_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class WeatherFetcher:
    """Fetch forecasts from multiple weather APIs."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': f'WeatherSniper/{Config.VERSION}'})
        self._cache: Dict[str, Tuple[float, List[ForecastPoint]]] = {}
        self._cache_ttl = int(getattr(Config, 'WEATHER_FORECAST_CACHE_SECONDS', 300))  # 5 min default
        # 10k-LIMIT MODE request meter (adaptive cache TTL / polling). Counts
        # external Open-Meteo calls over a rolling 24h window so limit10k mode
        # can stretch the cache TTL to stay under the daily budget (~13k/day).
        self._req_count = 0
        self._req_window_start = time.time()
        self._om_idx = 0  # round-robin index across Open-Meteo endpoints
        # endpoint URL -> epoch time when it may be retried after a rate/IP limit
        self._om_cooldowns: Dict[str, float] = {}

    # ── API keys (Config attr first, then environment) ───────────────────
    @staticmethod
    def _openweather_key() -> Optional[str]:
        # Config attr OR environment, matching WeatherAPI/Visual Crossing. Fixes
        # the gap where an OWM key set ONLY as a Railway env var (not on Config)
        # was silently skipped, so OWM never joined the ensemble.
        return getattr(Config, 'OPENWEATHER_API_KEY', None) or os.getenv('OPENWEATHER_API_KEY')

    @staticmethod
    def _weatherapi_key() -> Optional[str]:
        return getattr(Config, 'WEATHERAPI_API_KEY', None) or os.getenv('WEATHERAPI_API_KEY')

    @staticmethod
    def _visualcrossing_key() -> Optional[str]:
        return getattr(Config, 'VISUALCROSSING_API_KEY', None) or os.getenv('VISUALCROSSING_API_KEY')

    def _open_meteo_endpoints(self) -> List[str]:
        """Primary Open-Meteo endpoints + optional failover mirrors.
        Failover mirrors are appended last and only get reached when the primary
        endpoints are cooling down after a rate/IP limit.

        VPS MASTER GATE: when the VPS master switch (or the weather-proxy sub-
        toggle) is OFF, any endpoint pointing at the VPS base URL is stripped so
        the fetch goes DIRECT to Open-Meteo. Turning the VPS off therefore turns
        off the VPS-routed weather fetch, exactly as configured, without ever
        killing weather data entirely (the bot needs it to trade).
        """
        eps = list(getattr(Config, 'OPEN_METEO_ENDPOINTS', None) or [])
        if not eps:
            eps = ["https://api.open-meteo.com/v1/forecast"]
        for fb in (getattr(Config, 'OPEN_METEO_FAILOVER_ENDPOINTS', None) or []):
            if fb not in eps:
                eps.append(fb)
        try:
            from overlay import vps_service as _vps
            if not _vps.weather_proxy_enabled():
                base = str(getattr(Config, 'VPS_BASE_URL', '') or '')
                if base:
                    direct = [u for u in eps if not str(u).startswith(base)]
                    eps = direct or ["https://api.open-meteo.com/v1/forecast"]
        except Exception:
            pass
        return eps

    def _effective_cache_ttl(self) -> int:
        """Cache TTL used for the forecast cache. In 'normal' mode this is the
        fixed WEATHER_FORECAST_CACHE_SECONDS. In 'limit10k' mode it self-tunes:
        the observed request rate is projected to a full day and, if it would
        exceed WEATHER_DAILY_BUDGET * safety, the TTL is scaled up (slower
        polling) so the bot uses the ~10k free tier efficiently instead of
        blowing through it."""
        base = int(getattr(Config, 'WEATHER_FORECAST_CACHE_SECONDS', 300))
        mode = str(getattr(Config, 'WEATHER_FETCH_MODE', 'normal') or 'normal').lower()
        if mode != 'limit10k':
            return base
        now = time.time()
        # roll the counter window daily so the estimate stays current
        if now - self._req_window_start > 86400:
            self._req_count = 0
            self._req_window_start = now
        lo = int(getattr(Config, 'WEATHER_MIN_CACHE_SECONDS', 120))
        hi = int(getattr(Config, 'WEATHER_MAX_CACHE_SECONDS', 1800))
        budget = int(getattr(Config, 'WEATHER_DAILY_BUDGET', 13000))
        safety = float(getattr(Config, 'WEATHER_LIMIT_SAFETY_PCT', 0.80))
        target = max(1.0, budget * safety)
        elapsed = max(60.0, now - self._req_window_start)
        rate_per_day = self._req_count * 86400.0 / elapsed
        if rate_per_day <= target:
            return max(lo, min(base, hi))
        scale = rate_per_day / target
        return int(max(lo, min(hi, base * scale)))

    def _endpoint_cooling(self, url: str) -> bool:
        """True if this endpoint is resting after a recent rate/IP limit.
        Cooldowns auto-expire so the primary recovers on its own."""
        until = self._om_cooldowns.get(url, 0.0)
        if until and time.time() < until:
            return True
        if until:
            self._om_cooldowns.pop(url, None)  # cooldown expired -> eligible again
        return False

    def _mark_endpoint_cooldown(self, url: str, reason: str = '') -> None:
        """Rest an endpoint after a rate/IP-limit hit so we fail over to others."""
        if not getattr(Config, 'WEATHER_FAILOVER_ENABLED', True):
            return
        cd = int(getattr(Config, 'WEATHER_PROVIDER_COOLDOWN_SECONDS', 600))
        self._om_cooldowns[url] = time.time() + cd
        try:
            from data import notifier as _ntf
            _ntf.note_endpoint_cooldown(url, reason)
        except Exception:
            pass
        log.warning(f"⚠️  Open-Meteo endpoint cooling {cd}s ({url}) {reason}".rstrip())

    def _next_open_meteo_url(self) -> Optional[str]:
        """Pick the next AVAILABLE Open-Meteo endpoint (round-robin, skipping any
        that are cooling down). Returns None when every endpoint is cooling."""
        eps = self._open_meteo_endpoints()
        n = len(eps)
        for _ in range(n):
            url = eps[self._om_idx % n]
            self._om_idx += 1
            if not self._endpoint_cooling(url):
                return url
        return None

    def _proxy_headers(self, url: str) -> dict:
        """Attach the bearer token ONLY when the request goes to our VPS proxy
        (never to the public Open-Meteo endpoint or other providers)."""
        base = getattr(Config, 'VPS_BASE_URL', '') or ''
        tok = getattr(Config, 'VPS_AUTH_TOKEN', '') or ''
        if base and tok and url.startswith(base):
            return {'Authorization': 'Bearer ' + tok}
        return {}

    def _open_meteo_request(self, params: dict) -> Optional[dict]:
        """GET an Open-Meteo forecast, transparently failing over across endpoints
        on a rate/IP limit (HTTP status in Config.WEATHER_RATELIMIT_STATUS, or a
        JSON error whose reason mentions 'limit'). The limited endpoint is put on
        a cooldown and the next available mirror is tried. Returns parsed JSON, or
        None if no endpoint produced usable data."""
        ratelimit_status = set(getattr(Config, 'WEATHER_RATELIMIT_STATUS', [429, 403]) or [429, 403])
        failover = getattr(Config, 'WEATHER_FAILOVER_ENABLED', True)
        max_attempts = len(self._open_meteo_endpoints()) if failover else 1

        for _ in range(max_attempts):
            url = self._next_open_meteo_url()
            if not url:
                log.warning("⚠️  All Open-Meteo endpoints are cooling down — using other sources")
                return None
            try:
                self._req_count += 1  # 10k-limit meter: count every external attempt
                resp = self.session.get(url, params=params, timeout=10, headers=self._proxy_headers(url))
            except Exception as e:
                log.debug(f"Open-Meteo request error ({url}): {e}")
                try:
                    from data import notifier as _ntf
                    _ntf.note_endpoint_error(url, e)
                except Exception:
                    pass
                continue  # transient — try the next mirror without cooling it

            if resp.status_code in ratelimit_status:
                self._mark_endpoint_cooldown(url, f"HTTP {resp.status_code} (rate/IP limit)")
                continue
            if resp.status_code != 200:
                log.debug(f"Open-Meteo HTTP {resp.status_code} ({url})")
                return None

            try:
                data = resp.json()
            except Exception as e:
                log.debug(f"Open-Meteo bad JSON ({url}): {e}")
                return None

            # Open-Meteo soft error: {"error": true, "reason": "...limit..."}
            if isinstance(data, dict) and data.get('error'):
                reason = str(data.get('reason', ''))
                if 'limit' in reason.lower():
                    self._mark_endpoint_cooldown(url, f"API limit: {reason}")
                    continue
                log.debug(f"Open-Meteo error ({url}): {reason}")
                return None

            try:
                from data import notifier as _ntf
                _ntf.note_weather_source(url)
            except Exception:
                pass
            return data

        return None

    def fetch_all(self, lat: float, lon: float, city: str = '',
                  target_time: datetime = None) -> List[ForecastPoint]:
        """
        Fetch forecasts from ALL available sources for a location.
        Returns list of ForecastPoint from different models/sources.
        """
        cache_key = f"{lat:.2f},{lon:.2f},{target_time}"
        now = time.time()
        if cache_key in self._cache:
            cached_time, cached_data = self._cache[cache_key]
            if now - cached_time < self._effective_cache_ttl():
                return cached_data

        results = []

        # 1. Open-Meteo (multiple models, free)
        try:
            om_results = self._fetch_open_meteo(lat, lon, city, target_time)
            results.extend(om_results)
        except Exception as e:
            log.warning(f"Open-Meteo fetch failed: {e}")

        # 2. OpenWeatherMap
        if self._openweather_key():
            try:
                ow_results = self._fetch_openweather(lat, lon, city, target_time)
                results.extend(ow_results)
            except Exception as e:
                log.warning(f"OpenWeatherMap fetch failed: {e}")

        # 3. weather.gov (US only)
        if -130 < lon < -60 and 24 < lat < 50:
            try:
                wg_results = self._fetch_weather_gov(lat, lon, city, target_time)
                results.extend(wg_results)
            except Exception as e:
                log.warning(f"weather.gov fetch failed: {e}")

        # 4. WeatherAPI.com (global, key required)
        if self._weatherapi_key():
            try:
                wa_results = self._fetch_weatherapi(lat, lon, city, target_time)
                results.extend(wa_results)
            except Exception as e:
                log.warning(f"WeatherAPI fetch failed: {e}")

        # 5. Visual Crossing (global, key required)
        if self._visualcrossing_key():
            try:
                vc_results = self._fetch_visualcrossing(lat, lon, city, target_time)
                results.extend(vc_results)
            except Exception as e:
                log.warning(f"Visual Crossing fetch failed: {e}")

        self._cache[cache_key] = (now, results)
        log.info(f"Fetched {len(results)} forecast points for {city or f'{lat},{lon}'}")
        return results

    def _fetch_open_meteo(self, lat: float, lon: float, city: str,
                          target_time: datetime = None) -> List[ForecastPoint]:
        """
        Open-Meteo: SINGLE batch request with all models for speed.
        Pulls hourly temp + humidity/wind/precip/cloud AND the daily max/min for
        each model, so high/low-temperature markets can key off the day extreme.
        Endpoint is chosen round-robin across Config.OPEN_METEO_ENDPOINTS, with
        automatic failover to the next mirror when one is rate/IP limited.
        """
        results = []
        # Model members are Config-driven (Config.OPEN_METEO_MODELS). Default now
        # leads with ECMWF IFS HRES (`ecmwf_ifs`, ~9 km native, finest+freshest)
        # then IFS 0.25, GFS/ICON/JMA/GEM, and the old `ecmwf_ifs04` LAST as a
        # fallback member only (coarsest ECMWF grid; often all-null -> skipped).
        models = list(getattr(Config, 'OPEN_METEO_MODELS', None) or
                      ['ecmwf_ifs', 'ecmwf_ifs025', 'gfs_seamless',
                       'icon_seamless', 'jma_seamless', 'gem_seamless', 'ecmwf_ifs04'])
        # Per-member trust. HRES highest; the coarse/legacy 0.4 deg member lowest
        # (it is a fallback only). Unknown members fall back to 0.70 below.
        model_confidence = {
            'ecmwf_ifs': 0.92, 'ecmwf_ifs025': 0.90, 'ecmwf_ifs04': 0.70,
            'gfs_seamless': 0.80, 'icon_seamless': 0.82,
            'jma_seamless': 0.78, 'gem_seamless': 0.75,
        }
        # Clean display labels per member (else the ecmwf_ifs* ids collide).
        model_label = {
            'ecmwf_ifs': 'ECMWF-HRES', 'ecmwf_ifs025': 'ECMWF-025',
            'ecmwf_ifs04': 'ECMWF-04', 'gfs_seamless': 'GFS',
            'icon_seamless': 'ICON', 'jma_seamless': 'JMA', 'gem_seamless': 'GEM',
        }
        # NULL-MODEL GUARD: count points produced per member so we can log any
        # member that returned an all-null series (e.g. ecmwf_ifs04 going dark)
        # instead of silently dropping the highest-confidence forecast.
        # Req-27 #6: drop members disabled via the Telegram Weather-model toggles
        # (green tick = fetch, red X = skip). Fail-open; the SAME filtered list is
        # sent to the VPS proxy so the edge node skips the disabled member too.
        try:
            from overlay import weather_models as _wxm
            models = _wxm.active_models(models)
        except Exception:
            pass
        model_points = {m: 0 for m in models}

        params = {
            'latitude': lat,
            'longitude': lon,
            'hourly': 'temperature_2m,relative_humidity_2m,wind_speed_10m,precipitation,cloud_cover',
            'daily': 'temperature_2m_max,temperature_2m_min',
            'models': ','.join(models),
            'timezone': 'UTC',
            'forecast_days': 3,
        }

        data = self._open_meteo_request(params)
        if not data:
            return results

        try:
            hourly = data.get('hourly', {})
            times = hourly.get('time', [])
            if not times:
                return results

            daily = data.get('daily', {})
            daily_times = daily.get('time', [])

            for model in models:
                def _harr(base):
                    """Hourly array for this model (model-suffixed key → plain key)."""
                    arr = hourly.get(f'{base}_{model}')
                    if not arr:
                        arr = hourly.get(base, [])
                    return arr or []

                temps = _harr('temperature_2m')
                if not temps:
                    continue
                hums = _harr('relative_humidity_2m')
                winds = _harr('wind_speed_10m')
                precs = _harr('precipitation')
                clouds = _harr('cloud_cover')

                # Per-day max/min for THIS model (date 'YYYY-MM-DD' → (max, min)).
                dmax = daily.get(f'temperature_2m_max_{model}') or daily.get('temperature_2m_max', [])
                dmin = daily.get(f'temperature_2m_min_{model}') or daily.get('temperature_2m_min', [])
                day_map = {}
                for di, dstr in enumerate(daily_times):
                    mx = dmax[di] if di < len(dmax) else None
                    mn = dmin[di] if di < len(dmin) else None
                    day_map[str(dstr)[:10]] = (mx, mn)

                for i, t_str in enumerate(times):
                    if i >= len(temps) or temps[i] is None:
                        continue
                    try:
                        t = datetime.fromisoformat(t_str).replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    if target_time and abs((t - target_time).total_seconds()) > 7200:
                        continue

                    tmax, tmin = day_map.get(str(t_str)[:10], (None, None))

                    def _at(arr):
                        return arr[i] if i < len(arr) and arr[i] is not None else None

                    fp = ForecastPoint(
                        source='open_meteo',
                        model=model_label.get(model, model.upper()),
                        location=city or f"{lat},{lon}",
                        timestamp=t,
                        temp_c=temps[i],
                        temp_max_c=tmax,
                        temp_min_c=tmin,
                        humidity_pct=_at(hums),
                        wind_speed_kmh=_at(winds),
                        precip_mm=_at(precs),
                        cloud_cover_pct=_at(clouds),
                        confidence=model_confidence.get(model, 0.7),
                    )
                    results.append(fp)
                    model_points[model] = model_points.get(model, 0) + 1
        except Exception as e:
            log.debug(f"Open-Meteo batch failed: {e}")

        # NULL-MODEL GUARD: surface any member that produced ZERO points so a
        # silently-dark model (e.g. ecmwf_ifs04) is visible in the logs and the
        # weather trace, and never mistaken for agreement.
        missing = [model_label.get(m, m) for m, n in model_points.items() if n == 0]
        if missing and any(n > 0 for n in model_points.values()):
            log.info(f"   \U0001f9ea open-meteo null members @ {city or f'{lat},{lon}'}: "
                     f"{', '.join(missing)} (returned no data — skipped, ensemble intact)")
        try:
            from overlay import weather_trace as _wt
            _wt.record_fetch(city or f"{lat},{lon}", results, model_points, model_label)
        except Exception:
            pass

        return results

    def _fetch_openweather(self, lat: float, lon: float, city: str,
                           target_time: datetime = None) -> List[ForecastPoint]:
        """OpenWeatherMap 5-day/3-hour forecast."""
        results = []

        url = "https://api.openweathermap.org/data/2.5/forecast"
        params = {
            'lat': lat,
            'lon': lon,
            'appid': self._openweather_key(),
            'units': 'metric',
        }

        resp = self.session.get(url, params=params, timeout=10)
        if resp.status_code != 200:
            return results

        data = resp.json()
        for item in data.get('list', []):
            try:
                t = datetime.fromtimestamp(item['dt'], tz=timezone.utc)
            except Exception:
                continue

            if target_time:
                diff = abs((t - target_time).total_seconds())
                if diff > 7200:
                    continue

            main = item.get('main', {})
            wind_data = item.get('wind', {})
            rain = item.get('rain', {})
            clouds_data = item.get('clouds', {})

            fp = ForecastPoint(
                source='openweather',
                model='OWM',
                location=city or f"{lat},{lon}",
                timestamp=t,
                temp_c=main.get('temp', 0),
                temp_min_c=main.get('temp_min'),
                temp_max_c=main.get('temp_max'),
                humidity_pct=main.get('humidity'),
                wind_speed_kmh=(wind_data.get('speed', 0) * 3.6),  # m/s → km/h
                precip_mm=rain.get('3h', 0),
                cloud_cover_pct=clouds_data.get('all'),
                confidence=0.75,
            )
            results.append(fp)

        return results

    def _fetch_weather_gov(self, lat: float, lon: float, city: str,
                           target_time: datetime = None) -> List[ForecastPoint]:
        """weather.gov (NWS) — US only, free, no key."""
        results = []

        # Step 1: Get gridpoint
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        resp = self.session.get(points_url, timeout=10)
        if resp.status_code != 200:
            return results

        props = resp.json().get('properties', {})
        forecast_url = props.get('forecastHourly')
        if not forecast_url:
            return results

        # Step 2: Get hourly forecast
        resp2 = self.session.get(forecast_url, timeout=10)
        if resp2.status_code != 200:
            return results

        periods = resp2.json().get('properties', {}).get('periods', [])
        for period in periods:
            try:
                t = datetime.fromisoformat(period['startTime'])
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
            except Exception:
                continue

            if target_time:
                diff = abs((t - target_time).total_seconds())
                if diff > 7200:
                    continue

            # Convert F to C
            temp_f = period.get('temperature', 0)
            temp_c = (temp_f - 32) * 5.0 / 9.0

            wind_str = period.get('windSpeed', '0 mph')
            try:
                wind_mph = float(wind_str.split()[0])
                wind_kmh = wind_mph * 1.609
            except Exception:
                wind_kmh = 0

            fp = ForecastPoint(
                source='weather_gov',
                model='NWS',
                location=city or f"{lat},{lon}",
                timestamp=t,
                temp_c=temp_c,
                humidity_pct=period.get('relativeHumidity', {}).get('value'),
                wind_speed_kmh=wind_kmh,
                confidence=0.82,
            )
            results.append(fp)

        return results

    def _fetch_weatherapi(self, lat: float, lon: float, city: str,
                          target_time: datetime = None) -> List[ForecastPoint]:
        """WeatherAPI.com forecast.json — global, 3-day hourly with the day's
        max/min (day.maxtemp_c / day.mintemp_c) attached to every hour so high/
        low-temperature markets resolve against the true daily extreme."""
        results = []
        key = self._weatherapi_key()
        if not key:
            return results
        url = "https://api.weatherapi.com/v1/forecast.json"
        params = {'key': key, 'q': f"{lat},{lon}", 'days': 3,
                  'aqi': 'no', 'alerts': 'no'}
        try:
            resp = self.session.get(url, params=params, timeout=10)
            if resp.status_code != 200:
                log.debug(f"WeatherAPI HTTP {resp.status_code}")
                return results
            data = resp.json()
        except Exception as e:
            log.debug(f"WeatherAPI request failed: {e}")
            return results

        for day in data.get('forecast', {}).get('forecastday', []):
            d = day.get('day', {})
            tmax = d.get('maxtemp_c')
            tmin = d.get('mintemp_c')
            for hour in day.get('hour', []):
                epoch = hour.get('time_epoch')
                if epoch is None:
                    continue
                try:
                    t = datetime.fromtimestamp(epoch, tz=timezone.utc)
                except Exception:
                    continue
                if target_time and abs((t - target_time).total_seconds()) > 7200:
                    continue
                fp = ForecastPoint(
                    source='weatherapi',
                    model='WAPI',
                    location=city or f"{lat},{lon}",
                    timestamp=t,
                    temp_c=hour.get('temp_c', 0),
                    temp_max_c=tmax,
                    temp_min_c=tmin,
                    humidity_pct=hour.get('humidity'),
                    wind_speed_kmh=hour.get('wind_kph'),
                    precip_mm=hour.get('precip_mm'),
                    cloud_cover_pct=hour.get('cloud'),
                    confidence=0.80,
                )
                results.append(fp)
        return results

    def _fetch_visualcrossing(self, lat: float, lon: float, city: str,
                              target_time: datetime = None) -> List[ForecastPoint]:
        """Visual Crossing Timeline API — global, hourly readings plus each day's
        tempmax/tempmin, giving another strong independent ensemble member."""
        results = []
        key = self._visualcrossing_key()
        if not key:
            return results
        url = ("https://weather.visualcrossing.com/VisualCrossingWebServices"
               f"/rest/services/timeline/{lat},{lon}")
        params = {'unitGroup': 'metric', 'include': 'hours',
                  'key': key, 'contentType': 'json'}
        try:
            resp = self.session.get(url, params=params, timeout=15)
            if resp.status_code != 200:
                log.debug(f"VisualCrossing HTTP {resp.status_code}")
                return results
            data = resp.json()
        except Exception as e:
            log.debug(f"VisualCrossing request failed: {e}")
            return results

        for day in data.get('days', []):
            tmax = day.get('tempmax')
            tmin = day.get('tempmin')
            for hour in day.get('hours', []):
                epoch = hour.get('datetimeEpoch')
                if epoch is None:
                    continue
                try:
                    t = datetime.fromtimestamp(epoch, tz=timezone.utc)
                except Exception:
                    continue
                if target_time and abs((t - target_time).total_seconds()) > 7200:
                    continue
                fp = ForecastPoint(
                    source='visualcrossing',
                    model='VC',
                    location=city or f"{lat},{lon}",
                    timestamp=t,
                    temp_c=hour.get('temp', 0),
                    temp_max_c=tmax,
                    temp_min_c=tmin,
                    humidity_pct=hour.get('humidity'),
                    wind_speed_kmh=hour.get('windspeed'),
                    precip_mm=hour.get('precip'),
                    cloud_cover_pct=hour.get('cloudcover'),
                    confidence=0.80,
                )
                results.append(fp)
        return results


# ═════════════════════════════════════════════════════════════
# KNOWN CITY COORDINATES (for Polymarket weather markets)
# ═════════════════════════════════════════════════════════════
CITY_COORDS = {
    # Asia (popular on Polymarket weather markets)
    'tokyo': (35.6762, 139.6503),
    'taipei': (25.0330, 121.5654),
    'hong kong': (22.3193, 114.1694),
    'hongkong': (22.3193, 114.1694),
    'seoul': (37.5665, 126.9780),
    'singapore': (1.3521, 103.8198),
    'manila': (14.5995, 120.9842),
    'bangkok': (13.7563, 100.5018),
    'delhi': (28.6139, 77.2090),
    'mumbai': (19.0760, 72.8777),
    'shanghai': (31.2304, 121.4737),
    'beijing': (39.9042, 116.4074),
    'osaka': (34.6937, 135.5023),
    'jakarta': (-6.2088, 106.8456),
    'kuala lumpur': (3.1390, 101.6869),
    'kualalumpur': (3.1390, 101.6869),
    # US
    'new york': (40.7128, -74.0060),
    'nyc': (40.7128, -74.0060),
    'los angeles': (34.0522, -118.2437),
    'la': (34.0522, -118.2437),
    'chicago': (41.8781, -87.6298),
    'miami': (25.7617, -80.1918),
    'houston': (29.7604, -95.3698),
    'phoenix': (33.4484, -112.0740),
    'denver': (39.7392, -104.9903),
    'san francisco': (37.7749, -122.4194),
    'sf': (37.7749, -122.4194),
    'seattle': (47.6062, -122.3321),
    'dallas': (32.7767, -96.7970),
    'atlanta': (33.7490, -84.3880),
    'boston': (42.3601, -71.0589),
    'washington dc': (38.9072, -77.0369),
    'dc': (38.9072, -77.0369),
    # Europe
    'london': (51.5074, -0.1278),
    'paris': (48.8566, 2.3522),
    'berlin': (52.5200, 13.4050),
    'amsterdam': (52.3676, 4.9041),
    'rome': (41.9028, 12.4964),
    'madrid': (40.4168, -3.7038),
    'vienna': (48.2082, 16.3738),
    'zurich': (47.3769, 8.5417),
    'moscow': (55.7558, 37.6173),
    # Middle East
    'dubai': (25.2048, 55.2708),
    'riyadh': (24.7136, 46.6753),
    # Oceania
    'sydney': (-33.8688, 151.2093),
    'melbourne': (-37.8136, 144.9631),
    # South America
    'sao paulo': (-23.5505, -46.6333),
    'buenos aires': (-34.6037, -58.3816),
}


def get_city_coords(city_name: str) -> Optional[Tuple[float, float]]:
    """Look up coordinates for a city — AIRPORT FIRST (Polymarket resolution station),
    fall back to city center, then to CITY_COORDS database."""
    key = city_name.lower().strip()

    # 1. Check weather_stations for EXACT airport coordinates (THE EDGE)
    from data.weather_stations import get_airport_coords
    airport = get_airport_coords(key)
    if airport:
        return airport

    # 2. Fall back to city center coordinates
    return CITY_COORDS.get(key)
