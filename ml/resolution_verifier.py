"""
Resolution-station verifier -- a fast, low-token LLM check that a Polymarket
weather market resolves at the SAME airport as our hardcoded station, and if
not, returns the correct station / coordinates to forecast instead.

Uses the Freemodel **OpenAI Responses API** (api: "openai-responses"):

    POST {ML_RESPONSES_URL}/responses
    body: {"model": ..., "input": <prompt>, "max_output_tokens": N}

Defaults to the cheap NON-reasoning model (gpt-5.4-mini) with a tiny prompt, so
a single verification costs well under a few hundred tokens. Called only when
the deterministic check in data.resolution_rules is inconclusive or flags a
different station. Results are cached per city.

Config (all optional, read from env with sensible defaults):
    ML_RESPONSES_URL   default https://api.freemodel.dev/v1
    ML_API_KEY         shared Freemodel key (required to enable the check)
    ML_VERIFY_MODEL    default gpt-5.4-mini
"""

from __future__ import annotations

import os
from typing import Dict, Optional

import requests

from data.resolution_rules import extract_first_json, parse_responses_text

try:
    from config import Config
except Exception:  # pragma: no cover
    Config = None

try:
    from logger import log
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("resolution_verifier")


def _cfg(name: str, default):
    if Config is not None and getattr(Config, name, None) not in (None, ""):
        return getattr(Config, name)
    return default


class ResolutionVerifier:
    """Cheap LLM station verifier over the openai-responses API."""

    def __init__(self):
        base = _cfg("ML_RESPONSES_URL",
                    os.getenv("ML_RESPONSES_URL", "https://api.freemodel.dev/v1"))
        self.base_url = (base or "").rstrip("/")
        self.api_key = _cfg("ML_API_KEY", os.getenv("ML_API_KEY", "")) or ""
        self.model = _cfg("ML_VERIFY_MODEL",
                          os.getenv("ML_VERIFY_MODEL", "gpt-5.4-mini"))
        self.enabled = bool(self.api_key)
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })
        self._tokens_used = 0
        self._cache: Dict[str, Optional[dict]] = {}

    def _responses_query(self, prompt: str, max_output_tokens: int = 160) -> str:
        try:
            resp = self._session.post(
                f"{self.base_url}/responses",
                json={
                    "model": self.model,
                    "input": prompt,
                    "max_output_tokens": max_output_tokens,
                },
                timeout=8,
            )
        except Exception as e:
            log.warning(f"  station-verify request failed: {e}")
            return ""
        if resp.status_code != 200:
            log.warning(f"  station-verify HTTP {resp.status_code}: {resp.text[:80]}")
            return ""
        try:
            data = resp.json()
        except Exception:
            return ""
        usage = data.get("usage", {}) or {}
        self._tokens_used += int(
            usage.get("total_tokens")
            or (usage.get("input_tokens", 0) + usage.get("output_tokens", 0))
            or 0
        )
        return parse_responses_text(data)

    def verify_station(self, city: str, resolution_text: str, station) -> Optional[Dict]:
        if not self.enabled:
            return None
        key = (city or "").lower()
        if key in self._cache:
            return self._cache[key]

        if station is not None:
            hard = (f'name="{station.station_name}" icao={station.icao} '
                    f'lat={station.lat} lon={station.lon}')
        else:
            hard = "none (no hardcoded station)"

        prompt = (
            "You verify the resolution weather station for a Polymarket "
            "temperature market. Reply with ONLY compact JSON:\n"
            '{"match":true|false,"icao":"ICAO","lat":<num>,"lon":<num>,'
            '"conf":<0-1>,"why":"<=6 words"}\n'
            "Set match=true ONLY if the rules resolve at the SAME "
            "airport/station as ours. If different, set match=false and give "
            "that airport's ICAO and exact decimal coordinates.\n"
            f"CITY: {city}\n"
            f"OUR_STATION: {hard}\n"
            f"RULES:\n{(resolution_text or '')[:700]}"
        )
        text = self._responses_query(prompt, max_output_tokens=160)
        parsed = extract_first_json(text)
        self._cache[key] = parsed
        return parsed

    def get_status(self) -> Dict:
        return {
            "enabled": self.enabled,
            "model": self.model,
            "responses_url": self.base_url,
            "tokens_used": self._tokens_used,
            "cache_size": len(self._cache),
        }
