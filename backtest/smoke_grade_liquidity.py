"""
Offline smoke test for the stability-GRADE + LIQUIDITY-guard wiring.

No network: feeds synthetic order books and grades through the same
LiquidityGuard.can_enter and the dashboard's _grade_multiplier logic to
confirm skip/scale/maker-repricing behave as intended.

Run:  PYTHONIOENCODING=utf-8 PYTHONUTF8=1 python -m backtest.smoke_grade_liquidity
"""

from config import Config
from data.liquidity_guard import LiquidityGuard


def grade_multiplier(grade: float) -> float:
    """Mirror of WeatherBot._grade_multiplier (kept in sync for the smoke test)."""
    if not Config.GRADE_SIZING_ENABLED:
        return 1.0
    g = max(0.0, min(1.0, grade))
    lo, hi = Config.GRADE_SIZE_MIN_MULT, Config.GRADE_SIZE_MAX_MULT
    return lo + (hi - lo) * g


def main():
    guard = LiquidityGuard()
    print("=" * 64)
    print("GRADE MULTIPLIER (size scaling by stability grade)")
    print("=" * 64)
    for g in (0.0, 0.2, Config.GRADE_MIN_TO_TRADE, 0.5, Config.GRADE_NEUTRAL, 0.8, 1.0):
        gate = "SKIP (below GRADE_MIN_TO_TRADE)" if g < Config.GRADE_MIN_TO_TRADE else ""
        print(f"  grade={g:.2f}  ->  size x{grade_multiplier(g):.3f}   {gate}")

    print()
    print("=" * 64)
    print("LIQUIDITY GUARD — can_enter (maker-at-bid, asymmetric books)")
    print("=" * 64)
    scenarios = [
        # (name, best_bid, best_ask, edge, bid_depth_shares)
        ("Healthy cheap tail",   0.030, 0.040, 0.20, 500),
        ("Thin book (no bid)",   0.000, 0.030, 0.20, 0),
        ("Asymmetric 1.5/3.0",   0.015, 0.030, 0.20, 500),   # user's example
        ("Wide spread vs edge",  0.030, 0.090, 0.05, 500),
        ("Shallow depth",        0.030, 0.040, 0.20, 10),
        ("Healthy mid-range",    0.420, 0.450, 0.15, 200),
    ]
    thin_mult = Config.LIQUIDITY_THIN_SIZE_MULT
    for name, bid, ask, edge, depth in scenarios:
        chk = guard.can_enter(market_price=ask, best_bid=bid, best_ask=ask,
                              edge=edge, bid_depth=depth, ask_depth=depth)
        # Advisory mode (default): never skip. Full size + (maybe early) exit
        # when the guard passes; trimmed size + forced hold when it doesn't.
        if chk.passed:
            action = "FULL size, maker@%.3f, normal exit" % (chk.entry_price if chk.entry_price > 0 else bid)
        else:
            entry = bid if bid > 0 else ask
            action = "x%.2f size, maker@%.3f, HOLD to resolution" % (thin_mult, entry)
        print(f"  {name:24s} bid={bid:.3f} ask={ask:.3f} edge={edge:.0%} depth={depth:>4}")
        print(f"      guard={'PASS' if chk.passed else 'thin/wide'} -> {action}  ({chk.reason})")

    print()
    print("Interpretation (advisory mode, LIQUIDITY_STRICT_BLOCK=0): the bot NEVER")
    print("skips on liquidity — it enters maker-at-bid, and on thin/wide books it")
    print("trims size and holds to resolution instead of relying on an exit.")


if __name__ == "__main__":
    main()
