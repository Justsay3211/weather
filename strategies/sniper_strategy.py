"""
Sniper Strategy — Buy cheap mispriced weather buckets.

Inspired by the trader who turned $151 → $17,758 on Asian temperature markets.
Strategy: Buy YES on outcomes priced $0.007-$0.15 that our forecasts say are likely.

Key insight: Weather markets are slow to update. When a new forecast drops,
the market takes minutes/hours to reprice. We buy the correct bucket while
it's still cheap.

Edge formula: (1 - q) / q where q = market price
At q=0.007 → 142x return on win
At q=0.05 → 19x return on win
At q=0.10 → 9x return on win
"""

from typing import Dict, List, Optional
from dataclasses import dataclass

from config import Config
from data.probability_engine import BucketProbability
from logger import log


@dataclass
class SniperSignal:
    """A sniper trading signal."""
    market_title: str
    bucket_label: str
    token_id: str
    market_price: float
    our_probability: float
    edge: float
    expected_return: float   # if we win: (1/price - 1)
    kelly_size: float        # kelly fraction of bankroll
    confidence: float
    reason: str


class SniperStrategy:
    """
    Buy cheap buckets that forecasts strongly favor.
    
    Entry criteria:
    1. Market price < SNIPER_MAX_ENTRY_PRICE (default $0.15)
    2. Our probability > market price + MIN_EDGE (default 10%)
    3. At least 2+ models agree on the forecast
    4. Kelly sizing gives positive expected value
    
    Exit: Hold to resolution (binary market → $1.00 or $0.00)
    """

    def __init__(self):
        self.max_entry = Config.SNIPER_MAX_ENTRY_PRICE
        self.min_edge = Config.MIN_EDGE_TO_ENTER
        self.kelly_f = Config.KELLY_FRACTION
        self.max_bet_pct = Config.MAX_BET_PCT

    def evaluate(
        self,
        market_title: str,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
        token_ids: Dict[str, str],
        balance: float,
    ) -> List[SniperSignal]:
        """
        Evaluate all buckets for sniper opportunities.
        
        Returns list of SniperSignal sorted by expected value.
        """
        signals = []

        for bp in bucket_probs:
            label = bp.bucket_label
            market_price = market_prices.get(label)
            token_id = token_ids.get(label)

            if market_price is None or token_id is None:
                continue

            # Skip if too expensive
            if market_price > self.max_entry:
                continue

            # Skip if price is basically zero (no liquidity)
            if market_price < 0.005:
                continue

            # Real-chance filter: don't buy ~1% lottery buckets just because they're
            # cheap. Require our model to give the bucket a real shot (tunable).
            if bp.probability < Config.SNIPER_MIN_PROBABILITY:
                continue

            # Calculate edge
            edge = bp.probability - market_price

            # Must exceed minimum edge
            if edge < self.min_edge:
                continue

            # Need at least 2 models agreeing
            if bp.n_models < 2:
                continue

            # Expected return if we win
            expected_return = (1.0 / market_price) - 1.0

            # Kelly criterion sizing
            # f* = (bp - q) / b where b = (1/q - 1), p = our_prob, q = market_price
            b = (1.0 / market_price) - 1.0  # odds
            kelly = (bp.probability * b - (1 - bp.probability)) / b
            kelly = max(0, min(kelly * self.kelly_f, self.max_bet_pct))

            # Size in dollars
            bet_size = kelly * balance
            bet_size = max(Config.MIN_ORDER_SIZE, min(bet_size, balance * self.max_bet_pct))

            # Build reason string
            reason = (
                f"Forecast={bp.mean_forecast:.1f}°C±{bp.std_forecast:.1f} | "
                f"{bp.n_models} models | "
                f"P(us)={bp.probability:.1%} vs market={market_price:.1%} | "
                f"Edge={edge:.1%} | EV={expected_return:.0f}x"
            )

            signals.append(SniperSignal(
                market_title=market_title,
                bucket_label=label,
                token_id=token_id,
                market_price=market_price,
                our_probability=bp.probability,
                edge=edge,
                expected_return=expected_return,
                kelly_size=bet_size,
                confidence=bp.confidence,
                reason=reason,
            ))

        # Sort by edge × confidence (best first)
        signals.sort(key=lambda s: s.edge * s.confidence, reverse=True)
        return signals
