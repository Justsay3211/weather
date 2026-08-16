"""
VPS edge-node client: data offload (stream + prune) and status/pull.

Design:
- Fail-open: any network/parse error is swallowed so the trading loop never
  breaks because the VPS is down.
- Zero behaviour change when VPS_OFFLOAD_ENABLED is off -> data stays on
  Railway exactly as before (the /settings VPS tab toggles this live).
- Bearer token is read from Config (env only) and is NEVER logged.
"""
from __future__ import annotations

import os
import time
import json

try:
    from logger import log
except Exception:
    import logging
    log = logging.getLogger("vps_store")


def _config():
    try:
        from config import Config
        return Config
    except Exception:
        return None


# Append-only growers identified in the Railway disk audit.
_OFFLOAD_FILES = [
    "data/weather_trace.jsonl",
    "data/positions_timeseries.jsonl",
    "data/positions_mae_mfe.jsonl",
    "data/paper_trades.jsonl",
    "data/book_trace.jsonl",
]

# Req-27 #c: per-stream data-handling setting. Maps each offloaded stream
# (basename) to its VPS_HANDLING_* choice key. 'full' (default) = one
# continuous file per stream on the edge node; 'weekly'/'monthly' = one file
# per period. The chosen mode is sent with every /store POST so the node names
# the destination file accordingly (see edge app.py _store_path).
_STREAM_HANDLING_KEY = {
    "paper_trades.jsonl": "VPS_HANDLING_PAPER_TRADES",
    "positions_mae_mfe.jsonl": "VPS_HANDLING_MAE",
    "positions_timeseries.jsonl": "VPS_HANDLING_TIMESERIES",
    "weather_trace.jsonl": "VPS_HANDLING_WEATHER_TRACE",
    "book_trace.jsonl": "VPS_HANDLING_BOOK_TRACE",
}


def _handling_for(stream):
    """Return the data-handling mode ('full'|'weekly'|'monthly') for a stream
    basename, defaulting to 'full'. Fail-safe to 'full' on any error."""
    try:
        c = _config()
        key = _STREAM_HANDLING_KEY.get(stream)
        if not key or c is None:
            return "full"
        val = str(getattr(c, key, "full") or "full").strip().lower()
        return val if val in ("full", "weekly", "monthly") else "full"
    except Exception:
        return "full"

_LAST_OFFLOAD = 0.0


def _base():
    c = _config()
    return (getattr(c, "VPS_BASE_URL", "") or "").rstrip("/") if c is not None else ""


def _token():
    c = _config()
    return (getattr(c, "VPS_AUTH_TOKEN", "") or "") if c is not None else ""


def configured():
    return bool(_base() and _token())


def enabled():
    c = _config()
    return bool(configured() and getattr(c, "VPS_OFFLOAD_ENABLED", False))


def _headers(extra=None):
    h = {"Authorization": "Bearer " + _token()}
    if extra:
        h.update(extra)
    return h


def _requests():
    import requests
    return requests


def _timeout():
    c = _config()
    try:
        return float(getattr(c, "VPS_TIMEOUT_SECONDS", 8) or 8)
    except Exception:
        return 8.0


def _get(path, params=None, stream=False, timeout=None):
    r = _requests()
    return r.get(_base() + path, headers=_headers(), params=(params or {}),
                 timeout=(timeout or _timeout()), stream=stream)


def _notify_health(ok, detail=""):
    try:
        from data import notifier as _ntf
        _ntf.note_vps_health(ok, detail)
    except Exception:
        pass


def health():
    if not configured():
        return {"ok": False, "error": "not configured"}
    try:
        t0 = time.time()
        resp = _get("/health")
        dt = int((time.time() - t0) * 1000)
        if resp.status_code != 200:
            return {"ok": False, "error": "HTTP %s" % resp.status_code}
        d = resp.json() or {}
        d["ok"] = True
        d["latency_ms"] = dt
        _notify_health(True, "%s ms" % dt)
        return d
    except Exception as e:
        _notify_health(False, str(e)[:60])
        return {"ok": False, "error": str(e)}


def metrics():
    if not configured():
        return {"ok": False, "error": "not configured"}
    try:
        resp = _get("/metrics")
        if resp.status_code != 200:
            return {"ok": False, "error": "HTTP %s" % resp.status_code}
        d = resp.json() or {}
        d["ok"] = True
        return d
    except Exception as e:
        return {"ok": False, "error": str(e)}


def usage():
    if not configured():
        return {"ok": False, "error": "not configured"}
    try:
        resp = _get("/store/usage")
        if resp.status_code != 200:
            return {"ok": False, "error": "HTTP %s" % resp.status_code}
        d = resp.json() or {}
        d["ok"] = True
        return d
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _batch_lines():
    c = _config()
    try:
        return int(getattr(c, "VPS_OFFLOAD_BATCH_LINES", 2000) or 2000)
    except Exception:
        return 2000


def _post_stream(stream, lines):
    r = _requests()
    payload = {"stream": stream, "lines": lines, "handling": _handling_for(stream)}
    resp = r.post(_base() + "/store",
                  headers=_headers({"Content-Type": "application/json"}),
                  data=json.dumps(payload), timeout=max(_timeout(), 20))
    if resp.status_code != 200:
        return False
    try:
        return bool((resp.json() or {}).get("ok"))
    except Exception:
        return False


def offload_and_prune():
    """Ship each tracked file to the VPS in batches; truncate on full ack.
    A file is cleared locally ONLY after every batch was acknowledged."""
    if not enabled():
        return {"ok": False, "error": "offload disabled"}
    shipped = {}
    batch = _batch_lines()
    for path in _OFFLOAD_FILES:
        try:
            if not (os.path.exists(path) and os.path.getsize(path) > 0):
                continue
            with open(path, "r") as f:
                lines = [ln.rstrip("\n") for ln in f if ln.strip()]
            if not lines:
                continue
            stream = os.path.basename(path)
            ok_all = True
            for i in range(0, len(lines), batch):
                if not _post_stream(stream, lines[i:i + batch]):
                    ok_all = False
                    break
            if ok_all:
                open(path, "w").close()  # truncate only after full ack
                shipped[stream] = len(lines)
        except Exception as e:
            log.debug("vps offload %s failed: %s" % (path, e))
            continue
    return {"ok": True, "shipped": shipped}


def maybe_offload():
    """Interval/size-gated offload, safe to call every scan tick. No-op when
    disabled so Railway keeps data locally exactly as before."""
    global _LAST_OFFLOAD
    if not enabled():
        return
    c = _config()
    try:
        interval_h = float(getattr(c, "VPS_OFFLOAD_INTERVAL_HOURS", 12) or 12)
    except Exception:
        interval_h = 12.0
    now = time.time()
    due_time = (now - _LAST_OFFLOAD) >= interval_h * 3600.0
    due_size = False
    batch = _batch_lines()
    for path in _OFFLOAD_FILES:
        try:
            if os.path.exists(path) and os.path.getsize(path) > batch * 200:
                due_size = True
                break
        except Exception:
            continue
    if not (due_time or due_size):
        return
    _LAST_OFFLOAD = now
    res = offload_and_prune()
    try:
        n = sum((res.get("shipped") or {}).values())
        if n:
            log.info("VPS offload: shipped %d records, cleared local disk." % n)
            try:
                from data import notifier as _ntf
                _ntf.note_vps_offload("%d records across %d streams" % (n, len(res.get("shipped") or {})))
            except Exception:
                pass
            # Req-27: optional auto-backup pull of the consolidated VPS bundle
            # right after an offload, so a local copy of everything shipped is
            # always available without a manual /vpspull.
            try:
                if bool(getattr(c, "VPS_PULL_ON_OFFLOAD", False)):
                    pb = pull_bundle()
                    if pb.get("ok") and pb.get("path"):
                        log.info("VPS offload: pulled backup bundle -> %s" % pb.get("path"))
            except Exception:
                pass
    except Exception:
        pass


def pull_bundle(since=None):
    """Download the stored data bundle (dated zip) from the VPS to data/exports."""
    if not configured():
        return {"ok": False, "error": "not configured"}
    try:
        params = {"since": since} if since else {}
        resp = _get("/store/bundle", params=params, stream=True, timeout=max(_timeout(), 60))
        if resp.status_code == 204:
            return {"ok": True, "path": None}
        if resp.status_code != 200:
            return {"ok": False, "error": "HTTP %s" % resp.status_code}
        os.makedirs("data/exports", exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join("data/exports", "vps_bundle_%s.zip" % stamp)
        total = 0
        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    f.write(chunk)
                    total += len(chunk)
        return {"ok": True, "path": path, "size": total,
                "size_h": "%.1f MB" % (total / 1048576.0)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
