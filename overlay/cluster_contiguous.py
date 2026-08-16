"""
PEAK-CLUSTER CONTIGUITY FIX  (the "14,16,18 instead of 15,16,17" bug).

Proven by the ledger (see the basket deep-dive):
  * Contiguous peak_cluster baskets: 83% WR, +$40.
  * Probability-gapped baskets (interior bucket skipped): 40% WR, -$67 — they
    are the ENTIRE reason peak_cluster shows a loss.

Root cause (strategies/peak_cluster.py): the basket builds a window around the
peak, then does `sorted(window, key=-prob)` and greedily takes the highest-
probability buckets under the cost cap — selection by PROBABILITY, not ADJACENCY.
So it can drop an interior neighbour and leave a hole exactly where the outcome
most often lands (the winner is a neighbour of the peak more than half the time).

This overlay post-processes each PeakClusterSignal AFTER evaluate(), WITHOUT
touching the strategy file:
  1. Order the chosen legs by temperature (parsed from the bucket label).
  2. Anchor on the peak (highest-probability chosen leg).
  3. Walk outward (center, ±1, ±2 …). If an interior bucket is missing, try to
     FILL it from the live market (token + price present, combined per-share
     cost stays < the cost cap). If it can't be filled, STOP the ladder at the
     hole and keep the unbroken run — never jump the gap.
  4. If the resulting contiguous run has fewer than min-legs, DROP the signal
     (a gapped/too-short basket is a proven net loser).

Never re-orders by probability. Master switch CLUSTER_CONTIGUOUS_ENABLED
(default ON); hole-filling CLUSTER_CONTIGUOUS_FILL_HOLES (default ON).
Fail-open: on any error the original signal is returned unchanged.
"""

import re
from types import SimpleNamespace

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

SETTING_DEFAULTS = {
    "CLUSTER_CONTIGUOUS_ENABLED": True,
    "CLUSTER_CONTIGUOUS_FILL_HOLES": True,
}

_TEMP_RE = re.compile(r"(-?\d+)\s*\u00b0")


def ensure_defaults():
    if Config is None:
        return
    for key, default in SETTING_DEFAULTS.items():
        if not hasattr(Config, key):
            setattr(Config, key, default)


def _enabled():
    return Config is None or bool(getattr(Config, "CLUSTER_CONTIGUOUS_ENABLED", True))


def _fill_holes():
    return Config is None or bool(getattr(Config, "CLUSTER_CONTIGUOUS_FILL_HOLES", True))


def _temp_of(label):
    if not label:
        return None
    m = _TEMP_RE.search(str(label))
    if m:
        return int(m.group(1))
    m2 = re.search(r"(-?\d+)", str(label))
    return int(m2.group(1)) if m2 else None


def _min_legs():
    return max(3, int(getattr(Config, "PEAK_CLUSTER_MIN_LEGS", 3))) if Config else 3


def _max_cost():
    if Config is None:
        return 0.85
    return float(getattr(Config, "PEAK_CLUSTER_MAX_COST",
                         getattr(Config, "BASKET_MAX_COST", 0.85)))


def _new_leg(label, token_id, price, prob, size_usd):
    return SimpleNamespace(bucket_label=label, token_id=token_id,
                           price=float(price), prob=float(prob),
                           size_usd=float(size_usd))


def enforce(signal, market_prices=None, token_ids=None):
    """Return a contiguity-corrected signal, or None to drop it. Fail-open."""
    if not _enabled() or signal is None:
        return signal
    try:
        legs = list(getattr(signal, "legs", []) or [])
        if len(legs) < 2:
            return signal
        # (temp, leg) for legs we can place on the temperature axis.
        keyed = []
        for lg in legs:
            t = _temp_of(getattr(lg, "bucket_label", ""))
            if t is None:
                return signal  # can't reason about order -> leave untouched
            keyed.append((t, lg))
        keyed.sort(key=lambda x: x[0])
        temps = [t for t, _ in keyed]
        # Already contiguous (step of 1, no interior gaps)? Nothing to do.
        contiguous = all(temps[i + 1] - temps[i] == 1 for i in range(len(temps) - 1))
        if contiguous:
            return signal

        # Peak = highest-probability chosen leg.
        peak_idx = max(range(len(keyed)), key=lambda i: float(getattr(keyed[i][1], "prob", 0.0)))
        peak_temp = keyed[peak_idx][0]
        by_temp = {t: lg for t, lg in keyed}
        share = _shares(legs)
        cost_cap = _max_cost()

        # Walk outward from the peak, filling or stopping at holes.
        chosen = {peak_temp: by_temp[peak_temp]}
        cost = float(getattr(by_temp[peak_temp], "price", 0.0))
        for direction in (+1, -1):
            t = peak_temp
            while True:
                t += direction
                if t in by_temp:
                    lg = by_temp[t]
                elif _fill_holes() and market_prices is not None and token_ids is not None:
                    lg = _try_fill(t, market_prices, token_ids, share)
                    if lg is None:
                        break  # missing & unfillable -> stop this direction
                else:
                    break
                pr = float(getattr(lg, "price", 0.0))
                if pr <= 0 or cost + pr > cost_cap:
                    break
                chosen[t] = lg
                cost += pr

        ordered = [chosen[t] for t in sorted(chosen)]
        if len(ordered) < _min_legs():
            return None  # contiguous run too short -> drop the gapped basket

        # Recompute the signal in place (mutable dataclass).
        pr_sum = min(1.0, sum(float(getattr(l, "prob", 0.0)) for l in ordered))
        signal.legs = ordered
        signal.n_legs = len(ordered)
        signal.total_cost = round(cost, 4)
        signal.combined_prob = pr_sum
        if cost > 0:
            signal.expected_roi_pct = (1.0 - cost) / cost * 100.0
        try:
            lo, hi = sorted(chosen)[0], sorted(chosen)[-1]
            signal.reason = (f"peak-cluster CONTIGUOUS {lo}..{hi} "
                             f"({len(ordered)} legs, cost ${cost:.2f})")
        except Exception:
            pass
        return signal
    except Exception:
        return signal


def _shares(legs):
    """Recover the uniform basket share count from an existing leg."""
    for lg in legs:
        pr = float(getattr(lg, "price", 0.0) or 0.0)
        sz = float(getattr(lg, "size_usd", 0.0) or 0.0)
        if pr > 0 and sz > 0:
            return sz / pr
    return 0.0


def _try_fill(temp, market_prices, token_ids, share):
    """Construct a leg for a missing interior bucket if the market offers it."""
    for label, price in (market_prices or {}).items():
        if _temp_of(label) != temp:
            continue
        tok = (token_ids or {}).get(label, "")
        try:
            pr = float(price or 0.0)
        except (TypeError, ValueError):
            pr = 0.0
        if not tok or pr <= 0:
            return None
        return _new_leg(label, tok, pr, 0.0, round(share * pr, 4))
    return None


def enforce_all(signals, market_prices=None, token_ids=None):
    out = []
    for s in (signals or []):
        r = enforce(s, market_prices, token_ids)
        if r is not None:
            out.append(r)
    return out


ensure_defaults()
