"""
SELECTIVE SNIPER v2.0 — Built on 14M real trade calibration data.

THE REALITY (from SII-WANGZJ dataset, 14,698,335 weather trades):
  Every price tier shows NEGATIVE unconditional edge. The market systematically
  overprices weather outcomes. Buying all cheap tails is a LOSING strategy.

THE SOLUTION — SELECTIVE ENTRY:
  Only trade when our station-aware ensemble model's probability estimate is
  SIGNIFICANTLY above the market price. The edge comes from superior forecasting
  (airport station coordinates + multi-model ensemble), not from market inefficiency.

  Entry requirements:
    1. Edge >= 10 percentage points (our_prob - market_price >= 0.10)
    2. Edge RATIO >= 3.0x (our_prob / market_price >= 3.0)
    3. Minimum 3 models in agreement
    4. Station-aware forecast only (airport coordinates, not city center)
    5. Entry at best_bid (maker, 0% fee)
    6. Hold to binary resolution (NO mid-market selling)

  Kelly sizing:
    f = (our_prob - market_price) / (1 - market_price) * confidence * 0.25
    Conservative: 25% of full Kelly to survive variance

  Expected performance (if model is 3x better than market at the tails):
    Entry at 2c, model says 8% real → 5% vs 1% baseline
    Per $1 staked: EV = 0.05 * $50 - 0.95 * $1 = +$1.55 = +155% ROI per trade
    This compounds rapidly at 10-20 carefully selected trades per week.
"""

from typing import Dict, List, Optional

from config import Config
from data.weather_stations import get_station


class SelectiveSniperStrategy:
    """
    Ultra-selective weather sniper — only enters when the edge is overwhelming.

    Inspired by Wallet1 (0x594e..., $58K realized): buys cheap tails where
    ensemble models show 3-10x higher probability than the market price.
    """

    name = "selective_sniper"
    description = (
        "DATA-CALIBRATED: 14M-trade analysis shows only SELECTIVE entry works. "
        "Requires edge >= 10pp AND edge ratio >= 3x. Station-aware only. "
        "Maker-first at best_bid. Hold to resolution. Kelly-sized."
    )
    # ── ENTRY THRESHOLDS (calibrated from real 14M-trade data) ──
    MIN_EDGE_PCT = 0.10        # minimum 10 percentage points edge
    MIN_EDGE_RATIO = 3.0       # our_prob / market_price >= 3x
    MIN_MODELS = 3             # at least 3 weather models agreeing
    MIN_CONFIDENCE = 0.60      # ensemble confidence threshold
    MAX_ENTRY_PRICE = 0.15     # only trade cheap tails (Wallet1: 91% < $0.10)
    MIN_ENTRY_PRICE = 0.005    # avoid ultra-thin markets

    # ── KELLY SIZING ──
    KELLY_MULTIPLIER = 0.15    # conservative: 15% of full Kelly

    # ── SPREAD AWARENESS ──
    MAX_SPREAD_BPS = 1000      # max 10% spread on entry

    def evaluate(
        self,
        market_title: str,
        bucket_probs: list,     # List[BucketProbability]
        market_prices: dict,    # {label: best_ask_price}
        market_bids: dict,      # {label: best_bid_price}
        token_ids: dict,        # {label: token_id}
        balance: float,
        city: str = "",
        spread_bps_map: dict = None,  # {label: spread_bps}
    ) -> list:
        """
        Find high-selectivity sniper opportunities.

        Returns list of SniperSignals (one per qualifying bucket).
        """
        signals = []

        # Station check — must have airport station
        station = get_station(city) if city else None
        if not station:
            return []  # no station = no edge (can't beat the market)

        for bp in bucket_probs:
            label = bp.bucket_label
            our_prob = bp.probability
            confidence = bp.confidence
            n_models = bp.n_models
            mean_temp = bp.mean_forecast

            # ── 1. Model agreement ──
            if n_models < self.MIN_MODELS:
                continue
            if confidence < self.MIN_CONFIDENCE:
                continue

            # ── 2. Price checks ──
            market_price = market_prices.get(label, 0.99)
            market_bid = market_bids.get(label, market_price)
            token_id = token_ids.get(label)

            if not token_id:
                continue
            if market_price < self.MIN_ENTRY_PRICE:
                continue
            if market_price > self.MAX_ENTRY_PRICE:
                continue

            # ── 3. EDGE CHECK (the critical filter) ──
            edge = our_prob - market_price
            if edge < self.MIN_EDGE_PCT:
                continue  # not enough absolute edge

            # Edge ratio: how many times our prob exceeds market prob
            edge_ratio = our_prob / max(market_price, 0.005)
            if edge_ratio < self.MIN_EDGE_RATIO:
                continue  # our model doesn't disagree enough

            # ── 4. Spread check ──
            sp_bps = spread_bps_map.get(label, 500) if spread_bps_map else 500
            if sp_bps > self.MAX_SPREAD_BPS:
                continue

            # ── 5. Kelly sizing ──
            # f* = edge / (1 - market_price)  →  full Kelly
            # f = f* * KELLY_MULTIPLIER
            kelly_full = edge / (1.0 - market_price) if market_price < 1.0 else 0
            kelly_fraction = kelly_full * self.KELLY_MULTIPLIER * (confidence / 0.80)
            kelly_fraction = max(0.005, min(0.25, kelly_fraction))  # cap 0.5% to 25%

            size_usd = balance * kelly_fraction
            if size_usd < Config.MIN_ORDER_SIZE:
                continue

            # ── 6. Entry at best_bid (maker, 0% fee) ──
            entry_price = market_bid if market_bid > 0 else market_price
            shares = size_usd / entry_price if entry_price > 0 else 0

            # Expected value per $1 staked
            ev_per_dollar = (our_prob * (1.0 / entry_price - 1.0) -
                             (1.0 - our_prob) * 1.0)

            signal = SniperSignal(
                market_title=market_title,
                bucket_label=label,
                token_id=token_id,
                market_price=market_price,
                market_bid=market_bid,
                our_probability=our_prob,
                edge=edge,
                edge_ratio=edge_ratio,
                entry_price=entry_price,
                size_usd=size_usd,
                shares=shares,
                kelly_size=size_usd,
                expected_return=ev_per_dollar,
                confidence=confidence,
                mean_forecast=mean_temp,
                n_models=n_models,
                station_icao=station.icao,
                reason=(
                    f"SEL.SNIPE: {city} {label} | "
                    f"our={our_prob:.0%} vs mkt={market_price:.1%} "
                    f"(edge={edge:.0%}, ratio={edge_ratio:.1f}x) | "
                    f"entry={entry_price:.3f} maker | "
                    f"size=${size_usd:.2f} ({kelly_fraction:.0%} Kelly) | "
                    f"models={n_models} | {station.icao}"
                ),
            )
            signals.append(signal)

        # Sort by edge_ratio * confidence (best opportunities first)
        signals.sort(key=lambda s: s.edge_ratio * s.confidence, reverse=True)
        return signals


from dataclasses import dataclass


@dataclass
class SniperSignal:
    market_title: str
    bucket_label: str
    token_id: str
    market_price: float
    market_bid: float
    our_probability: float
    edge: float
    edge_ratio: float
    entry_price: float
    size_usd: float
    shares: float
    kelly_size: float
    expected_return: float
    confidence: float
    mean_forecast: float
    n_models: int
    station_icao: str
    reason: str
