"""
Scoped exit policies that run in the main loop WITHOUT modifying PositionManager.

Three exits, all reusing PositionManager._close_position(pos, price, reason) so
the conserved ledger, paper-balance credit, paper-trade log and PnL accounting
stay correct. The descriptive reason is passed STRAIGHT INTO _close_position so
paper_trades.jsonl records the true exit reason.

1. quick_flip exit (Req-30/33): a flip is a quick +10% / -5% trade.
   - <= STOP (-5%): cut the loss IMMEDIATELY. Req-33: this hard stop now fires
     for EVERY quick_flip position, INCLUDING ones that were parked to
     resolution (book-or-cut OFF) or otherwise marked hold_to_resolution, so a
     flip can never silently ride a loss down to -70% again. It is checked
     BEFORE the hold-to-resolution guard. Toggle via QUICK_FLIP_HARD_STOP_ENABLED
     ("Flip" tab in Telegram /settings).
   - >= TARGET (+10%): let the ML decide BOOK now vs HOLD for more (15/20/40/..%)
     based on peak, edge, time-left and city win-rate. If the ML is unavailable
     it BOOKS at the target (never round-trips a winner).
   - flat (between stop and target) at the hold cap: BOOK-OR-CUT. When
     QUICK_FLIP_BOOK_OR_CUT is ON we cut it at market (good for stale markets);
     when OFF we let it ride to resolution for UPSIDE only -- the -5% hard stop
     above still protects the downside every cycle.

2. GLOBAL PROFIT CAP (Req-30): any position above PROFIT_CAP_ROI_PCT (default
   300%) is handed to the ML: HOLD-to-settle for even more, or BOOK now to lock
   the gain (a position once ran 500% -> 0). With no ML it is left to settle.

3. STRICT thesis-invalidation: only VERY BAD, non-tail, exitable positions sell
   early; everything else holds to resolution.
"""

from logger import log

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None


def _cfg(name, default):
    return getattr(Config, name, default) if Config is not None else default


# Lazy ML engine singleton.
_ml_engine = None
_ml_init_failed = False


def _get_ml():
    global _ml_engine, _ml_init_failed
    if _ml_engine is not None:
        return _ml_engine
    if _ml_init_failed:
        return None
    try:
        from ml.decision_engine import MLDecisionEngine
        _ml_engine = MLDecisionEngine()
    except Exception as e:  # pragma: no cover
        _ml_init_failed = True
        log.debug(f"exit ML init failed: {e}")
        return None
    return _ml_engine


def _peak_roi(pos):
    entry = pos.entry_price
    peak = getattr(pos, 'peak_price', None)
    if peak and entry and entry > 0:
        return (peak - entry) / entry * 100.0
    return pos.roi_pct


def _ml_profit(pos, mode):
    """Ask the ML whether to BOOK or HOLD an in-profit position. mode in
    {'ladder','cap'}. Returns the decision dict; degrades to the engine's local
    fallback (BOOK for ladder, HOLD/settle for cap) when the API is unavailable."""
    ml = _get_ml()
    if ml is None:
        return {'action': 'HOLD' if mode == 'cap' else 'BOOK', 'reason': 'no ML'}
    try:
        mtc = pos.minutes_to_close
        res_hours = (mtc / 60.0) if mtc is not None else 24.0
        return ml.decide_profit_hold(
            city=pos.city, bucket_label=pos.bucket_label, strategy=pos.strategy,
            entry_price=pos.entry_price, current_price=pos.current_price,
            roi_pct=pos.roi_pct, peak_roi=_peak_roi(pos),
            hold_hours=pos.hold_hours, resolution_hours=res_hours,
            edge=getattr(pos, 'edge_at_entry', None), mode=mode,
        )
    except Exception as e:
        log.debug(f"ml profit decision failed: {e}")
        return {'action': 'HOLD' if mode == 'cap' else 'BOOK', 'reason': 'err'}


def check_ml_reviews(pm):
    """Req-31: the ML reviews each eligible OPEN position and may trigger an
    EARLY SELL when it is CONFIDENT the position should be cut. Conservative by
    design so it never forces a bad exit:
      - only when ML_REVIEW_POSITIONS is on and the API is live;
      - skips quick_flip (its own ladder handles it), hold-to-resolution legs,
        stale-price and zero-price positions;
      - requires a minimum hold time and minimum time-to-close;
      - only acts on a SELL with confidence >= ML_REVIEW_SELL_CONF;
      - caps ML calls per scan (budget / latency guard).
    HOLD or any low-confidence answer changes nothing."""
    if not _cfg('ML_REVIEW_POSITIONS', False):
        return []
    ml = _get_ml()
    if ml is None or not getattr(ml, 'enabled', False):
        return []
    sell_conf = float(_cfg('ML_REVIEW_SELL_CONF', 0.72))
    min_hold = float(_cfg('ML_REVIEW_MIN_HOLD_MIN', 20.0))
    min_mtc = float(_cfg('ML_REVIEW_MIN_MTC_MIN', 45.0))
    budget = int(_cfg('ML_REVIEW_MAX_PER_SCAN', 6))
    triggered = []
    used = 0
    for pos in pm.get_open_positions():
        if used >= budget:
            break
        if pos.strategy == 'quick_flip':
            continue
        if getattr(pos, 'hold_to_resolution', False):
            continue
        if getattr(pos, 'current_price_stale', False):
            continue
        if pos.current_price <= 0:
            continue
        if pos.hold_hours * 60.0 < min_hold:
            continue
        mtc = pos.minutes_to_close
        if mtc is not None and mtc < min_mtc:
            continue
        used += 1
        try:
            res_hours = (mtc / 60.0) if mtc is not None else 24.0
            d = ml.review_position(
                pos.city, pos.bucket_label, pos.entry_price, pos.current_price,
                pos.hold_hours, res_hours, strategy=pos.strategy,
                roi_pct=pos.roi_pct, peak_roi=_peak_roi(pos),
                edge=getattr(pos, 'edge_at_entry', None),
            )
        except Exception as e:
            log.debug(f"ml review failed: {e}")
            continue
        action = str(d.get('action', 'HOLD')).upper()
        try:
            conf = float(d.get('confidence', d.get('conf', 0.0)) or 0.0)
        except (ValueError, TypeError):
            conf = 0.0
        if action == 'SELL' and conf >= sell_conf:
            pm._close_position(pos, pos.current_price, 'ml_review_sell')
            triggered.append(pos)
            log.info(f"ML REVIEW SELL ({conf:.0%}): {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={pos.roi_pct:+.0f}% @ ${pos.current_price:.4f} "
                     f"PnL=${pos.pnl:+.2f} -- {str(d.get('reason', ''))[:48]}")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    return triggered


def check_flip_exits(pm):
    """quick_flip exit: ALWAYS -5% hard stop + +10% book / ML-managed upside.

    Req-33 fix: the user reported a flip sitting at a ~70% loss without exiting.
    Root cause: the -5% stop used to be checked AFTER the hold_to_resolution
    guard, and a FLAT flip at the hold cap with QUICK_FLIP_BOOK_OR_CUT OFF was
    marked hold_to_resolution=True -- which then exempted it from the stop on
    every later cycle, so it rode the loss all the way down. The hard stop is
    now the FIRST thing checked for every quick_flip position (even ones parked
    to resolution), gated by QUICK_FLIP_HARD_STOP_ENABLED so it can be turned
    off from Telegram.
    """
    if not _cfg('QUICK_FLIP_TIME_EXIT', True):
        return []
    default_max = float(_cfg('QUICK_FLIP_MAX_HOLD_MIN', 120))
    use_ml = bool(_cfg('QUICK_FLIP_USE_ML_PROFIT', True))
    book_or_cut = bool(_cfg('QUICK_FLIP_BOOK_OR_CUT', True))
    hard_stop = bool(_cfg('QUICK_FLIP_HARD_STOP_ENABLED', True))
    target = float(_cfg('QUICK_FLIP_TARGET_ROI', 10.0))     # book / decide at >= +10%
    stop = float(_cfg('QUICK_FLIP_STOP_LOSS_PCT', -5.0))    # cut at <= -5%
    mid_book = float(_cfg('QUICK_FLIP_LADDER_MID_ROI_PCT', 20.0))
    triggered = []
    changed = False
    for pos in pm.get_open_positions():
        if pos.strategy != 'quick_flip':
            continue
        if getattr(pos, 'current_price_stale', False):
            continue
        price = pos.current_price
        if price <= 0:
            continue
        roi = pos.roi_pct

        # 0) HARD STOP-LOSS (Req-33): cut the flip's loss at the stop (-5%)
        # IMMEDIATELY. Checked BEFORE the hold_to_resolution guard so a flip that
        # was parked to resolution (book-or-cut OFF) can NEVER ride a loss down.
        if hard_stop and roi <= stop:
            pm._close_position(pos, price, 'flip_stop')
            triggered.append(pos)
            log.info(f"FLIP STOP ({stop:.0f}%): {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={roi:+.1f}% @ ${price:.4f} PnL=${pos.pnl:+.2f}")
            continue

        # Parked-to-resolution flips ride ONLY for upside now; the stop above
        # already protected the downside this cycle.
        if getattr(pos, 'hold_to_resolution', False):
            continue

        max_hold = float(getattr(pos, 'flip_max_hold_minutes', 0) or default_max)
        held_min = pos.hold_hours * 60.0
        window_over = held_min >= max_hold

        # 1) At/above the profit target: ML decides BOOK vs HOLD-for-more.
        if roi >= target:
            book = True
            if use_ml:
                d = _ml_profit(pos, 'ladder')
                book = str(d.get('action', 'BOOK')).upper() == 'BOOK'
                if not book:
                    tgt = d.get('target_roi', roi)
                    log.info(f"FLIP HOLD (ML run->{tgt:.0f}%): {pos.city} "
                             f"{pos.bucket_label[:28]} ROI={roi:+.0f}%")
            if book:
                reason = 'flip_book_mid' if roi >= mid_book else 'flip_book'
                pm._close_position(pos, price, reason)
                triggered.append(pos)
                log.info(f"FLIP BOOK: {pos.city} {pos.bucket_label[:28]} "
                         f"ROI={roi:+.0f}% @ ${price:.4f} PnL=${pos.pnl:+.2f}")
            continue

        # 2) Flat (between stop and target): BOOK-OR-CUT at the hold cap.
        if not window_over:
            continue
        if book_or_cut:
            pm._close_position(pos, price, 'flip_timeout')
            triggered.append(pos)
            log.info(f"FLIP TIMEOUT (book-or-cut): {pos.city} "
                     f"{pos.bucket_label[:28]} held {held_min:.0f}m ROI={roi:+.1f}% "
                     f"@ ${price:.4f} PnL=${pos.pnl:+.2f}")
        else:
            if not getattr(pos, 'hold_to_resolution', False):
                pos.hold_to_resolution = True
                changed = True
                log.info(f"FLIP HOLD->resolution (book-or-cut OFF, -5% stop still "
                         f"armed): {pos.city} {pos.bucket_label[:28]} ROI={roi:+.1f}%")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    elif changed:
        pm._save_state()
    return triggered


def check_profit_caps(pm):
    """GLOBAL cap (any strategy): above PROFIT_CAP_ROI_PCT, ML decides HOLD-to-
    settle vs BOOK. With no ML, let it settle (ride to resolution)."""
    if not _cfg('PROFIT_CAP_ENABLED', True):
        return []
    cap = float(_cfg('PROFIT_CAP_ROI_PCT', 300.0))
    triggered = []
    changed = False
    for pos in pm.get_open_positions():
        if getattr(pos, 'current_price_stale', False):
            continue
        if pos.current_price <= 0:
            continue
        # BASKET EXEMPTION (overlay/basket_guard.py): peak_cluster + peaker
        # cool/warm baskets are any-one-wins, hold-to-$1. The global +300% cap
        # would book a single cheap winning leg early (~$0.48) instead of the
        # $1.00 the basket needs to cover its losing legs. Exempt baskets so the
        # winner rides to settlement. Toggle PROFIT_CAP_EXEMPT_BASKETS (ON).
        if _cfg('PROFIT_CAP_EXEMPT_BASKETS', True):
            try:
                from overlay.basket_guard import is_basket_position
                if is_basket_position(pos):
                    continue
            except Exception:
                pass
        roi = pos.roi_pct
        if roi < cap:
            continue
        # DETERMINISTIC by default: BOOK instantly at the cap to LOCK the gain
        # (fixes the round-trip where a capped winner gave it all back). Flip
        # PROFIT_CAP_ML_OVERRIDE ON to instead let the ML ride it past the cap.
        # NOTE: baskets never reach here (exempted above); late_observed_no
        # realistically never hits the cap (max ~100% ROI at $0.50 entry) so this
        # is only a harmless safety net for it, per design.
        if not _cfg('PROFIT_CAP_ML_OVERRIDE', False):
            pm._close_position(pos, pos.current_price, 'profit_cap_book')
            triggered.append(pos)
            log.info(f"CAP BOOK (deterministic): {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={roi:+.0f}% @ ${pos.current_price:.4f} PnL=${pos.pnl:+.2f}")
            continue
        d = _ml_profit(pos, 'cap')
        if str(d.get('action', 'HOLD')).upper() == 'BOOK':
            pm._close_position(pos, pos.current_price, 'profit_cap_book')
            triggered.append(pos)
            log.info(f"CAP BOOK (ML): {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={roi:+.0f}% @ ${pos.current_price:.4f} PnL=${pos.pnl:+.2f}")
        elif not getattr(pos, 'hold_to_resolution', False):
            pos.hold_to_resolution = True
            changed = True
            log.info(f"CAP HOLD->settle: {pos.city} {pos.bucket_label[:28]} "
                     f"ROI={roi:+.0f}% (ride to resolution)")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    elif changed:
        pm._save_state()
    return triggered


def check_peak_cluster_outer_tp(pm):
    """peak_cluster OUTER-LEG take-profit (OPT-IN, default OFF).

    You cannot know mid-flight which single basket leg will win, and selling the
    eventual winner caps the $1.00 payout the basket needs. So this NEVER sells
    the peak/core leg -- it only banks a run-up OUTER (shoulder) leg once it has
    profited, freeing some cost back while the peak + remaining legs ride to
    resolution for the full $1.

    Rules (peak_cluster-only; the peaker cool/warm baskets are NOT touched):
      * group open peak_cluster legs by cluster_box;
      * the PEAK leg of a box = the highest-priced leg (the market's most-likely
        winner) -- it is NEVER trimmed;
      * an OUTER leg is eligible when its ROI >= PEAK_CLUSTER_OUTER_TP_ROI
        (a fraction, 0.60 = +60%), it has a live bid, and it isn't stale;
      * at most floor(#outer * PEAK_CLUSTER_OUTER_TP_FRACTION) legs (>=1 when any
        are eligible) are trimmed per box, taking the highest-ROI ones first.

    Full closes only (there is no partial-sell primitive; the conserved ledger
    in _close_position stays the single source of truth). Fail-open everywhere.
    """
    if not _cfg('PEAK_CLUSTER_OUTER_TP_ENABLED', False):
        return []
    try:
        roi_frac = float(_cfg('PEAK_CLUSTER_OUTER_TP_ROI', 0.60))
        trim_frac = float(_cfg('PEAK_CLUSTER_OUTER_TP_FRACTION', 0.50))
    except (TypeError, ValueError):
        return []
    if trim_frac <= 0:
        return []
    roi_thresh_pct = roi_frac * 100.0
    # Bucket open peak_cluster legs by their box label.
    boxes = {}
    try:
        for pos in pm.get_open_positions():
            strat = (getattr(pos, 'strategy', '') or '').strip().lower()
            if strat != 'peak_cluster':
                continue
            box = (getattr(pos, 'cluster_box', '') or '').strip()
            if not box:
                continue
            boxes.setdefault(box, []).append(pos)
    except Exception as e:  # pragma: no cover
        log.debug(f"outer-tp scan failed: {e}")
        return []
    triggered = []
    for box, legs in boxes.items():
        if len(legs) < 2:
            continue  # a 1-leg remnant has no "outer" leg to trim
        # Peak = highest-priced leg (never trim). entry_price gives a stable
        # ranking; on ties the first wins.
        try:
            peak_leg = max(legs, key=lambda p: float(getattr(p, 'entry_price', 0.0) or 0.0))
        except Exception:
            continue
        outer = [p for p in legs if p is not peak_leg]
        eligible = []
        for p in outer:
            if getattr(p, 'current_price_stale', False):
                continue
            if float(getattr(p, 'current_price', 0.0) or 0.0) <= 0:
                continue
            if float(getattr(p, 'roi_pct', 0.0) or 0.0) >= roi_thresh_pct:
                eligible.append(p)
        if not eligible:
            continue
        eligible.sort(key=lambda p: float(getattr(p, 'roi_pct', 0.0) or 0.0), reverse=True)
        max_trim = int(len(outer) * trim_frac)
        if max_trim < 1:
            max_trim = 1  # trim at least one when the fraction rounds to zero
        for p in eligible[:max_trim]:
            try:
                pm._close_position(p, p.current_price, 'peak_cluster_outer_tp')
                triggered.append(p)
                log.info(f"CLUSTER OUTER-TP: {p.city} {p.bucket_label[:28]} [{box}] "
                         f"ROI={p.roi_pct:+.0f}% @ ${p.current_price:.4f} "
                         f"PnL=${p.pnl:+.2f} (peak leg holds)")
            except Exception as e:  # pragma: no cover
                log.debug(f"outer-tp close failed: {e}")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    return triggered


def check_liquidity_sweep_exits(pm):
    """LIQUIDITY SWEEP: when a NON-basket thesis breaks and ROI collapses to
    <= LIQ_SWEEP_TRIGGER_ROI_PCT (default -50%), cut it FAST at the best
    immediately-available liquidity (pos.current_price = the live bid) instead
    of letting it bleed toward -90%.

    Why -50% and not -20%: the ledger shows ~43% of positions recover from -20%
    (so cutting there throws away winners), but only ~4% recover from -50% (so
    by then the thesis is genuinely gone and getting out fast beats riding it
    down while liquidity evaporates). This fires EARLIER than THESIS_EXIT (-85%)
    so it is the primary cut; thesis-exit remains as a deeper backstop.

    Same guards + basket exemption as thesis-exit: baskets are any-one-wins
    structures that ride to resolution, tails below min-entry are held, we need
    a real bid to sweep into, and near-close positions are left to settle.
    Reuses PositionManager._close_position so the ledger stays correct.
    """
    if not _cfg('LIQ_SWEEP_EXIT_ENABLED', True):
        return []
    trigger_roi = float(_cfg('LIQ_SWEEP_TRIGGER_ROI_PCT', -50.0))
    min_entry = float(_cfg('LIQ_SWEEP_MIN_ENTRY_PRICE', 0.10))
    min_bid = float(_cfg('LIQ_SWEEP_MIN_BID', 0.02))
    min_mtc = float(_cfg('LIQ_SWEEP_MIN_MINUTES_TO_CLOSE', 30.0))
    triggered = []
    for pos in pm.get_open_positions():
        # quick_flip has its own hard stop; don't double-manage it here.
        if pos.strategy == 'quick_flip':
            continue
        # BASKET PROTECTION: peak_cluster + peaker cool/warm baskets are
        # any-one-wins structures held to resolution (the single winning leg
        # pays $1 and covers the losers). Never sweep a basket leg.
        if _cfg('LIQ_SWEEP_EXEMPT_BASKETS', True):
            try:
                from overlay.basket_guard import is_basket_position
                if is_basket_position(pos):
                    continue
            except Exception:
                pass
        if getattr(pos, 'current_price_stale', False):
            continue
        if pos.entry_price < min_entry:
            continue
        if pos.current_price < min_bid:
            continue
        mtc = pos.minutes_to_close
        if mtc is not None and mtc < min_mtc:
            continue
        if pos.roi_pct > trigger_roi:
            continue
        # Sweep out at the best immediately-available liquidity (live bid).
        pm._close_position(pos, pos.current_price, 'liquidity_sweep')
        triggered.append(pos)
        log.info(f"LIQUIDITY SWEEP: {pos.city} {pos.bucket_label[:28]} "
                 f"ROI={pos.roi_pct:.0f}% <= {trigger_roi:.0f}% — cut fast @ "
                 f"${pos.current_price:.4f} (thesis broken; only ~4% recover here)")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    return triggered


def check_thesis_exits(pm):
    """STRICT early exit: only VERY BAD, non-tail, exitable positions sell early.
    Everything else keeps holding to resolution."""
    if not _cfg('THESIS_EXIT_ENABLED', True):
        return []
    max_roi = float(_cfg('THESIS_EXIT_MAX_ROI_PCT', -85.0))
    min_entry = float(_cfg('THESIS_EXIT_MIN_ENTRY_PRICE', 0.10))
    min_bid = float(_cfg('THESIS_EXIT_MIN_BID', 0.02))
    min_mtc = float(_cfg('THESIS_EXIT_MIN_MINUTES_TO_CLOSE', 60.0))
    triggered = []
    for pos in pm.get_open_positions():
        if pos.strategy == 'quick_flip':
            continue
        # BASKET PROTECTION (overlay/basket_guard.py): peak_cluster + peaker
        # cool/warm baskets are any-one-wins structures held to resolution --
        # the single winning leg pays $1 and covers the losing legs. Cutting a
        # losing leg early on thesis-invalidation throws that away and was the
        # biggest realized leak (-$819 over 130 exits; 33 of them basket legs).
        # Toggle via BASKET_THESIS_EXEMPT (default ON).
        if _cfg('BASKET_THESIS_EXEMPT', True):
            try:
                from overlay.basket_guard import is_basket_position
                if is_basket_position(pos):
                    continue
            except Exception:
                pass
        if getattr(pos, 'current_price_stale', False):
            continue
        if pos.entry_price < min_entry:
            continue
        if pos.current_price < min_bid:
            continue
        mtc = pos.minutes_to_close
        if mtc is not None and mtc < min_mtc:
            continue
        if pos.roi_pct > max_roi:
            continue
        pm._close_position(pos, pos.current_price, 'thesis_invalidated')
        triggered.append(pos)
        log.info(f"THESIS EXIT: {pos.city} {pos.bucket_label[:28]} "
                 f"ROI={pos.roi_pct:.0f}% @ ${pos.current_price:.4f} (very bad - cut)")
    if triggered:
        pm._save_state()
        pm._assert_ledger()
    return triggered
