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
    "INSTAB_DELTA":        0.05,   # neg-gamma fraction rise to flag (fallback only)
    "INSTAB_PCTILE":       0.90,   # instab_score at/above this fires the label
    "INSTAB_MIN_HIST":     40,     # prior same-contract changes needed for a score
    "INSTAB_WINDOW":       250,    # trailing changes the percentile is taken over
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


TRADING_DAYS_YEAR = 243.0

# ── em_1d : one-session expected move ─────────────────────────────────
# expected_move() is a 1-sigma move to EXPIRY. em_1d is the NEXT-SESSION move.
# Computed in oc/em1d.py (shared by exposure_eod and exposure_hist):
#
#   em_1d^2 = k_t * ( w_iv*IV_d^2 + w_r1*r_t^2 + w_rv5*RV5 + w_rv22*RV22 )
#
# RECALIBRATED 2026-10-04 on exposure_hist rank 0, 286k symbol-days, target the
# next-day squared return, QLIKE loss, results windows excluded, fit Jun 2020 -
# Oct 2024 / test Nov 2024 - Sep 2026. QLIKE vs the previous formula on the test
# period: -0.045 (best in every year 2021-26); mean |z| stable 0.72-0.75 a year
# where the old formula drifted 0.75 -> 0.80.
#
# The previous 0.10*IV + 0.90*HV20 weights were fitted on nine quiet months
# (2025-12 to 2026-09); over six years IV carries about half the information.
# k_t (trailing 250-session pooled mean of r^2/sigma^2, sessions before t only)
# absorbs regime drift, e.g. the post-2024 compression of option premia; it has
# ranged 0.84-1.39 on variance (0.92-1.18 on em_1d). Refit the weights yearly
# walk-forward; do not hand-tune k.
#
# em_1d is a VARIANCE-MATCHED 1-sigma. Returns are fat-tailed, so mean |z| sits
# near 0.72, not the normal 0.798, and normal band multipliers are wrong. Use
# EM1D_BAND_MULT (empirical |z| quantiles, stable 2021-26 and on the test set).
EM1D_HAR_W = {"iv2": 0.508, "r1": 0.1045, "rv5": 0.0458, "rv22": 0.1919}
EM1D_SCALE_WINDOW = 250      # sessions in the rolling calibration scale
EM1D_SCALE_MIN_DAYS = 125    # below this many prior sessions, no scale (em_1d NULL)
EM1D_MIN_OBS = 20            # returns needed in the 22-session window
EM1D_BAND_MULT = {0.683: 0.86, 0.954: 2.0, 0.99: 3.0}

# LEGACY blend, kept only so old scripts (backfill_em1d.py, em_calibration.py)
# still import. Not used by the pipelines since 2026-10-04.
EM1D_W_IV = 0.10
EM1D_W_HV = 0.90

# HV20 construction. Since 2026-10-04 hv20 and em_1d use CA-ADJUSTED returns
# (bhavcopy cash_returns_adj, built by nse_ca.py apply), including the current
# session. The old defence against unadjusted ex-dates -- dropping |r| > 0.15 --
# removed 8,502 genuine moves for every 507 corporate-action artefacts (2020-26)
# and biased HV down exactly where it matters. HV_MAX_ABS_RET is retained for
# the legacy liquidity-band helpers below; the em_1d path no longer clips.
HV_WINDOW_DAYS   = 20      # trailing sessions INCLUDING the current one
HV_MAX_ABS_RET   = 0.15    # drop |log return| above this before estimating
HV_MIN_OBS       = 20      # below this, hv20 is NULL rather than a thin guess

# ── IV Rank / Percentile ────────────────────────────────────────────
# Computed ONCE by the nightly into exposure_eod, then read by the dashboard.
# It used to be derived per-request inside routes/iv.py; putting a second copy
# in the screener would have been a third. One calculation, many readers.
#
# Ranking raw front-month ATM IV makes the number a function of the roll cycle:
# mean IV Rank ran 16.8 at DTE 0-4 against 47.5 at DTE 15-21 — a 31pt swing with
# no change in vol regime, so every name in expiry week looked like cheap vol.
# (A linear CORR test misses this entirely, returning ~0.03, because the shape
# is a hump not a line. Check bucketed means, never correlation.)
#
# Interpolating rank-0 and rank-1 to a fixed 30-day point in TOTAL VARIANCE —
# what India VIX does — cuts the spread to 9.8pt and lifts expiry week to 26.3.
# Where rank-1 is missing (~45% of symbol-days) fall back to ranking rank-0
# inside a DTE band, which gets the spread to 12.7pt.
#
# The two series are ranked SEPARATELY and never pooled: a blended history took
# its minimum from a `band` row while `cm30` bottomed higher, moving NIFTY's
# IV Rank from 13.7 to 17.4 for no reason but the mixing.
IVRANK_CM_TARGET_DTE = 30          # constant-maturity point, calendar days
IVRANK_DTE_BAND      = (15, 35)    # fallback band when rank-1 is unavailable
IVRANK_MIN_SAMPLES   = 20          # below this, rank/pctile are NULL
IVRANK_LOOKBACK_DAYS = 90          # trailing window

# ── Empirical band-hold probability ───────────────────────────────────
# P(the underlying finishes INSIDE a +/- z*sigma band), measured rather than
# assumed. Use this, never NORM.DIST - the Gaussian is badly optimistic exactly
# where premium sellers operate:
#
#       z      empirical    normal    breaches vs normal
#      1.0        67.6%      68.3%       1.0x
#      1.5        82.7%      86.6%       1.3x
#      2.0        90.6%      95.4%       2.0x
#      2.5        94.5%      98.8%       4.6x
#      3.0        96.6%      99.7%      11.0x
#
# Two things the measurement settled:
#
# 1. HORIZON DOES NOT MATTER. 5/10/20/30-session horizons gave hold rates within
#    ~2pp of each other at every z, so the distribution is scale-invariant in z
#    and the table needs no horizon axis.
#
# 2. TRAILING VOL LEVEL MATTERS A LOT. At the same nominal z=2.0 the quietest
#    HV quintile held 83.5% against 96.3% for the most volatile - a 4.5x
#    difference in breach rate. That is mean reversion: a quiet name's hv20
#    understates forward vol (the quietest quintile was followed by 1.47x its
#    trailing level), so its z-bands are too narrow. Those are also the names
#    that screen best on band width, so ignoring this inverts the ranking.
#
# Source: 725,412 observations, bhavcopy cash_eod EQ closes 2025-11 to 2026-09,
# 5- and 20-session horizons pooled, corporate-action returns excluded per
# HV_MAX_ABS_RET. Bands are on trailing hv20 (annualised %).
# REFRESH after materially more history; these are not analytic constants.
HOLD_HV_BANDS = (20.0, 30.0, 40.0, 52.0)     # hv20 cut points -> 5 bands
HOLD_Z_GRID   = (1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5, 3.0)
HOLD_PROB_LUT = {
    1: (59.6, 68.1, 74.6, 79.5, 83.2, 86.1, 88.5, 91.9),   # hv < 20
    2: (61.7, 71.0, 78.5, 84.1, 88.2, 91.1, 93.3, 96.1),   # 20-30
    3: (66.8, 76.1, 83.0, 87.9, 91.4, 93.8, 95.5, 97.5),   # 30-40
    4: (72.2, 80.9, 87.0, 91.1, 93.8, 95.7, 96.9, 98.3),   # 40-52
    5: (79.5, 87.0, 91.7, 94.6, 96.3, 97.4, 98.2, 99.0),   # hv >= 52
}


def hv_band(hv20: float) -> int:
    """1..5 bucket for a trailing hv20, matching HOLD_PROB_LUT keys."""
    if hv20 is None:
        return 3
    for i, cut in enumerate(HOLD_HV_BANDS, start=1):
        if hv20 < cut:
            return i
    return len(HOLD_HV_BANDS) + 1


def hold_prob(z: float, hv20: float):
    """Empirical P(finish inside +/- z sigma), linearly interpolated on z.

    Returns None when z is unusable. Clamped at the grid edges: below the first
    z the table says nothing useful, and above the last extrapolation would be
    exactly the optimism this table exists to avoid.
    """
    if z is None or z != z or z <= 0:
        return None
    row = HOLD_PROB_LUT[hv_band(hv20)]
    g = HOLD_Z_GRID
    if z <= g[0]:
        return round(row[0] * z / g[0], 1)      # straight line to (0, 0)
    if z >= g[-1]:
        return row[-1]
    for i in range(len(g) - 1):
        if g[i] <= z <= g[i + 1]:
            f = (z - g[i]) / (g[i + 1] - g[i])
            return round(row[i] + f * (row[i + 1] - row[i]), 1)
    return None
# NSE trading days per year. Measured, not assumed: 116 distinct trading days were
# observed between 2026-01-23 and 2026-07-16 (174 calendar days), which annualises
# to 243.3. Independent cross-check: 365 - 104 weekend days = 261 weekdays, less
# the ~20 NSE public holidays in that window (Republic Day, Holi, Good Friday,
# Ambedkar Jayanti, Maharashtra Day, ...) = ~241.
#
# Do NOT use 252 — that is the NYSE figure and NSE closes ~11 more days a year.
# Do NOT tune this below ~241 to close the residual gap to theory. Validated over
# 12,018 rows / 131 symbols / 6 complete expiry cycles, mean |realised move| / EM:
#
#     divisor 365 (old): 0.991    252: 0.824    243: 0.809    232: 0.790
#     theory (E|X|/sigma for a calibrated 1-sigma band) = sqrt(2/pi) = 0.798
#
# 232 scores marginally closer, but only by absorbing the vol risk premium into a
# calendar constant. That premium is not stable: the per-cycle implied best divisor
# ranged 357-683 across the same six cycles, so a 10-day divisor change (2.1% of
# theory) is ~15x smaller than the cycle-to-cycle noise (31% of theory). 243 is a
# calendar measurement and stays true; the residual belongs in an explicit
# premium adjustment if it is ever worth modelling.
#
# IMPORTANT — this constant is NOT a global convention switch. Two other places
# use 365 and must stay that way:
#   * oc_processor._resolve_iv / greeks solve IV at t_years = dte/365. atm_iv is
#     therefore a "365-convention" number, and the 243 figure above was calibrated
#     against those exact IVs. Re-basing the IV solve as well would double-count
#     the correction and invalidate the validation.
#   * basis_annualized scales by 365/dte because cost of carry accrues on calendar
#     days — money charges interest over weekends and holidays.


def expected_move(ref_price: float, atm_iv: float, dte: float) -> float:
    """Expected 1-sigma move from now to expiry. Falls back to 5% if inputs missing.

    T is measured in TRADING days, not calendar days: option vol is quoted on a
    trading-day basis because price only diffuses when the market is open. The
    previous dte/365 scaling therefore under-stated every move by sqrt(365/243)
    = 1.23x, which showed up as only 60.2% of realised moves landing inside +/-1 EM
    where a calibrated 1-sigma band should contain 68.3%. Under dte/243 that
    coverage is 68.5%.

    NOTE: dte is a CALENDAR day count, so dte/TRADING_DAYS_YEAR slightly overstates
    elapsed trading time (a 30-calendar-day window holds ~21 trading days, not
    30 x 243/365 = 20.0 ... close, but not exact). A true trading-day countdown
    needs an NSE holiday calendar; the approximation is empirically well within
    the noise and errs marginally wide, which is the safer direction for a range.

    This is an EXPIRY-HORIZON quantity and scales with sqrt(dte). Do not use it to
    forecast a single session — the ratio (1-day move / expected_move) is then
    ~1/sqrt(dte), so any model built on it measures days-to-expiry rather than
    market structure. Use expected_move_1d() for next-day work.
    """
    if atm_iv and atm_iv > 0 and dte and dte > 0:
        return ref_price * (atm_iv / 100.0) * math.sqrt(dte / TRADING_DAYS_YEAR)
    return ref_price * 0.05


def expected_move_1d(ref_price: float, atm_iv: float) -> float:
    """IV-only expected 1-sigma move over ONE trading session.

    NOT the stored em_1d: that is the recalibrated IV + realised-variance model in
    oc/em1d.py, which needs the day's return history. This IV-only version is for
    live/intraday callers that only have the chain (helpers gamma analysis).

    Deliberately separate from expected_move(): a next-day forecast must not carry
    the sqrt(dte) horizon factor. Comparing a one-session realised move against an
    expiry-horizon EM is what produced the spurious 'hold rate' gradient (62.6% at
    low EM rising to 91.4% at high EM) — that gradient was almost entirely
    1/sqrt(dte), since the EM quartiles sorted on dte (9.3 -> 24.5 days), not on
    market structure. Against a true 1-day horizon the same rate is flat (~54-64%)
    across every dte bucket, which is what makes it usable as a baseline.

    The 5% fallback mirrors expected_move()'s, de-annualised to one session.
    """
    if atm_iv and atm_iv > 0:
        return ref_price * (atm_iv / 100.0) * math.sqrt(1.0 / TRADING_DAYS_YEAR)
    return ref_price * 0.05 / math.sqrt(TRADING_DAYS_YEAR)


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

NEUTRAL_BAND_FRAC = 0.05   # |net gamma at ref| within this fraction of GROSS gamma → "mixed"
                           # (provisional; tune from post-backfill distribution)


def gamma_flip(strikes, net_gex, ref_price=None,
               smooth_window: int = 1,
               neutral_band_frac: float = NEUTRAL_BAND_FRAC
               ) -> Tuple[Optional[float], Optional[float], str]:
    """
    Gamma-flip (zero-crossing) strike + nearest real strike + regime.

    v2 (simpler, externally verifiable — spec §1.1, corrected):
      - FLIP = per-strike net-gamma sign-change NEAREST ref_price (interpolated).
        This is where LOCAL dealer gamma flips sign — matches the GEX screen.
        (Cumulative-crossing was tried and rejected: it returned the wrong strike
        on near-zero multi-crossing chains, e.g. DMART 4200 vs the real ~4325.)
      - REGIME = sign of LOCAL net gamma interpolated AT ref_price, with a mixed
        band: |gamma_at_ref| <= neutral_band_frac × gross gamma → "mixed".
        Local-at-price (not aggregate, not cumulative) is the dealer regime price
        actually sits in — robust on spiky near-zero curves (the DMART artifact).
      - smooth_window default 1 (OFF) for verifiability. >1 applies a uniform
        filter before detection (legacy; not used by the batch).
      - Extremes (all_positive / all_negative) ONLY for genuinely monotone-sign
        curves (no per-strike sign change anywhere). Near-zero multi-crossing is
        never rounded to an extreme.
      - Flip strike emitted whenever a crossing exists (incl. mixed regime).

    Returns (flip_interp, flip_nearest, regime). flip None only if no crossing.
    regime ∈ {positive, negative, mixed, all_positive, all_negative,
              no_crossing, insufficient}
    """
    g = np.asarray(net_gex, float)
    x = np.asarray(strikes, float)
    # Optional legacy smoothing (default OFF)
    if smooth_window and smooth_window > 1 and len(g) >= smooth_window:
        from scipy.ndimage import uniform_filter1d
        g = uniform_filter1d(g, size=smooth_window)
    if len(g) < 2:
        return None, None, "insufficient"

    ref = ref_price if ref_price is not None else x[int(len(x) // 2)]
    order = np.argsort(x)
    xs, gs = x[order], g[order]

    # An all-zero profile has no gamma structure at all. It used to fall through
    # to `pos_all = (gs >= 0).all() == True` and get labelled "all_positive" —
    # i.e. the strongest possible pinning claim, made on a chain with no OI.
    if not np.any(np.abs(gs) > 0):
        return None, None, "no_gamma"

    # Per-strike sign-change crossings (LOCAL gamma flips sign).
    # STRICT PRODUCT, not a comparison of np.sign(): np.sign(0.0) is 0.0, so
    # `sign(a) != sign(b)` fires whenever a strike has EXACTLY zero net gamma
    # next to a non-zero one — even when both neighbours share a sign. Zero-GEX
    # strikes are not rare (37.7% of ocdata rows in Sep-2026), so a genuinely
    # all-positive chain with one dead strike reported a spurious flip AT that
    # strike and was denied its "all_positive" label. gs[i]*gs[i+1] < 0 is true
    # only for a real sign change.
    sc = np.where(gs[:-1] * gs[1:] < 0)[0]

    def _interp(i):
        d = gs[i + 1] - gs[i]
        return float(xs[i]) if abs(d) < 1e-12 else \
            float(xs[i] - gs[i] * (xs[i + 1] - xs[i]) / d)

    flip = nearest = None
    if len(sc) > 0:
        flips = [_interp(int(j)) for j in sc]
        best = int(np.argmin([abs(f - ref) for f in flips]))
        flip = round(flips[best], 2)
        nearest = float(xs[int(np.argmin(np.abs(xs - flip)))])

    # Regime from LOCAL net gamma interpolated at ref, mixed band scaled by gross
    gamma_at_ref = float(np.interp(ref, xs, gs))
    gross = float(np.abs(gs).sum())
    band = gross * neutral_band_frac
    pos_all = bool((gs >= 0).all())
    neg_all = bool((gs <= 0).all())

    if pos_all and len(sc) == 0:
        regime = "all_positive"
    elif neg_all and len(sc) == 0:
        regime = "all_negative"
    elif abs(gamma_at_ref) <= band:
        regime = "mixed"
    elif gamma_at_ref > 0:
        regime = "positive"
    else:
        regime = "negative"

    return flip, nearest, regime


def transition_width(strikes, net_gex, ref_price=None) -> Optional[float]:
    """Strike distance between the +/- gamma strikes straddling the flip.
    Uses the crossing NEAREST ref_price (consistent with gamma_flip), not the first."""
    g = np.asarray(net_gex, float)
    x = np.asarray(strikes, float)
    # Strict product — see the note in gamma_flip(). np.sign() treats an exactly
    # zero strike as its own sign and manufactures crossings around dead strikes.
    sc = np.where(g[:-1] * g[1:] < 0)[0]
    if len(sc) == 0:
        return None
    ref = ref_price if ref_price is not None else x[int(len(x) // 2)]
    def _interp(i):
        d = g[i + 1] - g[i]
        return float(x[i]) if abs(d) < 1e-12 else float(x[i] - g[i] * (x[i + 1] - x[i]) / d)
    best = int(np.argmin([abs(_interp(int(j)) - ref) for j in sc]))
    i = int(sc[best])
    return float(abs(x[i + 1] - x[i]))


DEGENERATE_WIDTH_NORM = 1.0
# transition_width_norm above this is arithmetically undefined, not "very wide".
# transition_width is floored at one strike_interval (it IS the gap between the
# two strikes straddling the flip), so once strike_interval > expected_move the
# ratio exceeds 1.0 purely because the grid is coarser than the move it is meant
# to resolve — no dealer behaviour is being measured. Observed on SAIL
# (interval 7.5 / EM 7.11 = 1.05) and YESBANK (1.0 / 0.98 = 1.02).


def width_norm(tw: Optional[float], exp_move: float) -> Optional[float]:
    """transition_width normalised by expected move, or None when undefined.

    Returns None — not a large number — in the degenerate regime, because the
    tight/moderate/wide bands downstream are only meaningful while the strike
    grid is finer than the expected move. Emitting 1.05 there would invite a
    "very wide zone" reading of what is really "grid too coarse to tell".

    Caveat for any caller: even in the valid range this ratio correlates 0.83
    with strike_interval/expected_move across the backfill, so it is largely a
    grid-density (liquidity-tier) proxy rather than a measure of dealer zone
    geometry. Control for the stored grid_density column before drawing
    behavioural conclusions from it.
    """
    if not tw or not exp_move or exp_move <= 0:
        return None
    wn = tw / exp_move
    if wn > DEGENERATE_WIDTH_NORM:
        return None
    return round(wn, 4)


# ═══════════════════════════════════════════════════════════════════
# Dealer gamma profile — zero-gamma flip, regime, gamma position, dealer vanna
# ═══════════════════════════════════════════════════════════════════
#
# ONE function, shared by oc_exposure_eod and build_exposure_history, so the
# flip and the regime label mean the same thing everywhere (agreed 2026-10-03).
#
#   Gamma(S) = lot * sum_k [ OI_ce,k * g(S,K,sig_ce,k) - OI_pe,k * g(S,K,sig_pe,k) ]
#
# Dealer convention: long calls, short puts (same sign as net_gex). Black-76
# gamma on the future, T = DTE/365 calendar (matches ocdata ce_gamma: median
# ratio 0.996 over 22k strikes), IV held per strike as S moves (sticky strike;
# sticky-moneyness moved the flip a median 0.08 EM only). The S^2 dollar scaling
# is a positive factor at each S and cannot move a zero, so it is omitted.
#
# Why not the per-strike flip (gamma_flip above): the sign change of per-strike
# net GEX nearest futures is mostly the ATM strike where call OI overtakes put
# OI (median -0.05 EM from price), i.e. an OI-layout fact, not a price at which
# dealer hedging changes sign. It can also contradict the regime label (e.g.
# ASHOKLEY 2026-10-01: regime negative, per-strike flip ABOVE price).
#
# Why the FULL quality-filtered chain, not the +-1.1 EM analysis window: OI walls
# just outside the window still carry real gamma at spot. Window vs full chain
# moved the flip with corr 0.89; +-2 EM vs full chain was 0.99.
#
# Prototype over 22,822 rank-0 chains (Jan 23 - Oct 1 2026): exactly one
# significant zero on 98%, 1.9 ms/chain. Next-day |move|/em_1d, pooled two-way:
# profile flip proximity t 3.7 (per-strike flip t 2.6, loses when both are in);
# gamma_position t -3.5.

PROFILE_SPAN_EM    = 3.0     # grid covers fut +- this many expected moves
PROFILE_STEP_FRAC  = 0.001   # grid step as a fraction of fut (0.1%)
PROFILE_MAX_PTS    = 800     # cap on grid points (very wide EMs)
PROFILE_MIN_PTS    = 50
PROFILE_MIXED_BAND = 0.05    # |Gamma| < band * max|Gamma| = dead band / "mixed"
PROFILE_MIN_DTE    = 0.5     # floor on DTE (days) so T never hits 0 on expiry day
VANNA_ATM_BAND_EM  = 0.25    # |K - fut| <= this * EM -> ATM vanna bucket
_SQRT_2PI = math.sqrt(2.0 * math.pi)


def _b76_d1(S: np.ndarray, K: np.ndarray, sig: np.ndarray, T: float) -> np.ndarray:
    """d1 on an (m, n) grid: S (m,) prices x K/sig (n,) strikes."""
    vs = sig[None, :] * math.sqrt(T)
    return (np.log(S[:, None] / K[None, :]) + 0.5 * vs * vs) / vs


def _b76_gamma_grid(S: np.ndarray, K: np.ndarray, sig: np.ndarray, T: float) -> np.ndarray:
    d1 = _b76_d1(S, K, sig, T)
    return np.exp(-0.5 * d1 * d1) / (_SQRT_2PI * S[:, None] * sig[None, :] * math.sqrt(T))


def _b76_vanna(F: float, K: np.ndarray, sig: np.ndarray, T: float) -> np.ndarray:
    """dDelta/dSigma per 1 vol POINT (0.01). Same for call and put.
    Positive for K above ~F, negative below."""
    d1 = _b76_d1(np.array([F]), K, sig, T)[0]
    d2 = d1 - sig * math.sqrt(T)
    pdf = np.exp(-0.5 * d1 * d1) / _SQRT_2PI
    return -pdf * d2 / sig * 0.01


def _profile_zeros(S: np.ndarray, G: np.ndarray, eps: float) -> List[float]:
    """Zeros of G between significant regions of opposite sign.

    Values with |G| < eps form a dead band; a crossing counts only when G moves
    from one significant sign to the other, and is placed at the actual zero
    inside that transition nearest its midpoint (linear interpolation)."""
    sig = np.where(G > eps, 1, np.where(G < -eps, -1, 0))
    idx = np.flatnonzero(sig != 0)
    zeros: List[float] = []
    for a, b in zip(idx[:-1], idx[1:]):
        if sig[a] == sig[b]:
            continue
        seg = np.arange(a, b + 1)
        zc = seg[:-1][np.sign(G[seg[:-1]]) != np.sign(G[seg[1:]])]
        if len(zc) == 0:
            continue
        mid = 0.5 * (S[a] + S[b])
        j = int(zc[np.argmin(np.abs(S[zc] - mid))])
        zeros.append(float(S[j] - G[j] * (S[j + 1] - S[j]) / (G[j + 1] - G[j])))
    return zeros


def dealer_gamma_profile(strikes, ce_oi, pe_oi, ce_iv, pe_iv, lot: float,
                         fut: float, dte: float, exp_move: float,
                         span_em: float = PROFILE_SPAN_EM,
                         band: float = PROFILE_MIXED_BAND,
                         return_curve: bool = False) -> Dict[str, Any]:
    """Zero-gamma flip, regime, gamma position and dealer vanna for one chain.

    Inputs are the quality-filtered FULL chain (not the analysis window). IVs in
    percent; a leg with no IV borrows the other leg's IV at the same strike;
    strikes with no IV on either leg are dropped.

    Returns (None-valued keys when the chain is unusable):
      profile_flip      zero of Gamma(S) nearest fut within +-span_em EM
      profile_fnd       (profile_flip - fut) / exp_move
      profile_regime    sign of Gamma(fut): positive / negative / mixed (|.| in
                        dead band); all_positive / all_negative when Gamma never
                        changes sign significantly on the grid. Always the
                        sign on price's side of the flip. Typically positive
                        <=> flip below price (99.99% of rank-0 rows), but not
                        always: with 2+ zeros price can sit in a pocket
                        bounded on both sides, and heavy ITM put OI above
                        price (seen on next-month chains) inverts the shape.
      gamma_position    Gamma(fut) / max|Gamma| on the grid, in [-1, 1]
      profile_n_zeros   significant zeros on the grid (1 on ~98% of chains)
      dealer_vanna      sum of dealer-signed vanna, shares per vol point
      dealer_vanna_put_wing / _atm / _call_wing
                        the same split by strike: K < fut - 0.25 EM / within /
                        above. Kept separate because the dealer sign per wing is
                        an assumption and IV shocks are not parallel.
      gross_vanna       sum of |vanna| x OI x lot, both legs
      dealer_vanna_norm dealer_vanna / gross_vanna, in [-1, 1]

    return_curve=True adds "_curve": (S, Gamma) numpy arrays for charting (the
    live GEX page draws the true profile from it). Off by default so the batch
    pipelines, which insert dict keys by column name, never see it.
    """
    keys = ("profile_flip", "profile_fnd", "profile_regime", "gamma_position",
            "profile_n_zeros", "dealer_vanna", "dealer_vanna_put_wing",
            "dealer_vanna_atm", "dealer_vanna_call_wing", "gross_vanna",
            "dealer_vanna_norm")
    out: Dict[str, Any] = {k: None for k in keys}
    K = np.asarray(strikes, dtype=float)
    oc = np.nan_to_num(np.asarray(ce_oi, dtype=float))
    op = np.nan_to_num(np.asarray(pe_oi, dtype=float))
    ivc = np.nan_to_num(np.asarray(ce_iv, dtype=float))
    ivp = np.nan_to_num(np.asarray(pe_iv, dtype=float))
    if not (fut and fut > 0 and exp_move and exp_move > 0) or len(K) < 2:
        return out
    sc = np.where(ivc > 0, ivc, ivp) / 100.0
    sp = np.where(ivp > 0, ivp, ivc) / 100.0
    ok = (sc > 0) & (sp > 0) & (K > 0) & ((oc > 0) | (op > 0))
    if ok.sum() < 2:
        return out
    K, oc, op, sc, sp = K[ok], oc[ok], op[ok], sc[ok], sp[ok]
    T = max(float(dte), PROFILE_MIN_DTE) / 365.0
    lot = float(lot) if lot and lot > 0 else 1.0

    # Gamma profile on a price grid
    half = max(span_em * exp_move, 0.02 * fut)
    half = min(half, 0.95 * fut)                       # keep S > 0
    n = int(min(PROFILE_MAX_PTS, max(PROFILE_MIN_PTS,
                                     round(2 * half / (PROFILE_STEP_FRAC * fut)))))
    S = np.linspace(fut - half, fut + half, n + 1)
    G = lot * ((_b76_gamma_grid(S, K, sc, T) * oc).sum(axis=1)
               - (_b76_gamma_grid(S, K, sp, T) * op).sum(axis=1))
    gmax = float(np.abs(G).max())
    if return_curve:
        out["_curve"] = (S, G)
    if np.isfinite(gmax) and gmax > 0:
        eps = band * gmax
        g_at = float(np.interp(fut, S, G))
        zeros = _profile_zeros(S, G, eps)
        out["gamma_position"] = round(g_at / gmax, 4)
        out["profile_n_zeros"] = len(zeros)
        if zeros:
            z = np.array(zeros)
            flip = float(z[np.argmin(np.abs(z - fut))])
            out["profile_flip"] = round(flip, 2)
            out["profile_fnd"] = round((flip - fut) / exp_move, 4)
            out["profile_regime"] = ("mixed" if abs(g_at) < eps
                                     else "positive" if g_at > 0 else "negative")
        else:
            out["profile_regime"] = ("mixed" if abs(g_at) < eps
                                     else "all_positive" if g_at > 0 else "all_negative")

    # Dealer vanna at fut (calls +, puts -), split by strike bucket
    vc = _b76_vanna(fut, K, sc, T) * oc * lot
    vp = _b76_vanna(fut, K, sp, T) * op * lot
    dv = vc - vp
    lo_b, hi_b = fut - VANNA_ATM_BAND_EM * exp_move, fut + VANNA_ATM_BAND_EM * exp_move
    gross = float(np.abs(vc).sum() + np.abs(vp).sum())
    out["dealer_vanna"] = round(float(dv.sum()), 2)
    out["dealer_vanna_put_wing"] = round(float(dv[K < lo_b].sum()), 2)
    out["dealer_vanna_atm"] = round(float(dv[(K >= lo_b) & (K <= hi_b)].sum()), 2)
    out["dealer_vanna_call_wing"] = round(float(dv[K > hi_b].sum()), 2)
    out["gross_vanna"] = round(gross, 2)
    out["dealer_vanna_norm"] = round(float(dv.sum()) / gross, 4) if gross > 0 else None
    return out


# Strike-quality rule shared by every caller of dealer_gamma_profile (batch eod,
# live GEX page, live exposure page) so they all see the same chain. Keep a strike
# if EITHER leg traded today OR either leg's bid-ask spread is within
# STRIKE_BAQ_MAX_PCT of its premium. Post-close (15:40) quotes widen or vanish, so
# the spread test alone dropped ~5% of symbol-days; a traded leg has a real
# LTP-based IV. (exposure_hist uses bhavcopy volume > 0, the same idea.)
STRIKE_BAQ_MAX_PCT = 10.0


def strike_quality_mask(ce_vol, pe_vol, ce_baq_pct, pe_baq_pct,
                        baq_max: float = STRIKE_BAQ_MAX_PCT) -> np.ndarray:
    cv = np.nan_to_num(np.asarray(ce_vol, dtype=float))
    pv = np.nan_to_num(np.asarray(pe_vol, dtype=float))
    cb = np.nan_to_num(np.asarray(ce_baq_pct, dtype=float), nan=np.inf)
    pb = np.nan_to_num(np.asarray(pe_baq_pct, dtype=float), nan=np.inf)
    return (cv > 0) | (pv > 0) | (cb <= baq_max) | (pb <= baq_max)


def profile_from_curves(curves, fut: float, exp_move: float,
                        band: float = PROFILE_MIXED_BAND) -> Dict[str, Any]:
    """Flip / regime / gamma position for SEVERAL expiries together.

    curves: list of (S, Gamma) from dealer_gamma_profile(..., return_curve=True),
    one per expiry, each on its own grid. They are interpolated onto one common
    grid and summed (each expiry already carries its own T and IVs), then the
    same zero/regime logic as the single-chain case is applied. Used by the live
    exposure page in all-expiry mode.
    """
    out: Dict[str, Any] = {"profile_flip": None, "profile_fnd": None,
                           "profile_regime": None, "gamma_position": None,
                           "profile_n_zeros": None, "_curve": None}
    curves = [(np.asarray(s, float), np.asarray(g, float)) for s, g in curves
              if s is not None and len(s) > 1]
    if not curves or not (fut and fut > 0):
        return out
    lo = max(c[0][0] for c in curves)
    hi = min(c[0][-1] for c in curves)
    if not hi > lo:
        return out
    n = int(min(PROFILE_MAX_PTS, max(PROFILE_MIN_PTS,
                                     round((hi - lo) / (PROFILE_STEP_FRAC * fut)))))
    S = np.linspace(lo, hi, n + 1)
    G = np.sum([np.interp(S, s, g) for s, g in curves], axis=0)
    out["_curve"] = (S, G)
    gmax = float(np.abs(G).max())
    if not (np.isfinite(gmax) and gmax > 0):
        return out
    eps = band * gmax
    g_at = float(np.interp(fut, S, G))
    zeros = _profile_zeros(S, G, eps)
    out["gamma_position"] = round(g_at / gmax, 4)
    out["profile_n_zeros"] = len(zeros)
    if zeros:
        z = np.array(zeros)
        flip = float(z[np.argmin(np.abs(z - fut))])
        out["profile_flip"] = round(flip, 2)
        out["profile_fnd"] = round((flip - fut) / exp_move, 4) if exp_move and exp_move > 0 else None
        out["profile_regime"] = ("mixed" if abs(g_at) < eps
                                 else "positive" if g_at > 0 else "negative")
    else:
        out["profile_regime"] = ("mixed" if abs(g_at) < eps
                                 else "all_positive" if g_at > 0 else "all_negative")
    return out


# ═══════════════════════════════════════════════════════════════════
# Skew — 25-delta risk reversal / butterfly
# ═══════════════════════════════════════════════════════════════════

RR_TARGET_DELTA = 0.25


def _interp_iv_at_delta(deltas, ivs, target: float = RR_TARGET_DELTA) -> Optional[float]:
    """Linear interpolation of IV at |delta| = target across one wing.

    deltas must be ABSOLUTE deltas for a single option type on its OTM side, so
    the ladder is monotone in strike. Returns None when target is not bracketed —
    never extrapolates. A wing whose deltas do not reach 0.25 (deep-discount
    futures, thin chains) has no observable 25-delta point and must say so rather
    than invent one.
    """
    d = np.asarray(deltas, float)
    v = np.asarray(ivs, float)
    ok = np.isfinite(d) & np.isfinite(v) & (v > 0)
    d, v = d[ok], v[ok]
    if len(d) < 2:
        return None
    order = np.argsort(-d)          # descending |delta|
    d, v = d[order], v[order]
    if target > d[0] or target < d[-1]:
        return None                 # not bracketed
    for i in range(len(d) - 1):
        hi, lo = d[i], d[i + 1]
        if lo <= target <= hi:
            span = hi - lo
            if abs(span) < 1e-12:
                return float(v[i])
            w = (target - lo) / span
            return float(v[i + 1] + w * (v[i] - v[i + 1]))
    return None


def risk_reversal_25(strikes, atm_strike, ce_delta, ce_iv, ce_ltp,
                     pe_delta, pe_iv, pe_ltp, atm_iv: Optional[float] = None) -> Dict[str, Optional[float]]:
    """25-delta risk reversal and butterfly from the OTM wings.

        RR25 = IV(25d call) - IV(25d put)      negative => puts bid => downside skew
        BF25 = (IV(25d call) + IV(25d put))/2 - IV(ATM)    smile curvature

    Returns {'rr25', 'bf25', 'iv_25c', 'iv_25p'}; any value may be None.

    Three constraints, each load-bearing:

    1. OTM ONLY (calls at/above ATM, puts at/below). ITM IV is nearly all intrinsic
       value, so vega is tiny and the IV solve is ill-conditioned — NSE does not even
       publish it, and where the pipeline back-solves one it is noise. A single ITM
       IV of 46 on a 0.86-delta call is enough to destroy the measure.

    2. TRADED STRIKES ONLY (ltp > 0). This is the subtle one. oc_processor._resolve_iv
       back-solves IV from LTP against the FUTURES price, but silently falls back to
       NSE's published IV — which is SPOT-anchored with different rate/dividend
       assumptions — whenever LTP is 0. The two conventions differ by a mean 13.9 vol
       points (median 13.6, max 59.4) on the 2026-07-16 book. Interpolating across
       that seam would swamp a risk reversal that lives on a few tenths of a point.
       Untraded strikes also break delta monotonicity: 248 of the 282 non-monotone
       ladder points on that date were untraded strikes carrying NSE IVs.
       A bid/ask-mid fallback was considered and rejected: the untraded strikes in
       the 0.15-0.35 delta band had a median bid/ask spread of 80% of premium and
       not one passed even a lenient 50% filter, so their mid carries no information.

    3. NO EXTRAPOLATION. If a wing's deltas never reach 0.25, return None.

    Sign convention matches FX desks (RR = call vol - put vol). Empirically the NSE
    indices print negative (BANKNIFTY -0.68, NIFTY -0.42, FINNIFTY -0.41 on
    2026-07-16) while single stocks skew positive (DIXON +1.12, RELIANCE +0.74) —
    index put skew from portfolio hedging, single-stock call skew from retail call
    demand. That split is the expected structure and is a useful sanity check.
    """
    out: Dict[str, Optional[float]] = {"rr25": None, "bf25": None,
                                       "iv_25c": None, "iv_25p": None}
    k   = np.asarray(strikes, float)
    cd  = np.asarray(ce_delta, float)
    civ = np.asarray(ce_iv, float)
    clt = np.asarray(ce_ltp, float)
    pd_ = np.asarray(pe_delta, float)
    piv = np.asarray(pe_iv, float)
    plt = np.asarray(pe_ltp, float)
    if atm_strike is None or not np.isfinite(float(atm_strike)):
        return out

    call_m = (k >= float(atm_strike)) & (civ > 0) & (clt > 0)
    put_m  = (k <= float(atm_strike)) & (piv > 0) & (plt > 0)

    iv_c = _interp_iv_at_delta(cd[call_m], civ[call_m])
    iv_p = _interp_iv_at_delta(np.abs(pd_[put_m]), piv[put_m])
    out["iv_25c"] = round(iv_c, 4) if iv_c is not None else None
    out["iv_25p"] = round(iv_p, 4) if iv_p is not None else None
    if iv_c is None or iv_p is None:
        return out

    out["rr25"] = round(iv_c - iv_p, 4)
    if atm_iv is not None and atm_iv > 0:
        out["bf25"] = round((iv_c + iv_p) / 2.0 - float(atm_iv), 4)
    return out


def lopsidedness(net_gex) -> float:
    """Dimensionless net/gross gamma ratio in [-1, +1]. +1 all positive, -1 all negative."""
    g = np.asarray(net_gex, float)
    gross = float(np.abs(g).sum())
    return round(float(g.sum()) / gross, 4) if gross > 1e-9 else 0.0


GAMMA_SHELF_FRAC = 0.5   # shelf member |net gamma| must be >= this × peak |net gamma|
                         # (provisional; tune from post-backfill distribution)


def gamma_shelf(strikes, net_gex, frac: float = GAMMA_SHELF_FRAC,
                prev_com: Optional[float] = None) -> Optional[Dict]:
    """Dominant |net gamma| shelf — the band of adjacent strikes carrying the
    largest dealer gamma inventory (spec §3.1). Analogous to oi_shelf but on
    |net gamma| per strike, NOT premium-weighted OI, and WITHOUT the round-number
    preferred-strike mask (that effect is OI-liquidity-specific, not gamma).

    Walk contiguously out from the peak |net gamma| strike while neighbours stay
    >= frac × peak; stop at the first strike below threshold (a real gamma trough).

    Args:
        strikes:  1-D strike array (any order).
        net_gex:  matching per-strike NET gamma (signed; CE_gamma×oi×lot − PE…).
        frac:     shelf membership threshold as a fraction of peak |net gamma|.
        prev_com: previous-session shelf center, for migration.

    Returns dict (or None if no usable gamma):
        center          |gamma|-weighted center of mass of the shelf (Q1b COM)
        width           number of strikes in the shelf
        peak_strike     strike of max |net gamma|
        peak_value      SIGNED net gamma at the peak strike
        single_strike   True when width == 1 (sharp, fragile pin)
        lo, hi          shelf strike bounds
        concentration   peak |net gamma| / total |net gamma| over the whole input
                        range (defense-strength input; high = concentrated)
        migration       center - prev_com (if prev_com given) — +ve = shelf up
    """
    x = np.asarray(strikes, float)
    g = np.asarray(net_gex, float)
    if x.size == 0 or g.size == 0:
        return None
    order = np.argsort(x)
    xs, gs = x[order], g[order]
    ag = np.abs(gs)
    total_abs = float(ag.sum())
    if ag.max() <= 0 or total_abs <= 0:
        return None

    peak_i = int(np.argmax(ag))
    thr = ag[peak_i] * frac
    lo = peak_i
    while lo - 1 >= 0 and ag[lo - 1] >= thr:
        lo -= 1
    hi = peak_i
    while hi + 1 < len(ag) and ag[hi + 1] >= thr:
        hi += 1

    members = np.arange(lo, hi + 1)
    sx, sa = xs[members], ag[members]
    tot = float(sa.sum())
    com = float((sx * sa).sum() / tot) if tot > 0 else float(xs[peak_i])
    width = int(len(members))

    out: Dict[str, Any] = {
        "center":        round(com, 2),
        "width":         width,
        "peak_strike":   float(xs[peak_i]),
        "peak_value":    round(float(gs[peak_i]), 4),
        "single_strike": bool(width == 1),
        "lo":            float(sx.min()),
        "hi":            float(sx.max()),
        "concentration": round(float(ag[peak_i] / total_abs), 4),
    }
    if prev_com is not None:
        out["migration"] = round(com - float(prev_com), 2)
    return out


# ═══════════════════════════════════════════════════════════════════
# Dealer divergence: gamma-COM vs OI-COM (spec §3.3)
# ═══════════════════════════════════════════════════════════════════
# Dealers' GAMMA center of mass (where hedging pressure concentrates) vs their
# OI center of mass (where open positions sit) can diverge. When they align, the
# gamma shelf and the OI book agree on the level; when they diverge, the gamma
# pin sits away from the OI pile — the pin is gamma-driven, not OI-confirmed, and
# more fragile.
#
# CRITICAL — both COMs MUST be computed over the SAME in-range strike slice (the
# adaptive analysis_range around fut). A full-chain OI-COM is dragged by stale
# deep-OTM/ITM OI (validated on EICHERMOT: full-chain 7420 vs in-range 7141 at
# spot 7101 — a 300+pt artifact). The batch passes the gamma shelf's own range
# slice so the two centers are apples-to-apples by construction.
#
# Combined CE+PE OI-COM (one stable reference) — NOT the side nearer the gamma
# center (that minimises the gap by selection and flips reference day to day).

DEALER_DIVERGENCE_BANDS = {
    "aligned":            0.5,   # |gap| / expected_move < this
    "diverging":          1.5,   # < this (and >= aligned)
    # >= diverging → "strongly_diverging"
}


def oi_com_inrange(strikes, ce_oi, pe_oi) -> Optional[float]:
    """Combined CE+PE open-interest center of mass over the GIVEN strike slice.
    Caller is responsible for passing only the in-range strikes (same slice the
    gamma shelf uses) — this function does NOT range-filter, so that the range
    definition lives in exactly one place (analysis_range, applied by the caller)."""
    x = np.asarray(strikes, float)
    c = np.asarray(ce_oi, float)
    p = np.asarray(pe_oi, float)
    if x.size == 0:
        return None
    tot = c + p
    s = float(tot.sum())
    if s <= 0:
        return None
    return round(float((x * tot).sum() / s), 2)


def dealer_divergence(gamma_com: Optional[float], oi_com: Optional[float],
                      expected_move: Optional[float],
                      bands: Optional[Dict] = None) -> Tuple[Optional[float], Optional[str]]:
    """Classify gamma-COM vs OI-COM divergence, expected-move-normalised.

    Returns (divergence_value, label). divergence_value = (gamma_com - oi_com) /
    expected_move (SIGNED: +ve = gamma shelf sits ABOVE the OI pile). label is the
    three-state band on |divergence_value|. Either may be None when inputs are
    missing or expected_move is non-positive."""
    b = {**DEALER_DIVERGENCE_BANDS, **(bands or {})}
    if (gamma_com is None or oi_com is None
            or expected_move is None or expected_move <= 0):
        return None, None
    dv = (float(gamma_com) - float(oi_com)) / float(expected_move)
    a = abs(dv)
    if a < b["aligned"]:
        label = "aligned"
    elif a < b["diverging"]:
        label = "diverging"
    else:
        label = "strongly_diverging"
    return round(dv, 4), label


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
    "mixed":        {"color": "#9ca3af", "order": 2, "label": "mixed"},           # grey (both signs)
    "negative":     {"color": "#f59e0b", "order": 3, "label": "negative"},        # orange
    "all_negative": {"color": "#dc2626", "order": 4, "label": "all negative"},    # deep red
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
    Signals (DESCRIPTIVE LABELS, not predictions -- see SIGNAL_INFO):
             regime_flip_to_neg/pos, flip_drift_up/down, pin_strengthening
             (shown as "Zone Narrowed"), instability_widening.

    RETIRED 2026-10-04: crash_risk, bull_trend_reinforce, bear_trend_reinforce.
    Their vanna legs read ce_vanna / pe_vanna, which are NOT dealer-signed (both
    legs enter with the same sign), so "pe_vanna < 0" only said put OI sits below
    the forward (true on 69% of rows) and "ce_vanna > 0" was true on 96%. None
    showed a next-day edge. Rebuild on dealer_vanna (and its wing split) before
    reinstating any of them.

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
    # Continuous instab_score (2026-10-04): today's ngf change as a percentile of
    # this symbol's own trailing changes (filled by Stage 2). A fixed +0.05 fired
    # on ~27% of rows because one strike changing sign moves ngf 0.055-0.083 on a
    # 12-18 strike range -- the label meant "one strike flipped", not "unusual".
    # Fall back to the fixed threshold until a symbol has enough history.
    sc_ = curr.get("instab_score")
    try:
        sc_ = None if sc_ is None else float(sc_)
    except (TypeError, ValueError):
        sc_ = None
    if sc_ is not None and sc_ == sc_:                      # not NaN
        if ng_c is not None and ng_p is not None and ng_c > ng_p and sc_ >= p["INSTAB_PCTILE"]:
            fired.append("instability_widening")
    elif ng_c is not None and ng_p is not None and (ng_c - ng_p) > p["INSTAB_DELTA"]:
        fired.append("instability_widening")

    return ",".join(fired), active


_UNSET = object()   # distinguishes "caller omitted atm_iv" from "atm_iv is None"


def confidence(total_oi: float, n_strikes: int, tw_norm: Optional[float],
               atm_iv: Any = _UNSET) -> str:
    """Row-level trust tag.

    atm_iv is a hard gate, not a tie-breaker: a row whose ATM IV could not be
    read has no expected_move anchor, so its *_norm geometry and iv_change are
    unusable no matter how many strikes or how much OI sit behind it. Before
    this gate existed such rows were still stamped "high" (29 of 32 occurrences
    in Jun-Jul 2026), which is precisely why the failure stayed invisible — the
    pipeline computed a degenerate value and then certified it as trustworthy.

    The _UNSET sentinel (rather than a None default) is deliberate: None is the
    *failure* value the gate must catch, so it cannot also mean "not supplied".
    Legacy three-arg callers skip the gate; callers that pass atm_iv=None get it.
    """
    if atm_iv is not _UNSET and not (atm_iv is not None and atm_iv > 0):
        return "low"
    if n_strikes >= 8 and total_oi > 0 and (tw_norm or 1) < 0.5:
        return "high"
    if n_strikes >= 4:
        return "medium"
    return "low"


# ═══════════════════════════════════════════════════════════════════
# Structural Strength Score (history view differentiator)
# ═══════════════════════════════════════════════════════════════════
# Per-day, day-over-day descriptive heuristic. Five independent-ish axes, +1/−1
# each, range [−5, +5]. "+" = structure stabilising (regime building), "−" =
# destabilising. The CUMULATIVE running sum over the window is the headline
# "building vs deteriorating" number.
#
# DISCIPLINE — this describes the *regime*, NOT an exogenous-shock forecast.
# BANKINDIA stabilised structurally into the Jun-19 crash; a high score did not
# predict the gap. Validate against next_day_realized_move before any predictive
# claim. The five axes:
#   1. lopsided (net_gex_norm) more positive  → +1 / more negative → −1
#   2. neg_gamma_fraction shrinking            → +1 / expanding     → −1
#   3. atm_iv_smoothed falling (iv_change<0)   → +1 / rising        → −1
#   4. flip receding (|flip_norm_distance| ↑)  → +1 / approaching   → −1
#   5. regime unchanged                        → +1 / changed       → −1
#
# REMOVED 2026-10-04: axis 6, transition_width_norm narrowing → +1. The width
# is the gap between the two strikes around the per-strike crossing, so it is
# ~83% strike-grid density, and narrow zones held LESS often than wide ones
# (31% vs 58%) -- the axis pointed the wrong way and added grid noise.
#
# EXCLUDED on purpose:
#   - realized-move shrinking → forward-outcome leak (validate AGAINST it, don't
#     bake it in).
#   - basis → stock-noisy in v1; revisit once dead-zoned basis history is trusted.
#
# OVERLAP NOTE: axes 1 (lopsided) and 5 (regime unchanged) partly co-capture
# regime persistence — intentional emphasis on persistence, not 5 fully
# orthogonal axes.

STRENGTH_EPS = 1e-9  # treat |delta| below this as "no change" (0 contribution)


def strength_axes(curr: Dict, prev: Optional[Dict]) -> Dict[str, int]:
    """Per-axis ±1/0 contributions for one day vs the previous row.
    First row of a window (prev is None) → all zeros (no day-over-day basis)."""
    ax = {"lopsided": 0, "neg_gamma": 0, "iv": 0, "flip": 0, "regime": 0}
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

    return ax


def strength_score(curr: Dict, prev: Optional[Dict]) -> int:
    """Net per-day structural strength score in [-5, +5] (sum of strength_axes)."""
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


# Signal metadata for UI / docs (single source of truth).
# These are DESCRIPTIVE LABELS of what changed since the previous session. None
# showed a stable next-day edge in the 2026-10 re-validation (date-clustered on
# eod, six years on hist); the predictive content is in the regime LEVEL and the
# gamma position, not in the day something changed. Wording says what happened,
# not what to do.
SIGNAL_INFO = {
    "regime_flip_to_neg": ("Regime → Negative",
        "Price moved below its zero-gamma level: dealers are now short gamma at "
        "price, so their hedging amplifies moves. Usually follows a fall through "
        "the flip. See γ POS for how deep."),
    "regime_flip_to_pos": ("Regime → Positive",
        "Price moved back above its zero-gamma level: dealers are long gamma at "
        "price again, so their hedging dampens moves. See γ POS for how deep."),
    # Text is INTERPOLATED from DEFAULTS, not hardcoded. The previous literal
    # said ">15% of expected move" long after DRIFT_FRAC was raised to 0.25,
    # so the tooltip described a threshold the code had stopped using.
    "flip_drift_up": ("Flip Drifting Up",
        f"Zero-gamma level moved up by >{DEFAULTS['DRIFT_FRAC']:.0%} of the expected "
        f"move since the last session, while within {DEFAULTS['DRIFT_MAX_NORM_DIST']:g} EM "
        "of price."),
    "flip_drift_down": ("Flip Drifting Down",
        f"Zero-gamma level moved down by >{DEFAULTS['DRIFT_FRAC']:.0%} of the expected "
        f"move since the last session, while within {DEFAULTS['DRIFT_MAX_NORM_DIST']:g} EM "
        "of price."),
    # Key kept for history and filters; shown as "Zone Narrowed". The width is the
    # gap between the two strikes around the per-strike crossing, so it is mostly
    # strike-grid density; narrow zones held LESS often than wide ones (31% vs 58%).
    "pin_strengthening": ("Zone Narrowed",
        "Per-strike transition zone narrowed by more than 10% under positive gamma. "
        "Mostly reflects the strike grid; not a pin-strength measure."),
    "instability_widening": ("Neg-γ Strikes Up",
        "Share of in-range strikes with negative net gamma rose by more than this "
        f"symbol usually sees (top {100 - DEFAULTS['INSTAB_PCTILE'] * 100:.0f}% of its own "
        "day-over-day changes; stored as instab_score)."),
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
        "Sign of the dealer gamma profile at the futures price (zero-gamma method), "
        "with a 5% mixed band.",
        "Positive = dealers long gamma, hedging DAMPENS moves. Negative = dealers short "
        "gamma, hedging AMPLIFIES moves. Changes only when price crosses the zero-gamma "
        "level, so runs are long. all_positive / all_negative when the profile never "
        "changes sign within ±3 expected moves."),
    "gamma_position": ("γ POS",
        "Dealer gamma at the futures price divided by the largest |gamma| on the "
        "profile, in [-1, +1].",
        "How deep in the regime price sits: +1 deep long gamma, -1 deep short gamma, "
        "0 at the flip. Lower values have been followed by larger next-day moves "
        "(six-year test), strongest in falling or fragile markets."),
    "days_in_regime": ("DAYS",
        "Consecutive sessions the UNDERLYING has spent in the current gamma regime "
        "(counts across monthly rolls).",
        "Long positive streaks are normal: the regime only changes when price crosses "
        "the zero-gamma level. Read it together with γ POS. Colour: green positive, "
        "red negative."),
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
    "rr25": ("RR25",
        "25-delta risk reversal: IV(25d call) - IV(25d put), in vol points.",
        "Negative = puts bid over calls = the market is paying up for downside; "
        "positive = call skew. Read it against the gamma columns rather than with "
        "them: it is near-orthogonal to net_gex_norm (corr -0.13), so agreement is "
        "genuine confirmation and disagreement is a real signal. NSE indices "
        "normally print negative (portfolio hedging demand) while single stocks "
        "often print positive (retail call demand) - compare a name to its own "
        "history, not to the cross-section. NULL when a wing never reaches 25 delta."),
    "bf25": ("BF25",
        "25-delta butterfly: mean of the two 25d wing IVs minus ATM IV.",
        "Smile curvature, and the companion to RR25: RR25 says which wing is bid, "
        "BF25 says whether BOTH wings are bid relative to ATM. High BF25 with flat "
        "RR25 = the market is paying for movement in either direction."),
    "grid_density": ("GRID",
        "strike_interval / expected_move - how fine the strike grid is vs the move "
        "it has to resolve.",
        "Diagnostic, not a trading signal. Low = dense grid (indices, liquid "
        "large-caps); high = coarse grid (illiquid mid-caps). Present because "
        "transition_width_norm correlates 0.83 with it: control for this column "
        "before reading any behavioural claim off zone width."),
    "gamma_flip": ("γ FLIP",
        "Zero-gamma level: the price at which total dealer gamma (re-evaluated across "
        "the whole chain) changes sign, nearest the futures price.",
        "Price above the flip = positive regime in the usual shape (put OI below, call "
        "OI above); below = negative. Typically sits about half an expected move "
        "below price."),
    "gamma_shelf_center": ("γ SHELF",
        "Center of mass of the dominant gamma shelf — the band of strikes carrying the "
        "largest dealer gamma inventory (Q1/Q1b: where hedging is concentrated).",
        "This is the hedging center of mass. A single-strike shelf (marked ◆) is a sharp, "
        "fragile pin; a wide shelf is distributed defense. Hover for width / peak."),
    "migration_effectiveness": ("MIG EFF",
        "|spot move %| / |gamma-COM move %| vs the previous session.",
        "≈1 = orderly (dealers repositioning in step with price); ≫1 = price running "
        "away from a static structure (unstable / chasing); ≈0 = COM repositioning "
        "without spot (anticipatory). Blank when COM barely moves."),
    "flip_velocity": ("FLIP Δ/d",
        "Flip migration speed in points per calendar day (vs previous session).",
        "Sign = direction of drift; larger magnitude = structure shifting faster. "
        "Calendar-normalised so weekend gaps don't inflate it."),
    "flip_norm_distance": ("FLIP DIST",
        "Zero-gamma level distance from price in expected-move units = "
        "(flip - fut) / expected_move.",
        "Closer to 0 = price near the level where dealer hedging switches sign; next-day "
        "moves have been larger there. Negative = flip below price (usual in positive "
        "regime)."),
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
