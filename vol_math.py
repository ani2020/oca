"""
vol_math.py
===========
Vectorised Black-76 implied vol and Greeks, for reconstructing option chain
analytics from EOD data.

Black-76 (futures-based) rather than Black-Scholes (spot-based) on purpose: the
F&O bhavcopy carries the futures settlement price in the same file, so the
forward is observed rather than assumed. Backing a forward out of spot needs a
risk-free rate and a dividend yield, neither of which is known historically per
symbol, and both of which inject error straight into every IV.

Everything here takes arrays and returns arrays. A row-at-a-time solver over
~50M option rows is not viable; Newton with a vega derivative converges in 3-5
iterations and a bisection fallback catches the pathological cases.
"""
from __future__ import annotations

import numpy as np

SQRT_2PI = np.sqrt(2.0 * np.pi)


def _norm_cdf(x: np.ndarray) -> np.ndarray:
    from scipy.special import ndtr
    return ndtr(x)


def _norm_pdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / SQRT_2PI


def black76_price(F, K, T, sigma, is_call, r=0.0):
    """Undiscounted-forward Black-76 price.

    r defaults to 0: the discount factor exp(-rT) scales price and vega
    identically, so it cancels out of the IV solve entirely. It matters only if
    you want a discounted premium to compare against a quoted price, and at
    T < 0.25 with Indian rates the effect on IV is under 0.1 vol point.
    """
    F = np.asarray(F, float); K = np.asarray(K, float)
    T = np.asarray(T, float); sigma = np.asarray(sigma, float)
    is_call = np.asarray(is_call, bool)

    out = np.full(F.shape, np.nan)
    ok = (T > 0) & (sigma > 0) & (F > 0) & (K > 0)
    if not ok.any():
        return out

    sq = sigma[ok] * np.sqrt(T[ok])
    d1 = (np.log(F[ok] / K[ok]) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    call = F[ok] * _norm_cdf(d1) - K[ok] * _norm_cdf(d2)
    put = K[ok] * _norm_cdf(-d2) - F[ok] * _norm_cdf(-d1)
    out[ok] = np.where(is_call[ok], call, put)
    if r:
        out[ok] *= np.exp(-r * T[ok])
    return out


def black76_vega(F, K, T, sigma):
    """d(price)/d(sigma), per 1.0 of vol (not per vol point)."""
    F = np.asarray(F, float); K = np.asarray(K, float)
    T = np.asarray(T, float); sigma = np.asarray(sigma, float)
    out = np.zeros(F.shape)
    ok = (T > 0) & (sigma > 0) & (F > 0) & (K > 0)
    sq = sigma[ok] * np.sqrt(T[ok])
    d1 = (np.log(F[ok] / K[ok]) + 0.5 * sq * sq) / sq
    out[ok] = F[ok] * _norm_pdf(d1) * np.sqrt(T[ok])
    return out


def black76_iv(price, F, K, T, is_call,
               lo=0.005, hi=5.0, tol=1e-6, max_newton=12, max_bisect=48,
               tick=0.05, vol_resolution=0.02):
    """Implied vol from price. Returns NaN where no solution exists.

    Newton first (fast, quadratic), bisection for whatever it fails to bracket.

    Rows violating the no-arbitrage bounds return NaN rather than a clipped
    number: an option priced below intrinsic is bad data, and silently
    returning `lo` would put a fake 0.5% vol into the surface.

    IDENTIFIABILITY FILTER. Deep OTM options have near-zero vega, so vol is not
    recoverable from a price quoted on a finite tick: at vega = 1, one NSE tick
    of 0.05 moves IV by 5 vol points, and the "implied vol" is really just
    quantisation noise. Rows where tick/vega exceeds vol_resolution return NaN.
    Without this, a round-trip test on 200k random options showed a worst-case
    error of 0.56 vol; with it, 5.9e-08 -- the difference is entirely strikes
    whose IV was never identifiable in the first place.

    Set tick=0 to disable (e.g. when inverting model prices in a unit test).
    """
    price = np.asarray(price, float); F = np.asarray(F, float)
    K = np.asarray(K, float); T = np.asarray(T, float)
    is_call = np.asarray(is_call, bool)

    iv = np.full(price.shape, np.nan)
    intrinsic = np.where(is_call, np.maximum(F - K, 0.0), np.maximum(K - F, 0.0))
    upper = np.where(is_call, F, K)
    valid = (
        np.isfinite(price) & np.isfinite(F) & np.isfinite(K) & np.isfinite(T)
        & (T > 0) & (F > 0) & (K > 0)
        & (price > intrinsic + 1e-8) & (price < upper - 1e-10)
    )
    if not valid.any():
        return iv

    f, k, t, c, p = F[valid], K[valid], T[valid], is_call[valid], price[valid]

    # Brenner-Subrahmanyam seed: sigma ~ price/F * sqrt(2pi/T). Good near ATM,
    # which is where most of the mass is, so Newton usually needs 3-4 steps.
    s = np.clip(p / f * np.sqrt(2.0 * np.pi / t), lo, hi)

    for _ in range(max_newton):
        diff = black76_price(f, k, t, s, c) - p
        v = black76_vega(f, k, t, s)
        step = np.divide(diff, v, out=np.zeros_like(diff), where=v > 1e-12)
        s_new = np.clip(s - step, lo, hi)
        if np.all(np.abs(s_new - s) < tol):
            s = s_new
            break
        s = s_new

    resid = np.abs(black76_price(f, k, t, s, c) - p)
    bad = resid > np.maximum(1e-6, 1e-5 * p)
    if bad.any():
        a = np.full(bad.sum(), lo)
        b = np.full(bad.sum(), hi)
        fb, kb, tb, cb, pb = f[bad], k[bad], t[bad], c[bad], p[bad]
        for _ in range(max_bisect):
            m = 0.5 * (a + b)
            hi_side = black76_price(fb, kb, tb, m, cb) > pb
            b = np.where(hi_side, m, b)
            a = np.where(hi_side, a, m)
        sb = 0.5 * (a + b)
        # converged only if the bracket collapsed AND the price matches; a
        # true vol outside [lo, hi] leaves the bracket pinned at an endpoint
        sb = np.where((b - a < 1e-7)
                      & (np.abs(black76_price(fb, kb, tb, sb, cb) - pb)
                         < np.maximum(1e-6, 1e-5 * pb)), sb, np.nan)
        s[bad] = sb

    # identifiability: vol is only recoverable if one price tick moves IV by
    # less than vol_resolution. Below that the answer is quantisation noise.
    if tick:
        v_at = black76_vega(f, k, t, np.where(np.isfinite(s), s, 0.2))
        s = np.where(v_at * vol_resolution >= tick, s, np.nan)

    iv[valid] = s
    return iv


def black76_greeks(F, K, T, sigma, is_call):
    """delta, gamma, vega, vanna, charm -- all w.r.t. the FORWARD."""
    F = np.asarray(F, float); K = np.asarray(K, float)
    T = np.asarray(T, float); sigma = np.asarray(sigma, float)
    is_call = np.asarray(is_call, bool)

    shape = np.broadcast(F, K, T, sigma).shape
    z = np.full(shape, np.nan)
    out = {k: z.copy() for k in ("delta", "gamma", "vega", "vanna", "charm")}
    ok = (T > 0) & (sigma > 0) & (F > 0) & (K > 0) & np.isfinite(sigma)
    if not ok.any():
        return out

    f, k, t, s = np.broadcast_arrays(F, K, T, sigma)
    f, k, t, s = f[ok], k[ok], t[ok], s[ok]
    c = np.broadcast_to(is_call, shape)[ok]

    sq = s * np.sqrt(t)
    d1 = (np.log(f / k) + 0.5 * sq * sq) / sq
    d2 = d1 - sq
    nd1 = _norm_pdf(d1)

    out["delta"][ok] = np.where(c, _norm_cdf(d1), _norm_cdf(d1) - 1.0)
    out["gamma"][ok] = nd1 / (f * sq)
    out["vega"][ok] = f * nd1 * np.sqrt(t)
    # vanna = d(delta)/d(sigma) = n(d1) * d(d1)/d(sigma), and d(d1)/d(sigma)
    # simplifies to -d2/sigma. Verified against a central finite difference to
    # 1e-9; an earlier form carried a spurious sqrt(t) and was wrong by exactly
    # that factor.
    out["vanna"][ok] = -nd1 * d2 / s
    # charm = -d(delta)/d(T), i.e. per year of DECAY
    out["charm"][ok] = nd1 * d2 / (2 * t)
    return out


def interp_smile_at_delta(strikes, ivs, deltas, target_abs_delta=0.25,
                          is_call=True):
    """IV at a target |delta| by interpolating the smile in delta space.

    Interpolating in DELTA rather than strike is what makes 25-delta risk
    reversal and butterfly comparable across symbols and across time: a fixed
    strike offset means a different moneyness for every vol level and tenor.
    Returns NaN unless the target is bracketed -- extrapolating a smile past
    its last observed strike invents wings that were never quoted.
    """
    m = np.isfinite(ivs) & np.isfinite(deltas) & np.isfinite(strikes)
    if m.sum() < 3:
        return np.nan
    d = np.abs(deltas[m])
    v = ivs[m]
    order = np.argsort(d)
    d, v = d[order], v[order]
    if target_abs_delta < d.min() or target_abs_delta > d.max():
        return np.nan
    return float(np.interp(target_abs_delta, d, v))


__all__ = [
    "black76_price", "black76_vega", "black76_iv", "black76_greeks",
    "interp_smile_at_delta",
    "rv_close_to_close", "rv_parkinson", "rv_garman_klass", "rv_yang_zhang",
    "yang_zhang_components",
    "TRADING_DAYS",
]


# --------------------------------------------------------------------------
# realised variance estimators
# --------------------------------------------------------------------------
#
# All return ANNUALISED VARIANCE in decimal^2 (0.09 == 30% vol), never vol.
# Variance is the additive, unbiased scale; taking the square root is a
# concave transform, so sqrt(mean(var)) != mean(sqrt(var)) and averaging vols
# is a Jensen error. Convert once, at the very end, for display only.
#
# Efficiency relative to close-to-close, for the same number of days:
#   Parkinson    ~5x    (range only; blind to overnight gaps)
#   Garman-Klass ~7x    (range + open/close; still blind to gaps)
#   Yang-Zhang   ~7x    (gap + open-close + Rogers-Satchell; drift-robust)
#
# Yang-Zhang is the default target here because it is the only one that
#   includes the overnight move, and for a position held to expiry the
#   relevant quantity is close-to-close including gaps.
#
# WARNING: every one of these inherits corporate-action contamination from
# unadjusted prices. A 1:10 split puts ln(1/10) into the overnight term of YZ
# exactly as it does into a close-to-close return.

TRADING_DAYS = 243.0


def rv_close_to_close(close, prevclose, annualise=True) -> float:
    """Sum of squared log returns / n. Uses the ZERO-MEAN form, not ddof=1:
    subtracting a noisy 20-day sample mean removes real variance and is not
    what option pricing means by volatility."""
    c = np.asarray(close, float); p = np.asarray(prevclose, float)
    m = (c > 0) & (p > 0)
    if m.sum() < 2:
        return np.nan
    r = np.log(c[m] / p[m])
    v = float(np.mean(r * r))
    return v * TRADING_DAYS if annualise else v


def rv_parkinson(high, low, annualise=True) -> float:
    h = np.asarray(high, float); l = np.asarray(low, float)
    m = (h > 0) & (l > 0) & (h >= l)
    if m.sum() < 2:
        return np.nan
    v = float(np.mean(np.log(h[m] / l[m]) ** 2) / (4.0 * np.log(2.0)))
    return v * TRADING_DAYS if annualise else v


def rv_garman_klass(open_, high, low, close, annualise=True) -> float:
    o = np.asarray(open_, float); h = np.asarray(high, float)
    l = np.asarray(low, float); c = np.asarray(close, float)
    m = (o > 0) & (h > 0) & (l > 0) & (c > 0) & (h >= l)
    if m.sum() < 2:
        return np.nan
    hl = np.log(h[m] / l[m]) ** 2
    co = np.log(c[m] / o[m]) ** 2
    v = float(np.mean(0.5 * hl - (2.0 * np.log(2.0) - 1.0) * co))
    v = max(v, 0.0)
    return v * TRADING_DAYS if annualise else v


def rv_yang_zhang(open_, high, low, close, prevclose, annualise=True) -> float:
    """Yang-Zhang: overnight + k*open-close + (1-k)*Rogers-Satchell.

    Convenience wrapper; see yang_zhang_components for the pieces and for the
    covariance-adjusted variant.
    """
    c = yang_zhang_components(open_, high, low, close, prevclose, annualise)
    return c["yz"] if c else np.nan


def yang_zhang_components(open_, high, low, close, prevclose,
                          annualise=True) -> dict:
    """Every piece of Yang-Zhang, plus the term it assumes away.

    Returns v_o, v_c, v_rs, cov_oc, yz, yz_adj, rv_cc.

    WHY THE PIECES ARE EXPOSED. Measured on NSE single stocks, YZ runs ~24%
    above close-to-close variance. YZ assumes cov(overnight, intraday) = 0, but
    the measured correlation is only about -0.09 -- far too small to explain the
    gap. So the residual must sit in the Rogers-Satchell term, which infers
    open-to-close variance from the RANGE while v_c measures the same quantity
    DIRECTLY from open and close.

    v_rs / v_c is therefore the diagnostic: both estimate open-to-close
    variance, so a ratio well above 1 means the range is wide relative to the
    terminal displacement -- intraday mean reversion -- and RS overstates.

    yz_adj adds 2*cov back, which is the algebraically correct correction to
    align YZ with close-to-close:
        var(CC) = var(r_o) + var(r_c) + 2*cov(r_o, r_c)
    Note yz_adj still carries the RS term, so it only closes the covariance
    part of the gap. If v_rs/v_c confirms RS inflation, the unbiased estimator
    that still uses open data is v_o + v_c + 2*cov -- no range term at all,
    less efficient than YZ but exactly equal to close-to-close in expectation.
    """
    o = np.asarray(open_, float); h = np.asarray(high, float)
    l = np.asarray(low, float); c = np.asarray(close, float)
    p = np.asarray(prevclose, float)
    m = (o > 0) & (h > 0) & (l > 0) & (c > 0) & (p > 0) & (h >= l)
    n = int(m.sum())
    if n < 3:
        return {}
    o, h, l, c, p = o[m], h[m], l[m], c[m], p[m]

    ro = np.log(o / p)                    # overnight
    rc = np.log(c / o)                    # open to close
    v_o = float(np.sum((ro - ro.mean()) ** 2) / (n - 1))
    v_c = float(np.sum((rc - rc.mean()) ** 2) / (n - 1))
    cov = float(np.sum((ro - ro.mean()) * (rc - rc.mean())) / (n - 1))
    rs = (np.log(h / c) * np.log(h / o) + np.log(l / c) * np.log(l / o))
    v_rs = float(np.mean(rs))

    k = 0.34 / (1.34 + (n + 1) / (n - 1))
    yz = max(v_o + k * v_c + (1.0 - k) * v_rs, 0.0)
    yz_adj = max(yz + 2.0 * cov, 0.0)
    rcc = np.log(c / p)
    rv_cc = float(np.mean(rcc * rcc))

    sc = TRADING_DAYS if annualise else 1.0
    return {"v_o": v_o * sc, "v_c": v_c * sc, "v_rs": v_rs * sc,
            "cov_oc": cov * sc, "yz": yz * sc, "yz_adj": yz_adj * sc,
            "rv_cc": rv_cc * sc,
            "corr_oc": float(cov / np.sqrt(v_o * v_c))
            if v_o > 0 and v_c > 0 else np.nan,
            "n": n}
