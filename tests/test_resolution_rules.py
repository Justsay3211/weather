"""Offline unit tests for the resolution-station verification feature.

No network / no requests import. The LLM is replaced by fakes. Run with:
    python tests/test_resolution_rules.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.resolution_rules import (  # noqa: E402
    extract_resolution_text, find_icaos, deterministic_station_match,
    extract_first_json, parse_responses_text, get_station_by_icao,
    StationResolver,
)
from data.weather_stations import get_station  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"PASS  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name}")


seoul = get_station("seoul")   # Incheon RKSI
tokyo = get_station("tokyo")   # Haneda RJTT

# --- extract_resolution_text ---
ev = {
    "description": "Resolves to the high temperature at Incheon (RKSI).",
    "markets": [{
        "description": "Per Wunderground RKSI",
        "resolutionSource": "https://www.wunderground.com/history/daily/kr/incheon/RKSI",
    }],
}
txt = extract_resolution_text(ev)
check("extract combines event+market", "RKSI" in txt and "Wunderground" in txt)
check("extract empty on non-dict", extract_resolution_text(None) == "")

# --- find_icaos ---
check("find icao", "RKSI" in find_icaos("station RKSI today HIGH"))
check("stopword filtered", "HIGH" not in find_icaos("HIGH RKSI"))

# --- deterministic_station_match ---
check("det true via icao", deterministic_station_match("resolves at RKSI", seoul) is True)
check("det true via name", deterministic_station_match("Incheon International Airport", seoul) is True)
check("det true via wu url", deterministic_station_match(seoul.wunderground_url, seoul) is True)
check("det false via other known", deterministic_station_match("resolves at RJTT Haneda", seoul) is False)
check("det none when no info", deterministic_station_match("the daily high temperature", seoul) is None)

# --- get_station_by_icao ---
check("by icao hit", getattr(get_station_by_icao("RKSI"), "icao", None) == "RKSI")
check("by icao miss", get_station_by_icao("ZZZZ") is None)

# --- extract_first_json ---
check("json direct", extract_first_json('{"match":true,"conf":0.9}')["match"] is True)
check("json fenced", extract_first_json('```json\n{"a":1}\n```')["a"] == 1)
check("json embedded", extract_first_json('sure: {"icao":"RJTT","lat":35.5,"lon":139.8} done')["icao"] == "RJTT")
check("json none", extract_first_json("no json here") is None)

# --- parse_responses_text ---
check("resp output_text str", parse_responses_text({"output_text": '{"match":true}'}) == '{"match":true}')
struct = {"output": [
    {"type": "reasoning", "content": []},
    {"type": "message", "content": [{"type": "output_text", "text": '{"m":1}'}]},
]}
check("resp structured skips reasoning", parse_responses_text(struct) == '{"m":1}')


class FakeML:
    enabled = True
    calls = 0

    def verify_station(self, city, text, station):
        FakeML.calls += 1
        return {"match": False, "icao": "RJTT", "lat": 35.5494, "lon": 139.7798,
                "conf": 0.95, "why": "haneda"}


# --- resolver: deterministic confirm, NO LLM call ---
r = StationResolver()
FakeML.calls = 0
res = r.resolve("Seoul", {"id": "e1", "description": "Resolves at Incheon RKSI per Wunderground."},
                ml_engine=FakeML())
check("resolve confirmed (no ml)", res.matched is True and res.source == "confirmed" and FakeML.calls == 0)
check("resolve confirmed coords", res.coords == (seoul.lat, seoul.lon))

# --- resolver: inconclusive (unknown code) -> LLM adjusts, known ICAO -> table coords ---
FakeML.calls = 0
res2 = r.resolve("Seoul", {"id": "e2", "description": "High temperature recorded at XXXX field."},
                 ml_engine=FakeML())
check("resolve adjusted-ml called once", res2.source == "adjusted-ml" and FakeML.calls == 1)
check("resolve adjusted uses table coords", res2.coords == (tokyo.lat, tokyo.lon) and res2.icao == "RJTT")


class OffML:
    enabled = False

    def verify_station(self, *a, **k):
        return None


# --- resolver: ML disabled, inconclusive -> hardcoded ---
res3 = StationResolver().resolve("Seoul", {"id": "e3", "description": "the daily high temperature"},
                                 ml_engine=OffML())
check("resolve ml-off uses hardcoded", res3.coords == (seoul.lat, seoul.lon) and res3.matched is None)


class LowML:
    enabled = True

    def verify_station(self, *a, **k):
        return {"match": False, "icao": "ABCD", "lat": 10, "lon": 10, "conf": 0.2}


# --- resolver: known-different station + low-conf LLM + skip_on_unknown -> SKIP ---
res4 = StationResolver().resolve("Seoul", {"id": "e4", "description": "resolves at RJTT Haneda"},
                                 ml_engine=LowML(), skip_on_unknown=True)
check("resolve skip on unverified mismatch", res4.coords is None and res4.source == "skip")


class RawML:
    enabled = True

    def verify_station(self, *a, **k):
        return {"match": False, "icao": "WXYZ", "lat": 12.34, "lon": 56.78, "conf": 0.9}


# --- resolver: ML adjust with raw coords when ICAO unknown but conf high ---
res5 = StationResolver().resolve("Seoul", {"id": "e5", "description": "resolves at WXYZ airport per rules"},
                                 ml_engine=RawML())
check("resolve adjusted raw coords", res5.coords == (12.34, 56.78) and res5.source == "adjusted-ml")

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
