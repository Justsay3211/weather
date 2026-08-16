"""Offline tests for observed_weather (pure split) and the strategy core."""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.observed_weather import split_observed_remaining, ObservedDayState
from data import observed_math as om
from strategies.late_observed_temp import decide_legs, DecideParams, LateObservedTempStrategy

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


def test_split():
    print("observed_weather.split:")
    times = [datetime(2026, 6, 10, h, 0) for h in range(0, 24)]
    temps = [10 + h * 0.8 for h in range(0, 24)]  # rises through the day
    now = datetime(2026, 6, 10, 16, 0)  # 4pm local
    obs, remaining, n_left = split_observed_remaining(times, temps, now, mode="high")
    check("observed max is 4pm value", abs(obs - (10 + 16 * 0.8)) < 1e-9)
    check("remaining hours counted", n_left == 7)
    check("remaining temps are post-4pm", min(remaining) > obs)


def test_decide_legs():
    print("strategy.decide_legs:")
    # Day locked: bucket "24" certain; low buckets dead; market still misprices.
    buckets = [
        ("22 or lower", float("-inf"), 22.5),
        ("23", 22.5, 23.5),
        ("24", 23.5, 24.5),
        ("25", 24.5, 25.5),
        ("26 or higher", 25.5, float("inf")),
    ]
    probs = om.observed_bucket_probabilities(24.0, None, 0.0, buckets, mode="high")
    yes_prices = {"22 or lower": 0.02, "23": 0.08, "24": 0.70, "25": 0.12, "26 or higher": 0.05}
    yes_tids = {b[0]: f"yes-{b[0]}" for b in buckets}
    no_prices = {"22 or lower": 0.97, "23": 0.90, "24": 0.30, "25": 0.85, "26 or higher": 0.95}
    no_tids = {b[0]: f"no-{b[0]}" for b in buckets}

    legs = decide_legs(probs, yes_prices, yes_tids, balance=3.0, grade=0.7,
                       no_prices=no_prices, no_token_ids=no_tids,
                       params=DecideParams(min_edge=0.05))
    labels = {(l.bucket_label, l.side) for l in legs}
    check("buys YES on locked bucket 24", ("24", "YES") in labels)
    # "23" is dead (prob 0), NO priced at 0.90 -> prob_no=1.0 clears fees: expect NO
    check("buys NO on dead bucket 23", ("23", "NO") in labels)
    check("no leg exceeds balance", all(l.size_usd <= 3.0 for l in legs))
    check("respects max_legs", len(legs) <= DecideParams().max_legs)
    check("all legs positive EV", all(l.ev_per_contract > 0 for l in legs))


def test_strategy_wrapper():
    print("strategy.evaluate:")
    strat = LateObservedTempStrategy()  # Config may be None offline -> defaults
    buckets = [("24", 23.5, 24.5), ("25", 24.5, 25.5), ("23", 22.5, 23.5)]
    state = ObservedDayState(
        observed_extreme_c=24.0, remaining_extreme_c=None, remaining_spread_c=0.0,
        hours_remaining=0, n_models=5, mode="high",
    )
    sigs = strat.evaluate(
        market_title="Highest temp in Tokyo",
        buckets=buckets,
        yes_prices={"24": 0.70, "25": 0.10, "23": 0.05},
        yes_token_ids={"24": "y24", "25": "y25", "23": "y23"},
        balance=3.0,
        city="Tokyo",
        observed_state=state,
        no_prices={"24": 0.30, "25": 0.88, "23": 0.92},
        no_token_ids={"24": "n24", "25": "n25", "23": "n23"},
        grade=0.7,
        market_type="highest_temperature",
    )
    check("locked day produces a signal", len(sigs) == 1)
    check("signal has legs", sigs and len(sigs[0].legs) >= 1)
    check("lock confidence is 1.0", sigs and abs(sigs[0].lock_confidence - 1.0) < 1e-9)


def main():
    test_split()
    test_decide_legs()
    test_strategy_wrapper()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
