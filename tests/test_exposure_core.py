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

def test_signal_crash_risk():
    prev = {"gex_regime": "negative"}
    curr = {"gex_regime": "negative", "iv_change": 1.5, "pe_vanna": -500,
            "expected_move": 800}
    sigs, _ = core.derive_signals(curr, prev)
    assert "crash_risk" in sigs

def test_signal_no_crash_without_rising_iv():
    prev = {"gex_regime": "negative"}
    curr = {"gex_regime": "negative", "iv_change": -1.0, "pe_vanna": -500,
            "expected_move": 800}
    sigs, _ = core.derive_signals(curr, prev)
    assert "crash_risk" not in sigs

def test_signal_bull_trend_reinforce():
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "positive", "ce_vanna": 500, "iv_change": 1.0,
            "expected_move": 800}
    sigs, _ = core.derive_signals(curr, prev)
    keys = sigs.split(",")
    assert "bull_trend_reinforce" in keys
    assert "trend_reinforce" not in keys   # old standalone name gone

def test_signal_bear_trend_reinforce():
    # negative gamma + negative PE vanna + FALLING IV → orderly bearish
    prev = {"gex_regime": "negative"}
    curr = {"gex_regime": "negative", "pe_vanna": -500, "iv_change": -1.0,
            "expected_move": 800}
    sigs, _ = core.derive_signals(curr, prev)
    assert "bear_trend_reinforce" in sigs
    assert "crash_risk" not in sigs        # falling IV → not crash

def test_bear_reinforce_and_crash_mutually_exclusive():
    # rising IV → crash_risk (not bear_reinforce); falling IV → bear_reinforce
    prev = {"gex_regime": "negative"}
    base = {"gex_regime": "negative", "pe_vanna": -500, "expected_move": 800}
    up = core.derive_signals({**base, "iv_change": 1.0}, prev)[0]
    dn = core.derive_signals({**base, "iv_change": -1.0}, prev)[0]
    assert "crash_risk" in up and "bear_trend_reinforce" not in up
    assert "bear_trend_reinforce" in dn and "crash_risk" not in dn

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
    """prev/curr where every axis improves → score +6."""
    prev = {"net_gex_norm": 0.2, "neg_gamma_fraction": 0.4, "iv_change": None,
            "atm_iv_smoothed": 25, "flip_norm_distance": 0.5,
            "gex_regime": "positive", "transition_width_norm": 0.6}
    curr = {"net_gex_norm": 0.5, "neg_gamma_fraction": 0.3, "iv_change": -1.0,
            "flip_norm_distance": 0.9, "gex_regime": "positive",
            "transition_width_norm": 0.5}
    return prev, curr

def test_strength_score_max_positive():
    prev, curr = _strong_pair()
    assert core.strength_score(curr, prev) == 6

def test_strength_score_max_negative():
    # mirror: every axis worsens → -6
    prev = {"net_gex_norm": 0.5, "neg_gamma_fraction": 0.3, "iv_change": None,
            "atm_iv_smoothed": 20, "flip_norm_distance": 0.9,
            "gex_regime": "positive", "transition_width_norm": 0.5}
    curr = {"net_gex_norm": 0.2, "neg_gamma_fraction": 0.4, "iv_change": 1.0,
            "flip_norm_distance": 0.5, "gex_regime": "negative",
            "transition_width_norm": 0.6}
    assert core.strength_score(curr, prev) == -6

def test_strength_score_range_bounded():
    prev, curr = _strong_pair()
    s = core.strength_score(curr, prev)
    assert -6 <= s <= 6

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
    assert ser[1]["strength_score"] == 6
    assert ser[1]["strength_cumulative"] == 6
    # originals untouched (returns new dicts)
    assert "strength_score" not in prev

def test_strength_series_oscillating_nets_near_zero():
    # noisy regime: alternating improve/worsen → cumulative stays small
    a = {"net_gex_norm": 0.2, "gex_regime": "positive"}
    b = {"net_gex_norm": 0.1, "gex_regime": "negative"}  # worsens
    c = {"net_gex_norm": 0.2, "gex_regime": "positive"}  # improves back
    ser = core.strength_series([a, b, c])
    assert abs(ser[-1]["strength_cumulative"]) <= 2
