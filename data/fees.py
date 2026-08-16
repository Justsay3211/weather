"""
Fee-aware expected-value math for Polymarket weather trades (dependency-free).

Polymarket charges a **taker** fee on weather markets and zero **maker** fee.
The per-contract taker fee follows the symmetric form used across the order
book:

    fee_per_contract(p) = TAKER_FEE_RATE * p * (1 - p)

where ``p`` is the fill price (0..1) of the token you are buying. Because the
fee peaks at p=0.5 and vanishes at the extremes, it materially changes the
break-even probability for mid-priced buckets — exactly where this bot trades.

Everything here is pure arithmetic (only ``math`` from the stdlib) so it imports
and unit-tests cleanly offline. Callers that want config-driven rates pass them
in explicitly; the module never imports the project ``Config``.

Conventions
-----------
* A "contract" pays **$1** if it wins, **$0** if it loses.
* ``price`` is the cost (0..1) of the token you buy — use the YES price for a
  YES leg and the NO price for a NO leg. The NO token of a bucket is its own
  tradeable token, so the same math applies symmetrically.
* ``prob_win`` is *your* estimated probability that the token you bought wins.
"""

from __future__ import annotations

import math

TAKER_FEE_RATE = 0.05
MAKER_FEE_RATE = 0.0


def _clamp_price(price: float) -> float:
    if price is None:
        return 0.0
    if price < 0.0:
        return 0.0
    if price > 1.0:
        return 1.0
    return float(price)


def taker_fee_per_contract(price: float, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Symmetric taker fee charged per 1 contract bought at ``price``."""
    p = _clamp_price(price)
    return fee_rate * p * (1.0 - p)


def ev_per_contract(
    prob_win: float,
    price: float,
    taker: bool = True,
    fee_rate: float = TAKER_FEE_RATE,
) -> float:
    """Expected $ profit per contract.

    win  -> receive (1 - price)
    lose -> lose price
    minus the taker fee (if a taker fill).
    """
    p = _clamp_price(price)
    q = max(0.0, min(1.0, float(prob_win)))
    gross = q * (1.0 - p) - (1.0 - q) * p
    fee = taker_fee_per_contract(p, fee_rate) if taker else 0.0
    return gross - fee


def breakeven_prob(price: float, taker: bool = True, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Minimum true win-probability needed for a non-negative EV.

    Maker: q* = p.  Taker: q* = p + fee_rate * p * (1 - p).
    """
    p = _clamp_price(price)
    if not taker:
        return p
    return p + taker_fee_per_contract(p, fee_rate)


def min_edge_required(price: float, taker: bool = True, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Edge (prob - price) needed just to break even after fees."""
    return breakeven_prob(price, taker, fee_rate) - _clamp_price(price)


def net_profit_if_win(price: float, shares: float = 1.0, taker: bool = True,
                      fee_rate: float = TAKER_FEE_RATE) -> float:
    """$ profit if the position wins (payout minus cost minus fee)."""
    p = _clamp_price(price)
    fee = taker_fee_per_contract(p, fee_rate) if taker else 0.0
    return shares * ((1.0 - p) - fee)


def roi_if_win(price: float, taker: bool = True, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Return-on-cost multiple if the position wins (0 when price is 0)."""
    p = _clamp_price(price)
    if p <= 0.0:
        return 0.0
    return net_profit_if_win(p, 1.0, taker, fee_rate) / p


def passes_fee_gate(
    prob_win: float,
    price: float,
    min_edge: float = 0.0,
    taker: bool = True,
    fee_rate: float = TAKER_FEE_RATE,
) -> bool:
    """True when the trade clears fees *and* a post-fee edge cushion.

    ``min_edge`` is an additional probability cushion required *on top of* the
    fee-adjusted break-even, so callers express a single intuitive knob.
    """
    q = max(0.0, min(1.0, float(prob_win)))
    if ev_per_contract(q, price, taker, fee_rate) <= 0.0:
        return False
    return (q - breakeven_prob(price, taker, fee_rate)) >= min_edge


def kelly_fraction(prob_win: float, price: float, cap: float = 1.0) -> float:
    """Fractional Kelly stake for a binary contract bought at ``price``.

    f* = (q * b - (1 - q)) / b, with b = (1/price - 1). Clamped to [0, cap].
    """
    p = _clamp_price(price)
    if p <= 0.0 or p >= 1.0:
        return 0.0
    q = max(0.0, min(1.0, float(prob_win)))
    b = (1.0 / p) - 1.0
    if b <= 0:
        return 0.0
    f = (q * b - (1.0 - q)) / b
    if math.isnan(f) or f <= 1e-9:
        return 0.0
    return min(f, cap)
