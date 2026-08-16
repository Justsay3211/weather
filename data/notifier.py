"""
data/notifier.py -- lightweight event -> Telegram notifier.

Owner-requested "Notifications" feature. Design guarantees:
  * FAST: sends are best-effort and never block the trading loop.
  * NO DISK LOGS: all state (transition tracking + rate limiting) is in RAM.
    Nothing is written to disk here. Bulk data still offloads to the VPS via
    data/vps_store.py; the rotating file log is size-capped in logger.py.
  * QUIET: only STATE CHANGES are announced (e.g. proxy -> direct), and repeats
    of the same message are rate-limited, so you never get spammed.

Every notification type is gated by a Config toggle in the /settings
"Notifications" tab (bot/settings_store.py). Toggle off -> nothing is sent.
"""
from __future__ import annotations

import time
import logging

log = logging.getLogger("weatherbot")

_SENDER = None       # callable(text) -> bool, bound once at startup
_STATE = {}          # transition tracker: state_key -> last value
_LAST_SENT = {}      # rate-limit: message_key -> epoch seconds
_MIN_REPEAT_S = 300  # never repeat the same message_key within 5 minutes


def bind(send_fn):
    """Wire the Telegram sender (called once from the dashboard at startup)."""
    global _SENDER
    _SENDER = send_fn


def _cfg():
    try:
        from config import Config
        return Config
    except Exception:
        return None


def _on(key):
    c = _cfg()
    try:
        return bool(getattr(c, key, False))
    except Exception:
        return False


def _emit(msg_key, text):
    """Best-effort, rate-limited send. Never raises, never blocks on failure."""
    if _SENDER is None:
        return
    now = time.time()
    if now - _LAST_SENT.get(msg_key, 0.0) < _MIN_REPEAT_S:
        return
    _LAST_SENT[msg_key] = now
    try:
        _SENDER(text)
    except Exception as e:
        log.debug("notifier emit failed: %s" % e)


def _changed(state_key, value):
    """True the first time a value is seen and whenever it changes."""
    prev = _STATE.get(state_key, "__unset__")
    if prev == value:
        return False
    _STATE[state_key] = value
    return True


def _classify(url):
    c = _cfg()
    base = ""
    try:
        base = (getattr(c, "VPS_BASE_URL", "") or "").rstrip("/")
    except Exception:
        base = ""
    if base and url and str(url).startswith(base):
        return "proxy"
    return "direct"


# -- weather data source (proxy <-> direct Open-Meteo) --------------------

def note_endpoint_error(url, err):
    """Record a proxy failure so the next switch to DIRECT can explain why."""
    if _classify(url) == "proxy":
        _STATE["last_proxy_err"] = str(err)[:120]


def note_weather_source(url):
    """Call on every SUCCESSFUL Open-Meteo fetch. Announces proxy<->direct
    switches when NOTIFY_WEATHER_SOURCE is on."""
    src = _classify(url)
    # Record the startup baseline silently; only announce real switches.
    if "weather_source" not in _STATE:
        _STATE["weather_source"] = src
        return
    if not _changed("weather_source", src):
        return
    if not _on("NOTIFY_WEATHER_SOURCE"):
        return
    if src == "direct":
        why = _STATE.pop("last_proxy_err", "")
        extra = (" \u2014 %s" % why) if why else " \u2014 proxy timeout/unreachable"
        _emit("weather_source:direct",
              "\u26a0\ufe0f Weather source: PROXY \u2192 DIRECT Open-Meteo%s" % extra)
    else:
        _STATE.pop("last_proxy_err", None)
        _emit("weather_source:proxy",
              "\u2705 Weather source: DIRECT \u2192 PROXY (edge node recovered)")


# -- VPS health / offload -------------------------------------------------

def note_vps_health(ok, detail=""):
    if not _changed("vps_health", bool(ok)):
        return
    if not _on("NOTIFY_VPS_HEALTH"):
        return
    if ok:
        _emit("vps_health:up", "\u2705 VPS edge node reachable%s" %
              ((" (%s)" % detail) if detail else ""))
    else:
        _emit("vps_health:down", "\U0001f534 VPS edge node unreachable%s" %
              ((" \u2014 %s" % detail) if detail else ""))


def note_vps_offload(summary):
    if not _on("NOTIFY_VPS_OFFLOAD"):
        return
    _emit("vps_offload", "\U0001f4e4 Offloaded data to VPS: %s" % summary)


# -- endpoint cooldown / ML status ----------------------------------------

def note_endpoint_cooldown(url, reason=""):
    if not _on("NOTIFY_ENDPOINT_COOLDOWN"):
        return
    _emit("cooldown:%s" % url, "\u2744\ufe0f Endpoint cooling down: %s %s" %
          (url, reason or ""))


def note_ml_status(ok, detail=""):
    if not _changed("ml_status", bool(ok)):
        return
    if not _on("NOTIFY_ML_STATUS"):
        return
    if ok:
        _emit("ml_status:ok", "\u2705 ML API responding again")
    else:
        _emit("ml_status:fail", "\u26a0\ufe0f ML API failing%s" %
              ((" \u2014 %s" % detail) if detail else ""))
