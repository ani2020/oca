"""OI Flow bucket analysis (delta-based buckets)."""
from __future__ import annotations
import math
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional
import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe, latest_data_date
from ..helpers import _assign_bucket, _dte_regime, _flow_signal
from .. import config

router = APIRouter()

@router.get("/api/oi_flow_buckets")
def oi_flow_buckets(
    symbol:       str            = Query(...),
    expiry:       str            = Query("all"),      # specific date or "all"
    date_from:    Optional[str]  = Query(None),       # YYYY-MM-DD, default today
    date_to:      Optional[str]  = Query(None),       # YYYY-MM-DD, default today
    min_oi:       float          = Query(0),
    min_volume:   float          = Query(0),
    max_baq_pct:  float          = Query(15.0),       # max bid-ask/ltp %
    b_atm:        float          = Query(0.50),       # |delta| >= this → DEEP_ITM
    b_near:       float          = Query(0.30),       # |delta| >= this → ATM
    b_far:        float          = Query(0.15),       # |delta| >= this → NEAR_OTM
    b_deep:       float          = Query(0.05),       # |delta| >= this → FAR_OTM
                                                       # < b_deep      → DEEP_OTM
    stable_pct:   float          = Query(1.0),        # total OI change % for crossing signal
    cross_pct:    float          = Query(5.0),        # bucket share change % for crossing signal
):
    """
    OI Flow Bucket Analysis.

    1. Fetches all snapshots for symbol/expiry in the date range.
    2. Assigns each strike to a fixed bucket based on its delta at the
       FIRST snapshot (stable cohort — prevents migration distortion).
    3. For each subsequent timestamp, sums OI, OI change, volume, TBQ
       per bucket.
    4. Computes velocity (d(OI)/dt) and acceleration (d²(OI)/dt²) per bucket.
    5. Detects migrated strikes (current delta bucket ≠ assigned bucket).
    6. Emits crossing signal when total OI stable but bucket composition shifts.
    """
    # Anchor to symbol's latest DATA date (not wall-clock) for stale-data days
    anchor = latest_data_date(symbol) or date.today().isoformat()
    d_from = date_from or anchor
    d_to   = date_to   or anchor

    expiry_filter = "" if expiry == "all" else "AND expiry = ?"
    expiry_params = [] if expiry == "all" else [expiry[:10]]

    # Fetch all rows in date range — no timestamp BETWEEN, use date cast
    df = qdf(
        f"""
        SELECT
            STRFTIME(timestamp, '%Y-%m-%d %H:%M')   AS ts,
            CAST(timestamp AS DATE)                  AS dt,
            CAST(expiry AS VARCHAR)                  AS expiry,
            strike_price,
            COALESCE(days_to_expiry,  0)             AS dte,
            COALESCE(underlying_price,0)             AS spot,
            ABS(COALESCE(ce_delta, 0))               AS ce_adelta,
            ABS(COALESCE(pe_delta, 0))               AS pe_adelta,
            COALESCE(ce_oi,        0)                AS ce_oi,
            COALESCE(pe_oi,        0)                AS pe_oi,
            COALESCE(ce_oi_change, 0)                AS ce_oi_chg,
            COALESCE(pe_oi_change, 0)                AS pe_oi_chg,
            COALESCE(ce_ltp,       0)                AS ce_ltp,
            COALESCE(pe_ltp,       0)                AS pe_ltp,
            COALESCE(ce_iv,        0)                AS ce_iv,
            COALESCE(pe_iv,        0)                AS pe_iv,
            COALESCE(ce_volume,    0)                AS ce_vol,
            COALESCE(pe_volume,    0)                AS pe_vol,
            COALESCE(ce_gamma,     0)                AS ce_gamma,
            COALESCE(pe_gamma,     0)                AS pe_gamma,
            COALESCE(ce_gexv,      0)                AS ce_gexv,
            COALESCE(pe_gexv,      0)                AS pe_gexv,
            COALESCE(ce_vanna,     0)                AS ce_vanna,
            COALESCE(net_vanna_ex, 0)                AS net_vanna,
            COALESCE(net_flow,     0)                AS net_flow,
            COALESCE(ce_tbq,       0)                AS ce_tbq,
            COALESCE(pe_tbq,       0)                AS pe_tbq,
            COALESCE(ce_bid_ask_spread, 0)           AS ce_baq,
            COALESCE(pe_bid_ask_spread, 0)           AS pe_baq,
            COALESCE(lotsize, 1)                     AS lot
        FROM {tbl()}
        WHERE symbol = ?
          AND CAST(timestamp AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
          AND expiry >= CURRENT_DATE
          {expiry_filter}
        ORDER BY timestamp, strike_price
        """,
        [symbol, d_from, d_to] + expiry_params,
    )
    if df.empty:
        raise HTTPException(404, "No flow data for given filters")

    # ── Step 1: Apply liquidity filters ──────────────────────────────────
    # For each strike, compute baq% for both sides
    df["ce_baq_pct"] = df.apply(
        lambda r: r["ce_baq"]/r["ce_ltp"]*100 if r["ce_ltp"] > 0 else 999, axis=1)
    df["pe_baq_pct"] = df.apply(
        lambda r: r["pe_baq"]/r["pe_ltp"]*100 if r["pe_ltp"] > 0 else 999, axis=1)

    df = df[
        (df["ce_oi"] >= min_oi) | (df["pe_oi"] >= min_oi)
    ]
    df = df[
        (df["ce_vol"] >= min_volume) | (df["pe_vol"] >= min_volume)
    ]
    df = df[
        (df["ce_baq_pct"] <= max_baq_pct) | (df["pe_baq_pct"] <= max_baq_pct)
    ]
    # Filter out rows with zero/missing Greeks — these are data quality issues
    # where NSE didn't publish IV/delta (e.g. untradeable deep strikes, pre-open).
    # A row is valid only if at least one side has both a non-zero delta AND non-zero IV.
    df = df[
        ((df["ce_adelta"] > 0) & (df["ce_iv"] > 0)) |
        ((df["pe_adelta"] > 0) & (df["pe_iv"] > 0))
    ]
    if df.empty:
        raise HTTPException(404, "All strikes filtered by liquidity criteria")

    # ── Step 2: Fixed cohort — assign bucket from FIRST snapshot ─────────
    timestamps = sorted(df["ts"].unique().tolist())
    if not timestamps:
        raise HTTPException(404, "No timestamps found")

    ts0 = timestamps[0]
    first_snap = df[df["ts"] == ts0][["strike_price","ce_adelta","pe_adelta"]].copy()

    # Use CE delta for CE-side bucket, PE delta for PE-side bucket.
    # DO NOT use max(ce,pe): put-call parity means pe_adelta ≈ 1-ce_adelta,
    # so max() always picks pe_adelta > 0.5 pushing all strikes into DEEP_ITM.
    # Instead assign each strike one bucket based on CE delta (calls drive the
    # bucket label; PE analysis uses the same strike grouping).
    # CE side: bucket from CE delta (call side)
    first_snap["ce_bucket_open"] = first_snap["ce_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))
    # PE side: bucket from PE delta (put side — pe_adelta already = |pe_delta|)
    first_snap["pe_bucket_open"] = first_snap["pe_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))

    ce_cohort = first_snap.set_index("strike_price")["ce_bucket_open"].to_dict()
    pe_cohort = first_snap.set_index("strike_price")["pe_bucket_open"].to_dict()

    # For groupby aggregation, use CE bucket as the primary bucket label
    # (consistent with how we report OI flow — CE and PE tracked under same strike)
    df["bucket"]    = df["strike_price"].map(ce_cohort).fillna("DEEP_OTM")
    df["ce_bucket_open"] = df["strike_price"].map(ce_cohort).fillna("DEEP_OTM")
    df["pe_bucket_open"] = df["strike_price"].map(pe_cohort).fillna("DEEP_OTM")

    # Current buckets — computed fresh each row
    df["ce_bucket_cur"] = df["ce_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))
    df["pe_bucket_cur"] = df["pe_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))

    # A strike "migrated" on CE side if its CE bucket changed
    df["ce_migrated"] = df["ce_bucket_open"] != df["ce_bucket_cur"]
    # A strike "migrated" on PE side if its PE bucket changed
    df["pe_migrated"] = df["pe_bucket_open"] != df["pe_bucket_cur"]
    df["migrated"]    = df["ce_migrated"] | df["pe_migrated"]

    # ── Step 3: DTE regime ───────────────────────────────────────────────
    dte_val  = float(df["dte"].median())
    dte_reg  = _dte_regime(dte_val)
    spot_now = float(df[df["ts"] == timestamps[-1]]["spot"].mean()) if timestamps else 0
    spot_t0  = float(df[df["ts"] == ts0]["spot"].mean()) if ts0 else spot_now
    spot_chg = spot_now - spot_t0

    # ── Step 4: Aggregate per timestamp × bucket ─────────────────────────
    grp = df.groupby(["ts","bucket"]).agg(
        ce_oi        = ("ce_oi",    "sum"),
        pe_oi        = ("pe_oi",    "sum"),
        ce_oi_chg    = ("ce_oi_chg","sum"),
        pe_oi_chg    = ("pe_oi_chg","sum"),
        ce_vol       = ("ce_vol",   "sum"),
        pe_vol       = ("pe_vol",   "sum"),
        ce_gexv      = ("ce_gexv",  "sum"),
        pe_gexv      = ("pe_gexv",  "sum"),
        ce_tbq       = ("ce_tbq",   "sum"),
        pe_tbq       = ("pe_tbq",   "sum"),
        net_vanna    = ("net_vanna","sum"),
        net_flow     = ("net_flow", "sum"),
        strike_count = ("strike_price","nunique"),
        migrated_count = ("migrated","sum"),
    ).reset_index()

    # Compute cumulative OI flow from first timestamp
    grp = grp.sort_values(["bucket","ts"])
    grp["ce_cum_flow"] = grp.groupby("bucket")["ce_oi_chg"].cumsum()
    grp["pe_cum_flow"] = grp.groupby("bucket")["pe_oi_chg"].cumsum()
    grp["net_cum_flow"] = grp["ce_cum_flow"] - grp["pe_cum_flow"]

    # PCR per bucket
    grp["pcr"] = grp.apply(
        lambda r: round(r["pe_oi"]/r["ce_oi"], 3) if r["ce_oi"] > 0 else None, axis=1)

    # ── Step 5: Velocity and Acceleration per bucket ──────────────────────
    def _deriv(series: pd.Series) -> pd.Series:
        """First derivative (velocity) using central differences."""
        return series.diff().fillna(0)

    vel_dfs = []
    for bname, bdf in grp.groupby("bucket"):
        bdf = bdf.sort_values("ts").copy()
        bdf["ce_velocity"]     = _deriv(bdf["ce_cum_flow"])
        bdf["pe_velocity"]     = _deriv(bdf["pe_cum_flow"])
        bdf["ce_acceleration"] = _deriv(bdf["ce_velocity"])
        bdf["pe_acceleration"] = _deriv(bdf["pe_velocity"])
        vel_dfs.append(bdf)
    if vel_dfs:
        grp = pd.concat(vel_dfs).sort_values(["ts","bucket"])

    # ── Step 6: Crossing signal ───────────────────────────────────────────
    crossing_signal = None
    if len(timestamps) >= 2:
        ts_now   = timestamps[-1]
        snap_t0  = grp[grp["ts"] == ts0].copy()
        snap_now = grp[grp["ts"] == ts_now].copy()
        total_t0  = snap_t0["ce_oi"].sum() + snap_t0["pe_oi"].sum()
        total_now = snap_now["ce_oi"].sum() + snap_now["pe_oi"].sum()
        if total_t0 > 0:
            total_chg_pct = abs(total_now - total_t0) / total_t0 * 100
            if total_chg_pct <= stable_pct:
                # Check if any bucket's share changed significantly
                for bname in _BUCKET_NAMES:
                    bt0  = snap_t0[snap_t0["bucket"]==bname]
                    bnow = snap_now[snap_now["bucket"]==bname]
                    oi_t0  = float(bt0["ce_oi"].sum() + bt0["pe_oi"].sum()) if not bt0.empty else 0
                    oi_now = float(bnow["ce_oi"].sum() + bnow["pe_oi"].sum()) if not bnow.empty else 0
                    share_t0  = oi_t0  / total_t0  * 100 if total_t0  > 0 else 0
                    share_now = oi_now / total_now * 100 if total_now > 0 else 0
                    if abs(share_now - share_t0) >= cross_pct:
                        direction = "increasing" if share_now > share_t0 else "decreasing"
                        crossing_signal = {
                            "fired":        True,
                            "bucket":       bname,
                            "direction":    direction,
                            "share_change": round(share_now - share_t0, 2),
                            "total_oi_chg": round(total_chg_pct, 2),
                            "description":  (
                                f"Total OI stable ({total_chg_pct:.1f}%) but "
                                f"{bname} bucket share {direction} by "
                                f"{abs(share_now-share_t0):.1f}% — "
                                f"rotation signal"
                            ),
                        }
                        break

    # ── Step 7: Migrated strikes at latest timestamp ──────────────────────
    latest = df.loc[(df["ts"] == timestamps[-1]) & (df["migrated"] == True)]
    migrated_list = []
    for _, row in latest.iterrows():
        entry: Dict[str, Any] = {
            "strike": float(row["strike_price"]),
            # CE side
            "ce_delta":       round(float(row["ce_adelta"]), 3),
            "ce_bucket_open": row["ce_bucket_open"],
            "ce_bucket_cur":  row["ce_bucket_cur"],
            "ce_migrated":    bool(row["ce_migrated"]),
            "ce_oi":          float(row["ce_oi"]),
            "ce_oi_chg":      float(row["ce_oi_chg"]),
            # PE side
            "pe_delta":       round(float(row["pe_adelta"]), 3),
            "pe_bucket_open": row["pe_bucket_open"],
            "pe_bucket_cur":  row["pe_bucket_cur"],
            "pe_migrated":    bool(row["pe_migrated"]),
            "pe_oi":          float(row["pe_oi"]),
            "pe_oi_chg":      float(row["pe_oi_chg"]),
        }
        migrated_list.append(entry)

    # ── Step 8: Flow signals at latest snapshot ───────────────────────────
    flow_signals = {}
    snap_latest = grp[grp["ts"] == timestamps[-1]] if timestamps else pd.DataFrame()
    # Get strike ranges per bucket from the full df at latest timestamp
    latest_df = df.loc[df["ts"] == timestamps[-1]] if timestamps else pd.DataFrame()
    bucket_strike_ranges: Dict[str, dict] = {}
    for bname in _BUCKET_NAMES:
        bstrikes = latest_df.loc[latest_df["bucket"] == bname, "strike_price"]
        if not bstrikes.empty:
            bucket_strike_ranges[bname] = {
                "min_strike": float(bstrikes.min()),
                "max_strike": float(bstrikes.max()),
            }
        else:
            bucket_strike_ranges[bname] = {"min_strike": None, "max_strike": None}

    for bname in _BUCKET_NAMES:
        brow = snap_latest[snap_latest["bucket"] == bname]
        if brow.empty:
            flow_signals[bname] = {"ce_signal":"—","pe_signal":"—",
                                   "flow_type":"—","buy_imbalance":0,
                                   "strike_count":0,
                                   **bucket_strike_ranges.get(bname,{})}
            continue
        r = brow.iloc[0]
        sig = _flow_signal(
            float(r.get("ce_oi_chg",0)), float(r.get("pe_oi_chg",0)),
            spot_chg,
            float(r.get("ce_tbq",0)),    float(r.get("pe_tbq",0)),
        )
        sig["strike_count"]    = int(r.get("strike_count",0))
        sig["migrated_count"]  = int(r.get("migrated_count",0))
        sig["ce_cum_flow"]     = round(float(r.get("ce_cum_flow",0)),0)
        sig["pe_cum_flow"]     = round(float(r.get("pe_cum_flow",0)),0)
        sig["net_cum_flow"]    = round(float(r.get("net_cum_flow",0)),0)
        sig["pcr"]             = r.get("pcr")
        sig.update(bucket_strike_ranges.get(bname, {}))
        flow_signals[bname] = sig

    # ── Serialise time-series per bucket ─────────────────────────────────
    bucket_series = {}
    for bname in _BUCKET_NAMES:
        bdf = grp[grp["bucket"]==bname].sort_values("ts")
        bucket_series[bname] = {
            "timestamps":       bdf["ts"].tolist(),
            "ce_cum_flow":      [round(v,0) for v in bdf["ce_cum_flow"].fillna(0)],
            "pe_cum_flow":      [round(v,0) for v in bdf["pe_cum_flow"].fillna(0)],
            "net_cum_flow":     [round(v,0) for v in bdf["net_cum_flow"].fillna(0)],
            "ce_velocity":      [round(v,2) for v in bdf.get("ce_velocity",pd.Series(dtype=float)).fillna(0)],
            "pe_velocity":      [round(v,2) for v in bdf.get("pe_velocity",pd.Series(dtype=float)).fillna(0)],
            "ce_acceleration":  [round(v,2) for v in bdf.get("ce_acceleration",pd.Series(dtype=float)).fillna(0)],
            "pe_acceleration":  [round(v,2) for v in bdf.get("pe_acceleration",pd.Series(dtype=float)).fillna(0)],
            "pcr":              [_safe(v) for v in bdf["pcr"]],
            "ce_gexv":          [round(v/1e6,4) for v in bdf["ce_gexv"].fillna(0)],
            "pe_gexv":          [round(v/1e6,4) for v in bdf["pe_gexv"].fillna(0)],
            "net_flow":         [round(v,2) for v in bdf["net_flow"].fillna(0)],
            "strike_count":     bdf["strike_count"].tolist(),
            "migrated_count":   bdf["migrated_count"].astype(int).tolist(),
            "ce_tbq":           [round(v,0) for v in bdf["ce_tbq"].fillna(0)],
            "pe_tbq":           [round(v,0) for v in bdf["pe_tbq"].fillna(0)],
        }

    return safe_response({
        "symbol":           symbol,
        "expiry":           expiry,
        "date_from":        d_from,
        "date_to":          d_to,
        "timestamps":       timestamps,
        "dte":              round(dte_val, 1),
        "dte_regime":       dte_reg,
        "spot_open":        round(spot_t0, 2),
        "spot_now":         round(spot_now, 2),
        "spot_chg":         round(spot_chg, 2),
        "bucket_thresholds": {
            "DEEP_ITM": f"|δ| ≥ {b_atm}",
            "ATM":      f"|δ| {b_near}–{b_atm}",
            "NEAR_OTM": f"|δ| {b_far}–{b_near}",
            "FAR_OTM":  f"|δ| {b_deep}–{b_far}",
            "DEEP_OTM": f"|δ| < {b_deep}",
        },
        "buckets":          bucket_series,
        "flow_signals":     flow_signals,
        "crossing_signal":  crossing_signal,
        "migrated_strikes": migrated_list,
    })



