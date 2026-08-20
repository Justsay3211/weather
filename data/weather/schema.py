"""Normalized internal schema + source-identity + freshness.

Every upstream source is converted to this common schema so downstream logic
never depends on GRIB/NetCDF/JSON shapes. Each series carries the *identity* of
what produced it (provider + underlying model family) so the consensus engine
can avoid double-counting providers that expose the same model.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional


class Category(object):
    """Which kind of input a source is (prompt section CORE PRINCIPLE)."""
    RAW_MODEL = "raw_model"        # direct meteorological model output
    PROVIDER = "provider"          # normalized provider/application API
    OBSERVATION = "observation"    # what is actually happening now
    DERIVED = "derived"            # nowcast/blend/bias-corrected product
    ENSEMBLE = "ensemble"          # ensemble prediction system


class Freshness(object):
    FRESH = "FRESH"
    AGING = "AGING"
    STALE = "STALE"
    EXPIRED = "EXPIRED"
    MISSING = "MISSING"
    CORRUPTED = "CORRUPTED"


@dataclass(frozen=True)
class SourceIdentity:
    """Mandatory source-identity / dependency-graph descriptor.

    `model_family` is the KEY used for de-duplication: Open-Meteo-ECMWF and
    direct-ECMWF-IFS share model_family='ECMWF_IFS' and must count as ONE
    independent model, not two.
    """
    source: str            # unique adapter key, e.g. 'open_meteo:ecmwf_ifs025'
    provider: str          # e.g. 'open_meteo', 'ecmwf', 'weatherapi'
    model_family: str      # e.g. 'ECMWF_IFS', 'NOAA_GFS', 'DWD_ICON', 'PROVIDER_BLEND'
    category: str          # Category.*
    product: str = ""      # e.g. 'normalized_hourly', 'open_data'
    model_version: str = ""
    ensemble: bool = False
    member: Optional[int] = None
    resolution: str = ""
    # conservative prior skill weight (0-1). Learned weights override later.
    prior_weight: float = 0.5
    # attribution / licensing metadata (prompt LICENSING section)
    license: str = ""
    attribution: str = ""
    commercial_ok: bool = True


@dataclass
class ForecastPoint:
    """One normalized forecast valid-time for one source."""
    valid_time: datetime                 # UTC forecast valid time
    temp_c: Optional[float] = None
    temp_min_c: Optional[float] = None
    temp_max_c: Optional[float] = None
    humidity_pct: Optional[float] = None
    wind_speed_kmh: Optional[float] = None
    wind_dir_deg: Optional[float] = None
    precip_mm: Optional[float] = None
    precip_prob_pct: Optional[float] = None
    cloud_cover_pct: Optional[float] = None
    pressure_hpa: Optional[float] = None
    dew_point_c: Optional[float] = None
    weather_code: Optional[str] = None


@dataclass
class ForecastSeries:
    """A normalized time-series for a single source + its provenance."""
    identity: SourceIdentity
    points: List[ForecastPoint] = field(default_factory=list)
    run_time: Optional[datetime] = None       # model init time if known
    retrieval_time: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    quality_status: str = "OK"                 # OK / rejected reason
    location: str = ""
    executed_on: str = ""                      # 'vps' | 'bot' — where fetched

    def freshness(self, now: Optional[datetime] = None,
                  aging_s: int = 3 * 3600, stale_s: int = 6 * 3600,
                  expired_s: int = 12 * 3600) -> str:
        if not self.points:
            return Freshness.MISSING
        if self.quality_status != "OK":
            return Freshness.CORRUPTED
        now = now or datetime.now(timezone.utc)
        ref = self.run_time or self.retrieval_time
        ref = _as_utc(ref)
        age = (now - ref).total_seconds()
        if age <= aging_s:
            return Freshness.FRESH
        if age <= stale_s:
            return Freshness.AGING
        if age <= expired_s:
            return Freshness.STALE
        return Freshness.EXPIRED

    def point_at(self, when: datetime, tol_s: int = 3600) -> Optional[ForecastPoint]:
        """Nearest point within tolerance of a target valid-time."""
        when = _as_utc(when)
        best = None
        best_d = None
        for p in self.points:
            d = abs((_as_utc(p.valid_time) - when).total_seconds())
            if d <= tol_s and (best_d is None or d < best_d):
                best, best_d = p, d
        return best


def _as_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
