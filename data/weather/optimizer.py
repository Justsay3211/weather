"""Meta-optimizer: dynamic model weighting + exploration + auto-repair.

Implements the prompt MODEL WEIGHTING section as a live, self-adjusting system
rather than hard-coded percentages. Final weight for a model family is:

    weight = prior
             * skill_multiplier(location, variable, lead, season)   # learned
             * freshness_factor(FRESH/AGING/STALE/...)              # recency
             * health_factor(GREEN/YELLOW/RED)                      # reliability
             * spread_penalty(outlier?)                             # robustness

Exploration (RL / bandit flavour): with probability epsilon a family that has
been under-sampled gets a temporary weight floor so the system keeps testing
whether a currently-worse model has improved (pairs with SourceSelector's audit
windows). This is the "recheck after days" behaviour, expressed as weights.

Auto-repair: if the current champion family is RED / EXPIRED, demote it to a
weight floor and let the next-best family take over, logging the repair action.
"""

import random
from typing import Dict, List, Optional

from .schema import Freshness
from .health import HealthState


FRESHNESS_FACTOR = {
    Freshness.FRESH: 1.0,
    Freshness.AGING: 0.8,
    Freshness.STALE: 0.4,
    Freshness.EXPIRED: 0.1,
    Freshness.MISSING: 0.0,
    Freshness.CORRUPTED: 0.0,
}

HEALTH_FACTOR = {
    HealthState.GREEN: 1.0,
    HealthState.YELLOW: 0.7,
    HealthState.RED: 0.2,
}


class WeightOptimizer:
    def __init__(self, skill_store=None, epsilon: float = 0.10,
                 explore_floor: float = 0.15, rng: Optional[random.Random] = None):
        self.skill = skill_store
        self.epsilon = float(epsilon)
        self.explore_floor = float(explore_floor)
        self.rng = rng or random.Random()
        self._sample_counts: Dict[str, int] = {}

    def note_sampled(self, family: str) -> None:
        self._sample_counts[family] = self._sample_counts.get(family, 0) + 1

    def compute(self, location: str, variable: str, lead_hours: float,
                families: Dict[str, Dict], lat: float = 0.0) -> Dict[str, Dict]:
        """families: family -> {prior, freshness, health, outlier(bool)}.
        Returns family -> {weight, skill, factors...} (weights normalized).
        """
        raw: Dict[str, float] = {}
        detail: Dict[str, Dict] = {}
        for fam, meta in families.items():
            prior = float(meta.get("prior", 0.5))
            fresh = meta.get("freshness", Freshness.FRESH)
            health = meta.get("health", HealthState.GREEN)
            outlier = bool(meta.get("outlier", False))

            skill_mult = 1.0
            skill_score = None
            if self.skill is not None:
                skill_score = self.skill.skill_score(location, variable, lead_hours, fam, lat=lat)
                if skill_score is not None:
                    skill_mult = 0.4 + 1.2 * skill_score

            f_fresh = FRESHNESS_FACTOR.get(fresh, 0.5)
            f_health = HEALTH_FACTOR.get(health, 0.7)
            f_spread = 0.6 if outlier else 1.0

            w = prior * skill_mult * f_fresh * f_health * f_spread

            # exploration floor for under-sampled families
            explored = False
            if self.rng.random() < self.epsilon:
                if self._sample_counts.get(fam, 0) < 5:
                    w = max(w, self.explore_floor)
                    explored = True

            raw[fam] = max(0.0, w)
            detail[fam] = {"skill": skill_score, "prior": prior,
                           "f_fresh": f_fresh, "f_health": f_health,
                           "f_spread": f_spread, "explored": explored}

        total = sum(raw.values()) or 1.0
        for fam in raw:
            detail[fam]["weight"] = raw[fam] / total
        return detail

    def auto_repair(self, weights: Dict[str, Dict],
                    families_meta: Dict[str, Dict]) -> List[str]:
        """Demote broken champions. Returns a list of human-readable actions.
        Mutates `weights` in place (renormalizes afterwards)."""
        actions: List[str] = []
        if not weights:
            return actions
        champion = max(weights.items(), key=lambda kv: kv[1].get("weight", 0.0))[0]
        meta = families_meta.get(champion, {})
        broken = (meta.get("health") == HealthState.RED or
                  meta.get("freshness") in (Freshness.EXPIRED, Freshness.MISSING,
                                            Freshness.CORRUPTED))
        if broken and len(weights) > 1:
            weights[champion]["weight"] = 0.01
            actions.append("demoted champion %s (health=%s freshness=%s); promoted next-best" % (
                champion, meta.get("health"), meta.get("freshness")))
            total = sum(v.get("weight", 0.0) for v in weights.values()) or 1.0
            for fam in weights:
                weights[fam]["weight"] = weights[fam].get("weight", 0.0) / total
        return actions

    def weighted_value(self, values_by_family: Dict[str, float],
                       weights: Dict[str, Dict]) -> Optional[float]:
        num = 0.0
        den = 0.0
        for fam, val in values_by_family.items():
            if val is None:
                continue
            w = weights.get(fam, {}).get("weight", 0.0)
            num += w * val
            den += w
        return (num / den) if den > 0 else None
