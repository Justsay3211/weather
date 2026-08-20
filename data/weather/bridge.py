"""Bridge: translate a MasterForecastResult into the shapes the rest of the bot
consumes — grade/edge Features fields and a clear, human buy-message block.

Kept dependency-free (pure stdlib) so it imports safely on the bot or the VPS
and is fully offline-testable.
"""

from typing import Dict, Optional


def support_score(mf, variable: str = "temp_c") -> float:
    """0..1 'supporting factors' score for a variable: blends master confidence,
    model agreement (effective independent families), calibration presence and
    whether a learned bias-correction was applied. This is the single number
    that expresses 'the 1000s of calculations agree'."""
    vf = getattr(mf, "variables", {}).get(variable)
    if vf is None:
        return 0.5
    conf_term = max(0.0, min(1.0, (vf.confidence or 0) / 100.0))
    n = vf.n_independent or getattr(mf, "n_independent", 0) or 0
    agree_term = max(0.0, min(1.0, n / 4.0))
    calib_term = 1.0 if vf.probability is not None else 0.5
    corr_term = 1.0 if vf.correction_method == "gbm" else (0.7 if vf.correction_method == "bias" else 0.5)
    warn_pen = min(0.3, 0.1 * len(getattr(mf, "warnings", []) or []))
    score = 0.45 * conf_term + 0.25 * agree_term + 0.15 * calib_term + 0.15 * corr_term
    return max(0.0, min(1.0, score - warn_pen))


def features_from_master(mf, variable: str = "temp_c") -> Dict[str, object]:
    """Return kwargs to splat into grade_edge_engine.Features(**...): the wx_* set."""
    vf = getattr(mf, "variables", {}).get(variable)
    out = {
        "wx_n_independent": int(getattr(mf, "n_independent", 0) or 0),
        "wx_warnings": len(getattr(mf, "warnings", []) or []),
    }
    if vf is not None:
        out["wx_confidence"] = float(vf.confidence or 0)
        out["wx_support"] = support_score(mf, variable)
        out["wx_calibrated_prob"] = vf.probability
        out["wx_correction"] = vf.correction_method
        if vf.n_independent:
            out["wx_n_independent"] = int(vf.n_independent)
    return out


def _fmt(v, unit="", nd=1):
    if v is None:
        return "n/a"
    try:
        return ("%." + str(nd) + "f%s") % (float(v), unit)
    except Exception:
        return str(v)


def buy_message_block(mf, variable: str = "temp_c", max_models: int = 6) -> str:
    """A compact, clear block for Telegram buy alerts:
    confidence + label, best estimate + uncertainty band, calibrated chance,
    calibration/bias status, model agreement, warnings, and a short model log.
    Plain text (no markdown surprises)."""
    vf = getattr(mf, "variables", {}).get(variable)
    lines = []
    lines.append("🧠 Master weather intelligence")
    if vf is not None:
        band = ""
        if vf.low is not None and vf.high is not None:
            band = " (" + _fmt(vf.low) + "–" + _fmt(vf.high) + ")"
        lines.append("• Best estimate: %s%s" % (_fmt(vf.estimate), band))
        lines.append("• Confidence: %d/100 (%s)" % (vf.confidence, vf.confidence_label))
        if vf.probability is not None:
            lines.append("• Calibrated chance: %d%%" % int(round(vf.probability * 100)))
        corr = {"gbm": "ML bias-corrected", "bias": "bias-corrected",
                "raw": "raw ensemble"}.get(vf.correction_method, vf.correction_method)
        lines.append("• Calibration: %s" % corr)
        lines.append("• Model agreement: %d independent families" % (vf.n_independent or getattr(mf, "n_independent", 0)))
        if vf.reasons:
            lines.append("• Why: " + "; ".join(vf.reasons[:3]))
    warns = getattr(mf, "warnings", []) or []
    if warns:
        lines.append("⚠️ " + "; ".join(warns[:3]))
    mlog = getattr(mf, "model_log", []) or []
    used = [l for l in mlog if l.startswith("USED")][:max_models]
    if used:
        lines.append("• Models: " + ", ".join(l.split()[1] for l in used))
    return "\n".join(lines)
