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
    "DRIFT_FRAC":          0.25,   # |velocity| >= this × expected_move (raised from 0.15)
    "DRIFT_MAX_NORM_DIST": 1.0,    # drift gate: only fire when |flip_norm_distance| < this
                                   #   (tuning range ~1.0–1.25; trends overshoot the EM)
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
    ref = ref_price if ref_price is not None else x[int(len(x) // 2)]
    # interpolated flip per crossing; pick the one NEAREST ref_price (not first)
    def _interp(i):
        d = g[i + 1] - g[i]
        return float(x[i]) if abs(d) < 1e-12 else float(x[i] - g[i] * (x[i + 1] - x[i]) / d)
    flips = [_interp(int(j)) for j in sc]
    best = int(np.argmin([abs(f - ref) for f in flips]))
    i = int(sc[best])
    flip = flips[best]
    nearest = float(x[int(np.argmin(np.abs(x - flip)))])
    gex_at_ref = float(g[int(np.argmin(np.abs(x - ref)))])
    regime = "positive" if gex_at_ref >= 0 else "negative"
    return round(flip, 2), nearest, regime


def transition_width(strikes, net_gex, ref_price=None) -> Optional[float]:
    """Strike distance between the +/- gamma strikes straddling the flip.
    Uses the crossing NEAREST ref_price (consistent with gamma_flip), not the first."""
    g = np.asarray(net_gex, float)
    x = np.asarray(strikes, float)
    sc = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
    if len(sc) == 0:
        return None
    ref = ref_price if ref_price is not None else x[int(len(x) // 2)]
    def _interp(i):
        d = g[i + 1] - g[i]
        return float(x[i]) if abs(d) < 1e-12 else float(x[i] - g[i] * (x[i + 1] - x[i]) / d)
    best = int(np.argmin([abs(_interp(int(j)) - ref) for j in sc]))
    i = int(sc[best])
    return float(abs(x[i + 1] - x[i]))


def lopsidedness(net_gex) -> float:
    """Dimensionless net/gross gamma ratio in [-1, +1]. +1 all positive, -1 all negative."""
    g = np.asarray(net_gex, float)
    gross = float(np.abs(g).sum())
    return round(float(g.sum()) / gross, 4) if gross > 1e-9 else 0.0


# ═══════════════════════════════════════════════════════════════════
# Basis (futures vs spot) — cash/futures positioning layer
# ═══════════════════════════════════════════════════════════════════

# Annualized basis beyond this (abs %) is treated as bad data (dividend/ex-date
# distortion or stale stock future), not a real signal. Indices sit well inside
# this (~5-7%); deep-negative single-stock outliers (-100%..-345% annualized seen
# in real data) are clamped to None so they don't fire false basis signals.
BASIS_ANNUAL_CLAMP = 60.0   # abs % — generous; only catches clear distortions


def basis_metrics(fut_price, spot, dte):
    """Basis between same-expiry future and spot.
    Returns (basis, basis_pct, basis_annualized) — any element None if not meaningful.

    Guards:
      - fut_price == spot  → fallback (no real future captured) → all None
      - spot <= 0          → undefined → all None
      - basis_annualized only when dte is usable (>0); clamped to None when its
        magnitude exceeds BASIS_ANNUAL_CLAMP (dividend/data distortion, esp. stocks).
    """
    if fut_price is None or spot is None or spot <= 0:
        return None, None, None
    # fallback sentinel: collector sets fut_price = spot when no matching future
    if abs(float(fut_price) - float(spot)) < 1e-9:
        return None, None, None
    basis = float(fut_price) - float(spot)
    basis_pct = basis / float(spot) * 100.0
    basis_annualized = None
    if dte is not None and dte > 0:
        ann = basis_pct * 365.0 / float(dte)
        # clamp obvious distortions (mostly single-stock dividend/data quirks)
        if abs(ann) <= BASIS_ANNUAL_CLAMP:
            basis_annualized = round(ann, 3)
    return round(basis, 4), round(basis_pct, 4), basis_annualized


# Below this |basis_pct| (≈ tick-size noise on the 15:30 LTP snapshot), treat the
# basis as flat/neutral — NOT contango/backwardation. A near-zero closing-tick
# print (e.g. the −0.03 Jun-18 BANKINDIA basis) must not render as a dramatic
# contango→backwardation flip. Interpretation-side only; does not touch stored data.
BASIS_DEADZONE_PCT = 0.1   # abs % of spot


def basis_deadzone(basis_pct, deadzone: float = BASIS_DEADZONE_PCT) -> bool:
    """True when |basis_pct| is within the dead-zone (tick-size noise → treat as
    neutral/zero). None basis_pct is treated as in-zone (no meaningful basis).
    Pure predicate — callers decide how to neutralise (suppress sign-flip emphasis,
    render as flat). Single-sourced so the screener and history view agree."""
    if basis_pct is None:
        return True
    try:
        return abs(float(basis_pct)) < deadzone
    except (TypeError, ValueError):
        return True


# ═══════════════════════════════════════════════════════════════════
# Regime colour ramp (single source — shared by screener + history)
# ═══════════════════════════════════════════════════════════════════
# Ordered stabilising → destabilising ramp. Consistent with the existing
# red = amplification / danger convention. Chosen over green/blue/red because the
# ramp is visually ORDERED (a 4-step gradient), and avoids blue clashing with the
# red=danger semantics used elsewhere. `order` lets the UI sort/interpolate; the
# words remain available as tooltips on the colour box.
REGIME_COLOR_RAMP = {
    "all_positive": {"color": "#0c8f4d", "order": 0, "label": "all positive"},   # deep green
    "positive":     {"color": "#10b981", "order": 1, "label": "positive"},        # green
    "negative":     {"color": "#f59e0b", "order": 2, "label": "negative"},        # orange
    "all_negative": {"color": "#dc2626", "order": 3, "label": "all negative"},    # deep red
}
REGIME_COLOR_FALLBACK = {"color": "#3d5270", "order": 99, "label": ""}  # muted / unknown


def regime_color(regime: Optional[str]) -> Dict[str, Any]:
    """Return {color, order, label} for a gex_regime word. Unknown → muted fallback."""
    return REGIME_COLOR_RAMP.get(regime or "", REGIME_COLOR_FALLBACK)


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
             instability_widening, crash_risk, bull_trend_reinforce,
             bear_trend_reinforce.

    NOTE: flip_velocity may be supplied here (legacy path) or computed in the
    Stage-2 SQL layer. When present on `curr`, drift is evaluated here.
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
    # Relevance gate (DRIFT ONLY): only fire when the flip is within
    # DRIFT_MAX_NORM_DIST expected-moves of spot — a distant flip's wobble is
    # noise. flip_norm_distance = (flip - fut) / expected_move (dimensionless).
    fnd = curr.get("flip_norm_distance")
    drift_relevant = (fnd is not None and abs(fnd) < p["DRIFT_MAX_NORM_DIST"])
    if fv is not None and thresh and drift_relevant:
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
    # Crash risk: negative gamma + RISING IV + negative PE vanna (disorderly,
    # dealers sell into weakness — vanna feedback loop).
    if cr in NEG and iv_chg > 0 and (curr.get("pe_vanna") or 0) < 0:
        fired.append("crash_risk")
    # Bull trend reinforce: positive gamma + positive CE vanna + RISING IV
    # (orderly upside continuation — dealers buy stock).
    if cr in POS and (curr.get("ce_vanna") or 0) > 0 and iv_chg > 0:
        fired.append("bull_trend_reinforce")
    # Bear trend reinforce: negative gamma + negative PE vanna + FALLING IV
    # (orderly downside grind / healthy pullback — vol being sold). Distinct from
    # crash_risk by IV direction (mutually exclusive: iv_chg < 0 vs > 0).
    if cr in NEG and iv_chg < 0 and (curr.get("pe_vanna") or 0) < 0:
        fired.append("bear_trend_reinforce")

    return ",".join(fired), active


def confidence(total_oi: float, n_strikes: int, tw_norm: Optional[float]) -> str:
    if n_strikes >= 8 and total_oi > 0 and (tw_norm or 1) < 0.5:
        return "high"
    if n_strikes >= 4:
        return "medium"
    return "low"


# ═══════════════════════════════════════════════════════════════════
# Structural Strength Score (history view differentiator)
# ═══════════════════════════════════════════════════════════════════
# Per-day, day-over-day descriptive heuristic. Six independent-ish axes, +1/−1
# each, range [−6, +6]. "+" = structure stabilising (regime building), "−" =
# destabilising. The CUMULATIVE running sum over the window is the headline
# "building vs deteriorating" number.
#
# DISCIPLINE — this describes the *regime*, NOT an exogenous-shock forecast.
# BANKINDIA stabilised structurally into the Jun-19 crash; a high score did not
# predict the gap. Validate against next_day_realized_move before any predictive
# claim. The six axes:
#   1. lopsided (net_gex_norm) more positive  → +1 / more negative → −1
#   2. neg_gamma_fraction shrinking            → +1 / expanding     → −1
#   3. atm_iv_smoothed falling (iv_change<0)   → +1 / rising        → −1
#   4. flip receding (|flip_norm_distance| ↑)  → +1 / approaching   → −1
#   5. regime unchanged                        → +1 / changed       → −1
#   6. transition_width_norm narrowing         → +1 / widening      → −1
#
# EXCLUDED on purpose:
#   - realized-move shrinking → forward-outcome leak (validate AGAINST it, don't
#     bake it in).
#   - basis → stock-noisy in v1; revisit once dead-zoned basis history is trusted.
#
# OVERLAP NOTE: axes 1 (lopsided) and 5 (regime unchanged) partly co-capture
# regime persistence — intentional emphasis on persistence, not 6 fully
# orthogonal axes. Axis 6 (transition_width) was added precisely because it is
# more independent of the regime/lopsided pair.

STRENGTH_EPS = 1e-9  # treat |delta| below this as "no change" (0 contribution)


def strength_axes(curr: Dict, prev: Optional[Dict]) -> Dict[str, int]:
    """Per-axis ±1/0 contributions for one day vs the previous row.
    First row of a window (prev is None) → all zeros (no day-over-day basis)."""
    ax = {"lopsided": 0, "neg_gamma": 0, "iv": 0, "flip": 0,
          "regime": 0, "trans_w": 0}
    if not prev:
        return ax

    def _f(d, k):
        v = d.get(k)
        try:
            return None if v is None else float(v)
        except (TypeError, ValueError):
            return None

    # 1. lopsided improving (more positive) / reducing (more negative)
    a, b = _f(curr, "net_gex_norm"), _f(prev, "net_gex_norm")
    if a is not None and b is not None and abs(a - b) > STRENGTH_EPS:
        ax["lopsided"] = 1 if a > b else -1

    # 2. neg_gamma_fraction shrinking (good) / expanding (bad)
    a, b = _f(curr, "neg_gamma_fraction"), _f(prev, "neg_gamma_fraction")
    if a is not None and b is not None and abs(a - b) > STRENGTH_EPS:
        ax["neg_gamma"] = 1 if a < b else -1

    # 3. IV falling (good) / rising (bad). Prefer stored iv_change; fall back to
    #    smoothed-ATM delta so the score still works on windows lacking iv_change.
    ivc = _f(curr, "iv_change")
    if ivc is None:
        a, b = _f(curr, "atm_iv_smoothed"), _f(prev, "atm_iv_smoothed")
        ivc = (a - b) if (a is not None and b is not None) else None
    if ivc is not None and abs(ivc) > STRENGTH_EPS:
        ax["iv"] = 1 if ivc < 0 else -1

    # 4. flip receding (|flip_norm_distance| increasing) / moving closer
    a, b = _f(curr, "flip_norm_distance"), _f(prev, "flip_norm_distance")
    if a is not None and b is not None and abs(abs(a) - abs(b)) > STRENGTH_EPS:
        ax["flip"] = 1 if abs(a) > abs(b) else -1

    # 5. regime unchanged (good) / changed (bad)
    rc, rp = curr.get("gex_regime"), prev.get("gex_regime")
    if rc is not None and rp is not None:
        ax["regime"] = 1 if rc == rp else -1

    # 6. transition_width narrowing (good) / widening (bad)
    a, b = _f(curr, "transition_width_norm"), _f(prev, "transition_width_norm")
    if a is not None and b is not None and abs(a - b) > STRENGTH_EPS:
        ax["trans_w"] = 1 if a < b else -1

    return ax


def strength_score(curr: Dict, prev: Optional[Dict]) -> int:
    """Net per-day structural strength score in [-6, +6] (sum of strength_axes)."""
    return int(sum(strength_axes(curr, prev).values()))


def strength_series(rows: List[Dict]) -> List[Dict]:
    """Annotate a date-ordered list of exposure_eod row-dicts with per-day score,
    its axis breakdown, and the cumulative running sum. Returns NEW dicts (does
    not mutate inputs) carrying the originals plus:
        strength_score          int  in [-6, +6]  (0 on first row)
        strength_axes           dict the per-axis ±1/0 contributions
        strength_cumulative     int  running sum from the window start
    Pure — frontend just renders. rows MUST be ascending by date."""
    out: List[Dict] = []
    prev: Optional[Dict] = None
    cum = 0
    for r in rows:
        axes = strength_axes(r, prev)
        sc = int(sum(axes.values()))
        cum += sc
        nr = dict(r)
        nr["strength_score"] = sc
        nr["strength_axes"] = axes
        nr["strength_cumulative"] = cum
        out.append(nr)
        prev = r
    return out


# ════════════════════════════════════════════════════════════════
# OI Shelf detection (OI-walls v2 — "call shelf" / "put shelf")
# ════════════════════════════════════════════════════════════════
# A single max-OI strike is a poor wall: institutional positioning sits in a
# BAND of adjacent strikes (e.g. KOTAKBANK CE OI 400/405/410 = one shelf, not a
# lone 400 wall). This detects that band so the screener shows the real ceiling /
# floor and its day-over-day migration, rather than one strike.
#
# Three pieces, learned from real-data validation across KOTAKBANK / NIFTY / ITC
# / RELIANCE / TATASTEEL (the NSE strike lattice is irregular per-symbol and NSE
# injects new strikes mid-cycle, so a naive geometric rule fragments real
# shelves):
#   1. FILTER to preferred strikes first — the behavioural round-number effect.
#      Indices: traders use multiples of 100 (the -50 strikes are illiquid
#      troughs; NIFTY -00 strikes carry ~1.8× the OI). Stocks: drop fractional
#      (.5) strikes (ITC integer strikes carry ~2.9× the OI of .5 strikes).
#      This is the index-vs-stock split, data-justified — not a hack.
#   2. GRID = the MODE of gaps between preferred strikes in a window around the
#      wall. OI-INDEPENDENT (so it doesn't shift with the threshold) and
#      injection-robust (a few off-grid mid-cycle strikes don't move the mode).
#   3. WALK out from the wall on that grid: include a strike if its OI ≥
#      FRAC × wall_OI; SKIP up to MAX_SKIP missing grid slots (NSE didn't list /
#      dead strike); STOP at a present-but-sub-threshold strike (a real OI
#      trough = a separate wall, e.g. RELIANCE 1350 vs 1400).
#
# FRAC is the key knob and is runtime-configurable (params / endpoint query
# param): low (~0.40) reveals broad shelves, high (~0.65) isolates tight walls.
# A peaky distribution (ITC) shelves at low frac and collapses to a lone wall at
# high frac — that's the control working, not a bug.
#
# Pure — takes parallel arrays, returns a dict. The endpoint supplies OI +
# day-over-day OI change + LTP change so the shelf can also carry an aggregate
# build-up signal and a center-of-mass migration vs the prior session.

# Index symbols use a 100-point preferred grid (fallback 50). Single source so
# the endpoint and any other caller agree on what counts as an index.
INDEX_SYMBOLS = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50",
    "SENSEX", "BANKEX", "SENSEX50",
}

OI_SHELF_DEFAULTS = {
    "FRAC":        0.40,  # shelf member OI must be >= FRAC × wall OI (runtime-tunable)
    "MAX_SKIP":    1,     # tolerate up to this many missing grid slots mid-shelf
    "GRID_WINDOW": 6,     # ± strikes around the wall used to infer the grid mode
    "INDEX_STEP":  100.0, # preferred index strike step
    "INDEX_STEP_FALLBACK": 50.0,  # if too few 100-strikes present
}


def _oi_buildup_signal(oi_chg: float, ltp_chg: float) -> str:
    """OI-change × PRICE-change build-up matrix (same convention as oi_walls).
    Aggregated across a shelf: pass summed oi_chg and the OI-weighted ltp_chg."""
    if oi_chg > 0 and ltp_chg > 0:  return "Long Build-Up"
    if oi_chg > 0 and ltp_chg < 0:  return "Short Build-Up"
    if oi_chg < 0 and ltp_chg > 0:  return "Short Covering"
    if oi_chg < 0 and ltp_chg < 0:  return "Long Unwinding"
    return "Neutral"


def _preferred_strike_mask(symbol: Optional[str], strikes: np.ndarray,
                           params: Dict,
                           is_index: Optional[bool] = None) -> np.ndarray:
    """Boolean mask of liquidity-preferred strikes (round-number effect).
    Indices → multiples of 100 (fallback 50). Stocks → integers (drop .5).
    Falls back to all strikes if the filter would leave too few.

    is_index: explicit override (the app passes this from config.NSE_INDICES so
    there is ONE authoritative index list). When None, fall back to membership in
    this module's INDEX_SYMBOLS set."""
    x = np.asarray(strikes, float)
    if is_index is None:
        is_index = bool(symbol) and str(symbol).upper() in INDEX_SYMBOLS
    if is_index:
        step = params["INDEX_STEP"]
        m = np.array([abs(s % step) < 1e-6 for s in x])
        if m.sum() >= 3:
            return m
        fb = params["INDEX_STEP_FALLBACK"]
        m = np.array([abs(s % fb) < 1e-6 for s in x])
        return m if m.sum() >= 3 else np.ones(len(x), dtype=bool)
    # stocks: drop fractional (.5) strikes
    m = np.array([abs(s - round(s)) < 1e-6 for s in x])
    return m if m.sum() >= 2 else np.ones(len(x), dtype=bool)


def _grid_mode(fx: np.ndarray, wall: float, window: int) -> float:
    """OI-independent grid step: the MODE of gaps between preferred strikes within
    ±window strikes of the wall. Robust to a few off-grid injected strikes."""
    from collections import Counter
    wi = int(np.argmin(np.abs(fx - wall)))
    lo = max(0, wi - window)
    hi = min(len(fx), wi + window + 1)
    gaps = np.round(np.diff(fx[lo:hi]), 3)
    gaps = gaps[gaps > 0]
    if len(gaps) == 0:
        return 0.0
    return float(Counter(gaps.tolist()).most_common(1)[0][0])


def oi_shelf(strikes, oi, oi_change=None, ltp_change=None,
             prev_com: Optional[float] = None, symbol: Optional[str] = None,
             is_index: Optional[bool] = None,
             params: Optional[Dict] = None) -> Optional[Dict]:
    """Detect the dominant OI shelf (contiguous band of high-OI preferred strikes).

    Args:
        strikes:    1-D array of strike prices (any order).
        oi:         matching open interest per strike (CE or PE — side-agnostic).
        oi_change:  optional matching day/session OI change per strike.
        ltp_change: optional matching LTP change per strike (for build-up signal).
        prev_com:   optional previous-session shelf center-of-mass, for migration.
        symbol:     ticker — drives the index-vs-stock preferred-strike filter.
        is_index:   explicit index flag (app passes from config.NSE_INDICES);
                    None → fall back to this module's INDEX_SYMBOLS set.
        params:     optional overrides (FRAC / MAX_SKIP / GRID_WINDOW / steps).

    Returns dict (or None if no usable OI):
        wall_strike   the single highest-OI PREFERRED strike (the classic "wall")
        wall_oi       OI at that strike
        lo, hi        shelf strike bounds (band incl. wall_strike)
        oi            summed OI across the shelf
        com           OI-weighted center of mass of the shelf
        n_strikes     number of strikes in the shelf
        is_shelf      True when the band spans >1 strike (else lone wall)
        grid          inferred preferred-strike grid step used for the walk
        members       the shelf strike list
        oi_change     summed OI change across the shelf (if oi_change given)
        signal        aggregate build-up signal across the shelf (if changes given)
        migration     com - prev_com (if prev_com given) — +ve = shelf moving up
    """
    p = {**OI_SHELF_DEFAULTS, **(params or {})}
    x = np.asarray(strikes, float)
    o = np.asarray(oi, float)
    if x.size == 0 or o.size == 0 or not np.isfinite(o).any() or o.max() <= 0:
        return None

    # sort by strike
    order = np.argsort(x)
    x, o = x[order], o[order]
    oc = (np.asarray(oi_change, float)[order] if oi_change is not None else None)
    lc = (np.asarray(ltp_change, float)[order] if ltp_change is not None else None)

    # 1. filter to preferred (liquid round-number) strikes
    keep = _preferred_strike_mask(symbol, x, p, is_index)
    fx, fo = x[keep], o[keep]
    foc = oc[keep] if oc is not None else None
    flc = lc[keep] if lc is not None else None
    if fx.size == 0 or fo.max() <= 0:
        return None

    # 2. OI-independent grid from the preferred-strike spacing near the wall
    wall_i = int(np.argmax(fo))
    grid = _grid_mode(fx, fx[wall_i], int(p["GRID_WINDOW"]))
    thr = fo[wall_i] * p["FRAC"]

    def _find(s):
        idx = np.where(np.abs(fx - s) < 1e-6)[0]
        return int(idx[0]) if len(idx) else -1

    # 3. walk out from the wall on the grid (skip gaps, stop at real troughs)
    members = [wall_i]
    if grid > 0:
        for direction in (-1, +1):
            s = fx[wall_i] + direction * grid
            skips = 0
            while True:
                i = _find(s)
                if i < 0:                       # no strike listed here
                    skips += 1
                    if skips > int(p["MAX_SKIP"]):
                        break
                    s += direction * grid
                    continue
                if fo[i] >= thr:                # part of the shelf
                    members.append(i)
                    skips = 0
                    s += direction * grid
                else:                           # real OI trough → separate wall
                    break
    members = sorted(set(members))

    sx = fx[members]
    so = fo[members]
    total_oi = float(so.sum())
    com = float((sx * so).sum() / total_oi) if total_oi > 0 else float(fx[wall_i])

    out: Dict[str, Any] = {
        "wall_strike": float(fx[wall_i]),
        "wall_oi":     float(fo[wall_i]),
        "lo":          float(sx.min()),
        "hi":          float(sx.max()),
        "oi":          round(total_oi, 2),
        "com":         round(com, 2),
        "n_strikes":   int(len(members)),
        "is_shelf":    bool(len(members) > 1),
        "grid":        grid,
        "members":     [float(v) for v in sx],
    }
    if foc is not None:
        shelf_oc = foc[members]
        out["oi_change"] = round(float(shelf_oc.sum()), 2)
        if flc is not None:
            shelf_lc = flc[members]
            denom = float(np.abs(so).sum())
            wlc = float((shelf_lc * so).sum() / denom) if denom > 0 else 0.0
            out["signal"] = _oi_buildup_signal(out["oi_change"], wlc)
    if prev_com is not None:
        out["migration"] = round(com - float(prev_com), 2)
    return out


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
    "bull_trend_reinforce": ("Bull Trend Reinforce",
        "Positive gamma + positive CE vanna + rising IV — orderly upside continuation "
        "(dealers buy stock to stay neutral). Trend-supportive."),
    "bear_trend_reinforce": ("Bear Trend Reinforce",
        "Negative gamma + negative PE vanna + FALLING IV — orderly downside grind / "
        "healthy pullback (vol being sold). Distinct from crash_risk: falling IV means "
        "controlled selling, not the disorderly vanna-feedback flush."),
}


# Column / metric help text for the exposure screener (directional-only).
# Single source — exposed to the frontend via the meta endpoint.
# {column_key: (label, meaning, interpret)}. No hard numeric thresholds yet
# (history thin); add quantile bands later without changing structure.
METRIC_INFO = {
    "fut_price": ("FUT",
        "Front-expiry futures price (Black-76 basis). All Greeks/flip computed on this.",
        "Primary price reference. Spot (underlying) shown in tooltip; the basis "
        "between them carries cost-of-carry + positioning."),
    "gex_regime": ("REGIME",
        "Net gamma sign at the strike nearest price (at-spot regime).",
        "Positive = dealers long gamma, hedging DAMPENS moves (vol-suppression / pin). "
        "Negative = dealers short gamma, hedging AMPLIFIES moves (vol-expansion risk)."),
    "days_in_regime": ("DAYS",
        "Consecutive sessions in the current gamma regime.",
        "Higher = established / persistent regime. Just-flipped (low) = fresh, less "
        "confirmed. Colour: green positive regime, red negative."),
    "net_gex_sign": ("AGG",
        "Sign of the aggregate (spot²-scaled) net gamma across the range.",
        "Green + = aggregate positive, red - = aggregate negative. When AGG disagrees "
        "with REGIME (local pin in a net-negative structure) the divergence is "
        "informative — local suppression sitting inside broader amplification."),
    "net_gex_norm": ("LOPSIDED",
        "Net/gross gamma ratio in [-1, +1] (lopsidedness).",
        "+1 = all positive gamma (pure vol-suppression), -1 = all negative (pure "
        "amplification), near 0 = balanced / mixed regime. Sign = which gamma dominates; "
        "magnitude = how dominant."),
    "gamma_flip": ("γ FLIP",
        "Gamma balance point — strike where net dealer gamma crosses zero.",
        "Above flip = positive-gamma (pinning) zone; below = negative (amplifying), or "
        "vice-versa per regime. The structural boundary price tends to gravitate to / "
        "react around."),
    "flip_velocity": ("FLIP Δ/d",
        "Flip migration speed in points per calendar day (vs previous session).",
        "Sign = direction of drift; larger magnitude = structure shifting faster. "
        "Calendar-normalised so weekend gaps don't inflate it."),
    "flip_norm_distance": ("FLIP DIST",
        "Flip distance from price in expected-move units = (flip - fut) / expected_move.",
        "|value| < 1 = flip within one expected move = LIVE, relevant boundary price "
        "could cross. > 1 = distant, less relevant. Sign = flip above / below price."),
    "transition_width_norm": ("TRANS W",
        "Width of the gamma-flip transition zone, normalised.",
        "Lower = sharper, well-defined flip (clean pin / abrupt regime boundary). "
        "Higher = blurry, gradual transition (weak / uncertain boundary)."),
    "neg_gamma_fraction": ("NEG γ%",
        "Fraction of in-range strikes with negative net gamma.",
        "Higher = broader amplification zone (more of the range is move-amplifying). "
        "Rising day-over-day underlies the instability_widening signal."),
    "pe_vanna": ("PE VANNA",
        "Put-side vanna exposure (IV-sensitivity of delta), tracked independently.",
        "Negative PE vanna under rising IV + negative gamma is the crash-feedback leg "
        "(dealers sell into weakness). Never netted with CE vanna."),
    "iv_change": ("IV Δ",
        "Change in smoothed ATM implied vol vs previous session.",
        "Rising IV with negative gamma = stress / crash setup; falling IV with negative "
        "gamma = orderly pullback. The IV direction discriminates crash vs bear-reinforce."),
    "basis_annualized": ("BASIS%",
        "Annualized basis = (fut - spot)/spot × 365/dte. Cost-of-carry + positioning. "
        "NULL when no future captured (fut==spot) or when distorted (dividend/data).",
        "Positive = contango (futures premium, normal/bullish carry). Negative = "
        "backwardation (futures discount — short pressure / borrow stress / event fear). "
        "Indices clean; single-stock values noisy near dividends."),
    "basis_chg": ("BASIS Δ",
        "Change in annualized basis vs previous session.",
        "Collapsing basis (large negative Δ) = carry unwinding / futures leading down — "
        "confirms downside. Expanding = building long carry. Sign-flip "
        "(contango→backwardation) is the cleanest standalone basis event."),
    "oi_turnover_ratio": ("OI TURN",
        "Session OI turnover = sum|OI change| / sum OI across in-range CE+PE.",
        "Very low = stale / quiet book (distrust the metrics). Very high = thin OI, "
        "unreliable. Mid-range = healthy active turnover. Indicator only — a data-quality "
        "lens, not wired to signals."),
    "regime_compression": ("COMPRESS",
        "Coiling state — sustained tightening (narrowing transition + flip converging "
        "+ flat/falling IV under positive gamma).",
        "A LEADING setup that MAY precede vol expansion. Self-extinguishes on release — "
        "act on it before it disappears. Badge shows consecutive coiling days."),
    "compression_release": ("REL",
        "Break day — compression ended WITH expansion (IV rising + regime flipping + "
        "transition widening).",
        "Confirms a real release into expansion (vs a quiet de-compression). The "
        "'spring just sprang' marker."),
    "confidence": ("CONF",
        "Data-quality confidence in the row's metrics (driven by strike count / "
        "in-range liquidity).",
        "Low confidence = sparse / thin profile; treat the flip, regime and widths "
        "with caution (few strikes can mislead the gamma read)."),
    "next_day_realized_move": ("NEXT MOVE%",
        "The actual next-session move % (forward outcome, where available).",
        "Used to validate whether signals predicted the subsequent move. Blank for the "
        "latest rows that have no next session yet."),
    "strength_score": ("STRENGTH",
        "Per-day structural strength score in [-6, +6] (day-over-day across 6 axes: "
        "lopsided, neg-gamma%, IV, flip distance, regime persistence, transition width).",
        "Positive = structure stabilising (regime building); negative = destabilising. "
        "The CUMULATIVE sum over the window is the headline 'building vs deteriorating' "
        "number — climbing steadily = genuine multi-day strengthening; oscillating around "
        "0 = noisy. Descriptive of the REGIME, not an exogenous-shock forecast."),
}
