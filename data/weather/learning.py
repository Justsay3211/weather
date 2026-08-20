"""Local learning + calibration layer (prompt BIAS CORRECTION / CALIBRATION /
FUTURE INJECTION).

Three cooperating pieces, all pure-stdlib so they run on a small VPS or the bot
with no numpy / xgboost dependency:

  1. LocalGBM  - a genuine gradient-boosted regression-stump ensemble (squared
                 loss). This is the "smart local xg model": it learns the
                 residual (observed - model) as a function of features
                 [lead_h, hour, month, ensemble_spread, model_value] so it can
                 correct systematic, state-dependent model error. Retrains
                 lazily from a rolling sample buffer.
  2. ResidualLearner - per (location, variable) wrapper around LocalGBM plus a
                 fast rolling-mean bias fallback for cold-start.
  3. Calibrator - reliability calibration for probability forecasts
                 (precip_prob). Bins predicted vs observed frequency and
                 applies pool-adjacent-violators so the mapping is monotonic.

Every corrected value is returned ALONGSIDE the raw value (never replacing it)
so raw-vs-corrected performance stays comparable, exactly as the prompt demands.
"""

import json
import math
import os
import tempfile
import threading
from typing import Dict, List, Optional, Tuple


# --------------------------------------------------------------------------
# Gradient boosted regression stumps (squared loss)
# --------------------------------------------------------------------------
class _Stump:
    __slots__ = ("feat", "thresh", "left", "right")

    def __init__(self, feat: int, thresh: float, left: float, right: float):
        self.feat = feat
        self.thresh = thresh
        self.left = left
        self.right = right

    def predict(self, x: List[float]) -> float:
        return self.left if x[self.feat] <= self.thresh else self.right

    def to_json(self):
        return [self.feat, self.thresh, self.left, self.right]

    @staticmethod
    def from_json(j):
        return _Stump(int(j[0]), float(j[1]), float(j[2]), float(j[3]))


class LocalGBM:
    """Minimal gradient boosting for regression. Depth-1 trees (stumps) keep it
    fast, robust to tiny data, and immune to overfitting on the modest sample
    counts a single location accumulates."""

    def __init__(self, n_estimators: int = 40, learning_rate: float = 0.1,
                 min_samples: int = 20):
        self.n_estimators = int(n_estimators)
        self.learning_rate = float(learning_rate)
        self.min_samples = int(min_samples)
        self.base = 0.0
        self.stumps: List[_Stump] = []
        self.n_features = 0
        self.trained_n = 0

    def _best_stump(self, X, grad) -> Optional[_Stump]:
        n = len(X)
        if n < 2:
            return None
        best = None
        best_sse = None
        for f in range(self.n_features):
            vals = sorted(set(row[f] for row in X))
            if len(vals) < 2:
                continue
            # candidate thresholds = midpoints (cap count for speed)
            cands = vals
            if len(vals) > 12:
                step = len(vals) // 12
                cands = vals[::step]
            for t in cands:
                ls = lc = rs = rc = 0.0
                for row, g in zip(X, grad):
                    if row[f] <= t:
                        ls += g
                        lc += 1
                    else:
                        rs += g
                        rc += 1
                if lc == 0 or rc == 0:
                    continue
                lm = ls / lc
                rm = rs / rc
                sse = 0.0
                for row, g in zip(X, grad):
                    pred = lm if row[f] <= t else rm
                    d = g - pred
                    sse += d * d
                if best_sse is None or sse < best_sse:
                    best_sse = sse
                    best = _Stump(f, float(t), float(lm), float(rm))
        return best

    def fit(self, X: List[List[float]], y: List[float]) -> bool:
        if not X or len(X) < self.min_samples:
            return False
        self.n_features = len(X[0])
        self.base = sum(y) / len(y)
        preds = [self.base] * len(y)
        self.stumps = []
        for _ in range(self.n_estimators):
            grad = [yi - pi for yi, pi in zip(y, preds)]   # negative gradient of MSE
            stump = self._best_stump(X, grad)
            if stump is None:
                break
            for i, row in enumerate(X):
                preds[i] += self.learning_rate * stump.predict(row)
            self.stumps.append(stump)
        self.trained_n = len(X)
        return True

    def predict(self, x: List[float]) -> float:
        p = self.base
        for s in self.stumps:
            p += self.learning_rate * s.predict(x)
        return p

    def to_json(self):
        return {"base": self.base, "lr": self.learning_rate,
                "nf": self.n_features, "tn": self.trained_n,
                "stumps": [s.to_json() for s in self.stumps]}

    def load_json(self, j):
        self.base = float(j.get("base", 0.0))
        self.learning_rate = float(j.get("lr", self.learning_rate))
        self.n_features = int(j.get("nf", 0))
        self.trained_n = int(j.get("tn", 0))
        self.stumps = [_Stump.from_json(s) for s in j.get("stumps", [])]


def make_features(lead_hours: float, hour_of_day: int, month: int,
                  ensemble_spread: float, model_value: float) -> List[float]:
    return [float(lead_hours or 0.0), float(hour_of_day or 0),
            float(month or 0), float(ensemble_spread or 0.0),
            float(model_value or 0.0)]


# --------------------------------------------------------------------------
# Per (location, variable) residual learner
# --------------------------------------------------------------------------
class ResidualLearner:
    def __init__(self, path: Optional[str] = None, max_samples: int = 1500,
                 retrain_every: int = 40):
        self.path = path
        self.max_samples = int(max_samples)
        self.retrain_every = int(retrain_every)
        # key -> {"X": [...], "y": [...], "since": int, "bias": float, "n": int}
        self._buf: Dict[str, Dict] = {}
        self._models: Dict[str, LocalGBM] = {}
        self._lock = threading.RLock()
        if path:
            self.load()

    @staticmethod
    def _key(location: str, variable: str) -> str:
        return (location or "?") + "|" + variable

    def observe(self, location: str, variable: str, features: List[float],
                residual: float) -> None:
        """Record one (features -> observed-minus-model residual) sample."""
        if residual is None:
            return
        k = self._key(location, variable)
        with self._lock:
            buf = self._buf.get(k)
            if buf is None:
                buf = {"X": [], "y": [], "since": 0, "bias": 0.0, "n": 0}
                self._buf[k] = buf
            buf["X"].append([float(f) for f in features])
            buf["y"].append(float(residual))
            buf["n"] += 1
            buf["bias"] = buf["bias"] + (residual - buf["bias"]) * 0.05
            if len(buf["X"]) > self.max_samples:
                buf["X"] = buf["X"][-self.max_samples:]
                buf["y"] = buf["y"][-self.max_samples:]
            buf["since"] += 1
            if buf["since"] >= self.retrain_every:
                self._retrain(k)
                buf["since"] = 0

    def _retrain(self, k: str) -> None:
        buf = self._buf.get(k)
        if not buf:
            return
        gbm = LocalGBM()
        if gbm.fit(buf["X"], buf["y"]):
            self._models[k] = gbm

    def correct(self, location: str, variable: str, features: List[float],
                raw_value: float) -> Tuple[float, str]:
        """Return (corrected_value, method). Falls back to rolling-mean bias when
        the GBM has not trained yet, and to raw when nothing is known."""
        if raw_value is None:
            return raw_value, "raw"
        k = self._key(location, variable)
        gbm = self._models.get(k)
        if gbm is not None and gbm.stumps:
            return raw_value + gbm.predict([float(f) for f in features]), "gbm"
        buf = self._buf.get(k)
        if buf and buf.get("n", 0) >= 8:
            return raw_value + buf.get("bias", 0.0), "bias"
        return raw_value, "raw"

    def info(self, location: str, variable: str) -> Dict:
        k = self._key(location, variable)
        buf = self._buf.get(k) or {}
        gbm = self._models.get(k)
        return {"samples": buf.get("n", 0), "rolling_bias": round(buf.get("bias", 0.0), 3),
                "gbm_trees": len(gbm.stumps) if gbm else 0,
                "gbm_trained_n": gbm.trained_n if gbm else 0}

    # ---- persistence ---------------------------------------------------
    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as fh:
                j = json.load(fh) or {}
            self._buf = j.get("buf", {})
            for k, mj in (j.get("models", {}) or {}).items():
                m = LocalGBM()
                m.load_json(mj)
                self._models[k] = m
        except Exception:
            self._buf = {}
            self._models = {}

    def save(self):
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            payload = {"buf": self._buf,
                       "models": {k: m.to_json() for k, m in self._models.items()}}
            fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass


# --------------------------------------------------------------------------
# Probability calibration (reliability) with pool-adjacent-violators
# --------------------------------------------------------------------------
class Calibrator:
    def __init__(self, path: Optional[str] = None, n_bins: int = 10):
        self.path = path
        self.n_bins = int(n_bins)
        # key -> list of [sum_pred, sum_obs, count] per bin
        self._bins: Dict[str, List[List[float]]] = {}
        self._lock = threading.RLock()
        if path:
            self.load()

    def _bin_idx(self, p: float) -> int:
        i = int(p * self.n_bins)
        return max(0, min(self.n_bins - 1, i))

    def observe(self, key: str, predicted_prob: float, occurred: bool) -> None:
        if predicted_prob is None:
            return
        p = max(0.0, min(1.0, float(predicted_prob)))
        with self._lock:
            bins = self._bins.get(key)
            if bins is None:
                bins = [[0.0, 0.0, 0.0] for _ in range(self.n_bins)]
                self._bins[key] = bins
            b = bins[self._bin_idx(p)]
            b[0] += p
            b[1] += 1.0 if occurred else 0.0
            b[2] += 1.0

    def _reliability(self, key: str) -> List[Tuple[float, float]]:
        """Return monotonic [(mean_pred, calibrated_freq)] with PAV smoothing."""
        bins = self._bins.get(key)
        if not bins:
            return []
        pts = []
        for sp, so, c in bins:
            if c <= 0:
                continue
            pts.append([sp / c, so / c, c])
        if not pts:
            return []
        # pool adjacent violators to enforce non-decreasing calibrated freq
        i = 0
        while i < len(pts) - 1:
            if pts[i][1] > pts[i + 1][1]:
                c1, c2 = pts[i][2], pts[i + 1][2]
                merged_freq = (pts[i][1] * c1 + pts[i + 1][1] * c2) / (c1 + c2)
                merged_pred = (pts[i][0] * c1 + pts[i + 1][0] * c2) / (c1 + c2)
                pts[i] = [merged_pred, merged_freq, c1 + c2]
                del pts[i + 1]
                if i > 0:
                    i -= 1
            else:
                i += 1
        return [(p[0], p[1]) for p in pts]

    def calibrate(self, key: str, predicted_prob: float) -> float:
        if predicted_prob is None:
            return predicted_prob
        p = max(0.0, min(1.0, float(predicted_prob)))
        rel = self._reliability(key)
        total = sum(b[2] for b in (self._bins.get(key) or []))
        if len(rel) < 2 or total < 30:
            return p   # not enough data: trust raw probability
        # piecewise-linear interpolation across reliability points
        if p <= rel[0][0]:
            return rel[0][1]
        if p >= rel[-1][0]:
            return rel[-1][1]
        for j in range(len(rel) - 1):
            x0, y0 = rel[j]
            x1, y1 = rel[j + 1]
            if x0 <= p <= x1 and x1 > x0:
                frac = (p - x0) / (x1 - x0)
                return y0 + frac * (y1 - y0)
        return p

    def brier(self, key: str) -> Optional[float]:
        """Approximate Brier score from bin aggregates (lower is better)."""
        bins = self._bins.get(key)
        if not bins:
            return None
        num = 0.0
        n = 0.0
        for sp, so, c in bins:
            if c <= 0:
                continue
            mean_p = sp / c
            freq = so / c
            num += c * ((mean_p - freq) ** 2)
            n += c
        return (num / n) if n else None

    def load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path, "r") as fh:
                self._bins = json.load(fh) or {}
        except Exception:
            self._bins = {}

    def save(self):
        if not self.path:
            return
        try:
            d = os.path.dirname(self.path)
            if d and not os.path.exists(d):
                os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d or ".", suffix=".tmp")
            with os.fdopen(fd, "w") as fh:
                json.dump(self._bins, fh)
            os.replace(tmp, self.path)
        except Exception:
            pass
