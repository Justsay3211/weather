"""
Confident Predictor Strategy — Buy the MOST LIKELY bucket early.

Backtest proven: 45% WR, +410% ROI (best overall PnL).

Logic:
- Our multi-model forecast points to ONE bucket with >35% probability
- The market hasn't updated yet (price is still < 0.50)
- We buy THAT bucket because we're more confident than the market
- Hold to resolution → $1.00 payout on win

This is DIFFERENT from sniper (which buys cheap tails).
This buys the EXPECTED outcome when it's still underpriced.

Example:
- Forecast says Tokyo high will be 27°C (5 models agree)
- Market has "27°C" priced at $0.25 (market thinks 25% chance)
- We think it's 42% likely → edge = 17%
- Buy at $0.25, if correct → $1.00 = 300% return

NO STOP-LOSS for this strategy — weather is binary,
either our forecast is right or wrong. No point selling
at $0.15 if we bought at $0.25.
"""

from typing import Dict, List
from dataclasses import dataclass

from config import Config
from data.probability_engine import BucketProbability
from logger import log


@dataclass
class ConfidentSignal:
    """A confident predictor signal."""
    market_title: str
    bucket_label: str
    token_id: str
    market_price: float
    our_probability: float
    edge: float
    expected_return: float
    size_usd: float
    confidence: float
    forecast_temp: float
    n_models: int
    reason: str


class ConfidentStrategy:
    """
    Buy the most-likely outcome bucket when market is still cheap.
    
    Entry criteria:
    1. Our probability for the bucket > 35%
    2. Market price < 50% (room for profit)
    3. Edge > 5% (our_prob - market_price)
    4. At least 3 models agree
    
    Exit: HOLD TO RESOLUTION. No stop-loss.
    This is a high-conviction play — we believe our forecast.
    """

    def __init__(self):
        self.min_our_prob = 0.35
        self.max_market_price = 0.50
        self.min_edge = 0.05
        self.min_models = 3

    def evaluate(
        self,
        market_title: str,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
        token_ids: Dict[str, str],
        balance: float,
    ) -> List[ConfidentSignal]:
        """Find the most-likely bucket that's still underpriced."""
        signals = []

        if not bucket_probs:
            return signals

        # Find the bucket with highest probability
        best_bp = max(bucket_probs, key=lambda b: b.probability)

        # Check criteria
        market_price = market_prices.get(best_bp.bucket_label)
        token_id = token_ids.get(best_bp.bucket_label)

        if market_price is None or token_id is None:
            return signals

        if best_bp.probability < self.min_our_prob:
            return signals

        if market_price > self.max_market_price:
            return signals

        if market_price < 0.01:
            return signals

        edge = best_bp.probability - market_price
        if edge < self.min_edge:
            return signals

        if best_bp.n_models < self.min_models:
            return signals

        # Sizing: more aggressive than sniper (higher confidence)
        # Kelly: f* = edge / (1/market_price - 1)
        b = (1.0 / market_price) - 1.0
        kelly = (best_bp.probability * b - (1 - best_bp.probability)) / b
        kelly = max(0, min(kelly * 0.25, 0.30))  # up to 30% of balance
        size = max(Config.MIN_ORDER_SIZE, kelly * balance)
        size = min(size, balance * 0.30)

        expected_return = (1.0 / market_price) - 1.0

        reason = (
            f"MOST LIKELY: forecast={best_bp.mean_forecast:.1f}°C | "
            f"{best_bp.n_models} models agree | "
            f"P(us)={best_bp.probability:.0%} vs market={market_price:.0%} | "
            f"Edge={edge:.0%}"
        )

        signals.append(ConfidentSignal(
            market_title=market_title,
            bucket_label=best_bp.bucket_label,
            token_id=token_id,
            market_price=market_price,
            our_probability=best_bp.probability,
            edge=edge,
            expected_return=expected_return,
            size_usd=size,
            confidence=best_bp.confidence,
            forecast_temp=best_bp.mean_forecast,
            n_models=best_bp.n_models,
            reason=reason,
        ))

        # Also check 2nd most likely if it has good edge
        sorted_probs = sorted(bucket_probs, key=lambda b: b.probability, reverse=True)
        if len(sorted_probs) > 1:
            second = sorted_probs[1]
            mp2 = market_prices.get(second.bucket_label)
            tid2 = token_ids.get(second.bucket_label)
            if mp2 and tid2 and second.probability > 0.25 and mp2 < 0.35:
                edge2 = second.probability - mp2
                if edge2 > 0.05 and second.n_models >= 3:
                    size2 = max(Config.MIN_ORDER_SIZE, size * 0.4)
                    signals.append(ConfidentSignal(
                        market_title=market_title,
                        bucket_label=second.bucket_label,
                        token_id=tid2,
                        market_price=mp2,
                        our_probability=second.probability,
                        edge=edge2,
                        expected_return=(1.0 / mp2) - 1.0,
                        size_usd=size2,
                        confidence=second.confidence,
                        forecast_temp=second.mean_forecast,
                        n_models=second.n_models,
                        reason=f"2nd MOST LIKELY: {second.probability:.0%} vs {mp2:.0%}",
                    ))

        return signals
