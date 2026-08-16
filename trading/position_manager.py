"""
Position Manager v2 — Full lifecycle with stop-loss, take-profit, weekly memory.

Features:
- Per-position P&L tracking (individual + aggregate)
- Stop-loss: auto-sell if price drops below threshold
- Take-profit: auto-sell if price rises above target
- Weekly performance memory (tracks weekly stats for ML learning)
- Context cleanup: free memory for closed/resolved markets
- Separate tracking: bought, holding, sold, won, lost, redeemed
- Market context pool: only active markets consume memory

PAPER-REALISM OVERHAUL (make dry-run behave like real trading):
- Settlement TRUTH comes from Polymarket's resolved outcome via MarketResolver
  (works even AFTER a market closes — fixes the "random values after close" bug).
  The weather observation is only a confirmation metric, never the source.
- update_prices FREEZES the last good price instead of writing 0/404 garbage.
- Pre-close win conclusion: flags a near-certain win in the final minutes when
  the venue price is >= 95/99% (signal/label only; real settlement still books
  at resolution).
- Conserved PnL ledger invariant is asserted after every state change.
- Every BUY/SELL/SETTLE/REDEEM is written to data/paper_trades.jsonl with the
  signal, strategy, edge, grade and why-bought reason.
"""

import os
import json
import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from config import Config
from logger import log

# Paper-realism helpers (degrade gracefully if unavailable).
try:
    from data.market_resolver import MarketResolver
except Exception:  # pragma: no cover
    MarketResolver = None
try:
    from trading import paper_engine as pe
except Exception:  # pragma: no cover
    try:
        import paper_engine as pe  # type: ignore
    except Exception:
        pe = None


def _cfg(name: str, default):
    """Read an optional Config flag with a safe default (decouples this module
    from the config edit landing)."""
    return getattr(Config, name, default)


# Strategies whose legs form an any-one-wins basket: grouped under a single
# "Box N" everywhere (open view, entry alert, resolution summary) and ALWAYS
# held to resolution (never stop-loss / trailing-stop / thesis-exit). peaker
# cool/warm baskets join peak_cluster here so they group + hold identically.
BASKET_STRATEGIES = ('peak_cluster', 'peaker_cool_basket', 'peaker_warm_basket')

# Exit reasons that the dashboard's exit-policy loop notifies directly (it sends
# ONE close alert per returned position). They are still CLOSED here for correct
# PnL / ledger / W-L accounting, but the _notify_close callback is suppressed for
# them so we don't double-notify. 'manual' is user-initiated from Telegram.
DASHBOARD_NOTIFIED_REASONS = (
    'manual', 'flip_stop', 'flip_book', 'flip_book_mid', 'flip_timeout',
    'profit_cap_book', 'thesis_invalidated', 'ml_review_sell',
)


@dataclass
class PositionRules:
    """Per-position risk management rules."""
    stop_loss_pct: float = -80.0       # sell if ROI drops below this %
    take_profit_pct: float = 300.0     # sell if ROI exceeds this %
    take_profit_price: float = 0.50    # sell YES token if price rises above this
    max_hold_hours: float = 48.0       # max time to hold before forced review
    trailing_stop: bool = False        # enable trailing stop
    trail_pct: float = 20.0            # trailing stop percentage from peak


@dataclass
class TrackedPosition:
    """A position being tracked by the bot."""
    id: str
    market_title: str
    bucket_label: str
    token_id: str
    condition_id: str
    entry_price: float
    shares: float
    cost_usd: float
    current_price: float
    current_value: float
    entry_time: datetime
    resolution_time: Optional[datetime]
    strategy: str
    city: str = ''
    slug: str = ''
    # Status lifecycle: open → (won|lost|sold|redeemed)
    status: str = 'open'
    pnl: float = 0.0
    realized_pnl: float = 0.0
    redeemable: bool = False
    # Risk management
    peak_price: float = 0.0           # highest price seen (for trailing stop)
    stop_loss_pct: float = -80.0
    take_profit_price: float = 0.50
    # Metadata
    exit_price: Optional[float] = None
    exit_time: Optional[datetime] = None
    exit_reason: str = ''             # 'resolution', 'stop_loss', 'take_profit', 'manual'
    # --- Paper-realism / observability fields ---
    edge_at_entry: float = 0.0        # edge (our prob - price) when bought
    grade: float = 0.0                # stability grade at entry
    lock_confidence: float = 0.0      # observed lock-confidence at entry
    signal: str = ''                  # which signal/strategy fired the buy
    reason: str = ''                  # human-readable why-bought
    last_good_price: float = 0.0      # last non-garbage price (freeze source)
    current_price_stale: bool = False # True when live price could not be read
    preclose_locked: bool = False     # flagged near-certain win before close
    settle_source: str = ''           # 'polymarket' | 'preclose_lock' | 'clob'
    # --- Request-23 fields ---
    hold_to_resolution: bool = False  # baskets / hold legs: never stop/trail/thesis-exit
    cluster_box: str = ''             # peak-cluster grouping label, e.g. 'Box 1'
    flip_max_hold_minutes: float = 0.0  # quick_flip book-or-cut window (persisted)


    @property
    def unrealized_pnl(self) -> float:
        if self.status == 'open':
            return self.current_value - self.cost_usd
        return self.pnl

    @property
    def roi_pct(self) -> float:
        if self.cost_usd <= 0:
            return 0
        return (self.unrealized_pnl / self.cost_usd) * 100

    @property
    def hold_hours(self) -> float:
        return (datetime.now(timezone.utc) - self.entry_time).total_seconds() / 3600

    @property
    def is_expired(self) -> bool:
        if self.resolution_time:
            return datetime.now(timezone.utc) > self.resolution_time
        return False

    @property
    def minutes_to_close(self) -> Optional[float]:
        if not self.resolution_time:
            return None
        return (self.resolution_time - datetime.now(timezone.utc)).total_seconds() / 60.0


@dataclass
class WeeklyStats:
    """Weekly performance summary for ML memory."""
    week_start: str           # ISO date
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pnl: float = 0.0
    roi_pct: float = 0.0
    best_city: str = ''
    best_strategy: str = ''
    avg_entry_price: float = 0.0
    avg_edge: float = 0.0


@dataclass
class MarketContext:
    """Active market context (freed when market resolves)."""
    slug: str
    city: str
    market_type: str
    resolution_time: Optional[datetime]
    our_probability: float = 0.0
    market_price: float = 0.0
    edge: float = 0.0
    forecast_temp: float = 0.0
    n_models: int = 0
    last_updated: float = 0.0
    active: bool = True



class PositionManager:
    """Full position lifecycle manager with risk controls and memory."""

    def __init__(self):
        self.positions: List[TrackedPosition] = []
        self.weekly_history: List[WeeklyStats] = []
        self.market_contexts: Dict[str, MarketContext] = {}
        self.paper_balance = Config.STARTING_BALANCE
        self.total_deposited = Config.STARTING_BALANCE
        self.total_redeemed = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        # Monotonic counter for naming peak-cluster baskets ("Box 1", "Box 2"...).
        # LEGACY: kept only so OLD saved state still loads. Real numbering now
        # uses per-strategy counters below, so peak_cluster / peaker_cool_basket /
        # peaker_warm_basket each get their OWN "Box N" sequence (they used to
        # SHARE one counter -- a cool basket would steal the cluster's next
        # number, the "box 1,2,3" bug). Cross-strategy uniqueness is handled by
        # grouping on (strategy, box) wherever the label is consumed.
        self.cluster_box_seq = 0
        self.cluster_box_seqs: Dict[str, int] = {}
        # Optional callback invoked after a position is closed/resolved (set by
        # the dashboard to route Telegram close/resolution alerts). The hook
        # skips reason=='manual' (flip/thesis exits notify directly, otherwise
        # they'd be double-notified with the wrong label).
        self._notify_close = None
        # Grouped peak-cluster resolution: fire ONE summary per basket once ALL
        # its legs have settled (wired by the dashboard). `_announced_cluster_close`
        # de-dupes so each "Box N" is summarised exactly once.
        self._notify_cluster_close = None
        self._announced_cluster_close = set()
        self._state_file = 'data/positions.json'
        self._weekly_file = 'data/weekly_memory.json'
        self._paper_trades_file = _cfg('PAPER_TRADE_LOG', 'data/paper_trades.jsonl')
        self._session = requests.Session()
        self._session.headers['User-Agent'] = f'WeatherSniper/{Config.VERSION}'
        # Polymarket resolved-outcome reader (settlement source of truth).
        self._resolver = MarketResolver() if MarketResolver else None
        self._load_state()
        self._load_weekly()

    # ============================
    # BALANCE
    # ============================

    def get_balance(self) -> float:
        if Config.is_paper():
            return self.paper_balance
        return self._get_onchain_balance() or self.paper_balance

    def get_portfolio_value(self) -> float:
        open_val = sum(p.current_value for p in self.positions if p.status == 'open')
        return self.get_balance() + open_val

    def get_total_pnl(self) -> float:
        realized = sum(p.pnl for p in self.positions if p.status != 'open')
        unrealized = sum(p.unrealized_pnl for p in self.positions if p.status == 'open')
        return realized + unrealized

    def _get_onchain_balance(self) -> Optional[float]:
        wallet = Config.POLY_PROXY_WALLET
        if not wallet:
            return None
        try:
            resp = self._session.get(
                'https://data-api.polymarket.com/positions',
                params={'user': wallet}, timeout=10,
            )
            # Derive from known positions
            return None
        except Exception:
            return None


    # ============================
    # POSITION LIFECYCLE
    # ============================

    def add_position(self, token_id: str, condition_id: str, entry_price: float,
                     shares: float, cost_usd: float, market_title: str,
                     bucket_label: str, strategy: str, city: str = '',
                     slug: str = '', resolution_time: datetime = None,
                     edge: float = 0.0, reason: str = '', grade: float = 0.0,
                     lock_confidence: float = 0.0, signal: str = '',
                     hold_to_resolution: bool = False, cluster_box: str = '',
                     flip_max_hold_minutes: float = 0.0) -> Optional[TrackedPosition]:
        """Add new position — checks balance FIRST, only tracks if order succeeds."""
        # === VALIDATE SHARES (must be positive) ===
        if shares <= 0 or cost_usd <= 0 or entry_price <= 0:
            return None

        # === DUPLICATE GUARD ===
        # Block re-buying the SAME outcome with the SAME strategy while an order
        # for it is still open or pending. A DIFFERENT strategy on the same market
        # (e.g. spread leg vs confident) is allowed.
        for p in self.positions:
            if (p.token_id == token_id and p.strategy == strategy
                    and p.status in ('open', 'pending')):
                log.debug(f"⏭️  SKIP dup: {city} {bucket_label[:25]} already {p.status} [{strategy}]")
                return None

        # === MIN ORDER: GTC needs >=5 shares AND >= $1 notional ===
        # Polymarket GTC floors to 5 shares; FOK/FAK need >= $1. We require both
        # (max of the two) so no order is dust. Applied in PAPER too so paper
        # simulates exactly what the venue would accept — otherwise grade/liquidity
        # size-trimming can produce sub-minimum "0-share" orders that can't fill.
        # Bump the spend up to the minimum (capped at balance by the check below).
        min_notional = max(Config.MIN_ORDER_SIZE, round(5 * entry_price, 2))
        if cost_usd < min_notional:
            cost_usd = min_notional
            shares = cost_usd / entry_price

        # === BALANCE CHECK (after min-order bump so it reflects real spend) ===
        # Skip (don't retry) when balance is insufficient — surfaced at INFO so
        # it's visible. Freed balance from sells/resolutions is picked up next scan.
        if not Config.is_paper():
            available = self.get_live_balance()   # CLOB query (10s cache, force-refreshed after each order)
            if available is not None and cost_usd > available:
                log.info(f"⏭️  SKIP {city} {bucket_label[:22]} — need ${cost_usd:.2f}, only ${available:.2f} (waiting for positions to resolve)")
                return None
        else:
            if cost_usd > self.paper_balance:
                log.info(f"⏭️  SKIP {city} {bucket_label[:22]} — need ${cost_usd:.2f}, only ${self.paper_balance:.2f} (waiting for positions to resolve)")
                return None

        # Determine take-profit based on entry price
        if entry_price < 0.05:
            tp_price = min(0.50, entry_price * 8)
        elif entry_price < 0.15:
            tp_price = min(0.60, entry_price * 5)
        else:
            tp_price = min(0.85, entry_price * 2.5)

        # === PAPER REALISTIC FILL ===
        # When enabled, walk the live ask ladder so the paper fill price/size and
        # partial fills mirror what the venue would actually give us. This is the
        # difference between paper "feeling random" and matching real trading.
        fill_note = ''
        if Config.is_paper() and pe is not None and _cfg('PAPER_REALISTIC_FILL', True):
            asks = self._fetch_ask_ladder(token_id)
            if asks:
                fr = pe.simulate_taker_fill(asks, cost_usd, max_price=0.99)
                if not fr.ok:
                    log.info(f"⏭️  SKIP {city} {bucket_label[:22]} — no fillable asks (book empty/above cap)")
                    return None
                entry_price = round(fr.fill_price, 4)
                shares = fr.filled_shares
                cost_usd = round(fr.filled_usd, 4)
                fill_note = fr.reason + (f" across {fr.levels_used} lvls" if fr.levels_used else '')
                if fr.partial:
                    log.info(f"⚖️  PARTIAL FILL {city} {bucket_label[:22]} — {shares:.0f}sh @ ${entry_price:.4f} ({fill_note})")

        # === LIVE: Place order FIRST. GTC sits in book until filled. ===
        # CRITICAL FIX: a GTC order != a filled position. Track as PENDING
        # until the fill is confirmed. This prevents phantom positions (wle.txt bug #1).
        if not Config.is_paper():
            order_result = self._place_live_order(token_id, entry_price, cost_usd, shares)
            if not order_result:
                return None  # Order failed — DO NOT track

            order_id = order_result.get('orderID', f"live_{int(time.time())}")

            # Force-refresh balance after placing order (prevents stale-balance cascade)
            if hasattr(self, '_balance_cache_time'):
                self._balance_cache_time = 0

            # Track as PENDING — NOT a filled position yet
            pending = TrackedPosition(
                id=order_id,
                market_title=market_title,
                bucket_label=bucket_label,
                token_id=token_id,
                condition_id=condition_id,
                entry_price=entry_price,
                shares=shares,
                cost_usd=cost_usd,
                current_price=entry_price,
                current_value=shares * entry_price,
                entry_time=datetime.now(timezone.utc),
                resolution_time=resolution_time,
                strategy=strategy,
                city=city,
                slug=slug,
                peak_price=entry_price,
                stop_loss_pct=Config.STOP_LOSS_PCT,
                take_profit_price=tp_price,
                status='pending',    # <-- KEY FIX: pending, not open
            )
            self.positions.append(pending)
            self.total_trades += 1
            pos = pending  # shared tail below returns `pos`
            log.info(
                f"  ORDER PLACED  {city} {bucket_label[:35]}  "
                f"{shares:.0f}sh @ ${entry_price:.4f}  ${cost_usd:.2f}  "
                f"ID={order_id[:20]}...  [{strategy}]  STATUS: PENDING (awaiting fill)"
            )

        else:
            # PAPER MODE
            pos = TrackedPosition(
                id=f"paper_{int(time.time())}_{self.total_trades}",
                market_title=market_title,
                bucket_label=bucket_label,
                token_id=token_id,
                condition_id=condition_id,
                entry_price=entry_price,
                shares=shares,
                cost_usd=cost_usd,
                current_price=entry_price,
                current_value=shares * entry_price,
                entry_time=datetime.now(timezone.utc),
                resolution_time=resolution_time,
                strategy=strategy,
                city=city,
                slug=slug,
                peak_price=entry_price,
                stop_loss_pct=Config.STOP_LOSS_PCT,
                take_profit_price=tp_price,
            )
            self.positions.append(pos)
            self.total_trades += 1
            self.paper_balance -= cost_usd
            log.info(f"\033[92m📋 PAPER BUY: {city} {bucket_label[:30]} | "
                     f"{shares:.0f}sh @ ${entry_price:.4f} | cost=${cost_usd:.2f}"
                     + (f" | {fill_note}" if fill_note else "") + "\033[0m")

        # === Observability metadata (common to both modes) ===
        pos.edge_at_entry = edge
        pos.reason = reason
        pos.grade = grade
        pos.lock_confidence = lock_confidence
        pos.signal = signal or strategy
        pos.last_good_price = pos.current_price
        # Request-23 / Request-28: hold-to-resolution flag (baskets + observed/
        # forced holds) and peak-cluster grouping label. ALL basket strategies
        # (peak_cluster + peaker cool/warm baskets) are ALWAYS hold-to-res.
        pos.hold_to_resolution = bool(hold_to_resolution) or strategy in BASKET_STRATEGIES
        pos.cluster_box = cluster_box
        pos.flip_max_hold_minutes = float(flip_max_hold_minutes or 0.0)

        # Register market context
        if slug and slug not in self.market_contexts:
            self.market_contexts[slug] = MarketContext(
                slug=slug, city=city,
                market_type='highest_temperature' if 'highest' in market_title.lower() else 'lowest_temperature',
                resolution_time=resolution_time,
                edge=edge,
            )

        # Rich per-trade log line (signal / strategy / why / edge) for paper.
        if Config.is_paper():
            self._log_paper_trade('BUY', pos, extra={'fill': fill_note})

        self._save_state()
        self._assert_ledger()
        log.info(f"📌 NEW: {city} {bucket_label} | {shares:.0f}sh @ ${entry_price:.4f} "
                 f"| edge={edge:+.0%} | {pos.signal} | TP=${tp_price:.2f} | SL={Config.STOP_LOSS_PCT}%"
                 + (f" | why: {reason}" if reason else ""))
        return pos

    def _fetch_ask_ladder(self, token_id: str) -> List[Tuple[float, float]]:
        """Read the live ask ladder [(price, size_shares), ...] ascending. Used to
        produce a realistic paper fill. Returns [] if the book can't be read."""
        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_book_client') or self._book_client is None:
                self._book_client = ClobClient()
            book = self._book_client.get_orderbook(token_id)
            asks = (book or {}).get('asks') or []
            ladder: List[Tuple[float, float]] = []
            for lvl in asks:
                if isinstance(lvl, dict):
                    price = float(lvl.get('price', 0) or 0)
                    size = float(lvl.get('size', 0) or 0)
                else:  # (price, size) tuple/list
                    price = float(lvl[0]); size = float(lvl[1])
                if price > 0 and size > 0:
                    ladder.append((price, size))
            ladder.sort(key=lambda x: x[0])
            return ladder
        except Exception as e:
            log.debug(f"ask ladder fetch failed {token_id[:12]}...: {e}")
            return []

    def _place_live_order(self, token_id: str, price: float, size_usd: float, shares: float) -> Optional[Dict]:
        """Place a real GTC limit order on Polymarket CLOB V2."""
        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_clob_client') or self._clob_client is None:
                self._clob_client = ClobClient()
                self._clob_client.init_py_clob_client(
                    private_key=Config.POLY_PRIVATE_KEY,
                    funder=Config.get_funder_address() or None,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )

            # Update balance/allowance with correct V2 params
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )
                self._clob_client._py_clob_client.update_balance_allowance(params)
            except Exception:
                pass

            result = self._clob_client.place_limit_order(
                token_id=token_id,
                side='BUY',
                price=price,
                size_pusd=size_usd,
                expiration='GTC',
                neg_risk=True,  # Weather markets are ALWAYS neg_risk
            )
            return result
        except Exception as e:
            log.error(f"\033[91m❌ CLOB error: {e}\033[0m")
            return None

    def get_live_balance(self) -> Optional[float]:
        """Get REAL available balance from CLOB (correct V2 API with AssetType)."""
        now = time.time()
        if hasattr(self, '_balance_cache_time') and (now - self._balance_cache_time) < 10:
            return getattr(self, '_balance_cache_value', None)

        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_clob_client') or self._clob_client is None:
                self._clob_client = ClobClient()
                self._clob_client.init_py_clob_client(
                    private_key=Config.POLY_PRIVATE_KEY,
                    funder=Config.get_funder_address() or None,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )

            # CORRECT V2 balance call — requires BalanceAllowanceParams with AssetType.COLLATERAL
            try:
                from py_clob_client_v2.clob_types import BalanceAllowanceParams, AssetType
                params = BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )
                bal_data = self._clob_client._py_clob_client.get_balance_allowance(params)
                if bal_data:
                    raw = float(bal_data.get('balance', 0))
                    available = raw / 1_000_000  # pUSD has 6 decimals
                    self._balance_cache_value = available
                    self._balance_cache_time = now
                    return available
            except ImportError:
                pass
            except Exception as e:
                log.debug(f"V2 balance API: {e}")

            # Fallback: on-chain pUSD balance via RPC
            wallet = Config.POLY_PROXY_WALLET or Config.derive_wallet_address()
            if wallet:
                onchain = self._clob_client.get_pusd_balance_onchain(wallet)
                if onchain is not None:
                    self._balance_cache_value = onchain
                    self._balance_cache_time = now
                    return onchain
        except Exception as e:
            log.debug(f"Balance fetch error: {e}")

        return None

    def recover_positions_on_start(self):
        """On bot restart: check CLOB for open orders and existing positions."""
        if Config.is_paper():
            return

        log.info("🔄 Recovering positions from CLOB...")
        try:
            from data.clob_client import ClobClient
            if not hasattr(self, '_clob_client') or self._clob_client is None:
                self._clob_client = ClobClient()
                self._clob_client.init_py_clob_client(
                    private_key=Config.POLY_PRIVATE_KEY,
                    funder=Config.get_funder_address() or None,
                    signature_type=Config.POLY_SIGNATURE_TYPE,
                )

            # Get open orders from CLOB (needs valid API key)
            try:
                open_orders = self._clob_client._py_clob_client.get_open_orders()
                if open_orders:
                    log.info(f"  Found {len(open_orders)} open orders on CLOB")
                    for order in open_orders:
                        oid = order.get('id', '')[:12]
                        price = order.get('price', 0)
                        size = order.get('original_size', 0)
                        log.info(f"    Order {oid}... | ${price} x {size}sh")
            except Exception as ce:
                if '401' in str(ce) or 'Unauthorized' in str(ce):
                    log.warning("  CLOB API key invalid/expired — skipping order recovery.")
                    log.warning("  >> Re-derive your API creds: the bot will auto-derive if you")
                    log.warning("  >> clear POLY_API_KEY/SECRET/PASSPHRASE from .env (keep private key).")
                else:
                    log.warning(f"  CLOB order recovery skipped: {str(ce)[:80]}")

            # Get positions from data-api (PUBLIC — no API key needed, works even on 401)
            wallet = Config.POLY_PROXY_WALLET or Config.derive_wallet_address()
            if wallet:
                resp = self._session.get(
                    'https://data-api.polymarket.com/positions',
                    params={'user': wallet}, timeout=10
                )
                if resp.status_code == 200:
                    positions = resp.json()
                    weather_pos = [p for p in positions
                                   if any(w in (p.get('title','') or '').lower()
                                          for w in ['temperature', '°c', '°f'])
                                   and float(p.get('size', 0) or 0) > 0]
                    log.info(f"📋 Found {len(weather_pos)} weather positions on-chain")
                    for p in weather_pos[:5]:
                        title = (p.get('title',''))[:40]
                        size = float(p.get('size', 0))
                        price = float(p.get('avgPrice', 0))
                        cur = float(p.get('curPrice', 0))
                        log.info(f"  📈 {title} | {size:.0f}sh @ ${price:.4f} → ${cur:.4f}")
        except Exception as e:
            log.warning(f"Position recovery failed: {e}")

    def get_open_positions(self) -> List[TrackedPosition]:
        """Return FILLED open positions (NOT pending GTC orders still in the book)."""
        return [p for p in self.positions if p.status == 'open']

    def get_pending_orders(self) -> List[TrackedPosition]:
        """Return orders placed but not yet filled (GTC sitting in the book)."""
        return [p for p in self.positions if p.status == 'pending']

    def sync_pending_orders(self):
        """Poll CLOB to check if pending GTC orders have filled. Moves filled
        orders from 'pending' to 'open' status. THIS is the missing piece that
        caused the phantom position bug."""
        pending = self.get_pending_orders()
        if not pending:
            return

        for pos in pending:
            try:
                # Check if order filled via CLOB status
                if not hasattr(self, '_clob_client') or not self._clob_client:
                    continue
                status = self._clob_client.get_order_status(pos.id)
                if status:
                    filled = float(status.get('filled', 0) or status.get('size_matched', 0) or 0)
                    if filled > 0:
                        actual_price = float(status.get('price', pos.entry_price))
                        pos.shares = filled
                        pos.cost_usd = filled * actual_price
                        pos.current_price = actual_price
                        pos.current_value = filled * actual_price
                        pos.entry_price = actual_price
                        pos.status = 'open'
                        if not Config.is_paper():
                            self.paper_balance -= pos.cost_usd
                        log.info(
                            f"  FILLED  {pos.city} {pos.bucket_label[:35]}  "
                            f"{filled:.0f}sh @ ${actual_price:.4f}  "
                            f"${pos.cost_usd:.2f} cost"
                        )
            except Exception as e:
                log.debug(f"  Order sync {pos.id[:16]}...: {e}")

    def get_positions_by_status(self, status: str) -> List[TrackedPosition]:
        return [p for p in self.positions if p.status == status]

    def get_positions_by_city(self, city: str) -> List[TrackedPosition]:
        return [p for p in self.positions if p.city.lower() == city.lower()]

    def get_redeemable_positions(self) -> List[TrackedPosition]:
        return [p for p in self.positions if p.redeemable and p.status in ('open', 'won')]

    # === Per-strategy basket numbering ("Box 1", "Box 2", ...) ===
    # Each basket strategy keeps its OWN counter, so peak_cluster,
    # peaker_cool_basket and peaker_warm_basket number independently. The stored
    # label stays "Box N" (clean); cross-strategy uniqueness is handled by
    # grouping on (strategy, box) wherever the label is consumed.
    def peek_cluster_box(self, strategy: str = 'peak_cluster') -> str:
        """Label for this strategy's NEXT basket WITHOUT consuming the number."""
        return f"Box {self.cluster_box_seqs.get(strategy, 0) + 1}"

    def commit_cluster_box(self, strategy: str = 'peak_cluster') -> str:
        """Consume + persist this strategy's next basket number; return label."""
        n = self.cluster_box_seqs.get(strategy, 0) + 1
        self.cluster_box_seqs[strategy] = n
        self.cluster_box_seq = max(self.cluster_box_seq, n)  # keep legacy field sane
        self._save_state()
        return f"Box {n}"

    def next_cluster_box(self, strategy: str = 'peak_cluster') -> str:
        """Back-compat convenience: peek + commit."""
        return self.commit_cluster_box(strategy)

    def reset_fresh(self, starting_balance: float = None):
        """RESTART FRESH — clear ALL positions and reset the paper balance to a
        new starting balance (defaults to Config.STARTING_BALANCE). Wipes the
        in-memory state and persists the clean slate so a Railway redeploy /
        Telegram Restart begins with no carried-over positions, counters or
        cluster numbering. Invoked by the dashboard restart hook (which the
        Telegram Restart button calls). Returns the new balance."""
        bal = (float(starting_balance) if starting_balance is not None
               else float(Config.STARTING_BALANCE))
        self.positions = []
        self.market_contexts = {}
        self.paper_balance = bal
        self.total_deposited = bal
        self.total_redeemed = 0.0
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.cluster_box_seq = 0
        self.cluster_box_seqs = {}
        self._announced_cluster_close = set()
        # Persist a customised starting balance so it survives the reset.
        try:
            Config.STARTING_BALANCE = bal
        except Exception:
            pass
        self._save_state()
        self._assert_ledger()
        log.info(f"♻️  RESTART FRESH — cleared all positions, balance reset to ${bal:.2f}")
        return bal

    def apply_starting_balance(self, new_balance: float = None) -> dict:
        """Re-apply the configured starting balance to the LIVE paper balance.

        Fixes the bug where setting a new balance (e.g. 300) then tapping Start
        kept trading with the old persisted balance: _load_state restores
        paper_balance from data/positions.json, so a changed STARTING_BALANCE
        never reached the live ledger. We rebase ONLY when the book is empty
        (no open/pending/closed positions) so the PnL ledger invariant stays
        intact; if any positions exist the caller should tell the user to
        Restart (which wipes + rebases via reset_fresh). Returns a status dict.
        """
        bal = (float(new_balance) if new_balance is not None
               else float(Config.STARTING_BALANCE))
        # Always remember the requested balance so a later Restart uses it.
        try:
            Config.STARTING_BALANCE = bal
        except Exception:
            pass
        if self.positions:
            active = [p for p in self.positions if p.status in ('open', 'pending')]
            return {'applied': False,
                    'reason': 'positions_open' if active else 'has_history',
                    'balance': self.paper_balance, 'target': bal,
                    'open': len(active)}
        # Empty book -> safe to rebase the whole paper ledger to the new balance.
        self.paper_balance = bal
        self.total_deposited = bal
        self.total_redeemed = 0.0
        self._save_state()
        try:
            self._assert_ledger()
        except Exception:
            pass
        log.info(f"💰 Starting balance applied — paper balance set to ${bal:.2f}")
        return {'applied': True, 'balance': bal, 'target': bal}


    # ============================
    # STOP-LOSS & TAKE-PROFIT
    # ============================

    def check_risk_triggers(self) -> List[TrackedPosition]:
        """Check all open positions for stop-loss / take-profit triggers."""
        triggered = []
        for pos in self.get_open_positions():
            # Update peak price for trailing stop
            if pos.current_price > pos.peak_price:
                pos.peak_price = pos.current_price

            # MAE/MFE overlay: record this scan's price for EVERY open position
            # (backtest path logging). Purely observational, fail-open.
            try:
                from overlay import mae_mfe as _mm
                _mm.observe(pos)
            except Exception:
                pass

            # BOOK-TRACE overlay: sample the live CLOB book for this open
            # position (entry snapshot on first sight, then periodic polls) so
            # we can later prove an exit is book-confirmed vs phantom. Uses the
            # bot's existing clob client; runs in-process, fail-open.
            try:
                from overlay import book_trace as _bt
                _bt.observe(pos, getattr(self, '_clob_client', None))
            except Exception:
                pass

            # HOLD-TO-RESOLUTION EXEMPTION (basket strategies + observed/forced
            # hold legs): an any-one-wins basket only profits if EVERY leg rides to
            # settlement (one winning bucket pays $1 and covers the whole basket),
            # and observed hold legs are EV+ to resolution. So we NEVER
            # stop-loss / trailing-stop / take-profit-exit these here — they settle
            # in check_resolutions. (Very-bad observed legs may still be cut by the
            # STRICT thesis-exit, which itself exempts baskets.) This fixes the bug
            # where the stop-loss kept closing peak-cluster legs on a dip.
            # P2b HARD FORCE-EXIT (wipeout guard): late_observed_no legs were
            # riding to $0 (the -100% wipeouts) because they hold to resolution
            # and bypass the 50% stop. If such a leg has collapsed to a near-total
            # loss but is still sellable, cut it. Scoped to late_observed_no ONLY
            # -- baskets / peak_cluster are never touched. Fully fail-open.
            try:
                if (bool(getattr(Config, 'P2B_FORCE_EXIT_ENABLED', True))
                        and 'late_observed_no' in str(getattr(pos, 'strategy', ''))
                        and pos.entry_price >= 0.03
                        and pos.roi_pct <= -90.0
                        and pos.current_price > 0.005
                        and not pos.current_price_stale):
                    self._close_position(pos, self._modeled_exit_price(pos, 'stop_loss'), 'stop_loss')
                    triggered.append(pos)
                    log.info("🧨 FORCE EXIT (wipeout guard): %s %s @ $%.4f (ROI=%.0f%%)"
                             % (pos.city, pos.bucket_label, pos.current_price, pos.roi_pct))
                    continue
            except Exception:
                pass

            if getattr(pos, 'hold_to_resolution', False):
                continue

            # QUICK-FLIP EXEMPTION: flips have their OWN +10% book / -5% stop
            # policy in exit_policies.check_flip_exits. The generic take-profit
            # ladder below was mislabeling flip LOSSES as 'take_profit' — it fires
            # on current_price >= take_profit_price, which for NO-side / higher
            # entries can sit BELOW entry, so a green "TAKE PROFIT" got booked at a
            # negative PnL (the -27% bug). Skip flips here; only their dedicated
            # policy closes them.
            if pos.strategy == 'quick_flip':
                continue

            # TAKE-PROFIT: price rose above target
            if pos.current_price >= pos.take_profit_price:
                self._close_position(pos, self._modeled_exit_price(pos, 'take_profit'), 'take_profit')
                triggered.append(pos)
                log.info(f"🎯 TAKE PROFIT: {pos.city} {pos.bucket_label} "
                         f"@ ${pos.current_price:.4f} (entry ${pos.entry_price:.4f}) "
                         f"PnL=${pos.pnl:+.2f}")
                continue

            # STOP-LOSS: ROI dropped below threshold
            if pos.roi_pct <= pos.stop_loss_pct:
                # For very cheap entries, don't stop-loss — hold to resolution
                # (binary markets: either $0 or $1, no point selling at $0.002)
                if pos.entry_price < 0.03:
                    continue  # hold to resolution for ultra-cheap
                self._close_position(pos, self._modeled_exit_price(pos, 'stop_loss'), 'stop_loss')
                triggered.append(pos)
                log.info(f"🛑 STOP LOSS: {pos.city} {pos.bucket_label} "
                         f"@ ${pos.current_price:.4f} (ROI={pos.roi_pct:.0f}%)")
                continue

            # TRAILING STOP (if enabled): price dropped X% from peak. Only after a
            # BIG run-up (>= TRAILING_MIN_PEAK_MULT x entry) so we don't choke a
            # good position that still has room to run — the user flagged trailing
            # exits firing too early on winners.
            min_peak_mult = float(_cfg('TRAILING_MIN_PEAK_MULT', 3.0))
            if pos.peak_price > pos.entry_price * min_peak_mult:
                trail_threshold = pos.peak_price * (1 - Config.TRAILING_STOP_PCT / 100)
                if pos.current_price < trail_threshold:
                    self._close_position(pos, self._modeled_exit_price(pos, 'trailing_stop'), 'trailing_stop')
                    triggered.append(pos)
                    log.info(f"📉 TRAILING STOP: {pos.city} {pos.bucket_label} "
                             f"@ ${pos.current_price:.4f} (peak=${pos.peak_price:.4f})")

        if triggered:
            self._save_state()
            self._assert_ledger()
        return triggered

    def _modeled_exit_price(self, pos, reason):
        """P2: realistic taker fill for market EXITS ONLY (stop-loss /
        take-profit / trailing). Applies a modeled slippage haircut to the
        current price to emulate crossing the spread into available liquidity
        (the liquidity-aware sell that existed but was never wired). NEVER used
        for settlement (won/lost) -- those book at Polymarket's released price.
        If a live CLOB order book is reachable it walks it via
        paper_engine.simulate_taker_fill; otherwise it falls back to the
        slippage model. Fully fail-open: any error returns the raw price.
        """
        try:
            px = float(getattr(pos, 'current_price', 0.0) or 0.0)
            if px <= 0.0 or reason in ('won', 'lost'):
                return px
            try:
                client = getattr(self, '_clob_client', None)
                if client is not None and pe is not None and getattr(pos, 'token_id', ''):
                    ob = client.get_orderbook(pos.token_id)
                    bids = (ob or {}).get('bids') or []
                    ladder = [(float(b[0]), float(b[1])) for b in bids if b and float(b[0]) > 0]
                    notional = float(getattr(pos, 'shares', 0.0) or 0.0) * px
                    if ladder and notional > 0:
                        fr = pe.simulate_taker_fill(ladder, notional, max_price=1.0)
                        if fr.ok:
                            return max(0.0, min(1.0, fr.fill_price))
            except Exception:
                pass
            slip = float(getattr(Config, 'PAPER_EXIT_SLIPPAGE_PCT', 1.5) or 0.0) / 100.0
            return max(0.0, px * (1.0 - slip))
        except Exception:
            return float(getattr(pos, 'current_price', 0.0) or 0.0)

    def _close_position(self, pos: TrackedPosition, exit_price: float, reason: str):
        """Close a position (sell or resolve)."""
        pos.exit_price = exit_price
        pos.exit_time = datetime.now(timezone.utc)
        pos.exit_reason = reason
        # A settlement (won/lost) books at $1/$0; ANY other reason is a market
        # SELL -> status 'sold'. This MUST cover every exit-policy reason
        # (flip_stop / flip_book / flip_book_mid / flip_timeout / profit_cap_book
        # / thesis_invalidated): previously they matched NO branch below, so PnL
        # stayed 0.0 even on a clear loss, the paper balance was never credited,
        # and W/L was never recorded ("even in minus it shows 0").
        is_resolution = reason in ('won', 'lost')
        pos.status = reason if is_resolution else 'sold'

        # Calculate PnL
        if not is_resolution:
            # Sold at market — PnL = (exit_price - entry_price) * shares
            pos.pnl = (exit_price - pos.entry_price) * pos.shares
            pos.realized_pnl = pos.pnl
            # -- WIN/LOSS ACCOUNTING FOR MARKET EXITS --
            # BUGFIX: previously ONLY 'won'/'lost' resolutions touched
            # self.wins/self.losses. Every market SELL — thesis-exit,
            # flip book-or-cut, stop-loss, trailing-stop, manual — was booked
            # with a real PnL but NEVER counted in the W/L record, so a thesis
            # exit closed at a loss was invisible to win-rate (it silently
            # dropped the loser and INFLATED WR). A market exit is a real,
            # realized outcome: a gain is a WIN, a loss is a LOSS. Count it by
            # realized-PnL sign; a true break-even (|pnl| < 1e-9) is neither.
            if pos.pnl > 1e-9:
                self.wins += 1
            elif pos.pnl < -1e-9:
                self.losses += 1
        elif reason == 'won':
            pos.pnl = pos.shares - pos.cost_usd
            pos.realized_pnl = pos.pnl
            self.wins += 1
        elif reason == 'lost':
            pos.pnl = -pos.cost_usd
            pos.realized_pnl = pos.pnl
            self.losses += 1

        # Credit balance in paper mode
        if Config.is_paper() and not is_resolution:
            self.paper_balance += pos.cost_usd + pos.pnl

        # -- OVERLAY hooks (fail-open, no core behavior change) --------------
        # (1) TAKEOUT skim: fence a % of each WIN's profit into the untouchable
        #     pool (up to target). (2) ADAPTIVE boost: record realized outcome
        #     per strategy. (3) MAE/MFE: finalize the recorded intra-trade path.
        try:
            if getattr(pos, 'pnl', 0.0) and pos.pnl > 0:
                from overlay import reserve_takeout as _rt
                _rt.on_realized_pnl(pos.pnl)
        except Exception as _e:
            log.debug(f"takeout skim skipped: {_e}")
        try:
            from overlay import adaptive_boost as _ab
            _ab.record(getattr(pos, 'strategy', ''), getattr(pos, 'pnl', 0.0))
        except Exception as _e:
            log.debug(f"adaptive record skipped: {_e}")
        try:
            from overlay import mae_mfe as _mm
            _mm.finalize(pos)
        except Exception as _e:
            log.debug(f"mae/mfe finalize skipped: {_e}")
        try:
            from overlay import book_trace as _bt
            _bt.finalize(pos, getattr(self, '_clob_client', None))
        except Exception as _e:
            log.debug(f"book trace finalize skipped: {_e}")

        # Free market context if no more open positions for this slug
        self._maybe_free_context(pos.slug)

        # Trade-log the close (paper): SETTLE for win/lose, SELL for exits.
        if Config.is_paper():
            action = 'SETTLE' if reason in ('won', 'lost') else 'SELL'
            self._log_paper_trade(action, pos)

        # Fire the close/resolution alert callback (Telegram), if wired. Skip
        # reasons the dashboard already notifies directly to avoid double-notify.
        # Basket legs (peak_cluster + peaker cool/warm baskets) are GROUPED:
        # instead of one won/lost alert per leg, we wait until EVERY leg of the
        # basket ("Box N") has resolved, then emit ONE grouped resolution summary
        # (which leg won + payout, the losing legs + their loss, net basket PnL).
        # Non-basket closes notify per-pos.
        if reason not in DASHBOARD_NOTIFIED_REASONS:
            box = getattr(pos, 'cluster_box', '') or ''
            if box and getattr(pos, 'strategy', '') in BASKET_STRATEGIES:
                self._maybe_notify_cluster_close(box, getattr(pos, 'strategy', ''))
            elif getattr(self, '_notify_close', None):
                try:
                    self._notify_close(pos)
                except Exception as e:
                    log.debug(f"close notify failed: {e}")


    # ============================
    # RESOLUTION & REDEMPTION
    # ============================

    def _maybe_notify_cluster_close(self, box: str, strategy: str = ''):
        """Once ALL legs of a basket ("Box N") have resolved, emit ONE grouped
        resolution summary via the wired callback. Fires only once per
        (strategy, box) (de-duped through `_announced_cluster_close`). Keyed by
        strategy too, so a peak_cluster "Box 1" and a peaker_cool_basket "Box 1"
        (independent per-strategy counters) are NEVER merged. Covers peak_cluster
        AND peaker cool/warm baskets."""
        if not box:
            return
        legs = [p for p in self.positions
                if (getattr(p, 'cluster_box', '') or '') == box
                and getattr(p, 'strategy', '') in BASKET_STRATEGIES
                and (not strategy or getattr(p, 'strategy', '') == strategy)]
        if not legs:
            return
        # Hold the summary until no leg is still open/pending.
        if any(l.status in ('open', 'pending') for l in legs):
            return
        key = (strategy, box)
        if key in self._announced_cluster_close:
            return
        self._announced_cluster_close.add(key)
        cb = getattr(self, '_notify_cluster_close', None)
        if cb:
            try:
                cb(box, legs)
            except Exception as e:
                log.debug(f"cluster close notify failed: {e}")

    def check_resolutions(self):
        """Resolve open positions using Polymarket's ACTUAL settled outcome.

        Source of truth (in priority order):
          1. MarketResolver — Gamma resolved outcomePrices for the slug. Works
             even after the market closes, so positions never get "stuck".
          2. Legacy CLOB-price fallback (only when the resolver can't answer).
        Also runs the pre-close win-conclusion pass first.
        """
        # Flag near-certain wins before close (signal/label only).
        self.check_preclose_locks()

        open_pos = self.get_open_positions()
        if not open_pos:
            return

        for pos in open_pos:
            try:
                if self._resolve_via_polymarket(pos):
                    continue
                # Fallback: legacy CLOB price check.
                self._legacy_resolution_check(pos)
            except Exception as e:
                log.debug(f"resolution check {pos.bucket_label[:20]}: {e}")

        self._save_state()
        self._assert_ledger()

    def _resolve_via_polymarket(self, pos: TrackedPosition) -> bool:
        """Settle from Polymarket's resolved outcome. Returns True if settled."""
        if not self._resolver or pe is None or not pos.slug:
            return False
        res = self._resolver.get_resolution(pos.slug)
        if not res or not res.resolved:
            return False

        bucket = self._match_bucket(res, pos)
        if bucket is None or bucket.won is None:
            return False

        # Determine which leg we hold: YES if our token == the bucket's YES token.
        side = 'yes'
        if bucket.token_id_yes and pos.token_id and bucket.token_id_yes != pos.token_id:
            side = 'no'

        decision = pe.decide_settlement(
            side=side,
            venue_won=bucket.won,
            venue_resolved=res.resolved,
            weather_won=None,  # weather is a confirmation metric, computed elsewhere
        )
        if decision.status not in ('won', 'lost'):
            return False

        pos.settle_source = decision.source
        if decision.status == 'won':
            pos.redeemable = True
            self._close_position(pos, decision.settle_price, 'won')
            log.info(f"✅ RESOLVED WON: {pos.city} {pos.bucket_label[:30]} "
                     f"[{side.upper()}] — {decision.reason}")
        else:
            self._close_position(pos, decision.settle_price, 'lost')
            log.info(f"❌ RESOLVED LOST: {pos.city} {pos.bucket_label[:30]} "
                     f"[{side.upper()}] — {decision.reason}")
        return True

    @staticmethod
    def _match_bucket(res, pos: TrackedPosition):
        """Find the resolution bucket for a position by token id, then by label."""
        # Prefer exact token match (unambiguous).
        for b in res.buckets:
            if b.token_id_yes and pos.token_id and b.token_id_yes == pos.token_id:
                return b
        # Fall back to label match (handles NO-side legs whose token differs).
        lbl = (pos.bucket_label or '').strip().lower()
        if lbl:
            for b in res.buckets:
                bl = (b.label or '').strip().lower()
                if bl and (bl == lbl or bl in lbl or lbl in bl):
                    return b
        return None

    def _legacy_resolution_check(self, pos: TrackedPosition):
        """Old CLOB-price resolution path — used only when the resolver can't
        answer (e.g. slug missing). Kept conservative."""
        try:
            resp = self._session.get(
                f"{Config.CLOB_API_URL}/price",
                params={'token_id': pos.token_id, 'side': 'SELL'},
                timeout=5,
            )
            if resp.status_code == 200:
                price = float(resp.json().get('price', 0))
                if price >= 0.99:
                    # P1 PHANTOM GUARD: a CLOB price snapping to ~1.00 is NOT a
                    # real settlement -- Polymarket releases the true resolved
                    # price at/near close. golden_no was booking $1.00 "wins"
                    # off this rail-snap (phantom wins). For golden_no we now
                    # REFUSE to settle from the legacy CLOB path and wait for the
                    # real resolver. Every other strategy (late_observed_no honest
                    # holds, baskets, etc.) is untouched, so nothing else changes.
                    _guard = bool(getattr(Config, 'PHANTOM_GUARD_GOLDEN_ONLY', True))
                    if _guard and ('golden_no' in str(getattr(pos, 'strategy', ''))):
                        log.info("⏸️ PHANTOM GUARD: skip legacy CLOB win for "
                                 "golden_no %s @ $%.3f (await real resolve)"
                                 % (pos.bucket_label[:30], price))
                    else:
                        pos.redeemable = True
                        pos.settle_source = 'clob'
                        self._close_position(pos, 1.0, 'won')
                        log.info(f"✅ RESOLVED WON (legacy): {pos.city} {pos.bucket_label[:30]} @ ${price:.3f}")
                elif price <= 0.01 and pos.is_expired:
                    pos.settle_source = 'clob'
                    self._close_position(pos, 0.0, 'lost')
                    log.info(f"❌ RESOLVED LOST (legacy): {pos.city} {pos.bucket_label[:30]} @ ${price:.3f}")
            elif resp.status_code == 404 and pos.is_expired:
                pos.settle_source = 'clob'
                self._close_position(pos, 0.0, 'lost')
                log.info(f"❌ RESOLVED LOST (legacy 404): {pos.city} {pos.bucket_label[:30]} — expired, no book")
        except Exception:
            pass

    def check_preclose_locks(self):
        """In the final minutes before close, flag open positions whose venue
        price says >= threshold as a near-certain WIN. This is a SIGNAL only —
        balance/PnL are still booked at real settlement in check_resolutions."""
        if pe is None:
            return
        threshold = _cfg('PAPER_PRECLOSE_LOCK_PCT', 0.95)
        window = _cfg('PAPER_PRECLOSE_WINDOW_MIN', 2.0)
        changed = False
        for pos in self.get_open_positions():
            if pos.preclose_locked or pos.current_price_stale:
                continue
            d = pe.preclose_conclusion(
                venue_price=pos.current_price,
                minutes_to_close=pos.minutes_to_close,
                lock_confidence=pos.lock_confidence or None,
                weather_won=None,
                price_threshold=threshold,
                window_minutes=window,
                lock_threshold=threshold,
            )
            if d:
                pos.preclose_locked = True
                changed = True
                log.info(f"🔒 PRECLOSE WIN LIKELY: {pos.city} {pos.bucket_label[:30]} — {d.reason}")
                if Config.is_paper():
                    self._log_paper_trade('PRECLOSE_LOCK', pos, extra={'note': d.reason})
        if changed:
            self._save_state()

    def redeem_position(self, pos: TrackedPosition) -> bool:
        """Redeem a winning position."""
        if not pos.redeemable:
            return False

        if Config.is_paper():
            payout = pos.shares * 1.0
            self.paper_balance += payout
            pos.status = 'redeemed'
            pos.pnl = payout - pos.cost_usd
            self.total_redeemed += payout
            log.info(f"💰 REDEEM: {pos.bucket_label} → +${payout:.2f}")
            # DOUBLE-COUNT FIX (2026-07-27): the WIN's PnL was already booked on
            # the SETTLE row (reason='won'). Redemption only moves the locked
            # cost back into cash — it is NOT a second, separate gain. If the
            # REDEEM row also carried pos.pnl, any tool that SUMS the pnl column
            # of paper_trades would count every win twice (this is exactly the
            # +$57 vs the true ~$9 that was reported). So log the REDEEM row with
            # pnl/realized_pnl = 0 (payout is kept as an explicit extra field for
            # reference). pos.pnl itself is left untouched so the conserved
            # ledger (realized = sold+lost+redeemed) still balances.
            self._log_paper_trade('REDEEM', pos, extra={
                'payout': round(payout, 2),
                'pnl': 0.0,
                'realized_pnl': 0.0,
                'settle_pnl': round(pos.pnl, 4),
                'note': 'win already booked at SETTLE; redeem is cash-move only',
            })
            self._save_state()
            self._assert_ledger()
            return True
        else:
            try:
                from data.clob_client import ClobClient
                client = ClobClient()
                success = client.redeem_position(pos.condition_id)
                if success:
                    pos.status = 'redeemed'
                    pos.pnl = pos.shares - pos.cost_usd
                    self.total_redeemed += pos.shares
                    self._save_state()
                    return True
            except Exception as e:
                log.error(f"Redeem failed: {e}")
        return False

    def redeem_all_winning(self) -> int:
        count = 0
        for pos in self.get_redeemable_positions():
            if self.redeem_position(pos):
                count += 1
        return count

    # ============================
    # PRICE UPDATES
    # ============================

    def update_prices(self):
        """Batch update prices for open positions.

        FREEZE-ON-BAD-DATA: never overwrite a good price with 0 / empty / a
        failed request. A closed or thin market returns no usable price — we keep
        the last good value and mark the position stale instead of showing random
        zeros (this was a major source of the "random values" symptom).
        """
        open_pos = self.get_open_positions()
        if not open_pos:
            return

        freeze = _cfg('PAPER_FREEZE_ON_BAD_PRICE', True)
        for pos in open_pos:
            try:
                resp = self._session.get(
                    f"{Config.CLOB_API_URL}/price",
                    params={'token_id': pos.token_id, 'side': 'SELL'},
                    timeout=3,
                )
                if resp.status_code == 200:
                    price = float(resp.json().get('price', 0) or 0)
                    if price > 0:
                        pos.current_price = price
                        pos.last_good_price = price
                        pos.current_value = pos.shares * price
                        pos.current_price_stale = False
                        if price > pos.peak_price:
                            pos.peak_price = price
                    else:
                        # No real bid/price — hold last good value, flag stale.
                        pos.current_price_stale = True
                        if not freeze:
                            pos.current_price = 0.0
                            pos.current_value = 0.0
                else:
                    pos.current_price_stale = True
            except Exception:
                pos.current_price_stale = True

        self._save_state()


    # ============================
    # CONTEXT MANAGEMENT (free memory for closed markets)
    # ============================

    def _maybe_free_context(self, slug: str):
        """Free market context if no open positions remain for it."""
        if not slug:
            return
        open_for_slug = [p for p in self.positions
                         if p.slug == slug and p.status == 'open']
        if not open_for_slug and slug in self.market_contexts:
            self.market_contexts[slug].active = False
            log.debug(f"Freed context: {slug}")

    def cleanup_contexts(self):
        """Remove all inactive market contexts (frees memory)."""
        inactive = [k for k, v in self.market_contexts.items() if not v.active]
        for k in inactive:
            del self.market_contexts[k]
        if inactive:
            log.info(f"🧹 Cleaned {len(inactive)} inactive market contexts")

    def get_active_context_count(self) -> int:
        """How many active market contexts we're tracking."""
        return sum(1 for v in self.market_contexts.values() if v.active)

    # ============================
    # PAPER TRADE LOG + LEDGER INVARIANT
    # ============================

    def _log_paper_trade(self, action: str, pos: TrackedPosition, extra: Optional[Dict] = None):
        """Append one structured record per BUY / SELL / SETTLE / REDEEM /
        PRECLOSE_LOCK to data/paper_trades.jsonl. Captures which signal &
        strategy fired, why we bought, the edge/grade/lock-confidence, the fill,
        and realized PnL — so the dry run is fully auditable."""
        if not Config.is_paper():
            return
        try:
            os.makedirs(os.path.dirname(self._paper_trades_file) or '.', exist_ok=True)
            rec = {
                'ts': datetime.now(timezone.utc).isoformat(),
                'action': action,
                'city': pos.city,
                'bucket': pos.bucket_label,
                'market': pos.market_title[:80],
                'slug': pos.slug,
                'strategy': pos.strategy,
                'signal': pos.signal or pos.strategy,
                'why': pos.reason,
                'entry_price': round(pos.entry_price, 4),
                'current_price': round(pos.current_price, 4),
                'shares': round(pos.shares, 2),
                'cost_usd': round(pos.cost_usd, 2),
                'edge': round(pos.edge_at_entry, 4),
                'grade': round(pos.grade, 3),
                'lock_confidence': round(pos.lock_confidence, 3),
                'status': pos.status,
                'exit_price': pos.exit_price,
                'exit_reason': pos.exit_reason,
                'settle_source': pos.settle_source,
                'pnl': round(pos.pnl, 4),
                'roi_pct': round(pos.roi_pct, 1),
                'preclose_locked': pos.preclose_locked,
                'stale_price': pos.current_price_stale,
                'resolution_time': pos.resolution_time.isoformat() if pos.resolution_time else None,
                'minutes_to_close': (round(pos.minutes_to_close, 1)
                                     if pos.minutes_to_close is not None else None),
                'balance_after': round(self.paper_balance, 2),
            }
            if extra:
                rec.update(extra)
            with open(self._paper_trades_file, 'a') as f:
                f.write(json.dumps(rec) + '\n')
        except Exception as e:
            log.debug(f"paper trade log failed: {e}")

    def _assert_ledger(self):
        """Verify the conserved PnL invariant after every state change (paper):

            cash balance + locked cost (open/pending/won) == deposited + realized

        'won' positions keep their cost LOCKED until redeemed (cash credited at
        redemption), so they count with open cost, not realized PnL.
        """
        if pe is None or not Config.is_paper():
            return True
        locked = sum(p.cost_usd for p in self.positions
                     if p.status in ('open', 'pending', 'won'))
        realized = sum(p.pnl for p in self.positions
                       if p.status in ('sold', 'lost', 'redeemed'))
        ok, drift = pe.ledger_ok(balance=self.paper_balance, open_cost=locked,
                                 realized=realized, deposited=self.total_deposited)
        if not ok:
            log.warning(f"⚠️ LEDGER DRIFT ${drift:+.4f} — balance=${self.paper_balance:.2f} "
                        f"locked=${locked:.2f} realized=${realized:.2f} "
                        f"deposited=${self.total_deposited:.2f}")
        return ok

    # ============================
    # WEEKLY MEMORY
    # ============================

    def record_weekly_stats(self):
        """Snapshot current week's performance for ML memory."""
        now = datetime.now(timezone.utc)
        week_start = (now - timedelta(days=now.weekday())).strftime('%Y-%m-%d')

        # Positions closed this week
        week_ago = now - timedelta(days=7)
        this_week = [p for p in self.positions
                     if p.status != 'open' and p.exit_time and p.exit_time > week_ago]

        if not this_week:
            return

        wins = [p for p in this_week if p.status in ('won', 'redeemed')]
        losses = [p for p in this_week if p.status == 'lost']
        pnl = sum(p.pnl for p in this_week)

        # Best performing city
        city_pnl: Dict[str, float] = {}
        for p in this_week:
            city_pnl[p.city] = city_pnl.get(p.city, 0) + p.pnl
        best_city = max(city_pnl, key=city_pnl.get) if city_pnl else ''

        # Best strategy
        strat_pnl: Dict[str, float] = {}
        for p in this_week:
            strat_pnl[p.strategy] = strat_pnl.get(p.strategy, 0) + p.pnl
        best_strat = max(strat_pnl, key=strat_pnl.get) if strat_pnl else ''

        stats = WeeklyStats(
            week_start=week_start,
            trades=len(this_week),
            wins=len(wins),
            losses=len(losses),
            pnl=pnl,
            roi_pct=(pnl / max(0.01, self.total_deposited)) * 100,
            best_city=best_city,
            best_strategy=best_strat,
            avg_entry_price=sum(p.entry_price for p in this_week) / max(1, len(this_week)),
        )
        self.weekly_history.append(stats)
        self._save_weekly()
        log.info(f"📅 Weekly: {stats.trades} trades | {stats.wins}W/{stats.losses}L | PnL=${stats.pnl:+.2f}")

    def get_weekly_summary(self) -> str:
        """Get compact weekly summary for ML context."""
        if not self.weekly_history:
            return "No weekly history yet."
        recent = self.weekly_history[-4:]  # last 4 weeks
        lines = []
        for w in recent:
            lines.append(
                f"W{w.week_start}: {w.trades}T {w.wins}W {w.losses}L "
                f"PnL=${w.pnl:+.1f} best={w.best_city}"
            )
        return " | ".join(lines)


    # ============================
    # STATISTICS (per-position + aggregate)
    # ============================

    @staticmethod
    def _closed_outcome(p) -> Optional[str]:
        """Classify a CLOSED position as 'win' or 'loss' for win-rate stats.

        - Resolutions: status 'won'/'redeemed' => win, 'lost' => loss.
        - Market exits (status 'sold': thesis-exit, flip book-or-cut, stop-loss,
          trailing-stop, manual) count by REALIZED-PnL sign so a thesis-exit sold
          at a loss is correctly a LOSS rather than being silently dropped (which
          used to inflate win-rate).
        Returns None for still-open / pending positions and exact break-evens.
        """
        st = getattr(p, 'status', '')
        if st in ('won', 'redeemed'):
            return 'win'
        if st == 'lost':
            return 'loss'
        if st == 'sold':
            if p.pnl > 1e-9:
                return 'win'
            if p.pnl < -1e-9:
                return 'loss'
        return None

    def get_outcome_breakdown(self) -> Dict[str, Dict]:
        """Group CLOSED positions into reporting buckets so quick scalp exits
        are shown SEPARATELY from settlements/redeems (user request: group the
        small losses/gains and show them apart from the main settle/redeem).

          settle_win  : resolved winners still locked (status 'won')
          redeemed    : resolved winners cashed out (status 'redeemed')
          settle_loss : resolved losers (status 'lost')
          small_gain  : market exits (status 'sold') booked at a PROFIT
          small_loss  : market exits (status 'sold') booked at a LOSS
          breakeven   : market exits at ~$0

        'small_*' are the flip / thesis / stop / cap scalps; a penny exit such as
        0.20 -> 0.02 lands in small_loss. Each value is {count, pnl}.
        """
        cats = {k: {'count': 0, 'pnl': 0.0} for k in (
            'settle_win', 'redeemed', 'settle_loss',
            'small_gain', 'small_loss', 'breakeven')}
        for p in self.positions:
            st = getattr(p, 'status', '')
            pnl = p.pnl or 0.0
            if st == 'won':
                key = 'settle_win'
            elif st == 'redeemed':
                key = 'redeemed'
            elif st == 'lost':
                key = 'settle_loss'
            elif st == 'sold':
                key = ('small_gain' if pnl > 1e-9
                       else 'small_loss' if pnl < -1e-9 else 'breakeven')
            else:
                continue
            cats[key]['count'] += 1
            cats[key]['pnl'] += pnl
        return cats

    def get_stats(self) -> Dict:
        """Comprehensive stats."""
        open_pos = self.get_open_positions()
        closed = [p for p in self.positions if p.status != 'open']
        total_closed = self.wins + self.losses
        win_rate = (self.wins / max(1, total_closed)) * 100
        open_value = sum(p.current_value for p in open_pos)

        return {
            'mode': 'PAPER' if Config.is_paper() else 'LIVE',
            'balance': self.get_balance(),
            'position_value': open_value,
            'portfolio_value': self.get_portfolio_value(),
            'total_pnl': self.get_total_pnl(),
            'roi_pct': (self.get_total_pnl() / max(0.01, self.total_deposited)) * 100,
            'total_trades': self.total_trades,
            'open_positions': len(open_pos),
            'wins': self.wins,
            'losses': self.losses,
            'win_rate': win_rate,
            'total_redeemed': self.total_redeemed,
            'avg_entry_price': (sum(p.entry_price for p in self.positions) /
                               max(1, len(self.positions))),
            'active_contexts': self.get_active_context_count(),
            'weekly_summary': self.get_weekly_summary(),
        }

    def get_position_detail(self, pos_id: str) -> Optional[Dict]:
        """Get detailed info for a single position."""
        pos = next((p for p in self.positions if p.id == pos_id), None)
        if not pos:
            return None
        return {
            'id': pos.id, 'city': pos.city, 'bucket': pos.bucket_label,
            'entry': pos.entry_price, 'current': pos.current_price,
            'shares': pos.shares, 'cost': pos.cost_usd,
            'pnl': pos.unrealized_pnl, 'roi_pct': pos.roi_pct,
            'status': pos.status, 'strategy': pos.strategy,
            'hold_hours': pos.hold_hours,
            'take_profit': pos.take_profit_price,
            'stop_loss_pct': pos.stop_loss_pct,
            'peak_price': pos.peak_price,
            'resolution': pos.resolution_time.isoformat() if pos.resolution_time else None,
        }

    def get_per_city_stats(self) -> Dict[str, Dict]:
        """PnL breakdown by city."""
        stats: Dict[str, Dict] = {}
        for p in self.positions:
            if p.city not in stats:
                stats[p.city] = {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
            stats[p.city]['trades'] += 1
            oc = self._closed_outcome(p)
            if oc == 'win':
                stats[p.city]['wins'] += 1
            elif oc == 'loss':
                stats[p.city]['losses'] += 1
            stats[p.city]['pnl'] += p.pnl if p.status != 'open' else p.unrealized_pnl
        return stats

    def get_per_strategy_stats(self) -> Dict[str, Dict]:
        """PnL breakdown by strategy.

        Wins/losses count CLOSED positions by outcome (see `_closed_outcome`):
        resolutions by won/lost, market exits (incl. thesis-exit) by realized-PnL
        sign. 'trades' counts every position (incl. still-open) for context.
        """
        stats: Dict[str, Dict] = {}
        for p in self.positions:
            if p.strategy not in stats:
                stats[p.strategy] = {'trades': 0, 'wins': 0, 'losses': 0, 'pnl': 0.0}
            stats[p.strategy]['trades'] += 1
            oc = self._closed_outcome(p)
            if oc == 'win':
                stats[p.strategy]['wins'] += 1
            elif oc == 'loss':
                stats[p.strategy]['losses'] += 1
            stats[p.strategy]['pnl'] += p.pnl if p.status != 'open' else p.unrealized_pnl
        return stats

    def record_performance_snapshot(self) -> str:
        """Append a timestamped performance snapshot to disk and log a one-line
        summary. This is our OWN-bot simulation record (per-strategy/per-city
        win-rate, PnL, ROI over time) — distinct from the offline backtest, so we
        can see how the bot actually performs as it runs. Returns the summary."""
        import json
        stats = self.get_stats()
        by_strategy = self.get_per_strategy_stats()
        ledger_ok_flag = self._assert_ledger()
        snap = {
            'ts': datetime.now(timezone.utc).isoformat(),
            'mode': stats['mode'],
            'balance': round(stats['balance'], 2),
            'portfolio_value': round(stats['portfolio_value'], 2),
            'total_pnl': round(stats['total_pnl'], 2),
            'roi_pct': round(stats['roi_pct'], 1),
            'trades': stats['total_trades'],
            'open': stats['open_positions'],
            'wins': stats['wins'], 'losses': stats['losses'],
            'win_rate': round(stats['win_rate'], 1),
            'ledger_ok': ledger_ok_flag,
            'by_strategy': by_strategy,
            'by_city': self.get_per_city_stats(),
        }
        try:
            os.makedirs('backtest/results', exist_ok=True)
            path = 'backtest/results/paper_performance.json'
            history = []
            if os.path.exists(path):
                try:
                    with open(path) as f:
                        history = json.load(f)
                except Exception:
                    history = []
            history.append(snap)
            history = history[-500:]  # bound file size
            with open(path, 'w') as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            log.debug(f"perf snapshot save failed: {e}")

        parts = []
        for strat, s in by_strategy.items():
            wr = (s['wins'] / max(1, s['trades'])) * 100
            parts.append(f"{strat}:{s['trades']}t {wr:.0f}%WR ${s['pnl']:+.2f}")
        summary = (f"📈 PERF [{snap['mode']}] bal=${snap['balance']:.2f} "
                   f"PnL=${snap['total_pnl']:+.2f} ({snap['roi_pct']:+.0f}%) "
                   f"WR={snap['win_rate']:.0f}% {snap['wins']}W/{snap['losses']}L"
                   + ("" if ledger_ok_flag else " ⚠️LEDGER")
                   + (" | " + " | ".join(parts) if parts else ""))
        log.info(summary)
        return summary


    # ============================
    # PERSISTENCE
    # ============================

    def _dict_to_pos(self, pd):
        """Rebuild a TrackedPosition from a _pos_to_dict() record (used by the
        /update + /recover real-recovery path). Mirrors _load_state()."""
        return TrackedPosition(
            id=pd['id'],
            market_title=pd.get('mt', pd.get('market_title', '')),
            bucket_label=pd.get('bl', pd.get('bucket_label', '')),
            token_id=pd.get('tid', pd.get('token_id', '')),
            condition_id=pd.get('cid', pd.get('condition_id', '')),
            entry_price=pd.get('ep', pd.get('entry_price', 0)),
            shares=pd.get('sh', pd.get('shares', 0)),
            cost_usd=pd.get('cu', pd.get('cost_usd', 0)),
            current_price=pd.get('cp', pd.get('current_price', 0)),
            current_value=pd.get('cv', pd.get('current_value', 0)),
            entry_time=datetime.fromisoformat(pd.get('et', pd.get('entry_time', '2026-01-01T00:00:00+00:00'))),
            resolution_time=datetime.fromisoformat(pd['rt']) if pd.get('rt') else None,
            strategy=pd.get('st', pd.get('strategy', '')),
            status=pd.get('status', 'open'),
            pnl=pd.get('pnl', 0),
            realized_pnl=pd.get('rpnl', 0),
            redeemable=pd.get('rdm', pd.get('redeemable', False)),
            city=pd.get('city', ''),
            slug=pd.get('slug', ''),
            peak_price=pd.get('pp', pd.get('peak_price', 0)),
            stop_loss_pct=pd.get('sl', -80),
            take_profit_price=pd.get('tp', 0.50),
            exit_price=pd.get('xp'),
            exit_reason=pd.get('xr', ''),
            exit_time=datetime.fromisoformat(pd['xt']) if pd.get('xt') else None,
            edge_at_entry=pd.get('edge', 0.0),
            grade=pd.get('grade', 0.0),
            lock_confidence=pd.get('lc', 0.0),
            signal=pd.get('sig', ''),
            reason=pd.get('why', ''),
            last_good_price=pd.get('lgp', 0.0),
            current_price_stale=pd.get('stale', False),
            preclose_locked=pd.get('plk', False),
            settle_source=pd.get('ss', ''),
            hold_to_resolution=pd.get('h2r', False),
            cluster_box=pd.get('cbox', ''),
            flip_max_hold_minutes=pd.get('fmh', 0.0),
        )

    def export_open_snapshot(self):
        """/update -- capture a RECOVERY snapshot of the CURRENT open book keyed
        by condition_id/token_id, so a later /recover rebuilds the REAL
        positions (not a trusted count) by matching market/condition id."""
        opens = [self._pos_to_dict(p) for p in self.positions
                 if getattr(p, 'status', '') in ('open', 'pending')]
        return {
            'ts': datetime.now(timezone.utc).isoformat(),
            'paper_balance': self.paper_balance,
            'open_count': len(opens),
            'open_positions': opens,
        }

    def recover_open_snapshot(self, snap, restore_balance=True):
        """/recover -- re-add open positions from a snapshot, matching by
        (token/condition id, strategy) so duplicates are never created. REAL
        recovery: positions are reconstructed from their market ids, not from a
        stored win/loss count. Returns {'added','skipped'}."""
        added = 0
        skipped = 0
        existing = {(getattr(p, 'token_id', ''), getattr(p, 'strategy', ''))
                    for p in self.positions}
        existing_cid = {(getattr(p, 'condition_id', ''), getattr(p, 'strategy', ''))
                        for p in self.positions}
        for pd in (snap.get('open_positions', []) or []):
            key = (pd.get('tid', ''), pd.get('st', ''))
            ckey = (pd.get('cid', ''), pd.get('st', ''))
            if key in existing or (ckey[0] and ckey in existing_cid):
                skipped += 1
                continue
            try:
                pos = self._dict_to_pos(pd)
                self.positions.append(pos)
                existing.add(key)
                if ckey[0]:
                    existing_cid.add(ckey)
                added += 1
            except Exception as e:
                log.debug(f"recover skip one: {e}")
                skipped += 1
        if restore_balance and 'paper_balance' in snap:
            try:
                self.paper_balance = float(snap['paper_balance'])
            except Exception:
                pass
        try:
            self._save_state()
        except Exception:
            pass
        return {'added': added, 'skipped': skipped}

    def _save_state(self):
        """Save positions to disk."""
        try:
            os.makedirs('data', exist_ok=True)
            state = {
                'paper_balance': self.paper_balance,
                'total_deposited': self.total_deposited,
                'total_redeemed': self.total_redeemed,
                'total_trades': self.total_trades,
                'wins': self.wins,
                'losses': self.losses,
                'cluster_box_seq': self.cluster_box_seq,
                'cluster_box_seqs': self.cluster_box_seqs,
                'positions': [self._pos_to_dict(p) for p in self.positions[-500:]],
                'market_contexts': {
                    k: {'slug': v.slug, 'city': v.city, 'active': v.active,
                         'edge': v.edge, 'forecast_temp': v.forecast_temp}
                    for k, v in self.market_contexts.items() if v.active
                },
                'last_updated': datetime.now(timezone.utc).isoformat(),
            }
            with open(self._state_file, 'w') as f:
                json.dump(state, f, separators=(',', ':'))
        except Exception as e:
            log.warning(f"Save failed: {e}")

    def _pos_to_dict(self, p: TrackedPosition) -> Dict:
        return {
            'id': p.id, 'mt': p.market_title[:60], 'bl': p.bucket_label,
            'tid': p.token_id, 'cid': p.condition_id,
            'ep': p.entry_price, 'sh': p.shares, 'cu': p.cost_usd,
            'cp': p.current_price, 'cv': p.current_value,
            'et': p.entry_time.isoformat(), 'st': p.strategy,
            'rt': p.resolution_time.isoformat() if p.resolution_time else None,
            'status': p.status, 'pnl': p.pnl, 'rpnl': p.realized_pnl,
            'rdm': p.redeemable, 'city': p.city, 'slug': p.slug,
            'pp': p.peak_price, 'sl': p.stop_loss_pct, 'tp': p.take_profit_price,
            'xp': p.exit_price, 'xr': p.exit_reason,
            'xt': p.exit_time.isoformat() if p.exit_time else None,
            # paper-realism fields
            'edge': p.edge_at_entry, 'grade': p.grade, 'lc': p.lock_confidence,
            'sig': p.signal, 'why': p.reason, 'lgp': p.last_good_price,
            'stale': p.current_price_stale, 'plk': p.preclose_locked,
            'ss': p.settle_source,
            # request-23 fields
            'h2r': p.hold_to_resolution, 'cbox': p.cluster_box,
            'fmh': p.flip_max_hold_minutes,
        }

    def _load_state(self):
        """Load from disk."""
        try:
            if not os.path.exists(self._state_file):
                return
            with open(self._state_file, 'r') as f:
                state = json.load(f)
            self.paper_balance = state.get('paper_balance', Config.STARTING_BALANCE)
            self.total_deposited = state.get('total_deposited', Config.STARTING_BALANCE)
            self.total_redeemed = state.get('total_redeemed', 0)
            self.total_trades = state.get('total_trades', 0)
            self.wins = state.get('wins', 0)
            self.losses = state.get('losses', 0)
            self.cluster_box_seq = state.get('cluster_box_seq', 0)
            self.cluster_box_seqs = dict(state.get('cluster_box_seqs', {}) or {})
            for pd in state.get('positions', []):
                try:
                    pos = TrackedPosition(
                        id=pd['id'],
                        market_title=pd.get('mt', pd.get('market_title', '')),
                        bucket_label=pd.get('bl', pd.get('bucket_label', '')),
                        token_id=pd.get('tid', pd.get('token_id', '')),
                        condition_id=pd.get('cid', pd.get('condition_id', '')),
                        entry_price=pd.get('ep', pd.get('entry_price', 0)),
                        shares=pd.get('sh', pd.get('shares', 0)),
                        cost_usd=pd.get('cu', pd.get('cost_usd', 0)),
                        current_price=pd.get('cp', pd.get('current_price', 0)),
                        current_value=pd.get('cv', pd.get('current_value', 0)),
                        entry_time=datetime.fromisoformat(pd.get('et', pd.get('entry_time', '2026-01-01T00:00:00+00:00'))),
                        resolution_time=datetime.fromisoformat(pd['rt']) if pd.get('rt') else None,
                        strategy=pd.get('st', pd.get('strategy', '')),
                        status=pd.get('status', 'open'),
                        pnl=pd.get('pnl', 0),
                        realized_pnl=pd.get('rpnl', 0),
                        redeemable=pd.get('rdm', pd.get('redeemable', False)),
                        city=pd.get('city', ''),
                        slug=pd.get('slug', ''),
                        peak_price=pd.get('pp', pd.get('peak_price', 0)),
                        stop_loss_pct=pd.get('sl', -80),
                        take_profit_price=pd.get('tp', 0.50),
                        exit_price=pd.get('xp'),
                        exit_reason=pd.get('xr', ''),
                        exit_time=datetime.fromisoformat(pd['xt']) if pd.get('xt') else None,
                        edge_at_entry=pd.get('edge', 0.0),
                        grade=pd.get('grade', 0.0),
                        lock_confidence=pd.get('lc', 0.0),
                        signal=pd.get('sig', ''),
                        reason=pd.get('why', ''),
                        last_good_price=pd.get('lgp', 0.0),
                        current_price_stale=pd.get('stale', False),
                        preclose_locked=pd.get('plk', False),
                        settle_source=pd.get('ss', ''),
                        hold_to_resolution=pd.get('h2r', False),
                        cluster_box=pd.get('cbox', ''),
                        flip_max_hold_minutes=pd.get('fmh', 0.0),
                    )
                    self.positions.append(pos)
                except Exception:
                    continue
            # Seed per-strategy box counters from existing positions when loading
            # OLD state (pre per-strategy counters) so a restart never reissues a
            # "Box N" a strategy is already using.
            if not self.cluster_box_seqs:
                import re as _re_box
                _seeded: Dict[str, int] = {}
                for _p in self.positions:
                    _st = getattr(_p, 'strategy', '') or ''
                    _bx = getattr(_p, 'cluster_box', '') or ''
                    _m = _re_box.search(r'(\d+)', _bx)
                    if _st in BASKET_STRATEGIES and _m:
                        _seeded[_st] = max(_seeded.get(_st, 0), int(_m.group(1)))
                self.cluster_box_seqs = _seeded
            log.info(f"Loaded {len(self.positions)} positions")
        except Exception as e:
            log.debug(f"No state: {e}")

    def _save_weekly(self):
        """Save weekly history."""
        try:
            os.makedirs('data', exist_ok=True)
            data = [
                {'ws': w.week_start, 't': w.trades, 'w': w.wins,
                 'l': w.losses, 'pnl': w.pnl, 'bc': w.best_city}
                for w in self.weekly_history[-52:]  # keep 1 year
            ]
            with open(self._weekly_file, 'w') as f:
                json.dump(data, f, separators=(',', ':'))
        except Exception:
            pass

    def _load_weekly(self):
        """Load weekly history."""
        try:
            if not os.path.exists(self._weekly_file):
                return
            with open(self._weekly_file, 'r') as f:
                data = json.load(f)
            for d in data:
                self.weekly_history.append(WeeklyStats(
                    week_start=d['ws'], trades=d['t'], wins=d['w'],
                    losses=d['l'], pnl=d['pnl'], best_city=d.get('bc', ''),
                ))
        except Exception:
            pass
