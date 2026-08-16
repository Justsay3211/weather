"""
QUICK-FLIP STRATEGY v3 (Req-28) -- high-confidence mispricing, 10%-and-out,
profit-only exit, with optional NO-side buys.

WHY v3 (the user's complaint):
  "quick flips trigger most and take up more capital and losses ... it is meant
  to enter mispriced price, exit faster at 10 percent profits ... don't make the
  exit close in loss ... we need HIGH confidence mispriced markets that tend to
  move as planned and 10 percent profit as initial target ... also utilise the
  NO buys on quick flip for profits."

  v2 fired on too many low-edge buckets (min_edge 0.08, min_conf 0.60, target
  15%) and sized too big (5% / $10, 3 per market), so it churned capital and
  leaked. v3 tightens the funnel and books fast:

  1. HIGH-CONFIDENCE ONLY -- min_confidence raised (default 0.72) and min_edge
     raised (default 0.10). Confidence must clear the floor AFTER boosts, so a
     bare low-edge bucket no longer qualifies.
  2. 10% INITIAL TARGET -- target_roi default 10. We require real headroom: the
     fair-value upside (our_prob vs price) must be >= the 10% book target, so a
     10% move is actually "as planned", not a hope.
  3. SMALLER / FEWER -- size_pct 0.03, max_size $6, max_per_market 2 (concurrent
     cap stays in the dashboard). Stops it from eating the scan budget.
  4. NEVER EXIT IN LOSS -- the signal carries a 10% book target; the profit-only
     ladder in trading/exit_policies books at >=10% and otherwise HOLDS to
     resolution. We never cut at a loss/breakeven (QUICK_FLIP_PROFIT_ONLY_EXIT).
  5. NO-SIDE BUYS -- when a bucket is OVERPRICED (market prices YES above our
     model), the NO token is the underpriced side. If NO has >= min_edge and
     clears confidence, we buy NO (token_id = the NO token) for the same fast
     10% flip. Controlled by QUICK_FLIP_NO_SIDE (default on).

  The two entry contexts from v2 are kept: a fresh model RUN that moved the
  forecast, the publish WINDOW, and a STALE book all BOOST confidence; none are
  hard-required, so pure high-edge mispricing still fires.

FORECAST UPDATE SCHEDULE (UTC, ~15 min publish delay):
  ECMWF 00/12 (+06/18 ens) - GFS 00/06/12/18 - HRRR hourly
  ICON 3-hourly - JMA 00/06/12/18 - GEM 00/12
"""

from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from config import Config
from data.weather_stations import get_station
from logger import log


# -- Forecast update times (UTC) --
FORECAST_UPDATE_SCHEDULE = {
    "ECMWF": [0, 12],
    "GFS": [0, 6, 12, 18],
    "HRRR": list(range(24)),
    "ICON": [0, 3, 6, 9, 12, 15, 18, 21],
    "JMA": [0, 6, 12, 18],
    "GEM": [0, 12],
}


def _current_run() -> Tuple[str, str, int]:
    """Return (run_id, model, minutes_since_update) for the most recent model run.

    run_id encodes the model + its publish timestamp, so it only changes when a
    NEW model run publishes -- that is the boundary we baseline against.
    """
    now = datetime.now(timezone.utc)
    best_model = "none"
    best_minutes = 10 ** 9
    best_dt: Optional[datetime] = None
    for model, hours in FORECAST_UPDATE_SCHEDULE.items():
        for h in hours:
            ut = now.replace(hour=h, minute=15, second=0, microsecond=0)
            if ut > now:
                ut -= timedelta(hours=24)
            diff = (now - ut).total_seconds() / 60.0
            if 0 <= diff < best_minutes:
                best_minutes = diff
                best_model = model
                best_dt = ut
    run_id = f"{best_model}:{best_dt.isoformat()}" if best_dt else "none"
    return run_id, best_model, int(best_minutes)


def minutes_since_last_update() -> Tuple[str, int]:
    """Back-compat helper: (model, minutes_since_update)."""
    _, model, minutes = _current_run()
    return model, minutes


@dataclass
class ForecastChange:
    """A real, run-over-run change in the ensemble forecast."""
    city: str
    station_icao: str
    market_type: str
    old_forecast_c: float
    new_forecast_c: float
    delta_c: float
    old_primary_bucket: str
    new_primary_bucket: str
    affected_buckets: List[str]
    timestamp: datetime


@dataclass
class QuickFlipSignal:
    """A rapid-entry, time-boxed (book-or-hold, profit-only) trade signal."""
    market_title: str
    bucket_label: str
    token_id: str
    direction: str
    entry_price: float
    target_price: float
    entry_reason: str
    forecast_change: Optional[ForecastChange]
    confidence: float
    expected_hold_minutes: int
    expected_roi_pct: float
    size_usd: float
    shares: float
    our_prob: float = 0.0
    side: str = 'YES'   # 'YES' = buy the bucket token; 'NO' = buy its NO token


class QuickFlipStrategy:
    """Enter ONLY high-confidence mispricings (YES or NO side), size small, target
    a fast 10% book, and never cut into a loss."""

    name = "quick_flip"
    description = (
        "High-confidence mispricing flip: buy the underpriced side (YES or NO) "
        "only when edge + confidence clear raised floors; small size; 10% book "
        "target; profit-only exit (holds to resolution rather than cutting a loss)."
    )

    def __init__(self):
        # per (city,market_type) run-boundary baseline
        self._last_forecasts: Dict[str, dict] = {}
        # per (city,market_type,label) last seen market price (stale detection)
        self._last_prices: Dict[str, float] = {}
        # per token_id last signal time (dedup cooldown)
        self._recent_signals: Dict[str, datetime] = {}
        # per market key first-seen time (Req-30 new-market hunting)
        self._seen_markets: Dict[str, datetime] = {}
        self._load_cfg()

    def _load_cfg(self):
        g = lambda n, d: getattr(Config, n, d)
        self.min_delta_c = float(g('QUICK_FLIP_MIN_DELTA_C', 1.0))
        # v3: raised confidence + edge floors (high-confidence ONLY)
        self.min_confidence = float(g('QUICK_FLIP_MIN_CONFIDENCE', 0.72))
        self.min_edge = float(g('QUICK_FLIP_MIN_EDGE', 0.10))
        self.max_entry_price = float(g('QUICK_FLIP_MAX_ENTRY', 0.85))
        self.max_hold_minutes = int(g('QUICK_FLIP_MAX_HOLD_MIN', 120))
        # v3: 10% initial target
        self.target_roi_pct = float(g('QUICK_FLIP_TARGET_ROI', 10.0))
        # v3: smaller / fewer
        self.size_pct = float(g('QUICK_FLIP_SIZE_PCT', 0.03))
        self.max_size_usd = float(g('QUICK_FLIP_MAX_SIZE_USD', 6.0))
        self.max_per_market = int(g('QUICK_FLIP_MAX_PER_MARKET', 2))
        self.cooldown_min = float(g('QUICK_FLIP_SIGNAL_COOLDOWN_MIN', 30.0))
        self.window_min = float(g('QUICK_FLIP_WINDOW_MIN', 20.0))
        self.window_boost = float(g('QUICK_FLIP_WINDOW_BOOST', 0.10))
        self.stale_boost = float(g('QUICK_FLIP_STALE_BOOST', 0.10))
        self.stale_eps = float(g('QUICK_FLIP_STALE_EPS', 0.01))
        # v3: NO-side buys
        self.no_side_enabled = bool(g('QUICK_FLIP_NO_SIDE', 1))
        self.no_min_edge = float(g('QUICK_FLIP_NO_MIN_EDGE', g('QUICK_FLIP_MIN_EDGE', 0.10)))
        self.min_entry_price = float(g('QUICK_FLIP_MIN_ENTRY', 0.03))
        # Req-30: boost a freshly-appeared market so we catch new mispricings early
        self.new_market_boost = float(g('QUICK_FLIP_NEW_MARKET_BOOST', 0.10))
        self.new_market_window_min = float(g('QUICK_FLIP_NEW_MARKET_WINDOW_MIN', 60.0))

    def should_poll_forecasts(self) -> bool:
        """True when we're inside the actionable window after a publish."""
        _, model, minutes = _current_run()
        if minutes < self.window_min:
            log.info(f"  FORECAST UPDATE: {model} updated {minutes}m ago -- ACTIONABLE WINDOW")
            return True
        return False

    def detect_changes(
        self,
        city: str,
        market_type: str,
        bucket_probs: list,
        current_time: datetime,
        run_id: str,
    ) -> Optional[ForecastChange]:
        """Compare the current forecast to the RUN-BOUNDARY baseline. Only emits a
        change when a NEW run has moved the ensemble mean by >= min_delta_c."""
        key = f"{city}_{market_type}"
        primary = max(bucket_probs, key=lambda b: b.probability) if bucket_probs else None
        if not primary:
            return None
        station = get_station(city)
        station_icao = station.icao if station else "???"
        current_mean = primary.mean_forecast
        current_label = primary.bucket_label
        probs = {bp.bucket_label: bp.probability for bp in bucket_probs}
        snapshot = {
            "mean_temp": current_mean,
            "primary_label": current_label,
            "probs": probs,
            "run_id": run_id,
            "timestamp": current_time,
        }
        prev = self._last_forecasts.get(key)
        if prev is None:
            self._last_forecasts[key] = snapshot
            return None
        if prev.get("run_id") == run_id:
            # Same model run -- no NEW data. Don't trade scan jitter.
            return None
        # A new run published: measure the run-over-run shift, then re-baseline.
        old_mean = prev["mean_temp"]
        old_label = prev["primary_label"]
        delta = abs(current_mean - old_mean)
        self._last_forecasts[key] = snapshot
        if delta < self.min_delta_c:
            return None
        affected = []
        for bp in bucket_probs:
            old_prob = prev.get("probs", {}).get(bp.bucket_label, 0)
            if abs(bp.probability - old_prob) > 0.05:
                affected.append(bp.bucket_label)
        return ForecastChange(
            city=city, station_icao=station_icao, market_type=market_type,
            old_forecast_c=old_mean, new_forecast_c=current_mean, delta_c=delta,
            old_primary_bucket=old_label, new_primary_bucket=current_label,
            affected_buckets=affected if affected else [current_label],
            timestamp=current_time,
        )

    def _update_last_prices(self, city: str, market_type: str, market_prices: dict):
        for label, price in market_prices.items():
            try:
                self._last_prices[f"{city}_{market_type}_{label}"] = float(price or 0.0)
            except (TypeError, ValueError):
                continue

    def _is_new_market(self, key: str, now: datetime) -> bool:
        """True while a market is still 'new' (first seen within the new-market
        window). The first sighting records the timestamp. Req-30: lets the flip
        consistently catch freshly-listed markets / mispricings early."""
        first = self._seen_markets.get(key)
        if first is None:
            self._seen_markets[key] = now
            return True
        return (now - first).total_seconds() / 60.0 < self.new_market_window_min

    def _boosted_confidence(self, base: float, run_changed: bool, in_window: bool,
                            stale: bool, new_market: bool = False) -> float:
        conf = base
        if run_changed:
            conf += self.window_boost
        if in_window:
            conf += self.window_boost
        if stale:
            conf += self.stale_boost
        if new_market:
            conf += self.new_market_boost
        return min(1.0, conf)

    def evaluate(
        self,
        market_title: str,
        bucket_probs: list,
        market_prices: dict,
        market_bids: dict,
        token_ids: dict,
        balance: float,
        city: str = "",
        market_type: str = "highest",
        no_prices: Optional[dict] = None,
        no_token_ids: Optional[dict] = None,
    ) -> List[QuickFlipSignal]:
        self._load_cfg()  # pick up live /settings overrides
        signals: List[QuickFlipSignal] = []
        if not bucket_probs or balance <= 0:
            return signals
        no_prices = no_prices or {}
        no_token_ids = no_token_ids or {}
        now = datetime.now(timezone.utc)
        run_id, model, minutes = _current_run()
        in_window = minutes < self.window_min
        # Req-30: is this a freshly-appeared market? (consistent new-market hunt)
        is_new = self._is_new_market(f"{city}_{market_type}_{market_title}", now)

        change = self.detect_changes(city, market_type, bucket_probs, now, run_id)
        changed_labels = set(change.affected_buckets) if change else set()
        if change:
            log.info(
                f"  RUN CHANGE: {city} {market_type} {change.station_icao} "
                f"shifted {change.delta_c:.1f}C ({change.old_primary_bucket}->"
                f"{change.new_primary_bucket}) on {model} run"
            )

        # Build candidate sides. For each bucket we may consider:
        #   YES: buy the bucket token when our_prob > market_price (underpriced YES)
        #   NO : buy the NO token when (1-our_prob) > no_price  (overpriced YES)
        # candidate tuple: (edge, side, bp, label, token, price, side_prob, run_changed)
        candidates = []
        for bp in bucket_probs:
            label = bp.bucket_label
            our_prob = float(getattr(bp, 'probability', 0.0) or 0.0)
            run_changed = label in changed_labels
            # YES side
            yes_token = token_ids.get(label)
            if yes_token:
                yes_price = float(market_prices.get(label, 0.99) or 0.99)
                yes_edge = our_prob - yes_price
                candidates.append((yes_edge, 'YES', bp, label, yes_token, yes_price, our_prob, run_changed))
            # NO side
            if self.no_side_enabled:
                no_token = no_token_ids.get(label)
                if no_token:
                    # explicit NO price if provided, else complement of YES
                    if label in no_prices and no_prices.get(label) is not None:
                        no_price = float(no_prices.get(label) or 0.0)
                    else:
                        yp = float(market_prices.get(label, 0.0) or 0.0)
                        no_price = max(0.0, 1.0 - yp) if yp > 0 else 0.0
                    if no_price > 0:
                        no_prob = max(0.0, 1.0 - our_prob)
                        no_edge = no_prob - no_price
                        candidates.append((no_edge, 'NO', bp, label, no_token, no_price, no_prob, run_changed))

        # Most mispriced first.
        candidates.sort(key=lambda r: r[0], reverse=True)

        placed = 0
        for edge, side, bp, label, token_id, price, side_prob, run_changed in candidates:
            if placed >= self.max_per_market:
                break
            if price > self.max_entry_price or price < self.min_entry_price:
                continue

            min_edge = self.no_min_edge if side == 'NO' else self.min_edge
            # high-confidence mispricing ONLY: a real run move can corroborate but
            # cannot substitute for genuine edge.
            if edge < min_edge:
                continue

            # Dedup cooldown: don't re-signal the same token repeatedly.
            last = self._recent_signals.get(token_id)
            if last and (now - last).total_seconds() / 60.0 < self.cooldown_min:
                continue

            # 10% initial target with real headroom: the price must be able to
            # rise target_roi% and still sit at/under our fair value.
            target_price = round(price * (1.0 + self.target_roi_pct / 100.0), 4)
            target_price = min(target_price, side_prob, 0.97)
            fair_roi = (side_prob - price) / price * 100.0 if price > 0 else 0.0
            if target_price <= price or fair_roi < self.target_roi_pct:
                continue
            expected_roi = (target_price - price) / price * 100.0

            # Confidence: edge-derived OR ensemble agreement, boosted by
            # run/window/stale, must clear the RAISED floor.
            prev_price = self._last_prices.get(f"{city}_{market_type}_{label}")
            stale = prev_price is not None and abs(float(market_prices.get(label, 0.0) or 0.0) - prev_price) < self.stale_eps
            edge_conf = max(0.0, min(1.0, edge / max(min_edge, 0.01) * 0.6))
            agree = float(getattr(bp, 'confidence', 0.0) or 0.0)
            conf = self._boosted_confidence(max(edge_conf, agree), run_changed,
                                            in_window, stale, new_market=is_new)
            if conf < self.min_confidence:
                continue

            size_usd = min(balance * self.size_pct, self.max_size_usd)
            if size_usd < 1.0:
                continue
            shares = size_usd / price if price > 0 else 0
            delta_c = abs(change.delta_c) if change else 0.0
            hold_minutes = min(self.max_hold_minutes, 30 + int(delta_c * 10))
            path = "+run" if run_changed else "+early"
            tags = (path + ("+window" if in_window else "")
                    + ("+stale" if stale else "") + ("+new" if is_new else ""))
            disp = label if side == 'YES' else f"NO {label}"

            signals.append(QuickFlipSignal(
                market_title=market_title,
                bucket_label=label,
                token_id=token_id,
                direction="BUY",
                entry_price=price,
                target_price=target_price,
                entry_reason=(
                    f"FLIP[{model}{tags}|{side}]: edge {edge:+.0%} "
                    f"buy {disp} @ {price:.3f} -> {target_price:.3f} "
                    f"({expected_roi:.0f}% target, conf {conf:.0%}, <={hold_minutes}m)"
                ),
                forecast_change=change,
                confidence=conf,
                expected_hold_minutes=hold_minutes,
                expected_roi_pct=expected_roi,
                size_usd=size_usd,
                shares=shares,
                our_prob=side_prob,
                side=side,
            ))
            self._recent_signals[token_id] = now
            placed += 1

        self._update_last_prices(city, market_type, market_prices)
        return signals


# -- MULTI-OUTCOME SPREAD DETECTOR (kept for compatibility) --

def find_spread_arbitrage(
    bucket_probs: list,
    market_prices: dict,
    token_ids: dict,
    balance: float,
) -> List[dict]:
    """Detect systematic underpricing (bucket prices sum < ~0.85) and surface the
    top underpriced cluster. Retained for compatibility; the peak_cluster strategy
    is the productionized, peak-centered version of this idea."""
    opportunities = []
    prices_list = [market_prices.get(bp.bucket_label, 0) for bp in bucket_probs]
    market_sum = sum(p for p in prices_list if p > 0)
    if market_sum <= 0:
        return []
    mispriced = []
    for bp in bucket_probs:
        mp = market_prices.get(bp.bucket_label, 0.99)
        if mp <= 0 or bp.probability <= 0:
            continue
        edge_ratio = bp.probability / max(mp, 0.01)
        edge = bp.probability - mp
        mispriced.append((bp, mp, edge, edge_ratio))
    mispriced.sort(key=lambda x: x[3], reverse=True)
    if market_sum < 0.85 and len(mispriced) >= 2:
        best = mispriced[:3]
        total_cost = sum(m[1] for m in best)
        total_prob = sum(m[0].probability for m in best)
        if total_cost < total_prob:
            total_roi = (total_prob - total_cost) / total_cost * 100
            opportunities.append({
                "type": "cluster_underpriced",
                "market_sum": market_sum,
                "buckets": [(m[0].bucket_label, m[1], m[0].probability) for m in best],
                "total_cost": total_cost,
                "total_probability": total_prob,
                "expected_roi_pct": total_roi,
                "edge_ratio": total_prob / max(total_cost, 0.01),
            })
    return opportunities
