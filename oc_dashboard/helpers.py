"""Shared calculation helpers — gamma, clustering, flow analysis."""
from __future__ import annotations
from datetime import date
from typing import Any, Dict, List, Optional

import math
import numpy as np
import pandas as pd
from scipy.signal import find_peaks
from scipy.ndimage import uniform_filter1d

# Shared pure-math (single source, also used by standalone batch script).
# Robust import: try as-is, then add project root to path. If still missing,
# fail loudly with a clear message rather than breaking every route silently.
_core = None
try:
    import exposure_core as _core
except ImportError:
    import sys, pathlib
    _root = pathlib.Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    try:
        import exposure_core as _core
    except ImportError as _e:
        raise ImportError(
            "exposure_core.py not found. It must be placed at the project root "
            f"({_root}), alongside the oc_dashboard package and oc_exposure_eod.py. "
            "Download exposure_core.py and put it there."
        ) from _e
from fastapi import HTTPException

from .db import qdf, tbl, _safe, ts_filter_clause


# ═══════════════════════════════════════════════════════════════════
# Gamma profile and flip point
# ═══════════════════════════════════════════════════════════════════

def _build_gamma_profile(
    symbol: str, expiry: str, ts_filter: str,
    num_levels: int = 200, price_range_pct: float = 5.0,
) -> tuple[pd.DataFrame, float, float, int]:
    """Returns (profile_df, spot, dte, lot_size)."""
    # Use ts_filter if provided, otherwise fall back to MAX(timestamp) for this expiry
    if ts_filter:
        _ts_clause, _ts_params = ts_filter_clause(ts_filter)
    else:
        _ts_clause = f"AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)"
        _ts_params = [symbol, expiry[:10]]

    df = qdf(
        f"""
        SELECT
            strike_price,
            COALESCE(ce_gamma,        0) AS ce_gamma,
            COALESCE(pe_gamma,        0) AS pe_gamma,
            COALESCE(ce_oi,           0) AS ce_oi,
            COALESCE(pe_oi,           0) AS pe_oi,
            COALESCE(underlying_price,0) AS spot,
            COALESCE(days_to_expiry,  0) AS dte,
            COALESCE(atm_strike,      0) AS atm_strike,
            lotsize                       AS raw_lot,
            COALESCE(ce_iv,           0) AS ce_iv
        FROM {tbl()}
        WHERE symbol = ?
          AND expiry  = ?
          {_ts_clause}
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + _ts_params,
    )
    if df.empty:
        return pd.DataFrame(), 0, 0, 1

    spot     = float(df["spot"].iloc[0])
    dte      = float(df["dte"].iloc[0])
    lot_size = max(int(df["raw_lot"].iloc[0]) if df["raw_lot"].iloc[0] is not None else 1, 1)
    strikes  = df["strike_price"].values
    call_gex = df["ce_gamma"].values * df["ce_oi"].values * lot_size
    put_gex  = df["pe_gamma"].values * df["pe_oi"].values * lot_size
    net_gex  = call_gex - put_gex   # sign: positive = call gamma dominant

    # Adaptive range from ATM IV (expected move + buffer)
    atm_mask = np.abs(strikes - df["atm_strike"].iloc[0]) < 1e-6
    if not atm_mask.any():
        # Fallback: nearest strike to atm_strike
        atm_mask = np.zeros(len(strikes), dtype=bool)
        atm_mask[int(np.argmin(np.abs(strikes - df["atm_strike"].iloc[0])))] = True
    atm_iv   = float(df.loc[df["strike_price"] == strikes[atm_mask][0], "ce_iv"].iloc[0]) if atm_mask.any() else 0
    lo, hi   = gamma_analysis_range(spot, atm_iv=atm_iv, dte=dte)

    # Flip point: computed on ACTUAL discrete strikes (not linspace) for accuracy.
    # Linspace maps multiple levels to same strike, shifting the zero-crossing.
    rng_mask   = (strikes >= lo) & (strikes <= hi)
    flip_point, flip_nearest, _ = _core.gamma_flip(
        strikes[rng_mask], net_gex[rng_mask], ref_price=spot
    )

    # Linspace grid for VISUAL chart only (smooth curve)
    levels = np.linspace(lo, hi, num_levels)
    rows = []
    for lvl in levels:
        ni = int(np.argmin(np.abs(strikes - lvl)))
        cg =  float(call_gex[ni]) / 1e9
        pg = -float(put_gex[ni])  / 1e9
        rows.append({
            "level":                float(lvl),
            "near_strike":          float(strikes[ni]),
            "call_gamma_billions":  cg,
            "put_gamma_billions":   pg,
            "total_gamma_billions": cg + pg,
        })
    result_df = pd.DataFrame(rows)
    # Attach flip point to the df so callers can read it directly
    result_df.attrs["flip_point"]   = flip_point
    result_df.attrs["flip_nearest"] = flip_nearest
    return result_df, spot, dte, lot_size


def _flip_and_magnet(gdf: pd.DataFrame):
    # Prefer the pre-computed discrete-strike flip (stored in df.attrs by
    # _build_gamma_profile) — more accurate than linspace zero-crossing.
    # Fall back to linspace crossing only if attrs not set.
    gamma_flip = gdf.attrs.get("flip_point") if hasattr(gdf, "attrs") else None
    if gamma_flip is None:
        g, x = gdf["total_gamma_billions"].values, gdf["level"].values
        idx  = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
        if len(idx):
            i = idx[0]; d = g[i+1]-g[i]
            if d: gamma_flip = float(x[i] - g[i]*(x[i+1]-x[i])/d)
    max_g = float(gdf["total_gamma_billions"].max())
    magnet = None
    if max_g > 0:
        zone = gdf[gdf["total_gamma_billions"] >= 0.75 * max_g]
        if not zone.empty:
            magnet = {
                "lower":    float(zone["near_strike"].min()),
                "upper":    float(zone["near_strike"].max()),
                "center":   float(zone.loc[zone["total_gamma_billions"].idxmax(), "near_strike"]),
                "strength": round(max_g, 4),
            }
    return gamma_flip, magnet




# ═══════════════════════════════════════════════════════════════════
# Gamma analysis (inline helper used by gamma_analysis endpoint)
# ═══════════════════════════════════════════════════════════════════

def _gamma_analysis_inline(
    gdf: pd.DataFrame,
    spot: float,
    dte: float,
    atm_iv: float,
    atr: Optional[float],
    lambda_gamma: float = 0.1,
    magnet_threshold: float = 0.75,
    decay_threshold: float = 0.40,
) -> Dict:
    import math

    g = gdf["total_gamma_billions"].values
    x = gdf["level"].values

    # Gamma flip — use the single-source value computed by _build_gamma_profile
    # (smoothed discrete-strike flip in gdf.attrs), NOT a separate linspace
    # zero-crossing. This keeps the GEX page showing ONE consistent flip value.
    gamma_flip = gdf.attrs.get("flip_point") if hasattr(gdf, "attrs") else None
    if gamma_flip is None:
        # Fallback only if attrs missing
        idx = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
        if len(idx):
            i = idx[0]
            d = g[i + 1] - g[i]
            if d:
                gamma_flip = float(x[i] - g[i] * (x[i + 1] - x[i]) / d)

    # Gamma at spot
    ns_col = gdf["near_strike"].values
    nearest = int(np.argmin(np.abs(ns_col - spot)))
    gamma_at_spot = float(g[nearest])

    regime = "Positive Gamma" if (gamma_flip is None or spot >= gamma_flip) else "Negative Gamma"

    # Magnet zone
    max_g = float(gdf["total_gamma_billions"].max())
    magnet = None
    if max_g > 0:
        zone = gdf[gdf["total_gamma_billions"] >= magnet_threshold * max_g]
        if not zone.empty:
            magnet = {
                "lower":    float(zone["near_strike"].min()),
                "upper":    float(zone["near_strike"].max()),
                "center":   float(zone.loc[zone["total_gamma_billions"].idxmax(), "near_strike"]),
                "strength": round(max_g, 4),
            }

    # ATR / gamma-adjusted ATR
    gamma_adj_atr = None
    if atr is not None:
        scale = 1 / math.sqrt(1 + lambda_gamma * abs(gamma_at_spot))
        gamma_adj_atr = round(atr * scale, 2)

    # Upper boundary
    df_s = gdf.sort_values("near_strike").reset_index(drop=True)
    peak_idx = int(df_s["total_gamma_billions"].idxmax())
    peak_g   = float(df_s.loc[peak_idx, "total_gamma_billions"])
    window   = 3
    upper_boundary = None
    for i in range(peak_idx + window, len(df_s)):
        wg = df_s.loc[i - window:i, "total_gamma_billions"].mean()
        if wg <= (1 - decay_threshold) * peak_g:
            upper_boundary = float(df_s.loc[i, "near_strike"])
            break

    # Lower boundary
    lower_boundary = None
    if gamma_flip is not None:
        below = df_s[df_s["near_strike"] < gamma_flip].copy()
        if len(below) >= window + 1:
            below["gamma_change"] = below["total_gamma_billions"].diff()
            below["rolling_drop"] = below["gamma_change"].rolling(window).sum()
            min_idx = below["rolling_drop"].idxmin()
            if not pd.isna(min_idx):
                lower_boundary = float(below.loc[min_idx, "near_strike"])

    ga = round(gamma_adj_atr or 0)
    bullish_break = round(upper_boundary + ga, 0) if upper_boundary and ga else None
    bearish_break = round(lower_boundary - ga, 0) if lower_boundary and ga else None

    # Expected range
    exp_range = None
    if atm_iv and atm_iv > 0:
        raw_move = spot * atm_iv * np.sqrt(1 / 252)
        scale    = 1 / math.sqrt(1 + lambda_gamma * abs(gamma_at_spot))
        exp_range = round(raw_move * scale, 2)

    # Trend / behavior
    trend = "Directional / Trending"
    if regime == "Positive Gamma":
        if magnet:
            trend = "Range Bound with Upward Drift" if spot < magnet["center"] \
               else "Range Bound with Downward Drift"
        else:
            trend = "Range Bound"

    behavior = "Directional moves with volatility expansion"
    if regime == "Positive Gamma":
        if magnet and magnet["lower"] <= spot <= magnet["upper"]:
            behavior = "Pinning behavior with volatility compression"
        else:
            behavior = "Choppy mean-reverting price action"

    pin_zone = None
    if regime == "Positive Gamma" and dte is not None and dte <= 10:
        pin_zone = magnet

    # Structures
    structures: List[str] = []
    if regime == "Positive Gamma" and magnet and ga:
        center = magnet["center"]
        if abs(spot - center) <= ga:
            structures.append(f"Short Straddle @ {center:.0f}")
            structures.append(
                f"Iron Condor: sell {center-ga:.0f}P/{center+ga:.0f}C  "
                f"buy {center-2*ga:.0f}P/{center+2*ga:.0f}C"
            )
        elif spot < magnet["lower"]:
            structures.append(f"Put Credit Spread: sell {magnet['lower']:.0f}P / buy {magnet['lower']-ga:.0f}P")
        elif spot > magnet["upper"]:
            structures.append(f"Call Credit Spread: sell {magnet['upper']:.0f}C / buy {magnet['upper']+ga:.0f}C")
    elif regime == "Negative Gamma":
        if bullish_break and spot > bullish_break:
            structures.append(f"Call Ratio Spread above {bullish_break:.0f}")
        if bearish_break and spot < bearish_break:
            structures.append(f"Put Backspread below {bearish_break:.0f}")

    # Warnings
    warnings: List[str] = []
    if regime == "Positive Gamma":
        warnings.append("Avoid long straddles (volatility compression likely)")
    if gamma_flip and gamma_adj_atr and abs(spot - gamma_flip) <= gamma_adj_atr:
        warnings.append("Avoid short ATM options near gamma flip")
    if bullish_break and gamma_adj_atr and spot >= bullish_break - gamma_adj_atr:
        warnings.append("Avoid naked short calls (upside acceleration risk)")
    if bearish_break and gamma_adj_atr and spot <= bearish_break + gamma_adj_atr:
        warnings.append("Avoid naked short puts (downside acceleration risk)")
    if regime == "Negative Gamma":
        warnings.append("Avoid iron condors and range-bound strategies")

    return {
        "regime": regime,
        "gamma_flip": gamma_flip,
        "gamma_at_spot": round(gamma_at_spot, 4),
        "magnet": magnet,
        "upper_boundary": upper_boundary,
        "lower_boundary": lower_boundary,
        "bullish_break": bullish_break,
        "bearish_break": bearish_break,
        "atr": round(atr, 2) if atr else None,
        "gamma_adj_atr": gamma_adj_atr,
        "expected_range": exp_range,
        "trend": trend,
        "behavior": behavior,
        "pin_zone": pin_zone,
        "structures": structures,
        "warnings": warnings,
    }




# ═══════════════════════════════════════════════════════════════════
# Delta-bucket flow helpers
# ═══════════════════════════════════════════════════════════════════

_BUCKET_NAMES = ["DEEP_ITM", "ATM", "NEAR_OTM", "FAR_OTM", "DEEP_OTM"]

def _assign_bucket(abs_delta: float, b_atm: float, b_near: float,
                   b_far: float, b_deep: float) -> str:
    """Assign a bucket name based on absolute delta value."""
    if abs_delta >= b_atm:   return "DEEP_ITM"
    if abs_delta >= b_near:  return "ATM"
    if abs_delta >= b_far:   return "NEAR_OTM"
    if abs_delta >= b_deep:  return "FAR_OTM"
    return "DEEP_OTM"

def _dte_regime(dte: float) -> str:
    if dte <= 1:   return "expiry"
    if dte <= 7:   return "short"
    if dte <= 21:  return "medium"
    return "long"

def _flow_signal(ce_oi_chg: float, pe_oi_chg: float,
                 spot_chg: float, ce_tbq: float, pe_tbq: float) -> dict:
    """Classify the dominant flow type for a bucket."""
    net_oi = ce_oi_chg + pe_oi_chg
    # Primary OI signal
    if ce_oi_chg > 0 and spot_chg > 0:   ce_sig = "Long Build-Up"
    elif ce_oi_chg > 0 and spot_chg <= 0: ce_sig = "Short Build-Up"
    elif ce_oi_chg < 0 and spot_chg > 0:  ce_sig = "Short Covering"
    elif ce_oi_chg < 0 and spot_chg <= 0: ce_sig = "Long Unwinding"
    else: ce_sig = "Neutral"

    if pe_oi_chg > 0 and spot_chg <= 0:   pe_sig = "Long Build-Up"
    elif pe_oi_chg > 0 and spot_chg > 0:  pe_sig = "Short Build-Up"
    elif pe_oi_chg < 0 and spot_chg <= 0: pe_sig = "Short Covering"
    elif pe_oi_chg < 0 and spot_chg > 0:  pe_sig = "Long Unwinding"
    else: pe_sig = "Neutral"

    # Flow type classification
    buy_imb = (ce_tbq - pe_tbq) / (ce_tbq + pe_tbq) if (ce_tbq + pe_tbq) > 0 else 0
    if   ce_oi_chg > 0 and pe_oi_chg > 0 and buy_imb > 0.2:
        flow_type = "Speculative"
    elif pe_oi_chg > 0 and spot_chg > 0:
        flow_type = "Hedging"
    elif ce_oi_chg > 0 and pe_oi_chg < 0 and buy_imb > 0.1:
        flow_type = "Squeeze Setup"
    elif pe_oi_chg > 0 and abs(pe_oi_chg) > abs(ce_oi_chg) * 3:
        flow_type = "Panic Flow"
    elif abs(buy_imb) < 0.05 and abs(net_oi) < 100:
        flow_type = "Dealer Positioning"
    else:
        flow_type = "Mixed"

    return {"ce_signal": ce_sig, "pe_signal": pe_sig, "flow_type": flow_type,
            "buy_imbalance": round(buy_imb, 3)}




# ═══════════════════════════════════════════════════════════════════
# Smart Money clustering helpers
# ═══════════════════════════════════════════════════════════════════

from scipy.ndimage import uniform_filter1d

# Shared pure-math (single source, also used by standalone batch script).
# Robust import: try as-is, then add project root to path. If still missing,
# fail loudly with a clear message rather than breaking every route silently.
_core = None
try:
    import exposure_core as _core
except ImportError:
    import sys, pathlib
    _root = pathlib.Path(__file__).resolve().parent.parent
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
    try:
        import exposure_core as _core
    except ImportError as _e:
        raise ImportError(
            "exposure_core.py not found. It must be placed at the project root "
            f"({_root}), alongside the oc_dashboard package and oc_exposure_eod.py. "
            "Download exposure_core.py and put it there."
        ) from _e

# ── Cluster detection ──────────────────────────────────────────────────────

def _smooth(arr: np.ndarray, window: int) -> np.ndarray:
    """Simple uniform (box) smoothing, handles edges cleanly."""
    if window <= 1 or len(arr) < window:
        return arr.astype(float)
    return uniform_filter1d(arr.astype(float), size=window, mode="nearest")


def _find_clusters(
    strikes:       np.ndarray,   # sorted strike array
    prem_oi:       np.ndarray,   # premium-weighted OI per strike
    smoothing:     int   = 3,
    min_prom_pct:  float = 10.0, # % of max value for prominence threshold
    extra: dict          = None,  # extra per-strike arrays {name: array}
) -> list:
    """
    Peak detection on the premium-weighted OI curve.
    Returns list of cluster dicts sorted by center_strike.
    CE and PE are called independently — this function is side-agnostic.
    """
    if len(strikes) < 3 or prem_oi.sum() == 0:
        return []

    smoothed = _smooth(prem_oi, smoothing)
    prominence_threshold = smoothed.max() * min_prom_pct / 100.0

    peaks, props = find_peaks(
        smoothed,
        prominence=prominence_threshold,
        distance=max(1, len(strikes) // 20),   # min distance between peaks
    )
    if len(peaks) == 0:
        # Fallback: treat the global maximum as one cluster
        peaks = np.array([int(np.argmax(smoothed))])

    # Build valley boundaries between adjacent peaks.
    # CRITICAL: do NOT extend the first cluster down to index 0 or the last
    # cluster up to the final index — that sweeps in far ITM/OTM tail strikes
    # with tiny premium OI, ballooning the range and dragging the centroid.
    # Instead, bound each cluster by where its premium OI decays to a small
    # fraction of its peak value (the "skirt" of the peak).
    peak_vals  = smoothed[peaks]
    boundaries = []
    for i, pk in enumerate(peaks):
        pk_val = smoothed[pk]
        # decay threshold — strikes below this fraction of the peak are
        # not considered part of the cluster
        decay = pk_val * 0.10

        # Walk left from peak until premium decays below threshold OR we
        # reach the midpoint to the previous peak (whichever comes first)
        left_limit = 0 if i == 0 else (peaks[i-1] + pk) // 2
        lo = pk
        while lo > left_limit and smoothed[lo-1] >= decay:
            lo -= 1

        # Walk right from peak similarly
        right_limit = len(strikes)-1 if i == len(peaks)-1 else (pk + peaks[i+1]) // 2
        hi = pk
        while hi < right_limit and smoothed[hi+1] >= decay:
            hi += 1

        boundaries.append((lo, hi))

    clusters = []
    for i, (lo, hi) in enumerate(boundaries):
        member_idx   = np.arange(lo, hi+1)
        member_prem  = prem_oi[member_idx]
        total_prem   = float(member_prem.sum())
        if total_prem <= 0:
            continue
        weighted_center = float(
            (strikes[member_idx] * member_prem).sum() / total_prem
        )
        top3 = float(np.sort(member_prem)[-3:].sum())

        cluster = {
            "center_strike":      round(weighted_center, 1),
            "peak_strike":        float(strikes[peaks[i]]),
            "min_strike":         float(strikes[lo]),
            "max_strike":         float(strikes[hi]),
            "tv_weight":          round(total_prem, 2),   # time-value-weighted (clustering basis)
            "concentration_ratio":round(top3 / total_prem, 4) if total_prem > 0 else None,
            "member_count":       int(len(member_idx)),
        }
        # Weighted averages of any extra columns
        for colname, arr in (extra or {}).items():
            vals = arr[member_idx]
            cluster[colname] = round(
                float((vals * member_prem).sum() / total_prem)
                if total_prem > 0 else 0.0, 4
            )
        clusters.append(cluster)
    return clusters


def _match_clusters(prev: list, curr: list, spot: float, tol_pct: float = 2.0) -> dict:
    """
    Match clusters across two days by nearest center_strike within tolerance.
    Returns dict mapping curr cluster index → prev cluster index (or None).
    Label: TRACKED | EMERGING | DISSOLVED | SPLIT
    """
    tol = spot * tol_pct / 100.0
    mapping = {}   # curr_idx → prev_idx | None
    used_prev = set()

    for ci, cc in enumerate(curr):
        best_dist, best_pi = float("inf"), None
        for pi, pc in enumerate(prev):
            dist = abs(cc["center_strike"] - pc["center_strike"])
            if dist < tol and dist < best_dist:
                best_dist, best_pi = dist, pi
        mapping[ci] = best_pi
        if best_pi is not None:
            used_prev.add(best_pi)

    # Dissolved = prev clusters with no match in curr
    dissolved_prev = [pi for pi in range(len(prev)) if pi not in used_prev]
    return mapping, dissolved_prev


# ── EOD snapshot helper ────────────────────────────────────────────────────


def gamma_analysis_range(spot, expected_move=None, atm_iv=None, dte=None,
                         buffer_pct=0.5, max_pct=15.0):
    """
    Compute adaptive strike range for gamma flip/peak analysis.
    Uses expected move (from ATM straddle or IV×√T) + small buffer, capped.
    Both GEX page and Exposure page should use this for consistency.
    """
    if expected_move and expected_move > 0:
        em = expected_move
    elif atm_iv and atm_iv > 0 and dte and dte > 0:
        # Fallback: compute from IV
        em = spot * (atm_iv / 100.0) * math.sqrt(dte / 365.0)
    else:
        em = spot * 0.05  # last resort: 5% fixed

    buffer = spot * buffer_pct / 100.0
    half_range = em + buffer
    # Cap
    max_range = spot * max_pct / 100.0
    half_range = min(half_range, max_range)

    return spot - half_range, spot + half_range


def gamma_flip_from_strikes(strikes, net_gex_values):
    """Gamma flip (zero-crossing) → (flip_strike, nearest_real_strike).
    Thin wrapper over exposure_core.gamma_flip (single-sourced math)."""
    flip, nearest, _regime = _core.gamma_flip(strikes, net_gex_values)
    return flip, nearest


def _eod_df(symbol: str, expiry_filter: str, exp_params: list,
            date_from: str, date_to: str) -> "pd.DataFrame":
    """Fetch EOD (≤15:30) snapshot per strike per date."""
    ef = expiry_filter
    return qdf(
        f"""
        WITH eod_ts AS (
            SELECT
                CAST(timestamp AS DATE) AS dt,
                strike_price,
                MAX(timestamp)          AS eod_ts
            FROM {tbl()}
            WHERE symbol = ?
              AND CAST(timestamp AS DATE) >= CAST(? AS DATE)
              AND CAST(timestamp AS DATE) <= CAST(? AS DATE)
              AND STRFTIME(timestamp, '%H:%M') <= '15:30'
              AND expiry >= CURRENT_DATE
              {ef}
            GROUP BY dt, strike_price
        )
        SELECT
            CAST(e.dt AS VARCHAR)          AS dt,
            t.strike_price,
            CAST(t.expiry AS VARCHAR)      AS expiry,
            COALESCE(t.ce_ltp,   0)        AS ce_ltp,
            COALESCE(t.pe_ltp,   0)        AS pe_ltp,
            COALESCE(t.ce_oi,    0)        AS ce_oi,
            COALESCE(t.pe_oi,    0)        AS pe_oi,
            COALESCE(t.ce_oi_change, 0)    AS ce_oi_chg,
            COALESCE(t.pe_oi_change, 0)    AS pe_oi_chg,
            COALESCE(t.ce_volume, 0)       AS ce_vol,
            COALESCE(t.pe_volume, 0)       AS pe_vol,
            COALESCE(t.ce_iv,    0)        AS ce_iv,
            COALESCE(t.pe_iv,    0)        AS pe_iv,
            ABS(COALESCE(t.ce_delta, 0))   AS ce_adelta,
            ABS(COALESCE(t.pe_delta, 0))   AS pe_adelta,
            COALESCE(t.ce_gamma, 0)        AS ce_gamma,
            COALESCE(t.pe_gamma, 0)        AS pe_gamma,
            COALESCE(t.ce_tbq,   0)        AS ce_tbq,
            COALESCE(t.pe_tbq,   0)        AS pe_tbq,
            COALESCE(t.ce_bid_ask_spread,0) AS ce_baq,
            COALESCE(t.pe_bid_ask_spread,0) AS pe_baq,
            COALESCE(t.underlying_price,0)  AS spot,
            COALESCE(t.days_to_expiry,0)    AS dte,
            COALESCE(t.lotsize, 1)          AS lotsize,
            -- Pre-computed columns from oc_processor
            COALESCE(t.ce_prem_oi,     0)   AS ce_prem_oi,
            COALESCE(t.pe_prem_oi,     0)   AS pe_prem_oi,
            COALESCE(t.ce_prem_oi_chg, 0)   AS ce_prem_oi_chg,
            COALESCE(t.pe_prem_oi_chg, 0)   AS pe_prem_oi_chg,
            COALESCE(t.ce_time_value,  0)   AS ce_tv,
            COALESCE(t.pe_time_value,  0)   AS pe_tv,
            COALESCE(t.ce_nd2, 0)           AS ce_nd2,
            COALESCE(t.pe_nd2, 0)           AS pe_nd2,
            -- Exposure columns
            COALESCE(t.ce_gexv,  0)          AS ce_gexv,
            COALESCE(t.pe_gexv,  0)          AS pe_gexv,
            COALESCE(t.net_gexv, 0)          AS net_gexv,
            COALESCE(t.ce_vanna_ex,  0)      AS ce_vanna_ex,
            COALESCE(t.pe_vanna_ex,  0)      AS pe_vanna_ex,
            COALESCE(t.net_vanna_ex, 0)      AS net_vanna_ex,
            COALESCE(t.ce_delta_oi_chg, 0)  AS ce_delta_oi_chg,
            COALESCE(t.pe_delta_oi_chg, 0)  AS pe_delta_oi_chg,
            COALESCE(t.ce_delta_oi, 0)      AS ce_delta_oi,
            COALESCE(t.pe_delta_oi, 0)      AS pe_delta_oi,
            COALESCE(t.net_charm_ex, 0)     AS net_charm_ex
        FROM {tbl()} t
        JOIN eod_ts e
          ON  t.symbol       = ?
          AND t.strike_price = e.strike_price
          AND t.timestamp    = e.eod_ts
        WHERE 1=1 {ef}
        ORDER BY e.dt, t.strike_price
        """,
        # Param order matches ? positions:
        # 1=symbol(CTE) 2=date_from 3=date_to 4=expiry(CTE)
        # 5=symbol(JOIN) 6=expiry(outer WHERE)
        [symbol, date_from, date_to] + exp_params
        + [symbol] + exp_params,
    )


# ── Main endpoint ──────────────────────────────────────────────────────────

