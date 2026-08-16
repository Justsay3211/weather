"""
CITY THROTTLE.

PnL is strongly dispersed by city. Winners cluster in a handful of markets
(Paris, Seoul, Shanghai, London, Hong Kong, Moscow, Wellington) while a
repeating set leaks (Madrid, Ankara, Houston, Chicago, Tokyo, Lucknow). There is
no per-city knob in the base bot, so this overlay adds one:

  * BLOCK a city outright (skip every trade there), or
  * scale size UP for proven winners / DOWN for chronic leakers.

City lists + multipliers are live-tunable comma-separated settings (case- and
space-insensitive match on the `city` passed to _place). Master switch
CITY_THROTTLE_ENABLED (default ON). Fail-open.

Defaults are intentionally conservative: no city is blocked outright; leakers are
halved and winners get a mild boost, so the mechanism is active but never
zeroes a market unless the user asks.
"""

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

SETTING_DEFAULTS = {
    "CITY_THROTTLE_ENABLED": True,
    "CITY_BLOCKLIST": "",
    "CITY_UNDERWEIGHT": "madrid,ankara,houston,chicago,tokyo,lucknow",
    "CITY_OVERWEIGHT": "paris,seoul,shanghai,london,hong kong,moscow",
    "CITY_UNDERWEIGHT_MULT": 0.5,
    "CITY_OVERWEIGHT_MULT": 1.3,
}


def ensure_defaults():
    if Config is None:
        return
    for key, default in SETTING_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _list(name):
    raw = getattr(Config, name, "") if Config is not None else ""
    if not raw:
        return set()
    return {c.strip().lower() for c in str(raw).split(",") if c.strip()}


def _f(name, default):
    if Config is None:
        return default
    try:
        return float(getattr(Config, name, default))
    except (TypeError, ValueError):
        return default


def _norm(city):
    return (city or "").strip().lower()


def city_allowed(city):
    """Return (ok, reason). ok=False => city is blocklisted."""
    if Config is None or not bool(getattr(Config, "CITY_THROTTLE_ENABLED", True)):
        return True, "ok"
    c = _norm(city)
    if c and c in _list("CITY_BLOCKLIST"):
        return False, f"city '{c}' blocklisted"
    return True, "ok"


def city_multiplier(city):
    """Bounded size multiplier for this city (1.0 = neutral)."""
    if Config is None or not bool(getattr(Config, "CITY_THROTTLE_ENABLED", True)):
        return 1.0
    c = _norm(city)
    if not c:
        return 1.0
    if c in _list("CITY_UNDERWEIGHT"):
        return max(0.1, _f("CITY_UNDERWEIGHT_MULT", 0.5))
    if c in _list("CITY_OVERWEIGHT"):
        return min(2.0, _f("CITY_OVERWEIGHT_MULT", 1.3))
    return 1.0


ensure_defaults()
