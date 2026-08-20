"""Historical verification + location/variable/lead-time model skill.

Implements the prompt's HISTORICAL VERIFICATION ENGINE and LOCATION-SPECIFIC
MODEL SKILL sections. Every forecast we serve can later be scored against an
observation; accumulated errors become per
    (location, variable, lead_bucket, season, model_family)
skill statistics that drive DYNAMIC MODEL WEIGHTING and SMART SOURCE SELECTION.

Design notes:
  * Pure stdlib, JSON-persisted, import-safe on both the bot and the VPS.
  * Statistics are EXPONENTIALLY-WEIGHTED (recency-biased) so that when a model
    version changes (prompt VERSIONING) its skill adapts instead of being
    frozen by years of history. alpha controls the memory.
  * Never claims perfection: exposes MAE / RMSE / bias + a normalized 0..1
    skill_score used only as a MULTIPLIER on conservative priors.
"""

import json
import math
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ---- lead-time + season bucketing ---------------------------------------
LEAD_NOWCAST = "nowcast"      # 0-6h
LEAD_SHORT = "short"          # 6-48h
LEAD_MEDIUM = "medium"        # 48-168h
LEAD_EXTENDED = "extended"    # 168h+


def lead_bucket(lead_hours: float) -> str:
    if lead_hours is None:
        return LEAD_SHORT
    if lead_hours <= 6:
        return LEAD_NOWCAST
    if lead_hours <= 48:
        return LEAD_SHORT
    if lead_hours <= 168:
        return LEAD_MEDIUM
    return LEAD_EXTENDED


def season_of(when: datetime, lat: float = 0.0) -> str:
    """Meteorological season, hemisphere-aware. Tropics (|lat|<23.5) use a
    wet/dry-ish split by month which is good enough as a regime tag."""
    m = when.month
    if abs(lat) < 23.5:
        # crude monsoon/dry regime tag for tropics
        return "wet" if m in (6, 7, 8, 9, 10) else "dry"
    north = lat >= 0
    if m in (12, 1, 2):
        return "winter" if north else "summer"
    if m in (3, 4, 5):
        return "spring" if north else "autumn"
    if m in (6, 7, 8):
        return "summer" if north else "winter"
    return "autumn" if north else "spring"


# typical error scale per variable — used to map MAE -> 0..1 skill_score.
# A model with MAE == scale gets skill_score ~0.5; MAE==0 -> ~1.0.
ERROR_SCALE = {
    "temp_c": 3.0,
    "temp_min_c": 3.0,
    "temp_max_c": 3.0,
    "precip_mm": 4.0,
    "precip_prob_pct": 30.0,
    "humidity_pct": 12.0,
    "wind_speed_kmh": 8.0,
    "cloud_cover_pct": 25.0,
    "pressure_hpa": 4.0,
}


def _key(location: str, variable: str, bucket: str, season: str, family: str) -> str:
    return "|".join([location or "?", variable, bucket, season, family])


class SkillStore:
    """Exponentially-weighted verification statistics keyed per
    (location, variable, lead_bucket, season, model_family)."""

    def __init__(self, path: Optional[str] = None, alpha: float = 0.06):
        self.path = path
        self.alpha = float(alpha)
        self._data: Dict[str, Dict[str, float]] = {}
        self._lock = threading.RLock()
        if path:
            self.load()

    # ---- persistence ---------------------------------------------------
    def load(self) -> None:
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as fh:
                self._data = json.load(fh) or {}
        except Exception:
            self._data = {}

    def save(self) -> None:
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(self._data, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass

    # ---- recording -----------------------------------------------------
    def record(self, location: str, variable: str, lead_hours: float,
               family: str, forecast_value: float, observed_value: float,
               when: Optional[datetime] = None, lat: float = 0.0) -> None:
        """Fold one (forecast, observation) pair into the running stats."""
        if forecast_value is None or observed_value is None:
            return
        when = when or datetime.now(timezone.utc)
        bucket = lead_bucket(lead_hours)
        season = season_of(when, lat)
        err = float(forecast_value) - float(observed_value)
        abs_err = abs(err)
        sq = err * err
        k = _key(location, variable, bucket, season, family)
        a = self.alpha
        with self._lock:
            st = self._data.get(k)
            if st is None:
                st = {"n": 0.0, "mae": abs_err, "mse": sq, "bias": err,
                      "last_ts": when.timestamp()}
            else:
                st["mae"] = (1 - a) * st["mae"] + a * abs_err
                st["mse"] = (1 - a) * st["mse"] + a * sq
                st["bias"] = (1 - a) * st["bias"] + a * err
                st["last_ts"] = when.timestamp()
            st["n"] = st.get("n", 0.0) + 1.0
            self._data[k] = st

    # ---- reading -------------------------------------------------------
    def stats(self, location: str, variable: str, lead_hours: float,
              family: str, when: Optional[datetime] = None,
              lat: float = 0.0) -> Optional[Dict[str, float]]:
        when = when or datetime.now(timezone.utc)
        k = _key(location, variable, lead_bucket(lead_hours),
                 season_of(when, lat), family)
        st = self._data.get(k)
        if not st:
            return None
        out = dict(st)
        out["rmse"] = math.sqrt(max(0.0, st.get("mse", 0.0)))
        return out

    def skill_score(self, location: str, variable: str, lead_hours: float,
                    family: str, when: Optional[datetime] = None,
                    lat: float = 0.0) -> Optional[float]:
        """0..1 (higher == more skillful). None when no history yet.
        Uses MAE mapped through the per-variable error scale:
            score = scale / (scale + mae)
        so mae=0 -> 1.0, mae=scale -> 0.5, mae>>scale -> ~0."""
        st = self.stats(location, variable, lead_hours, family, when, lat)
        if not st or st.get("n", 0) < 3:
            return None
        scale = ERROR_SCALE.get(variable, 3.0)
        mae = max(0.0, st.get("mae", scale))
        return scale / (scale + mae)

    def bias(self, location: str, variable: str, lead_hours: float,
             family: str, when: Optional[datetime] = None,
             lat: float = 0.0) -> float:
        st = self.stats(location, variable, lead_hours, family, when, lat)
        if not st or st.get("n", 0) < 3:
            return 0.0
        return float(st.get("bias", 0.0))

    def weight_for(self, location: str, variable: str, lead_hours: float,
                   family: str, prior: float,
                   when: Optional[datetime] = None, lat: float = 0.0) -> float:
        """Blend a conservative prior with learned skill. With no history the
        prior is returned unchanged (prompt: priors are only initial)."""
        sc = self.skill_score(location, variable, lead_hours, family, when, lat)
        if sc is None:
            return float(prior)
        # skill in [0,1] -> multiplier in [0.4, 1.6] centred on 1.0
        mult = 0.4 + 1.2 * sc
        return max(0.02, float(prior) * mult)

    def ranking(self, location: str, variable: str, lead_hours: float,
                families: List[str], when: Optional[datetime] = None,
                lat: float = 0.0) -> List[Tuple[str, Optional[float]]]:
        """Families sorted best-first by skill_score (None sorts last)."""
        scored = []
        for fam in families:
            scored.append((fam, self.skill_score(location, variable, lead_hours, fam, when, lat)))
        scored.sort(key=lambda kv: (kv[1] is None, -(kv[1] or 0.0)))
        return scored

    def snapshot(self, limit: int = 200) -> Dict[str, Dict[str, float]]:
        out = {}
        for k, st in list(self._data.items())[:limit]:
            row = dict(st)
            row["rmse"] = math.sqrt(max(0.0, st.get("mse", 0.0)))
            out[k] = row
        return out
