"""Unit tests for exposure_core pure math. Fast, deterministic, no DB/app deps."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

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
    # 23400 × 0.15 × sqrt(23/365) ≈ 881
    em = core.expected_move(23400, 15.0, 23)
    assert 870 < em < 890

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

def test_signal_drift_threshold():
    # velocity below 15% of expected move → no drift
    prev = {"gex_regime": "positive"}
    curr = {"gex_regime": "positive", "flip_velocity": 50, "expected_move": 800}
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" not in sigs   # 50 < 120
    # above threshold → fires
    curr["flip_velocity"] = 200
    sigs, _ = core.derive_signals(curr, prev)
    assert "flip_drift_up" in sigs

def test_signal_no_prev_returns_empty():
    sigs, active = core.derive_signals({"gex_regime": "positive"}, None)
    assert sigs == "" and active == "positive"


# ── confidence ─────────────────────────────────────────────────────
def test_confidence_levels():
    assert core.confidence(1000, 10, 0.3) == "high"
    assert core.confidence(1000, 5, 0.8) == "medium"
    assert core.confidence(0, 2, None) == "low"
