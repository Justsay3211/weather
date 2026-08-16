"""
WEATHER TRACE / HEALTH WATCHER (overlay, fail-open, ZERO core impact).

What this is
------------
A lightweight, append-only side-car that records — for every weather decision —
exactly which data drove it. It NEVER changes a trading decision, never raises
into the hot path (every public function is wrapped so any error is swallowed),
and writes at most one short JSON line per event. It exists purely so we can
answer, after the fact:

  * "For this position, what weather data was actually used — and was any of it
     null / stale / cache-bridged at decision time?"
  * "Do the paid providers (OpenWeather / WeatherAPI / Visual Crossing) AGREE
     with Open-Meteo on the peak, or is one of them dragging the ensemble?"
  * "Which Open-Meteo members are silently returning no data lately?"
  * "How often are we locking vs starved, and how wide is the model spread when
     we lock?"

Two record types are written to the SAME JSONL (distinguished by `kind`):
  - kind="fetch"    : provider / model-member agreement at fetch_all time.
  - kind="observed" : the observed-lock decision (locked vs starved, spread,
                      hours left, models-with-data vs total, cache usage).

Everything is opt-outable via Config.WEATHER_TRACE_ENABLED (default ON) and is
surfaced to the user through /exportdata (the file is shipped) and a dedicated
/weatherhealth summary command.

Design rules (match mae_mfe.py side-car):
  * import-safe with no hard deps (works in a bare offline sandbox);
  * buffered append with a size cap so a long run can't grow unbounded in RAM;
  * fail-open everywhere — a broken watcher must never stop a trade.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

try:
    from config import Config
except Exception:  # pragma: no cover - offline import safety
    Config = None  # type: ignore

try:
    from logger import log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("weather_trace")

_TRACE_PATH = "data/weather_trace.jsonl"
_BUF: List[str] = []
_FLUSH_EVERY = 20
_BUF_CAP = 5000


def _enabled() -> bool:
    try:
        if Config is None:
            return True
        return bool(getattr(Config, "WEATHER_TRACE_ENABLED", True))
    except Exception:
        return True


def _path() -> str:
    try:
        if Config is not None:
            return getattr(Config, "WEATHER_TRACE_PATH", _TRACE_PATH)
    except Exception:
        pass
    return _TRACE_PATH


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _r(x, nd: int = 2):
    try:
        return round(float(x), nd) if x is not None else None
    except Exception:
        return None


def _append(rec: Dict[str, Any]) -> None:
    """Buffer a record and flush periodically. Fail-open."""
    try:
        _BUF.append(json.dumps(rec, default=str))
        if len(_BUF) >= _FLUSH_EVERY:
            flush()
        elif len(_BUF) > _BUF_CAP:
            del _BUF[:-_BUF_CAP]
    except Exception as e:
        try:
            log.debug(f"weather_trace append failed: {e}")
        except Exception:
            pass


def flush() -> None:
    """Write buffered records to disk. Fail-open."""
    if not _BUF:
        return
    try:
        path = _path()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as f:
            f.write("\n".join(_BUF) + "\n")
        _BUF.clear()
    except Exception as e:
        try:
            log.debug(f"weather_trace flush failed: {e}")
        except Exception:
            pass


def record_observed(
    lat: float,
    lon: float,
    target_day: str,
    mode: str,
    observed_extreme: Optional[float],
    current_temp: Optional[float],
    remaining_spread: Optional[float],
    hours_remaining: Optional[float],
    n_models_with_data: int,
    n_models_total: int,
    is_locked: bool,
    used_cache: bool = False,
    starved: bool = False,
    city: Optional[str] = None,
    market: Optional[str] = None,
    strategy: Optional[str] = None,
) -> None:
    """Capture the observed-lock decision & data health. Fail-open."""
    if not _enabled():
        return
    try:
        rec = {
            "kind": "observed",
            "ts": _now_iso(),
            "city": city,
            "market": market,
            "strategy": strategy,
            "lat": round(float(lat), 3) if lat is not None else None,
            "lon": round(float(lon), 3) if lon is not None else None,
            "day": target_day,
            "mode": mode,
            "observed_extreme_c": _r(observed_extreme),
            "current_temp_c": _r(current_temp),
            "remaining_spread_c": _r(remaining_spread),
            "hours_remaining": _r(hours_remaining),
            "models_with_data": int(n_models_with_data or 0),
            "models_total": int(n_models_total or 0),
            "models_null": max(0, int(n_models_total or 0) - int(n_models_with_data or 0)),
            "is_locked": bool(is_locked),
            "used_cache": bool(used_cache),
            "starved": bool(starved),
        }
        _append(rec)
    except Exception:
        pass


def record_fetch(
    city: str,
    forecast_points: List[Any],
    model_points: Optional[Dict[str, int]] = None,
    model_label: Optional[Dict[str, str]] = None,
) -> None:
    """Capture per-source / per-member agreement for one fetch. Fail-open.

    `forecast_points` is a list of ForecastPoint-like objects exposing
    .source, .model, .temp_max_c / .temp_c. We summarise, per source, how many
    points came in and a representative peak temperature, so provider agreement
    (or a rogue provider) is visible without storing every point.
    """
    if not _enabled():
        return
    try:
        by_source: Dict[str, Dict[str, Any]] = {}
        for fp in (forecast_points or []):
            src = getattr(fp, "source", None) or "?"
            g = by_source.setdefault(src, {"n": 0, "peaks": []})
            g["n"] += 1
            peak = getattr(fp, "temp_max_c", None)
            if peak is None:
                peak = getattr(fp, "temp_c", None)
            if peak is not None:
                try:
                    g["peaks"].append(float(peak))
                except Exception:
                    pass
        src_summary = {}
        all_peaks: List[float] = []
        for src, g in by_source.items():
            peak = max(g["peaks"]) if g["peaks"] else None
            if peak is not None:
                all_peaks.append(peak)
            src_summary[src] = {"n": g["n"], "peak_c": _r(peak)}
        agree_spread = None
        if len(all_peaks) >= 2:
            agree_spread = round(max(all_peaks) - min(all_peaks), 2)
        null_members = []
        if model_points:
            lbl = model_label or {}
            null_members = [lbl.get(m, m) for m, n in model_points.items() if not n]
        rec = {
            "kind": "fetch",
            "ts": _now_iso(),
            "city": city,
            "sources": src_summary,
            "provider_peak_spread_c": agree_spread,
            "null_members": null_members,
        }
        _append(rec)
    except Exception:
        pass


def read_all() -> List[Dict[str, Any]]:
    """Read every trace record (buffered + on disk). Fail-open -> []."""
    out: List[Dict[str, Any]] = []
    try:
        flush()
    except Exception:
        pass
    try:
        path = _path()
        if os.path.exists(path):
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except Exception:
                        continue
    except Exception:
        pass
    return out


def summarize(limit: int = 500) -> str:
    """Human-readable health summary for the /weatherhealth command."""
    recs = read_all()
    if not recs:
        return ("\U0001f321\ufe0f <b>Weather health</b>\nNo trace captured yet — it "
                "builds as the bot fetches weather & evaluates locks (side-car, "
                "zero core impact). Check back after a few scans.")
    obs = [r for r in recs if r.get("kind") == "observed"][-limit:]
    fetch = [r for r in recs if r.get("kind") == "fetch"][-limit:]
    lines = ["\U0001f321\ufe0f <b>Weather health</b> (last "
             f"{len(obs)} locks / {len(fetch)} fetches)"]
    if obs:
        locked = sum(1 for r in obs if r.get("is_locked"))
        starved = sum(1 for r in obs if r.get("starved"))
        cached = sum(1 for r in obs if r.get("used_cache"))
        spreads = [r["remaining_spread_c"] for r in obs if r.get("remaining_spread_c") is not None]
        avg_spread = round(sum(spreads) / len(spreads), 2) if spreads else 0.0
        nulls = [r.get("models_null", 0) for r in obs]
        avg_null = round(sum(nulls) / len(nulls), 2) if nulls else 0.0
        lines.append(
            f"\u2022 Locked {locked}/{len(obs)} \u00b7 starved {starved} \u00b7 "
            f"cache-bridged {cached}")
        lines.append(
            f"\u2022 Avg model spread {avg_spread}\u00b0C \u00b7 avg null members/lock {avg_null}")
        trouble: Dict[str, int] = {}
        for r in obs:
            if r.get("starved") or r.get("used_cache") or r.get("models_null"):
                key = r.get("city") or f"{r.get('lat')},{r.get('lon')}"
                trouble[key] = trouble.get(key, 0) + 1
        if trouble:
            top = sorted(trouble.items(), key=lambda kv: kv[1], reverse=True)[:5]
            lines.append("\u2022 Data gaps: " + ", ".join(f"{k} ({v})" for k, v in top))
    if fetch:
        spreads = [r["provider_peak_spread_c"] for r in fetch if r.get("provider_peak_spread_c") is not None]
        if spreads:
            avg = round(sum(spreads) / len(spreads), 2)
            worst = round(max(spreads), 2)
            lines.append(
                f"\u2022 Provider peak agreement: avg \u00b1{avg}\u00b0C, worst \u00b1{worst}\u00b0C "
                f"(low = providers agree)")
        nm: Dict[str, int] = {}
        for r in fetch:
            for m in (r.get("null_members") or []):
                nm[m] = nm.get(m, 0) + 1
        if nm:
            top = sorted(nm.items(), key=lambda kv: kv[1], reverse=True)[:6]
            lines.append("\u2022 Silent members: " + ", ".join(f"{k} ({v}x)" for k, v in top))
    lines.append("\u2139\ufe0f Full detail ships in /exportdata (weather_trace.jsonl).")
    return "\n".join(lines)


def trace_path() -> str:
    """Public accessor for the export bundle."""
    return _path()
