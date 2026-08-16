"""
Market resolver — fetch Polymarket's ACTUAL resolved outcome for a weather
market, including AFTER it closes.

Why this exists
---------------
The scanner deliberately DROPS closed events (``if event.get('closed'): return
None``), so the moment a market settles the trading loop loses all knowledge of
it and paper positions get stuck or show garbage PnL. This module fetches the
event by slug *including* closed ones and reads the resolution straight from
Gamma:

  * Each child market carries ``outcomePrices`` (e.g. '["1","0"]'). After UMA
    resolves, the YES leg of the winning bucket prices to ~1.0 and every losing
    bucket to ~0.0. That is Polymarket's OWN settled truth — NOT a forecast.
  * ``closed`` / ``umaResolutionStatus`` tell us whether settlement is final.

Design rule (per spec): Polymarket's resolved ``outcomePrices`` are the SOURCE
OF TRUTH for win/lose. The weather observation is only a CONFIRMATION metric and
a PRE-close signal — it never overrides the venue's settled value.

Network note: ``requests`` is imported lazily so this module (and its pure
``parse_event_resolution`` helper) import cleanly offline / in tests.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional

try:
    from config import Config
    _GAMMA = Config.GAMMA_API_URL
    _CLOB = Config.CLOB_API_URL
    _VERSION = Config.VERSION
except Exception:  # pragma: no cover - bare sandbox / import isolation
    _GAMMA = "https://gamma-api.polymarket.com"
    _CLOB = "https://clob.polymarket.com"
    _VERSION = "0"

try:
    from logger import log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("market_resolver")

# A YES price this high/low on a closed market = settled win / loss.
_WIN_PRICE = 0.99
_LOSE_PRICE = 0.01


def _to_list(raw) -> list:
    """Gamma returns arrays as JSON strings ('["1","0"]') or real lists."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
            return v if isinstance(v, list) else []
        except Exception:
            return []
    return []


@dataclass
class BucketResolution:
    label: str
    token_id_yes: Optional[str]
    yes_price: Optional[float]      # 0..1 resolved/last price for the YES leg
    won: Optional[bool]             # True/False once resolved, else None
    condition_id: str = ""


@dataclass
class MarketResolution:
    slug: str
    closed: bool
    resolved: bool                  # final settlement known for >=1 bucket
    uma_status: str = ""
    winning_label: Optional[str] = None
    buckets: List[BucketResolution] = field(default_factory=list)
    source: str = "gamma"
    reason: str = ""
    raw_end: Optional[str] = None

    def yes_won(self, label: str) -> Optional[bool]:
        for b in self.buckets:
            if b.label == label:
                return b.won
        return None

    def price_for(self, label: str) -> Optional[float]:
        for b in self.buckets:
            if b.label == label:
                return b.yes_price
        return None


def parse_event_resolution(event: dict, slug: str = "") -> MarketResolution:
    """Pure: derive resolution from a Gamma event dict. Offline-testable.

    A child market is treated as individually settled when its YES price has
    snapped to the 0/1 rail AND the market (or its parent event) reports closed
    / resolved. The winning bucket is the one whose YES leg settled to ~1.0.
    """
    if not isinstance(event, dict):
        return MarketResolution(slug=slug, closed=False, resolved=False,
                                reason="no event")

    closed = bool(event.get("closed"))
    uma = (event.get("umaResolutionStatus")
           or event.get("uma_resolution_status") or "")
    if not uma:
        statuses = event.get("umaResolutionStatuses")
        if isinstance(statuses, list) and statuses and isinstance(statuses[0], str):
            uma = statuses[0]

    buckets: List[BucketResolution] = []
    any_resolved = False
    winning: Optional[str] = None

    for m in event.get("markets", []) or []:
        if not isinstance(m, dict):
            continue
        label = m.get("question") or m.get("groupItemTitle") or ""
        ids = _to_list(m.get("clobTokenIds"))
        prices = _to_list(m.get("outcomePrices"))
        yes_price: Optional[float] = None
        if prices:
            try:
                yes_price = float(prices[0])
            except (TypeError, ValueError):
                yes_price = None

        m_closed = bool(m.get("closed")) or closed
        m_resolved = (str(m.get("umaResolutionStatus") or "").lower()
                      in ("resolved", "settled")) or bool(m.get("resolvedBy"))
        won: Optional[bool] = None
        if yes_price is not None and (m_closed or m_resolved):
            if yes_price >= _WIN_PRICE:
                won = True
                any_resolved = True
                winning = label
            elif yes_price <= _LOSE_PRICE:
                won = False
                any_resolved = True

        buckets.append(BucketResolution(
            label=label,
            token_id_yes=ids[0] if ids else None,
            yes_price=yes_price,
            won=won,
            condition_id=m.get("conditionId", "") or "",
        ))

    resolved = bool(closed and any_resolved) or winning is not None
    return MarketResolution(
        slug=slug or event.get("slug", ""),
        closed=closed,
        resolved=resolved,
        uma_status=uma or "",
        winning_label=winning,
        buckets=buckets,
        raw_end=event.get("endDate") or event.get("end_date_iso"),
        reason="parsed from gamma event",
    )


class MarketResolver:
    """Fetch Polymarket's actual resolution for a market slug (closed or not)."""

    def __init__(self, timeout: float = 8.0):
        self.timeout = timeout
        self._session = None
        self._cache: Dict[str, MarketResolution] = {}

    def _get_session(self):
        if self._session is None:
            try:
                import requests  # lazy
                s = requests.Session()
                s.headers.update({"User-Agent": f"WeatherSniper/{_VERSION}",
                                  "Accept": "application/json"})
                self._session = s
            except Exception as e:  # pragma: no cover
                log.warning(f"requests unavailable for resolver: {e}")
                return None
        return self._session

    def fetch_event_by_slug(self, slug: str) -> Optional[dict]:
        """Fetch a Gamma event by slug WITHOUT the scanner's closed-filter."""
        s = self._get_session()
        if s is None or not slug:
            return None
        try:
            resp = s.get(f"{_GAMMA}/events", params={"slug": slug}, timeout=self.timeout)
            if resp.status_code != 200:
                return None
            data = resp.json()
            events = data if isinstance(data, list) else data.get("events", [])
            if not events:
                return None
            return events[0]
        except Exception as e:  # pragma: no cover
            log.debug(f"resolver fetch failed {slug}: {e}")
            return None

    def get_resolution(self, slug: str, use_cache: bool = True) -> Optional[MarketResolution]:
        """Return Polymarket's resolution for a slug. Final results are cached
        permanently (they never change); unresolved lookups are re-fetched."""
        if use_cache and slug in self._cache and self._cache[slug].resolved:
            return self._cache[slug]
        ev = self.fetch_event_by_slug(slug)
        if ev is None:
            return None
        res = parse_event_resolution(ev, slug)
        self._cache[slug] = res
        return res

    def get_token_settle_price(self, token_id: str) -> Optional[float]:
        """Last/settled CLOB SELL price for a token (fallback only)."""
        s = self._get_session()
        if s is None or not token_id:
            return None
        try:
            resp = s.get(f"{_CLOB}/price",
                         params={"token_id": token_id, "side": "SELL"},
                         timeout=self.timeout)
            if resp.status_code == 200:
                return float(resp.json().get("price", 0) or 0)
        except Exception:  # pragma: no cover
            pass
        return None
