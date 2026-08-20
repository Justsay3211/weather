"""Consensus engine + confidence engine (prompt MODEL CONSENSUS / CONFIDENCE).

Consensus never blindly averages: it de-weights duplicate model families,
computes robust statistics (median/mean/spread/IQR) and reports how many
INDEPENDENT model families agree. Confidence is computed SEPARATELY from the
forecast value and is driven by agreement, ensemble/model spread, freshness,
effective independent count, observation support and source health.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from .schema import ForecastSeries, Category, Freshness
from .registry import effective_independent_count, dependency_report


@dataclass
class ConsensusResult:
    variable: str
    valid_time: Optional[datetime]
    median: Optional[float] = None
    mean: Optional[float] = None
    weighted_mean: Optional[float] = None
    vmin: Optional[float] = None
    vmax: Optional[float] = None
    stdev: Optional[float] = None
    iqr: Optional[float] = None
    spread: Optional[float] = None
    n_values: int = 0
    n_independent: int = 0
    contributors: List[str] = field(default_factory=list)
    outliers: List[str] = field(default_factory=list)


def _percentile(sorted_vals: List[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(math.floor(idx))
    hi = int(math.ceil(idx))
    if lo == hi:
        return sorted_vals[lo]
    frac = idx - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _get_var(point, variable: str) -> Optional[float]:
    return getattr(point, variable, None)


def consensus_for(series_list: List[ForecastSeries], variable: str,
                  when: datetime, tol_s: int = 3600) -> ConsensusResult:
    """Blend one variable across sources at a target valid-time.

    De-duplication: when several series share a model_family, only their MEAN
    contributes one vote to the family, so providers exposing the same model do
    not dominate. Weighted mean uses each family's best prior_weight.
    """
    # family -> list of (value, weight, source_key)
    by_family: Dict[str, List[Tuple[float, float, str]]] = {}
    contributing: List[ForecastSeries] = []
    for s in series_list:
        if s.identity.category == Category.OBSERVATION:
            continue
        p = s.point_at(when, tol_s=tol_s)
        if p is None:
            continue
        v = _get_var(p, variable)
        if v is None:
            continue
        by_family.setdefault(s.identity.model_family, []).append(
            (float(v), float(s.identity.prior_weight), s.identity.source))
        contributing.append(s)

    res = ConsensusResult(variable=variable, valid_time=when)
    if not by_family:
        return res

    family_values: List[float] = []
    family_weights: List[float] = []
    contributors: List[str] = []
    for fam, items in by_family.items():
        vals = [it[0] for it in items]
        fam_val = sum(vals) / len(vals)            # collapse duplicates to 1 vote
        fam_w = max(it[1] for it in items)
        family_values.append(fam_val)
        family_weights.append(fam_w)
        contributors.append(fam)

    sv = sorted(family_values)
    n = len(sv)
    res.n_values = n
    res.n_independent = n
    res.contributors = contributors
    res.median = _percentile(sv, 0.5)
    res.mean = sum(sv) / n
    res.vmin = sv[0]
    res.vmax = sv[-1]
    if n >= 2:
        mean = res.mean
        res.stdev = math.sqrt(sum((x - mean) ** 2 for x in sv) / (n - 1))
        res.iqr = _percentile(sv, 0.75) - _percentile(sv, 0.25)
        res.spread = sv[-1] - sv[0]
    else:
        res.stdev = 0.0
        res.iqr = 0.0
        res.spread = 0.0
    tw = sum(family_weights) or 1.0
    res.weighted_mean = sum(v * w for v, w in zip(family_values, family_weights)) / tw

    # outliers: > 2 stdev from the mean (only meaningful with >=3 families)
    if n >= 3 and res.stdev and res.stdev > 1e-9:
        for fam, val in zip(contributors, family_values):
            if abs(val - res.mean) > 2.0 * res.stdev:
                res.outliers.append(fam)
    return res


@dataclass
class ConfidenceResult:
    score: int = 0                 # 0-100
    label: str = "VERY LOW"
    reasons: List[str] = field(default_factory=list)
    components: Dict[str, float] = field(default_factory=dict)


def _label(score: float) -> str:
    if score >= 85:
        return "VERY HIGH"
    if score >= 70:
        return "HIGH"
    if score >= 50:
        return "MEDIUM"
    if score >= 30:
        return "LOW"
    return "VERY LOW"


def confidence_for(series_list: List[ForecastSeries],
                   consensus: ConsensusResult,
                   variable: str,
                   when: datetime,
                   typical_spread: float = 3.0,
                   observation_series: Optional[List[ForecastSeries]] = None,
                   health_states: Optional[Dict[str, str]] = None) -> ConfidenceResult:
    """Confidence is NOT the forecast probability. It measures how much we trust
    the consensus, from agreement/spread/independence/freshness/obs/health."""
    reasons: List[str] = []
    comp: Dict[str, float] = {}

    n_ind = effective_independent_count(series_list)
    # independence: 1 family -> 0.2, 4+ -> 1.0
    ind_score = max(0.0, min(1.0, (n_ind - 1) / 3.0)) if n_ind > 0 else 0.0
    comp["independence"] = ind_score
    if n_ind >= 4:
        reasons.append("%d independent model families agree" % n_ind)
    elif n_ind <= 1:
        reasons.append("only %d independent model family available" % n_ind)

    # agreement from spread relative to a typical spread for the variable
    spread = consensus.spread if consensus.spread is not None else typical_spread
    agree_score = max(0.0, min(1.0, 1.0 - (spread / (typical_spread * 2.0)))) if typical_spread > 0 else 0.5
    comp["agreement"] = agree_score
    if agree_score >= 0.7:
        reasons.append("model spread is low")
    elif agree_score <= 0.3:
        reasons.append("models disagree widely")

    # freshness: fraction of contributing series that are FRESH/AGING
    fresh_ok = 0
    fresh_total = 0
    for s in series_list:
        if s.identity.category == Category.OBSERVATION or not s.points:
            continue
        fresh_total += 1
        f = s.freshness(now=when)
        if f in (Freshness.FRESH, Freshness.AGING):
            fresh_ok += 1
    fresh_score = (fresh_ok / fresh_total) if fresh_total else 0.0
    comp["freshness"] = fresh_score
    if fresh_total and fresh_score < 0.5:
        reasons.append("several model feeds are stale")

    # observation support: does an observation agree with consensus median?
    obs_score = 0.5
    if observation_series:
        for obs in observation_series:
            p = obs.point_at(when, tol_s=3 * 3600)
            v = getattr(p, variable, None) if p else None
            if v is not None and consensus.median is not None:
                diff = abs(v - consensus.median)
                obs_score = max(0.0, min(1.0, 1.0 - diff / (typical_spread * 2.0)))
                if obs_score >= 0.7:
                    reasons.append("current observations support the forecast")
                elif obs_score <= 0.3:
                    reasons.append("observations diverge from models (possible init mismatch)")
                break
    comp["observation"] = obs_score

    # source health: fraction GREEN
    health_score = 1.0
    if health_states:
        vals = list(health_states.values())
        if vals:
            green = sum(1 for v in vals if v == "GREEN")
            health_score = green / len(vals)
    comp["health"] = health_score

    # weighted blend
    weights = {"independence": 0.30, "agreement": 0.30, "freshness": 0.20,
               "observation": 0.12, "health": 0.08}
    total = sum(comp[k] * weights[k] for k in weights)
    score = int(round(total * 100))
    score = max(0, min(100, score))
    return ConfidenceResult(score=score, label=_label(score), reasons=reasons, components=comp)
