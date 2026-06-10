"""
exposure_core.py
================
Pure exposure-math functions with NO FastAPI / DuckDB / app dependencies.
Imported by BOTH the FastAPI app (oc_dashboard/helpers.py) and the standalone
batch script (oc_exposure_eod.py) so the gamma/flip/range/signal logic lives in
exactly ONE place — fix bugs here, both consumers get the fix.

Only depends on numpy + math + pandas (for EWMA helper).
"""
from __future__ import annotations
import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


# ── Default parameters (batch + endpoints can override) ────────────────────
DEFAULTS = {
    "DTE_EXCLUDE":     3,
    "IV_SMOOTH_SPAN":  5,
    "STRIKE_BUFFER_K": 1.0,
    "MAX_RANGE_PCT":   15.0,
    "RANGE_CHG_CAP":   0.25,
    "BAQ_MAX_PCT":     10.0,
    "EARNINGS_WINDOW": 2,
    # signal thresholds
    "DRIFT_FRAC":          0.15,   # |velocity| >= this × expected_move
    "PIN_NARROW_FRAC":     0.90,   # transition width must drop below this × prev
    "INSTAB_DELTA":        0.05,   # neg-gamma fraction rise to flag
}


# ═══════════════════════════════════════════════════════════════════
# Range
# ═══════════════════════════════════════════════════════════════════

def strike_interval(strikes: np.ndarray) -> float:
    """Median spacing between consecutive distinct strikes."""
    if len(strikes) < 2:
        return 0.0
    diffs = np.diff(np.sort(np.unique(np.asarray(strikes, float))))
    diffs = diffs[diffs > 0]
    return float(np.median(diffs)) if len(diffs) else 0.0


def expected_move(ref_price: float, atm_iv: float, dte: float) -> float:
    """Expected move = ref × IV × sqrt(T). Falls back to 5% if inputs missing."""
    if atm_iv and atm_iv > 0 and dte and dte > 0:
        return ref_price * (atm_iv / 100.0) * math.sqrt(dte / 365.0)
    return ref_price * 0.05


def analysis_range(ref_price: float, exp_move: float, strike_int: float,
                   prev_range_half: Optional[float] = None,
                   params: Optional[Dict] = None) -> Tuple[float, float, float]:
    """
    Adaptive strike range for gamma analysis, centered on ref_price (fut_price).
    Range = ref ± (exp_move + K × strike_interval), absolute-capped and
    day-over-day change-capped. Returns (lo, hi, half_width).
    """
    p = {**DEFAULTS, **(params or {})}
    half = exp_move + p["STRIKE_BUFFER_K"] * strike_int
    max_half = ref_price * p["MAX_RANGE_PCT"] / 100.0
    half = min(half, max_half)
    if prev_range_half and prev_range_half > 0:
        cap = p["RANGE_CHG_CAP"]
        half = max(prev_range_half * (1 - cap),
                   min(half, prev_range_half * (1 + cap)))
    return ref_price - half, ref_price + half, half


# ═══════════════════════════════════════════════════════════════════
# Gamma flip / transition
# ═══════════════════════════════════════════════════════════════════

def gamma_flip(strikes, net_gex, ref_price=None,
               smooth_window: int = 5) -> Tuple[Optional[float], Optional[float], str]:
    """
    Interpolated gamma-flip (zero-crossing) strike + nearest real strike + regime.
    Regime is the sign of net gamma AT ref_price (spot/fut), not the top strike.
    smooth_window: uniform filter size to suppress noise before zero-crossing search.
    Set to 1 to disable. Default 5 — suppresses illiquid strike oscillations.
    Returns (flip_interp, flip_nearest, regime). flip None if no crossing.
    regime ∈ {positive, negative, all_positive, all_negative, no_crossing, insufficient}
    """
    from scipy.ndimage import uniform_filter1d
    g = np.asarray(net_gex, float)
    x = np.asarray(strikes, float)
    # Smooth before zero-crossing detection if window > 1
    if smooth_window and smooth_window > 1 and len(g) >= smooth_window:
        g = uniform_filter1d(g, size=smooth_window)
    if len(g) < 2:
        return None, None, "insufficient"
    sc = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
    if len(sc) == 0:
        if (g >= 0).all():
            return None, None, "all_positive"
        if (g <= 0).all():
            return None, None, "all_negative"
        return None, None, "no_crossing"
    i = sc[0]
    denom = g[i + 1] - g[i]
    flip = float(x[i]) if abs(denom) < 1e-12 else \
        float(x[i] - g[i] * (x[i + 1] - x[i]) / denom)
    nearest = float(x[int(np.argmin(np.abs(x - flip)))])
    ref = ref_price if ref_price is not None else x[int(len(x) // 2)]
    gex_at_ref = float(g[int(np.argmin(np.abs(x - ref)))])
    regime = "positive" if gex_at_ref >= 0 else "negative"
    return round(flip, 2), nearest, regime


def transition_width(strikes, net_gex) -> Optional[float]:
    """Strike distance between last +gamma and first -gamma straddling the flip."""
    g = np.asarray(net_gex, float)
    x = np.asarray(strikes, float)
    sc = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
    if len(sc) == 0:
        return None
    i = sc[0]
    return float(abs(x[i + 1] - x[i]))


def lopsidedness(net_gex) -> float:
    """Dimensionless net/gross gamma ratio in [-1, +1]. +1 all positive, -1 all negative."""
    g = np.asarray(net_gex, float)
    gross = float(np.abs(g).sum())
    return round(float(g.sum()) / gross, 4) if gross > 1e-9 else 0.0


# ═══════════════════════════════════════════════════════════════════
# Smoothing
# ═══════════════════════════════════════════════════════════════════

def ewma_last(values: List[float], span: int) -> float:
    """Last value of an EWMA over a list. Empty → 0."""
    if not values:
        return 0.0
    return float(pd.Series(values).ewm(span=span).mean().iloc[-1])


# ═══════════════════════════════════════════════════════════════════
# Signal derivation (day-over-day)
# ═══════════════════════════════════════════════════════════════════

def derive_signals(curr: Dict, prev: Optional[Dict],
                   params: Optional[Dict] = None) -> Tuple[str, str]:
    """
    Return (fired_signals_csv, active_regime). Needs prev-day dict for triggers.
    Signals: regime_flip_to_neg/pos, flip_drift_up/down, pin_strengthening,
             instability_widening, crash_risk, trend_reinforce.
    """
    p = {**DEFAULTS, **(params or {})}
    fired: List[str] = []
    active = curr.get("gex_regime") or "unknown"
    if not prev:
        return "", active

    pr = prev.get("gex_regime")
    cr = curr.get("gex_regime")
    POS = ("positive", "all_positive")
    NEG = ("negative", "all_negative")

    if pr in POS and cr in NEG:
        fired.append("regime_flip_to_neg")
    if pr in NEG and cr in POS:
        fired.append("regime_flip_to_pos")

    fv = curr.get("flip_velocity")
    em = curr.get("expected_move") or 0
    thresh = em * p["DRIFT_FRAC"] if em > 0 else None
    if fv is not None and thresh:
        if fv <= -thresh:
            fired.append("flip_drift_down")
        elif fv >= thresh:
            fired.append("flip_drift_up")

    tw_c, tw_p = curr.get("transition_width_norm"), prev.get("transition_width_norm")
    if tw_c is not None and tw_p is not None and tw_p > 0 and \
       tw_c < tw_p * p["PIN_NARROW_FRAC"] and cr in POS:
        fired.append("pin_strengthening")

    ng_c, ng_p = curr.get("neg_gamma_fraction"), prev.get("neg_gamma_fraction")
    if ng_c is not None and ng_p is not None and (ng_c - ng_p) > p["INSTAB_DELTA"]:
        fired.append("instability_widening")

    iv_chg = curr.get("iv_change") or 0
    if cr in NEG and iv_chg > 0 and (curr.get("pe_vanna") or 0) < 0:
        fired.append("crash_risk")
    if cr in POS and (curr.get("ce_vanna") or 0) > 0 and iv_chg > 0:
        fired.append("trend_reinforce")

    return ",".join(fired), active


def confidence(total_oi: float, n_strikes: int, tw_norm: Optional[float]) -> str:
    if n_strikes >= 8 and total_oi > 0 and (tw_norm or 1) < 0.5:
        return "high"
    if n_strikes >= 4:
        return "medium"
    return "low"


# Signal metadata for UI / docs (single source of truth)
SIGNAL_INFO = {
    "regime_flip_to_neg": ("Regime → Negative",
        "Gamma flipped from positive (pinning) to negative (amplifying) at spot. "
        "Vol expansion likely; favor protection / long premium."),
    "regime_flip_to_pos": ("Regime → Positive",
        "Gamma flipped to positive (pinning) regime. Favor premium selling near the flip."),
    "flip_drift_up": ("Flip Drifting Up",
        "Gamma balance point migrating higher (>15% of expected move). Resistance/pin rising."),
    "flip_drift_down": ("Flip Drifting Down",
        "Gamma balance point migrating lower. Support broadening downward."),
    "pin_strengthening": ("Pin Strengthening",
        "Transition zone narrowing under positive gamma — sharper pin. Sell premium around flip."),
    "instability_widening": ("Instability Widening",
        "Negative-gamma fraction rising — amplification zone broadening. Higher move risk."),
    "crash_risk": ("Crash Risk",
        "Negative gamma + rising IV + negative PE vanna — dealers sell into weakness "
        "(vanna feedback). Downside amplification setup."),
    "trend_reinforce": ("Trend Reinforcement",
        "Positive gamma + positive CE vanna + rising IV — trend continuation supportive."),
}
