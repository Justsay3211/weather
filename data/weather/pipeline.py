"""Pipeline orchestrator.

Responsibilities:
  * Decide, per source, WHERE it runs (VPS / bot / off) via ExecutionRouter.
  * Enforce cache-first + per-source daily request budgets + circuit breakers.
  * Fetch (on the bot) OR delegate to the VPS proxy OR skip — without ever
    making unoptimal direct calls when a source is VPS-only.
  * Normalize + QC every series, then run consensus + confidence per variable.
  * Produce a fully explainable PipelineResult with provenance + source health.

IO is injected:
  * bot_http_get(url, params, headers) -> dict   (direct-from-bot fetch)
  * vps_fetch(source_key, lat, lon, city) -> list[ForecastSeries] | None
    (delegate to the VPS proxy; return None when the node is unreachable so the
    pipeline can skip rather than fall back to unoptimal direct calls)
Both are optional; when absent the corresponding location simply yields nothing.
"""

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

from .schema import ForecastSeries, Category, Freshness
from .registry import SourceRegistry, effective_independent_count, dependency_report
from .execution import ExecutionRouter, Location
from .engine import consensus_for, confidence_for, ConsensusResult, ConfidenceResult
from .health import SourceHealth


@dataclass
class PipelineResult:
    location: str
    when: datetime
    consensus: Dict[str, ConsensusResult] = field(default_factory=dict)
    confidence: Dict[str, ConfidenceResult] = field(default_factory=dict)
    series: List[ForecastSeries] = field(default_factory=list)
    n_independent: int = 0
    execution: Dict[str, str] = field(default_factory=dict)   # source -> location used
    health: Dict[str, dict] = field(default_factory=dict)
    provenance: Dict[str, List[str]] = field(default_factory=dict)
    excluded: Dict[str, str] = field(default_factory=dict)     # source -> reason
    fetched_count: int = 0
    cache_hits: int = 0


def _qc(series: ForecastSeries) -> ForecastSeries:
    """Quality control: drop impossible values, flag empty/corrupt series."""
    good = []
    for p in series.points:
        if p.temp_c is not None and (p.temp_c < -90 or p.temp_c > 60):
            continue
        if p.humidity_pct is not None and (p.humidity_pct < 0 or p.humidity_pct > 100):
            p.humidity_pct = max(0.0, min(100.0, p.humidity_pct))
        if p.precip_mm is not None and p.precip_mm < 0:
            p.precip_mm = 0.0
        good.append(p)
    series.points = good
    if not good:
        series.quality_status = "empty_after_qc"
    return series


class WeatherPipeline:
    def __init__(self, adapters: List, router: ExecutionRouter,
                 bot_http_get: Optional[Callable] = None,
                 vps_fetch: Optional[Callable] = None,
                 cache_ttl_s: int = 300,
                 health: Optional[SourceHealth] = None,
                 now_fn: Optional[Callable[[], datetime]] = None):
        self.adapters = adapters
        self.router = router
        self.bot_http_get = bot_http_get
        self.vps_fetch = vps_fetch
        self.cache_ttl_s = int(cache_ttl_s)
        self.health = health or SourceHealth()
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.registry = SourceRegistry()
        for a in adapters:
            for idn in a.identities():
                self.registry.register(idn)
        # cache: key -> (epoch, list[ForecastSeries])
        self._cache: Dict[str, tuple] = {}
        # per-source rolling daily request counters
        self._req_counts: Dict[str, int] = {}
        self._req_window: float = time.time()
        # split of upstream requests by execution location (for /weatherquota)
        self.req_by_location: Dict[str, int] = {Location.VPS: 0, Location.BOT: 0}

    # ---- budgets -------------------------------------------------------
    def _roll_window(self):
        if time.time() - self._req_window > 86400:
            self._req_counts = {}
            self._req_window = time.time()
            self.req_by_location = {Location.VPS: 0, Location.BOT: 0}

    def _budget_ok(self, adapter) -> bool:
        self._roll_window()
        used = self._req_counts.get(adapter.provider, 0)
        return used < int(getattr(adapter, "daily_budget", 1000))

    def _count_req(self, adapter, location: str):
        self._req_counts[adapter.provider] = self._req_counts.get(adapter.provider, 0) + 1
        self.req_by_location[location] = self.req_by_location.get(location, 0) + 1

    def quota_snapshot(self) -> Dict:
        self._roll_window()
        return {
            "window_started": int(self._req_window),
            "by_provider": dict(self._req_counts),
            "by_location": dict(self.req_by_location),
            "total": sum(self._req_counts.values()),
        }

    # ---- fetch one adapter --------------------------------------------
    def _fetch_adapter(self, adapter, lat, lon, city, result: PipelineResult) -> List[ForecastSeries]:
        provider = adapter.provider
        location = self.router.resolve(provider)
        result.execution[provider] = location

        if location == Location.OFF:
            result.excluded[provider] = "disabled (execution=off)"
            return []

        cache_key = "%s|%s|%s" % (provider, round(lat, 3), round(lon, 3))
        cached = self._cache.get(cache_key)
        if cached and (time.time() - cached[0]) < self.cache_ttl_s:
            result.cache_hits += 1
            return cached[1]

        if self.health.is_open(provider):
            result.excluded[provider] = "circuit breaker open"
            return cached[1] if cached else []

        series: List[ForecastSeries] = []
        t0 = time.time()
        try:
            if location == Location.VPS:
                if self.vps_fetch is None:
                    # VPS-only but no proxy wired: DO NOT make unoptimal direct
                    # calls — skip and let the VPS take care of it.
                    result.excluded[provider] = "vps-only, proxy unavailable (skipped, no direct call)"
                    return cached[1] if cached else []
                got = self.vps_fetch(provider, lat, lon, city)
                if got is None:
                    result.excluded[provider] = "vps unreachable (skipped, no direct fallback)"
                    return cached[1] if cached else []
                series = list(got)
                for s in series:
                    s.executed_on = Location.VPS
            else:  # Location.BOT
                if self.bot_http_get is None:
                    result.excluded[provider] = "bot fetch not wired"
                    return cached[1] if cached else []
                if not self._budget_ok(adapter):
                    result.excluded[provider] = "daily budget exhausted"
                    return cached[1] if cached else []
                series = adapter.fetch_and_parse(self.bot_http_get, lat, lon, city)
                for s in series:
                    s.executed_on = Location.BOT
            self._count_req(adapter, location)
            result.fetched_count += 1
            self.health.record_ok(provider, latency_ms=(time.time() - t0) * 1000.0)
        except Exception as exc:  # noqa: BLE001 — any adapter/network error
            self.health.record_fail(provider)
            result.excluded[provider] = "error: %s" % (str(exc)[:120])
            return cached[1] if cached else []

        series = [_qc(s) for s in series]
        series = [s for s in series if s.points]
        if series:
            self._cache[cache_key] = (time.time(), series)
        return series

    # ---- run -----------------------------------------------------------
    def run(self, lat: float, lon: float, city: str = "",
            when: Optional[datetime] = None,
            variables: Optional[List[str]] = None,
            observation_series: Optional[List[ForecastSeries]] = None) -> PipelineResult:
        when = when or self.now_fn()
        variables = variables or ["temp_c", "precip_mm", "precip_prob_pct"]
        result = PipelineResult(location=city or ("%s,%s" % (lat, lon)), when=when)

        all_series: List[ForecastSeries] = []
        for adapter in self.adapters:
            all_series.extend(self._fetch_adapter(adapter, lat, lon, city, result))

        result.series = all_series
        result.n_independent = effective_independent_count(all_series)
        result.provenance = dependency_report(all_series)
        result.health = self.health.snapshot()

        typical = {"temp_c": 3.0, "precip_mm": 3.0, "precip_prob_pct": 30.0,
                   "humidity_pct": 15.0, "wind_speed_kmh": 10.0}
        health_states = {k: v.get("state") for k, v in result.health.items()}
        for var in variables:
            cons = consensus_for(all_series, var, when)
            result.consensus[var] = cons
            result.confidence[var] = confidence_for(
                all_series, cons, var, when,
                typical_spread=typical.get(var, 3.0),
                observation_series=observation_series,
                health_states=health_states)
        return result
