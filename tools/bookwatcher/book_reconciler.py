"""
book_reconciler.py - confirm phantom vs per-book exits on the NEXT run.

Joins book_trace snapshots (written by book_watcher.py) back to the closed
positions in paper_trades, and for every close answers one question:

    Could the book actually have filled our size at the price we booked?

For each close it picks the book snapshot nearest to (and at/just-before) the
exit timestamp, recomputes fillable size + achievable VWAP, and labels the exit
book_confirmed / partial / phantom. It then recomputes a BOOK-TRUE realized P&L
(using the achievable VWAP instead of the snapped exit) and compares it to the
clob-booked P&L, per strategy and overall.

Works offline. Python 3.9 compatible. Reads JSONL (VPS bundle) or CSV exports.

Usage:
    python book_reconciler.py --trades <paper_trades_dir_or_csv> \
                              --books  <book_trace_dir> \
                              --out    reconciled.csv
"""
from __future__ import annotations

import argparse
import csv
import glob
import json
import os
from typing import Any, Dict, List, Optional

from book_watcher import Book, fillable, classify_fill, CONFIRMED, PARTIAL, PHANTOM, SELL, BUY

CLOSE_ACTIONS = {"SELL", "SETTLE"}


def _load_records(path: str) -> List[Dict[str, Any]]:
    recs: List[Dict[str, Any]] = []
    if os.path.isdir(path):
        files = sorted(glob.glob(os.path.join(path, "*.jsonl"))) + sorted(
            glob.glob(os.path.join(path, "*.csv"))
        )
    else:
        files = [path]
    for f in files:
        if f.endswith(".jsonl"):
            for ln in open(f):
                ln = ln.strip()
                if ln:
                    recs.append(json.loads(ln))
        elif f.endswith(".csv"):
            with open(f) as fh:
                for row in csv.DictReader(fh):
                    recs.append(dict(row))
    return recs


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _nearest_book(books_by_token: Dict[str, List[Dict[str, Any]]], token: str, ts: float,
                  tolerance_s: float = 3600.0) -> Optional[Dict[str, Any]]:
    """Latest snapshot at or before ts (fallback: closest within tolerance)."""
    snaps = books_by_token.get(str(token), [])
    if not snaps:
        return None
    before = [s for s in snaps if s["ts"] <= ts + 1e-6]
    if before:
        return max(before, key=lambda s: s["ts"])
    nearest = min(snaps, key=lambda s: abs(s["ts"] - ts))
    return nearest if abs(nearest["ts"] - ts) <= tolerance_s else None


def _book_from_snapshot(snap: Dict[str, Any]) -> Optional[Book]:
    """Rehydrate a Book from a stored snapshot if raw levels were kept.
    Falls back to a synthetic top-of-book from best_bid/best_ask when only
    summary fields were stored."""
    if "bids" in snap and "asks" in snap:
        return Book.from_raw(snap.get("token_id", ""),
                             {"bids": snap["bids"], "asks": snap["asks"]}, ts=snap["ts"])
    bb, ba = snap.get("best_bid"), snap.get("best_ask")
    if bb is None and ba is None:
        return None
    raw = {"bids": [], "asks": []}
    # use recorded neighbour depth if present to approximate size at touch
    depth = 0.0
    nb = snap.get("neighbors", {}).get("pm0c", {})
    if bb is not None:
        raw["bids"] = [{"price": bb, "size": nb.get("bid_shares", depth) or 1e9}]
    if ba is not None:
        raw["asks"] = [{"price": ba, "size": nb.get("ask_shares", depth) or 1e9}]
    return Book.from_raw(snap.get("token_id", ""), raw, ts=snap["ts"])


def reconcile(trades: List[Dict[str, Any]], books: List[Dict[str, Any]],
              slippage: float = 0.02) -> List[Dict[str, Any]]:
    for b in books:
        b["ts"] = _f(b.get("ts"))
    by_token: Dict[str, List[Dict[str, Any]]] = {}
    for b in books:
        by_token.setdefault(str(b.get("token_id")), []).append(b)

    out: List[Dict[str, Any]] = []
    for t in trades:
        if t.get("action") not in CLOSE_ACTIONS:
            continue
        token = str(t.get("token_id", t.get("slug", "")))
        ts = _f(t.get("ts_epoch", t.get("t")))
        shares = _f(t.get("shares"))
        booked_exit = _f(t.get("exit_price"))
        booked_pnl = _f(t.get("pnl"))
        cost = _f(t.get("cost_usd"))
        snap = _nearest_book(by_token, token, ts) if ts else None
        verdict = "no_book"
        vwap = None
        fill_ratio = None
        book_pnl = None
        if snap is not None:
            book = _book_from_snapshot(snap)
            if book is not None:
                fr = fillable(SELL, book, shares, booked_exit or (book.mid or 0), slippage)
                verdict = classify_fill(fr)
                vwap = fr.vwap
                fill_ratio = fr.fill_ratio
                if vwap is not None:
                    # book-true realized: sell fillable shares at achievable VWAP
                    proceeds = fr.fillable_shares * vwap
                    per_share_cost = (cost / shares) if shares else 0.0
                    book_pnl = round(proceeds - per_share_cost * fr.fillable_shares, 4)
        out.append({
            "pid": t.get("id", t.get("pid")),
            "city": t.get("city"),
            "strategy": t.get("strategy"),
            "settle_source": t.get("settle_source"),
            "shares": shares,
            "booked_exit": booked_exit,
            "booked_pnl": booked_pnl,
            "book_verdict": verdict,
            "book_vwap": vwap,
            "fill_ratio": fill_ratio,
            "book_true_pnl": book_pnl,
            "phantom_gap": (round(booked_pnl - book_pnl, 4)
                            if book_pnl is not None else None),
        })
    return out


def summarize(rows: List[Dict[str, Any]]) -> None:
    from collections import defaultdict
    agg = defaultdict(lambda: [0, 0.0, 0.0])  # verdict -> [n, booked, book_true]
    tot_booked = tot_true = 0.0
    for r in rows:
        v = r["book_verdict"]
        agg[v][0] += 1
        agg[v][1] += r["booked_pnl"] or 0.0
        if r["book_true_pnl"] is not None:
            agg[v][2] += r["book_true_pnl"]
        tot_booked += r["booked_pnl"] or 0.0
        tot_true += (r["book_true_pnl"] if r["book_true_pnl"] is not None else (r["booked_pnl"] or 0.0))
    print("verdict         n   booked_pnl   book_true_pnl")
    for v, (n, b, bt) in sorted(agg.items()):
        print("%-14s %3d  %10.2f   %12.2f" % (v, n, b, bt))
    print("-" * 48)
    print("%-14s %3d  %10.2f   %12.2f" % ("TOTAL", len(rows), tot_booked, tot_true))
    phantom = sum(1 for r in rows if r["book_verdict"] == PHANTOM)
    print("\nPhantom exits: %d / %d closes. Phantom-inflated P&L: %.2f"
          % (phantom, len(rows), tot_booked - tot_true))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--trades", required=True, help="paper_trades dir or CSV")
    ap.add_argument("--books", required=True, help="book_trace dir written by book_watcher")
    ap.add_argument("--slippage", type=float, default=0.02)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()
    trades = _load_records(args.trades)
    books = _load_records(args.books)
    rows = reconcile(trades, books, args.slippage)
    summarize(rows)
    if args.out and rows:
        with open(args.out, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print("wrote %s (%d rows)" % (args.out, len(rows)))


if __name__ == "__main__":
    main()
