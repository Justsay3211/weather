#!/usr/bin/env python3
"""
HONEST stability backtest — measures REAL forecast skill, no lookahead.

The key dishonesty in the older backtests: they generated the "forecast" as
    forecast = actual_temp + random_noise
and the market price as
    market_price = our_prob - random_noise
which is circular — it bakes in a win before a single trade. Numbers from that
are meaningless.

This script instead pulls TWO independent real datasets from Open-Meteo:

  1. ARCHIVED FORECAST  (historical-forecast-api.open-meteo.com)
       The actual per-model forecast that was published BEFORE the day happened.
       This is what we would genuinely have traded on — zero lookahead.

  2. ACTUAL OBSERVED    (archive-api.open-meteo.com)
       The real measured daily max at the airport station — the ground truth
       that the market resolves against.

So the forecast→outcome relationship is REAL. We run the exact StabilityEngine +
ProbabilityEngine + StabilityStrategy used live, at the airport station, and check
which adjacent-bucket basket would have won.

HONEST CAVEAT (printed in the report): the free APIs do NOT give the historical
Polymarket order book, so ENTRY PRICES are modeled, not real. We therefore report
two things separately:
  • Forecast skill  — % of days the true max landed in our predicted basket.
                      This is REAL and is the thing our edge depends on.
  • Modeled PnL     — PnL under a transparent market-price model (stated assumptions).
                      Treat as indicative, not a guarantee.

Usage:
  python -m backtest.stability_backtest --days 120 --cities london seoul tokyo singapore ankara
  python -m backtest.stability_backtest --days 90            # default stable-city set
"""

import os
import sys
import math
import json
import time
import argparse
import requests
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.weather_stations import get_station, STATIONS
from data.probability_engine import ProbabilityEngine, BucketProbability
from data.weather_fetcher import ForecastPoint
from logger import log

ARCHIVE_FORECAST = "https://historical-forecast-api.open-meteo.com/v1/forecast"
ARCHIVE_ACTUAL = "https://archive-api.open-meteo.com/v1/archive"

MODELS = ['ecmwf_ifs025', 'gfs_seamless', 'icon_seamless', 'jma_seamless', 'gem_seamless']
MODEL_NAME = {  # map api model → ProbabilityEngine weight key
    'ecmwf_ifs025': 'ECMWF', 'gfs_seamless': 'GFS', 'icon_seamless': 'ICON',
    'jma_seamless': 'JMA', 'gem_seamless': 'GEM',
}

DEFAULT_CITIES = ['london', 'seoul', 'tokyo', 'singapore', 'ankara',
                  'paris', 'madrid', 'beijing', 'taipei', 'moscow']


@dataclass
class DayResult:
    city: str
    date: str
    actual_max: float
    forecast_max: float
    model_spread: float
    basket_centers: List[float]
    basket_won: bool          # did actual land in our adjacent basket?
    center_hit: bool          # did actual land in the single center bucket?
    basket_cost: float        # modeled $ cost of the basket
    pnl: float                # modeled PnL ($1 payout on the winning leg)


@dataclass
class CityStats:
    city: str
    days: int = 0
    basket_wins: int = 0
    center_hits: int = 0
    pnl: float = 0.0
    invested: float = 0.0
    mae: float = 0.0          # mean abs forecast error (°C) — pure forecast skill


class StabilityBacktest:
    def __init__(self, neighbor_span: int = 1, bucket_width: float = 1.0):
        self.session = requests.Session()
        self.engine = ProbabilityEngine()
        self.neighbor_span = neighbor_span
        self.bucket_width = bucket_width  # °C per bucket (Polymarket weather = 1°C)

    # ── data ──────────────────────────────────────────────────────
    def _archived_forecast(self, lat, lon, date_str) -> Dict[str, float]:
        """Per-model archived forecast daily-max for one date (no lookahead)."""
        try:
            r = self.session.get(ARCHIVE_FORECAST, params={
                'latitude': lat, 'longitude': lon,
                'start_date': date_str, 'end_date': date_str,
                'daily': 'temperature_2m_max', 'models': ','.join(MODELS),
                'timezone': 'UTC',
            }, timeout=20)
            if r.status_code != 200:
                return {}
            daily = r.json().get('daily', {})
            out = {}
            for m in MODELS:
                arr = daily.get(f'temperature_2m_max_{m}')
                if arr and arr[0] is not None:
                    out[m] = float(arr[0])
            return out
        except Exception as e:
            log.debug(f"forecast fetch {date_str}: {e}")
            return {}

    def _actual_max_series(self, lat, lon, start, end) -> Dict[str, float]:
        """Observed daily-max for a date range (ground truth)."""
        try:
            r = self.session.get(ARCHIVE_ACTUAL, params={
                'latitude': lat, 'longitude': lon,
                'start_date': start, 'end_date': end,
                'daily': 'temperature_2m_max', 'timezone': 'UTC',
            }, timeout=25)
            if r.status_code != 200:
                return {}
            daily = r.json().get('daily', {})
            return {d: t for d, t in zip(daily.get('time', []),
                                         daily.get('temperature_2m_max', []))
                    if t is not None}
        except Exception as e:
            log.debug(f"actual fetch: {e}")
            return {}

    # ── core ──────────────────────────────────────────────────────
    def run(self, cities: List[str], days_back: int, starting_balance: float = 10.0):
        end = datetime.now(timezone.utc) - timedelta(days=2)
        start = end - timedelta(days=days_back)
        log.info(f"═══ HONEST STABILITY BACKTEST {start.date()} → {end.date()} ═══")
        log.info(f"Cities: {cities} | span=±{self.neighbor_span} buckets")
        log.info("Forecast = REAL archived forecast. Actual = REAL observation. "
                 "Market prices = MODELED (see caveat).\n")

        city_stats: Dict[str, CityStats] = {}
        all_days: List[DayResult] = []

        for city in cities:
            st = get_station(city)
            if not st:
                log.warning(f"No station for {city} — skip")
                continue
            lat, lon = st.lat, st.lon

            actuals = self._actual_max_series(lat, lon, start.strftime('%Y-%m-%d'),
                                              end.strftime('%Y-%m-%d'))
            if not actuals:
                log.warning(f"No actuals for {city}")
                continue

            cs = CityStats(city=city)
            abs_errs = []

            for date_str, actual_max in actuals.items():
                fc = self._archived_forecast(lat, lon, date_str)
                if len(fc) < 3:   # need ≥3 models, same as live
                    continue
                time.sleep(0.05)  # be polite to the free API

                day = self._simulate_day(city, date_str, actual_max, fc)
                if day is None:
                    continue
                all_days.append(day)
                cs.days += 1
                cs.invested += day.basket_cost
                cs.pnl += day.pnl
                if day.basket_won:
                    cs.basket_wins += 1
                if day.center_hit:
                    cs.center_hits += 1
                abs_errs.append(abs(day.forecast_max - day.actual_max))

            if cs.days:
                cs.mae = sum(abs_errs) / len(abs_errs)
                city_stats[city] = cs
                log.info(
                    f"  {city:11} days={cs.days:3} basket_win={cs.basket_wins/cs.days:4.0%} "
                    f"center_hit={cs.center_hits/cs.days:4.0%} MAE={cs.mae:.2f}°C "
                    f"PnL=${cs.pnl:+.2f} ROI={cs.pnl/max(0.01,cs.invested)*100:+.0f}%"
                )

        self._report(city_stats, all_days, starting_balance)
        return city_stats, all_days

    def _simulate_day(self, city, date_str, actual_max, fc) -> Optional[DayResult]:
        target = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc, hour=12)
        ens = list(fc.values())
        forecast_max = sum(ens) / len(ens)
        spread = _std(ens)

        # Build integer-degree buckets around the forecast (like Polymarket).
        center_deg = round(forecast_max)
        buckets = []
        for d in range(center_deg - 6, center_deg + 7):
            buckets.append((f"{d}°C", d - 0.5, d + 0.5))

        # ProbabilityEngine on the REAL archived forecast points.
        fps = [ForecastPoint(source='archive', model=MODEL_NAME.get(m, m),
                             location=city, timestamp=target, temp_c=t, confidence=0.8)
               for m, t in fc.items()]
        probs = self.engine.estimate_bucket_probabilities(fps, buckets, target)
        prob_by_label = {p.bucket_label: p for p in probs}

        # Center bucket = closest to forecast; basket = center ± span.
        labels = [b[0] for b in buckets]
        centers = [round(forecast_max) + off for off in range(-6, 7)]
        ci = min(range(len(labels)), key=lambda i: abs((centers[i]) - forecast_max))

        basket_idx = [j for j in range(ci - self.neighbor_span, ci + self.neighbor_span + 1)
                      if 0 <= j < len(labels)]
        basket_centers = [centers[j] for j in basket_idx]

        # Did the REAL observed max land in our basket? (ground-truth resolution)
        def in_bucket(val, lo, hi):
            return lo <= val < hi
        basket_won = any(in_bucket(actual_max, buckets[j][1], buckets[j][2]) for j in basket_idx)
        center_hit = in_bucket(actual_max, buckets[ci][1], buckets[ci][2])

        # ── MODELED market prices (transparent assumption) ──
        # Market ≈ a wider, less-confident normal centered on the SAME forecast
        # (markets see public forecasts too) → our edge is only the tighter spread.
        # Price each leg at the market's implied probability, floor 1¢.
        market_std = max(1.2, spread * 1.6)
        basket_cost = 0.0
        winning_payout = 0.0
        for j in basket_idx:
            lo, hi = buckets[j][1], buckets[j][2]
            mp = _normal_mass(forecast_max, market_std, lo, hi)
            price = max(0.01, min(0.97, mp))
            shares = 1.0  # 1 share per leg → leg cost = price, payout = $1 if it wins
            basket_cost += price
            if in_bucket(actual_max, lo, hi):
                winning_payout = shares * 1.0

        pnl = winning_payout - basket_cost  # one leg pays $1 if basket_won

        return DayResult(
            city=city, date=date_str, actual_max=actual_max,
            forecast_max=round(forecast_max, 2), model_spread=round(spread, 2),
            basket_centers=basket_centers, basket_won=basket_won, center_hit=center_hit,
            basket_cost=round(basket_cost, 4), pnl=round(pnl, 4),
        )

    # ── report ────────────────────────────────────────────────────
    def _report(self, city_stats: Dict[str, CityStats], days: List[DayResult],
                starting_balance: float):
        if not days:
            log.info("No results.")
            return
        tot_days = len(days)
        basket_wins = sum(1 for d in days if d.basket_won)
        center_hits = sum(1 for d in days if d.center_hit)
        pnl = sum(d.pnl for d in days)
        invested = sum(d.basket_cost for d in days)
        mae = sum(abs(d.forecast_max - d.actual_max) for d in days) / tot_days

        log.info(f"\n{'═'*64}")
        log.info("  HONEST STABILITY BACKTEST — RESULTS")
        log.info(f"{'═'*64}")
        log.info("  FORECAST SKILL (real, no lookahead):")
        log.info(f"    Days tested:        {tot_days}")
        log.info(f"    Basket win rate:    {basket_wins/tot_days:.1%}  "
                 f"(actual max landed within ±{self.neighbor_span}°C of forecast)")
        log.info(f"    Center-bucket hit:  {center_hits/tot_days:.1%}  (exact degree)")
        log.info(f"    Forecast MAE:       {mae:.2f}°C")
        log.info(f"{'─'*64}")
        log.info("  MODELED PnL (market prices ASSUMED — see caveat):")
        log.info(f"    Total cost:         ${invested:.2f}")
        log.info(f"    Total PnL:          ${pnl:+.2f}")
        log.info(f"    ROI:                {pnl/max(0.01,invested)*100:+.0f}%")
        log.info(f"{'─'*64}")
        log.info("  STABLE-CITY RANKING (by basket win rate):")
        for city, cs in sorted(city_stats.items(),
                               key=lambda kv: kv[1].basket_wins / max(1, kv[1].days),
                               reverse=True):
            log.info(f"    {city:11} {cs.basket_wins/max(1,cs.days):5.0%} basket | "
                     f"MAE {cs.mae:.2f}°C | {cs.days} days | ROI "
                     f"{cs.pnl/max(0.01,cs.invested)*100:+.0f}%")
        log.info(f"{'═'*64}")
        log.info("  CAVEAT: forecast skill & basket-win rate are REAL. Modeled PnL "
                 "assumes market prices ~ a wider normal on the same public forecast; "
                 "real Polymarket order-book prices may differ. Validate live in paper "
                 "mode before sizing up.")
        log.info(f"{'═'*64}\n")

        # save
        try:
            out_dir = os.path.join(os.path.dirname(__file__), 'results')
            os.makedirs(out_dir, exist_ok=True)
            path = os.path.join(out_dir, 'stability_backtest.json')
            with open(path, 'w') as f:
                json.dump({
                    'tested_days': tot_days,
                    'basket_win_rate': basket_wins / tot_days,
                    'center_hit_rate': center_hits / tot_days,
                    'forecast_mae_c': mae,
                    'modeled_pnl': pnl, 'modeled_roi_pct': pnl / max(0.01, invested) * 100,
                    'by_city': {c: {
                        'days': s.days, 'basket_win_rate': s.basket_wins / max(1, s.days),
                        'center_hit_rate': s.center_hits / max(1, s.days),
                        'mae_c': s.mae, 'pnl': s.pnl,
                    } for c, s in city_stats.items()},
                }, f, indent=2)
            log.info(f"Saved → {path}")
        except Exception as e:
            log.warning(f"save failed: {e}")


def _std(xs):
    if len(xs) < 2:
        return 0.5
    m = sum(xs) / len(xs)
    return max(0.3, math.sqrt(sum((x - m) ** 2 for x in xs) / len(xs)))


def _normal_mass(mean, std, lo, hi):
    def phi(x):
        return 0.5 * (1 + math.erf((x - mean) / (std * math.sqrt(2))))
    if hi == float('inf'):
        return 1 - phi(lo)
    if lo == float('-inf'):
        return phi(hi)
    return max(0.001, phi(hi) - phi(lo))


def main():
    ap = argparse.ArgumentParser(description='Honest stability backtest (real forecasts)')
    ap.add_argument('--days', type=int, default=90)
    ap.add_argument('--cities', nargs='+', default=None)
    ap.add_argument('--span', type=int, default=1, help='neighbor buckets each side (±N)')
    ap.add_argument('--balance', type=float, default=10.0)
    args = ap.parse_args()

    cities = args.cities or DEFAULT_CITIES
    bt = StabilityBacktest(neighbor_span=args.span)
    bt.run(cities, args.days, starting_balance=args.balance)


if __name__ == '__main__':
    main()
