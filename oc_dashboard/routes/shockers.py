"""Shockers and Movers endpoints."""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response
from .. import config

router = APIRouter()

@router.get("/api/volume_shockers")
def volume_shockers(top_n: int = Query(30), filter_type: str = Query("all")):
    sql = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               COALESCE(ce_volume,0)+COALESCE(pe_volume,0) AS total_vol,
               DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE           -- active expiries only
          AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0   -- has live price
    ),
    latest AS (SELECT * FROM ts_ranked WHERE rk=1),
    prev   AS (SELECT * FROM ts_ranked WHERE rk=2)
    SELECT
        l.symbol, l.strike_price,
        CAST(l.expiry AS VARCHAR) AS expiry,
        l.total_vol               AS vol_now,
        p.total_vol               AS vol_prev,
        l.total_vol-p.total_vol   AS vol_delta,
        CASE WHEN p.total_vol>0
             THEN ROUND((l.total_vol-p.total_vol)*100.0/p.total_vol,1)
             ELSE NULL END        AS vol_pct_chg
    FROM latest l
    JOIN prev p ON l.symbol=p.symbol AND l.strike_price=p.strike_price AND l.expiry=p.expiry
    WHERE l.total_vol > p.total_vol AND l.total_vol > 0
    ORDER BY vol_delta DESC LIMIT ?
    """
    df = qdf(sql, [top_n * 3])  # fetch extra then filter
    if df.empty: return []
    idx_set = config.NSE_INDICES
    if filter_type == "index":  df = df[df["symbol"].isin(idx_set)]
    elif filter_type == "stock": df = df[~df["symbol"].isin(idx_set)]
    return to_records(df.head(top_n))


@router.get("/api/iv_shockers")
def iv_shockers(top_n: int = Query(30), filter_type: str = Query("all")):
    sql = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               (COALESCE(ce_iv,0)+COALESCE(pe_iv,0))/2.0 AS avg_iv,
               DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE           -- active expiries only
          AND (COALESCE(ce_iv,0) + COALESCE(pe_iv,0)) > 0     -- has live IV
    ),
    latest AS (SELECT * FROM ts_ranked WHERE rk=1),
    prev   AS (SELECT * FROM ts_ranked WHERE rk=2)
    SELECT
        l.symbol, l.strike_price,
        CAST(l.expiry AS VARCHAR) AS expiry,
        l.avg_iv                  AS iv_now,
        p.avg_iv                  AS iv_prev,
        l.avg_iv-p.avg_iv         AS iv_delta,
        ABS(l.avg_iv-p.avg_iv)    AS abs_iv_delta
    FROM latest l
    JOIN prev p ON l.symbol=p.symbol AND l.strike_price=p.strike_price AND l.expiry=p.expiry
    WHERE l.avg_iv>0 AND p.avg_iv>0
    ORDER BY abs_iv_delta DESC LIMIT ?
    """
    df = qdf(sql, [top_n * 3])
    if df.empty: return []
    if filter_type == "index":  df = df[df["symbol"].isin(config.NSE_INDICES)]
    elif filter_type == "stock": df = df[~df["symbol"].isin(config.NSE_INDICES)]
    return to_records(df.head(top_n))


@router.get("/api/top_movers")
def top_movers(
    side:        str = Query("CE"),
    top_n:       int = Query(20),
    filter_type: str = Query("all"),
):
    col = "ce_ltp" if side.upper() == "CE" else "pe_ltp"
    idx_list = ", ".join(f"'{s}'" for s in config.NSE_INDICES)
    sym_filter = (
        f"AND n.symbol IN ({idx_list})"      if filter_type == "index"
        else f"AND n.symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    base = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               COALESCE({col},0) AS ltp,
               DENSE_RANK() OVER (
                   PARTITION BY symbol, strike_price, expiry
                   ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE          -- exclude expired options
    ),
    latest AS (SELECT * FROM ts_ranked WHERE rk=1),
    prev   AS (SELECT * FROM ts_ranked WHERE rk=2)
    SELECT
        n.symbol, n.strike_price,
        CAST(n.expiry AS VARCHAR) AS expiry,
        n.ltp                     AS ltp_now,
        p.ltp                     AS ltp_prev,
        n.ltp-p.ltp               AS ltp_chg,
        CASE WHEN p.ltp>0
             THEN ROUND((n.ltp-p.ltp)*100.0/p.ltp,1)
             ELSE NULL END        AS ltp_pct_chg
    FROM latest n
    JOIN prev p ON n.symbol=p.symbol AND n.strike_price=p.strike_price AND n.expiry=p.expiry
    WHERE p.ltp>0                             -- at least one trade both snapshots
      AND n.ltp>0                             -- actively traded now
      {sym_filter}
    """
    return safe_response({
        "gainers": to_records(qdf(base + f" ORDER BY ltp_chg DESC LIMIT {top_n}")),
        "losers":  to_records(qdf(base + f" ORDER BY ltp_chg ASC  LIMIT {top_n}")),
    })


