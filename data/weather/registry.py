"""Source registry + dependency graph + effective-independent-model count.

The registry is the single place that knows which providers map to which
underlying model family. This is what stops the engine from treating
Open-Meteo-ECMWF and direct-ECMWF as two independent votes.
"""

from typing import Dict, Iterable, List, Set

from .schema import SourceIdentity, ForecastSeries, Category


class SourceRegistry:
    def __init__(self):
        self._by_source: Dict[str, SourceIdentity] = {}

    def register(self, identity: SourceIdentity) -> None:
        self._by_source[identity.source] = identity

    def get(self, source: str):
        return self._by_source.get(source)

    def all(self) -> List[SourceIdentity]:
        return list(self._by_source.values())

    def model_family(self, source: str) -> str:
        idn = self._by_source.get(source)
        return idn.model_family if idn else source


def effective_independent_count(series_list: Iterable[ForecastSeries]) -> int:
    """Count DISTINCT underlying model families among contributing series.

    Providers (Category.PROVIDER) that are pure blends still each count once
    under their own model_family, but two providers exposing the same raw model
    family collapse to one. Observations are not model votes.
    """
    families: Set[str] = set()
    for s in series_list:
        if s.identity.category == Category.OBSERVATION:
            continue
        if not s.points:
            continue
        families.add(s.identity.model_family)
    return len(families)


def dependency_report(series_list: Iterable[ForecastSeries]) -> Dict[str, List[str]]:
    """model_family -> [providers contributing to it]. Explainability aid."""
    out: Dict[str, List[str]] = {}
    for s in series_list:
        out.setdefault(s.identity.model_family, []).append(s.identity.provider)
    return out
