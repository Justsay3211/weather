"""
book_watcher.py - order-book liquidity verifier for weather_pol.

WHY THIS EXISTS
---------------
Exiting early at ~0.99 is GOOD -- IF the book can actually fill us. A CLOB mark to
0.99/1.00 is REAL profit only when there is enough depth to absorb our position size
at (or near) that price, at BOTH entry and exit. Today the ledger books exits at a
snapped price with no depth check, so "clob" wins can be phantom.

This module snapshots the order book at entry and exit for every position, samples it
periodically while the position is open, records depth AT the touch price and at
neighbouring price levels, tracks the mid-price curve (highs / lows / spread), and
classifies each fill as:
    book_confirmed  -> full size fillable within slippage  (REAL)
    partial         -> only part of size fillable          (HAIRCUT)
    phantom         -> ~no depth; the mark was fantasy      (FAKE)

The live sampling runs on the VPS/bot (needs network for real books). All depth math
is network-free and unit-tested in test_book_watcher.py. Python 3.9 compatible.

NEXT-RUN CONFIRMATION
---------------------
book_reconciler.py joins these snapshots back to paper_trades closes so you can prove,
per position, whether each exit was per-book (real) or phantom -- and recompute a
book-true P&L to compare against the clob-booked P&L.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

Level = Tuple[float, float]  # (price, size_in_shares)

BUY = "BUY"
SELL = "SELL"
CONFIRMED = "book_confirmed"
PARTIAL = "partial"
PHANTOM = "phantom"


# ---------------------------------------------------------------------------
# Book normalisation
# ---------------------------------------------------------------------------
def _norm_levels(raw: Any) -> List[Level]:
    """Accept dicts ({price,size}) or py-clob-client OrderSummary objects."""
    out: List[Level] = []
    for lv in (raw or []):
        if isinstance(lv, dict):
            p = lv.get("price", lv.get("p"))
            s = lv.get("size", lv.get("s"))
        else:
            p = getattr(lv, "price", None)
            s = getattr(lv, "size", None)
        if p is None or s is None:
            continue
        p = float(p)
        s = float(s)
        if s > 0:
            out.append((p, s))
    return out


@dataclass
class Book:
    token_id: str
    ts: float
    bids: List[Level]  # best (highest) bid first
    asks: List[Level]  # best (lowest) ask first

    @staticmethod
    def from_raw(token_id: str, raw: Any, ts: Optional[float] = None) -> "Book":
        if isinstance(raw, dict):
            rb = raw.get("bids", raw.get("buys", []))
            ra = raw.get("asks", raw.get("sells", []))
        else:
            rb = getattr(raw, "bids", [])
            ra = getattr(raw, "asks", [])
        bids = sorted(_norm_levels(rb), key=lambda x: -x[0])
        asks = sorted(_norm_levels(ra), key=lambda x: x[0])
        return Book(str(token_id), ts if ts is not None else time.time(), bids, asks)

    @property
    def best_bid(self) -> Optional[float]:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Optional[float]:
        return self.asks[0][0] if self.asks else None

    @property
    def mid(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None:
            return round((bb + ba) / 2.0, 4)
        return bb if bb is not None else ba

    @property
    def spread(self) -> Optional[float]:
        bb, ba = self.best_bid, self.best_ask
        if bb is not None and ba is not None:
            return round(ba - bb, 4)
        return None

    def one_sided(self) -> bool:
        return not self.bids or not self.asks


# ---------------------------------------------------------------------------
# Fill / depth math  (pure, unit-tested)
# ---------------------------------------------------------------------------
@dataclass
class FillResult:
    side: str
    want_shares: float
    limit_price: float
    price_bound: float          # worst price we allowed (limit +/- slippage)
    fillable_shares: float      # how many shares the book can actually absorb
    fill_ratio: float           # fillable / want  (clamped 0..1)
    vwap: Optional[float]       # volume-weighted avg fill price achievable
    slippage_vs_limit: Optional[float]
    levels_consumed: int
    depth_at_touch: float       # size available at best price on our side
    enough: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "side": self.side,
            "want_shares": round(self.want_shares, 4),
            "limit_price": self.limit_price,
            "price_bound": self.price_bound,
            "fillable_shares": self.fillable_shares,
            "fill_ratio": self.fill_ratio,
            "vwap": self.vwap,
            "slippage_vs_limit": self.slippage_vs_limit,
            "levels_consumed": self.levels_consumed,
            "depth_at_touch": self.depth_at_touch,
            "enough": self.enough,
        }


def fillable(
    side: str,
    book: Book,
    shares: float,
    limit_price: float,
    slippage: float = 0.02,
    max_price: float = 1.0,
) -> FillResult:
    """Walk the book to see how much of `shares` can fill within a price bound.

    BUY  -> consume ASKS from best upward, stop once price > limit*(1+slippage).
    SELL -> consume BIDS from best downward, stop once price < limit*(1-slippage).
    Returns achievable VWAP and whether the whole size fills (market-impact aware).
    """
    side = side.upper()
    shares = float(shares)
    limit_price = float(limit_price)
    if side == BUY:
        levels = book.asks
        bound = min(max_price, limit_price * (1.0 + slippage))
        allowed = lambda p: p <= bound + 1e-9
    elif side == SELL:
        levels = book.bids
        bound = max(0.0, limit_price * (1.0 - slippage))
        allowed = lambda p: p >= bound - 1e-9
    else:
        raise ValueError("side must be BUY or SELL")

    remaining = shares
    got = 0.0
    notional = 0.0
    used = 0
    depth_touch = levels[0][1] if levels else 0.0
    for (p, s) in levels:
        if not allowed(p):
            break
        take = min(remaining, s)
        got += take
        notional += take * p
        remaining -= take
        used += 1
        if remaining <= 1e-9:
            break

    vwap = round(notional / got, 4) if got > 0 else None
    slip = None
    if vwap is not None:
        slip = round(abs(vwap - limit_price), 4)
    ratio = round(min(got / shares, 1.0), 4) if shares > 0 else 0.0
    return FillResult(
        side=side,
        want_shares=shares,
        limit_price=round(limit_price, 4),
        price_bound=round(bound, 4),
        fillable_shares=round(got, 4),
        fill_ratio=ratio,
        vwap=vwap,
        slippage_vs_limit=slip,
        levels_consumed=used,
        depth_at_touch=round(depth_touch, 4),
        enough=(got + 1e-9 >= shares),
    )


def neighbor_depth(
    book: Book,
    center: float,
    cents: Tuple[int, ...] = (0, 1, 2, 5),
    tick: float = 0.01,
) -> Dict[str, Dict[str, float]]:
    """Depth (shares + notional) within +/- N cents of `center` on each side.
    Captures the neighbourhood shape, not just the exact touch price."""
    out: Dict[str, Dict[str, float]] = {}
    for c in cents:
        lo = center - c * tick - 1e-9
        hi = center + c * tick + 1e-9
        bid_sh = sum(s for p, s in book.bids if lo <= p <= hi)
        ask_sh = sum(s for p, s in book.asks if lo <= p <= hi)
        bid_no = sum(s * p for p, s in book.bids if lo <= p <= hi)
        ask_no = sum(s * p for p, s in book.asks if lo <= p <= hi)
        out["pm%dc" % c] = {
            "bid_shares": round(bid_sh, 3),
            "ask_shares": round(ask_sh, 3),
            "bid_notional": round(bid_no, 3),
            "ask_notional": round(ask_no, 3),
        }
    return out


def classify_fill(fr: FillResult, min_ratio: float = 0.95, phantom_ratio: float = 0.10) -> str:
    """book_confirmed if ~all size fills; phantom if ~nothing; partial otherwise."""
    if fr.fillable_shares <= 1e-9 or fr.fill_ratio <= phantom_ratio:
        return PHANTOM
    if fr.fill_ratio >= min_ratio:
        return CONFIRMED
    return PARTIAL


# ---------------------------------------------------------------------------
# Per-position mid-price curve (highs / lows over the hold)
# ---------------------------------------------------------------------------
@dataclass
class PositionCurve:
    pid: str
    token_id: str
    n: int = 0
    mid_hi: float = float("-inf")
    mid_lo: float = float("inf")
    spread_hi: float = float("-inf")
    first_mid: Optional[float] = None
    last_mid: Optional[float] = None

    def update(self, mid: Optional[float], spread: Optional[float]) -> None:
        if mid is not None:
            if self.first_mid is None:
                self.first_mid = mid
            self.last_mid = mid
            self.mid_hi = max(self.mid_hi, mid)
            self.mid_lo = min(self.mid_lo, mid)
            self.n += 1
        if spread is not None:
            self.spread_hi = max(self.spread_hi, spread)

    def summary(self) -> Dict[str, Any]:
        f = lambda x: None if x in (float("inf"), float("-inf")) else round(x, 4)
        return {
            "samples": self.n,
            "mid_first": f(self.first_mid) if self.first_mid is not None else None,
            "mid_last": f(self.last_mid) if self.last_mid is not None else None,
            "mid_high": f(self.mid_hi),
            "mid_low": f(self.mid_lo),
            "spread_high": f(self.spread_hi),
        }


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------
class BookWatcher:
    """Records book snapshots for entries, exits and periodic samples.

    get_book(token_id) -> raw book (dict or py-clob-client OrderBookSummary).
    On the VPS wire it to py_clob_client.get_order_book(token_id).
    """

    def __init__(
        self,
        store_dir: str,
        get_book: Callable[[str], Any],
        slippage: float = 0.02,
        min_touch_notional: float = 5.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store_dir = store_dir
        self.get_book = get_book
        self.slippage = slippage
        self.min_touch_notional = min_touch_notional
        self.clock = clock
        self.curves: Dict[str, PositionCurve] = {}
        os.makedirs(store_dir, exist_ok=True)

    # -- persistence -------------------------------------------------------
    def _write(self, rec: Dict[str, Any]) -> None:
        day = time.strftime("%Y%m%d", time.gmtime(rec["ts"]))
        path = os.path.join(self.store_dir, day + ".jsonl")
        with open(path, "a") as fh:
            fh.write(json.dumps(rec) + "\n")

    def snapshot(self, token_id: str) -> Book:
        return Book.from_raw(token_id, self.get_book(token_id), ts=self.clock())

    # -- events ------------------------------------------------------------
    def _record(self, kind: str, pos: Dict[str, Any], book: Optional[Book]) -> Dict[str, Any]:
        token = str(pos["token_id"])
        pid = str(pos.get("id", pos.get("pid", token)))
        shares = float(pos.get("shares", 0) or 0)
        book = book or self.snapshot(token)
        side = BUY if kind == "entry" else SELL
        px = pos.get("entry_price") if kind == "entry" else pos.get("exit_price")
        px = float(px if px is not None else (book.mid or 0.0))
        fr = fillable(side, book, shares, px, self.slippage)
        label = classify_fill(fr)
        # a lone tiny order at the touch is not real liquidity
        touch_notional = fr.depth_at_touch * px
        if label == CONFIRMED and touch_notional < self.min_touch_notional and fr.levels_consumed <= 1:
            label = PARTIAL
        cur = self.curves.setdefault(pid, PositionCurve(pid, token))
        cur.update(book.mid, book.spread)
        rec = {
            "ts": book.ts,
            "event": kind,               # entry | exit | poll
            "pid": pid,
            "token_id": token,
            "city": pos.get("city"),
            "strategy": pos.get("strategy"),
            "side": side,
            "ref_price": round(px, 4),
            "best_bid": book.best_bid,
            "best_ask": book.best_ask,
            "mid": book.mid,
            "spread": book.spread,
            "one_sided": book.one_sided(),
            "fill": fr.to_dict(),
            "verdict": label,            # book_confirmed | partial | phantom
            "neighbors": neighbor_depth(book, px),
            "curve": cur.summary(),
        }
        self._write(rec)
        return rec

    def on_entry(self, pos: Dict[str, Any], book: Optional[Book] = None) -> Dict[str, Any]:
        return self._record("entry", pos, book)

    def on_exit(self, pos: Dict[str, Any], book: Optional[Book] = None) -> Dict[str, Any]:
        return self._record("exit", pos, book)

    def poll_once(self, pos: Dict[str, Any], book: Optional[Book] = None) -> Dict[str, Any]:
        return self._record("poll", pos, book)


def watch_open_positions(
    watcher: BookWatcher,
    list_open: Callable[[], List[Dict[str, Any]]],
    interval_s: float = 60.0,
    stop: Optional[Callable[[], bool]] = None,
    max_iters: Optional[int] = None,
) -> int:
    """Live sampler. Run on the VPS in a background thread/process.
    Polls every open position every `interval_s` and records the book curve.
    Returns the number of poll cycles executed."""
    iters = 0
    while True:
        if stop is not None and stop():
            break
        if max_iters is not None and iters >= max_iters:
            break
        for pos in list_open():
            try:
                watcher.poll_once(pos)
            except Exception as exc:  # never let one bad book kill the loop
                print("book poll error pid=%s: %s" % (pos.get("id"), exc))
        iters += 1
        time.sleep(interval_s)
    return iters
