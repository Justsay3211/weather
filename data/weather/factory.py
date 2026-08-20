"""Build a fully wired WeatherPipeline from Config + live VPS state.

This is the single place that translates user-facing settings (Config /
runtime_settings.json / vps_service master switches) into a concrete pipeline:
which providers are enabled, where each runs, and how requests reach the
network. Everything here is defensive so a mis-config degrades gracefully
instead of crashing the bot.

The bot's real HTTP + VPS-proxy callables are injected by the caller
(data/weather_fetcher.py) so this module stays import-safe and offline-testable.
"""

from typing import Callable, List, Optional

from .execution import ExecutionRouter
from .pipeline import WeatherPipeline
from .sources import (
    OpenMeteoAdapter, OpenWeatherAdapter, WeatherApiAdapter,
    VisualCrossingAdapter, NwsAdapter,
)


def _parse_overrides(raw: str) -> dict:
    out = {}
    for chunk in (raw or "").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        src, loc = chunk.split(":", 1)
        src, loc = src.strip(), loc.strip().lower()
        if src and loc in ("vps", "bot", "off"):
            out[src] = loc
    return out


def build_adapters(config) -> List:
    """Instantiate every ENABLED provider adapter from Config."""
    adapters: List = []
    if getattr(config, "WEATHER_SRC_OPEN_METEO_ENABLED", True):
        models = [m for m in getattr(config, "OPEN_METEO_MODELS", None)
                  or ["ecmwf_ifs025", "gfs_global", "icon_global"]]
        # keep only models the pipeline adapter recognizes (maps to a family)
        known = set(OpenMeteoAdapter.MODELS.keys())
        models = [m for m in models if m in known] or ["ecmwf_ifs025", "gfs_global", "icon_global"]
        adapters.append(OpenMeteoAdapter(
            models=models,
            forecast_days=int(getattr(config, "WEATHER_PIPELINE_FORECAST_DAYS", 3))))
    if getattr(config, "WEATHER_SRC_OPENWEATHER_ENABLED", True) and getattr(config, "OPENWEATHER_API_KEY", ""):
        adapters.append(OpenWeatherAdapter(api_key=config.OPENWEATHER_API_KEY))
    if getattr(config, "WEATHER_SRC_WEATHERAPI_ENABLED", True) and getattr(config, "WEATHERAPI_API_KEY", ""):
        adapters.append(WeatherApiAdapter(api_key=config.WEATHERAPI_API_KEY,
                                          days=int(getattr(config, "WEATHER_PIPELINE_FORECAST_DAYS", 3))))
    if getattr(config, "WEATHER_SRC_VISUALCROSSING_ENABLED", True) and getattr(config, "VISUALCROSSING_API_KEY", ""):
        adapters.append(VisualCrossingAdapter(api_key=config.VISUALCROSSING_API_KEY))
    if getattr(config, "WEATHER_SRC_NWS_ENABLED", True):
        adapters.append(NwsAdapter())
    return adapters


def build_router(config, vps_available: bool, vps_weather_enabled: bool) -> ExecutionRouter:
    return ExecutionRouter(
        mode=getattr(config, "WEATHER_EXECUTION_MODE", "auto"),
        source_overrides=_parse_overrides(getattr(config, "WEATHER_SOURCE_LOCATION", "")),
        vps_available=bool(vps_available),
        vps_weather_enabled=bool(vps_weather_enabled))


def build_pipeline(config,
                   bot_http_get: Optional[Callable] = None,
                   vps_fetch: Optional[Callable] = None,
                   vps_available: bool = False,
                   vps_weather_enabled: bool = False,
                   cache_ttl_s: Optional[int] = None) -> WeatherPipeline:
    adapters = build_adapters(config)
    router = build_router(config, vps_available, vps_weather_enabled)
    ttl = cache_ttl_s if cache_ttl_s is not None else int(
        getattr(config, "WEATHER_FORECAST_CACHE_SECONDS", 300))
    return WeatherPipeline(adapters, router, bot_http_get=bot_http_get,
                           vps_fetch=vps_fetch, cache_ttl_s=ttl)


def describe(config, vps_available: bool = False, vps_weather_enabled: bool = False) -> dict:
    """Human-readable summary of the resolved configuration (for /weatherquota
    and diagnostics) without performing any network IO."""
    adapters = build_adapters(config)
    router = build_router(config, vps_available, vps_weather_enabled)
    providers = {}
    for a in adapters:
        providers[a.provider] = router.resolve(a.provider)
    return {
        "enabled": bool(getattr(config, "WEATHER_PIPELINE_ENABLED", False)),
        "mode": getattr(config, "WEATHER_EXECUTION_MODE", "auto"),
        "providers": providers,
        "router": router.summary(),
    }
