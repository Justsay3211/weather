"""
Master Weather Intelligence pipeline (multi-source, multi-layer).

This package implements a modular, source-agnostic, cache-first, fault-tolerant
weather engine layered on top of the existing WeatherFetcher. It is designed so
additional model sources (raw NWP, AI models, ensembles, observations) can be
injected later WITHOUT rewriting the core.

Key design goals (from master_weather_intelligence_pipeline_prompt.md):
  * Never blindly average providers -> track source identity / model family and
    de-duplicate providers that expose the SAME underlying model.
  * Distinguish RAW model / PROVIDER API / OBSERVATION / DERIVED categories.
  * Cache-first, per-source request budgets + circuit breakers.
  * Consensus (median/mean/spread/effective-independent-count) + a separate
    confidence engine + source-health monitor + robust fallback ladder.
  * Fully customizable EXECUTION LOCATION: each source (or the whole pipeline)
    can run on the VPS, on the bot (Railway/runtime), or be turned OFF. When a
    source is VPS-only and the VPS is unavailable the bot does NOT fall back to
    unoptimal direct calls -- it simply skips that source ("vps take care").

The package is intentionally dependency-light (stdlib only) so it compiles and
unit-tests offline. Network access is injected via a `http_get` callable, so
the whole pipeline is testable without hitting the internet.
"""

from .schema import (
    SourceIdentity,
    ForecastPoint,
    ForecastSeries,
    Freshness,
    Category,
)
from .registry import SourceRegistry, effective_independent_count
from .execution import ExecutionRouter, Location
from .engine import consensus_for, ConsensusResult, confidence_for, ConfidenceResult
from .health import SourceHealth, HealthState
from .pipeline import WeatherPipeline, PipelineResult

__all__ = [
    "SourceIdentity",
    "ForecastPoint",
    "ForecastSeries",
    "Freshness",
    "Category",
    "SourceRegistry",
    "effective_independent_count",
    "ExecutionRouter",
    "Location",
    "consensus_for",
    "ConsensusResult",
    "confidence_for",
    "ConfidenceResult",
    "SourceHealth",
    "HealthState",
    "WeatherPipeline",
    "PipelineResult",
]
