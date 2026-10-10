"""
backtest_straddle.py  (v2)
==========================
Point-in-time backtest of the straddle screener over completed monthly expiries.

    python backtest_straddle.py --dte 25 --paths 8000 --out bt.csv
    python backtest_straddle.py --dte 25 --timing-gate --out bt_timed.csv
    python backtest_straddle.py --dte 35 --dte 25 --dte 15        # (run each)

What changed from v1, and why
-----------------------------
v1 ran on 7 expiries because its data sources only reached 2026-01. Every one
has been replaced:

  completed_expiries   exposure_eod  -> exposure_hist   (7 expiries -> ~60)
  chain premiums       ocdata        -> exposure_hist.atm_ce / atm_pe
  returns              cash_eod raw  -> cash_returns_adj.log_ret  (CA-adjusted)
  event dates          tvnews (empty)-> bhav.corporate_events, lag T+1
  liquidity            bid/ask       -> traded_frac x log(OI) proxy

SCORING, cut twice by measurement
---------------------------------
ev_per_margin / cushion_net / pop / cvar05 correlated 0.96-1.00 -- one term
survives, cvar becomes a gate. vrp_z uses the Goyal-Saretto form
ln(HV20) - ln(IV), NOT a model forecast: with the best RV forecast being
debiased ATM IV (beta 0.843), ln(IV) - ln(RV_model) reduces to 0.157*ln(IV),
which is IV level wearing a disguise. trend_eff is now +1 (see straddle_core).

FIVE JUDGEMENT CALLS, all reversible
------------------------------------
1. LIQUIDITY PROXY. options_eod carries no bid/ask, so the spread gate cannot
   run before 2026. Proxied by traded_frac * ln(1+OI). Cross-check it against
   real spreads on the 2026 overlap before trusting it.
2. CORPORATE ACTIONS IN THE WINDOW. NSE adjusts strikes on a split, so a
   straddle spanning one is not the same contract at exit. Modelling that is
   out of scope, so those (symbol, expiry) pairs are EXCLUDED. Silently wrong
   P&L is worse than a smaller sample.
3. REALISED PATH uses raw closes, which is safe ONLY because of (2) -- with no
   CA in the window, raw prices are already continuous.
4. IV_RANK is computed here (not a column in exposure_hist): point-in-time
   percentile of atm_iv over a trailing window, per symbol.
5. EXPIRY_ROLLED does not exist in exposure_hist. The ts_slope guard instead
   verifies rank-1 is genuinely the next monthly with a sane DTE gap.

WHAT THIS CAN AND CANNOT SETTLE
-------------------------------
~60 expiries is enough to detect an IC of 0.05 at t~2, which 7 was not. It is
NOT enough to fit weights: keep them equal. And the single largest effect found
so far is not cross-sectional at all -- between-expiry dispersion was 13.4% sd
against a selection effect indistinguishable from zero. --timing-gate tests it.
"""
from __future__ import annotations

import argparse
import dataclasses
import sys
import warnings
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import straddle_core as sc  # noqa: E402

TRADING_DAYS = sc.TRADING_DAYS
EVENT_LAG = 1          # Indian results are released after the close: move is T+1
IVRANK_LOOKBACK = 252  # calendar-ish window for the point-in-time IV percentile


# --------------------------------------------------------------------------
# data access
# --------------------------------------------------------------------------

def connect(db: str, bhav: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(db, read_only=True)
    con.execute(f"ATTACH IF NOT EXISTS '{bhav}' AS bhav (READ_ONLY)")
    return con


def completed_expiries(con, min_symbols: int) -> list:
    return [r[0] for r in con.execute("""
        SELECT expiry, count(DISTINCT symbol) n
        FROM exposure_hist
        WHERE expiry_rank = 0 AND expiry < CURRENT_DATE
        GROUP BY expiry HAVING count(DISTINCT symbol) >= ?
        ORDER BY expiry
    """, [min_symbols]).fetchall()]


def pick_entry_date(con, expiry: date, target_dte: int):
    """Trading date whose DTE is closest to target, at ANY expiry_rank.

    The rank filter used to be expiry_rank = 0, which silently capped the
    reachable DTE. A monthly expiry only BECOMES front-month about 30 days out;
    before that it is rank 1 or 2. Measured on exposure_hist: rank-0 rows exist
    at DTE 31-35 for only 92 of 193 expiries (15.5k rows against 143k at
    DTE <= 15), so a 35-DTE sweep silently sampled the handful of months with
    unusual expiry spacing rather than all of them -- 23 expiries instead of 71,
    and not a random 23.

    Selecting by expiry alone makes every DTE available for every expiry, so a
    DTE sweep is like-for-like.
    """
    row = con.execute("""
        SELECT date FROM exposure_hist
        WHERE expiry = ?
        GROUP BY date, dte ORDER BY abs(dte - ?), -dte LIMIT 1
    """, [expiry, target_dte]).fetchone()
    return row[0] if row else None


def load_snapshot(con, entry: date, expiry: date) -> pd.DataFrame:
    """Target-expiry row plus the NEXT expiry's ATM IV for the term structure.

    No expiry_rank filter on the target: at 35 or 45 DTE the contract is rank 1
    or 2, not 0, and filtering on rank 0 is what limited the DTE sweep.

    The term-structure leg is likewise the expiry immediately AFTER the target
    on that date -- not "rank 1", which would BE the target once the target is
    rank 1 and would give a slope of zero. exposure_hist has no expiry_rolled
    flag, so the guard is explicit: the DTE gap must be in a monthly range, or
    the two contracts are not adjacent monthlies and the slope compares unlike
    things.
    """
    return con.execute("""
        WITH r0 AS (
            SELECT symbol, dte, dte_trading, spot, fut_price, atm_strike,
                   atm_iv, atm_ce, atm_pe, atm_straddle, hv20, em_1d,
                   expected_move, mean_abs_move, lot, lot_src,
                   -- Canonical flip/regime = dealer gamma profile (2026-10-04).
                   -- gamma_score now comes from gamma_position (see
                   -- straddle_core.gamma_regime_score); the old lookup is retired.
                   gex_regime, gamma_flip, gamma_position,
                   neg_gamma_fraction, net_vanna,
                   rr25, bf25, n_strikes, confidence, traded_frac,
                   total_oi_in_range, total_vol_in_range, expiry_rank,
                   compression_days, days_in_regime, days_since_flip,
                   net_gex_norm, dealer_divergence, basis_annualized,
                   gamma_shelf_width, concentration, oi_turnover_ratio,
                   iv_cm30, iv_rank AS iv_rank_stored, regime_compression
            FROM exposure_hist
            WHERE date = ? AND expiry = ?
        ),
        nxt AS (
            SELECT symbol, atm_iv AS atm_iv_next, dte AS dte_next, expiry AS exp1,
                   row_number() OVER (PARTITION BY symbol ORDER BY expiry) AS rn
            FROM exposure_hist
            WHERE date = ? AND expiry > ? AND atm_iv > 0
        )
        SELECT r0.*, n.atm_iv_next, n.dte_next, n.exp1
        FROM r0 LEFT JOIN (SELECT * FROM nxt WHERE rn = 1) n USING (symbol)
    """, [entry, expiry, entry, expiry]).df()


def load_iv_history(con, symbols: list, upto: date) -> dict:
    """Point-in-time ln(HV20/IV) and atm_iv series per symbol.

    Kept on expiry_rank = 0 deliberately: this is a CONSTANT-MATURITY series
    used for the own-history z-score and the IV percentile, so it must track
    one consistent tenor through time. Mixing ranks here would put a 45-day IV
    next to a 5-day IV in the same history and make the z-score meaningless.
    That is a different requirement from the snapshot above, which needs a
    specific contract.
    """
    ph = ",".join("?" * len(symbols))
    df = con.execute(f"""
        SELECT symbol, date, atm_iv, hv20
        FROM exposure_hist
        WHERE expiry_rank = 0 AND date <= ? AND symbol IN ({ph})
          AND atm_iv > 0
        ORDER BY symbol, date
    """, [upto] + list(symbols)).df()
    out = {}
    for s, g in df.groupby("symbol"):
        iv = g["atm_iv"].to_numpy(float)
        hv = g["hv20"].to_numpy(float)
        ok = np.isfinite(hv) & (hv > 0)
        # Goyal-Saretto: ln(trailing realised) - ln(implied). NOT a model
        # forecast -- see the module docstring.
        gs = np.where(ok, np.log(np.where(ok, hv, 1.0) / iv), np.nan)
        out[s] = {"iv": iv, "gs": gs}
    return out


def load_returns(con, symbols: list, upto: date, lookback: int = 600) -> dict:
    """CA-adjusted daily log returns, oldest first, bounded by `upto`.

    log_ret is NULL where a corporate action could not be resolved. Those are
    DROPPED, never zero-filled: a zero fabricates a flat day and deflates every
    variance estimate containing it.
    """
    ph = ",".join("?" * len(symbols))
    df = con.execute(f"""
        SELECT symbol, trade_date, log_ret
        FROM bhav.cash_returns_adj
        WHERE trade_date <= ? AND symbol IN ({ph}) AND log_ret IS NOT NULL
        ORDER BY symbol, trade_date
    """, [upto] + list(symbols)).df()
    return {s: g["log_ret"].to_numpy(float)[-lookback:]
            for s, g in df.groupby("symbol")}


def load_paths(con, symbols: list, entry: date, expiry: date) -> dict:
    """Daily closes from the day after entry through expiry.

    Raw closes are safe here ONLY because ca_in_window() removes any pair with
    a corporate action inside the holding period -- with no CA, the series is
    already continuous. cash_eod CLOSE is the last-30-minute VWAP for NSE
    equities, which is the settlement basis.
    """
    ph = ",".join("?" * len(symbols))
    df = con.execute(f"""
        SELECT symbol, trade_date, close FROM bhav.cash_eod
        WHERE series = 'EQ' AND trade_date > ? AND trade_date <= ?
          AND symbol IN ({ph}) ORDER BY symbol, trade_date
    """, [entry, expiry] + list(symbols)).df()
    return {s: g["close"].to_numpy(float) for s, g in df.groupby("symbol")}


def ca_in_window(con, symbols: list, entry: date, expiry: date) -> set:
    """Symbols with a price-moving corporate action inside the holding window.

    Excluded rather than modelled: NSE adjusts option strikes on a split or
    bonus, so the contract at exit is not the one entered. Dividends are left
    in -- they do not change the strike.
    """
    ph = ",".join("?" * len(symbols))
    try:
        df = con.execute(f"""
            SELECT DISTINCT symbol FROM bhav.corporate_actions
            WHERE ex_date > ? AND ex_date <= ? AND symbol IN ({ph})
              AND ca_type IN ('SPLIT','BONUS','DEMERGER','RIGHTS')
        """, [entry, expiry] + list(symbols)).df()
        return set(df["symbol"])
    except Exception as exc:  # noqa: BLE001
        print(f"  [corporate_actions unavailable: {exc}]")
        return set()


def load_events(con, symbols: list, entry: date, expiry: date) -> dict:
    """Results events inside the holding window, with the T+1 lag applied.

    Measured over the full universe: r2(T)/baseline = 1.41 against
    r2(T+1)/baseline = 4.79. Indian results are released after the close, so
    the move lands the NEXT session. A gate keyed on T alone watches a day on
    which little happens.

    Returns True / False / None. None means the feed has no row for that
    symbol, which is NOT the same as "no event" -- apply_gates distinguishes
    them and GateConfig.require_event_data decides which way it falls.
    """
    ph = ",".join("?" * len(symbols))
    try:
        df = con.execute(f"""
            SELECT DISTINCT symbol, event_date FROM bhav.corporate_events
            WHERE is_results AND NOT coalesce(is_amendment, FALSE)
              AND symbol IN ({ph})
        """, list(symbols)).df()
    except Exception as exc:  # noqa: BLE001
        print(f"  [corporate_events unavailable: {exc}] -- event gate INACTIVE")
        return {}
    if df.empty:
        return {}
    df["event_date"] = pd.to_datetime(df["event_date"]).dt.date
    seen, out = set(df["symbol"]), {}
    lo = entry - pd.Timedelta(days=EVENT_LAG + 1).to_pytimedelta()
    for s in symbols:
        if s not in seen:
            continue
        g = df.loc[df["symbol"] == s, "event_date"]
        out[s] = bool(((g > lo) & (g <= expiry)).any())
    return out


# --------------------------------------------------------------------------
# per-expiry evaluation
# --------------------------------------------------------------------------

def realised_pnl(st: sc.Structure, path: np.ndarray, stop_k_em, em_ref: float) -> dict:
    """Walk the ACTUAL price path under the SAME exit rule the simulator used.

    Comparing a stop-rule prediction against a hold-to-expiry outcome would
    make the calibration meaningless.
    """
    centre = st.legs[0].strike if st.kind == "straddle" else st.ref_price
    credit = sc.net_credit(st)
    hit = None
    if stop_k_em is not None:
        br = np.where(np.abs(path - centre) > stop_k_em * em_ref)[0]
        hit = int(br[0]) if br.size else None

    if hit is None:
        s_exit = float(path[-1])
        exit_val = float(sc._intrinsic(st, np.array([s_exit]))[0])
        exercised, held = True, len(path)
    else:
        s_exit = float(path[hit])
        rem = float(len(path) - 1 - hit)
        tv = (sc.SQRT_2_OVER_PI * sc.deannualise_vol(st.iv_pct)
              * np.sqrt(max(rem, 0.0)) * s_exit)
        exit_val = float(sc._intrinsic(st, np.array([s_exit]))[0] + tv)
        exercised, held = False, hit + 1

    pnl = credit - exit_val - sc.exit_costs(st, exit_val, exercised)
    margin = sc.combo_margin(st)
    return {"realised_pnl": pnl,
            "realised_ret": pnl / st.gross_credit if st.gross_credit > 0 else np.nan,
            "realised_pnl_per_margin": pnl / margin if margin > 0 else np.nan,
            "exit_spot": s_exit, "exit_reason": "STOP" if hit is not None else "EXPIRY",
            "held_days": held,
            "realised_move_pct": abs(float(path[-1]) - centre) / centre * 100.0}


def run_expiry(con, expiry: date, args) -> pd.DataFrame:
    entry = pick_entry_date(con, expiry, args.dte)
    if entry is None:
        return pd.DataFrame()
    df = load_snapshot(con, entry, expiry)
    if df.empty:
        return pd.DataFrame()
    syms = df["symbol"].tolist()

    rets = load_returns(con, syms, entry)
    hist = load_iv_history(con, syms, entry)
    paths = load_paths(con, syms, entry, expiry)
    events = load_events(con, syms, entry, expiry)
    ca_bad = ca_in_window(con, syms, entry, expiry)

    horizon = max(int(df["dte_trading"].median()), 2)
    print(f"\n{expiry}  entry={entry}  dte={df['dte'].median():.0f}  "
          f"h={horizon}td  n={len(df)}  ca_excluded={len(ca_bad)}")

    cfg = sc.GateConfig(min_dte=args.min_dte, max_dte=args.max_dte,
                        max_spread_to_credit=1e9,      # no quotes: see below
                        require_event_data=args.require_events)

    feats, meta = [], []
    for _, r in df.iterrows():
        sym = r["symbol"]
        if sym in ca_bad:
            continue
        rr, path = rets.get(sym), paths.get(sym)
        if rr is None or rr.size < 60 or path is None or path.size < 3:
            continue
        ce, pe = r.get("atm_ce"), r.get("atm_pe")
        iv, spot = r.get("atm_iv"), r.get("spot")
        if not all(pd.notna(x) and float(x) > 0 for x in (ce, pe, iv, spot)):
            continue
        ref = float(r["fut_price"]) if pd.notna(r.get("fut_price")) else float(spot)
        lot = int(r["lot"]) if pd.notna(r.get("lot")) and r["lot"] > 0 else 1

        # Modelled entry spread. Calibrated on 2026 quotes; see
        # straddle_core.SPREAD_LUT. Without this every historical fill is at
        # mid, which overstates returns by 35-84% and inverts the liquidity
        # signal, because the omitted cost is largest exactly where the chain
        # is thinnest.
        tvol = (float(r["total_vol_in_range"])
                if pd.notna(r.get("total_vol_in_range")) else np.nan)
        sfrac = sc.spread_fraction(tvol, float(r["dte"]))

        st = sc.Structure(
            symbol=sym, lot_size=lot, ref_price=ref, iv_pct=float(iv),
            kind="straddle", spread_frac=sfrac,
            legs=[sc.Leg("CE", float(r["atm_strike"]), float(ce), -1),
                  sc.Leg("PE", float(r["atm_strike"]), float(pe), -1)])

        passed, why = sc.apply_gates(
            st, int(r["dte"]), cfg, event_in_window=events.get(sym),
            n_strikes=int(r["n_strikes"]) if pd.notna(r.get("n_strikes")) else None,
            confidence=r.get("confidence"))

        # LIQUIDITY, absolute not ratio. traded_frac separated liquidity
        # regimes 1.2:1 while raw volume separated them 11:1 -- the ratio is
        # near-constant because its denominator shrinks with its numerator, so
        # a 5-of-5 sparse chain scored 1.00 and a deep 20-of-50 scored 0.40.
        liq = float(np.log1p(max(tvol, 0.0))) if np.isfinite(tvol) else np.nan
        tf_disp = (float(r["traded_frac"])
                   if pd.notna(r.get("traded_frac")) else np.nan)  # kept for the
                   # output only, so the old and new liquidity measures can be
                   # compared on the same rows
        if np.isfinite(tvol) and tvol < args.min_volume:
            passed = False
            why = why + [f"volume {tvol:.0f} < {args.min_volume}"]

        try:
            frv = sc.forward_rv(rr, horizon, model=args.model,
                                min_obs=min(args.min_obs, rr.size))
            # --vol-source em1d: anchor the path volatility to the stored em_1d
            # (oc/em1d.py: IV + today's return + RV5 + RV22, rolling scale)
            # instead of the return-only model. Innovations (fat tails) and,
            # for GARCH, the mean-reverting recursion are kept; only the level
            # is replaced. em_1d forecast realised variance to expiry better than
            # EWMA on 2021-26 (rank IC 0.574/0.589 vs 0.532/0.535, QLIKE -0.04
            # to -0.13); whether that ranks STRADDLES better is what this run
            # measures (2026-10-04).
            if args.vol_source == "em1d":
                em = r.get("em_1d")
                if em is None or not np.isfinite(em) or em <= 0:
                    continue          # keep the comparison on identical rows
                em = float(em)
                # em_1d is already variance-matched to realised moves, so the
                # resampled residuals must have unit RMS. Residuals standardised
                # by a lagging EWMA sigma run at RMS ~1.15, which would make
                # anchored paths ~16% wider than em_1d (measured 0.0243 vs
                # 0.021). Shape (fat tails) is kept; only the scale is fixed.
                z = np.asarray(frv.innovations, dtype=float)
                z = z[np.isfinite(z)]
                rms = float(np.sqrt(np.mean(z * z))) if z.size else 0.0
                if rms <= 0:
                    continue
                # NOTE for --model garch11_t: only the START is anchored; the
                # recursion reverts to GARCH's own long-run level within the
                # hold (0.021 -> 0.0135 by day 17 in a test). Compare on ewma.
                frv = dataclasses.replace(
                    frv, var_path=np.full(horizon, em * em), sigma_next=em,
                    rv_annual_pct=sc.annualise_vol(em), innovations=z / rms)
            mc = sc.fhs_paths(frv, horizon, n_paths=args.paths,
                              drift=0.0, seed=args.seed)
        except Exception:
            continue
        ms = sc.empirical_move_stats(mc)
        em_ref = float(spot) * ms["mean_abs"]
        sim = sc.simulate_structure(st, mc, float(spot),
                                    stop_k_em=args.stop_k, em_ref=em_ref)
        if sim.cvar05_per_margin < -args.max_cvar:
            passed = False
            why = why + [f"cvar/margin {sim.cvar05_per_margin:.2f} worse than "
                         f"{-args.max_cvar}"]

        h = hist.get(sym, {})
        gs_series = h.get("gs", np.array([]))
        cur_gs = (float(np.log(r["hv20"] / iv))
                  if pd.notna(r.get("hv20")) and r["hv20"] > 0 else np.nan)
        z = (sc.robust_z(cur_gs, gs_series[:-1][np.isfinite(gs_series[:-1])])
             if gs_series.size > 12 else np.nan)
        iv_hist = h.get("iv", np.array([]))[-IVRANK_LOOKBACK:]
        ivr = (float((iv_hist < iv).mean() * 100)
               if iv_hist.size > 30 else np.nan)

        # ts_slope, with an explicit roll guard (no expiry_rolled column here)
        ts = np.nan
        if (pd.notna(r.get("atm_iv_next")) and r["atm_iv_next"] > 0
                and pd.notna(r.get("dte_next"))
                and 15 <= (r["dte_next"] - r["dte"]) <= 45):
            ts = float(np.log(iv / r["atm_iv_next"]))

        def _f(col, default=np.nan):
            v = r.get(col)
            return float(v) if pd.notna(v) else default

        em_norm = float(r["expected_move"]) if pd.notna(r.get("expected_move")) else np.nan
        shelf_w = _f("gamma_shelf_width")
        feats.append(sc.Features(
            symbol=sym,
            ev_per_margin=sim.ev_per_margin,
            vrp_z=z, ts_slope=ts,
            gamma_score=sc.gamma_regime_score(r.get("gamma_position")),
            trend_eff=sc.path_efficiency(rr[-20:]),
            liquidity=liq,
            compression_days=_f("compression_days", 0.0),
            days_in_regime=_f("days_in_regime"),
            net_gex_norm=_f("net_gex_norm"),
            dealer_divergence=_f("dealer_divergence"),
            basis_annualized=_f("basis_annualized"),
            shelf_width_norm=(shelf_w / em_norm
                              if np.isfinite(shelf_w) and np.isfinite(em_norm)
                              and em_norm > 0 else np.nan),
            cvar05_per_margin=sim.cvar05_per_margin, tfr=sim.tfr,
            cushion_net=sc.net_credit(st) / (float(spot) * ms["median_abs"]),
            pop=sim.pop, iv_pct=float(iv),
            rv_fcst_pct=frv.rv_annual_pct, iv_rank=ivr,
            iv_cm30=_f("iv_cm30"), oi_turnover_ratio=_f("oi_turnover_ratio"),
            spread_frac=sfrac, spread_to_credit=st.spread_to_credit,
            vrp_ratio=float(iv) / frv.rv_annual_pct if frv.rv_annual_pct > 0 else np.nan))

        m = {"expiry": expiry, "entry_date": entry, "dte": int(r["dte"]),
             "expiry_rank_at_entry": int(r["expiry_rank"]) if pd.notna(r.get("expiry_rank")) else None,
             "horizon_td": horizon, "model": args.model,
             "gate_pass": passed, "gate_reasons": "|".join(why),
             "strike": float(r["atm_strike"]), "spot_entry": float(spot),
             "gross_credit": st.gross_credit, "net_credit": sc.net_credit(st),
             "margin": sc.combo_margin(st), "lot": lot,
             "hv20": r.get("hv20"), "bf25": r.get("bf25"), "rr25": r.get("rr25"),
             "sim_ev": sim.ev, "sim_cvar05": sim.cvar05,
             "sim_stop_rate": sim.stop_rate, "em_ref": em_ref,
             "event_known": events.get(sym), "traded_frac": tf_disp,
             "total_vol_in_range": tvol, "spread_frac": sfrac,
             "spread_cost": st.spread_cost,
             "regime_compression": bool(r.get("regime_compression"))
                                   if pd.notna(r.get("regime_compression")) else None}
        m.update(realised_pnl(st, path, args.stop_k, em_ref))
        meta.append(m)

    if not feats:
        return pd.DataFrame()

    dm = sc.demean_universe([f.vrp_z for f in feats])
    for f, v in zip(feats, dm):
        f.vrp_z = float(v)

    scored = sc.cross_sectional_score(feats)
    out = pd.concat([pd.DataFrame(scored).drop(columns=["rank_parts"]),
                     pd.DataFrame(meta)], axis=1)
    # universe-level timing signal: the median own-history VRP z BEFORE
    # demeaning is the aggregate premium for this date
    out["universe_vrp"] = float(np.nanmedian(
        [f.vrp_z for f in feats])) if feats else np.nan
    out["universe_iv_rank"] = float(np.nanmean([f.iv_rank for f in feats]))
    # Month-level premium gate, computed exactly as build_vol_predictions does
    # live (straddle_core.universe_premium_gate): median iv/rv_forecast over ALL
    # candidates of this expiry, fixed threshold, no look-ahead.
    u_med, u_ok = sc.universe_premium_gate([f.iv_pct for f in feats],
                                           [f.rv_fcst_pct for f in feats],
                                           args.min_universe_vrp)
    out["universe_vrp_median"] = u_med
    out["universe_gate"] = u_ok
    return out


# --------------------------------------------------------------------------
# reporting
# --------------------------------------------------------------------------

def spearman(a, b) -> float:
    """Rank correlation, NaN when undefined.

    Guards the constant-input case explicitly. scipy warns and returns NaN when
    either array has no variation, which happens routinely here:
    compression_days is 0 for every symbol on roughly 99% of dates (only ~1% of
    rows are ever compressed). gamma_score was a four-value bucket that could
    collapse to one value on a quiet date; it is now continuous (gamma_position). Returning NaN silently is correct --
    a constant feature genuinely cannot rank anything -- but the warning spam
    hides real problems, and `constant_report` below turns the same fact into a
    diagnostic instead.
    """
    a, b = np.asarray(a, float), np.asarray(b, float)
    m = np.isfinite(a) & np.isfinite(b)
    if m.sum() < 8:
        return np.nan
    av, bv = a[m], b[m]
    if av.min() == av.max() or bv.min() == bv.max():
        return np.nan
    from scipy.stats import spearmanr
    return float(spearmanr(av, bv).statistic)


def constant_report(d: pd.DataFrame, cols: list) -> None:
    """How often each feature has no cross-sectional variation.

    A feature that is constant on most dates cannot contribute to a ranking on
    those dates, however good it looks pooled. Worth seeing before reading any
    IC: a term that is live on 5% of expiries is a GATE, not a ranking signal,
    and equal-weighting it into the composite just dilutes the others.
    """
    print("\n--- cross-sectional variation by feature ---")
    rows = []
    for c in cols:
        if c not in d.columns:
            continue
        live = nonnull = 0
        for _, g in d.groupby("expiry"):
            v = pd.to_numeric(g[c], errors="coerce").to_numpy(float)
            v = v[np.isfinite(v)]
            if v.size >= 8:
                nonnull += 1
                if v.min() < v.max():
                    live += 1
        if nonnull:
            rows.append({"feature": c, "expiries_with_data": nonnull,
                         "expiries_with_variation": live,
                         "pct_live": round(100.0 * live / nonnull, 1)})
    if rows:
        t = pd.DataFrame(rows).sort_values("pct_live")
        print(t.to_string(index=False))
        dead = t[t.pct_live < 50]
        if not dead.empty:
            print("  ^ below 50% is a gate, not a ranking term -- move it out of")
            print("    RANK_DIRECTION or bucket it so it actually varies.")


def premium_gate_report(df: pd.DataFrame, args) -> None:
    """Month-level premium gate: what trading only passing expiries would have
    done, as a monthly P&L record (mean realised P&L per margin of the stock-
    gated trades each expiry), before and after Nov 2024."""
    y = "realised_pnl_per_margin"
    g = df[df.gate_pass]
    m = g.groupby("expiry").agg(pnl=(y, "mean"), n=(y, "size"),
                                med=("universe_vrp_median", "first"),
                                ok=("universe_gate", "first")).reset_index()
    m["post"] = pd.to_datetime(m["expiry"]) >= pd.Timestamp("2024-11-01")
    print("\n" + "#" * 80)
    print(f"#  PREMIUM GATE: universe median iv/rv_forecast >= {args.min_universe_vrp}")
    print("#" * 80)

    def _rec(x: pd.DataFrame) -> str:
        if x.empty:
            return "n=0"
        c = x["pnl"].cumsum()
        dd = float((c - c.cummax()).min())
        return (f"expiries={len(x):3d}  mean/expiry {100*x.pnl.mean():+6.2f}%  "
                f"sum {100*x.pnl.sum():+7.1f}%  worst {100*x.pnl.min():+6.1f}%  "
                f"max drawdown {100*dd:+6.1f}%  months>0 {100*(x.pnl > 0).mean():4.0f}%")

    for lab, sub in (("2021 - Oct 2024", m[~m.post]), ("Nov 2024 -     ", m[m.post]),
                     ("ALL            ", m)):
        print(f"  {lab} trade every month : {_rec(sub)}")
        print(f"  {lab} passing months only: {_rec(sub[sub.ok == True])}")  # noqa: E712
        print(f"  {lab} failing months     : {_rec(sub[sub.ok == False])}")  # noqa: E712
    print("  per-expiry: " + "  ".join(
        f"{str(r.expiry)[:7]} {r.med:.2f}{'+' if r.ok else '-'}{100*r.pnl:+.1f}%"
        for r in m.itertuples() if pd.notna(r.med)))


def report(df: pd.DataFrame, gated_only: bool, args) -> None:
    d = df[df.gate_pass] if gated_only else df
    d = d[np.isfinite(d.realised_pnl_per_margin)]
    if d.empty:
        print("no rows")
        return
    tgt = "realised_pnl_per_margin"
    print("\n" + "=" * 80)
    print(f"  {'GATED' if gated_only else 'ALL'}  n={len(d):,}  "
          f"expiries={d.expiry.nunique()}  symbols={d.symbol.nunique()}")
    print("=" * 80)

    ics = []
    print("\n--- per-expiry rank IC ---")
    for e, g in d.groupby("expiry"):
        ic = spearman(g["score"], g[tgt])
        ics.append(ic)
        print(f"  {e}  n={len(g):3d}  IC={ic:+.3f}  "
              f"win%={100*(g.realised_pnl>0).mean():5.1f}  "
              f"meanRoM={100*g[tgt].mean():+6.2f}%")
    ics = np.array([i for i in ics if np.isfinite(i)])
    if ics.size > 2:
        t = ics.mean() / (ics.std(ddof=1) / np.sqrt(ics.size))
        print(f"\n  mean IC {ics.mean():+.4f}  sd {ics.std(ddof=1):.3f}  "
              f"t {t:+.2f}  periods {ics.size}")
        print(f"  |t|>2 needed to distinguish from zero; with {ics.size} periods")
        print(f"  the detectable IC at t=2 is about "
              f"{2*ics.std(ddof=1)/np.sqrt(ics.size):+.3f}")

    # Report the CUT terms alongside the ranked ones. They are no longer
    # weighted, but they are still computed and written, and a term that was
    # dead over 71 expiries could look different at another DTE or after a
    # definition change. Dropping them from the report would mean never
    # finding out -- and would also hide a regression if one silently came back.
    CUT = ["ts_slope", "vrp_z", "gamma_score", "liquidity", "compression_days",
           "days_in_regime", "net_gex_norm", "dealer_divergence",
           "basis_annualized", "shelf_width_norm", "oi_turnover_ratio"]
    EXTRA = ["score", "pop", "iv_rank", "iv_cm30", "tfr", "cushion_net",
             "cvar05_per_margin"]
    print("\n--- component standalone IC ---")
    print("  [R] = ranked, [c] = cut on prior evidence, [d] = diagnostic only")
    for c in list(sc.RANK_DIRECTION) + CUT + EXTRA:
        if c not in d.columns:
            continue
        per = np.array([x for x in (spearman(g[c], g[tgt])
                                    for _, g in d.groupby("expiry"))
                        if np.isfinite(x)])
        if per.size < 3:
            continue
        tt = per.mean() / (per.std(ddof=1) / np.sqrt(per.size))
        mark = ("[R]" if c in sc.RANK_DIRECTION else
                "[c]" if c in CUT else "[d]")
        tag = "  <-- composite" if c == "score" else ""
        print(f"  {mark} {c:22s} IC={per.mean():+.4f}  t={tt:+5.2f}  "
              f"live={per.size:3d}  pos={int((per>0).sum())}{tag}")
    print("\n  The composite must beat its best RANKED component. Every version")
    print("  so far has not: 6 terms scored 0.079 vs ev_per_margin 0.149;")
    print("  12 terms scored 0.032 vs 0.125. For roughly uncorrelated signals")
    print("  the composite lands near sum(IC)/sqrt(k), so weak terms dilute.")
    exp_ic = sum(
        np.nanmean([spearman(g[c], g[tgt]) for _, g in d.groupby("expiry")])
        for c in sc.RANK_DIRECTION if c in d.columns)
    k = sum(1 for c in sc.RANK_DIRECTION if c in d.columns)
    if k:
        print(f"  predicted for k={k} if uncorrelated: {exp_ic/np.sqrt(k):+.4f}")
        print("  -- if the observed composite falls well short of this, the")
        print("     ranked terms overlap and one of them should go.")

    constant_report(d, list(sc.RANK_DIRECTION) + CUT)

    print("\n--- pairwise rank correlation among RANKED terms ---")
    rk = [c for c in sc.RANK_DIRECTION if c in d.columns]
    if len(rk) > 1:
        import itertools
        for a, b in itertools.combinations(rk, 2):
            per = np.array([x for x in (spearman(g[a], g[b])
                                        for _, g in d.groupby("expiry"))
                            if np.isfinite(x)])
            if per.size:
                print(f"  {a:18s} x {b:18s} {per.mean():+.3f}")
        print("  Near zero means the terms carry independent information and")
        print("  the composite should beat each of them. Above ~0.5 means one")
        print("  is largely a restatement of another -- cut it.")

    print("\n--- quintiles (look for MONOTONICITY) ---")
    q = d.copy()
    try:
        q["qt"] = q.groupby("expiry")["score"].transform(
            lambda s: pd.qcut(s, 5, labels=[1, 2, 3, 4, 5], duplicates="drop"))
        print(q.groupby("qt", observed=True).apply(lambda g: pd.Series({
            "n": len(g), "meanRoM%": 100 * g[tgt].mean(),
            "medRoM%": 100 * g[tgt].median(),
            "win%": 100 * (g.realised_pnl > 0).mean(),
            "worst%": 100 * g[tgt].min()}), include_groups=False)
            .round(2).to_string())
    except Exception as exc:  # noqa: BLE001
        print(f"  unavailable: {exc}")

    print("\n--- TIMING vs SELECTION ---")
    per_exp = d.groupby("expiry")[tgt].mean()
    within = d.groupby("expiry")[tgt].std().mean()
    print(f"  between-expiry sd of mean RoM : {100*per_exp.std():6.2f}%")
    print(f"  average within-expiry sd      : {100*within:6.2f}%")
    print(f"  best expiry {100*per_exp.max():+.1f}%   "
          f"worst {100*per_exp.min():+.1f}%   spread {100*(per_exp.max()-per_exp.min()):.1f}%")
    if "universe_vrp" in d.columns:
        uv = d.groupby("expiry").agg(u=("universe_vrp", "first"),
                                     r=(tgt, "mean"))
        print(f"  corr(universe_vrp, expiry mean RoM) = "
              f"{spearman(uv['u'], uv['r']):+.3f}   n={len(uv)}")
        print("  A strong positive here means WHEN to be short vol matters more")
        print("  than WHICH name -- and the timing gate is worth more than any")
        print("  amount of cross-sectional refinement.")

    print("\n--- realised P&L shape ---")
    r = d["realised_ret"]
    print(f"  mean {100*r.mean():+6.1f}%  median {100*r.median():+6.1f}%  "
          f"p05 {100*r.quantile(.05):+7.1f}%  worst {100*r.min():+8.1f}%  "
          f"skew {r.skew():+.2f}")
    print(f"  losing > 100% of credit: {(r < -1).sum()} of {len(r)}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    ap.add_argument("--bhav", default=r"C:\Users\aniru\od\bhavcopy.duckdb")
    ap.add_argument("--dte", type=int, default=25)
    ap.add_argument("--stop-k", type=float, default=None, dest="stop_k")
    ap.add_argument("--model", default="ewma", choices=["ewma", "garch11_t"],
                    help="path generator for the FHS simulator. ewma by "
                         "default: GARCH forecasts vary 3.7x less than the "
                         "target within symbol and add nothing as a point "
                         "forecast, though either is fine for generating paths")
    ap.add_argument("--paths", type=int, default=8000)
    ap.add_argument("--vol-source", default="model", choices=["model", "em1d"],
                    dest="vol_source",
                    help="path volatility level: the --model fit on returns, or the "
                         "stored exposure_hist em_1d at entry")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--min-obs", type=int, default=120, dest="min_obs")
    ap.add_argument("--min-dte", type=int, default=10, dest="min_dte")
    ap.add_argument("--max-dte", type=int, default=45, dest="max_dte")
    ap.add_argument("--min-symbols", type=int, default=40, dest="min_symbols")
    ap.add_argument("--min-volume", type=float, default=1000.0, dest="min_volume",
                    help="reject chains below this many in-range contracts "
                         "traded. Absolute volume, not traded_frac -- the "
                         "ratio separated liquidity regimes 1.2:1, volume 11:1")
    ap.add_argument("--max-cvar", type=float, default=5.0, dest="max_cvar",
                    help="reject when simulated CVaR5 per margin is worse than -X")
    ap.add_argument("--require-events", action="store_true", dest="require_events")
    ap.add_argument("--timing-gate", action="store_true", dest="timing_gate",
                    help="also report results restricted to expiries whose "
                         "universe_vrp is above its own median")
    ap.add_argument("--min-universe-vrp", type=float, default=sc.UNIVERSE_VRP_MIN,
                    dest="min_universe_vrp",
                    help="month-level premium gate: trade an expiry only when the "
                         "universe median iv/rv_forecast is at or above this")
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    con = connect(args.db, args.bhav)
    exps = completed_expiries(con, args.min_symbols)
    print(f"{len(exps)} completed expiries: {exps[0]} .. {exps[-1]}"
          if exps else "no expiries")
    print(f"stop: {'hold to expiry' if args.stop_k is None else f'{args.stop_k} x EM'}"
          f"   model: {args.model}")

    frames = []
    for e in exps:
        try:
            f = run_expiry(con, e, args)
        except Exception as exc:  # noqa: BLE001
            print(f"  {e}: ERROR {type(exc).__name__}: {exc}")
            continue
        if not f.empty:
            frames.append(f)
    con.close()
    if not frames:
        print("no results")
        return 1
    df = pd.concat(frames, ignore_index=True)

    print(f"\ngate pass: {int(df.gate_pass.sum()):,} / {len(df):,}")
    rej = df.loc[~df.gate_pass, "gate_reasons"].str.split("|").explode()
    rej = rej[rej != ""]
    if not rej.empty:
        for k, v in rej.value_counts().head(8).items():
            print(f"    {v:5d}  {k}")

    report(df, False, args)
    report(df, True, args)
    if args.timing_gate and "universe_vrp" in df.columns:
        uv = df.groupby("expiry")["universe_vrp"].first()
        rich = set(uv[uv > uv.median()].index)
        print("\n" + "#" * 80)
        print(f"#  TIMING GATE: {len(rich)} of {len(uv)} expiries with "
              f"universe_vrp above median")
        print("#" * 80)
        report(df[df.expiry.isin(rich) & df.gate_pass], False, args)

    if "universe_gate" in df.columns:
        premium_gate_report(df, args)

    if args.out:
        df.drop(columns=["rank_parts"], errors="ignore").to_csv(args.out, index=False)
        print(f"\nwrote {args.out} ({len(df):,} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
