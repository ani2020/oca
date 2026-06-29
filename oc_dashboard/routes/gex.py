"""GEX, Gamma profile/analysis, Max Pain endpoints."""
from __future__ import annotations
import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, ts_filter_clause, _ts_lo_hi, latest_ts
from ..cache import cache_get, cache_set
from ..helpers import _build_gamma_profile, _flip_and_magnet, _gamma_analysis_inline
from .. import config

router = APIRouter()

@router.get("/api/gex")
def gex_chart(
    symbol:    str = Query(...),
    expiry:    str = Query(...),
    timestamp: str = Query(...),
):
    """
    Fetch raw GEX columns; scale to ₹M here to avoid alias-referencing
    alias bug in DuckDB Binder.
    """
    ts_clause, ts_params = ts_filter_clause(timestamp)
    df = qdf(
        f"""
        SELECT
            strike_price,
            COALESCE(ce_gexv,          0) AS raw_ce_gex,
            COALESCE(pe_gexv,          0) AS raw_pe_gex,
            COALESCE(net_gexv,        0) AS raw_net_gexv,
            COALESCE(ce_oi,           0) AS ce_oi,
            COALESCE(pe_oi,           0) AS pe_oi,
            COALESCE(atm_strike,      0) AS atm_strike,
            COALESCE(underlying_price,0) AS spot
        FROM {tbl()}
        WHERE symbol    = ?
          AND expiry = ?
          {ts_clause}
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + ts_params,
    )
    if df.empty:
        raise HTTPException(404, "No GEX data for given filters")
    df["ce_gexv"]   = df["raw_ce_gex"]   / 1e6
    df["pe_gexv"]   = df["raw_pe_gex"]   / 1e6
    df["net_gexv"] = df["raw_net_gexv"] / 1e6
    df.drop(columns=["raw_ce_gex","raw_pe_gex","raw_net_gexv"], inplace=True)
    return to_records(df)


@router.get("/api/gamma_profile")
def gamma_profile(
    symbol:          str           = Query(...),
    expiry:          str           = Query(...),
    timestamp:       Optional[str] = Query(None),
    num_levels:      int           = Query(200),
    price_range_pct: float         = Query(5.0),
):
    ts_filter = timestamp or latest_ts(symbol)
    gdf, spot, dte, lot = _build_gamma_profile(
        symbol, expiry, ts_filter, num_levels, price_range_pct
    )
    if gdf.empty:
        raise HTTPException(404, "No data for gamma profile")
    gamma_flip, magnet = _flip_and_magnet(gdf)
    fut = gdf.attrs.get("fut") if hasattr(gdf, "attrs") else None
    return safe_response({
        "spot": spot, "fut": fut, "gamma_flip": gamma_flip,
        "magnet": magnet, "profile": to_records(gdf),
    })


@router.get("/api/gamma_analysis")
def gamma_analysis(
    symbol:          str           = Query(...),
    expiry:          str           = Query(...),
    timestamp:       Optional[str] = Query(None),
    price_range_pct: float         = Query(5.0),
    num_levels:      int           = Query(200),
):
    ts_filter = timestamp or latest_ts(symbol)
    gdf, spot, dte, lot = _build_gamma_profile(
        symbol, expiry, ts_filter, num_levels, price_range_pct
    )
    if gdf.empty:
        raise HTTPException(404, "No data")

    # ATM IV
    raw = qdf(
        f"""
        SELECT strike_price, COALESCE(atm_strike,0) AS atm_strike,
               COALESCE(ce_iv, 0) AS ce_iv
        FROM {tbl()}
        WHERE symbol = ?
          AND expiry  = ? 
          AND timestamp = ?
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], timestamp],
    )
    atm_str = float(raw["atm_strike"].iloc[0]) if not raw.empty else spot
    near    = raw.iloc[(raw["strike_price"] - atm_str).abs().argsort()[:2]]
    atm_iv_pct = float(near["ce_iv"].mean()) if not near.empty else 0.0
    atm_iv     = atm_iv_pct / 100.0

    # ATR — try three sources in order of preference:
    # 1. m_volatility column in oc.duckdb (annualised vol → daily ATR proxy)
    # 2. nse_fetcher historical OHLC (if module present)
    # 3. None (gamma analysis still runs, ATR-dependent fields show —)
    atr = None
    try:
        vol_df = qdf(
            f"SELECT AVG(COALESCE(m_volatility, 0)) AS avg_vol, "
            f"AVG(COALESCE(underlying_price, 0)) AS avg_spot "
            f"FROM {tbl()} "
            f"WHERE symbol = ? AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)",
            [symbol, symbol],
        )
        if not vol_df.empty:
            avg_vol  = float(vol_df["avg_vol"].iloc[0] or 0)
            avg_spot = float(vol_df["avg_spot"].iloc[0] or spot)
            if avg_vol > 0 and avg_spot > 0:
                # Convert annualised % vol to daily point ATR
                atr = round(avg_spot * (avg_vol / 100.0) / (252 ** 0.5), 2)
    except Exception:
        pass

    if atr is None:
        try:
            from nse_fetcher import NSEFetcher
            fetcher    = NSEFetcher()
            end_date   = date.today()
            start_date = end_date - timedelta(days=30)
            ohlc_df = (
                fetcher.get_index_historical(symbol, start_date, end_date)
                if NSEFetcher.is_index(symbol)
                else fetcher.get_equity_historical(symbol, start_date, end_date)
            )
            if ohlc_df is not None and not ohlc_df.empty and \
                    all(c in ohlc_df.columns for c in ("high","low","close")):
                high, low, close = ohlc_df["high"], ohlc_df["low"], ohlc_df["close"]
                tr = pd.concat([
                    high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs(),
                ], axis=1).max(axis=1)
                val = float(tr.rolling(14).mean().iloc[-1])
                if not np.isnan(val):
                    atr = val
        except ModuleNotFoundError:
            pass   # nse_fetcher not installed — silent, m_volatility path is preferred
        except Exception as exc:
            print(f"  ATR via NSEFetcher skipped: {exc}")

    result = _gamma_analysis_inline(
        gdf=gdf, spot=spot, dte=dte, atm_iv=atm_iv, atr=atr,
    )
    result.update(symbol=symbol, expiry=expiry, spot=spot,
                  fut=gdf.attrs.get("fut") if hasattr(gdf, "attrs") else None,
                  dte=dte,
                  atm_iv_pct=round(atm_iv_pct, 2) if atm_iv_pct else None)
    return safe_response(result)


# ===========================================================================
@router.get("/api/max_pain")
def max_pain(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Compute max pain strike for one expiry at a given timestamp.
    Max pain = strike where total intrinsic-value loss for option buyers is maximised,
    i.e. where option writers (MMs) lose least if price expires there.
    Returns: pain curve (intrinsic loss per potential expiry price) + max_pain_strike.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi = _ts_lo_hi(ts_filter)
    df = qdf(
        f"""
        SELECT strike_price,
               COALESCE(ce_oi, 0) AS ce_oi,
               COALESCE(pe_oi, 0) AS pe_oi,
               COALESCE(lotsize, 1) AS lotsize,
               COALESCE(underlying_price, 0) AS spot
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp = ?
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], timestamp],
    )
    if df.empty:
        raise HTTPException(404, "No data for max pain")

    strikes  = df["strike_price"].values
    ce_oi    = df["ce_oi"].values
    pe_oi    = df["pe_oi"].values
    lot      = max(int(df["lotsize"].iloc[0]), 1)
    spot     = float(df["spot"].iloc[0])

    # For each candidate expiry price, compute total intrinsic value (buyer loss = writer gain)
    pain = []
    for price in strikes:
        call_loss = float(np.sum(np.maximum(price - strikes, 0) * ce_oi * lot))
        put_loss  = float(np.sum(np.maximum(strikes - price, 0) * pe_oi * lot))
        pain.append({
            "price":     float(price),
            "call_pain": call_loss / 1e6,
            "put_pain":  put_loss  / 1e6,
            "total_pain":(call_loss + put_loss) / 1e6,
        })

    pain_df      = pd.DataFrame(pain)
    max_pain_idx = int(pain_df["total_pain"].idxmin())
    max_pain_strike = float(pain_df.loc[max_pain_idx, "price"])

    return safe_response({
        "symbol":          symbol,
        "expiry":          expiry[:10],
        "spot":            spot,
        "max_pain_strike": max_pain_strike,
        "distance_pts":    round(max_pain_strike - spot, 2),
        "distance_pct":    round((max_pain_strike - spot) / spot * 100, 3) if spot else None,
        "pain_curve":      to_records(pain_df),
    })


@router.get("/api/max_pain_series")
def max_pain_series(
    symbol: str = Query(...),
    expiry: str = Query(...),
):
    """
    Time series of max pain strike for one symbol+expiry across all timestamps in DB.
    Shows how max pain drifts intraday as OI changes.
    """
    # Get all distinct timestamps for this symbol+expiry
    ts_df = qdf(
        f"SELECT DISTINCT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts "
        f"FROM {tbl()} WHERE symbol = ? AND expiry = ? ORDER BY ts",
        [symbol, expiry[:10]],
    )
    if ts_df.empty:
        raise HTTPException(404, "No timestamps found")

    series = []
    for ts in ts_df["ts"].tolist():
        if ts is None or (isinstance(ts, float)):
            continue          # skip NaN rows produced by STRFTIME on NULL timestamps
        ts = str(ts)
        lo, hi = _ts_lo_hi(ts)
        df = qdf(
            f"""
            SELECT strike_price,
                   COALESCE(ce_oi, 0) AS ce_oi,
                   COALESCE(pe_oi, 0) AS pe_oi,
                   COALESCE(lotsize, 1) AS lotsize,
                   COALESCE(underlying_price, 0) AS spot
            FROM {tbl()}
            WHERE symbol = ? AND expiry = ?
              AND timestamp BETWEEN ? AND ?
            ORDER BY strike_price
            """,
            [symbol, expiry[:10], lo, hi],
        )
        if df.empty:
            continue
        strikes = df["strike_price"].values
        ce_oi   = df["ce_oi"].values
        pe_oi   = df["pe_oi"].values
        lot     = max(int(df["lotsize"].iloc[0]), 1)
        spot    = float(df["spot"].iloc[0])
        pain_vals = []
        for price in strikes:
            total = float(np.sum(np.maximum(price - strikes, 0) * ce_oi * lot) +
                          np.sum(np.maximum(strikes - price, 0) * pe_oi * lot))
            pain_vals.append(total)
        mp_idx = int(np.argmin(pain_vals))
        series.append({
            "timestamp":       ts,
            "max_pain_strike": float(strikes[mp_idx]),
            "spot":            spot,
            "distance_pts":    round(float(strikes[mp_idx]) - spot, 2),
        })

    return safe_response({"symbol": symbol, "expiry": expiry[:10], "series": series})


