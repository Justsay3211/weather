"""
overlay/book_trace.py - ORDER-BOOK LIQUIDITY TRACE (phantom-vs-real enabler).

WHY: a CLOB mark to ~1.00 / an early exit is REAL profit only if the order book
actually has enough depth to fill our size at (or near) that price. Today the
ledger books exits with no depth check, so a "clob" win can be phantom (e.g. 1387
shares 'sold' into a $3 book).

WHAT: runs IN-PROCESS with the bot on Railway - no separate VPS process, no extra
env, no standalone poller. On each scan it observes open positions (periodic
samples gated by BOOK_TRACE_POLL_SECONDS) and at entry/exit it snapshots the live
CLOB book via the bot's EXISTING clob client (client.get_orderbook). For every
snapshot it records, market-impact aware:
  * fillable_shares / fill_ratio / achievable VWAP for OUR size
  * depth at the touch price and at neighbour cents (+/-0,1,2,5c)
  * the mid-price curve over the hold (first/last/high/low, max spread)
  * a verdict:  book_confirmed | partial | phantom

It writes data/book_trace.jsonl exactly like the other side-car traces, so
vps_store offloads it to the edge node and /exportdata ships it - nothing else to
wire and nothing to run separately.

Purely observational: never changes a decision, size, or price. Fail-open.
Master switch BOOK_TRACE_ENABLED (default ON). Python 3.9 safe (no f-strings).
"""
import json
import os
import time

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

PATH = "data/book_trace.jsonl"

CONFIRMED = "book_confirmed"
PARTIAL = "partial"
PHANTOM = "phantom"

SETTING_DEFAULTS = {
    "BOOK_TRACE_ENABLED": True,
    "BOOK_TRACE_POLL_SECONDS": 120,        # min seconds between periodic samples / position
    "BOOK_TRACE_SLIPPAGE": 0.02,           # price tolerance when walking the book
    "BOOK_TRACE_MIN_TOUCH_NOTIONAL": 5.0,  # a lone sub-$5 order is NOT real liquidity
    "VPS_HANDLING_BOOK_TRACE": "full",     # offload file mode on the edge node
}

_TRACK = {}      # pid -> curve/state dict
_BUF = []
_FLUSH_EVERY = 100
_BUF_CAP = 3000


def ensure_defaults():
    if Config is None:
        return
    for k, v in SETTING_DEFAULTS.items():
        if not hasattr(Config, k):
            setattr(Config, k, v)


def _enabled():
    return Config is None or bool(getattr(Config, "BOOK_TRACE_ENABLED", True))


def _cfgf(key, default):
    try:
        if Config is None:
            return float(default)
        return float(getattr(Config, key, default))
    except Exception:
        return float(default)


def _pid(pos):
    return (getattr(pos, "id", None) or getattr(pos, "token_id", None)
            or "%s:%s" % (getattr(pos, "city", ""), getattr(pos, "bucket_label", "")))


def _levels(raw):
    """Normalise bids/asks into [(price, size)] floats. Accepts (p,s) tuples/lists
    (clob_client format) or {'price','size'} dicts."""
    out = []
    for lv in (raw or []):
        try:
            if isinstance(lv, dict):
                p = float(lv.get("price"))
                s = float(lv.get("size"))
            else:
                p = float(lv[0])
                s = float(lv[1])
        except (TypeError, ValueError, IndexError):
            continue
        if s > 0:
            out.append((p, s))
    return out


def _fillable(side, levels, shares, limit_price, slippage):
    """Walk the book for OUR size. SELL consumes bids (desc); BUY consumes asks
    (asc). Stops once price passes limit +/- slippage. Market-impact aware.
    Returns (fillable_shares, vwap, ratio, depth_at_touch, levels_used)."""
    shares = float(shares or 0)
    if side == "SELL":
        levels = sorted(levels, key=lambda x: -x[0])
        bound = max(0.0, limit_price * (1.0 - slippage))
        ok = lambda p: p >= bound - 1e-9
    else:
        levels = sorted(levels, key=lambda x: x[0])
        bound = min(1.0, limit_price * (1.0 + slippage))
        ok = lambda p: p <= bound + 1e-9
    remaining = shares
    got = 0.0
    notional = 0.0
    used = 0
    depth_touch = levels[0][1] if levels else 0.0
    for p, s in levels:
        if not ok(p):
            break
        take = min(remaining, s)
        got += take
        notional += take * p
        remaining -= take
        used += 1
        if remaining <= 1e-9:
            break
    vwap = round(notional / got, 4) if got > 0 else None
    ratio = round(min(got / shares, 1.0), 4) if shares > 0 else 0.0
    return round(got, 4), vwap, ratio, round(depth_touch, 4), used


def _classify(ratio, fillable_shares, depth_touch, price, levels_used, min_touch_notional):
    if fillable_shares <= 1e-9 or ratio <= 0.10:
        return PHANTOM
    if ratio >= 0.95:
        # a single tiny order sitting at the touch is not real liquidity
        if levels_used <= 1 and depth_touch * float(price or 0) < min_touch_notional:
            return PARTIAL
        return CONFIRMED
    return PARTIAL


def _neighbor(bids, asks, center, tick=0.01, cents=(0, 1, 2, 5)):
    out = {}
    for c in cents:
        lo = center - c * tick - 1e-9
        hi = center + c * tick + 1e-9
        bd = sum(s for p, s in bids if lo <= p <= hi)
        ad = sum(s for p, s in asks if lo <= p <= hi)
        out["pm%dc" % c] = {"bid_sh": round(bd, 3), "ask_sh": round(ad, 3)}
    return out


def _get_book(client, token_id):
    if client is None or not token_id:
        return None
    try:
        return client.get_orderbook(token_id)
    except Exception:
        return None


def flush():
    """Public flush hook (safe on shutdown / between scans)."""
    global _BUF
    if not _BUF:
        return
    try:
        os.makedirs("data", exist_ok=True)
        with open(PATH, "a") as f:
            f.write("".join(_BUF))
        _BUF = []
    except Exception:
        if len(_BUF) > _BUF_CAP:
            _BUF = _BUF[-_BUF_CAP:]


def _append(row):
    global _BUF
    _BUF.append(json.dumps(row) + "\n")
    if len(_BUF) >= _FLUSH_EVERY:
        flush()
    elif len(_BUF) > _BUF_CAP:
        _BUF = _BUF[-_BUF_CAP:]


def _record(event, pos, client, book=None):
    if not _enabled():
        return None
    token = getattr(pos, "token_id", "") or ""
    book = book or _get_book(client, token)
    if not book:
        return None
    bids = _levels(book.get("bids"))
    asks = _levels(book.get("asks"))
    best_bid = max((p for p, _ in bids), default=None)
    best_ask = min((p for p, _ in asks), default=None)
    if best_bid is not None and best_ask is not None:
        mid = round((best_bid + best_ask) / 2.0, 4)
        spread = round(best_ask - best_bid, 4)
    else:
        mid = best_bid if best_bid is not None else best_ask
        spread = None
    shares = float(getattr(pos, "shares", 0) or 0)
    if event == "entry":
        side = "BUY"
        px = float(getattr(pos, "entry_price", 0) or (mid or 0))
        levels = asks
    else:
        side = "SELL"
        px = float(getattr(pos, "exit_price", 0) or getattr(pos, "current_price", 0) or (mid or 0))
        levels = bids
    slippage = _cfgf("BOOK_TRACE_SLIPPAGE", 0.02)
    min_touch = _cfgf("BOOK_TRACE_MIN_TOUCH_NOTIONAL", 5.0)
    got, vwap, ratio, depth_touch, used = _fillable(side, levels, shares, px, slippage)
    verdict = _classify(ratio, got, depth_touch, px, used, min_touch)
    pid = _pid(pos)
    t = _TRACK.get(pid)
    if t is None:
        t = {"mid_hi": mid, "mid_lo": mid, "spread_hi": spread,
             "first": mid, "last": mid, "n": 0, "last_poll": 0.0, "entered": False}
        _TRACK[pid] = t
    if mid is not None:
        t["mid_hi"] = mid if t["mid_hi"] is None else max(t["mid_hi"], mid)
        t["mid_lo"] = mid if t["mid_lo"] is None else min(t["mid_lo"], mid)
        t["last"] = mid
        if t["first"] is None:
            t["first"] = mid
        t["n"] += 1
    if spread is not None:
        t["spread_hi"] = spread if t["spread_hi"] is None else max(t["spread_hi"], spread)
    row = {
        "t": time.time(), "event": event, "id": pid, "token_id": token,
        "city": getattr(pos, "city", ""), "strategy": getattr(pos, "strategy", ""),
        "bucket": getattr(pos, "bucket_label", ""),
        "side": side, "ref_price": round(px, 4), "shares": round(shares, 4),
        "best_bid": best_bid, "best_ask": best_ask, "mid": mid, "spread": spread,
        "fillable_shares": got, "fill_ratio": ratio, "vwap": vwap,
        "depth_at_touch": depth_touch, "levels_used": used,
        "one_sided": (not bids) or (not asks),
        "verdict": verdict,
        "neighbors": _neighbor(bids, asks, px),
        "curve": {"samples": t["n"], "mid_first": t["first"], "mid_last": t["last"],
                  "mid_high": t["mid_hi"], "mid_low": t["mid_lo"], "spread_high": t["spread_hi"]},
    }
    _append(row)
    return row


def observe(pos, client):
    """Called each scan for every open position. Records the ENTRY snapshot on
    first sight, then periodic samples gated by BOOK_TRACE_POLL_SECONDS. Fully
    fail-open; never raises into the trade loop."""
    if not _enabled():
        return
    try:
        pid = _pid(pos)
        t = _TRACK.get(pid)
        now = time.time()
        if t is None or not t.get("entered"):
            _record("entry", pos, client)
            tt = _TRACK.get(pid)
            if tt is not None:
                tt["entered"] = True
                tt["last_poll"] = now
            return
        interval = _cfgf("BOOK_TRACE_POLL_SECONDS", 120)
        if now - t.get("last_poll", 0.0) >= interval:
            _record("poll", pos, client)
            t["last_poll"] = now
    except Exception:
        pass


def finalize(pos, client):
    """Called at close. Records the EXIT book snapshot (was the exit actually
    fillable, or phantom?) and clears the tracker. Fail-open."""
    if not _enabled():
        return
    try:
        _record("exit", pos, client)
    except Exception:
        pass
    finally:
        _TRACK.pop(_pid(pos), None)
        flush()


ensure_defaults()
