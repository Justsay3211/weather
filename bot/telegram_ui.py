"""
Telegram Bot Integration — Notifications + Commands.

Features:
- Send trade alerts (entry, exit, win, loss)
- Detailed redemption alerts (full market, entry/exit, profit/PnL)
- ONE grouped "Peak Cluster Box N" alert per basket (not one per leg)
- Send daily summary
- Commands: /status, /positions, /balance, /pnl, /markets, /stop
- Paginated + sortable positions view (10 per page), peak-cluster legs grouped
- Non-blocking (runs in background thread)
"""

import os
import csv
import json
import html
import time
import logging
import threading
import requests
from collections import deque
from typing import Optional, Dict, List
from datetime import datetime, timezone

from config import Config
from logger import log


class TelegramBot:
    """Telegram bot for notifications and commands."""

    PAGE_SIZE = 10
    _SORT_NAMES = {
        'pnl': 'Top PnL', 'loss': 'Biggest losers',
        'roi': 'Top ROI', 'recent': 'Most recent',
    }

    def __init__(self, position_manager=None, scanner=None):
        self.token = Config.TELEGRAM_BOT_TOKEN
        self.chat_id = Config.TELEGRAM_CHAT_ID
        self.enabled = bool(self.token and self.chat_id)
        self.pm = position_manager
        self.scanner = scanner
        self._session = requests.Session()
        self._poll_thread: Optional[threading.Thread] = None
        self._running = False
        self._last_update_id = 0
        # Optional dashboard hook: restart_fresh(starting_balance=None) clears
        # ALL positions and resets the paper balance for a fresh start. Set by
        # the dashboard; the inline Restart button / /restart invoke it.
        self._on_restart = None
        self._restart_pending = False
        # Req-29 settings UX: capture typed input (e.g. a new starting balance),
        # and log human-readable changes so the OK button can summarise them.
        self._awaiting = None          # None | 'balance' | 'ml_url' | 'recover_upload'
        self._ml_wiz = {}              # transient /mlsetup wizard state
        self._session_changes = []     # ["STARTING_BALANCE = 300", ...]
        # Req-29 mlanalysis: optional ML engine handle (set by the dashboard via
        # attach_ml); None keeps mlanalysis on its heuristic fallback.
        self.ml = None
        # Req-29 ai-summary: capture WARNING+ log lines into a ring buffer so
        # /aisummary can surface recent runtime errors for sharing.
        self._error_log = deque(maxlen=300)
        self._install_error_capture()
        # Seed with already-redeemed ids so a restart doesn't re-announce the
        # whole backlog — only NEW redemptions after startup are sent.
        self._announced_redeemed = set(
            p.id for p in self.pm.positions if p.status == 'redeemed'
        ) if self.pm else set()

        if not self.enabled:
            log.info("Telegram: disabled (no token/chat_id set)")
        else:
            log.info(f"Telegram: enabled → chat {self.chat_id}")

    @property
    def base_url(self):
        return "https" + "://api.telegram.org/bot" + str(self.token)

    @staticmethod
    def _esc(s) -> str:
        """HTML-escape dynamic text so market names with &/</> don't break parse."""
        return html.escape(str(s if s is not None else ''))

    # ==============================================================
    # SEND MESSAGES
    # ==============================================================

    def send(self, text: str, parse_mode: str = 'HTML', reply_markup: dict = None) -> bool:
        """Send a message to the configured chat (optionally with an inline keyboard)."""
        if not self.enabled:
            return False
        # Intercept the legacy blind "Redeemed N winning positions!" message and
        # replace it with the detailed per-position breakdown (full market, entry/
        # exit, cost/payout, PnL). The detailed header starts with "<b>REDEEMED",
        # so it never matches this guard and there is no recursion.
        stripped = text.strip()
        if stripped.startswith('\U0001F4B0 Redeemed ') and stripped.endswith('positions!'):
            self.notify_redeems_recent()
            return True
        try:
            payload = {
                'chat_id': self.chat_id,
                'text': text,
                'parse_mode': parse_mode,
                'disable_web_page_preview': True,
            }
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            resp = self._session.post(
                f"{self.base_url}/sendMessage", json=payload, timeout=10,
            )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Telegram send failed: {e}")
            return False

    def _edit(self, message_id: int, text: str, reply_markup: dict = None) -> bool:
        """Edit an existing message (used to refresh panels in place)."""
        if not self.enabled:
            return False
        try:
            payload = {
                'chat_id': self.chat_id, 'message_id': message_id,
                'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True,
            }
            if reply_markup is not None:
                payload['reply_markup'] = reply_markup
            r = self._session.post(f"{self.base_url}/editMessageText", json=payload, timeout=10)
            return r.status_code == 200
        except Exception as e:
            log.debug(f"Telegram edit failed: {e}")
            return False

    def _answer_callback(self, callback_id: str, text: str = ''):
        try:
            self._session.post(f"{self.base_url}/answerCallbackQuery",
                               json={'callback_query_id': callback_id, 'text': text},
                               timeout=10)
        except Exception:
            pass

    def _delete_message(self, message_id):
        """Best-effort delete of a chat message (used to scrub a pasted ML key)."""
        if not self.enabled or message_id is None:
            return False
        try:
            r = self._session.post(self.base_url + '/deleteMessage',
                                   json={'chat_id': self.chat_id, 'message_id': message_id},
                                   timeout=10)
            return r.status_code == 200
        except Exception:
            return False

    def _install_error_capture(self):
        """Attach a handler to the root logger that records WARNING+ lines into
        an in-memory ring buffer (for /aisummary). Idempotent + defensive."""
        try:
            buf = self._error_log

            class _RingHandler(logging.Handler):
                def emit(self, record):
                    try:
                        if record.levelno >= logging.WARNING:
                            ts = datetime.now(timezone.utc).strftime('%m-%d %H:%M:%S')
                            buf.append(
                                f"{ts} {record.levelname} "
                                f"{record.name}: {record.getMessage()}"
                            )
                    except Exception:
                        pass

            root = logging.getLogger()
            if not any(getattr(h, '_wp_ring', False) for h in root.handlers):
                h = _RingHandler()
                h._wp_ring = True
                h.setLevel(logging.WARNING)
                root.addHandler(h)
                if root.level == 0 or root.level > logging.WARNING:
                    root.setLevel(logging.WARNING)
        except Exception:
            pass

    def attach_ml(self, ml):
        """Wire the ML decision engine so /mlanalysis can use it for a narrative."""
        self.ml = ml

    # ==============================================================
    # LIFECYCLE (startup ready / start / restart fresh)
    # ==============================================================

    def _main_keyboard(self) -> dict:
        """Inline keyboard shown on startup: Start / Settings / Restart."""
        return {'inline_keyboard': [[
            {'text': '▶️ Start Trading', 'callback_data': 'act:start'},
            {'text': '⚙️ Settings', 'callback_data': 'act:settings'},
            {'text': '♻️ Restart', 'callback_data': 'act:restart'},
        ]]}

    def send_startup_ready(self):
        """Announce a successful deploy/boot WITHOUT auto-trading and show the
        Start / Settings / Restart inline keyboard. Trading begins only when the
        user taps Start Trading (or sends /start, or types 'start')."""
        try:
            from bot import settings_store
            bools, _nums = settings_store.snapshot()
        except Exception:
            bools = {}
        try:
            bal = self.pm.get_balance() if self.pm else 0.0
        except Exception:
            bal = 0.0
        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        trading = '🟢 ON' if bools.get('TRADING_ENABLED') else '🔴 OFF (tap Start Trading)'
        msg = (
            f"✅ <b>Bot initialized successfully</b>\n"
            f"{mode} | starting balance ${bal:.2f}\n"
            f"Trading: <b>{trading}</b>\n\n"
            f"▶️ <b>Start Trading</b> — begin placing trades (or send /start)\n"
            f"⚙️ <b>Settings</b> — strategies, gates & starting balance\n"
            f"♻️ <b>Restart</b> — clear ALL positions & start fresh\n"
        )
        self.send(msg, reply_markup=self._main_keyboard())

    def _prompt_restart(self):
        """Ask for confirmation before the destructive restart-fresh action."""
        self._restart_pending = True
        kb = {'inline_keyboard': [[
            {'text': '✅ Yes, clear all & restart', 'callback_data': 'act:restart_confirm'},
            {'text': '✖️ Cancel', 'callback_data': 'act:restart_cancel'},
        ]]}
        self.send(
            "♻️ <b>Restart fresh?</b>\n"
            "This CLOSES/clears ALL positions and resets the paper balance to "
            "the configured starting balance. This cannot be undone.",
            reply_markup=kb,
        )

    def _do_restart(self):
        """Invoke the dashboard restart hook (clear all positions + reset balance)."""
        self._restart_pending = False
        if not self._on_restart:
            self.send("⚠️ Restart hook not wired — cannot restart from here.")
            return
        try:
            self._on_restart()
            try:
                bal = self.pm.get_balance() if self.pm else 0.0
            except Exception:
                bal = 0.0
            self.send(
                f"♻️ <b>Restarted fresh</b> — all positions cleared, "
                f"balance reset to ${bal:.2f}. Tap Start Trading to begin.",
                reply_markup=self._main_keyboard(),
            )
        except Exception as e:
            log.debug(f"restart failed: {e}")
            self.send("⚠️ Restart failed — see logs.")

    def notify_trade(self, side: str, bucket_label: str, price: float,
                     size_usd: float, shares: float, strategy: str,
                     edge: float = 0, city: str = ''):
        """Send trade notification."""
        emoji = '🟢' if side == 'BUY' else '🔴'
        msg = (
            f"{emoji} <b>{side}</b> — {self._esc(strategy.upper())}\n"
            f"📍 {self._esc(city)} | {self._esc(bucket_label)}\n"
            f"💰 ${price:.4f} × {shares:.0f} = ${size_usd:.2f}\n"
        )
        if edge > 0:
            msg += f"📊 Edge: {edge:.1%}\n"
        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        msg += f"\n{mode}"
        self.send(msg)

    def notify_cluster(self, box_label: str, city: str, market_title: str,
                       legs: List, total_cost: float, combined_prob: float,
                       roi_pct: float, group_label: str = None):
        """ONE grouped alert for a whole peak-cluster basket.

        Replaces the old behaviour of firing a separate notify_trade per leg
        (6 buckets => 6 messages). Now a single "🧺 PEAK CLUSTER Box N" message
        lists every bucket bought, the combined basket cost, and the any-one-
        wins ROI. `legs` is the list of placed TrackedPositions in the basket.
        """
        try:
            n = len(legs)
            total_cost_usd = sum(getattr(l, 'cost_usd', 0.0) or 0.0 for l in legs)
            title = (group_label or 'PEAK CLUSTER').upper()
            head = (
                f"🧺 <b>{self._esc(title)} {self._esc(box_label)}</b> — {n} bucket{'s' if n != 1 else ''}\n"
                f"📍 {self._esc(city)} | {self._esc((market_title or '')[:60])}\n"
            )
            lines = []
            for l in legs:
                lines.append(
                    f"   • {self._esc(getattr(l, 'bucket_label', ''))} "
                    f"@ ${getattr(l, 'entry_price', 0.0):.3f} × "
                    f"{getattr(l, 'shares', 0.0):.0f} = ${getattr(l, 'cost_usd', 0.0):.2f}\n"
                )
            foot = (
                f"💰 basket cost ${total_cost_usd:.2f} "
                f"(per-share ${total_cost:.3f}) | P(any)~{combined_prob:.0%}\n"
                f"🎯 ROI ~{roi_pct:.0f}% if ANY bucket wins | holds → resolution "
                f"(never stop-lossed)\n"
            )
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            self.send(head + ''.join(lines) + foot + f"\n{mode}")
        except Exception as e:
            log.debug(f"notify_cluster failed: {e}")

    def notify_cluster_resolution(self, box_label: str, legs: List):
        """ONE grouped resolution summary for a peak-cluster basket once EVERY
        leg has settled. Shows which bucket WON and the amount it won, plus the
        losing buckets and their loss, and the net basket PnL. Replaces the
        per-leg won/lost spam for cluster baskets.

        `legs` is the list of resolved TrackedPositions in the basket (fed by
        PositionManager._maybe_notify_cluster_close once none are open/pending).
        """
        try:
            if not legs:
                return
            city = self._esc(getattr(legs[0], 'city', ''))
            market_title = self._esc((getattr(legs[0], 'market_title', '') or '')[:60])
            winners = [l for l in legs if getattr(l, 'status', '') in ('won', 'redeemed')]
            losers = [l for l in legs if getattr(l, 'status', '') == 'lost']
            others = [l for l in legs if l not in winners and l not in losers]
            net = sum(getattr(l, 'pnl', 0.0) or 0.0 for l in legs)
            cost = sum(getattr(l, 'cost_usd', 0.0) or 0.0 for l in legs)
            ret = cost + net
            head_emoji = '✅' if net >= 0 else '🔴'
            head = (
                f"{head_emoji} 🧺 <b>PEAK CLUSTER {self._esc(box_label)} RESOLVED</b>\n"
                f"📍 {city} | {market_title}\n"
            )
            lines = []
            if winners:
                for l in winners:
                    payout = (getattr(l, 'shares', 0.0) or 0.0) * 1.0
                    lines.append(
                        f"   ✅ WON {self._esc(getattr(l, 'bucket_label', ''))} "
                        f"→ ${getattr(l, 'pnl', 0.0):+.2f} "
                        f"(entry ${getattr(l, 'entry_price', 0.0):.3f} × "
                        f"{getattr(l, 'shares', 0.0):.0f}sh → payout ${payout:.2f})\n"
                    )
            else:
                lines.append("   ⚠️ No winning bucket in this basket.\n")
            for l in losers:
                lines.append(
                    f"   ❌ {self._esc(getattr(l, 'bucket_label', ''))} "
                    f"→ ${getattr(l, 'pnl', 0.0):+.2f} "
                    f"(cost ${getattr(l, 'cost_usd', 0.0):.2f} lost)\n"
                )
            for l in others:
                lines.append(
                    f"   • {self._esc(getattr(l, 'bucket_label', ''))} "
                    f"→ ${getattr(l, 'pnl', 0.0):+.2f} ({self._esc(getattr(l, 'status', ''))})\n"
                )
            foot = (
                f"💰 <b>Basket net PnL ${net:+.2f}</b> "
                f"(cost ${cost:.2f} → return ${ret:.2f})\n"
            )
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            self.send(head + ''.join(lines) + foot + f"\n{mode}")
        except Exception as e:
            log.debug(f"notify_cluster_resolution failed: {e}")

    def notify_resolution(self, won: bool, bucket_label: str, pnl: float, city: str = ''):
        """Send simple resolution notification (kept for compatibility)."""
        emoji = '✅' if won else '❌'
        result = 'WON' if won else 'LOST'
        msg = (
            f"{emoji} <b>RESOLVED: {result}</b>\n"
            f"📍 {self._esc(city)} | {self._esc(bucket_label)}\n"
            f"💰 PnL: ${pnl:+.2f}\n"
        )
        self.send(msg)

    def notify_close(self, pos):
        """Send a close/resolution alert for ANY closed position — stop-loss,
        take-profit, trailing-stop, flip/thesis exit, or won/lost resolution.

        Wired via PositionManager._notify_close (risk-trigger & resolution
        closes) and called directly by the dashboard for flip/thesis exits
        (whose reason is relabeled 'manual' after close, so the PM hook skips
        them to avoid a double-notify). Fully defensive — never raises."""
        try:
            reason = getattr(pos, 'exit_reason', '') or ''
            status = getattr(pos, 'status', '') or ''
            pnl = getattr(pos, 'pnl', 0.0) or 0.0
            roi = getattr(pos, 'roi_pct', 0.0) or 0.0
            if status == 'won':
                head = '✅ <b>RESOLVED WON</b>'
            elif status == 'lost':
                head = '❌ <b>RESOLVED LOST</b>'
            else:
                head = {
                    'take_profit': '🎯 <b>TAKE PROFIT</b>',
                    'stop_loss': '🛑 <b>STOP LOSS</b>',
                    'trailing_stop': '📉 <b>TRAILING STOP</b>',
                    'flip_timeout': '⏲️ <b>FLIP book-or-cut</b>',
                    'thesis_invalidated': '🚫 <b>THESIS EXIT</b>',
                    'manual': '🔴 <b>SOLD</b>',
                }.get(reason, '🔴 <b>SOLD</b>')
            entry = getattr(pos, 'entry_price', 0.0) or 0.0
            exit_px = getattr(pos, 'exit_price', None)
            if exit_px is None:
                exit_px = getattr(pos, 'current_price', 0.0) or 0.0
            shares = getattr(pos, 'shares', 0.0) or 0.0
            name = self._esc(getattr(pos, 'bucket_label', '') or getattr(pos, 'market_title', ''))
            mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
            msg = (
                f"{head} — {self._esc(getattr(pos, 'strategy', ''))}\n"
                f"📍 {self._esc(getattr(pos, 'city', ''))} | {name}\n"
                f"💵 entry ${entry:.4f} → exit ${exit_px:.4f} | {shares:.0f}sh\n"
                f"📊 PnL ${pnl:+.2f} ({roi:+.0f}%)\n"
                f"{mode}"
            )
            self.send(msg)
        except Exception as e:
            log.debug(f"notify_close failed: {e}")

    def notify_redeems_recent(self):
        """Find positions that have newly become 'redeemed' since the last call
        and announce them in full detail. Self-discovers from the position
        manager so the dashboard doesn't need to pass anything."""
        if not self.pm:
            return
        fresh = [p for p in self.pm.positions
                 if p.status == 'redeemed' and p.id not in self._announced_redeemed]
        for p in fresh:
            self._announced_redeemed.add(p.id)
        if fresh:
            self.notify_redeems(fresh)

    def notify_redeems(self, positions: List):
        """Detailed redemption notification — one block per redeemed position with
        the full market name, entry/exit price, cost/payout and realized PnL."""
        if not positions:
            return
        payout_total = sum(p.shares * 1.0 for p in positions)
        pnl_total = sum(p.pnl for p in positions)
        n = len(positions)
        header = (
            f"💰 <b>REDEEMED {n} winning position{'s' if n != 1 else ''}</b>\n"
            f"   payout +${payout_total:.2f} | realized PnL ${pnl_total:+.2f}\n"
        )
        blocks = []
        for p in positions:
            exit_px = p.exit_price if p.exit_price is not None else 1.0
            payout = p.shares * 1.0
            name = self._esc(p.bucket_label or p.market_title)
            blocks.append(
                f"\n✅ <b>{self._esc(p.city)}</b>  ({self._esc(p.strategy)})\n"
                f"   {name}\n"
                f"   entry ${p.entry_price:.4f} → exit ${exit_px:.4f} | {p.shares:.0f}sh\n"
                f"   cost ${p.cost_usd:.2f} → payout ${payout:.2f} | "
                f"PnL ${p.pnl:+.2f} ({p.roi_pct:+.0f}%)\n"
            )
        # Respect Telegram's ~4096-char message cap — chunk if necessary.
        msg = header
        for b in blocks:
            if len(msg) + len(b) > 3900:
                self.send(msg)
                msg = ''
            msg += b
        if msg:
            self.send(msg)

    def notify_redeem(self, bucket_label: str, amount: float):
        """Legacy single-redeem notification (kept for compatibility)."""
        msg = (
            f"💰 <b>REDEEMED</b>\n"
            f"📍 {self._esc(bucket_label)}\n"
            f"💵 +${amount:.2f}\n"
        )
        self.send(msg)

    # ==============================================================
    # POSITIONS VIEW (paginated + sortable, peak-cluster legs GROUPED)
    # ==============================================================

    def _open_units(self, sort_key: str) -> List[dict]:
        """Group open positions into display UNITS so a peak-cluster basket shows
        as ONE entry ("Box N" + all its legs) instead of N separate rows.

        Each unit: {kind, box, positions, pnl, roi, recent}. Non-cluster
        positions are single-position units. Units are sorted as a whole.
        """
        open_pos = self.pm.get_open_positions() if self.pm else []
        clusters: Dict[str, list] = {}
        units: List[dict] = []
        basket_strats = ('peak_cluster', 'peaker_cool_basket', 'peaker_warm_basket')
        for p in open_pos:
            box = getattr(p, 'cluster_box', '') or ''
            if box and getattr(p, 'strategy', '') in basket_strats:
                clusters.setdefault(box, []).append(p)
            else:
                units.append({
                    'kind': 'single', 'box': '', 'positions': [p],
                    'pnl': p.unrealized_pnl, 'roi': p.roi_pct,
                    'recent': p.entry_time,
                    'strategy': getattr(p, 'strategy', '') or '',
                })
        for box, legs in clusters.items():
            pnl = sum(l.unrealized_pnl for l in legs)
            cost = sum(l.cost_usd for l in legs)
            roi = (pnl / cost * 100.0) if cost > 0 else 0.0
            recent = max(l.entry_time for l in legs)
            strat = getattr(legs[0], 'strategy', 'peak_cluster') if legs else 'peak_cluster'
            units.append({
                'kind': 'cluster', 'box': box, 'positions': legs,
                'pnl': pnl, 'roi': roi, 'recent': recent, 'strategy': strat,
            })
        if sort_key == 'pnl':
            units.sort(key=lambda u: u['pnl'], reverse=True)
        elif sort_key == 'loss':
            units.sort(key=lambda u: u['pnl'])
        elif sort_key == 'roi':
            units.sort(key=lambda u: u['roi'], reverse=True)
        elif sort_key == 'strategy':
            units.sort(key=lambda u: (u.get('strategy', '') or '', -u['pnl']))
        else:  # 'recent'
            units.sort(key=lambda u: u['recent'], reverse=True)
        return units

    def _fmt_position(self, p, idx: int) -> str:
        pe = '🟢' if p.unrealized_pnl >= 0 else '🔴'
        lock = ' 🔒' if getattr(p, 'preclose_locked', False) else ''
        stale = ' ~stale' if getattr(p, 'current_price_stale', False) else ''
        name = self._esc(p.bucket_label or p.market_title)
        return (
            f"{idx}. {pe} <b>{self._esc(p.city)}</b>  "
            f"${p.unrealized_pnl:+.2f} ({p.roi_pct:+.0f}%){lock}{stale}\n"
            f"   {name}\n"
            f"   entry ${p.entry_price:.4f} → ${p.current_price:.4f} | "
            f"{p.shares:.0f}sh | cost ${p.cost_usd:.2f} | {self._esc(p.strategy)}\n\n"
        )

    def _fmt_cluster_unit(self, unit: dict, idx: int) -> str:
        """Render a whole peak-cluster basket as ONE grouped block: a "Box N"
        header with the aggregate PnL, then each bucket leg indented under it."""
        legs = unit['positions']
        pe = '🟢' if unit['pnl'] >= 0 else '🔴'
        city = self._esc(getattr(legs[0], 'city', '') if legs else '')
        cost = sum(l.cost_usd for l in legs)
        label = {
            'peak_cluster': 'Peak Cluster',
            'peaker_cool_basket': 'Peaker Cool Basket',
            'peaker_warm_basket': 'Peaker Warm Basket',
        }.get(unit.get('strategy', 'peak_cluster'), 'Peak Cluster')
        out = (
            f"{idx}. {pe} 🧺 <b>{self._esc(label)} {self._esc(unit['box'])}</b> — {city}  "
            f"${unit['pnl']:+.2f} ({unit['roi']:+.0f}%)\n"
            f"   {len(legs)} buckets | cost ${cost:.2f} | hold → resolution\n"
        )
        for l in legs:
            name = self._esc(l.bucket_label or l.market_title)
            out += (
                f"      • {name}: ${l.entry_price:.3f}→${l.current_price:.3f} "
                f"{l.shares:.0f}sh (${l.unrealized_pnl:+.2f})\n"
            )
        out += "\n"
        return out

    def _positions_view(self, page: int = 0, sort: str = 'recent',
                        with_summary: bool = False):
        """Build (text, inline_keyboard) for a page of open positions.

        Pagination is by display UNIT (a peak-cluster basket counts as one
        unit), so a 6-leg basket no longer eats 6 of the 10 page slots.
        """
        units = self._open_units(sort)
        total_units = len(units)
        total_pos = sum(len(u['positions']) for u in units)
        pages = max(1, (total_units + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        page = max(0, min(page, pages - 1))
        start = page * self.PAGE_SIZE
        chunk = units[start:start + self.PAGE_SIZE]

        text = ''
        if with_summary and self.pm:
            s = self.pm.get_stats()
            text += (
                f"📊 <b>Weather Sniper Status</b>\n"
                f"Mode: {s['mode']} | Balance: ${s['balance']:.2f}\n"
                f"Positions value: ${s.get('position_value', 0.0):.2f} | "
                f"Portfolio: ${s['portfolio_value']:.2f}\n"
                f"PnL: ${s['total_pnl']:+.2f} ({s['roi_pct']:+.1f}%) | "
                f"WR: {s['win_rate']:.0f}% ({s['wins']}W/{s['losses']}L)\n"
                f"Trades: {s['total_trades']} | Open: {s['open_positions']} | "
                f"Redeemed: ${s['total_redeemed']:.2f}\n"
                f"{'-'*28}\n"
            )
        sort_name = self._SORT_NAMES.get(sort, sort)
        shown_to = start + len(chunk)
        text += (f"<b>Open {start + 1}-{shown_to} of {total_units} "
                 f"({total_pos} positions)</b> · sorted: {sort_name}\n\n")
        if not chunk:
            text += "No open positions.\n"
        else:
            last_strat = None
            for i, u in enumerate(chunk, start=start + 1):
                if sort == 'strategy':
                    su = u.get('strategy', '') or '—'
                    if su != last_strat:
                        text += f"\n📂 <b>{self._esc(su)}</b>\n"
                        last_strat = su
                if u['kind'] == 'cluster':
                    text += self._fmt_cluster_unit(u, i)
                else:
                    text += self._fmt_position(u['positions'][0], i)

        sm = '1' if with_summary else '0'
        nav = []
        if page > 0:
            nav.append({'text': '⬅️ Prev', 'callback_data': f"pos:{page-1}:{sort}:{sm}"})
        nav.append({'text': f"{page+1}/{pages}", 'callback_data': 'noop'})
        if page < pages - 1:
            nav.append({'text': 'Next ➡️', 'callback_data': f"pos:{page+1}:{sort}:{sm}"})
        dot = lambda k: ('• ' if sort == k else '')
        sort_row = [
            {'text': dot('pnl') + '💰 PnL', 'callback_data': f"pos:0:pnl:{sm}"},
            {'text': dot('loss') + '📉 Losses', 'callback_data': f"pos:0:loss:{sm}"},
            {'text': dot('roi') + '📈 ROI', 'callback_data': f"pos:0:roi:{sm}"},
            {'text': dot('recent') + '🕒 Recent', 'callback_data': f"pos:0:recent:{sm}"},
        ]
        strat_row = [
            {'text': dot('strategy') + '🗂 By strategy',
             'callback_data': f"pos:0:strategy:{sm}"},
        ]
        return text, {'inline_keyboard': [nav, sort_row, strat_row]}

    def send_positions(self, page: int = 0, sort: str = 'recent',
                       with_summary: bool = False, edit_message_id: int = None):
        if not self.pm:
            return
        text, kb = self._positions_view(page, sort, with_summary)
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def send_status(self):
        """Status = summary + first page of open positions (paginated/sortable)."""
        if not self.pm:
            return
        self.send_positions(page=0, sort='recent', with_summary=True)

    def send_markets_summary(self):
        """Send summary of available markets."""
        if not self.scanner:
            return
        markets = self.scanner.scan_weather_markets(days_ahead=2)
        msg = f"🌤️ <b>Active Weather Markets: {len(markets)}</b>\n\n"
        by_city: Dict[str, int] = {}
        for m in markets:
            by_city[m.city] = by_city.get(m.city, 0) + 1
        for city, count in sorted(by_city.items(), key=lambda x: -x[1]):
            msg += f"  📍 {self._esc(city)}: {count} markets\n"
        self.send(msg)

    def _outcome_breakdown_text(self) -> str:
        """Grouped outcome breakdown (Req-30): settlements/redeems kept SEPARATE
        from the small quick-flip/exit scalps (gains & losses)."""
        if not self.pm or not hasattr(self.pm, 'get_outcome_breakdown'):
            return ''
        try:
            b = self.pm.get_outcome_breakdown()
        except Exception:
            return ''
        g = lambda k: b.get(k, {'count': 0, 'pnl': 0.0})
        sw, rd, sl = g('settle_win'), g('redeemed'), g('settle_loss')
        sg, slo = g('small_gain'), g('small_loss')
        return (
            f"🏦 <b>Settled/Redeemed</b>: ✅ {sw['count']} ${sw['pnl']:+.2f} | "
            f"💰 {rd['count']} ${rd['pnl']:+.2f} | "
            f"❌ {sl['count']} ${sl['pnl']:+.2f}\n"
            f"⚡ <b>Flip/exit scalps</b>: 🟢 {sg['count']} ${sg['pnl']:+.2f} | "
            f"🔴 {slo['count']} ${slo['pnl']:+.2f}\n"
        )

    def send_periodic_summary(self, interval_min: int = 0):
        """Periodic status summary pushed every SUMMARY_INTERVAL_MIN minutes
        (Req-30 summary timer): balance, position value, PnL, WR + the grouped
        settle/redeem vs flip-scalp breakdown."""
        if not self.pm:
            return
        s = self.pm.get_stats()
        hdr = (f"⏲️ <b>Summary</b> (every {interval_min}m)\n"
               if interval_min else "⏲️ <b>Summary</b>\n")
        msg = (
            hdr + f"{'-'*28}\n"
            f"Mode: {s['mode']} | Balance: ${s['balance']:.2f}\n"
            f"Positions value: ${s.get('position_value', 0.0):.2f} "
            f"(open {s['open_positions']})\n"
            f"Portfolio: ${s['portfolio_value']:.2f}\n"
            f"PnL: ${s['total_pnl']:+.2f} ({s['roi_pct']:+.1f}%) | "
            f"WR {s['win_rate']:.0f}% ({s['wins']}W/{s['losses']}L)\n"
        )
        msg += self._outcome_breakdown_text()
        self.send(msg)

    def send_daily_summary(self):
        """Send end-of-day summary."""
        if not self.pm:
            return
        stats = self.pm.get_stats()
        today_positions = [p for p in self.pm.positions
                          if p.entry_time.date() == datetime.now(timezone.utc).date()]
        today_pnl = sum(p.pnl for p in today_positions if p.status != 'open')
        msg = (
            f"📅 <b>Daily Summary</b>\n"
            f"{'-'*30}\n"
            f"New trades today: {len(today_positions)}\n"
            f"Today's PnL: ${today_pnl:+.2f}\n"
            f"Total PnL: ${stats['total_pnl']:+.2f}\n"
            f"Balance: ${stats['balance']:.2f}\n"
            f"Positions value: ${stats.get('position_value', 0.0):.2f}\n"
            f"Portfolio: ${stats['portfolio_value']:.2f}\n"
            f"Win Rate: {stats['win_rate']:.0f}%\n"
        )
        msg += self._outcome_breakdown_text()
        self.send(msg)

    # ==============================================================
    # ANALYSIS (/analysis) — per-strategy performance + downloadable trade log
    # ==============================================================

    def _trade_log_path(self) -> str:
        """Resolve the paper-trade JSONL path (PositionManager's, else Config)."""
        path = getattr(self.pm, '_paper_trades_file', None) if self.pm else None
        return path or getattr(Config, 'PAPER_TRADE_LOG', 'data/paper_trades.jsonl')

    def _read_trade_log(self) -> List[dict]:
        """Read every structured record from data/paper_trades.jsonl (one per
        BUY / SELL / SETTLE / REDEEM / PRECLOSE_LOCK). Returns [] if missing."""
        path = self._trade_log_path()
        recs: List[dict] = []
        try:
            if not os.path.exists(path):
                return recs
            with open(path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            log.debug(f"trade log read failed: {e}")
        return recs

    # ---- Req-27 #2: whole-history merge for /analysis --------------------
    def _history_files(self) -> List[str]:
        """All on-disk JSONL files that may hold paper-trade history: the live
        local log plus any VPS bundle pulled back under data/exports (see
        VPS_PULL_ON_OFFLOAD). De-duplicated, existing files only."""
        import glob as _glob
        paths = []
        try:
            live = self._trade_log_path()
            if live:
                paths.append(live)
        except Exception:
            pass
        for pat in ('data/exports/**/paper_trades*.jsonl',
                    'data/exports/paper_trades*.jsonl',
                    'data/paper_trades*.jsonl'):
            try:
                paths.extend(_glob.glob(pat, recursive=True))
            except Exception:
                continue
        seen, out = set(), []
        for p in paths:
            try:
                rp = os.path.realpath(p)
            except Exception:
                rp = p
            if rp in seen or not os.path.exists(p):
                continue
            seen.add(rp)
            out.append(p)
        return out

    def _merged_history_records(self) -> List[dict]:
        """Every paper-trade record still visible on disk (local live tail +
        any pulled-back VPS bundle), de-duplicated. This is the whole history,
        not just the live ledger the section above reflects."""
        recs: List[dict] = []
        seen = set()
        for path in self._history_files():
            try:
                with open(path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            r = json.loads(line)
                        except Exception:
                            continue
                        key = (r.get('ts'), r.get('action'), r.get('bucket'),
                               r.get('strategy'), r.get('cost_usd'),
                               r.get('pnl'))
                        if key in seen:
                            continue
                        seen.add(key)
                        recs.append(r)
            except Exception as e:
                log.debug(f"history read failed {path}: {e}")
        return recs

    _CLOSING_ACTIONS = ('SELL', 'SETTLE', 'REDEEM', 'PRECLOSE_LOCK')

    def _send_all_time_history(self):
        """Additive all-time tally from the merged raw log so /analysis shows
        the WHOLE paper-trade history (incl. offloaded), not just the handful
        of live not-yet-offloaded positions. Never raises; sends nothing when
        there is no history to add."""
        recs = self._merged_history_records()
        if not recs:
            return
        by_strat: Dict[str, dict] = {}
        opens = 0
        realized = 0.0
        for r in recs:
            strat = str(r.get('strategy') or r.get('signal') or 'unknown')
            g = by_strat.setdefault(strat, {'opens': 0, 'w': 0, 'l': 0, 'pnl': 0.0})
            action = str(r.get('action') or '').upper()
            try:
                pnl = float(r.get('pnl') or 0.0)
            except (ValueError, TypeError):
                pnl = 0.0
            if action == 'BUY':
                g['opens'] += 1
                opens += 1
            if action in self._CLOSING_ACTIONS:
                g['pnl'] += pnl
                realized += pnl
                if pnl > 0:
                    g['w'] += 1
                elif pnl < 0:
                    g['l'] += 1
        if not opens and abs(realized) < 1e-9:
            return
        text = (
            f"{'-'*28}\n"
            f"\U0001F5C4\uFE0F <b>All-time history</b> (raw log, incl. VPS-offloaded)\n"
            f"Rows {len(recs)} \u00B7 Positions opened {opens} \u00B7 "
            f"Realized ${realized:+.2f}\n"
        )
        for strat, g in sorted(by_strat.items(),
                               key=lambda kv: kv[1]['pnl'], reverse=True):
            closed = g['w'] + g['l']
            wr = (g['w'] / closed * 100.0) if closed else 0.0
            pe = '\U0001F7E2' if g['pnl'] >= 0 else '\U0001F534'
            text += (f"{pe} <b>{self._esc(strat)}</b>: {g['opens']} opened \u00B7 "
                     f"{g['w']}W/{g['l']}L \u00B7 {wr:.0f}% \u00B7 ${g['pnl']:+.2f}\n")
        text += ("<i>Live section above is authoritative for open positions; "
                 "this all-time tally is from the raw trade log.</i>\n")
        self.send(text)

    def _send_document(self, file_path: str, caption: str = '') -> bool:
        """Upload a file to the chat as a downloadable document (sendDocument).
        Used by /analysis to ship the raw trade log. Fully defensive."""
        if not self.enabled:
            return False
        try:
            if not os.path.exists(file_path):
                self.send(f"⚠️ Log file not found: {self._esc(file_path)}")
                return False
            with open(file_path, 'rb') as fh:
                files = {'document': (os.path.basename(file_path), fh)}
                data = {'chat_id': self.chat_id}
                if caption:
                    data['caption'] = caption[:1000]
                resp = self._session.post(
                    f"{self.base_url}/sendDocument",
                    data=data, files=files, timeout=30,
                )
            return resp.status_code == 200
        except Exception as e:
            log.debug(f"Telegram sendDocument failed: {e}")
            return False

    # 'pnl' is per-row realized PnL. IMPORTANT: REDEEM rows now carry pnl=0 (the
    # win is booked once on its SETTLE row) so SUMMING the pnl column no longer
    # double-counts wins. 'payout' (redeem cash-back) and 'settle_pnl' (the
    # win's PnL, for reference on the redeem row) are shown as separate columns
    # so the cash movement is still visible without polluting the pnl total.
    _CSV_COLUMNS = [
        'ts', 'action', 'city', 'bucket', 'market', 'strategy', 'signal',
        'entry_price', 'exit_price', 'shares', 'cost_usd', 'edge', 'grade',
        'status', 'exit_reason', 'settle_source', 'pnl', 'payout', 'settle_pnl',
        'roi_pct', 'minutes_to_close', 'balance_after', 'note',
    ]

    def _csv_path(self) -> str:
        base = self._trade_log_path()
        if base.endswith('.jsonl'):
            return base[:-6] + '.csv'
        return base + '.csv'

    def _write_trades_csv(self, recs: List[dict]) -> Optional[str]:
        """Flatten the trade-log records into a tidy CSV for download."""
        if not recs:
            return None
        path = self._csv_path()
        try:
            os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
            with open(path, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=self._CSV_COLUMNS,
                                   extrasaction='ignore')
                w.writeheader()
                for r in recs:
                    w.writerow({k: r.get(k, '') for k in self._CSV_COLUMNS})
            return path
        except Exception as e:
            log.debug(f"csv write failed: {e}")
            return None

    def _mae_mfe_path(self) -> str:
        return getattr(Config, 'MAE_MFE_SUMMARY_PATH', 'data/positions_mae_mfe.jsonl')

    def send_data_export(self):
        """/exportdata -- ship the side-car RESEARCH dataset (per-position price
        path + decision context + outcome) as downloadable JSONL + CSV. This is
        the lightweight, parallel data capture: it never touches the core hot
        path, and answers what-if questions like 'late_no only @ 0.35 -> profit?'.
        Read-only and fully defensive."""
        if not self.enabled:
            return
        # ============================================================
        # PRIMARY: weather_trace -- the REAL per-decision weather data
        # (locks, model spread, provider agreement, starvation). Shipped
        # FIRST + as flattened CSVs so it is the USEFUL export, not an
        # afterthought behind the MAE dump. Fully defensive / read-only.
        # ============================================================
        import csv as _csv
        _wt_sent = False
        try:
            from overlay import weather_trace as _wt
            _wrecs = _wt.read_all()
            _obs = [r for r in _wrecs if r.get('kind') == 'observed']
            _fet = [r for r in _wrecs if r.get('kind') == 'fetch']
            if _wrecs:
                _locked = sum(1 for r in _obs if r.get('is_locked'))
                _starved = sum(1 for r in _obs if r.get('starved'))
                self.send(
                    f"\U0001F321\uFE0F <b>Weather research dataset</b> \u2014 "
                    f"{len(_obs)} observed-lock decisions + {len(_fet)} provider "
                    f"fetches (locked {_locked}, starved {_starved}).\n"
                    f"Flattened CSVs below: observed extreme, remaining model "
                    f"spread, models-with-data vs total, provider peak agreement, "
                    f"cache/starvation \u2014 join to outcomes for real what-if work."
                )
                if _obs:
                    _ocols = ['ts', 'city', 'market', 'strategy', 'day', 'mode',
                              'observed_extreme_c', 'current_temp_c',
                              'remaining_spread_c', 'hours_remaining',
                              'models_with_data', 'models_total', 'models_null',
                              'is_locked', 'used_cache', 'starved', 'lat', 'lon']
                    _op = 'data/weather_trace_observed.csv'
                    with open(_op, 'w', newline='') as _f:
                        _w = _csv.writer(_f)
                        _w.writerow(_ocols)
                        for r in _obs:
                            _w.writerow([r.get(c, '') for c in _ocols])
                    self._send_document(_op, caption=f"weather_trace_observed.csv ({len(_obs)} rows)")
                    _wt_sent = True
                if _fet:
                    _fp = 'data/weather_trace_fetch.csv'
                    with open(_fp, 'w', newline='') as _f:
                        _w = _csv.writer(_f)
                        _w.writerow(['ts', 'city', 'provider_peak_spread_c', 'null_members', 'sources'])
                        for r in _fet:
                            _nm = r.get('null_members') or []
                            _w.writerow([r.get('ts', ''), r.get('city', ''),
                                         r.get('provider_peak_spread_c', ''),
                                         '|'.join(str(x) for x in _nm),
                                         json.dumps(r.get('sources', {}), default=str)])
                    self._send_document(_fp, caption=f"weather_trace_fetch.csv ({len(_fet)} rows)")
                    _wt_sent = True
                try:
                    _wtp = _wt.trace_path()
                    _wt.flush()
                    if os.path.exists(_wtp) and os.path.getsize(_wtp) > 0:
                        self._send_document(_wtp, caption="weather_trace.jsonl (raw)")
                        _wt_sent = True
                except Exception as _e:
                    log.debug(f"weather_trace raw ship failed: {_e}")
                try:
                    self.send(_wt.summarize())
                except Exception:
                    pass
        except Exception as _e:
            log.debug(f"weather_trace export failed: {_e}")
        path = self._mae_mfe_path()
        recs = []
        try:
            if os.path.exists(path):
                with open(path, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            recs.append(json.loads(line))
                        except Exception:
                            continue
        except Exception as e:
            log.debug(f"data export read failed: {e}")
        if not recs:
            self.send("�� No research data captured yet — it builds as positions close "
                      "(side-car logger, zero core impact). Try again after some trades settle.")
            return
        cols = ['t', 'id', 'strategy', 'city', 'bucket', 'signal', 'entry', 'edge',
                'grade', 'prob', 'cost_usd', 'shares', 'min_price', 'max_price',
                'mae_pct', 'mfe_pct', 'crossed_-20', 'crossed_-30', 'crossed_-50',
                'exit_reason', 'exit_price', 'realized_pnl', 'final_status']
        csv_path = (path[:-6] + '.csv') if path.endswith('.jsonl') else (path + '.csv')
        try:
            os.makedirs(os.path.dirname(csv_path) or '.', exist_ok=True)
            with open(csv_path, 'w', newline='') as f:
                w = csv.writer(f)
                w.writerow(cols)
                for r in recs:
                    cr = r.get('crossed', {}) or {}
                    w.writerow([
                        r.get('t', ''), r.get('id', ''), r.get('strategy', ''),
                        r.get('city', ''), r.get('bucket', ''), r.get('signal', ''),
                        r.get('entry', ''), r.get('edge', ''), r.get('grade', ''),
                        r.get('prob', ''), r.get('cost_usd', ''), r.get('shares', ''),
                        r.get('min_price', ''), r.get('max_price', ''), r.get('mae_pct', ''),
                        r.get('mfe_pct', ''), cr.get('-20', ''), cr.get('-30', ''),
                        cr.get('-50', ''), r.get('exit_reason', ''), r.get('exit_price', ''),
                        r.get('realized_pnl', ''), r.get('final_status', ''),
                    ])
        except Exception as e:
            log.debug(f"data export csv failed: {e}")
            csv_path = None
        self.send(
            f"📊 <b>Price-path dataset (secondary)</b> — {len(recs)} closed positions with full "
            f"price-path (MAE/MFE + dip crossings) and decision context "
            f"(entry, edge, grade, prob, size). Use it for what-if backtests."
        )
        self._send_document(path, caption=f"positions_mae_mfe.jsonl ({len(recs)} rows)")
        if csv_path:
            self._send_document(csv_path, caption="positions_mae_mfe.csv")
        # WEATHER TRACE (watcher side-car): ship the raw per-decision weather
        # health log too, so what-if analysis can join outcomes to the exact
        # weather data (per-model null/data, provider agreement, cache/starve).
        try:
            from overlay import weather_trace as _wt
            wt_path = _wt.trace_path()
            _wt.flush()
            if os.path.exists(wt_path) and os.path.getsize(wt_path) > 0:
                n = sum(1 for _ in open(wt_path))
                self._send_document(wt_path, caption=f"weather_trace.jsonl ({n} events)")
        except Exception as e:
            log.debug(f"weather_trace export skipped: {e}")
        # BOOK TRACE (liquidity side-car): ship the per-position order-book depth
        # log so what-if analysis can prove each exit was book-confirmed vs
        # phantom (enough depth to fill our size at entry/exit). Fully defensive.
        try:
            from overlay import book_trace as _bt
            _bt.flush()
            bt_path = getattr(_bt, 'PATH', 'data/book_trace.jsonl')
            if os.path.exists(bt_path) and os.path.getsize(bt_path) > 0:
                n = sum(1 for _ in open(bt_path))
                bt_csv = self._jsonl_to_csv(bt_path)
                self._send_document(bt_path, caption=f"book_trace.jsonl ({n} book snapshots)")
                if bt_csv and os.path.exists(bt_csv):
                    self._send_document(bt_csv, caption="book_trace.csv")
        except Exception as e:
            log.debug(f"book_trace export skipped: {e}")

    def send_analysis(self):
        """/analysis -- clean strategy performance + HOW positions closed
        (counts, W/L, realized PnL per exit type) + downloadable CSVs.

        Everything is computed from the PositionManager (authoritative), so the
        W/L and PnL shown here MATCH the position ledger (the raw log tally could
        double-count across restarts -- that was the "pnl error"/messy exits).
        The raw trade log CSV and the per-position MAE/MFE path CSV are attached.
        """
        if not self.pm:
            self.send("\u26A0\uFE0F Analysis unavailable -- position manager not wired.")
            return
        stats = self.pm.get_stats()
        by_strat = self.pm.get_per_strategy_stats()

        text = (
            f"\U0001F4C8 <b>Strategy Analysis</b> -- {stats['mode']}\n"
            f"Balance ${stats['balance']:.2f} | PnL ${stats['total_pnl']:+.2f} "
            f"({stats['roi_pct']:+.1f}%)\n"
            f"WR {stats['win_rate']:.0f}% ({stats['wins']}W/{stats['losses']}L) | "
            f"Trades {stats['total_trades']} | Open {stats['open_positions']} | "
            f"Redeemed ${stats['total_redeemed']:.2f}\n"
            f"Positions value ${stats.get('position_value', 0.0):.2f} | "
            f"Portfolio ${stats['portfolio_value']:.2f}\n"
            f"{'-'*28}\n"
            f"<b>By strategy</b> (closed W/L \u00B7 WR \u00B7 realized PnL)\n"
        )
        text += self._outcome_breakdown_text()
        if not by_strat:
            text += "  (no trades yet)\n"
        else:
            for strat, s in sorted(by_strat.items(),
                                   key=lambda kv: kv[1]['pnl'], reverse=True):
                closed = s['wins'] + s['losses']
                wr = (s['wins'] / closed * 100.0) if closed else 0.0
                pe = '\U0001F7E2' if s['pnl'] >= 0 else '\U0001F534'
                text += (
                    f"{pe} <b>{self._esc(strat)}</b>: {s['trades']} pos \u00B7 "
                    f"{s['wins']}W/{s['losses']}L \u00B7 {wr:.0f}% \u00B7 ${s['pnl']:+.2f}\n"
                )

        # HOW POSITIONS CLOSED -- grouped by canonical exit from the PM ledger
        # (source of truth) so counts + realized PnL are consistent. Plain labels.
        exit_groups: Dict[str, dict] = {}
        for p in self.pm.positions:
            if getattr(p, 'status', '') == 'open':
                continue
            lbl = self._close_label(p)
            g = exit_groups.setdefault(lbl, {'n': 0, 'pnl': 0.0, 'w': 0, 'l': 0})
            g['n'] += 1
            g['pnl'] += (getattr(p, 'pnl', 0.0) or 0.0)
            oc = self.pm._closed_outcome(p)
            if oc == 'win':
                g['w'] += 1
            elif oc == 'loss':
                g['l'] += 1
        text += f"{'-'*28}\n<b>How positions closed</b>\n"
        if not exit_groups:
            text += "  (nothing closed yet)\n"
        else:
            tot_n = sum(g['n'] for g in exit_groups.values())
            tot_pnl = sum(g['pnl'] for g in exit_groups.values())
            for lbl, g in sorted(exit_groups.items(),
                                 key=lambda kv: kv[1]['pnl'], reverse=True):
                pe = '\U0001F7E2' if g['pnl'] >= 0 else '\U0001F534'
                text += (f"  {pe} {self._esc(lbl)}: {g['n']} closed \u00B7 "
                         f"{g['w']}W/{g['l']}L \u00B7 ${g['pnl']:+.2f}\n")
            text += f"  <b>\u03A3 {tot_n} closed \u00B7 ${tot_pnl:+.2f} realized</b>\n"

        self.send(text)

        # Req-27 #2: additive ALL-TIME history (everything stored on disk), not
        # just the live ledger. Offloading truncates the local log, so the live
        # view alone hides earlier trades; this tallies every paper-trade record
        # still visible (local tail + any pulled-back VPS bundle). Gated by
        # VPS_ANALYSIS_MERGE_ENABLED (default ON); purely additive + fail-open.
        try:
            if bool(getattr(Config, 'VPS_ANALYSIS_MERGE_ENABLED', True)):
                self._send_all_time_history()
        except Exception as e:
            log.debug(f"all-time history section skipped: {e}")

        # Downloadables: (1) tidy trades CSV, (2) per-position MAE/MFE path CSV.
        recs = self._read_trade_log()
        if recs:
            csv_path = self._write_trades_csv(recs)
            if csv_path:
                self._send_document(
                    csv_path,
                    caption=(f"\U0001F4CE Trades CSV -- {len(recs)} rows "
                             f"(buys / sells / exits / profits)"),
                )
            else:
                self._send_document(
                    self._trade_log_path(),
                    caption=(f"\U0001F4CE Full trade log -- {len(recs)} records"),
                )
        else:
            self.send("\u2139\uFE0F No trade-log records yet -- the log is empty.")
        mm_csv = self._jsonl_to_csv('data/positions_mae_mfe.jsonl')
        if mm_csv:
            self._send_document(
                mm_csv,
                caption="\U0001F4CE MAE/MFE per-position -- worst/best ROI vs "
                        "realized (shows round-trips: peaks that gave gains back)")

    def _jsonl_to_csv(self, jsonl_path: str):
        """Flatten a JSONL file to a CSV next to it; return CSV path or None.
        Columns = union of keys across rows (stable first-seen order)."""
        try:
            if not os.path.exists(jsonl_path):
                return None
            rows = []
            with open(jsonl_path) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rows.append(json.loads(line))
                    except Exception:
                        continue
            if not rows:
                return None
            cols = []
            for r in rows:
                for k in r.keys():
                    if k not in cols:
                        cols.append(k)
            out = (jsonl_path[:-6] if jsonl_path.endswith('.jsonl')
                   else jsonl_path) + '.csv'
            with open(out, 'w', newline='') as f:
                w = csv.DictWriter(f, fieldnames=cols, extrasaction='ignore')
                w.writeheader()
                for r in rows:
                    w.writerow({k: r.get(k, '') for k in cols})
            return out
        except Exception as e:
            log.debug(f"jsonl->csv failed: {e}")
            return None

    # ==============================================================
    # PERIODIC EXPORT + DISK GUARD (P3) -- bundle research files into a dated
    # zip, ship it, then delete the on-disk sources so Railway's ephemeral disk
    # can never fill and stall the bot. Fully defensive / fail-open.
    # ==============================================================
    _EXPORT_PATHS = [
        'data/paper_trades.jsonl', 'data/paper_trades.csv',
        'data/positions_mae_mfe.jsonl', 'data/positions_mae_mfe.csv',
        'data/positions_timeseries.jsonl', 'data/weather_trace.jsonl',
        'data/weather_trace_observed.csv', 'data/weather_trace_fetch.csv',
        'data/book_trace.jsonl', 'data/book_trace.csv',
    ]

    def _data_dir_mb(self):
        """Approx size of data/ in MB (defensive)."""
        total = 0
        try:
            for root, _dirs, files in os.walk('data'):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(root, fn))
                    except Exception:
                        continue
        except Exception:
            return 0.0
        return total / (1024.0 * 1024.0)

    def _zip_and_ship_exports(self, reason='periodic', delete_after=True):
        """Bundle all on-disk export files into ONE dated zip (dated folder
        inside), ship it, then optionally delete the sources so they re-record
        fresh. Returns True if a zip was sent."""
        if not self.enabled:
            return False
        import zipfile as _zip
        import glob as _glob
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M')
        folder = 'weatherpol_export_' + stamp
        zip_path = os.path.join('data', folder + '.zip')
        try:
            recs = self._read_trade_log()
            if recs:
                self._write_trades_csv(recs)
            self._jsonl_to_csv('data/positions_mae_mfe.jsonl')
            self._jsonl_to_csv('data/book_trace.jsonl')
        except Exception:
            pass
        candidates = list(self._EXPORT_PATHS)
        try:
            candidates += sorted(_glob.glob('data/recover/recover_*.json'))
        except Exception:
            pass
        present = []
        for pth in candidates:
            try:
                if os.path.exists(pth) and os.path.getsize(pth) > 0:
                    present.append(pth)
            except Exception:
                continue
        if not present:
            if reason == 'manual':
                self.send("\u2139\uFE0F Nothing on disk to export yet.")
            return False
        try:
            os.makedirs('data', exist_ok=True)
            with _zip.ZipFile(zip_path, 'w', _zip.ZIP_DEFLATED) as zf:
                for pth in present:
                    zf.write(pth, arcname=os.path.join(folder, os.path.basename(pth)))
        except Exception as e:
            log.debug("export zip build failed: %s" % e)
            return False
        cap = ("\U0001F4E6 Data export (" + reason + ") -- " + str(len(present))
               + " files, dated " + stamp + " UTC (trades, MAE/MFE, weather).")
        sent = self._send_document(zip_path, caption=cap)
        try:
            os.remove(zip_path)
        except Exception:
            pass
        if sent and delete_after:
            removed = 0
            for pth in present:
                try:
                    os.remove(pth)
                    removed += 1
                except Exception:
                    continue
            self.send("\U0001F5D1\uFE0F Rotated: shipped " + str(len(present))
                      + " file(s), cleared " + str(removed)
                      + " from disk. Fresh recording resumes now.")
        return bool(sent)

    def maybe_periodic_export(self):
        """Scan-tick hook. Force-rotates when data/ crosses the disk guard (in
        ANY mode -- this is what stops the crash), and on the configured hour
        interval when EXPORT_PERIODIC_ENABLED is on. Fully defensive."""
        if not self.enabled:
            return
        try:
            import time as _time
            guard_mb = float(getattr(Config, 'EXPORT_DISK_GUARD_MB', 400) or 0)
            if guard_mb > 0 and self._data_dir_mb() >= guard_mb:
                log.info("export disk guard tripped; rotating early")
                self._zip_and_ship_exports(reason='disk-guard', delete_after=True)
                self._last_export_ts = _time.time()
                return
            if not bool(getattr(Config, 'EXPORT_PERIODIC_ENABLED', False)):
                return
            hours = float(getattr(Config, 'EXPORT_PERIODIC_HOURS', 6) or 0)
            if hours <= 0:
                return
            last = getattr(self, '_last_export_ts', 0.0)
            if (_time.time() - last) >= hours * 3600.0:
                self._zip_and_ship_exports(reason='periodic', delete_after=True)
                self._last_export_ts = _time.time()
        except Exception as e:
            log.debug("periodic export check failed: %s" % e)


    # ==============================================================
    # MANUAL CLOSE (/close) — list open positions with a Sell button
    # ==============================================================

    def send_close_menu(self, edit_message_id: int = None):
        """List open positions, each with a Sell button that closes it at the
        current price (manual exit)."""
        if not self.pm:
            self.send("⚠️ Position manager not wired.")
            return
        open_pos = self.pm.get_open_positions()
        if not open_pos:
            text = "✅ No open positions to close."
            kb = {'inline_keyboard': []}
        else:
            text = ("🧮 <b>Manual close</b> — tap a Sell button to close that "
                    "position at its current price:\n\n")
            rows = []
            for i, p in enumerate(open_pos[:30], start=1):
                pe = '🟢' if p.unrealized_pnl >= 0 else '🔴'
                name = self._esc(p.bucket_label or p.market_title)
                text += (
                    f"{i}. {pe} <b>{self._esc(p.city)}</b> {name} · "
                    f"{self._esc(p.strategy)}\n"
                    f"   ${p.entry_price:.3f}→${p.current_price:.3f} | "
                    f"{p.shares:.0f}sh | ${p.unrealized_pnl:+.2f} "
                    f"({p.roi_pct:+.0f}%)\n"
                )
                rows.append([{
                    'text': f"🔴 Sell #{i} · {p.city} ${p.unrealized_pnl:+.2f}",
                    'callback_data': f"close:{p.id}",
                }])
            kb = {'inline_keyboard': rows}
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def _do_manual_close(self, pos_id: str, callback_id: str, message_id):
        """Sell ONE open position at its current price via the PositionManager."""
        pos = (next((p for p in self.pm.positions if p.id == pos_id), None)
               if self.pm else None)
        if not pos or pos.status != 'open':
            self._answer_callback(callback_id, 'Not open')
            self.send("⚠️ That position is no longer open.")
            return
        try:
            px = pos.current_price or pos.entry_price
            self.pm._close_position(pos, px, 'manual')
            try:
                self.pm._save_state()
            except Exception:
                pass
            self._answer_callback(callback_id, 'Sold')
            self.send(
                f"🔴 <b>SOLD (manual)</b> — {self._esc(pos.strategy)}\n"
                f"📍 {self._esc(pos.city)} | "
                f"{self._esc(pos.bucket_label or pos.market_title)}\n"
                f"💵 entry ${pos.entry_price:.4f} → exit ${px:.4f} | "
                f"{pos.shares:.0f}sh\n"
                f"📊 PnL ${pos.pnl:+.2f} ({pos.roi_pct:+.0f}%)"
            )
            self.send_close_menu(edit_message_id=message_id)
        except Exception as e:
            log.debug(f"manual close failed: {e}")
            self._answer_callback(callback_id, 'Failed')
            self.send("⚠️ Manual close failed — see logs.")

    # ==============================================================
    # /done — Closed history + Open positions
    # ==============================================================

    _DONE_PAGE = 8

    def send_done_menu(self, edit_message_id: int = None):
        kb = {'inline_keyboard': [[
            {'text': '📕 Closed', 'callback_data': 'done:closed:0'},
            {'text': '📗 Open', 'callback_data': 'done:open:0'},
        ]]}
        text = (
            "🗂 <b>Positions</b>\n"
            "📕 <b>Closed</b> — all exits / loss / settle / redeem (history)\n"
            "📗 <b>Open</b> — current positions (🟢 profit / 🔴 losing)"
        )
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    @staticmethod
    def _close_label(p) -> str:
        """Human-readable description of HOW a closed position ended."""
        st = getattr(p, 'status', '')
        reason = getattr(p, 'exit_reason', '') or ''
        if st == 'redeemed':
            return '💰 redeemed'
        if st == 'won':
            return '✅ won (settled)'
        if st == 'lost':
            return '❌ lost (settled)'
        return {
            'take_profit': '\U0001F3AF take-profit',
            'stop_loss': '\U0001F6D1 stop-loss',
            'trailing_stop': '\U0001F4C9 trailing-stop',
            'flip_book': '\U0001F4B5 flip booked',
            'flip_book_mid': '\U0001F4B5 flip booked',
            'flip_stop': '\U0001F6D1 flip stop',
            'flip_timeout': '\u23F2\uFE0F flip book/cut',
            'thesis_invalidated': '\U0001F6AB thesis-exit',
            'profit_cap_book': '\U0001F9E2 profit-cap booked',
            'ml_review_sell': '\U0001F916 ML review sell',
            'manual': '\U0001F534 manual sell',
        }.get(reason, '\U0001F534 sold')

    def _done_closed_view(self, page: int = 0, strat: str = 'all'):
        """Build (text, keyboard) for a page of CLOSED positions -- when bought,
        when/how closed, per-position P/L -- with per-strategy filter chips so
        you can see exactly which positions closed for one strategy."""
        all_closed = ([p for p in self.pm.positions if p.status != 'open']
                      if self.pm else [])
        strat_set = []
        for p in all_closed:
            s = getattr(p, 'strategy', '') or '\u2014'
            if s not in strat_set:
                strat_set.append(s)
        strat_set.sort()
        closed = (all_closed if strat == 'all'
                  else [p for p in all_closed
                        if (getattr(p, 'strategy', '') or '\u2014') == strat])
        # ORDER BY STRATEGY when unfiltered so closed positions are GROUPED by
        # strategy (each strategy block contiguous, newest-first inside it).
        # A single-strategy filter keeps a simple newest-first list.
        def _when(p):
            t = getattr(p, 'exit_time', None) or p.entry_time
            try:
                return t.timestamp()
            except Exception:
                return 0.0
        if strat == 'all':
            closed.sort(key=lambda p: ((getattr(p, 'strategy', '') or '\u2014'), -_when(p)))
        else:
            closed.sort(key=_when, reverse=True)
        # Per-strategy subtotals over the WHOLE filtered set, so the by-strategy
        # status line stays correct even when a group spans multiple pages.
        strat_tot = {}
        for _p in closed:
            s = getattr(_p, 'strategy', '') or '\u2014'
            w, l, r = strat_tot.get(s, (0, 0, 0.0))
            oc = self.pm._closed_outcome(_p)
            w += 1 if oc == 'win' else 0
            l += 1 if oc == 'loss' else 0
            r += (_p.pnl or 0.0)
            strat_tot[s] = (w, l, r)
        total = len(closed)
        pages = max(1, (total + self._DONE_PAGE - 1) // self._DONE_PAGE)
        page = max(0, min(page, pages - 1))
        chunk = closed[page * self._DONE_PAGE:(page + 1) * self._DONE_PAGE]

        wins = sum(1 for p in closed if self.pm._closed_outcome(p) == 'win')
        losses = sum(1 for p in closed if self.pm._closed_outcome(p) == 'loss')
        realized = sum((p.pnl or 0.0) for p in closed)
        filt = '' if strat == 'all' else f" \u00B7 {self._esc(strat)}"
        text = (f"\U0001F4D5 <b>Closed positions</b> ({total}{filt}) -- "
                f"{wins}W/{losses}L | realized ${realized:+.2f}\n\n")
        if not chunk:
            text += "No closed positions in this view.\n"
        cur_strat = None
        idx = page * self._DONE_PAGE
        for p in chunk:
            idx += 1
            # Group header + spacing whenever the strategy changes (unfiltered).
            if strat == 'all':
                s = getattr(p, 'strategy', '') or '\u2014'
                if s != cur_strat:
                    cur_strat = s
                    gw, gl, gr = strat_tot.get(s, (0, 0, 0.0))
                    text += (f"\n\U0001F4C2 <b>{self._esc(self._short_strat(s))}</b> "
                             f"\u00B7 {gw}W/{gl}L \u00B7 ${gr:+.2f}\n")
            val = p.pnl or 0.0
            pe = '\u2705' if val > 0 else ('\u274C' if val < 0 else '\u2796')
            bought = p.entry_time.strftime('%m-%d %H:%M') if p.entry_time else '?'
            closed_at = (p.exit_time.strftime('%m-%d %H:%M')
                         if getattr(p, 'exit_time', None) else '?')
            exit_px = p.exit_price if p.exit_price is not None else p.current_price
            name = self._esc(p.bucket_label or p.market_title)
            box = getattr(p, 'cluster_box', '') or ''
            box_s = f" [{self._esc(box)}]" if box else ''
            text += (
                f"{idx}. {pe} <b>{self._esc(p.city)}</b> {name} \u00B7 {self._esc(p.strategy)}{box_s}\n"
                f"   {self._close_label(p)} | ${val:+.2f} ({p.roi_pct:+.0f}%)\n"
                f"   bought {bought} @ ${p.entry_price:.3f} -> "
                f"closed {closed_at} @ ${exit_px:.3f} | {p.shares:.0f}sh\n\n"
            )
        nav = []
        if page > 0:
            nav.append({'text': '\u2B05\uFE0F Prev',
                        'callback_data': f"done:closed:{page-1}:{strat}"})
        nav.append({'text': f"{page+1}/{pages}", 'callback_data': 'noop'})
        if page < pages - 1:
            nav.append({'text': 'Next \u27A1\uFE0F',
                        'callback_data': f"done:closed:{page+1}:{strat}"})
        rows = [nav]
        def _chip(s, lbl):
            return {'text': ('\u2022 ' if s == strat else '') + lbl,
                    'callback_data': f"done:closed:0:{s}"}
        chip_row = [_chip('all', '\U0001F310 All')]
        for s in strat_set:
            chip_row.append(_chip(s, self._short_strat(s)))
            if len(chip_row) == 3:
                rows.append(chip_row)
                chip_row = []
        if chip_row:
            rows.append(chip_row)
        rows.append([{'text': '\U0001F4D7 Open positions',
                      'callback_data': 'done:open:0'}])
        return text, {'inline_keyboard': rows}

    @staticmethod
    def _short_strat(s: str) -> str:
        return {
            'late_observed_no': 'LateNo',
            'late_observed_yes': 'LateYes',
            'peak_cluster': 'Cluster',
            'peaker_cool_basket': 'Cool',
            'peaker_warm_basket': 'Warm',
            'peaker': 'Peaker',
            'quick_flip': 'Flip',
        }.get(s, (s[:8] if s else '\u2014'))

    # ==============================================================
    # /aisummary — captured runtime warnings/errors
    # ==============================================================

    def send_ai_summary(self):
        """Dump recent WARNING+ runtime log lines captured since startup so you
        can copy them to share. Healthy = nothing captured."""
        lines = list(self._error_log)
        if not lines:
            self.send("✅ <b>AI summary</b> — no warnings or errors captured "
                      "since startup. Bot looks healthy. 🟢")
            return
        errs = sum(1 for l in lines if ' ERROR' in l or ' CRITICAL' in l)
        warns = sum(1 for l in lines if ' WARNING' in l)
        tail = lines[-40:]
        head = (f"🩺 <b>AI summary — runtime issues</b>\n"
                f"Captured {errs} error(s), {warns} warning(s); showing last "
                f"{len(tail)}.\n{'-'*28}\n")
        body = "\n".join(self._esc(l) for l in tail)
        msg = head + f"<code>{body}</code>"
        while len(msg) > 3900 and len(tail) > 5:
            tail = tail[len(tail) // 2:]
            body = "\n".join(self._esc(l) for l in tail)
            msg = head + f"<code>{body}</code>"
        self.send(msg)

    # ==============================================================
    # /mlanalysis — ML (or heuristic) report on all trades
    # ==============================================================

    def send_ml_analysis(self):
        """A report of how trading is going, what's failing, what's observed and
        what to improve. Uses the ML engine for the narrative when it's enabled
        (ML_API_KEY set); otherwise falls back to a heuristic summary."""
        if not self.pm:
            self.send("⚠️ ML analysis unavailable — position manager not wired.")
            return
        stats = self.pm.get_stats()
        by_strat = self.pm.get_per_strategy_stats()
        by_city = (self.pm.get_per_city_stats()
                   if hasattr(self.pm, 'get_per_city_stats') else {})
        ranked = sorted(by_strat.items(), key=lambda kv: kv[1]['pnl'],
                        reverse=True)
        winners = [(k, v) for k, v in ranked if v['pnl'] > 0]
        losers = [(k, v) for k, v in ranked if v['pnl'] < 0]

        text = (f"🧠 <b>ML Analysis</b> — {stats['mode']}\n"
                f"WR {stats['win_rate']:.0f}% "
                f"({stats['wins']}W/{stats['losses']}L) | "
                f"PnL ${stats['total_pnl']:+.2f} | "
                f"Trades {stats['total_trades']}\n{'-'*28}\n")
        narrative = self._ml_narrative(stats, by_strat, by_city)
        if narrative:
            text += narrative + f"\n{'-'*28}\n"
        text += "<b>What's working</b>\n"
        if winners:
            for k, v in winners[:4]:
                c = v['wins'] + v['losses']
                wr = (v['wins'] / c * 100) if c else 0
                text += f"  🟢 {self._esc(k)}: ${v['pnl']:+.2f} ({wr:.0f}% WR)\n"
        else:
            text += "  (no net-positive strategy yet)\n"
        text += "<b>What's failing</b>\n"
        if losers:
            for k, v in losers[:4]:
                c = v['wins'] + v['losses']
                wr = (v['wins'] / c * 100) if c else 0
                text += f"  🔴 {self._esc(k)}: ${v['pnl']:+.2f} ({wr:.0f}% WR)\n"
        else:
            text += "  (no net-losing strategy)\n"
        tips = self._ml_heuristic_tips(stats, ranked)
        if tips:
            text += "<b>Suggested improvements</b>\n"
            for t in tips:
                text += f"  • {self._esc(t)}\n"
        self.send(text)

    def _ml_narrative(self, stats, by_strat, by_city) -> str:
        """Ask the ML engine for a short narrative if it's wired + enabled."""
        ml = getattr(self, 'ml', None)
        if not ml or not getattr(ml, 'enabled', False):
            return ("<i>ML narrative inactive — set ML_API_KEY to let the model "
                    "write the report. Heuristic analysis below.</i>")
        if not getattr(Config, 'ML_ANALYSIS_ENABLED', True):
            return ("<i>ML Analysis is turned OFF (toggle it on in ⚙️ Settings › ML). "
                    "Heuristic analysis below.</i>")
        try:
            if hasattr(ml, 'write_trade_report'):
                return self._esc(ml.write_trade_report(stats, by_strat, by_city))
        except Exception as e:
            log.debug(f"ml narrative failed: {e}")
        return ""

    @staticmethod
    def _ml_heuristic_tips(stats, ranked) -> List[str]:
        tips = []
        closed = stats['wins'] + stats['losses']
        if closed < 20:
            tips.append("Sample still small (<20 closed) — let it run to judge "
                        "edge.")
        if stats['win_rate'] < 50 and closed >= 10:
            tips.append("Win-rate <50% — tighten entry gates on the losing "
                        "strategies.")
        worst = ranked[-1] if ranked else None
        if worst and worst[1]['pnl'] < 0:
            tips.append(f"Consider disabling or tuning '{worst[0]}' — biggest "
                        f"PnL drag.")
        if not tips:
            tips.append("No red flags from the heuristic pass.")
        return tips

    # ==============================================================
    # COMMAND HANDLER (polls for incoming commands)
    # ==============================================================

    def start_polling(self):
        """Start polling for commands in a background thread."""
        if not self.enabled:
            return
        self._running = True
        self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._poll_thread.start()
        log.info("Telegram command polling started")

    def stop_polling(self):
        """Stop polling."""
        self._running = False
        if self._poll_thread:
            self._poll_thread.join(timeout=5)

    def _poll_loop(self):
        """Background loop to check for incoming commands."""
        while self._running:
            try:
                self._check_updates()
            except Exception as e:
                log.debug(f"Telegram poll error: {e}")
            try:
                self.notify_redeems_recent()
            except Exception as e:
                log.debug(f"redeem announce error: {e}")
            time.sleep(3)

    def _check_updates(self):
        """Check for new Telegram messages/commands."""
        try:
            resp = self._session.get(
                f"{self.base_url}/getUpdates",
                params={'offset': self._last_update_id + 1, 'timeout': 2},
                timeout=10,
            )
            if resp.status_code != 200:
                return

            data = resp.json()
            for update in data.get('result', []):
                self._last_update_id = update['update_id']

                cb = update.get('callback_query')
                if cb:
                    cb_chat = str(cb.get('message', {}).get('chat', {}).get('id', ''))
                    if cb_chat == self.chat_id:
                        self._handle_callback(
                            cb.get('data', ''), cb.get('id', ''),
                            cb.get('message', {}).get('message_id'),
                        )
                    continue

                msg = update.get('message', {})
                text = msg.get('text', '').strip()
                chat_id = str(msg.get('chat', {}).get('id', ''))

                if chat_id != self.chat_id:
                    continue

                doc = msg.get('document')
                if doc:
                    self._handle_document(doc)
                    continue

                self._last_msg_id = msg.get('message_id')
                self._handle_command(text)
        except Exception:
            pass

    # ==============================================================
    # SETTINGS PANEL (live tunables + tick-box toggles)
    # ==============================================================

    _SETTINGS_DEFAULT_GROUP = 'main'

    # Short button labels for the on/off toggles (fallback = the key name).
    _LABELS = {
        'TRADING_ENABLED': 'Trading',
        'LATE_OBSERVED_ENABLED': 'Late-Obs',
        'LATE_OBSERVED_NO_SIDE': 'LateObs NO',
        'QUICK_FLIP_ENABLED': 'Quick-Flip',
        'PEAK_CLUSTER_ENABLED': 'Cluster',
        'PEAKER_ENABLED': 'Peaker',
        'CONFIDENT_ENABLED': 'Confident',
        'SNIPER_ENABLED': 'Sniper',
        'SPREAD_ENABLED': 'Spread',
        'STABILITY_ENABLED': 'Stability',
        'ML_ENABLED': 'Use ML',
        'ML_DECISION_ENABLED': 'ML-Decide',
        'ML_ANALYSIS_ENABLED': 'ML Analysis',
        'ML_REVIEW_POSITIONS': 'ML Review-Pos',
        'ML_SELECT_MARKETS': 'ML Market-Pick',
        'AUTO_REDEEM_ENABLED': 'Auto-Redeem',
        'PROFIT_CAP_ENABLED': 'Profit-Cap',
        'PROFIT_CAP_ML_OVERRIDE': 'Cap ML-ride',
        'PEAK_CLUSTER_CONTIGUOUS_ENABLED': 'Cluster contiguous',
        'PEAK_CLUSTER_PROB_BASED_ENABLED': 'Cluster prob-based',
        'PORTFOLIO_GUARD_ENABLED': 'Port-Guard',
        'DRAWDOWN_GATE_ENABLED': 'Drawdown-Gate',
        'QUICK_FLIP_PROFIT_ONLY_EXIT': 'Flip profit-only',
        'QUICK_FLIP_USE_ML_EXIT': 'Flip ML-exit',
        'QUICK_FLIP_BOOK_OR_CUT': 'Flip book-or-cut',
        'PEAKER_PREFER_COOL': 'Prefer cool',
        'PEAKER_TRADE_DECIDED': 'Peaker decided',
        'PEAK_CLUSTER_TRADE_DECIDED': 'Cluster decided',
        'THESIS_EXIT_ENABLED': 'Thesis-exit',
        'LIQUIDITY_GUARD_ENABLED': 'LiqGuard',
        'LIQUIDITY_STRICT_BLOCK': 'LiqStrict',
        'GRADE_SIZING_ENABLED': 'GradeSize',
        'SKIP_DECIDED_MARKETS': 'SkipDecided',
        # Req-27 #6: per-model weather fetch toggles (show the model name).
        'WX_MODEL_ECMWF_IFS_ENABLED': 'ECMWF-HRES',
        'WX_MODEL_ECMWF_IFS025_ENABLED': 'ECMWF-025',
        'WX_MODEL_ECMWF_IFS04_ENABLED': 'ECMWF-04',
        'WX_MODEL_GFS_SEAMLESS_ENABLED': 'GFS',
        'WX_MODEL_ICON_SEAMLESS_ENABLED': 'ICON',
        'WX_MODEL_JMA_SEAMLESS_ENABLED': 'JMA',
        'WX_MODEL_GEM_SEAMLESS_ENABLED': 'GEM',
        # Req-27 #g / VPS controls
        'ML_VETO_BATCH_ENABLED': 'ML veto batch',
        'VPS_OFFLOAD_ENABLED': 'VPS offload',
        'VPS_PULL_ON_OFFLOAD': 'Pull backup on offload',
        'VPS_ANALYSIS_MERGE_ENABLED': 'Analysis merges VPS',
        'VPS_HANDLING_PAPER_TRADES': 'Paper-trades handling',
        'VPS_HANDLING_MAE': 'MAE/MFE handling',
        'VPS_HANDLING_TIMESERIES': 'Timeseries handling',
        'VPS_HANDLING_WEATHER_TRACE': 'Weather-trace handling',
    }

    @staticmethod
    def _fmt_num(v):
        """Compact number formatting for buttons/labels (ints w/o decimals)."""
        if isinstance(v, bool) or v is None:
            return str(v)
        if isinstance(v, int):
            return str(v)
        try:
            f = float(v)
        except (TypeError, ValueError):
            return str(v)
        if f == int(f):
            return str(int(f))
        return f"{f:g}"

    def _settings_view(self, group: str = None):
        """Build (text, inline_keyboard) for ONE settings tab/group, so the
        panel stays browsable instead of one giant +/- wall."""
        from bot import settings_store
        bools, nums = settings_store.snapshot()
        groups = settings_store.GROUPS
        gid = group or self._SETTINGS_DEFAULT_GROUP
        g = next((x for x in groups if x['id'] == gid), groups[0])
        gid = g['id']
        bkeys, nkeys = settings_store.group_keys(gid)
        skeys = settings_store.group_str_keys(gid)
        strs = settings_store.str_snapshot()

        mode = '📋 PAPER' if Config.is_paper() else '🔴 LIVE'
        master = '🟢 ON' if bools.get('TRADING_ENABLED') else '🔴 OFF'
        text = (
            f"⚙️ <b>Bot Settings</b> · {mode}\n"
            f"Master trading: <b>{master}</b>\n"
            f"{'-'*30}\n"
            f"📂 <b>{self._esc(g['title'])}</b>\n"
        )
        if bkeys:
            text += "\n<b>Toggles</b>\n"
            for k in bkeys:
                text += f"  {'✅' if bools.get(k) else '❌'} {self._esc(self._LABELS.get(k, k))}\n"
        if nkeys:
            text += "\n<b>Gates</b>\n"
            for k in nkeys:
                text += f"  • {self._esc(k)} = <b>{self._fmt_num(nums.get(k))}</b>\n"
        if skeys:
            text += "\n<b>Models / Choices</b>\n"
            for k in skeys:
                text += f"  • {self._esc(k)} = <b>{self._esc(str(strs.get(k)))}</b>\n"
        text += "\n<i>Or type /set KEY VALUE · /toggle KEY</i>"

        rows = []
        # Tab row(s): 3 per row, the active tab marked with a dot.
        tab_row = []
        for x in groups:
            label = ('• ' if x['id'] == gid else '') + x['tab']
            tab_row.append({'text': label, 'callback_data': f"st:{x['id']}"})
            if len(tab_row) == 3:
                rows.append(tab_row)
                tab_row = []
        if tab_row:
            rows.append(tab_row)
        # Toggle buttons: 2 per row.
        for i in range(0, len(bkeys), 2):
            row = []
            for k in bkeys[i:i + 2]:
                on = bools.get(k)
                row.append({'text': f"{'✅' if on else '❌'} {self._LABELS.get(k, k)}",
                            'callback_data': f"tg:{k}:{gid}"})
            rows.append(row)
        # Numeric gates: one row each [➖ step][KEY = val][➕ step].
        for k in nkeys:
            step = settings_store.NUM_KEYS[k][2]
            v = nums.get(k)
            rows.append([
                {'text': f"➖{self._fmt_num(step)}", 'callback_data': f"dn:{k}:{gid}"},
                {'text': f"{k} = {self._fmt_num(v)}", 'callback_data': 'noop'},
                {'text': f"➕{self._fmt_num(step)}", 'callback_data': f"up:{k}:{gid}"},
            ])
        # String/choice settings (e.g. ML model): tap to cycle to the next value.
        for k in skeys:
            rows.append([
                {'text': f"🔁 {self._LABELS.get(k, k)}: {strs.get(k)}", 'callback_data': f"cy:{k}:{gid}"},
            ])
        # Req-29: type-to-change starting balance + an OK/Apply button that
        # summarises changes and offers Start. Shown on every tab.
        bal_now = self.pm.get_balance() if self.pm else 0.0
        rows.append([
            {'text': f"💰 Set Starting Balance (now ${self._fmt_num(bal_now)})",
             'callback_data': 'act:setbal'},
        ])
        rows.append([
            {'text': '✅ OK / Apply changes', 'callback_data': 'act:settings_ok'},
        ])
        return text, {'inline_keyboard': rows}

    def send_settings(self, group: str = None, edit_message_id: int = None):
        text, kb = self._settings_view(group)
        if edit_message_id is not None:
            self._edit(edit_message_id, text, kb)
        else:
            self.send(text, reply_markup=kb)

    def _handle_callback(self, data: str, callback_id: str, message_id):
        from bot import settings_store
        if not data or data == 'noop':
            self._answer_callback(callback_id)
            return

        # Positions pager/sorter: "pos:<page>:<sort>:<with_summary>"
        if data.startswith('pos:'):
            try:
                _, page_s, sort_key, sm = data.split(':')
                page = int(page_s)
            except (ValueError, IndexError):
                self._answer_callback(callback_id)
                return
            self._answer_callback(callback_id)
            self.send_positions(page=page, sort=sort_key,
                                with_summary=(sm == '1'),
                                edit_message_id=message_id)
            return

        # Manual close: "close:<position_id>"
        if data.startswith('close:'):
            self._do_manual_close(data.split(':', 1)[1], callback_id, message_id)
            return

        # /done sub-views: "done:closed:<page>[:<strat>]" | "done:open:<page>"
        if data.startswith('done:'):
            parts = data.split(':')
            which = parts[1] if len(parts) > 1 else 'closed'
            try:
                pg = int(parts[2]) if len(parts) > 2 else 0
            except (ValueError, IndexError):
                pg = 0
            strat = parts[3] if len(parts) > 3 else 'all'
            self._answer_callback(callback_id)
            if which == 'closed':
                d_text, d_kb = self._done_closed_view(pg, strat)
                self._edit(message_id, d_text, d_kb)
            else:
                self.send_positions(page=pg, sort='pnl', with_summary=True,
                                    edit_message_id=message_id)
            return

        # Recover menu choice: "recv:files" | "recv:upload"
        if data.startswith('recv:'):
            choice = data.split(':', 1)[1]
            self._answer_callback(callback_id)
            if choice == 'files':
                self._send_recover_file_list()
            elif choice == 'upload':
                self._awaiting = 'recover_upload'
                self.send(
                    "\U0001F4E4 <b>Upload recovery file</b>\n"
                    "Send me the <code>recover_*.json</code> file (as a document) "
                    "that /update gave you. I'll rebuild the REAL open book from it "
                    "(matched by market/condition id, duplicates skipped)."
                )
            return

        # /mlsetup wizard: provider pick "mlp:<name>" or "mlp:__all__"
        if data.startswith('mlp:'):
            self._mlwiz_pick_provider(data.split(':', 1)[1], callback_id)
            return

        # /mlsetup wizard: model pick "mlm:<slot>:<idx>" (d=decision a=analysis)
        if data.startswith('mlm:'):
            self._mlwiz_pick_model(data, callback_id)
            return

        # Recovery restore: "rec:<filename>"
        if data.startswith('rec:'):
            fn = data.split(':', 1)[1]
            self._answer_callback(callback_id, 'Recovering\u2026')
            if not self.pm:
                self.send("\u26A0\uFE0F Recovery unavailable.")
                return
            path = os.path.join('data/recover', fn)
            try:
                with open(path) as _f:
                    snap = json.load(_f)
                res = self.pm.recover_open_snapshot(snap)
                try:
                    os.remove(path)
                except Exception:
                    pass
                self.send(
                    f"\u2705 <b>Recovered</b> {res.get('added', 0)} position(s) "
                    f"(skipped {res.get('skipped', 0)} dup/invalid). File consumed.\n"
                    f"Run <code>/status</code> to verify against today's markets."
                )
            except Exception as e:
                self.send(f"\u26A0\uFE0F Recover failed: {e}")
            return

        # Settings tab switch: "st:<group_id>"
        if data.startswith('st:'):
            group = data.split(':', 1)[1]
            self._answer_callback(callback_id)
            self.send_settings(group=group, edit_message_id=message_id)
            return

        # Lifecycle action buttons: "act:start|settings|restart"
        if data.startswith('act:'):
            action = data.split(':', 1)[1]
            if action == 'start':
                from bot import settings_store
                settings_store.set_value('TRADING_ENABLED', True)
                self._restart_pending = False
                self._answer_callback(callback_id, 'Trading enabled')
                self.send(self._start_message())
            elif action == 'settings':
                self._restart_pending = False
                self._answer_callback(callback_id)
                self.send_settings(edit_message_id=message_id)
            elif action == 'setbal':
                self._awaiting = 'balance'
                self._answer_callback(callback_id, 'Type the new balance')
                bal_now = self.pm.get_balance() if self.pm else 0.0
                self.send(
                    f"💰 <b>Set starting balance</b>\n"
                    f"Current: <b>${bal_now:.2f}</b>\n\n"
                    f"Type the new amount as a number (e.g. <code>500</code>)."
                )
            elif action == 'settings_ok':
                self._answer_callback(callback_id, 'Applying')
                self._finish_settings()
            elif action == 'restart':
                self._answer_callback(callback_id)
                self._prompt_restart()
            elif action == 'restart_confirm':
                self._answer_callback(callback_id, 'Restarting fresh')
                self._do_restart()
            elif action == 'restart_cancel':
                self._restart_pending = False
                self._answer_callback(callback_id, 'Cancelled')
                self.send("✖️ Restart cancelled — positions untouched.")
            else:
                self._answer_callback(callback_id)
            return

        # Toggle / bump: "<action>:<KEY>[:<group>]"
        parts = data.split(':')
        action = parts[0]
        key = parts[1] if len(parts) > 1 else ''
        group = parts[2] if len(parts) > 2 else None
        if not key:
            self._answer_callback(callback_id)
            return
        ok, msg = False, 'no change'
        if action == 'tg':
            ok, msg = settings_store.toggle(key)
        elif action == 'cy':
            ok, msg = settings_store.cycle(key)
        elif action == 'up':
            ok, msg = settings_store.bump(key, +1)
        elif action == 'dn':
            ok, msg = settings_store.bump(key, -1)
        if ok:
            self._note_change(msg or key)
        self._answer_callback(callback_id, msg)
        if ok and message_id is not None:
            self.send_settings(group=group, edit_message_id=message_id)

    # ----- Req-29 settings / balance UX helpers -----------------------------
    def _note_change(self, msg: str):
        """Record a human-readable settings change for the OK summary."""
        try:
            if msg and msg not in self._session_changes:
                self._session_changes.append(msg)
                self._session_changes = self._session_changes[-40:]
        except Exception:
            pass

    def _consume_awaited_input(self, text: str):
        """Handle a typed value we were waiting for (currently: balance)."""
        from bot import settings_store
        what = self._awaiting
        self._awaiting = None
        if what == 'ml_key':
            self._mlwiz_consume_key(text)
            return
        if what == 'ml_url':
            self._mlwiz_set_url(text)
            return
        if what == 'recover_upload':
            self._awaiting = 'recover_upload'
            self.send("\U0001F4CE Please <b>upload the recovery .json file</b> as a "
                      "document (the one /update sent you), or tap a saved point "
                      "via /recover.")
            return
        if what == 'balance':
            raw = text.strip().lstrip('$').replace(',', '')
            try:
                val = float(raw)
            except ValueError:
                self._awaiting = 'balance'
                self.send("⚠️ That doesn't look like a number. Type e.g. <code>500</code>.")
                return
            ok, msg = settings_store.set_value('STARTING_BALANCE', val)
            if ok:
                self._note_change(msg or f"STARTING_BALANCE = {val:g}")
            self.send(
                ("✅ " if ok else "⚠️ ") + msg + "\n\n"
                "Tap <b>OK / Apply changes</b> when you're done, or change more first.",
                reply_markup={'inline_keyboard': [[
                    {'text': '✅ OK / Apply changes', 'callback_data': 'act:settings_ok'},
                    {'text': '⚙️ Settings', 'callback_data': 'act:settings'},
                ]]},
            )

    def _start_message(self) -> str:
        """Enable-trading confirmation. Applies the configured starting balance
        to the live paper ledger when the book is empty (fixes 'set 300 -> only
        traded 100')."""
        note = ""
        if self.pm is not None:
            try:
                res = self.pm.apply_starting_balance()
                if res.get('applied'):
                    note = f"\nStarting balance: <b>${res['balance']:.2f}</b>"
                elif res.get('reason') == 'positions_open':
                    note = (f"\n⚠️ Balance NOT changed — {res.get('open', 0)} position(s) "
                            f"still open. Tap ♻️ Restart to apply ${res['target']:.2f}.")
                elif res.get('reason') == 'has_history':
                    note = (f"\n⚠️ Balance kept at ${res['balance']:.2f} (closed-trade "
                            f"history present). Tap ♻️ Restart to start fresh at "
                            f"${res['target']:.2f}.")
            except Exception:
                pass
        return "🟢 <b>Trading ENABLED</b> — the bot will place new trades." + note

    def _finish_settings(self):
        """OK button: summarise changes, apply the balance if flat, offer Start."""
        changes = list(self._session_changes)
        self._session_changes = []
        if changes:
            body = "\n".join(f"  • {self._esc(c)}" for c in changes)
            summary = f"✅ <b>Settings changed</b>\n{body}"
        else:
            summary = "✅ <b>Settings saved</b> — no changes this session."
        bal_note = ""
        if self.pm is not None:
            try:
                res = self.pm.apply_starting_balance()
                if res.get('applied'):
                    bal_note = f"\n💰 Starting balance is now <b>${res['balance']:.2f}</b>."
                elif res.get('reason') == 'positions_open':
                    bal_note = (f"\n⚠️ {res.get('open', 0)} position(s) open — new balance "
                                f"(${res['target']:.2f}) applies after ♻️ Restart.")
                elif res.get('reason') == 'has_history':
                    bal_note = (f"\n💰 Balance ${res['balance']:.2f}. Tap ♻️ Restart to "
                                f"start fresh at ${res['target']:.2f}.")
            except Exception:
                pass
        kb = {'inline_keyboard': [[
            {'text': '▶️ Start bot now', 'callback_data': 'act:start'},
            {'text': '♻️ Restart fresh', 'callback_data': 'act:restart'},
        ]]}
        self.send(summary + bal_note + "\n\nReady — <b>settings changed, start bot now.</b>",
                  reply_markup=kb)

    # ==============================================================
    # ML AUTO-SETUP WIZARD + FILE-BASED RECOVERY (added 2026-08-03)
    # ==============================================================
    def _download_document(self, file_id):
        """getFile + fetch bytes; return decoded text or None (fail-open)."""
        try:
            r = self._session.get(self.base_url + '/getFile',
                                  params={'file_id': file_id}, timeout=15)
            if r.status_code != 200:
                return None
            fp = (r.json().get('result') or {}).get('file_path')
            if not fp:
                return None
            file_url = self.base_url.replace('/bot', '/file/bot', 1) + '/' + fp
            d = self._session.get(file_url, timeout=30)
            if d.status_code != 200:
                return None
            return d.content.decode('utf-8', 'replace')
        except Exception:
            return None

    def _handle_document(self, doc):
        """A document arrived; only meaningful while awaiting a recovery upload."""
        if self._awaiting != 'recover_upload':
            self.send("\U0001F4CE Got a file, but I wasn't expecting one. Use "
                      "<code>/recover</code> -> \U0001F4E4 Upload to restore positions.")
            return
        self._awaiting = None
        if not self.pm:
            self.send("\u26A0\uFE0F Recovery unavailable -- position manager not wired.")
            return
        name = doc.get('file_name', 'upload.json')
        raw = self._download_document(doc.get('file_id', ''))
        if not raw:
            self.send("\u26A0\uFE0F Couldn't download that file. Try /recover -> Upload again.")
            return
        try:
            snap = json.loads(raw)
        except Exception as e:
            self.send("\u26A0\uFE0F That isn't valid JSON (<code>%s</code>). Upload the "
                      "<code>recover_*.json</code> file /update sent you."
                      % self._esc(str(e)[:80]))
            return
        try:
            res = self.pm.recover_open_snapshot(snap)
            self.send(
                "\u2705 <b>Recovered from upload</b> (%s)\n"
                "Added %d position(s), skipped %d dup/invalid.\n"
                "Run <code>/status</code> to verify against today's markets."
                % (self._esc(name), res.get('added', 0), res.get('skipped', 0))
            )
        except Exception as e:
            self.send("\u26A0\uFE0F Recover failed: <code>%s</code>" % self._esc(str(e)[:100]))

    def _send_recover_menu(self):
        """Two-source recovery picker: saved files vs uploaded file."""
        self.send(
            "\u267B\uFE0F <b>Recover positions</b>\n"
            "Rebuild the REAL open book (matched by market/condition id, "
            "duplicates skipped). Choose a source:",
            reply_markup={'inline_keyboard': [
                [{'text': '\U0001F4C2 From saved files', 'callback_data': 'recv:files'}],
                [{'text': '\U0001F4E4 Upload a file', 'callback_data': 'recv:upload'}],
            ]},
        )

    def _send_recover_file_list(self):
        """List on-disk recover_*.json points as restore buttons."""
        try:
            _d = 'data/recover'
            _files = sorted([x for x in os.listdir(_d)
                             if x.startswith('recover_') and x.endswith('.json')],
                            reverse=True)[:8] if os.path.isdir(_d) else []
        except Exception:
            _files = []
        if not _files:
            self.send("\U0001F4ED No saved recovery files on this deploy. Run "
                      "<code>/update</code> first, or use \U0001F4E4 Upload if you "
                      "kept a downloaded <code>recover_*.json</code>.")
            return
        rows = [[{'text': '\u267B\uFE0F ' + x.replace('recover_', '').replace('.json', ''),
                  'callback_data': 'rec:' + x}] for x in _files]
        self.send(
            "\u267B\uFE0F <b>Saved recovery points</b>\n"
            "Pick one to rebuild the REAL open book:",
            reply_markup={'inline_keyboard': rows},
        )

    # ----- /mlsetup interactive wizard --------------------------------------
    def _handle_vps_command(self, cmd):
        """VPS edge-node status + pull commands. Fast: the node serves counters
        from RAM. All fail-open so a down VPS never breaks the bot."""
        try:
            from data import vps_store
        except Exception as e:
            self.send("VPS client unavailable: %s" % e); return
        if not vps_store.configured():
            self.send("\u26a0\ufe0f VPS not configured. Set VPS_BASE_URL + VPS_AUTH_TOKEN in Railway env."); return
        try:
            if cmd == '/vpshealth':
                h = vps_store.health()
                if not h.get('ok'):
                    self.send("\U0001f534 VPS unreachable: %s" % h.get('error', 'no response')); return
                up = int(h.get('uptime_s', 0) or 0)
                self.send("\U0001f7e2 VPS up\nversion: %s\nuptime: %dh %dm\nround-trip: %s ms" % (
                    h.get('version', '?'), up // 3600, (up % 3600) // 60, h.get('latency_ms', '?')))
            elif cmd == '/vpsweather':
                m = vps_store.metrics()
                if not m.get('ok'):
                    self.send("\U0001f534 VPS metrics unavailable: %s" % m.get('error', '')); return
                w = m.get('weather', {})
                self.send("\U0001f326\ufe0f VPS weather\npolls ok/fail: %s/%s\nlast poll: %ss ago\nfresh models: %s\nsilent: %s\ncache hit rate: %s%%" % (
                    w.get('polls_ok', '?'), w.get('polls_fail', '?'), w.get('last_poll_age_s', '?'),
                    ', '.join(w.get('fresh_models', []) or []) or '-',
                    ', '.join(w.get('silent_models', []) or []) or '-',
                    w.get('cache_hit_rate', '?')))
            elif cmd == '/vpsstorage':
                u = vps_store.usage()
                if not u.get('ok'):
                    self.send("\U0001f534 VPS storage unavailable: %s" % u.get('error', '')); return
                streams = u.get('streams', {}) or {}
                sflat = ', '.join(("%s(%s)" % (k, v)) for k, v in streams.items()) or '-'
                self.send("\U0001f4be VPS storage\nused: %s MB / %s MB (%s%% free)\nrecords: %s\noldest: %s\nstreams: %s" % (
                    u.get('used_mb', '?'), u.get('total_mb', '?'), u.get('free_pct', '?'),
                    u.get('records', '?'), u.get('oldest', '-'), sflat))
            elif cmd == '/vpsstats':
                m = vps_store.metrics()
                if not m.get('ok'):
                    self.send("\U0001f534 VPS stats unavailable: %s" % m.get('error', '')); return
                reqs = m.get('requests', {}) or {}
                rflat = ', '.join(("%s=%s" % (k, v)) for k, v in reqs.items()) or '-'
                self.send("\U0001f4ca VPS stats\ntotal requests: %s\nby route: %s\ncache hit rate: %s%%\noffload enabled: %s" % (
                    m.get('requests_total', '?'), rflat,
                    (m.get('weather', {}) or {}).get('cache_hit_rate', '?'),
                    getattr(Config, 'VPS_OFFLOAD_ENABLED', False)))
            elif cmd == '/vpspull':
                self.send("\u2b07\ufe0f Pulling stored data bundle from the VPS ...")
                res = vps_store.pull_bundle()
                if not res.get('ok'):
                    self.send("\U0001f534 Pull failed: %s" % res.get('error', '')); return
                path = res.get('path')
                if path and os.path.exists(path):
                    self._send_document(path, caption="\U0001f4e6 VPS data bundle (%s)" % res.get('size_h', ''))
                    try:
                        os.remove(path)
                    except Exception:
                        pass
                else:
                    self.send("\u2139\ufe0f VPS reported no stored data to pull.")
            elif cmd == '/vpscheck':
                self._vps_full_check()
        except Exception as e:
            self.send("VPS command failed: %s" % e)

    def _vps_full_check(self):
        # One-shot 'does everything work?' rollup: reachability, weather
        # polls, storage headroom, offload config + an 'update needed?' verdict.
        from data import vps_store
        lines = ["\U0001f9ea <b>VPS full check</b>"]
        todo = []
        h = vps_store.health()
        if h.get('ok'):
            lines.append("\u2705 reachable (%s ms, v%s)" % (h.get('latency_ms', '?'), h.get('version', '?')))
            try:
                if int(h.get('latency_ms', 0) or 0) > 1500:
                    todo.append("High round-trip (>1.5s): node under load -> raise threads or check network.")
            except Exception:
                pass
        else:
            lines.append("\U0001f534 NOT reachable: %s" % h.get('error', '?'))
            todo.append("Open TCP 8080 (provider firewall + ufw); confirm the container is up; then re-run /vpscheck.")
        m = vps_store.metrics()
        if m.get('ok'):
            w = m.get('weather', {}) or {}
            lines.append("\U0001f326\ufe0f polls ok/fail: %s/%s | cache hit: %s%%" % (w.get('polls_ok', '?'), w.get('polls_fail', '?'), w.get('cache_hit_rate', '?')))
            silent = w.get('silent_models', []) or []
            if silent:
                lines.append("\u26a0\ufe0f silent models: %s" % ', '.join(silent))
                todo.append("Silent models (%s) return no data -> remove from OPEN_METEO_MODELS." % ', '.join(silent))
            try:
                if int(w.get('polls_fail', 0) or 0) > int(w.get('polls_ok', 0) or 0):
                    todo.append("More failed polls than good -> check upstream key/limit or raise OM_TIMEOUT_SECONDS.")
            except Exception:
                pass
        else:
            lines.append("\u26a0\ufe0f metrics unavailable: %s" % m.get('error', ''))
        u = vps_store.usage()
        if u.get('ok'):
            lines.append("\U0001f4be storage: %s/%s MB (%s%% free)" % (u.get('used_mb', '?'), u.get('total_mb', '?'), u.get('free_pct', '?')))
            try:
                if float(u.get('free_pct', 100) or 100) < 10:
                    todo.append("Storage under 10% free -> pull a bundle (/vpspull) or raise STORE_MAX_MB.")
            except Exception:
                pass
        else:
            lines.append("\u2139\ufe0f storage: %s" % u.get('error', ''))
        lines.append("\u2699\ufe0f offload: %s (every %sh, %s lines/batch)" % ('ON' if getattr(Config, 'VPS_OFFLOAD_ENABLED', False) else 'OFF', getattr(Config, 'VPS_OFFLOAD_INTERVAL_HOURS', '?'), getattr(Config, 'VPS_OFFLOAD_BATCH_LINES', '?')))
        if todo:
            lines.append("")
            lines.append("\U0001f527 <b>Updates needed:</b>")
            for _i, _t in enumerate(todo, 1):
                lines.append("%d. %s" % (_i, _t))
        else:
            lines.append("")
            lines.append("\u2705 All checks passed \u2014 no updates needed.")
        self.send("\n".join(lines))

    def _mlwiz_prompt_key(self):
        """P4: ask the owner to paste the ML API key in chat; deleted on receipt."""
        self._ml_wiz = {}
        self._awaiting = 'ml_key'
        self.send(
            "\U0001F511 <b>ML auto-setup</b> (step 0/3 -- key)\n"
            "Paste your <b>ML API key</b> here. I will:\n"
            "  \u2022 delete your message immediately,\n"
            "  \u2022 keep the key in memory only (never logged, never written to disk),\n"
            "  \u2022 then ask for the endpoint and auto-detect + wire the models.\n\n"
            "Send <code>cancel</code> to abort."
        )

    def _mlwiz_consume_key(self, text):
        """P4: store the pasted key in-memory, delete the user's message, continue."""
        try:
            mid = getattr(self, '_last_msg_id', None)
            if mid is not None:
                self._delete_message(mid)
        except Exception:
            pass
        raw = (text or '').strip()
        if raw.lower() in ('cancel', 'stop', 'abort'):
            self._ml_wiz = {}
            self.send("\u274C ML setup cancelled. Nothing stored.")
            return
        if len(raw) < 8:
            self._awaiting = 'ml_key'
            self.send("\u26A0\uFE0F That key looks too short. Paste the full key, "
                      "or send <code>cancel</code>.")
            return
        # In-memory ONLY -- never via settings_store, so it never hits
        # runtime_settings.json. Railway env stays the canonical persistent store.
        setattr(Config, 'ML_API_KEY', raw)
        masked = (raw[:3] + '\u2026' + raw[-3:]) if len(raw) >= 8 else '***'
        self.send("\u2705 Key received (<code>%s</code>) and your message was deleted. "
                  "It stays in memory only." % self._esc(masked))
        self._awaiting = 'ml_url'
        cur = getattr(Config, 'ML_API_URL', '') or ''
        self.send(
            "\U0001F9E0 <b>ML auto-setup</b> (step 1/3 -- endpoint)\n"
            "Now send the <b>API base URL / endpoint</b>. Examples:\n"
            "  \u2022 <code>https://api.openai.com/v1</code>\n"
            "  \u2022 <code>https://agentrouter.org/v1</code>\n"
            "  \u2022 your gateway URL (Groq / Together / OpenRouter / vLLM)\n\n"
            + ("Current: <code>%s</code>\n" % self._esc(cur) if cur else "")
            + "Or type <code>default</code> for the provider's own base URL."
        )

    def _mlwiz_start(self):
        """Step 1: require the env key, then ask for the endpoint."""
        key = getattr(Config, 'ML_API_KEY', '') or ''
        if not key:
            self._mlwiz_prompt_key()
            return
        self._ml_wiz = {}
        self._awaiting = 'ml_url'
        cur = getattr(Config, 'ML_API_URL', '') or ''
        self.send(
            "\U0001F9E0 <b>ML auto-setup</b> (step 1/3)\n"
            "Send the <b>API base URL / endpoint</b> to use. Examples:\n"
            "  \u2022 <code>https://api.openai.com/v1</code>\n"
            "  \u2022 <code>https://api.anthropic.com</code>\n"
            "  \u2022 your gateway URL (Groq / Together / OpenRouter / vLLM)\n\n"
            + ("Current: <code>%s</code>\n" % self._esc(cur) if cur else "")
            + "Or type <code>default</code> to use the provider's own default base URL."
        )

    def _mlwiz_set_url(self, text):
        """Step 2: store the endpoint and show the provider picker."""
        t = (text or '').strip()
        if t.lower() in ('default', 'none', '-'):
            t = ''
        self._ml_wiz['url'] = t
        rows = [
            [{'text': 'OpenAI', 'callback_data': 'mlp:openai'},
             {'text': 'Anthropic', 'callback_data': 'mlp:anthropic'}],
            [{'text': 'Gemini', 'callback_data': 'mlp:google_gemini'},
             {'text': 'OpenAI-compatible', 'callback_data': 'mlp:openai_compatible'}],
            [{'text': 'Cohere', 'callback_data': 'mlp:cohere'},
             {'text': 'Ollama', 'callback_data': 'mlp:ollama'}],
            [{'text': '\U0001F50D I don\u2019t know -- try all', 'callback_data': 'mlp:__all__'}],
        ]
        self.send(
            "\U0001F9E0 <b>ML auto-setup</b> (step 2/3)\n"
            "Endpoint: <code>%s</code>\n\n"
            "Which provider is this? I'll fetch the live model list. Not sure? "
            "Tap <b>Try all</b> and I'll probe each one." % self._esc(t or '(provider default)'),
            reply_markup={'inline_keyboard': rows},
        )

    def _mlwiz_pick_provider(self, sel, callback_id):
        """Step 3a: discover models for the chosen provider (or auto-detect)."""
        from ml import provider_profiles as _pp
        url = self._ml_wiz.get('url', '')
        key = getattr(Config, 'ML_API_KEY', '') or ''
        self._answer_callback(callback_id, 'Discovering models\u2026')
        if sel == '__all__':
            self.send("\U0001F50D Probing every provider against your endpoint\u2026 one moment.")
            name, models, tried = _pp.autodetect_profile(url, key)
            report = "\n".join("  %s %s -- %s" % (('\u2705' if ok else '\u274C'),
                                                  self._esc(n), self._esc(note))
                               for n, ok, note in tried)
            if not name:
                self.send("\u274C <b>Auto-detect failed</b> -- no provider answered.\n" + report +
                          "\n\nCheck the endpoint + that <code>ML_API_KEY</code> is set in env, "
                          "then run <code>/mlsetup</code> again.")
                return
            self._ml_wiz['profile'] = name
            self._ml_wiz['models'] = models
            self.send("\u2705 <b>Detected provider:</b> <code>%s</code> (%d models)\n%s"
                      % (self._esc(name), len(models), report))
        else:
            if sel not in _pp.PROFILES:
                self.send("\u274C Unknown provider.")
                return
            ok, models, err = _pp.discover_models(url, key, sel)
            if not ok:
                self.send("\u274C <b>Couldn't list models</b> for <code>%s</code>:\n"
                          "<code>%s</code>\n\nFixes: verify the endpoint + "
                          "<code>ML_API_KEY</code> in env, or pick \U0001F50D Try all."
                          % (self._esc(sel), self._esc(err)))
                return
            self._ml_wiz['profile'] = sel
            self._ml_wiz['models'] = models
        self._ml_wiz['slot'] = 'd'
        self.send(
            "\U0001F9E0 <b>Wire model 1/2 -- DECISION model</b>\n"
            "(used for live trade decisions). Pick one:",
            reply_markup=self._mlwiz_model_kb('d'),
        )

    def _mlwiz_model_kb(self, slot):
        """Build a 2-per-row keyboard of discovered models (first 24)."""
        models = self._ml_wiz.get('models', [])[:24]
        rows, row = [], []
        for i, m in enumerate(models):
            row.append({'text': str(m)[:24], 'callback_data': 'mlm:%s:%d' % (slot, i)})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        return {'inline_keyboard': rows}

    def _mlwiz_pick_model(self, data, callback_id):
        """Step 3b: record the chosen decision/analysis model, then finalize."""
        p = data.split(':')
        slot = p[1] if len(p) > 1 else ''
        try:
            idx = int(p[2])
        except (IndexError, ValueError):
            self._answer_callback(callback_id)
            return
        models = self._ml_wiz.get('models', [])
        if idx < 0 or idx >= len(models):
            self._answer_callback(callback_id, 'stale -- rerun /mlsetup')
            return
        chosen = models[idx]
        if slot == 'd':
            self._ml_wiz['decision'] = chosen
            self._ml_wiz['slot'] = 'a'
            self._answer_callback(callback_id, 'Decision: ' + str(chosen)[:30])
            self.send(
                "\U0001F9E0 <b>Wire model 2/2 -- ANALYSIS model</b>\n"
                "(used for /mlanalysis + position review). Pick one:",
                reply_markup=self._mlwiz_model_kb('a'),
            )
        elif slot == 'a':
            self._ml_wiz['analysis'] = chosen
            self._answer_callback(callback_id, 'Analysis: ' + str(chosen)[:30])
            self._mlwiz_finalize()

    def _mlwiz_finalize(self):
        """Persist the wizard result + live re-init the engine + self-test."""
        from bot import settings_store
        w = self._ml_wiz
        prof = w.get('profile', '')
        url = w.get('url', '')
        dec = w.get('decision', '')
        ana = w.get('analysis', '') or dec
        if url:
            settings_store.set_value('ML_API_URL', url)
        settings_store.set_value('ML_PROVIDER_PROFILE', prof)
        settings_store.set_value('ML_LOCKED_PROFILE', prof)
        settings_store.set_value('ML_MODEL', dec)
        settings_store.set_value('ML_ANALYSIS_MODEL', ana)
        settings_store.set_value('ML_ENABLED', True)
        live = ''
        try:
            from ml.decision_engine import MLDecisionEngine
            eng = MLDecisionEngine()
            self.attach_ml(eng)
            r = eng.self_test()
            if r.get('ok'):
                live = "\u2705 <b>Live test PASSED</b> -- model replied in %ss." % r.get('latency_s')
            else:
                live = ("\u26A0\uFE0F Live test not passing yet: <code>%s</code>\n"
                        "Settings are saved; run <code>/mltest</code> or restart if needed."
                        % self._esc(str(r.get('reason') or r.get('error') or 'unknown')[:120]))
        except Exception as e:
            live = "\u26A0\uFE0F Saved, but live re-init failed: <code>%s</code>" % self._esc(str(e)[:100])
        self._ml_wiz = {}
        self.send(
            "\u2705 <b>ML wired</b>\n"
            "Provider: <code>%s</code>\n"
            "Endpoint: <code>%s</code>\n"
            "Decision model: <code>%s</code>\n"
            "Analysis model: <code>%s</code>\n%s\n"
            "If the trading loop was already running, restart so every component "
            "uses the new models. Status: <code>/mlstatus</code>."
            % (self._esc(prof), self._esc(url or '(provider default)'),
               self._esc(dec), self._esc(ana), live)
        )

    def _handle_command(self, text: str):
        """Handle incoming bot commands."""
        # Req-29: capture a typed value when we're awaiting one (e.g. a new
        # starting balance). A slash-command cancels the awaiting state.
        if self._awaiting and text and not text.startswith('/'):
            self._consume_awaited_input(text)
            return
        if self._awaiting and text.startswith('/'):
            self._awaiting = None
        cmd = text.lower().split()[0] if text else ''
        parts = text.split()

        if cmd in ('/start', '/resume', 'start'):
            from bot import settings_store
            settings_store.set_value('TRADING_ENABLED', True)
            self.send(self._start_message())
        elif cmd in ('/restart', 'restart'):
            self._prompt_restart()
        elif cmd == '/stop' or cmd == '/pause':
            from bot import settings_store
            settings_store.set_value('TRADING_ENABLED', False)
            self.send("🔴 <b>Trading DISABLED</b> — monitoring & resolving only, no new buys.")
        elif cmd == '/settings' or cmd == '/config':
            grp = parts[1].lower() if len(parts) >= 2 else None
            self.send_settings(group=grp)
        elif cmd == '/set':
            from bot import settings_store
            if len(parts) >= 3:
                ok, msg = settings_store.set_value(parts[1], parts[2])
                if ok:
                    self._note_change(msg or f"{parts[1]} = {parts[2]}")
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send("Usage: <code>/set KEY VALUE</code>  e.g. <code>/set BASKET_MAX_COST 0.80</code>")
        elif cmd == '/toggle':
            from bot import settings_store
            if len(parts) >= 2:
                ok, msg = settings_store.toggle(parts[1])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send("Usage: <code>/toggle KEY</code>  e.g. <code>/toggle SNIPER_ENABLED</code>")
        elif cmd == '/status' or cmd == '/stats':
            self.send_status()
        elif cmd == '/balance' or cmd == '/bal':
            bal = self.pm.get_balance() if self.pm else 0
            self.send(f"💰 Balance: ${bal:.2f}")
        elif cmd == '/pnl':
            pnl = self.pm.get_total_pnl() if self.pm else 0
            self.send(f"📊 Total PnL: ${pnl:+.2f}")
        elif cmd == '/positions' or cmd == '/pos':
            self.send_positions(page=0, sort='recent', with_summary=False)
        elif cmd == '/markets':
            self.send_markets_summary()
        elif cmd == '/analysis' or cmd == '/analyze' or cmd == '/report':
            self.send_analysis()
        elif cmd in ('/exportdata', '/data', '/research'):
            self.send_data_export()
        elif cmd in ('/weatherhealth', '/weather', '/whealth'):
            try:
                from overlay import weather_trace as _wt
                self.send(_wt.summarize())
            except Exception as e:
                self.send(f"\u26A0\uFE0F weather health unavailable: {e}")
        elif cmd in ('/close', '/sell'):
            self.send_close_menu()
        elif cmd in ('/done', '/history'):
            self.send_done_menu()
        elif cmd in ('/aisummary', '/errors', '/ai'):
            self.send_ai_summary()
        elif cmd in ('/mlanalysis', '/ml', '/mlreport'):
            self.send_ml_analysis()
        elif cmd in ('/mlstatus', '/mlhealth', '/mlconfig'):
            if not self.ml:
                self.send("\U0001F9E0 <b>ML</b>: engine not attached \u2014 running rules/local model only.")
            else:
                try:
                    st = self.ml.get_status()
                    active = st.get('enabled')
                    # NOTE: precompute any value that contains a backslash escape
                    # (\u2705 etc.) into a local. Python 3.11 forbids a backslash
                    # inside an f-string {expression}; hoisting keeps it 3.11-safe.
                    active_txt = ('\u2705 yes' if active
                                  else '\u274C no \u2014 no API key, using LOCAL model')
                    last_err = (st.get('last_error') or '\u2014')[:80]
                    self.send(
                        "\U0001F9E0 <b>ML Status</b>\n"
                        f"Provider active: {active_txt}\n"
                        f"Decision model: <code>{st.get('model')}</code>\n"
                        f"Analysis model: <code>{st.get('analysis_model')}</code>\n"
                        f"Local fallback: <code>{st.get('local_model')}</code>\n"
                        f"Calls: {st.get('calls')}  \u2022  tokens: {st.get('tokens_used')}\n"
                        f"API failures: {st.get('api_failures')}  \u2022  timeout: {st.get('timeout_s')}s\n"
                        f"Last error: <code>{last_err}</code>\n"
                        "Run <code>/mltest</code> for a live WORKS/NOT-WORKING check."
                    )
                except Exception as e:
                    self.send(f"\u26A0\uFE0F ML status unavailable: {e}")
        elif cmd in ('/mltest', '/mlping'):
            if not self.ml:
                self.send("\U0001F9E0 ML engine not attached; nothing to test. Set ML_API_KEY + ML_API_URL + ML_MODEL and restart.")
            else:
                self.send("\U0001F9E0 Testing ML provider\u2026 (one live call)")
                try:
                    r = self.ml.self_test()
                    if r.get('ok'):
                        self.send(
                            "\u2705 <b>ML WORKS</b>\n"
                            f"Model: <code>{r.get('model')}</code>\n"
                            f"URL: <code>{r.get('url')}</code>\n"
                            f"Latency: {r.get('latency_s')}s\n"
                            f"Reply: <code>{(r.get('reply') or '')[:60]}</code>"
                        )
                    else:
                        self.send(
                            "\u274C <b>ML NOT WORKING</b> (bot keeps trading on rules/local)\n"
                            f"Model: <code>{r.get('model')}</code>\n"
                            f"URL: <code>{r.get('url')}</code>\n"
                            f"Reason: <code>{(r.get('reason') or r.get('error') or 'unknown')[:120]}</code>"
                        )
                except Exception as e:
                    self.send(f"\u26A0\uFE0F ML test error: {e}")
        elif cmd == '/mlkey':
            # P4: fold key entry into the wizard -- prompt, delete msg, auto-detect.
            self._mlwiz_prompt_key()
        elif cmd in ('/vpshealth', '/vpsweather', '/vpsstorage', '/vpsstats', '/vpspull', '/vpscheck'):
            self._handle_vps_command(cmd)
        elif cmd in ('/mlsetup', '/mlprovider', '/mlprofile'):
            # Pick the provider wire-format so the ML talks to whatever endpoint
            # you point ML_API_URL at. Usage:
            #   /mlsetup                -> list profiles + current selection
            #   /mlsetup openai         -> select a profile (and LOCK it)
            try:
                from ml import provider_profiles as _pp
            except Exception as e:
                self.send(f"\u26A0\uFE0F profiles unavailable: {e}")
                return
            from bot import settings_store
            if len(parts) >= 2 and parts[1].strip().lower() != 'list':
                sel = parts[1].strip().lower()
                if sel not in _pp.PROFILES:
                    names = ', '.join(_pp.PROFILES.keys())
                    self.send(f"\u274C Unknown profile <code>{self._esc(sel)}</code>.\nPick one of: <code>{names}</code>")
                    return
                settings_store.set_value('ML_PROVIDER_PROFILE', sel)
                settings_store.set_value('ML_LOCKED_PROFILE', sel)
                prof = _pp.PROFILES[sel]
                self.send(
                    f"\u2705 <b>ML provider locked: {self._esc(prof.label)}</b>\n"
                    f"Chat path: <code>{self._esc(prof.chat_path)}</code>\n"
                    f"Auth: <code>{self._esc(prof.auth_header or ('?key=' if prof.key_in_query else '(none)'))}</code>\n"
                    f"Now set <code>ML_API_URL</code> (base), <code>ML_API_KEY</code>, "
                    f"<code>ML_MODEL</code> and restart, then run <code>/mltest</code>.\n"
                    f"Undo with <code>/mlreset</code>."
                )
            elif len(parts) >= 2:
                cur = (getattr(self.ml, 'profile', None).name if self.ml and getattr(self.ml, 'profile', None) else '\u2014')
                lines = ["\U0001F9E0 <b>ML provider profiles</b>",
                         f"Active: <code>{self._esc(cur)}</code>\n",
                         "Choose the wire-format that matches your endpoint:"]
                for name, prof in _pp.PROFILES.items():
                    mark = '\u2022 ' if name == cur else '   '
                    lines.append(f"{mark}<code>{name}</code> \u2014 {self._esc(prof.label)}")
                lines.append("\nSelect + lock with <code>/mlsetup NAME</code> "
                             "(e.g. <code>/mlsetup openai</code>). Then set "
                             "<code>ML_API_URL</code>/<code>ML_API_KEY</code>/<code>ML_MODEL</code> and <code>/mltest</code>.")
                self.send("\n".join(lines))
            else:
                self._mlwiz_start()
        elif cmd == '/mlreset':
            # Clear the locked profile + reset failure counters so the ML layer
            # re-detects cleanly on next restart. Never touches trading.
            from bot import settings_store
            settings_store.set_value('ML_LOCKED_PROFILE', '')
            if self.ml:
                try:
                    self.ml._api_failures = 0
                    self.ml._last_error = ''
                except Exception:
                    pass
            self.send("\u267B\uFE0F <b>ML lock cleared.</b> The provider profile will "
                      "fall back to <code>ML_PROVIDER_PROFILE</code> (or auto-default) "
                      "on next restart. Failure counters reset. Trading untouched.")
        elif cmd in ('/update', '/savepoint', '/snapshot'):
            if not self.pm:
                self.send("\u26A0\uFE0F Recovery unavailable -- position manager not wired.")
            else:
                try:
                    import time as _time
                    snap = self.pm.export_open_snapshot()
                    os.makedirs('data/recover', exist_ok=True)
                    fn = 'recover_' + _time.strftime('%Y%m%d_%H%M%S') + '.json'
                    with open(os.path.join('data/recover', fn), 'w') as _f:
                        json.dump(snap, _f, indent=2, default=str)
                    n = snap.get('open_count', 0)
                    bal = snap.get('paper_balance', 0.0)
                    self.send(
                        f"\U0001F4BE <b>Recovery point saved</b>\n"
                        f"Captured {n} open position(s) + balance ${bal:.2f}, keyed "
                        f"by market/condition id.\n"
                        f"Use <code>/recover</code> to rebuild the REAL book."
                    )
                    self._send_document(
                        os.path.join('data/recover', fn),
                        caption=('recovery snapshot: %d open, $%.2f. KEEP THIS FILE -- '
                                 'a new Railway deploy wipes the on-disk copy, so you '
                                 'can re-upload it via /recover -> Upload.' % (n, bal)),
                    )
                except Exception as e:
                    self.send(f"\u26A0\uFE0F Recovery save failed: {e}")
        elif cmd in ('/recover', '/restore'):
            if not self.pm:
                self.send("\u26A0\uFE0F Recovery unavailable -- position manager not wired.")
            else:
                self._send_recover_menu()
        elif cmd == '/redeem':
            if self.pm:
                count = self.pm.redeem_all_winning()
                # redeem_all_winning may return a count (int) or a list.
                n = len(count) if isinstance(count, list) else count
                self.notify_redeems_recent()
                self.send(f"💰 Redeemed {n} positions")
        elif cmd in ('/reserve', '/res'):
            from bot import settings_store
            if len(parts) >= 2:
                ok, msg = settings_store.set_value('TAKEOUT_RESERVE_USD', parts[1])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                try:
                    from overlay import reserve_takeout as _rt
                    self.send(_rt.status(self.pm))
                except Exception as e:
                    self.send(f"⚠️ reserve unavailable: {e}")
        elif cmd in ('/takeout', '/take'):
            arg = parts[1].lower() if len(parts) >= 2 else ''
            try:
                from overlay import reserve_takeout as _rt
            except Exception as e:
                self.send(f"⚠️ takeout unavailable: {e}")
                return
            if arg in ('withdraw', 'out', 'cash', 'w'):
                try:
                    ok, msg = _rt.withdraw(self.pm)
                    self.send(("✅ " if ok else "⚠️ ") + msg)
                except Exception as e:
                    self.send(f"⚠️ withdraw failed: {e}")
            elif arg:
                from bot import settings_store
                ok, msg = settings_store.set_value('TAKEOUT_TARGET_USD', parts[1])
                self.send(("✅ " if ok else "⚠️ ") + msg)
            else:
                self.send(_rt.status(self.pm))
        elif cmd == '/info':
            from bot import settings_store
            info = getattr(settings_store, 'INFO', {}) or {}
            if len(parts) >= 2:
                key = parts[1].upper()
                txt = info.get(key)
                self.send(f"ℹ️ <b>{key}</b>\n{txt}" if txt else f"No info for <code>{key}</code>. Try /info with no key to list.")
            else:
                keys = ', '.join(sorted(info.keys()))
                self.send(f"ℹ️ <b>{len(info)} documented settings</b> — use <code>/info KEY</code>:\n{keys}")
        elif cmd == '/help':
            self.send(
                "🌤️ <b>Weather Sniper Commands</b>\n"
                "<b>/start</b> — enable trading (or just type 'start')\n"
                "<b>/restart</b> — clear ALL positions & start fresh (or type 'restart')\n"
                "<b>/stop</b> — disable trading (monitor only)\n"
                "<b>/settings</b> — tabbed panel: toggle strategies & tune every gate\n"
                "   (e.g. <code>/settings peaker</code> opens that tab)\n"
                "/set KEY VALUE — set a gate, e.g. /set BASKET_MAX_COST 0.80\n"
                "/toggle KEY — flip a toggle, e.g. /toggle SNIPER_ENABLED\n"
                "/status — summary + positions (paged, sortable)\n"
                "/balance — current balance\n"
                "/pnl — total profit/loss\n"
                "/positions — open positions (10/page; sort by PnL/Losses/ROI/Recent)\n"
                "/markets — active weather markets\n"
                "/analysis — per-strategy performance + downloadable CSV\n"
                "/exportdata — download research dataset (price paths + context) for what-if analysis\n"
                "/weatherhealth — weather data health (locks, model spread, provider agreement, gaps)\n"
                "/close — manually sell an open position (tap Sell)\n"
                "/done — closed history + open positions (🟢/🔴)\n"
                "/aisummary — recent runtime warnings/errors to share\n"
                "/mlanalysis — ML report: how it's going, what's failing\n"
                "/mlstatus — ML provider config + health (calls, failures, last error)\n"
                "/mltest — live-ping the ML provider: shows WORKS / NOT WORKING\n"
                "/mlsetup [name] — list ML providers / lock one (openai, anthropic, gemini, ...)\n"
                "/mlreset — unlock the provider + clear ML failure counters\n"
                "/redeem — redeem winning positions\n"
                "/update \u2014 save a recovery point of open positions (by market id)\n"
                "/recover \u2014 rebuild the REAL open book from a saved point\n"
                "/reserve [USD] — view/set untouchable cash reserve\n"
                "/takeout [USD|withdraw] — set win-skim target / withdraw the pool\n"
                "/info KEY — explain any setting (effect + range)\n"
                "/vpshealth — VPS up + round-trip latency\n"
                "/vpsweather — VPS weather polls + cache hit rate\n"
                "/vpsstorage — VPS stored data + disk headroom\n"
                "/vpsstats — VPS request counters\n"
                "/vpspull — download stored data bundle from the VPS\n"
                "/vpscheck — full VPS check + what needs fixing\n"
                "/help — this message"
            )
        elif cmd.startswith('/'):
            self.send(f"❓ Unknown command. Try /help")
