"""
PEAK-CLUSTER PROB-BASED FILL (peak_cluster ONLY, opt-in).

The contiguity overlay keeps an unbroken temperature ladder around the peak, but
the ACTUAL winning bucket is sometimes a high-probability bucket a step OUTSIDE
that contiguous run (the "missed 4th-bucket winner"). When
PEAK_CLUSTER_PROB_BASED_ENABLED is ON, this overlay ADDS any window bucket whose
model probability is >= PEAK_CLUSTER_PROB_MIN and that still fits under the
basket cost cap -- even if it creates a gap in the ladder.

HARD SCOPE: applied ONLY to peak_cluster signals in dashboard.py. It never sees
the peaker cool/warm baskets (those are shaped in a different code path), so it
cannot change the cool/warm basket structure. Fail-open: any error returns the
signals unchanged so it can never break a scan.
"""
from types import SimpleNamespace

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None


def _f(name, default):
    if Config is None:
        return default
    try:
        return float(getattr(Config, name, default))
    except (TypeError, ValueError):
        return default


def _prob_min():
    return _f('PEAK_CLUSTER_PROB_MIN', 0.10)


def _max_cost():
    return _f('PEAK_CLUSTER_MAX_COST', _f('BASKET_MAX_COST', 0.85))


def _max_legs():
    return int(_f('PEAK_CLUSTER_MAX_LEGS', 7))


def _shares(legs):
    """Recover the (uniform) share count the basket sized each leg at."""
    for lg in legs:
        pr = float(getattr(lg, 'price', 0.0) or 0.0)
        sz = float(getattr(lg, 'size_usd', 0.0) or 0.0)
        if pr > 0 and sz > 0:
            return sz / pr
    return 0.0


def augment(signal, bucket_probs=None, market_prices=None, token_ids=None):
    if signal is None or not bucket_probs:
        return signal
    try:
        legs = list(getattr(signal, 'legs', []) or [])
        if not legs:
            return signal
        have = {getattr(l, 'bucket_label', '') for l in legs}
        share = _shares(legs)
        if share <= 0:
            return signal
        cost = sum(float(getattr(l, 'price', 0.0) or 0.0) for l in legs)
        cost_cap = _max_cost()
        max_legs = _max_legs()
        prob_min = _prob_min()
        market_prices = market_prices or {}
        token_ids = token_ids or {}
        cands = []
        for bp in bucket_probs:
            label = getattr(bp, 'bucket_label', '')
            if not label or label in have:
                continue
            prob = float(getattr(bp, 'probability', 0.0) or 0.0)
            if prob < prob_min:
                continue
            try:
                price = float(market_prices.get(label) or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            token = token_ids.get(label, '')
            if not token or price <= 0:
                continue
            cands.append((prob, label, price, token))
        cands.sort(key=lambda x: -x[0])
        added = []
        for prob, label, price, token in cands:
            if len(legs) + len(added) >= max_legs:
                break
            if cost + price > cost_cap:
                continue
            added.append(SimpleNamespace(
                bucket_label=label, token_id=token, price=price,
                prob=prob, size_usd=round(share * price, 4)))
            cost += price
        if not added:
            return signal
        new_legs = legs + added
        prob_sum = min(1.0, sum(float(getattr(l, 'prob', 0.0) or 0.0)
                                for l in new_legs))
        signal.legs = new_legs
        signal.n_legs = len(new_legs)
        signal.total_cost = round(cost, 4)
        signal.combined_prob = prob_sum
        if cost > 0:
            signal.expected_roi_pct = (1.0 - cost) / cost * 100.0
        try:
            signal.reason = (getattr(signal, 'reason', '') or '') + \
                f" +prob-fill {len(added)} (>= {prob_min:.0%})"
        except Exception:
            pass
        return signal
    except Exception:
        return signal


def augment_all(signals, bucket_probs=None, market_prices=None, token_ids=None):
    out = []
    for s in (signals or []):
        r = augment(s, bucket_probs, market_prices, token_ids)
        out.append(r if r is not None else s)
    return out
