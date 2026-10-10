"""
build_exposure_history.py
=========================
Reconstruct the exposure-metric feature set from historical EOD option chains.

    python build_exposure_history.py --from 2020-06-15 --to 2026-09-15
    python build_exposure_history.py --from 2024-01-01 --to 2024-12-31 --symbols NIFTY,RELIANCE

Writes to `exposure_hist` in oc.duckdb -- a SEPARATE table from `exposure_eod`.
That separation is deliberate: exposure_eod is built from live intraday chain
snapshots, this is built from EOD settlement data, and the two will not agree
exactly. Merging them would make it impossible to tell later whether a backtest
result came from observed data or from reconstruction. Where the two overlap
(2026-01-23 onward) they can be compared, which is the only honest way to find
out how good the reconstruction is.

Where the values come from, and the judgement calls
---------------------------------------------------

FORWARD from futures_eod, not spot. Black-76 on the observed futures settlement
price removes the need to assume a risk-free rate and a dividend yield per
symbol per day, neither of which is known historically. Spot is carried for
moneyness and basis only.

TIME in TRADING days / 243, not calendar / 365. This is the convention that
makes atm_iv directly comparable to hv20 and em_1d, which straddle_core
annualises on 243. Quoting on the calendar convention instead would make IV and
RV differ by sqrt(365/243) = 1.23x -- a 23% phantom variance premium in every
comparison. atm_iv_cal is stored alongside for anyone who needs the
market-quoted number; the two are related exactly by sqrt(T_trad / T_cal).

VOLUME > 0 FILTER. Between 60% and 80% of pre-2024 option rows never traded --
their `close` is a modelled settlement price, not a market price. Fitting a
smile to those invents a surface. Rows fall back to the oi > 0 universe only
when fewer than MIN_TRADED_STRIKES actually traded, and `iv_source` plus
`confidence` record which path was taken so downstream can filter on it.

GEX SIGN CONVENTION: dealers long calls, short puts, so net_gex sums call gamma
OI minus put gamma OI. This is the standard dealer-positioning assumption, not
an observed fact. It is stated here so that a sign disagreement with
exposure_eod is recognisable as a convention difference rather than a bug.
Note this also means a chain with identical CE and PE open interest at every
strike nets to exactly zero, because a call and a put on the same strike have
identical gamma.

25-DELTA WINGS are interpolated in DELTA space and return NaN unless the target
is bracketed by quoted strikes. Extrapolating past the last traded strike would
invent wings that were never priced.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vol_math as vm  # noqa: E402
import exposure_core as core  # noqa: E402  (shared dealer gamma profile)

TRADING_DAYS = 243.0
CALENDAR_DAYS = 365.0
SQRT_2_OVER_PI = 0.7978845608        # E|Z| for a standard normal
MIN_STRIKES = 5
MIN_TRADED_STRIKES = 8               # below this, fall back to the oi > 0 set
SMILE_RANGE_PCT = 0.25               # wide window: IV surface and 25d wings
#: Exposure aggregates (net_gex, gamma flip, neg_gamma_fraction, OI in range)
#: are summed over a NARROW window instead, because they are sums over whichever
#: strikes you include -- the window IS part of the definition.
#:
#: Measured against exposure_eod for Feb-2026, the live pipeline's window is
#: spot +/- 1.124 * expected_move, where expected_move is
#:
#:     spot * atm_iv * sqrt(dte_CALENDAR / 243)
#:
#: i.e. CALENDAR days over a TRADING-day annualisation, and with NO sqrt(2/pi)
#: factor -- it is a one-standard-deviation move despite the name. Ratio check
#: against that form came out 1.0213, against the calendar-365 form 1.2517.
#:
#: Getting this wrong is not subtle. A first attempt used
#: 0.798 * sigma * sqrt(dte_TRADING/243), which is 0.798 * sqrt(2/3) = 0.65 of
#: the right width; the resulting window held 11 strikes against the live 14 and
#: pushed flip_norm_distance agreement DOWN from 0.46 to 0.29.
EXPOSURE_EM_MULT = 1.124
EXPOSURE_FALLBACK_PCT = 0.10         # used when atm_iv is unavailable

SCHEMA = """
CREATE TABLE IF NOT EXISTS exposure_hist (
  symbol VARCHAR, date DATE, expiry DATE, expiry_rank TINYINT, dte INTEGER,
  dte_trading INTEGER,
  fut_price DOUBLE, spot DOUBLE, basis DOUBLE, basis_pct DOUBLE,
  atm_strike DOUBLE, strike_interval DOUBLE,
  atm_iv DOUBLE,            -- annualised on 243 trading days
  atm_iv_cal DOUBLE,        -- annualised on 365 calendar days (market quote)
  atm_straddle DOUBLE, atm_ce DOUBLE, atm_pe DOUBLE,
  iv_25c DOUBLE, iv_25p DOUBLE, rr25 DOUBLE, bf25 DOUBLE,
  net_gex DOUBLE, gex_regime VARCHAR, gamma_flip DOUBLE,
  flip_norm_distance DOUBLE, neg_gamma_fraction DOUBLE,
  peak_pos_gamma_strike DOUBLE, peak_neg_gamma_strike DOUBLE,
  net_vanna DOUBLE, net_charm DOUBLE,
  range_lo DOUBLE, range_hi DOUBLE, n_strikes_exposure INTEGER,
  total_oi_in_range DOUBLE, total_vol_in_range DOUBLE,
  n_strikes INTEGER, n_strikes_traded INTEGER, traded_frac DOUBLE,
  grid_density DOUBLE, confidence VARCHAR, iv_source VARCHAR,
  lot DOUBLE, lot_src VARCHAR,
  hv20 DOUBLE, em_1d DOUBLE, expected_move DOUBLE, mean_abs_move DOUBLE,
  computed_at TIMESTAMP,
  PRIMARY KEY (symbol, date, expiry)
);
"""


#: Columns added after the table was first created. CREATE TABLE IF NOT EXISTS
#: silently skips an existing table, so a schema change made only in SCHEMA
#: above never reaches a database that already has the table -- the run then
#: fails on the first INSERT ... BY NAME with a missing-column error. Every new
#: column must be listed here too.
MIGRATIONS = [
    ("range_lo", "DOUBLE"),
    ("range_hi", "DOUBLE"),
    ("n_strikes_exposure", "INTEGER"),
    ("mean_abs_move", "DOUBLE"),
    # Dealer gamma profile + dealer-signed vanna (exposure_core, 2026-10-03).
    # Same function and same definitions as exposure_eod, so these columns are
    # comparable across the two tables (unlike the legacy gamma_flip /
    # flip_norm_distance, which here is a FRACTION OF SPOT, not EM units).
    ("profile_flip", "DOUBLE"),
    ("gamma_flip_strike", "DOUBLE"),
    ("flip_norm_distance_strike", "DOUBLE"),
    ("gex_regime_strike", "VARCHAR"),
    ("profile_fnd", "DOUBLE"),
    ("profile_regime", "VARCHAR"),
    ("gamma_position", "DOUBLE"),
    ("profile_n_zeros", "INTEGER"),
    ("dealer_vanna", "DOUBLE"),
    ("dealer_vanna_put_wing", "DOUBLE"),
    ("dealer_vanna_atm", "DOUBLE"),
    ("dealer_vanna_call_wing", "DOUBLE"),
    ("gross_vanna", "DOUBLE"),
    ("dealer_vanna_norm", "DOUBLE"),
]


def connect(db: str, bhav: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db)
    con.execute(f"ATTACH IF NOT EXISTS '{bhav}' AS bhav (READ_ONLY)")
    con.execute(SCHEMA)
    for col, typ in MIGRATIONS:
        con.execute(f"ALTER TABLE exposure_hist ADD COLUMN IF NOT EXISTS {col} {typ}")
    return con


def trading_day_index(con) -> dict:
    """trade_date -> ordinal trading-day number.

    DTE in TRADING days must come from the observed calendar, not a weekday
    count: NSE holidays would otherwise inflate it and bias every IV.
    """
    d = con.execute(
        "SELECT DISTINCT trade_date FROM bhav.cash_eod ORDER BY trade_date"
    ).df()["trade_date"]
    d = pd.to_datetime(d).dt.date
    return {dd: i for i, dd in enumerate(d)}


def load_day(con, d: date, symbols: list | None):
    filt = ""
    extra: list = []
    if symbols:
        filt = f" AND symbol IN ({','.join('?' * len(symbols))})"
        extra = list(symbols)
    opt = con.execute(f"""
        SELECT symbol, expiry, strike, opt_type, close, settle, volume, oi,
               spot, lot, lot_src
        FROM bhav.options_eod
        WHERE trade_date = ? AND strike > 0 AND expiry >= ?{filt}
    """, [d, d] + extra).df()
    fut = con.execute(f"""
        SELECT symbol, expiry, close AS fut_price, spot
        FROM bhav.futures_eod WHERE trade_date = ? AND close > 0{filt}
    """, [d] + extra).df()
    return opt, fut


def hv20_map(con, d: date) -> dict:
    """Trailing 20-trading-day close-to-close vol, annualised on 243.

    CA-adjusted via close/prevclose. Point-in-time -- only data up to `d`.
    """
    df = con.execute("""
        WITH r AS (
            SELECT symbol, ln(close / prevclose) AS lr,
                   row_number() OVER (PARTITION BY symbol ORDER BY trade_date DESC) rn
            FROM bhav.cash_eod
            WHERE series = 'EQ' AND trade_date <= ? AND close > 0 AND prevclose > 0
        )
        SELECT symbol, stddev_samp(lr) * sqrt(243) * 100 AS hv20
        FROM r WHERE rn <= 20 GROUP BY symbol HAVING count(*) >= 15
    """, [d]).df()
    return dict(zip(df["symbol"], df["hv20"]))


def _gamma_flip(strikes: np.ndarray, net_gamma: np.ndarray, fwd: float) -> float:
    """Strike at which CUMULATIVE net dealer gamma crosses zero.

    Cumulative rather than per-strike: the flip is where aggregate positioning
    changes sign as spot traverses the chain, which is what decides whether
    dealer hedging damps or amplifies a move. Linearly interpolated between the
    bracketing strikes; when several crossings exist, the one nearest the
    forward is the relevant one.
    """
    order = np.argsort(strikes)
    k, g = strikes[order], net_gamma[order]
    cum = np.cumsum(g)
    cross = np.where(np.diff(np.sign(cum)) != 0)[0]
    if cross.size == 0:
        return np.nan
    i = cross[np.argmin(np.abs(k[cross] - fwd))]
    c0, c1 = cum[i], cum[i + 1]
    if c1 == c0:
        return float(k[i])
    return float(k[i] + (-c0 / (c1 - c0)) * (k[i + 1] - k[i]))


def _profile_from_long(K, is_call, oi, iv, ok, t_trad: float, t_cal: float,
                       lot: float, fwd: float, em: float) -> dict:
    """Pivot the long (strike, CE/PE) rows to per-strike legs and run the shared
    exposure_core.dealer_gamma_profile.

    IV here is annualised on 243 TRADING days; the shared function (like
    exposure_eod) works on CALENDAR time, T = DTE/365, with calendar-quoted IV.
    Gamma and vanna depend on sigma*sqrt(T) and on sigma itself, so convert the
    IV to the calendar quote (same total variance) instead of passing a
    trading-day sigma with a calendar T. Otherwise vanna "per vol point" would
    mean a different vol point in the two tables.
    """
    m = ok & np.isfinite(iv) & (oi > 0)
    if not (np.isfinite(em) and em > 0) or m.sum() < 2 or not (t_cal > 0):
        return core.dealer_gamma_profile([], [], [], [], [], 1, 0, 0, 0)  # all None
    to_cal_pct = np.sqrt(t_trad / t_cal) * 100.0
    d = pd.DataFrame({"k": K[m], "c": is_call[m], "oi": oi[m], "iv": iv[m] * to_cal_pct})
    ce = d[d["c"]].groupby("k").agg(oi=("oi", "sum"), iv=("iv", "mean"))
    pe = d[~d["c"]].groupby("k").agg(oi=("oi", "sum"), iv=("iv", "mean"))
    ks = ce.index.union(pe.index)
    ce, pe = ce.reindex(ks, fill_value=0.0), pe.reindex(ks, fill_value=0.0)
    return core.dealer_gamma_profile(
        ks.to_numpy(float), ce["oi"].to_numpy(), pe["oi"].to_numpy(),
        ce["iv"].to_numpy(), pe["iv"].to_numpy(),
        lot, fwd, t_cal * CALENDAR_DAYS, em)


def compute_symbol_expiry(g: pd.DataFrame, fwd: float, spot: float,
                          t_trad: float, t_cal: float, lot: float,
                          lot_src) -> dict | None:
    """All metrics for one (symbol, expiry) chain on one day.

    Two strike windows, deliberately:
      * SMILE window (+/-25%) for IV inversion, ATM IV and the 25-delta wings.
        More strikes give a better-determined smile.
      * EXPOSURE window (spot +/- 1.1 expected moves) for net_gex, gamma flip,
        neg_gamma_fraction and OI-in-range. These are sums over whichever
        strikes you choose to include, so the window IS part of the definition;
        matching the live pipeline's band is what makes them comparable.
    """
    g = g[(g["strike"] > fwd * (1 - SMILE_RANGE_PCT))
          & (g["strike"] < fwd * (1 + SMILE_RANGE_PCT))]
    if len(g) < MIN_STRIKES:
        return None

    traded = g[g["volume"] > 0]
    n_traded = int(traded["strike"].nunique())
    if n_traded >= MIN_TRADED_STRIKES:
        use, iv_source = traded, "traded"
    else:
        use, iv_source = g[g["oi"] > 0], "settle_oi"
    if len(use) < MIN_STRIKES:
        return None

    price = use["close"].to_numpy(float)
    price = np.where(price > 0, price, use["settle"].to_numpy(float))
    K = use["strike"].to_numpy(float)
    is_call = (use["opt_type"].astype(str).str.upper() == "CE").to_numpy()
    F = np.full(K.shape, fwd)
    T = np.full(K.shape, t_trad)

    iv = vm.black76_iv(price, F, K, T, is_call)
    gk = vm.black76_greeks(F, K, T, iv, is_call)
    ok = np.isfinite(iv)
    if ok.sum() < MIN_STRIKES:
        return None

    oi = use["oi"].to_numpy(float)
    vol_ = use["volume"].to_numpy(float)
    ks = np.unique(K)
    interval = float(np.median(np.diff(ks))) if ks.size > 1 else np.nan

    atm_k = float(ks[np.argmin(np.abs(ks - fwd))])
    at = ok & (K == atm_k)
    atm_iv = float(np.nanmean(iv[at])) if at.any() else np.nan
    ce = g.loc[(g["strike"] == atm_k) & (g["opt_type"].str.upper() == "CE"), "close"]
    pe = g.loc[(g["strike"] == atm_k) & (g["opt_type"].str.upper() == "PE"), "close"]
    atm_ce = float(ce.iloc[0]) if len(ce) else np.nan
    atm_pe = float(pe.iloc[0]) if len(pe) else np.nan

    cm, pm = ok & is_call, ok & ~is_call
    iv25c = vm.interp_smile_at_delta(K[cm], iv[cm], gk["delta"][cm], 0.25)
    iv25p = vm.interp_smile_at_delta(K[pm], iv[pm], gk["delta"][pm], 0.25)
    both = np.isfinite(iv25c) and np.isfinite(iv25p)
    rr25 = (iv25c - iv25p) if both else np.nan
    bf25 = ((iv25c + iv25p) / 2 - atm_iv) if both and np.isfinite(atm_iv) else np.nan

    # Bhavcopy OI is already in SHARES (contracts x lot) in every year 2020-2026:
    # 87-100% of OI values are exact lot multiples, <1% below one lot. Multiplying
    # by the lot again (the original code) inflated every hist exposure (net_gex,
    # net_vanna, net_charm, dealer vanna) by the lot size -- 25x to 30,000x across
    # symbols -- which leaves within-chain ratios intact (flip, regime, gamma
    # position, vanna share) but makes raw magnitudes incomparable across symbols
    # and ~550x eod's on the median chain. Found 2026-10-03 comparing
    # exposure_hist vs exposure_eod dealer_vanna on 2026-08-20 (ratio 538-587,
    # median lot 550). exposure_eod uses ocdata OI in CONTRACTS x lot = shares,
    # so with mult = 1 the two tables are on the same scale.
    mult = 1.0
    gsign = np.where(is_call, 1.0, -1.0)
    gex_all = np.where(ok, gk["gamma"] * oi * mult * fwd * fwd * 0.01 * gsign, 0.0)
    van_all = np.where(ok, gk["vanna"] * oi * mult * gsign, 0.0)
    chm_all = np.where(ok, gk["charm"] * oi * mult * gsign, 0.0)

    # Narrow exposure window, matching the live pipeline's definition exactly.
    # NOTE this is a one-SD move, NOT the mean absolute move the ATM straddle
    # prices (which is 0.798 of it). The name `expected_move` is the live
    # pipeline's; it is kept for comparability, not because it is accurate.
    if np.isfinite(atm_iv) and np.isfinite(t_cal) and t_cal > 0:
        dte_cal = t_cal * CALENDAR_DAYS
        em_h = spot * atm_iv * np.sqrt(dte_cal / TRADING_DAYS)
        half = EXPOSURE_EM_MULT * em_h
    else:
        em_h = np.nan
        half = EXPOSURE_FALLBACK_PCT * spot
    lo_b, hi_b = spot - half, spot + half
    win = (K >= lo_b) & (K <= hi_b)
    if win.sum() < MIN_STRIKES:                 # too thin: widen to the fallback
        lo_b = spot * (1 - EXPOSURE_FALLBACK_PCT)
        hi_b = spot * (1 + EXPOSURE_FALLBACK_PCT)
        win = (K >= lo_b) & (K <= hi_b)

    per_k = (pd.DataFrame({"k": K[win], "gex": gex_all[win]})
             .groupby("k")["gex"].sum())
    net_gex = float(per_k.sum()) if len(per_k) else np.nan
    flip = (_gamma_flip(per_k.index.to_numpy(float), per_k.to_numpy(float), fwd)
            if len(per_k) > 1 else np.nan)

    # Shared dealer gamma profile on the whole smile-window chain (not the
    # narrow exposure window: OI walls just outside it still carry gamma at spot).
    prof = _profile_from_long(K, is_call, oi, iv, ok, t_trad, t_cal, mult, fwd, em_h)

    return {
        "fut_price": fwd, "spot": spot,
        "basis": fwd - spot,
        "basis_pct": ((fwd - spot) / spot * 100) if spot else np.nan,
        "atm_strike": atm_k, "strike_interval": interval,
        "atm_iv": atm_iv * 100 if np.isfinite(atm_iv) else np.nan,
        "atm_iv_cal": (atm_iv * 100 * np.sqrt(t_trad / t_cal)
                       if np.isfinite(atm_iv) and t_cal > 0 else np.nan),
        "atm_straddle": ((atm_ce + atm_pe)
                         if np.isfinite(atm_ce) and np.isfinite(atm_pe) else np.nan),
        "atm_ce": atm_ce, "atm_pe": atm_pe,
        "iv_25c": iv25c * 100 if np.isfinite(iv25c) else np.nan,
        "iv_25p": iv25p * 100 if np.isfinite(iv25p) else np.nan,
        "rr25": rr25 * 100 if np.isfinite(rr25) else np.nan,
        "bf25": bf25 * 100 if np.isfinite(bf25) else np.nan,
        "net_gex": net_gex,
        # CANONICAL names carry the shared PROFILE definition (agreed 2026-10-04),
        # the same function and units as exposure_eod: flip_norm_distance is now
        # in EXPECTED-MOVE units, not a fraction of spot.
        "gex_regime": prof.get("profile_regime"),
        "gamma_flip": prof.get("profile_flip"),
        "flip_norm_distance": prof.get("profile_fnd"),
        # LEGACY hist definitions, kept under the *_strike names exposure_eod
        # uses for its legacy columns (the constructions differ: here the regime
        # was the window net GEX sign, the flip a cumulative-sum crossing, and the
        # distance a FRACTION OF SPOT). The old caveat stands for these: they
        # reconstruct the live per-strike flip poorly; do NOT backtest on them.
        "gex_regime_strike": "positive" if net_gex > 0 else "negative",
        "gamma_flip_strike": flip,
        "flip_norm_distance_strike": ((flip - spot) / spot) if np.isfinite(flip) else np.nan,
        "neg_gamma_fraction": float((per_k < 0).mean()) if len(per_k) else np.nan,
        "peak_pos_gamma_strike": float(per_k.idxmax()) if len(per_k) else np.nan,
        "peak_neg_gamma_strike": float(per_k.idxmin()) if len(per_k) else np.nan,
        "net_vanna": float(np.nansum(van_all[win])),
        "net_charm": float(np.nansum(chm_all[win])),
        "range_lo": lo_b, "range_hi": hi_b,
        "expected_move": em_h,
        "n_strikes_exposure": int(np.unique(K[win]).size),
        "total_oi_in_range": float(np.nansum(oi[win])),
        "total_vol_in_range": float(np.nansum(vol_[win])),
        "n_strikes": int(ks.size), "n_strikes_traded": n_traded,
        "traded_frac": (n_traded / ks.size) if ks.size else np.nan,
        "grid_density": (interval / fwd) if np.isfinite(interval) and fwd else np.nan,
        "iv_source": iv_source,
        "confidence": ("high" if iv_source == "traded" and n_traded >= 15
                       else "medium" if iv_source == "traded" else "low"),
        "lot": lot, "lot_src": lot_src,
        **prof,
    }


def build_day(con, d: date, tdi: dict, symbols: list | None) -> pd.DataFrame:
    opt, fut = load_day(con, d, symbols)
    if opt.empty or fut.empty:
        return pd.DataFrame()

    hv = hv20_map(con, d)
    fut_px = {(r.symbol, r.expiry): (r.fut_price, r.spot)
              for r in fut.itertuples(index=False)}
    d_idx = tdi.get(d)

    rows = []
    for (sym, exp), g in opt.groupby(["symbol", "expiry"], sort=False):
        fp = fut_px.get((sym, exp))
        if fp is None:
            continue                      # no futures for that expiry -> no forward
        fwd, fspot = fp
        spot = float(g["spot"].dropna().iloc[0]) if g["spot"].notna().any() else fspot
        if not (np.isfinite(fwd) and fwd > 0 and np.isfinite(spot) and spot > 0):
            continue

        dte_cal = (exp - d).days
        e_idx = tdi.get(exp)
        dte_trad = ((e_idx - d_idx) if (e_idx is not None and d_idx is not None)
                    else max(int(round(dte_cal * TRADING_DAYS / CALENDAR_DAYS)), 1))
        if dte_cal <= 0 or dte_trad <= 0:
            continue

        lv = g["lot"].dropna()
        lot = float(lv.iloc[0]) if len(lv) else np.nan
        ls = g["lot_src"].dropna()
        lot_src = str(ls.iloc[0]) if len(ls) else None

        m = compute_symbol_expiry(g, float(fwd), float(spot),
                                  dte_trad / TRADING_DAYS, dte_cal / CALENDAR_DAYS,
                                  lot, lot_src)
        if m is None:
            continue
        m.update({"symbol": sym, "date": d, "expiry": exp,
                  "dte": dte_cal, "dte_trading": int(dte_trad)})

        h = hv.get(sym, np.nan)
        m["hv20"] = h
        # em_1d matches the live pipeline's blend. NOTE this is a STANDARD
        # DEVIATION, not an expected move: the ATM straddle prices
        # E|move| = 0.798 * sigma. straddle_core applies that factor itself.
        if np.isfinite(m["atm_iv"]) and np.isfinite(h):
            sd1 = (0.25 * m["atm_iv"] + 0.75 * h) / 100 / np.sqrt(TRADING_DAYS)
        elif np.isfinite(m["atm_iv"]):
            sd1 = m["atm_iv"] / 100 / np.sqrt(TRADING_DAYS)
        else:
            sd1 = np.nan
        m["em_1d"] = sd1
        # mean absolute move, for anyone who wants the quantity the ATM
        # straddle actually prices rather than the live pipeline's 1-SD figure
        m["mean_abs_move"] = (SQRT_2_OVER_PI * m["expected_move"]
                              if np.isfinite(m.get("expected_move", np.nan)) else np.nan)
        rows.append(m)

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df["expiry_rank"] = (df.groupby("symbol")["expiry"]
                         .rank(method="dense").astype(int) - 1)
    df["computed_at"] = datetime.now()
    return df


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    ap.add_argument("--bhav", default=r"C:\Users\aniru\od\bhavcopy.duckdb")
    ap.add_argument("--from", dest="from_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", required=True,
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--symbols", default=None,
                    help="comma-separated; omit for the whole universe")
    ap.add_argument("--max-rank", type=int, default=2,
                    help="keep expiry_rank 0..N (default 2)")
    ap.add_argument("--replace", action="store_true",
                    help="delete existing rows in range before writing")
    args = ap.parse_args()

    symbols = args.symbols.split(",") if args.symbols else None
    con = connect(args.db, args.bhav)
    tdi = trading_day_index(con)

    if args.replace:
        n = con.execute("SELECT count(*) FROM exposure_hist WHERE date BETWEEN ? AND ?",
                        [args.from_date, args.to_date]).fetchone()[0]
        con.execute("DELETE FROM exposure_hist WHERE date BETWEEN ? AND ?",
                    [args.from_date, args.to_date])
        print(f"deleted {n:,} existing row(s) in range")

    days = con.execute("""
        SELECT DISTINCT trade_date FROM bhav.options_eod
        WHERE trade_date BETWEEN ? AND ? ORDER BY trade_date
    """, [args.from_date, args.to_date]).df()["trade_date"].tolist()
    print(f"{len(days)} day(s) with option data")

    total, errs = 0, 0
    t0 = datetime.now()
    for i, d in enumerate(days, 1):
        d = d if isinstance(d, date) else pd.Timestamp(d).date()
        try:
            df = build_day(con, d, tdi, symbols)
        except Exception as exc:  # noqa: BLE001
            print(f"  {d}: ERROR {type(exc).__name__}: {exc}")
            errs += 1
            continue
        if df.empty:
            continue
        df = df[df["expiry_rank"] <= args.max_rank]
        con.register("_eh", df)
        try:
            con.execute("INSERT OR REPLACE INTO exposure_hist BY NAME SELECT * FROM _eh")
        except Exception as exc:  # noqa: BLE001
            # Most likely a column in the frame that the table lacks. Fail loudly
            # and immediately rather than after deleting the range: a silent
            # partial run that leaves stale rows in place is worse than a crash.
            con.unregister("_eh")
            print(f"\nINSERT FAILED on {d}: {exc}")
            print("If this names a missing column, add it to MIGRATIONS and re-run.")
            raise
        con.unregister("_eh")
        total += len(df)
        if i % 25 == 0 or i == len(days):
            el = (datetime.now() - t0).total_seconds()
            print(f"  [{i}/{len(days)}] {d}  rows={total:,}  "
                  f"{el:.0f}s elapsed, ~{el / i * (len(days) - i) / 60:.1f}m left")

    print(f"\nwrote {total:,} rows to exposure_hist ({errs} day(s) errored)")

    # em_1d with the shared recalibrated model (oc/em1d.py). Overwrites the
    # per-row value build_day wrote, for exactly the range just rebuilt; the
    # rolling scale is refreshed from the table first, so it includes these days.
    import em1d
    em1d.run(con, args.bhav, hist_range=(args.from_date, args.to_date))
    q = con.execute("""
        SELECT count(*) n, count(DISTINCT symbol) syms, count(DISTINCT date) dates,
               round(100.0*count(*) FILTER (WHERE atm_iv IS NULL)/count(*), 2) iv_null,
               round(100.0*count(*) FILTER (WHERE rr25 IS NULL)/count(*), 2) rr_null,
               round(100.0*count(*) FILTER (WHERE gamma_flip IS NULL)/count(*), 2) flip_null,
               round(avg(traded_frac)*100, 1) traded_pct,
               round(100.0*count(*) FILTER (WHERE profile_regime IS NULL)/count(*), 2) prof_null
        FROM exposure_hist WHERE date BETWEEN ? AND ?
    """, [args.from_date, args.to_date]).fetchone()
    print(f"  rows={q[0]:,} symbols={q[1]} dates={q[2]}")
    print(f"  NULL: atm_iv {q[3]}%  rr25 {q[4]}%  gamma_flip {q[5]}%   "
          f"avg traded strikes {q[6]}%  profile_regime {q[7]}%")
    print(con.execute("""
        SELECT confidence, iv_source, count(*) n FROM exposure_hist
        WHERE date BETWEEN ? AND ? GROUP BY 1, 2 ORDER BY 3 DESC
    """, [args.from_date, args.to_date]).df().to_string(index=False))
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
