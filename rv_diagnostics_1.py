"""
rv_diagnostics.py
=================
Is the realised-vol forecast any good? Measure it, per symbol, over five years.

    python rv_diagnostics.py --from 2020-09-01 --to 2026-06-30
    python rv_diagnostics.py --horizon 20 --max-symbols 60 --no-garch     # fast pass
    python rv_diagnostics.py --per-symbol --out rvdiag.csv                # full

Why this runs before anything else
----------------------------------
The 7-expiry straddle backtest showed two defects in the forecast layer: GARCH
and EWMA produced identical rank ICs (-0.064 vs -0.065), and realised |move| /
predicted EM had a median of 0.764 against the 0.845 a correct forecast
implies -- roughly 10% over-forecast at the centre. Both sit in the DENOMINATOR
of vrp_z, cushion_net and every simulator output, so fixing them improves every
downstream signal at once.

Unlike the ranking model this needs no trades and no expiries: ~200 symbols x
~60 monthly sample dates is thousands of independent observations, so it
converges in one run rather than in five years.

The four forecasters
--------------------
HV20    trailing 20d realised. The naive baseline everything must beat.
EWMA    RiskMetrics lambda=0.94. No mean reversion.
GARCH   (1,1)-t, properly h-day AGGREGATED (not sigma_1 * sqrt(h)).
ATM_IV  the market's forecast, from exposure_hist.

ATM_IV is the one to watch. If it wins on QLIKE then ln(IV / RV_forecast) is
measuring forecast error more than it measures a variance risk premium, and the
honest response is to use the published Goyal-Saretto form (ln HV - ln IV) as
the signal and keep the model only where the simulator needs a path-generating
process.

Metrics
-------
MZ     Mincer-Zarnowitz: regress realised on forecast. Want alpha=0, beta=1.
       beta < 1 means forecasts are OVER-DISPERSED -- too high when high, too
       low when low -- and should be shrunk toward the unconditional mean. The
       fitted value IS the shrunk forecast, so beta is the shrinkage factor.
QLIKE  robust variance loss, lower better. Used instead of MSE because MSE is
       dominated by the few highest-vol observations and ranks models
       differently (and worse).
bias   median(realised / forecast). 1.0 unbiased; 0.9 means forecasts run ~11%
       high.

Validated on synthetic data with known defects: an over-dispersed forecast
returns MZ_beta 0.55 while its bias still reads 1.01, and a uniformly 11%-high
forecast returns bias 0.899 with beta 0.914. The two failure modes are
distinguishable, which is the point of reporting both.

Point-in-time
-------------
Every forecast uses only returns strictly BEFORE the sample date, and every
realised value only the h trading days strictly AFTER it. Results dates are
joined from corporate_events so the effect of excluding the event day from the
realised window is measured rather than assumed.
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
import straddle_core as sc  # noqa: E402

TRADING_DAYS = sc.TRADING_DAYS


# --------------------------------------------------------------------------
# metrics
# --------------------------------------------------------------------------

def mz(forecast: np.ndarray, realised: np.ndarray) -> dict:
    """Mincer-Zarnowitz. beta is directly usable as a shrinkage factor."""
    m = np.isfinite(forecast) & np.isfinite(realised) & (forecast > 0) & (realised > 0)
    f, a = forecast[m], realised[m]
    if f.size < 30:
        return {"n": int(f.size), "alpha": np.nan, "beta": np.nan, "r2": np.nan}
    X = np.column_stack([np.ones_like(f), f])
    coef, *_ = np.linalg.lstsq(X, a, rcond=None)
    pred = X @ coef
    ss_res = float(np.sum((a - pred) ** 2))
    ss_tot = float(np.sum((a - a.mean()) ** 2))
    return {"n": int(f.size), "alpha": float(coef[0]), "beta": float(coef[1]),
            "r2": 1.0 - ss_res / ss_tot if ss_tot > 0 else np.nan}


def qlike(forecast: np.ndarray, realised: np.ndarray) -> float:
    """On VARIANCE, not vol. Inputs here are vols, so square them."""
    m = np.isfinite(forecast) & np.isfinite(realised) & (forecast > 0) & (realised > 0)
    f, a = forecast[m] ** 2, realised[m] ** 2
    if f.size == 0:
        return np.nan
    return float(np.mean(a / f - np.log(a / f) - 1.0))


def summarise(df: pd.DataFrame, models: list, target: str = "realised") -> pd.DataFrame:
    rows = []
    for mdl in models:
        if mdl not in df.columns:
            continue
        f = df[mdl].to_numpy(float)
        a = df[target].to_numpy(float)
        r = mz(f, a)
        ok = np.isfinite(f) & np.isfinite(a) & (f > 0) & (a > 0)
        rows.append({
            "model": mdl, "n": r["n"],
            "MZ_alpha": r["alpha"], "MZ_beta": r["beta"], "MZ_r2": r["r2"],
            "QLIKE": qlike(f, a),
            "bias": float(np.median(a[ok] / f[ok])) if ok.any() else np.nan,
            "mean_fcst": float(np.mean(f[ok])) if ok.any() else np.nan,
            "mean_real": float(np.mean(a[ok])) if ok.any() else np.nan,
        })
    return pd.DataFrame(rows).set_index("model")


# --------------------------------------------------------------------------
# data
# --------------------------------------------------------------------------

def load_returns(con, symbols=None) -> dict:
    """symbol -> (dates, log-returns), oldest first, CA-adjusted."""
    filt = ""
    params: list = []
    if symbols:
        filt = f" AND symbol IN ({','.join('?' * len(symbols))})"
        params = list(symbols)
    df = con.execute(f"""
        SELECT symbol, trade_date, ln(close / prevclose) AS lr
        FROM bhav.cash_eod
        WHERE series = 'EQ' AND close > 0 AND prevclose > 0{filt}
        ORDER BY symbol, trade_date
    """, params).df()
    out = {}
    for s, g in df.groupby("symbol"):
        out[s] = (pd.to_datetime(g["trade_date"]).dt.date.to_numpy(),
                  g["lr"].to_numpy(float))
    return out


def load_iv(con, horizon: int) -> pd.DataFrame:
    """ATM IV from exposure_hist, the expiry whose trading DTE is nearest the
    forecast horizon. Matching on horizon matters: comparing a 7-day IV against
    a 20-day realised vol measures term structure, not forecast skill."""
    return con.execute("""
        WITH r AS (
            SELECT symbol, date, atm_iv, dte_trading,
                   row_number() OVER (PARTITION BY symbol, date
                                      ORDER BY abs(dte_trading - ?)) rn
            FROM exposure_hist
            WHERE atm_iv > 0 AND dte_trading BETWEEN ? AND ?
        )
        SELECT symbol, date, atm_iv, dte_trading FROM r WHERE rn = 1
    """, [horizon, max(horizon - 8, 2), horizon + 12]).df()


def load_event_days(con) -> set:
    try:
        df = con.execute("""
            SELECT DISTINCT symbol, event_date FROM bhav.corporate_events
            WHERE is_results AND NOT coalesce(is_amendment, FALSE)
        """).df()
        return set(zip(df["symbol"], pd.to_datetime(df["event_date"]).dt.date))
    except Exception as exc:  # noqa: BLE001
        print(f"  [events unavailable: {exc}]")
        return set()


# --------------------------------------------------------------------------
# main loop
# --------------------------------------------------------------------------

def build_panel(con, args) -> pd.DataFrame:
    iv = load_iv(con, args.horizon)
    if iv.empty:
        print("no exposure_hist rows -- run build_exposure_history.py first")
        return pd.DataFrame()

    symbols = sorted(iv["symbol"].unique())
    if args.symbols:
        symbols = [s for s in symbols if s in set(args.symbols.split(","))]
    if args.max_symbols:
        # widest-coverage symbols first, so a truncated run stays representative
        cov = iv.groupby("symbol").size().sort_values(ascending=False)
        symbols = [s for s in cov.index if s in set(symbols)][:args.max_symbols]
    print(f"{len(symbols)} symbol(s)")

    rets = load_returns(con, symbols)
    events = load_event_days(con)
    iv = iv[iv["symbol"].isin(symbols)].copy()
    iv["date"] = pd.to_datetime(iv["date"]).dt.date
    iv_map = {(r.symbol, r.date): (r.atm_iv, r.dte_trading)
              for r in iv.itertuples(index=False)}

    h = args.horizon
    rows = []
    t0 = datetime.now()
    for si, sym in enumerate(symbols, 1):
        pack = rets.get(sym)
        if pack is None:
            continue
        dates, lr = pack
        if lr.size < args.min_obs + h + 5:
            continue

        idx = np.arange(args.min_obs, lr.size - h)
        if idx.size == 0:
            continue
        if args.sample == "monthly":
            ym = np.array([d.year * 12 + d.month for d in dates[idx]])
            keep = np.concatenate([[True], np.diff(ym) != 0])
            idx = idx[keep]
        else:
            idx = idx[::max(args.every, 1)]

        for i in idx:
            d = dates[i]
            if not (args.from_date <= d <= args.to_date):
                continue
            hist = lr[:i]                      # strictly before d
            fut = lr[i:i + h]                  # strictly after d
            fut_dates = dates[i:i + h]
            if fut.size < h:
                continue

            realised = float(np.std(fut, ddof=1) * np.sqrt(TRADING_DAYS) * 100)
            mask = np.array([(sym, dd) not in events for dd in fut_dates])
            realised_ex = (float(np.std(fut[mask], ddof=1) * np.sqrt(TRADING_DAYS) * 100)
                           if mask.sum() > 2 and mask.sum() >= h - 3 else np.nan)

            hv20 = float(np.std(hist[-20:], ddof=1) * np.sqrt(TRADING_DAYS) * 100)
            try:
                ewma = sc.forward_rv(hist, h, model="ewma",
                                     min_obs=args.min_obs).rv_annual_pct
            except Exception:
                ewma = np.nan
            garch = np.nan
            if not args.no_garch:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        fg = sc.forward_rv(hist, h, model="garch11_t",
                                           min_obs=args.min_obs)
                    garch = fg.rv_annual_pct if fg.converged else np.nan
                except Exception:
                    garch = np.nan

            ivv = iv_map.get((sym, d))
            rows.append({
                "symbol": sym, "date": d,
                "HV20": hv20, "EWMA": ewma, "GARCH": garch,
                "ATM_IV": ivv[0] if ivv else np.nan,
                "iv_dte": ivv[1] if ivv else np.nan,
                "realised": realised, "realised_ex_event": realised_ex,
                "had_event": bool((~mask).any()),
            })
        if si % 20 == 0 or si == len(symbols):
            el = (datetime.now() - t0).total_seconds()
            print(f"  [{si}/{len(symbols)}] {sym:14s} rows={len(rows):,}  "
                  f"{el:.0f}s elapsed, ~{el / si * (len(symbols) - si) / 60:.1f}m left")
    return pd.DataFrame(rows)


def report(df: pd.DataFrame, args) -> None:
    models = ["HV20", "EWMA", "GARCH", "ATM_IV"]
    print("\n" + "=" * 78)
    print(f"  FORECAST QUALITY   horizon={args.horizon}td   n={len(df):,} "
          f"observations, {df.symbol.nunique()} symbols")
    print("=" * 78)
    s = summarise(df, models)
    print(s.round(4).to_string())
    print("\n  MZ_beta is the shrinkage factor: forecast_shrunk = alpha + beta*forecast.")
    print("  beta < 1 => over-dispersed, shrink toward the mean.")
    print("  bias < 1 => forecasts run HIGH by 1/bias - 1.")
    print("  QLIKE: lower is better. Compare every model against HV20 -- a model")
    print("  that cannot beat trailing realised vol is not earning its place.")

    best = s["QLIKE"].idxmin() if s["QLIKE"].notna().any() else None
    if best:
        print(f"\n  >>> best QLIKE: {best}")
        if best == "ATM_IV":
            print("      The market forecasts better than the models do, so")
            print("      ln(IV / RV_forecast) is dominated by MODEL ERROR rather than")
            print("      premium. Prefer the Goyal-Saretto form (ln HV20 - ln IV) as")
            print("      the VRP signal and keep the model only inside the simulator.")
        elif best == "HV20":
            print("      The naive baseline wins. The forecasting layer is decoration")
            print("      at this horizon; keep it only as the FHS path generator.")

    print("\n--- does excluding event days fix the bias? ---")
    ev = df[df.had_event]
    if df["realised_ex_event"].notna().sum() > 100 and len(ev) > 100:
        s2 = summarise(ev, models, target="realised")
        s3 = summarise(ev, models, target="realised_ex_event")
        print(pd.DataFrame({"bias_with_event": s2["bias"], "bias_ex_event": s3["bias"],
                            "QLIKE_with": s2["QLIKE"], "QLIKE_ex": s3["QLIKE"]})
              .round(4).to_string())
        print("  Event-containing windows only. If bias moves toward 1.0 once the")
        print("  event day is removed, the diffusive forecast is fine and the JUMP is")
        print("  what it cannot see -- an argument for modelling the event separately")
        print("  rather than for shrinking the forecast.")
    else:
        print("  too few event-containing windows to compare")

    print("\n--- by vol regime (is the bias level-dependent?) ---")
    q = df.copy()
    q["bucket"] = pd.qcut(q["HV20"], 4, labels=["low", "mid-lo", "mid-hi", "high"],
                          duplicates="drop")
    for b, g in q.groupby("bucket", observed=True):
        line = (f"  {str(b):8s} n={len(g):6,}  HV20={g.HV20.mean():6.2f}  "
                f"realised={g.realised.mean():6.2f}")
        for m in models:
            gg = g[np.isfinite(g[m]) & (g[m] > 0)]
            if len(gg) > 20:
                line += f"  {m}_bias={np.median(gg.realised / gg[m]):5.3f}"
        print(line)
    print("  A bias that varies across buckets is what MZ_beta < 1 detects; a flat")
    print("  bias is a pure scale error needing only a constant multiplier.")

    if args.per_symbol and df["GARCH"].notna().any():
        print("\n--- worst-calibrated symbols (model vs IV, by QLIKE gap) ---")
        rows = []
        for sym, g in df.groupby("symbol"):
            if len(g) < 20:
                continue
            rows.append({"symbol": sym, "n": len(g),
                         "QLIKE_GARCH": qlike(g.GARCH.to_numpy(float),
                                              g.realised.to_numpy(float)),
                         "QLIKE_IV": qlike(g.ATM_IV.to_numpy(float),
                                           g.realised.to_numpy(float)),
                         "bias_GARCH": float(np.nanmedian(g.realised / g.GARCH))})
        t = pd.DataFrame(rows).dropna()
        if not t.empty:
            t["gap"] = t.QLIKE_GARCH - t.QLIKE_IV
            print(t.sort_values("gap", ascending=False).head(15).round(4)
                  .to_string(index=False))
            print("  Positive gap = the model is worse than IV for that symbol.")
            print("  A per-symbol reliability discount on vrp_z is the natural use.")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=r"oc.duckdb")
    ap.add_argument("--bhav", default=r"bhavcopy.duckdb")
    ap.add_argument("--from", dest="from_date", default="2020-09-01",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", default="2026-06-30",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--horizon", type=int, default=20, help="trading days ahead")
    ap.add_argument("--sample", choices=["monthly", "every"], default="monthly")
    ap.add_argument("--every", type=int, default=5)
    ap.add_argument("--min-obs", type=int, default=120, dest="min_obs")
    ap.add_argument("--max-symbols", type=int, default=None, dest="max_symbols")
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--no-garch", action="store_true", dest="no_garch",
                    help="skip the GARCH arm (much faster)")
    ap.add_argument("--per-symbol", action="store_true", dest="per_symbol")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

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
