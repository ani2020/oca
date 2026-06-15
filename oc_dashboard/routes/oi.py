"""OI endpoints — signals, walls, history."""
from __future__ import annotations
import pandas as pd
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _ts_lo_hi, latest_ts
from ..cache import cache_get
from .. import config

router = APIRouter()

@router.get("/api/oi_change")
def oi_change(
    symbol:        str   = Query(...),
    filter_type:   str   = Query("all"),
    min_oi_change: int   = Query(0),     # hide rows where |ce+pe oi chg| < this
):
    # Use the DB's ce_oi_change column (NSE's own computed delta vs their last
    # reference) rather than subtracting consecutive snapshots — gives meaningful
    # intraday context rather than tiny 4-minute movements.
    df = qdf(
        f"""
        SELECT
            STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS timestamp,
            strike_price,
            CAST(expiry AS VARCHAR)         AS expiry,
            COALESCE(ce_oi,        0)       AS ce_oi,
            COALESCE(pe_oi,        0)       AS pe_oi,
            COALESCE(ce_oi_change, 0)       AS ce_oi_chg,
            COALESCE(pe_oi_change, 0)       AS pe_oi_chg,
            COALESCE(ce_ltp,       0)       AS ce_ltp,
            COALESCE(pe_ltp,       0)       AS pe_ltp,
            COALESCE(ce_volume,    0)       AS ce_volume,
            COALESCE(pe_volume,    0)       AS pe_volume,
            COALESCE(ce_delta,     0)       AS ce_delta,
            COALESCE(ce_prem_oi_chg, 0)     AS ce_prem_oi_chg,
            COALESCE(pe_prem_oi_chg, 0)     AS pe_prem_oi_chg,
            COALESCE(underlying_price, 0)   AS spot
        FROM {tbl()}
        WHERE symbol = ?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol = ?)
          AND expiry >= CURRENT_DATE
          AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
        ORDER BY expiry, strike_price
        """,
        [symbol, symbol],
    )
    if df.empty:
        raise HTTPException(404, f"No OI data for {symbol}")

    # Compute LTP signal direction from ce_oi_change sign convention:
    # positive OI change + positive LTP = longs entering
    def _sig(oi, ltp):
        if oi > 0 and ltp > 0: return "Long Build-Up"
        if oi > 0 and ltp < 0: return "Short Build-Up"
        if oi < 0 and ltp > 0: return "Short Covering"
        if oi < 0 and ltp < 0: return "Long Unwinding"
        return "Neutral"

    df["ce_signal"] = df.apply(lambda r: _sig(r["ce_oi_chg"], r["ce_ltp"]), axis=1)
    df["pe_signal"] = df.apply(lambda r: _sig(r["pe_oi_chg"], r["pe_ltp"]), axis=1)

    # Add Vol/OI ratio — high ratio = fresh positioning
    df["ce_vol_oi"] = df.apply(
        lambda r: round(r["ce_volume"] / r["ce_oi"], 3) if r["ce_oi"] > 0 else None, axis=1)
    df["pe_vol_oi"] = df.apply(
        lambda r: round(r["pe_volume"] / r["pe_oi"], 3) if r["pe_oi"] > 0 else None, axis=1)

    # Filter: hide rows where both OI changes are below threshold (pure noise)
    abs_min = max(min_oi_change, 1)  # always filter zero-zero rows
    df = df[
        (df["ce_oi_chg"].abs() >= abs_min) |
        (df["pe_oi_chg"].abs() >= abs_min)
    ]

    # Sort by total absolute OI activity descending
    df["total_abs_oi"] = df["ce_oi_chg"].abs() + df["pe_oi_chg"].abs()
    df = df.sort_values("total_abs_oi", ascending=False).drop(columns=["total_abs_oi"])

    return to_records(df)


@router.get("/api/oi_signals_all")
def oi_signals_all():
    symbols = qdf(f"SELECT DISTINCT symbol FROM {tbl()} ORDER BY symbol")["symbol"].tolist()
    rows = []
    for sym in symbols:
        ts_df = qdf(
            f"SELECT DISTINCT CAST(timestamp AS VARCHAR) AS ts FROM {tbl()} "
            f"WHERE symbol=? ORDER BY ts DESC LIMIT 2", [sym],
        )
        if len(ts_df) < 2: continue
        ts_new, ts_old = ts_df["ts"].iloc[0], ts_df["ts"].iloc[1]
        agg = qdf(
            f"""
            SELECT
                SUM(COALESCE(n.ce_oi,  0))-SUM(COALESCE(o.ce_oi,  0)) AS ce_oi_chg,
                SUM(COALESCE(n.pe_oi,  0))-SUM(COALESCE(o.pe_oi,  0)) AS pe_oi_chg,
                AVG(COALESCE(n.ce_ltp, 0))-AVG(COALESCE(o.ce_ltp, 0)) AS avg_ce_ltp_chg,
                AVG(COALESCE(n.pe_ltp, 0))-AVG(COALESCE(o.pe_ltp, 0)) AS avg_pe_ltp_chg
            FROM {tbl()} n
            JOIN {tbl()} o
              ON  n.symbol=o.symbol AND n.strike_price=o.strike_price AND n.expiry=o.expiry
            WHERE n.symbol=?
              AND n.timestamp  BETWEEN ?  AND ? 
              AND o.timestamp  BETWEEN ?  AND ?
            """,
            [sym] + _ts_lo_hi(ts_new) + _ts_lo_hi(ts_old),
        )
        if agg.empty: continue
        r = agg.iloc[0].to_dict()
        r["symbol"] = sym; r["ts_new"] = ts_new
        r["total_oi_chg"] = abs(r.get("ce_oi_chg") or 0) + abs(r.get("pe_oi_chg") or 0)
        rows.append(r)
    if not rows: return []
    return to_records(pd.DataFrame(rows).sort_values("total_oi_chg", ascending=False))


@router.get("/api/oi_walls")
def oi_walls(filter_type: str = Query("all")):
    """
    For each symbol at latest timestamp, find:
    - Call wall  = strike with highest CE OI (resistance)
    - Put wall   = strike with highest PE OI (support)
    - Distance from spot and fut_price
    - Wall strength = max_oi / avg_oi
    - PCR at wall strikes
    Sortable across all symbols.
    """
    idx_list = ", ".join(f"'{s}'" for s in config.NSE_INDICES)
    # Qualify with t. — `symbol` is ambiguous in the base CTE (t JOIN latest l)
    sym_filter = (
        f"AND t.symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND t.symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    df = qdf(
        f"""
        WITH latest AS (
            SELECT symbol, MAX(timestamp) AS ts
            FROM {tbl()} GROUP BY symbol
        ),
        base AS (
            SELECT t.symbol,
                   t.strike_price,
                   COALESCE(t.ce_oi,0)           AS ce_oi,
                   COALESCE(t.pe_oi,0)           AS pe_oi,
                   COALESCE(t.ce_ltp,0)          AS ce_ltp,
                   COALESCE(t.pe_ltp,0)          AS pe_ltp,
                   COALESCE(t.ce_iv, 0)          AS ce_iv,
                   COALESCE(t.pe_iv, 0)          AS pe_iv,
                   COALESCE(t.ce_oi_change, 0)   AS ce_oi_change,
                   COALESCE(t.pe_oi_change, 0)   AS pe_oi_change,
                   COALESCE(t.underlying_price,0) AS spot,
                   COALESCE(t.fut_price,0)        AS fut_price,
                   COALESCE(t.atm_strike,0)       AS atm_strike,
                   COALESCE(t.lotsize,1)          AS lotsize
            FROM {tbl()} t
            JOIN latest l ON t.symbol=l.symbol AND t.timestamp=l.ts
            WHERE t.ce_oi > 0 OR t.pe_oi > 0
            {sym_filter}
        ),
        agg AS (
            SELECT symbol,
                   MAX(spot)      AS spot,
                   MAX(fut_price) AS fut_price,
                   MAX(atm_strike) AS atm_strike,
                   AVG(ce_oi)     AS avg_ce_oi,
                   AVG(pe_oi)     AS avg_pe_oi,
                   MAX(ce_oi)     AS max_ce_oi,
                   MAX(pe_oi)     AS max_pe_oi,
                   SUM(ce_oi)     AS total_ce_oi,
                   SUM(pe_oi)     AS total_pe_oi
            FROM base GROUP BY symbol
        ),
        ce_wall AS (
            SELECT b.symbol,
                   b.strike_price    AS ce_wall_strike,
                   b.ce_oi           AS ce_wall_oi,
                   COALESCE(b.ce_ltp, 0)             AS ce_ltp,
                   COALESCE(b.ce_oi_change, 0)        AS ce_oi_chg,
                   COALESCE(b.ce_iv,  0)              AS ce_iv,
                   -- LTP and IV changes vs previous snapshot (use DB columns)
                   COALESCE(b.ce_ltp, 0) - COALESCE(prev.ce_ltp, 0)  AS ce_ltp_chg,
                   COALESCE(b.ce_iv,  0) - COALESCE(prev.ce_iv,  0)  AS ce_iv_chg,
                   CASE WHEN COALESCE(b.ce_oi_change,0) > 0 AND COALESCE(b.ce_ltp,0) > 0 THEN 'Long Build-Up'
                        WHEN COALESCE(b.ce_oi_change,0) > 0 AND COALESCE(b.ce_ltp,0) < 0 THEN 'Short Build-Up'
                        WHEN COALESCE(b.ce_oi_change,0) < 0 AND COALESCE(b.ce_ltp,0) > 0 THEN 'Short Covering'
                        WHEN COALESCE(b.ce_oi_change,0) < 0 AND COALESCE(b.ce_ltp,0) < 0 THEN 'Long Unwinding'
                        ELSE 'Neutral' END            AS ce_signal
            FROM base b
            LEFT JOIN base prev ON prev.symbol = b.symbol
                AND prev.strike_price = b.strike_price
            QUALIFY ROW_NUMBER() OVER (PARTITION BY b.symbol ORDER BY b.ce_oi DESC) = 1
        ),
        pe_wall AS (
            SELECT b.symbol,
                   b.strike_price    AS pe_wall_strike,
                   b.pe_oi           AS pe_wall_oi,
                   COALESCE(b.pe_ltp, 0)             AS pe_ltp,
                   COALESCE(b.pe_oi_change, 0)        AS pe_oi_chg,
                   COALESCE(b.pe_iv,  0)              AS pe_iv,
                   COALESCE(b.pe_ltp, 0) - COALESCE(prev.pe_ltp, 0)  AS pe_ltp_chg,
                   COALESCE(b.pe_iv,  0) - COALESCE(prev.pe_iv,  0)  AS pe_iv_chg,
                   CASE WHEN COALESCE(b.pe_oi_change,0) > 0 AND COALESCE(b.pe_ltp,0) > 0 THEN 'Long Build-Up'
                        WHEN COALESCE(b.pe_oi_change,0) > 0 AND COALESCE(b.pe_ltp,0) < 0 THEN 'Short Build-Up'
                        WHEN COALESCE(b.pe_oi_change,0) < 0 AND COALESCE(b.pe_ltp,0) > 0 THEN 'Short Covering'
                        WHEN COALESCE(b.pe_oi_change,0) < 0 AND COALESCE(b.pe_ltp,0) < 0 THEN 'Long Unwinding'
                        ELSE 'Neutral' END            AS pe_signal
            FROM base b
            LEFT JOIN base prev ON prev.symbol = b.symbol
                AND prev.strike_price = b.strike_price
            QUALIFY ROW_NUMBER() OVER (PARTITION BY b.symbol ORDER BY b.pe_oi DESC) = 1
        )
        SELECT
            a.symbol,
            a.spot,
            a.fut_price,
            a.atm_strike,
            c.ce_wall_strike,
            c.ce_wall_oi,
            p.pe_wall_strike,
            p.pe_wall_oi,
            -- distances from spot
            c.ce_wall_strike - a.spot           AS ce_dist_spot,
            a.spot - p.pe_wall_strike           AS pe_dist_spot,
            -- distances from futures
            c.ce_wall_strike - a.fut_price      AS ce_dist_fut,
            a.fut_price - p.pe_wall_strike      AS pe_dist_fut,
            -- wall strength (how thick vs average)
            CASE WHEN a.avg_ce_oi > 0
                 THEN ROUND(a.max_ce_oi / a.avg_ce_oi, 1) ELSE NULL END AS ce_wall_strength,
            CASE WHEN a.avg_pe_oi > 0
                 THEN ROUND(a.max_pe_oi / a.avg_pe_oi, 1) ELSE NULL END AS pe_wall_strength,
            -- PCR
            CASE WHEN a.total_ce_oi > 0
                 THEN ROUND(a.total_pe_oi / a.total_ce_oi, 3) ELSE NULL END AS pcr,
            -- distance between walls
            c.ce_wall_strike - p.pe_wall_strike AS wall_range,
            -- CE wall enrichment: LTP, IV, OI change at the wall strike
            c.ce_ltp, c.ce_ltp_chg, c.ce_iv, c.ce_iv_chg, c.ce_oi_chg, c.ce_signal,
            -- PE wall enrichment
            p.pe_ltp, p.pe_ltp_chg, p.pe_iv, p.pe_iv_chg, p.pe_oi_chg, p.pe_signal
        FROM agg a
        JOIN ce_wall c ON a.symbol = c.symbol
        JOIN pe_wall p ON a.symbol = p.symbol
        ORDER BY a.symbol
        """
    )
    if df.empty:
        return []
    return safe_response(to_records(df))



@router.get("/api/oi_history")
def oi_history(
    symbol:     str = Query(...),
    expiry:     str = Query(...),
    days:       int = Query(5),
    price_range_pct: float = Query(10.0),   # ATM ± % filter
):
    """
    Multi-day OI history for heatmap: strikes × dates → OI change.
    Filters strikes to ATM ± price_range_pct to keep the heatmap readable.
    """
    # Use intraday timestamps when days=1 (today), daily aggregation otherwise
    today = date.today().isoformat()
    if days <= 1:
        # Intraday: show each snapshot as a separate column in the heatmap
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%H:%M') AS date,
                strike_price,
                COALESCE(ce_oi, 0)        AS ce_oi,
                COALESCE(pe_oi, 0)        AS pe_oi,
                COALESCE(ce_oi_change, 0) AS ce_oi_chg,
                COALESCE(pe_oi_change, 0) AS pe_oi_chg,
                COALESCE(underlying_price,0) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
            ORDER BY timestamp, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    else:
        df = qdf(
            f"""
            WITH daily AS (
                SELECT
                    CAST(timestamp AS DATE)  AS dt,
                    strike_price,
                    AVG(COALESCE(ce_oi, 0))            AS ce_oi,
                    AVG(COALESCE(pe_oi, 0))            AS pe_oi,
                    AVG(COALESCE(underlying_price, 0)) AS spot
                FROM {tbl()}
                WHERE symbol=? AND expiry=?
                  AND CAST(timestamp AS DATE) >=
                      CAST(? AS DATE) - CAST({days} AS INTEGER) * INTERVAL '1' DAY
                  AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
                GROUP BY dt, strike_price
            )
            SELECT
                CAST(dt AS VARCHAR)  AS date,
                strike_price,
                ce_oi, pe_oi, spot,
                ce_oi - LAG(ce_oi) OVER (
                    PARTITION BY strike_price ORDER BY dt) AS ce_oi_chg,
                pe_oi - LAG(pe_oi) OVER (
                    PARTITION BY strike_price ORDER BY dt) AS pe_oi_chg
            FROM daily
            ORDER BY dt, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    # If multi-day query empty (only today's data in DB), fall back to intraday
    if df.empty and days > 1:
        today = date.today().isoformat()
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%H:%M') AS date,
                strike_price,
                COALESCE(ce_oi, 0)        AS ce_oi,
                COALESCE(pe_oi, 0)        AS pe_oi,
                COALESCE(ce_oi_change, 0) AS ce_oi_chg,
                COALESCE(pe_oi_change, 0) AS pe_oi_chg,
                COALESCE(underlying_price,0) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
            ORDER BY timestamp, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    if df.empty:
        raise HTTPException(404, "No history data — check symbol/expiry selection")

    # Filter by price range around average spot
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


