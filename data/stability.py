"""
Weather Stability Engine — score how PREDICTABLE a city's resolution-day
temperature is, so we only bet size on the days/cities where our forecast
edge is real.

The idea (from an 80%-win-rate weather trader): some cities/days have STABLE,
sideways temperature with all models agreeing — those are highly predictable
and safe to hold to resolution. Other days are volatile (sharp warming/cooling,
high wind gusts, pressure swings, incoming rain) — there the forecast is
unreliable, so we either skip or exit early instead of holding.

Stability score (0..1, higher = more predictable) blends:
  1. Model agreement   — low spread across ECMWF/GFS/ICON/... on the day's max
  2. Intraday flatness  — small temp swing in the hours around resolution
  3. Trend             — "stable/sideways" beats "sharp warming/cooling"
  4. Wind gusts         — calm air = stable; gusty = volatile
  5. Pressure stability — flat surface pressure = settled weather
  6. Precip / cloud     — rain or heavy cloud can cap the daytime max (risk)

We also expose a TREND label (stable / warming / cooling / sideways) and a
`rain_block` flag (precip likely to suppress the high) so the exit logic can
decide hold-vs-exit per the user's rules.
"""

import math
import time
import requests
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

from config import Config
from logger import log
from data.weather_stations import get_station


@dataclass
class StabilityReport:
    city: str
    score: float                 # 0..1 composite stability (higher = predictable)
    trend: str                   # 'stable' | 'warming' | 'cooling' | 'sideways' | 'unknown'
    model_spread_c: float        # std of daily-max across models (°C)
    intraday_swing_c: float      # max-min temp in the resolution hour window
    max_gust_kmh: float
    pressure_swing_hpa: float
    humidity_pct: float
    precip_mm: float             # total precip in the window
    rain_block: bool             # rain/heavy cloud likely caps the daytime high
    forecast_max_c: float        # ensemble forecast daily max (airport station)
    predictable: bool            # score >= threshold AND station-backed
    reason: str

    def hold_to_resolution(self) -> bool:
        """Stable + no rain risk → safe to hold for the full $1 payout."""
        return self.predictable and not self.rain_block


class StabilityEngine:
    """Fetch airport-station hourly weather and score resolution-day stability."""

    # ── thresholds (tunable; validated in backtest) ──
    PREDICTABLE_SCORE = float(getattr(Config, 'STABILITY_MIN_SCORE', 0.62) or 0.62)
    GUST_CALM_KMH = 25.0         # gusts below this are "calm"
    GUST_VOLATILE_KMH = 55.0     # gusts above this are "volatile"
    PRESSURE_STABLE_HPA = 3.0    # pressure swing below this is settled
    PRESSURE_VOLATILE_HPA = 12.0
    SWING_FLAT_C = 2.0           # intraday swing below this is flat
    SWING_VOLATILE_C = 8.0
    SPREAD_TIGHT_C = 1.0         # model std below this = strong agreement
    SPREAD_WIDE_C = 4.0
    TREND_FLAT_C = 1.5           # |day-over-day max change| below this = sideways/stable
    RAIN_BLOCK_MM = 2.0          # precip above this in daytime can suppress the high

    HOURLY_MODELS = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless',
                     'jma_seamless', 'gem_seamless']

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({'User-Agent': f'WeatherSniper/{Config.VERSION}'})
        self._cache: Dict[str, Tuple[float, StabilityReport]] = {}
        self._cache_ttl = 600  # 10 min

    # ──────────────────────────────────────────────────────────────
    def assess(self, city: str, target_time: datetime,
               lat: float = None, lon: float = None) -> Optional[StabilityReport]:
        """Build a StabilityReport for `city` on the resolution day of target_time."""
        station = get_station(city)
        if station:
            lat, lon = station.lat, station.lon
        if lat is None or lon is None:
            return None

        date_str = target_time.astimezone(timezone.utc).strftime('%Y-%m-%d')
        cache_key = f"{lat:.3f},{lon:.3f},{date_str}"
        now = time.time()
        if cache_key in self._cache:
            ts, rep = self._cache[cache_key]
            if now - ts < self._cache_ttl:
                return rep

        daily, hourly = self._fetch(lat, lon, target_time)
        if not daily:
            return None

        rep = self._score(city, target_time, daily, hourly, station_backed=station is not None)
        self._cache[cache_key] = (now, rep)
        return rep

    # ──────────────────────────────────────────────────────────────
    def _fetch(self, lat: float, lon: float, target_time: datetime):
        """Return (daily_max_by_model, hourly_features) for the target day."""
        url = "https://api.open-meteo.com/v1/forecast"
        params = {
            'latitude': lat,
            'longitude': lon,
            'daily': 'temperature_2m_max',
            'hourly': ('temperature_2m,relative_humidity_2m,surface_pressure,'
                       'wind_gusts_10m,precipitation,cloud_cover'),
            'models': ','.join(self.HOURLY_MODELS),
            'timezone': 'UTC',
            'forecast_days': 4,
        }
        try:
            resp = self.session.get(url, params=params, timeout=12)
            if resp.status_code != 200:
                return None, None
            data = resp.json()
        except Exception as e:
            log.debug(f"Stability fetch failed: {e}")
            return None, None

        target_date = target_time.astimezone(timezone.utc).strftime('%Y-%m-%d')

        # daily max per model (+ previous day for trend)
        daily = data.get('daily', {})
        dtimes = daily.get('time', [])
        daily_max_by_model: Dict[str, float] = {}
        prev_max_by_model: Dict[str, float] = {}
        try:
            idx = dtimes.index(target_date)
        except ValueError:
            idx = None
        for model in self.HOURLY_MODELS:
            key = f'temperature_2m_max_{model}'
            arr = daily.get(key) or daily.get('temperature_2m_max') or []
            if idx is not None and idx < len(arr) and arr[idx] is not None:
                daily_max_by_model[model] = float(arr[idx])
                if idx - 1 >= 0 and arr[idx - 1] is not None:
                    prev_max_by_model[model] = float(arr[idx - 1])

        hourly = self._extract_hourly(data.get('hourly', {}), target_date, target_time)
        hourly['prev_max_by_model'] = prev_max_by_model
        return daily_max_by_model, hourly

    def _extract_hourly(self, h: Dict, target_date: str, target_time: datetime) -> Dict:
        """Pull the resolution-window hourly features (12:00–18:00 local-ish UTC day)."""
        times = h.get('time', [])
        # Use the FIRST model's hourly fields for stability features (gust/pressure/etc.
        # are nearly identical across models; temp swing uses ensemble mean below).
        def col(name):
            for model in self.HOURLY_MODELS:
                v = h.get(f'{name}_{model}')
                if v:
                    return v
            return h.get(name, [])

        temps = col('temperature_2m')
        gusts = col('wind_gusts_10m')
        pres = col('surface_pressure')
        hum = col('relative_humidity_2m')
        precip = col('precipitation')
        clouds = col('cloud_cover')

        # daytime window indices (10:00–18:00 UTC of the target date — where the max forms)
        idxs = []
        for i, t in enumerate(times):
            if not t.startswith(target_date):
                continue
            try:
                hh = int(t[11:13])
            except Exception:
                continue
            if 9 <= hh <= 18:
                idxs.append(i)

        def vals(arr):
            return [arr[i] for i in idxs if i < len(arr) and arr[i] is not None]

        return {
            'temps': vals(temps),
            'gusts': vals(gusts),
            'pressure': vals(pres),
            'humidity': vals(hum),
            'precip': vals(precip),
            'clouds': vals(clouds),
        }

    # ──────────────────────────────────────────────────────────────
    def _score(self, city, target_time, daily_max_by_model, hourly, station_backed) -> StabilityReport:
        maxes = list(daily_max_by_model.values())
        forecast_max = sum(maxes) / len(maxes) if maxes else 0.0
        model_spread = _std(maxes)

        temps = hourly.get('temps', [])
        gusts = hourly.get('gusts', [])
        pressure = hourly.get('pressure', [])
        humidity = hourly.get('humidity', [])
        precip = hourly.get('precip', [])
        clouds = hourly.get('clouds', [])

        intraday_swing = (max(temps) - min(temps)) if len(temps) >= 2 else 3.0
        max_gust = max(gusts) if gusts else 30.0
        pressure_swing = (max(pressure) - min(pressure)) if len(pressure) >= 2 else 5.0
        humidity_avg = (sum(humidity) / len(humidity)) if humidity else 60.0
        precip_total = sum(precip) if precip else 0.0
        cloud_avg = (sum(clouds) / len(clouds)) if clouds else 40.0

        # ── component sub-scores (each 0..1, higher = more stable) ──
        s_spread = _lin(model_spread, self.SPREAD_TIGHT_C, self.SPREAD_WIDE_C)
        s_swing = _lin(intraday_swing, self.SWING_FLAT_C, self.SWING_VOLATILE_C)
        s_gust = _lin(max_gust, self.GUST_CALM_KMH, self.GUST_VOLATILE_KMH)
        s_pres = _lin(pressure_swing, self.PRESSURE_STABLE_HPA, self.PRESSURE_VOLATILE_HPA)

        # weighted composite (model agreement + intraday flatness dominate)
        score = (0.34 * s_spread + 0.26 * s_swing +
                 0.22 * s_gust + 0.18 * s_pres)
        score = max(0.0, min(1.0, score))

        # ── trend: today's max vs yesterday's max (ensemble) ──
        prev = hourly.get('prev_max_by_model', {})
        prev_vals = list(prev.values())
        prev_max = (sum(prev_vals) / len(prev_vals)) if prev_vals else forecast_max
        delta = forecast_max - prev_max
        if abs(delta) <= self.TREND_FLAT_C:
            trend = 'stable' if intraday_swing <= self.SWING_FLAT_C else 'sideways'
        elif delta > 0:
            trend = 'warming'
        else:
            trend = 'cooling'

        # sharp move penalty — fast warming/cooling is less predictable
        if trend in ('warming', 'cooling') and abs(delta) > 2 * self.TREND_FLAT_C:
            score *= 0.85

        # ── rain block: precip or heavy cloud likely suppresses the daytime high ──
        rain_block = (precip_total >= self.RAIN_BLOCK_MM) or (cloud_avg >= 85 and precip_total > 0.2)

        predictable = (score >= self.PREDICTABLE_SCORE) and station_backed

        reason = (
            f"{city}: score={score:.2f} trend={trend} "
            f"spread={model_spread:.1f}C swing={intraday_swing:.1f}C "
            f"gust={max_gust:.0f}kmh dP={pressure_swing:.1f}hPa "
            f"precip={precip_total:.1f}mm{' RAIN-BLOCK' if rain_block else ''}"
        )

        return StabilityReport(
            city=city, score=round(score, 3), trend=trend,
            model_spread_c=round(model_spread, 2),
            intraday_swing_c=round(intraday_swing, 2),
            max_gust_kmh=round(max_gust, 1),
            pressure_swing_hpa=round(pressure_swing, 2),
            humidity_pct=round(humidity_avg, 1),
            precip_mm=round(precip_total, 2),
            rain_block=rain_block,
            forecast_max_c=round(forecast_max, 2),
            predictable=predictable,
            reason=reason,
        )

    # ──────────────────────────────────────────────────────────────
    def rank_cities(self, cities: List[str], target_time: datetime) -> List[StabilityReport]:
        """Assess a list of cities and return them ranked most-predictable first."""
        reports = []
        for c in cities:
            try:
                rep = self.assess(c, target_time)
                if rep:
                    reports.append(rep)
            except Exception as e:
                log.debug(f"rank_cities {c}: {e}")
        reports.sort(key=lambda r: r.score, reverse=True)
        return reports


# ── helpers ──────────────────────────────────────────────────────
def _std(xs: List[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs))


def _lin(value: float, good: float, bad: float) -> float:
    """Map value→[0,1]: <=good →1 (stable), >=bad →0 (volatile), linear between."""
    if value <= good:
        return 1.0
    if value >= bad:
        return 0.0
    return 1.0 - (value - good) / (bad - good)
