"""
Resolution-rule station verification.

Makes the bot forecast/observe the EXACT airport station each Polymarket
weather market actually settles on, instead of blindly using a hardcoded
city coordinate.

Pipeline (per scanned market):
  1. Pull the market's resolution rules from the Gamma EVENT metadata
     (event description + each child market's description / resolutionSource /
     question). The scanner already stores the full event dict as market.raw.
  2. Compare the station named in those rules to our hardcoded station
     (data.weather_stations) DETERMINISTICALLY -- zero LLM tokens when the
     ICAO code, airport-name token, or Wunderground URL already matches.
  3. ONLY when the deterministic check is inconclusive, or a DIFFERENT station
     is named, call a fast/cheap LLM (gpt-5.4-mini via the Freemodel
     openai-responses API) to decide match vs. adjust and return the correct
     coordinates.
  4. The resolved coordinates feed the weather/observed engines, so a mismatch
     is corrected instead of silently forecasting the wrong place.

This module imports nothing that needs the network, so every pure helper is
fully unit-testable offline. The optional LLM call is injected (`ml_engine`).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

try:
    from data.weather_stations import STATIONS, get_station
except Exception:  # pragma: no cover - keep importable in isolation
    STATIONS = {}

    def get_station(city):
        return None

try:
    from logger import log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("resolution_rules")


# Build a reverse ICAO -> station lookup from whatever stations we know.
_STATION_BY_ICAO: Dict[str, object] = {}
for _st in (STATIONS or {}).values():
    _code = (getattr(_st, "icao", "") or "").upper()
    if _code:
        _STATION_BY_ICAO.setdefault(_code, _st)


def get_station_by_icao(icao: Optional[str]):
    """Look up a known resolution station by its ICAO airport code."""
    if not icao:
        return None
    return _STATION_BY_ICAO.get(str(icao).strip().upper())


# ICAO airport codes are 4 uppercase letters on word boundaries.
_ICAO_RE = re.compile(r"\b([A-Z]{4})\b")
# 4-letter uppercase tokens that are NOT station codes.
_ICAO_STOPWORDS = {
    "TEMP", "HIGH", "WILL", "DATE", "TIME", "NOAA", "WMO", "YYYY",
    "JSON", "HTTP", "HTML", "UTC", "THIS", "THAT", "FROM", "WITH",
    "WHEN", "WERE", "BEEN", "OPEN", "META", "WAIT", "GMT", "EST",
    "EDT", "PST", "PDT", "USA", "TRUE", "NULL", "FAQ", "ONLY",
    "EACH", "DAILY", "CITY",
}


def extract_resolution_text(event: Optional[dict]) -> str:
    """Collect every piece of resolution-relevant text from a Gamma event.

    Pulls the event-level ``description`` / ``resolutionSource`` / ``title``
    plus each child market's ``description`` / ``resolutionSource``.
    Deduplicated and capped so a downstream LLM prompt stays cheap.
    """
    if not isinstance(event, dict):
        return ""
    chunks: List[str] = []

    def _add(v):
        if isinstance(v, str):
            s = v.strip()
            if s and s not in chunks:
                chunks.append(s)

    _add(event.get("description"))
    _add(event.get("resolutionSource"))
    _add(event.get("title"))
    for m in event.get("markets", []) or []:
        if not isinstance(m, dict):
            continue
        _add(m.get("resolutionSource"))
        _add(m.get("description"))
    return "\n".join(chunks)[:1500]


def find_icaos(text: str) -> List[str]:
    """Return candidate ICAO codes found in free text (stopwords removed)."""
    if not text:
        return []
    out: List[str] = []
    for m in _ICAO_RE.findall(text.upper()):
        if m in _ICAO_STOPWORDS:
            continue
        if m not in out:
            out.append(m)
    return out


def _station_name_tokens(station) -> List[str]:
    """Distinctive lowercase tokens from a station name for fuzzy matching."""
    if not station:
        return []
    generic = {"airport", "international", "intl", "field", "the", "of",
               "air", "force", "base", "station", "space", "county", "regional"}
    toks = re.split(r"[^a-z]+", (station.station_name or "").lower())
    return [t for t in toks if t and t not in generic and len(t) > 3]


def deterministic_station_match(text: str, station) -> Optional[bool]:
    """Compare rules text to our hardcoded station WITHOUT an LLM.

    Returns:
      True  -> rules clearly name OUR station (ICAO / airport name / WU url).
      False -> rules clearly name a DIFFERENT known airport.
      None  -> inconclusive (no station info, or ambiguous) -> ask the LLM.
    """
    if not text:
        return None
    up = text.upper()
    low = text.lower()

    if station is not None:
        icao = (station.icao or "").upper()
        if icao and icao in up:
            return True
        wu = (getattr(station, "wunderground_url", "") or "").lower()
        if wu and wu in low:
            return True
        name_tokens = _station_name_tokens(station)
        if name_tokens and any(tok in low for tok in name_tokens):
            return True

    icaos = find_icaos(text)
    if station is not None and (station.icao or "").upper() in icaos:
        return True
    if icaos:
        # A 4-letter code mapping to a DIFFERENT known station is a definite
        # mismatch; an unknown code is "probably some station" -> let the LLM
        # confirm (None) rather than hard-failing.
        for code in icaos:
            other = get_station_by_icao(code)
            if other is not None and (
                station is None or other.icao != getattr(station, "icao", None)
            ):
                return False
        return None
    return None


def extract_first_json(text: str) -> Optional[dict]:
    """Best-effort: parse the first JSON object found in a model reply."""
    if not text:
        return None
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    try:
        v = json.loads(text)
        return v if isinstance(v, dict) else None
    except Exception:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    blob = text[start:i + 1]
                    try:
                        v = json.loads(blob)
                        return v if isinstance(v, dict) else None
                    except Exception:
                        start = -1
    return None


def parse_responses_text(data: dict) -> str:
    """Extract the assistant text from an OpenAI *Responses API* payload.

    Handles the convenience ``output_text`` field and the structured
    ``output -> [message] -> content -> [output_text]`` form. Reasoning and
    tool items are skipped.
    """
    if not isinstance(data, dict):
        return ""
    ot = data.get("output_text")
    if isinstance(ot, str) and ot.strip():
        return ot
    if isinstance(ot, list) and ot:
        joined = "".join(x for x in ot if isinstance(x, str))
        if joined.strip():
            return joined
    parts: List[str] = []
    for item in data.get("output", []) or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in (None, "message"):
            continue  # skip reasoning / tool-call items
        for c in item.get("content", []) or []:
            if isinstance(c, dict) and c.get("type") in ("output_text", "text"):
                t = c.get("text")
                if isinstance(t, str):
                    parts.append(t)
    return "".join(parts)


@dataclass
class ResolvedStation:
    """Outcome of resolving the coordinates a market actually settles on."""
    coords: Optional[Tuple[float, float]]   # None => caller should SKIP
    icao: Optional[str] = None
    station_name: Optional[str] = None
    source: str = "default"                  # confirmed | adjusted-ml | default | skip
    matched: Optional[bool] = None
    confidence: float = 0.0
    reason: str = ""


class StationResolver:
    """Resolve the EXACT resolution-station coordinates for a scanned market."""

    def __init__(self):
        self._cache: Dict[str, ResolvedStation] = {}

    def _cache_key(self, city: str, event: Optional[dict]) -> str:
        eid = ""
        if isinstance(event, dict):
            eid = str(event.get("id") or event.get("slug") or "")
        return f"{(city or '').lower()}|{eid}"

    def _city_center(self, city: str) -> Optional[Tuple[float, float]]:
        """City-center fallback via weather_fetcher, imported lazily so this
        module stays usable offline / in tests."""
        try:
            from data.weather_fetcher import get_city_coords
            return get_city_coords(city)
        except Exception:
            return None

    def _hard(self, station, coords, source, matched, reason, conf=0.0):
        return ResolvedStation(
            coords=coords,
            icao=getattr(station, "icao", None),
            station_name=getattr(station, "station_name", None),
            source=source, matched=matched, confidence=conf, reason=reason,
        )

    def resolve(self, city: str, event: Optional[dict], ml_engine=None,
                verify_enabled: bool = True, min_conf: float = 0.6,
                skip_on_unknown: bool = False) -> ResolvedStation:
        key = self._cache_key(city, event)
        cached = self._cache.get(key)
        if cached is not None:
            return cached

        station = get_station(city)
        hard_coords = (station.lat, station.lon) if station else self._city_center(city)
        text = extract_resolution_text(event)

        result = self._resolve_inner(
            city, station, hard_coords, text, ml_engine,
            verify_enabled, min_conf, skip_on_unknown,
        )
        self._cache[key] = result
        return result

    def _resolve_inner(self, city, station, hard_coords, text, ml_engine,
                       verify_enabled, min_conf, skip_on_unknown):
        # No rules text -> use what we have.
        if not text:
            if hard_coords is None:
                return ResolvedStation(None, source="skip", matched=None,
                                       reason="no rules text and no coords")
            return self._hard(station, hard_coords,
                              "confirmed" if station else "default",
                              None, "no rules text; used hardcoded")

        det = deterministic_station_match(text, station)
        if det is True:
            return self._hard(station, hard_coords, "confirmed", True,
                              "rules match hardcoded station (no LLM)")

        # det is False (different station) or None (inconclusive). This is the
        # ONLY place tokens are ever spent.
        if verify_enabled and ml_engine is not None and getattr(ml_engine, "enabled", False):
            v = None
            try:
                v = ml_engine.verify_station(city, text, station)
            except Exception as e:  # pragma: no cover
                log.warning(f"station verify failed for {city}: {e}")
            if isinstance(v, dict):
                conf = float(v.get("conf", v.get("confidence", 0.0)) or 0.0)
                match = bool(v.get("match", False))
                if match and station is not None:
                    return self._hard(station, hard_coords, "confirmed", True,
                                      f"LLM confirms station (conf {conf:.2f})", conf)
                # Mismatch -> adjust coordinates to the rule's station.
                icao = (v.get("icao") or "").upper() or None
                known = get_station_by_icao(icao) if icao else None
                if known is not None:
                    return ResolvedStation(
                        coords=(known.lat, known.lon), icao=known.icao,
                        station_name=known.station_name, source="adjusted-ml",
                        matched=False, confidence=conf,
                        reason=f"rule station {known.icao} (table coords)")
                lat, lon = v.get("lat"), v.get("lon")
                if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                        and -90 <= lat <= 90 and -180 <= lon <= 180
                        and conf >= min_conf and not (lat == 0 and lon == 0)):
                    return ResolvedStation(
                        coords=(float(lat), float(lon)), icao=icao,
                        station_name=v.get("station") or v.get("name"),
                        source="adjusted-ml", matched=False, confidence=conf,
                        reason=f"adjusted to rule station {icao or '?'} (conf {conf:.2f})")
                # LLM unsure -> fall through to hardcoded/skip.

        # No LLM, or LLM inconclusive.
        if det is False:
            if skip_on_unknown or hard_coords is None:
                return ResolvedStation(None, source="skip", matched=False,
                                       reason="rules name a different station; no verified coords")
            return self._hard(station, hard_coords, "default", False,
                              "different station suspected; LLM unavailable -- used hardcoded (RISK)")
        # Inconclusive.
        if hard_coords is None:
            return ResolvedStation(None, source="skip", matched=None,
                                   reason="no station match and no coords")
        return self._hard(station, hard_coords,
                          "confirmed" if station else "default", None,
                          "no conclusive station info; used hardcoded")
