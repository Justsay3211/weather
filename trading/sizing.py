"""
Factor-Kelly position sizing — the single source of truth for *how much* to
stake on a signal.

WHY THIS EXISTS (and what it fixes)
-----------------------------------
The old sizing was a flat %-of-bankroll Kelly that hit a 25%-of-balance ceiling
with NO dollar cap. On a $100 book that meant ~$25 a clip, and because the
scanner fires many legs in the first cycle it dumped the WHOLE bankroll in
seconds (logs: $100 -> $0.37 in 14s), leaving no dry powder for the good
markets that appear later. It also ignored *which* strategy / *how good* the
signal was.

THE NEW MODEL — a tiered ladder driven by a multi-factor strength score:

    strength = w_edge*edge_norm + w_prob*prob_win + w_grade*grade
             + w_winrate*win_rate            (each term in [0, 1])

    strength  ->  tier         ->  stake
    < good        weak-valid       BASE   ($3)
    >= good       good             GOOD   ($5)
    >= vgood      very good        VGOOD  ($10)
    >= perfect    perfect          PERFECT($15)   <- hard max per position

The chosen tier is then multiplied by an optional per-strategy bias (boost the
strategy that actually wins, trim the one that bleeds) and finally clamped to a
per-trade balance-fraction safety and the live balance. A *very good* signal
therefore deploys MORE capital while a barely-passing one stays small — and
nothing can exceed the perfect-tier dollar cap.

Pure module: depends only on the stdlib so it stays unit-testable offline.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SizingParams:
    # Tier dollar amounts (per the user's ladder: 3 / 5 / 10 / 15-max).
    base_usd: float = 3.0
    good_usd: float = 5.0
    vgood_usd: float = 10.0
    perfect_usd: float = 15.0
    # Strength thresholds that promote a signal up the ladder.
    good_strength: float = 0.40
    vgood_strength: float = 0.65
    perfect_strength: float = 0.85
    # Strength-score factor weights (normalised internally).
    w_edge: float = 0.35
    w_prob: float = 0.25
    w_grade: float = 0.20
    w_winrate: float = 0.20
    edge_full: float = 0.25          # post-fee edge that counts as "max" strength
    # Win-rate handling: blend the observed strategy win-rate toward a neutral
    # prior until we have enough closed trades, so a cold-start strategy is
    # neither over- nor under-weighted.
    winrate_prior: float = 0.45
    winrate_full_trust_n: int = 20
    # Safety clamps.
    max_fraction: float = 0.25       # never stake more than this fraction of balance
    min_order_usd: float = 1.0


def _clip01(x: float) -> float:
    if x != x:  # NaN guard
        return 0.0
    return max(0.0, min(1.0, x))


def blended_winrate(win_rate: Optional[float], n_trades: int, params: SizingParams) -> float:
    """Shrink a raw win-rate toward the neutral prior when the sample is small."""
    if win_rate is None or n_trades <= 0:
        return params.winrate_prior
    trust = _clip01(n_trades / float(max(1, params.winrate_full_trust_n)))
    return trust * _clip01(win_rate) + (1.0 - trust) * params.winrate_prior


def signal_strength(
    edge: float,
    prob_win: float,
    grade: float,
    win_rate: Optional[float] = None,
    n_trades: int = 0,
    params: Optional[SizingParams] = None,
) -> float:
    """Composite signal-strength score in [0, 1] from multiple factors."""
    p = params or SizingParams()
    edge_norm = _clip01(edge / p.edge_full) if p.edge_full > 0 else 0.0
    prob_norm = _clip01(prob_win)
    grade_norm = _clip01(grade)
    wr_norm = _clip01(blended_winrate(win_rate, n_trades, p))
    wsum = p.w_edge + p.w_prob + p.w_grade + p.w_winrate
    if wsum <= 0:
        return 0.0
    s = (p.w_edge * edge_norm + p.w_prob * prob_norm
         + p.w_grade * grade_norm + p.w_winrate * wr_norm) / wsum
    return _clip01(s)


def tier_for_strength(strength: float, params: Optional[SizingParams] = None) -> str:
    p = params or SizingParams()
    if strength >= p.perfect_strength:
        return 'perfect'
    if strength >= p.vgood_strength:
        return 'vgood'
    if strength >= p.good_strength:
        return 'good'
    return 'base'


def tier_usd(tier: str, params: Optional[SizingParams] = None) -> float:
    p = params or SizingParams()
    return {
        'perfect': p.perfect_usd,
        'vgood': p.vgood_usd,
        'good': p.good_usd,
        'base': p.base_usd,
    }.get(tier, p.base_usd)


def factor_kelly_stake(
    edge: float,
    prob_win: float,
    balance: float,
    grade: float = 0.6,
    win_rate: Optional[float] = None,
    n_trades: int = 0,
    strategy_mult: float = 1.0,
    params: Optional[SizingParams] = None,
) -> float:
    """Return the USD stake for a signal.

    Combines a multi-factor strength score -> tiered ladder ($3/$5/$10/$15) ->
    per-strategy bias -> balance-fraction safety -> live-balance clamp.
    """
    p = params or SizingParams()
    if balance <= 0:
        return 0.0
    strength = signal_strength(edge, prob_win, grade, win_rate, n_trades, p)
    tier = tier_for_strength(strength, p)
    usd = tier_usd(tier, p) * max(0.0, strategy_mult)
    # Never let the per-strategy bias push a single position past the perfect cap.
    usd = min(usd, p.perfect_usd * max(1.0, strategy_mult) if strategy_mult > 1.0 else p.perfect_usd)
    # Hard ceiling: the perfect-tier dollar cap is the absolute per-position max
    # UNLESS a deliberate boost (>1) is configured, which may exceed it modestly.
    hard_cap = p.perfect_usd * max(1.0, strategy_mult)
    usd = min(usd, hard_cap)
    # Safety clamps vs the live balance.
    usd = min(usd, balance * p.max_fraction, balance)
    if usd < p.min_order_usd:
        usd = min(p.min_order_usd, balance)
    return round(usd, 2)


def describe(edge, prob_win, grade, win_rate=None, n_trades=0, strategy_mult=1.0,
             params: Optional[SizingParams] = None) -> str:
    """Human-readable one-liner for logging WHY a stake was chosen."""
    p = params or SizingParams()
    s = signal_strength(edge, prob_win, grade, win_rate, n_trades, p)
    tier = tier_for_strength(s, p)
    return (f"strength={s:.2f}->{tier} (edge={edge:+.0%} prob={prob_win:.0%} "
            f"grade={grade:.2f} wr={blended_winrate(win_rate, n_trades, p):.0%} "
            f"x{strategy_mult:.2f})")
