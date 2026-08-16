"""
Local ML Model - XGBoost-powered (with rule fallback), no API, fast inference.

Used as:
  - A standalone decision-maker when ML_API_KEY is not set.
  - A graceful fallback when the LLM API fails / times out.
  - A second opinion alongside the LLM.

Capabilities:
  - predict_entry: BUY/SKIP with probability
  - predict_exit:  HOLD/SELL with confidence
  - decide_hold:   BOOK/HOLD ladder for in-profit positions (mid-trade)
  - train_on_history: fit XGBoost on collected trade outcomes
"""

import json
import os
import pickle
from pathlib import Path
from typing import Dict, Optional

import numpy as np

from logger import log


FEATURE_NAMES = [
    "edge",              # our_prob - market_price
    "edge_ratio",        # our_prob / market_price
    "n_models",          # number of models in agreement
    "forecast_std",      # std across model forecasts
    "lead_hours",        # hours until resolution
    "market_price",      # current best_ask
    "market_spread_bps", # bid-ask spread
    "city_win_rate",     # historical win rate for this city
    "confidence",        # ensemble confidence
    "bucket_position",   # 0=tail, 0.5=mid, 1=near-certain
    "hour_of_day",       # UTC hour
    "day_of_week",       # 0=Mon, 6=Sun
]

# City historical win rates (from SII-WANGZJ 14M-trade calibration)
CITY_WIN_RATES = {
    "buenos-aires": 0.101, "dallas": 0.099, "atlanta": 0.098,
    "toronto": 0.097, "seattle": 0.095, "nyc": 0.088,
    "seoul": 0.086, "london": 0.084, "ankara": 0.082,
    "wellington": 0.082, "chicago": 0.079, "paris": 0.056,
    "austin": 0.047, "denver": 0.045, "tokyo": 0.038,
    "shanghai": 0.036, "hong-kong": 0.035, "singapore": 0.035,
    "mumbai": 0.035, "delhi": 0.034, "moscow": 0.033,
}


class LocalModel:
    """XGBoost-based fast local ML model for trade decisions."""

    def __init__(self, model_path: str = "data/xgb_model.json"):
        self.model_path = Path(model_path)
        self.model = None
        self._loaded = False
        self._load_or_init()

    def _load_or_init(self):
        try:
            import xgboost as xgb
            if self.model_path.exists():
                self.model = xgb.XGBClassifier()
                self.model.load_model(str(self.model_path))
                self._loaded = True
                log.info(f"  XGBoost model loaded ({self.model_path.stat().st_size} bytes)")
            else:
                log.info("  XGBoost model not found - using rule-based fallback")
        except ImportError:
            log.info("  xgboost not installed - using rule-based fallback")
        except Exception as e:
            log.warning(f"  XGBoost load failed: {e} - using rule-based")

    # ------------------------------------------------------------------ #
    # Entry
    # ------------------------------------------------------------------ #
    def predict_entry(self, features: dict) -> dict:
        if self.model is not None and self._loaded:
            return self._xgb_predict(features)
        return self._rules_predict(features)

    # ------------------------------------------------------------------ #
    # Exit (generic hold/sell)
    # ------------------------------------------------------------------ #
    def predict_exit(self, features: dict) -> dict:
        pnl_pct = features.get("pnl_pct", 0)
        forecast_changed = features.get("forecast_changed", False)
        hours_remaining = features.get("hours_remaining", 24)

        if pnl_pct >= 50:
            return {"action": "SELL", "confidence": 0.85, "source": "rules",
                    "reason": f"Profit target hit (+{pnl_pct:.0f}%)"}
        if forecast_changed and pnl_pct < 0:
            return {"action": "SELL", "confidence": 0.75, "source": "rules",
                    "reason": "Forecast reversed against position"}
        if hours_remaining < 2 and pnl_pct < -50:
            return {"action": "SELL", "confidence": 0.60, "source": "rules",
                    "reason": "Near resolution, deep loss"}
        return {"action": "HOLD", "confidence": 0.80, "source": "rules",
                "reason": "Hold to binary resolution"}

    # ------------------------------------------------------------------ #
    # Mid-trade BOOK vs HOLD ladder (profit management)
    # ------------------------------------------------------------------ #
    def decide_hold(self, features: dict) -> dict:
        """Rule-based profit manager used when the LLM is unavailable.
        Returns {action:'BOOK'|'HOLD', target_roi, confidence, reason, source}."""
        roi = float(features.get("roi_pct", 0.0))
        peak = float(features.get("peak_roi", roi))
        hrs = float(features.get("hours_remaining", 24) or 24)
        mode = features.get("mode", "ladder")
        give_back = peak - roi  # how far we've slipped from the high-water mark

        if mode == "cap":
            # Above the 300% cap: protect against the 500%->0 round-trip. If the
            # price is clearly fading from peak, lock it; otherwise ride to settle.
            if give_back >= 60:
                return {"action": "BOOK", "target_roi": roi, "confidence": 0.7,
                        "source": "rules", "reason": "cap: fading from peak, lock it"}
            return {"action": "HOLD", "target_roi": max(peak, roi), "confidence": 0.6,
                    "source": "rules", "reason": "cap: ride to settle"}

        # ladder mode (position is at/above the +profit target)
        if roi >= 50:
            return {"action": "BOOK", "target_roi": roi, "confidence": 0.7,
                    "source": "rules", "reason": "big gain - lock it"}
        if give_back >= 8:
            return {"action": "BOOK", "target_roi": roi, "confidence": 0.65,
                    "source": "rules", "reason": "pulled back from peak"}
        if roi >= 20 and hrs < 2:
            return {"action": "BOOK", "target_roi": roi, "confidence": 0.6,
                    "source": "rules", "reason": "near close - take it"}
        nxt = 20 if roi < 20 else (40 if roi < 40 else 50)
        return {"action": "HOLD", "target_roi": nxt, "confidence": 0.55,
                "source": "rules", "reason": f"run toward {nxt}%"}

    # ------------------------------------------------------------------ #
    # Inference internals
    # ------------------------------------------------------------------ #
    def _xgb_predict(self, features: dict) -> dict:
        try:
            x = np.array([[features.get(f, 0) for f in FEATURE_NAMES]], dtype=np.float32)
            prob = float(self.model.predict_proba(x)[0][1])
            action = "BUY" if prob > 0.55 else "SKIP"
            return {"action": action, "probability": prob, "confidence": prob, "source": "xgb"}
        except Exception as e:
            log.debug(f"XGBoost inference failed: {e} - falling back to rules")
            return self._rules_predict(features)

    def _rules_predict(self, features: dict) -> dict:
        edge = features.get("edge", 0)
        edge_ratio = features.get("edge_ratio", 0)
        n_models = features.get("n_models", 0)
        confidence = features.get("confidence", 0)
        spread_bps = features.get("market_spread_bps", 500)
        market_price = features.get("market_price", 0.5)
        city_win_rate = features.get("city_win_rate", 0.07)

        score = 0.0
        if edge > 0.10:
            score += 0.35
        elif edge > 0.05:
            score += 0.20
        elif edge > 0.02:
            score += 0.10

        if edge_ratio > 5.0:
            score += 0.30
        elif edge_ratio > 3.0:
            score += 0.20
        elif edge_ratio > 2.0:
            score += 0.10

        if n_models >= 5:
            score += 0.15
        elif n_models >= 3:
            score += 0.10

        if confidence > 0.80:
            score += 0.10

        if spread_bps > 1000:
            score -= 0.20
        elif spread_bps > 500:
            score -= 0.10

        if 0.10 <= market_price <= 0.50:
            score += 0.05

        # Favor historically stronger cities a touch.
        if city_win_rate >= 0.09:
            score += 0.05
        elif city_win_rate < 0.04:
            score -= 0.05

        score = max(0.0, min(0.95, score))
        action = "BUY" if score >= 0.50 else "SKIP"
        return {"action": action, "probability": score, "confidence": score, "source": "rules"}

    def train_on_history(self, trades: list) -> bool:
        if len(trades) < 50:
            log.info(f"  Need 50+ trades to train (have {len(trades)})")
            return False
        try:
            import xgboost as xgb
            X, y = [], []
            for t in trades:
                feats = t.get("features", {})
                if not feats:
                    continue
                X.append([feats.get(f, 0) for f in FEATURE_NAMES])
                y.append(1 if t.get("won", False) else 0)
            if len(X) < 50:
                log.info(f"  Only {len(X)} trades carry features - need 50+ to train")
                return False
            self.model = xgb.XGBClassifier(
                n_estimators=50, max_depth=4, learning_rate=0.1,
                objective="binary:logistic", eval_metric="logloss", verbosity=0,
            )
            self.model.fit(np.array(X), np.array(y))
            self.model.save_model(str(self.model_path))
            self._loaded = True
            log.info(f"  XGBoost trained on {len(X)} trades - saved to {self.model_path}")
            return True
        except ImportError:
            log.warning("  xgboost not installed - cannot train")
        except Exception as e:
            log.warning(f"  XGBoost training failed: {e}")
        return False

    def get_status(self) -> dict:
        return {
            "model": "XGBoost" if self._loaded else "Rules",
            "loaded": self._loaded,
            "trained": self._loaded,
        }


_instance: Optional[LocalModel] = None


def get_local_model() -> LocalModel:
    global _instance
    if _instance is None:
        _instance = LocalModel()
    return _instance
