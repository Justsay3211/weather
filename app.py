#!/usr/bin/env python3
"""
WeatherPol Edge Node
====================
A tiny, fast, token-guarded service that does three jobs for the bot:

  1. Weather proxy + self-warming cache for Open-Meteo. The node owns a clean
     per-IP request quota and keeps a warm / last-good cache, so a momentary
     upstream hiccup still returns data (this is the "cache bridge" that fixes
     the P0 data starvation).
  2. Durable data store: the bot streams its jsonl here and deletes locally,
     so Railway's tiny disk never fills.
  3. Health / metrics for the bot's /vps* Telegram commands.

Auth: every endpoint EXCEPT /health requires  Authorization: Bearer <token>
(shared PROXY_AUTH_TOKEN). Plain HTTP is fine for this use; optional TLS via
the bundled Caddyfile + a domain if you want a padlock.
"""
import os, time, json, threading, io, zipfile, glob
from flask import Flask, request, jsonify, Response

try:
    import requests
except Exception:
    requests = None

APP_VERSION = "edge-1.2.0"  # 2026-08-20: UTC-daily open_meteo_today counter
GIT_SHA = os.getenv("GIT_SHA", "").strip()
VERSION_FULL = APP_VERSION + ((" (" + GIT_SHA + ")") if GIT_SHA else "")
START_TS = time.time()

TOKEN = os.getenv("PROXY_AUTH_TOKEN", "").strip()
UPSTREAM = os.getenv("OM_UPSTREAM", "https://api.open-meteo.com").rstrip("/")
UPSTREAM_FORECAST = os.getenv("OM_UPSTREAM_FORECAST", "").strip() or (UPSTREAM + "/v1/forecast")
OM_APIKEY = os.getenv("OM_APIKEY", "").strip()
CACHE_TTL = int(os.getenv("CACHE_TTL_SECONDS", "90"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL_SECONDS", "120"))
STALE_MAX = int(os.getenv("STALE_MAX_SECONDS", "10800"))
UPSTREAM_TIMEOUT = int(os.getenv("OM_TIMEOUT_SECONDS", "9"))
STORE_DIR = os.getenv("STORE_DIR", "/data/store")
STORE_MAX_MB = int(os.getenv("STORE_MAX_MB", "18000"))
PORT = int(os.getenv("PORT", "8080"))
REFRESH_MAX_IDLE = int(os.getenv("REFRESH_MAX_IDLE_SECONDS", "3600"))
REFRESH_MAX_KEYS = int(os.getenv("REFRESH_MAX_KEYS", "64"))

os.makedirs(STORE_DIR, exist_ok=True)

# Boot banner -> visible in `docker compose logs -f` so you can confirm which
# edge version/commit is actually running.
print("[edge] WeatherPol Edge %s starting | upstream=%s | cache_ttl=%ss poll=%ss idle_evict=%ss max_keys=%s | store=%s"
      % (VERSION_FULL, UPSTREAM_FORECAST, CACHE_TTL, POLL_INTERVAL, REFRESH_MAX_IDLE, REFRESH_MAX_KEYS, STORE_DIR), flush=True)

app = Flask(__name__)
_LOCK = threading.Lock()
_CACHE = {}
_METRICS = {"requests_total": 0, "by_route": {}, "polls_ok": 0, "polls_fail": 0,
            "last_poll_ts": 0.0, "cache_hits": 0, "cache_misses": 0, "model_points": {},
            # 2026-08-20: TODAY's Open-Meteo upstream calls, reset at UTC midnight.
            # polls_ok/polls_fail are since-boot; this is the daily budget view.
            "open_meteo_today": 0, "today_utc": ""}


def _authed():
    if not TOKEN:
        return True
    hdr = request.headers.get("Authorization", "")
    if hdr.startswith("Bearer "):
        return hdr[7:].strip() == TOKEN
    return request.headers.get("X-Auth-Token", "").strip() == TOKEN


def _count(route):
    _METRICS["requests_total"] += 1
    _METRICS["by_route"][route] = _METRICS["by_route"].get(route, 0) + 1


def _bump_today():
    """Increment the TODAY upstream-call counter, rolling over at UTC midnight.
    Called once per REAL Open-Meteo request (the single chokepoint in
    _fetch_upstream) so it reflects true daily budget consumption."""
    import datetime as _dt
    day = _dt.datetime.utcnow().strftime("%Y-%m-%d")
    if _METRICS.get("today_utc") != day:
        _METRICS["today_utc"] = day
        _METRICS["open_meteo_today"] = 0
    _METRICS["open_meteo_today"] += 1


@app.before_request
def _gate():
    if request.path == "/health":
        return None
    if not _authed():
        return jsonify({"error": "unauthorized"}), 401
    return None


def _cache_key(params):
    items = sorted((k, str(v)) for k, v in params.items())
    return "&".join("%s=%s" % (k, v) for k, v in items)


def _fetch_upstream(params):
    if requests is None:
        return None, "requests unavailable"
    p = dict(params)
    if OM_APIKEY and "apikey" not in p:
        p["apikey"] = OM_APIKEY
    _bump_today()  # count EVERY real upstream request against today's budget
    try:
        r = requests.get(UPSTREAM_FORECAST, params=p, timeout=UPSTREAM_TIMEOUT)
        if r.status_code != 200:
            return None, "HTTP %s" % r.status_code
        d = r.json()
        if isinstance(d, dict) and d.get("error"):
            return None, str(d.get("reason", "upstream error"))
        return d, None
    except Exception as e:
        return None, str(e)


def _track_models(data):
    try:
        hourly = data.get("hourly", {}) if isinstance(data, dict) else {}
        for k, v in hourly.items():
            if k == "time":
                continue
            _METRICS["model_points"][k] = sum(1 for x in (v or []) if x is not None)
    except Exception:
        pass


@app.route("/v1/forecast")
def forecast():
    _count("/v1/forecast")
    params = {k: v for k, v in request.args.items()}
    key = _cache_key(params)
    now = time.time()
    with _LOCK:
        hit = _CACHE.get(key)
    if hit and (now - hit["ts"]) < CACHE_TTL and hit.get("ok"):
        _METRICS["cache_hits"] += 1
        hit["last_access"] = now
        return jsonify(hit["data"])
    _METRICS["cache_misses"] += 1
    data, err = _fetch_upstream(params)
    if data is not None:
        _METRICS["polls_ok"] += 1
        _METRICS["last_poll_ts"] = now
        _track_models(data)
        with _LOCK:
            _CACHE[key] = {"ts": now, "data": data, "params": params, "ok": True, "last_access": now}
        return jsonify(data)
    _METRICS["polls_fail"] += 1
    if hit and (now - hit["ts"]) < STALE_MAX and hit.get("ok"):
        return jsonify(hit["data"])  # last-good bridge
    return jsonify({"error": True, "reason": "upstream unavailable: %s" % (err or "")}), 502


def _refresher():
    while True:
        time.sleep(max(15, POLL_INTERVAL))
        try:
            with _LOCK:
                keys = list(_CACHE.keys())
            # Cap warm keys: keep only the most-recently-accessed ones so a burst
            # of one-off locations cannot grow the warm set forever.
            if len(keys) > REFRESH_MAX_KEYS:
                with _LOCK:
                    ranked = sorted(_CACHE.items(),
                                    key=lambda kv: kv[1].get("last_access", kv[1].get("ts", 0)),
                                    reverse=True)
                    for k, _r in ranked[REFRESH_MAX_KEYS:]:
                        _CACHE.pop(k, None)
                    keys = [k for k, _ in ranked[:REFRESH_MAX_KEYS]]
            for key in keys:
                with _LOCK:
                    rec = _CACHE.get(key)
                if not rec:
                    continue
                # IDLE EVICTION: stop re-fetching forecasts no client has asked
                # for in REFRESH_MAX_IDLE seconds. This is the fix for runaway
                # Open-Meteo usage -- the warmer used to poll every cached key
                # forever, so a few days of scans could burn tens of thousands
                # of upstream calls with nobody consuming them.
                if (time.time() - rec.get("last_access", rec.get("ts", 0))) > REFRESH_MAX_IDLE:
                    with _LOCK:
                        _CACHE.pop(key, None)
                    continue
                data, err = _fetch_upstream(rec["params"])
                now = time.time()
                if data is not None:
                    _METRICS["polls_ok"] += 1
                    _METRICS["last_poll_ts"] = now
                    _track_models(data)
                    with _LOCK:
                        _CACHE[key] = {"ts": now, "data": data, "params": rec["params"],
                                       "ok": True, "last_access": rec.get("last_access", now)}
                else:
                    _METRICS["polls_fail"] += 1
        except Exception:
            pass


def _period_suffix(handling):
    """File-name suffix for a data-handling mode (Req-27 #c):
      full    -> one continuous file per stream (default)
      weekly  -> Monday-anchored week window, e.g. 2026-W33_aug10-aug16
      monthly -> calendar month, e.g. 2026-08_august
    """
    h = str(handling or "full").strip().lower()
    if h not in ("weekly", "monthly"):
        return "full"
    from datetime import datetime, timezone, timedelta
    now = datetime.now(timezone.utc)
    if h == "monthly":
        return now.strftime("%Y-%m_%B").lower()
    start = now - timedelta(days=now.weekday())
    end = start + timedelta(days=6)
    return "%s_%s-%s" % (start.strftime("%Y-W%V"),
                         start.strftime("%b%d").lower(),
                         end.strftime("%b%d").lower())


def _store_path(stream, handling="full"):
    safe = "".join(c for c in stream if c.isalnum() or c in "._-") or "misc"
    base = safe.replace(".jsonl", "")
    d = os.path.join(STORE_DIR, base)
    os.makedirs(d, exist_ok=True)
    suffix = _period_suffix(handling)
    if suffix == "full":
        # Default: one continuous file per stream (fixes the many-files-per-day
        # sprawl the bot's /vpspull used to download).
        return os.path.join(d, "%s_full.jsonl" % base)
    return os.path.join(d, "%s_%s.jsonl" % (base, suffix))


def _dir_bytes(path):
    tot = 0
    for root, _d, files in os.walk(path):
        for fn in files:
            try:
                tot += os.path.getsize(os.path.join(root, fn))
            except Exception:
                pass
    return tot


def _maybe_prune():
    try:
        if _dir_bytes(STORE_DIR) <= STORE_MAX_MB * 1048576:
            return
        for fn in sorted(glob.glob(os.path.join(STORE_DIR, "*", "*.jsonl"))):
            if _dir_bytes(STORE_DIR) <= STORE_MAX_MB * 1048576:
                break
            try:
                os.remove(fn)
            except Exception:
                pass
    except Exception:
        pass


@app.route("/store", methods=["POST"])
def store_put():
    _count("/store")
    try:
        body = request.get_json(force=True, silent=True) or {}
        stream = body.get("stream", "misc")
        handling = body.get("handling", "full")
        lines = body.get("lines", [])
        if not isinstance(lines, list):
            return jsonify({"ok": False, "error": "lines must be a list"}), 400
        path = _store_path(stream, handling)
        with open(path, "a") as f:
            for ln in lines:
                f.write((ln if isinstance(ln, str) else json.dumps(ln)) + "\n")
        _maybe_prune()
        return jsonify({"ok": True, "stored": len(lines), "stream": stream})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/store/list")
def store_list():
    _count("/store/list")
    out = {}
    for fn in glob.glob(os.path.join(STORE_DIR, "*", "*.jsonl")):
        try:
            out[os.path.relpath(fn, STORE_DIR)] = os.path.getsize(fn)
        except Exception:
            pass
    return jsonify({"ok": True, "files": out})


@app.route("/store/usage")
def store_usage():
    _count("/store/usage")
    used = _dir_bytes(STORE_DIR)
    try:
        st = os.statvfs(STORE_DIR)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
    except Exception:
        total = STORE_MAX_MB * 1048576
        free = max(0, total - used)
    streams = {}
    records = 0
    oldest = None
    for fn in glob.glob(os.path.join(STORE_DIR, "*", "*.jsonl")):
        s = os.path.basename(os.path.dirname(fn))
        try:
            n = sum(1 for _ in open(fn))
        except Exception:
            n = 0
        streams[s] = streams.get(s, 0) + n
        records += n
        try:
            mt = os.path.getmtime(fn)
            if oldest is None or mt < oldest:
                oldest = mt
        except Exception:
            pass
    return jsonify({"ok": True, "used_mb": round(used / 1048576.0, 1),
                    "total_mb": round(total / 1048576.0, 1),
                    "free_pct": round(100.0 * free / total, 1) if total else 0,
                    "records": records, "streams": streams,
                    "oldest": time.strftime("%Y-%m-%d %H:%M", time.gmtime(oldest)) if oldest else "-"})


@app.route("/store/bundle")
def store_bundle():
    _count("/store/bundle")
    files = glob.glob(os.path.join(STORE_DIR, "*", "*.jsonl"))
    if not files:
        return ("", 204)
    mem = io.BytesIO()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in files:
            zf.write(fn, arcname=os.path.join("vps_store_" + stamp, os.path.relpath(fn, STORE_DIR)))
    mem.seek(0)
    return Response(mem.read(), mimetype="application/zip",
                    headers={"Content-Disposition": "attachment; filename=vps_bundle_%s.zip" % stamp})


@app.route("/store/prune", methods=["POST"])
def store_prune():
    _count("/store/prune")
    body = request.get_json(force=True, silent=True) or {}
    before = body.get("before")
    removed = 0
    for fn in glob.glob(os.path.join(STORE_DIR, "*", "*.jsonl")):
        day = os.path.basename(fn).replace(".jsonl", "")
        if before is None or day < str(before):
            try:
                os.remove(fn); removed += 1
            except Exception:
                pass
    return jsonify({"ok": True, "removed": removed})


@app.route("/health")
def health():
    return jsonify({"ok": True, "version": VERSION_FULL, "uptime_s": int(time.time() - START_TS)})


@app.route("/metrics")
def metrics():
    _count("/metrics")
    now = time.time()
    mp = _METRICS["model_points"]
    fresh = [k for k, n in mp.items() if n > 0]
    silent = [k for k, n in mp.items() if n == 0]
    hits = _METRICS["cache_hits"]; misses = _METRICS["cache_misses"]
    hr = round(100.0 * hits / (hits + misses), 1) if (hits + misses) else 0.0
    last = _METRICS["last_poll_ts"]
    upstream_total = _METRICS["polls_ok"] + _METRICS["polls_fail"]
    return jsonify({"ok": True, "version": VERSION_FULL, "uptime_s": int(now - START_TS),
                    "requests_total": _METRICS["requests_total"],
                    "requests_total_note": "ALL edge routes (weather+store+metrics) since container start; NOT Open-Meteo upstream calls",
                    "open_meteo_upstream_total": upstream_total,
                    "open_meteo_today": _METRICS["open_meteo_today"],
                    "open_meteo_today_utc_date": _METRICS["today_utc"],
                    "requests": _METRICS["by_route"],
                    "weather": {"polls_ok": _METRICS["polls_ok"], "polls_fail": _METRICS["polls_fail"],
                                "open_meteo_upstream_total": upstream_total,
                                "open_meteo_today": _METRICS["open_meteo_today"],
                                "open_meteo_today_utc_date": _METRICS["today_utc"],
                                "last_poll_age_s": int(now - last) if last else -1,
                                "cache_hit_rate": hr, "cached_queries": len(_CACHE),
                                "fresh_models": fresh, "silent_models": silent}})


_t = threading.Thread(target=_refresher, daemon=True)
_t.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT)
