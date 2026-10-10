"""Unit tests for exposure_core pure math. Fast, deterministic, no DB/app deps."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import math

import numpy as np
import pytest
import exposure_core as core


# ── strike_interval ────────────────────────────────────────────────
def test_strike_interval_uniform():
    assert core.strike_interval(np.array([100, 150, 200, 250])) == 50.0

def test_strike_interval_single():
    assert core.strike_interval(np.array([100])) == 0.0

def test_strike_interval_irregular():
    # median of [50,50,100] = 50
    assert core.strike_interval(np.array([100, 150, 200, 300])) == 50.0


# ── expected_move ──────────────────────────────────────────────────
def test_expected_move_basic():
    # 23400 x 0.15 x sqrt(23/243) ~= 1080  (TRADING days, not calendar)
    em = core.expected_move(23400, 15.0, 23)
    assert 1070 < em < 1090


def test_expected_move_uses_trading_day_convention():
    """T must be scaled by trading days, not calendar days.

    Regression guard: the original dte/365 scaling under-stated every move by
    sqrt(365/243) = 1.226x, so only 60.2% of realised moves landed inside +/-1 EM
    where a calibrated 1-sigma band should hold 68.3% (validated over 12,018 rows
    / 6 expiry cycles). 243 is the measured NSE figure, NOT the NYSE 252."""
    assert core.TRADING_DAYS_YEAR == 243.0
    em = core.expected_move(1000.0, 20.0, 30)
    assert em == pytest.approx(1000.0 * 0.20 * math.sqrt(30 / 243.0))
    # and it is materially wider than the old calendar-day form
    old = 1000.0 * 0.20 * math.sqrt(30 / 365.0)
    assert em / old == pytest.approx(math.sqrt(365.0 / 243.0), rel=1e-9)


def test_expected_move_1d_has_no_horizon_factor():
    """The 1-day EM must be independent of dte.

    This is the whole point of the separate function: expected_move() scales with
    sqrt(dte), so using it as a next-day forecast makes (1d move / EM) ~ 1/sqrt(dte)
    and any model built on that ratio measures days-to-expiry, not structure."""
    e1 = core.expected_move_1d(1000.0, 20.0)
    assert e1 == pytest.approx(1000.0 * 0.20 * math.sqrt(1.0 / 243.0))
    # one session of the horizon EM at dte=1 is the same thing
    assert e1 == pytest.approx(core.expected_move(1000.0, 20.0, 1))


def test_expected_move_1d_fallback():
    # no IV -> 5% annual fallback de-annualised to one session
    assert core.expected_move_1d(1000.0, 0) == pytest.approx(
        1000.0 * 0.05 / math.sqrt(243.0))
    assert core.expected_move_1d(1000.0, None) == pytest.approx(
        1000.0 * 0.05 / math.sqrt(243.0))


def test_expected_move_fallback():
    # no IV → 5% fallback
    assert core.expected_move(20000, 0, 0) == 1000.0


# ── analysis_range ─────────────────────────────────────────────────
def test_analysis_range_centered_on_ref():
    lo, hi, half = core.analysis_range(23400, 800, 50)
    assert lo < 23400 < hi
    assert abs((lo + hi) / 2 - 23400) < 1e-6   # centered
    assert half == 850   # 800 + 1×50

def test_analysis_range_absolute_cap():
    # huge expected move → capped at 15%
    lo, hi, half = core.analysis_range(10000, 5000, 100)
    assert half == 1500   # 15% of 10000

def test_analysis_range_change_cap():
    # prev half 1000, new would be 2000 → capped to +25% = 1250
    lo, hi, half = core.analysis_range(20000, 1900, 100, prev_range_half=1000)
    assert half == 1250


# ── gamma_flip ─────────────────────────────────────────────────────
def test_gamma_flip_simple_crossing():
    strikes = np.array([100, 110, 120, 130])
    gex     = np.array([-10, -5, 5, 10])   # crosses between 110 and 120
    flip, nearest, regime = core.gamma_flip(strikes, gex, ref_price=125)
    assert 110 < flip < 120
    assert nearest in (110, 120)
    assert regime == "positive"   # ref 125 is above flip → positive gamma

def test_gamma_flip_regime_at_ref_below():
    strikes = np.array([100, 110, 120, 130])
    gex     = np.array([-10, -5, 5, 10])
    _, _, regime = core.gamma_flip(strikes, gex, ref_price=105)
    assert regime == "negative"   # ref below flip → negative gamma

def test_gamma_flip_all_positive():
    flip, nearest, regime = core.gamma_flip(
        np.array([100, 110, 120]), np.array([5, 8, 10]))
    assert flip is None and regime == "all_positive"

def test_gamma_flip_all_negative():
    flip, nearest, regime = core.gamma_flip(
        np.array([100, 110, 120]), np.array([-5, -8, -10]))
    assert flip is None and regime == "all_negative"

def test_gamma_flip_interpolation_exact():
    # linear crossing exactly at 115
    strikes = np.array([110, 120])
    gex     = np.array([-5, 5])
    flip, _, _ = core.gamma_flip(strikes, gex, ref_price=120)
    assert flip == 115.0


# ── transition_width ───────────────────────────────────────────────
def test_transition_width():
    strikes = np.array([100, 150, 200, 250])
    gex     = np.array([-10, -5, 5, 10])   # crossing 150→200
    assert core.transition_width(strikes, gex) == 50.0

def test_transition_width_no_crossing():
    assert core.transition_width(np.array([1,2,3]), np.array([1,2,3])) is None


# ── lopsidedness ───────────────────────────────────────────────────
def test_lopsidedness_all_positive():
    assert core.lopsidedness(np.array([10, 20, 30])) == 1.0

def test_lopsidedness_all_negative():
    assert core.lopsidedness(np.array([-10, -20, -30])) == -1.0

def test_lopsidedness_balanced():
    assert abs(core.lopsidedness(np.array([10, -10]))) < 1e-9

def test_lopsidedness_bounded():
    # any input stays within [-1, 1]
    g = np.array([-1e9, 5e3, -3e8, 2e3])
    r = core.lopsidedness(g)
    assert -1.0 <= r <= 1.0


# ── derive_signals ─────────────────────────────────────────────────
def test_signal_regime_flip_to_neg():
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "negative", "expected_move": 800}
    sigs, active = core.derive_signals(curr, prev)
    assert "regime_flip_to_neg" in sigs
    assert active == "negative"

def test_instability_uses_symbol_percentile_when_available():
    """2026-10-04: with an instab_score the label fires on the symbol's own top
    decile of ngf rises, not on a fixed +0.05 (which was one strike flipping)."""
    prev = {"gex_regime": "positive", "neg_gamma_fraction": 0.40}
    up = {"gex_regime": "positive", "neg_gamma_fraction": 0.48}
    fire = lambda c: "instability_widening" in core.derive_signals(c, prev)[0].split(",")
    assert fire({**up, "instab_score": 0.95})           # unusual for this name
    assert not fire({**up, "instab_score": 0.50})       # +0.08 but ordinary here
    assert fire({**up, "instab_score": None})           # no history: fixed fallback
    assert not fire({**prev, "instab_score": 0.99})     # no rise, no label


def test_retired_vanna_signals_never_fire():
    """crash_risk / bull_trend_reinforce / bear_trend_reinforce were retired on
    2026-10-04 (their ce/pe vanna legs are not dealer-signed). Inputs that used to
    fire each of them must now fire none, and they must be gone from SIGNAL_INFO."""
    retired = {"crash_risk", "bull_trend_reinforce", "bear_trend_reinforce"}
    cases = [
        ({"gex_regime": "negative"}, {"gex_regime": "negative", "iv_change": 1.5,
                                      "pe_vanna": -500, "expected_move": 800}),
        ({"gex_regime": "positive"}, {"gex_regime": "positive", "ce_vanna": 500,
                                      "iv_change": 1.0, "expected_move": 800}),
        ({"gex_regime": "negative"}, {"gex_regime": "negative", "pe_vanna": -500,
                                      "iv_change": -1.0, "expected_move": 800}),
    ]
    for prev, curr in cases:
        sigs, _ = core.derive_signals(curr, prev)
        assert not (set(filter(None, sigs.split(","))) & retired), sigs
    assert not (set(core.SIGNAL_INFO) & retired)


def test_signal_drift_threshold():
    # velocity below 25% of expected move → no drift (flip near spot)
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "positive", "flip_velocity": 50, "expected_move": 800,
            "flip_norm_distance": 0.3}
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" not in sigs   # 50 < 200 (0.25×800)
    # above threshold + flip near spot → fires
    curr["flip_velocity"] = 300
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" in sigs

def test_signal_drift_relevance_gate():
    # velocity well above threshold, but flip is DISTANT (|norm_dist| > 1) → suppressed
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "positive", "flip_velocity": 400, "expected_move": 800,
            "flip_norm_distance": 1.5}
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" not in sigs   # distant flip wobble = noise
    # same velocity, flip near spot → fires
    curr["flip_norm_distance"] = 0.5
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" in sigs

def test_signal_drift_gate_configurable():
    # widen the gate to 1.25 → a flip at 1.1 EM now fires
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "positive", "flip_velocity": 400, "expected_move": 800,
            "flip_norm_distance": 1.1}
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" not in sigs   # default gate 1.0 → suppressed
    sigs, _ = core.derive_signals(curr, prev, params={"DRIFT_MAX_NORM_DIST": 1.25})
    assert "flip_drift_up" in sigs       # widened gate → fires

def test_signal_no_prev_returns_empty():
    sigs, active = core.derive_signals({"gex_regime": "positive"}, None)
    assert sigs == "" and active == "positive"


# ── confidence ─────────────────────────────────────────────────────
def test_width_norm_valid_range():
    """Normal case: grid finer than the expected move, ratio is meaningful."""
    assert core.width_norm(50.0, 337.6) == 0.1481      # NIFTY-like
    assert core.width_norm(10.0, 10.0) == 1.0          # boundary is inclusive


def test_width_norm_degenerate_returns_none():
    """strike_interval > expected_move => the metric is undefined, not 'wide'.

    transition_width is floored at one strike interval, so a coarse grid forces
    the ratio above 1.0 regardless of dealer behaviour. Emitting the number would
    invite a 'very wide zone' reading of what is really 'grid too coarse to tell'.
    Real observed cases from the 2026-07-16 snapshot."""
    assert core.width_norm(7.5, 7.11) is None          # SAIL   -> would be 1.0545
    assert core.width_norm(1.0, 0.98) is None          # YESBANK-> would be 1.0199


def test_width_norm_null_inputs():
    assert core.width_norm(None, 100.0) is None        # monotone curve, no flip
    assert core.width_norm(50.0, 0.0) is None          # no expected-move anchor
    assert core.width_norm(0.0, 100.0) is None


def test_confidence_levels():
    assert core.confidence(1000, 10, 0.3) == "high"
    assert core.confidence(1000, 5, 0.8) == "medium"
    assert core.confidence(0, 2, None) == "low"


def test_confidence_atm_iv_gate():
    """A row with no readable ATM IV is never trustworthy, however thick the
    chain: without an IV anchor there is no expected_move, so every *_norm
    metric on the row is meaningless. Regression guard for the Jun-Jul 2026
    bug where 29 of 32 zero-IV rows were stamped 'high'."""
    # None (no leg quoted) and 0.0 (legacy sentinel) both demote to low
    assert core.confidence(1000, 10, 0.3, None) == "low"
    assert core.confidence(1000, 10, 0.3, 0.0) == "low"
    # A real reading leaves the existing ladder untouched
    assert core.confidence(1000, 10, 0.3, 15.4) == "high"
    assert core.confidence(1000, 5, 0.8, 15.4) == "medium"
    # Omitting the arg entirely skips the gate (back-compat with 3-arg callers)
    assert core.confidence(1000, 10, 0.3) == "high"


# ── basis dead-zone (history view + screener basis) ────────────────
def test_basis_deadzone_noise_is_neutral():
    # the -0.03 Jun-18 BANKINDIA closing-tick print → within dead-zone
    assert core.basis_deadzone(-0.03) is True
    assert core.basis_deadzone(0.05) is True

def test_basis_deadzone_real_basis_outside():
    assert core.basis_deadzone(0.5) is False
    assert core.basis_deadzone(-1.2) is False

def test_basis_deadzone_none_in_zone():
    # no meaningful basis → treat as in-zone (neutral)
    assert core.basis_deadzone(None) is True

def test_basis_deadzone_configurable():
    assert core.basis_deadzone(0.15, deadzone=0.2) is True
    assert core.basis_deadzone(0.15, deadzone=0.1) is False


# ── regime colour ramp (shared screener + history) ─────────────────
def test_regime_color_ordered_ramp():
    # stabilising → destabilising ordering
    assert core.regime_color("all_positive")["order"] < core.regime_color("positive")["order"]
    assert core.regime_color("positive")["order"] < core.regime_color("negative")["order"]
    assert core.regime_color("negative")["order"] < core.regime_color("all_negative")["order"]

def test_regime_color_unknown_fallback():
    m = core.regime_color("nonsense")
    assert m["order"] == 99 and m["label"] == ""

def test_regime_color_has_hex():
    assert core.regime_color("all_positive")["color"].startswith("#")


# ── structural strength score ──────────────────────────────────────
def _strong_pair():
    """prev/curr where every axis improves → score +5 (five axes since the
    transition-width axis was removed 2026-10-04)."""
    prev = {"net_gex_norm": 0.2, "neg_gamma_fraction": 0.4, "iv_change": None,
            "atm_iv_smoothed": 25, "flip_norm_distance": 0.5,
            "gex_regime": "positive", "transition_width_norm": 0.6}
    curr = {"net_gex_norm": 0.5, "neg_gamma_fraction": 0.3, "iv_change": -1.0,
            "flip_norm_distance": 0.9, "gex_regime": "positive",
            "transition_width_norm": 0.5}
    return prev, curr

def test_strength_score_max_positive():
    prev, curr = _strong_pair()
    assert core.strength_score(curr, prev) == 5

def test_strength_score_max_negative():
    # mirror: every axis worsens → -5
    prev = {"net_gex_norm": 0.5, "neg_gamma_fraction": 0.3, "iv_change": None,
            "atm_iv_smoothed": 20, "flip_norm_distance": 0.9,
            "gex_regime": "positive", "transition_width_norm": 0.5}
    curr = {"net_gex_norm": 0.2, "neg_gamma_fraction": 0.4, "iv_change": 1.0,
            "flip_norm_distance": 0.5, "gex_regime": "negative",
            "transition_width_norm": 0.6}
    assert core.strength_score(curr, prev) == -5

def test_strength_score_range_bounded():
    prev, curr = _strong_pair()
    s = core.strength_score(curr, prev)
    assert -5 <= s <= 5

def test_strength_transition_width_not_an_axis():
    """Removed 2026-10-04: a narrowing transition zone must not move the score."""
    prev = {"gex_regime": "positive", "transition_width_norm": 0.6}
    curr = {"gex_regime": "positive", "transition_width_norm": 0.3}
    ax = core.strength_axes(curr, prev)
    assert "trans_w" not in ax and core.strength_score(curr, prev) == 1   # regime only

def test_strength_score_first_row_zero():
    _, curr = _strong_pair()
    assert core.strength_score(curr, None) == 0

def test_strength_regime_change_costs_axis():
    # identical except regime flips → regime axis = -1, others 0 → score -1
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "negative"}
    ax = core.strength_axes(curr, prev)
    assert ax["regime"] == -1

def test_strength_iv_falls_back_to_smoothed_atm():
    # no iv_change → use atm_iv_smoothed delta
    prev = {"atm_iv_smoothed": 25, "gex_regime": "positive"}
    curr = {"atm_iv_smoothed": 23, "gex_regime": "positive"}  # IV falling → +1
    ax = core.strength_axes(curr, prev)
    assert ax["iv"] == 1

def test_strength_flip_uses_absolute_distance():
    # flip receding = |flip_norm_distance| increasing (sign-agnostic)
    prev = {"flip_norm_distance": -0.4, "gex_regime": "positive"}
    curr = {"flip_norm_distance": -0.8, "gex_regime": "positive"}  # |0.8|>|0.4| → +1
    ax = core.strength_axes(curr, prev)
    assert ax["flip"] == 1

def test_strength_series_cumulative():
    prev, curr = _strong_pair()
    ser = core.strength_series([prev, curr])
    assert ser[0]["strength_score"] == 0          # first row, no prior
    assert ser[0]["strength_cumulative"] == 0
    assert ser[1]["strength_score"] == 5
    assert ser[1]["strength_cumulative"] == 5
    # originals untouched (returns new dicts)
    assert "strength_score" not in prev

def test_strength_series_oscillating_nets_near_zero():
    # noisy regime: alternating improve/worsen → cumulative stays small
    a = {"net_gex_norm": 0.2, "gex_regime": "positive"}
    b = {"net_gex_norm": 0.1, "gex_regime": "negative"}  # worsens
    c = {"net_gex_norm": 0.2, "gex_regime": "positive"}  # improves back
    ser = core.strength_series([a, b, c])
    assert abs(ser[-1]["strength_cumulative"]) <= 2


# -- 25-delta risk reversal ------------------------------------------
# Real NIFTY chain, 2026-07-16, expiry 2026-07-28, atm_strike 24050.
# Cross-checked against an independent SQL implementation (rr25 = -0.415).
_NIFTY_CHAIN = [
    # strike, ce_delta, ce_iv, ce_ltp, pe_delta, pe_iv, pe_ltp
    (23850.0, 0.7931, 7.1175, 364.30, -0.2069, 7.2199, 114.20),
    (23900.0, 0.7457, 7.0672, 330.00, -0.2534, 7.1502, 129.30),
    (23950.0, 0.6933, 6.9794, 296.25, -0.3074, 7.1431, 147.80),
    (24050.0, 0.5711, 6.9184, 237.30, -0.4269, 7.0587, 188.00),
    (24250.0, 0.3129, 6.8003, 141.95, -0.6820, 6.8680, 289.75),
    (24300.0, 0.2546, 6.7416, 122.00, -0.7373, 6.8948, 322.00),
    (24350.0, 0.2031, 6.7217, 105.00, -0.7891, 6.8506, 354.00),
]


def _chain_cols(rows):
    return ([r[0] for r in rows], [r[1] for r in rows], [r[2] for r in rows],
            [r[3] for r in rows], [r[4] for r in rows], [r[5] for r in rows],
            [r[6] for r in rows])


def test_interp_iv_at_delta_brackets():
    # descending |delta| ladder; 0.25 sits between 0.2546 and 0.2031
    iv = core._interp_iv_at_delta([0.2546, 0.2031], [6.7416, 6.7217], 0.25)
    assert iv == pytest.approx(6.7398, abs=1e-3)


def test_interp_iv_at_delta_never_extrapolates():
    """A wing that does not reach the target delta has no observable 25d point.

    Real case: TORNTPHARM on 2026-07-16 had a -2.85% futures basis (JB Chemicals
    merger), pushing every OTM call below 0.145 delta. Extrapolating would invent
    a number; None is the honest answer."""
    assert core._interp_iv_at_delta([0.145, 0.09, 0.02], [30, 31, 32], 0.25) is None
    assert core._interp_iv_at_delta([0.45, 0.40], [20, 21], 0.25) is None
    assert core._interp_iv_at_delta([0.25], [20.0], 0.25) is None   # single point


def test_risk_reversal_25_nifty_real_chain():
    k, cd, civ, clt, pd_, piv, plt = _chain_cols(_NIFTY_CHAIN)
    r = core.risk_reversal_25(k, 24050.0, cd, civ, clt, pd_, piv, plt, atm_iv=6.92)
    assert r["iv_25c"] == pytest.approx(6.74, abs=0.02)
    assert r["iv_25p"] == pytest.approx(7.16, abs=0.02)
    # negative => puts bid over calls => index put skew, the expected sign
    assert r["rr25"] == pytest.approx(-0.415, abs=0.02)
    assert r["rr25"] < 0
    assert r["bf25"] == pytest.approx(0.03, abs=0.02)


def test_risk_reversal_25_excludes_itm():
    """ITM IVs must never enter the calculation.

    Regression guard: ITM options are nearly all intrinsic value, so vega is tiny
    and the IV solve is ill-conditioned. Poisoning one deep-ITM call with a wild
    IV must not move the result by a single basis point, because the OTM mask
    should have excluded it before interpolation."""
    k, cd, civ, clt, pd_, piv, plt = _chain_cols(_NIFTY_CHAIN)
    clean = core.risk_reversal_25(k, 24050.0, cd, civ, clt, pd_, piv, plt, 6.92)
    civ_bad = list(civ)
    civ_bad[0] = 46.0          # strike 23850 is an ITM call (delta 0.79)
    dirty = core.risk_reversal_25(k, 24050.0, cd, civ_bad, clt, pd_, piv, plt, 6.92)
    assert dirty["rr25"] == clean["rr25"]


def test_risk_reversal_25_excludes_untraded_strikes():
    """ltp == 0 strikes carry NSE's SPOT-anchored IV, not the pipeline's
    futures-anchored solve. The two differ by ~14 vol points on average, which
    would swamp a risk reversal measured in tenths of a point.

    The untraded strike must be dropped before interpolation, so a wildly wrong
    IV on it cannot move the result. Here the remaining traded strikes (0.3129 /
    0.2031) still bracket 0.25, so the wing degrades gracefully to a slightly
    coarser interpolation rather than vanishing."""
    k, cd, civ, clt, pd_, piv, plt = _chain_cols(_NIFTY_CHAIN)
    clt_bad = list(clt)
    civ_bad = list(civ)
    clt_bad[5] = 0.0           # 24300 untraded ...
    civ_bad[5] = 20.6          # ... carrying a spot-anchored IV 14 pts too high
    r = core.risk_reversal_25(k, 24050.0, cd, civ_bad, clt_bad, pd_, piv, plt, 6.92)
    # the poisoned 20.6 is excluded: the result stays in the real 6.7-6.8 range
    assert r["iv_25c"] == pytest.approx(6.76, abs=0.05)
    assert r["rr25"] == pytest.approx(-0.40, abs=0.05)
    # sanity: had 20.6 leaked in, iv_25c would be an order of magnitude higher
    assert r["iv_25c"] < 10


def test_risk_reversal_25_untraded_breaks_bracket():
    """When dropping untraded strikes leaves the target unbracketed, return None
    rather than extrapolating from the surviving points."""
    k, cd, civ, clt, pd_, piv, plt = _chain_cols(_NIFTY_CHAIN)
    clt_bad = list(clt)
    # zero every OTM call below 0.5 delta: survivors are 0.5711 only, so the
    # wing has <2 usable points and 0.25 cannot be bracketed
    clt_bad[4] = 0.0           # 24250 (d=0.3129)
    clt_bad[5] = 0.0           # 24300 (d=0.2546)
    clt_bad[6] = 0.0           # 24350 (d=0.2031)
    r = core.risk_reversal_25(k, 24050.0, cd, civ, clt_bad, pd_, piv, plt, 6.92)
    assert r["iv_25c"] is None
    assert r["rr25"] is None
    # the put wing was untouched and still resolves
    assert r["iv_25p"] is not None


def test_risk_reversal_25_no_atm_iv_still_gives_rr():
    """bf25 needs an ATM anchor; rr25 does not."""
    k, cd, civ, clt, pd_, piv, plt = _chain_cols(_NIFTY_CHAIN)
    r = core.risk_reversal_25(k, 24050.0, cd, civ, clt, pd_, piv, plt, atm_iv=None)
    assert r["rr25"] is not None
    assert r["bf25"] is None


def test_risk_reversal_25_empty_and_bad_input():
    assert core.risk_reversal_25([], None, [], [], [], [], [], [])["rr25"] is None
    k, cd, civ, clt, pd_, piv, plt = _chain_cols(_NIFTY_CHAIN)
    # no traded strikes at all
    zeros = [0.0] * len(k)
    r = core.risk_reversal_25(k, 24050.0, cd, civ, zeros, pd_, piv, zeros)
    assert r["rr25"] is None and r["iv_25c"] is None and r["iv_25p"] is None


# ── dealer_gamma_profile (zero-gamma flip, regime, gamma position, dealer vanna) ──
def _chain(strikes, ce_oi, pe_oi, iv=25.0):
    k = np.array(strikes, dtype=float)
    return dict(strikes=k, ce_oi=np.array(ce_oi, float), pe_oi=np.array(pe_oi, float),
                ce_iv=np.full(len(k), iv), pe_iv=np.full(len(k), iv))


def test_profile_put_wall_below_call_wall_above():
    """Puts below, calls above, price nearer the call wall: dealers long gamma at
    price, zero-gamma level between the walls and BELOW price."""
    c = _chain([90, 95, 100, 105, 110], [0, 0, 0, 500, 1000], [1000, 500, 0, 0, 0])
    p = core.dealer_gamma_profile(**c, lot=1, fut=103.0, dte=20, exp_move=5.0)
    assert p["profile_regime"] == "positive"
    assert 95 < p["profile_flip"] < 103
    assert p["profile_fnd"] < 0
    assert p["gamma_position"] > 0
    assert p["profile_n_zeros"] == 1


def test_profile_regime_consistent_with_flip_side():
    """Price below the zero-gamma level -> negative regime, flip ABOVE price."""
    c = _chain([90, 95, 100, 105, 110], [0, 0, 0, 500, 1000], [1000, 500, 0, 0, 0])
    p = core.dealer_gamma_profile(**c, lot=1, fut=96.0, dte=20, exp_move=5.0)
    assert p["profile_regime"] == "negative"
    assert p["profile_flip"] > 96.0 and p["gamma_position"] < 0


def test_profile_calls_only_is_all_positive():
    c = _chain([100, 105, 110], [800, 900, 700], [0, 0, 0])
    p = core.dealer_gamma_profile(**c, lot=1, fut=104.0, dte=20, exp_move=5.0)
    assert p["profile_regime"] == "all_positive"
    assert p["profile_flip"] is None and p["profile_n_zeros"] == 0
    assert p["gamma_position"] > 0


def test_profile_lot_scales_vanna_not_flip():
    c = _chain([90, 95, 100, 105, 110], [0, 0, 0, 500, 1000], [1000, 500, 0, 0, 0])
    a = core.dealer_gamma_profile(**c, lot=1, fut=103.0, dte=20, exp_move=5.0)
    b = core.dealer_gamma_profile(**c, lot=250, fut=103.0, dte=20, exp_move=5.0)
    assert a["profile_flip"] == b["profile_flip"]
    assert b["dealer_vanna"] == pytest.approx(250 * a["dealer_vanna"], rel=1e-3)   # a is rounded to 2 dp


def test_dealer_vanna_wings_add_not_cancel():
    """Dealer short OTM puts and long OTM calls: BOTH wings carry positive dealer
    vanna (an IV rise makes the dealer longer delta on each side), so the net
    is not a cancellation of the two. ce/pe_vanna (no dealer sign) DO cancel."""
    c = _chain([80, 90, 100, 110, 120], [0, 0, 0, 1000, 1000], [1000, 1000, 0, 0, 0])
    p = core.dealer_gamma_profile(**c, lot=1, fut=100.0, dte=20, exp_move=5.0)
    assert p["dealer_vanna_put_wing"] > 0
    assert p["dealer_vanna_call_wing"] > 0
    assert p["dealer_vanna"] == pytest.approx(
        p["dealer_vanna_put_wing"] + p["dealer_vanna_atm"] + p["dealer_vanna_call_wing"], rel=1e-6)
    assert 0 < p["dealer_vanna_norm"] <= 1


def test_profile_iv_fallback_to_other_leg():
    c = _chain([90, 95, 100, 105, 110], [0, 0, 0, 500, 1000], [1000, 500, 0, 0, 0])
    base = core.dealer_gamma_profile(**c, lot=1, fut=103.0, dte=20, exp_move=5.0)
    c2 = dict(c); c2["ce_iv"] = np.where(c["ce_oi"] > 0, 25.0, 0.0)   # untraded CE legs: no IV
    alt = core.dealer_gamma_profile(**c2, lot=1, fut=103.0, dte=20, exp_move=5.0)
    assert alt["profile_flip"] == pytest.approx(base["profile_flip"])


def test_profile_unusable_inputs_return_none():
    c = _chain([100, 105], [0, 0], [0, 0])
    p = core.dealer_gamma_profile(**c, lot=1, fut=102.0, dte=20, exp_move=5.0)
    assert all(v is None for v in p.values())
    p = core.dealer_gamma_profile(**_chain([100, 105], [10, 10], [10, 10]),
                                  lot=1, fut=102.0, dte=20, exp_move=0.0)
    assert all(v is None for v in p.values())


def test_strike_quality_mask_traded_or_tight():
    m = core.strike_quality_mask(ce_vol=[0, 5, 0, 0], pe_vol=[0, 0, 0, 0],
                                 ce_baq_pct=[50, 50, 8, np.nan],
                                 pe_baq_pct=[50, 50, 50, np.inf])
    assert m.tolist() == [False, True, True, False]


def test_profile_from_curves_identical_expiries_match_single():
    """Two identical expiries summed must give the single-expiry flip/regime;
    no curves -> all None."""
    c = _chain([90, 95, 100, 105, 110], [0, 0, 0, 500, 1000], [1000, 500, 0, 0, 0])
    one = core.dealer_gamma_profile(**c, lot=1, fut=103.0, dte=20, exp_move=5.0,
                                    return_curve=True)
    both = core.profile_from_curves([one["_curve"], one["_curve"]], 103.0, 5.0)
    assert both["profile_flip"] == pytest.approx(one["profile_flip"], abs=0.02)
    assert both["profile_regime"] == one["profile_regime"]
    assert both["gamma_position"] == pytest.approx(one["gamma_position"], abs=1e-3)
    assert core.profile_from_curves([], 103.0, 5.0)["profile_flip"] is None


def test_return_curve_off_by_default():
    """Batch pipelines insert dict keys by column name: no _curve unless asked."""
    c = _chain([90, 95, 100, 105, 110], [0, 0, 0, 500, 1000], [1000, 500, 0, 0, 0])
    assert "_curve" not in core.dealer_gamma_profile(**c, lot=1, fut=103.0, dte=20, exp_move=5.0)
