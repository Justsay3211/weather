"""Master forecast engine — the brain that sits on top of the raw pipeline.

The WeatherPipeline handles INGEST -> NORMALIZE -> QC -> CACHE -> raw consensus.
This module implements the higher prompt layers that were missing:

  L7  observation / model comparison        (bias signal)
  L8  bias correction                       (learning.ResidualLearner / LocalGBM)
  L9  dynamic model weighting               (optimizer.WeightOptimizer + skill)
  L10 multi-model ensemble (weighted)       (per-family weighted estimate)
  L11 probabilistic calibration             (learning.Calibrator)
  L14 confidence engine                     (engine.confidence_for + skill)
  L15 natural-language explanation          (reasons + provenance + warnings)

It also runs the continuous-verification loop (HISTORICAL VERIFICATION ENGINE):
`ingest_observation` compares a stored forecast against a later observation and
feeds the error into skill stats, the residual learner and the calibrator, so
the system genuinely improves over time. And it produces the SMART SOURCE
SELECTION plan (champion/challenger + request-saving + periodic re-audit).

Everything is persisted to JSON under a state dir so it survives restarts and
can run on the VPS or the bot.
"""

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from .schema import ForecastSeries, Category, Freshness
from .registry import effective_independent_count, dependency_report
from .engine import consensus_for, confidence_for
from .skill import SkillStore, lead_bucket, season_of
from .learning import ResidualLearner, Calibrator, make_features
from .selection import SourceSelector
from .optimizer import WeightOptimizer


@dataclass
class VariableForecast:
    variable: str
    raw_estimate: Optional[float] = None       # weighted ensemble, uncorrected
    estimate: Optional[float] = None           # bias-corrected best estimate
    low: Optional[float] = None                # uncertainty band low
    high: Optional[float] = None               # uncertainty band high
    spread: Optional[float] = None
    probability: Optional[float] = None        # calibrated (precip only), 0..1
    raw_probability: Optional[float] = None
    confidence: int = 0
    confidence_label: str = "VERY LOW"
    reasons: List[str] = field(default_factory=list)
    correction_method: str = "raw"
    n_independent: int = 0
    weights: Dict[str, float] = field(default_factory=dict)


@dataclass
class MasterForecastResult:
    location: str
    when: datetime
    variables: Dict[str, VariableForecast] = field(default_factory=dict)
    provenance: Dict[str, List[str]] = field(default_factory=dict)
    excluded: Dict[str, str] = field(default_factory=dict)
    suppressed: Dict[str, str] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    repairs: List[str] = field(default_factory=list)
    model_log: List[str] = field(default_factory=list)
    n_independent: int = 0

    def overall_confidence(self) -> int:
        if not self.variables:
            return 0
        return int(round(sum(v.confidence for v in self.variables.values()) / len(self.variables)))


def _hours_lead(when: datetime, target: datetime) -> float:
    return max(0.0, (target - when).total_seconds() / 3600.0)


def _families_at(series_list: List[ForecastSeries], variable: str, when: datetime,
                 tol_s: int = 3600):
    """family -> (mean_value, prior, worst_freshness, any_outlier_placeholder)."""
    by_fam: Dict[str, List] = {}
    for s in series_list:
        if s.identity.category == Category.OBSERVATION or not s.points:
            continue
        p = s.point_at(when, tol_s=tol_s)
        if p is None:
            continue
        v = getattr(p, variable, None)
        if v is None:
            continue
        by_fam.setdefault(s.identity.model_family, []).append(
            (float(v), float(s.identity.prior_weight), s.freshness(now=when)))
    return by_fam


class MasterEngine:
    def __init__(self, state_dir: Optional[str] = None,
                 reeval_days: float = 3.0, min_independent: int = 3,
                 epsilon: float = 0.10):
        self.state_dir = state_dir
        sp = os.path.join(state_dir, "skill.json") if state_dir else None
        lp = os.path.join(state_dir, "residual.json") if state_dir else None
        cp = os.path.join(state_dir, "calibration.json") if state_dir else None
        selp = os.path.join(state_dir, "selection.json") if state_dir else None
        self.skill = SkillStore(path=sp)
        self.learner = ResidualLearner(path=lp)
        self.calibrator = Calibrator(path=cp)
        self.selector = SourceSelector(skill_store=self.skill, path=selp,
                                       reeval_days=reeval_days,
                                       min_independent=min_independent)
        self.optimizer = WeightOptimizer(skill_store=self.skill, epsilon=epsilon)

    def save(self) -> None:
        self.skill.save()
        self.learner.save()
        self.calibrator.save()
        self.selector.save()

    # ---- selection planning -------------------------------------------
    def selection_plan(self, location: str, variable: str, lead_hours: float,
                       provider_families: Dict[str, List[str]],
                       now: Optional[float] = None) -> Dict[str, Dict]:
        return self.selector.plan(location, variable, lead_hours,
                                  provider_families, now=now)

    def suppressed_providers(self, location: str, variable: str, lead_hours: float,
                             provider_families: Dict[str, List[str]]) -> Dict[str, str]:
        """provider -> reason, for providers the selector wants to SKIP now."""
        plan = self.selection_plan(location, variable, lead_hours, provider_families)
        return {p: d["reason"] for p, d in plan.items() if not d["fetch"]}

    # ---- analysis (L7..L15) -------------------------------------------
    def analyze(self, result, lat: float, lon: float,
                when: Optional[datetime] = None,
                variables: Optional[List[str]] = None,
                observation_series: Optional[List[ForecastSeries]] = None
                ) -> MasterForecastResult:
        """`result` is a pipeline.PipelineResult. Produces the master forecast."""
        series_list = list(getattr(result, "series", []) or [])
        now = datetime.now(timezone.utc)
        when = when or getattr(result, "when", None) or now
        variables = variables or ["temp_c", "precip_mm", "precip_prob_pct"]
        location = getattr(result, "location", "") or ("%s,%s" % (lat, lon))

        out = MasterForecastResult(location=location, when=when)
        out.provenance = dependency_report(series_list)
        out.excluded = dict(getattr(result, "excluded", {}) or {})
        out.n_independent = effective_independent_count(series_list)

        lead_h = _hours_lead(now, when)
        health = getattr(result, "health", {}) or {}
        health_states = {k: v.get("state") for k, v in health.items()}

        typical = {"temp_c": 3.0, "precip_mm": 3.0, "precip_prob_pct": 30.0,
                   "humidity_pct": 15.0, "wind_speed_kmh": 10.0}

        for var in variables:
            cons = consensus_for(series_list, var, when)
            vf = VariableForecast(variable=var)
            vf.n_independent = cons.n_independent
            vf.spread = cons.spread

            by_fam = _families_at(series_list, var, when)
            if not by_fam:
                out.variables[var] = vf
                continue

            # per-family meta for the optimizer
            fam_meta = {}
            fam_value = {}
            for fam, items in by_fam.items():
                vals = [it[0] for it in items]
                fam_value[fam] = sum(vals) / len(vals)
                worst_fresh = Freshness.FRESH
                order = {Freshness.FRESH: 0, Freshness.AGING: 1, Freshness.STALE: 2,
                         Freshness.EXPIRED: 3, Freshness.MISSING: 4, Freshness.CORRUPTED: 5}
                for it in items:
                    if order.get(it[2], 0) > order.get(worst_fresh, 0):
                        worst_fresh = it[2]
                fam_meta[fam] = {
                    "prior": max(it[1] for it in items),
                    "freshness": worst_fresh,
                    "health": "GREEN",
                    "outlier": fam in cons.outliers,
                }
            # map provider health onto families (best-effort)
            for s in series_list:
                st = health_states.get(s.identity.provider)
                if st and s.identity.model_family in fam_meta:
                    # keep the worst health seen
                    cur = fam_meta[s.identity.model_family]["health"]
                    rank = {"GREEN": 0, "YELLOW": 1, "RED": 2}
                    if rank.get(st, 0) > rank.get(cur, 0):
                        fam_meta[s.identity.model_family]["health"] = st

            weights = self.optimizer.compute(location, var, lead_h, fam_meta, lat=lat)
            out.repairs.extend(self.optimizer.auto_repair(weights, fam_meta))
            for fam in weights:
                self.optimizer.note_sampled(fam)
            vf.weights = {fam: round(weights[fam]["weight"], 3) for fam in weights}

            raw_est = self.optimizer.weighted_value(fam_value, weights)
            vf.raw_estimate = raw_est

            # bias correction (L8) on the ensemble estimate
            if raw_est is not None and var in ("temp_c", "temp_min_c", "temp_max_c",
                                               "humidity_pct", "wind_speed_kmh"):
                feats = make_features(lead_h, when.hour, when.month,
                                      cons.spread or 0.0, raw_est)
                corrected, method = self.learner.correct(location, var, feats, raw_est)
                vf.estimate = corrected
                vf.correction_method = method
            else:
                vf.estimate = raw_est
                vf.correction_method = "raw"

            # uncertainty band from spread, widened by lead
            band = (cons.stdev or cons.spread or typical.get(var, 3.0))
            lead_factor = 1.0 + min(2.0, lead_h / 72.0)
            if vf.estimate is not None:
                vf.low = vf.estimate - band * lead_factor
                vf.high = vf.estimate + band * lead_factor

            # calibrated probability for precip probability variable
            if var == "precip_prob_pct" and vf.estimate is not None:
                raw_p = max(0.0, min(1.0, vf.estimate / 100.0))
                vf.raw_probability = raw_p
                ckey = location + "|precip>0.2mm|" + lead_bucket(lead_h)
                vf.probability = self.calibrator.calibrate(ckey, raw_p)

            # confidence (L14) — engine confidence + skill boost
            conf = confidence_for(series_list, cons, var, when,
                                  typical_spread=typical.get(var, 3.0),
                                  observation_series=observation_series,
                                  health_states=health_states)
            reasons = list(conf.reasons)
            best_skill = None
            for fam in fam_value:
                sc = self.skill.skill_score(location, var, lead_h, fam, lat=lat)
                if sc is not None and (best_skill is None or sc > best_skill):
                    best_skill = sc
            score = conf.score
            if best_skill is not None:
                score = int(round(0.8 * score + 0.2 * (best_skill * 100)))
                if best_skill >= 0.7:
                    reasons.append("historical skill for this location is strong")
            if vf.correction_method == "gbm":
                reasons.append("local ML bias-correction applied")
            vf.confidence = max(0, min(100, score))
            vf.confidence_label = _label(vf.confidence)
            vf.reasons = reasons

            out.variables[var] = vf

        # warnings (L15)
        if out.n_independent < 3:
            out.warnings.append("only %d independent model families available" % out.n_independent)
        if lead_h > 168:
            out.warnings.append("extended-range lead (%.0fh): treat as probabilistic" % lead_h)
        stale = [s.identity.source for s in series_list
                 if s.freshness(now=now) in (Freshness.STALE, Freshness.EXPIRED)]
        if stale:
            out.warnings.append("%d model feed(s) stale/expired" % len(stale))

        # model log (what was used / excluded / suppressed)
        for fam, provs in out.provenance.items():
            out.model_log.append("USED  %-14s <- %s" % (fam, ", ".join(sorted(set(provs)))))
        for prov, reason in out.excluded.items():
            out.model_log.append("SKIP  %-14s (%s)" % (prov, reason))
        return out

    # ---- verification loop (continuous learning) ----------------------
    def ingest_observation(self, location: str, lat: float, variable: str,
                           lead_hours: float, family_values: Dict[str, float],
                           observed_value: float, when: Optional[datetime] = None,
                           ensemble_spread: float = 0.0) -> None:
        """Fold a matured forecast vs its observation into skill + learner.
        family_values: model_family -> value that was forecast for `when`.
        """
        when = when or datetime.now(timezone.utc)
        if observed_value is None:
            return
        ens_vals = [v for v in family_values.values() if v is not None]
        ens_mean = (sum(ens_vals) / len(ens_vals)) if ens_vals else None
        for fam, val in family_values.items():
            if val is None:
                continue
            self.skill.record(location, variable, lead_hours, fam,
                              val, observed_value, when=when, lat=lat)
        if ens_mean is not None and variable in ("temp_c", "temp_min_c", "temp_max_c",
                                                 "humidity_pct", "wind_speed_kmh"):
            feats = make_features(lead_hours, when.hour, when.month,
                                  ensemble_spread, ens_mean)
            residual = observed_value - ens_mean
            self.learner.observe(location, variable, feats, residual)

    def ingest_precip_outcome(self, location: str, lead_hours: float,
                              predicted_prob: float, occurred: bool) -> None:
        from .skill import lead_bucket as _lb
        ckey = location + "|precip>0.2mm|" + _lb(lead_hours)
        self.calibrator.observe(ckey, predicted_prob, occurred)


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
