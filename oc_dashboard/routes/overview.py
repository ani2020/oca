"""Overview and snapshot endpoints."""
from __future__ import annotations
import numpy as np
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, latest_ts
from ..cache import cache_get, cache_set
from .. import config

router = APIRouter()

@router.get("/api/overview")
def overview():
    cached = cache_get("overview")
    if cached is not None:
        return cached
    # Expiry scoping: every card metric below is an aggregate over the snapshot.
    # Without an expiry filter these summed ALL live expiries — for NIFTY that is
    # 18 chains (weeklies + monthlies + LEAPS out to 2031), which inverted the sign
    # of net_gex (+2.8e12 all-expiry vs -1.4e11 front-month) and pushed PCR the
    # wrong side of 1.0. Scope to NEAR (front monthly, DTE > 3) using the same
    # definition as the exposure EOD batch (_monthly_expiries in oc_exposure_eod)
    # so the overview and the exposure screen agree on what "the market" is.
    sql = f"""
    WITH latest AS (
        SELECT symbol, MAX(timestamp) AS ts
        FROM {tbl()} GROUP BY symbol
    ),
    exps AS (
        SELECT DISTINCT o.symbol, CAST(o.expiry AS DATE) AS e,
               CAST(l.ts AS DATE) AS snap_d
        FROM {tbl()} o
        JOIN latest l ON o.symbol = l.symbol AND o.timestamp = l.ts
        WHERE CAST(o.expiry AS DATE) >= CAST(l.ts AS DATE)
    ),
    monthly AS (
        SELECT symbol, e, snap_d, ROW_NUMBER() OVER (
            PARTITION BY symbol, DATE_TRUNC('month', e) ORDER BY e DESC) AS rn
        FROM exps
    ),
    near AS (
        SELECT symbol, e AS near_expiry, ROW_NUMBER() OVER (
            PARTITION BY symbol ORDER BY e) AS r
        FROM monthly
        WHERE rn = 1 AND DATE_DIFF('day', snap_d, e) > {config.DTE_EXCLUDE_OVERVIEW}
    ),
    sel AS (SELECT symbol, near_expiry FROM near WHERE r = 1)
    SELECT
        t.symbol,
        l.ts                               AS timestamp,
        s.near_expiry                      AS expiry,
        AVG(t.underlying_price)            AS spot,
        AVG(t.atm_strike)                  AS atm_strike,
        SUM(COALESCE(t.ce_oi,     0))      AS total_ce_oi,
        SUM(COALESCE(t.pe_oi,     0))      AS total_pe_oi,
        SUM(COALESCE(t.ce_volume, 0))      AS total_ce_vol,
        SUM(COALESCE(t.pe_volume, 0))      AS total_pe_vol,
        SUM(COALESCE(t.net_gexv,  0))      AS net_gex,
        AVG(COALESCE(t.ce_iv,     0))      AS avg_ce_iv,
        AVG(COALESCE(t.pe_iv,     0))      AS avg_pe_iv,
        MAX(COALESCE(t.lotsize,  1))       AS lot_size,
        CASE WHEN SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0)) > 0
             THEN SUM(COALESCE(t.ce_iv,0)*COALESCE(t.ce_oi,0)
                    + COALESCE(t.pe_iv,0)*COALESCE(t.pe_oi,0))
                / SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0))
             ELSE NULL END                 AS oi_wtd_iv,
        AVG(COALESCE(t.m_volatility,0)) - AVG(COALESCE(t.ce_iv,0)) AS rv_iv_spread,
        -- Expected move from pre-computed columns (ATM row values propagated to all rows)
        AVG(CASE WHEN t.distance_from_atm = 0
            THEN COALESCE(t.expected_move_straddle, 0) END)      AS exp_move_straddle,
        AVG(CASE WHEN t.distance_from_atm = 0
            THEN COALESCE(t.expected_move_theoretical, 0) END)   AS exp_move_theoretical
    FROM {tbl()} t
    JOIN latest l
      ON  t.symbol = l.symbol
      AND t.timestamp = l.ts
    JOIN sel s
      ON  t.symbol = s.symbol
      AND CAST(t.expiry AS DATE) = s.near_expiry
    GROUP BY t.symbol, l.ts, s.near_expiry
    ORDER BY t.symbol
    """
    df = qdf(sql)
    df["pcr"] = df.apply(
        lambda r: round(r["total_pe_oi"] / r["total_ce_oi"], 3)
        if r["total_ce_oi"] else None, axis=1,
    )
    result = to_records(df)
    cache_set("overview", result)
    return result


@router.get("/api/snapshot")
def snapshot(symbol: str = Query(...)):
    ts = latest_ts(symbol)
    # Scoped to NEAR (front monthly, DTE > 3) — same definition as the exposure
    # EOD batch. Previously these aggregates spanned every live expiry, which for
    # index symbols (NIFTY: 18 chains incl. weeklies and LEAPS) inverted net_gex's
    # sign and skewed PCR. num_expiries now reports how many chains exist for the
    # symbol while the metrics themselves describe only the front month.
    df = qdf(
        f"""
        WITH exps AS (
            SELECT DISTINCT CAST(expiry AS DATE) AS e
            FROM {tbl()}
            WHERE symbol = ? AND STRFTIME(timestamp, '%Y-%m-%d %H:%M') = ?
              AND CAST(expiry AS DATE) >= CAST(? AS DATE)
        ),
        monthly AS (
            SELECT e, ROW_NUMBER() OVER (
                PARTITION BY DATE_TRUNC('month', e) ORDER BY e DESC) AS rn
            FROM exps
        ),
        near AS (
            SELECT e FROM monthly
            WHERE rn = 1 AND DATE_DIFF('day', CAST(? AS DATE), e) > {config.DTE_EXCLUDE_OVERVIEW}
            ORDER BY e LIMIT 1
        )
        SELECT underlying_price, atm_strike,
            (SELECT e FROM near)         AS expiry,
            SUM(COALESCE(ce_oi,     0)) AS total_ce_oi,
            SUM(COALESCE(pe_oi,     0)) AS total_pe_oi,
            SUM(COALESCE(ce_volume, 0)) AS total_ce_vol,
            SUM(COALESCE(pe_volume, 0)) AS total_pe_vol,
            SUM(COALESCE(net_gexv,  0)) AS net_gex,
            AVG(COALESCE(ce_iv,     0)) AS avg_ce_iv,
            AVG(COALESCE(pe_iv,     0)) AS avg_pe_iv,
            (SELECT COUNT(*) FROM exps)  AS num_expiries
        FROM {tbl()}
        -- latest_ts() is minute precision ('YYYY-MM-DD HH:MM'); match by minute.
        -- Exact equality missed every snapshot with non-zero seconds -> 404.
        WHERE symbol = ? AND STRFTIME(timestamp, '%Y-%m-%d %H:%M') = ?
          AND CAST(expiry AS DATE) = (SELECT e FROM near)
        GROUP BY underlying_price, atm_strike LIMIT 1
        """,
        [symbol, ts, ts, ts, symbol, ts],
    )
    if df.empty:
        raise HTTPException(404, f"No snapshot for {symbol}")
    r = df.iloc[0].to_dict()
    r.update(symbol=symbol, timestamp=ts,
             pcr=round(r["total_pe_oi"]/r["total_ce_oi"],3) if r["total_ce_oi"] else None)
    return {k: (None if isinstance(v, float) and (np.isnan(v) or np.isinf(v)) else v)
            for k, v in r.items()}


@router.get("/api/overview_meta")
def overview_meta(
    expiry_rank: int = Query(0, description="0 = NEAR (front monthly), 1 = NEXT monthly"),
):
    """Lightweight: latest snapshot timestamp + today's exposure signal counts.
    Query-only (reads pre-computed exposure_eod); never computes on load.

    Signal counts are scoped to a single expiry_rank (default 0 = NEAR), matching
    the exposure screener's contract. Without this scoping the counts summed rows
    across BOTH near and next monthly expiries, roughly doubling every tally and
    surfacing next-month-only signals the screener never showed."""
    out = {"snapshot_ts": None, "signal_summary": {}, "exposure_date": None,
           "expiry_rank": expiry_rank}

    # Latest snapshot timestamp across the table (minute precision)
    try:
        ts = qdf(f"SELECT STRFTIME(MAX(timestamp), '%Y-%m-%d %H:%M') AS ts "
                 f"FROM {tbl()} WHERE timestamp IS NOT NULL")
        if not ts.empty and ts["ts"].iloc[0]:
            out["snapshot_ts"] = str(ts["ts"].iloc[0])
    except Exception:
        pass

    # Today's exposure signal counts (query-only, graceful if table missing)
    try:
        latest = qdf("SELECT MAX(date) AS d FROM exposure_eod WHERE expiry_rank = ?",
                     [expiry_rank])
        if not latest.empty and latest["d"].iloc[0] is not None:
            d = str(latest["d"].iloc[0])
            out["exposure_date"] = d
            rows = qdf("SELECT signals FROM exposure_eod "
                       "WHERE date = CAST(? AS DATE) AND expiry_rank = ? "
                       "AND signals != ''", [d, expiry_rank])
            counts = {}
            for s in rows["signals"].tolist():
                for sig in str(s).split(","):
                    sig = sig.strip()
                    if sig:
                        counts[sig] = counts.get(sig, 0) + 1
            # compression/release indicator counts
            ind = qdf("SELECT "
                      "SUM(CASE WHEN regime_compression THEN 1 ELSE 0 END) AS comp, "
                      "SUM(CASE WHEN compression_release THEN 1 ELSE 0 END) AS rel "
                      "FROM exposure_eod "
                      "WHERE date = CAST(? AS DATE) AND expiry_rank = ?",
                      [d, expiry_rank])
            if not ind.empty:
                counts["_compressing"] = int(ind["comp"].iloc[0] or 0)
                counts["_releasing"] = int(ind["rel"].iloc[0] or 0)
            out["signal_summary"] = counts
    except Exception:
        pass

    return safe_response(out)
