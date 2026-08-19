"""
Run Manager — the MASTER run-id / runtime-id system.

Why this exists (user requirement)
----------------------------------
"a run id random number generated at each time; if same random number present it
checks if the runtime id present already, if no ok or change; the runtime id
must be master and perfectly built because it is crucial for bot logs and
correct running; recover-update adds the runtime id, and on recover it checks
all on runtime id and continues from the data of runtime."

What it guarantees
------------------
* Every boot mints a NEW random run_id (collision-checked against the local
  manifest AND, when reachable, the VPS store) so two runs never share an id.
* The run_id is the MASTER key stamped into every log line (via logger), every
  paper trade, every offloaded file name, and the session manifest.
* RECOVER mode: `resume(run_id)` re-attaches to a previous run_id instead of
  minting a new one, so a Railway redeploy / crash continues the SAME session
  timeline and its VPS data instead of starting a phantom fresh history.
* Fully fail-open + dependency-light (stdlib only). Never blocks trading.

Manifest: data/session_manifest.jsonl (append-only, one record per run event)
Pointer: data/current_run.json (the active run descriptor)
"""
from __future__ import annotations

import json
import os
import random
import socket
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

try:
    from config import Config  # type: ignore
except Exception:  # pragma: no cover
    Config = None  # type: ignore

try:
    from logger import log  # type: ignore
except Exception:  # pragma: no cover
    import logging
    log = logging.getLogger("run_manager")

_DATA_DIR = "data"
MANIFEST_PATH = os.path.join(_DATA_DIR, "session_manifest.jsonl")
POINTER_PATH = os.path.join(_DATA_DIR, "current_run.json")

_STATE: Dict[str, object] = {}


def _cfg(name, default):
    if Config is None:
        return default
    try:
        v = getattr(Config, name, default)
        return default if v is None else v
    except Exception:
        return default


def _enabled() -> bool:
    try:
        return bool(_cfg("RUN_ID_ENABLED", True))
    except Exception:
        return True


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _mint_id() -> str:
    """Random master run id: R-<yyyymmdd>-<8 hex>. The date prefix keeps ids
    human-sortable; the random suffix is the collision-checked unique part."""
    day = datetime.now(timezone.utc).strftime("%Y%m%d")
    suffix = "%08x" % random.getrandbits(32)
    return "R-%s-%s" % (day, suffix)


def _known_ids_local() -> set:
    ids = set()
    try:
        if os.path.exists(MANIFEST_PATH):
            with open(MANIFEST_PATH, "r") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        rid = (json.loads(ln) or {}).get("run_id")
                        if rid:
                            ids.add(rid)
                    except Exception:
                        continue
    except Exception:
        pass
    return ids


def _known_ids_vps() -> set:
    """Ask the VPS store for run ids it has seen (fail-open empty set)."""
    try:
        from data import vps_store
        if not vps_store.configured():
            return set()
        fn = getattr(vps_store, "known_run_ids", None)
        if callable(fn):
            return set(fn() or [])
    except Exception:
        pass
    return set()


def _collision_free_id() -> str:
    """Mint an id and REGENERATE while it already exists locally or on the VPS.
    This is the user's 'if same random number present, check runtime id; if not
    present ok, else change' rule."""
    known = _known_ids_local() | _known_ids_vps()
    for _ in range(12):
        rid = _mint_id()
        if rid not in known:
            return rid
    # Extremely unlikely: fall back to a time-salted id guaranteed unique.
    return "%s-%d" % (_mint_id(), int(time.time() * 1000) % 100000)


def _append_manifest(record: Dict[str, object]) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(MANIFEST_PATH, "a") as f:
            f.write(json.dumps(record, separators=(",", ":")) + "\n")
    except Exception as e:
        try:
            log.debug("run_manager manifest append failed: %s" % e)
        except Exception:
            pass


def _write_pointer(descriptor: Dict[str, object]) -> None:
    try:
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(POINTER_PATH, "w") as f:
            json.dump(descriptor, f)
    except Exception:
        pass


def _read_pointer() -> Optional[Dict[str, object]]:
    try:
        if os.path.exists(POINTER_PATH):
            with open(POINTER_PATH, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None


def start(mode: str = "fresh", resume_run_id: Optional[str] = None) -> Dict[str, object]:
    """Begin (or resume) a run and return the active descriptor.

    mode='fresh'   -> mint a NEW collision-free run_id.
    mode='recover' -> reuse resume_run_id (or the last pointer's id) so the
                      session timeline + VPS data continue from where they were.
    """
    global _STATE
    version = str(_cfg("VERSION", ""))
    host = ""
    try:
        host = socket.gethostname()
    except Exception:
        host = ""

    if not _enabled():
        _STATE = {"run_id": "R-disabled", "mode": mode, "version": version,
                  "started_at": _now_iso(), "host": host, "enabled": False}
        return dict(_STATE)

    if mode == "recover":
        rid = resume_run_id
        if not rid:
            prev = _read_pointer() or {}
            rid = prev.get("run_id")
        if not rid:
            # nothing to recover -> behave like fresh but flag it
            rid = _collision_free_id()
            mode = "fresh_no_prior"
    else:
        rid = _collision_free_id()

    boot = 1
    try:
        prev = _read_pointer() or {}
        if prev.get("run_id") == rid:
            boot = int(prev.get("boot_count", 1)) + 1
    except Exception:
        boot = 1

    descriptor = {
        "run_id": rid,
        "mode": mode,
        "version": version,
        "host": host,
        "started_at": _now_iso(),
        "boot_count": boot,
        "enabled": True,
    }
    _STATE = dict(descriptor)
    _write_pointer(descriptor)
    _append_manifest({"event": "start", **descriptor})
    try:
        log.info("\U0001F194 RUN %s (%s) boot#%d v%s" % (rid, mode, boot, version))
    except Exception:
        pass
    # Best-effort: tell the logger to stamp this id on every line.
    try:
        import logger as _lg
        if hasattr(_lg, "set_run_id"):
            _lg.set_run_id(rid)
    except Exception:
        pass
    return dict(descriptor)


def resume(resume_run_id: Optional[str] = None) -> Dict[str, object]:
    """Convenience wrapper for the recover-update path."""
    return start(mode="recover", resume_run_id=resume_run_id)


def current() -> Dict[str, object]:
    if not _STATE:
        # Lazy auto-start so run_id() is always usable even if start() was missed.
        try:
            return start(mode="fresh")
        except Exception:
            return {"run_id": "R-unknown", "mode": "lazy", "enabled": _enabled()}
    return dict(_STATE)


def run_id() -> str:
    return str(current().get("run_id", "R-unknown"))


def stamp(record: Dict[str, object]) -> Dict[str, object]:
    """Attach run_id + bot_version to any dict about to be persisted/offloaded."""
    try:
        cur = current()
        record.setdefault("run_id", cur.get("run_id"))
        record.setdefault("bot_version", cur.get("version"))
    except Exception:
        pass
    return record


def note(event: str, **extra) -> None:
    """Append an arbitrary manifest event stamped with the active run_id."""
    rec = {"event": event, "ts": _now_iso()}
    rec.update(extra)
    stamp(rec)
    _append_manifest(rec)


def history(limit: int = 20) -> List[Dict[str, object]]:
    out: List[Dict[str, object]] = []
    try:
        if os.path.exists(MANIFEST_PATH):
            with open(MANIFEST_PATH, "r") as f:
                for ln in f:
                    ln = ln.strip()
                    if not ln:
                        continue
                    try:
                        out.append(json.loads(ln))
                    except Exception:
                        continue
    except Exception:
        pass
    return out[-limit:]
