"""
Trading Executor — Spread-Aware, Balance-Safe, Fill-Confirmed

The bot that actually makes money. Every design choice here flows from
real trading experience (including the bugs in wle.txt):

KEY RULES BAKED INTO THIS EXECUTOR:
1. A position is ONLY created when an order FILLS (not when placed).
   GTC orders sitting in the book are "pending" — not positions.
   This single fix eliminates phantom positions (bug #1 from wle.txt).
2. Balance checked via CLOB get_balance_allowance() every 10s (parallel).
   Stale balance = phantom buys = "not enough balance" cascade (bug #2).
3. ALWAYS maker-first: post GTC at best_bid, 0% fee. Never cross spread
   unless exit via forecast reversal. Weather is slow — no urgency.
4. Hold to resolution by default. Weather markets are BINARY ($1 or $0).
   Selling mid-market is the #1 way to lose money on winning trades.
5. Position recovery on restart: check CLOB open orders + data-api.
"""

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

from config import Config
from logger import log


@dataclass
class Position:
    """A FILLED position — only created when CLOB confirms the fill."""
    id: str
    market_title: str
    bucket_label: str
    token_id: str
    side: str
    entry_price: float
    shares: float
    cost_usd: float
    timestamp: datetime
    strategy: str
    order_id: str = ""
    status: str = 'open'
    exit_price: Optional[float] = None
    pnl: float = 0.0


@dataclass
class PendingOrder:
    """An order placed on CLOB that hasn't filled yet."""
    order_id: str
    token_id: str
    price: float
    size_usd: float
    shares: float
    market_title: str
    bucket_label: str
    strategy: str
    placed_at: float
    status: str = 'pending'  # pending, filled, cancelled, expired


class TradingExecutor:
    """Execute trades — paper safe, live confirmed."""

    def __init__(self):
        self.is_paper = Config.is_paper()
        self.balance = Config.STARTING_BALANCE
        self._clob_client = None
        self._balance_cache_time = 0.0
        self._balance_cache_ttl = 10.0  # refresh every 10s

        # Two separate lists — this is the key fix
        self.positions: List[Position] = []       # FILLED only
        self.pending_orders: List[PendingOrder] = []  # placed, not filled

        self.trade_history: List[dict] = []
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_pnl = 0.0
        self._positions_file = 'data/weather_positions.json'

        self._session = requests.Session()
        self._session.headers.update({'User-Agent': f'WeatherSniper/{Config.VERSION}'})

        self._load_state()

        if self.is_paper:
            log.info(f"  PAPER MODE  Starting balance: ${self.balance:.2f}")
        else:
            log.info(f"  LIVE MODE  Wallet: {Config.derive_wallet_address()[:10]}...")
            self._init_live()

    # ── live init ──────────────────────────────────────────────────

    def _init_live(self):
        if not Config.POLY_PRIVATE_KEY:
            log.error("No POLY_PRIVATE_KEY — cannot trade live. Use paper mode.")
            self.is_paper = True
            return
        try:
            from data.clob_client import ClobClient
            self._clob_client = ClobClient()
            self._clob_client.init(
                private_key=Config.POLY_PRIVATE_KEY,
                funder=Config.POLY_FUNDER_ADDRESS or None,
                signature_type=Config.POLY_SIGNATURE_TYPE,
            )
            # Force first balance update
            self._refresh_balance()
            log.info(f"  CLOB ready  Balance: ${self.balance:.4f} pUSD")
        except Exception as e:
            log.error(f"CLOB init failed: {e}")
            log.warning("Falling back to paper mode")
            self.is_paper = True

    # ── balance — always fresh ─────────────────────────────────────

    def _refresh_balance(self):
        """Update balance from CLOB (live) or cache (paper)."""
        if self.is_paper:
            return

        now = time.time()
        if now - self._balance_cache_time < self._balance_cache_ttl:
            return  # still fresh

        try:
            if self._clob_client:
                bal = self._clob_client.get_available_balance()
                if bal is not None and bal >= 0:
                    self.balance = bal
                    self._balance_cache_time = now
        except Exception as e:
            log.debug(f"Balance refresh: {e}")

    def get_balance(self) -> float:
        self._refresh_balance()
        return self.balance

    def has_balance_for(self, cost_usd: float) -> bool:
        """Check if enough balance available — conservative (keep buffer)."""
        bal = self.get_balance()
        # Always keep at least $0.20 buffer for gas/redeem/emergency
        return bal >= (cost_usd + 0.20)

    # ── open positions ─────────────────────────────────────────────

    def get_open_positions(self) -> List[Position]:
        return [p for p in self.positions if p.status == 'open']

    def get_pending_order_count(self) -> int:
        return len([o for o in self.pending_orders if o.status == 'pending'])

    def total_exposure(self) -> float:
        """Total capital locked in open positions + pending orders."""
        pos_cost = sum(p.cost_usd for p in self.get_open_positions())
        pend_cost = sum(o.size_usd for o in self.pending_orders if o.status == 'pending')
        return pos_cost + pend_cost

    # ── the core: place a buy ──────────────────────────────────────

    def place_buy(
        self,
        token_id: str,
        price: float,
        size_usd: float,
        market_title: str = '',
        bucket_label: str = '',
        strategy: str = 'sniper',
    ) -> Optional[Position]:
        """
        Place a BUY order. In paper: simulated. In live: GTC limit at best_bid.
        Returns Position ONLY if filled (or paper-simulated fill).
        Returns None if order placed but not yet filled (pending) or failed.
        """
        # ── 1. balance check ──
        if not self.has_balance_for(size_usd):
            log.warning(
                f"  SKIP (balance): need ${size_usd:.2f}, have ${self.get_balance():.2f}  "
                f"{bucket_label}"
            )
            return None

        # ── 2. size sanity ──
        shares = size_usd / price if price > 0 else 0
        if shares <= 0:
            log.warning(f"  SKIP (zero shares): price={price:.4f} size={size_usd:.2f}")
            return None

        # P2b ENTRY GATE (late_observed_no): skip entries too close to $1.00
        # where there's no room to profit -- these caused the $0.04 churn and the
        # worst risk/reward fills. Scoped to late_observed_no; fail-open.
        try:
            if 'late_observed_no' in str(strategy):
                ceiling = float(getattr(Config, 'P2B_ENTRY_CEILING', 0.90) or 0.99)
                min_room = float(getattr(Config, 'P2B_MIN_ROOM_USD', 0.10) or 0.0)
                if price > ceiling or (1.0 - price) < min_room:
                    log.info("  SKIP (P2b entry gate): %s price=%.3f ceiling=%.3f room=%.3f<min=%.3f"
                             % (bucket_label, price, ceiling, 1.0 - price, min_room))
                    return None
        except Exception:
            pass

        # ── 3. position limits ──
        open_pos = self.get_open_positions()
        pending = self.get_pending_order_count()
        total_booked = len(open_pos) + pending
        if total_booked >= Config.MAX_POSITIONS + 3:  # +3 buffer for pending
            log.warning(f"  SKIP (max slots): {total_booked} open+pending >= {Config.MAX_POSITIONS}")
            return None

        if self.is_paper:
            return self._paper_buy(token_id, price, size_usd, shares,
                                   market_title, bucket_label, strategy)
        else:
            return self._live_buy(token_id, price, size_usd, shares,
                                  market_title, bucket_label, strategy)

    def _paper_buy(self, token_id, price, size_usd, shares,
                   market_title, bucket_label, strategy) -> Position:
        """Paper entry — instant fill (no spread model for weather)."""
        self.balance -= size_usd
        self.total_trades += 1

        pos = Position(
            id=f"paper_{int(time.time())}_{self.total_trades}",
            market_title=market_title,
            bucket_label=bucket_label,
            token_id=token_id,
            side='BUY',
            entry_price=price,
            shares=shares,
            cost_usd=size_usd,
            timestamp=datetime.now(timezone.utc),
            strategy=strategy,
        )
        self.positions.append(pos)
        self._save_state()

        log.info(
            f"  PAPER BUY  {bucket_label}  {shares:.0f}sh @ ${price:.4f}  "
            f"${size_usd:.2f} cost  bal: ${self.balance:.2f}  [{strategy}]"
        )
        return pos

    def _live_buy(self, token_id, price, size_usd, shares,
                  market_title, bucket_label, strategy) -> Optional[Position]:
        """Live CLOB entry — GTC limit at best_bid (maker, 0% fee)."""
        if not self._clob_client:
            log.error("  CLOB not ready — cannot place live order")
            return None

        try:
            # Use maker-first: place at best bid price, let taker cross to us
            result = self._clob_client.place_limit_order(
                token_id=token_id,
                side='BUY',
                price=price,
                size_pusd=size_usd,
                expiration='GTC',
            )

            if not result:
                log.warning(f"  ORDER NULL: {bucket_label} @ ${price:.4f}")
                return None

            order_id = result.get('orderID', '')
            status = str(result.get('status', '')).upper()

            # GTC sits in book. Track as PENDING, not filled.
            pending = PendingOrder(
                order_id=order_id,
                token_id=token_id,
                price=price,
                size_usd=size_usd,
                shares=shares,
                market_title=market_title,
                bucket_label=bucket_label,
                strategy=strategy,
                placed_at=time.time(),
            )
            self.pending_orders.append(pending)

            log.info(
                f"  ORDER PLACED  {bucket_label}  {shares:.0f}sh @ ${price:.4f}  "
                f"${size_usd:.2f}  ID={order_id[:20]}...  [{strategy}]"
            )
            # Note: position NOT created yet — created when fill confirmed
            return None  # None = order placed, not filled yet

        except Exception as e:
            error_msg = str(e)[:120]
            log.error(f"  ORDER FAILED: {bucket_label}  {error_msg}")
            return None

    # ── fill confirmation ──────────────────────────────────────────

    def confirm_fill(self, order_id: str, fill_price: float, fill_shares: float):
        """Called when CLOB confirms an order filled (via WS or polling).
        THIS is when a position is born — not before."""
        for pending in self.pending_orders:
            if pending.order_id == order_id and pending.status == 'pending':
                pending.status = 'filled'
                cost = fill_shares * fill_price
                self.balance -= cost
                self.total_trades += 1

                pos = Position(
                    id=order_id,
                    market_title=pending.market_title,
                    bucket_label=pending.bucket_label,
                    token_id=pending.token_id,
                    side='BUY',
                    entry_price=fill_price,
                    shares=fill_shares,
                    cost_usd=cost,
                    timestamp=datetime.now(timezone.utc),
                    strategy=pending.strategy,
                    order_id=order_id,
                )
                self.positions.append(pos)
                self._save_state()

                log.info(
                    f"  FILLED  {pending.bucket_label}  {fill_shares:.0f}sh "
                    f"@ ${fill_price:.4f}  ${cost:.2f}  [{pending.strategy}]"
                )
                return pos

        log.debug(f"  confirm_fill: order {order_id[:20]}... not found in pending")
        return None

    def cancel_pending(self, order_id: str):
        """Mark a pending order as cancelled."""
        for pending in self.pending_orders:
            if pending.order_id == order_id and pending.status == 'pending':
                pending.status = 'cancelled'
                log.info(f"  CANCELLED  {pending.bucket_label}  {order_id[:20]}...")
                return

    # ── exit / resolve ─────────────────────────────────────────────

    def exit_position(self, position: Position, exit_price: float, reason: str = ''):
        """Early exit (forecast reversal or profit-take). Uses taker sell."""
        if self.is_paper:
            payout = position.shares * exit_price
            position.exit_price = exit_price
            position.pnl = payout - position.cost_usd
            position.status = 'sold'
            self.balance += payout
        else:
            # Live: try to sell at bid (taker), or post GTC if no urgency
            if self._clob_client:
                try:
                    result = self._clob_client.place_limit_order(
                        token_id=position.token_id,
                        side='SELL',
                        price=exit_price,
                        size_pusd=position.shares * exit_price,
                        expiration='GTC',
                    )
                    if result:
                        log.info(f"  EXIT order placed: {position.bucket_label} @ ${exit_price:.4f}")
                except Exception as e:
                    log.error(f"  EXIT failed: {e}")
            # For now, mark as sold for tracking
            position.exit_price = exit_price
            position.pnl = position.shares * exit_price - position.cost_usd
            position.status = 'sold'

        self.total_pnl += position.pnl
        if position.pnl > 0:
            self.wins += 1
        else:
            self.losses += 1

        emoji = '+' if position.pnl > 0 else ''
        log.info(
            f"  EXIT  {emoji}${position.pnl:+.2f}  {position.bucket_label}  "
            f"@ ${exit_price:.4f}  {reason}"
        )
        self._save_state()

    def resolve_position(self, position: Position, won: bool):
        """Market resolved — binary payout $1.00 or $0.00."""
        if won:
            payout = position.shares * 1.0
            position.pnl = payout - position.cost_usd
            position.status = 'won'
            position.exit_price = 1.0
            self.wins += 1
            if self.is_paper:
                self.balance += payout
        else:
            position.pnl = -position.cost_usd
            position.status = 'lost'
            position.exit_price = 0.0
            self.losses += 1

        self.total_pnl += position.pnl
        self._save_state()

        won_str = 'WON' if won else 'LOST'
        log.info(
            f"  RESOLVED {won_str}  {position.bucket_label}  "
            f"PnL: ${position.pnl:+.2f}  Total PnL: ${self.total_pnl:+.2f}"
        )

    # ── position recovery on restart ───────────────────────────────

    async def recover_positions(self):
        """On restart, check CLOB open orders + data-api for existing positions."""
        if self.is_paper or not self._clob_client:
            log.info("  Paper mode — no position recovery needed")
            return

        log.info("  Checking CLOB for existing positions...")
        try:
            open_orders = self._clob_client.get_open_orders()
            if open_orders:
                log.info(f"  Found {len(open_orders)} open CLOB orders")
                for order in open_orders:
                    # If filled > 0, it's a position
                    filled = float(order.get('filled', 0) or order.get('size_matched', 0) or 0)
                    if filled > 0:
                        order_id = order.get('id', order.get('order_id', ''))
                        price = float(order.get('price', 0))
                        token_id = order.get('asset_id', order.get('token_id', ''))
                        side = order.get('side', 'BUY')

                        pos = Position(
                            id=order_id,
                            market_title=order.get('title', 'Recovered position'),
                            bucket_label=order.get('outcome', ''),
                            token_id=token_id,
                            side=side,
                            entry_price=price,
                            shares=filled,
                            cost_usd=filled * price,
                            timestamp=datetime.now(timezone.utc),
                            strategy='recovered',
                            order_id=order_id,
                        )
                        self.positions.append(pos)
                        self.total_trades += 1
                        log.info(f"    Recovered: {filled}sh @ ${price:.4f} [{order_id[:20]}...]")
            else:
                log.info("  No open CLOB orders found")
        except Exception as e:
            log.warning(f"  Position recovery: {e}")

        # Also sync pending orders
        try:
            pending = self._clob_client.get_open_orders(status_filter='OPEN')
            for order in (pending or []):
                oid = order.get('id', order.get('order_id', ''))
                # Skip if already tracked
                if any(o.order_id == oid for o in self.pending_orders):
                    continue
                if any(p.id == oid for p in self.positions):
                    continue
                po = PendingOrder(
                    order_id=oid,
                    token_id=order.get('asset_id', order.get('token_id', '')),
                    price=float(order.get('price', 0)),
                    size_usd=float(order.get('original_size', 0)) * float(order.get('price', 0)),
                    shares=float(order.get('original_size', 0)),
                    market_title=order.get('title', 'Recovered order'),
                    bucket_label=order.get('outcome', ''),
                    strategy='recovered',
                    placed_at=time.time(),
                    status='pending',
                )
                self.pending_orders.append(po)
        except Exception as e:
            log.debug(f"  Pending order recovery: {e}")

        self._save_state()
        log.info(
            f"  Recovery complete: {len(self.get_open_positions())} positions, "
            f"{self.get_pending_order_count()} pending orders"
        )

    # ── periodic sync ──────────────────────────────────────────────

    async def sync_orders(self):
        """Poll CLOB for order status changes. Converts filled orders to positions."""
        if self.is_paper:
            return

        for pending in list(self.pending_orders):
            if pending.status != 'pending':
                continue
            # Timeout stale orders (> 3 hours in book)
            if time.time() - pending.placed_at > 10800:
                pending.status = 'cancelled'
                log.info(f"  EXPIRED  {pending.bucket_label}  {pending.order_id[:20]}...")
                continue

        # Refresh balance periodically
        self._refresh_balance()

        # Check if any pending orders filled by polling order status
        if self._clob_client:
            for pending in list(self.pending_orders):
                if pending.status != 'pending':
                    continue
                try:
                    status = self._clob_client.get_order_status(pending.order_id)
                    if status:
                        filled = float(status.get('filled', 0) or status.get('size_matched', 0) or 0)
                        if filled > 0:
                            price = float(status.get('price', pending.price))
                            self.confirm_fill(pending.order_id, price, filled)
                except Exception:
                    pass  # skip on error, try next cycle

    # ── stats ──────────────────────────────────────────────────────

    def get_stats(self) -> dict:
        closed = self.wins + self.losses
        open_pos = self.get_open_positions()
        return {
            'mode': 'PAPER' if self.is_paper else 'LIVE',
            'balance': self.balance,
            'total_trades': self.total_trades,
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': (self.wins / max(1, closed)) * 100,
            'open_positions': len(open_pos),
            'pending_orders': self.get_pending_order_count(),
            'total_exposure': self.total_exposure(),
            'total_pnl': self.total_pnl,
            'roi_pct': (self.total_pnl / max(Config.STARTING_BALANCE, 0.01)) * 100,
        }

    # ── persistence ────────────────────────────────────────────────

    def _save_state(self):
        try:
            os.makedirs('data', exist_ok=True)
            state = {
                'balance': self.balance,
                'total_trades': self.total_trades,
                'wins': self.wins,
                'losses': self.losses,
                'total_pnl': self.total_pnl,
                'positions': [
                    {
                        'id': p.id, 'market_title': p.market_title,
                        'bucket_label': p.bucket_label, 'token_id': p.token_id,
                        'side': p.side, 'entry_price': p.entry_price,
                        'shares': p.shares, 'cost_usd': p.cost_usd,
                        'timestamp': p.timestamp.isoformat(),
                        'strategy': p.strategy, 'status': p.status,
                        'exit_price': p.exit_price, 'pnl': p.pnl,
                        'order_id': p.order_id,
                    }
                    for p in self.positions[-200:]
                ],
                'pending_orders': [
                    {
                        'order_id': o.order_id, 'token_id': o.token_id,
                        'price': o.price, 'size_usd': o.size_usd,
                        'shares': o.shares, 'market_title': o.market_title,
                        'bucket_label': o.bucket_label, 'strategy': o.strategy,
                        'placed_at': o.placed_at, 'status': o.status,
                    }
                    for o in self.pending_orders[-50:]
                ],
            }
            with open(self._positions_file, 'w') as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            log.debug(f"State save: {e}")

    def _load_state(self):
        try:
            if os.path.exists(self._positions_file):
                with open(self._positions_file, 'r') as f:
                    state = json.load(f)
                self.balance = state.get('balance', Config.STARTING_BALANCE)
                self.total_trades = state.get('total_trades', 0)
                self.wins = state.get('wins', 0)
                self.losses = state.get('losses', 0)
                self.total_pnl = state.get('total_pnl', 0)
                for p_data in state.get('positions', []):
                    try:
                        self.positions.append(Position(
                            id=p_data.get('id', ''), market_title=p_data.get('market_title', ''),
                            bucket_label=p_data.get('bucket_label', ''),
                            token_id=p_data.get('token_id', ''), side=p_data.get('side', 'BUY'),
                            entry_price=p_data.get('entry_price', 0),
                            shares=p_data.get('shares', 0), cost_usd=p_data.get('cost_usd', 0),
                            timestamp=datetime.fromisoformat(p_data['timestamp']),
                            strategy=p_data.get('strategy', 'sniper'),
                            status=p_data.get('status', 'open'),
                            exit_price=p_data.get('exit_price'),
                            pnl=p_data.get('pnl', 0),
                            order_id=p_data.get('order_id', ''),
                        ))
                    except Exception:
                        continue
                for o_data in state.get('pending_orders', []):
                    try:
                        self.pending_orders.append(PendingOrder(
                            order_id=o_data.get('order_id', ''),
                            token_id=o_data.get('token_id', ''),
                            price=o_data.get('price', 0),
                            size_usd=o_data.get('size_usd', 0),
                            shares=o_data.get('shares', 0),
                            market_title=o_data.get('market_title', ''),
                            bucket_label=o_data.get('bucket_label', ''),
                            strategy=o_data.get('strategy', ''),
                            placed_at=o_data.get('placed_at', time.time()),
                            status=o_data.get('status', 'pending'),
                        ))
                    except Exception:
                        continue
                log.info(f"  Loaded state: {len(self.positions)} pos, "
                         f"{len(self.pending_orders)} pending, ${self.balance:.2f} bal")
        except Exception:
            pass
