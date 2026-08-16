"""
Probability Engine — Convert multi-source forecasts into bucket probabilities.

Takes forecast points from multiple weather models and produces a probability
distribution over temperature/weather buckets that Polymarket uses.

Key insight: Polymarket weather markets have outcomes like:
  "Tokyo high temp: 24°C", "25°C", "26°C", "27°C or higher"
We estimate P(temp in bucket) using ensemble of forecasts.
"""

import math
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
from datetime import datetime, timezone

from data.weather_fetcher import ForecastPoint
from logger import log


@dataclass
class BucketProbability:
    """Probability estimate for a market outcome bucket."""
    bucket_label: str      # e.g. "25°C", "26°C or higher"
    bucket_low: float      # lower bound (inclusive)
    bucket_high: float     # upper bound (exclusive), inf for "X or higher"
    probability: float     # our estimated probability (0-1)
    confidence: float      # how confident we are in this estimate
    n_models: int          # number of models contributing
    mean_forecast: float   # weighted mean temperature
    std_forecast: float    # standard deviation across models


class ProbabilityEngine:
    """
    Ensemble probability estimation.
    
    Approach:
    1. Weight forecasts by model confidence/accuracy
    2. Compute weighted mean + std of temperature
    3. Assume normal distribution around mean
    4. Calculate P(temp in each bucket) from CDF
    """

    def __init__(self):
        # Model confidence weights (higher = more trusted)
        self.model_weights = {
            'ECMWF': 0.95,
            'ECMWF_IFS04': 0.95,
            'GFS': 0.80,
            'ICON': 0.85,
            'JMA': 0.82,
            'GEM': 0.75,
            'OWM': 0.70,
            'NWS': 0.80,
            'HRRR': 0.88,
            'UKMO': 0.83,
            # Commercial multi-model blends (WeatherAPI.com / Visual Crossing).
            # Treated as strong, independent ensemble members.
            'WAPI': 0.80,
            'VC': 0.80,
        }


    def estimate_bucket_probabilities(
        self,
        forecasts: List[ForecastPoint],
        buckets: List[Tuple[str, float, float]],
        target_time: datetime = None,
        market_type: str = None,
    ) -> List[BucketProbability]:
        """
        Estimate probability for each temperature bucket.
        
        Args:
            forecasts: List of ForecastPoint from multiple models
            buckets: List of (label, low_bound, high_bound) for each outcome
                     e.g. [("24°C", 23.5, 24.5), ("25°C", 24.5, 25.5), ...]
            target_time: Target resolution time (filter forecasts)
            market_type: e.g. 'highest_temperature' / 'lowest_temperature'.
                When provided, the ensemble keys off each model's daily MAX
                (high markets) or daily MIN (low markets) instead of a single
                hourly reading — a much closer match to how Polymarket resolves.
        
        Returns:
            List of BucketProbability for each bucket
        """
        if not forecasts:
            log.warning("No forecasts available for probability estimation")
            return [BucketProbability(
                bucket_label=label, bucket_low=lo, bucket_high=hi,
                probability=1.0 / len(buckets), confidence=0.0,
                n_models=0, mean_forecast=0, std_forecast=999
            ) for label, lo, hi in buckets]

        # Filter forecasts by target time if specified
        relevant = forecasts
        if target_time:
            relevant = [
                f for f in forecasts
                if abs((f.timestamp - target_time).total_seconds()) < 7200
            ]
            if not relevant:
                relevant = forecasts  # fallback to all

        # Choose which temperature field drives the ensemble. "Highest temp"
        # markets resolve on the day's MAX, "lowest temp" on the day's MIN; use
        # those when the forecaster supplied them, else fall back to the hourly
        # temp_c (handled per-point in _weighted_ensemble).
        mt = (market_type or '').lower()
        if 'low' in mt or 'min' in mt:
            temp_field = 'temp_min_c'
        elif 'high' in mt or 'max' in mt:
            temp_field = 'temp_max_c'
        else:
            temp_field = None

        # Compute weighted mean and std
        mean_temp, std_temp, n_models = self._weighted_ensemble(relevant, temp_field)

        # Calculate probability for each bucket using normal CDF
        results = []
        for label, lo, hi in buckets:
            prob = self._normal_prob(mean_temp, std_temp, lo, hi)
            # Confidence based on model agreement, count, and ensemble spread
            # Higher spread = lower confidence (models disagree)
            spread_confidence = max(0.1, 1.0 - std_temp / 5.0)  # was /10, too lenient
            model_count_confidence = min(1.0, n_models * 0.2)    # was 0.15, too conservative
            confidence = spread_confidence * model_count_confidence * 0.95
            confidence = max(0.10, min(0.95, confidence))  # floor 10%, ceiling 95%

            results.append(BucketProbability(
                bucket_label=label,
                bucket_low=lo,
                bucket_high=hi,
                probability=prob,
                confidence=confidence,
                n_models=n_models,
                mean_forecast=mean_temp,
                std_forecast=std_temp,
            ))

        # Normalize probabilities to sum to 1
        total = sum(r.probability for r in results)
        if total > 0:
            for r in results:
                r.probability /= total

        return results


    def _weighted_ensemble(self, forecasts: List[ForecastPoint],
                           temp_field: str = None) -> Tuple[float, float, int]:
        """
        Compute weighted mean and std from multiple model forecasts.
        Returns (mean_temp, std_temp, n_unique_models).

        When temp_field is set (e.g. 'temp_max_c'/'temp_min_c') each point
        contributes that field if present, otherwise it falls back to temp_c.
        """
        if not forecasts:
            return 20.0, 5.0, 0

        def _val(f: ForecastPoint) -> float:
            if temp_field:
                v = getattr(f, temp_field, None)
                if v is not None:
                    return v
            return f.temp_c

        # Group by model, take most recent from each
        model_temps: Dict[str, List[float]] = {}
        for f in forecasts:
            model_key = f"{f.source}_{f.model}"
            if model_key not in model_temps:
                model_temps[model_key] = []
            model_temps[model_key].append(_val(f))

        # Weighted mean across models
        total_weight = 0
        weighted_sum = 0
        temps_list = []

        for model_key, temps in model_temps.items():
            model_name = model_key.split('_', 1)[1] if '_' in model_key else model_key
            weight = self.model_weights.get(model_name.upper(), 0.7)
            avg_temp = sum(temps) / len(temps)
            weighted_sum += avg_temp * weight
            total_weight += weight
            temps_list.append(avg_temp)

        mean_temp = weighted_sum / total_weight if total_weight > 0 else 20.0
        n_models = len(model_temps)

        # Standard deviation across models
        if len(temps_list) >= 2:
            variance = sum((t - mean_temp) ** 2 for t in temps_list) / len(temps_list)
            std_temp = math.sqrt(variance)
            # Minimum std of 0.5°C (weather is never perfectly predictable)
            std_temp = max(0.5, std_temp)
        else:
            std_temp = 1.5  # single model → higher uncertainty

        return mean_temp, std_temp, n_models

    def _normal_prob(self, mean: float, std: float, lo: float, hi: float) -> float:
        """Calculate P(lo <= X < hi) for normal distribution N(mean, std²)."""
        if std <= 0:
            std = 0.5

        def phi(x):
            """Standard normal CDF approximation."""
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        if hi == float('inf'):
            return 1.0 - phi((lo - mean) / std)
        if lo == float('-inf'):
            return phi((hi - mean) / std)

        p_hi = phi((hi - mean) / std)
        p_lo = phi((lo - mean) / std)
        return max(0.001, p_hi - p_lo)  # floor at 0.1%

    def find_edge(
        self,
        bucket_probs: List[BucketProbability],
        market_prices: Dict[str, float],
    ) -> List[Dict]:
        """
        Find mispriced buckets where our probability > market price.
        
        Args:
            bucket_probs: Our estimated probabilities
            market_prices: Dict of bucket_label → market price (0-1)
        
        Returns:
            List of trading opportunities sorted by edge
        """
        opportunities = []

        for bp in bucket_probs:
            market_price = market_prices.get(bp.bucket_label, None)
            if market_price is None:
                continue

            edge = bp.probability - market_price
            # Require minimum edge AND confidence
            if edge > 0.05 and bp.confidence > 0.3:
                # Kelly sizing
                kelly_f = (edge * bp.confidence) / (1 - market_price) if market_price < 1 else 0
                kelly_f = min(kelly_f, 0.25)  # cap at 25%

                # Expected value
                ev = edge * (1.0 / market_price - 1) if market_price > 0 else 0

                opportunities.append({
                    'bucket_label': bp.bucket_label,
                    'our_prob': bp.probability,
                    'market_price': market_price,
                    'edge': edge,
                    'edge_pct': edge * 100,
                    'ev': ev,
                    'kelly_fraction': kelly_f,
                    'confidence': bp.confidence,
                    'n_models': bp.n_models,
                    'mean_forecast': bp.mean_forecast,
                    'std_forecast': bp.std_forecast,
                })

        # Sort by edge * confidence (best opportunities first)
        opportunities.sort(key=lambda x: x['edge'] * x['confidence'], reverse=True)
        return opportunities
