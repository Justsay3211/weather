"""
Market Scanner — Discover active weather markets on Polymarket.

CONFIRMED SLUG PATTERN (from reference wallet research):
- highest-temperature-in-{city}-on-{month}-{day}-{year}
- lowest-temperature-in-{city}-on-{month}-{day}-{year}

Each event has 11 markets (temperature buckets).
Cities: Houston, Lucknow, Seoul, Tokyo, London, Taipei, Hong Kong, Beijing, Ankara, etc.

NOTE (overhaul): bucket-bound parsing now delegates to data.bucket_parse, a
mojibake-hardened parser, and each outcome now carries BOTH sides of the
binary market (YES + NO token id and price) so observation-driven strategies
can trade the NO leg of a dead bucket.
"""

import json
import time
import requests
from typing import Dict, List, Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

from config import Config
from logger import log
from data.bucket_parse import parse_bucket_bounds


@dataclass
class WeatherMarket:
    """A discovered weather market on Polymarket."""
    event_id: str
    title: str
    description: str
    city: str
    country: str
    market_type: str          # 'highest_temperature', 'lowest_temperature'
    resolution_time: Optional[datetime]
    outcomes: List[Dict]      # [{label, token_id, token_id_no, price, price_no, bucket_low, bucket_high}]
    active: bool
    volume: float
    liquidity: float
    slug: str
    raw: Dict = field(default_factory=dict)
    measurement_date: Optional[datetime] = None  # the LOCAL calendar day the high/low is measured (from slug)


# Cities that have active weather markets on Polymarket.
# Asian cities are heavily traded by the reference 90%-win wallets, so we cover
# the stable, airport-station-backed ones (see data/weather_stations.py).
MARKET_CITIES = {
    'houston': ('Houston', 'USA', 'F'),
    'new-york-city': ('New York City', 'USA', 'F'),
    'lucknow': ('Lucknow', 'India', 'C'),
    'seoul': ('Seoul', 'South Korea', 'C'),
    'tokyo': ('Tokyo', 'Japan', 'C'),
    'london': ('London', 'UK', 'C'),
    'taipei': ('Taipei', 'Taiwan', 'C'),
    'hong-kong': ('Hong Kong', 'China', 'C'),
    'beijing': ('Beijing', 'China', 'C'),
    'ankara': ('Ankara', 'Turkey', 'C'),
    'singapore': ('Singapore', 'Singapore', 'C'),
    'wellington': ('Wellington', 'New Zealand', 'C'),
    'cape-town': ('Cape Town', 'South Africa', 'C'),
    'buenos-aires': ('Buenos Aires', 'Argentina', 'C'),
    'atlanta': ('Atlanta', 'USA', 'F'),
    'chicago': ('Chicago', 'USA', 'F'),
    'seattle': ('Seattle', 'USA', 'F'),
    'lagos': ('Lagos', 'Nigeria', 'C'),
    'madrid': ('Madrid', 'Spain', 'C'),
    'paris': ('Paris', 'France', 'C'),
    'mumbai': ('Mumbai', 'India', 'C'),
    'dubai': ('Dubai', 'UAE', 'C'),
    'sydney': ('Sydney', 'Australia', 'C'),
    'austin': ('Austin', 'USA', 'F'),
    'moscow': ('Moscow', 'Russia', 'C'),
    'new-york': ('New York City', 'USA', 'F'),
    # ═══ Asian markets (stable, airport-station-backed) ═══
    'delhi': ('Delhi', 'India', 'C'),
    'bangkok': ('Bangkok', 'Thailand', 'C'),
    'shanghai': ('Shanghai', 'China', 'C'),
    'osaka': ('Osaka', 'Japan', 'C'),
    'jakarta': ('Jakarta', 'Indonesia', 'C'),
    'manila': ('Manila', 'Philippines', 'C'),
    'kuala-lumpur': ('Kuala Lumpur', 'Malaysia', 'C'),
}


class MarketScanner:
    """Scan Polymarket for active weather markets — optimized for speed."""

    def __init__(self):
        self.base_url = Config.GAMMA_API_URL
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': f'WeatherSniper/{Config.VERSION}',
            'Accept': 'application/json',
            'Connection': 'keep-alive',
        })
        # Connection pooling for speed
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10, pool_maxsize=20, max_retries=1
        )
        self.session.mount('https://', adapter)
        self._cache: Dict[str, Tuple[float, List[WeatherMarket]]] = {}
        self._cache_ttl = 30.0

    def scan_weather_markets(self, days_ahead: int = 3) -> List[WeatherMarket]:
        """
        Discover all active weather markets using confirmed slug pattern.
        Uses parallel fetching for speed (ThreadPoolExecutor).
        """
        cache_key = f'weather_scan_{days_ahead}'
        now = time.time()
        if cache_key in self._cache:
            cached_time, cached = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                return cached

        from concurrent.futures import ThreadPoolExecutor, as_completed

        markets = []
        today = datetime.now(timezone.utc)

        # Build all slug tasks
        tasks = []
        for day_offset in range(0, days_ahead + 1):
            target = today + timedelta(days=day_offset)
            month_str = target.strftime('%B').lower()
            day_str = str(target.day)
            year_str = str(target.year)

            for city_slug, (city_name, country, temp_unit) in MARKET_CITIES.items():
                for temp_type in ['highest-temperature', 'lowest-temperature']:
                    slug = f'{temp_type}-in-{city_slug}-on-{month_str}-{day_str}-{year_str}'
                    tasks.append((slug, city_name, country, temp_type, temp_unit, target))

        # Parallel fetch (10 workers for speed)
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {
                executor.submit(self._fetch_by_slug, *task): task
                for task in tasks
            }
            for future in as_completed(futures):
                try:
                    market = future.result()
                    if market:
                        markets.append(market)
                except Exception:
                    pass

        markets.sort(key=lambda m: (m.resolution_time or datetime.max.replace(tzinfo=timezone.utc), -m.volume))
        self._cache[cache_key] = (now, markets)
        log.info(f"Found {len(markets)} active weather markets ({days_ahead}d ahead, {len(tasks)} checked)")
        return markets

    def _fetch_by_slug(self, slug: str, city: str, country: str,
                       temp_type: str, temp_unit: str,
                       target_date: datetime) -> Optional[WeatherMarket]:
        """Fetch a single market event by its slug."""
        resp = self.session.get(
            f"{self.base_url}/events",
            params={'slug': slug},
            timeout=8,
        )
        if resp.status_code != 200:
            return None

        data = resp.json()
        events = data if isinstance(data, list) else data.get('events', [])
        if not events:
            return None

        event = events[0]
        if event.get('slug') != slug:
            return None

        # Must be active and not closed
        if event.get('closed') or not event.get('active', True):
            return None

        # Parse outcomes
        outcomes = []
        for m in event.get('markets', []):
            outcome = self._parse_outcome(m, temp_unit)
            if outcome:
                outcomes.append(outcome)

        if not outcomes:
            return None

        # Resolution time
        end_str = event.get('endDate') or event.get('end_date_iso')
        resolution_time = None
        if end_str:
            try:
                resolution_time = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
            except Exception:
                resolution_time = target_date.replace(hour=23, minute=59)

        mtype = 'highest_temperature' if 'highest' in temp_type else 'lowest_temperature'

        return WeatherMarket(
            event_id=str(event.get('id', '')),
            title=event.get('title', ''),
            description=event.get('description', ''),
            city=city,
            country=country,
            market_type=mtype,
            resolution_time=resolution_time,
            outcomes=outcomes,
            active=True,
            volume=float(event.get('volume', 0) or 0),
            liquidity=float(event.get('liquidity', 0) or 0),
            slug=slug,
            raw=event,
            measurement_date=target_date,  # the LOCAL day the slug names (e.g. june-3)
        )

    def _parse_outcome(self, market: Dict, temp_unit: str = 'C') -> Optional[Dict]:
        """Parse a market into an outcome dict with temperature bounds.

        Carries BOTH legs of the binary market: the YES token/price AND the NO
        token/price (clobTokenIds[1] / outcomePrices[1]). The NO leg lets the
        late-observed strategy bet *against* buckets the observed data has ruled
        out, which is its own tradeable token with its own price.
        """
        question = market.get('question', '') or market.get('groupItemTitle', '')

        raw_ids = market.get('clobTokenIds', '[]')
        if isinstance(raw_ids, str):
            try:
                clob_ids = json.loads(raw_ids)
            except Exception:
                clob_ids = []
        else:
            clob_ids = raw_ids if isinstance(raw_ids, list) else []

        if not clob_ids:
            return None

        # Parse prices
        raw_prices = market.get('outcomePrices', '[]')
        if isinstance(raw_prices, str):
            try:
                prices = json.loads(raw_prices)
            except Exception:
                prices = [0.5, 0.5]
        else:
            prices = raw_prices if isinstance(raw_prices, list) else [0.5, 0.5]

        yes_price = float(prices[0]) if prices else 0.5
        # NO price: prefer the explicit second outcome price, else complement.
        if len(prices) > 1 and prices[1] is not None:
            try:
                no_price = float(prices[1])
            except (TypeError, ValueError):
                no_price = max(0.0, 1.0 - yes_price)
        else:
            no_price = max(0.0, 1.0 - yes_price)

        bucket_lo, bucket_hi = parse_bucket_bounds(question, temp_unit)

        return {
            'label': question,
            'token_id': clob_ids[0],  # YES token
            'token_id_no': clob_ids[1] if len(clob_ids) > 1 else None,
            'price': yes_price,
            'price_no': no_price,
            'bucket_low': bucket_lo,
            'bucket_high': bucket_hi,
            'market_id': market.get('id', ''),
            'condition_id': market.get('conditionId', ''),
        }

    def _parse_bucket_bounds(self, text: str, temp_unit: str = 'C') -> Tuple[float, float]:
        """Backwards-compatible shim around the hardened bucket parser.

        Kept so any external callers / tests that referenced this method keep
        working; the real logic now lives in data.bucket_parse.
        """
        return parse_bucket_bounds(text, temp_unit)

    def get_outcome_prices(self, market: WeatherMarket) -> Dict[str, float]:
        """Fetch live YES prices for all outcomes."""
        prices = {}
        for outcome in market.outcomes:
            token_id = outcome.get('token_id')
            if not token_id:
                continue
            try:
                resp = self.session.get(
                    f"{Config.CLOB_API_URL}/price",
                    params={'token_id': token_id, 'side': 'BUY'},
                    timeout=5,
                )
                if resp.status_code == 200:
                    price = float(resp.json().get('price', 0))
                    prices[outcome['label']] = price
                    outcome['price'] = price
                    # keep the NO price coherent with the refreshed YES price
                    outcome['price_no'] = max(0.0, 1.0 - price)
                else:
                    prices[outcome['label']] = outcome.get('price', 0.5)
            except Exception:
                prices[outcome['label']] = outcome.get('price', 0.5)
        return prices

    def get_reference_positions(self, wallet: str) -> List[Dict]:
        """Get positions for a reference trader wallet."""
        try:
            resp = self.session.get(
                'https://data-api.polymarket.com/positions',
                params={'user': wallet},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            log.warning(f"Failed to fetch positions for {wallet[:10]}: {e}")
        return []

    def get_reference_trades(self, wallet: str, limit: int = 50) -> List[Dict]:
        """Get recent trades for a reference trader wallet."""
        try:
            resp = self.session.get(
                'https://data-api.polymarket.com/trades',
                params={'user': wallet, 'limit': limit},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json() if isinstance(resp.json(), list) else []
        except Exception as e:
            log.warning(f"Failed to fetch trades for {wallet[:10]}: {e}")
        return []
