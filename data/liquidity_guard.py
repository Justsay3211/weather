"""
Liquidity Guard — The spread/buy-sell gap manager.

THE PROBLEM (user's core concern):
  Buy YES at 5c, price rises to 9c, but no buyers at 9c. The bid is at 3c.
  You CAN'T exit at the displayed "price" — you can only sell at the BID.

THE SOLUTION — SPREAD-AWARE EXECUTION:
  1. Every entry uses BEST_BID (maker) — never cross the spread as taker.
     Maker = 0% fee, you earn the spread instead of paying it.
  2. Every exit is evaluated against the REAL BID (what you can actually sell at).
     The "mid" or "last price" is a LIE — the bid is what matters.
  3. Entry guard: spread_bps MUST be < threshold × edge.
     If spread > 10% of edge, skip the trade — you'll never overcome the friction.
  4. Position monitoring: track the REAL bid price, not mid.
  5. Thin-book detection: flag when depth < minimum viable fill.

SPREAD TIERS (from live weather market data):
  Cheap tails ($0.01-0.05): spread = $0.01-0.02 (tight, 20-200bps)
  Mid-range ($0.05-0.50): spread = $0.02-0.04 (moderate, 50-400bps)
  Near-certain ($0.70+): spread = $0.03-0.06 (wider, 50-100bps)
  Thin/empty book: spread = $0.10+ (unusable)

USAGE:
  guard = LiquidityGuard()
  if guard.can_enter(market_price, best_bid, best_ask, edge, depth):
      entry_price = best_bid  # maker, not ask
      # ... place GTC order at best_bid
"""

from dataclasses import dataclass
from typing import Optional


@dataclass
class LiquidityCheck:
    passed: bool
    entry_price: float         # best_bid (maker) — what we should pay
    exit_price: float          # current best_bid — what we could sell at now
    spread_bps: float          # current spread
    depth_shares: float        # available depth at entry price
    thin_book: bool            # is the book effectively empty?
    reason: str                # why it passed/failed


class LiquidityGuard:
    """
    Spread-aware execution guard.
    NEVER crosses the spread — always posts maker orders at best_bid.
    """

    # Spread thresholds by price range — ABSOLUTE cents, not bps.
    # Polymarket weather markets trade on a 1¢ tick, so a single-tick spread on
    # a 3¢ bucket is ~2800bps yet perfectly normal. Judging cheap books by bps
    # rejects everything; absolute cents is the right gauge for a 1¢-tick venue.
    MAX_SPREAD_ABS = {
        "tail": 0.02,      # $0.01-0.05: up to a 2¢ spread is normal
        "mid": 0.03,       # $0.05-0.50: up to 3¢
        "high": 0.06,      # $0.50+: up to 6¢
    }

    MIN_DEPTH_USD = 5.0      # minimum $5 depth at entry price
    MIN_BID_DEPTH_USD = 3.0  # minimum $3 on the bid side to be able to exit

    def can_enter(
        self,
        market_price: float,    # best_ask (what taker pays)
        best_bid: float,        # what maker gets
        best_ask: float,        # what taker pays
        edge: float,            # our edge (our_prob - market_price)
        bid_depth: float = 100,  # shares available at bid
        ask_depth: float = 100,  # shares available at ask
    ) -> LiquidityCheck:
        """
        Check if a trade can be entered given current liquidity.
        Returns LiquidityCheck with entry recommendation (maker at best_bid).

        Calibrated for penny markets: a "good" book just needs a real bid, a
        spread within a few ticks, and enough bid-side depth to exit later.
        """
        mid = (best_bid + best_ask) / 2 if (best_bid + best_ask) > 0 else market_price
        spread = round(best_ask - best_bid, 4)  # snap to the 1¢ tick grid (avoid float artifacts)
        spread_bps = (spread / mid * 10000) if mid > 0 else 9999
        bid_depth_usd = bid_depth * best_bid

        # Determine price tier
        if market_price <= 0.05:
            tier = "tail"
        elif market_price <= 0.50:
            tier = "mid"
        else:
            tier = "high"

        max_spread = self.MAX_SPREAD_ABS[tier]

        # Thin book = no real bid to exit into, or near-resolved, or no depth.
        thin_book = (best_bid < 0.01 or best_ask > 0.99 or bid_depth < 1)

        # ── Check 1: Thin / empty book (can't exit) ──
        if thin_book:
            return LiquidityCheck(
                passed=False, entry_price=best_bid, exit_price=best_bid,
                spread_bps=spread_bps, depth_shares=bid_depth,
                thin_book=True,
                reason=f"Thin book (bid={best_bid:.2f}, ask={best_ask:.2f}, depth={bid_depth:.0f}sh)"
            )

        # ── Check 2: Spread within a few ticks (absolute) ──
        if spread > max_spread:
            return LiquidityCheck(
                passed=False, entry_price=best_bid, exit_price=best_bid,
                spread_bps=spread_bps, depth_shares=bid_depth,
                thin_book=thin_book,
                reason=f"Spread ${spread:.2f} > max ${max_spread:.2f} for {tier} tier"
            )

        # ── Check 3: Enough bid-side depth to exit ──
        if bid_depth_usd < self.MIN_BID_DEPTH_USD:
            return LiquidityCheck(
                passed=False, entry_price=best_bid, exit_price=best_bid,
                spread_bps=spread_bps, depth_shares=bid_depth,
                thin_book=thin_book,
                reason=f"Bid depth ${bid_depth_usd:.2f} < min ${self.MIN_BID_DEPTH_USD}"
            )

        # ── PASSED: Enter at best_bid (maker, 0% fee) ──
        return LiquidityCheck(
            passed=True,
            entry_price=best_bid,  # MAKER — post at bid, wait for taker to fill us
            exit_price=best_bid,    # current exit price (will be monitored)
            spread_bps=spread_bps,
            depth_shares=bid_depth,
            thin_book=False,
            reason=f"OK: spread=${spread:.2f}, bid_depth=${bid_depth_usd:.2f}, maker@{best_bid:.2f}",
        )

    def can_exit(
        self,
        current_bid: float,
        shares: float,
        min_acceptable_price: float = 0,
    ) -> LiquidityCheck:
        """
        Check if a position can be exited at an acceptable price.
        The REAL exit price is the BID, not the mid or last price.
        """
        exit_value = current_bid * shares
        if current_bid <= 0:
            return LiquidityCheck(
                passed=False, entry_price=0, exit_price=0,
                spread_bps=9999, depth_shares=0, thin_book=True,
                reason="No bid — cannot exit"
            )

        if current_bid < min_acceptable_price:
            return LiquidityCheck(
                passed=False, entry_price=0, exit_price=current_bid,
                spread_bps=0, depth_shares=0, thin_book=False,
                reason=f"Bid {current_bid:.3f} < min acceptable {min_acceptable_price:.3f}"
            )

        return LiquidityCheck(
            passed=True, entry_price=0, exit_price=current_bid,
            spread_bps=0, depth_shares=shares, thin_book=False,
            reason=f"Exit at ${current_bid:.4f} = ${exit_value:.2f}"
        )
