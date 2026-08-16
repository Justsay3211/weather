"""
Observed-weather engine — "what has actually happened so far today".

This is the data backbone of the overhauled, observation-driven strategy. For a
given location and local measurement day it returns:

* the **max (or min) temperature already observed** so far today,
* the **forecast extreme over the remaining hours** of the local day,
* a **spread** (cross-model std) on that remaining forecast, and
* how many hours are left and how many models contributed.

Those feed ``data.observed_math`` to produce a locked-in bucket distribution.

Two-source design (THE fix for "observed_state is None for every city")
----------------------------------------------------------------------
Forecast-only models (ecmwf_ifs04, gfs_seamless, …) return ``null`` for hours
that have **already elapsed today** — they only look forward. Asking them "what
was the high so far?" yields nothing, so the observed extreme was always None.

So we now query TWO things:

1. **History-aware source** — the plain Open-Meteo forecast (NO ``models=``
   param) plus the live ``current`` reading. Its ``temperature_2m`` backfills
   recent ACTUAL hours, which is exactly what "observed so far today" needs.
2. **Spread source** — the multi-model request, used ONLY for the cross-model
   spread over the REMAINING hours (what the forecast models are good at).

Either source can fail independently; we degrade gracefully and log which one
succeeded.

Network note
------------
The actual HTTP call to Open-Meteo uses ``requests``, imported **lazily inside**
the fetch helpers so this module (and the pure helpers below) import cleanly
where ``requests`` is missing or the network is disabled.
``split_observed_remaining`` is fully unit-testable offline.

Rate-limit failover
-------------------
When an Open-Meteo endpoint returns a rate/IP limit (HTTP status in
``Config.WEATHER_RATELIMIT_STATUS``, or a JSON error whose reason mentions
"limit"), that endpoint is put on a ``Config.WEATHER_PROVIDER_COOLDOWN_SECONDS``
cooldown and requests transparently fail over to the next mirror in
``Config.OPEN_METEO_ENDPOINTS`` (+ optional
``Config.OPEN_METEO_FAILOVER_ENDPOINTS``). Cooldowns auto-expire so the primary
recovers on its own. Config is read lazily so the module stays importable
offline; defaults match ``WeatherFetcher``.

Diagnostics
-----------
Every path that yields ``None`` emits a log line (HTTP status + error body on
non-200, which source(s) failed, and which post-parse guard tripped).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Sequence, Tuple

try:  # logger pulls Config (and dotenv) in the real app; stay importable offline.
    from logger import log  # type: ignore
except Exception:  # pragma: no cover - fallback only used in bare sandboxes
    import logging
    log = logging.getLogger("observed_weather")

# Open-Meteo forecast models we average for a remaining-hours spread estimate.
# Leads with ECMWF IFS HRES (`ecmwf_ifs`, ~9 km native, finest+freshest) + IFS
# 0.25, so the lock path benefits from the sharpest ECMWF data (C8). Config can
# override via OPEN_METEO_MODELS; the old 0.4 deg `ecmwf_ifs04` is kept LAST as a
# fallback member only (often all-null -> the null-tolerant parser skips it).
_DEFAULT_MODELS = ("ecmwf_ifs", "ecmwf_ifs025", "gfs_seamless", "icon_seamless",
                   "jma_seamless", "gem_seamless", "ecmwf_ifs04")
_OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"


# ── LAST-GOOD OBSERVED CACHE (C7) ──────────────────────────────────────────
# The day's extreme can never un-happen, so a cached observed value is a hard
# floor (high mode) / ceiling (low mode). Two jobs: (1) clamp a fresh read so a
# glitchy LOWER value can never weaken a lock we already earned; (2) bridge a
# momentary Open-Meteo starvation with the last-good value so a near-certain
# trade is not missed. It can ONLY raise (high) / lower (low) the extreme, so it
# can never manufacture false confidence. Keyed per (lat, lon, local-date, mode)
# so it resets every local day; TTL bounds intra-day staleness.
_OBS_CACHE: Dict[tuple, Tuple[float, float]] = {}


def _obs_cache_enabled() -> bool:
    c = _config()
    return bool(getattr(c, "OBSERVED_CACHE_ENABLED", True)) if c is not None else True


def _obs_cache_ttl() -> int:
    c = _config()
    return int(getattr(c, "OBSERVED_CACHE_TTL_SECONDS", 10800)) if c is not None else 10800


def _obs_cache_get(key):
    rec = _OBS_CACHE.get(key)
    if not rec:
        return None
    ts, val = rec
    if time.time() - ts > _obs_cache_ttl():
        _OBS_CACHE.pop(key, None)
        return None
    return val


def _obs_cache_merge(key, value, mode):
    """Store & return the up-only (high) / down-only (low) merged extreme."""
    prev = _obs_cache_get(key)
    if prev is None:
        merged = float(value)
    else:
        merged = max(prev, float(value)) if mode == "high" else min(prev, float(value))
    _OBS_CACHE[key] = (time.time(), merged)
    return merged


def _trace_starved(lat, lon, target_day, mode, n_models_total):
    """WATCHER (fail-open): record a pure starvation event (no data, no cache
    bridge) so /weatherhealth and the export can show which cities/positions
    got NO usable weather at decision time. Never raises into the hot path.
    """
    try:
        from overlay import weather_trace as _wt
        _wt.record_observed(
            lat=lat, lon=lon, target_day=str(target_day), mode=mode,
            observed_extreme=None, current_temp=None, remaining_spread=None,
            hours_remaining=None, n_models_with_data=0,
            n_models_total=int(n_models_total or 0),
            is_locked=False, used_cache=False, starved=True,
        )
    except Exception:
        pass


def _config():
    """Lazily import Config; return None offline so the module stays importable."""
    try:
        from config import Config  # type: ignore
        return Config
    except Exception:  # pragma: no cover - bare sandbox / import-time safety
        return None


def _failover_enabled() -> bool:
    c = _config()
    return bool(getattr(c, "WEATHER_FAILOVER_ENABLED", True)) if c is not None else True


def _cooldown_seconds() -> int:
    c = _config()
    return int(getattr(c, "WEATHER_PROVIDER_COOLDOWN_SECONDS", 600)) if c is not None else 600


def _ratelimit_status() -> set:
    c = _config()
    vals = getattr(c, "WEATHER_RATELIMIT_STATUS", [429, 403]) if c is not None else [429, 403]
    return set(vals or [429, 403])


def _open_meteo_endpoints() -> List[str]:
    """Primary Open-Meteo endpoints + optional failover mirrors (appended last)."""
    c = _config()
    eps = list(getattr(c, "OPEN_METEO_ENDPOINTS", None) or []) if c is not None else []
    if not eps:
        eps = [_OPEN_METEO_URL]
    fbs = (getattr(c, "OPEN_METEO_FAILOVER_ENDPOINTS", None) or []) if c is not None else []
    for fb in fbs:
        if fb not in eps:
            eps.append(fb)
    return eps


def _proxy_headers(url: str) -> Dict:
    """Bearer token ONLY for our VPS proxy; never sent to public Open-Meteo."""
    c = _config()
    base = (getattr(c, "VPS_BASE_URL", "") or "") if c is not None else ""
    tok = (getattr(c, "VPS_AUTH_TOKEN", "") or "") if c is not None else ""
    if base and tok and url.startswith(base):
        return {"Authorization": "Bearer " + tok}
    return {}


@dataclass
class ObservedDayState:
    """Snapshot of a single local measurement day, mid-day."""
    observed_extreme_c: float
    remaining_extreme_c: Optional[float]
    remaining_spread_c: float
    hours_remaining: int
    n_models: int
    current_temp_c: Optional[float] = None
    mode: str = "high"
    as_of_local: Optional[str] = None
    raw: Dict = field(default_factory=dict)

    @property
    def is_locked(self) -> bool:
        """True when no forecast hours remain in the local day."""
        return self.hours_remaining <= 0 or self.remaining_extreme_c is None


def split_observed_remaining(
    hourly_times: Sequence,
    hourly_temps: Sequence,
    now_local: datetime,
    mode: str = "high",
) -> Tuple[Optional[float], List[float], int]:
    """Split one day's hourly series into (observed_extreme, remaining_temps, hours_left).

    ``hourly_times`` may be naive ``datetime`` objects (local) or ISO strings.
    Hours at or before ``now_local`` count as observed; later hours are the
    remainder. Pure / offline-testable.
    """
    observed: List[float] = []
    remaining: List[float] = []
    for t, temp in zip(hourly_times, hourly_temps):
        if temp is None:
            continue
        if isinstance(t, str):
            try:
                t = datetime.fromisoformat(t.replace("Z", ""))
            except Exception:
                continue
        if t <= now_local:
            observed.append(float(temp))
        else:
            remaining.append(float(temp))

    if not observed:
        observed_extreme = None
    else:
        observed_extreme = max(observed) if mode == "high" else min(observed)
    return observed_extreme, remaining, len(remaining)


def _stdev(values: Sequence[float]) -> float:
    vals = [float(v) for v in values if v is not None]
    if len(vals) < 2:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
    return math.sqrt(var)


class ObservedWeather:
    """Fetches and assembles an :class:`ObservedDayState` from Open-Meteo."""

    def __init__(self, models: Sequence[str] = _DEFAULT_MODELS, timeout: float = 8.0):
        self.models = list(models)
        self.timeout = timeout
        self._om_idx = 0  # round-robin index across Open-Meteo endpoints
        # endpoint URL -> epoch time when it may be retried after a rate/IP limit
        self._om_cooldowns: Dict[str, float] = {}

    # -- networking (lazy import keeps the module importable offline) --------
    def _http_get(self, url: str, params: Dict) -> Optional[Dict]:
        try:
            import requests  # lazy: only needed when actually fetching
        except Exception as e:  # pragma: no cover
            log.warning(f"requests unavailable, cannot fetch observed weather: {e}")
            return None
        try:
            resp = requests.get(url, params=params, timeout=self.timeout, headers=_proxy_headers(url))
            if resp.status_code != 200:
                # Open-Meteo returns a JSON body like {"error": true,
                # "reason": "..."} on 400 — surface it instead of silent None.
                body = ""
                try:
                    body = resp.text[:200].replace("\n", " ")
                except Exception:
                    pass
                log.warning(
                    f"\u26a0\ufe0f  observed-weather HTTP {resp.status_code} "
                    f"@ {params.get('latitude')},{params.get('longitude')} "
                    f"models={params.get('models', '-')} \u2014 {body}"
                )
                return None
            try:
                from data import notifier as _ntf
                _ntf.note_weather_source(url)
            except Exception:
                pass
            return resp.json()
        except Exception as e:
            log.warning(f"\u26a0\ufe0f  observed-weather fetch failed: {e}")
            return None

    # -- endpoint cooldown / failover (mirrors WeatherFetcher) ---------------
    def _endpoint_cooling(self, url: str) -> bool:
        """True if this endpoint is resting after a recent rate/IP limit.
        Cooldowns auto-expire so the primary recovers on its own."""
        until = self._om_cooldowns.get(url, 0.0)
        if until and time.time() < until:
            return True
        if until:
            self._om_cooldowns.pop(url, None)  # cooldown expired -> eligible again
        return False

    def _mark_endpoint_cooldown(self, url: str, reason: str = "") -> None:
        """Rest an endpoint after a rate/IP-limit hit so we fail over to others."""
        if not _failover_enabled():
            return
        cd = _cooldown_seconds()
        self._om_cooldowns[url] = time.time() + cd
        try:
            from data import notifier as _ntf
            _ntf.note_endpoint_cooldown(url, reason)
        except Exception:
            pass
        log.warning(f"\u26a0\ufe0f  observed-weather endpoint cooling {cd}s ({url}) {reason}".rstrip())

    def _next_open_meteo_url(self) -> Optional[str]:
        """Next AVAILABLE Open-Meteo endpoint (round-robin, skipping cooling ones).
        Returns None when every endpoint is cooling."""
        eps = _open_meteo_endpoints()
        n = len(eps)
        for _ in range(n):
            url = eps[self._om_idx % n]
            self._om_idx += 1
            if not self._endpoint_cooling(url):
                return url
        return None

    def _open_meteo_fetch(self, params: Dict) -> Optional[Dict]:
        """GET an Open-Meteo forecast with transparent failover across endpoints
        on a rate/IP limit (status in Config.WEATHER_RATELIMIT_STATUS, or a JSON
        error reason mentioning 'limit'). The limited endpoint is cooled and the
        next available mirror is tried. Returns parsed JSON or None."""
        try:
            import requests  # lazy: only needed when actually fetching
        except Exception as e:  # pragma: no cover
            log.warning(f"requests unavailable, cannot fetch observed weather: {e}")
            return None

        ratelimit_status = _ratelimit_status()
        failover = _failover_enabled()
        max_attempts = len(_open_meteo_endpoints()) if failover else 1

        for _ in range(max_attempts):
            url = self._next_open_meteo_url()
            if not url:
                log.warning("\u26a0\ufe0f  All Open-Meteo endpoints are cooling down — observed weather unavailable")
                return None
            try:
                resp = requests.get(url, params=params, timeout=self.timeout, headers=_proxy_headers(url))
            except Exception as e:
                log.warning(f"\u26a0\ufe0f  observed-weather fetch failed ({url}): {e}")
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
                body = ""
                try:
                    body = resp.text[:200].replace("\n", " ")
                except Exception:
                    pass
                log.warning(
                    f"\u26a0\ufe0f  observed-weather HTTP {resp.status_code} "
                    f"@ {params.get('latitude')},{params.get('longitude')} "
                    f"models={params.get('models', '-')} \u2014 {body}"
                )
                return None
            try:
                data = resp.json()
            except Exception as e:
                log.warning(f"\u26a0\ufe0f  observed-weather bad JSON ({url}): {e}")
                return None

            # Open-Meteo soft error: {"error": true, "reason": "...limit..."}
            if isinstance(data, dict) and data.get("error"):
                reason = str(data.get("reason", ""))
                if "limit" in reason.lower():
                    self._mark_endpoint_cooldown(url, f"API limit: {reason}")
                    continue
                log.warning(
                    f"\u26a0\ufe0f  observed-weather error @ "
                    f"{params.get('latitude')},{params.get('longitude')}: {reason}"
                )
                return None

            return data

        return None

    @staticmethod
    def _parse_day(
        data: Dict, target_day
    ) -> Tuple[List[Optional[datetime]], List[List[Optional[float]]], List[int]]:
        """Return (day_times, model_series, day_idx) for ``target_day`` in ``data``.

        ``model_series`` is every ``temperature_2m*`` hourly array (one per model,
        or a single plain array on a no-``models`` request). ``day_idx`` indexes
        the hourly rows whose local date equals ``target_day``.
        """
        hourly = data.get("hourly", {}) or {}
        times: List[Optional[datetime]] = []
        for t in hourly.get("time", []) or []:
            try:
                times.append(datetime.fromisoformat(str(t).replace("Z", "")))
            except Exception:
                times.append(None)
        model_series: List[List[Optional[float]]] = [
            list(val) for key, val in hourly.items() if key.startswith("temperature_2m")
        ]
        day_idx = [i for i, t in enumerate(times) if t is not None and t.date() == target_day]
        day_times = [times[i] for i in day_idx]
        return day_times, model_series, day_idx

    @staticmethod
    def _current_temp(data: Dict) -> Optional[float]:
        """Live ``current`` temperature. Matches by prefix because multi-model
        responses suffix the key (e.g. ``temperature_2m_ecmwf_ifs04``)."""
        cur = data.get("current", {}) or {}
        for k, v in cur.items():
            if k.startswith("temperature_2m") and v is not None:
                try:
                    return float(v)
                except Exception:
                    return None
        return None

    def get_state(
        self,
        lat: float,
        lon: float,
        measurement_date: Optional[datetime] = None,
        mode: str = "high",
    ) -> Optional[ObservedDayState]:
        """Assemble the observed/remaining state for the local measurement day.

        Returns ``None`` if no usable data could be fetched — and always logs
        *why* so the silence is debuggable.
        """
        base_params: Dict = {
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m",
            "current": "temperature_2m",
            "timezone": "auto",
            "forecast_days": 2,
            "past_days": 1,
        }

        # 1) HISTORY-AWARE source: plain forecast (no models=). Its hourly
        #    temperature_2m backfills the ACTUAL elapsed hours of today, and it
        #    carries a usable live `current` reading. This is what the observed
        #    extreme must come from — forecast-only models return null for the
        #    past, which is why the observed extreme used to always be None.
        obs_data = self._open_meteo_fetch(base_params)

        # 2) SPREAD source: multi-model, used ONLY for the remaining-hours
        #    cross-model spread (what the forecast models are good at).
        model_data = self._open_meteo_fetch({**base_params, "models": ",".join(self.models)})

        if not obs_data and not model_data:
            log.warning(
                f"   \U0001f319 observed fetch returned no data @ {lat:.2f},{lon:.2f} "
                f"(history source AND multi-model source both failed)"
            )
            _trace_starved(lat, lon, measurement_date, mode, 0)
            return None
        if not obs_data:
            log.info(
                f"   \u2139\ufe0f  observed: history source failed @ {lat:.2f},{lon:.2f}, "
                f"using multi-model for observed too (past hours may be sparse)"
            )

        # Local clock / target day come from whichever response we have.
        primary_data = obs_data or model_data
        utc_offset = int(primary_data.get("utc_offset_seconds", 0) or 0)
        now_local = datetime.now(timezone.utc) + timedelta(seconds=utc_offset)
        now_local = now_local.replace(tzinfo=None)
        target_day = (measurement_date or now_local).date()

        # --- OBSERVED extreme (+ current) from the history-aware source -----
        obs_source = obs_data or model_data
        day_times, obs_series, day_idx = self._parse_day(obs_source, target_day)
        if not obs_series:
            log.warning(
                f"   \U0001f319 observed: response had no temperature_2m hourly series "
                f"@ {lat:.2f},{lon:.2f}"
            )
            _trace_starved(lat, lon, target_day, mode, len(self.models))
            return None

        s0 = obs_series[0]
        day_vals0 = [s0[i] if i < len(s0) else None for i in day_idx]
        observed_extreme, _, _ = split_observed_remaining(day_times, day_vals0, now_local, mode)

        current_temp = self._current_temp(obs_source)
        if current_temp is not None:
            if observed_extreme is None:
                observed_extreme = current_temp
            else:
                observed_extreme = (max(observed_extreme, current_temp)
                                    if mode == "high" else min(observed_extreme, current_temp))

        # LAST-GOOD OBSERVED CACHE (C7): clamp a fresh read up-only(high)/
        # down-only(low) so a glitchy lower value can't weaken an earned lock,
        # and bridge a momentary starvation (fresh=None) with the last-good
        # value so a near-certain trade is not missed. Never lowers a high /
        # raises a low -> can never manufacture false confidence.
        ckey = (round(lat, 2), round(lon, 2), str(target_day), mode)
        used_cache = False
        if _obs_cache_enabled():
            if observed_extreme is not None:
                observed_extreme = _obs_cache_merge(ckey, observed_extreme, mode)
            else:
                cached = _obs_cache_get(ckey)
                if cached is not None:
                    observed_extreme = cached
                    used_cache = True
                    log.info(
                        f"   \U0001f5d2\ufe0f  observed: fresh read starved @ {lat:.2f},{lon:.2f} "
                        f"— using last-good cached {mode} extreme {cached:.1f}°C "
                        f"(cache bridges the gap; never lowers a high/raises a low)"
                    )

        if observed_extreme is None:
            log.info(
                f"   \U0001f319 observed: no actual readings yet for {target_day} "
                f"@ {lat:.2f},{lon:.2f} (local now {now_local:%Y-%m-%d %H:%M}, "
                f"{len(day_idx)} day-rows, no current) — too early in local day"
            )
            return None

        # --- REMAINING extreme + cross-model spread from the SPREAD source --
        spread_source = model_data or obs_data
        s_day_times, s_series, s_day_idx = self._parse_day(spread_source, target_day)
        per_model_remaining: List[float] = []
        hours_remaining = 0
        for series in s_series:
            day_vals = [series[i] if i < len(series) else None for i in s_day_idx]
            _, remaining, n_left = split_observed_remaining(s_day_times, day_vals, now_local, mode)
            hours_remaining = max(hours_remaining, n_left)
            if remaining:
                per_model_remaining.append(max(remaining) if mode == "high" else min(remaining))

        if per_model_remaining:
            remaining_extreme = sum(per_model_remaining) / len(per_model_remaining)
            remaining_spread = _stdev(per_model_remaining)
        else:
            remaining_extreme = None
            remaining_spread = 0.0

        state = ObservedDayState(
            observed_extreme_c=observed_extreme,
            remaining_extreme_c=remaining_extreme,
            remaining_spread_c=remaining_spread,
            hours_remaining=hours_remaining,
            n_models=len(s_series),
            current_temp_c=current_temp,
            mode=mode,
            as_of_local=now_local.isoformat(timespec="minutes"),
        )
        # WATCHER (fail-open, zero core impact): record exactly which weather
        # data drove this lock decision. n_models_with_data vs total surfaces
        # silent members; used_cache flags a bridged read.
        try:
            from overlay import weather_trace as _wt
            _wt.record_observed(
                lat=lat, lon=lon, target_day=str(target_day), mode=mode,
                observed_extreme=observed_extreme, current_temp=current_temp,
                remaining_spread=remaining_spread, hours_remaining=hours_remaining,
                n_models_with_data=len(per_model_remaining), n_models_total=len(s_series),
                is_locked=state.is_locked, used_cache=used_cache, starved=False,
            )
        except Exception:
            pass
        return state
