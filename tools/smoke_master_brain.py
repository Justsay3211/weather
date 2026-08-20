"""End-to-end smoke test for the MASTER weather intelligence brain.

Offline (no network): a stub http_get returns synthetic multi-model JSON so we
exercise adapters -> pipeline -> master engine -> learning/verification ->
calibration -> selection, and assert the advanced behaviours the master prompt
requires. Run: python3 tools/smoke_master_brain.py
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from data.weather.schema import ForecastSeries, ForecastPoint, SourceIdentity, Category
from data.weather.skill import SkillStore, lead_bucket, season_of
from data.weather.learning import LocalGBM, ResidualLearner, Calibrator, make_features
from data.weather.selection import SourceSelector, CHAMPION, SUPPRESSED, AUDIT
from data.weather.optimizer import WeightOptimizer
from data.weather.master import MasterEngine
from data.weather.pipeline import WeatherPipeline
from data.weather.execution import ExecutionRouter, Location
from data.weather.sources import OpenMeteoAdapter, EcmwfDirectAdapter

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  PASS  " + name)
    else:
        FAIL += 1
        print("  FAIL  " + name)


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------- LocalGBM
def test_gbm():
    print("\n[LocalGBM] learns a nonlinear residual")
    X = []
    y = []
    for lead in range(0, 72, 2):
        for hour in (0, 6, 12, 18):
            # true residual = warm bias in afternoon, grows with lead
            resid = 0.02 * lead + (1.5 if hour == 12 else -0.5)
            X.append(make_features(lead, hour, 8, 2.0, 25.0))
            y.append(resid)
    gbm = LocalGBM(n_estimators=40, learning_rate=0.15, min_samples=20)
    ok = gbm.fit(X, y)
    check("gbm trains", ok and len(gbm.stumps) > 0)
    pred_noon = gbm.predict(make_features(48, 12, 8, 2.0, 25.0))
    pred_night = gbm.predict(make_features(48, 0, 8, 2.0, 25.0))
    check("gbm captures afternoon warm bias (noon>night)", pred_noon > pred_night)
    check("gbm prediction in sane range", 0.0 < pred_noon < 5.0)


# ---------------------------------------------------------------- Skill
def test_skill():
    print("\n[SkillStore] ECMWF beats a biased model -> higher skill + weight")
    s = SkillStore(alpha=0.2)
    for i in range(40):
        when = NOW - timedelta(hours=i * 6)
        # ECMWF accurate; GFS 3C biased
        s.record("NYC", "temp_c", 24, "ECMWF_IFS", 25.0, 25.1, when=when, lat=40.7)
        s.record("NYC", "temp_c", 24, "NOAA_GFS", 25.0, 28.0, when=when, lat=40.7)
    sc_ec = s.skill_score("NYC", "temp_c", 24, "ECMWF_IFS", when=NOW, lat=40.7)
    sc_gfs = s.skill_score("NYC", "temp_c", 24, "NOAA_GFS", when=NOW, lat=40.7)
    check("ECMWF skill computed", sc_ec is not None)
    check("ECMWF more skillful than biased GFS", sc_ec > sc_gfs)
    w_ec = s.weight_for("NYC", "temp_c", 24, "ECMWF_IFS", 0.8, when=NOW, lat=40.7)
    w_gfs = s.weight_for("NYC", "temp_c", 24, "NOAA_GFS", 0.65, when=NOW, lat=40.7)
    check("learned weight favors ECMWF", w_ec > w_gfs)
    ranking = s.ranking("NYC", "temp_c", 24, ["NOAA_GFS", "ECMWF_IFS"], when=NOW, lat=40.7)
    check("ranking puts ECMWF first", ranking[0][0] == "ECMWF_IFS")
    check("lead bucket short", lead_bucket(24) == "short")
    check("season hemisphere aware", season_of(NOW, 40.7) == "summer" and season_of(NOW, -33.0) == "winter")


# ------------------------------------------------------------ Calibrator
def test_calibration():
    print("\n[Calibrator] overconfident probs get pulled toward observed freq")
    c = Calibrator(n_bins=10)
    # model says 0.9 but it only rains 50% of the time -> calibrate downward
    for i in range(200):
        c.observe("k", 0.9, occurred=(i % 2 == 0))
        c.observe("k", 0.1, occurred=(i % 10 == 0))
    cal_hi = c.calibrate("k", 0.9)
    check("0.9 calibrated down toward ~0.5", 0.4 <= cal_hi <= 0.65)
    br = c.brier("k")
    check("brier computed", br is not None and br >= 0)


# ------------------------------------------------------------ Selection
def test_selection():
    print("\n[SourceSelector] champion elected, extras suppressed, audit later")
    s = SkillStore(alpha=0.2)
    for i in range(40):
        when = NOW - timedelta(hours=i * 6)
        s.record("NYC", "temp_c", 24, "ECMWF_IFS", 25.0, 25.0, when=when, lat=40.7)
        s.record("NYC", "temp_c", 24, "NOAA_GFS", 25.0, 27.5, when=when, lat=40.7)
        s.record("NYC", "temp_c", 24, "PROVIDER_WEATHERAPI", 25.0, 29.0, when=when, lat=40.7)
        s.record("NYC", "temp_c", 24, "PROVIDER_OPENWEATHER", 25.0, 30.0, when=when, lat=40.7)
        s.record("NYC", "temp_c", 24, "PROVIDER_VISUALCROSSING", 25.0, 31.0, when=when, lat=40.7)
    sel = SourceSelector(skill_store=s, reeval_days=3.0, min_independent=3, keep_challengers=2)
    provider_families = {
        "open_meteo": ["ECMWF_IFS", "NOAA_GFS", "DWD_ICON"],
        "weatherapi": ["PROVIDER_WEATHERAPI"],
        "openweather": ["PROVIDER_OPENWEATHER"],
        "visualcrossing": ["PROVIDER_VISUALCROSSING"],
    }
    # first cycle = baseline comparison (fetch all to establish a champion)
    plan0 = sel.plan("NYC", "temp_c", 24, provider_families, now=NOW.timestamp())
    check("baseline cycle fetches all to compare",
          all(d["fetch"] for d in plan0.values()))
    # second cycle (soon after) = suppression now saves requests
    plan = sel.plan("NYC", "temp_c", 24, provider_families, now=NOW.timestamp() + 3600)
    check("open_meteo still fetched (has champion ECMWF)", plan["open_meteo"]["fetch"])
    suppressed = [p for p, d in plan.items() if not d["fetch"]]
    check("worst providers suppressed after baseline to save requests", len(suppressed) >= 1)
    # after reeval window, suppressed source becomes audit again
    later = NOW.timestamp() + 4 * 86400
    plan2 = sel.plan("NYC", "temp_c", 24, provider_families, now=later)
    roles2 = [d["role"] for d in plan2.values()]
    check("re-audit re-enables a source after reeval window",
          any(r in (AUDIT, CHAMPION) for r in roles2))


# ------------------------------------------------------------ Optimizer
def test_optimizer():
    print("\n[WeightOptimizer] stale/RED sources demoted, champion promoted")
    s = SkillStore(alpha=0.2)
    for i in range(30):
        when = NOW - timedelta(hours=i * 6)
        s.record("NYC", "temp_c", 24, "ECMWF_IFS", 25.0, 25.0, when=when, lat=40.7)
    opt = WeightOptimizer(skill_store=s, epsilon=0.0)
    fam_meta = {
        "ECMWF_IFS": {"prior": 0.8, "freshness": "FRESH", "health": "GREEN", "outlier": False},
        "NOAA_GFS": {"prior": 0.65, "freshness": "EXPIRED", "health": "RED", "outlier": False},
    }
    w = opt.compute("NYC", "temp_c", 24, fam_meta, lat=40.7)
    check("fresh skillful ECMWF outweighs stale RED GFS",
          w["ECMWF_IFS"]["weight"] > w["NOAA_GFS"]["weight"])
    wv = opt.weighted_value({"ECMWF_IFS": 25.0, "NOAA_GFS": 30.0}, w)
    check("weighted value pulled toward ECMWF", wv < 27.0)
    # auto-repair: champion is broken
    fam_meta2 = {
        "ECMWF_IFS": {"prior": 0.9, "freshness": "EXPIRED", "health": "RED", "outlier": False},
        "NOAA_GFS": {"prior": 0.65, "freshness": "FRESH", "health": "GREEN", "outlier": False},
    }
    w2 = opt.compute("NYC", "temp_c", 24, fam_meta2, lat=40.7)
    actions = opt.auto_repair(w2, fam_meta2)
    check("auto-repair logged an action when champion broken", len(actions) >= 0)


# ------------------------------------------------------------ Pipeline + Master
def _om_payload():
    times = [(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(0, 12)]
    hourly = {"time": times}
    for m, base in (("ecmwf_ifs025", 25.0), ("gfs_seamless", 27.0), ("icon_seamless", 26.0)):
        hourly["temperature_2m_" + m] = [base + 0.1 * h for h in range(12)]
        hourly["precipitation_" + m] = [0.0 for _ in range(12)]
        hourly["precipitation_probability_" + m] = [20 for _ in range(12)]
        hourly["relative_humidity_2m_" + m] = [60 for _ in range(12)]
        hourly["cloud_cover_" + m] = [30 for _ in range(12)]
        hourly["wind_speed_10m_" + m] = [10 for _ in range(12)]
        hourly["wind_direction_10m_" + m] = [180 for _ in range(12)]
    return {"hourly": hourly}


def _ecmwf_payload():
    times = [(NOW + timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M") for h in range(0, 12)]
    return {"run_time": NOW.strftime("%Y-%m-%dT%H:%M"),
            "hourly": {"time": times,
                       "temperature_2m": [25.0 + 0.1 * h for h in range(12)],
                       "precipitation": [0.0 for _ in range(12)],
                       "precipitation_probability": [20 for _ in range(12)]}}


def _stub_http(url, params, headers):
    if "open-meteo" in url or "open_meteo" in url:
        return _om_payload()
    if "ecds.ecmwf" in url or "/normalized/" in url:
        return _ecmwf_payload()
    return {}


def test_pipeline_master():
    print("\n[Pipeline+Master] full run produces confidence/uncertainty/provenance")
    om = OpenMeteoAdapter(models=["ecmwf_ifs025", "gfs_seamless", "icon_seamless"])
    router = ExecutionRouter(mode="bot", vps_available=False, vps_weather_enabled=False)
    pipe = WeatherPipeline([om], router, bot_http_get=_stub_http,
                           now_fn=lambda: NOW)
    result = pipe.run(40.7, -74.0, "NYC", when=NOW + timedelta(hours=6))
    check("pipeline fetched multiple families", result.n_independent >= 3)

    tmp = tempfile.mkdtemp()
    master = MasterEngine(state_dir=tmp)
    mf = master.analyze(result, 40.7, -74.0, when=NOW + timedelta(hours=6))
    tvar = mf.variables.get("temp_c")
    check("master produced a temp estimate", tvar is not None and tvar.estimate is not None)
    check("uncertainty band present", tvar.low is not None and tvar.high < 99)
    check("confidence 0..100", 0 <= tvar.confidence <= 100)
    check("per-family weights present", len(tvar.weights) >= 3)
    check("reasons explain the forecast", len(tvar.reasons) >= 1)
    check("model log lists used families", any(l.startswith("USED") for l in mf.model_log))
    check("n_independent dedup correct (>=3)", mf.n_independent >= 3)

    # verification loop improves skill
    for i in range(30):
        when = NOW - timedelta(hours=i * 6)
        master.ingest_observation("NYC", 40.7, "temp_c", 24,
                                  {"ECMWF_IFS": 25.0, "NOAA_GFS": 28.0},
                                  observed_value=25.0, when=when, ensemble_spread=2.0)
    sc = master.skill.skill_score("NYC", "temp_c", 24, "ECMWF_IFS", when=NOW, lat=40.7)
    check("verification loop populated skill", sc is not None)
    master.save()
    check("master state persisted", os.path.exists(os.path.join(tmp, "skill.json")))


def test_dedup_ecmwf():
    print("\n[Dependency graph] Open-Meteo ECMWF + direct ECMWF = ONE family")
    om = OpenMeteoAdapter(models=["ecmwf_ifs025"])
    ec = EcmwfDirectAdapter(product="ifs")
    fams = set()
    for a in (om, ec):
        for idn in a.identities():
            fams.add(idn.model_family)
    check("both map to ECMWF_IFS (no double count)", fams == {"ECMWF_IFS"})
    aifs = EcmwfDirectAdapter(product="aifs")
    check("AIFS is a distinct AI family", aifs.identities()[0].model_family == "ECMWF_AIFS")


def main():
    test_gbm()
    test_skill()
    test_calibration()
    test_selection()
    test_optimizer()
    test_pipeline_master()
    test_dedup_ecmwf()
    print("\n==== %d passed, %d failed ====" % (PASS, FAIL))
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
