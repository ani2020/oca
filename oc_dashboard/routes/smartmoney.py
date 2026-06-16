"""Smart Money Flow — premium-weighted OI clustering."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Any, Dict, Optional
import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, safe_response, _safe, latest_data_date
from ..helpers import _smooth, _find_clusters, _match_clusters, _eod_df
from .. import config

router = APIRouter()

# Track clusters across periods + velocity helpers
def _track_side(periods, period_clusters, side):
    from ..helpers import _match_clusters
    tracks = []
    track_id = 0
    prev_cls = []
    active = {}
    for period in periods:
        curr_cls = period_clusters.get(period, {}).get(side, [])
        spot = period_clusters.get(period, {}).get("spot", 0)
        mapping, dissolved = _match_clusters(prev_cls, curr_cls, spot)
        new_active = {}
        for ci, cc in enumerate(curr_cls):
            pi = mapping.get(ci)
            cc["period"] = period
            if pi is not None:
                tid = next(
                    (t["id"] for t in tracks
                     if t["points"] and
                     t["points"][-1].get("_pi") == pi and
                     t["status"] != "DISSOLVED"), None)
                if tid is None:
                    tid = track_id; track_id += 1
                    tracks.append({"id": tid, "status": "TRACKED", "points": []})
                cc["_pi"] = ci
                cc["status"] = "TRACKED"
                tracks[tid]["points"].append(cc)
                new_active[tid] = ci
            else:
                tid = track_id; track_id += 1
                cc["_pi"] = ci
                cc["status"] = "EMERGING"
                tracks.append({"id": tid, "status": "EMERGING", "points": [cc]})
                new_active[tid] = ci
        for tid_prev in active:
            if tid_prev not in new_active:
                for t in tracks:
                    if t["id"] == tid_prev:
                        t["status"] = "DISSOLVED"
        active = new_active
        prev_cls = curr_cls
    return tracks


def _add_velocity(tracks):
    for t in tracks:
        pts = t["points"]
        pf = [p.get("prem_flow", 0) for p in pts]
        vel = [0.0] + [pf[i]-pf[i-1] for i in range(1, len(pf))]
        acc = [0.0] + [vel[i]-vel[i-1] for i in range(1, len(vel))]
        for i, p in enumerate(pts):
            p["velocity"] = round(vel[i], 2)
            p["acceleration"] = round(acc[i], 2)
    return tracks

@router.get("/api/smart_money_flow")
def smart_money_flow(
    symbol:          str           = Query(...),
    expiry:          str           = Query("all"),
    date_from:       Optional[str] = Query(None),
    date_to:         Optional[str] = Query(None),
    mode:            str           = Query("multiday"),   # multiday | intraday
    intraday_date:   Optional[str] = Query(None),
    min_oi:          float         = Query(0),
    min_volume:      float         = Query(100),
    max_baq_pct:     float         = Query(15.0),
    smoothing:       int           = Query(3),
    min_prom_pct:    float         = Query(10.0),
    min_oi_change:   float         = Query(0),    # filter stale positions
):
    # Anchor to the symbol's latest DATA date (not wall-clock) so stale-data
    # days don't yield empty windows; widen the default lookback from that anchor.
    _anchor_str = latest_data_date(symbol) or date.today().isoformat()
    _anchor     = date.fromisoformat(_anchor_str)
    today       = _anchor_str
    d_from      = date_from or (_anchor - timedelta(days=5)).isoformat()
    d_to        = date_to   or today
    exp_filter = "" if expiry == "all" else "AND expiry = ?"
    exp_params = [] if expiry == "all" else [expiry[:10]]

    # ── Intraday mode: single day, all snapshots ──────────────────────────
    if mode == "intraday":
        iday = intraday_date or today
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts,
                strike_price,
                CAST(expiry AS VARCHAR)          AS expiry,
                COALESCE(ce_ltp,  0)             AS ce_ltp,
                COALESCE(pe_ltp,  0)             AS pe_ltp,
                COALESCE(ce_oi,   0)             AS ce_oi,
                COALESCE(pe_oi,   0)             AS pe_oi,
                COALESCE(ce_oi_change, 0)        AS ce_oi_chg,
                COALESCE(pe_oi_change, 0)        AS pe_oi_chg,
                COALESCE(ce_volume, 0)           AS ce_vol,
                COALESCE(pe_volume, 0)           AS pe_vol,
                COALESCE(ce_iv,   0)             AS ce_iv,
                COALESCE(pe_iv,   0)             AS pe_iv,
                ABS(COALESCE(ce_delta, 0))       AS ce_adelta,
                ABS(COALESCE(pe_delta, 0))       AS pe_adelta,
                COALESCE(ce_gamma, 0)            AS ce_gamma,
                COALESCE(pe_gamma, 0)            AS pe_gamma,
                COALESCE(ce_tbq,  0)             AS ce_tbq,
                COALESCE(pe_tbq,  0)             AS pe_tbq,
                COALESCE(ce_bid_ask_spread, 0)   AS ce_baq,
                COALESCE(pe_bid_ask_spread, 0)   AS pe_baq,
                COALESCE(underlying_price, 0)    AS spot,
                COALESCE(days_to_expiry, 0)      AS dte,
                COALESCE(lotsize, 1)             AS lotsize,
                COALESCE(ce_prem_oi,     0)      AS ce_prem_oi,
                COALESCE(pe_prem_oi,     0)      AS pe_prem_oi,
                COALESCE(ce_prem_oi_chg, 0)      AS ce_prem_oi_chg,
                COALESCE(pe_prem_oi_chg, 0)      AS pe_prem_oi_chg,
                COALESCE(ce_time_value,  0)      AS ce_tv,
                COALESCE(pe_time_value,  0)      AS pe_tv,
                COALESCE(ce_nd2,         0)      AS ce_nd2,
                COALESCE(pe_nd2,         0)      AS pe_nd2
            FROM {tbl()}
            WHERE symbol = ?
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND expiry >= CURRENT_DATE
              {exp_filter}
              AND (ce_ltp > 0 OR pe_ltp > 0)
            ORDER BY timestamp, strike_price
            """,
            [symbol, iday] + exp_params,
        )
        if df.empty:
            raise HTTPException(404, "No intraday data")
        period_key = "ts"
        periods    = sorted(df["ts"].unique())

    # ── Multi-day mode: EOD snapshots ─────────────────────────────────────
    else:
        df = _eod_df(symbol, exp_filter, exp_params, d_from, d_to)
        if df.empty:
            raise HTTPException(404, "No EOD data for date range — check symbol/expiry/dates")
        period_key = "dt"
        periods    = sorted(df["dt"].unique())

    # ── Liquidity + data quality filters ─────────────────────────────────
    if min_oi_change > 0:
        df = df[
            (df["ce_oi_chg"].abs() >= min_oi_change) |
            (df["pe_oi_chg"].abs() >= min_oi_change)
        ]
    df["ce_baq_pct"] = df.apply(
        lambda r: r["ce_baq"]/r["ce_ltp"]*100 if r["ce_ltp"] > 0 else 999, axis=1)
    df["pe_baq_pct"] = df.apply(
        lambda r: r["pe_baq"]/r["pe_ltp"]*100 if r["pe_ltp"] > 0 else 999, axis=1)
    df = df[
        ((df["ce_oi"] >= min_oi) | (df["pe_oi"] >= min_oi)) &
        ((df["ce_vol"] >= min_volume) | (df["pe_vol"] >= min_volume)) &
        ((df["ce_baq_pct"] <= max_baq_pct) | (df["pe_baq_pct"] <= max_baq_pct)) &
        (((df["ce_adelta"] > 0) & (df["ce_iv"] > 0)) |
         ((df["pe_adelta"] > 0) & (df["pe_iv"] > 0)))
    ]
    if df.empty:
        raise HTTPException(404, "All strikes filtered by liquidity / data quality")

    # ── Run clustering per period ─────────────────────────────────────────
    # Results: { period → { "ce": [clusters], "pe": [clusters], "spot": float } }
    period_clusters: dict = {}
    all_spots: dict = {}

    for period in periods:
        snap = df[df[period_key] == period].copy()
        if snap.empty:
            continue
        strikes     = snap["strike_price"].values
        spot        = float(snap["spot"].median())
        all_spots[period] = spot

        # premium-weighted OI: ltp × oi (or pre-computed col if available)
        # Use TIME-VALUE-weighted OI for clustering, not total premium.
        # Total premium (ltp×oi) is dominated by deep ITM options whose
        # intrinsic value is huge — this pushed CE clusters below spot and
        # PE clusters above spot (backwards). Time value (extrinsic) is the
        # genuine "bet" portion and peaks where real positioning sits.
        ce_tv_oi = (snap["ce_tv"] * snap["ce_oi"]).values   # extrinsic × OI
        pe_tv_oi = (snap["pe_tv"] * snap["pe_oi"]).values
        # Keep total prem_oi for display/reporting
        ce_prem_oi = snap["ce_prem_oi"].values
        pe_prem_oi = snap["pe_prem_oi"].values

        ce_extra = {
            "avg_delta":    snap["ce_adelta"].values,
            "avg_iv":       snap["ce_iv"].values,
            "avg_gamma":    snap["ce_gamma"].values,
            "avg_nd2":      snap["ce_nd2"].values,          # P(ITM) from DB
            "prem_flow":    snap["ce_prem_oi_chg"].values,  # ce_ltp × ce_oi_chg (from DB)
            "premium_oi":   snap["ce_prem_oi"].values,      # total premium OI (display)
            "oi_vol_ratio": np.where(snap["ce_vol"] > 0,
                                snap["ce_oi_chg"] / snap["ce_vol"], 0),
            "gamma_adj_oi": (snap["ce_oi_chg"] * snap["ce_gamma"]
                             * snap["lotsize"]).values,
        }
        pe_extra = {
            "avg_delta":    snap["pe_adelta"].values,
            "avg_iv":       snap["pe_iv"].values,
            "avg_gamma":    snap["pe_gamma"].values,
            "avg_nd2":      snap["pe_nd2"].values,
            "prem_flow":    snap["pe_prem_oi_chg"].values,
            "premium_oi":   snap["pe_prem_oi"].values,      # total premium OI (display)
            "oi_vol_ratio": np.where(snap["pe_vol"] > 0,
                                snap["pe_oi_chg"] / snap["pe_vol"], 0),
            "gamma_adj_oi": (snap["pe_oi_chg"] * snap["pe_gamma"]
                             * snap["lotsize"]).values,
        }

        # Cluster on time-value-weighted OI (genuine positioning signal)
        ce_clusters = _find_clusters(
            strikes, ce_tv_oi, smoothing, min_prom_pct, ce_extra)
        pe_clusters = _find_clusters(
            strikes, pe_tv_oi, smoothing, min_prom_pct, pe_extra)

        # Add PCR per cluster (pe_oi sum / ce_oi sum for member strikes)
        for cl in ce_clusters:
            mi = (snap["strike_price"] >= cl["min_strike"]) &                  (snap["strike_price"] <= cl["max_strike"])
            ce_tot = float(snap.loc[mi, "ce_oi"].sum())
            pe_tot = float(snap.loc[mi, "pe_oi"].sum())
            cl["pcr"] = round(pe_tot/ce_tot, 3) if ce_tot > 0 else None
            # Buy/sell imbalance
            ce_tbq = float(snap.loc[mi, "ce_tbq"].sum())
            pe_tbq = float(snap.loc[mi, "pe_tbq"].sum())
            cl["buy_imbalance"] = round(
                (ce_tbq - pe_tbq)/(ce_tbq + pe_tbq), 3
            ) if (ce_tbq + pe_tbq) > 0 else 0

        for cl in pe_clusters:
            mi = (snap["strike_price"] >= cl["min_strike"]) &                  (snap["strike_price"] <= cl["max_strike"])
            ce_tot = float(snap.loc[mi, "ce_oi"].sum())
            pe_tot = float(snap.loc[mi, "pe_oi"].sum())
            cl["pcr"] = round(pe_tot/ce_tot, 3) if ce_tot > 0 else None
            pe_tbq = float(snap.loc[mi, "pe_tbq"].sum())
            ce_tbq = float(snap.loc[mi, "ce_tbq"].sum())
            cl["buy_imbalance"] = round(
                (ce_tbq - pe_tbq)/(ce_tbq + pe_tbq), 3
            ) if (ce_tbq + pe_tbq) > 0 else 0

        period_clusters[period] = {
            "ce":   ce_clusters,
            "pe":   pe_clusters,
            "spot": spot,
        }

    # ── Track clusters across periods (multi-day migration) ───────────────
    ce_tracks: list = []   # list of track dicts {id, points[{period,cluster}], status}
    pe_tracks: list = []

    def _track_side(periods, period_clusters, side):
        tracks   = []
        track_id = 0
        prev_cls = []
        active   = {}   # track_id → cluster dict of prev period

        for period in periods:
            curr_cls = period_clusters.get(period, {}).get(side, [])
            spot     = period_clusters.get(period, {}).get("spot", 0)
            mapping, dissolved = _match_clusters(prev_cls, curr_cls, spot)

            # Update active tracks
            new_active = {}
            for ci, cc in enumerate(curr_cls):
                pi = mapping.get(ci)
                cc["period"] = period
                if pi is not None:
                    # Find existing track for pi
                    tid = next(
                        (t["id"] for t in tracks
                         if t["points"] and
                         t["points"][-1].get("_pi") == pi and
                         t["status"] != "DISSOLVED"),
                        None
                    )
                    if tid is None:
                        tid = track_id; track_id += 1
                        tracks.append({"id": tid, "status": "TRACKED",
                                       "points": []})
                    cc["_pi"] = ci
                    cc["status"] = "TRACKED"
                    tracks[tid]["points"].append(cc)
                    new_active[tid] = ci
                else:
                    # New cluster — EMERGING
                    tid = track_id; track_id += 1
                    cc["_pi"] = ci
                    cc["status"] = "EMERGING"
                    tracks.append({"id": tid, "status": "EMERGING",
                                   "points": [cc]})
                    new_active[tid] = ci

            # Mark dissolved tracks
            for tid, prev_ci in active.items():
                if tid not in new_active:
                    for t in tracks:
                        if t["id"] == tid:
                            t["status"] = "DISSOLVED"
            active   = new_active
            prev_cls = curr_cls

        return tracks

    ce_tracks = _track_side(periods, period_clusters, "ce")
    pe_tracks = _track_side(periods, period_clusters, "pe")

    # ── Velocity and acceleration per track ───────────────────────────────
    def _add_velocity(tracks):
        for t in tracks:
            pts = t["points"]
            pf  = [p.get("prem_flow", 0) for p in pts]
            vel = [0.0] + [pf[i]-pf[i-1] for i in range(1, len(pf))]
            acc = [0.0] + [vel[i]-vel[i-1] for i in range(1, len(vel))]
            for i, p in enumerate(pts):
                p["velocity"]     = round(vel[i], 2)
                p["acceleration"] = round(acc[i], 2)
        return tracks

    ce_tracks = _add_velocity(ce_tracks)
    pe_tracks = _add_velocity(pe_tracks)

    # Clean internal keys before serialising
    for t in ce_tracks + pe_tracks:
        for p in t["points"]:
            p.pop("_pi", None)

    return safe_response({
        "symbol":    symbol,
        "expiry":    expiry,
        "mode":      mode,
        "date_from": d_from,
        "date_to":   d_to,
        "periods":   periods,
        "spots":     all_spots,
        "ce_tracks": ce_tracks,
        "pe_tracks": pe_tracks,
        "cluster_params": {
            "smoothing":     smoothing,
            "min_prom_pct":  min_prom_pct,
        },
    })


