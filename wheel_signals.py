"""
wheel_signals -- which entry signal predicts how a short option in the wheel universe works out?

For every candidate the backtest could trade (same selection as wheel_bt.load_data:
point-in-time index members, traded close, volume / OI gates, 21-45 DTE, furthest-OTM
strike with ROM >= target and |delta| <= cap, one per symbol / side / day, and the
strategy's results / corporate-action entry blackouts), this

  1. sells one lot at the day's close (less the half-spread of its volume bucket),
  2. walks it forward with the strategy's exit rules -- results / corp-action exit,
     3x stop on the SPAN mark, time exit, final exit -- buying back at the traded
     close (or the mark + untraded slippage),
  3. records the outcome: kept = share of the premium kept, pnl_margin = P&L / margin,
  4. records candidate signals known at entry:
       span_rich   premium / price at NSE's flat SPAN vol (the current --min-rich gate)
       smile       the strike's market IV / the stock's ATM IV (same expiry)
       vrp_hv10/20/60, vrp_ewma, vrp_hvx, vrp_har
                   ATM IV / a realised-vol forecast: HV over 10/20/60 sessions, EWMA (0.94),
                   HV20 with results days removed, HAR (Corsi) fitted on the in-sample years
       ivk_har     the strike's IV / the HAR forecast (smile and vol premium together)

and scores each signal by its monthly cross-sectional rank correlation (Spearman IC)
with the outcome, plus the top-minus-bottom quintile spread, separately for the
in-sample (<= --split) and out-of-sample years, and for puts / calls.

Outcomes of the same contract on consecutive days overlap, so treat t-stats as rough.
Choose on the in-sample rows; the out-of-sample rows are the check.

    python wheel_signals.py                      # 2019-10 .. latest, split 2024-01-01
    python wheel_signals.py --split 2024-01-01 --out C:\\Users\\aniru\\od
writes <out>/wheel_signals_candidates.csv and <out>/wheel_signals_summary.csv
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import wheel_bt as wb

RATE = 0.065
IS_END_DEFAULT = date(2024, 1, 1)
SIGNALS = ["span_rich", "smile", "vrp_hv10", "vrp_hv20", "vrp_hv60", "vrp_ewma", "vrp_hvx", "vrp_har", "ivk_har"]


# ---------------------------------------------------------------------------
# vectorised Black-Scholes (spot, rate r, no dividends) and implied vol
# ---------------------------------------------------------------------------

def _ncdf(x):
    """Normal CDF via Abramowitz-Stegun 7.1.26 erf (abs error < 1.5e-7), numpy-vectorised."""
    z = np.abs(x) / np.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t - 0.284496736) * t
               + 0.254829592) * t * np.exp(-z * z)
    erf = np.sign(x) * y
    return 0.5 * (1.0 + erf)


def bs_vec(is_call, s, k, t, v, r=RATE):
    s, k, t, v = (np.asarray(a, float) for a in (s, k, t, v))
    sd = v * np.sqrt(t)
    with np.errstate(divide="ignore", invalid="ignore"):
        d1 = (np.log(s / k) + (r + 0.5 * v * v) * t) / sd
    d2 = d1 - sd
    dk = k * np.exp(-r * t)
    call = s * _ncdf(d1) - dk * _ncdf(d2)
    put = dk * _ncdf(-d2) - s * _ncdf(-d1)
    return np.where(is_call, call, put)


def iv_vec(is_call, price, s, k, t, r=RATE, iters=60):
    """Bisection on [1%, 500%]; NaN where the price is outside the no-arbitrage range."""
    is_call = np.asarray(is_call, bool)
    price, s, k, t = (np.asarray(a, float) for a in (price, s, k, t))
    lo, hi = np.full(price.shape, 0.01), np.full(price.shape, 5.0)
    p_lo, p_hi = bs_vec(is_call, s, k, t, lo, r), bs_vec(is_call, s, k, t, hi, r)
    ok = (price > p_lo) & (price < p_hi) & (t > 0) & (s > 0) & (k > 0)
    for _ in range(iters):
        mid = 0.5 * (lo + hi)
        up = bs_vec(is_call, s, k, t, mid, r) < price
        lo, hi = np.where(up, mid, lo), np.where(up, hi, mid)
    return np.where(ok, 0.5 * (lo + hi), np.nan)


# ---------------------------------------------------------------------------
# realised-vol forecasts per canonical symbol and day
# ---------------------------------------------------------------------------

def load_vol(con, start: date, end: date, results: dict, is_end: date) -> pd.DataFrame:
    al = wb._alias_sql()
    r = con.execute(f"""
        SELECT coalesce(a.canon, c.symbol) AS sym, c.trade_date AS d, c.log_ret AS lr
        FROM bc.cash_returns_adj c LEFT JOIN {al} ON a.raw = c.symbol
        WHERE c.trade_date BETWEEN ? AND ? AND c.log_ret IS NOT NULL
          AND coalesce(a.canon, c.symbol) IN ({wb.universe_symbols_sql(con)})
        ORDER BY 1, 2""", [start - pd.Timedelta(days=200), end]).df()
    r["d"] = pd.to_datetime(r["d"])
    out = []
    for sym, g in r.groupby("sym", sort=False):
        g = g.drop_duplicates("d").set_index("d").sort_index()
        lr = g["lr"].astype(float)
        f = pd.DataFrame(index=g.index)
        f["hv10"] = lr.rolling(10, min_periods=8).std() * np.sqrt(252)
        f["hv20"] = lr.rolling(20, min_periods=15).std() * np.sqrt(252)
        f["hv60"] = lr.rolling(60, min_periods=45).std() * np.sqrt(252)
        f["ewma"] = np.sqrt((lr ** 2).ewm(alpha=0.06, adjust=False).mean() * 252)
        res = {pd.Timestamp(x) for x in results.get(sym, [])}
        near = pd.Series([(x in res) or ((x - pd.Timedelta(days=1)) in res) for x in g.index], index=g.index)
        f["hvx"] = lr.where(~near).rolling(20, min_periods=12).std() * np.sqrt(252)    # results days removed
        r2 = lr ** 2
        f["rv_d"], f["rv_w"], f["rv_m"] = r2, r2.rolling(5).mean(), r2.rolling(22).mean()
        f["fut"] = r2[::-1].rolling(21, min_periods=15).mean()[::-1].shift(-1)        # target: next-21 variance
        f["sym"] = sym
        out.append(f.reset_index())
    v = pd.concat(out, ignore_index=True)
    # HAR fitted on in-sample rows only (no look-ahead into the check years)
    fit = v[(v["d"] < pd.Timestamp(is_end)) & v[["rv_d", "rv_w", "rv_m", "fut"]].notna().all(axis=1)]
    X = np.column_stack([np.ones(len(fit)), fit["rv_d"], fit["rv_w"], fit["rv_m"]])
    beta, *_ = np.linalg.lstsq(X, fit["fut"].to_numpy(), rcond=None)
    Xa = np.column_stack([np.ones(len(v)), v["rv_d"], v["rv_w"], v["rv_m"]])
    v["har"] = np.sqrt(np.clip(Xa @ beta, 1e-8, None) * 252)
    v.loc[v[["rv_d", "rv_w", "rv_m"]].isna().any(axis=1), "har"] = np.nan
    print(f"  HAR (fitted < {is_end}): b0={beta[0]:.2e} d={beta[1]:.3f} w={beta[2]:.3f} m={beta[3]:.3f}")
    return v[["sym", "d", "hv10", "hv20", "hv60", "ewma", "hvx", "har"]]


def load_atm_iv(con, cands: pd.DataFrame) -> pd.DataFrame:
    """ATM IV per (raw, expiry, day): mean IV of the nearest-strike call and put that traded."""
    keys = cands[["trade_date", "raw", "expiry", "und"]].drop_duplicates(["trade_date", "raw", "expiry"])
    con.register("_ak", keys)
    a = con.execute("""
        SELECT k.trade_date, k.raw, k.expiry, k.und, o.opt_type, o.strike, o.close
        FROM _ak k JOIN bc.options_eod o ON o.trade_date = k.trade_date AND o.symbol = k.raw AND o.expiry = k.expiry
        WHERE o.close > 0 AND o.volume > 0
        QUALIFY row_number() OVER (PARTITION BY k.trade_date, k.raw, k.expiry, o.opt_type
                                   ORDER BY abs(o.strike / k.und - 1)) = 1""").df()
    con.unregister("_ak")
    for c in ("trade_date", "expiry"):
        a[c] = pd.to_datetime(a[c]).dt.date
    t = np.array([max((e - d).days, 1) / 365.0 for d, e in zip(a["trade_date"], a["expiry"])])
    a["iv"] = iv_vec(a["opt_type"] == "CE", a["close"], a["und"], a["strike"], t)
    return a.groupby(["trade_date", "raw", "expiry"], as_index=False)["iv"].mean().rename(columns={"iv": "atm_iv"})


# ---------------------------------------------------------------------------
# outcome of one short, using the strategy's rules
# ---------------------------------------------------------------------------

def make_walker(p: wb.Params, data: wb.Data):
    cal = data.calendar
    pos = {d: i for i, d in enumerate(cal)}
    ctx = wb.Ctx(data.calendar, data.results, data.ca, data.sector, data.board, data.known, data.coverage,
                 data.market, data.hedge)

    def td(a, b):
        return pos.get(b, 0) - pos.get(a, 0)

    def entry_ok(sym, d, exp) -> bool:
        if p.require_event_coverage and ctx.coverage:
            cov = ctx.coverage.get(sym)
            if cov is None or d < cov - timedelta(days=100):
                return False
        nxt = wb._next_on_or_after(wb.event_dates(p, ctx, sym, d), d)
        if nxt is not None and td(d, nxt) <= p.earn_entry_days:
            return False
        cax = wb._next_on_or_after(ctx.ca.get(sym, []), d)
        return not (cax is not None and cax <= exp)

    def walk(c) -> tuple:
        """-> (kept, pnl_margin, reason, days) or None when the path is missing."""
        key = (c.raw, c.opt_type, c.expiry, float(c.strike))
        path = data.paths.get(key)
        if not path:
            return None
        entry = float(c.premium)
        credit = entry * (1 - wb.half_spread(p, float(c.volume)))
        mark, und, dl = entry, float(c.und), float(c.delta)
        i0 = pos.get(c.trade_date)
        if i0 is None:
            return None
        for d in cal[i0 + 1:]:
            row = path.get(d)
            if row is not None:
                close, vol, sp, sdl, _, u = row[:6]
                mark = sp if sp == sp else (close if close == close else mark)
                und = u if u == u else und
                dl = abs(sdl) if sdl == sdl else dl
            traded = row is not None and row[1] > 0
            left = td(d, c.expiry)
            res = wb._next_on_or_after(wb.event_dates(p, ctx, c.symbol, d), d)
            cax = wb._next_on_or_after(ctx.ca.get(c.symbol, []), d)
            reason = None
            if d >= c.expiry:
                reason = "expiry"
            elif res is not None and res <= c.expiry and td(d, res) <= p.earn_exit_lead:
                reason = "results"
            elif cax is not None and cax <= c.expiry and td(d, cax) <= 1:
                reason = "corp_action"
            elif p.stop_mult and mark == mark and (traded or not p.stop_requires_trade) and mark >= p.stop_mult * entry:
                reason = "stop_mult"
            elif left <= p.final_exit_td:
                reason = "final_exit"
            elif left <= p.time_exit_td and dl == dl and dl > p.near_money_delta:
                reason = "time_exit"
            if reason:
                if reason == "expiry":
                    fill = max(c.strike - und, 0.0) if c.opt_type == "PE" else max(und - c.strike, 0.0)
                else:
                    ref = row[0] if (traded and row[0] == row[0] and row[0] > 0) else mark
                    v = row[1] if row is not None else 0.0
                    fill = ref * (1 + wb.half_spread(p, max(v, float(c.volume))) + (0 if traded else p.untraded_slip))
                pnl = (credit - fill) * float(c.lot)
                return (credit - fill) / entry, pnl / float(c.margin), reason, (d - c.trade_date).days
        return None

    return entry_ok, walk


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

def score(df: pd.DataFrame, sig: str, y: str = "kept") -> dict:
    x = df[["month", sig, y]].replace([np.inf, -np.inf], np.nan).dropna()
    ics, spreads = [], []
    for _, g in x.groupby("month"):
        if len(g) < 20 or g[sig].nunique() < 5:
            continue
        ics.append(g[sig].rank().corr(g[y].rank()))
        q = pd.qcut(g[sig].rank(method="first"), 5, labels=False)
        spreads.append(g.loc[q == 4, y].mean() - g.loc[q == 0, y].mean())
    ics = np.array([i for i in ics if i == i])
    if len(ics) == 0:
        return dict(n=len(x), months=0)
    return dict(n=len(x), months=len(ics), ic=round(float(ics.mean()), 3),
                ic_t=round(float(ics.mean() / (ics.std(ddof=1) / np.sqrt(len(ics)))), 2) if len(ics) > 2 else None,
                ic_pos=round(float((ics > 0).mean()), 2), q5_q1=round(float(np.nanmean(spreads)), 3))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    wb.add_param_args(ap)
    ap.add_argument("--split", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=IS_END_DEFAULT,
                    help="first out-of-sample day (HAR is fitted before it)")
    ap.add_argument("--out", type=Path, default=wb.WHEEL_DB.parent)
    a = ap.parse_args(argv)
    # the reference setup; any wheel_bt flag overrides it
    base = replace(wb.Params(), entry_mode="all", stop_mult=3.0, dte_lo=21, dte_hi=45, min_rich=None)
    p = wb.params_from_args(a, base)
    return run(p, a.split, a.out)


def run(p: wb.Params, split: date, out: Path, con=None) -> int:
    print(f"  setup: {p.start}..{p.end} dte {p.dte_lo}-{p.dte_hi} rom>={p.rom_target} delta<={p.delta_cap} "
          f"stop {p.stop_mult}x universe {p.universe} prices {p.price_basis}")

    own = con is None
    con = con or wb.connect()
    try:
        data = wb.load_data(p, con)
        c = data.cands.copy()
        entry_ok, walk = make_walker(p, data)
        keep = [entry_ok(r.symbol, r.trade_date, r.expiry) for r in c.itertuples(index=False)]
        c = c[keep].reset_index(drop=True)
        print(f"  {len(c):,} candidates after the strategy's entry blackouts")
        outs = [walk(r) for r in c.itertuples(index=False)]
        c["kept"] = [o[0] if o else np.nan for o in outs]
        c["pnl_margin"] = [o[1] if o else np.nan for o in outs]
        c["exit_reason"] = [o[2] if o else None for o in outs]
        c["days_held"] = [o[3] if o else np.nan for o in outs]
        c = c[c["kept"].notna()].reset_index(drop=True)

        t = np.array([max((e - d).days, 1) / 365.0 for d, e in zip(c["trade_date"], c["expiry"])])
        c["iv_k"] = iv_vec(c["opt_type"] == "CE", c["premium"], c["und"], c["strike"], t)
        span_model = bs_vec(c["opt_type"] == "CE", c["und"], c["strike"], t, c["svol"].where(c["svol"] > 0))
        c["span_rich"] = np.where(span_model > 1e-6, c["premium"] / span_model, np.nan)
        c = c.merge(load_atm_iv(con, c), on=["trade_date", "raw", "expiry"], how="left")
        vol = load_vol(con, p.start, p.end, data.results, split)
    finally:
        if own:
            con.close()

    c["d"] = pd.to_datetime(c["trade_date"])
    c = c.merge(vol.rename(columns={"sym": "symbol"}), on=["symbol", "d"], how="left")
    c["smile"] = c["iv_k"] / c["atm_iv"]
    for h in ("hv10", "hv20", "hv60", "ewma", "hvx", "har"):
        c[f"vrp_{h}"] = c["atm_iv"] / c[h]
    c["ivk_har"] = c["iv_k"] / c["har"]
    c["month"] = c["d"].dt.to_period("M")
    c["period"] = np.where(c["trade_date"] < split, "IS", "OOS")

    rows = []
    for per in ("IS", "OOS"):
        for side in ("all", "PE", "CE"):
            sub = c[(c["period"] == per) & ((c["opt_type"] == side) if side != "all" else True)]
            for s in SIGNALS:
                for y in ("kept", "pnl_margin"):
                    rows.append(dict(period=per, side=side, signal=s, outcome=y, **score(sub, s, y)))
    summ = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    cols = ["trade_date", "symbol", "opt_type", "expiry", "strike", "premium", "lot", "margin", "volume", "delta",
            "und", "svol", "iv_k", "atm_iv", "hv10", "hv20", "hv60", "ewma", "hvx", "har"] + SIGNALS + \
           ["kept", "pnl_margin", "exit_reason", "days_held", "period"]
    c[cols].to_csv(out / "wheel_signals_candidates.csv", index=False)
    summ.to_csv(out / "wheel_signals_summary.csv", index=False)

    with pd.option_context("display.width", 200, "display.max_rows", 200):
        k = summ[(summ["outcome"] == "kept") & (summ["side"] == "all")]
        piv = k.pivot(index="signal", columns="period", values=["ic", "ic_t", "ic_pos", "q5_q1"])
        print("\n  Share of premium kept -- monthly rank IC and top-minus-bottom quintile (all sides):")
        print("  " + piv.reindex(SIGNALS).to_string().replace("\n", "\n  "))
        print(f"\n  base rates: kept mean IS {c.loc[c.period=='IS','kept'].mean():.3f}, "
              f"OOS {c.loc[c.period=='OOS','kept'].mean():.3f}; candidates IS {int((c.period=='IS').sum()):,}, "
              f"OOS {int((c.period=='OOS').sum()):,}")
        print(f"  wrote {out / 'wheel_signals_candidates.csv'} and {out / 'wheel_signals_summary.csv'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
