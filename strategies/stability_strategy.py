"""
Stability Strategy — trade only the PREDICTABLE city-days, and spread across
ADJACENT temperature buckets so any near-correct outcome still wins.

Approach (from an 80%-win-rate weather trader):
  1. Only trade cities/days with a high stability score (StabilityEngine).
     Stable, sideways, calm, model-agreeing days are highly predictable.
  2. Center on the ensemble forecast max (airport station).
  3. Buy the center bucket AND its neighbors (e.g. forecast 24°C → 23+24+25).
     If the true high lands on any of them, the winning leg pays $1 — and as
     long as the combined cost is below that $1, the basket is +EV.
  4. Exit rule travels with the signal:
       - stable + no rain  → HOLD to resolution (collect full $1)
       - unstable          → exit ~1h before resolution, or take 0.80–0.90 early
       - rain blocks the high → HOLD (low buckets / "or below" tend to hit)

Sizing is conservative and scales with the stability score: more predictable
day → larger basket, capped by config.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from config import Config
from logger import log
from data.probability_engine import BucketProbability
from data.stability import StabilityReport


@dataclass
class StabilityLeg:
    bucket_label: str
    token_id: str
    market_price: float
    our_probability: float
    size_usd: float
    offset: int               # 0 = center, ±1 / ±2 = neighbor


@dataclass
class StabilitySignal:
    market_title: str
    city: str
    primary_bucket: str
    forecast_max_c: float
    stability_score: float
    trend: str
    legs: List[StabilityLeg] = field(default_factory=list)
    total_cost: float = 0.0
    combined_prob: float = 0.0      # our P(any leg wins)
    payout_if_win: float = 0.0      # $ from the single winning leg
    hold_to_resolution: bool = True
    exit_hint: str = ''             # human-readable exit rule
    reason: str = ''


_NUM_RE = re.compile(r'(-?\d+(?:\.\d+)?)')


def bucket_center(label: str, lo: float, hi: float) -> Optional[float]:
    """Best-effort numeric center of a bucket for adjacency math."""
    if lo != float('-inf') and hi != float('inf'):
        return (lo + hi) / 2.0
    m = _NUM_RE.search(label or '')
    if m:
        return float(m.group(1))
    if lo != float('-inf'):
        return lo
    if hi != float('inf'):
        return hi
    return None


class StabilityStrategy:
    """Adjacent-bucket basket on high-stability city-days."""

    name = "stability"
    description = (
        "Trade only high-stability (predictable) city-days. Buy the forecast-max "
        "bucket plus neighbors so any near-correct outcome wins. Hold stable days "
        "to resolution; exit unstable days early."
    )

    # ── tunables ──
    NEIGHBOR_SPAN = int(getattr(Config, 'STABILITY_NEIGHBOR_SPAN', 1) or 1)  # ±1 → 3 legs
    MAX_LEG_PRICE = float(getattr(Config, 'STABILITY_MAX_LEG_PRICE', 0.60) or 0.60)
    MIN_BASKET_EDGE = float(getattr(Config, 'STABILITY_MIN_EDGE', 0.08) or 0.08)
    MAX_BASKET_FRACTION = float(getattr(Config, 'STABILITY_MAX_FRACTION', 0.25) or 0.25)
    EARLY_EXIT_PRICE = float(getattr(Config, 'STABILITY_EARLY_EXIT_PRICE', 0.85) or 0.85)
    EXIT_HOURS_BEFORE = float(getattr(Config, 'STABILITY_EXIT_HOURS_BEFORE', 1.0) or 1.0)

    def evaluate(
        self,
        market_title: str,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
        token_ids: Dict[str, str],
        balance: float,
        city: str,
        stability: StabilityReport,
    ) -> List[StabilitySignal]:
        if stability is None or not stability.predictable:
            return []
        if not bucket_probs:
            return []

        # Order buckets by numeric center so neighbors are adjacent in the list.
        indexed = []
        for bp in bucket_probs:
            c = bucket_center(bp.bucket_label, bp.bucket_low, bp.bucket_high)
            if c is not None:
                indexed.append((c, bp))
        if not indexed:
            return []
        indexed.sort(key=lambda x: x[0])

        # Center = bucket whose center is closest to the ensemble forecast max.
        target = stability.forecast_max_c
        center_i = min(range(len(indexed)), key=lambda i: abs(indexed[i][0] - target))

        # Conviction → basket WIDTH, but adjacency is DIRECTIONAL, not blind.
        # Always buy the forecast-peak (center). Add at most ONE neighbor, on the
        # side the day is trending toward:
        #   warming → upper neighbor (+1), cooling → lower neighbor (-1),
        #   flat → the side the fractional forecast leans toward.
        # The neighbor is only KEPT if it survives the price floor + MAX_LEG_PRICE
        # checks below (i.e. it's a real, sellable mispricing — "not all adjacent").
        center_val, center_bp = indexed[center_i]
        center_conf = getattr(center_bp, 'confidence', 0.0)
        lean = 1 if (target - center_val) >= 0 else -1
        trend = (stability.trend or '').lower()
        if trend == 'warming':
            direction = 1
        elif trend == 'cooling':
            direction = -1
        else:
            direction = lean
        tight = (stability.score >= Config.BASKET_TIGHT_GRADE
                 and center_conf >= Config.BASKET_TIGHT_CONFIDENCE)
        # Tight (high conviction) → center only. Otherwise → center + trend neighbor.
        offsets = [0] if tight else [0, direction]

        chosen = []
        seen_idx = set()
        for off in offsets:
            j = center_i + off
            if j < 0 or j >= len(indexed) or j in seen_idx:
                continue
            seen_idx.add(j)
            _, bp = indexed[j]
            price = market_prices.get(bp.bucket_label)
            tid = token_ids.get(bp.bucket_label)
            if price is None or tid is None:
                continue
            if price <= 0 or price > self.MAX_LEG_PRICE:
                continue
            chosen.append((off, bp, price, tid))

        if not chosen:
            return []

        combined_market = sum(p for _, _, p, _ in chosen)
        # PROFIT GUARANTEE: buying EVERY leg must cost < BASKET_MAX_COST so any
        # single winning leg ($1 payout) nets a profit margin. (0.85 → ≥~18%.)
        if combined_market >= Config.BASKET_MAX_COST:
            log.debug(f"Stability {city}: basket cost {combined_market:.2f} >= max {Config.BASKET_MAX_COST} ({'tight' if tight else 'wide'}) — skip")
            return []

        combined_prob = sum(bp.probability for _, bp, _, _ in chosen)
        basket_edge = combined_prob - combined_market
        if basket_edge < self.MIN_BASKET_EDGE:
            log.debug(f"Stability {city}: edge {basket_edge:.2f} < min — skip")
            return []

        # Size the basket: scale with stability score, cap by config fraction.
        basket_usd = balance * self.MAX_BASKET_FRACTION * stability.score
        basket_usd = max(Config.MIN_ORDER_SIZE * len(chosen), basket_usd)
        basket_usd = min(basket_usd, balance * self.MAX_BASKET_FRACTION)

        # Allocate by inverse price (cheaper legs get more shares) weighted to center.
        weights = []
        for off, bp, price, tid in chosen:
            w = (1.0 / max(price, 0.02)) * (1.0 if off == 0 else 0.6)
            weights.append(w)
        wsum = sum(weights) or 1.0

        legs = []
        for (off, bp, price, tid), w in zip(chosen, weights):
            leg_usd = max(Config.MIN_ORDER_SIZE, basket_usd * (w / wsum))
            legs.append(StabilityLeg(
                bucket_label=bp.bucket_label, token_id=tid,
                market_price=price, our_probability=bp.probability,
                size_usd=round(leg_usd, 2), offset=off,
            ))

        total_cost = round(sum(l.size_usd for l in legs), 2)
        hold = stability.hold_to_resolution()
        if hold:
            exit_hint = "HOLD to resolution (stable, no rain block)"
        elif stability.rain_block:
            exit_hint = "HOLD — rain likely caps the high (favor lower buckets)"
        else:
            exit_hint = (f"EXIT ~{self.EXIT_HOURS_BEFORE:.0f}h before resolution "
                         f"or take profit at ${self.EARLY_EXIT_PRICE:.2f}")

        sig = StabilitySignal(
            market_title=market_title, city=city,
            primary_bucket=indexed[center_i][1].bucket_label,
            forecast_max_c=stability.forecast_max_c,
            stability_score=stability.score, trend=stability.trend,
            legs=legs, total_cost=total_cost,
            combined_prob=round(combined_prob, 3),
            payout_if_win=round(min(l.size_usd / l.market_price for l in legs), 2),
            hold_to_resolution=hold, exit_hint=exit_hint,
            reason=(
                f"STABILITY {city} score={stability.score:.2f} {stability.trend} | "
                f"center={indexed[center_i][1].bucket_label} (fcst {target:.1f}C) | "
                f"{len(legs)} legs cost=${total_cost:.2f} "
                f"Pwin={combined_prob:.0%} edge={basket_edge:.0%} | {exit_hint}"
            ),
        )
        return [sig]
