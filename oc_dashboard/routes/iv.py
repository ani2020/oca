"""IV endpoints — smile, rank, term structure, PC skew, OI-weighted IV."""
from __future__ import annotations
import numpy as np
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, ts_filter_clause, _ts_lo_hi, latest_ts
from .. import config

router = APIRouter()

@router.get("/api/iv_smile")
def iv_smile(
    symbol:      str           = Query(...),
    expiry:      str           = Query(...),
    timestamp:   Optional[str] = Query(None),
    filter_type: str           = Query("all"),
):
    ts_filter = timestamp or latest_ts(symbol)
    df = qdf(
        f"""
        SELECT strike_price,
               COALESCE(ce_iv,     0) AS ce_iv,
               COALESCE(pe_iv,     0) AS pe_iv,
               COALESCE(ce_volume, 0) AS ce_volume,
               COALESCE(pe_volume, 0) AS pe_volume,
               COALESCE(ce_oi,     0) AS ce_oi,
               COALESCE(pe_oi,     0) AS pe_oi,
               COALESCE(underlying_price,0) AS spot
        FROM {tbl()}
        WHERE symbol = ?
          AND expiry = ? 
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
          AND (COALESCE(ce_iv,0)>0 OR COALESCE(pe_iv,0)>0)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
    )
    if df.empty:
        raise HTTPException(404, "No IV data found")

    from scipy.interpolate import UnivariateSpline

    def _fit(x_all: np.ndarray, y_all: np.ndarray):
        fit_mask = (y_all > 0.5) & (y_all < 150)
        xf, yf   = x_all[fit_mask], y_all[fit_mask]
        fitted   = np.full(len(x_all), np.nan)
        zscore   = np.full(len(x_all), np.nan)
        anomaly  = np.zeros(len(x_all), dtype=int)
        if len(xf) >= 5:
            try:
                s   = max(len(xf) * 0.5, 3.0)
                spl = UnivariateSpline(xf, yf, s=s, k=min(3, len(xf) - 1))
                fv  = spl(x_all)
                res = y_all - fv
                std = float(np.std(res[fit_mask])) or 1.0
                z   = res / std
                for i in range(len(x_all)):
                    if y_all[i] > 0:
                        fitted[i]  = float(fv[i])
                        zscore[i]  = float(z[i])
                        anomaly[i] = int(abs(z[i]) > 2.0 and fit_mask[i])
            except Exception as exc:
                print(f"  ⚠ Spline: {exc}")
        return fitted, zscore, anomaly

    strikes = df["strike_price"].values
    df["ce_iv_fit"], df["ce_zscore"], df["ce_anomaly"] = _fit(strikes, df["ce_iv"].values)
    df["pe_iv_fit"], df["pe_zscore"], df["pe_anomaly"] = _fit(strikes, df["pe_iv"].values)
    return to_records(df)


@router.get("/api/iv_term_structure")
def iv_term_structure(
    symbol:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    ATM CE and PE IV across all expiries at a given timestamp.
    Uses the row where distance_from_atm = 0 (ATM strike) per expiry.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)
    df = qdf(
        f"""
        SELECT
            CAST(expiry AS VARCHAR)         AS expiry,
            COALESCE(days_to_expiry, 0)     AS dte,
            COALESCE(ce_iv, 0)              AS ce_iv,
            COALESCE(pe_iv, 0)              AS pe_iv,
            COALESCE(ce_iv_nse, 0)          AS ce_iv_nse,
            COALESCE(underlying_price, 0)   AS spot,
            strike_price
        FROM {tbl()}
        WHERE symbol = ?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)
          AND distance_from_atm = 0
        ORDER BY expiry
        """,
        [symbol, symbol],
    )
    if df.empty:
        # Fallback: nearest strike to spot per expiry
        df = qdf(
            f"""
            WITH ranked AS (
                SELECT
                    CAST(expiry AS VARCHAR)       AS expiry,
                    COALESCE(days_to_expiry, 0)   AS dte,
                    COALESCE(ce_iv, 0)            AS ce_iv,
                    COALESCE(pe_iv, 0)            AS pe_iv,
                    COALESCE(ce_iv_nse, 0)        AS ce_iv_nse,
                    COALESCE(underlying_price, 0) AS spot,
                    strike_price,
                    ABS(distance_from_atm)        AS atm_dist,
                    ROW_NUMBER() OVER (
                        PARTITION BY expiry
                        ORDER BY ABS(distance_from_atm), strike_price
                    ) AS rn
                FROM {tbl()}
                AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)
            )
            SELECT * FROM ranked WHERE rn = 1 ORDER BY expiry
            """,
            [symbol, symbol],
        )
    if df.empty:
        raise HTTPException(404, "No IV term structure data")

    # Compute mid-IV and skew per row
    df["mid_iv"]  = (df["ce_iv"] + df["pe_iv"]) / 2.0
    df["skew"]    = df["pe_iv"] - df["ce_iv"]     # positive = put premium (normal)

    return safe_response({
        "symbol":    symbol,
        "timestamp": ts_filter[:16],
        "rows":      to_records(df[["expiry","dte","ce_iv","pe_iv","ce_iv_nse","mid_iv","skew","spot","strike_price"]]),
    })


@router.get("/api/iv_rank")
def iv_rank(
    symbol:        str           = Query(...),
    expiry:        str           = Query(...),
    timestamp:     Optional[str] = Query(None),
    lookback_days: int           = Query(90),
):
    """
    IV Rank and IV Percentile for ATM options of a given symbol+expiry.
    Lookback window is configurable (default 90 days from the selected timestamp).

    IV Rank     = (current_iv - min_iv) / (max_iv - min_iv)  × 100
    IV Pctile   = fraction of historical ATM IVs below current IV × 100
    RV-IV spread = m_volatility (realised annualised) - ATM IV
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)

    # Current ATM IV
    cur = qdf(
        f"""
        SELECT COALESCE(ce_iv, 0) AS ce_iv,
               COALESCE(pe_iv, 0) AS pe_iv,
               COALESCE(m_volatility, 0) AS rv
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
          AND distance_from_atm = 0
        LIMIT 1
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
    )
    if cur.empty:
        raise HTTPException(404, "No current ATM IV data")

    ce_iv_now = float(cur["ce_iv"].iloc[0])
    pe_iv_now = float(cur["pe_iv"].iloc[0])
    atm_iv    = (ce_iv_now + pe_iv_now) / 2.0
    rv        = float(cur["rv"].iloc[0])

    # Historical ATM IVs within lookback window
    cutoff = (datetime.strptime(ts_filter[:16], "%Y-%m-%d %H:%M")
              - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    hist = qdf(
        f"""
        SELECT (COALESCE(ce_iv,0) + COALESCE(pe_iv,0)) / 2.0 AS atm_iv
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp >= ?
          AND distance_from_atm = 0
          AND (COALESCE(ce_iv,0) + COALESCE(pe_iv,0)) > 0
        """,
        [symbol, expiry[:10], cutoff],
    )

    iv_rank = iv_pctile = None
    hist_count = 0
    if not hist.empty:
        vals = hist["atm_iv"].dropna().values
        hist_count = len(vals)
        if len(vals) >= 2:
            iv_min, iv_max = float(vals.min()), float(vals.max())
            iv_rank   = round((atm_iv - iv_min) / (iv_max - iv_min) * 100, 1)                         if iv_max > iv_min else None
            iv_pctile = round(float(np.mean(vals < atm_iv)) * 100, 1)

    return safe_response({
        "symbol":       symbol,
        "expiry":       expiry[:10],
        "ce_iv":        round(ce_iv_now, 2),
        "pe_iv":        round(pe_iv_now, 2),
        "atm_iv":       round(atm_iv, 2),
        "rv":           round(rv, 2),
        "rv_iv_spread": round(rv - atm_iv, 2),
        "iv_rank":      iv_rank,
        "iv_pctile":    iv_pctile,
        "lookback_days":lookback_days,
        "hist_count":   hist_count,
    })


@router.get("/api/pc_skew")
def pc_skew(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Put-Call skew analysis:
    - 25-delta risk reversal: IV of ~25-delta put minus ~25-delta call
    - OTM skew: left-wing slope vs right-wing slope of IV smile
    - riskreversal, sentiment, regime columns from the ATM row
    - Time series of riskreversal across timestamps for this expiry
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)

    # All strikes for this expiry/timestamp
    df = qdf(
        f"""
        SELECT
            strike_price,
            distance_from_atm,
            COALESCE(ce_iv,    0) AS ce_iv,
            COALESCE(pe_iv,    0) AS pe_iv,
            COALESCE(ce_delta, 0) AS ce_delta,
            COALESCE(pe_delta, 0) AS pe_delta,
            COALESCE(riskreversal, 0) AS riskreversal,
            CAST(sentiment AS VARCHAR)  AS sentiment,
            CAST(regime    AS VARCHAR)  AS regime,
            COALESCE(underlying_price, 0) AS spot
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
    )
    if df.empty:
        raise HTTPException(404, "No skew data")

    spot = float(df["spot"].iloc[0])

    # ATM row (distance_from_atm == 0)
    atm_rows = df[df["distance_from_atm"] == 0]
    atm_row  = atm_rows.iloc[0].to_dict() if not atm_rows.empty else {}

    # 25-delta RR: find CE strike with delta nearest 0.25 and PE with |delta| nearest 0.25
    ce_candidates = df[(df["ce_delta"] > 0) & (df["ce_iv"] > 0)].copy()
    pe_candidates = df[(df["pe_delta"] < 0) & (df["pe_iv"] > 0)].copy()

    rr_25d = None
    ce_25d_iv = pe_25d_iv = None
    ce_25d_strike = pe_25d_strike = None

    if not ce_candidates.empty and not pe_candidates.empty:
        ce_candidates["d_dist"] = (ce_candidates["ce_delta"] - 0.25).abs()
        pe_candidates["d_dist"] = (pe_candidates["pe_delta"].abs() - 0.25).abs()
        ce_25 = ce_candidates.loc[ce_candidates["d_dist"].idxmin()]
        pe_25 = pe_candidates.loc[pe_candidates["d_dist"].idxmin()]
        ce_25d_iv     = float(ce_25["ce_iv"])
        pe_25d_iv     = float(pe_25["pe_iv"])
        ce_25d_strike = float(ce_25["strike_price"])
        pe_25d_strike = float(pe_25["strike_price"])
        rr_25d        = round(pe_25d_iv - ce_25d_iv, 2)

    # OTM wing slopes (from smile spline — simple linear fit each side)
    atm_strike = float(atm_row.get("strike_price", spot))
    left  = df[(df["strike_price"] < atm_strike) & (df["pe_iv"] > 1)].copy()
    right = df[(df["strike_price"] > atm_strike) & (df["ce_iv"] > 1)].copy()

    left_slope = right_slope = skew_asymmetry = None
    if len(left) >= 3:
        c = np.polyfit(left["strike_price"].values, left["pe_iv"].values, 1)
        left_slope = round(float(c[0]) * 100, 4)   # IV change per 1% of spot
    if len(right) >= 3:
        c = np.polyfit(right["strike_price"].values, right["ce_iv"].values, 1)
        right_slope = round(float(c[0]) * 100, 4)
    if left_slope is not None and right_slope is not None:
        skew_asymmetry = round(abs(left_slope) - abs(right_slope), 4)

    # riskreversal time series for this symbol+expiry (ATM row only)
    # Risk reversal series: limit to the same date as the selected timestamp
    # to avoid pulling in years of history and compressing the x-axis.
    ts_date = ts_filter[:10]   # "YYYY-MM-DD"
    rr_ts_df = qdf(
        f"""
        SELECT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts,
               COALESCE(riskreversal, 0)             AS riskreversal,
               CAST(sentiment AS VARCHAR)             AS sentiment,
               CAST(regime    AS VARCHAR)             AS regime
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND distance_from_atm = 0
          AND CAST(timestamp AS DATE) = CAST(? AS DATE)
        ORDER BY timestamp
        """,
        [symbol, expiry[:10], ts_date],
    )

    return safe_response({
        "symbol":         symbol,
        "expiry":         expiry[:10],
        "spot":           spot,
        "atm_strike":     atm_strike,
        # 25-delta RR
        "rr_25d":         rr_25d,
        "ce_25d_iv":      ce_25d_iv,
        "pe_25d_iv":      pe_25d_iv,
        "ce_25d_strike":  ce_25d_strike,
        "pe_25d_strike":  pe_25d_strike,
        # Wing slopes
        "left_wing_slope":  left_slope,
        "right_wing_slope": right_slope,
        "skew_asymmetry":   skew_asymmetry,
        # ATM row signals from DB
        "riskreversal": _safe(atm_row.get("riskreversal")),
        "sentiment":    atm_row.get("sentiment", ""),
        "regime":       atm_row.get("regime", ""),
        # Time series
        "rr_series": to_records(rr_ts_df),
    })


@router.get("/api/oi_weighted_iv")
def oi_weighted_iv(
    symbol:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    OI-weighted average IV per expiry + overall.
    More accurate measure of the market's effective implied vol than simple average.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)
    df = qdf(
        f"""
        SELECT
            CAST(expiry AS VARCHAR)           AS expiry,
            COALESCE(days_to_expiry, 0)       AS dte,
            SUM(COALESCE(ce_iv,0) * COALESCE(ce_oi,0)) AS ce_iv_x_oi,
            SUM(COALESCE(pe_iv,0) * COALESCE(pe_oi,0)) AS pe_iv_x_oi,
            SUM(COALESCE(ce_oi,0))            AS total_ce_oi,
            SUM(COALESCE(pe_oi,0))            AS total_pe_oi
        FROM {tbl()}
        WHERE symbol = ? AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)
          AND COALESCE(ce_iv,0) > 0
        GROUP BY expiry, days_to_expiry
        ORDER BY expiry
        """,
        [symbol, symbol],
    )
    if df.empty:
        raise HTTPException(404, "No IV data")

    df["oi_wtd_ce_iv"] = df.apply(
        lambda r: r["ce_iv_x_oi"] / r["total_ce_oi"] if r["total_ce_oi"] > 0 else None, axis=1)
    df["oi_wtd_pe_iv"] = df.apply(
        lambda r: r["pe_iv_x_oi"] / r["total_pe_oi"] if r["total_pe_oi"] > 0 else None, axis=1)
    df["oi_wtd_avg_iv"] = df.apply(
        lambda r: (r["ce_iv_x_oi"] + r["pe_iv_x_oi"]) / (r["total_ce_oi"] + r["total_pe_oi"])
        if (r["total_ce_oi"] + r["total_pe_oi"]) > 0 else None, axis=1)

    total_ce_x = float(df["ce_iv_x_oi"].sum())
    total_pe_x = float(df["pe_iv_x_oi"].sum())
    total_ce   = float(df["total_ce_oi"].sum())
    total_pe   = float(df["total_pe_oi"].sum())
    overall    = (total_ce_x + total_pe_x) / (total_ce + total_pe) if (total_ce + total_pe) > 0 else None

    return safe_response({
        "symbol":         symbol,
        "overall_oi_iv":  round(overall, 2) if overall else None,
        "by_expiry":      to_records(df[["expiry","dte","oi_wtd_ce_iv","oi_wtd_pe_iv","oi_wtd_avg_iv"]]),
    })



@router.get("/api/iv_intraday_trend")
def iv_intraday_trend(filter_type: str = Query("all")):
    """
    For each symbol: OI-weighted ATM IV at first timestamp today vs latest.
    Returns iv_open, iv_now, iv_chg, iv_chg_pct, direction indicator.
    """
    idx_list = ", ".join(f"'{s}'" for s in config.NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    today = date.today().isoformat()
    df = qdf(
        f"""
        WITH day_bounds AS (
            SELECT symbol,
                   MIN(timestamp) AS ts_open,
                   MAX(timestamp) AS ts_now
            FROM {tbl()}
            WHERE CAST(timestamp AS DATE) = CAST(? AS DATE)
            {sym_filter}
            GROUP BY symbol
        ),
        iv_open AS (
            SELECT t.symbol,
                   SUM(COALESCE(t.ce_iv,0)*COALESCE(t.ce_oi,0)
                      +COALESCE(t.pe_iv,0)*COALESCE(t.pe_oi,0))
                   / NULLIF(SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0)),0) AS iv
            FROM {tbl()} t
            JOIN day_bounds d ON t.symbol=d.symbol AND t.timestamp=d.ts_open
            WHERE t.expiry >= CURRENT_DATE AND t.ce_iv > 0
            GROUP BY t.symbol
        ),
        iv_now AS (
            SELECT t.symbol,
                   SUM(COALESCE(t.ce_iv,0)*COALESCE(t.ce_oi,0)
                      +COALESCE(t.pe_iv,0)*COALESCE(t.pe_oi,0))
                   / NULLIF(SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0)),0) AS iv
            FROM {tbl()} t
            JOIN day_bounds d ON t.symbol=d.symbol AND t.timestamp=d.ts_now
            WHERE t.expiry >= CURRENT_DATE AND t.ce_iv > 0
            GROUP BY t.symbol
        )
        SELECT
            o.symbol,
            ROUND(o.iv, 2) AS iv_open,
            ROUND(n.iv, 2) AS iv_now,
            ROUND(n.iv - o.iv, 2) AS iv_chg,
            CASE WHEN o.iv > 0
                 THEN ROUND((n.iv - o.iv) / o.iv * 100, 2)
                 ELSE NULL END AS iv_chg_pct
        FROM iv_open o
        JOIN iv_now  n ON o.symbol = n.symbol
        ORDER BY ABS(n.iv - o.iv) DESC
        """,
        [today],
    )
    return [] if df.empty else to_records(df)


@router.get("/api/iv_history")
def iv_history(
    symbol:     str   = Query(...),
    expiry:     str   = Query(...),
    days:       int   = Query(5),
    price_range_pct: float = Query(10.0),
):
    """Multi-day IV history for heatmap: strikes × dates → ATM IV."""
    df = qdf(
        f"""
        WITH daily AS (
            SELECT
                CAST(timestamp AS DATE)  AS dt,
                strike_price,
                AVG(COALESCE(ce_iv, 0)) AS ce_iv,
                AVG(COALESCE(pe_iv, 0)) AS pe_iv,
                AVG(COALESCE(underlying_price, 0)) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND CAST(timestamp AS DATE) >= CAST(? AS DATE) - INTERVAL ({days}) DAYS
              AND COALESCE(ce_iv, 0) > 0
            GROUP BY dt, strike_price
        )
        SELECT
            CAST(dt AS VARCHAR) AS date,
            strike_price,
            ce_iv, pe_iv, spot,
            (ce_iv + pe_iv) / 2.0 AS avg_iv,
            (ce_iv + pe_iv) / 2.0
              - LAG((ce_iv + pe_iv) / 2.0) OVER (PARTITION BY strike_price ORDER BY dt) AS iv_chg
        FROM daily
        ORDER BY dt, strike_price
        """,
        [symbol, expiry[:10], date.today().isoformat()],
    )
    if df.empty:
        raise HTTPException(404, "No IV history")

    avg_spot = float(df["spot"].mean())
    if avg_spot > 0:
        lo_price = avg_spot * (1 - price_range_pct / 100)
        hi_price = avg_spot * (1 + price_range_pct / 100)
        df = df[(df["strike_price"] >= lo_price) & (df["strike_price"] <= hi_price)]

    dates   = sorted(df["date"].unique())
    strikes = sorted(df["strike_price"].unique())
    return safe_response({
        "symbol":  symbol,
        "expiry":  expiry[:10],
        "dates":   dates,
        "strikes": [float(s) for s in strikes],
        "rows":    to_records(df),
    })



