"""Screener endpoints — delta, premium lens, strike trend, snapshot."""
from __future__ import annotations
import math
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, ts_filter_clause, _ts_lo_hi, expiry_clause, latest_ts
from .. import config
from .. import margin as _margin

# exposure_core is the single source for the empirical hold-probability table.
# Import path depends on where the dashboard is launched from: `oc.exposure_core`
# when cwd is od\ (how oc_exposure_eod.py runs), plain `exposure_core` when it
# is od\oc\. Try both rather than assuming, and degrade to None so a launch-path
# difference cannot take the whole screener module down at import time.
try:
    import oc.exposure_core as core
except ImportError:  # pragma: no cover
    try:
        import exposure_core as core
    except ImportError:
        core = None

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
    expiry:       Optional[str] = Query(None),   # exact expiry, or None for all live
    min_oi:       float         = Query(0),      # per-LEG open interest floor
    min_volume:   float         = Query(0),      # per-LEG traded volume floor
):
    tgt_hi = abs(target_delta) / 100.0
    tgt_lo = abs(min_delta)    / 100.0

    idx_list = ", ".join(f"\'{s}\'" for s in config.NSE_INDICES)

    # Optional exact-expiry filter. The row-level `expiry >= floor` below keeps
    # only live contracts; this narrows to one of them.
    exp_clause, exp_params = "", []
    if expiry:
        exp_clause = "AND expiry = CAST(? AS DATE)"
        exp_params = [expiry[:10]]
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )

    # Timestamp handling — ONE branch, deliberately.
    #
    # The old code branched two ways: given a `timestamp` it applied a GLOBAL
    # minute filter (STRFTIME(...) = ?) to every symbol; without one it used a
    # per-symbol MAX(timestamp) CTE. Those branches return different symbol
    # universes. loadDsTimestamps() populates the picker from NIFTY, so the
    # moment it resolved, the screener pinned EVERY symbol to a NIFTY minute —
    # and stocks, scraped on a different cadence, mostly had no row at that
    # exact minute and silently dropped out. Whether you got stocks depended on
    # whether an async call had returned yet, which is the "empty on first open,
    # fine after touching the slider" bug.
    #
    # Now `timestamp` only moves the as-of ceiling; the CTE always resolves each
    # symbol's own latest snapshot at or before it.
    _t = tbl()
    as_of_params: list = []
    as_of_where  = ""
    expiry_floor = "CURRENT_DATE"
    if timestamp:
        # Lexicographic compare is chronological for zero-padded ISO minutes.
        ts_norm = timestamp[:16] if len(timestamp) >= 16 else f"{timestamp[:10]} 23:59"
        as_of_where  = "WHERE STRFTIME(timestamp, '%Y-%m-%d %H:%M') <= ?"
        expiry_floor = "CAST(? AS DATE)"
        as_of_params = [ts_norm]

    df = qdf(
        f"""
        WITH latest_ts AS (
            SELECT symbol, MAX(timestamp) AS max_ts
            FROM {_t}
            {as_of_where}
            GROUP BY symbol
        )
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
        FROM {_t} t
        WHERE 1=1 {sym_filter}
          AND t.timestamp = (SELECT max_ts FROM latest_ts WHERE latest_ts.symbol = t.symbol)
          AND expiry >= {expiry_floor}
          {exp_clause}
          AND (COALESCE(ce_ltp, 0) + COALESCE(pe_ltp, 0)) > 0
          AND (
            ABS(COALESCE(ce_delta, 0)) BETWEEN {tgt_lo} AND {tgt_hi}
            OR
            ABS(COALESCE(pe_delta, 0)) BETWEEN {tgt_lo} AND {tgt_hi}
          )
        ORDER BY symbol, expiry, strike_price
        """,
        as_of_params + ([ts_norm[:10]] if timestamp else []) + exp_params,
    )
    if df.empty:
        return safe_response({"target_delta": target_delta, "min_delta": min_delta, "rows": []})

    rows = []
    for _, r in df.iterrows():
        lot  = max(int(r["raw_lot"]) if r["raw_lot"] else 1, 1)
        spot = float(r["spot"])

        def make_row(otype, delta_raw, ltp, iv, oi, vol, gamma, theta, gexv):
            d   = abs(float(delta_raw))
            k   = float(r["strike_price"])
            ltp = float(ltp)
            # Approximate SPAN-shaped margin for shorting THIS leg. Replaces the
            # per-contract ICICI fetch for screening: brokers disagree on RMS
            # add-ons, so relative ordering is what matters, and this is computed
            # identically for everything in a class. See margin.py.
            mgn, msrc = _margin.best_short_margin(
                str(r["symbol"]), otype, spot, k, ltp, lot, float(iv),
                expiry=r["expiry"], as_of=timestamp[:10] if timestamp else None,
            )
            return {
                "option_type": otype, "symbol": r["symbol"],
                "expiry": r["expiry"], "strike_price": k,
                "delta": round(d, 4), "ltp": ltp,
                "iv": float(iv), "oi": float(oi), "volume": float(vol),
                "gamma": round(float(gamma), 6),
                "theta": round(float(theta), 4),
                "gexv":  round(float(gexv),  2),
                "net_gexv": round(float(r["net_gexv"]), 2),
                "lot_size": lot,
                "premium_per_lot": round(ltp * lot, 2),
                # Unlevered yield on the strike: what the premium is worth as a
                # percentage of the capital the strike represents. Distinct from
                # return_on_margin (levered) — for a covered call against held
                # stock, or a cash-secured put, THIS is the relevant number
                # because the strike, not the margin, is the capital at risk.
                "prem_strike_pct": round(ltp / k * 100, 4) if k > 0 else None,
                "risk_indicator":  round(k * lot, 2),
                "spot": spot, "dte": float(r["dte"]),
                "margin": mgn,
                "return_on_margin": _margin.return_on_margin(ltp * lot, mgn),
                # "span" = exchange SPAN+ELM from the stored NSE file, else
                # "approx". ICICI values, when fetched, overwrite client-side.
                "margin_source": msrc,
            }

        # Per-LEG liquidity gates. Previously a row-level OR in SQL
        # ((ce_ltp + pe_ltp) > 0), which let a dead call ride along with a liquid
        # put — the grid renders ONE ROW PER LEG, so the gate has to be per leg
        # too. An untraded leg has no price you could act on.
        def _liquid(ltp, oi, vol):
            return float(ltp) > 0 and float(oi) >= min_oi and float(vol) >= min_volume

        ce_d = abs(float(r["ce_delta"]))
        if tgt_lo <= ce_d <= tgt_hi and _liquid(r["ce_ltp"], r["ce_oi"], r["ce_volume"]):
            rows.append(make_row("CE", r["ce_delta"], r["ce_ltp"], r["ce_iv"],
                                 r["ce_oi"], r["ce_volume"], r["ce_gamma"],
                                 r["ce_theta"], r["ce_gexv"]))

        pe_d = abs(float(r["pe_delta"]))
        if tgt_lo <= pe_d <= tgt_hi and _liquid(r["pe_ltp"], r["pe_oi"], r["pe_volume"]):
            rows.append(make_row("PE", r["pe_delta"], r["pe_ltp"], r["pe_iv"],
                                 r["pe_oi"], r["pe_volume"], r["pe_gamma"],
                                 r["pe_theta"], r["pe_gexv"]))

    return safe_response({
        "target_delta": target_delta, "min_delta": min_delta,
        "expiry": expiry, "min_oi": min_oi, "min_volume": min_volume,
        "rows": rows,
    })




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


@router.get("/api/straddle_screener")
def straddle_screener(
    timestamp:   Optional[str] = Query(None),
    filter_type: str           = Query("all"),   # all | index | stock
    expiry_rank: int           = Query(0),       # 0 = nearest live expiry, 1 = next
    min_oi:      float         = Query(0),       # applied to BOTH legs
):
    """ATM short-straddle screen. One row per symbol.

    Grain is deliberately one row per SYMBOL, not per contract - a straddle is a
    position, not a leg, which is why this is its own screen rather than more
    columns on the delta screener.

    ATM is the row with distance_from_atm = 0 rather than nearest-to-fut_price:
    reuses what the pipeline already computed and stays consistent with iv_rank.

    RICHNESS - the one column worth looking at:
        be_vs_hv = straddle_premium / (fut * hv20/100 * sqrt(dte/243))

    i.e. how wide the breakeven sits against what REALISED vol implies over the
    same horizon. Above 1.0 the market pays you more than recent realised
    movement justifies.

    Note what this is NOT. Comparing straddle premium to `expected_move` would
    be circular: both come from the same ATM IV - an ATM straddle is about
    0.798*F*sigma*sqrt(T) and EM is F*sigma*sqrt(T), so the ratio is a constant
    and measures nothing. Substituting hv20 for the IV leg makes it a genuine
    comparison of two independent measurements (market price vs realised
    movement). It reduces to ~0.798 * IV/HV - the variance risk premium in the
    form you actually trade.
    """
    _t = tbl()
    as_of_params: list = []
    as_of_where  = ""
    expiry_floor = "CURRENT_DATE"
    if timestamp:
        ts_norm = timestamp[:16] if len(timestamp) >= 16 else f"{timestamp[:10]} 23:59"
        as_of_where  = "WHERE STRFTIME(timestamp, '%Y-%m-%d %H:%M') <= ?"
        expiry_floor = "CAST(? AS DATE)"
        as_of_params = [ts_norm]

    idx_list = ", ".join(f"\'{s}\'" for s in config.NSE_INDICES)
    if filter_type == "index":
        sym_filter = f"AND t.symbol IN ({idx_list})"
    elif filter_type == "stock":
        sym_filter = f"AND t.symbol NOT IN ({idx_list})"
    else:
        sym_filter = ""

    rank = max(0, int(expiry_rank))

    df = qdf(
        f"""
        WITH latest_ts AS (
            SELECT symbol, MAX(timestamp) AS max_ts
            FROM {_t}
            {as_of_where}
            GROUP BY symbol
        ),
        atm AS (
            SELECT t.symbol,
                   CAST(t.expiry AS VARCHAR)       AS expiry,
                   t.strike_price,
                   COALESCE(t.underlying_price, 0) AS spot,
                   COALESCE(t.fut_price, t.underlying_price, 0) AS fut,
                   COALESCE(t.days_to_expiry, 0)   AS dte,
                   t.lotsize                       AS raw_lot,
                   COALESCE(t.ce_ltp, 0) AS ce_ltp, COALESCE(t.pe_ltp, 0) AS pe_ltp,
                   COALESCE(t.ce_iv, 0)  AS ce_iv,  COALESCE(t.pe_iv, 0)  AS pe_iv,
                   COALESCE(t.ce_oi, 0)  AS ce_oi,  COALESCE(t.pe_oi, 0)  AS pe_oi,
                   ROW_NUMBER() OVER (PARTITION BY t.symbol ORDER BY t.expiry) AS rk
            FROM {_t} t
            JOIN latest_ts l ON l.symbol = t.symbol AND t.timestamp = l.max_ts
            WHERE t.distance_from_atm = 0
              AND t.expiry >= {expiry_floor}
              AND COALESCE(t.ce_ltp,0) > 0 AND COALESCE(t.pe_ltp,0) > 0
              {sym_filter}
        ),
        hv AS (
            SELECT symbol, hv20, em_1d, iv_change, iv_pctile, iv_rank,
                   iv_rank_method, iv_rank_n
            FROM exposure_eod
            WHERE expiry_rank = 0
              AND date = (SELECT MAX(date) FROM exposure_eod)
        )
        SELECT a.*, h.hv20, h.em_1d, h.iv_change, h.iv_pctile, h.iv_rank,
               h.iv_rank_method, h.iv_rank_n
        FROM atm a
        LEFT JOIN hv h ON h.symbol = a.symbol
        WHERE a.rk = {rank + 1}
          AND a.ce_oi >= {float(min_oi)} AND a.pe_oi >= {float(min_oi)}
        ORDER BY a.symbol
        """,
        as_of_params + ([ts_norm[:10]] if timestamp else []),
    )
    if df.empty:
        return safe_response({"expiry_rank": rank, "rows": []})

    def _num(v):
        try:
            f = float(v)
            return f if f == f else None      # NaN -> None
        except (TypeError, ValueError):
            return None

    rows = []
    for _, r in df.iterrows():
        sym = str(r["symbol"])
        lot = max(int(r["raw_lot"]) if r["raw_lot"] else 1, 1)
        fut = float(r["fut"]); k = float(r["strike_price"]); dte = float(r["dte"])
        ce, pe = float(r["ce_ltp"]), float(r["pe_ltp"])
        prem = ce + pe
        iv = (float(r["ce_iv"]) + float(r["pe_iv"])) / 2.0

        # Netted margin. SPAN offsets the two legs heavily, so costing this at
        # 2x a single leg would understate RoM by ~40% and push every straddle
        # down an RoM-sorted list as an artifact of the margin model.
        mgn, msrc = _margin.best_combined_margin(
            sym, r["expiry"], lot,
            [("CE", fut, k, ce, float(r["ce_iv"])), ("PE", fut, k, pe, float(r["pe_iv"]))],
            as_of=timestamp[:10] if timestamp else None)

        hv20 = _num(r["hv20"])
        be_vs_hv = None
        if hv20 and hv20 > 0 and dte > 0 and fut > 0:
            hv_move = fut * (hv20 / 100.0) * math.sqrt(dte / 243.0)
            if hv_move > 0:
                be_vs_hv = round(prem / hv_move, 3)

        rows.append({
            "symbol": sym, "expiry": r["expiry"], "dte": dte,
            "spot": round(float(r["spot"]), 2), "fut_price": round(fut, 2),
            "atm_strike": k, "lot_size": lot,
            "ce_ltp": ce, "pe_ltp": pe,
            "straddle_prem": round(prem, 2),
            "straddle_prem_lot": round(prem * lot, 2),
            "be_lo": round(k - prem, 2), "be_hi": round(k + prem, 2),
            "be_width_pct": round(prem / fut * 100, 3) if fut > 0 else None,
            "atm_iv": round(iv, 2),
            "hv20": round(hv20, 2) if hv20 else None,
            "vrp": round(iv - hv20, 2) if hv20 else None,
            "be_vs_hv": be_vs_hv,
            "em_1d": _num(r["em_1d"]),
            # IV context from the nightly's stored constant-maturity series.
            # iv_pctile is shown on the grid in preference to iv_rank: with a
            # 90-day window the range can be set by a single spike, and the
            # percentile is the more robust of the two. Rank travels along for
            # the hover, with the method so a `band` reading (DTE spread 13.6pt
            # vs cm30's 10.1) can be read with appropriate suspicion.
            "iv_change": _num(r["iv_change"]),
            "iv_pctile": _num(r["iv_pctile"]),
            "iv_rank": _num(r["iv_rank"]),
            "iv_rank_method": (str(r["iv_rank_method"])
                               if r["iv_rank_method"] is not None
                               and r["iv_rank_method"] == r["iv_rank_method"] else None),
            "iv_rank_n": int(_num(r["iv_rank_n"]) or 0),
            "margin": mgn,
            "return_on_margin": _margin.return_on_margin(prem * lot, mgn),
            "margin_source": msrc,
            "ce_oi": float(r["ce_oi"]), "pe_oi": float(r["pe_oi"]),
        })

    return safe_response({"expiry_rank": rank, "min_oi": min_oi, "rows": rows})


@router.get("/api/strangle_screener")
def strangle_screener(
    timestamp:    Optional[str] = Query(None),
    filter_type:  str           = Query("all"),   # all | index | stock
    expiry_rank:  int           = Query(0),
    target_delta: float         = Query(20.0),    # absolute delta, per side
    min_oi:       float         = Query(0),       # per LEG
):
    """Delta-selected short strangle screen. One row per symbol.

    STRIKE SELECTION is at-or-beyond: the chosen strike has |delta| <= target,
    taking the closest such strike. A seller never ends up with a band TIGHTER
    than asked for. Flip the comparison to `>=` for nearest-delta instead.

    WHY z RATHER THAN A WIDTH RATIO: for a straddle, band width vs vol is the
    whole question. For a strangle you are OTM on both sides and the question is
    how far the breakevens sit in vol units. z is comparable across symbols and
    captures the asymmetry skew creates - a nominal 20-delta strangle routinely
    sits ~1.9 sigma on the put side and ~2.4 on the call side.

    P(HOLD) IS EMPIRICAL, never NORM.DIST. Measured on 725k observations, the
    Gaussian overstates badly exactly where sellers live: at z=2.0 it claims
    95.4% while reality is 90.6%, and at z=3.0 it claims 99.7% against 96.6%
    (11x the breaches). It is also conditioned on trailing vol, because at the
    same nominal z the quietest HV band held 83.2% against 96.3% for the most
    volatile - mean reversion makes a quiet name's z-bands too narrow, and those
    are precisely the names that screen best on width. See exposure_core.
    """
    _t = tbl()
    as_of_params: list = []
    as_of_where  = ""
    expiry_floor = "CURRENT_DATE"
    if timestamp:
        ts_norm = timestamp[:16] if len(timestamp) >= 16 else f"{timestamp[:10]} 23:59"
        as_of_where  = "WHERE STRFTIME(timestamp, '%Y-%m-%d %H:%M') <= ?"
        expiry_floor = "CAST(? AS DATE)"
        as_of_params = [ts_norm]

    idx_list = ", ".join(f"\'{s}\'" for s in config.NSE_INDICES)
    if filter_type == "index":
        sym_filter = f"AND t.symbol IN ({idx_list})"
    elif filter_type == "stock":
        sym_filter = f"AND t.symbol NOT IN ({idx_list})"
    else:
        sym_filter = ""

    rank = max(0, int(expiry_rank))
    tgt  = abs(float(target_delta)) / 100.0
    moi  = float(min_oi)

    df = qdf(
        f"""
        WITH latest_ts AS (
            SELECT symbol, MAX(timestamp) AS max_ts
            FROM {_t}
            {as_of_where}
            GROUP BY symbol
        ),
        snap AS (
            SELECT t.symbol,
                   CAST(t.expiry AS VARCHAR)       AS expiry,
                   t.strike_price,
                   COALESCE(t.underlying_price, 0) AS spot,
                   COALESCE(t.fut_price, t.underlying_price, 0) AS fut,
                   COALESCE(t.days_to_expiry, 0)   AS dte,
                   t.lotsize                       AS raw_lot,
                   COALESCE(t.ce_ltp,0) AS ce_ltp, COALESCE(t.pe_ltp,0) AS pe_ltp,
                   COALESCE(t.ce_iv,0)  AS ce_iv,  COALESCE(t.pe_iv,0)  AS pe_iv,
                   COALESCE(t.ce_oi,0)  AS ce_oi,  COALESCE(t.pe_oi,0)  AS pe_oi,
                   COALESCE(t.ce_delta,0) AS ce_delta, COALESCE(t.pe_delta,0) AS pe_delta,
                   DENSE_RANK() OVER (PARTITION BY t.symbol ORDER BY t.expiry) AS er
            FROM {_t} t
            JOIN latest_ts l ON l.symbol = t.symbol AND t.timestamp = l.max_ts
            WHERE t.expiry >= {expiry_floor}
              {sym_filter}
        ),
        sel AS (SELECT * FROM snap WHERE er = {rank + 1}),
        base AS (
            SELECT symbol, ANY_VALUE(expiry) AS expiry, MAX(spot) AS spot,
                   MAX(fut) AS fut, MAX(dte) AS dte, MAX(raw_lot) AS raw_lot
            FROM sel GROUP BY symbol
        ),
        ce AS (
            SELECT symbol, strike_price AS ce_k, ce_ltp, ce_iv, ce_oi, ce_delta,
                   ROW_NUMBER() OVER (PARTITION BY symbol
                                      ORDER BY ABS(ce_delta) DESC) AS rn
            FROM sel
            WHERE ce_ltp > 0 AND ce_oi >= {moi}
              AND ABS(ce_delta) > 0 AND ABS(ce_delta) <= {tgt}
        ),
        pe AS (
            SELECT symbol, strike_price AS pe_k, pe_ltp, pe_iv, pe_oi, pe_delta,
                   ROW_NUMBER() OVER (PARTITION BY symbol
                                      ORDER BY ABS(pe_delta) DESC) AS rn
            FROM sel
            WHERE pe_ltp > 0 AND pe_oi >= {moi}
              AND ABS(pe_delta) > 0 AND ABS(pe_delta) <= {tgt}
        ),
        hv AS (
            SELECT symbol, hv20, em_1d, iv_change, iv_pctile, iv_rank,
                   iv_rank_method, iv_rank_n
            FROM exposure_eod
            WHERE expiry_rank = 0
              AND date = (SELECT MAX(date) FROM exposure_eod)
        )
        SELECT b.*, c.ce_k, c.ce_ltp, c.ce_iv, c.ce_oi, c.ce_delta,
               p.pe_k, p.pe_ltp, p.pe_iv, p.pe_oi, p.pe_delta,
               h.hv20, h.em_1d, h.iv_change, h.iv_pctile, h.iv_rank,
               h.iv_rank_method, h.iv_rank_n
        FROM base b
        JOIN ce c ON c.symbol = b.symbol AND c.rn = 1
        JOIN pe p ON p.symbol = b.symbol AND p.rn = 1
        LEFT JOIN hv h ON h.symbol = b.symbol
        ORDER BY b.symbol
        """,
        as_of_params + ([ts_norm[:10]] if timestamp else []),
    )
    if df.empty:
        return safe_response({"expiry_rank": rank, "target_delta": target_delta, "rows": []})

    def _num(v):
        try:
            f = float(v)
            return f if f == f else None
        except (TypeError, ValueError):
            return None

    rows = []
    for _, r in df.iterrows():
        sym = str(r["symbol"])
        lot = max(int(r["raw_lot"]) if r["raw_lot"] else 1, 1)
        fut = float(r["fut"]); dte = float(r["dte"])
        ck, pk = float(r["ce_k"]), float(r["pe_k"])
        ce, pe = float(r["ce_ltp"]), float(r["pe_ltp"])
        prem = ce + pe

        mgn, msrc = _margin.best_combined_margin(
            sym, r["expiry"], lot,
            [("CE", fut, ck, ce, float(r["ce_iv"])), ("PE", fut, pk, pe, float(r["pe_iv"]))],
            as_of=timestamp[:10] if timestamp else None)

        hv20 = _num(r["hv20"])
        z_hi = z_lo = z_min = p_hold = None
        be_hi, be_lo = ck + prem, pk - prem
        if hv20 and hv20 > 0 and dte > 0 and fut > 0:
            sig = fut * (hv20 / 100.0) * math.sqrt(dte / 243.0)
            if sig > 0:
                z_hi = round((be_hi - fut) / sig, 2)
                z_lo = round((fut - be_lo) / sig, 2)
                z_min = min(z_hi, z_lo)
                # Two-sided: convert each side's z to its own tail. Using only
                # the narrow side would understate the total breach chance by
                # ignoring the far wing entirely.
                if core is not None:
                    th = core.hold_prob(z_hi, hv20)
                    tl = core.hold_prob(z_lo, hv20)
                    if th is not None and tl is not None:
                        p_hold = round(100.0 - (100.0 - th) / 2.0 - (100.0 - tl) / 2.0, 1)

        # Market's own view: P(finish between the breakevens) under Black-76,
        # using each side's OWN IV so the skew is respected. This is the number
        # delta intuition produces - a 20-delta strangle "should" be ~60-70%.
        p_otm = None
        ce_iv_f, pe_iv_f = float(r["ce_iv"]), float(r["pe_iv"])
        if dte > 0 and fut > 0 and ce_iv_f > 0 and pe_iv_f > 0 and be_lo > 0:
            t = dte / 243.0
            sc, sp = ce_iv_f / 100.0, pe_iv_f / 100.0
            d2_hi = (math.log(fut / be_hi) - 0.5 * sc * sc * t) / (sc * math.sqrt(t))
            d2_lo = (math.log(fut / be_lo) - 0.5 * sp * sp * t) / (sp * math.sqrt(t))
            ncdf = lambda x: 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
            p_otm = round(100.0 * (1.0 - ncdf(d2_hi) - ncdf(-d2_lo)), 1)

        # Gap. Positive = realised movement says this band is SAFER than the
        # option market is pricing, i.e. premium is rich.
        #
        # CAVEAT, and it is not small: the two numbers differ in TWO ways, not
        # one. p_hold uses realised vol AND the measured fat-tailed
        # distribution; p_otm uses implied vol AND a lognormal. So the gap mixes
        # the variance risk premium with the distributional difference, and the
        # second term is a roughly -5pp drag at 2 sigma. Read the gap as a
        # ranking signal, not as a probability edge you can size against.
        edge = round(p_hold - p_otm, 1) if (p_hold is not None and p_otm is not None) else None

        rows.append({
            "symbol": sym, "expiry": r["expiry"], "dte": dte,
            "spot": round(float(r["spot"]), 2), "fut_price": round(fut, 2),
            "ce_strike": ck, "pe_strike": pk, "lot_size": lot,
            "ce_ltp": ce, "pe_ltp": pe,
            "ce_delta": round(abs(float(r["ce_delta"])) * 100, 1),
            "pe_delta": round(abs(float(r["pe_delta"])) * 100, 1),
            "strangle_prem": round(prem, 2),
            "strangle_prem_lot": round(prem * lot, 2),
            "be_lo": round(be_lo, 2), "be_hi": round(be_hi, 2),
            "width_pct": round((ck - pk) / fut * 100, 2) if fut > 0 else None,
            "z_lo": z_lo, "z_hi": z_hi, "z_min": z_min,
            "p_hold": p_hold, "p_otm": p_otm, "edge": edge,
            "atm_iv": round((float(r["ce_iv"]) + float(r["pe_iv"])) / 2.0, 2),
            "hv20": round(hv20, 2) if hv20 else None,
            "hv_band": core.hv_band(hv20) if (hv20 and core is not None) else None,
            "iv_change": _num(r["iv_change"]),
            "iv_pctile": _num(r["iv_pctile"]),
            "iv_rank": _num(r["iv_rank"]),
            "iv_rank_method": (str(r["iv_rank_method"])
                               if r["iv_rank_method"] is not None
                               and r["iv_rank_method"] == r["iv_rank_method"] else None),
            "iv_rank_n": int(_num(r["iv_rank_n"]) or 0),
            "em_1d": _num(r["em_1d"]),
            "margin": mgn,
            "return_on_margin": _margin.return_on_margin(prem * lot, mgn),
            "margin_source": msrc,
            "ce_oi": float(r["ce_oi"]), "pe_oi": float(r["pe_oi"]),
        })

    return safe_response({
        "expiry_rank": rank, "target_delta": target_delta,
        "min_oi": min_oi, "rows": rows,
    })


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


