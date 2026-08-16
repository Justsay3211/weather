"""
Polymarket Weather Resolution Stations — the EDGE.

Every Polymarket weather market resolves to Wunderground data from a SPECIFIC
airport weather station — NOT the city center. Example: Seoul resolves to
Incheon Airport (RKSI), 30-50km from downtown Seoul. A forecast for "Seoul"
(city center) can differ by 1-3C from Incheon Airport.

THIS is the edge that Wallet1 and other profitable weather traders exploit:
they forecast the exact airport station, while retail uses generic city forecasts.

Station codes extracted from Gamma API resolutionSource field (June 2026).
Coordinates are the exact airport locations for Open-Meteo API queries.
"""

from dataclasses import dataclass


@dataclass
class WeatherStation:
    city: str
    station_name: str
    icao: str            # ICAO airport code (Wunderground identifier)
    lat: float           # exact airport latitude
    lon: float           # exact airport longitude
    wunderground_url: str


# Verified from Polymarket Gamma API resolutionSource field
STATIONS: dict[str, WeatherStation] = {
    "seoul": WeatherStation(
        city="Seoul", station_name="Incheon Intl Airport",
        icao="RKSI", lat=37.4691, lon=126.4505,
        wunderground_url="https://www.wunderground.com/history/daily/kr/incheon/RKSI",
    ),
    "tokyo": WeatherStation(
        city="Tokyo", station_name="Tokyo Haneda Airport",
        icao="RJTT", lat=35.5494, lon=139.7798,
        wunderground_url="https://www.wunderground.com/history/daily/jp/tokyo/RJTT",
    ),
    "london": WeatherStation(
        city="London", station_name="London City Airport",
        icao="EGLC", lat=51.5052, lon=0.0553,
        wunderground_url="https://www.wunderground.com/history/daily/gb/london/EGLC",
    ),
    "paris": WeatherStation(
        city="Paris", station_name="Paris-Le Bourget Airport",
        icao="LFPB", lat=48.9694, lon=2.4414,
        wunderground_url="https://www.wunderground.com/history/daily/fr/paris/LFPB",
    ),
    "madrid": WeatherStation(
        city="Madrid", station_name="Adolfo Suarez Madrid-Barajas Airport",
        icao="LEMD", lat=40.4719, lon=-3.5626,
        wunderground_url="https://www.wunderground.com/history/daily/es/madrid/LEMD",
    ),
    "ankara": WeatherStation(
        city="Ankara", station_name="Ankara Esenboga Airport",
        icao="LTAC", lat=40.1281, lon=32.9950,
        wunderground_url="https://www.wunderground.com/history/daily/tr/ankara/LTAC",
    ),
    "istanbul": WeatherStation(
        city="Istanbul", station_name="Istanbul Airport",
        icao="LTFM", lat=41.2611, lon=28.7420,
        wunderground_url="https://www.wunderground.com/history/daily/tr/istanbul/LTFM",
    ),
    "beijing": WeatherStation(
        city="Beijing", station_name="Beijing Capital Intl Airport",
        icao="ZBAA", lat=40.0799, lon=116.6031,
        wunderground_url="https://www.wunderground.com/history/daily/cn/beijing/ZBAA",
    ),
    "singapore": WeatherStation(
        city="Singapore", station_name="Singapore Changi Airport",
        icao="WSSS", lat=1.3644, lon=103.9915,
        wunderground_url="https://www.wunderground.com/history/daily/sg/singapore/WSSS",
    ),
    "hong-kong": WeatherStation(
        city="Hong Kong", station_name="Hong Kong Intl Airport",
        icao="VHHH", lat=22.3080, lon=113.9185,
        wunderground_url="https://www.wunderground.com/history/daily/hk/hong-kong/VHHH",
    ),
    "taipei": WeatherStation(
        city="Taipei", station_name="Taipei Songshan Airport",
        icao="RCSS", lat=25.0694, lon=121.5517,
        wunderground_url="https://www.wunderground.com/history/daily/tw/taipei/RCSS",
    ),
    "moscow": WeatherStation(
        city="Moscow", station_name="Moscow Sheremetyevo Airport",
        icao="UUEE", lat=55.9726, lon=37.4146,
        wunderground_url="https://www.wunderground.com/history/daily/ru/moscow/UUEE",
    ),
    "chicago": WeatherStation(
        city="Chicago", station_name="Chicago O'Hare Intl Airport",
        icao="KORD", lat=41.9742, lon=-87.9073,
        wunderground_url="https://www.wunderground.com/history/daily/us/chicago/KORD",
    ),
    "houston": WeatherStation(
        city="Houston", station_name="Houston Hobby Airport",
        icao="KHOU", lat=29.6454, lon=-95.2789,
        wunderground_url="https://www.wunderground.com/history/daily/us/houston/KHOU",
    ),
    "atlanta": WeatherStation(
        city="Atlanta", station_name="Atlanta Hartsfield-Jackson Airport",
        icao="KATL", lat=33.6407, lon=-84.4277,
        wunderground_url="https://www.wunderground.com/history/daily/us/atlanta/KATL",
    ),
    "seattle": WeatherStation(
        city="Seattle", station_name="Seattle-Tacoma Intl Airport",
        icao="KSEA", lat=47.4490, lon=-122.3093,
        wunderground_url="https://www.wunderground.com/history/daily/us/seattle/KSEA",
    ),
    "los-angeles": WeatherStation(
        city="Los Angeles", station_name="Los Angeles Intl Airport",
        icao="KLAX", lat=33.9416, lon=-118.4085,
        wunderground_url="https://www.wunderground.com/history/daily/us/los-angeles/KLAX",
    ),
    "buenos-aires": WeatherStation(
        city="Buenos Aires", station_name="Buenos Aires Ezeiza Airport",
        icao="SAEZ", lat=-34.8222, lon=-58.5358,
        wunderground_url="https://www.wunderground.com/history/daily/ar/buenos-aires/SAEZ",
    ),
    "lucknow": WeatherStation(
        city="Lucknow", station_name="Lucknow Chaudhary Charan Singh Airport",
        icao="VILK", lat=26.7606, lon=80.8893,
        wunderground_url="https://www.wunderground.com/history/daily/in/lucknow/VILK",
    ),
    "wellington": WeatherStation(
        city="Wellington", station_name="Wellington Intl Airport",
        icao="NZWN", lat=-41.3272, lon=174.8053,
        wunderground_url="https://www.wunderground.com/history/daily/nz/wellington/NZWN",
    ),
    # ═══ NEWLY VERIFIED (June 2, 2026) ═══
    "miami": WeatherStation(
        city="Miami", station_name="Miami Intl Airport",
        icao="KMIA", lat=25.7932, lon=-80.2906,
        wunderground_url="https://www.wunderground.com/history/daily/us/miami/KMIA",
    ),
    "dallas": WeatherStation(
        city="Dallas", station_name="Dallas Love Field",
        icao="KDAL", lat=32.8472, lon=-96.8518,
        wunderground_url="https://www.wunderground.com/history/daily/us/dallas/KDAL",
    ),
    "denver": WeatherStation(
        city="Denver", station_name="Buckley Space Force Base",
        icao="KBKF", lat=39.7017, lon=-104.7518,
        wunderground_url="https://www.wunderground.com/history/daily/us/denver/KBKF",
    ),
    "austin": WeatherStation(
        city="Austin", station_name="Austin-Bergstrom Intl Airport",
        icao="KAUS", lat=30.1945, lon=-97.6699,
        wunderground_url="https://www.wunderground.com/history/daily/us/austin/KAUS",
    ),
    "toronto": WeatherStation(
        city="Toronto", station_name="Toronto Pearson Intl Airport",
        icao="CYYZ", lat=43.6777, lon=-79.6248,
        wunderground_url="https://www.wunderground.com/history/daily/ca/toronto/CYYZ",
    ),
    "sao-paulo": WeatherStation(
        city="Sao Paulo", station_name="Sao Paulo-Guarulhos Intl Airport",
        icao="SBGR", lat=-23.4356, lon=-46.4731,
        wunderground_url="https://www.wunderground.com/history/daily/br/sao-paulo/SBGR",
    ),
    "shanghai": WeatherStation(
        city="Shanghai", station_name="Shanghai Pudong Intl Airport",
        icao="ZSPD", lat=31.1443, lon=121.8083,
        wunderground_url="https://www.wunderground.com/history/daily/cn/shanghai/ZSPD",
    ),
    "new-york-city": WeatherStation(
        city="New York City", station_name="New York LaGuardia Airport",
        icao="KLGA", lat=40.7772, lon=-73.8726,
        wunderground_url="https://www.wunderground.com/history/daily/us/new-york/KLGA",
    ),
    "new-york": WeatherStation(
        city="New York", station_name="New York LaGuardia Airport",
        icao="KLGA", lat=40.7772, lon=-73.8726,
        wunderground_url="https://www.wunderground.com/history/daily/us/new-york/KLGA",
    ),
    "nyc": WeatherStation(
        city="NYC", station_name="New York LaGuardia Airport",
        icao="KLGA", lat=40.7772, lon=-73.8726,
        wunderground_url="https://www.wunderground.com/history/daily/us/new-york/KLGA",
    ),
    "tel-aviv": WeatherStation(
        city="Tel Aviv", station_name="Tel Aviv Ben Gurion Airport",
        icao="LLBG", lat=32.0114, lon=34.8867,
        wunderground_url="https://www.wunderground.com/history/daily/il/tel-aviv/LLBG",
    ),
    "sao paulo": WeatherStation(
        city="Sao Paulo", station_name="Sao Paulo-Guarulhos Intl Airport",
        icao="SBGR", lat=-23.4356, lon=-46.4731,
        wunderground_url="https://www.wunderground.com/history/daily/br/sao-paulo/SBGR",
    ),
    "new york": WeatherStation(
        city="New York", station_name="New York LaGuardia Airport",
        icao="KLGA", lat=40.7772, lon=-73.8726,
        wunderground_url="https://www.wunderground.com/history/daily/us/new-york/KLGA",
    ),
    "new york city": WeatherStation(
        city="New York City", station_name="New York LaGuardia Airport",
        icao="KLGA", lat=40.7772, lon=-73.8726,
        wunderground_url="https://www.wunderground.com/history/daily/us/new-york/KLGA",
    ),
    "sydney": WeatherStation(
        city="Sydney", station_name="Sydney Airport",
        icao="YSSY", lat=-33.9399, lon=151.1753,
        wunderground_url="https://www.wunderground.com/history/daily/au/sydney/YSSY",
    ),
    "berlin": WeatherStation(
        city="Berlin", station_name="Berlin Brandenburg Airport",
        icao="EDDB", lat=52.3667, lon=13.5033,
        wunderground_url="https://www.wunderground.com/history/daily/de/berlin/EDDB",
    ),
    "rome": WeatherStation(
        city="Rome", station_name="Rome Fiumicino Airport",
        icao="LIRF", lat=41.8003, lon=12.2389,
        wunderground_url="https://www.wunderground.com/history/daily/it/rome/LIRF",
    ),
    "delhi": WeatherStation(
        city="Delhi", station_name="Delhi Indira Gandhi Intl Airport",
        icao="VIDP", lat=28.5562, lon=77.1000,
        wunderground_url="https://www.wunderground.com/history/daily/in/delhi/VIDP",
    ),
    "bangkok": WeatherStation(
        city="Bangkok", station_name="Bangkok Suvarnabhumi Airport",
        icao="VTBS", lat=13.6900, lon=100.7501,
        wunderground_url="https://www.wunderground.com/history/daily/th/bangkok/VTBS",
    ),
    "osaka": WeatherStation(
        city="Osaka", station_name="Osaka Itami Airport",
        icao="RJOO", lat=34.7855, lon=135.4382,
        wunderground_url="https://www.wunderground.com/history/daily/jp/osaka/RJOO",
    ),
    "jakarta": WeatherStation(
        city="Jakarta", station_name="Soekarno-Hatta Intl Airport",
        icao="WIII", lat=-6.1256, lon=106.6558,
        wunderground_url="https://www.wunderground.com/history/daily/id/jakarta/WIII",
    ),
    "manila": WeatherStation(
        city="Manila", station_name="Ninoy Aquino Intl Airport",
        icao="RPLL", lat=14.5086, lon=121.0197,
        wunderground_url="https://www.wunderground.com/history/daily/ph/manila/RPLL",
    ),
    "kuala-lumpur": WeatherStation(
        city="Kuala Lumpur", station_name="Kuala Lumpur Intl Airport",
        icao="WMKK", lat=2.7456, lon=101.7099,
        wunderground_url="https://www.wunderground.com/history/daily/my/kuala-lumpur/WMKK",
    ),
    "warsaw": WeatherStation(
        city="Warsaw", station_name="Warsaw Chopin Airport",
        icao="EPWA", lat=52.1657, lon=20.9671,
        wunderground_url="https://www.wunderground.com/history/daily/pl/warsaw/EPWA",
    ),
}


def get_station(city: str) -> WeatherStation | None:
    """Get the Polymarket resolution station for a city."""
    key = city.lower().strip().replace(' ', '-')
    return STATIONS.get(key)


def get_airport_coords(city: str) -> tuple[float, float] | None:
    """Get exact airport coordinates for Open-Meteo forecasting."""
    st = get_station(city)
    if st:
        return (st.lat, st.lon)
    return None


def get_wunderground_url(city: str) -> str | None:
    """Get the Wunderground history URL for resolution verification."""
    st = get_station(city)
    return st.wunderground_url if st else None
