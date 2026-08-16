"""
Offline unit tests for the dependency-free overhaul modules.

These run in the sandbox WITHOUT network access and WITHOUT the third-party
trading dependencies (dotenv / requests / py_clob_client / web3), because they
only exercise the pure modules: bucket_parse, fees, observed_math.

Run:  python -m pytest tests/test_overhaul.py   (or)   python tests/test_overhaul.py
"""

import math
import os
import sys

# Make "data" importable whether run from repo root or tests/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data import bucket_parse as bp
from data import fees
from data import observed_math as om

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  - {name}")
    else:
        FAIL += 1
        print(f"  FAIL- {name}")


def approx(a, b, tol=1e-6):
    return abs(a - b) <= tol


# ---------------------------------------------------------------------------
# bucket_parse
# ---------------------------------------------------------------------------

def test_bucket_parse():
    print("bucket_parse:")
    lo, hi = bp.parse_bucket_bounds("Will the high temp be 24°C on May 29?")
    check("exact C lower", approx(lo, 23.5))
    check("exact C upper", approx(hi, 24.5))

    # Mojibaked degree sign (UTF-8 read as Latin-1).
    lo, hi = bp.parse_bucket_bounds("be 24Â°C on May 29")
    check("mojibake exact lower", approx(lo, 23.5))
    check("mojibake exact upper", approx(hi, 24.5))

    # Degree glyph + unit missing entirely, with direction word.
    lo, hi = bp.parse_bucket_bounds("will it be 38 or higher")
    check("no-unit or-higher open top", math.isinf(hi) and hi > 0)
    check("no-unit or-higher floor", approx(lo, 37.5))

    lo, hi = bp.parse_bucket_bounds("17°C or below")
    check("or-below open bottom", math.isinf(lo) and lo < 0)
    check("or-below ceiling", approx(hi, 17.5))

    # Fahrenheit range -> Celsius.
    lo, hi = bp.parse_bucket_bounds("be between 80-81°F on May 1")
    # 79.5F -> ~26.39C, 81.5F -> ~27.5C
    check("F range lower ~26.39", approx(lo, (79.5 - 32) * 5 / 9, 1e-4))
    check("F range upper ~27.5", approx(hi, (81.5 - 32) * 5 / 9, 1e-4))

    # Fahrenheit single open-ended below.
    lo, hi = bp.parse_bucket_bounds("71°F or below")
    check("F or-below open bottom", math.isinf(lo))
    check("F or-below ceiling ~22", approx(hi, (71.5 - 32) * 5 / 9, 1e-4))

    # Trailing plus sign means open top.
    lo, hi = bp.parse_bucket_bounds("30°C+")
    check("trailing + open top", math.isinf(hi))
    check("trailing + floor", approx(lo, 29.5))

    # Unparseable.
    lo, hi = bp.parse_bucket_bounds("some nonsense label")
    check("fallback both inf", math.isinf(lo) and math.isinf(hi))
    check("is_bounded false on fallback", bp.is_bounded((lo, hi)) is False)


# ---------------------------------------------------------------------------
# fees
# ---------------------------------------------------------------------------

def test_fees():
    print("fees:")
    # Fee peaks at 0.5 and vanishes at the extremes.
    check("fee at 0.5", approx(fees.taker_fee_per_contract(0.5), 0.05 * 0.25))
    check("fee at 0.0", approx(fees.taker_fee_per_contract(0.0), 0.0))
    check("fee at 1.0", approx(fees.taker_fee_per_contract(1.0), 0.0))

    # Break-even is price for maker, price+fee for taker.
    check("maker breakeven == price", approx(fees.breakeven_prob(0.4, taker=False), 0.4))
    check("taker breakeven > price", fees.breakeven_prob(0.4, taker=True) > 0.4)
    check("taker breakeven value",
          approx(fees.breakeven_prob(0.4, taker=True), 0.4 + 0.05 * 0.4 * 0.6))

    # EV sign behaviour.
    check("positive EV when prob beats breakeven",
          fees.ev_per_contract(0.60, 0.40, taker=True) > 0)
    check("negative EV at fair maker price minus fee",
          fees.ev_per_contract(0.40, 0.40, taker=True) < 0)
    check("zero-ish EV maker at fair price",
          approx(fees.ev_per_contract(0.40, 0.40, taker=False), 0.0))

    # Gate requires post-fee edge cushion.
    check("gate passes with cushion",
          fees.passes_fee_gate(0.60, 0.40, min_edge=0.10, taker=True))
    check("gate fails without cushion",
          not fees.passes_fee_gate(0.43, 0.40, min_edge=0.10, taker=True))

    # Kelly is positive only with edge and capped.
    check("kelly positive with edge", fees.kelly_fraction(0.6, 0.4) > 0)
    check("kelly zero without edge", fees.kelly_fraction(0.4, 0.4) == 0)
    check("kelly capped", fees.kelly_fraction(0.99, 0.5, cap=0.25) <= 0.25)


# ---------------------------------------------------------------------------
# observed_math
# ---------------------------------------------------------------------------

def test_observed_math():
    print("observed_math:")
    buckets = [
        ("22 or lower", float("-inf"), 22.5),
        ("23", 22.5, 23.5),
        ("24", 23.5, 24.5),
        ("25", 24.5, 25.5),
        ("26 or higher", 25.5, float("inf")),
    ]

    # Day fully locked at observed max 24 (no hours remain): bucket "24" wins.
    probs = om.observed_bucket_probabilities(24.0, None, 0.0, buckets, mode="high")
    check("locked: 24 ~1.0", approx(probs["24"], 1.0, 1e-6))
    check("locked: 23 == 0 (below floor)", probs["23"] == 0.0)
    check("locked: 22-lower == 0", probs["22 or lower"] == 0.0)
    check("locked: 25 == 0 (cooling done)", approx(probs["25"], 0.0, 1e-6))

    # Some hours remain, forecast remainder ~24.2 with small spread:
    probs = om.observed_bucket_probabilities(24.0, 24.2, 0.4, buckets, mode="high")
    check("remaining: probs sum to 1", approx(sum(probs.values()), 1.0, 1e-6))
    check("remaining: below-floor still 0", probs["23"] == 0.0 and probs["22 or lower"] == 0.0)
    check("remaining: mass on 24/25/26 only",
          probs["24"] + probs["25"] + probs["26 or higher"] > 0.999)

    # Lowest-temperature mode: observed min is a ceiling.
    low_buckets = [
        ("5 or lower", float("-inf"), 5.5),
        ("6", 5.5, 6.5),
        ("7", 6.5, 7.5),
        ("8 or higher", 7.5, float("inf")),
    ]
    probs = om.observed_bucket_probabilities(6.0, None, 0.0, low_buckets, mode="low")
    check("low locked: 6 ~1.0", approx(probs["6"], 1.0, 1e-6))
    check("low locked: 7 == 0 (above ceiling)", approx(probs["7"], 0.0, 1e-6))
    check("low locked: 8-higher == 0", approx(probs["8 or higher"], 0.0, 1e-6))

    # Lock confidence: no hours remain => fully locked.
    check("lock conf == 1 when no remainder",
          approx(om.lock_confidence(24.0, None, 0.0), 1.0))
    # Remaining forecast well below observed => high confidence.
    check("lock conf high when remainder cool",
          om.lock_confidence(24.0, 21.0, 0.5, mode="high") > 0.99)
    # Remaining forecast above observed => low confidence.
    check("lock conf low when remainder hot",
          om.lock_confidence(24.0, 27.0, 0.5, mode="high") < 0.01)


def main():
    test_bucket_parse()
    test_fees()
    test_observed_math()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
