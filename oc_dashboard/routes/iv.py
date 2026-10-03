"""IV endpoints — smile, rank, term structure, PC skew, OI-weighted IV."""
from __future__ import annotations
import numpy as np
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, ts_filter_clause, _ts_lo_hi, latest_ts, latest_data_date
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
    ts_clause, ts_params = ts_filter_clause(ts_filter)
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
          {ts_clause}
          AND (COALESCE(ce_iv,0)>0 OR COALESCE(pe_iv,0)>0)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + ts_params,
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
                    if fit_mask[i]:  # only where spline was actually fit (no extrapolation)
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
    ts_clause, ts_params = ts_filter_clause(ts_filter)
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
          {ts_clause}
          AND distance_from_atm = 0
        ORDER BY expiry
        """,
        [symbol] + ts_params,
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
                {ts_clause}
            )
            SELECT * FROM ranked WHERE rn = 1 ORDER BY expiry
            """,
            [symbol] + ts_params,
        )
    if df.empty:
        raise HTTPException(404, "No IV term structure data")

    # Compute mid-IV and skew per row
    df["mid_iv"]  = (df["ce_iv"] + df["pe_iv"]) / 2.0
    df["skew"]    = df["ce_iv"] - df["pe_iv"]     # CE-PE, market standard: negative = puts bid

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
    IV Rank and IV Percentile for a symbol.

    READS the values the nightly stored on exposure_eod. It does not compute
    them. Both the constant-maturity construction and the ranking live in
    oc_exposure_eod._update_iv_rank, with the parameters in exposure_core — so
    this endpoint, any screener column, and any backtest all quote one number.

    This previously derived the series per request from raw ocdata ticks, which
    meant a single bad print could pin min/max for 90 days, and the "90 day"
    lookback silently truncated to one contract's own life.

    IV Rank   = (iv - min) / (max - min) x 100     where we sit in the range
    IV Pctile = share of history strictly below    robust to a single spike
    Prefer the percentile when the range is narrow or one outlier dominates it.
    """
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)

    # Live ATM IV / realised vol for display (intraday, from ocdata).
    cur = qdf(
        f"""
        SELECT COALESCE(ce_iv, 0) AS ce_iv,
               COALESCE(pe_iv, 0) AS pe_iv,
               COALESCE(m_volatility, 0) AS rv
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          {ts_clause}
          AND distance_from_atm = 0
        LIMIT 1
        """,
        [symbol, expiry[:10]] + ts_params,
    )
    if cur.empty:
        raise HTTPException(404, "No current ATM IV data")

    ce_iv_now = float(cur["ce_iv"].iloc[0])
    pe_iv_now = float(cur["pe_iv"].iloc[0])
    atm_iv    = (ce_iv_now + pe_iv_now) / 2.0
    rv        = float(cur["rv"].iloc[0])

    # Stored rank as of the requested date (or the latest on or before it).
    as_of = ts_filter[:10]
    stored = qdf(
        """
        SELECT date, iv_cm30, iv_rank, iv_pctile, iv_rank_method, iv_rank_n,
               atm_iv, iv_change, hv20, em_1d
        FROM exposure_eod
        WHERE symbol = ? AND expiry_rank = 0 AND date <= CAST(? AS DATE)
        ORDER BY date DESC LIMIT 1
        """,
        [symbol, as_of],
    )

    def _f(row, col):
        if stored.empty or col not in stored.columns:
            return None
        v = row[col]
        try:
            f = float(v)
            return f if f == f else None
        except (TypeError, ValueError):
            return None

    if stored.empty:
        iv_rank = iv_pctile = ranked_iv = None
        method = None
        hist_count = 0
        iv_change = hv20 = em_1d = None
        as_of_date = None
    else:
        r = stored.iloc[0]
        iv_rank    = _f(r, "iv_rank")
        iv_pctile  = _f(r, "iv_pctile")
        ranked_iv  = _f(r, "iv_cm30")
        iv_change  = _f(r, "iv_change")
        hv20       = _f(r, "hv20")
        em_1d      = _f(r, "em_1d")
        n          = _f(r, "iv_rank_n")
        hist_count = int(n) if n is not None else 0
        m          = r["iv_rank_method"] if "iv_rank_method" in stored.columns else None
        method     = str(m) if m is not None and m == m else None
        as_of_date = str(r["date"])[:10]

    return safe_response({
        "symbol":       symbol,
        "expiry":       expiry[:10],
        "ce_iv":        round(ce_iv_now, 2),
        "pe_iv":        round(pe_iv_now, 2),
        "atm_iv":       round(atm_iv, 2),
        "rv":           round(rv, 2),
        "rv_iv_spread": round(rv - atm_iv, 2),
        "iv_rank":      round(iv_rank, 1) if iv_rank is not None else None,
        "iv_pctile":    round(iv_pctile, 1) if iv_pctile is not None else None,
        # The daily constant-maturity value that was ranked — shown next to the
        # live intraday atm_iv rather than implying they are the same thing.
        "ranked_iv":    round(ranked_iv, 2) if ranked_iv is not None else None,
        "iv_change":    round(iv_change, 2) if iv_change is not None else None,
        "hv20":         round(hv20, 2) if hv20 is not None else None,
        "em_1d":        round(em_1d, 2) if em_1d is not None else None,
        "method":       method,
        "as_of":        as_of_date,
        "lookback_days": config.IVRANK_LOOKBACK_DAYS,
        "hist_count":   hist_count,
        "min_samples":  config.IVRANK_MIN_SAMPLES,
    })


@router.get("/api/pc_skew")
def pc_skew(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Put-Call skew analysis:
    - 25-delta risk reversal: CE IV minus PE IV at ~25 delta (market standard:
      negative = puts bid). Matches exposure_eod.rr25, which is also CE-PE.
    - OTM skew: left-wing slope vs right-wing slope of IV smile
    - riskreversal, sentiment, regime columns from the ATM row
    - Time series of riskreversal across timestamps for this expiry
    """
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)
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
          {ts_clause}
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + ts_params,
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
        rr_25d        = round(ce_25d_iv - pe_25d_iv, 2)

    # OTM wing slopes (from smile spline — simple linear fit each side)
    atm_strike = float(atm_row.get("strike_price", spot))
    left  = df[(df["strike_price"] < atm_strike) & (df["pe_iv"] > 1)].copy()
    right = df[(df["strike_price"] > atm_strike) & (df["ce_iv"] > 1)].copy()

    left_slope = right_slope = skew_asymmetry = None
    # Slope units: np.polyfit on raw strikes gives IV points per 1 STRIKE POINT
    # (per rupee). The old `* 100` claimed "per 1% of spot" but 1% of spot is
    # spot/100 rupees, not 100 — those coincide only at spot = 10,000. At NIFTY
    # 23,635 the reported slope was 2.36x too small; on a Rs 500 stock it was
    # 20x too large, so the number was not comparable across symbols at all.
    _slope_scale = (spot / 100.0) if spot > 0 else 100.0
    if len(left) >= 3:
        c = np.polyfit(left["strike_price"].values, left["pe_iv"].values, 1)
        left_slope = round(float(c[0]) * _slope_scale, 4)   # IV pts per 1% of spot
    if len(right) >= 3:
        c = np.polyfit(right["strike_price"].values, right["ce_iv"].values, 1)
        right_slope = round(float(c[0]) * _slope_scale, 4)
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
    ts_clause, ts_params = ts_filter_clause(ts_filter)
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
        WHERE symbol = ?
          {ts_clause}
          AND COALESCE(ce_iv,0) > 0
        GROUP BY expiry, days_to_expiry
        ORDER BY expiry
        """,
        [symbol] + ts_params,
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
    df = qdf(
        f"""
        WITH latest_date AS (
            -- CAST(MAX(timestamp) AS DATE) instead of MAX(CAST(timestamp AS DATE)): the two are
            -- mathematically identical (date-truncation is monotonic in timestamp), but the first
            -- lets DuckDB answer from the timestamp column's min/max metadata directly, while the
            -- second forces a per-row CAST across the whole table (27M+ rows and growing) just to
            -- find today's date.
            SELECT CAST(MAX(timestamp) AS DATE) AS d FROM {tbl()}
        ),
        day_bounds AS (
            -- timestamp >= d AND < d+1day is the same set of rows as CAST(timestamp AS DATE) = d,
            -- but as a range on the raw column DuckDB can prune with zonemap metadata instead of
            -- casting every row to compare.
            SELECT symbol,
                   MIN(timestamp) AS ts_open,
                   MAX(timestamp) AS ts_now
            FROM {tbl()}, latest_date
            WHERE timestamp >= latest_date.d AND timestamp < latest_date.d + INTERVAL '1 day'
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
        """
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
        [symbol, expiry[:10], (latest_data_date(symbol) or date.today().isoformat())],
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



