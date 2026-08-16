"""
Observed-temperature → bucket-probability math (dependency-free).

Core edge of the overhauled bot
-------------------------------
For a *daily high* market, the final settled high ``H`` can only ever be
**≥ the maximum temperature already observed so far today**. Once the local day
is far enough along that the peak heating hours have passed, the observed
max-so-far effectively *locks* the answer, while the order book often still
prices stale forecast uncertainty. That gap is the trade.

This module turns three observed quantities into a probability distribution
over temperature buckets:

* ``observed_max_c``        — highest temp seen so far today (a hard floor on H)
* ``remaining_max_c``       — max forecast temp over the *remaining* hours
                              (``None`` when no daylight/hours remain)
* ``remaining_spread_c``    — forecast uncertainty (≈ std) on that remainder

It is pure math (only ``math`` from the stdlib), so it imports and unit-tests
cleanly offline. The same machinery handles *lowest temperature* markets by
passing ``mode="low"`` (a floor on the observed-min becomes a ceiling).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

NEG_INF = float("-inf")
POS_INF = float("inf")

# A small irreducible measurement/station uncertainty so we never claim a
# perfectly degenerate 0/1 distribution even when no hours remain.
_MIN_SIGMA = 0.35

Bucket = Tuple[str, float, float]  # (label, low_c, high_c)


def _normal_cdf(x: float, mu: float, sigma: float) -> float:
    """Standard normal CDF of ``x`` under N(mu, sigma) via ``math.erf``."""
    if sigma <= 0:
        return 1.0 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def _prob_remaining_between(lo: float, hi: float, mu: float, sigma: float) -> float:
    """P(lo <= future_peak < hi) under N(mu, sigma), clamped to [0, 1]."""
    p = _normal_cdf(hi, mu, sigma) - _normal_cdf(lo, mu, sigma)
    return max(0.0, min(1.0, p))


def observed_bucket_probabilities(
    observed_extreme_c: float,
    remaining_extreme_c: Optional[float],
    remaining_spread_c: float,
    buckets: Sequence[Bucket],
    mode: str = "high",
) -> Dict[str, float]:
    """Return ``{label: probability}`` for the final daily extreme.

    ``mode="high"`` (default) treats ``observed_extreme_c`` as a hard floor on
    the final value (daily high). ``mode="low"`` mirrors the logic so the
    observed minimum becomes a hard ceiling on the final value (daily low).

    The returned probabilities are normalized to sum to 1 across the supplied
    buckets (when any mass is assignable).
    """
    if mode not in ("high", "low"):
        raise ValueError("mode must be 'high' or 'low'")

    # Reduce the "low" case to the "high" case by negating the temperature axis.
    if mode == "low":
        neg_buckets = [(lbl, -hi, -lo) for (lbl, lo, hi) in buckets]
        obs = -observed_extreme_c
        rem = None if remaining_extreme_c is None else -remaining_extreme_c
        probs = observed_bucket_probabilities(obs, rem, remaining_spread_c, neg_buckets, mode="high")
        # Map labels straight back (labels are preserved through negation).
        return probs

    sigma = max(_MIN_SIGMA, float(remaining_spread_c or 0.0))
    has_remaining = remaining_extreme_c is not None
    # Expected final high: the larger of what we've locked in and what's left.
    mu = observed_extreme_c if not has_remaining else max(observed_extreme_c, float(remaining_extreme_c))

    raw: Dict[str, float] = {}
    for label, lo, hi in buckets:
        # Bucket entirely below the locked floor -> impossible.
        if hi <= observed_extreme_c:
            raw[label] = 0.0
            continue

        if lo <= observed_extreme_c < hi:
            # The bucket CONTAINS the locked floor. It wins unless a remaining
            # hour pushes the high to/above ``hi``.
            if not has_remaining:
                raw[label] = 1.0
            else:
                # P(future peak < hi) given the floor; small sigma => near 1
                # when the forecast remainder is clearly below hi.
                raw[label] = max(0.0, min(1.0, _normal_cdf(hi, mu, sigma)))
            continue

        # Bucket lies entirely ABOVE the locked floor.
        if not has_remaining:
            raw[label] = 0.0
            continue
        if math.isinf(hi):
            # Open-topped "X or higher" bucket.
            raw[label] = max(0.0, min(1.0, 1.0 - _normal_cdf(lo, mu, sigma)))
        else:
            raw[label] = _prob_remaining_between(lo, hi, mu, sigma)

    total = sum(raw.values())
    if total <= 0:
        return {label: 0.0 for label, _, _ in buckets}
    return {label: val / total for label, val in raw.items()}


def expected_final_extreme(
    observed_extreme_c: float,
    remaining_extreme_c: Optional[float],
    mode: str = "high",
) -> float:
    """Expected final daily extreme given observed + remaining forecast."""
    if remaining_extreme_c is None:
        return observed_extreme_c
    if mode == "low":
        return min(observed_extreme_c, float(remaining_extreme_c))
    return max(observed_extreme_c, float(remaining_extreme_c))


def lock_confidence(
    observed_extreme_c: float,
    remaining_extreme_c: Optional[float],
    remaining_spread_c: float,
    mode: str = "high",
) -> float:
    """A 0..1 score for how "locked" the daily extreme already is.

    1.0 means no hours remain (fully locked). Otherwise it decays with how far
    the remaining forecast could still move the extreme, scaled by spread.
    """
    if remaining_extreme_c is None:
        return 1.0
    sigma = max(_MIN_SIGMA, float(remaining_spread_c or 0.0))
    if mode == "low":
        gap = observed_extreme_c - float(remaining_extreme_c)  # positive => locked
    else:
        gap = float(remaining_extreme_c) - observed_extreme_c  # negative => locked
        gap = -gap
    # gap > 0 means the remaining forecast cannot beat the observed extreme.
    # Convert to a probability-like confidence via the normal CDF.
    return max(0.0, min(1.0, _normal_cdf(gap, 0.0, sigma)))
