"""
rv_diagnostics.py  (v2)
=======================
Is the realised-vol forecast any good? Measure it properly, over five years.

    python rv_diagnostics.py --no-garch --max-symbols 60          # quick pass
    python rv_diagnostics.py --per-symbol --strata --out rvdiag.csv

What changed from v1, and why each mattered
-------------------------------------------

1. NO DUPLICATE ESTIMATORS. v1 reimplemented mz() and qlike() alongside the
   copies in straddle_core, with a different n-threshold and a different input
   scale. Two definitions, two answers, silently. Both now import from
   straddle_core, which is the single source.

2. EVERYTHING IS VARIANCE. v1 regressed vol on vol and averaged vols. Taking
   sqrt is concave, so sqrt(mean(var)) != mean(sqrt(var)); shrinking vol then
   squaring is a DIFFERENT estimator from shrinking variance then rooting.
   Panel, regression, loss and shrinkage are all variance now. Vol appears in
   display only, converted once at the very end.

3. MZ ON LOG-VARIANCE. Variance residuals are violently heteroskedastic, so a
   level regression is dominated by the high-vol tail. Shrinkage becomes a
   power, var_shrunk = exp(alpha) * var_f ** beta, which cannot go negative at
   low vol and compresses the high tail harder.

4. YANG-ZHANG REALISED TARGET. A 20-observation close-to-close sd is a very
   noisy dependent variable. Noise in y does not bias beta but it craters R2
   and destroys power. YZ is ~7x more efficient AND gap-aware, which Parkinson
   and Garman-Klass are not -- and for a position held to expiry the overnight
   move is not optional. Close-to-close is kept alongside for comparison.

5. ADDITIVE EVENT SPLIT, NOT SLICING. v1 dropped the event day and recomputed
   the sd, which mechanically lowers the estimate -- removing the largest
   observation from any window does that -- so the test confirmed its own
   conclusion. Now:
       RV_total = sum of squared returns over h days
       RV_diff  = (h / (h-k)) * sum over NON-event days
       RV_jump  = RV_total - RV_diff
   Additive, exhaustive, no winsorising judgement call.

6. FAMA-MACBETH, NOT POOLED OLS. Every symbol samples the same ~66 dates and
   all load the same index vol factor, so pooled residuals are massively
   cross-correlated: effective N is nearer 66 than 18,000, and pooled standard
   errors understate uncertainty by roughly 16x. Pooled regression was also
   largely measuring "high-vol stocks have high vol" rather than forecast
   skill, which is why v1's R2 came out at 0.01 and looked like a scandal.
   Per-date regression removes the cross-sectional level effect entirely.
   Newey-West with lag h-1 is applied when sampling overlaps.

7. STRATA AND PER-YEAR STABILITY. One beta across 220 names assumes one
   error structure. Cut by liquidity, vol level, jump share and history depth,
   and refit per year -- a beta that drifts year to year is not a calibration,
   it is a fit to the past.

Still upstream and NOT fixed here: cash_eod prices carry no corporate-action
adjustment, so a 1:10 split injects ln(1/10) into the close-to-close return AND
into the Yang-Zhang overnight term. 92 such returns across 82 F&O symbols.
Every number below inherits it until nse_ca.py lands.
"""
from __future__ import annotations

import argparse
import sys
import warnings
from datetime import datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import straddle_core as sc          # noqa: E402  canonical mz / qlike / FM
import vol_math as vm               # noqa: E402  canonical RV estimators

TRADING_DAYS = sc.TRADING_DAYS

#: forecast columns, all ANNUALISED VARIANCE (decimal^2)
MODELS = ["HV20_CC", "HV20_YZ", "EWMA", "GARCH", "ATM_IV"]
#: realised columns, same scale.
#: RV_CC is the DEFAULT TARGET and RV_YZ is not, despite YZ being ~6x more
#: efficient. Measured on the 10-symbol trial, YZ variance runs 13% above
#: close-to-close at the median (0.1049 vs 0.0821) -- systematic, not outliers.
#: The cause is structural: a close-to-close return is overnight + intraday, so
#:     var(CC) = var(r_o) + var(r_c) + 2*cov(r_o, r_c)
#: while Yang-Zhang sums var(r_o) + k*var(r_c) + (1-k)*RS and ASSUMES the
#: covariance is zero. Indian stocks gap and fade, so that covariance is
#: negative and YZ overstates close-to-close variance.
#:
#: This is not academic. Scoring against RV_YZ made GARCH's vol_bias look
#: repaired -- 0.873 against RV_CC, 0.977 against RV_YZ -- which was the YZ
#: inflation, not calibration. And for a position held to expiry the quantity
#: that matters IS close-to-close terminal variance, covariance included.
#: YZ stays as a low-noise REGRESSOR (HV20_YZ), never as the target.
TARGETS = ["RV_CC", "RV_YZ", "RV_DIFF"]


def v2vol(v):
    """Variance -> vol in percent. Display only, applied once, at the end."""
    v = np.asarray(v, float)
    return np.where(v > 0, np.sqrt(np.maximum(v, 0)) * 100.0, np.nan)


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def load_ohlc(con, symbols=None, source: str = "adj") -> dict:
    """symbol -> dict of aligned arrays, on CORPORATE-ACTION-ADJUSTED prices.

    source="adj" (default) joins cash_returns_adj, built by nse_ca.py:
      * `r` is the adjusted log return, NULL on days where a corporate action
        could not be resolved (unparseable rights/demerger, IPO listing day,
        unexplained extreme).
      * `p` is prevclose RESCALED by the adjustment factor.

    That second point matters and is easy to miss. On a split day the whole of
    open/high/low/close is already on the NEW scale and only prevclose is on
    the old one -- so v_c and Rogers-Satchell are fine, and ONLY the overnight
    term ln(open/prevclose) is corrupted. Rescaling prevclose repairs
    Yang-Zhang without throwing the day away.

    NULL returns propagate as NaN into every estimator, where the >0 masks drop
    them. Dropping is right: a missing day costs one observation, whereas
    treating it as zero fabricates a no-move day and deflates every variance
    estimate containing it.

    source="raw" reproduces the unadjusted behaviour, for comparison only. A
    1:10 split then puts ln(1/10) into the return series and, standardised,
    z ~ -115 into the GARCH innovation pool.
    """
    filt, params = "", []
    if symbols:
        filt = f" AND c.symbol IN ({','.join('?' * len(symbols))})"
        params = list(symbols)

    df = None
    if source == "adj":
        try:
            df = con.execute(f"""
                SELECT c.symbol, c.trade_date, c.open, c.high, c.low, c.close,
                       c.prevclose, a.log_ret, a.adj_factor, a.flagged
                FROM bhav.cash_eod c
                LEFT JOIN bhav.cash_returns_adj a
                  ON a.symbol = c.symbol AND a.trade_date = c.trade_date
                WHERE c.series = 'EQ' AND c.close > 0 AND c.prevclose > 0{filt}
                ORDER BY c.symbol, c.trade_date
            """, params).df()
        except Exception as exc:  # noqa: BLE001
            print(f"  [cash_returns_adj unavailable: {exc}]")
            print("  !! falling back to RAW prices -- splits and bonuses are NOT")
            print("     corrected. Run nse_ca.py apply first.")
            source = "raw"
    if df is None:
        df = con.execute(f"""
            SELECT c.symbol, c.trade_date, c.open, c.high, c.low, c.close,
                   c.prevclose,
                   ln(c.close / c.prevclose) AS log_ret,
                   CAST(NULL AS DOUBLE) AS adj_factor,
                   CAST(NULL AS VARCHAR) AS flagged
            FROM bhav.cash_eod c
            WHERE c.series = 'EQ' AND c.close > 0 AND c.prevclose > 0{filt}
            ORDER BY c.symbol, c.trade_date
        """, params).df()

    n_null = int(df["log_ret"].isna().sum())
    n_adj = int(df["adj_factor"].notna().sum())
    print(f"  returns source={source}: {len(df):,} rows, "
          f"{n_adj:,} CA-adjusted, {n_null:,} nulled")
    if df["flagged"].notna().any():
        for k, v in df["flagged"].value_counts().head(5).items():
            print(f"      {v:6,}  {k}")

    out = {}
    for s, g in df.groupby("symbol"):
        p = g["prevclose"].to_numpy(float)
        fa = g["adj_factor"].to_numpy(float)
        r = g["log_ret"].to_numpy(float)
        # rescale prevclose where a factor exists; blank it where the day is
        # flagged, so the overnight term drops rather than lying
        p_adj = np.where(np.isfinite(fa) & (fa > 0), p * fa, p)
        p_adj = np.where(np.isfinite(r), p_adj, np.nan)
        out[s] = {
            "date": pd.to_datetime(g["trade_date"]).dt.date.to_numpy(),
            "o": g["open"].to_numpy(float), "h": g["high"].to_numpy(float),
            "l": g["low"].to_numpy(float), "c": g["close"].to_numpy(float),
            "p": p_adj,
            "r": r,
        }
    return out


def load_iv(con, horizon: int, tol: int) -> pd.DataFrame:
    """ATM IV from exposure_hist, expiry closest to the horizon.

    The tolerance is SYMMETRIC (+/- tol). v1 used -8/+12, which let term
    structure slope leak asymmetrically into the measured bias.
    """
    return con.execute("""
        WITH r AS (
            SELECT symbol, date, atm_iv, dte_trading,
                   row_number() OVER (PARTITION BY symbol, date
                                      ORDER BY abs(dte_trading - ?)) rn
            FROM exposure_hist
            WHERE atm_iv > 0 AND dte_trading BETWEEN ? AND ?
        )
        SELECT symbol, date, atm_iv, dte_trading FROM r WHERE rn = 1
    """, [horizon, max(horizon - tol, 2), horizon + tol]).df()


def load_event_days(con) -> set:
    try:
        df = con.execute("""
            SELECT DISTINCT symbol, event_date FROM bhav.corporate_events
            WHERE is_results AND NOT coalesce(is_amendment, FALSE)
        """).df()
        return set(zip(df["symbol"], pd.to_datetime(df["event_date"]).dt.date))
    except Exception as exc:  # noqa: BLE001
        print(f"  [events unavailable: {exc}] -- RV_DIFF will equal RV_CC")
        return set()


def current_universe(con, days: int) -> tuple:
    """(symbols, last_date) still trading F&O. Delisted / renamed / demerged
    names cannot matter for a forecast tuned for tomorrow's trades."""
    row = con.execute("SELECT max(trade_date) FROM bhav.futures_eod").fetchone()
    if not row or row[0] is None:
        return [], None
    syms = [r[0] for r in con.execute("""
        SELECT DISTINCT symbol FROM bhav.futures_eod
        WHERE trade_date > (SELECT max(trade_date) - ? FROM bhav.futures_eod)
        ORDER BY symbol
    """, [days]).fetchall()]
    return syms, row[0]


def load_liquidity(con) -> pd.DataFrame:
    """Per-symbol median option turnover, for the liquidity stratum."""
    try:
        return con.execute("""
            SELECT symbol, median(total_vol_in_range) AS med_opt_vol,
                   median(total_oi_in_range) AS med_oi
            FROM exposure_hist GROUP BY symbol
        """).df()
    except Exception:
        return pd.DataFrame(columns=["symbol", "med_opt_vol", "med_oi"])


# --------------------------------------------------------------------------
# panel
# --------------------------------------------------------------------------

def build_panel(con, args) -> pd.DataFrame:
    iv = load_iv(con, args.horizon, args.iv_tol)
    if iv.empty:
        print("no exposure_hist rows -- run build_exposure_history.py first")
        return pd.DataFrame()

    hist_syms = set(iv["symbol"].unique())
    if args.universe == "recent":
        live, last_day = current_universe(con, args.universe_days)
        if live:
            live = set(live)
            dropped = sorted(hist_syms - live)
            symbols = sorted(hist_syms & live)
            print(f"universe: {len(symbols)} trading F&O within "
                  f"{args.universe_days}d of {last_day}")
            if dropped:
                print(f"  dropped {len(dropped)} no-longer-trading: "
                      f"{', '.join(dropped[:12])}"
                      f"{' ...' if len(dropped) > 12 else ''}")
        else:
            symbols = sorted(hist_syms)
    else:
        symbols = sorted(hist_syms)
        print(f"universe: all {len(symbols)} (includes non-trading names)")

    if args.symbols:
        symbols = [s for s in symbols if s in set(args.symbols.split(","))]
    if args.max_symbols:
        cov = iv.groupby("symbol").size().sort_values(ascending=False)
        symbols = [s for s in cov.index if s in set(symbols)][:args.max_symbols]
        print(f"  truncated to {len(symbols)} by coverage "
              f"(biases toward long history -- quick pass only)")

    px = load_ohlc(con, symbols, source=args.returns)
    events = load_event_days(con)
    iv = iv[iv["symbol"].isin(symbols)].copy()
    iv["date"] = pd.to_datetime(iv["date"]).dt.date
    iv_map = {(r.symbol, r.date): (r.atm_iv, r.dte_trading)
              for r in iv.itertuples(index=False)}

    h = args.horizon
    rows, short = [], []
    t0 = datetime.now()
    for si, sym in enumerate(symbols, 1):
        d = px.get(sym)
        if d is None:
            continue
        n = d["c"].size
        if n < args.min_obs + h + 5:
            short.append((sym, n))
            continue

        # A window is only usable if enough of its returns survived CA
        # adjustment. A single nulled day is tolerable; several mean the
        # window spans an unresolved action and the variance would be built
        # on a truncated sample.
        finite_r = np.isfinite(d["r"])
        idx = np.arange(args.min_obs, n - h)
        if idx.size == 0:
            continue
        if args.sample == "monthly":
            ym = np.array([x.year * 12 + x.month for x in d["date"][idx]])
            idx = idx[np.concatenate([[True], np.diff(ym) != 0])]
        else:
            idx = idx[::max(args.every, 1)]

        for i in idx:
            dt = d["date"][i]
            if not (args.from_date <= dt <= args.to_date):
                continue
            sl = slice(i, i + h)
            fd = d["date"][sl]
            if fd.size < h:
                continue
            if int(finite_r[sl].sum()) < h - args.max_nulled:
                continue

            # ---- realised, all as ANNUALISED VARIANCE ----
            yzc = vm.yang_zhang_components(d["o"][sl], d["h"][sl], d["l"][sl],
                                           d["c"][sl], d["p"][sl])
            rv_yz = yzc.get("yz", np.nan)
            rv_yz_adj = yzc.get("yz_adj", np.nan)
            rv_cc = vm.rv_close_to_close(d["c"][sl], d["p"][sl])
            corr_oc = yzc.get("corr_oc", np.nan)

            # ---- event window ----
            # Indian results are usually released AFTER the close, so the move
            # lands on T+1. args.event_lag widens the event window to
            # [T, T+lag]; the t0/t1 columns below let the right lag be measured
            # rather than assumed.
            fr = d["r"][sl]
            ev_pos = [j for j, x in enumerate(fd) if (sym, x) in events]
            is_ev = np.zeros(h, dtype=bool)
            for j in ev_pos:
                for q in range(0, args.event_lag + 1):
                    if j + q < h:
                        is_ev[j + q] = True
            k = int(is_ev.sum())

            # Jump decomposition from RAW SUMS OF SQUARES, kept dimensionally
            # honest. With S_n = sum of non-event r2 and S_e = sum of event r2:
            #
            #   jump_var_lump = S_e - k * S_n/(h-k)      <- TOTAL variance over
            #                                               the window, the J in
            #                                               EM = sqrt(sig2_d*T + J)
            #   rv_jump       = jump_var_lump * 243/h    <- annualised RATE, for
            #                                               share plots only
            #
            # These are NOT interchangeable. Feeding the annualised rate into a
            # DJKS-style expected move is wrong by a factor of h/243. The
            # baseline offset k*S_n/(h-k) is what makes an event day at
            # baseline contribute exactly zero jump.
            if k and (h - k) >= 3:
                # nansum, and count only FINITE days -- CA-adjusted returns
                # carry NaN where an action could not be resolved, and a plain
                # np.sum propagates it into RV_DIFF and jump_var_lump. Measured
                # on the full run: 3 of 3,411 event windows lost this way. The
                # divisor must count finite non-event days too, or the rescale
                # to h diffusive days is wrong by however many were dropped.
                nz = fr[~is_ev]
                ez = fr[is_ev]
                n_ok = int(np.isfinite(nz).sum())
                e_ok = int(np.isfinite(ez).sum())
                if n_ok >= 3 and e_ok >= 1:
                    s_n = float(np.nansum(nz ** 2))
                    s_e = float(np.nansum(ez ** 2))
                    rv_diff = (s_n / n_ok) * TRADING_DAYS
                    # baseline offset uses the EVENT day count actually
                    # observed, not the nominal k
                    jump_lump = max(s_e - e_ok * s_n / n_ok, 0.0)
                else:
                    rv_diff, jump_lump = rv_cc, 0.0
            else:
                rv_diff, jump_lump = rv_cc, 0.0
            rv_jump = jump_lump * TRADING_DAYS / h

            # squared returns on the event day, the day after, and a non-event
            # baseline -- the raw material for choosing event_lag
            ev_t0 = ev_t1 = base_r2 = np.nan
            if len(ev_pos) == 1:
                j = ev_pos[0]
                base_mask = np.ones(h, dtype=bool)
                base_mask[max(j - 1, 0):min(j + 3, h)] = False
                if base_mask.sum() >= 5:
                    base_r2 = float(np.median(fr[base_mask] ** 2))
                    ev_t0 = float(fr[j] ** 2)
                    if j + 1 < h:
                        ev_t1 = float(fr[j + 1] ** 2)

            # ---- forecasts, same scale, strictly prior data ----
            hist = d["r"][:i]
            hv_cc = vm.rv_close_to_close(d["c"][i - 20:i], d["p"][i - 20:i])
            hv_yz = vm.rv_yang_zhang(d["o"][i - 20:i], d["h"][i - 20:i],
                                     d["l"][i - 20:i], d["c"][i - 20:i],
                                     d["p"][i - 20:i])
            try:
                ew = sc.forward_rv(hist, h, model="ewma",
                                   min_obs=args.min_obs).rv_annual_pct
                ewma = (ew / 100.0) ** 2
            except Exception:
                ewma = np.nan
            garch = np.nan
            if not args.no_garch:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        fg = sc.forward_rv(hist, h, model="garch11_t",
                                           min_obs=args.min_obs)
                    garch = (fg.rv_annual_pct / 100.0) ** 2 if fg.converged else np.nan
                except Exception:
                    garch = np.nan

            ivv = iv_map.get((sym, dt))
            rows.append({
                "symbol": sym, "date": dt, "year": dt.year,
                "HV20_CC": hv_cc, "HV20_YZ": hv_yz, "EWMA": ewma, "GARCH": garch,
                "ATM_IV": (ivv[0] / 100.0) ** 2 if ivv else np.nan,
                "iv_dte": ivv[1] if ivv else np.nan,
                "RV_YZ": rv_yz, "RV_YZ_ADJ": rv_yz_adj, "RV_CC": rv_cc,
                "RV_DIFF": rv_diff, "RV_JUMP": rv_jump,
                "jump_var_lump": jump_lump,
                "v_o": yzc.get("v_o", np.nan), "v_c": yzc.get("v_c", np.nan),
                "v_rs": yzc.get("v_rs", np.nan),
                "cov_oc": yzc.get("cov_oc", np.nan),
                "corr_on_id": corr_oc,
                "ev_r2_t0": ev_t0, "ev_r2_t1": ev_t1, "base_r2": base_r2,
                "n_event_days": k, "n_hist": int(i),
            })
        if si % 20 == 0 or si == len(symbols):
            el = (datetime.now() - t0).total_seconds()
            print(f"  [{si}/{len(symbols)}] {sym:14s} rows={len(rows):,}  "
                  f"{el:.0f}s, ~{el / si * (len(symbols) - si) / 60:.1f}m left")

    if short:
        print(f"\n  {len(short)} skipped for < {args.min_obs + h + 5} returns: "
              + ", ".join(f"{s}({n})" for s, n in sorted(short)[:10])
              + (" ..." if len(short) > 10 else ""))
        print("  Recent listings / demergers. Excluded because GARCH needs the")
        print("  history; lower --min-obs to include at the cost of a noisier fit.")
    return pd.DataFrame(rows)


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def common_sample(df: pd.DataFrame, models: list, target: str) -> pd.DataFrame:
    """Restrict to rows where EVERY model and the target are present.

    v1 scored ATM_IV on 12,581 rows and the rest on 17,922, so the QLIKE
    ranking compared models on different samples -- not a comparison at all.
    """
    m = np.isfinite(df[target].to_numpy(float)) & (df[target].to_numpy(float) > 0)
    for c in models:
        v = df[c].to_numpy(float)
        m &= np.isfinite(v) & (v > 0)
    return df[m]


def score_models(df: pd.DataFrame, models: list, target: str,
                 fm_min_n: int = 10) -> pd.DataFrame:
    """Per-model scores.

    Calibration comes from a POOLED regression with two-way clustered standard
    errors, not Fama-MacBeth. FM gives each date its own intercept, and that
    intercept absorbs market-wide level bias -- the very thing MZ is meant to
    detect. A forecast 20% high on every symbol every day would show FM beta
    ~ 1. FM is kept as `beta_xs`, a SEPARATE measure of cross-sectional ranking
    skill, which is a different question.

    r2_within (symbol means removed) is the skill number. Pooled r2 is inflated
    by "high-vol stocks have high vol", which no forecast deserves credit for.
    """
    rows = []
    for c in models:
        f = df[c].to_numpy(float)
        a = df[target].to_numpy(float)
        p = sc.pooled_mz_clustered(f, a, df["date"].to_numpy(),
                                   df["symbol"].to_numpy(), scale="log_var")
        fm = sc.fama_macbeth(df, c, target, scale="log_var", min_n=fm_min_n)
        ratio = a / f
        rows.append({
            "model": c, "n": p["n"],
            "beta": p["beta"], "se": p["se_beta"], "t_vs_1": p["t_beta_vs1"],
            "alpha": p["alpha"],
            "r2": p["r2"], "r2_within": p["r2_within"],
            "beta_xs": fm["beta"] if fm.get("ok") else np.nan,
            "QLIKE": sc.qlike(f, a),
            "var_bias": float(np.median(ratio)),
            "vol_bias": float(np.sqrt(np.median(ratio))),
            "mean_fcst_vol": float(v2vol(np.mean(f))),
            "mean_real_vol": float(v2vol(np.mean(a))),
        })
    return pd.DataFrame(rows).set_index("model")


def safe_qcut(s: pd.Series, labels: list):
    """Quantile buckets that survive ties.

    pd.qcut with duplicates="drop" can return FEWER bins than requested when
    values repeat, and then raises because the label count no longer matches.
    Derive labels from the bins that actually survive. Returns None when fewer
    than two distinct bins exist, so the caller can skip the stratum rather
    than report a single-bucket "comparison".
    """
    s = pd.to_numeric(s, errors="coerce")
    if s.notna().sum() < 10:
        return None
    for k in range(len(labels), 1, -1):
        try:
            cat = pd.qcut(s, k, duplicates="drop")
        except (ValueError, IndexError):
            continue
        n = len(cat.cat.categories)
        if n < 2:
            continue
        # spread the requested names across however many bins survived
        if n == len(labels):
            names = labels
        elif n == 2:
            names = [labels[0], labels[-1]]
        else:
            names = labels[:n]
        return cat.cat.rename_categories(names)
    return None


def jump_buckets(jump_share: pd.Series):
    """Buckets for a ZERO-INFLATED variable.

    Most windows contain no results day, so jump_share is exactly 0 on well
    over a third of rows and equal-count tertiles are meaningless -- the lower
    edges coincide and qcut collapses them. Split on the structural zero first,
    then halve the non-zero mass at its median.
    """
    js = pd.to_numeric(jump_share, errors="coerce").fillna(0.0)
    out = pd.Series(pd.NA, index=js.index, dtype="object")
    zero = js <= 0
    out[zero] = "none"
    nz = js[~zero]
    if nz.size >= 10:
        med = float(nz.median())
        out[~zero] = np.where(nz <= med, "some", "heavy")
    elif nz.size:
        out[~zero] = "some"
    return out.astype("category")


def report(df: pd.DataFrame, args) -> None:
    models = [m for m in MODELS if df[m].notna().any()]
    tgt = args.target
    d = common_sample(df, models, tgt)

    n_sym = d.symbol.nunique()
    # On a small universe the per-date cross-section is tiny; lower the bar so
    # the run completes, but say loudly that the inference is not meaningful.
    fm_min_n = min(10, max(4, n_sym // 2))
    trial = n_sym < 25

    print("\n" + "=" * 82)
    print(f"  FORECAST QUALITY   target={tgt}  horizon={args.horizon}td")
    print(f"  common sample n={len(d):,} of {len(df):,}   "
          f"symbols={n_sym}   dates={d.date.nunique()}")
    print("=" * 82)
    if trial:
        print("  !! TRIAL RUN: only %d symbols. Per-date cross-section is too" % n_sym)
        print("     small for Fama-MacBeth; betas and t-stats here are NOT")
        print("     meaningful. Use this run to check QLIKE and vol_bias move in")
        print("     the expected direction, then re-run on the full universe.")
    print("  All quantities are ANNUALISED VARIANCE. vol_bias = sqrt(var_bias).")
    print("  t_vs_1 tests beta == 1, the null a calibrated forecast satisfies.\n")
    s = score_models(d, models, tgt, fm_min_n=fm_min_n)
    print(s.round(4).to_string())

    print("\n  Read: beta < 1 => over-dispersed, shrink as")
    print("        var_shrunk = exp(alpha) * var_forecast ** beta")
    print("        vol_bias < 1 => forecasts run HIGH by 1/vol_bias - 1.")
    print("        t_vs_1 uses TWO-WAY CLUSTERED se (date + symbol); clustering")
    print("        on symbol also absorbs overlapping-window autocorrelation.")
    print("        r2_within is skill (symbol means removed); plain r2 is")
    print("        inflated by cross-sectional vol level.")
    print("        beta_xs is Fama-MacBeth = cross-sectional RANKING skill only;")
    print("        it cannot see level bias, so never read it as calibration.")
    if s["QLIKE"].notna().any():
        best = s["QLIKE"].idxmin()
        print(f"\n  >>> best QLIKE: {best}")
        if best == "ATM_IV":
            print("      Market beats the models. ln(IV / RV_forecast) is then")
            print("      dominated by MODEL error, not premium -- prefer the")
            print("      Goyal-Saretto form (ln HV - ln IV) for the signal and keep")
            print("      the model only as the FHS path generator.")
        elif best.startswith("HV20"):
            print("      Naive baseline wins. Forecasting layer is decoration at")
            print("      this horizon.")

    if args.overlap_lag:
        print("\n  [overlap] clustering on symbol already permits arbitrary")
        print("  within-symbol serial correlation, so overlapping forward")
        print("  windows are handled by the clustered se. No Newey-West needed.")

    print("\n--- Yang-Zhang vs close-to-close (why RV_CC is the target) ---")
    r = d["RV_YZ"] / d["RV_CC"].replace(0, np.nan)
    co = d["corr_on_id"].dropna()
    print(f"  RV_YZ / RV_CC       median {r.median():.4f}  "
          f"=> vol ratio {np.sqrt(r.median()):.4f}")
    if "RV_YZ_ADJ" in d.columns and d["RV_YZ_ADJ"].notna().any():
        ra = d["RV_YZ_ADJ"] / d["RV_CC"].replace(0, np.nan)
        print(f"  RV_YZ_ADJ / RV_CC   median {ra.median():.4f}   "
              f"(YZ + 2*cov, the algebraically correct alignment)")
    if len(co) > 30:
        print(f"  corr(overnight, intraday)  median {co.median():+.4f}  "
              f"share negative {100*(co<0).mean():.1f}%")
    if "v_rs" in d.columns and d["v_rs"].notna().any():
        rr = d["v_rs"] / d["v_c"].replace(0, np.nan)
        print(f"  v_rs / v_c          median {rr.median():.4f}")
        print("  ^ THE DIAGNOSTIC. v_c measures open-to-close variance DIRECTLY;")
        print("    v_rs infers the same quantity from the RANGE. A ratio well")
        print("    above 1 means the range is wide relative to terminal")
        print("    displacement -- intraday mean reversion -- so Rogers-Satchell")
        print("    overstates and that, not the covariance, is the YZ gap.")
        print("    If confirmed, the unbiased estimator that still uses open")
        print("    data is v_o + v_c + 2*cov, with no range term at all.")

    print("\n--- event timing: is the move on T or T+1? ---")
    ev = d.dropna(subset=["ev_r2_t0", "base_r2"])
    if len(ev) > 30:
        b = ev["base_r2"].replace(0, np.nan)
        m0 = float(np.nanmedian(ev["ev_r2_t0"] / b))
        m1 = float(np.nanmedian(ev["ev_r2_t1"] / b)) if ev["ev_r2_t1"].notna().any() else np.nan
        print(f"  windows with exactly one event: {len(ev):,}")
        print(f"  median r2(T)   / baseline = {m0:6.3f}")
        print(f"  median r2(T+1) / baseline = {m1:6.3f}")
        print("  Indian results are usually released AFTER the close, so the")
        print("  move lands on T+1. Whichever ratio is larger is the day the")
        print("  event actually hits; set --event-lag accordingly (0 = T only,")
        print("  1 = T and T+1).")
        if np.isfinite(m1) and m1 > m0:
            print(f"  >>> T+1 dominates ({m1:.3f} vs {m0:.3f}). Current lag="
                  f"{args.event_lag}; use --event-lag 1 or shift the date.")
        if max(m0, m1 if np.isfinite(m1) else 0) < 1.3:
            print("  !! neither day is much above baseline -- either the dates are")
            print("     misaligned, or results moves are smaller than assumed.")
    else:
        print("  too few single-event windows")

    print("\n--- event decomposition (additive, no slicing) ---")
    ev = d[d.n_event_days > 0]
    if len(ev) > 50:
        share = ev["RV_JUMP"] / ev["RV_CC"].replace(0, np.nan)
        print(f"  {len(ev):,} windows contain a results day "
              f"({100*len(ev)/len(d):.1f}%)")
        print(f"  jump share of total variance: median {share.median():.3f}  "
              f"p90 {share.quantile(.9):.3f}")
        if "jump_var_lump" in ev.columns:
            jl = ev["jump_var_lump"]
            print(f"  jump_var_lump (J, TOTAL variance over the window, NOT a")
            print(f"  rate): median {jl.median():.6f}  p90 {jl.quantile(.9):.6f}")
            print(f"  implied one-off move sqrt(J): median "
                  f"{100*np.sqrt(max(jl.median(),0)):.2f}%  "
                  f"p90 {100*np.sqrt(max(jl.quantile(.9),0)):.2f}%")
            print("  This is the J for EM = sqrt(sigma2_d * T + J). Do NOT use")
            print("  RV_JUMP there -- it is annualised and wrong by h/243.")
        print("\n  same models scored against the DIFFUSIVE target:")
        d2 = common_sample(ev, models, "RV_DIFF")
        tot = score_models(common_sample(ev, models, "RV_CC"), models,
                           "RV_CC", fm_min_n=fm_min_n)
        s2 = score_models(d2, models, "RV_DIFF", fm_min_n=fm_min_n)
        cmp = pd.DataFrame({
            "vol_bias_vs_total": tot["vol_bias"],
            "vol_bias_vs_diffusive": s2["vol_bias"],
            "QLIKE_total": tot["QLIKE"],
            "QLIKE_diffusive": s2["QLIKE"],
        })
        print(cmp.round(4).to_string())
        print("  If bias moves toward 1.0 against the DIFFUSIVE target, the")
        print("  forecast is fine and the JUMP is what it cannot see -- model the")
        print("  event separately (DJKS) rather than shrinking the forecast.")
        print("  If it does not move, the miscalibration is genuinely diffusive.")
    else:
        print("  too few event windows")

    print("\n--- per-year beta stability (is one number even defensible?) ---")
    for c in models:
        line = f"  {c:9s}"
        for yr, g in d.groupby("year"):
            gg = common_sample(g, [c], tgt)
            if len(gg) < 50:
                continue
            fm = sc.fama_macbeth(gg, c, tgt, scale="log_var", min_n=fm_min_n)
            if np.isfinite(fm["beta"]):
                line += f"  {yr}:{fm['beta']:+.2f}"
        print(line)
    print("  A beta that drifts year to year is not a calibration, it is a fit")
    print("  to the past. Only a stable one justifies a single shrinkage constant.")

    if args.strata:
        print("\n--- strata ---")
        q = d.copy()
        q["vol_level"] = safe_qcut(q["HV20_YZ"], ["low", "mid", "high"])
        q["jump_share"] = jump_buckets(
            q["RV_JUMP"] / q["RV_CC"].replace(0, np.nan))
        q["history"] = safe_qcut(q["n_hist"], ["short", "long"])
        for key in ("vol_level", "jump_share", "history"):
            if key not in q.columns or q[key] is None or q[key].isna().all():
                print(f"\n  by {key}: not enough distinct values to bucket")
                continue
            print(f"\n  by {key}:")
            for lvl, g in q.groupby(key, observed=True):
                gg = common_sample(g, models, tgt)
                if len(gg) < 100:
                    print(f"    {str(lvl):7s} n={len(gg):6,}  (too few, skipped)")
                    continue
                ss = score_models(gg, models, tgt, fm_min_n=fm_min_n)
                bits = "  ".join(f"{m}:b={ss.loc[m,'beta']:+.2f}/"
                                 f"q={ss.loc[m,'QLIKE']:.3f}" for m in models)
                print(f"    {str(lvl):7s} n={len(gg):6,}  {bits}")
        print("\n  Different beta per stratum => one global shrinkage is wrong;")
        print("  fit per stratum, or carry the stratum as a feature.")

    if args.per_symbol:
        print("\n--- per-symbol: model vs market (QLIKE gap) ---")
        rows = []
        for sym, g in d.groupby("symbol"):
            if len(g) < 20:
                continue
            mdl = "GARCH" if "GARCH" in models else "EWMA"
            rows.append({
                "symbol": sym, "n": len(g),
                f"QLIKE_{mdl}": sc.qlike(g[mdl].to_numpy(float),
                                         g[tgt].to_numpy(float)),
                "QLIKE_IV": sc.qlike(g["ATM_IV"].to_numpy(float),
                                     g[tgt].to_numpy(float)),
                "vol_bias": float(np.sqrt(np.nanmedian(g[tgt] / g[mdl]))),
            })
        t = pd.DataFrame(rows).dropna()
        if not t.empty:
            gapcol = [c for c in t.columns if c.startswith("QLIKE_") and c != "QLIKE_IV"][0]
            t["gap"] = t[gapcol] - t["QLIKE_IV"]
            print(t.sort_values("gap", ascending=False).head(15).round(4)
                  .to_string(index=False))
            print("  Positive gap = model worse than IV for that symbol.")
            print("  Natural use: a per-symbol reliability discount on vrp_z.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    ap.add_argument("--bhav", default=r"C:\Users\aniru\od\bhavcopy.duckdb")
    ap.add_argument("--from", dest="from_date", default="2020-09-01",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", default="2026-06-30",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--horizon", type=int, default=20)
    ap.add_argument("--iv-tol", type=int, default=8, dest="iv_tol",
                    help="symmetric DTE tolerance when matching IV to horizon")
    ap.add_argument("--target", default="RV_CC", choices=TARGETS,
                    help="RV_CC is correct for a held-to-expiry position; "
                         "RV_YZ overstates it (see TARGETS note)")
    ap.add_argument("--event-lag", type=int, default=1, dest="event_lag",
                    help="event affects days [T, T+lag]; 1 because Indian "
                         "results are usually released after the close")
    ap.add_argument("--sample", choices=["monthly", "every"], default="monthly")
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--min-obs", type=int, default=120, dest="min_obs")
    ap.add_argument("--max-symbols", type=int, default=None, dest="max_symbols")
    ap.add_argument("--universe", choices=["recent", "all"], default="recent")
    ap.add_argument("--universe-days", type=int, default=60, dest="universe_days")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--returns", choices=["adj", "raw"], default="adj",
                    help="'adj' uses cash_returns_adj (CA-corrected, from "
                         "nse_ca.py); 'raw' uses unadjusted cash_eod prices "
                         "and will carry split/bonus contamination")
    ap.add_argument("--max-nulled", type=int, default=2, dest="max_nulled",
                    help="skip a window with more than this many CA-nulled days")
    ap.add_argument("--no-garch", action="store_true", dest="no_garch")
    ap.add_argument("--per-symbol", action="store_true", dest="per_symbol")
    ap.add_argument("--strata", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    # overlapping sampling needs Newey-West; monthly with h<=21 is near-disjoint
    args.overlap_lag = (args.horizon - 1
                        if args.sample == "every" and args.every < args.horizon
                        else 0)

    con = duckdb.connect(args.db, read_only=True)
    con.execute(f"ATTACH IF NOT EXISTS '{args.bhav}' AS bhav (READ_ONLY)")
    try:
        df = build_panel(con, args)
    finally:
        con.close()
    if df.empty:
        print("no observations")
        return 1
    report(df, args)
    if args.out:
        df.to_csv(args.out, index=False)
        print(f"\nwrote {args.out} ({len(df):,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
