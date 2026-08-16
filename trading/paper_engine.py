"""
Paper trading engine — make dry-run behave like real trading.

Every function here is PURE and offline-testable. The PositionManager / dashboard
use them (when ``Config.is_paper()``) to:

  * simulate realistic fills against the live order book — a taker walks the ask
    ladder (volume-weighted price + slippage + partial fills); a maker rests at
    the bid and only fills when the market trades through it,
  * settle positions from Polymarket's ACTUAL resolved outcome (source of
    truth), with the weather observation as a confirmation metric only,
  * conclude a near-certain win in the final minutes before close when the venue
    price is >= 95/99%,
  * keep a conserved PnL ledger (cash + open cost basis == deposited + realized).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence, Tuple


# ----------------------------------------------------------------------------
# FILLS
# ----------------------------------------------------------------------------
@dataclass
class FillResult:
    fill_price: float        # volume-weighted average fill price
    filled_shares: float
    filled_usd: float
    partial: bool            # True when the book couldn't absorb the full size
    levels_used: int
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.filled_shares > 0 and self.fill_price > 0


def simulate_taker_fill(asks: Sequence[Tuple[float, float]], size_usd: float,
                        max_price: float = 0.99) -> FillResult:
    """Walk the ask ladder spending up to ``size_usd``.

    ``asks`` = ``[(price, size_shares), ...]`` ascending by price (as returned
    by ClobClient.get_orderbook). Returns the volume-weighted fill, flagging a
    partial fill when the book is too thin to absorb the whole order.
    """
    if size_usd <= 0 or not asks:
        return FillResult(0.0, 0.0, 0.0, True, 0, "no asks or zero size")
    spent = 0.0
    shares = 0.0
    levels = 0
    remaining = size_usd
    for price, depth in asks:
        if price <= 0 or price > max_price:
            break
        levels += 1
        level_cost = price * depth
        if level_cost >= remaining:
            buy_shares = remaining / price
            shares += buy_shares
            spent += buy_shares * price
            remaining = 0.0
            break
        shares += depth
        spent += level_cost
        remaining -= level_cost
    if shares <= 0:
        return FillResult(0.0, 0.0, 0.0, True, levels, "ask ladder above max price")
    avg = spent / shares
    partial = remaining > 1e-9
    return FillResult(avg, shares, spent, partial, levels,
                      "partial fill (book too thin)" if partial else "filled")


def maker_fills(best_ask_after: float, post_price: float) -> bool:
    """A resting BUY at ``post_price`` fills once the market's best ask trades
    down to or below it (a seller crosses our bid)."""
    if post_price <= 0 or best_ask_after <= 0:
        return False
    return best_ask_after <= post_price + 1e-9


# ----------------------------------------------------------------------------
# SETTLEMENT
# ----------------------------------------------------------------------------
@dataclass
class SettlementDecision:
    status: str               # 'won' | 'lost' | 'open' | 'concluded_win'
    settle_price: Optional[float]   # 1.0 win, 0.0 lose, None if still open
    source: str               # 'polymarket' | 'preclose_lock' | 'none'
    confirmed_by_weather: Optional[bool] = None
    reason: str = ""


def decide_settlement(*, side: str, venue_won: Optional[bool],
                      venue_resolved: bool,
                      weather_won: Optional[bool] = None) -> SettlementDecision:
    """SOURCE OF TRUTH = Polymarket resolved outcome (``venue_won`` = did the
    YES leg of this bucket win). Weather is a confirmation metric only and never
    overrides the venue.

    ``side`` is 'yes' or 'no' (our position's leg on this bucket).
    """
    side = (side or "yes").lower()
    if venue_resolved and venue_won is not None:
        our_win = venue_won if side == "yes" else (not venue_won)
        confirmed: Optional[bool] = None
        if weather_won is not None:
            confirmed = (weather_won == venue_won)
        reason = "settled on Polymarket resolved outcomePrices"
        if confirmed is True:
            reason += " (weather agrees)"
        elif confirmed is False:
            reason += " (weather DISAGREES — venue wins)"
        return SettlementDecision(
            status="won" if our_win else "lost",
            settle_price=1.0 if our_win else 0.0,
            source="polymarket",
            confirmed_by_weather=confirmed,
            reason=reason,
        )
    return SettlementDecision("open", None, "none", None, "venue not resolved yet")


def preclose_conclusion(*, venue_price: float, minutes_to_close: Optional[float],
                        lock_confidence: Optional[float],
                        weather_won: Optional[bool],
                        price_threshold: float = 0.95,
                        window_minutes: float = 2.0,
                        lock_threshold: float = 0.95) -> Optional[SettlementDecision]:
    """In the final minutes before close, conclude a near-certain WIN early when
    the venue price is >= threshold AND (weather confirms OR lock confidence is
    high). This is a SIGNAL/label only — paper still books the real settlement at
    resolution via :func:`decide_settlement`. Returns None when not warranted.
    """
    if minutes_to_close is None or minutes_to_close > window_minutes:
        return None
    strong_price = venue_price >= price_threshold
    strong_lock = (lock_confidence is not None and lock_confidence >= lock_threshold)
    if strong_price and (weather_won is not False) and (strong_lock or weather_won is True):
        return SettlementDecision(
            status="concluded_win",
            settle_price=None,
            source="preclose_lock",
            confirmed_by_weather=weather_won,
            reason=(f"price {venue_price:.0%} >= {price_threshold:.0%} with "
                    f"{minutes_to_close:.1f}m to close"),
        )
    return None


# ----------------------------------------------------------------------------
# LEDGER INVARIANT
# ----------------------------------------------------------------------------
def ledger_ok(*, balance: float, open_cost: float, realized: float,
              deposited: float, tol: float = 0.01) -> Tuple[bool, float]:
    """Conservation invariant for paper accounting:

        cash balance + open cost basis  ==  deposited + realized PnL

    Returns ``(ok, drift)`` where ``drift`` is the signed imbalance.
    """
    lhs = balance + open_cost
    rhs = deposited + realized
    return abs(lhs - rhs) <= tol, (lhs - rhs)
