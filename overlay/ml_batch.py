"""ML-veto batching (Req-27 #g).

Aggregate several entry-validation legs into ONE ML call instead of one call
per leg. Opt-in via settings:
  ML_VETO_BATCH_ENABLED  (default OFF) -> master switch ('normal' when off)
  ML_VETO_GROUP_SIZE     (1..4, default 1) -> legs per grouped call

When batching is off or the group size is <= 1 this is a no-op and every leg is
validated individually (the original per-call behaviour). Fully fail-open: any
error leaves the per-leg path untouched, and a grouped verdict is only ever a
pre-warm of the SAME cache validate_signal() reads, so it can never change a
verdict the per-call path would not have produced.
"""


def _cfg(name, default):
    try:
        from config import Config
        return getattr(Config, name, default)
    except Exception:
        return default


def enabled():
    """True only when the master switch is ON and the group size is > 1."""
    try:
        if not bool(_cfg('ML_VETO_BATCH_ENABLED', False)):
            return False
        return int(_cfg('ML_VETO_GROUP_SIZE', 1) or 1) > 1
    except Exception:
        return False


def group_size():
    try:
        n = int(_cfg('ML_VETO_GROUP_SIZE', 1) or 1)
    except Exception:
        n = 1
    return max(1, min(4, n))


def prewarm(engine, items):
    """Pre-warm ``engine``'s entry cache for ``items`` in chunks of
    ML_VETO_GROUP_SIZE. ``items`` is a list of dicts with city, bucket_label,
    entry_price, our_prob, edge (see MLDecisionEngine.validate_signals_batch).

    Returns the number of grouped model calls made (0 = disabled / nothing to
    do). Never raises.
    """
    try:
        if engine is None or not enabled():
            return 0
        if not hasattr(engine, 'validate_signals_batch'):
            return 0
        items = [it for it in (items or []) if it]
        if len(items) < 2:
            return 0
        n = group_size()
        calls = 0
        for i in range(0, len(items), n):
            chunk = items[i:i + n]
            if len(chunk) < 2:
                break  # a lone trailing leg validates normally (one call)
            res = engine.validate_signals_batch(chunk)
            if isinstance(res, dict) and res.get('calls'):
                calls += int(res.get('calls') or 0)
        return calls
    except Exception:
        return 0
