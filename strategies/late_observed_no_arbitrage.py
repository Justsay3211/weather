"""
Late Observed NO-Arbitrage  (new strategy, runs alongside the others).

User requirement: "a new strategy like 'late observed no arbitrage' if other NO
positions combined or high chance winning like that."

The insight
-----------
A weather market is a set of MUTUALLY-EXCLUSIVE temperature buckets. Exactly ONE
bucket wins; every other bucket's NO settles at $1. So buying NO on N of the M
buckets WINS on (M-1) of them and only loses on the single bucket that settles
YES. Once the observed extreme is locked, the physically-impossible buckets are
near-certain NO wins. This builds a NO BASKET over the ruled-out buckets and
enters only when the basket is a structural near-arb (high combined calibrated
P(NO) that clears fees). Each leg is scored through data.grade_edge_engine.
Emits LateObservedSignal/Leg tagged 'late_observed_no_arb'. Fully fail-open.
"""
from __future__ import annotations

import re
from typing import List, Optional, Sequence

try:
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("late_observed_no_arb")

from strategies.late_observed_temp import LateObservedLeg, LateObservedSignal

try:
    from data import grade_edge_engine as gee
except Exception:  # pragma: no cover
    gee = None  # type: ignore

TAG = "late_observed_no_arb"


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


def _i(name, default):
    try:
        return int(_cfg(name, default))
    except (TypeError, ValueError):
        return int(default)


def _b(name, default):
    try:
        return bool(_cfg(name, default))
    except Exception:
        return bool(default)


def _bucket_center(label) -> Optional[float]:
    try:
        nums = re.findall(r"-?\d+(?:\.\d+)?", str(label))
        if not nums:
            return None
        vals = [float(n) for n in nums[:2]]
        return sum(vals) / len(vals)
    except Exception:
        return None


class LateObservedNoArbitrageStrategy:
    """Build a near-arb NO basket over the ruled-out buckets of a locked market."""

    def enabled(self) -> bool:
        return _b("LATE_OBS_NO_ARB_ENABLED", True)

    def evaluate(self, market_title, buckets, balance, city, observed_state, *,
                 no_prices: Optional[Sequence[float]] = None,
                 no_token_ids: Optional[Sequence[str]] = None,
                 grade=None, market_type=None,
                 days_to_resolution=None, win_rate=None, n_trades=0
                 ) -> List[LateObservedSignal]:
        if not self.enabled() or observed_state is None or gee is None:
            return []
        try:
            lock_conf = float(getattr(observed_state, "lock_confidence", 0.0) or 0.0)
            if lock_conf < _f("LATE_OBS_NO_ARB_MIN_LOCK", 0.75):
                return []
            observed_extreme = getattr(observed_state, "observed_extreme_c", None)
            hours_left = float(getattr(observed_state, "hours_remaining", 0.0) or 0.0)
            spread_c = float(getattr(observed_state, "remaining_spread_c", 0.0) or 0.0)
            n_models = int(getattr(observed_state, "n_models", 0) or 0)
            mode = "low" if "low" in str(market_type or "").lower() else "high"

            no_prices = list(no_prices or [])
            no_token_ids = list(no_token_ids or [])
            buckets = list(buckets or [])
            min_price = _f("LATE_OBS_NO_ARB_MIN_PRICE", 0.04)
            max_price = _f("LATE_OBS_NO_ARB_MAX_PRICE", 0.97)
            min_legs = _i("LATE_OBS_NO_ARB_MIN_LEGS", 3)
            max_legs = _i("LATE_OBS_NO_ARB_MAX_LEGS", 6)
            min_grade = _f("LATE_OBS_NO_ARB_MIN_GRADE", 0.50)

            candidates = []
            for i, bk in enumerate(buckets):
                label = bk if isinstance(bk, str) else getattr(bk, "label", str(bk))
                price = no_prices[i] if i < len(no_prices) else None
                token = no_token_ids[i] if i < len(no_token_ids) else ""
                if price is None or not token:
                    continue
                if not (min_price <= float(price) <= max_price):
                    continue
                center = _bucket_center(label)
                # Only buckets RULED OUT by the lock (the near-certain NOs).
                if center is not None and observed_extreme is not None:
                    if mode == "high" and center <= float(observed_extreme):
                        continue
                    if mode == "low" and center >= float(observed_extreme):
                        continue
                dist = None if (center is None or observed_extreme is None) else abs(center - float(observed_extreme))
                res = gee.score(gee.Features(
                    side="NO", entry_price=float(price), raw_prob=float(price),
                    lock_confidence=lock_conf, hours_remaining=hours_left,
                    remaining_spread_c=spread_c, bucket_distance_c=dist,
                    n_models=n_models, days_to_resolution=days_to_resolution,
                    strategy=TAG, win_rate=win_rate, n_trades=n_trades,
                ))
                if res.grade < min_grade:
                    continue
                candidates.append((label, token, float(price), res, dist))

            candidates.sort(key=lambda c: (c[3].prob_calibrated, (c[4] or 0.0)), reverse=True)
            chosen = candidates[:max_legs]
            if len(chosen) < min_legs:
                return []

            k = len(chosen)
            avg_no_prob = sum(c[3].prob_calibrated for c in chosen) / k
            if avg_no_prob < _f("LATE_OBS_NO_ARB_MIN_AVG_PROB", 0.80):
                return []

            basket_max = min(round(balance, 2), _f("LATE_OBS_NO_ARB_MAX_BASKET_USD", 15.0))
            per_leg = max(_f("MIN_ORDER_SIZE", 1.0), round(basket_max / k, 2))

            legs: List[LateObservedLeg] = []
            for label, token, price, res, dist in chosen:
                legs.append(LateObservedLeg(
                    bucket_label=label, side="NO", token_id=token, price=price,
                    our_probability=res.prob_calibrated, edge=res.edge,
                    ev_per_contract=res.edge, size_usd=per_leg,
                    reason="no-arb leg %s" % res.reason,
                ))
            log.info("   \U0001F517 NO-ARB %s: %d-leg NO basket avg P=%.0f%% (lock %.0f%%)"
                     % (city, k, avg_no_prob * 100, lock_conf * 100))
            return [LateObservedSignal(
                market_title=market_title, city=city,
                market_type=str(market_type or ""),
                observed_extreme_c=float(observed_extreme or 0.0),
                remaining_extreme_c=getattr(observed_state, "remaining_extreme_c", None),
                hours_remaining=int(hours_left),
                lock_confidence=lock_conf, legs=legs,
                reason="NO-arb %d-leg basket, avg P(NO)=%.0f%%" % (k, avg_no_prob * 100),
            )]
        except Exception as e:  # pragma: no cover - fail open
            log.debug("no-arb evaluate failed (fail-open): %s" % e)
            return []
