"""
Grade + Edge Engine  —  the separate, multi-pipeline "compute app" that decides
HOW GOOD a trade is (grade) and HOW MISPRICED it is (edge).

Why this exists
---------------
The old edge was a single overconfident normal-CDF probability minus price, and
the old grade was a single stability score. Both were "one simple broken
number": edge was inverted (biggest bets on the most mispriced-against-us legs)
and size scaled UP with raw edge. This module replaces that with a SMART,
multi-parameter, multi-pipeline scorer modeled after a gradient-boosted tree
(XGBoost-style) ensemble: many small, independently-auditable decision stumps,
each voting a bounded margin, blended and then CALIBRATED so the output
probability is realistic (fixes the overconfidence that produced phantom edge).

It is intentionally a standalone engine (its own file / "app") so it can be:
  * unit-tested offline (pure-python, stdlib + optional numpy),
  * reused by every strategy (late_observed_no, remaster, no-arb, golden),
  * swapped for a real trained model later (same GradeEdgeResult contract),
  * blended with the live ML engine when one is configured (ml helps).

Pipelines (each returns a bounded sub-score in logit space)
----------------------------------------------------------
  1. LOCK pipeline      — observed-extreme lock strength, hours remaining,
                          cross-model remaining spread, distance of the bucket
                          from the locked extreme (the physical core edge).
  2. STRUCTURE pipeline — entry price prior (favourites vs longshots), edge band
                          (the audited 0.10-0.50 sweet spot, >0.50 = trap),
                          days-to-resolution.
  3. LIQUIDITY pipeline — spread, bid depth, thin-book penalty (a great thesis
                          you cannot exit is not a great trade).
  4. HISTORY pipeline   — realized per-strategy win-rate (Bayesian-shrunk).
  5. ML pipeline        — optional live ML confidence blend (fail-open no-op).

Output
------
GradeEdgeResult:
  prob_calibrated  — realistic P(win) after Platt calibration (fixes overconf).
  edge             — prob_calibrated - fee-adjusted breakeven (post-fee).
  grade            — 0..1 quality score (gate + a MILD size lever).
  size_strength    — 0..1 decoupled-from-raw-edge strength for sizing.
  confidence       — how much data backed the score.
  components       — per-pipeline contribution (for logs / weather_trace).

Everything is fail-open: any error returns a conservative neutral result so the
trading loop never breaks because the scorer had a bad day.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("grade_edge_engine")

try:
    from data import fees as _fees  # type: ignore
except Exception:  # pragma: no cover
    _fees = None  # type: ignore


# --------------------------------------------------------------------------- #
# Config helpers (read LIVE so Telegram toggles take effect next scan)
# --------------------------------------------------------------------------- #
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


def enabled() -> bool:
    return _b("GRADE_EDGE_ENGINE_ENABLED", True)


def _clip(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def _sigmoid(z):
    if z >= 0:
        ez = math.exp(-z)
        return 1.0 / (1.0 + ez)
    ez = math.exp(z)
    return ez / (1.0 + ez)


def _logit(p):
    p = _clip(p, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


# --------------------------------------------------------------------------- #
# Inputs / outputs
# --------------------------------------------------------------------------- #
@dataclass
class Features:
    # side / pricing
    side: str = "NO"                    # 'YES' | 'NO'
    entry_price: float = 0.0           # price of the token we BUY
    raw_prob: float = 0.0              # model P(win) BEFORE calibration (0..1)
    # lock pipeline
    lock_confidence: float = 0.0       # 0..1 observed-extreme lock strength
    hours_remaining: float = 0.0       # hours of heating/cooling left today
    remaining_spread_c: float = 0.0    # cross-model stdev of remaining extreme (C)
    bucket_distance_c: Optional[float] = None  # |bucket edge - locked extreme| C (bigger = safer NO)
    n_models: int = 0                  # forecast members that had data
    # structure pipeline
    days_to_resolution: Optional[float] = None
    # liquidity pipeline
    spread: Optional[float] = None     # best_ask - best_bid (absolute)
    bid_depth_usd: Optional[float] = None
    thin_book: bool = False
    # history pipeline
    strategy: str = ""
    win_rate: Optional[float] = None   # realized WR for this strategy (0..1)
    n_trades: int = 0
    # ml pipeline
    ml_prob: Optional[float] = None    # live ML P(win) if available
    ml_confidence: Optional[float] = None


@dataclass
class GradeEdgeResult:
    prob_calibrated: float = 0.0
    edge: float = 0.0
    grade: float = 0.0
    size_strength: float = 0.0
    confidence: float = 0.0
    breakeven: float = 0.0
    components: Dict[str, float] = field(default_factory=dict)
    pipeline: str = "grade_edge_engine"
    reason: str = ""


# --------------------------------------------------------------------------- #
# Fee-aware breakeven (post-fee price the token must clear to be EV+)
# --------------------------------------------------------------------------- #
def _breakeven(entry_price: float) -> float:
    p = _clip(entry_price, 1e-4, 0.9999)
    try:
        if _fees is not None and hasattr(_fees, "breakeven_probability"):
            return float(_fees.breakeven_probability(p))
    except Exception:
        pass
    # Fallback: taker fee = fee_rate * p * (1-p) charged on the winning payout.
    if _b("ASSUME_TAKER_FILLS", True):
        fee_rate = _f("TAKER_FEE_RATE", 0.05)
    else:
        fee_rate = _f("MAKER_FEE_RATE", 0.0)
    fee = fee_rate * p * (1.0 - p)
    return _clip(p + fee, 0.0, 1.0)


# --------------------------------------------------------------------------- #
# Boosted-stump pipelines. Each appends bounded logit votes + a weight.
# --------------------------------------------------------------------------- #
def _lock_votes(fx: Features):
    """Physical-core pipeline: the observed extreme is a hard floor/ceiling."""
    votes = []
    lc = _clip(fx.lock_confidence, 0.0, 1.0)
    # Stump: lock confidence. Below min-lock is strongly negative; strong lock
    # is strongly positive. Centered on the trade threshold.
    min_lock = _f("LATE_OBSERVED_MIN_LOCK", 0.70)
    votes.append(("lock_conf", _clip((lc - min_lock) / 0.20, -1.5, 1.8)))
    # Stump: hours remaining. Fewer hours of heating left = more locked.
    hr = fx.hours_remaining or 0.0
    if hr <= 1.0:
        votes.append(("hours_left", 0.9))
    elif hr <= 3.0:
        votes.append(("hours_left", 0.4))
    elif hr >= 8.0:
        votes.append(("hours_left", -0.8))
    else:
        votes.append(("hours_left", 0.0))
    # Stump: cross-model spread of the REMAINING extreme. Tight = trustworthy.
    sp = fx.remaining_spread_c or 0.0
    votes.append(("model_spread", _clip((1.2 - sp) / 1.2, -1.0, 0.8)))
    # Stump: how far the bucket sits from the locked extreme (NO safety margin).
    if fx.bucket_distance_c is not None:
        d = fx.bucket_distance_c
        if fx.side.upper() == "NO":
            votes.append(("bucket_gap", _clip((d - 0.5) / 2.0, -1.2, 1.6)))
        else:  # YES wants the bucket to CONTAIN the locked extreme (small gap)
            votes.append(("bucket_gap", _clip((1.0 - d) / 1.0, -1.2, 1.2)))
    # Stump: forecast breadth (more members with data = more trustworthy).
    if fx.n_models >= 4:
        votes.append(("breadth", 0.4))
    elif fx.n_models <= 1:
        votes.append(("breadth", -0.5))
    return votes


def _structure_votes(fx: Features):
    """Market-structure pipeline: price prior + edge-band + timing."""
    votes = []
    p = _clip(fx.entry_price, 0.0, 1.0)
    # Stump: price prior. The audit found the 0.50-0.80 band durably profitable;
    # <0.35 longshots and >0.85 favourites bleed. Favor the sweet band.
    if 0.50 <= p <= 0.80:
        votes.append(("price_prior", 0.7))
    elif p < 0.30:
        votes.append(("price_prior", -0.7))
    elif p > 0.88:
        votes.append(("price_prior", -0.4))
    else:
        votes.append(("price_prior", 0.1))
    # Stump: raw edge band. 0.10-0.50 is the sweet spot; edge > 0.50 is the
    # CONFIDENCE TRAP (audit: -$13.71 avg) -> penalize, do NOT reward.
    raw_edge = _clip(fx.raw_prob - _breakeven(p), -1.0, 1.0)
    if 0.10 <= raw_edge <= 0.50:
        votes.append(("edge_band", 0.6))
    elif raw_edge > 0.50:
        votes.append(("edge_band", -0.9))          # trap guard (KEY inversion fix)
    elif raw_edge < 0.0:
        votes.append(("edge_band", -1.2))
    else:
        votes.append(("edge_band", 0.0))
    # Stump: days-to-resolution. same-day is allowed now (user enables) but only
    # rewarded when the lock is strong; multi-day gets a mild prior.
    d = fx.days_to_resolution
    if d is not None:
        if d < 1.0:
            votes.append(("timing", 0.2 if fx.lock_confidence >= 0.80 else -0.3))
        elif d <= 3.0:
            votes.append(("timing", 0.3))
        else:
            votes.append(("timing", -0.1))
    return votes


def _liquidity_votes(fx: Features):
    votes = []
    if fx.thin_book:
        votes.append(("thin_book", -1.4))
    if fx.spread is not None:
        if fx.spread <= 0.02:
            votes.append(("spread", 0.3))
        elif fx.spread >= 0.06:
            votes.append(("spread", -0.6))
    if fx.bid_depth_usd is not None:
        if fx.bid_depth_usd >= 10.0:
            votes.append(("depth", 0.3))
        elif fx.bid_depth_usd < 3.0:
            votes.append(("depth", -0.5))
    return votes


def _history_votes(fx: Features):
    votes = []
    if fx.win_rate is not None:
        prior = _f("KELLY_WINRATE_PRIOR", 0.45)
        full_n = max(1, int(_f("KELLY_WINRATE_FULL_TRUST_N", 20)))
        n = max(0, int(fx.n_trades or 0))
        # Bayesian shrink toward the prior until enough trades observed.
        shrunk = (fx.win_rate * n + prior * full_n) / (n + full_n)
        votes.append(("win_rate", _clip((shrunk - 0.50) / 0.20, -1.2, 1.2)))
    return votes


def _ml_votes(fx: Features):
    votes = []
    if fx.ml_prob is not None:
        conf = fx.ml_confidence if fx.ml_confidence is not None else 0.5
        w = _clip(conf, 0.0, 1.0)
        votes.append(("ml", _clip(_logit(fx.ml_prob) * 0.5 * w, -1.5, 1.5)))
    return votes


# --------------------------------------------------------------------------- #
# Main scorer
# --------------------------------------------------------------------------- #
_PIPELINE_WEIGHT = {
    "lock": 1.0,
    "structure": 0.8,
    "liquidity": 0.5,
    "history": 0.6,
    "ml": 0.7,
}


def score(fx: Features) -> GradeEdgeResult:
    """Run all pipelines, blend in logit space, calibrate, and derive
    edge / grade / size_strength. Fully fail-open."""
    be = _breakeven(fx.entry_price)
    try:
        if not enabled():
            # Neutral passthrough using the raw model probability.
            prob = _clip(fx.raw_prob, 0.0, 1.0)
            return GradeEdgeResult(
                prob_calibrated=prob, edge=prob - be, grade=0.5,
                size_strength=_clip((prob - be) / 0.25, 0.0, 1.0),
                confidence=0.3, breakeven=be, pipeline="disabled",
                reason="engine disabled (raw passthrough)")

        pipes = {
            "lock": _lock_votes(fx),
            "structure": _structure_votes(fx),
            "liquidity": _liquidity_votes(fx),
            "history": _history_votes(fx),
            "ml": _ml_votes(fx),
        }
        components: Dict[str, float] = {}
        # Anchor on the raw model logit, then add weighted pipeline votes. Each
        # pipeline vote-sum is squashed so no single pipeline dominates.
        z = _logit(fx.raw_prob) if fx.raw_prob > 0 else 0.0
        z *= _f("GRADE_EDGE_RAW_ANCHOR_W", 0.6)
        n_signals = 0
        for name, votes in pipes.items():
            if not votes:
                continue
            s = sum(v for _, v in votes)
            s = _clip(s, -3.0, 3.0)
            contrib = _PIPELINE_WEIGHT.get(name, 0.5) * s
            z += contrib
            components[name] = round(contrib, 4)
            n_signals += len(votes)

        # --- CALIBRATION (Platt): temperature-scale the logit so the output is
        # realistic and NOT overconfident (the core phantom-edge fix). T>1 pulls
        # probabilities toward 0.5; a small bias corrects systematic optimism.
        T = max(0.5, _f("GRADE_EDGE_CALIB_TEMPERATURE", 1.6))
        bias = _f("GRADE_EDGE_CALIB_BIAS", -0.15)
        prob = _sigmoid(z / T + bias)
        # REALISTIC CEILING: nothing in a weather market is a certainty. Cap the
        # calibrated probability at GRADE_EDGE_MAX_PROB (default 0.97) and floor
        # symmetrically so the engine can NEVER reproduce the old normal-CDF
        # ~0.999 overconfidence that drove phantom edge + oversizing.
        cap = _clip(_f("GRADE_EDGE_MAX_PROB", 0.97), 0.5, 0.999)
        prob = _clip(prob, 1.0 - cap, cap)

        edge = prob - be
        # confidence grows with how many independent signals fired + lock.
        confidence = _clip(0.25 + 0.05 * n_signals + 0.3 * _clip(fx.lock_confidence, 0, 1), 0.0, 1.0)

        # GRADE: quality 0..1. Blends calibrated prob margin, lock, model
        # agreement, and liquidity. Independent of raw edge magnitude.
        spread_term = 1.0 - _clip((fx.remaining_spread_c or 0.0) / 2.0, 0.0, 1.0)
        liq_term = 0.0 if fx.thin_book else 1.0
        grade = _clip(
            0.35 * _clip(fx.lock_confidence, 0, 1)
            + 0.25 * _clip((edge + 0.10) / 0.35, 0, 1)   # mild, saturates fast
            + 0.20 * spread_term
            + 0.20 * liq_term,
            0.0, 1.0,
        )

        # SIZE STRENGTH: DECOUPLED from raw edge (old bug: size scaled up with
        # overconfident edge). Driven by grade + calibrated prob + win-rate,
        # with edge only a small, saturating lever.
        wr_term = 0.5 if fx.win_rate is None else _clip(fx.win_rate, 0, 1)
        size_strength = _clip(
            0.45 * grade
            + 0.25 * _clip(prob, 0, 1)
            + 0.20 * wr_term
            + 0.10 * _clip(edge / 0.20, 0, 1),   # capped edge lever
            0.0, 1.0,
        )

        return GradeEdgeResult(
            prob_calibrated=round(prob, 5),
            edge=round(edge, 5),
            grade=round(grade, 4),
            size_strength=round(size_strength, 4),
            confidence=round(confidence, 4),
            breakeven=round(be, 5),
            components=components,
            pipeline="gbm_multi_pipeline_v1",
            reason="prob=%.3f be=%.3f edge=%+.3f grade=%.2f str=%.2f" % (
                prob, be, edge, grade, size_strength),
        )
    except Exception as e:  # pragma: no cover - fail open
        try:
            log.debug("grade_edge_engine.score failed (fail-open): %s" % e)
        except Exception:
            pass
        prob = _clip(fx.raw_prob, 0.0, 1.0)
        return GradeEdgeResult(
            prob_calibrated=prob, edge=prob - be, grade=0.5,
            size_strength=0.3, confidence=0.2, breakeven=be,
            pipeline="error_fallback", reason="error: %s" % (str(e)[:60]))


def status() -> Dict[str, object]:
    """Small dict for /settings + version banners."""
    return {
        "enabled": enabled(),
        "pipeline": "gbm_multi_pipeline_v1",
        "calibration_temperature": _f("GRADE_EDGE_CALIB_TEMPERATURE", 1.6),
        "calibration_bias": _f("GRADE_EDGE_CALIB_BIAS", -0.15),
        "raw_anchor_w": _f("GRADE_EDGE_RAW_ANCHOR_W", 0.6),
        "pipelines": list(_PIPELINE_WEIGHT.keys()),
    }
