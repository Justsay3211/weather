"""Per-model weather-fetch toggles (Req-27 #6).

Each Open-Meteo model member the bot fetches has a live on/off toggle exposed in
the Telegram /settings -> Weather tab. Green tick (✅) = the model is fetched;
red X (❌) = it is skipped. Because the SAME filtered ``models`` list is sent as
the Open-Meteo request parameter, a disabled member is skipped BOTH on the bot
AND on the VPS edge-node proxy (the node forwards whatever ``models`` the bot
asks for), so no request quota is wasted on a dead member such as the frequently
all-null ``ecmwf_ifs04``.

Fail-open by design: any error returns the input list unchanged, and if a user
somehow disables *every* member we keep the full list rather than fire an empty
request that would starve the forecaster.
"""

# Canonical members the bot may fetch, in settings-panel display order, mapped
# to their friendly labels. Kept in sync with data/weather_fetcher.py
# (model_label) and config.OPEN_METEO_MODELS.
MODEL_LABELS = {
    "ecmwf_ifs": "ECMWF-HRES",
    "ecmwf_ifs025": "ECMWF-025",
    "ecmwf_ifs04": "ECMWF-04",
    "gfs_seamless": "GFS",
    "icon_seamless": "ICON",
    "jma_seamless": "JMA",
    "gem_seamless": "GEM",
}

MODEL_ORDER = list(MODEL_LABELS.keys())


def toggle_key(model):
    """Settings BOOL key for a model id.

    e.g. ``ecmwf_ifs`` -> ``WX_MODEL_ECMWF_IFS_ENABLED``.
    """
    return "WX_MODEL_%s_ENABLED" % str(model).strip().upper()


def is_enabled(model):
    """True if the model's toggle is ON (defaults ON when unset)."""
    try:
        from config import Config
        return bool(getattr(Config, toggle_key(model), True))
    except Exception:
        return True


def active_models(models):
    """Return ``models`` filtered to the enabled members (fail-open).

    Preserves order. Returns the original list unchanged on any error or if the
    filter would empty it (so at least one member is always requested).
    """
    try:
        kept = [m for m in models if is_enabled(m)]
        return kept or list(models)
    except Exception:
        return list(models)
