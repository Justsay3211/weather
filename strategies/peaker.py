"""
PEAKER STRATEGY (Req-28 redesign) -- market-anchored, cross-validated daily-peak play.

WHAT CHANGED (and why it kept losing):
The old peaker estimated the peak from the model forecast and shifted it DOWN by
a hot-bias, then bought the bare peak. Buying the market's fairly-priced favorite
at its fair price has ~zero edge (pay 60c for a 60%-likely bucket = break-even
before fees -> a net loser). The user is right: the EDGE is the ANY-ONE-WINS
BASKET, not the lone favorite.

NEW DESIGN (exactly as specified):
  1. ANCHOR ON THE MARKET. Polymarket already prices the winning degree high
     (~60%). We take the MARKET's highest-priced bucket as the estimated peak
     -- "market always prices on the winning market".
  2. CROSS-VALIDATE WITH OUR MODEL. We only act when our model AGREES: our
     model's own peak bucket must sit within PEAKER_ALIGN_BUCKETS of the market
     peak AND our probability for that bucket must not contradict the price
     (our_prob >= price * PEAKER_CONFIRM_RATIO). If the model disagrees, we SKIP
     -- this is the core fix for "always losing" (we stop fighting / rubber-
     stamping the market with no edge).
  3. DIRECTIONAL BASKET = THE EDGE.
       * COOLING trend  -> PEAKER COOL BASKET: peak bucket + the -1C neighbour.
       * WARMING trend   -> PEAKER WARM BASKET: peak bucket + the +1C neighbour.
     Buy BOTH legs in EQUAL SHARES only when the combined per-share cost
     (peak price + neighbour price) is < PEAKER_MAX_COST (default 0.95). The
     buckets are mutually exclusive, so whichever one resolves to $1 covers the
     basket + profit after fees. The basket is grouped + labelled "peaker cool
     basket" / "peaker warm basket" in Telegram, status and /analysis.
  4. SOLO ONLY ON A REAL EDGE. The bare 1-leg peaker fires only when our model
     shows a GENUINE edge over the price (our_prob - price >= PEAKER_SOLO_MIN_EDGE)
     at very high confidence -- otherwise we don't take a no-edge favorite.

GUARANTEES:
  * equal SHARES across legs -> any single winning bucket covers the basket + net,
  * combined per-share basket cost < PEAKER_MAX_COST,
  * every leg clears the dust / sellability floor,
  * HOLD to resolution (the any-one-wins payoff is realised at settlement).

Returning [] (no trade) is the correct output most of the time.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import Config
from logger import log
# Type hints only; import defensively so a rename can never crash startup.
try:
    from data.probability_engine import BucketProbability
except Exception:  # pragma: no cover
    BucketProbability = object  # type: ignore
try:
    from data.stability import StabilityReport
except Exception:  # pragma: no cover
    StabilityReport = object  # type: ignore


# -- dataclasses --

@dataclass
class PeakerLeg:
    bucket_label: str
    token_id: str
    market_price: float        # best_ask we pay (before maker re-price)
    our_probability: float     # ensemble probability for this bucket
    size_usd: float            # allocation
    role: str                  # 'peak' | 'neighbor_warm' | 'neighbor_cool'
    offset: int                # 0 = peak, +1 = warmer, -1 = cooler


@dataclass
class PeakerSignal:
    market_title: str
    city: str
    forecast_max_c: float
    trend: str                 # 'warming' | 'cooling' | 'stable' | 'sideways'
    stability_score: float
    confidence: float
    legs: List[PeakerLeg] = field(default_factory=list)
    total_cost: float = 0.0    # per-share basket cost (sum of leg prices)
    basket_usd: float = 0.0    # total $ deployed across legs
    combined_prob: float = 0.0
    n_models: int = 0
    expected_roi_pct: float = 0.0
    sub_strategy: str = 'peaker'   # 'peaker' | 'peaker_cool_basket' | 'peaker_warm_basket'
    display_label: str = 'peaker'  # human label used in grouped Telegram/status/analysis
    is_basket: bool = False        # True for the grouped cool/warm baskets
    direction: str = 'stable'
    hold_hint: bool = True
    exit_hint: str = ''
    reason: str = ''


# -- helpers --

_NUM_RE = re.compile(r'(-?\d+(?:\.\d+)?)')


def _bucket_numeric_center(label: str, lo: float, hi: float) -> Optional[float]:
    """Best-effort numeric center of a bucket label for adjacency math."""
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


# -- the strategy --

class PeakerStrategy:
    """Market-anchored, cross-validated peak play. Prefers the directional
    any-one-wins basket (cool/warm); takes the bare peak only on a real model
    edge. Calibrated to the proven cool-neighbour win."""

    name = "peaker"
    description = (
        "Anchor on the market's highest-priced (winning) bucket, cross-validate "
        "with our model, then buy the directional any-one-wins basket (peak + "
        "cool/warm neighbour, combined < 95c). Bare peak only on a genuine edge. "
        "Equal shares, holds to resolution."
    )

    def __init__(self):
        self._load_cfg()

    def _load_cfg(self):
        g = lambda n, d: getattr(Config, n, d)
        self.enabled = bool(g('PEAKER_ENABLED', 1))
        self.min_grade = float(g('PEAKER_MIN_GRADE', 0.55))
        self.min_models = int(g('PEAKER_MIN_MODELS', 3))
        self.max_std = float(g('PEAKER_MAX_STD', 1.6))
        self.min_conf = float(g('PEAKER_MIN_CONFIDENCE', 0.55))
        # market anchor gates
        self.market_min_price = float(g('PEAKER_MARKET_MIN_PRICE', 0.40))
        self.max_peak_price = float(g('PEAKER_MAX_PEAK_PRICE', 0.90))
        self.max_nb_price = float(g('PEAKER_MAX_NEIGHBOR_PRICE', 0.70))
        # cross-validation gates
        self.align_buckets = int(g('PEAKER_ALIGN_BUCKETS', 1))
        self.confirm_ratio = float(g('PEAKER_CONFIRM_RATIO', 0.85))
        # basket economics
        self.max_cost = float(g('PEAKER_MAX_COST', 0.95))
        self.fee_buffer = float(g('PEAKER_FEE_BUFFER', 0.02))
        self.min_net = float(g('PEAKER_MIN_NET_PROFIT', 0.03))
        self.min_edge = float(g('PEAKER_MIN_EDGE', 0.03))
        # solo gates (a bare favorite needs a GENUINE model edge)
        self.solo_min_edge = float(g('PEAKER_SOLO_MIN_EDGE', 0.08))
        self.solo_min_conf = float(g('PEAKER_SOLO_MIN_CONFIDENCE', 0.75))
        # sizing
        self.base_fraction = float(g('PEAKER_BASE_FRACTION', 0.05))
        self.max_fraction = float(g('PEAKER_MAX_FRACTION', 0.20))
        self.max_usd = float(g('PEAKER_MAX_USD', 15.0))
        self.prefer_cool = bool(g('PEAKER_PREFER_COOL', 1))
        self.cool_size_mult = float(g('PEAKER_COOL_SIZE_MULT', 1.35))
        self.cool_edge_relax = float(g('PEAKER_COOL_EDGE_RELAX', 0.02))
        self.warm_size_mult = float(g('PEAKER_WARM_SIZE_MULT', 0.80))
        self.min_entry = float(g('MIN_ENTRY_PRICE', 0.02))
        self.min_order = float(g('MIN_ORDER_SIZE', 1.0))

    def evaluate(
        self,
        market_title: str,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
        token_ids: Dict[str, str],
        balance: float,
        city: str,
        stability: Optional[StabilityReport] = None,
        grade: float = 0.60,
    ) -> List[PeakerSignal]:
        self._load_cfg()  # pick up live /settings overrides
        if not self.enabled or not bucket_probs or balance <= 0:
            return []

        # -- gate 1: stability / grade --
        if stability is None:
            log.debug(f"Peaker {city}: no stability report -- patient skip")
            return []
        eff_grade = min(grade, stability.score)
        if not stability.predictable and stability.score < self.min_grade:
            log.debug(f"Peaker {city}: grade {stability.score:.2f} < {self.min_grade} -- skip")
            return []
        if eff_grade < self.min_grade:
            log.debug(f"Peaker {city}: eff grade {eff_grade:.2f} < {self.min_grade} -- skip")
            return []

        # -- gate 2: enough agreeing models --
        n_models = max(bp.n_models for bp in bucket_probs)
        if n_models < self.min_models:
            log.debug(f"Peaker {city}: {n_models} models < {self.min_models} -- skip")
            return []

        # -- gate 3: tight ensemble spread --
        ens_std = min((bp.std_forecast for bp in bucket_probs), default=999)
        if ens_std > self.max_std:
            log.debug(f"Peaker {city}: std {ens_std:.2f}C > {self.max_std} -- skip")
            return []

        # -- index buckets by numeric center; carry price + prob + token + conf --
        indexed: List[Tuple[float, BucketProbability, float]] = []
        for bp in bucket_probs:
            c = _bucket_numeric_center(bp.bucket_label, bp.bucket_low, bp.bucket_high)
            price = market_prices.get(bp.bucket_label)
            if c is None or price is None:
                continue
            indexed.append((c, bp, float(price or 0.0)))
        if len(indexed) < 1:
            return []
        indexed.sort(key=lambda x: x[0])

        # -- ANCHOR ON THE MARKET: the highest-priced bucket is the market's
        #    "winning degree" estimate ("market always prices on winning market") --
        market_i = max(range(len(indexed)), key=lambda i: indexed[i][2])
        market_center, market_bp, market_price = indexed[market_i]

        # -- CROSS-VALIDATE WITH OUR MODEL --
        # (a) the market peak must actually be a high-probability bucket
        if market_price < self.market_min_price:
            log.debug(f"Peaker {city}: market peak price {market_price:.2f} < {self.market_min_price} -- not a confident market, skip")
            return []
        if market_price > self.max_peak_price:
            log.debug(f"Peaker {city}: market peak price {market_price:.2f} > {self.max_peak_price} -- too rich, skip")
            return []
        # (b) our model's own peak bucket must be within align_buckets of it
        model_i = max(range(len(indexed)), key=lambda i: indexed[i][1].probability)
        if abs(model_i - market_i) > self.align_buckets:
            log.debug(f"Peaker {city}: model peak {model_i} not aligned with market peak {market_i} -- disagree, skip")
            return []
        # (c) our probability for the market peak must not contradict the price
        peak_our_prob = float(getattr(market_bp, 'probability', 0.0) or 0.0)
        if peak_our_prob < market_price * self.confirm_ratio:
            log.debug(f"Peaker {city}: our prob {peak_our_prob:.2f} < {self.confirm_ratio:.2f} x price {market_price:.2f} -- model not confirming, skip")
            return []
        # (d) peak-bucket confidence floor
        peak_conf = float(getattr(market_bp, 'confidence', 0.0) or 0.0)
        if peak_conf < self.min_conf:
            log.debug(f"Peaker {city}: peak conf {peak_conf:.2f} < {self.min_conf} -- skip")
            return []

        center_i = market_i
        center_bp = market_bp

        # -- decide the shape from the trend --
        trend = (getattr(stability, 'trend', None) or 'unknown').lower()
        has_warm = (center_i + 1) < len(indexed)
        has_cool = (center_i - 1) >= 0

        peak_token = token_ids.get(center_bp.bucket_label)
        if not peak_token or market_price < self.min_entry:
            log.debug(f"Peaker {city}: peak bucket has no token / below entry floor -- skip")
            return []

        def _neighbor(direction: int):
            ni = center_i + direction
            if ni < 0 or ni >= len(indexed):
                return None
            _, nbp, nprice = indexed[ni]
            ntok = token_ids.get(nbp.bucket_label)
            if not ntok or nprice <= 0 or nprice < self.min_entry:
                return None
            if nprice > self.max_nb_price:
                return None
            return (ni, nbp, nprice, ntok)

        # Choose direction: cooling -> cool basket; warming -> warm basket;
        # stable/ambiguous -> prefer cool (the proven winner) if it fits.
        chosen_dir = 0
        sub = 'peaker'
        display = 'peaker'
        neighbor = None
        if trend == 'cooling' and has_cool:
            nb = _neighbor(-1)
            if nb and (market_price + nb[2]) < self.max_cost:
                chosen_dir, sub, display, neighbor = -1, 'peaker_cool_basket', 'peaker cool basket', nb
        elif trend == 'warming' and has_warm:
            nb = _neighbor(+1)
            if nb and (market_price + nb[2]) < self.max_cost:
                chosen_dir, sub, display, neighbor = +1, 'peaker_warm_basket', 'peaker warm basket', nb

        if chosen_dir == 0 and self.prefer_cool and has_cool:
            nb = _neighbor(-1)
            if nb and (market_price + nb[2]) < self.max_cost:
                chosen_dir, sub, display, neighbor = -1, 'peaker_cool_basket', 'peaker cool basket', nb

        # -- assemble legs --
        legs_src: List[Tuple[BucketProbability, float, str, str, int]] = [
            (center_bp, market_price, peak_token, 'peak', 0)
        ]
        if neighbor is not None:
            _, nbp, nprice, ntok = neighbor
            role = 'neighbor_cool' if chosen_dir < 0 else 'neighbor_warm'
            legs_src.append((nbp, nprice, ntok, role, chosen_dir))

        is_basket = len(legs_src) > 1

        # -- SOLO requires a GENUINE model edge (no no-edge favorites) --
        if not is_basket:
            solo_edge = peak_our_prob - market_price
            if solo_edge < self.solo_min_edge or peak_conf < self.solo_min_conf:
                log.debug(f"Peaker {city}: solo edge {solo_edge:+.2f} / conf {peak_conf:.2f} "
                          f"below solo gate ({self.solo_min_edge}, {self.solo_min_conf}) -- skip")
                return []
            sub, display = 'peaker', 'peaker'

        # -- per-share basket cost + fee-aware floor --
        total_cost = sum(p for _, p, _, _, _ in legs_src)
        if total_cost <= 0:
            return []
        max_basket_cost = min(self.max_cost, 1.0 - (self.fee_buffer + self.min_net))
        if is_basket and total_cost >= max_basket_cost:
            log.debug(f"Peaker {city}: basket cost ${total_cost:.2f}/sh >= ${max_basket_cost:.2f} -- skip")
            return []

        # -- combined probability / edge (cool side gets a small relaxed gate) --
        combined_prob = min(0.99, sum(float(bp.probability) for bp, _, _, _, _ in legs_src))
        edge = combined_prob - total_cost
        eff_min_edge = self.min_edge
        if sub == 'peaker_cool_basket':
            eff_min_edge = max(0.0, self.min_edge - self.cool_edge_relax)
        if is_basket and edge < eff_min_edge:
            log.debug(f"Peaker {city}: basket edge {edge:+.1%} < {eff_min_edge:.1%} -- skip")
            return []

        # -- confidence-scaled sizing with cool/warm multipliers --
        conf_span = max(0.0, min(1.0, (peak_conf - self.min_conf) / max(0.01, 1.0 - self.min_conf)))
        frac = self.base_fraction + (self.max_fraction - self.base_fraction) * conf_span
        frac = max(self.base_fraction, min(self.max_fraction, frac))
        size_mult = 1.0
        if sub == 'peaker_cool_basket':
            size_mult = self.cool_size_mult
        elif sub == 'peaker_warm_basket':
            size_mult = self.warm_size_mult
        basket_usd = balance * frac * size_mult
        basket_usd = min(basket_usd, self.max_usd, balance * self.max_fraction)
        basket_usd = max(self.min_order * len(legs_src), basket_usd)

        # -- equal SHARES across legs --
        cost_per_share = total_cost
        target_shares = basket_usd / cost_per_share if cost_per_share > 0 else 0.0
        max_price = max(p for _, p, _, _, _ in legs_src)
        min_shares_for_floor = self.min_order / max_price if max_price > 0 else 0.0
        shares = max(min_shares_for_floor, target_shares)

        legs: List[PeakerLeg] = []
        for bp, price, tok, role, offset in legs_src:
            leg_usd = max(self.min_order, round(shares * price, 2))
            legs.append(PeakerLeg(
                bucket_label=bp.bucket_label,
                token_id=tok,
                market_price=price,
                our_probability=float(bp.probability),
                size_usd=leg_usd,
                role=role,
                offset=offset,
            ))

        total_deployed = round(sum(l.size_usd for l in legs), 2)
        expected_roi_pct = ((1.0 - cost_per_share) / cost_per_share * 100.0) if cost_per_share > 0 else 0.0

        peak_label = next(l.bucket_label for l in legs if l.role == 'peak')
        nb_legs = [l for l in legs if l.role != 'peak']
        if not nb_legs:
            shape = f'peaker solo (peak {peak_label} @ {market_price:.2f}, edge {peak_our_prob - market_price:+.0%})'
        elif nb_legs[0].role == 'neighbor_cool':
            shape = f'peaker COOL basket (peak {peak_label} + cooler {nb_legs[0].bucket_label})'
        else:
            shape = f'peaker WARM basket (peak {peak_label} + warmer {nb_legs[0].bucket_label})'

        exit_hint = "HOLD -- market-confirmed peaker, let it resolve"
        reason = (
            f"PEAKER {city} [{sub}] trend={trend} grade={eff_grade:.2f} conf={peak_conf:.0%} "
            f"mkt_peak@{market_price:.2f} our_p={peak_our_prob:.0%} | {shape} | "
            f"{len(legs)}legs ${total_deployed:.2f} cost${total_cost:.2f}/sh "
            f"Pwin={combined_prob:.0%} edge={edge:+.0%} roi~{expected_roi_pct:.0f}%"
        )
        log.info(f"   > {reason}")

        return [PeakerSignal(
            market_title=market_title,
            city=city,
            forecast_max_c=getattr(stability, 'forecast_max_c', market_center),
            trend=trend,
            stability_score=eff_grade,
            confidence=peak_conf,
            legs=legs,
            total_cost=round(total_cost, 4),
            basket_usd=total_deployed,
            combined_prob=combined_prob,
            n_models=n_models,
            expected_roi_pct=expected_roi_pct,
            sub_strategy=sub,
            display_label=display,
            is_basket=is_basket,
            direction=('warming' if chosen_dir > 0 else 'cooling' if chosen_dir < 0 else 'stable'),
            hold_hint=True,
            exit_hint=exit_hint,
            reason=reason,
        )]
