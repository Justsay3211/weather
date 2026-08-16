"""
GOLDEN FILTER + LATE-OBSERVED TIMING GATE  (overlay, fail-open, ZERO core impact).

Why this module exists
----------------------
A full 962-position audit across the pre-1000 history + the recent exports found
a small, DURABLE profitable core buried inside a much larger losing book. The
core is captured by four conditions (the "Golden Filter"), and it validated
profitably in EVERY time period independently (P1 +$253, P2 +$49, P3 +$29),
while everything it rejects was net -$203:

    strategy in {late_observed_no, peaker_cool_basket}
    days-to-resolution  >= 1.0        (NEVER same-day: same-day was -$86 / -0.87 avg)
    entry price          0.50 - 0.80  (<0.35 longshots and >0.80 favourites bleed)
    edge                 0.10 - 0.50  (edge > 0.50 is the CONFIDENCE TRAP: -$13.71 avg)
    grade               <= 0.90       (grade > 0.90 over-confident/mispriced)

What it does (two independent, fully fail-open jobs)
---------------------------------------------------
1. classify_golden(...)  -> is this incoming leg a Golden-Filter entry?
   When GOLDEN_NO_ENABLED is ON and a leg qualifies, dashboard._place() RE-TAGS
   the position's strategy to ``golden_no`` (a NEW, separately-named fusion
   strategy) and applies the golden size boost. The original strategy logic is
   NOT modified in any way -- this only skims the qualifying subset into a
   named, boosted strategy. Turn GOLDEN_NO_ENABLED off and behaviour is
   byte-for-byte identical to before (legs keep their original tag).

2. timing_allowed(...)   -> is this late_observed leg's days-to-resolution
   bucket switched ON? The user gets independent Telegram buttons for
   same-day / 1-day / 2-day / 3-day+ before resolution. Default: same-day OFF
   (the audit says it loses), 1/2/3+ ON. Also powers the scanner's
   "fetch only markets resolving after the next day" request via
   late_observed_should_fetch().

Everything reads Config LIVE (so Telegram toggles take effect next scan), every
public function is wrapped so any error FAILS OPEN (trade proceeds / leg kept),
and it costs only a couple of dict lookups + arithmetic.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Tuple

try:
    from config import Config
except Exception:  # pragma: no cover - offline import safety
    Config = None  # type: ignore


SETTING_DEFAULTS = {
    "GOLDEN_NO_ENABLED": True,
    "GOLDEN_MIN_DAYS_TO_RES": 1.0,
    "GOLDEN_ENTRY_MIN": 0.50,
    "GOLDEN_ENTRY_MAX": 0.80,
    "GOLDEN_EDGE_MIN": 0.10,
    "GOLDEN_EDGE_MAX": 0.50,
    "GOLDEN_GRADE_MAX": 0.90,
    "GOLDEN_NO_SIDE_ONLY": True,
    "GOLDEN_NO_SAMEDAY_ENABLED": False,
    "LATE_OBS_TIMING_SAMEDAY_ENABLED": False,
    "LATE_OBS_TIMING_1D_ENABLED": True,
    "LATE_OBS_TIMING_2D_ENABLED": True,
    "LATE_OBS_TIMING_3D_ENABLED": True,
    "LATE_OBS_FETCH_AFTER_NEXT_DAY": True,
}

_GOLDEN_SOURCE_STRATS = frozenset({"late_observed_no", "peaker_cool_basket"})
_LATE_OBS_STRATS = frozenset({"late_observed_no", "late_observed_yes"})

GOLDEN_TAG = "golden_no"


def ensure_defaults() -> None:
    if Config is None:
        return
    for key, default in SETTING_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _b(name, default):
    if Config is None:
        return default
    try:
        return bool(getattr(Config, name, default))
    except Exception:
        return default


def _f(name, default):
    if Config is None:
        return default
    try:
        return float(getattr(Config, name, default))
    except (TypeError, ValueError):
        return default


def days_to_resolution(resolution_time, now=None):
    """Fractional days between now and resolution. None when unknown (fail-open)."""
    if resolution_time is None:
        return None
    try:
        now = now or datetime.now(timezone.utc)
        rt = resolution_time
        if getattr(rt, "tzinfo", None) is None:
            rt = rt.replace(tzinfo=timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        return (rt - now).total_seconds() / 86400.0
    except Exception:
        return None


def _timing_bucket(days):
    if days < 1.0:
        return "sameday"
    if days < 2.0:
        return "1d"
    if days < 3.0:
        return "2d"
    return "3d"


def timing_allowed(strategy, resolution_time):
    """(ok, reason). Gates ONLY late_observed strategies by their days-to-
    resolution bucket toggle. Everything else / unknown timing fails OPEN."""
    if Config is None:
        return True, "ok"
    tag = (strategy or "").strip().lower()
    if tag not in _LATE_OBS_STRATS:
        return True, "ok"
    days = days_to_resolution(resolution_time)
    if days is None:
        return True, "ok (no resolution time)"
    bucket = _timing_bucket(days)
    toggle = {
        "sameday": "LATE_OBS_TIMING_SAMEDAY_ENABLED",
        "1d": "LATE_OBS_TIMING_1D_ENABLED",
        "2d": "LATE_OBS_TIMING_2D_ENABLED",
        "3d": "LATE_OBS_TIMING_3D_ENABLED",
    }[bucket]
    if not _b(toggle, True):
        return False, "{0} timing {1} ({2:.2f}d) is OFF".format(tag, bucket, days)
    return True, "ok"


def late_observed_should_fetch(strategy, resolution_time):
    """Scanner helper: should late_observed even look at this market? When
    LATE_OBS_FETCH_AFTER_NEXT_DAY is ON and same-day trading is OFF, skip
    same-day markets so no Open-Meteo request is spent on a market
    late_observed_no would never trade. Fail-open (True on any doubt)."""
    if Config is None:
        return True
    try:
        if not _b("LATE_OBS_FETCH_AFTER_NEXT_DAY", True):
            return True
        if _b("LATE_OBS_TIMING_SAMEDAY_ENABLED", False):
            return True
        days = days_to_resolution(resolution_time)
        if days is None:
            return True
        return days >= 1.0
    except Exception:
        return True


def _is_no_side(bucket_label, side_hint=""):
    if side_hint and side_hint.strip().upper() == "NO":
        return True
    bl = (bucket_label or "").strip().upper()
    return bl.startswith("NO ") or bl.startswith("NO:") or bl == "NO"


def classify_golden(strategy, entry_price, edge, grade, resolution_time,
                    bucket_label="", side_hint=""):
    """Does this incoming leg pass the Golden Filter? (is_golden, reason).
    Fail-CLOSED for promotion: any error/missing data => NOT golden, so the leg
    simply keeps its original strategy tag and nothing changes."""
    if Config is None or not _b("GOLDEN_NO_ENABLED", True):
        return False, "golden_no disabled"
    try:
        tag = (strategy or "").strip().lower()
        if tag not in _GOLDEN_SOURCE_STRATS:
            return False, "strategy not eligible"
        if _b("GOLDEN_NO_SIDE_ONLY", True) and tag == "late_observed_no":
            if not _is_no_side(bucket_label, side_hint):
                return False, "not NO-side"
        p = float(entry_price)
        if not (_f("GOLDEN_ENTRY_MIN", 0.50) <= p <= _f("GOLDEN_ENTRY_MAX", 0.80)):
            return False, "price {0:.2f} outside golden band".format(p)
        if edge is None:
            return False, "no edge"
        e = float(edge)
        if not (_f("GOLDEN_EDGE_MIN", 0.10) <= e <= _f("GOLDEN_EDGE_MAX", 0.50)):
            return False, "edge {0:+.2f} outside golden band (trap guard)".format(e)
        if grade is not None:
            try:
                if float(grade) > _f("GOLDEN_GRADE_MAX", 0.90):
                    return False, "grade {0:.2f} over max".format(float(grade))
            except (TypeError, ValueError):
                pass
        days = days_to_resolution(resolution_time)
        if days is None:
            return False, "no resolution time"
        if days < _f("GOLDEN_MIN_DAYS_TO_RES", 1.0) and not _b("GOLDEN_NO_SAMEDAY_ENABLED", False):
            return False, "same-day ({0:.2f}d) not golden".format(days)
        return True, "GOLDEN {0} p{1:.2f} e{2:+.2f} {3:.1f}d".format(tag, p, e, days)
    except Exception:
        return False, "golden classify error (fail-open, keep original)"


ensure_defaults()
