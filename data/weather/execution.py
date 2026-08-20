"""Execution-location router — the customizable VPS / bot / off control.

This is the piece that makes the pipeline run "entirely on VPS, or on the bot
(Railway/runtime), or a per-source mix", exactly as requested:

  * WEATHER_EXECUTION_MODE (global default):
      - 'vps'    : upstream fetching is the VPS's job. The bot makes ZERO direct
                   upstream requests. If the VPS proxy is unreachable the bot
                   does NOT fall back to unoptimal direct calls — it skips the
                   source and lets the VPS take care of it (cache still served).
      - 'bot'    : the bot fetches everything directly (Railway/runtime). No VPS.
      - 'hybrid' : use the per-source override map, defaulting to 'bot'.
      - 'auto'   : prefer VPS when its master switch + weather-proxy are ON and
                   the node is reachable, else fetch on the bot.
  * WEATHER_SOURCE_LOCATION : per-source override, e.g.
      {"open_meteo": "vps", "weatherapi": "bot", "nws": "off"}
    A per-source value always wins over the global mode.
  * Master weather-source toggle: when the VPS master switch (or the weather
    sub-toggle) is OFF, 'vps'/'auto' resolve to 'bot' so the pipeline keeps
    running on the bot — turning the VPS weather source off = run on bot.

Resolution returns one of Location.VPS / Location.BOT / Location.OFF for each
source. The pipeline uses this to decide HOW (and whether) to fetch.
"""

from typing import Dict, Optional


class Location(object):
    VPS = "vps"
    BOT = "bot"
    OFF = "off"


class ExecutionRouter:
    def __init__(self, mode: str = "auto",
                 source_overrides: Optional[Dict[str, str]] = None,
                 vps_available: bool = False,
                 vps_weather_enabled: bool = False):
        """
        mode                : global WEATHER_EXECUTION_MODE
        source_overrides    : WEATHER_SOURCE_LOCATION map (source_key -> location)
        vps_available       : is the VPS node currently reachable / healthy
        vps_weather_enabled : VPS master switch AND weather-proxy sub-toggle ON
        """
        self.mode = str(mode or "auto").lower()
        self.source_overrides = {str(k): str(v).lower()
                                 for k, v in (source_overrides or {}).items()}
        self.vps_available = bool(vps_available)
        self.vps_weather_enabled = bool(vps_weather_enabled)

    # ---- helpers -------------------------------------------------------
    def _provider_of(self, source_key: str) -> str:
        # accepts either 'open_meteo' or 'open_meteo:ecmwf_ifs025'
        return source_key.split(":", 1)[0]

    def _override_for(self, source_key: str) -> Optional[str]:
        if source_key in self.source_overrides:
            return self.source_overrides[source_key]
        prov = self._provider_of(source_key)
        return self.source_overrides.get(prov)

    # ---- main API ------------------------------------------------------
    def resolve(self, source_key: str) -> str:
        """Return Location.* for the given source, honouring overrides + master."""
        override = self._override_for(source_key)
        target = override if override else self.mode

        if target == Location.OFF:
            return Location.OFF

        if target == Location.BOT:
            return Location.BOT

        if target == Location.VPS:
            # VPS explicitly requested. If the VPS weather source is disabled by
            # the master toggle, fall back to running on the bot (turning VPS
            # off = run on bot). If it IS enabled but momentarily unreachable we
            # STILL return VPS: the pipeline must not make unoptimal direct
            # calls — it will serve cache / skip. "vps take care".
            if not self.vps_weather_enabled:
                return Location.BOT
            return Location.VPS

        if target == "hybrid":
            # no explicit per-source override matched -> default to bot
            return Location.BOT

        # 'auto' (and any unknown value): prefer VPS when usable, else bot
        if self.vps_weather_enabled and self.vps_available:
            return Location.VPS
        return Location.BOT

    def bot_may_fetch(self, source_key: str) -> bool:
        return self.resolve(source_key) == Location.BOT

    def summary(self, source_keys) -> Dict[str, str]:
        return {k: self.resolve(k) for k in source_keys}
