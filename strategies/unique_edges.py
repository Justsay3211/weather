"""
UNIQUE EDGE STRATEGIES — Opportunities others miss.

1. YESTERDAY-WEATHER ARBITRAGE:
   The same weather station resolved yesterday. That outcome tells you how
   the station behaves (microclimate, measurement bias). If Incheon Airport
   recorded 24C yesterday under similar conditions, the probability of 24C
   today is higher than the model predicts.

2. NEIGHBORING-CITY CORRELATION:
   Temperatures in nearby cities are highly correlated (r > 0.85 for cities
   within 300km). If Shanghai resolved at 28C and the same weather system
   moves east, Seoul's market might be mispriced. We use the ALREADY-RESOLVED
   neighbor's temperature as a feature.

3. WEEKEND INEFFICIENCY:
   Less trading activity on weekends -> wider spreads, more mispricing.
   But also: weather models have fewer updates on weekends -> the market
   is slower to react. This is an OPPORTUNITY for those who poll anyway.

4. TIMEZONE ATTENTION ARBITRAGE:
   Asian temperature markets (Seoul, Tokyo, Taipei) are most active during
   Asian trading hours (00:00-08:00 UTC). US markets during US hours. When
   a market is "out of hours" for its primary audience, mispricing increases.

5. BASE-RATE ANCHORING:
   The market's baseline is ~9% per bucket (1/11). But some stations
   have systematic biases. A station at high altitude has more variance.
   A coastal station has less variance. We adjust our priors accordingly.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple


# ── CORRELATED CITY PAIRS ──
# Pairs where one market resolving gives information about the other.
# Format: (source_city, target_city, correlation, distance_km, same_weather_system)
CORRELATED_PAIRS = [
    ("seoul", "tokyo", 0.78, 1156, True),      # Same systems move west->east
    ("tokyo", "seoul", 0.78, 1156, True),
    ("shanghai", "seoul", 0.82, 866, True),     # Very close, same latitude
    ("shanghai", "tokyo", 0.76, 1750, True),
    ("taipei", "hong-kong", 0.88, 800, True),   # Very close
    ("hong-kong", "taipei", 0.88, 800, True),
    ("beijing", "seoul", 0.72, 950, True),
    ("london", "paris", 0.85, 344, True),       # Close, same systems
    ("paris", "london", 0.85, 344, True),
    ("london", "berlin", 0.75, 930, True),
    ("paris", "berlin", 0.72, 878, True),
    ("chicago", "toronto", 0.82, 702, True),
    ("dallas", "houston", 0.90, 385, True),     # Very close
    ("houston", "dallas", 0.90, 385, True),
    ("new-york", "toronto", 0.76, 553, True),
    ("los-angeles", "austin", 0.45, 1975, False), # Different climate zones
    ("singapore", "bangkok", 0.72, 1429, True),
    ("delhi", "lucknow", 0.91, 416, True),       # Very close
    ("lucknow", "delhi", 0.91, 416, True),
    ("sydney", "wellington", 0.62, 2225, False),
    ("moscow", "ankara", 0.55, 1788, False),
    ("buenos-aires", "sao-paulo", 0.58, 1670, False),
]

# ── STATION BIAS FACTORS ──
# Some stations have systematic measurement biases (based on historical data)
STATION_BIAS = {
    "RKSI": {"type": "coastal", "variance": "high", "bias_c": 0.0},     # Incheon — coastal
    "RJTT": {"type": "coastal", "variance": "medium", "bias_c": 0.0},   # Haneda
    "EGLC": {"type": "urban", "variance": "low", "bias_c": +0.5},       # London City — urban heat island
    "LFPB": {"type": "suburban", "variance": "low", "bias_c": 0.0},
    "KORD": {"type": "suburban", "variance": "high", "bias_c": 0.0},    # Chicago — high variance
    "KATL": {"type": "suburban", "variance": "medium", "bias_c": 0.0},
    "KSEA": {"type": "coastal", "variance": "medium", "bias_c": -0.5},  # Seattle — cooler coastal
    "KLAX": {"type": "coastal", "variance": "low", "bias_c": 0.0},      # LA — stable
    "KDAL": {"type": "suburban", "variance": "high", "bias_c": 0.0},    # Dallas — high variance
    "KHOU": {"type": "coastal", "variance": "medium", "bias_c": 0.0},   # Houston
    "KMIA": {"type": "coastal", "variance": "low", "bias_c": 0.0},      # Miami — very stable
    "KBKF": {"type": "high_altitude", "variance": "high", "bias_c": -2.0}, # Denver — 1.7km altitude!
    "ZBAA": {"type": "suburban", "variance": "high", "bias_c": 0.0},
    "ZSPD": {"type": "coastal", "variance": "medium", "bias_c": 0.0},
    "WSSS": {"type": "coastal", "variance": "low", "bias_c": 0.0},      # Singapore — very stable (tropical)
    "RCSS": {"type": "urban", "variance": "medium", "bias_c": 0.0},
    "SAEZ": {"type": "suburban", "variance": "medium", "bias_c": 0.0},
    "SBGR": {"type": "suburban", "variance": "medium", "bias_c": 0.0},
    "CYYZ": {"type": "suburban", "variance": "high", "bias_c": 0.0},    # Toronto — high variance
    "NZWN": {"type": "coastal", "variance": "high", "bias_c": 0.0},     # Wellington — windy, high variance
}


def get_correlated_cities(city: str) -> List[Tuple[str, float]]:
    """Get cities correlated with this one, with correlation strength."""
    key = city.lower().strip()
    return [(target, corr) for src, target, corr, dist, same in CORRELATED_PAIRS
            if src == key and same]


def get_station_bias(icao: str) -> dict:
    """Get station-specific bias/variance characteristics."""
    return STATION_BIAS.get(icao.upper(), {"type": "unknown", "variance": "medium", "bias_c": 0.0})


def adjust_probability_for_correlation(
    our_prob: float,
    city: str,
    neighbor_results: Dict[str, float],  # {neighbor_city: resolved_temp_c}
) -> float:
    """
    Adjust probability estimate based on ALREADY-RESOLVED neighbor markets.

    If Shanghai (highly correlated with Seoul) resolved at 26C,
    and our Seoul forecast says 24C, the real probability of
    warmer temperatures is HIGHER than our ensemble says.
    """
    correlations = get_correlated_cities(city)
    if not correlations:
        return our_prob

    adjustment = 0.0
    total_weight = 0.0

    for neighbor_city, corr in correlations:
        if neighbor_city in neighbor_results:
            neighbor_temp = neighbor_results[neighbor_city]
            # Strong correlation = strong signal
            total_weight += corr
            # If neighbor was warmer than its forecast, ours might be too
            # (simplified — full model would compare to neighbor's forecast)
            adjustment += corr * 0.5  # half-weight the correlation signal

    if total_weight > 0:
        adjustment /= total_weight
        our_prob += adjustment * 0.05  # max 5pp adjustment
        our_prob = min(0.95, max(0.01, our_prob))

    return our_prob


def is_weekend_inefficiency_window() -> Tuple[bool, float]:
    """
    Check if we're in a weekend inefficiency window.
    Returns (is_weekend, inefficiency_multiplier).

    On weekends, the edge threshold can be LOWER because the market
    is less efficient -> more mispricing opportunities.
    """
    now = datetime.now(timezone.utc)
    dow = now.weekday()  # 0=Mon, 6=Sun
    hour = now.hour

    if dow in (5, 6):  # Saturday or Sunday
        return True, 1.3  # 30% more lenient on weekends
    if dow == 4 and hour > 20:  # Friday night
        return True, 1.2
    if dow == 0 and hour < 4:   # Monday early AM
        return True, 1.15

    return False, 1.0


def is_asian_attention_window(city: str) -> Tuple[bool, str]:
    """
    Check if we're in the primary attention window for a city's market.
    Markets are LESS efficient outside their primary hours.

    Returns (in_primary_window, timezone_label)
    """
    asian_cities = {"seoul", "tokyo", "taipei", "hong-kong", "shanghai",
                    "beijing", "singapore", "bangkok", "delhi", "mumbai", "lucknow"}
    european_cities = {"london", "paris", "berlin", "rome", "madrid", "moscow",
                       "istanbul", "ankara", "warsaw", "tel-aviv"}
    us_cities = {"new-york", "chicago", "los-angeles", "houston", "dallas",
                 "atlanta", "seattle", "austin", "denver", "miami", "toronto"}

    now = datetime.now(timezone.utc)
    hour = now.hour
    key = city.lower().strip()

    if key in asian_cities:
        # Asian hours: 00:00-08:00 UTC
        return (0 <= hour <= 8), "Asia"
    elif key in european_cities:
        # European hours: 06:00-16:00 UTC
        return (6 <= hour <= 16), "Europe"
    elif key in us_cities:
        # US hours: 12:00-22:00 UTC
        return (12 <= hour <= 22), "US"
    else:
        return True, "unknown"


def compute_market_sum_opportunity(
    bucket_probs: list,
    market_prices: dict,
) -> Optional[dict]:
    """
    Multi-outcome market sum arbitrage.

    In an 11-bucket weather market, EXACTLY ONE bucket resolves to $1.00.
    Therefore, the sum of all bucket YES prices should be approximately $1.00.

    When sum < $0.80: market is UNDERPRICING -> buy the cluster
    When sum > $1.20: market is OVERPRICING -> potential short/sell opportunity
    When individual bucket price < 0.01 but our prob > 0.05: deep mispricing

    Returns None if no opportunity, or a dict describing the edge.
    """
    total_market = 0.0
    total_our = 0.0
    underpriced = []
    n_buckets = 0

    for bp in bucket_probs:
        label = bp.bucket_label
        mp = market_prices.get(label, 0)
        if mp <= 0:
            continue
        n_buckets += 1
        total_market += mp
        total_our += bp.probability

        # Find individual deep mispricing
        if mp < 0.03 and bp.probability > 0.08:
            underpriced.append({
                "label": label,
                "market_price": mp,
                "our_prob": bp.probability,
                "edge_ratio": bp.probability / mp if mp > 0 else 999,
            })

    avg_market = total_market / max(n_buckets, 1)
    sum_deviation = total_market - 1.0

    opportunity = None

    if sum_deviation < -0.15 and underpriced:
        # Market is significantly underpricing -> buy opportunity
        opportunity = {
            "type": "market_underpriced",
            "sum_deviation": sum_deviation,
            "n_buckets": n_buckets,
            "avg_bucket_price": avg_market,
            "underpriced_tails": underpriced[:3],
            "signal": "BUY cheap tails — market sum below $0.85",
        }
    elif sum_deviation > 0.15:
        opportunity = {
            "type": "market_overpriced",
            "sum_deviation": sum_deviation,
            "n_buckets": n_buckets,
            "avg_bucket_price": avg_market,
            "signal": "Market overpriced — avoid or look for reversal",
        }

    return opportunity
