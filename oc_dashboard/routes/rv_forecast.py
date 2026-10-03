"""
rv_forecast.py — RV forecast monitor panel (Phase 0: next-day + rest-of-day).

EXPERIMENTAL. Reads the rolling pseudo-OOS log from rv.duckdb (built offline
by rv_build.py / rv_model.py in C:\\Users\\aniru\\od\\claude -- see
areas/rv-forecast.md). next_day is backtest-only unless rv_model.py --live has
been scheduled; rest_of_day is backtest-only always (resolves same-evening, no
--live path exists for it yet). Surfaced for observation while history
accumulates -- weeks of forward evidence are wanted before any of this feeds a
trading decision. The frontend panel carries an "experimental" badge for the
same reason the vol screener carries "backtest only": a number on a dashboard
reads as validated whether or not it is, so the caveat has to be as visible as
the number.

Separate DB file (rv.duckdb, not oc.duckdb) on purpose: keeps this workstream
off the pending oc.duckdb partitioning work and off its market-hours write
lock. rv.duckdb is written only by an offline batch job after close, so it is
safe to read here at any time -- unlike oc.duckdb, which routes elsewhere in
this app read via db.qdf() and which blocks during market hours.

Metrics reuse straddle_core's canonical qlike() / pooled_mz_clustered() rather
than reimplementing them -- see rv_diagnostics.py's own note on why a second
copy of these estimators is a designed-in way to get two silent answers to the
same question.

Horizon differences a caller needs to know about
    'actual' means something different per horizon (mirrors rv_model.py's own
    score()): next_day compares against rv_day.rv_day (the target day's full
    session); rest_of_day compares against the SUM OF THE REMAINING BUCKETS
    after issue_bucket on the SAME day (target_d == issue_d for rod), computed
    from rv_bucket at query time so it can never drift out of sync with how
    the target was built in build_rod_feature_frame.

    Units: next_day's fcst is annualised (sqrt(fcst * TRADING_DAYS) * 100) --
    a full trading day's variance scaled to a year is the standard quantity.
    rest_of_day's fcst covers only a FRACTION of a session (2-6 of 7 buckets),
    so annualising it the same way overstates it; rod is reported as plain
    session-remaining sigma% (sqrt(fcst) * 100) instead, un-annualised, with a
    "units" field in every rod response so a consumer doesn't have to guess.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..db import to_records, safe_response
from .. import config

# oc/ is already on sys.path (run.py inserts it so `oc_dashboard` itself is
# importable), and straddle_core.py lives directly in oc/ -- so this needs no
# sys.path handling of its own.
import straddle_core as sc

router = APIRouter()

MIN_ISSUE_DAYS_READY = 20   # scorecard badge flips from "collecting" at this N
VALID_HORIZONS = ("next_day", "rest_of_day")


def _rv_db_path() -> str:
    """rv.duckdb lives alongside oc.duckdb, whatever path the app was started
    with -- same pattern app.py uses for icici_tickers.csv."""
    return str(Path(config.DB_FILE).with_name("rv.duckdb"))


def rv_qdf(sql: str, params: list = []):
    path = _rv_db_path()
    if not Path(path).exists():
        raise HTTPException(404, "rv.duckdb not found -- run rv_build.py / "
                                  "rv_model.py first (see areas/rv-forecast.md)")
    try:
        con = duckdb.connect(path, read_only=True)
        try:
            return con.execute(sql, params).df()
        finally:
            con.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _check_horizon(horizon: str) -> None:
    if horizon not in VALID_HORIZONS:
        raise HTTPException(400, f"horizon must be one of {VALID_HORIZONS}, got {horizon!r}")


def _fetch_scored(horizon: str) -> pd.DataFrame:
    """model, symbol, d, fcst, actual -- 'actual' computed per-horizon, see
    module docstring."""
    if horizon == "next_day":
        return rv_qdf("""
            SELECT f.model, f.symbol, f.target_d AS d, f.fcst, r.rv_day AS actual
            FROM rv_fcst f JOIN rv_day r ON r.symbol = f.symbol AND r.d = f.target_d
            WHERE f.horizon = 'next_day' AND r.rv_day > 0
        """)
    return rv_qdf("""
        SELECT f.model, f.symbol, f.target_d AS d, f.fcst, (
            SELECT sum(b.rv) FROM rv_bucket b
            WHERE b.symbol = f.symbol AND b.d = f.target_d AND b.bucket > f.issue_bucket
        ) AS actual
        FROM rv_fcst f WHERE f.horizon = 'rest_of_day'
    """)


def _vol_pct(x: np.ndarray, horizon: str) -> np.ndarray:
    """Variance -> vol%, annualised for next_day, plain session-remaining
    sigma% for rest_of_day -- see module docstring."""
    if horizon == "next_day":
        return np.sqrt(x * sc.TRADING_DAYS) * 100.0
    return np.sqrt(x) * 100.0


@router.get("/api/rv_forecast/scorecard")
def rv_scorecard(horizon: str = Query("next_day")):
    """QLIKE / bias / MZ panel per model, over everything scored so far.

    naive is the control (next_day: tomorrow=today persistence; rest_of_day:
    extrapolates today's own realised rate to the remaining buckets).
    ols_aug/ols_idx are the pooled models -- see rv_model.py for the feature
    set. A model has to clearly beat naive here before it means anything.
    """
    _check_horizon(horizon)
    df = _fetch_scored(horizon)
    df = df.dropna(subset=["actual", "fcst"])
    df = df[(df["actual"] > 0) & (df["fcst"] > 0)]
    if df.empty:
        return safe_response({"ready": False, "n_issue_days": 0, "horizon": horizon, "models": [],
                              "note": "no scored forecasts yet -- run rv_model.py"})

    n_issue_days = int(rv_qdf(
        "SELECT count(DISTINCT issue_d) n FROM rv_fcst WHERE horizon = ?",
        [horizon])["n"].iloc[0])

    out = []
    for model, g in df.groupby("model"):
        f = g["fcst"].to_numpy(float)
        a = g["actual"].to_numpy(float)
        ql = sc.qlike(f, a)
        mz = sc.pooled_mz_clustered(f, a, g["d"].to_numpy(), g["symbol"].to_numpy())
        bias = float(a.sum() / f.sum()) if f.sum() > 0 else None
        out.append({
            "model": model, "n": int(len(g)), "n_symbols": mz.get("n_symbols"),
            "qlike": round(ql, 4) if np.isfinite(ql) else None,
            "bias_ratio": round(bias, 4) if bias is not None else None,
            "mz_a": round(mz["alpha"], 4) if np.isfinite(mz["alpha"]) else None,
            "mz_b": round(mz["beta"], 4) if np.isfinite(mz["beta"]) else None,
            "mz_r2_within": round(mz["r2_within"], 4) if np.isfinite(mz["r2_within"]) else None,
        })
    out.sort(key=lambda r: (r["qlike"] is None, r["qlike"]))

    ready = n_issue_days >= MIN_ISSUE_DAYS_READY
    return safe_response({
        "ready": ready,
        "n_issue_days": n_issue_days,
        "horizon": horizon,
        "models": out,
        "note": (f"{n_issue_days} issue days scored." if ready else
                 f"Collecting -- {n_issue_days}/{MIN_ISSUE_DAYS_READY} issue "
                 f"days scored. Directional only until then."),
    })


@router.get("/api/rv_forecast/latest")
def rv_latest(model: str = Query("ols_aug"), horizon: str = Query("next_day"),
              limit: int = Query(400)):
    """Most recent issue day's per-symbol forecast.

    For rest_of_day this returns up to 5 rows per symbol (one per issue
    bucket 1-5, all sharing the same issue_d) -- issue_bucket is included so
    the caller can tell them apart. next_day is one row per symbol.
    """
    _check_horizon(horizon)
    day_df = rv_qdf("SELECT max(issue_d) AS d FROM rv_fcst WHERE model = ? AND horizon = ?",
                    [model, horizon])
    day = day_df["d"].iloc[0] if not day_df.empty else None
    if day is None or pd.isna(day):          # max() over no rows -> NULL -> NaT, not None
        return safe_response({"issue_d": None, "model": model, "horizon": horizon,
                              "units": None, "rows": []})

    df = rv_qdf("""
        SELECT symbol, issue_bucket, target_d, fcst
        FROM rv_fcst WHERE model = ? AND horizon = ? AND issue_d = ?
        ORDER BY issue_bucket, fcst DESC LIMIT ?
    """, [model, horizon, str(day)[:10], limit])
    units = "ann_vol_pct"
    if not df.empty:
        vol = _vol_pct(df["fcst"].to_numpy(float), horizon)
        if horizon == "next_day":
            df["ann_vol_pct"] = vol
        else:
            df["session_vol_pct"] = vol
            units = "session_vol_pct"
    return safe_response({"issue_d": str(day)[:10], "model": model, "horizon": horizon,
                          "units": units, "rows": to_records(df)})


@router.get("/api/rv_forecast/scatter")
def rv_scatter(model: str = Query("ols_aug"), horizon: str = Query("next_day")):
    """Forecast vs realised RV (vol %, see _vol_pct for units per horizon),
    for the tracking chart -- one point per (symbol, target_d[, issue_bucket]).
    """
    _check_horizon(horizon)
    df = _fetch_scored(horizon)
    df = df.dropna(subset=["actual", "fcst"])
    df = df[(df["actual"] > 0) & (df["fcst"] > 0)]
    if df.empty:
        return safe_response({"model": model, "horizon": horizon, "units": None, "rows": []})
    df = df[df["model"] == model]
    if df.empty:
        return safe_response({"model": model, "horizon": horizon, "units": None, "rows": []})
    df["fcst_vol"] = _vol_pct(df["fcst"].to_numpy(float), horizon)
    df["actual_vol"] = _vol_pct(df["actual"].to_numpy(float), horizon)
    units = "ann_vol_pct" if horizon == "next_day" else "session_vol_pct"
    return safe_response({"model": model, "horizon": horizon, "units": units,
                          "rows": to_records(df[["symbol", "d", "fcst_vol", "actual_vol"]])})
