"""
VPS Service — the dedicated, Telegram-customizable control layer for EVERYTHING
that touches the VPS edge node (weather proxy + data offload/store + pull).

User requirement
----------------
"a VPS on/off toggle in settings where OFF turns ALL types of VPS service
(including the weather fetch) off, ON does it; in the VPS setting I want
multiple options and toggles; a document-level toggle e.g. paper-trade VPS
offload OR bot's own memory; when offload ON it offloads to VPS storage and
deletes on Railway; when I run analysis with offload ON it checks the VPS and
returns the file; smart VPS management history; version on node; all toggles
accessible + customizable by Telegram."

This module is the SINGLE source of truth every other module asks:
  * services_enabled()          -> master switch (OFF = no VPS at all)
  * weather_proxy_enabled()     -> route Open-Meteo through the VPS?
  * offload_enabled()           -> ship append-only data to the VPS + prune?
  * document_target(stream)     -> 'vps' or 'railway' per data document
  * pull_on_analysis_enabled()  -> analysis pulls the bundle from the VPS first?
  * handling_for(stream)        -> 'full' | 'weekly' | 'monthly'

Every getter reads Config LIVE so a Telegram toggle takes effect next scan, and
every getter is fail-open (returns a safe default on any error). Turning the
master switch OFF hard-forces every sub-service off regardless of its own knob.
"""
from __future__ import annotations

from typing import Dict, List

try:
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("vps_service")


# stream basename -> (handling key, per-document target key)
_STREAM_KEYS = {
    "paper_trades.jsonl": ("VPS_HANDLING_PAPER_TRADES", "VPS_DOC_PAPER_TRADES"),
    "positions_mae_mfe.jsonl": ("VPS_HANDLING_MAE", "VPS_DOC_MAE"),
    "positions_timeseries.jsonl": ("VPS_HANDLING_TIMESERIES", "VPS_DOC_TIMESERIES"),
    "weather_trace.jsonl": ("VPS_HANDLING_WEATHER_TRACE", "VPS_DOC_WEATHER_TRACE"),
    "book_trace.jsonl": ("VPS_HANDLING_BOOK_TRACE", "VPS_DOC_BOOK_TRACE"),
    "session_manifest.jsonl": ("VPS_HANDLING_MANIFEST", "VPS_DOC_MANIFEST"),
}

SETTING_DEFAULTS = {
    # master switch — OFF disables EVERY VPS service incl. weather proxy
    "VPS_SERVICES_ENABLED": True,
    # sub-service switches (only matter when the master is ON)
    "VPS_WEATHER_PROXY_ENABLED": True,
    "VPS_OFFLOAD_ENABLED": False,
    "VPS_PULL_ON_ANALYSIS": True,
    "VPS_PULL_ON_OFFLOAD": False,
    # per-document default target: 'vps' (offload+delete on Railway) or 'railway'
    "VPS_DOC_DEFAULT": "vps",
}


def _cfg(name, default):
    if Config is None:
        return default
    try:
        v = getattr(Config, name, default)
        return default if v is None else v
    except Exception:
        return default


def _b(name, default):
    try:
        return bool(_cfg(name, default))
    except Exception:
        return bool(default)


def ensure_defaults() -> None:
    if Config is None:
        return
    for k, v in SETTING_DEFAULTS.items():
        if not hasattr(Config, k):
            try:
                setattr(Config, k, v)
            except Exception:
                pass


def configured() -> bool:
    """Is a VPS endpoint + token even present?"""
    try:
        base = str(_cfg("VPS_BASE_URL", "") or "").strip()
        token = str(_cfg("VPS_AUTH_TOKEN", "") or _cfg("PROXY_AUTH_TOKEN", "") or "").strip()
        return bool(base and token)
    except Exception:
        return False


def services_enabled() -> bool:
    """MASTER switch. OFF => no VPS service of ANY kind (incl. weather proxy)."""
    return bool(configured() and _b("VPS_SERVICES_ENABLED", True))


def weather_proxy_enabled() -> bool:
    """Route Open-Meteo through the VPS proxy? Hard-off when master is off."""
    return bool(services_enabled() and _b("VPS_WEATHER_PROXY_ENABLED", True))


def offload_enabled() -> bool:
    """Ship data to the VPS + prune Railway? Hard-off when master is off."""
    return bool(services_enabled() and _b("VPS_OFFLOAD_ENABLED", False))


def pull_on_analysis_enabled() -> bool:
    """When analysis runs and offload is on, pull the bundle from the VPS first
    so /analysis is never empty after a prune/restart."""
    return bool(offload_enabled() and _b("VPS_PULL_ON_ANALYSIS", True))


def pull_on_offload_enabled() -> bool:
    return bool(offload_enabled() and _b("VPS_PULL_ON_OFFLOAD", False))


def document_target(stream: str) -> str:
    """Per-document destination: 'vps' (offload + delete on Railway) or
    'railway' (keep in the bot's own memory). A document only offloads when the
    master + offload are on AND its target is 'vps'."""
    base = str(stream or "").split("/")[-1]
    default = str(_cfg("VPS_DOC_DEFAULT", "vps") or "vps").lower()
    keys = _STREAM_KEYS.get(base)
    if not keys:
        val = default
    else:
        val = str(_cfg(keys[1], default) or default).lower()
    return val if val in ("vps", "railway") else default


def stream_offloads(stream: str) -> bool:
    """Should THIS document be offloaded right now?"""
    return bool(offload_enabled() and document_target(stream) == "vps")


def handling_for(stream: str) -> str:
    base = str(stream or "").split("/")[-1]
    keys = _STREAM_KEYS.get(base)
    if not keys:
        return "full"
    val = str(_cfg(keys[0], "full") or "full").lower()
    return val if val in ("full", "weekly", "monthly") else "full"


def offload_streams() -> List[str]:
    """The documents currently targeted at the VPS (used by the offloader)."""
    return [s for s in _STREAM_KEYS.keys() if document_target(s) == "vps"]


def status() -> Dict[str, object]:
    """Snapshot for /settings and the version banner."""
    docs = {}
    for s in _STREAM_KEYS.keys():
        docs[s] = {"target": document_target(s), "handling": handling_for(s)}
    # version on node + active run id (crucial for logs / correct running)
    version = str(_cfg("VERSION", _cfg("BOT_VERSION", "")) or "")
    node_version = ""
    run_id = ""
    try:
        from data import run_manager as _rm
        run_id = _rm.run_id() or ""
    except Exception:
        pass
    return {
        "configured": configured(),
        "master_on": services_enabled(),
        "weather_proxy": weather_proxy_enabled(),
        "offload": offload_enabled(),
        "pull_on_analysis": pull_on_analysis_enabled(),
        "pull_on_offload": pull_on_offload_enabled(),
        "weather_fetch_mode": str(_cfg("WEATHER_FETCH_MODE", "normal") or "normal"),
        "base_url": str(_cfg("VPS_BASE_URL", "") or ""),
        "version": version,
        "node_version": node_version,
        "run_id": run_id,
        "documents": docs,
    }


def node_version(timeout: float = 4.0) -> str:
    """Query the VPS edge node's /health for its running version ('version on
    node'). Returns '' on any failure so callers can render gracefully."""
    if not (configured() and services_enabled()):
        return ""
    base = str(_cfg("VPS_BASE_URL", "") or "").rstrip("/")
    if not base:
        return ""
    try:
        import requests  # type: ignore
        for path in ("/health", "/metrics"):
            try:
                r = requests.get(base + path, timeout=timeout)
                if r.status_code == 200:
                    v = (r.json() or {}).get("version")
                    if v:
                        return str(v)
            except Exception:
                continue
    except Exception:
        pass
    return ""


ensure_defaults()
