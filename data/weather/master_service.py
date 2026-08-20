"""MasterWeatherService -- the single live entry point for the whole advanced
weather intelligence pipeline (the "master pipeline we missed").

It composes, in order:
  1. WeatherPipeline.run()   -> fetch every ENABLED source (routed VPS/bot/off),
                                standardize + normalize + QC into ForecastSeries,
                                de-duplicate dependent models (open-meteo ECMWF
                                vs direct ECMWF count once), and build a raw
                                weighted consensus + confidence per variable.
  2. MasterEngine.analyze()  -> layer the BRAIN on top of the raw consensus:
                                historical skill weighting, residual/bias
                                correction (local GBM), probability calibration,
                                model + ensemble agreement, forecast age /
                                freshness, smart SOURCE SELECTION (champion vs
                                challenger vs suppressed to save requests), the
                                RL-lite weight optimizer, quality control and
                                auto-repair -> a calibrated MasterForecastResult.
  3. bridge                  -> turn that into (a) grade/edge features so the
                                grade & edge engine folds weather in, (b) a clear
                                Telegram buy block (best estimate + band,
                                confidence + label, calibrated chance,
                                calibration/bias status, model agreement, why,
                                warnings), and (c) a support/confidence score.

Everything is defensive: if the master brain is disabled or errors, the service
still returns the plain pipeline consensus so the bot degrades gracefully.

The service ALSO exposes the smart-source-selection plan so callers can suppress
non-elected providers on the NEXT fetch (open-meteo -> ecmwf only once ecmwf is
proven best, then re-audit after WEATHER_SELECT_REEVAL_DAYS), and feedback hooks
(ingest_observation / ingest_precip_outcome) so the system LEARNS and adjusts.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from logger import log

from . import bridge as _bridge
from . import factory as _factory


@dataclass
class MasterServiceResult:
    """Everything a caller needs from one location analysis."""
    location: str
    when: datetime
    master: Any = None                # MasterForecastResult (or None if brain off)
    pipeline: Any = None              # raw PipelineResult
    n_independent: int = 0
    # convenience projections for the driving temperature variable
    grade_edge_features: Dict[str, Any] = field(default_factory=dict)
    buy_blocks: Dict[str, str] = field(default_factory=dict)   # variable -> message
    support: Dict[str, float] = field(default_factory=dict)    # variable -> 0..1
    warnings: List[str] = field(default_factory=list)
    provenance: Dict[str, List[str]] = field(default_factory=dict)
    suppressed_providers: List[str] = field(default_factory=list)
    brain_active: bool = False


class MasterWeatherService:
    def __init__(self, config,
                 bot_http_get: Optional[Callable] = None,
                 vps_fetch: Optional[Callable] = None,
                 vps_available: bool = False,
                 vps_weather_enabled: bool = False,
                 cache_ttl_s: Optional[int] = None):
        self.config = config
        self.pipeline = _factory.build_pipeline(
            config, bot_http_get=bot_http_get, vps_fetch=vps_fetch,
            vps_available=vps_available, vps_weather_enabled=vps_weather_enabled,
            cache_ttl_s=cache_ttl_s)
        # brain is optional / persisted; None when WEATHER_MASTER_ENABLED is off
        self.master = _factory.build_master(config)
        self._default_vars = ["temp_c", "precip_mm", "precip_prob_pct"]

    # -- main analysis ---------------------------------------------------
    def analyze(self, lat: float, lon: float, city: str = "",
                variables: Optional[List[str]] = None,
                when: Optional[datetime] = None,
                observation_series: Optional[List] = None) -> MasterServiceResult:
        when = when or datetime.now(timezone.utc)
        variables = variables or self._default_vars
        pres = self.pipeline.run(lat, lon, city=city, when=when)
        out = MasterServiceResult(
            location=city or getattr(pres, "location", ""),
            when=when, pipeline=pres,
            n_independent=int(getattr(pres, "n_independent", 0) or 0),
            provenance=dict(getattr(pres, "provenance", {}) or {}))

        if self.master is None:
            # brain off -> expose the raw consensus so the bot still works
            return out
        try:
            mf = self.master.analyze(pres, lat, lon, when=when,
                                     variables=variables,
                                     observation_series=observation_series)
        except Exception as e:  # pragma: no cover - defensive
            log.debug(f"MasterWeatherService: brain analyze failed, using raw consensus: {e}")
            return out

        out.master = mf
        out.brain_active = True
        out.n_independent = int(getattr(mf, "n_independent", out.n_independent) or out.n_independent)
        out.warnings = list(getattr(mf, "warnings", []) or [])
        try:
            out.suppressed_providers = list(self.master.suppressed_providers() or [])
        except Exception:
            out.suppressed_providers = []

        # per-variable projections via the bridge
        for var in variables:
            try:
                out.support[var] = float(_bridge.support_score(mf, var))
            except Exception:
                pass
            try:
                out.buy_blocks[var] = _bridge.buy_message_block(mf, var)
            except Exception:
                pass
        # grade/edge features keyed on the driving temperature variable
        try:
            out.grade_edge_features = dict(_bridge.features_from_master(mf, "temp_c"))
        except Exception:
            out.grade_edge_features = {}
        return out

    # -- smart source selection (request saving) -------------------------
    def selection_plan(self) -> Dict[str, str]:
        """family -> role (champion/challenger/audit/suppressed). Empty when the
        brain is off. Callers use this to STOP fetching suppressed sources."""
        if self.master is None:
            return {}
        try:
            return dict(self.master.selection_plan())
        except Exception:
            return {}

    def suppressed_providers(self) -> List[str]:
        if self.master is None:
            return []
        try:
            return list(self.master.suppressed_providers() or [])
        except Exception:
            return []

    # -- learning feedback (called after settlement / observation) -------
    def ingest_observation(self, location: str, lat: float, variable: str,
                           observed_value: float, when: Optional[datetime] = None):
        if self.master is None:
            return
        try:
            self.master.ingest_observation(location, lat, variable, observed_value, when=when)
        except Exception as e:
            log.debug(f"MasterWeatherService.ingest_observation failed: {e}")

    def ingest_precip_outcome(self, location: str, lead_hours: float,
                              predicted_prob: float, occurred: bool):
        if self.master is None:
            return
        try:
            self.master.ingest_precip_outcome(location, lead_hours, predicted_prob, occurred)
        except Exception as e:
            log.debug(f"MasterWeatherService.ingest_precip_outcome failed: {e}")

    def save(self):
        """Persist all learned state (skill/residual/calibration/selection)."""
        if self.master is None:
            return
        try:
            self.master.save()
        except Exception as e:
            log.debug(f"MasterWeatherService.save failed: {e}")

    def quota_snapshot(self) -> Dict:
        try:
            return dict(self.pipeline.quota_snapshot())
        except Exception:
            return {}


def build_master_service(config, **kwargs) -> Optional[MasterWeatherService]:
    """Factory used by weather_fetcher: returns a service only when the advanced
    pipeline is enabled, else None so the legacy fetcher stays in charge."""
    if not bool(getattr(config, "WEATHER_PIPELINE_ENABLED", False)):
        return None
    try:
        return MasterWeatherService(config, **kwargs)
    except Exception as e:  # pragma: no cover
        log.debug(f"build_master_service failed: {e}")
        return None
