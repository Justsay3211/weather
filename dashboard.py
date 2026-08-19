"""
Weather Sniper Bot — Main Dashboard / Trading Loop

Flow:
1. Scan Polymarket for active weather markets (slug-based, confirmed pattern)
2. For each market: fetch multi-source forecasts
3. Run probability engine → find mispriced buckets
4. Run strategies (LateObserved primary + QuickFlip + optional PeakBasket/Confident) → signals
5. Execute trades (paper or live)
6. Monitor positions, check resolutions, redeem winners
7. Send Telegram notifications

Usage:
    python dashboard.py              # paper mode (default)
    python dashboard.py --live       # live trading
    python dashboard.py --once       # single scan then exit
    python dashboard.py --status     # print status and exit
"""

import sys
import os
import time
import argparse
from collections import Counter
from datetime import datetime, timezone

from config import Config
from logger import log
from data.weather_fetcher import WeatherFetcher, get_city_coords, CITY_COORDS
from data.probability_engine import ProbabilityEngine
from data.market_scanner import MarketScanner, MARKET_CITIES
from data.observed_weather import ObservedWeather
from strategies.peaker import PeakerStrategy
from strategies.confident_strategy import ConfidentStrategy
from strategies.late_observed_temp import LateObservedTempStrategy
from strategies.quick_flip import QuickFlipStrategy
from strategies.peak_cluster import PeakClusterStrategy
# NEW (2026-08-19): remaster + NO-arbitrage strategies (run alongside the core),
# the master run-id manager, and the multi-pipeline grade+edge engine.
from strategies.late_observed_remaster import LateObservedRemasterStrategy
from strategies.late_observed_no_arbitrage import LateObservedNoArbitrageStrategy
try:
    from data import run_manager
except Exception:  # keep bot runnable even if run_manager import fails
    run_manager = None
try:
    from data import grade_edge_engine
except Exception:
    grade_edge_engine = None
from data.stability import StabilityEngine
from data.liquidity_guard import LiquidityGuard
from data.clob_client import ClobClient
from data.market_timing import outcome_decided, city_local_now
from data.resolution_rules import StationResolver
from trading.position_manager import PositionManager
from trading import exit_policies
from trading import sizing
from bot.telegram_ui import TelegramBot
from bot import settings_store
from ml.decision_engine import MLDecisionEngine
try:
    from ml.resolution_verifier import ResolutionVerifier
except Exception:  # keep the bot runnable even if the verifier import fails
    ResolutionVerifier = None


class WeatherBot:
    """Main weather trading bot with full dashboard."""

    def __init__(self):
        settings_store.load_into_config()   # apply persisted runtime overrides (Telegram /settings)
        # MASTER RUN-ID: mint a random, collision-checked run id for this boot
        # (stamped into logs, paper trades, offload names + the session
        # manifest). RECOVER mode reuses the previous id to continue the same
        # timeline. Fail-open so a bad manifest never blocks startup.
        self.run = {}
        try:
            if run_manager is not None and getattr(Config, 'RUN_ID_ENABLED', True):
                _recover = str(os.getenv('RUN_RECOVER', '')).strip().lower() in ('1', 'true', 'yes')
                _rid_env = os.getenv('RUN_RESUME_ID', '').strip() or None
                self.run = run_manager.start(
                    mode='recover' if (_recover or _rid_env) else 'fresh',
                    resume_run_id=_rid_env,
                )
                log.info(f"\U0001F194 run_id={self.run.get('run_id')} mode={self.run.get('mode')}")
        except Exception as _rme:
            log.debug(f"run_manager start skipped: {_rme}")
        # Req-28: NEVER auto-trade on boot. A fresh Railway deploy (or any
        # restart) must come up with trading OFF and wait for the user to press
        # [Start Trading] / type 'start'. Force the master switch False here even
        # if a previously-persisted runtime setting had it True.
        Config.TRADING_ENABLED = False
        self.fetcher = WeatherFetcher()
        self.engine = ProbabilityEngine()
        self.scanner = MarketScanner()
        self.confident = ConfidentStrategy()
        # PRIMARY strategy: trade the observed/locked daily extreme (YES + NO).
        self.late_observed = LateObservedTempStrategy()
        # NEW: remaster (smarter second opinion on the SAME observed signal via
        # the grade+edge engine) + NO-arbitrage basket. Both run ALONGSIDE
        # late_observed_no, which is left untouched.
        self.late_observed_remaster = LateObservedRemasterStrategy(base=self.late_observed)
        self.late_observed_no_arb = LateObservedNoArbitrageStrategy()
        # Forecast-change arbitrage: enter a freshly mispriced bucket before the
        # book digests new model data; flips quick or holds if structural.
        self.quick_flip = QuickFlipStrategy()
        # Parallel peak-cluster basket: buy an adjacent bucket basket around the
        # estimated peak (combined per-share cost < $1) so any single winning leg profits.
        self.peak_cluster = PeakClusterStrategy()
        # Peaker: unified HIGH-confidence peak play (merge of the old safety_peak
        # + peak_basket). Estimates the peak bucket and takes one focused shape
        # (peak-only / peak+warmer / peak+cooler) in equal shares; the wide
        # both-shoulders basket is delegated to peak_cluster above.
        self.peaker = PeakerStrategy()
        self.observed = ObservedWeather()
        self.stability_engine = StabilityEngine()
        # Stability GRADE + liquidity guard (applied across ALL strategies)
        self.liquidity = LiquidityGuard()
        self.clob = ClobClient()          # read-only order-book fetches (no auth needed)
        self._book_cache = {}             # token_id -> (timestamp, book) — short TTL
        self.pm = PositionManager()
        self.ml = MLDecisionEngine()
        # Resolution-station verification: confirm/adjust the EXACT airport each
        # market settles on. The deterministic check is free; the verifier LLM
        # is only consulted when a station looks ambiguous or different.
        self.station_resolver = StationResolver()
        self.resolution_verifier = ResolutionVerifier() if ResolutionVerifier else None
        self.telegram = TelegramBot(position_manager=self.pm, scanner=self.scanner)
        # Wire the lightweight event notifier -> Telegram (proxy<->direct switch,
        # VPS health/offload, ML status). In-memory only; see data/notifier.py.
        try:
            from data import notifier as _notifier
            _notifier.bind(self.telegram.send)
        except Exception as _ne:
            log.debug(f"notifier bind failed: {_ne}")
        # Req-30: give Telegram the ML engine so /mlanalysis writes a real report.
        try:
            self.telegram.attach_ml(self.ml)
        except Exception as _e:
            log.debug(f"attach_ml failed: {_e}")
        # Route close/resolution alerts (stop-loss, take-profit, trailing-stop,
        # won/lost) through Telegram. Flip/cap/thesis exits are notified directly
        # in run_once; the PM hook skips their reasons (DASHBOARD_NOTIFIED_REASONS)
        # to avoid a double-notify.
        self.pm._notify_close = self.telegram.notify_close
        # Peak-cluster baskets resolve as ONE grouped summary (winner + payout,
        # losing buckets + loss, net) instead of one won/lost alert per leg.
        self.pm._notify_cluster_close = self.telegram.notify_cluster_resolution
        # Req-28: let the Telegram Restart button / 'restart' command drive a
        # full fresh restart (clear ALL positions + reset balance, trading OFF).
        self.telegram._on_restart = self.restart_fresh
        self.scan_count = 0
        self.signals_generated = 0
        self.trades_placed = 0
        self._funnel = Counter()          # per-scan placement funnel (diagnostics)
        # Per-scan capital-deployment guard counters (reset every run_once). Stop
        # the old "dump the whole bankroll on the first cycle" behaviour: cap how
        # many NEW buys and how much NEW capital one scan may deploy.
        self._scan_buys = 0
        self._scan_deployed_usd = 0.0
        self._last_resolution_check = 0
        self._last_daily_summary = ''
        self._last_weekly_record = ''
        self._last_summary_ts = time.time()   # Req-30 periodic summary timer

    def restart_fresh(self, starting_balance=None):
        """Restart the bot like new: clear ALL positions and reset the ledger to
        a fresh starting balance, trading left OFF (the user presses Start again
        afterwards). Wired to the Telegram Restart button / 'restart' command.
        """
        bal = starting_balance if starting_balance is not None else Config.STARTING_BALANCE
        try:
            self.pm.reset_fresh(starting_balance=bal)
        except Exception as e:
            log.error(f"restart_fresh failed: {e}")
            return False
        Config.TRADING_ENABLED = False  # a fresh restart must not auto-resume trading
        # RECOVER/RESTART run-id note: stamp the restart event on the active
        # run so the manifest + logs show the continuation point.
        try:
            if run_manager is not None:
                run_manager.note('restart_fresh', starting_balance=float(bal))
        except Exception:
            pass
        self.scan_count = 0
        self.signals_generated = 0
        self.trades_placed = 0
        self._scan_buys = 0
        self._scan_deployed_usd = 0.0
        self._book_cache = {}
        log.info(f"♻️  RESTART FRESH — positions cleared, balance reset to ${bal:.2f}, trading OFF")
        return True

    def run_once(self):
        """Run a single scan cycle."""
        self.scan_count += 1
        self._funnel = Counter()  # reset per-cycle diagnostics funnel
        # Reset the per-scan capital-deployment guard so each scan gets a fresh
        # buy-count / deploy-$ budget (see _can_deploy).
        self._scan_buys = 0
        self._scan_deployed_usd = 0.0
        now = datetime.now(timezone.utc)
        log.info(f"\n{'═'*60}")
        log.info(f"🔍 SCAN #{self.scan_count} — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        log.info(f"{'═'*60}")

        # Step 0: Sync pending orders (GTC fills), check resolutions, redeem
        if not Config.is_paper():
            self.pm.sync_pending_orders()

        if time.time() - self._last_resolution_check > 300:
            self._check_resolutions()
            self.pm.check_risk_triggers()  # stop-loss / take-profit
            flip_exits = exit_policies.check_flip_exits(self.pm)    # quick_flip +10%/-5% + ML upside
            cap_exits = exit_policies.check_profit_caps(self.pm)    # Req-30: global 300% ML-managed cap
            # LIQUIDITY SWEEP first: cut a broken non-basket thesis fast at -50%
            # into available liquidity (primary cut; runs BEFORE thesis-exit so
            # -85% is only a deeper backstop). Baskets exempt.
            sweep_exits = exit_policies.check_liquidity_sweep_exits(self.pm)
            thesis_exits = exit_policies.check_thesis_exits(self.pm)  # strict thesis-invalidation (very bad only)
            ml_review_exits = exit_policies.check_ml_reviews(self.pm)  # Req-31: ML early HOLD/SELL position review
            # peak_cluster OUTER-leg take-profit (2026-07-27, OPT-IN default OFF):
            # banks a profited shoulder leg while NEVER selling the peak leg, so
            # the basket can still resolve to the full $1. No-op unless
            # PEAK_CLUSTER_OUTER_TP_ENABLED is turned on.
            cluster_tp_exits = exit_policies.check_peak_cluster_outer_tp(self.pm)
            # Flip/cap/sweep/thesis/ml-review closes pass their real reason into
            # _close_position; the PM _notify_close hook skips those reasons
            # (DASHBOARD_NOTIFIED_REASONS) so they are notified ONCE here.
            for _p in (flip_exits or []) + (cap_exits or []) + (sweep_exits or []) + (thesis_exits or []) + (ml_review_exits or []) + (cluster_tp_exits or []):
                try:
                    self.telegram.notify_close(_p)
                except Exception as e:
                    log.debug(f"flip/thesis notify failed: {e}")
            self.pm.cleanup_contexts()     # free closed market memory
            self.pm.record_performance_snapshot()  # log + persist our own paper/live performance
            self._last_resolution_check = time.time()

        # Weekly memory recording
        week_str = now.strftime('%Y-W%W')
        if week_str != self._last_weekly_record and now.weekday() == 0:
            self.pm.record_weekly_stats()
            self._last_weekly_record = week_str

        # Step 1: Discover weather markets
        markets = self.scanner.scan_weather_markets(days_ahead=Config.SCAN_DAYS_AHEAD)
        if not markets:
            log.info("No active weather markets found. Waiting...")
            return

        log.info(f"Found {len(markets)} active weather markets")

        # Req-31: let the ML rank today's cities so the per-scan buy budget is
        # spent on the highest-conviction markets first. Ordering ONLY — no
        # market is dropped, so nothing profitable is skipped. No-op when the
        # ML or ML_SELECT_MARKETS is off.
        markets = self._ml_prioritize_markets(markets)

        # Balance early-out: if free balance can't cover even a minimum order,
        # don't bother scanning for buys this cycle — wait for open positions to
        # sell/resolve and free up capital (rechecked next scan; live re-queries CLOB).
        free_balance = self.pm.get_balance()
        if not Config.TRADING_ENABLED:
            log.info("⏸  Trading DISABLED (/stop) — monitoring & resolving only, no new buys")
        elif free_balance < Config.MIN_ORDER_SIZE:
            log.info(f"⏸  Balance ${free_balance:.2f} < min order ${Config.MIN_ORDER_SIZE:.2f} — "
                     f"skipping buys, waiting for {len(self.pm.get_open_positions())} positions to resolve")
        else:
            # Step 2: Evaluate each market
            for market in markets:
                try:
                    self._evaluate_market(market)
                except Exception as e:
                    log.error(f"Error evaluating {market.title[:40]}: {e}")
                    continue

        # Step 3: Update prices for open positions
        self.pm.update_prices()

        # Step 4: Print dashboard
        self._print_dashboard()

        # Step 5: Daily summary
        today_str = now.strftime('%Y-%m-%d')
        if today_str != self._last_daily_summary and now.hour >= 22:
            self.telegram.send_daily_summary()
            self._last_daily_summary = today_str

        # Step 6: Periodic summary timer (Req-30). When SUMMARY_INTERVAL_MIN > 0,
        # push a compact status summary to Telegram every N minutes.
        try:
            iv = int(getattr(Config, 'SUMMARY_INTERVAL_MIN', 0) or 0)
        except Exception:
            iv = 0
        if iv > 0 and (time.time() - self._last_summary_ts) >= iv * 60:
            try:
                self.telegram.send_periodic_summary(iv)
            except Exception as e:
                log.debug(f"periodic summary failed: {e}")
            self._last_summary_ts = time.time()

        # Step 7: Periodic data export + disk guard (P3). Ships a dated zip of
        # all research files then clears them so Railway's ephemeral disk can
        # never fill and stall the bot. The disk guard runs even in continuous
        # mode, so a full disk can no longer stop trading.
        try:
            self.telegram.maybe_periodic_export()
        except Exception as _ee:
            log.debug("periodic export failed: %s" % _ee)
        try:
            from data import vps_store
            vps_store.maybe_offload()
        except Exception as _ve:
            log.debug("vps offload skip: %s" % _ve)

    def _ml_prioritize_markets(self, markets):
        """Reorder markets so the ML's top-ranked cities are evaluated first.
        Ordering only — never drops a market. Safe no-op when ML / ML_SELECT_
        MARKETS is off or the engine has no API key."""
        try:
            if not (getattr(Config, 'ML_ENABLED', True) and getattr(Config, 'ML_SELECT_MARKETS', False)):
                return markets
            ml = getattr(self, 'ml', None)
            if ml is None or not getattr(ml, 'enabled', False):
                return markets
            cities = []
            for m in markets:
                c = getattr(m, 'city', None)
                if c and c not in cities:
                    cities.append(c)
            if len(cities) <= 1:
                return markets
            wk = ''
            try:
                if hasattr(self.pm, 'get_weekly_context'):
                    wk = self.pm.get_weekly_context() or ''
            except Exception:
                wk = ''
            ranked = ml.select_markets(cities, weekly_context=wk) or []
            if not ranked:
                return markets
            rank = {str(c).strip().lower(): i for i, c in enumerate(ranked)}
            markets.sort(key=lambda m: rank.get(str(getattr(m, 'city', '')).strip().lower(), 999))
            log.info(f"  🧠 ML market priority: {', '.join(str(c) for c in ranked[:5])}")
            return markets
        except Exception as e:
            log.debug(f"ml prioritise failed: {e}")
            return markets

    # -----------------------------------------------------------------
    # STABILITY GRADE + LIQUIDITY — applied across every strategy
    # -----------------------------------------------------------------
    def _grade_multiplier(self, grade: float) -> float:
        """Map a stability grade (0..1) to a size multiplier.

        Higher grade = more stable weather = bigger size. Linear between
        GRADE_SIZE_MIN_MULT (grade 0) and GRADE_SIZE_MAX_MULT (grade 1).
        """
        if not Config.GRADE_SIZING_ENABLED:
            return 1.0
        g = max(0.0, min(1.0, grade))
        lo, hi = Config.GRADE_SIZE_MIN_MULT, Config.GRADE_SIZE_MAX_MULT
        return lo + (hi - lo) * g

    # -----------------------------------------------------------------
    # BEST-KELLY FACTOR SIZING + PORTFOLIO GUARD (Req-23)
    # -----------------------------------------------------------------
    def _sizing_params(self):
        """Build a sizing.SizingParams from the live Config (so Telegram
        /settings overrides flow straight into the allocator)."""
        return sizing.SizingParams(
            base_usd=Config.KELLY_TIER_BASE_USD,
            good_usd=Config.KELLY_TIER_GOOD_USD,
            vgood_usd=Config.KELLY_TIER_VGOOD_USD,
            perfect_usd=Config.KELLY_TIER_PERFECT_USD,
            good_strength=Config.KELLY_GOOD_STRENGTH,
            vgood_strength=Config.KELLY_VGOOD_STRENGTH,
            perfect_strength=Config.KELLY_PERFECT_STRENGTH,
            w_edge=Config.KELLY_W_EDGE,
            w_prob=Config.KELLY_W_PROB,
            w_grade=Config.KELLY_W_GRADE,
            w_winrate=Config.KELLY_W_WINRATE,
            edge_full=Config.KELLY_EDGE_FULL,
            winrate_prior=Config.KELLY_WINRATE_PRIOR,
            winrate_full_trust_n=Config.KELLY_WINRATE_FULL_TRUST_N,
            max_fraction=Config.KELLY_MAX_FRACTION,
            min_order_usd=Config.MIN_ORDER_SIZE,
        )

    def _strategy_win_rate(self, strategy: str):
        """Realized (win_rate, n_trades) for a strategy from the PM ledger.

        Returns (None, 0) when the strategy has no closed trades yet, so the
        sizing model falls back to its neutral win-rate prior.
        """
        try:
            stats = self.pm.get_per_strategy_stats() or {}
        except Exception:
            return None, 0
        s = stats.get(strategy)
        if not s:
            return None, 0
        n = int(s.get('trades', 0) or 0)
        if n <= 0:
            return None, 0
        wins = float(s.get('wins', 0) or 0)
        return (wins / n), n

    def _strategy_size_mult(self, strategy: str) -> float:
        """Per-strategy size bias — boost what wins, trim what bleeds."""
        try:
            table = getattr(Config, 'STRATEGY_SIZE_MULT', {}) or {}
        except Exception:
            table = {}
        return float(table.get(strategy, 1.0))

    def _ml_adjust(self, *, size_usd, city, bucket_label, entry_price, our_prob, edge):
        """Consult the ML decision engine (finally wired into the trade path).

        Returns (new_size_usd, ok, reason). A high-confidence SKIP vetoes the
        buy (ok=False); otherwise the ML confidence scales size between
        ML_SIZE_MIN_MULT and ML_SIZE_MAX_MULT. Safe no-op when the engine is
        disabled / has no API key (it returns a local BUY@0.7 fallback).
        """
        try:
            res = self.ml.validate_signal(
                city, bucket_label, entry_price, our_prob, edge,
            )
        except Exception as e:
            log.debug(f"ML validate failed: {e}")
            return size_usd, True, 'ml-error→allow'
        action = str(res.get('action', 'BUY') or 'BUY').upper()
        conf = float(res.get('confidence', 0.5) or 0.5)
        if action == 'SKIP' and conf >= float(getattr(Config, 'ML_VETO_CONF', 0.66)):
            return size_usd, False, f"ML VETO {conf:.0%}: {res.get('reason', '')[:40]}"
        lo = float(getattr(Config, 'ML_SIZE_MIN_MULT', 0.7))
        hi = float(getattr(Config, 'ML_SIZE_MAX_MULT', 1.2))
        mult = lo + (hi - lo) * max(0.0, min(1.0, conf))
        return size_usd * mult, True, f"ML {action} {conf:.0%}→x{mult:.2f}"

    def _can_deploy(self, size_usd: float, count_as_buy: bool = True):
        """Portfolio guard: keep dry powder for good markets later in the scan.

        Enforces a cash reserve, a max-deployed ceiling, a per-scan deploy cap,
        and a per-scan max-buys cap. Returns (ok, why).
        """
        if not getattr(Config, 'PORTFOLIO_GUARD_ENABLED', True):
            return True, 'ok'
        try:
            balance = self.pm.get_balance()
            pv = self.pm.get_portfolio_value()
        except Exception as e:
            log.debug(f"portfolio-guard read failed: {e}")
            return True, 'ok'
        # RESERVE/TAKEOUT overlay: cash fenced into the takeout pool (+ the flat
        # reserve) is NOT deployable. Subtract it so the guard treats it as
        # untouchable capital. Fail-open.
        try:
            from overlay import reserve_takeout as _rt
            _locked = float(_rt.locked_total())
            if _locked > 0:
                balance = max(0.0, balance - _locked)
        except Exception as _e:
            log.debug(f"reserve overlay skipped: {_e}")
        if pv <= 0:
            return True, 'ok'
        if count_as_buy and self._scan_buys >= int(getattr(Config, 'MAX_BUYS_PER_SCAN', 6)):
            return False, f"max {Config.MAX_BUYS_PER_SCAN} buys/scan reached"
        # Effective cash reserve = the LARGER of the % portfolio reserve and the
        # absolute-dollar floor BALANCE_RESERVE_USD (a hard $ floor the bot never
        # trades below, independent of portfolio size). 0 disables the floor.
        reserve = pv * float(getattr(Config, 'PORTFOLIO_RESERVE_PCT', 0.15))
        _dollar_floor = float(getattr(Config, 'BALANCE_RESERVE_USD', 0.0) or 0.0)
        reserve = max(reserve, _dollar_floor)
        if balance - size_usd < reserve:
            _pct = float(getattr(Config, 'PORTFOLIO_RESERVE_PCT', 0.15))
            _why = ('${:.2f} balance floor'.format(_dollar_floor)
                    if _dollar_floor >= pv * _pct
                    else '{:.0%} cash reserve'.format(_pct))
            return False, f"would breach {_why} (${reserve:.2f})"
        deployed = pv - balance
        if deployed + size_usd > pv * float(getattr(Config, 'PORTFOLIO_MAX_DEPLOY_PCT', 0.85)):
            return False, f"would exceed {Config.PORTFOLIO_MAX_DEPLOY_PCT:.0%} deployed cap"
        if self._scan_deployed_usd + size_usd > pv * float(getattr(Config, 'MAX_DEPLOY_PER_SCAN_PCT', 0.30)):
            return False, f"would exceed {Config.MAX_DEPLOY_PER_SCAN_PCT:.0%} per-scan deploy cap"
        return True, 'ok'

    def _resolve_station(self, market, city, lat, lon):
        """Confirm/adjust the EXACT resolution station for a market.

        Returns (lat, lon, ok). ok=False means the caller should SKIP this
        market (rules name a station we can't verify and skip-on-unknown is on).
        Logs 📍 confirmed / ⚠️ adjusted / ⛔ skip.
        """
        if not Config.RESOLUTION_VERIFY_ENABLED:
            return lat, lon, True
        try:
            rs = self.station_resolver.resolve(
                city, getattr(market, 'raw', None),
                ml_engine=self.resolution_verifier,
                verify_enabled=Config.RESOLUTION_VERIFY_ENABLED,
                min_conf=Config.RESOLUTION_VERIFY_MIN_CONF,
                skip_on_unknown=Config.RESOLUTION_SKIP_ON_UNKNOWN,
            )
        except Exception as e:
            log.debug(f"station resolve failed {city}: {e}")
            return lat, lon, True
        if rs is None:
            return lat, lon, True
        if rs.source == 'skip' or rs.coords is None:
            log.info(f"   ⛔ STATION {city} — {rs.reason} — skip")
            return lat, lon, False
        if rs.source == 'adjusted-ml':
            log.info(f"   ⚠️  STATION {city} — adjusted to {rs.icao or '?'} "
                     f"({rs.station_name or '?'}) — {rs.reason}")
            return rs.coords[0], rs.coords[1], True
        # confirmed / default: keep the resolver's coords (same as hardcoded).
        log.debug(f"   📍 STATION {city} — {rs.source} {rs.icao or 'hardcoded'} ({rs.reason})")
        if rs.coords:
            return rs.coords[0], rs.coords[1], True
        return lat, lon, True

    def _get_book(self, token_id: str):
        """Fetch the live order book for a token, with a short TTL cache.

        Only called when a signal actually fires, so multiple legs/strategies
        on the same token share one network call. Returns None on failure.
        """
        if not token_id:
            return None
        ttl = Config.LIQUIDITY_BOOK_CACHE_SECONDS
        cached = self._book_cache.get(token_id)
        if cached and (time.time() - cached[0]) < ttl:
            return cached[1]
        book = None
        try:
            book = self.clob.get_orderbook(token_id)
        except Exception as e:
            log.debug(f"orderbook fetch failed {token_id[:10]}: {e}")
        self._book_cache[token_id] = (time.time(), book)
        return book

    def _place(self, *, token_id, condition_id, entry_price, base_size_usd,
               market_title, bucket_label, strategy, city, slug,
               resolution_time, edge=0.0, grade=None, hold_hint=False,
               early_exit_price=None, apply_grade_size=True,
               reason='', lock_confidence=0.0, signal='',
               our_prob=0.0, use_factor_kelly=False, use_ml=False,
               cluster_box='', count_as_buy=True, basket_leg=False):
        """Single placement path for ALL strategies.

        Applies the stability GRADE (gate + size multiplier), the best-Kelly
        FACTOR sizing (when use_factor_kelly), the ML decision engine (when
        use_ml), the LIQUIDITY guard (maker-at-bid entry, skip thin/wide books),
        and the PORTFOLIO guard, then opens the position and sets the
        grade-based exit. Returns the position or None.

        `apply_grade_size=False` skips the grade size multiplier (used by
        strategies that already size their own legs) while still enforcing the
        grade gate, liquidity guard, and exit rule.

        `use_factor_kelly=True` replaces base_size_usd with the multi-factor
        Kelly stake (trading/sizing.py): tiered $3/$5/$10/$15 by signal strength
        (edge + P(win) + grade + realized win-rate) × per-strategy bias.

        `use_ml=True` consults the ML engine for a veto + size scale.

        `cluster_box` tags a peak-cluster leg with its "Box N" group label so
        the UI can render the basket as one unit and exempt it from stops.

        `basket_leg=True` marks one leg of an ATOMIC all-or-none basket
        (peak_cluster / peaker cool|warm basket). The caller already ran the
        portfolio guard ONCE on the whole basket, so a basket leg SKIPS the
        per-leg portfolio guard, is NOT size-trimmed on a thin book, and is
        bumped to the venue minimum instead of being dropped below it — so a
        basket is never picked apart leg-by-leg to a single surviving leg.

        `reason` / `lock_confidence` / `signal` are observability metadata
        carried into the position so paper logs record WHY each buy fired.
        """
        # Hard safety: never place when trading is disabled (toggled mid-scan via /stop).
        if not Config.TRADING_ENABLED:
            return None

        # -- OVERLAY GATES (overlay/*.py): drop a leg BEFORE any order is built.
        # (1) strategy_gate: decoupled per-sub-strategy on/off (a disabled
        #     sub-strategy never trades even if it generated a signal).
        # (2) entry_gate: entry-band filter (late_observed_no <0.50 junk band,
        #     late_observed_yes only cheap).
        # (3) city_throttle: skip blocklisted cities.
        # All fail OPEN (any error -> trade proceeds) and cost only a dict lookup.
        try:
            from overlay import strategy_gate as _sg
            _ok, _why = _sg.trade_allowed(strategy)
            if not _ok:
                self._funnel['toggle_off'] += 1
                log.info(f"   \u26d4 GATE {strategy}:{bucket_label[:18]} \u2014 {_why}")
                return None
        except Exception as _e:
            log.debug(f"strategy_gate skipped: {_e}")
        try:
            from overlay import entry_gate as _eg
            # Pass the model win-prob so the late_observed_yes edge gate and the
            # global longshot floor can use forecast edge (fail-open if absent).
            _mp = None
            try:
                _mp = float(our_prob)
            except (TypeError, ValueError, NameError):
                _mp = None
            _ok, _why = _eg.entry_allowed(strategy, entry_price, model_prob=_mp)
            if not _ok:
                self._funnel['band_gate'] += 1
                log.info(f"   \u26d4 BAND {strategy}:{bucket_label[:18]} \u2014 {_why}")
                return None
        except Exception as _e:
            log.debug(f"entry_gate skipped: {_e}")
        try:
            from overlay import city_throttle as _ct
            _ok, _why = _ct.city_allowed(city)
            if not _ok:
                self._funnel['city_block'] += 1
                log.info(f"   \u26d4 CITY {strategy}:{bucket_label[:18]} \u2014 {_why}")
                return None
        except Exception as _e:
            log.debug(f"city_throttle skipped: {_e}")

        # (4) golden_gate: NEW fusion overlay (overlay/golden_gate.py). Two jobs,
        #     both fully fail-open and additive (do NOT touch other strategies):
        #       a. TIMING: drop a late_observed leg whose days-to-resolution
        #          bucket toggle (same-day / 1d / 2d / 3d) is switched OFF.
        #       b. GOLDEN: if the leg passes the audited Golden Filter, RE-TAG it
        #          to 'golden_no' so it sizes with the golden boost and shows as
        #          a separately-named strategy. GOLDEN_NO_ENABLED off => unchanged.
        try:
            from overlay import golden_gate as _gg
            _ok, _why = _gg.timing_allowed(strategy, resolution_time)
            if not _ok:
                self._funnel['toggle_off'] += 1
                log.info(f"   \u26d4 TIMING {strategy}:{bucket_label[:18]} \u2014 {_why}")
                return None
            _is_gold, _greason = _gg.classify_golden(
                strategy, entry_price, edge, grade, resolution_time,
                bucket_label=bucket_label, side_hint=signal)
            if _is_gold and strategy != _gg.GOLDEN_TAG:
                log.info(f"   \U0001F947 {_greason} \u2014 re-tag {strategy} -> golden_no")
                strategy = _gg.GOLDEN_TAG
                signal = _gg.GOLDEN_TAG  # Req-27 #1: re-tag signal too so BUY log/alert shows golden_no (not observed_no)
        except Exception as _e:
            log.debug(f"golden_gate skipped: {_e}")

        if grade is None:
            grade = Config.GRADE_NEUTRAL
        if early_exit_price is None:
            early_exit_price = Config.STABILITY_EARLY_EXIT_PRICE

        # -- Grade gate: skip trades on unpredictable city-days --
        if Config.GRADE_SIZING_ENABLED and grade < Config.GRADE_MIN_TO_TRADE:
            self._funnel['grade_skip'] += 1
            log.info(f"   ⏭️  GRADE SKIP {strategy}:{bucket_label[:18]} — grade {grade:.2f} < {Config.GRADE_MIN_TO_TRADE}")
            return None

        # -- PRICE FLOORS --
        # (1) HARD DUST FLOOR (all strategies): below ABS_PRICE_FLOOR a leg can't
        #     even rest on the 1c-tick venue — true junk, always reject.
        abs_floor = getattr(Config, 'ABS_PRICE_FLOOR', 0.01)
        if entry_price < abs_floor:
            self._funnel['dust'] += 1
            log.info(f"   ⏭️  DUST {strategy}:{bucket_label[:18]} — ask ${entry_price:.4f} < ${abs_floor:.2f}")
            return None
        # (2) SELLABILITY FLOOR (early-exit strategies only): a leg that plans to
        #     SELL before resolution needs a real bid (~5c) to exit into. HOLD-to-
        #     resolution legs bypass this — an EV+ 2-4c locked tail never needs a
        #     bid and is exactly the reference 90%-WR wallet's cheap-tail edge.
        if (not hold_hint) and entry_price < Config.MIN_ENTRY_PRICE:
            self._funnel['price_floor'] += 1
            log.info(f"   ⏭️  PRICE FLOOR {strategy}:{bucket_label[:18]} — ask ${entry_price:.4f} < ${Config.MIN_ENTRY_PRICE:.2f} (no exit bid)")
            return None

        # -- SIZING --
        # Best-Kelly factor sizing (Req-23) when the caller opts in, otherwise
        # the legacy grade-scaled base size. Either way a per-strategy bias is
        # applied so we lean toward the strategy that actually wins.
        strat_mult = self._strategy_size_mult(strategy)
        if use_factor_kelly and getattr(Config, 'KELLY_FACTOR_SIZING_ENABLED', True):
            wr, n = self._strategy_win_rate(strategy)
            size_usd = sizing.factor_kelly_stake(
                edge=edge, prob_win=our_prob, balance=self.pm.get_balance(),
                grade=grade, win_rate=wr, n_trades=n,
                strategy_mult=strat_mult, params=self._sizing_params(),
            )
            log.info(f"   🎚️  KELLY {strategy}:{bucket_label[:18]} → ${size_usd:.2f} "
                     f"[{sizing.describe(edge, our_prob, grade, wr, n, strat_mult, self._sizing_params())}]")
            if size_usd <= 0:
                self._funnel['kelly_zero'] += 1
                return None
        else:
            mult = self._grade_multiplier(grade) if apply_grade_size else 1.0
            size_usd = base_size_usd * mult * strat_mult

        # -- SIZING OVERLAYS (overlay/*.py) \u2014 bounded, multiplicative, fail-open.
        # (a) late_observed_no entry-band lean, (b) per-city over/under-weight,
        # (c) adaptive per-strategy boost from realized win/loss. Each returns
        # 1.0 when disabled or on error, so base sizing is untouched.
        try:
            from overlay import sizing_overlay as _so
            _m = float(_so.size_multiplier(strategy, entry_price, edge))
            from overlay import city_throttle as _ct2
            _m *= float(_ct2.city_multiplier(city))
            from overlay import adaptive_boost as _ab
            _m *= float(_ab.multiplier(strategy))
            if _m > 0 and abs(_m - 1.0) > 1e-6:
                _old = size_usd
                size_usd = size_usd * _m
                log.info(f"   \U0001f39a OVERLAY size {strategy}:{bucket_label[:18]} "
                         f"${_old:.2f}\u2192${size_usd:.2f} (\u00d7{_m:.2f})")
        except Exception as _e:
            log.debug(f"sizing overlay skipped: {_e}")

        # -- Liquidity AWARENESS (adapt, don't block) --
        # Read the book and adjust: enter MAKER at best_bid, trim size on
        # thin/wide books, and force hold-to-resolution when it's too thin to
        # exit. Only skips if LIQUIDITY_STRICT_BLOCK is explicitly enabled.
        fill_price = entry_price
        if Config.LIQUIDITY_GUARD_ENABLED:
            book = self._get_book(token_id)
            if book and book.get('best_bid', 0) > 0:
                bid_depth = book['bids'][0][1] if book.get('bids') else 0.0
                ask_depth = book['asks'][0][1] if book.get('asks') else 0.0
                chk = self.liquidity.can_enter(
                    market_price=entry_price,
                    best_bid=book['best_bid'], best_ask=book['best_ask'],
                    edge=edge, bid_depth=bid_depth, ask_depth=ask_depth,
                )
                # MAKER entry: post at the bid (cheaper, 0% fee, earn the spread).
                fill_price = book['best_bid']
                if not chk.passed:
                    if Config.LIQUIDITY_STRICT_BLOCK and not basket_leg:
                        self._funnel['liq_skip'] += 1
                        log.info(f"   ⏭️  LIQ SKIP {strategy}:{bucket_label[:18]} — {chk.reason}")
                        return None
                    # Aware mode: hold to resolution (can't rely on an exit). An
                    # ATOMIC basket leg keeps FULL size (trimming cheap legs is
                    # exactly what collapsed baskets to a single leg).
                    if not basket_leg:
                        size_usd *= Config.LIQUIDITY_THIN_SIZE_MULT
                    hold_hint = True
                    self._funnel['liq_thin'] += 1
                    log.info(f"   💧 LIQ THIN {strategy}:{bucket_label[:18]} — {chk.reason} "
                             f"→ {'basket keep-size' if basket_leg else f'size x{Config.LIQUIDITY_THIN_SIZE_MULT}'} + hold, maker@{fill_price:.3f}")
            else:
                # No book / no bid: stay at scan price, hold. Basket legs keep
                # full size (atomic all-or-none); solo legs trim.
                if Config.LIQUIDITY_STRICT_BLOCK and not basket_leg:
                    self._funnel['liq_skip'] += 1
                    log.info(f"   ⏭️  LIQ SKIP {strategy}:{bucket_label[:18]} — no order book")
                    return None
                if not basket_leg:
                    size_usd *= Config.LIQUIDITY_THIN_SIZE_MULT
                hold_hint = True
                self._funnel['liq_nobook'] += 1
                log.info(f"   💧 LIQ NOBOOK {strategy}:{bucket_label[:18]} — no bid → {'basket keep-size' if basket_leg else f'size x{Config.LIQUIDITY_THIN_SIZE_MULT}'} + hold")

        if fill_price <= 0:
            return None
        # Maker re-pricing must respect the same floors. Dust is always rejected;
        # the sellability floor again applies only to early-exit legs (hold legs,
        # including anything forced to hold by a thin book above, may rest cheap).
        if fill_price < abs_floor:
            self._funnel['dust'] += 1
            log.info(f"   ⏭️  DUST {strategy}:{bucket_label[:18]} — maker fill ${fill_price:.4f} < ${abs_floor:.2f}")
            return None
        if (not hold_hint) and fill_price < Config.MIN_ENTRY_PRICE:
            self._funnel['price_floor'] += 1
            log.info(f"   ⏭️  PRICE FLOOR {strategy}:{bucket_label[:18]} — maker fill ${fill_price:.4f} < ${Config.MIN_ENTRY_PRICE:.2f} (no exit bid)")
            return None

        # -- ML DECISION (veto + size scale) --
        if use_ml and getattr(Config, 'ML_DECISION_ENABLED', True):
            size_usd, ml_ok, ml_reason = self._ml_adjust(
                size_usd=size_usd, city=city, bucket_label=bucket_label,
                entry_price=fill_price, our_prob=our_prob, edge=edge,
            )
            if not ml_ok:
                self._funnel['ml_veto'] += 1
                log.info(f"   🤖 {ml_reason} — skip {strategy}:{bucket_label[:18]}")
                return None
            log.debug(f"   🤖 {ml_reason} {strategy}:{bucket_label[:18]} → ${size_usd:.2f}")

        # -- PORTFOLIO GUARD (keep dry powder for later/better markets) --
        # Basket legs SKIP the per-leg guard: the caller already ran the guard
        # ONCE on the whole basket cost (atomic all-or-none), so we never reject
        # individual legs here and end up with a partial 1-leg basket.
        if not basket_leg:
            ok, why = self._can_deploy(size_usd, count_as_buy=count_as_buy)
            if not ok:
                self._funnel['portfolio_guard'] += 1
                log.info(f"   🏦 GUARD {strategy}:{bucket_label[:18]} — {why}")
                return None

        if size_usd < Config.MIN_ORDER_SIZE:
            if basket_leg:
                # Don't DROP a cheap basket leg — bump it to the venue minimum so
                # the basket stays intact (dropping cheap legs one by one is what
                # collapsed baskets to a single surviving leg).
                size_usd = Config.MIN_ORDER_SIZE
            else:
                self._funnel['below_min'] += 1
                return None
        shares = size_usd / fill_price

        pos = self.pm.add_position(
            token_id=token_id,
            condition_id=condition_id,
            entry_price=fill_price,
            shares=shares,
            cost_usd=size_usd,
            market_title=market_title,
            bucket_label=bucket_label,
            strategy=strategy,
            city=city,
            slug=slug,
            resolution_time=resolution_time,
            edge=edge,
            reason=reason,
            grade=grade,
            lock_confidence=lock_confidence,
            signal=signal or strategy,
            hold_to_resolution=bool(hold_hint),
            cluster_box=cluster_box,
        )
        if pos:
            self._funnel['placed'] += 1
            # Tally per-scan capital deployment (portfolio guard budget).
            self._scan_deployed_usd += size_usd
            if count_as_buy:
                self._scan_buys += 1
            # Grade-based exit: stable/rain → hold to resolution; else take profit early.
            if hold_hint:
                pos.take_profit_price = 0.99
                pos.exit_reason = 'hold_grade'
            else:
                pos.take_profit_price = early_exit_price
                pos.exit_reason = 'grade_early_exit'
        else:
            # Passed every _place gate but PositionManager still declined: almost
            # always the DUPLICATE GUARD (already holding this token+strategy),
            # or a min-notional / insufficient-balance / unfilled maker check.
            self._funnel['add_reject'] += 1
        return pos

    def _evaluate_market(self, market):
        """Evaluate a single weather market for trading opportunities."""
        city = market.city
        city_lower = city.lower().replace(' ', '')

        # Get coordinates
        coords = get_city_coords(city)
        if not coords:
            # Try without spaces and common variants
            for key, val in CITY_COORDS.items():
                if city_lower in key.replace(' ', '') or key.replace(' ', '') in city_lower:
                    coords = val
                    break
        if not coords:
            self._funnel['no_coords'] += 1
            log.debug(f"No coordinates for: {city}")
            return

        lat, lon = coords

        # -- RESOLUTION-STATION VERIFICATION --
        # Forecast/observe the EXACT airport this market settles on. If the rules
        # name a different station we adjust coordinates (or skip when unknown).
        lat, lon, station_ok = self._resolve_station(market, city, lat, lon)
        if not station_ok:
            return

        # -- HIGHEST-TEMP-ONLY GATE (optional) --
        # When enabled, only trade daily-high markets. Disabled by default now
        # that the observed strategy locks the low overnight and the high in the
        # afternoon, and trades the NO side either way.
        if Config.HIGHEST_TEMP_ONLY and 'highest' not in (market.market_type or '').lower():
            log.debug(f"   ⏭️  SKIP {city} {market.market_type} — highest-temp only")
            return

        # -- OUTCOME-DECIDED GATE (timezone-aware, PER-STRATEGY) --
        # A "decided/locked" day KILLS the edge for forecast-guessing strategies
        # but IS the edge for observation strategies (the recorded extreme is a
        # hard floor/ceiling while the book still prices stale forecast doubt).
        # So this is NO LONGER a blanket skip. We only HARD-skip when the city's
        # local day is FULLY OVER (value recorded, just awaiting UMA payout —
        # nothing left for anyone). Otherwise we flag a LOCK WINDOW and let each
        # strategy opt in below via its own *_TRADE_DECIDED config flag.
        # (This was the bug behind "signals but zero trades": the old gate
        # returned here and LateObserved never even ran.)
        in_lock_window = False
        if Config.SKIP_DECIDED_MARKETS:
            try:
                decided, why = outcome_decided(
                    market.market_type, market.measurement_date, lat, lon
                )
            except Exception as e:
                decided, why = False, ''
                log.debug(f"decided-gate failed {city}: {e}")
            if decided:
                fully_over = False
                try:
                    if market.measurement_date is not None:
                        fully_over = (city_local_now(lat, lon).date()
                                      > market.measurement_date.date())
                except Exception as e:
                    log.debug(f"day-over check failed {city}: {e}")
                if fully_over:
                    self._funnel['over'] += 1
                    log.info(f"   ⛔ OVER {city} {market.market_type.split('_')[0]} "
                             f"{market.measurement_date:%b-%d} — {why} — skip (day fully over, awaiting UMA)")
                    return
                in_lock_window = True
                self._funnel['lock_window'] += 1
                log.info(f"   🔓 LOCK WINDOW {city} {market.market_type.split('_')[0]} "
                         f"{market.measurement_date:%b-%d} — {why} — observation strategies only")

        # Fetch forecasts
        target_time = market.resolution_time
        forecasts = self.fetcher.fetch_all(lat, lon, city, target_time)
        # WEATHER-DATA BUY GUARD (Req-30): NEVER evaluate/buy a market without
        # sufficient live weather data. fetch_all returns nothing when every
        # provider failed / is cooling down (the "Open-Meteo cooling down" /
        # "observed fetch returned no data" warnings). This single choke point
        # protects EVERY strategy below — they all run after this return.
        n_models = len(forecasts) if forecasts else 0
        min_models = max(1, int(getattr(Config, 'WEATHER_MIN_FORECAST_MODELS', 1)))
        if getattr(Config, 'WEATHER_BUY_GUARD_ENABLED', True) and n_models < min_models:
            self._funnel['no_weather_data'] += 1
            log.warning(f"   🚫 WEATHER GUARD {city} — {n_models} forecast model(s) "
                        f"(< {min_models}); skip, no buys without sufficient weather data")
            return
        if not forecasts:
            self._funnel['no_forecast'] += 1
            log.debug(f"No forecasts for {city}")
            return

        # Build bucket list from outcomes (carry BOTH legs: YES + NO).
        buckets = []
        token_ids = {}
        no_token_ids = {}
        condition_ids = {}
        no_prices = {}
        for outcome in market.outcomes:
            label = outcome['label']
            lo = outcome.get('bucket_low', float('-inf'))
            hi = outcome.get('bucket_high', float('inf'))
            buckets.append((label, lo, hi))
            token_ids[label] = outcome.get('token_id', '')
            no_token_ids[label] = outcome.get('token_id_no', '')
            no_prices[label] = outcome.get('price_no', max(0.0, 1.0 - outcome.get('price', 0.5)))
            condition_ids[label] = outcome.get('condition_id', '')

        if not buckets:
            return

        # Run probability engine
        bucket_probs = self.engine.estimate_bucket_probabilities(
            forecasts, buckets, target_time, market_type=market.market_type
        )

        # Get market prices (from scan data, already fetched)
        market_prices = {o['label']: o.get('price', 0.5) for o in market.outcomes}

        balance = self.pm.get_balance()

        # -- STABILITY GRADE (computed ONCE, applied to every strategy below) --
        # Stability is a GRADE, not a strategy: it scales position size and
        # decides hold-to-resolution vs early-exit for ALL strategies.
        stab = None
        if Config.STABILITY_ENABLED:
            try:
                stab = self.stability_engine.assess(
                    city, market.resolution_time, lat=lat, lon=lon
                )
            except Exception as e:
                log.debug(f"Stability assess failed {city}: {e}")
        grade = stab.score if stab else Config.GRADE_NEUTRAL
        # Hold to resolution when weather is stable, or when rain pins the high.
        hold_hint = bool(stab and (stab.hold_to_resolution() or stab.rain_block))
        early_exit_price = Config.STABILITY_EARLY_EXIT_PRICE
        if stab:
            log.info(
                f"   📐 GRADE {city}: {grade:.2f} ({stab.trend}"
                f"{', rain-block' if stab.rain_block else ''}) "
                f"× size={self._grade_multiplier(grade):.2f} | "
                f"{'HOLD' if hold_hint else f'exit@{early_exit_price:.2f}'}"
            )

        # ------------------------------------------------------
        # LATE OBSERVED-TEMPERATURE — THE PRIMARY strategy.
        # Once the local day's peak/trough is locked, the observed extreme is a
        # hard floor/ceiling on settlement. YES the locked bucket and NO the
        # buckets the observed data has ruled out, with fee-aware gating.
        # Trades in the LOCK WINDOW by default (that's its whole edge).
        # ------------------------------------------------------
        if Config.LATE_OBSERVED_ENABLED and (
            not in_lock_window or getattr(Config, 'LATE_OBSERVED_TRADE_DECIDED', True)
        ):
            mode = 'low' if 'low' in (market.market_type or '').lower() else 'high'
            # REQUEST REDUCER (overlay/golden_gate.py): late_observed_no only
            # trades markets resolving AFTER the next day (same-day is a proven
            # loser, OFF by default). When same-day is off, skip the Open-Meteo
            # observed-state fetch for a same-day market entirely. Fail-open.
            _obs_fetch_ok = True
            try:
                from overlay import golden_gate as _gg
                if not _gg.late_observed_should_fetch('late_observed_no', market.resolution_time):
                    _obs_fetch_ok = False
                    self._funnel['toggle_off'] += 1
                    log.info(f"   \u23ed\ufe0f  LATE-OBS skip-fetch {city} {mode} \u2014 same-day market")
            except Exception as _e:
                log.debug(f"late_observed fetch-reducer skipped: {_e}")
            observed_state = None
            if _obs_fetch_ok:
                try:
                    observed_state = self.observed.get_state(
                        lat, lon, market.measurement_date, mode
                    )
                except Exception as e:
                    log.debug(f"observed-state fetch failed {city}: {e}")
            if observed_state is None:
                # The PRIMARY edge needs observed station data. Make the silence
                # visible: no data = no observed extreme yet (early in the local
                # day / fetch failed / offline). This is the #1 reason the
                # primary stays quiet, so log it instead of skipping silently.
                self._funnel['primary_no_data'] += 1
                log.info(f"   🌙 PRIMARY no-data {city} {mode} — no observed station reading yet "
                         f"(early in local day / fetch failed / offline)")
            if observed_state is not None:
                obs_signals = self.late_observed.evaluate(
                    market.title, buckets, market_prices, token_ids, balance,
                    city, observed_state,
                    no_prices=no_prices, no_token_ids=no_token_ids,
                    grade=grade, market_type=market.market_type,
                )
                if obs_signals:
                    self._funnel['primary_signal'] += 1
                for sig in obs_signals:
                    self.signals_generated += 1
                    log.info(
                        f"   🌡️  OBSERVED {city} {mode} | lock={sig.lock_confidence:.0%} "
                        f"obs={sig.observed_extreme_c:.1f}°C | {len(sig.legs)} legs | {sig.reason}"
                    )
                    # Req-27 #g: optional ML-veto batching. Pre-warm the ML
                    # entry-cache for all legs of this observed signal with ONE
                    # grouped model call (group size N) instead of one call per
                    # leg. Opt-in + fail-open; the grouped verdicts land in the
                    # same cache the per-leg validate_signal() calls below read.
                    try:
                        from overlay import ml_batch as _mlb
                        _mlb.prewarm(self.ml, [
                            {'city': city,
                             'bucket_label': f"{_lg.side} {_lg.bucket_label}",
                             'entry_price': _lg.price,
                             'our_prob': getattr(_lg, 'our_probability', 0.0),
                             'edge': _lg.edge}
                            for _lg in sig.legs if _lg.token_id
                        ])
                    except Exception:
                        pass
                    for leg in sig.legs:
                        side = leg.side.lower()
                        token = leg.token_id
                        if not token:
                            log.debug(f"      skip {side} {leg.bucket_label[:18]}: no token id")
                            continue
                        pos = self._place(
                            token_id=token,
                            condition_id=condition_ids.get(leg.bucket_label, ''),
                            entry_price=leg.price,
                            base_size_usd=leg.size_usd,
                            market_title=market.title,
                            bucket_label=f"{leg.side} {leg.bucket_label}",
                            strategy=f'late_observed_{side}',
                            city=city,
                            slug=market.slug,
                            resolution_time=market.resolution_time,
                            edge=leg.edge,
                            grade=grade,
                            hold_hint=True,  # observed edge realizes at resolution
                            early_exit_price=early_exit_price,
                            apply_grade_size=False,  # strategy already Kelly-sizes
                            reason=sig.reason,
                            lock_confidence=sig.lock_confidence,
                            signal=f'late_observed_{side}',
                            our_prob=getattr(leg, 'our_probability', 0.0),
                            use_factor_kelly=True,   # best-Kelly factor sizing
                            use_ml=True,             # ML veto + size scale
                        )
                        if pos:
                            self.trades_placed += 1
                            self.telegram.notify_trade(
                                'BUY', f"{leg.side} {leg.bucket_label}", pos.entry_price,
                                pos.cost_usd, pos.shares, pos.strategy,
                                edge=leg.edge, city=city,
                            )

        # ==================================================================
        # NEW STRATEGIES (2026-08-19): REMASTER + NO-ARBITRAGE. Both run on the
        # SAME observed_state as late_observed_no (which is left untouched),
        # placing under their own tags. Fully guarded + fail-open so they can
        # never disturb the proven core edge.
        # ==================================================================
        _os_state = locals().get('observed_state', None)
        if _os_state is not None:
            _days_to_res = None
            try:
                from datetime import datetime as _dt, timezone as _tz
                if getattr(market, 'resolution_time', None):
                    _rt = market.resolution_time
                    if getattr(_rt, 'tzinfo', None) is None:
                        _rt = _rt.replace(tzinfo=_tz.utc)
                    _days_to_res = (_rt - _dt.now(_tz.utc)).total_seconds() / 86400.0
            except Exception:
                _days_to_res = None
            try:
                _wr = self._strategy_win_rate('late_observed_no')
            except Exception:
                _wr = None

            # ---- REMASTER: smarter second opinion via grade+edge engine ----
            try:
                if getattr(Config, 'LATE_OBSERVED_REMASTER_ENABLED', True):
                    _rm_signals = self.late_observed_remaster.evaluate(
                        market.title, buckets, market_prices, token_ids, balance,
                        city, _os_state, no_prices=no_prices, no_token_ids=no_token_ids,
                        grade=grade, market_type=market.market_type,
                        days_to_resolution=_days_to_res, win_rate=_wr,
                        base_signals=locals().get('obs_signals', None),
                    )
                    for _sig in (_rm_signals or []):
                        for _leg in _sig.legs:
                            if not _leg.token_id:
                                continue
                            _pos = self._place(
                                token_id=_leg.token_id,
                                condition_id=condition_ids.get(_leg.bucket_label, ''),
                                entry_price=_leg.price,
                                base_size_usd=_leg.size_usd,
                                market_title=market.title,
                                bucket_label=f"{_leg.side} {_leg.bucket_label}",
                                strategy='late_observed_remaster',
                                city=city, slug=market.slug,
                                resolution_time=market.resolution_time,
                                edge=_leg.edge, grade=grade, hold_hint=True,
                                early_exit_price=early_exit_price,
                                apply_grade_size=False, reason=_sig.reason,
                                lock_confidence=_sig.lock_confidence,
                                signal='late_observed_remaster',
                                our_prob=getattr(_leg, 'our_probability', 0.0),
                                use_factor_kelly=True, use_ml=True,
                            )
                            if _pos:
                                self.trades_placed += 1
                                self.telegram.notify_trade(
                                    'BUY', f"{_leg.side} {_leg.bucket_label}", _pos.entry_price,
                                    _pos.cost_usd, _pos.shares, _pos.strategy,
                                    edge=_leg.edge, city=city,
                                )
            except Exception as _rme:
                log.debug(f"remaster placement skipped: {_rme}")

            # ---- NO-ARBITRAGE: near-arb NO basket over ruled-out buckets ----
            try:
                if getattr(Config, 'LATE_OBS_NO_ARB_ENABLED', True):
                    _na_signals = self.late_observed_no_arb.evaluate(
                        market.title, buckets, balance, city, _os_state,
                        no_prices=no_prices, no_token_ids=no_token_ids,
                        grade=grade, market_type=market.market_type,
                        days_to_resolution=_days_to_res, win_rate=_wr,
                    )
                    for _sig in (_na_signals or []):
                        for _leg in _sig.legs:
                            if not _leg.token_id:
                                continue
                            _pos = self._place(
                                token_id=_leg.token_id,
                                condition_id=condition_ids.get(_leg.bucket_label, ''),
                                entry_price=_leg.price,
                                base_size_usd=_leg.size_usd,
                                market_title=market.title,
                                bucket_label=f"NO {_leg.bucket_label}",
                                strategy='late_observed_no_arb',
                                city=city, slug=market.slug,
                                resolution_time=market.resolution_time,
                                edge=_leg.edge, grade=grade, hold_hint=True,
                                early_exit_price=early_exit_price,
                                apply_grade_size=False, reason=_sig.reason,
                                lock_confidence=_sig.lock_confidence,
                                signal='late_observed_no_arb',
                                our_prob=getattr(_leg, 'our_probability', 0.0),
                                use_factor_kelly=True, use_ml=True,
                            )
                            if _pos:
                                self.trades_placed += 1
                                self.telegram.notify_trade(
                                    'BUY', f"NO {_leg.bucket_label}", _pos.entry_price,
                                    _pos.cost_usd, _pos.shares, _pos.strategy,
                                    edge=_leg.edge, city=city,
                                )
            except Exception as _nae:
                log.debug(f"no-arb placement skipped: {_nae}")

        # ------------------------------------------------------
        # QUICK FLIP (Req-28 v3) — HIGH-confidence mispricing flip with a 10%
        # profit target and a PROFIT-ONLY exit (the timer NEVER books a flip at
        # a loss/breakeven — see trading/exit_policies.check_flip_exits). Enters
        # a mispriced bucket before the book corrects and books the first ~10%
        # rung; also hunts mispriced NO tokens (QUICK_FLIP_NO_SIDE) for the same
        # flip. Tighter defaults (higher confidence, smaller size, fewer per
        # market) so it triggers less and stops eating capital. Runs in the lock
        # window too (the forecast-change edge holds).
        # ------------------------------------------------------
        if getattr(Config, 'QUICK_FLIP_ENABLED', False) and (
            not in_lock_window or getattr(Config, 'QUICK_FLIP_TRADE_DECIDED', True)
        ):
            qf_mode = 'low' if 'low' in (market.market_type or '').lower() else 'highest'
            try:
                flip_signals = self.quick_flip.evaluate(
                    market.title, bucket_probs, market_prices, market_prices,
                    token_ids, balance, city=city, market_type=qf_mode,
                    no_prices=no_prices, no_token_ids=no_token_ids,
                )
            except Exception as e:
                flip_signals = []
                log.debug(f"quick_flip eval failed {city}: {e}")
            # Concurrent-flip cap: a flip is a fast, small information-arb trade;
            # don't let many pile up and tie down capital.
            open_flips = sum(1 for p in self.pm.get_open_positions() if p.strategy == 'quick_flip')
            if flip_signals and open_flips >= int(getattr(Config, 'QUICK_FLIP_MAX_CONCURRENT', 6)):
                log.info(f"   ⏸️  FLIP CAP {city} — {open_flips} open flips ≥ cap, skipping new flips")
                flip_signals = []
            for fsig in flip_signals:
                self.signals_generated += 1
                qf_side = str(getattr(fsig, 'side', 'YES') or 'YES').upper()
                side_tag = '' if qf_side == 'YES' else 'NO '
                disp_label = f"{side_tag}{fsig.bucket_label}"
                log.info(
                    f"   ⚡ FLIP {city} {qf_side} | {disp_label[:28]} @ ${fsig.entry_price:.3f} "
                    f"→ ${fsig.target_price:.3f} ({fsig.expected_roi_pct:.0f}% ROI, "
                    f"{fsig.expected_hold_minutes}m) | {fsig.entry_reason}"
                )
                qf_prob = getattr(fsig, 'our_prob', 0.0)
                qf_edge = max(0.0, qf_prob - fsig.entry_price) if qf_prob else (fsig.expected_roi_pct / 100.0)
                pos = self._place(
                    token_id=fsig.token_id,
                    condition_id=condition_ids.get(fsig.bucket_label, ''),
                    entry_price=fsig.entry_price,
                    base_size_usd=fsig.size_usd,
                    market_title=market.title,
                    bucket_label=disp_label,
                    strategy='quick_flip',
                    city=city,
                    slug=market.slug,
                    resolution_time=market.resolution_time,
                    edge=qf_edge,
                    grade=grade,
                    hold_hint=False,            # quick-flip EXITS into the correction (profit-only ladder)
                    early_exit_price=fsig.target_price,
                    apply_grade_size=False,     # quick-flip sizes itself
                    reason=fsig.entry_reason,
                    lock_confidence=fsig.confidence,
                    signal='quick_flip',
                    our_prob=qf_prob,
                    use_factor_kelly=True,      # best-Kelly factor sizing
                    use_ml=True,                # ML veto + size scale
                )
                if pos:
                    # Carry the flip's hold window so the loop can book-or-cut it
                    # (PROFIT-ONLY — the timer never loss-cuts a flip).
                    pos.flip_max_hold_minutes = fsig.expected_hold_minutes
                    pos.flip_side = qf_side
                    self.trades_placed += 1
                    self.telegram.notify_trade(
                        'BUY', disp_label, pos.entry_price,
                        pos.cost_usd, pos.shares, 'quick_flip',
                        edge=qf_edge, city=city,
                    )

        # ------------------------------------------------------
        # PEAK CLUSTER — parallel adjacent-bucket basket around the estimated
        # peak. Buys 3-7 neighbouring buckets whose COMBINED per-share cost is
        # < PEAK_CLUSTER_MAX_COST, so ANY single winning leg profits after fees.
        # HOLDS TO RESOLUTION (never stop-lossed/trailed — the basket only works
        # if every leg rides to settlement). Forecast-based → skipped once the
        # extreme is locked. Each basket is grouped as "Peak Cluster Box N":
        # ONE Telegram alert + one status group for all its legs. This is the
        # wide 4-leg "both shoulders" shape that the peaker delegates here.
        # ------------------------------------------------------
        if getattr(Config, 'PEAK_CLUSTER_ENABLED', True) and (
            not in_lock_window or getattr(Config, 'PEAK_CLUSTER_TRADE_DECIDED', False)
        ):
            try:
                cluster_signals = self.peak_cluster.evaluate(
                    market.title, bucket_probs, market_prices, token_ids,
                    balance, city=city, grade=grade,
                )
            except Exception as e:
                cluster_signals = []
                log.debug(f"peak_cluster eval failed {city}: {e}")
            # CONTIGUITY FIX (overlay/cluster_contiguous.py): rebuild each basket
            # as an unbroken temperature ladder around the peak (fill/keep the
            # interior neighbours, never a probability-gapped hole). Gapped
            # baskets are the proven loss source; a too-short run is dropped.
            # peak_cluster-ONLY basket shaping (never touches the peaker baskets):
            #   * CONTIGUOUS: rebuild as an unbroken ladder around the peak.
            #   * PROB_BASED (opt-in): also pull in any window bucket with model
            #     prob >= PEAK_CLUSTER_PROB_MIN that fits the cost cap -- catches a
            #     high-prob non-adjacent winner the contiguous run would skip.
            if getattr(Config, 'PEAK_CLUSTER_CONTIGUOUS_ENABLED', True):
                try:
                    from overlay import cluster_contiguous as _cc
                    cluster_signals = _cc.enforce_all(cluster_signals, market_prices, token_ids)
                except Exception as _e:
                    log.debug(f"cluster contiguity overlay skipped: {_e}")
            if getattr(Config, 'PEAK_CLUSTER_PROB_BASED_ENABLED', False):
                try:
                    from overlay import cluster_probfill as _cp
                    cluster_signals = _cp.augment_all(
                        cluster_signals, bucket_probs, market_prices, token_ids)
                except Exception as _e:
                    log.debug(f"cluster prob-fill overlay skipped: {_e}")
            for sig in cluster_signals:
                self.signals_generated += 1
                # ATOMIC basket (Req-28): a cluster is all-or-none. Filter to
                # placeable legs, enforce the min-leg floor up front (NEVER a
                # 1-leg "cluster"), check the portfolio guard ONCE for the whole
                # basket, then place every leg with basket_leg=True so the per-leg
                # liquidity-thin trim / portfolio guard / below-min checks can't
                # pick the basket apart one leg at a time.
                cl_legs = [lg for lg in sig.legs if lg.token_id]
                # JUNK-LEG PRUNE (added 2026-07-27, peak_cluster ONLY): drop the
                # dead ~1-3c dust legs that never win yet still pad basket cost
                # (the audited -$12 drain). Keep the pricier legs (the ones the
                # market actually rates near the peak). If pruning would fall
                # below the min-leg floor, keep the top-priced legs so a valid
                # ladder still stands. Fail-open: any error leaves legs as-is.
                try:
                    _leg_floor = float(getattr(Config, 'PEAK_CLUSTER_LEG_MIN_PRICE', 0.0) or 0.0)
                except (TypeError, ValueError):
                    _leg_floor = 0.0
                min_legs = int(getattr(Config, 'PEAK_CLUSTER_MIN_LEGS', 3))
                if _leg_floor > 0 and cl_legs:
                    _kept = [lg for lg in cl_legs if float(getattr(lg, 'price', 0.0)) >= _leg_floor]
                    _dropped = len(cl_legs) - len(_kept)
                    if _dropped > 0:
                        if len(_kept) >= min_legs:
                            log.info(f"   \U0001f9fa CLUSTER {city} — pruned {_dropped} junk leg(s) "
                                     f"< ${_leg_floor:.2f} (kept {len(_kept)})")
                            cl_legs = _kept
                        else:
                            # Pruning would break the ladder: keep the top-priced
                            # legs up to the original count so the basket stands.
                            log.info(f"   \U0001f9fa CLUSTER {city} — prune would drop below "
                                     f"{min_legs} legs; keeping top-priced legs")
                            cl_legs = sorted(
                                cl_legs, key=lambda lg: float(getattr(lg, 'price', 0.0)),
                                reverse=True,
                            )[:max(min_legs, len(_kept))]
                if len(cl_legs) < min_legs:
                    log.info(f"   🧺 CLUSTER {city} — only {len(cl_legs)} placeable legs "
                             f"(< {min_legs}) — skip (never a 1-leg cluster)")
                    continue
                cl_total_usd = sum(lg.size_usd for lg in cl_legs)
                ok, why = self._can_deploy(cl_total_usd, count_as_buy=True)
                if not ok:
                    self._funnel['portfolio_guard'] += 1
                    log.info(f"   🏦 GUARD cluster {city} — {why} (basket ${cl_total_usd:.2f})")
                    continue
                # Reserve the next box label for this basket (peek; commit only
                # if at least one leg actually fills).
                box_label = self.pm.peek_cluster_box('peak_cluster')
                log.info(f"   🧺 CLUSTER {city} [{box_label}] | {len(cl_legs)} legs | {sig.reason}")
                placed_legs = []
                for leg in cl_legs:
                    pos = self._place(
                        token_id=leg.token_id,
                        condition_id=condition_ids.get(leg.bucket_label, ''),
                        entry_price=leg.price,
                        base_size_usd=leg.size_usd,
                        market_title=market.title,
                        bucket_label=leg.bucket_label,
                        strategy='peak_cluster',
                        city=city,
                        slug=market.slug,
                        resolution_time=market.resolution_time,
                        edge=sig.combined_prob - sig.total_cost,
                        grade=grade,
                        hold_hint=True,          # basket pays off at resolution (NEVER stop/trail)
                        early_exit_price=early_exit_price,
                        apply_grade_size=False,  # cluster sizes its own legs (equal-share basket math)
                        reason=sig.reason,
                        signal='peak_cluster',
                        our_prob=getattr(leg, 'prob', 0.0),
                        use_factor_kelly=False,  # keep the basket's equal-share legs intact
                        use_ml=False,            # the basket is ONE unit; don't veto single legs
                        cluster_box=box_label,
                        count_as_buy=False,      # whole basket counts as ONE buy (below)
                        basket_leg=True,         # ATOMIC: no per-leg trim/guard/below-min drop
                    )
                    if pos:
                        self.trades_placed += 1
                        placed_legs.append(pos)
                if placed_legs:
                    # Consume the box number and tag every placed leg with it.
                    committed = self.pm.commit_cluster_box('peak_cluster')
                    for lp in placed_legs:
                        lp.cluster_box = committed
                    # Whole basket = ONE buy toward the per-scan budget.
                    self._scan_buys += 1
                    try:
                        self.pm._save_state()
                    except Exception:
                        pass
                    # ONE grouped Telegram alert for the whole basket (no per-leg spam).
                    try:
                        self.telegram.notify_cluster(
                            committed, city, market.title, placed_legs,
                            sig.total_cost, sig.combined_prob, sig.expected_roi_pct,
                        )
                    except Exception as e:
                        log.debug(f"cluster notify failed: {e}")

        # ------------------------------------------------------
        # PEAKER (Req-28 MARKET-ANCHORED) — the market itself prices the winning
        # bucket (a ~>=60c favourite implies ~60% win / ~40% upside). PEAKER
        # anchors on that high-probability favourite, CROSS-VALIDATES it with our
        # model, and only buys on CONFIRMATION. A bare-favourite SOLO buy is
        # ~breakeven (why peaker kept losing), so the EDGE is the cool/warm
        # BASKET:
        #   • peaker             — 1-leg solo, only on a genuine confirmed edge
        #   • peaker_cool_basket — model peak == market favourite AND cooling →
        #                          add the -1°C neighbour; if peak + (-1°C)
        #                          combined cost < 95¢ buy BOTH, grouped as ONE
        #   • peaker_warm_basket — same but warming → +1°C neighbour
        # A basket is HELD to resolution and grouped (shared cluster box) so the
        # UI/PM render + resolve it as ONE "peaker cool/warm basket" — ONE
        # Telegram alert, one status group, in status + analysis. Placed
        # ATOMICALLY (all-or-none). Forecast-based → skipped once locked.
        # ------------------------------------------------------
        if getattr(Config, 'PEAKER_ENABLED', True) and (
            not in_lock_window or getattr(Config, 'PEAKER_TRADE_DECIDED', False)
        ):
            try:
                peaker_signals = self.peaker.evaluate(
                    market.title, bucket_probs, market_prices, token_ids, balance,
                    city=city, stability=stab, grade=grade,
                )
            except Exception as e:
                peaker_signals = []
                log.debug(f"peaker eval failed {city}: {e}")
            for sig in peaker_signals:
                self.signals_generated += 1
                is_basket = bool(getattr(sig, 'is_basket', sig.sub_strategy != 'peaker'))
                disp_label = getattr(sig, 'display_label', None) or sig.sub_strategy.replace('_', ' ')
                log.info(
                    f"   🛡️  PEAKER {city} | {disp_label} | {sig.direction} | "
                    f"peak={sig.forecast_max_c:.1f}°C conf={sig.confidence:.0%} | "
                    f"{len(sig.legs)} legs | {sig.reason}"
                )
                pk_legs = [lg for lg in sig.legs if lg.token_id]
                if not pk_legs:
                    continue
                # ATOMIC basket: a peaker basket is all-or-none — guard ONCE on
                # the whole basket cost and place legs with basket_leg=True so the
                # per-leg thin/guard/below-min checks can't trim it to one leg.
                pk_total_usd = sum(lg.size_usd for lg in pk_legs)
                ok, why = self._can_deploy(pk_total_usd, count_as_buy=True)
                if not ok:
                    self._funnel['portfolio_guard'] += 1
                    log.info(f"   🏦 GUARD peaker {city} — {why} (basket ${pk_total_usd:.2f})")
                    continue
                # A multi-leg basket groups under a shared cluster box so the UI
                # and PM render/resolve it as ONE unit (same as peak_cluster).
                box_label = self.pm.peek_cluster_box(sig.sub_strategy) if is_basket else ''
                pk_edge = sig.combined_prob - sig.total_cost
                placed_peaker = []
                for leg in pk_legs:
                    pos = self._place(
                        token_id=leg.token_id,
                        condition_id=condition_ids.get(leg.bucket_label, ''),
                        entry_price=leg.market_price,
                        base_size_usd=leg.size_usd,
                        market_title=market.title,
                        bucket_label=leg.bucket_label,
                        strategy=sig.sub_strategy,   # peaker / peaker_cool_basket / peaker_warm_basket
                        city=city,
                        slug=market.slug,
                        resolution_time=market.resolution_time,
                        edge=pk_edge,
                        grade=grade,
                        hold_hint=True,          # any-one-wins basket pays off at resolution (NEVER stop/trail)
                        early_exit_price=early_exit_price,
                        apply_grade_size=False,  # peaker sizes its own equal-share legs
                        reason=sig.reason,
                        signal=sig.sub_strategy,
                        our_prob=getattr(leg, 'our_probability', 0.0),
                        use_factor_kelly=False,  # keep the equal-share basket legs intact
                        use_ml=False,            # the basket is ONE unit; don't veto single legs
                        cluster_box=box_label,
                        count_as_buy=False,      # whole basket counts as ONE buy (below)
                        basket_leg=True,         # ATOMIC: no per-leg trim/guard/below-min drop
                    )
                    if pos:
                        self.trades_placed += 1
                        placed_peaker.append(pos)
                if placed_peaker:
                    # The peaker signal = ONE buy toward the per-scan budget.
                    self._scan_buys += 1
                    pk_roi = getattr(sig, 'expected_roi_pct', None)
                    if pk_roi is None:
                        pk_roi = ((sig.combined_prob / sig.total_cost - 1.0) * 100.0) if sig.total_cost else 0.0
                    if is_basket:
                        # Group as ONE unit: tag every leg with the box, persist,
                        # and send ONE grouped Telegram alert labelled e.g.
                        # "peaker cool basket".
                        committed = self.pm.commit_cluster_box(sig.sub_strategy)
                        for lp in placed_peaker:
                            lp.cluster_box = committed
                        try:
                            self.pm._save_state()
                        except Exception:
                            pass
                        try:
                            self.telegram.notify_cluster(
                                committed, city, market.title, placed_peaker,
                                sig.total_cost, sig.combined_prob, pk_roi,
                                group_label=disp_label,
                            )
                        except Exception as e:
                            log.debug(f"peaker basket notify failed: {e}")
                    else:
                        # Solo peaker leg — single trade alert.
                        for lp in placed_peaker:
                            self.telegram.notify_trade(
                                'BUY', lp.bucket_label, lp.entry_price,
                                lp.cost_usd, lp.shares, sig.sub_strategy,
                                edge=pk_edge, city=city,
                            )

        # ------------------------------------------------------
        # CONFIDENT — simple peak-only fallback (DEMOTED, opt-in). Only fires
        # when explicitly enabled as a second opinion on peak-only bets. Skipped
        # in the lock window (no forecast edge once the extreme is recorded).
        # ------------------------------------------------------
        if Config.CONFIDENT_ENABLED and (
            not in_lock_window or getattr(Config, 'CONFIDENT_TRADE_DECIDED', False)
        ):
            confident_signals = self.confident.evaluate(
                market.title, bucket_probs, market_prices, token_ids, balance,
            )
            for signal in confident_signals[:1]:
                # Skip if another peak strategy already bought this bucket (dedup).
                self.signals_generated += 1
                log.info(
                    f"   💎 CONFIDENT: {city} | {signal.bucket_label[:25]} @ "
                    f"${signal.market_price:.3f} | P={signal.our_probability:.0%} | "
                    f"Edge={signal.edge:.0%}"
                )
                pos = self._place(
                    token_id=signal.token_id,
                    condition_id=condition_ids.get(signal.bucket_label, ''),
                    entry_price=signal.market_price,
                    base_size_usd=signal.size_usd,
                    market_title=market.title,
                    bucket_label=signal.bucket_label,
                    strategy='confident',
                    city=city,
                    slug=market.slug,
                    resolution_time=market.resolution_time,
                    edge=signal.edge,
                    grade=grade, hold_hint=True, early_exit_price=early_exit_price,
                    reason=f"P={signal.our_probability:.0%} edge={signal.edge:.0%}",
                    signal='confident',
                )
                if pos:
                    self.trades_placed += 1
                    self.telegram.notify_trade(
                        'BUY', signal.bucket_label, pos.entry_price,
                        pos.cost_usd, pos.shares, 'confident',
                        edge=signal.edge, city=city,
                    )

    def _check_resolutions(self):
        """Check if any positions resolved, redeem winners."""
        self.pm.check_resolutions()
        redeemed = self.pm.redeem_all_winning()
        if redeemed > 0:
            log.info(f"💰 Redeemed {redeemed} winning positions")
            self.telegram.send(f"💰 Redeemed {redeemed} winning positions!")

    def _print_dashboard(self):
        """Print comprehensive dashboard."""
        stats = self.pm.get_stats()
        open_pos = self.pm.get_open_positions()
        pending = self.pm.get_pending_orders() if hasattr(self.pm, 'get_pending_orders') else []

        log.info(f"\n{'━'*60}")
        log.info(f"  🌡️  WEATHER SNIPER DASHBOARD")
        log.info(f"{'━'*60}")
        log.info(f"  Mode:        {stats['mode']}")
        log.info(f"  Balance:     ${stats['balance']:.2f}")
        log.info(f"  Portfolio:   ${stats['portfolio_value']:.2f}")
        log.info(f"  Total PnL:   ${stats['total_pnl']:+.2f} ({stats['roi_pct']:+.1f}%)")
        log.info(f"{'─'*60}")
        log.info(f"  Trades:      {stats['total_trades']}")
        log.info(f"  Win Rate:    {stats['win_rate']:.0f}% ({stats['wins']}W / {stats['losses']}L)")
        log.info(f"  Open:        {len(open_pos)} filled + {len(pending)} pending")
        log.info(f"  Redeemed:    ${stats['total_redeemed']:.2f}")
        log.info(f"  Signals:     {self.signals_generated} generated")
        ml_status = self.ml.get_status()
        if ml_status['enabled']:
            log.info(f"  ML Engine:   {ml_status['model']} ({ml_status['tokens_used']} tokens)")
        if self.resolution_verifier is not None:
            vs = self.resolution_verifier.get_status()
            if vs.get('enabled'):
                log.info(f"  Station LLM: {vs['model']} ({vs['tokens_used']} tokens, {vs['cache_size']} cached)")
        log.info(f"  Contexts:    {stats.get('active_contexts', 0)} active markets")
        log.info(f"{'─'*60}")

        # -- SCAN FUNNEL (this cycle) --------------------------------------
        # The single most useful debug line: every signal either becomes a
        # placed trade or dies at a known gate. If trades==0 this shows EXACTLY
        # which gate ate them (price floor / liquidity / duplicate / no-data),
        # so we never again have to guess "why didn't it trade".
        f = self._funnel
        if f:
            placed = f.get('placed', 0)
            log.info(f"  🔎 SCAN FUNNEL (this cycle):")
            log.info(f"     ✅ placed={placed}   ⛔ add-skip dup/min/bal={f.get('add_reject', 0)}")
            log.info(f"     ⏭️  price_floor={f.get('price_floor', 0)}  dust={f.get('dust', 0)}  "
                     f"grade_skip={f.get('grade_skip', 0)}  liq_skip={f.get('liq_skip', 0)}")
            log.info(f"     🏦 portfolio_guard={f.get('portfolio_guard', 0)}  🤖 ml_veto={f.get('ml_veto', 0)}  "
                     f"🎚️ kelly_zero={f.get('kelly_zero', 0)}  below_min={f.get('below_min', 0)}")
            log.info(f"     💧 liq_thin_hold={f.get('liq_thin', 0)}  liq_nobook_hold={f.get('liq_nobook', 0)}")
            log.info(f"     🔓 lock_window={f.get('lock_window', 0)}  ⛔ over={f.get('over', 0)}  "
                     f"🌡️  primary_signal={f.get('primary_signal', 0)}  🌙 primary_no_data={f.get('primary_no_data', 0)}")
            log.info(f"     ⚠️  no_forecast={f.get('no_forecast', 0)}  no_coords={f.get('no_coords', 0)}")
        deployed = stats['portfolio_value'] - stats['balance']
        log.info(f"  💰 deployed ${deployed:.2f} across {len(open_pos)} pos | free ${stats['balance']:.2f} "
                 f"| this scan: {self._scan_buys} buys / ${self._scan_deployed_usd:.2f}")
        log.info(f"{'��'*60}")

        if open_pos:
            log.info(f"  FILLED POSITIONS:")
            for p in open_pos[:10]:
                pnl_e = '+' if p.unrealized_pnl >= 0 else ''
                lock = ' 🔒' if getattr(p, 'preclose_locked', False) else ''
                stale = ' ~stale' if getattr(p, 'current_price_stale', False) else ''
                box = f" [{p.cluster_box}]" if getattr(p, 'cluster_box', '') else ''
                log.info(
                    f"    {pnl_e} {p.city:12} {p.bucket_label[:30]:30} "
                    f"${p.entry_price:.4f}->${p.current_price:.4f} "
                    f"({p.roi_pct:+.0f}%){box}{lock}{stale}"
                )

        if pending:
            log.info(f"  PENDING (in orderbook, awaiting fill):")
            for p in pending[:5]:
                log.info(
                    f"    ... {p.city:12} {p.bucket_label[:30]:30} "
                    f"${p.entry_price:.4f} {p.shares:.0f}sh"
                )

        log.info(f"{'━'*60}\n")

    def print_status(self):
        """Print status and exit."""
        Config.print_status()
        self._print_dashboard()

    def run_loop(self):
        """Main trading loop — Railway/cloud ready with graceful shutdown."""
        import signal

        Config.print_status()
        log.info("Weather Sniper Bot starting...")
        log.info(f"   Scan interval: {Config.SCAN_INTERVAL_SECONDS}s")
        log.info(f"   Days ahead: {Config.SCAN_DAYS_AHEAD}")
        strats = []
        if Config.LATE_OBSERVED_ENABLED: strats.append('LateObserved*')
        if getattr(Config, 'QUICK_FLIP_ENABLED', False): strats.append('QuickFlip')
        if getattr(Config, 'PEAK_CLUSTER_ENABLED', True): strats.append('PeakCluster')
        if getattr(Config, 'PEAKER_ENABLED', True): strats.append('Peaker')
        if Config.CONFIDENT_ENABLED: strats.append('Confident')
        if Config.SNIPER_ENABLED: strats.append('Sniper')
        if Config.SPREAD_ENABLED: strats.append('Spread')
        log.info(f"   Strategies: {', '.join(strats) if strats else 'none'}")
        log.info(f"   Telegram: {'ON' if self.telegram.enabled else 'OFF'}")
        ml_status = self.ml.get_status()
        log.info(f"   ML: {ml_status.get('model','?')} (local: {ml_status.get('local_model','?')})")
        log.info("")

        self.telegram.start_polling()
        # Req-28: do NOT auto-start trading on deploy. Announce readiness + the
        # three controls; trading begins only when the user presses Start / types
        # 'start'. The startup card + [Start Trading][Settings][Restart] keyboard
        # is sent by the Telegram bot itself.
        try:
            self.telegram.send_startup_ready()
        except Exception as e:
            log.debug(f"startup-ready notify failed: {e}")

        if not Config.is_paper():
            self.pm.recover_positions_on_start()

        shutdown_flag = [False]
        force_count = [0]

        def _handle_shutdown(signum, frame):
            force_count[0] += 1
            if force_count[0] >= 2:
                # Second Ctrl+C = force exit immediately
                log.info("Force quit (2nd interrupt). Exiting now.")
                os._exit(0)
            log.info("Stopping... (press Ctrl+C again to force quit)")
            shutdown_flag[0] = True
            # Raise KeyboardInterrupt to break out of any blocking call / sleep
            raise KeyboardInterrupt()

        # SIGTERM (Railway) uses flag-only; SIGINT (Ctrl+C) raises to break immediately
        try:
            signal.signal(signal.SIGTERM, lambda s, f: shutdown_flag.__setitem__(0, True))
        except (ValueError, OSError, AttributeError):
            pass
        try:
            signal.signal(signal.SIGINT, _handle_shutdown)
        except (ValueError, OSError, AttributeError):
            pass

        scan_start = time.time()
        try:
            while not shutdown_flag[0]:
                try:
                    self.run_once()
                except KeyboardInterrupt:
                    break
                except Exception as e:
                    log.error(f"Scan error: {e}")

                if shutdown_flag[0]:
                    break

                elapsed = time.time() - scan_start
                sleep_time = max(1, Config.SCAN_INTERVAL_SECONDS - elapsed)
                try:
                    for _ in range(int(sleep_time)):
                        if shutdown_flag[0]:
                            break
                        time.sleep(1)
                except KeyboardInterrupt:
                    break
                scan_start = time.time()
        except KeyboardInterrupt:
            pass
        finally:
            log.info("Shutting down — saving state...")
            try:
                self.telegram.send("Bot shutting down.")
                self.telegram.stop_polling()
            except Exception:
                pass
            self.pm._save_state()
            log.info("State saved. Goodbye!")


def main():
    parser = argparse.ArgumentParser(description='Weather Sniper Bot')
    parser.add_argument('--live', action='store_true', help='Enable live trading')
    parser.add_argument('--paper', action='store_true', help='Paper/dry-run mode (default)')
    parser.add_argument('--once', action='store_true', help='Run single scan then exit')
    parser.add_argument('--status', action='store_true', help='Print status and exit')
    parser.add_argument('--balance', type=float, help='Override starting balance')
    parser.add_argument('--days', type=int, default=3, help='Days ahead to scan')
    args = parser.parse_args()

    if args.live:
        Config.TRADING_MODE = 'live'
    if args.balance:
        Config.STARTING_BALANCE = args.balance
    if args.days:
        Config.SCAN_DAYS_AHEAD = args.days

    bot = WeatherBot()

    if args.status:
        bot.print_status()
    elif args.once:
        bot.run_once()
        bot._print_dashboard()
    else:
        bot.run_loop()


if __name__ == '__main__':
    main()
