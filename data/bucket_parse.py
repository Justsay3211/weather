r"""
Robust temperature-bucket label parser (dependency-free).

Why this module exists
----------------------
Polymarket weather outcome labels arrive in many noisy shapes:

    "Will the high temp be 24°C on May 29?"
    "...be 38°C or higher on..."
    "...be 17°C or below..."
    "...be between 80-81°F on..."
    "...be 71°F or below..."

Real API payloads are frequently *mojibaked* (UTF-8 degree sign decoded as
Latin-1) so the degree glyph shows up as ``Â°`` or the unit letter is
missing entirely. The old scanner regex required a literal ``°c``/``°f`` and
silently fell through to a naive ``\d+`` grab, which mis-bounded a large
fraction of buckets. This module normalizes the text first, then extracts
bounds with direction-aware logic.

All bounds are returned in **Celsius** as ``(low, high)`` half-open-ish bands.
The module imports nothing outside the stdlib (``re``, ``math``) so it is fully
importable and unit-testable in an offline sandbox.
"""

from __future__ import annotations

import re
import math
from typing import Optional, Tuple

NEG_INF = float("-inf")
POS_INF = float("inf")

# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------

# Common mojibake / HTML-entity spellings of the degree sign.
_DEGREE_VARIANTS = (
    "ÃÂ°",  # double-encoded
    "Â°",              # UTF-8 ° misread as Latin-1
    "â°",        # stray triple-byte artifact
    "&deg;",
    "&#176;",
    "&#xb0;",
    "º",                    # masculine ordinal often used as degree
    "⁰",                    # superscript zero sometimes substituted
)

# Unicode dashes that should all collapse to a plain hyphen.
_DASH_VARIANTS = ("‒", "–", "—", "―", "−", "‐", "‑")


def normalize_degrees(text: str) -> str:
    """Return ``text`` with degree mojibake, dashes and whitespace normalized.

    The result is lower-cased, every known degree spelling becomes a single
    ``°`` glyph, every unicode dash becomes ``-`` and runs of whitespace are
    collapsed. This is deliberately lossy/aggressive — the goal is to make the
    downstream regexes simple and tolerant.
    """
    if not text:
        return ""
    s = str(text)
    for variant in _DEGREE_VARIANTS:
        s = s.replace(variant, "°")
    for d in _DASH_VARIANTS:
        s = s.replace(d, "-")
    s = s.lower()
    # Drop a stray 'deg'/'degree(s)' word into the degree glyph so number
    # detection below is uniform.
    s = re.sub(r"\s*degrees?\b", "°", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _to_celsius(value: float, unit: str) -> float:
    if unit == "f":
        return (value - 32.0) * 5.0 / 9.0
    return value


def _detect_unit(norm_text: str, default_unit: str) -> str:
    """Best-effort temperature unit detection from already-normalized text."""
    # Prefer a unit letter that sits next to a degree glyph or a number.
    m = re.search(r"°\s*([cf])\b", norm_text)
    if m:
        return m.group(1)
    m = re.search(r"(\d)\s*°?\s*([cf])\b", norm_text)
    if m:
        return m.group(2)
    if "fahrenheit" in norm_text:
        return "f"
    if "celsius" in norm_text or "centigrade" in norm_text:
        return "c"
    return (default_unit or "c").lower()


# Direction keywords -------------------------------------------------------
_HIGHER_WORDS = ("or higher", "or above", "or more", "or over", "or warmer",
                 "and higher", "and above", "at least", "greater than",
                 "or hotter", "or greater")
_LOWER_WORDS = ("or lower", "or below", "or less", "or under", "or colder",
                "and lower", "and below", "at most", "less than", "or cooler")


def _has_any(text: str, words) -> bool:
    return any(w in text for w in words)


def parse_bucket_bounds(text: str, temp_unit: str = "C") -> Tuple[float, float]:
    """Parse a weather outcome label into ``(low_c, high_c)`` Celsius bounds.

    Resolution rules (in priority order):

    * **Range** — ``"between 80-81°f"`` / ``"80 to 81°f"`` →
      ``(lo-0.5, hi+0.5)`` (each integer label is its own ±1°/2 band).
    * **Open-ended up** — any "or higher"-style phrase → ``(t-0.5, +inf)``.
    * **Open-ended down** — any "or lower"-style phrase → ``(-inf, t+0.5)``.
    * **Trailing +/-** — ``"38°c+"`` → up-open, ``"17°c-"`` → down-open.
    * **Exact** — a single temperature → ``(t-0.5, t+0.5)``.
    * **Fallback** — unparseable → ``(-inf, +inf)`` (caller should treat as
      unusable rather than tradeable).

    Importantly, direction detection no longer requires a ``°`` glyph or unit
    letter to be present, which is what made the original parser brittle.
    """
    norm = normalize_degrees(text)
    if not norm:
        return (NEG_INF, POS_INF)

    unit = _detect_unit(norm, temp_unit)

    # 1) Range: "between X-Y", "X-Y", "X to Y" (unit/degree optional).
    range_match = (
        re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*[cf]?\s*(?:-|to)\s*(-?\d+(?:\.\d+)?)\s*°?\s*[cf]?", norm)
    )
    # Guard: a leading "or lower"/"or higher" sentence can contain a stray dash;
    # only treat as a range when two distinct numbers are captured AND no
    # open-ended keyword dominates.
    if range_match and not (_has_any(norm, _HIGHER_WORDS) or _has_any(norm, _LOWER_WORDS)):
        lo = float(range_match.group(1))
        hi = float(range_match.group(2))
        if hi < lo:
            lo, hi = hi, lo
        lo_c = _to_celsius(lo - 0.5, unit)
        hi_c = _to_celsius(hi + 0.5, unit)
        return (lo_c, hi_c)

    # Find the primary temperature number (first signed integer/decimal that is
    # adjacent to a degree glyph or unit, else the first number at all).
    num = None
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*°", norm)
    if not m:
        m = re.search(r"(-?\d+(?:\.\d+)?)\s*[cf]\b", norm)
    if not m:
        m = re.search(r"(-?\d+(?:\.\d+)?)", norm)
    if m:
        num = float(m.group(1))

    if num is None:
        return (NEG_INF, POS_INF)

    # 2) Direction keywords / trailing sign.
    up = _has_any(norm, _HIGHER_WORDS) or bool(re.search(r"\d\s*°?\s*[cf]?\s*\+", norm))
    down = _has_any(norm, _LOWER_WORDS) or bool(re.search(r"\d\s*°?\s*[cf]?\s*-\s*(?:$|[^0-9])", norm))

    if up and not down:
        return (_to_celsius(num - 0.5, unit), POS_INF)
    if down and not up:
        return (NEG_INF, _to_celsius(num + 0.5, unit))

    # 3) Exact single bucket.
    return (_to_celsius(num - 0.5, unit), _to_celsius(num + 0.5, unit))


def is_bounded(bounds: Tuple[float, float]) -> bool:
    """True when at least one side is finite (i.e. the parse produced signal)."""
    lo, hi = bounds
    return not (math.isinf(lo) and math.isinf(hi))


def bucket_contains(bounds: Tuple[float, float], temp_c: float) -> bool:
    """Whether a Celsius temperature falls inside ``[low, high)`` style bounds."""
    lo, hi = bounds
    return lo <= temp_c < hi
