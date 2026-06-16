"""Screener endpoints — delta, premium lens, strike trend, snapshot."""
from __future__ import annotations
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, ts_filter_clause, _ts_lo_hi, expiry_clause, latest_ts
from .. import config

router = APIRouter()

@router.get("/api/atm_strikes")
def atm_strikes(
    symbol: str           = Query(...),
    expiry: str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """Return distinct distance_from_atm values and corresponding strikes."""
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)
    df = qdf(
        f"SELECT DISTINCT distance_from_atm, strike_price, atm_strike "
        f"FROM {tbl()} WHERE symbol=? AND expiry  = ?  "
        f"{ts_clause} ORDER BY distance_from_atm",
        [symbol, expiry[:10]] + ts_params,
    )
    return to_records(df) if not df.empty else []


@router.get("/api/strike_trend")
def strike_trend(
    symbol:       str   = Query(...),
    strike_price: float = Query(...),
    expiry:       str   = Query(...),
    metric:       str   = Query("ce_ltp"),
):
    if metric not in config.SAFE_METRICS:
        raise HTTPException(400, f"Unknown metric '{metric}'")
    df = qdf(
        f"""
        SELECT CAST(timestamp AS VARCHAR) AS timestamp,
               COALESCE({metric},0)       AS value
        FROM {tbl()}
        WHERE symbol=? AND strike_price=? AND expiry  = ? 
        ORDER BY timestamp
        """,
        [symbol, strike_price, expiry[:10]],
    )
    return to_records(df)


@router.get("/api/delta_screener")
def delta_screener(
    timestamp:    Optional[str] = Query(None),
    target_delta: float         = Query(30.0),   # upper bound (absolute)
    min_delta:    float         = Query(5.0),    # lower bound (absolute)
    filter_type:  str           = Query("all"),  # all | index | stock
):
    tgt_hi = abs(target_delta) / 100.0
    tgt_lo = abs(min_delta)    / 100.0

    idx_list = ", ".join(f"\'{s}\'" for s in config.NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )

    if timestamp:
        ts_clause, ts_params = ts_filter_clause(timestamp)
        ts_where  = ts_clause
        pre_cte   = ""
    else:
        # Use a CTE to get latest timestamp per symbol — more reliable than
        # correlated subquery which some DuckDB versions handle inconsistently
        _t = tbl()
        pre_cte  = f"""latest_ts AS (
            SELECT symbol, MAX(timestamp) AS max_ts
            FROM {_t} GROUP BY symbol
        )"""
        ts_where  = "AND timestamp = (SELECT max_ts FROM latest_ts WHERE latest_ts.symbol = t.symbol)"
        ts_params = []

    df = qdf(
        f"""
        {"WITH " + pre_cte if pre_cte else ""}
        SELECT
            symbol, strike_price,
            CAST(expiry AS VARCHAR)       AS expiry,
            COALESCE(underlying_price, 0) AS spot,
            COALESCE(days_to_expiry,   0) AS dte,
            lotsize                       AS raw_lot,
            COALESCE(ce_ltp,    0) AS ce_ltp,    COALESCE(pe_ltp,    0) AS pe_ltp,
            COALESCE(ce_delta,  0) AS ce_delta,  COALESCE(pe_delta,  0) AS pe_delta,
            COALESCE(ce_nd2,    0) AS ce_nd2,    COALESCE(pe_nd2,    0) AS pe_nd2,
            COALESCE(ce_iv,     0) AS ce_iv,     COALESCE(pe_iv,     0) AS pe_iv,
            COALESCE(ce_oi,     0) AS ce_oi,     COALESCE(pe_oi,     0) AS pe_oi,
            COALESCE(ce_volume, 0) AS ce_volume, COALESCE(pe_volume, 0) AS pe_volume,
            COALESCE(ce_gamma,  0) AS ce_gamma,  COALESCE(pe_gamma,  0) AS pe_gamma,
            COALESCE(ce_theta,  0) AS ce_theta,  COALESCE(pe_theta,  0) AS pe_theta,
            COALESCE(ce_gexv,   0) AS ce_gexv,   COALESCE(pe_gexv,   0) AS pe_gexv,
            COALESCE(net_gexv,  0) AS net_gexv
        FROM {tbl()} t
        WHERE 1=1 {sym_filter} {ts_where}
          AND expiry >= CURRENT_DATE
          AND (COALESCE(ce_ltp, 0) + COALESCE(pe_ltp, 0)) > 0
          AND (
            ABS(COALESCE(ce_delta, 0)) BETWEEN {tgt_lo} AND {tgt_hi}
            OR
            ABS(COALESCE(pe_delta, 0)) BETWEEN {tgt_lo} AND {tgt_hi}
          )
        ORDER BY symbol, expiry, strike_price
        """,
        ts_params,
    )
    if df.empty:
        return safe_response({"target_delta": target_delta, "min_delta": min_delta, "rows": []})

    rows = []
    for _, r in df.iterrows():
        lot  = max(int(r["raw_lot"]) if r["raw_lot"] else 1, 1)
        spot = float(r["spot"])

        def make_row(otype, delta_raw, ltp, iv, oi, vol, gamma, theta, gexv):
            d = abs(float(delta_raw))
            return {
                "option_type": otype, "symbol": r["symbol"],
                "expiry": r["expiry"], "strike_price": float(r["strike_price"]),
                "delta": round(d, 4), "ltp": float(ltp),
                "iv": float(iv), "oi": float(oi), "volume": float(vol),
                "gamma": round(float(gamma), 6),
                "theta": round(float(theta), 4),
                "gexv":  round(float(gexv),  2),
                "net_gexv": round(float(r["net_gexv"]), 2),
                "lot_size": lot,
                "premium_per_lot": round(float(ltp) * lot, 2),
                "risk_indicator":  round(float(r["strike_price"]) * lot, 2),
                "spot": spot, "dte": float(r["dte"]),
                "margin": None, "return_on_margin": None,
            }

        ce_d = abs(float(r["ce_delta"]))
        if tgt_lo <= ce_d <= tgt_hi:
            rows.append(make_row("CE", r["ce_delta"], r["ce_ltp"], r["ce_iv"],
                                 r["ce_oi"], r["ce_volume"], r["ce_gamma"],
                                 r["ce_theta"], r["ce_gexv"]))

        pe_d = abs(float(r["pe_delta"]))
        if tgt_lo <= pe_d <= tgt_hi:
            rows.append(make_row("PE", r["pe_delta"], r["pe_ltp"], r["pe_iv"],
                                 r["pe_oi"], r["pe_volume"], r["pe_gamma"],
                                 r["pe_theta"], r["pe_gexv"]))

    return safe_response({"target_delta": target_delta, "min_delta": min_delta, "rows": rows})




@router.get("/api/delta_oi")
def delta_oi(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Delta-weighted OI = SUM(delta × OI × lot) across all strikes.
    Represents the net delta position carried by market makers (they are on the
    opposite side of retail/hedger flow). A strongly positive value means MMs
    are net short delta — they buy underlying on rallies (amplifying moves).
    Returns per-strike breakdown + aggregate per expiry + grand total.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)
    ts_clause, ts_params = ts_filter_clause(ts_filter)

    df = qdf(
        f"""
        SELECT
            CAST(expiry AS VARCHAR)       AS expiry,
            strike_price,
            COALESCE(ce_delta, 0)         AS ce_delta,
            COALESCE(pe_delta, 0)         AS pe_delta,
            COALESCE(ce_oi,    0)         AS ce_oi,
            COALESCE(pe_oi,    0)         AS pe_oi,
            COALESCE(lotsize,  1)         AS lotsize,
            COALESCE(underlying_price, 0) AS spot,
            COALESCE(ce_vanna, 0)         AS ce_vanna,
            COALESCE(pe_vanna, 0)         AS pe_vanna,
            COALESCE(net_vanna_ex, 0)     AS net_vanna_ex,
            COALESCE(net_charm_ex, 0)     AS net_charm_ex,
            COALESCE(net_flow,  0)        AS net_flow
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          {ts_clause}
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + ts_params,
    )
    if df.empty:
        raise HTTPException(404, "No delta OI data")

    lot  = max(int(df["lotsize"].iloc[0]), 1)
    spot = float(df["spot"].iloc[0])

    # Per-strike delta exposure (MM is short what retail is long)
    df["ce_delta_oi"]  = df["ce_delta"]        * df["ce_oi"] * lot
    df["pe_delta_oi"]  = df["pe_delta"].abs()  * df["pe_oi"] * lot
    df["net_delta_oi"] = df["ce_delta_oi"] - df["pe_delta_oi"]  # net MM delta

    # Scale to millions
    for c in ["ce_delta_oi", "pe_delta_oi", "net_delta_oi"]:
        df[c] = df[c] / 1e6

    net_total   = float(df["net_delta_oi"].sum())
    ce_total    = float(df["ce_delta_oi"].sum())
    pe_total    = float(df["pe_delta_oi"].sum())
    net_flow    = float(df["net_flow"].sum())
    net_vanna   = float(df["net_vanna_ex"].sum())
    net_charm   = float(df["net_charm_ex"].sum())

    return safe_response({
        "symbol":        symbol,
        "expiry":        expiry[:10],
        "spot":          spot,
        "net_delta_oi":  round(net_total, 4),
        "ce_delta_oi":   round(ce_total,  4),
        "pe_delta_oi":   round(pe_total,  4),
        "net_flow":      round(net_flow,  2),
        "net_vanna_ex":  round(net_vanna, 4),
        "net_charm_ex":  round(net_charm, 4),
        "interpretation": (
            "MMs net SHORT delta — buying pressure on rallies (trend amplifier)"
            if net_total < -0.5 else
            "MMs net LONG delta — selling pressure on rallies (mean-reverting)"
            if net_total > 0.5 else
            "MMs near delta-neutral — balanced book"
        ),
        "rows": to_records(df[[
            "strike_price", "ce_delta", "pe_delta",
            "ce_oi", "pe_oi", "ce_delta_oi", "pe_delta_oi", "net_delta_oi",
        ]]),
    })


@router.get("/api/strike_snapshot")
def strike_snapshot(
    symbol:       str           = Query(...),
    strike_price: float         = Query(...),
    expiry:       str           = Query(...),
    timestamp:    Optional[str] = Query(None),
):
    """
    Full single-row snapshot for one strike at a given timestamp.
    Returns all greeks, theoretical price, IV, moneyness, etc.
    Used by the enhanced Strike Trend / Option Lens panel.
    """
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)
    df = qdf(
        f"""
        SELECT
            strike_price, CAST(expiry AS VARCHAR) AS expiry,
            COALESCE(underlying_price,0)  AS spot,
            COALESCE(fut_price,0)         AS fut_price,
            COALESCE(days_to_expiry,0)    AS dte,
            COALESCE(distance_from_atm,0) AS distance_from_atm,
            -- CE
            COALESCE(ce_ltp,0)            AS ce_ltp,
            COALESCE(ce_TPrice,0)         AS ce_tprice,
            COALESCE(ce_ltp_s,'NA')       AS ce_ltp_s,
            COALESCE(ce_iv,0)             AS ce_iv,
            COALESCE(ce_delta,0)          AS ce_delta,
            COALESCE(ce_gamma,0)          AS ce_gamma,
            COALESCE(ce_theta,0)          AS ce_theta,
            COALESCE(ce_vega,0)           AS ce_vega,
            COALESCE(ce_vanna,0)          AS ce_vanna,
            COALESCE(ce_charm,0)          AS ce_charm,
            COALESCE(ce_oi,0)             AS ce_oi,
            COALESCE(ce_volume,0)         AS ce_volume,
            COALESCE(ce_intrinsic_value,0) AS ce_intrinsic,
            COALESCE(ce_time_value,0)     AS ce_time_value,
            COALESCE(ce_bid,0)            AS ce_bid,
            COALESCE(ce_ask,0)            AS ce_ask,
            COALESCE(ce_bid_ask_spread,0) AS ce_spread,
            COALESCE(ce_moneyness,'NA')   AS ce_moneyness,
            COALESCE(ce_gexv,0)           AS ce_gexv,
            -- PE
            COALESCE(pe_ltp,0)            AS pe_ltp,
            COALESCE(pe_TPrice,0)         AS pe_tprice,
            COALESCE(pe_ltp_s,'NA')       AS pe_ltp_s,
            COALESCE(pe_iv,0)             AS pe_iv,
            COALESCE(pe_delta,0)          AS pe_delta,
            COALESCE(pe_gamma,0)          AS pe_gamma,
            COALESCE(pe_theta,0)          AS pe_theta,
            COALESCE(pe_vega,0)           AS pe_vega,
            COALESCE(pe_vanna,0)          AS pe_vanna,
            COALESCE(pe_charm,0)          AS pe_charm,
            COALESCE(pe_oi,0)             AS pe_oi,
            COALESCE(pe_volume,0)         AS pe_volume,
            COALESCE(pe_intrinsic_value,0) AS pe_intrinsic,
            COALESCE(pe_time_value,0)     AS pe_time_value,
            COALESCE(pe_bid,0)            AS pe_bid,
            COALESCE(pe_ask,0)            AS pe_ask,
            COALESCE(pe_bid_ask_spread,0) AS pe_spread,
            COALESCE(pe_moneyness,'NA')   AS pe_moneyness,
            COALESCE(pe_gexv,0)           AS pe_gexv,
            -- Composite
            COALESCE(riskreversal,0)      AS riskreversal,
            CAST(sentiment AS VARCHAR)    AS sentiment,
            CAST(regime    AS VARCHAR)    AS regime,
            COALESCE(net_flow,0)          AS net_flow,
            COALESCE(m_volatility,0)      AS rv
        FROM {tbl()}
        WHERE symbol=? AND strike_price=? AND expiry=?
          {ts_clause}
        LIMIT 1
        """,
        [symbol, strike_price, expiry[:10]] + ts_params,
    )
    if df.empty:
        raise HTTPException(404, "No snapshot data")
    row = df.iloc[0].to_dict()
    # Add derived fields
    ce_ltp  = float(row.get("ce_ltp", 0))
    ce_tp   = float(row.get("ce_tprice", 0))
    pe_ltp  = float(row.get("pe_ltp", 0))
    pe_tp   = float(row.get("pe_tprice", 0))
    row["ce_price_ratio"] = round(ce_ltp / ce_tp, 4) if ce_tp > 0 else None
    row["pe_price_ratio"] = round(pe_ltp / pe_tp, 4) if pe_tp > 0 else None
    return safe_response(row)


# Extend strike_trend to support multiple metrics (up to 3)
@router.get("/api/strike_trend_multi")
def strike_trend_multi(
    symbol:       str   = Query(...),
    strike_price: float = Query(...),
    expiry:       str   = Query(...),
    m1:           str   = Query("ce_ltp"),
    m2:           Optional[str] = Query(None),
    m3:           Optional[str] = Query(None),
):
    """Return time-series for up to 3 metrics for one strike+expiry."""
    metrics = [m for m in [m1, m2, m3] if m and m in config.SAFE_METRICS]
    if not metrics:
        raise HTTPException(400, "No valid metrics specified")
    sel = ", ".join(f"COALESCE({m},0) AS {m}" for m in metrics)
    df = qdf(
        f"""
        SELECT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS timestamp, {sel}
        FROM {tbl()}
        WHERE symbol=? AND strike_price=? AND expiry=?
        ORDER BY timestamp
        """,
        [symbol, strike_price, expiry[:10]],
    )
    return safe_response({"metrics": metrics, "rows": to_records(df)})


@router.get("/api/premium_lens")
def premium_lens(
    symbol:        str           = Query(...),
    expiry:        str           = Query(...),
    timestamp:     Optional[str] = Query(None),
    min_ratio:     float         = Query(0.0),   # show only where ratio < min_ratio
    max_ratio:     float         = Query(999.0), # or > max_ratio (outside normal band)
    min_oi:        int           = Query(0),
):
    """
    Premium richness/cheapness screener.
    Uses ce_TPrice/pe_TPrice (Black-Scholes theoretical) vs actual LTP.
    ce_ltp_s/pe_ltp_s (Premium/Discount/NA) from DB is also shown.
    Filters to show only strikes outside the configured ratio range.
    """
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)
    df = qdf(
        f"""
        SELECT
            strike_price,
            distance_from_atm,
            COALESCE(days_to_expiry,  0) AS dte,
            COALESCE(ce_ltp,     0) AS ce_ltp,
            COALESCE(ce_TPrice,  0) AS ce_tprice,
            COALESCE(ce_ltp_s, 'NA') AS ce_ltp_s,
            COALESCE(ce_iv,      0) AS ce_iv,
            COALESCE(ce_delta,   0) AS ce_delta,
            COALESCE(ce_oi,      0) AS ce_oi,
            COALESCE(ce_volume,  0) AS ce_volume,
            COALESCE(ce_bid_ask_spread, 0) AS ce_spread,
            COALESCE(pe_ltp,     0) AS pe_ltp,
            COALESCE(pe_TPrice,  0) AS pe_tprice,
            COALESCE(pe_ltp_s, 'NA') AS pe_ltp_s,
            COALESCE(pe_iv,      0) AS pe_iv,
            COALESCE(pe_delta,   0) AS pe_delta,
            COALESCE(pe_oi,      0) AS pe_oi,
            COALESCE(pe_volume,  0) AS pe_volume,
            COALESCE(pe_bid_ask_spread, 0) AS pe_spread,
            COALESCE(underlying_price,  0) AS spot
        FROM {tbl()}
        WHERE symbol=? AND expiry=?
          {ts_clause}
          AND (COALESCE(ce_ltp,0) > 0 OR COALESCE(pe_ltp,0) > 0)
          AND (COALESCE(ce_oi,0) >= ? OR COALESCE(pe_oi,0) >= ?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + ts_params + [min_oi, min_oi],
    )
    if df.empty:
        raise HTTPException(404, "No data")

    # Compute price ratios
    df["ce_ratio"] = df.apply(
        lambda r: round(r["ce_ltp"] / r["ce_tprice"], 4) if r["ce_tprice"] > 0 else None, axis=1)
    df["pe_ratio"] = df.apply(
        lambda r: round(r["pe_ltp"] / r["pe_tprice"], 4) if r["pe_tprice"] > 0 else None, axis=1)
    df["ce_diff_pct"] = df.apply(
        lambda r: round((r["ce_ltp"] - r["ce_tprice"]) / r["ce_tprice"] * 100, 2)
        if r["ce_tprice"] > 0 else None, axis=1)
    df["pe_diff_pct"] = df.apply(
        lambda r: round((r["pe_ltp"] - r["pe_tprice"]) / r["pe_tprice"] * 100, 2)
        if r["pe_tprice"] > 0 else None, axis=1)

    # Apply ratio filter: show rows outside the "normal" band
    if min_ratio > 0 or max_ratio < 999:
        mask = (
            ((df["ce_ratio"].notna()) & ((df["ce_ratio"] < min_ratio) | (df["ce_ratio"] > max_ratio))) |
            ((df["pe_ratio"].notna()) & ((df["pe_ratio"] < min_ratio) | (df["pe_ratio"] > max_ratio)))
        )
        df = df[mask]

    return safe_response({"rows": to_records(df), "symbol": symbol, "expiry": expiry[:10]})


