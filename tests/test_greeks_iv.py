"""Regression tests for greeks.py IV solving.

Guards the py_vollib Black-76 argument-order bug fixed on 2026-08-18:

    py_vollib.black.implied_volatility(price, F, K, r, t, flag)   <- r BEFORE t
    py_vollib.black_scholes.implied_volatility(price, S, K, t, r, flag)
    py_vollib.black.greeks.analytical.*(flag, F, K, t, r, sigma)

The Black (futures) IV solver is the only function in the package taking r
before t. Passing (t, r) does NOT raise - the discount factor stays ~1 so
every real quote remains inside the solver's feasible band - it silently
solves at a fixed t = r = 0.10 yr (36.5 days) regardless of true DTE. The
returned sigma is therefore the true one rescaled by sqrt(T_true / 0.10):
near-expiry IV understated ~2x, far-expiry overstated. Live 2026-04-13 to
2026-08-18; remediated by backfill_iv_greeks.py.

The DTE-invariance test below is the real guard: any silent substitution of
a constant T shows up as IV varying with DTE for the same true vol.

Run:  pytest oc/tests/test_greeks_iv.py -v
"""
import pathlib
import sys

import pytest

# greeks.py lives at the repo root (od\claude), two levels up from oc/tests
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import greeks as greeks_mod  # noqa: E402
from greeks import Greeks  # noqa: E402


@pytest.fixture(scope="module")
def g():
    return Greeks(risk_free_rate=10.0, prefer_vollib=True)


def test_vollib_backend_available():
    """These tests are meaningless against the Brent fallback."""
    assert greeks_mod.HAS_VOLLIB, "py_vollib not installed - install it or these tests prove nothing"


# ── canary: a real production row ───────────────────────────────────
# NIFTY 24200 CE, 2026-08-18 EOD: F=24216.5, LTP=133.05, 7 DTE.
# Buggy (t, r) order -> 4.0891 ; correct (r, t) order -> 9.3374 ; NSE published 9.92.
def test_canary_nifty_24200ce(g):
    iv = g.calculate_implied_volatility(133.05, 24216.5, 24200.0, 7 / 365, "call")
    assert iv is not None
    assert iv == pytest.approx(9.3374, abs=1e-3), (
        f"got {iv} - 4.089 means the (t, r) argument swap is back in greeks.py"
    )


def test_canary_not_the_buggy_value(g):
    """Explicit lower bound: the buggy value is ~4.09, guard against regressing to it."""
    iv = g.calculate_implied_volatility(133.05, 24216.5, 24200.0, 7 / 365, "call")
    assert iv > 6.0


# ── round-trip: price at known sigma, recover sigma ─────────────────
@pytest.mark.parametrize("dte", [1, 3, 7, 14, 30, 45, 90, 180])
@pytest.mark.parametrize("sigma_pct", [8.0, 15.0, 32.0, 55.0])
@pytest.mark.parametrize("flag", ["call", "put"])
def test_iv_roundtrip(g, dte, sigma_pct, flag):
    """Black-76 price at sigma -> IV solve must return sigma, at every DTE."""
    from py_vollib.black import black

    F, K, r = 24000.0, 24000.0, 0.10
    t = dte / 365.0
    price = black("c" if flag == "call" else "p", F, K, t, r, sigma_pct / 100.0)
    iv = g.calculate_implied_volatility(price, F, K, t, flag)
    assert iv == pytest.approx(sigma_pct, rel=1e-4), f"dte={dte} flag={flag}"


def test_iv_invariant_to_dte(g):
    """THE structural guard.

    Price eight different maturities at the SAME 20% vol, then solve each.
    All eight must return 20%. Under the arg swap they instead scale as
    sqrt(dte/36.5) - i.e. 3.3% at 1 DTE up to 44% at 180 DTE.
    """
    from py_vollib.black import black

    F, K, r, sigma = 24000.0, 24000.0, 0.10, 0.20
    ivs = []
    for dte in (1, 3, 7, 14, 30, 45, 90, 180):
        t = dte / 365.0
        ivs.append(g.calculate_implied_volatility(black("c", F, K, t, r, sigma), F, K, t, "call"))
    assert max(ivs) - min(ivs) < 0.01, f"IV varies with DTE - constant-T bug: {ivs}"
    assert all(iv == pytest.approx(20.0, rel=1e-4) for iv in ivs)


# ── greeks consistency with the solved IV ───────────────────────────
def test_delta_sane_at_short_dte(g):
    """NIFTY 24500 CE at 7 DTE was reported as 0.02 delta under the bug; true ~0.19.

    A short-dated 1.4%-OTM call on an 9.4-vol index is not a 2-delta option.
    """
    iv = g.calculate_implied_volatility(32.45, 24216.5, 24500.0, 7 / 365, "call")
    res = g.calculate_greeks(spot_price=24154.9, fut_price=24216.5, strike_price=24500.0,
                             time_to_expiry_days=7, implied_volatility=iv, option_type="call")
    assert 0.10 < res["delta"] < 0.30, f"delta={res['delta']} iv={iv}"


def test_put_call_delta_parity(g):
    """Black-76: delta_c - delta_p = exp(-rT), within rounding."""
    import math

    kw = dict(spot_price=24154.9, fut_price=24216.5, strike_price=24200.0,
              time_to_expiry_days=7, implied_volatility=9.5)
    c = g.calculate_greeks(option_type="call", **kw)
    p = g.calculate_greeks(option_type="put", **kw)
    assert c["delta"] - p["delta"] == pytest.approx(math.exp(-0.10 * 7 / 365), abs=1e-4)
    assert c["gamma"] == pytest.approx(p["gamma"], rel=1e-6)


def test_charm_per_calendar_day(g):
    """charm is dDelta/dt per CALENDAR day (/365), matching theta's basis.

    Was /252 (NYSE count) - wrong exchange and inconsistent with theta.
    Cross-check: recompute the annual figure and divide by 365.
    """
    import math

    from scipy.stats import norm

    F, K, T, r, sigma = 24216.5, 24200.0, 7 / 365, 0.10, 0.095
    res = g.calculate_greeks(spot_price=24154.9, fut_price=F, strike_price=K,
                             time_to_expiry_days=7, implied_volatility=sigma * 100,
                             option_type="call")
    sqT = math.sqrt(T)
    d1 = (math.log(F / K) + (r + 0.5 * sigma ** 2) * T) / (sigma * sqT)
    d2 = d1 - sigma * sqT
    charm_annual = norm.cdf(d1) - norm.pdf(d1) * (2 * r * T - d2 * sigma * sqT) / (2 * T * sigma * sqT)
    assert res["charm"] == pytest.approx(charm_annual / 365.0, abs=1e-4)


# ── degenerate inputs must not raise ────────────────────────────────
@pytest.mark.parametrize("price,F,K,t", [
    (0.0, 24216.5, 24200.0, 7 / 365),      # no quote
    (133.05, 24216.5, 24200.0, 0.0),       # expired
    (133.05, 24216.5, 24200.0, -1.0),      # negative T
    (0.05, 24216.5, 30000.0, 1 / 365),     # far OTM, 1 day
    (2089.9, 36270.0, 38500.0, 7 / 365),   # deep ITM put, wide book
])
def test_degenerate_inputs_return_none_or_positive(g, price, F, K, t):
    iv = g.calculate_implied_volatility(price, F, K, t, "call")
    assert iv is None or iv > 0
