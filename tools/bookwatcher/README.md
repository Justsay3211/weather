# Book Watcher — liquidity truth for weather_pol

**Premise (agreed):** exiting early at ~0.99 is *good* — you lock profit, free capital,
and skip resolution risk. The only thing that makes an early/`clob` exit **real** instead
of **phantom** is whether the order book had **enough depth to fill our size** at (or near)
that price. So we stop arguing about `clob` vs `polymarket` and instead **measure the book**
at entry and exit.

## What it does
- **Snapshots the book** at every entry and exit (`on_entry` / `on_exit`).
- **Samples periodically** while a position is open (`watch_open_positions`) so we capture the
  **mid-price curve**: first/last/high/low and max spread over the hold.
- Records depth **at the touch price AND neighbours** (±0, 1, 2, 5¢), both shares and notional.
- **Classifies every fill** with market-impact-aware VWAP math:
  - `book_confirmed` — full size fills within slippage → **REAL**
  - `partial` — only part of size fills → **HAIRCUT** (records achievable VWAP + fill ratio)
  - `phantom` — ~no depth (e.g. 1,387 shares into a $3 book) → **FAKE**
- Downgrades a "confirmed" touch that is a single tiny order (< `min_touch_notional`) — a lone
  $3 bid at 0.99 is **not** real liquidity.

## Files
- `book_watcher.py` — live watcher + pure depth math (`fillable`, `neighbor_depth`, `classify_fill`).
- `book_reconciler.py` — **next-run confirmation**: joins snapshots to `paper_trades` closes,
  recomputes book-true P&L, and reports how much of the booked P&L was phantom.
- `test_book_watcher.py` — 8 unit tests (deep-book confirm, thin-book phantom, partial, neighbours,
  tiny-touch downgrade). Network-free.

## Wiring into the bot (VPS side)
The watcher only needs a `get_book(token_id)` callable returning bids/asks. With
`py_clob_client`:

```python
from py_clob_client.client import ClobClient
from book_watcher import BookWatcher, watch_open_positions

client = ClobClient(host, key=..., chain_id=137)
watcher = BookWatcher(store_dir="store/book_trace", get_book=lambda tid: client.get_order_book(tid))

# In trading/position_manager.py:
#   - right after a BUY fills:      watcher.on_entry(pos)
#   - right before/at any exit:     rec = watcher.on_exit(pos)
#         if rec["verdict"] == "phantom": skip/flag the exit (do NOT book $1.00)
#         elif rec["verdict"] == "partial": book only rec["fill"]["fillable_shares"] at VWAP
#
# pos needs at least: {id, token_id, shares, entry_price/exit_price, city, strategy}

# Background sampler (records the curve while positions are open):
watch_open_positions(watcher, list_open=open_positions_provider, interval_s=60)
```

Run the sampler in a **background thread/process** so it never blocks the trade loop.

## Confirming phantom vs per-book on the NEXT run
After a run has written `book_trace/`:

```bash
python book_reconciler.py --trades store/paper_trades --books store/book_trace --out reconciled.csv
```

Output per verdict: count, **booked P&L vs book-true P&L**, and total **phantom-inflated P&L**.
That is the number that finally tells us whether the "$70" is real cash or a mark.

## Recommended exit policy (once wired)
1. On exit, require `book_confirmed` for the full size, **or** book only the `fillable_shares`
   at the achievable **VWAP** (never the snapped 1.00).
2. Treat `phantom` exits as *not exited* — hold to real resolution (or don't count the P&L).
3. Log the neighbour curve so we can later size positions to the **depth actually available**
   at the price band we trade (this directly caps the 1,387-share-into-$3-book problem at entry).
