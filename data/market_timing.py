"""
Market timing guard — DON'T trade weather markets whose outcome is already decided.

A "highest/lowest temperature on June 3" market is settled by the observed
extreme over that LOCAL calendar day, but the Polymarket market stays OPEN until
UMA resolves (often hours later, sometimes the next day). The day's peak high is
typically reached in the afternoon; once that window passes, the result is a
recorded fact. Buying buckets after that = buying a known outcome (usually a
loss across cheap legs).

This module answers, in the CITY'S LOCAL TIME: is the measurement window for this
market already closed? If yes, the bot must skip it (or only buy a CONFIRMED
winner — not speculative cheap baskets).

Timezone: we read the real UTC offset from Open-Meteo (timezone=auto), cached per
city. Falls back to a longitude estimate (15°/hour) if the API is unavailable —
good enough for an hour-margin gate, and we never crash the trading loop on it.
"""

from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple

import requests

from config import Config
from logger import log


_OFFSET_CACHE: dict = {}   # (round(lat,1), round(lon,1)) -> utc_offset_hours (float)


def get_utc_offset_hours(lat: float, lon: float) -> float:
    """Real UTC offset (hours) for a coordinate, cached. Handles DST + political
    zones via Open-Meteo; falls back to longitude/15 if the call fails."""
    key = (round(lat, 1), round(lon, 1))
    if key in _OFFSET_CACHE:
        return _OFFSET_CACHE[key]

    offset_h = round(lon / 15.0)  # fallback estimate
    try:
        resp = requests.get(
            'https://api.open-meteo.com/v1/forecast',
            params={'latitude': lat, 'longitude': lon,
                    'timezone': 'auto', 'forecast_days': 1},
            timeout=8,
        )
        if resp.status_code == 200:
            secs = resp.json().get('utc_offset_seconds')
            if secs is not None:
                offset_h = float(secs) / 3600.0
    except Exception as e:
        log.debug(f"utc offset fetch failed ({lat},{lon}): {e} — using lon estimate {offset_h}h")

    _OFFSET_CACHE[key] = offset_h
    return offset_h


def city_local_now(lat: float, lon: float) -> datetime:
    """Current wall-clock time in the city (naive datetime in local time)."""
    offset_h = get_utc_offset_hours(lat, lon)
    return datetime.now(timezone.utc) + timedelta(hours=offset_h)


def outcome_decided(
    market_type: str,
    measurement_date: Optional[datetime],
    lat: float,
    lon: float,
    high_lock_hour: Optional[int] = None,
) -> Tuple[bool, str]:
    """Is this market's outcome already a recorded fact (in city-local time)?

    - measurement_date: the LOCAL calendar day the high/low is measured.
    - highest_temperature: decided if the local day is fully past, OR it's the
      measurement day and local time is past the afternoon lock hour (the daily
      high is set by then).
    - lowest_temperature: the daily min can occur late at night, so we only call
      it decided once the local day has fully ended.

    Returns (decided, reason).
    """
    if measurement_date is None:
        return False, ""

    if high_lock_hour is None:
        high_lock_hour = int(getattr(Config, 'HIGH_TEMP_LOCK_HOUR', 18))

    local = city_local_now(lat, lon)
    d = measurement_date.date() if hasattr(measurement_date, 'date') else measurement_date
    local_date = local.date()

    # Day fully over locally → the extreme is recorded, whatever the market type.
    if local_date > d:
        return True, f"local {local:%Y-%m-%d %H:%M} is past measurement day {d}"

    # Same local day: the afternoon high is locked once the lock hour passes.
    if local_date == d and 'highest' in market_type and local.hour >= high_lock_hour:
        return True, f"local {local:%H:%M} ≥ {high_lock_hour}:00 on measurement day — daily high already set"

    return False, ""
