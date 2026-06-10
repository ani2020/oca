"""Exposure Flow — Gamma, Directional (Delta), and Volatility (Vanna) layers."""
from __future__ import annotations
import math
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..db import qdf, tbl, safe_response, _safe
from ..helpers import _eod_df, gamma_flip_from_strikes, gamma_analysis_range
from .. import config

router = APIRouter()


# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

def _gamma_layer(snap: pd.DataFrame) -> Dict[str, Any]:
    """Compute gamma layer metrics for one period's snapshot.
    Uses ce_gamma × ce_oi × lotsize formula — identical to GEX page —
    so flip points match between the two screens."""
    strikes  = snap["strike_price"].values
    spot     = float(snap["spot"].median())
    dte      = float(snap["dte"].median()) if "dte" in snap.columns else 25
    lot_size = float(snap["lotsize"].median()) if "lotsize" in snap.columns else 1

    # Recompute GEX from raw greeks — same formula as _build_gamma_profile
    call_gex = snap["ce_gamma"].values * snap["ce_oi"].values * lot_size
    put_gex  = snap["pe_gamma"].values * snap["pe_oi"].values * lot_size
    net_gex  = call_gex - put_gex   # consistent sign convention with GEX page

    # Adaptive range (shared helper — same inputs as GEX page)
    atm_iv = float(snap.loc[snap["ce_iv"] > 0, "ce_iv"].median()) if (snap["ce_iv"] > 0).any() else 0
    lo, hi = gamma_analysis_range(spot, atm_iv=atm_iv, dte=dte)
    mask = (strikes >= lo) & (strikes <= hi)
    flip_interp, flip_strike = gamma_flip_from_strikes(strikes[mask], net_gex[mask])

    # Peak positive gamma (vol suppression / pin candidate) — within ±10% of spot
    gex_near = np.where(mask, net_gex, 0)  # zero out far-OTM for peak search
    peak_pos_idx = np.argmax(gex_near)
    peak_pos = {
        "strike": float(strikes[peak_pos_idx]),
        "value":  round(float(net_gex[peak_pos_idx]) / 1e6, 4),  # in ₹M
    } if gex_near[peak_pos_idx] > 0 else None

    # Peak negative gamma (vol amplification zone)
    peak_neg_idx = np.argmin(gex_near)
    peak_neg = {
        "strike": float(strikes[peak_neg_idx]),
        "value":  round(float(net_gex[peak_neg_idx]) / 1e6, 4),
    } if gex_near[peak_neg_idx] < 0 else None

    # Total net GEX (market-wide gamma bias)
    total_gex = round(float(net_gex.sum()) / 1e6, 4)

    return {
        "flip_point":    flip_interp,
        "flip_strike":   flip_strike,
        "peak_pos":      peak_pos,
        "peak_neg":      peak_neg,
        "total_gex_m":   total_gex,
        "spot":          spot,
    }


def _directional_layer(snap: pd.DataFrame) -> Dict[str, Any]:
    """Compute directional (delta) layer metrics for one period's snapshot."""
    ce_dflow = snap["ce_delta_oi_chg"].values
    pe_dflow = snap["pe_delta_oi_chg"].values
    strikes  = snap["strike_price"].values
    spot     = float(snap["spot"].median())

    net_flow = float(ce_dflow.sum() + pe_dflow.sum())
    ce_sum   = float(ce_dflow.sum())
    pe_sum   = float(pe_dflow.sum())

    # Rotation locus: strike with highest |net delta flow|
    abs_net = np.abs(ce_dflow + pe_dflow)
    if abs_net.sum() > 0:
        # Weighted centroid of delta flow
        centroid = float((strikes * abs_net).sum() / abs_net.sum())
        peak_idx = np.argmax(abs_net)
        peak_strike = float(strikes[peak_idx])
        nearest = float(strikes[np.argmin(np.abs(strikes - centroid))])
    else:
        centroid, peak_strike, nearest = spot, spot, spot

    return {
        "net_delta_flow":  round(net_flow, 2),
        "ce_delta_flow":   round(ce_sum, 2),
        "pe_delta_flow":   round(pe_sum, 2),
        "rotation_centroid": round(centroid, 1),
        "rotation_peak":     peak_strike,
        "rotation_nearest":  nearest,
        "spot":              spot,
    }


def _vanna_layer(snap: pd.DataFrame) -> Dict[str, Any]:
    """Compute vanna layer metrics for one period's snapshot."""
    net_vanna = snap["net_vanna_ex"].values
    strikes   = snap["strike_price"].values

    total = float(net_vanna.sum())
    peak_idx = np.argmax(np.abs(net_vanna))
    peak = {
        "strike": float(strikes[peak_idx]),
        "value":  round(float(net_vanna[peak_idx]), 2),
    } if len(net_vanna) > 0 else None

    return {
        "net_vanna":  round(total, 2),
        "peak_vanna": peak,
    }


def _divergence_signals(
    gamma_series: Dict[str, Dict],
    delta_series: Dict[str, Dict],
    vanna_series: Dict[str, Dict],
    periods: List[str],
) -> Dict[str, Any]:
    """
    Compute pairwise divergence indicators across the period series.
    Returns signals for the latest period based on recent trajectory.
    """
    signals = {}
    if len(periods) < 2:
        return {"gamma_vanna": None, "delta_gamma": None, "delta_price": None}

    curr_p, prev_p = periods[-1], periods[-2]
    gc = gamma_series.get(curr_p, {}); gp = gamma_series.get(prev_p, {})
    dc = delta_series.get(curr_p, {}); dp = delta_series.get(prev_p, {})
    vc = vanna_series.get(curr_p, {}); vp = vanna_series.get(prev_p, {})

    # Gamma vs Vanna divergence
    gex_change = (gc.get("total_gex_m") or 0) - (gp.get("total_gex_m") or 0)
    vanna_change = (vc.get("net_vanna") or 0) - (vp.get("net_vanna") or 0)
    gamma_stable = abs(gex_change) < abs(gc.get("total_gex_m") or 1) * 0.15
    vanna_moving = abs(vanna_change) > abs(vc.get("net_vanna") or 1) * 0.20

    gv_signal = None
    if gamma_stable and vanna_moving:
        gv_signal = {
            "type": "fragile_pin",
            "label": "Gamma stable but vanna building — pin may break",
            "gamma_change": round(gex_change, 4),
            "vanna_change": round(vanna_change, 2),
        }
    elif abs(gex_change) > abs(gc.get("total_gex_m") or 1) * 0.30:
        gv_signal = {
            "type": "gamma_shift",
            "label": "Significant gamma regime change",
            "gamma_change": round(gex_change, 4),
            "vanna_change": round(vanna_change, 2),
        }
    signals["gamma_vanna"] = gv_signal

    # Delta vs Gamma divergence
    net_dflow = dc.get("net_delta_flow", 0)
    dg_signal = None
    if abs(net_dflow) > 100 and gc.get("total_gex_m", 0) > 0:
        dg_signal = {
            "type": "coiled_spring",
            "label": "Directional flow building under positive gamma — watch for breakout",
            "delta_flow": round(net_dflow, 2),
            "gex": gc.get("total_gex_m", 0),
        }
    signals["delta_gamma"] = dg_signal

    # Delta vs Price divergence
    spot_chg = (dc.get("spot") or 0) - (dp.get("spot") or 0)
    dp_signal = None
    if net_dflow > 0 and spot_chg < 0:
        dp_signal = {
            "type": "bullish_accumulation",
            "label": "Bullish delta flow against price drop — accumulation",
            "delta_flow": round(net_dflow, 2),
            "spot_change": round(spot_chg, 2),
        }
    elif net_dflow < 0 and spot_chg > 0:
        dp_signal = {
            "type": "bearish_distribution",
            "label": "Bearish delta flow against price rise — distribution",
            "delta_flow": round(net_dflow, 2),
            "spot_change": round(spot_chg, 2),
        }
    signals["delta_price"] = dp_signal

    return signals


# ═══════════════════════════════════════════════════════════════════
# Main endpoint
# ═══════════════════════════════════════════════════════════════════

@router.get("/api/exposure_flow")
def exposure_flow(
    symbol:         str           = Query(...),
    expiry:         str           = Query("all"),
    date_from:      Optional[str] = Query(None),
    date_to:        Optional[str] = Query(None),
    mode:           str           = Query("multiday"),  # multiday | intraday
    intraday_date:  Optional[str] = Query(None),
    min_oi:         float         = Query(0),
    min_volume:     float         = Query(100),
):
    today   = date.today().isoformat()
    d_from  = date_from or (date.today() - timedelta(days=5)).isoformat()
    d_to    = date_to or today
    ef      = "" if expiry == "all" else "AND expiry = ?"
    ep      = [] if expiry == "all" else [expiry[:10]]

    # ── Fetch data ────────────────────────────────────────────────
    if mode == "intraday":
        iday = intraday_date or today
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS dt,
                strike_price,
                CAST(expiry AS VARCHAR)           AS expiry,
                COALESCE(ce_oi, 0)                AS ce_oi,
                COALESCE(pe_oi, 0)                AS pe_oi,
                COALESCE(ce_oi_change, 0)         AS ce_oi_chg,
                COALESCE(pe_oi_change, 0)         AS pe_oi_chg,
                COALESCE(ce_volume, 0)            AS ce_vol,
                COALESCE(pe_volume, 0)            AS pe_vol,
                COALESCE(ce_iv, 0)                AS ce_iv,
                COALESCE(pe_iv, 0)                AS pe_iv,
                ABS(COALESCE(ce_delta, 0))        AS ce_adelta,
                COALESCE(underlying_price, 0)     AS spot,
                COALESCE(days_to_expiry, 0)       AS dte,
                COALESCE(lotsize, 1)              AS lotsize,
                COALESCE(ce_gexv, 0)              AS ce_gexv,
                COALESCE(pe_gexv, 0)              AS pe_gexv,
                COALESCE(net_gexv, 0)             AS net_gexv,
                COALESCE(ce_vanna_ex, 0)          AS ce_vanna_ex,
                COALESCE(pe_vanna_ex, 0)          AS pe_vanna_ex,
                COALESCE(net_vanna_ex, 0)         AS net_vanna_ex,
                COALESCE(ce_delta_oi_chg, 0)      AS ce_delta_oi_chg,
                COALESCE(pe_delta_oi_chg, 0)      AS pe_delta_oi_chg,
                COALESCE(ce_delta_oi, 0)          AS ce_delta_oi,
                COALESCE(pe_delta_oi, 0)          AS pe_delta_oi,
                COALESCE(net_charm_ex, 0)         AS net_charm_ex
            FROM {tbl()}
            WHERE symbol = ?
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND expiry >= CURRENT_DATE
              {ef}
              AND (ce_ltp > 0 OR pe_ltp > 0)
            ORDER BY timestamp, strike_price
            """,
            [symbol, iday] + ep,
        )
        if df.empty:
            raise HTTPException(404, "No intraday data")
        period_key = "dt"
    else:
        df = _eod_df(symbol, ef, ep, d_from, d_to)
        if df.empty:
            raise HTTPException(404, "No EOD data for date range")
        period_key = "dt"

    # ── Liquidity filter ──────────────────────────────────────────
    df = df[
        ((df["ce_oi"] >= min_oi) | (df["pe_oi"] >= min_oi)) &
        ((df["ce_vol"] >= min_volume) | (df["pe_vol"] >= min_volume))
    ]
    # Zero-Greeks filter
    df = df[
        ((df["ce_adelta"] > 0) & (df["ce_iv"] > 0)) |
        ((df.get("pe_adelta", df.get("pe_iv", pd.Series(dtype=float))) > 0) &
         (df["pe_iv"] > 0))
    ]
    if df.empty:
        raise HTTPException(404, "All strikes filtered by liquidity/data quality")

    periods = sorted(df[period_key].unique())
    if not periods:
        raise HTTPException(404, "No periods found")

    # ── Compute layers per period ─────────────────────────────────
    gamma_series:  Dict[str, Dict] = {}
    delta_series:  Dict[str, Dict] = {}
    vanna_series:  Dict[str, Dict] = {}
    gex_profiles:  Dict[str, List] = {}  # per-strike GEX for latest period

    for period in periods:
        snap = df[df[period_key] == period].sort_values("strike_price")
        if snap.empty:
            continue
        gamma_series[period]  = _gamma_layer(snap)
        delta_series[period]  = _directional_layer(snap)
        vanna_series[period]  = _vanna_layer(snap)

    # GEX profile for latest 2 periods (current + previous for comparison)
    for p in periods[-2:]:
        snap = df[df[period_key] == p].sort_values("strike_price")
        gex_profiles[p] = [
            {
                "strike":     float(r["strike_price"]),
                "net_gex":    round(float(r["net_gexv"]) / 1e6, 4),
                "ce_gex":     round(float(r["ce_gexv"]) / 1e6, 4),
                "pe_gex":     round(float(r["pe_gexv"]) / 1e6, 4),
                "ce_d_flow":  round(float(r["ce_delta_oi_chg"]), 2),
                "pe_d_flow":  round(float(r["pe_delta_oi_chg"]), 2),
                "net_vanna":  round(float(r["net_vanna_ex"]), 2),
            }
            for _, r in snap.iterrows()
        ]

    # ── Divergence signals ────────────────────────────────────────
    divergence = _divergence_signals(
        gamma_series, delta_series, vanna_series, periods
    )

    # ── Summary table (one row per period) ────────────────────────
    summary = []
    for i, p in enumerate(periods):
        g = gamma_series.get(p, {})
        d = delta_series.get(p, {})
        v = vanna_series.get(p, {})
        row: Dict[str, Any] = {
            "period":          p,
            "spot":            g.get("spot"),
            # Gamma
            "flip_point":      g.get("flip_strike"),
            "flip_interp":     g.get("flip_point"),
            "peak_pos_strike": g["peak_pos"]["strike"] if g.get("peak_pos") else None,
            "peak_pos_gex":    g["peak_pos"]["value"]  if g.get("peak_pos") else None,
            "peak_neg_strike": g["peak_neg"]["strike"] if g.get("peak_neg") else None,
            "peak_neg_gex":    g["peak_neg"]["value"]  if g.get("peak_neg") else None,
            "total_gex_m":     g.get("total_gex_m"),
            # Directional
            "net_delta_flow":  d.get("net_delta_flow"),
            "ce_delta_flow":   d.get("ce_delta_flow"),
            "pe_delta_flow":   d.get("pe_delta_flow"),
            "rotation_peak":   d.get("rotation_peak"),
            "rotation_nearest":d.get("rotation_nearest"),
            # Vanna
            "net_vanna":       v.get("net_vanna"),
            "peak_vanna_str":  v["peak_vanna"]["strike"] if v.get("peak_vanna") else None,
            "peak_vanna_val":  v["peak_vanna"]["value"]  if v.get("peak_vanna") else None,
        }
        # Period-over-period changes for the summary
        if i > 0:
            gp = gamma_series.get(periods[i-1], {})
            row["flip_change"] = (
                round(g.get("flip_point", 0) - gp.get("flip_point", 0), 1)
                if g.get("flip_point") and gp.get("flip_point") else None
            )
            row["gex_change"] = round(
                (g.get("total_gex_m") or 0) - (gp.get("total_gex_m") or 0), 4
            )
        else:
            row["flip_change"] = None
            row["gex_change"]  = None
        summary.append(row)

    return safe_response({
        "symbol":       symbol,
        "expiry":       expiry,
        "mode":         mode,
        "date_from":    d_from,
        "date_to":      d_to,
        "periods":      periods,
        "gamma":        gamma_series,
        "directional":  delta_series,
        "vanna":        vanna_series,
        "divergence":   divergence,
        "gex_profiles": gex_profiles,
        "summary":      summary,
    })
