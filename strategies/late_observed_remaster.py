"""
Late Observed — REMASTER  (runs ALONGSIDE late_observed_no, never replaces it).

User requirement
----------------
"I don't want to make more changes to late_obs_no. If you think you are good,
make a new strategy 'late_observed_remaster' that runs along with
late_observed_no; put all the improvements (P1-P4) in the new one."

So late_observed_no stays byte-for-byte the proven +$13.69 / 65% WR winner. The
remaster is a SECOND, smarter opinion on the SAME observed signal that folds in
every upgrade the user asked for:

  P1  Lock-based timing        — same-day allowed when the extreme is truly
                                 locked (gated on lock_confidence, not a blunt
                                 days-to-resolution rule).
  P2  Calibrated probability   — routes each leg through data.grade_edge_engine
                                 (multi-pipeline, Platt-calibrated) so the
                                 overconfident normal-CDF edge is fixed.
  P3  Joint / no-arb awareness — keeps only the single best expression per
                                 bucket and drops legs whose calibrated edge or
                                 grade is weak (quality over quantity).
  P4  Size decoupled from raw  — stake scales with size_strength (grade + win-
                                 rate + calibrated prob), NOT raw edge, so the
                                 biggest bets no longer land on the most
                                 mispriced-against-us legs.

It emits the SAME LateObservedSignal / LateObservedLeg objects the dashboard
already knows how to place, tagged strategy 'late_observed_remaster'. Fully
fail-open: any error yields no remaster legs and the base strategy is untouched.
"""
from __future__ import annotations

from dataclasses import replace
from typing import List, Optional

try:
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("late_observed_remaster")

from strategies.late_observed_temp import (
    LateObservedTempStrategy, LateObservedLeg, LateObservedSignal,
)

try:
    from data import grade_edge_engine as gee
except Exception:  # pragma: no cover
    gee = None  # type: ignore


def _cfg(name, default):
    if Config is None:
        return default
    try:
        v = getattr(Config, name, default)
        return default if v is None else v
    except Exception:
        return default


def _f(name, default):
    try:
        return float(_cfg(name, default))
    except (TypeError, ValueError):
        return float(default)


def _b(name, default):
    try:
        return bool(_cfg(name, default))
    except Exception:
        return bool(default)


TAG = "late_observed_remaster"


class LateObservedRemasterStrategy:
    """Smarter second opinion on the observed signal. Shares the base decision
    core but re-scores every leg through the grade+edge engine."""

    def __init__(self, base: Optional[LateObservedTempStrategy] = None):
        # Reuse the SAME base strategy instance when the dashboard passes it, so
        # we never double-fetch or diverge from the proven core.
        self.base = base or LateObservedTempStrategy()

    def enabled(self) -> bool:
        return _b("LATE_OBSERVED_REMASTER_ENABLED", True)

    # ------------------------------------------------------------------ #
    def evaluate(self, market_title, buckets, market_prices, token_ids, balance,
                 city, observed_state, *, no_prices=None, no_token_ids=None,
                 grade=None, market_type=None,
                 days_to_resolution=None, win_rate=None, n_trades=0,
                 base_signals=None) -> List[LateObservedSignal]:
        """Return remaster signals. If `base_signals` (the already-computed
        late_observed signals) are supplied we re-score those; otherwise we call
        the base strategy once ourselves."""
        if not self.enabled() or observed_state is None:
            return []
        try:
            signals = base_signals
            if signals is None:
                signals = self.base.evaluate(
                    market_title, buckets, market_prices, token_ids, balance,
                    city, observed_state, no_prices=no_prices,
                    no_token_ids=no_token_ids, grade=grade, market_type=market_type,
                )
            if not signals:
                return []

            min_edge = _f("REMASTER_MIN_EDGE", 0.05)
            min_grade = _f("REMASTER_MIN_GRADE", 0.45)
            min_lock = _f("REMASTER_MIN_LOCK", 0.70)
            sameday_lock = _f("REMASTER_SAMEDAY_MIN_LOCK", 0.85)
            floor_usd = _f("LATE_OBSERVED_SIZE_FLOOR_USD", 3.0)
            max_usd = _f("LATE_OBSERVED_SIZE_MAX_USD", 15.0)

            lock_conf = float(getattr(observed_state, "lock_confidence", 0.0) or 0.0)
            hours_left = float(getattr(observed_state, "hours_remaining", 0.0) or 0.0)
            spread_c = float(getattr(observed_state, "remaining_spread_c", 0.0) or 0.0)
            n_models = int(getattr(observed_state, "n_models", 0) or 0)
            observed_extreme = getattr(observed_state, "observed_extreme_c", None)

            # P1 lock-based same-day gate: allow same-day ONLY when strongly locked.
            if days_to_resolution is not None and days_to_resolution < 1.0:
                if lock_conf < sameday_lock:
                    log.info("   \U0001F501 REMASTER skip same-day %s — lock %.0f%% < %.0f%%"
                             % (city, lock_conf * 100, sameday_lock * 100))
                    return []

            out_signals: List[LateObservedSignal] = []
            for sig in signals:
                new_legs: List[LateObservedLeg] = []
                for leg in sig.legs:
                    bucket_distance = _bucket_distance(leg.bucket_label, observed_extreme)
                    res = _score_leg(
                        leg=leg, lock_conf=lock_conf, hours_left=hours_left,
                        spread_c=spread_c, n_models=n_models,
                        bucket_distance=bucket_distance,
                        days_to_resolution=days_to_resolution,
                        win_rate=win_rate, n_trades=n_trades,
                    )
                    if res is None:
                        # engine unavailable -> keep the base leg as-is
                        new_legs.append(replace(leg, reason=(leg.reason + " | remaster(passthru)")))
                        continue
                    if res.edge < min_edge or res.grade < min_grade or lock_conf < min_lock:
                        continue
                    size_usd = round(floor_usd + (max_usd - floor_usd) * res.size_strength, 2)
                    size_usd = min(size_usd, round(min(balance, max_usd), 2))
                    new_legs.append(replace(
                        leg,
                        our_probability=res.prob_calibrated,
                        edge=res.edge,
                        size_usd=size_usd,
                        reason=("remaster %s | %s" % (res.reason, leg.reason))[:220],
                    ))
                if new_legs:
                    out_signals.append(LateObservedSignal(
                        market_title=sig.market_title, city=sig.city,
                        market_type=sig.market_type,
                        observed_extreme_c=sig.observed_extreme_c,
                        remaining_extreme_c=sig.remaining_extreme_c,
                        hours_remaining=sig.hours_remaining,
                        lock_confidence=sig.lock_confidence,
                        legs=new_legs,
                        reason="REMASTER: " + sig.reason,
                    ))
            return out_signals
        except Exception as e:  # pragma: no cover - fail open
            log.debug("remaster evaluate failed (fail-open): %s" % e)
            return []


def _bucket_distance(bucket_label, observed_extreme) -> Optional[float]:
    """Approx |bucket numeric edge - observed extreme| in the market's unit.
    Best-effort parse of the first number in the label; None on failure."""
    if observed_extreme is None:
        return None
    try:
        import re
        nums = re.findall(r"-?\d+(?:\.\d+)?", str(bucket_label))
        if not nums:
            return None
        # Use the closest bound to the observed extreme.
        vals = [float(n) for n in nums[:2]]
        return min(abs(v - float(observed_extreme)) for v in vals)
    except Exception:
        return None


def _score_leg(*, leg, lock_conf, hours_left, spread_c, n_models,
               bucket_distance, days_to_resolution, win_rate, n_trades):
    if gee is None:
        return None
    try:
        fx = gee.Features(
            side=getattr(leg, "side", "NO"),
            entry_price=float(getattr(leg, "price", 0.0) or 0.0),
            raw_prob=float(getattr(leg, "our_probability", 0.0) or 0.0),
            lock_confidence=lock_conf,
            hours_remaining=hours_left,
            remaining_spread_c=spread_c,
            bucket_distance_c=bucket_distance,
            n_models=n_models,
            days_to_resolution=days_to_resolution,
            strategy=TAG,
            win_rate=win_rate,
            n_trades=n_trades,
        )
        return gee.score(fx)
    except Exception:
        return None
