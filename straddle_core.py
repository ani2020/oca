"""
straddle_core.py
================
Pure-computation core for the straddle / strangle volatility screener.

No FastAPI, no DuckDB, no I/O. Everything here takes arrays and scalars and
returns numbers, so it can be unit-tested without a database and reused by
both the nightly precompute job and the live "recompute" endpoint.

Design notes that are easy to get wrong and are therefore enforced here
-----------------------------------------------------------------------

1. FORWARD VARIANCE IS AGGREGATED, NOT SCALED.
   sigma_1 * sqrt(h) is wrong whenever current vol differs from the long-run
   level, which is exactly the regime a screener fires in. We sum the h-step
   GARCH variance forecast path instead.

2. THE MONTE CARLO UPDATES SIGMA INSIDE THE PATH.
   Sampling standardised residuals and scaling them all by a constant sigma_0
   destroys volatility clustering; the h-day sum then converges to Gaussian by
   CLT and the tail -- the only part that matters for a short straddle -- is
   too thin. This is filtered historical simulation (Barone-Adesi / Engle):
   resample z, run the GARCH recursion forward, rescale each step by sigma_t.

3. EXIT RULE LIVES INSIDE THE SIMULATION.
   P&L is read off the path under the actual exit policy (EM-distance stop),
   not off the terminal distribution. A hold-to-expiry distribution answers a
   question we do not trade.

4. CREDIT MULTIPLE STOPS ARE NOT PORTABLE ACROSS STRUCTURES.
   A k*credit stop on a naked leg and on an ATM straddle fire at completely
   different move sizes (for an ATM straddle, k=3 needs ~2.3 sigma*sqrt(T),
   effectively never). The stop here is expressed in EXPECTED-MOVE DISTANCE,
   which is structure-independent. See stop_k_em.

5. DAY COUNT IS 243, NOT 252 OR 365.
   Empirically measured for NSE. Calendar days are used only where the
   instrument itself is calendar-dated (theta/charm), never for vol scaling.

6. POP IS A DIAGNOSTIC, NEVER A RANKING INPUT.
   For an ATM straddle POP is ~57.5% structurally and RISES with fat tails,
   so it is highest exactly where losses are worst. It is reported because it
   is asked for, and excluded from the ranking on purpose.
"""
from __future__ import annotations

import math
import warnings
from dataclasses import dataclass, asdict
from typing import Literal, Optional, Sequence

import numpy as np
import pandas as pd

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

TRADING_DAYS = 243.0          # NSE, empirically measured
CALENDAR_DAYS = 365.0
MAD_TO_SD = 1.4826            # consistency constant for a normal
SQRT_2_OVER_PI = math.sqrt(2.0 / math.pi)   # 0.79788 -- E|Z| for a standard normal

# NSE / SEBI charge schedule. Named constants rather than literals so a rate
# change is a one-line edit and the backtest can be re-run on old rates.
STT_SELL_PREMIUM = 0.0010     # 0.10% of premium, on the SELL leg only
STT_EXERCISE_ITM = 0.00125    # 0.125% of INTRINSIC settlement value, on exercise
EXCH_TXN_PCT = 0.0003503      # NSE F&O options transaction charge, on premium
SEBI_TURNOVER_PCT = 0.000001  # 0.0001%
STAMP_DUTY_PCT = 0.00003      # 0.003%, buy side only
GST_RATE = 0.18               # on (brokerage + exchange + SEBI)
BROKERAGE_PER_ORDER = 20.0    # flat, typical discount broker

# Straddle/strangle SPAN behaves closer to "worse leg" than "sum of legs",
# because only one side can finish ITM. Applied to the larger single-leg
# margin. Relative ordering is what matters, not the rupee.
COMBO_MARGIN_MULT = 1.15

# --------------------------------------------------------------------------
# bid-ask spread model
# --------------------------------------------------------------------------
#
# Calibrated on 37,868 ATM straddle quotes from ocdata (2026), the only period
# with real bid/ask. options_eod carries no quotes, so every backtest before
# this charged ZERO spread -- which flattered thin chains most and produced two
# false conclusions: that `liquidity` had a NEGATIVE information coefficient
# (illiquid names "outperformed" because their costs were the ones omitted),
# and that 45-DTE straddles were the best tenor.
#
# Entry half-spread as a FRACTION OF GROSS CREDIT, by volume quintile x DTE
# band. Volume is the dominant axis -- a 17x range from thinnest to deepest
# quintile, against 1.2x for traded_frac, which is why traded_frac was useless
# as a liquidity measure. DTE adds a U-shape on top: the spread is roughly
# fixed in rupees while credit shrinks with sqrt(T), so very short tenors are
# expensive again despite being liquid.
#
# MEAN, not median. You pay the realised spread on every trade, so the
# expectation is the right statistic; the distribution is right-skewed and the
# median understates the cost by roughly a third.
SPREAD_VOL_BREAKS = (308.0, 3213.0, 10534.0, 28989.0)   # quintile edges, contracts
SPREAD_DTE_BREAKS = (17, 27, 37)                        # band edges, calendar days

#: [volume quintile 1..5][dte band 1..4] -> mean half-spread / gross credit
SPREAD_LUT = (
    (0.5877, 0.4461, 0.1508, 0.1767),   # q1  thinnest  (n=33/19/174/7354)
    (0.1109, 0.0521, 0.0631, 0.0823),   # q2            (n=351/464/1606/5147)
    (0.0518, 0.0250, 0.0311, 0.0336),   # q3            (n=2370/2248/1947/1008)
    (0.0318, 0.0154, 0.0184, 0.0319),   # q4            (n=3299/2569/1381/324)
    (0.0173, 0.0092, 0.0111, 0.0213),   # q5  deepest   (n=3747/2569/893/365)
)


def spread_fraction(volume: float, dte: float) -> float:
    """Entry half-spread as a fraction of gross credit.

    volume is total contracts traded across the in-range strikes
    (exposure_hist.total_vol_in_range), NOT traded_frac -- see the note above.

    Returns the thinnest-bucket cost when volume is missing, deliberately: an
    unknown chain is far more likely to be thin than deep, and under-charging
    is the failure mode that produced the wrong answers before.
    """
    if volume is None or not np.isfinite(volume) or volume < 0:
        vq = 0
    else:
        vq = sum(1 for b in SPREAD_VOL_BREAKS if volume > b)
    db = sum(1 for b in SPREAD_DTE_BREAKS if dte > b)
    return float(SPREAD_LUT[vq][db])


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def annualise_vol(daily_sigma: float, days: float = TRADING_DAYS) -> float:
    """Daily sigma (decimal) -> annualised vol in PERCENT."""
    return float(daily_sigma) * math.sqrt(days) * 100.0


def deannualise_vol(annual_pct: float, days: float = TRADING_DAYS) -> float:
    """Annualised vol in percent -> daily sigma in decimal."""
    return float(annual_pct) / 100.0 / math.sqrt(days)


def robust_z(x: float, history: Sequence[float]) -> float:
    """Median/MAD z-score. Robust to the odd corrupted IV row, which a
    mean/stdev z is not."""
    h = np.asarray([v for v in history if v is not None and np.isfinite(v)], dtype=float)
    if h.size < 8:
        return float("nan")
    med = float(np.median(h))
    mad = float(np.median(np.abs(h - med)))
    if mad <= 0:
        return float("nan")
    return float((x - med) / (MAD_TO_SD * mad))


def path_efficiency(returns: Sequence[float]) -> float:
    """|sum r| / sum|r| over the window. 1.0 = perfectly trending, 0 = pure
    round trip. A straddle is defeated by EFFICIENT moves, not merely large
    ones, and nothing else in the feature set sees this."""
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    denom = float(np.sum(np.abs(r)))
    if denom <= 0:
        return float("nan")
    return float(abs(np.sum(r)) / denom)


# --------------------------------------------------------------------------
# 1. forward realised variance
# --------------------------------------------------------------------------

@dataclass
class ForwardRV:
    """h-day-ahead variance forecast plus everything the simulator needs to
    propagate it. rv_annual_pct is the headline number; the rest is plumbing."""
    rv_annual_pct: float          # annualised vol over the whole h-day horizon
    var_path: np.ndarray          # (h,) per-day variance forecasts, decimal^2
    sigma_next: float             # sigma_{t+1}, decimal daily
    innovations: np.ndarray       # standardised residuals z_t for resampling
    omega: float
    alpha: float
    beta: float
    mu: float                     # fitted mean daily return (usually ~0)
    model: str
    n_obs: int
    converged: bool

    @property
    def total_variance(self) -> float:
        return float(np.sum(self.var_path))

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("var_path")
        d.pop("innovations")
        d["total_variance"] = self.total_variance
        return d


def forward_rv(
    returns: Sequence[float],
    horizon: int,
    model: Literal["garch11_t", "egarch_t", "ewma"] = "garch11_t",
    min_obs: int = 120,
) -> ForwardRV:
    """Fit a conditional-variance model and produce the AGGREGATED h-day
    forecast.

    `returns` are daily LOG returns in decimal (not percent, not annualised),
    CA-adjusted, oldest-first. Source these from bhavcopy.duckdb cash_eod with
    series='EQ' -- NOT from NSE m_volatility, which runs ~22% above ATM IV and
    has a floor.

    The returned rv_annual_pct is sqrt(sum of the h daily variance forecasts)
    annualised -- the vol that, applied over h days, reproduces the model's
    total expected variance. This is the number to compare with an h-day option
    IV. It is NOT sigma_1 * sqrt(h) and will differ materially whenever current
    vol is far from the long-run level.
    """
    r = np.asarray(returns, dtype=float)
    r = r[np.isfinite(r)]
    n = r.size
    if n < min_obs:
        raise ValueError(f"need >= {min_obs} returns, got {n}")
    if horizon < 1:
        raise ValueError("horizon must be >= 1")

    if model == "ewma":
        return _forward_rv_ewma(r, horizon)

    try:
        from arch import arch_model
    except ImportError:  # pragma: no cover
        warnings.warn("arch not installed; falling back to EWMA")
        return _forward_rv_ewma(r, horizon)

    # arch fits in percent units for numerical conditioning; convert back after.
    scale = 100.0
    y = r * scale

    vol = "GARCH" if model == "garch11_t" else "EGARCH"
    am = arch_model(y, mean="Constant", vol=vol, p=1, q=1, dist="t")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        res = am.fit(disp="off", show_warning=False)

    p = res.params
    omega = float(p.get("omega", np.nan)) / scale ** 2
    alpha = float(p.get("alpha[1]", np.nan))
    beta = float(p.get("beta[1]", np.nan))
    mu = float(p.get("mu", 0.0)) / scale

    fc = res.forecast(horizon=horizon, reindex=False)
    var_path = np.asarray(fc.variance.values[-1], dtype=float) / scale ** 2

    # Standardised residuals: the empirical innovation pool for FHS. These carry
    # the actual skew and kurtosis of this symbol, so no separate moment
    # estimation is needed (and doing it separately would double-count).
    z = np.asarray(res.std_resid, dtype=float)
    z = z[np.isfinite(z)]
    z = z - np.mean(z)          # centre; scale is already ~1 by construction

    total_var = float(np.sum(var_path))
    rv_daily = math.sqrt(total_var / horizon)
    sigma_next = math.sqrt(float(var_path[0]))

    return ForwardRV(
        rv_annual_pct=annualise_vol(rv_daily),
        var_path=var_path,
        sigma_next=sigma_next,
        innovations=z,
        omega=omega, alpha=alpha, beta=beta, mu=mu,
        model=model, n_obs=n,
        converged=bool(res.convergence_flag == 0),
    )


def _forward_rv_ewma(r: np.ndarray, horizon: int, lam: float = 0.94) -> ForwardRV:
    """RiskMetrics fallback. No mean reversion, so the h-day forecast IS flat --
    stated plainly rather than dressed up as a forecast path."""
    var = float(np.var(r, ddof=1))
    for x in r:
        var = lam * var + (1 - lam) * x * x
    var_path = np.full(horizon, var, dtype=float)
    sig = math.sqrt(var)
    z = r / sig
    return ForwardRV(
        rv_annual_pct=annualise_vol(sig), var_path=var_path, sigma_next=sig,
        innovations=z - z.mean(), omega=float("nan"), alpha=1 - lam, beta=lam,
        mu=0.0, model="ewma", n_obs=r.size, converged=True,
    )


def mincer_zarnowitz(forecast, realised, scale: str = "log_var",
                     min_n: int = 10) -> dict:
    """Regress realised on forecast. Want intercept 0, slope 1.

    THE CANONICAL DEFINITION -- do not reimplement elsewhere. rv_diagnostics
    once carried its own copy with a different n-threshold and a different
    input scale, which silently produced two answers to the same question.

    scale:
      "log_var" (default) -- regress ln(realised_var) on ln(forecast_var).
          Variance residuals are violently heteroskedastic, so a level
          regression is dominated by the high-vol tail. Shrinkage is then a
          POWER, not a multiplier:
              var_shrunk = exp(alpha) * var_forecast ** beta
          This form cannot go negative at low vol and compresses the high tail
          harder, both of which the linear form gets wrong.
      "var"  -- regress variance on variance. Linear shrinkage:
              var_shrunk = alpha + beta * var_forecast
      "vol"  -- regress vol on vol. Reported only for comparability with the
          older literature; shrinking vol then squaring is NOT the same
          estimator as shrinking variance, so do not mix them.

    Inputs are VARIANCE for "log_var"/"var" and VOL for "vol". Passing the
    wrong one is silent, so callers should convert once and stay there.
    """
    f = np.asarray(forecast, dtype=float)
    a = np.asarray(realised, dtype=float)
    m = np.isfinite(f) & np.isfinite(a) & (f > 0) & (a > 0)
    f, a = f[m], a[m]
    if f.size < min_n:
        return {"n": int(f.size), "alpha": float("nan"), "beta": float("nan"),
                "r2": float("nan"), "scale": scale}
    if scale == "log_var":
        x, y = np.log(f), np.log(a)
    else:
        x, y = f, a
    X = np.column_stack([np.ones_like(x), x])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    pred = X @ coef
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2))
    return {
        "n": int(f.size),
        "alpha": float(coef[0]),
        "beta": float(coef[1]),
        "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
        "scale": scale,
    }


def qlike(forecast_var: Sequence[float], realised_var: Sequence[float]) -> float:
    """QLIKE loss on VARIANCE. Lower is better. THE CANONICAL DEFINITION.

    Used instead of MSE because MSE is dominated by the few highest-vol
    observations and ranks forecast models differently (and worse). Inputs are
    variance, not vol -- passing vol gives a number that looks plausible and
    means nothing.
    """
    f = np.asarray(forecast_var, dtype=float)
    a = np.asarray(realised_var, dtype=float)
    m = np.isfinite(f) & np.isfinite(a) & (f > 0) & (a > 0)
    f, a = f[m], a[m]
    if f.size == 0:
        return float("nan")
    return float(np.mean(a / f - np.log(a / f) - 1.0))


def _cluster_meat(X: np.ndarray, e: np.ndarray, g: np.ndarray) -> np.ndarray:
    """Sum of outer products of within-cluster score sums."""
    k = X.shape[1]
    M = np.zeros((k, k))
    order = np.argsort(g, kind="stable")
    gs, Xs, es = g[order], X[order], e[order]
    edges = np.flatnonzero(np.r_[True, gs[1:] != gs[:-1], True])
    for a, b in zip(edges[:-1], edges[1:]):
        s = Xs[a:b].T @ es[a:b]
        M += np.outer(s, s)
    return M


def pooled_mz_clustered(forecast, realised, dates, symbols,
                        scale: str = "log_var") -> dict:
    """Pooled MZ regression with TWO-WAY CLUSTERED standard errors.

    THIS is the right estimator for forecast calibration, and Fama-MacBeth is
    not. FM fits a separate intercept per date, and that intercept absorbs
    exactly what MZ exists to measure: market-wide level bias. A forecast that
    is 20% too high on every symbol on every day is badly miscalibrated, yet FM
    reports beta ~ 1 because the daily alpha soaks the level up. FM is built for
    risk-premium estimation, where removing the common factor is the point.
    Calibration is the opposite requirement.

    Pooling preserves the absolute bias. The problem pooling creates -- every
    symbol loading the same index vol factor, so residuals are correlated
    within a date and within a symbol -- is handled in the VARIANCE, not by
    changing the estimator. Cameron-Gelbach-Miller two-way:

        V = V_date + V_symbol - V_(date,symbol)

    Clustering on symbol also permits arbitrary within-symbol serial
    correlation, which subsumes the overlapping-window problem entirely -- no
    Newey-West adjustment is needed on top.

    Also returns r2_within: R-squared after removing symbol means. Pooled R2 is
    inflated by cross-sectional level ("high-vol stocks have high vol"), which
    is not forecast skill. Use beta for calibration and r2_within for skill.
    """
    f = np.asarray(forecast, dtype=float)
    a = np.asarray(realised, dtype=float)
    d = np.asarray(dates)
    s = np.asarray(symbols)
    m = np.isfinite(f) & np.isfinite(a) & (f > 0) & (a > 0)
    f, a, d, s = f[m], a[m], d[m], s[m]
    n = f.size
    blank = {"n": int(n), "alpha": float("nan"), "beta": float("nan"),
             "se_beta": float("nan"), "t_beta_vs1": float("nan"),
             "se_alpha": float("nan"), "r2": float("nan"),
             "r2_within": float("nan"), "n_dates": 0, "n_symbols": 0,
             "scale": scale, "ok": False}
    if n < 30:
        return blank

    x, y = (np.log(f), np.log(a)) if scale == "log_var" else (f, a)
    X = np.column_stack([np.ones_like(x), x])
    XtX = X.T @ X
    try:
        XtX_inv = np.linalg.inv(XtX)
    except np.linalg.LinAlgError:
        return blank
    beta = XtX_inv @ (X.T @ y)
    e = y - X @ beta

    gd = pd.factorize(d)[0]
    gs = pd.factorize(s)[0]
    gds = pd.factorize(pd.Series(gd).astype(str) + "|" + pd.Series(gs).astype(str))[0]
    G_d, G_s = gd.max() + 1, gs.max() + 1
    k = X.shape[1]

    def sandwich(g, G):
        c = (G / max(G - 1, 1)) * ((n - 1) / max(n - k, 1))   # small-sample adj
        return c * (XtX_inv @ _cluster_meat(X, e, g) @ XtX_inv)

    V = (sandwich(gd, G_d) + sandwich(gs, G_s)
         - sandwich(gds, gds.max() + 1))
    var_b = float(V[1, 1])
    if not np.isfinite(var_b) or var_b <= 0:
        # CGM can go non-PSD; fall back to the larger one-way variance rather
        # than reporting a standard error that does not exist
        var_b = max(float(sandwich(gd, G_d)[1, 1]),
                    float(sandwich(gs, G_s)[1, 1]))
    se_b = float(np.sqrt(var_b)) if var_b > 0 else float("nan")
    var_a = float(V[0, 0])
    se_a = float(np.sqrt(var_a)) if var_a > 0 else float("nan")

    ss_res = float(e @ e)
    ss_tot = float(((y - y.mean()) ** 2).sum())
    # within-symbol: demean both sides by symbol, so only time-series variation
    dfw = pd.DataFrame({"y": y, "x": x, "s": gs})
    yw = dfw["y"] - dfw.groupby("s")["y"].transform("mean")
    xw = dfw["x"] - dfw.groupby("s")["x"].transform("mean")
    if float((xw ** 2).sum()) > 0:
        bw = float((xw * yw).sum() / (xw ** 2).sum())
        rw = yw - bw * xw
        r2w = 1.0 - float((rw ** 2).sum()) / float((yw ** 2).sum()) \
            if float((yw ** 2).sum()) > 0 else float("nan")
    else:
        r2w = float("nan")

    return {
        "n": int(n), "alpha": float(beta[0]), "beta": float(beta[1]),
        "se_beta": se_b,
        "t_beta_vs1": float((beta[1] - 1.0) / se_b) if se_b > 0 else float("nan"),
        "se_alpha": se_a,
        "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan"),
        "r2_within": r2w,
        "n_dates": int(G_d), "n_symbols": int(G_s),
        "scale": scale, "ok": True,
    }


def fama_macbeth(panel, forecast_col: str, realised_col: str,
                 date_col: str = "date", scale: str = "log_var",
                 min_n: int = 10, min_dates: int = 3) -> dict:
    """Per-date regression, averaged. SECONDARY DIAGNOSTIC ONLY.

    DO NOT use this for calibration. The per-date intercept absorbs market-wide
    level bias, which is the main thing MZ is supposed to detect -- see
    pooled_mz_clustered, which is the correct estimator.

    What it is still good for: measuring CROSS-SECTIONAL skill, i.e. whether a
    forecast ranks symbols correctly on a given day, with the common vol factor
    deliberately removed. That is a real question, just a different one.
    """
    betas, alphas, r2s, dates = [], [], [], []
    for d, g in panel.groupby(date_col):
        r = mincer_zarnowitz(g[forecast_col].to_numpy(float),
                             g[realised_col].to_numpy(float),
                             scale=scale, min_n=min_n)
        if np.isfinite(r["beta"]):
            betas.append(r["beta"]); alphas.append(r["alpha"])
            r2s.append(r["r2"]); dates.append(d)

    blank = {"n_dates": len(betas), "beta": float("nan"), "t_beta": float("nan"),
             "t_beta_vs1": float("nan"), "beta_sd": float("nan"),
             "alpha": float("nan"), "t_alpha": float("nan"),
             "r2": float("nan"), "beta_series": [], "dates": [], "ok": False}
    if len(betas) < min_dates:
        return blank

    b = np.asarray(betas); a = np.asarray(alphas)
    se_b = b.std(ddof=1) / np.sqrt(b.size)
    se_a = a.std(ddof=1) / np.sqrt(a.size)
    return {
        "n_dates": int(b.size),
        "beta": float(b.mean()),
        "t_beta": float(b.mean() / se_b) if se_b > 0 else float("nan"),
        # t against the null that matters: beta == 1 (a calibrated forecast)
        "t_beta_vs1": float((b.mean() - 1.0) / se_b) if se_b > 0 else float("nan"),
        "beta_sd": float(b.std(ddof=1)),
        "alpha": float(a.mean()),
        "t_alpha": float(a.mean() / se_a) if se_a > 0 else float("nan"),
        "r2": float(np.mean(r2s)),
        "beta_series": [float(x) for x in b],
        "dates": list(dates),
        "ok": True,
    }


def newey_west_se(x, lag: int) -> float:
    """Newey-West standard error of a mean. RETAINED BUT UNUSED.

    Superseded by clustering on symbol in pooled_mz_clustered, which permits
    arbitrary within-symbol serial correlation and therefore already covers
    overlapping forward windows.

    It was also being MISUSED: lag = h - 1 = 19 was applied to a series of
    per-date coefficients sampled MONTHLY, where consecutive samples sit ~21
    trading days apart and 20-day windows barely touch. Correct lag there is
    ~0, so it over-corrected rather than under-corrected.

    Indexing is by array position, which IS trading-day index, so market
    holidays never enter -- that would only matter for a calendar-indexed
    series with gaps.
    """
    x = np.asarray(x, dtype=float)
    x = x[np.isfinite(x)]
    n = x.size
    if n < 3:
        return float("nan")
    e = x - x.mean()
    g0 = float(np.dot(e, e) / n)
    s = g0
    for L in range(1, min(lag, n - 1) + 1):
        g = float(np.dot(e[L:], e[:-L]) / n)
        s += 2.0 * (1.0 - L / (lag + 1.0)) * g       # Bartlett kernel
    s = max(s, 1e-18)
    return float(np.sqrt(s / n))


# --------------------------------------------------------------------------
# 2. filtered historical simulation
# --------------------------------------------------------------------------

def fhs_paths(
    frv: ForwardRV,
    horizon: int,
    n_paths: int = 20_000,
    drift: Optional[float] = None,
    seed: Optional[int] = 42,
    tail_pool: Optional[Sequence[float]] = None,
) -> np.ndarray:
    """Filtered historical simulation. Returns (n_paths, horizon) LOG returns.

    The GARCH recursion is run forward INSIDE each path:

        sigma^2_{t+1} = omega + alpha * eps_t^2 + beta * sigma^2_t
        eps_t         = sigma_t * z_t,  z_t resampled from the innovation pool

    so volatility clusters within the path and the h-day sum keeps its fat tail.
    Scaling a fixed sigma_0 by resampled z would not.

    drift:
        None  -> use the fitted mean mu. For a 1-year sample this is very noisy.
        0.0   -> driftless, the safe default for a short-vol screen.
        float -> supplied (e.g. risk-free minus dividend yield, per day).

        Bootstrapping RAW historical returns instead would silently inherit
        whatever drift the sample happened to contain, which for a bull year
        biases every straddle result. Hence the explicit switch.

    tail_pool:
        Optional larger innovation pool (e.g. 3-5 years of standardised
        residuals, or GPD-simulated tail draws). The GARCH fit window should be
        short enough to track the current regime, but 1 year of residuals gives
        only a handful of draws beyond 3 sigma -- precisely the region that sets
        CVaR. Supplying a wider pool decouples the two.
    """
    rng = np.random.default_rng(seed)
    pool = np.asarray(tail_pool if tail_pool is not None else frv.innovations, dtype=float)
    pool = pool[np.isfinite(pool)]
    if pool.size < 30:
        raise ValueError(f"innovation pool too small: {pool.size}")

    mu = frv.mu if drift is None else float(drift)

    # EWMA fallback has no usable (omega, alpha, beta); degrade to constant-sigma
    # resampling rather than silently producing a different model.
    if not all(np.isfinite([frv.omega, frv.alpha, frv.beta])):
        sig = math.sqrt(float(np.mean(frv.var_path)))
        z = rng.choice(pool, size=(n_paths, horizon), replace=True)
        return mu + sig * z

    omega, alpha, beta = frv.omega, frv.alpha, frv.beta
    out = np.empty((n_paths, horizon), dtype=float)
    var_t = np.full(n_paths, float(frv.var_path[0]), dtype=float)

    for h in range(horizon):
        z = rng.choice(pool, size=n_paths, replace=True)
        sig_t = np.sqrt(var_t)
        eps = sig_t * z
        out[:, h] = mu + eps
        var_t = omega + alpha * eps ** 2 + beta * var_t

    return out


def empirical_move_stats(paths: np.ndarray) -> dict:
    """Terminal absolute-move distribution, in decimal return units.

    Both mean and median are returned because they are different things and get
    confused constantly: the ATM straddle price is the MEAN absolute move
    (E|X| = 0.798*sigma under a normal), while the 50% band is the MEDIAN
    absolute move (0.674*sigma). One standard deviation is neither.
    """
    term = np.sum(paths, axis=1)
    a = np.abs(term)
    sd = float(np.std(term, ddof=1))
    return {
        "sd": sd,
        "mean_abs": float(np.mean(a)),
        "median_abs": float(np.median(a)),
        "p90_abs": float(np.quantile(a, 0.90)),
        "p99_abs": float(np.quantile(a, 0.99)),
        # E|X|/sigma: 0.798 under a normal, LOWER under fat tails. A reading
        # well below 0.798 says this name's moves are centre-heavy and the
        # Brenner-Subrahmanyam constant overstates its vol.
        "mad_sd_ratio": float(np.mean(a) / sd) if sd > 0 else float("nan"),
        "skew": float(((term - term.mean()) ** 3).mean() / sd ** 3) if sd > 0 else float("nan"),
    }


def tail_fragility(paths: np.ndarray, alpha: float = 0.05) -> float:
    """CVaR of the terminal absolute move, empirical / Gaussian, variance-matched.

    1.0 = Gaussian tails. ~1.2 = t(6)-like. >1.3 = genuinely fragile.

    This replaces the |empirical POP - Gaussian POP| formulation, which fails
    three ways: the sign is backwards (fat tails RAISE short-straddle POP,
    because the breakeven sits in the shoulders where leptokurtosis removes
    mass, not in the tail), the magnitude is compressed (10 POP points across
    the whole range from normal to infinite kurtosis, vs 32% on CVaR), and the
    estimator noise on a proportion swamps any sensible threshold.
    """
    term = np.sum(paths, axis=1)
    sd = float(np.std(term, ddof=1))
    if sd <= 0:
        return float("nan")
    a = np.abs(term)
    q = float(np.quantile(a, 1 - alpha))
    cvar_emp = float(np.mean(a[a >= q]))
    # Gaussian CVaR of |X| at the same variance: E[|X| | |X| > q_g]
    from scipy.stats import norm
    q_g = norm.ppf(1 - alpha / 2)
    cvar_gauss = sd * (2 * norm.pdf(q_g) / alpha)
    return float(cvar_emp / cvar_gauss) if cvar_gauss > 0 else float("nan")


# --------------------------------------------------------------------------
# 3. structure definition, costs, margin
# --------------------------------------------------------------------------

@dataclass
class Leg:
    opt: Literal["CE", "PE"]
    strike: float
    premium: float            # mid, per share
    qty: int = -1             # negative = short
    bid: Optional[float] = None
    ask: Optional[float] = None

    @property
    def half_spread(self) -> float:
        if self.bid is None or self.ask is None:
            return 0.0
        if not (np.isfinite(self.bid) and np.isfinite(self.ask)) or self.ask <= self.bid:
            return 0.0
        return (self.ask - self.bid) / 2.0


@dataclass
class Structure:
    symbol: str
    legs: list
    lot_size: int
    ref_price: float          # futures preferred over spot
    iv_pct: float
    kind: Literal["straddle", "strangle"] = "straddle"
    #: modelled half-spread as a fraction of gross credit, used when the legs
    #: carry no bid/ask (i.e. every historical bar). Set from
    #: spread_fraction(volume, dte). None means fall back to quoted spreads.
    spread_frac: Optional[float] = None

    @property
    def gross_credit(self) -> float:
        return float(sum(-l.qty * l.premium for l in self.legs))

    @property
    def spread_cost(self) -> float:
        """Half-spread on entry, per share.

        Prefers real quotes when the legs have them; otherwise uses the
        calibrated fraction. Returning 0 when neither is available is what made
        every pre-2026 backtest overstate returns -- so an explicit
        spread_frac=0.0 is the only way to get a free fill now.
        """
        quoted = float(sum(abs(l.qty) * l.half_spread for l in self.legs))
        if quoted > 0:
            return quoted
        if self.spread_frac is not None:
            return float(self.spread_frac) * self.gross_credit
        return 0.0

    @property
    def spread_to_credit(self) -> float:
        c = self.gross_credit
        return float(self.spread_cost / c) if c > 0 else float("inf")


def entry_costs(st: Structure) -> float:
    """Per-share entry cost of opening the short structure (sell side)."""
    prem = st.gross_credit
    stt = STT_SELL_PREMIUM * prem
    exch = EXCH_TXN_PCT * prem
    sebi = SEBI_TURNOVER_PCT * prem
    brok = (BROKERAGE_PER_ORDER * len(st.legs)) / max(st.lot_size, 1)
    gst = GST_RATE * (brok + exch + sebi)
    return float(stt + exch + sebi + brok + gst + st.spread_cost)


def exit_costs(st: Structure, exit_value: float, exercised: bool) -> float:
    """Per-share cost of closing.

    exercised=True is the held-to-expiry case where the ITM leg is settled by
    exercise. STT is then charged on the INTRINSIC SETTLEMENT VALUE, not on the
    premium -- on a deep-ITM finish that dwarfs every other cost line, and it is
    routinely left out of retail P&L models.
    """
    if exercised:
        stt = STT_EXERCISE_ITM * exit_value
        exch = EXCH_TXN_PCT * exit_value
        brok = 0.0                       # exercise, no closing order
        sd = 0.0
    else:
        stt = 0.0                        # buy-to-close attracts no STT
        exch = EXCH_TXN_PCT * exit_value
        brok = (BROKERAGE_PER_ORDER * len(st.legs)) / max(st.lot_size, 1)
        sd = STAMP_DUTY_PCT * exit_value
    sebi = SEBI_TURNOVER_PCT * exit_value
    gst = GST_RATE * (brok + exch + sebi)
    spr = 0.0 if exercised else st.spread_cost
    return float(stt + exch + sebi + brok + sd + gst + spr)


def net_credit(st: Structure) -> float:
    """Credit actually banked at entry. This is the numerator of the cushion --
    using gross mid credit instead systematically overstates it, by ~2% on a
    liquid index and 20%+ on an illiquid single name."""
    return float(st.gross_credit - entry_costs(st))


def leg_margin(symbol: str, opt: str, ref: float, strike: float,
               premium: float, iv_pct: float,
               min_scan: float = 0.05, exposure: float = 0.03,
               k_sigma: float = 3.0, horizon_days: float = 2.0,
               floor_frac: float = 0.5) -> float:
    """Per-share SPAN-shaped margin for ONE short leg. Mirrors the existing
    oc_dashboard.margin module; duplicated here so straddle_core stays
    import-free of the web app."""
    sigma = max(float(iv_pct or 0.0), 0.0) / 100.0
    scan = k_sigma * sigma * math.sqrt(horizon_days / TRADING_DAYS)
    base = max(min_scan, scan) + exposure
    otm = max(0.0, strike - ref) if opt.upper().startswith("C") else max(0.0, ref - strike)
    return float(premium + max(base * ref - otm, floor_frac * base * ref))


def combo_margin(st: Structure, **kw) -> float:
    """Margin for the whole structure, per share.

    NOT the sum of the legs. Only one side of a straddle or strangle can finish
    ITM, so SPAN scans the worse side; charging both legs overstates capital by
    roughly 2x and makes RoM rank wrong in a way that varies with skew.
    """
    legs = [leg_margin(st.symbol, l.opt, st.ref_price, l.strike,
                       l.premium, st.iv_pct, **kw) for l in st.legs]
    if not legs:
        return float("nan")
    return float(COMBO_MARGIN_MULT * max(legs))


# --------------------------------------------------------------------------
# 4. path simulation of the structure
# --------------------------------------------------------------------------

@dataclass
class SimResult:
    ev: float                 # mean P&L per share, net of costs
    ev_per_margin: float
    cvar05: float             # mean of worst 5% P&L (negative number)
    cvar05_per_margin: float
    var05: float
    pop: float                # DIAGNOSTIC ONLY -- never a ranking input
    win_rate: float
    median_pnl: float
    p95_loss: float
    max_loss: float
    stop_rate: float          # fraction of paths that hit the exit rule
    mean_hold_days: float
    skew: float
    tfr: float                # tail fragility ratio
    n_paths: int

    def to_dict(self) -> dict:
        return asdict(self)


def _intrinsic(st: Structure, s: np.ndarray) -> np.ndarray:
    """Value of the short structure (what it costs to buy back) at price s."""
    v = np.zeros_like(s)
    for l in st.legs:
        if l.opt.upper().startswith("C"):
            v += abs(l.qty) * np.maximum(s - l.strike, 0.0)
        else:
            v += abs(l.qty) * np.maximum(l.strike - s, 0.0)
    return v


def simulate_structure(
    st: Structure,
    paths: np.ndarray,
    spot: float,
    stop_k_em: Optional[float] = 1.25,
    em_ref: Optional[float] = None,
) -> SimResult:
    """Run the structure through the simulated paths under an explicit exit rule.

    stop_k_em:
        Exit when |S_t - centre| exceeds k * EM, where EM is the h-day expected
        move in PRICE units. None = hold to expiry.

        Deliberately NOT a credit multiple. For an ATM straddle a k*credit stop
        fires at sqrt(k^2 - 1) * credit of adverse move, so k=3 needs ~2.3
        sigma*sqrt(T) -- an effectively unreachable stop -- while the same k on
        a 0.25-delta naked short fires around 1.4 sigma*sqrt(T). The multiple
        validated on naked legs does not transfer.

        A distance stop is also immune to the ATM straddle's second problem:
        sitting at maximum vega, a premium-based stop can fire on an IV spike
        with spot unchanged, closing a position that was never actually tested.

    em_ref:
        Expected move in price units. Defaults to the simulated MEAN absolute
        terminal move (which is what the straddle actually prices), not 1 sigma.
    """
    n_paths, horizon = paths.shape
    centre = float(st.legs[0].strike) if st.kind == "straddle" else float(spot)

    cum = np.cumsum(paths, axis=1)
    prices = spot * np.exp(cum)                     # (n_paths, horizon)

    if em_ref is None:
        term = np.sum(paths, axis=1)
        em_ref = float(spot * np.mean(np.abs(np.exp(term) - 1.0)))

    credit = net_credit(st)

    exercised = np.ones(n_paths, dtype=bool)
    hold_days = np.full(n_paths, float(horizon), dtype=float)

    if stop_k_em is None:
        exit_val = _intrinsic(st, prices[:, -1])
    else:
        thresh = stop_k_em * em_ref
        breached = np.abs(prices - centre) > thresh
        any_b = breached.any(axis=1)
        first = np.where(any_b, breached.argmax(axis=1), horizon - 1)

        s_at = prices[np.arange(n_paths), first]
        # On a stop we buy back at intrinsic + remaining time value. Time value
        # is approximated from the remaining-horizon expected move: crude, but
        # honest that a mid-life exit is NOT an intrinsic-only transaction.
        # Ignoring it flatters every stopped path.
        rem = (horizon - 1 - first).astype(float)
        tv = SQRT_2_OVER_PI * deannualise_vol(st.iv_pct) * np.sqrt(np.maximum(rem, 0.0)) * s_at
        val_stop = _intrinsic(st, s_at) + tv
        val_hold = _intrinsic(st, prices[:, -1])

        exit_val = np.where(any_b, val_stop, val_hold)
        exercised = ~any_b
        hold_days = np.where(any_b, first + 1.0, float(horizon))

    costs_out = np.array([exit_costs(st, float(v), bool(e))
                          for v, e in zip(exit_val, exercised)])
    pnl = credit - exit_val - costs_out

    q05 = float(np.quantile(pnl, 0.05))
    tail = pnl[pnl <= q05]
    cvar = float(tail.mean()) if tail.size else q05
    margin = combo_margin(st)
    sd = float(np.std(pnl, ddof=1))

    return SimResult(
        ev=float(pnl.mean()),
        ev_per_margin=float(pnl.mean() / margin) if margin > 0 else float("nan"),
        cvar05=cvar,
        cvar05_per_margin=float(cvar / margin) if margin > 0 else float("nan"),
        var05=q05,
        pop=float((pnl > 0).mean()),
        win_rate=float((pnl > 0).mean()),
        median_pnl=float(np.median(pnl)),
        p95_loss=float(np.quantile(pnl, 0.05)),
        max_loss=float(pnl.min()),
        stop_rate=float((~exercised).mean()) if stop_k_em is not None else 0.0,
        mean_hold_days=float(hold_days.mean()),
        skew=float(((pnl - pnl.mean()) ** 3).mean() / sd ** 3) if sd > 0 else float("nan"),
        tfr=tail_fragility(paths),
        n_paths=int(n_paths),
    )


def sweep_stop(st: Structure, paths: np.ndarray, spot: float,
               ks: Sequence[Optional[float]] = (None, 0.75, 1.0, 1.25, 1.5, 2.0)) -> list:
    """Run the same paths under several exit rules.

    Always include None (no stop) in the sweep. Because a straddle stop has to
    sit far out before it stops being noise-triggered, "no stop, size smaller"
    is a live candidate for the optimum, not a failure case.
    """
    out = []
    for k in ks:
        r = simulate_structure(st, paths, spot, stop_k_em=k)
        d = r.to_dict()
        d["stop_k_em"] = k
        out.append(d)
    return out


# --------------------------------------------------------------------------
# 5. scoring features
# --------------------------------------------------------------------------

# Gamma-regime score. RETIRED as a lookup on 2026-10-04.
#
# The old table scored (per-strike regime, flip above spot): 0.85 / 0.45 / 0.35 /
# 0.15. Over 71 expiries it ranked straddles BACKWARDS (IC -0.044, t -2.57), and
# a six-year proxy on exposure_hist (short ATM straddle, ~25 DTE to expiry,
# cross-sectional IC per expiry) reproduced that: -0.036 (t -2.6) Jun 2020 -
# Oct 2024 and -0.052 (t -2.5) after. It was built on the per-strike flip, which
# mostly marks the ATM strike where call OI overtakes put OI; under the dealer
# gamma-profile definition now stored, its top bucket (positive, flip above
# spot) occurs on ~0.01% of rows.
#
# Replacement: the dealer-profile gamma_position, mapped to [0, 1]. It is a
# DISPLAY / diagnostic term, still NOT ranked (it is in backtest_straddle's CUT
# list). The same proxy gives it no stable straddle IC (+0.010, t 0.6, then
# -0.048, t -1.9): negative-gamma names do move more next day (pooled t -5.4 over
# six years), but IV already prices that over a 25-day hold. The place for
# gamma information in this screener is the simulator's volatility (better paths
# move ev_per_margin), not a ranking term.


# Month-level premium gate (added 2026-10-04). A cross-sectional rank cannot say
# WHETHER to sell premium this month, only which names; this gate does the first.
# Measured on backtest_straddle (EWMA paths, 72 expiries 2021-26): when the
# universe MEDIAN of iv / rv_forecast at entry is below 1.05, the expiry's mean
# realised P&L per margin was +0.1% (7 expiries) before Nov 2024 and -2.7% (13)
# after; at or above it, +3.0% (42) and +1.0% (10). Month median vs month mean
# outcome: rank corr +0.23 (p 0.05), same sign in both regimes. 13 of the 23
# post-Nov-2024 expiries fell below the line (7 of 49 before), which explains
# much of the post-2024 decay. Small samples: a gate to validate prospectively
# (vol_outcome), not a proven rule. Re-test at ~40 post-break expiries.
UNIVERSE_VRP_MIN = 1.05


def universe_premium_gate(iv_pct, rv_fcst_pct, threshold: float = UNIVERSE_VRP_MIN):
    """(median iv/rv_forecast across the candidate universe, passes?) for one
    asof/expiry. All candidates count, including stock-level rejects: the gate
    reads the MARKET's premium, not the shortlist's. (nan, None) when fewer than
    20 usable ratios."""
    iv = np.asarray(iv_pct, dtype=float)
    rv = np.asarray(rv_fcst_pct, dtype=float)
    ok = np.isfinite(iv) & np.isfinite(rv) & (iv > 0) & (rv > 0)
    if ok.sum() < 20:
        return float("nan"), None
    med = float(np.median(iv[ok] / rv[ok]))
    return med, bool(med >= threshold)


def gamma_regime_score(gamma_position: Optional[float], default: float = 0.5) -> float:
    """Dealer gamma position Gamma(fut)/max|Gamma| in [-1, 1] -> [0, 1].
    1 = deep in dealer long gamma (dampened), 0 = deep short gamma (amplified)."""
    if gamma_position is None:
        return default
    try:
        g = float(gamma_position)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(g):
        return default
    return float(min(1.0, max(0.0, 0.5 * (1.0 + g))))


@dataclass
class Features:
    """Raw (un-ranked) scoring inputs for one candidate.

    The list is SHORT on purpose. Two rounds of measurement cut it:

    COLLAPSED (measured Jun-2026, n=121): ev_per_margin, cushion_net, pop and
    cvar05_per_margin correlate 0.96-1.00 with each other. With driftless FHS,
    EV = credit - E|move| - costs while cushion = credit / median|move|, so both
    are monotone in the same ratio. One term survives; cvar becomes a GATE.

    DROPPED (measured Sep-2026, full universe): vrp_z built on a MODEL forecast
    is not a premium measure. With the best RV forecast being debiased ATM IV
    (beta 0.843), ln(IV) - ln(RV_fcst) = -alpha + 0.157*ln(IV), i.e. IV level
    with extra steps. vrp_z here therefore uses the Goyal-Saretto form,
    ln(HV20) - ln(IV), which is TRAILING realised vol -- a different quantity,
    and the published one.

    tfr is carried for display only: IC -0.033 over 7 expiries and near-zero
    correlation with the composite, so it earns no weight.
    """
    symbol: str
    ev_per_margin: float      # simulator EV per rupee of margin (clean paths)
    trend_eff: float          # prior 20d path efficiency -- HIGHER is better
    # --- carried, NOT ranked ----------------------------------------------
    # Everything below was measured over 71 expiries and cut, or was never a
    # ranking candidate. Kept because the pages display them, the gates use
    # them, and re-testing a cut term later costs nothing while the column is
    # still written. See RANK_DIRECTION for each one's measured IC.
    ts_slope: float = float("nan")
    vrp_z: float = float("nan")
    gamma_score: float = float("nan")
    liquidity: float = float("nan")
    compression_days: float = float("nan")
    days_in_regime: float = float("nan")
    net_gex_norm: float = float("nan")
    dealer_divergence: float = float("nan")
    basis_annualized: float = float("nan")
    shelf_width_norm: float = float("nan")
    cvar05_per_margin: float = float("nan")   # used as a GATE, not a rank
    tfr: float = float("nan")
    cushion_net: float = float("nan")
    pop: float = float("nan")
    vrp_ratio: float = float("nan")
    iv_pct: float = float("nan")
    rv_fcst_pct: float = float("nan")
    iv_rank: float = float("nan")
    iv_cm30: float = float("nan")
    oi_turnover_ratio: float = float("nan")
    spread_frac: float = float("nan")
    spread_to_credit: float = float("nan")


#: signal -> +1 if higher is better, -1 if lower is better
#:
#: TWO TERMS. The end state of a cull from twelve, run over 71 monthly expiries
#: of NSE single-stock straddles with corporate actions corrected and bid-ask
#: spread charged. Measured composite performance:
#:
#:     2-term (ev + trend)     IC 0.1290  t 8.01  55/71 positive
#:     3-term (+ ts_slope)     IC 0.1285  t 8.21  56/71
#:     ev_per_margin alone     IC 0.1250  t 6.67  54/71
#:     12-term                 IC 0.0322  t 2.08  37/71
#:
#: The composite buys +0.004 IC over the single best term. Nearly nothing --
#: but it lifts t from 6.67 to 8.01 and wins an extra expiry, so it is kept as
#: stabilisation, not as a source of edge. ts_slope was dropped: it LOWERED IC
#: (0.1290 -> 0.1285) while raising t, i.e. noise reduction at best, and a
#: third equal weight is not worth a coin flip.
#:
#: WHY EVERYTHING ELSE WENT -- each measured over the same 71 expiries:
#:   gamma_score       -0.0439 (t -2.57) SIGNIFICANTLY INVERTED, twice
#:   days_in_regime    -0.0198  | the temporal pair the whole exposure_hist
#:   compression_days  -0.0122  | enrichment was built for. compression_days
#:                              | was also CONSTANT across the cross-section
#:                              | on 31 of 71 expiries, so it could not rank
#:                              | anything there. "Duration-in-state ranks
#:                              | straddles" is now tested and false.
#:   liquidity         -0.0130 (still negative WITH spread charged)
#:   net_gex_norm      -0.0080
#:   vrp_z             -0.0077  third measurement, third null; rebuilding it on
#:                             constant-maturity iv_cm30 did not rescue it
#:   dealer_divergence -0.0024
#:   oi_turnover_ratio -0.0009
#:   shelf_width_norm  +0.0052 (t 0.29)
#:   basis_annualized  +0.0197 (t 1.33)
#:
#: THE DILUTION ARITHMETIC, before adding anything. For roughly uncorrelated
#: signals an equal-weighted composite lands near sum(IC)/sqrt(k): twelve terms
#: predicted 0.0393 against 0.0322 observed. A term with IC 0 does not merely
#: fail to help -- it divides the composite by a larger sqrt(k). Any candidate
#: must beat the 2-term composite, not merely show a positive IC alone.
#:
#: WHERE THE REMAINING LEVERAGE IS, on this evidence: not more features.
#: ev_per_margin is a SIMULATOR OUTPUT, so its quality tracks how well FHS
#: reproduces the return distribution -- better paths move IC, better features
#: do not. And spread already takes 40% of gross return (3.68% -> 2.21% RoM),
#: so a 20% improvement in fills beats any feature measured here.
RANK_DIRECTION = {
    "ev_per_margin": +1,     # IC 0.1250, t 6.67, 54/71. Simulator EV per rupee
                             # of margin, on CA-clean paths, net of modelled
                             # spread. Carries essentially all the signal.
    "trend_eff": +1,         # IC 0.0710, t 5.32, 51/71. PRIOR 20d path
                             # efficiency, and HIGHER is better: efficiency
                             # mean-reverts, so a name that just trended hard
                             # tends to consolidate. Shipping this at -1 -- the
                             # intuition that trending hurts a straddle, true
                             # CONCURRENTLY but not for the prior window -- put
                             # the only consistent signal in backwards for the
                             # first seven expiries.
}


def cross_sectional_score(rows: Sequence[Features],
                          weights: Optional[dict] = None) -> list:
    """Equal-weighted mean of cross-sectional PERCENTILE RANKS.

    Percentile ranks rather than z-scores because these distributions are
    fat-tailed and one corrupted print should not dominate the composite.

    Weights default to EQUAL and should stay there. Fitting them needs ~60
    independent cross-sections to detect a true IC of 0.05 at t=2; NSE single
    stocks expire monthly and all load on the same index vol factor, so the
    available history cannot support estimation. If weights are ever fitted,
    shrink them at least 70% toward equal.
    """
    if not rows:
        return []
    names = list(RANK_DIRECTION)
    w = {k: 1.0 for k in names}
    if weights:
        w.update({k: float(v) for k, v in weights.items() if k in w})

    mat = {k: np.array([getattr(r, k, np.nan) for r in rows], dtype=float) for k in names}
    ranks = {}
    for k, v in mat.items():
        x = v * RANK_DIRECTION[k]
        ok = np.isfinite(x)
        pr = np.full(x.size, np.nan)
        if ok.sum() > 1:
            order = np.argsort(np.argsort(x[ok]))
            pr[ok] = order / (ok.sum() - 1)
        elif ok.sum() == 1:
            pr[ok] = 0.5
        ranks[k] = pr

    out = []
    for i, r in enumerate(rows):
        parts = {k: ranks[k][i] for k in names}
        vals = [(w[k], parts[k]) for k in names if np.isfinite(parts[k])]
        score = (sum(wi * p for wi, p in vals) / sum(wi for wi, _ in vals)
                 if vals else float("nan"))
        d = asdict(r)
        d["score"] = float(score * 100.0) if np.isfinite(score) else float("nan")
        d["rank_parts"] = {k: (None if not np.isfinite(v) else round(float(v), 4))
                           for k, v in parts.items()}
        out.append(d)

    order = np.argsort([-(o["score"] if np.isfinite(o["score"]) else -1e9) for o in out])
    for pos, i in enumerate(order, start=1):
        out[i]["score_rank"] = pos
    return out


def demean_universe(z_values: Sequence[float]) -> np.ndarray:
    """Subtract the cross-sectional median so the VRP signal ranks names rather
    than timing the market.

    All ~160 NSE single names load on the same index vol factor: on a day when
    index IV is rich, nearly every own-history z is positive at once. The
    undemeaned signal therefore fires and stops firing together, giving one
    concentrated position wearing eight tickers. The universe median is itself a
    legitimate signal -- for SIZING total short-vol exposure -- but it belongs
    in the policy layer, not in the cross-sectional ranking.
    """
    z = np.asarray(z_values, dtype=float)
    med = np.nanmedian(z)
    return z - (med if np.isfinite(med) else 0.0)


# --------------------------------------------------------------------------
# 6. gates
# --------------------------------------------------------------------------

@dataclass
class GateConfig:
    max_spread_to_credit: float = 0.15
    min_dte: int = 10
    max_dte: int = 45
    event_buffer_days: int = 2        # reject if an event falls within DTE + buffer
    min_credit_abs: float = 1.0
    min_n_strikes: int = 8
    allowed_confidence: tuple = ("high", "medium")
    require_event_data: bool = False  # flip True once tvnews earnings_calendar fills


def apply_gates(st: Structure, dte: int, cfg: GateConfig,
                event_in_window: Optional[bool] = None,
                n_strikes: Optional[int] = None,
                confidence: Optional[str] = None):
    """Returns (passed, reasons_rejected).

    event_in_window=None means the event feed had NO DATA for this symbol --
    which is NOT the same as "no event". With require_event_data=True that is a
    rejection; with False it passes and is flagged, so the pages can ship while
    tvnews.duckdb earnings_calendar is still filling.
    """
    bad = []
    if not (cfg.min_dte <= dte <= cfg.max_dte):
        bad.append(f"dte {dte} outside [{cfg.min_dte},{cfg.max_dte}]")
    if st.gross_credit < cfg.min_credit_abs:
        bad.append("credit below floor")
    s2c = st.spread_to_credit
    if not np.isfinite(s2c) or s2c > cfg.max_spread_to_credit:
        bad.append(f"spread/credit {s2c:.3f} > {cfg.max_spread_to_credit}")
    if event_in_window is True:
        bad.append("event in window")
    elif event_in_window is None and cfg.require_event_data:
        bad.append("no event data")
    if n_strikes is not None and n_strikes < cfg.min_n_strikes:
        bad.append(f"only {n_strikes} strikes")
    if confidence is not None and str(confidence).lower() not in cfg.allowed_confidence:
        bad.append(f"confidence={confidence}")
    return (len(bad) == 0), bad


__all__ = [
    "TRADING_DAYS", "SQRT_2_OVER_PI",
    "ForwardRV", "forward_rv", "mincer_zarnowitz", "qlike",
    "pooled_mz_clustered", "fama_macbeth", "newey_west_se",
    "fhs_paths", "empirical_move_stats", "tail_fragility",
    "Leg", "Structure", "entry_costs", "exit_costs", "net_credit",
    "leg_margin", "combo_margin", "spread_fraction", "SPREAD_LUT",
    "SimResult", "simulate_structure", "sweep_stop",
    "Features", "RANK_DIRECTION", "cross_sectional_score", "demean_universe",
    "gamma_regime_score", "UNIVERSE_VRP_MIN", "universe_premium_gate", "GateConfig", "apply_gates",
    "annualise_vol", "deannualise_vol", "robust_z", "path_efficiency",
]
