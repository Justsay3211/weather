# Session 2026-08-20 — Advanced Weather Pipeline, Quota & Fixes

Continuation of the 2026-08-19 grade+edge+VPS upgrade. This session delivered
the **advanced multi-layer weather-source pipeline**, made execution fully
customizable (VPS / bot / off, per-source), added the `/weatherquota` command
with a UTC-daily edge counter, consolidated the scattered late-observed
settings, and genuinely fixed the `/analysis` trade-log.

---

## 1. Advanced weather pipeline (`data/weather/`)

A layered, fully-explainable ensemble pipeline that replaces "more data" with
"better, de-duplicated, independent forecasts". Opt-in (default OFF); the legacy
fetcher stays the default until `WEATHER_PIPELINE_ENABLED=1`.

**Package modules**
- `schema.py` — `SourceIdentity` (with `model_family` as the de-dup key),
  `ForecastPoint`, `ForecastSeries`, `Category`, `Freshness`.
- `registry.py` — dependency graph + `effective_independent_count` /
  `dependency_report` so Open-Meteo-ECMWF and direct-ECMWF count as ONE
  independent model, not two.
- `execution.py` — `ExecutionRouter` (mode = auto/vps/bot/off, per-source
  overrides, VPS availability) resolving each provider to a `Location`.
- `health.py` — per-source circuit breaker (opens after repeated failures).
- `engine.py` — `consensus_for` (median/weighted) + `confidence_for`
  (independence .30 / agreement .30 / freshness .20 / observation .12 /
  health .08).
- `sources.py` — 5 provider adapters (Open-Meteo multi-model, WeatherAPI,
  OpenWeather, Visual Crossing, NWS) with injected `http_get` for offline tests.
- `pipeline.py` — `WeatherPipeline` orchestration + per-source daily budget +
  QC + `quota_snapshot()`.
- `factory.py` — `build_adapters` / `build_router` / `build_pipeline` /
  `describe` from Config.

**Key correctness fix this session:** the Open-Meteo model map now recognizes
BOTH the raw ids (`gfs_global`, `icon_global`, …) AND the blended `*_seamless`
variants the live `OPEN_METEO_MODELS` config actually uses (`gfs_seamless`,
`icon_seamless`, `jma_seamless`, `gem_seamless`, `ecmwf_ifs`). Without this the
ensemble silently collapsed to a single model (ECMWF only).

## 2. Customizable execution (VPS / bot / off)

- `WEATHER_EXECUTION_MODE` = `auto` | `vps` | `bot` | `off` (global default).
- `WEATHER_SOURCE_LOCATION` = per-source overrides string (e.g.
  `open_meteo:vps,nws:bot`).
- Behavior guarantees:
  - **VPS off (master switch)** → weather runs on the bot.
  - **VPS-only + proxy unavailable / unreachable** → source is SKIPPED, the bot
    makes **ZERO** unoptimal direct calls (the VPS "takes care of it").
  - **off** → source disabled entirely.
  - old (legacy fetcher) and new (pipeline) both preserved; switch via
    `WEATHER_PIPELINE_ENABLED`.
- Per-source enable toggles: `WEATHER_SRC_OPEN_METEO_ENABLED`,
  `_OPENWEATHER_`, `_WEATHERAPI_`, `_VISUALCROSSING_`, `_NWS_ENABLED`.

## 3. Wiring into the bot (`data/weather_fetcher.py`)

- `fetch_all()` now delegates to the pipeline when `WEATHER_PIPELINE_ENABLED`
  is set, converting `PipelineResult` → legacy `List[ForecastPoint]` so all
  downstream consumers are unchanged.
- Injected `bot_http_get` (bot session) for BOT-routed sources and an
  `edge_http_get` that routes Open-Meteo through the VPS edge cache for
  VPS-routed sources. Non-proxyable providers on VPS-only return `None` →
  skipped, never a direct call.
- **Any** pipeline error falls back transparently to the legacy multi-source
  path (validated by the wire test).

## 4. `/weatherquota` command + edge daily counter

- **Edge node (`weatherpol-edge-node/app.py`, now `edge-1.2.0`)**: added
  `open_meteo_today`, a UTC-midnight-resetting counter bumped once per REAL
  upstream Open-Meteo request (single chokepoint in `_fetch_upstream`). Exposed
  in `/metrics` as `open_meteo_today` + `open_meteo_today_utc_date`.
- **Bot**: `/weatherquota` (aliases `/wxquota`, `/quota`) shows today's usage
  split VPS (edge) vs bot (Railway direct) against the ~13k/day budget + safety
  target, the fetch mode, adaptive cache TTL window, and — when the pipeline is
  on — the resolved per-source execution routing (`factory.describe`).
- Helper `overlay/vps_service.edge_metrics()` fetches the edge `/metrics` JSON
  (respects the VPS master switch, returns `{}` on any failure).

## 5. Settings consolidation (`bot/settings_store.py`)

- New consolidated group **`lateobsall`** (tab "Late-Obs ⭐", "the main one")
  gathering ALL scattered late-observed keys (10 bool + 30 num), arranged
  master toggles → primary gates → entry bands → timing → remaster → no-arb →
  observed-no fix. Detailed tabs kept.
- New group **`wxpipeline`** (tab "WX Pipeline") for the pipeline + execution
  keys. 27 groups total.

## 6. `/analysis` trade-log fix (`bot/telegram_ui.py`)

- Root cause: the downloadable CSV was built ONLY from `_read_trade_log()`
  (the live LOCAL log), which is truncated after a VPS offload → "No trade-log
  records yet" even when history existed in the pulled-back bundle.
- Fix: build the CSV from `_merged_history_records()` (local tail + VPS bundle)
  first, fall back to the live log; caption shows the source; clearer empty msg.

---

## Tests
- `tools/smoke_weather_pipeline.py` — **20/20 PASS** (dedup, independence,
  consensus, routing vps/bot/off, VPS-only zero-direct-call, budget stop,
  circuit breaker, QC).
- `tools/smoke_turn51_wire.py` — **9/9 PASS** (fetch_all delegation, ForecastPoint
  conversion, only-Open-Meteo hit, multi-family ensemble, legacy path when off,
  graceful fallback on pipeline error).
- `tools/weather_source_selftest.py` — deploy-time live-key check for all 5
  providers (stdlib urllib).

## New config keys (`config.py`)
`WEATHERAPI_API_KEY`, `VISUALCROSSING_API_KEY`, `ECMWF_API_KEY`,
`WEATHER_PIPELINE_ENABLED` (0), `WEATHER_EXECUTION_MODE` ('auto'),
`WEATHER_SOURCE_LOCATION` (''), `WEATHER_SRC_*_ENABLED` (5×, all 1),
`WEATHER_PIPELINE_FORECAST_DAYS` (3).

## Files changed this session
- NEW: `data/weather/{__init__,schema,registry,execution,health,engine,sources,pipeline,factory}.py`,
  `tools/smoke_weather_pipeline.py`, `tools/weather_source_selftest.py`,
  `tools/smoke_turn51_wire.py`, this doc.
- CHANGED: `config.py`, `bot/settings_store.py`, `bot/telegram_ui.py`,
  `overlay/vps_service.py`, `data/weather_fetcher.py`,
  `weatherpol-edge-node/app.py` (edge-1.2.0).
