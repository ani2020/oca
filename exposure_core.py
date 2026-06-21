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
}
