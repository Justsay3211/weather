"""Smart source selection (prompt COST OPTIMIZATION + MODEL DIVERSITY + MOST
IMPORTANT RULE: do not confuse MORE DATA with BETTER FORECAST).

This is the "multi-agent-level" scheduler the user described:

  * For a given (location, variable, lead) it ranks model families by learned
    skill and elects a CHAMPION plus a small diverse challenger set.
  * Providers whose ONLY contribution is a non-selected family are SUPPRESSED
    so we stop spending requests on them (e.g. once ECMWF is proven best we
    stop hitting the extra provider APIs and only keep a diverse safety net).
  * Suppression is never permanent: every `reeval_days` a source enters an
    AUDIT window where it is fetched again so we can detect whether a
    previously-worse model has improved (or the champion regressed) and
    re-elect. Between audits we also keep >=2 independent families live so
    confidence never collapses to a single point of failure.

Decisions are explainable: `plan()` returns per-provider action + reason.
"""

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional


CHAMPION = "champion"
CHALLENGER = "challenger"
AUDIT = "audit"
SUPPRESSED = "suppressed"


class SourceSelector:
    def __init__(self, skill_store=None, path: Optional[str] = None,
                 reeval_days: float = 3.0, min_independent: int = 3,
                 keep_challengers: int = 2):
        """
        skill_store      : SkillStore used to rank families (may be None early).
        reeval_days      : how often a suppressed source is force-audited.
        min_independent  : always keep at least this many independent families
                           live so confidence + diversity survive.
        keep_challengers : diverse challengers kept alongside the champion.
        """
        self.skill = skill_store
        self.path = path
        self.reeval_s = float(reeval_days) * 86400.0
        self.min_independent = int(min_independent)
        self.keep_challengers = int(keep_challengers)
        # family -> last audit epoch
        self._last_audit: Dict[str, float] = {}
        self._lock = threading.RLock()
        if path:
            self.load()

    def _due_for_audit(self, family: str, now: float) -> bool:
        last = self._last_audit.get(family, 0.0)
        return (now - last) >= self.reeval_s

    def mark_audited(self, family: str, now: Optional[float] = None) -> None:
        with self._lock:
            self._last_audit[family] = now if now is not None else time.time()
        self.save()

    def elect(self, location: str, variable: str, lead_hours: float,
              families: List[str], now: Optional[float] = None) -> Dict[str, str]:
        """Return family -> role (champion/challenger/audit/suppressed)."""
        now = now if now is not None else time.time()
        roles: Dict[str, str] = {}
        if not families:
            return roles
        # rank by skill when available, else keep given order (priors handle it)
        if self.skill is not None:
            ranked = [f for f, _ in self.skill.ranking(location, variable, lead_hours, families)]
        else:
            ranked = list(families)

        champion = ranked[0]
        roles[champion] = CHAMPION
        kept_independent = 1

        for fam in ranked[1:]:
            if kept_independent < max(self.min_independent, 1 + self.keep_challengers):
                roles[fam] = CHALLENGER
                kept_independent += 1
            elif self._due_for_audit(fam, now):
                roles[fam] = AUDIT           # temporarily re-enabled to re-check
                kept_independent += 1
            else:
                roles[fam] = SUPPRESSED
        # Any family we actually fetched this cycle (champion/challenger/audit)
        # has just been "checked", so reset its audit clock. This is what makes
        # suppression engage AFTER the initial baseline comparison and only
        # re-open a source once reeval_days elapse.
        with self._lock:
            for fam, role in roles.items():
                if role != SUPPRESSED:
                    self._last_audit[fam] = now
        return roles

    def plan(self, location: str, variable: str, lead_hours: float,
             provider_families: Dict[str, List[str]],
             now: Optional[float] = None) -> Dict[str, Dict]:
        """Translate family roles into per-provider fetch decisions.

        provider_families: provider -> list of model families it can supply.
        A provider is FETCHED if any family it supplies is champion/challenger/
        audit; otherwise SUPPRESSED (request saved). Reason is human-readable.
        """
        now = now if now is not None else time.time()
        all_families: List[str] = []
        for fams in provider_families.values():
            for f in fams:
                if f not in all_families:
                    all_families.append(f)
        roles = self.elect(location, variable, lead_hours, all_families, now)

        out: Dict[str, Dict] = {}
        for provider, fams in provider_families.items():
            best_role = SUPPRESSED
            picked = None
            for f in fams:
                r = roles.get(f, SUPPRESSED)
                order = {CHAMPION: 3, CHALLENGER: 2, AUDIT: 1, SUPPRESSED: 0}
                if order[r] > order[best_role]:
                    best_role = r
                    picked = f
            fetch = best_role != SUPPRESSED
            if best_role == CHAMPION:
                reason = "champion model (%s)" % picked
            elif best_role == CHALLENGER:
                reason = "diverse challenger (%s)" % picked
            elif best_role == AUDIT:
                reason = "re-audit after %.0fd to check for improvement (%s)" % (
                    self.reeval_s / 86400.0, picked)
            else:
                reason = "suppressed to save requests (no elected family)"
            out[provider] = {"fetch": fetch, "role": best_role,
                             "family": picked, "reason": reason}
        return out

    def snapshot(self) -> Dict:
        return {"reeval_days": self.reeval_s / 86400.0,
                "min_independent": self.min_independent,
                "last_audit": dict(self._last_audit)}

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as fh:
                j = json.load(fh) or {}
            self._last_audit = j.get("last_audit", {})
        except Exception:
            self._last_audit = {}

    def save(self):
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump({"last_audit": self._last_audit}, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass
