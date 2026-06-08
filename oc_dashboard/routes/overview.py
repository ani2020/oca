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
    sql = f"""
    WITH latest AS (
        SELECT symbol, MAX(timestamp) AS ts
        FROM {tbl()} GROUP BY symbol
    )
    SELECT
        t.symbol,
        l.ts                               AS timestamp,
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
    GROUP BY t.symbol, l.ts
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
    df = qdf(
        f"""
        SELECT underlying_price, atm_strike,
            SUM(COALESCE(ce_oi,     0)) AS total_ce_oi,
            SUM(COALESCE(pe_oi,     0)) AS total_pe_oi,
            SUM(COALESCE(ce_volume, 0)) AS total_ce_vol,
            SUM(COALESCE(pe_volume, 0)) AS total_pe_vol,
            SUM(COALESCE(net_gexv,  0)) AS net_gex,
            AVG(COALESCE(ce_iv,     0)) AS avg_ce_iv,
            AVG(COALESCE(pe_iv,     0)) AS avg_pe_iv,
            COUNT(DISTINCT expiry)       AS num_expiries
        FROM {tbl()} WHERE symbol = ? AND timestamp = ?
        GROUP BY underlying_price, atm_strike LIMIT 1
        """,
        [symbol, ts],
    )
    if df.empty:
        raise HTTPException(404, f"No snapshot for {symbol}")
    r = df.iloc[0].to_dict()
    r.update(symbol=symbol, timestamp=ts,
             pcr=round(r["total_pe_oi"]/r["total_ce_oi"],3) if r["total_ce_oi"] else None)
    return {k: (None if isinstance(v, float) and (np.isnan(v) or np.isinf(v)) else v)
            for k, v in r.items()}


