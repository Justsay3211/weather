"""Source-health monitor (GREEN / YELLOW / RED) — prompt SOURCE HEALTH section."""

import time
from dataclasses import dataclass, field
from typing import Dict


class HealthState(object):
    GREEN = "GREEN"
    YELLOW = "YELLOW"
    RED = "RED"


@dataclass
class _Stat:
    last_ok_ts: float = 0.0
    last_fail_ts: float = 0.0
    consecutive_failures: int = 0
    total_ok: int = 0
    total_fail: int = 0
    last_latency_ms: float = 0.0
    last_http: int = 0
    open_until: float = 0.0   # circuit-breaker cooldown


class SourceHealth:
    """Tracks per-source success/failure + a simple circuit breaker."""

    def __init__(self, breaker_threshold: int = 3, cooldown_s: int = 600):
        self._stats: Dict[str, _Stat] = {}
        self.breaker_threshold = int(breaker_threshold)
        self.cooldown_s = int(cooldown_s)

    def _stat(self, source: str) -> _Stat:
        st = self._stats.get(source)
        if st is None:
            st = _Stat()
            self._stats[source] = st
        return st

    def record_ok(self, source: str, latency_ms: float = 0.0, http: int = 200) -> None:
        st = self._stat(source)
        st.last_ok_ts = time.time()
        st.consecutive_failures = 0
        st.total_ok += 1
        st.last_latency_ms = latency_ms
        st.last_http = http
        st.open_until = 0.0

    def record_fail(self, source: str, http: int = 0) -> None:
        st = self._stat(source)
        st.last_fail_ts = time.time()
        st.consecutive_failures += 1
        st.total_fail += 1
        st.last_http = http
        if st.consecutive_failures >= self.breaker_threshold:
            st.open_until = time.time() + self.cooldown_s

    def is_open(self, source: str) -> bool:
        """True when the circuit breaker is open (skip this source for now)."""
        st = self._stats.get(source)
        if not st:
            return False
        if st.open_until and time.time() < st.open_until:
            return True
        if st.open_until and time.time() >= st.open_until:
            st.open_until = 0.0  # half-open: allow a retry
        return False

    def state(self, source: str) -> str:
        st = self._stats.get(source)
        if not st or (st.total_ok == 0 and st.total_fail == 0):
            return HealthState.YELLOW
        if self.is_open(source):
            return HealthState.RED
        if st.consecutive_failures == 0:
            return HealthState.GREEN
        if st.consecutive_failures < self.breaker_threshold:
            return HealthState.YELLOW
        return HealthState.RED

    def snapshot(self) -> Dict[str, Dict]:
        out = {}
        for k, st in self._stats.items():
            out[k] = {
                "state": self.state(k),
                "consecutive_failures": st.consecutive_failures,
                "total_ok": st.total_ok,
                "total_fail": st.total_fail,
                "last_latency_ms": round(st.last_latency_ms, 1),
                "last_http": st.last_http,
                "breaker_open": self.is_open(k),
            }
        return out
