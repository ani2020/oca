"""OI endpoints — signals, walls, history."""
from __future__ import annotations
import pandas as pd
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _ts_lo_hi, latest_ts, latest_data_date
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
    # Single set-based query (was a 2-query-per-symbol loop = ~262 round-trips).
    # DENSE_RANK tags each symbol's two most recent snapshots; aggregate the
    # latest-vs-previous diff for ALL symbols in one pass.
    df = qdf(
        f"""
        WITH ranked AS (
            SELECT symbol, timestamp, strike_price, expiry,
                   COALESCE(ce_oi, 0)  AS ce_oi,
                   COALESCE(pe_oi, 0)  AS pe_oi,
                   COALESCE(ce_ltp, 0) AS ce_ltp,
                   COALESCE(pe_ltp, 0) AS pe_ltp,
                   DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
            FROM {tbl()}
        ),
        two AS (
            SELECT * FROM ranked WHERE rk <= 2
        ),
        -- ensure a symbol actually has two distinct snapshots
        valid AS (
            SELECT symbol FROM two GROUP BY symbol
            HAVING COUNT(DISTINCT timestamp) >= 2
        ),
        agg AS (
            SELECT t.symbol,
                   SUM(CASE WHEN rk=1 THEN ce_oi ELSE 0 END)
                     - SUM(CASE WHEN rk=2 THEN ce_oi ELSE 0 END) AS ce_oi_chg,
                   SUM(CASE WHEN rk=1 THEN pe_oi ELSE 0 END)
                     - SUM(CASE WHEN rk=2 THEN pe_oi ELSE 0 END) AS pe_oi_chg,
                   AVG(CASE WHEN rk=1 THEN ce_ltp END)
                     - AVG(CASE WHEN rk=2 THEN ce_ltp END) AS avg_ce_ltp_chg,
                   AVG(CASE WHEN rk=1 THEN pe_ltp END)
                     - AVG(CASE WHEN rk=2 THEN pe_ltp END) AS avg_pe_ltp_chg,
                   MAX(CASE WHEN rk=1 THEN CAST(timestamp AS VARCHAR) END) AS ts_new
            FROM two t
            JOIN valid v ON t.symbol = v.symbol
            GROUP BY t.symbol
        )
        SELECT symbol, ce_oi_chg, pe_oi_chg, avg_ce_ltp_chg, avg_pe_ltp_chg, ts_new,
               ABS(COALESCE(ce_oi_chg,0)) + ABS(COALESCE(pe_oi_chg,0)) AS total_oi_chg
        FROM agg
        ORDER BY total_oi_chg DESC
        """
    )
    if df.empty:
        return []
    return to_records(df)


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
        WITH ranked AS (
            -- tag each symbol's two most recent snapshots
            SELECT t.*,
                   DENSE_RANK() OVER (PARTITION BY t.symbol ORDER BY t.timestamp DESC) AS rk
            FROM {tbl()} t
            WHERE (t.ce_oi > 0 OR t.pe_oi > 0)
            {sym_filter}
        ),
        base AS (
            SELECT symbol, strike_price,
                   COALESCE(ce_oi,0)           AS ce_oi,
                   COALESCE(pe_oi,0)           AS pe_oi,
                   COALESCE(ce_ltp,0)          AS ce_ltp,
                   COALESCE(pe_ltp,0)          AS pe_ltp,
                   COALESCE(ce_iv, 0)          AS ce_iv,
                   COALESCE(pe_iv, 0)          AS pe_iv,
                   COALESCE(ce_oi_change, 0)   AS ce_oi_change,
                   COALESCE(pe_oi_change, 0)   AS pe_oi_change,
                   COALESCE(underlying_price,0) AS spot,
                   COALESCE(fut_price,0)        AS fut_price,
                   COALESCE(atm_strike,0)       AS atm_strike,
                   COALESCE(lotsize,1)          AS lotsize
            FROM ranked WHERE rk = 1
        ),
        prev_base AS (
            -- previous snapshot per symbol/strike (for real LTP/IV change)
            SELECT symbol, strike_price,
                   COALESCE(ce_ltp,0) AS ce_ltp,
                   COALESCE(pe_ltp,0) AS pe_ltp,
                   COALESCE(ce_iv, 0) AS ce_iv,
                   COALESCE(pe_iv, 0) AS pe_iv
            FROM ranked WHERE rk = 2
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
                   b.ce_ltp                           AS ce_ltp,
                   b.ce_oi_change                     AS ce_oi_chg,
                   b.ce_iv                            AS ce_iv,
                   -- real LTP/IV change vs previous snapshot
                   b.ce_ltp - COALESCE(prev.ce_ltp, b.ce_ltp)  AS ce_ltp_chg,
                   b.ce_iv  - COALESCE(prev.ce_iv,  b.ce_iv)   AS ce_iv_chg,
                   -- build-up matrix: OI change × PRICE change (not price level)
                   CASE WHEN b.ce_oi_change > 0 AND (b.ce_ltp - COALESCE(prev.ce_ltp,b.ce_ltp)) > 0 THEN 'Long Build-Up'
                        WHEN b.ce_oi_change > 0 AND (b.ce_ltp - COALESCE(prev.ce_ltp,b.ce_ltp)) < 0 THEN 'Short Build-Up'
                        WHEN b.ce_oi_change < 0 AND (b.ce_ltp - COALESCE(prev.ce_ltp,b.ce_ltp)) > 0 THEN 'Short Covering'
                        WHEN b.ce_oi_change < 0 AND (b.ce_ltp - COALESCE(prev.ce_ltp,b.ce_ltp)) < 0 THEN 'Long Unwinding'
                        ELSE 'Neutral' END            AS ce_signal
            FROM base b
            LEFT JOIN prev_base prev ON prev.symbol = b.symbol
                AND prev.strike_price = b.strike_price
            QUALIFY ROW_NUMBER() OVER (PARTITION BY b.symbol ORDER BY b.ce_oi DESC) = 1
        ),
        pe_wall AS (
            SELECT b.symbol,
                   b.strike_price    AS pe_wall_strike,
                   b.pe_oi           AS pe_wall_oi,
                   b.pe_ltp                           AS pe_ltp,
                   b.pe_oi_change                     AS pe_oi_chg,
                   b.pe_iv                            AS pe_iv,
                   b.pe_ltp - COALESCE(prev.pe_ltp, b.pe_ltp)  AS pe_ltp_chg,
                   b.pe_iv  - COALESCE(prev.pe_iv,  b.pe_iv)   AS pe_iv_chg,
                   CASE WHEN b.pe_oi_change > 0 AND (b.pe_ltp - COALESCE(prev.pe_ltp,b.pe_ltp)) > 0 THEN 'Long Build-Up'
                        WHEN b.pe_oi_change > 0 AND (b.pe_ltp - COALESCE(prev.pe_ltp,b.pe_ltp)) < 0 THEN 'Short Build-Up'
                        WHEN b.pe_oi_change < 0 AND (b.pe_ltp - COALESCE(prev.pe_ltp,b.pe_ltp)) > 0 THEN 'Short Covering'
                        WHEN b.pe_oi_change < 0 AND (b.pe_ltp - COALESCE(prev.pe_ltp,b.pe_ltp)) < 0 THEN 'Long Unwinding'
                        ELSE 'Neutral' END            AS pe_signal
            FROM base b
            LEFT JOIN prev_base prev ON prev.symbol = b.symbol
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
    # Anchor to the symbol's latest DATA date (not wall-clock) so stale-data
    # days (weekends/holidays/missed scrapes) don't return empty.
    today = latest_data_date(symbol) or date.today().isoformat()
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


