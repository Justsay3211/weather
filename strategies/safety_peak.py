"""
Safety Peak Strategy — FOCUSED high-confidence 1-2 bucket directional peak.

THE EDGE (Req-25):
This is the "buy for safety" play the user described: when the peaker has
estimated the daily peak temperature ACCURATELY and with HIGH confidence (tight
ensemble, many agreeing models, stable/predictable grade), we take a focused,
patient position on just the peak bucket PLUS ONE best-guess neighbour for
safety. If either of the two buckets lands, the winner covers the loser's loss
AND nets a profit after fees.

HOW IT DIFFERS FROM THE OTHER PEAK STRATEGIES:
- peak_cluster : wide "any-one-wins" basket, DYNAMIC 3-7 neighbouring buckets.
- peak_basket  : directional/symmetric basket (peak + 1, or both shoulders when
                 flat) — runs on a looser stability gate.
- safety_peak  : THIS one. The TIGHTEST, most patient play — exactly 1-2 legs
                 (peak + a single directional neighbour), only when confidence
                 is HIGH. Waits for the good signal instead of trading often.

WHAT THIS STRATEGY DOES:
1. Requires a HIGH-confidence, accurate peak estimate:
   - stability grade >= SAFETY_PEAK_MIN_GRADE,
   - >= SAFETY_PEAK_MIN_MODELS agreeing models,
   - ensemble spread (std) <= SAFETY_PEAK_MAX_STD (tight = accurate),
   - peak-bucket confidence >= SAFETY_PEAK_MIN_CONFIDENCE.
   If any fails, it stays patient and does NOTHING (waits for opportunities).
2. Buys the estimated peak bucket, plus ONE safety neighbour:
   - warming trend  -> upper neighbour (+1°C),
   - cooling trend  -> lower neighbour (-1°C),
   - stable/flat    -> the single shoulder the ensemble mean leans toward
     (mean above bucket center -> upper, else lower). Stays at 2 legs (focused).
3. Buys EQUAL SHARES on both legs, so whichever single bucket resolves to $1
   pays the same — the winner ALWAYS covers the other leg's loss + net profit.
4. Per-share basket cost must leave room for profit AFTER fees:
   cost <= 1 - SAFETY_PEAK_FEE_BUFFER - SAFETY_PEAK_MIN_NET_PROFIT.
5. Every leg must clear the sellability/dust floor; hold to resolution.
6. Scales capital modestly with confidence; never > SAFETY_PEAK_MAX_FRACTION of
   balance and never > SAFETY_PEAK_MAX_USD per basket.

DESIGN RULES:
- Never buy a leg below MIN_ENTRY_PRICE — no bid exists, instant loss.
- Never buy when the peak is already priced > SAFETY_PEAK_MAX_PEAK_PRICE.
- Always hold to resolution — thin books make early exits losing.
- Patient by design: a no-trade is the correct output most of the time.
"""

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from config import Config
from logger import log
from data.probability_engine import BucketProbability
from data.stability import StabilityReport


# ── dataclasses ──

@dataclass
class SafetyPeakLeg:
    """A single leg (bucket) in a safety-peak position."""
    bucket_label: str
    token_id: str
    market_price: float        # best_ask (what we pay before maker re-price)
    our_probability: float     # ensemble probability for this bucket
    size_usd: float            # how much to allocate
    role: str                  # 'peak' | 'neighbor_warm' | 'neighbor_cool'
    offset: int                # 0 = peak, +1/-1 = neighbor


@dataclass
class SafetyPeakSignal:
    """A complete safety-peak trading signal."""
    market_title: str
    city: str
    forecast_max_c: float      # ensemble forecast daily max
    trend: str                 # 'warming' | 'cooling' | 'stable' | 'sideways'
    stability_score: float     # 0-1 grade
    confidence: float          # peak-bucket confidence (0-1)
    legs: List[SafetyPeakLeg] = field(default_factory=list)
    total_cost: float = 0.0    # per-share basket cost (sum of leg prices)
    basket_usd: float = 0.0    # total $ deployed across legs
    combined_prob: float = 0.0    # P(any leg wins)
    n_models: int = 0
    expected_roi_pct: float = 0.0
    direction: str = 'peak_only'  # 'warming' | 'cooling' | 'stable_lean' | 'peak_only'
    hold_hint: bool = True
    exit_hint: str = ''
    reason: str = ''


# ── helpers ──

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


# ── the strategy ──

class SafetyPeakStrategy:
    """
    Focused high-confidence peak + one safety neighbour.

    Decision chain:
      HIGH-confidence gate (grade + models + tight spread + bucket confidence)
      → find peak → pick ONE directional safety neighbour → price/edge checks
      → equal-shares fee-aware sizing → HOLD to resolution.

    Patient by design: returns [] (no trade) unless the signal is strong.
    """

    name = "safety_peak"
    description = (
        "High-confidence focused peak: buy the accurately-estimated peak bucket "
        "plus ONE directional safety neighbour in equal shares, so any single "
        "winner covers the other leg + profit after fees. Patient; hold to resolution."
    )

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
    ) -> List[SafetyPeakSignal]:
        """
        Evaluate a single weather market and produce zero or one SafetyPeakSignal.
        Returns a list so it slots cleanly into the dashboard loop; at most one.
        """
        if not bucket_probs:
            return []

        min_grade = float(getattr(Config, 'SAFETY_PEAK_MIN_GRADE', 0.65))
        min_models = int(getattr(Config, 'SAFETY_PEAK_MIN_MODELS', 3))
        max_std = float(getattr(Config, 'SAFETY_PEAK_MAX_STD', 1.2))
        min_conf = float(getattr(Config, 'SAFETY_PEAK_MIN_CONFIDENCE', 0.70))

        # ── gate 1: stability / grade (accurate, predictable day) ──
        if stability is None:
            log.debug(f"SafetyPeak {city}: no stability report -- patient skip")
            return []
        eff_grade = min(grade, stability.score)
        if not stability.predictable and stability.score < min_grade:
            log.debug(f"SafetyPeak {city}: grade {stability.score:.2f} < {min_grade} -- patient skip")
            return []
        if eff_grade < min_grade:
            log.debug(f"SafetyPeak {city}: eff grade {eff_grade:.2f} < {min_grade} -- patient skip")
            return []

        # ── gate 2: enough agreeing models ──
        n_models = max(bp.n_models for bp in bucket_probs)
        if n_models < min_models:
            log.debug(f"SafetyPeak {city}: {n_models} models < {min_models} -- patient skip")
            return []

        # ── gate 3: tight ensemble spread (accurate peak estimate) ──
        peak_std = max((bp.std_forecast for bp in bucket_probs), default=999)
        # std is shared across buckets; use the min (any) as the ensemble spread.
        ens_std = min((bp.std_forecast for bp in bucket_probs), default=999)
        if ens_std > max_std:
            log.debug(f"SafetyPeak {city}: ensemble std {ens_std:.2f}°C > {max_std} -- not tight enough")
            return []

        # ── index buckets by numeric center for adjacency math ──
        indexed: List[Tuple[float, BucketProbability]] = []
        for bp in bucket_probs:
            c = _bucket_numeric_center(bp.bucket_label, bp.bucket_low, bp.bucket_high)
            if c is not None:
                indexed.append((c, bp))
        if not indexed:
            return []
        indexed.sort(key=lambda x: x[0])

        # ── find the peak bucket (closest to ensemble forecast max) ──
        target = stability.forecast_max_c
        center_i = min(range(len(indexed)), key=lambda i: abs(indexed[i][0] - target))
        center_val, center_bp = indexed[center_i]

        # ── gate 4: peak-bucket confidence must be HIGH ──
        peak_conf = float(getattr(center_bp, 'confidence', 0.0) or 0.0)
        if peak_conf < min_conf:
            log.debug(f"SafetyPeak {city}: peak confidence {peak_conf:.2f} < {min_conf} -- patient skip")
            return []

        # ── choose exactly ONE safety neighbour (focused 1-2 leg play) ──
        trend = (stability.trend or 'unknown').lower()
        if trend == 'warming':
            direction = +1
            direction_label = 'warming'
        elif trend == 'cooling':
            direction = -1
            direction_label = 'cooling'
        else:
            # stable/flat: lean toward the side the ensemble mean sits on relative
            # to the peak-bucket center, so the single neighbour covers the more
            # likely miss. Stays at 2 legs (focused), unlike peak_basket symmetric.
            mean_fc = float(getattr(center_bp, 'mean_forecast', target) or target)
            direction = +1 if mean_fc >= center_val else -1
            direction_label = 'stable_lean'

        # ── build legs: peak always, plus the single chosen neighbour ──
        candidates: List[Tuple[int, BucketProbability, str]] = [(center_i, center_bp, 'peak')]
        ni = center_i + direction
        if 0 <= ni < len(indexed):
            _, neighbor_bp = indexed[ni]
            role = 'neighbor_warm' if direction > 0 else 'neighbor_cool'
            candidates.append((ni, neighbor_bp, role))

        # ── price & floor checks ──
        max_peak_price = float(getattr(Config, 'SAFETY_PEAK_MAX_PEAK_PRICE', 0.85))
        max_nb_price = float(getattr(Config, 'SAFETY_PEAK_MAX_NEIGHBOR_PRICE', 0.60))
        kept: List[Tuple[int, BucketProbability, str, float, str]] = []
        for idx, bp, role in candidates:
            price = market_prices.get(bp.bucket_label)
            tid = token_ids.get(bp.bucket_label)
            if price is None or tid is None or price <= 0:
                continue
            if price < Config.MIN_ENTRY_PRICE:
                log.debug(f"   SKIP SAFETY FLOOR {city}:{bp.bucket_label} ${price:.4f} < ${Config.MIN_ENTRY_PRICE:.2f}")
                continue
            if role == 'peak' and price > max_peak_price:
                log.debug(f"   SKIP SAFETY PEAK PRICED {city}:{bp.bucket_label} ${price:.2f} > ${max_peak_price:.2f}")
                continue
            if role != 'peak' and price > max_nb_price:
                log.debug(f"   SKIP SAFETY NEIGHBOR PRICED {city}:{bp.bucket_label} ${price:.2f} > ${max_nb_price:.2f}")
                continue
            kept.append((idx, bp, role, price, tid))

        # Must have at least the peak; a lone peak is allowed (1-leg) only if it
        # already clears the fee-aware profit floor on its own.
        if not any(role == 'peak' for _, _, role, _, _ in kept):
            log.debug(f"   SKIP SAFETY {city}: peak bucket failed checks")
            return []

        # ── per-share basket cost & fee-aware profit floor (any-one-wins) ──
        total_market_cost = sum(price for _, _, _, price, _ in kept)
        if total_market_cost <= 0:
            return []
        fee_buffer = float(getattr(Config, 'SAFETY_PEAK_FEE_BUFFER', 0.02))
        min_net = float(getattr(Config, 'SAFETY_PEAK_MIN_NET_PROFIT', 0.05))
        max_basket_cost = 1.0 - (fee_buffer + min_net)
        if total_market_cost >= max_basket_cost:
            log.debug(f"   SKIP SAFETY COST {city}: ${total_market_cost:.2f}/share >= "
                      f"${max_basket_cost:.2f} (winner can't cover basket + {min_net:.0%} net + {fee_buffer:.0%} fees)")
            return []

        # ── combined probability / edge ──
        combined_prob = min(0.99, sum(bp.probability for _, bp, _, _, _ in kept))
        basket_edge = combined_prob - total_market_cost
        min_edge = float(getattr(Config, 'SAFETY_PEAK_MIN_EDGE', 0.05))
        if basket_edge < min_edge:
            log.debug(f"   SKIP SAFETY EDGE {city}: edge {basket_edge:.1%} < {min_edge:.1%} "
                      f"(Pwin={combined_prob:.0%} cost=${total_market_cost:.2f})")
            return []

        # ── confidence-scaled sizing (modest, patient) ──
        base_fraction = float(getattr(Config, 'SAFETY_PEAK_BASE_FRACTION', 0.05))
        max_fraction = float(getattr(Config, 'SAFETY_PEAK_MAX_FRACTION', 0.20))
        max_usd = float(getattr(Config, 'SAFETY_PEAK_MAX_USD', 15.0))
        # Confidence above the gate scales size from base toward max linearly.
        conf_span = max(0.0, min(1.0, (peak_conf - min_conf) / max(0.01, 1.0 - min_conf)))
        frac = base_fraction + (max_fraction - base_fraction) * conf_span
        frac = max(base_fraction, min(max_fraction, frac))
        basket_usd = balance * frac
        basket_usd = min(basket_usd, max_usd, balance * max_fraction)
        basket_usd = max(Config.MIN_ORDER_SIZE * len(kept), basket_usd)

        # ── allocate EQUAL SHARES across legs (any-one-wins guarantee) ──
        cost_per_share = total_market_cost
        target_shares = basket_usd / cost_per_share if cost_per_share > 0 else 0.0
        max_price = max(price for _, _, _, price, _ in kept)
        min_shares_for_floor = Config.MIN_ORDER_SIZE / max_price if max_price > 0 else 0.0
        shares = max(min_shares_for_floor, target_shares)

        legs: List[SafetyPeakLeg] = []
        for idx, bp, role, price, tid in kept:
            leg_usd = max(Config.MIN_ORDER_SIZE, round(shares * price, 2))
            offset = 0 if role == 'peak' else (+1 if role == 'neighbor_warm' else -1)
            legs.append(SafetyPeakLeg(
                bucket_label=bp.bucket_label,
                token_id=tid,
                market_price=price,
                our_probability=bp.probability,
                size_usd=leg_usd,
                role=role,
                offset=offset,
            ))

        total_deployed = round(sum(l.size_usd for l in legs), 2)
        # ROI if any single leg wins: each winning share pays $1 vs cost_per_share.
        expected_roi_pct = ((1.0 - cost_per_share) / cost_per_share * 100.0) if cost_per_share > 0 else 0.0

        exit_hint = "HOLD — high-confidence safety peak, let it resolve"
        hold_hint = True

        peak_label = next(l.bucket_label for l in legs if l.role == 'peak')
        nb_labels = [
            (f"+{l.bucket_label}" if l.offset > 0 else f"-{l.bucket_label}")
            for l in legs if l.role != 'peak'
        ]
        neighbor_str = (' ' + ' '.join(nb_labels)) if nb_labels else ' (peak-only)'

        reason = (
            f"SAFETY PEAK {city} trend={trend} grade={eff_grade:.2f} conf={peak_conf:.0%} "
            f"std={ens_std:.2f}°C | peak={peak_label} (fcst {target:.1f}°C){neighbor_str} | "
            f"{len(legs)}legs ${total_deployed:.2f} Pwin={combined_prob:.0%} "
            f"edge={basket_edge:.0%} roi~{expected_roi_pct:.0f}% | {exit_hint}"
        )

        log.info(f"   > SAFETY {reason}")
        return [SafetyPeakSignal(
            market_title=market_title,
            city=city,
            forecast_max_c=target,
            trend=trend,
            stability_score=eff_grade,
            confidence=peak_conf,
            legs=legs,
            total_cost=round(total_market_cost, 4),
            basket_usd=total_deployed,
            combined_prob=combined_prob,
            n_models=n_models,
            expected_roi_pct=expected_roi_pct,
            direction=direction_label,
            hold_hint=hold_hint,
            exit_hint=exit_hint,
            reason=reason,
        )]
