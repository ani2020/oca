"""
enrich_exposure_hist.py
=======================
Add the columns exposure_eod has and exposure_hist lacks, so the history is
backtestable on the same features the live dashboard already uses.

    python enrich_exposure_hist.py --stage 1 --from 2020-06-15 --to 2026-09-15
    python enrich_exposure_hist.py --stage 2            # fast, no chain pass
    python enrich_exposure_hist.py --stage both --from 2020-06-15 --to 2026-09-15

Why this exists
---------------
exposure_hist is a per-day SNAPSHOT with no memory. exposure_eod carries 43
columns it does not, and the valuable ones are almost all TEMPORAL -- how long
something has been true, not what is true today:

    compression_days / compression_release / days_since_release
    days_in_regime / days_since_flip / flip_velocity
    gamma_com_migration / migration_effectiveness / concentration_stability

For a short straddle, duration-in-state is plausibly worth more than state. A
symbol compressed for 20 sessions is a different proposition from one compressed
for 2, and a single-day snapshot cannot say which it is.

It also repairs three things the backtest measured as broken:
    oi_turnover_ratio   real churn, replacing traded_frac (which separated
                        liquidity regimes 1.2:1 while volume separated them 11:1)
    iv_cm30             constant-maturity IV. vrp_z and iv_rank were built on
                        raw atm_iv, so a 15-DTE IV sat next to a 35-DTE IV in
                        the same own-history z -- the series mixed tenors and
                        the z was measuring term structure as much as richness
    net_gex_norm        lopsidedness in [-1,+1]. Raw net_gex was ~6.7e6x the
                        live value and not comparable across symbols at all

NOTHING IS REIMPLEMENTED. Every metric calls the same exposure_core function the
live builder calls, and stage 2 is the live run_stage2 SQL with the table name
parameterised. If a definition changes in exposure_core, both paths change
together -- which is the whole point of not writing a second copy.

Two stages
----------
STAGE 1  re-walks the option chains and fills the per-day metrics. Slow (a full
         pass over options_eod), but it does not touch what build_exposure_history
         already wrote -- only adds columns.
STAGE 2  pure SQL window functions over exposure_hist. Minutes. Needs stage 1
         first, because the compression test reads transition_width_norm.
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import exposure_core as core   # noqa: E402  same definitions as the live builder
import vol_math as vm          # noqa: E402

TRADING_DAYS = 243.0
SMILE_RANGE_PCT = 0.25
EXPOSURE_EM_MULT = 1.124
EXPOSURE_FALLBACK_PCT = 0.10
MIN_STRIKES = 5
COMPRESS_LOOKBACK = 2          # matches oc_exposure_eod.COMPRESS_LOOKBACK

#: added by stage 1
STAGE1_COLS = [
    ("transition_width", "DOUBLE"), ("transition_width_norm", "DOUBLE"),
    ("net_gex_norm", "DOUBLE"), ("net_gex_sign", "INTEGER"),
    ("gamma_shelf_center", "DOUBLE"), ("gamma_shelf_width", "DOUBLE"),
    ("gamma_shelf_peak_strike", "DOUBLE"), ("gamma_shelf_peak_value", "DOUBLE"),
    ("gamma_shelf_single_strike", "BOOLEAN"), ("concentration", "DOUBLE"),
    ("gamma_com_migration", "DOUBLE"),
    ("oi_com_inrange", "DOUBLE"),
    ("dealer_divergence", "DOUBLE"), ("dealer_divergence_label", "VARCHAR"),
    ("oi_turnover_ratio", "DOUBLE"),
    ("basis_annualized", "DOUBLE"),
]
#: added by stage 2
STAGE2_COLS = [
    ("atm_iv_smoothed", "DOUBLE"), ("iv_change", "DOUBLE"),
    ("basis_chg", "DOUBLE"), ("flip_velocity", "DOUBLE"),
    ("regime_compression", "BOOLEAN"), ("compression_release", "BOOLEAN"),
    ("compression_days", "INTEGER"), ("days_in_regime", "INTEGER"),
    ("days_since_flip", "INTEGER"), ("days_since_release", "INTEGER"),
    ("iv_cm30", "DOUBLE"), ("iv_rank", "DOUBLE"), ("iv_pctile", "DOUBLE"),
    ("iv_rank_method", "VARCHAR"), ("iv_rank_n", "INTEGER"),
]


def add_columns(con, cols):
    for c, t in cols:
        con.execute(f"ALTER TABLE exposure_hist ADD COLUMN IF NOT EXISTS {c} {t}")


# --------------------------------------------------------------------------
# stage 1 -- per-day metrics from the chain
# --------------------------------------------------------------------------

def stage1_day(con, d: date, prev_com: dict) -> int:
    """Recompute the missing per-day metrics for one date.

    prev_com carries the previous session's gamma centre of mass per
    (symbol, expiry) so gamma_shelf() can report migration -- the one stage-1
    metric that is not purely same-day.
    """
    opt = con.execute("""
        SELECT o.symbol, o.expiry, o.strike, o.opt_type, o.close, o.settle,
               o.volume, o.oi, o.lot
        FROM bhav.options_eod o
        WHERE o.trade_date = ? AND o.strike > 0 AND o.expiry >= ?
    """, [d, d]).df()
    if opt.empty:
        return 0
    base = con.execute("""
        SELECT symbol, expiry, expiry_rank, dte, dte_trading, spot, fut_price,
               atm_iv, expected_move, atm_strike
        FROM exposure_hist WHERE date = ?
    """, [d]).df()
    if base.empty:
        return 0

    rows = []
    idx = {(r.symbol, r.expiry): r for r in base.itertuples(index=False)}
    for (sym, exp), g in opt.groupby(["symbol", "expiry"], sort=False):
        b = idx.get((sym, exp))
        if b is None or not np.isfinite(b.fut_price) or b.fut_price <= 0:
            continue
        fwd, spot = float(b.fut_price), float(b.spot)
        em = float(b.expected_move) if pd.notna(b.expected_move) else np.nan

        g = g[(g["strike"] > fwd * (1 - SMILE_RANGE_PCT))
              & (g["strike"] < fwd * (1 + SMILE_RANGE_PCT))]
        if len(g) < MIN_STRIKES:
            continue

        # rebuild net gamma per strike exactly as build_exposure_history does,
        # then restrict to the SAME narrow exposure window -- these metrics are
        # sums over a strike set, so the window is part of the definition
        price = np.where(g["close"].to_numpy(float) > 0,
                         g["close"].to_numpy(float), g["settle"].to_numpy(float))
        K = g["strike"].to_numpy(float)
        is_call = (g["opt_type"].astype(str).str.upper() == "CE").to_numpy()
        T = np.full(K.shape, max(float(b.dte_trading), 1.0) / TRADING_DAYS)
        F = np.full(K.shape, fwd)
        iv = vm.black76_iv(price, F, K, T, is_call)
        gk = vm.black76_greeks(F, K, T, iv, is_call)
        ok = np.isfinite(iv)
        if ok.sum() < MIN_STRIKES:
            continue

        lot = float(g["lot"].dropna().iloc[0]) if g["lot"].notna().any() else 1.0
        oi = g["oi"].to_numpy(float)
        vol_ = g["volume"].to_numpy(float)
        gsign = np.where(is_call, 1.0, -1.0)
        gex = np.where(ok, gk["gamma"] * oi * lot * fwd * fwd * 0.01 * gsign, 0.0)

        half = (EXPOSURE_EM_MULT * em if np.isfinite(em)
                else EXPOSURE_FALLBACK_PCT * spot)
        win = (K >= spot - half) & (K <= spot + half)
        if win.sum() < MIN_STRIKES:
            win = (K >= spot * (1 - EXPOSURE_FALLBACK_PCT)) & (K <= spot * (1 + EXPOSURE_FALLBACK_PCT))
        if win.sum() < MIN_STRIKES:
            continue

        per = (pd.DataFrame({"k": K[win], "g": gex[win]})
               .groupby("k")["g"].sum().sort_index())
        ks, ng = per.index.to_numpy(float), per.to_numpy(float)

        ce_oi = (pd.DataFrame({"k": K[win & is_call], "o": oi[win & is_call]})
                 .groupby("k")["o"].sum().reindex(ks, fill_value=0.0).to_numpy(float))
        pe_oi = (pd.DataFrame({"k": K[win & ~is_call], "o": oi[win & ~is_call]})
                 .groupby("k")["o"].sum().reindex(ks, fill_value=0.0).to_numpy(float))

        tw = core.transition_width(ks, ng, fwd)
        shelf = core.gamma_shelf(ks, ng, prev_com=prev_com.get((sym, exp))) or {}
        ocom = core.oi_com_inrange(ks, ce_oi, pe_oi)
        dd_v, dd_l = core.dealer_divergence(shelf.get("center"), ocom,
                                            em if np.isfinite(em) else None)
        _, _, bann = core.basis_metrics(fwd, spot, float(b.dte))
        tot_oi = float(np.nansum(oi[win]))
        tot_vol = float(np.nansum(vol_[win]))

        if shelf.get("center") is not None:
            prev_com[(sym, exp)] = shelf["center"]

        rows.append({
            "symbol": sym, "date": d, "expiry": exp,
            "transition_width": tw,
            "transition_width_norm": core.width_norm(tw, em) if np.isfinite(em) else None,
            "net_gex_norm": core.lopsidedness(ng),
            "net_gex_sign": int(np.sign(ng.sum())),
            "gamma_shelf_center": shelf.get("center"),
            "gamma_shelf_width": shelf.get("width"),
            "gamma_shelf_peak_strike": shelf.get("peak_strike"),
            "gamma_shelf_peak_value": shelf.get("peak_value"),
            "gamma_shelf_single_strike": shelf.get("single_strike"),
            "concentration": shelf.get("concentration"),
            "gamma_com_migration": shelf.get("migration"),
            "oi_com_inrange": ocom,
            "dealer_divergence": dd_v, "dealer_divergence_label": dd_l,
            "oi_turnover_ratio": round(tot_vol / tot_oi, 4) if tot_oi > 0 else None,
            "basis_annualized": bann,
        })

    if not rows:
        return 0
    df = pd.DataFrame(rows)
    con.register("_e1", df)
    sets = ", ".join(f"{c} = s.{c}" for c, _ in STAGE1_COLS)
    con.execute(f"""
        UPDATE exposure_hist e SET {sets}
        FROM _e1 s
        WHERE e.symbol = s.symbol AND e.date = s.date AND e.expiry = s.expiry
    """)
    con.unregister("_e1")
    return len(df)


def run_stage1(con, d0: date, d1: date) -> None:
    add_columns(con, STAGE1_COLS)
    days = [r[0] for r in con.execute(
        "SELECT DISTINCT date FROM exposure_hist WHERE date BETWEEN ? AND ? "
        "ORDER BY date", [d0, d1]).fetchall()]
    print(f"stage 1: {len(days)} day(s)")
    prev_com: dict = {}
    total = 0
    t0 = datetime.now()
    for i, d in enumerate(days, 1):
        d = d if isinstance(d, date) else pd.Timestamp(d).date()
        try:
            total += stage1_day(con, d, prev_com)
        except Exception as exc:  # noqa: BLE001
            print(f"  {d}: ERROR {type(exc).__name__}: {exc}")
        if i % 25 == 0 or i == len(days):
            el = (datetime.now() - t0).total_seconds()
            print(f"  [{i}/{len(days)}] {d}  rows={total:,}  "
                  f"{el:.0f}s, ~{el/i*(len(days)-i)/60:.1f}m left")


# --------------------------------------------------------------------------
# stage 2 -- temporal state, ported from oc_exposure_eod.run_stage2
# --------------------------------------------------------------------------

def run_stage2(con) -> None:
    """The live run_stage2 SQL with the table name swapped.

    Kept statement-for-statement rather than rewritten, so the history and the
    live table carry the SAME definitions. Two guards in the original matter
    here and are preserved verbatim:

    EXPIRY ROLL GUARD in the compression test. Both compression conditions are
    ratios with expected_move in the denominator, and EM scales with sqrt(DTE).
    Across a roll the LAG(LOOKBACK) baseline is a different contract at a much
    shorter DTE, so EM triples and both ratios collapse mechanically -- the test
    then fires on term structure, not on tightening. Measured live at 5.28% of
    roll-crossing rows against 0.77% off-roll, every one a false positive.

    NULL vs FALSE. Rows with insufficient history get FALSE, not NULL, so a
    missing history never reads as "not compressed" downstream by accident.
    """
    add_columns(con, STAGE1_COLS + STAGE2_COLS)
    T = "exposure_hist"

    # Stage 2 READS three stage-1 columns -- basis_annualized (2b),
    # transition_width and transition_width_norm (2c, 2d) -- so both column
    # sets must exist before any of this runs. Adding only STAGE2_COLS here
    # made `--stage 2` fail on a missing binding. ADD COLUMN IF NOT EXISTS is
    # idempotent, so adding both is free and makes stage 2 runnable first, on
    # its own, as a cheap check that the ported SQL executes.
    #
    # When stage 1 has not run, those columns are NULL and the compression
    # family correctly yields FALSE throughout. The populated-% report at the
    # end shows which columns are still empty -- that is the signal to run
    # stage 1, not a failure.
    missing_s1 = con.execute(f"""
        SELECT count(*) FILTER (WHERE transition_width_norm IS NOT NULL) FROM {T}
    """).fetchone()[0]
    if not missing_s1:
        print("  [stage 1 has not populated transition_width_norm --")
        print("   compression_* and days_since_release will be empty/FALSE]")

    # atm_iv_smoothed + iv_change (live builder derives these upstream; here
    # they come from the stored series so stage 2 is self-contained)
    print("  2a. atm_iv_smoothed, iv_change")
    con.execute(f"""
        WITH w AS (
            SELECT symbol, date, expiry_rank, expiry, atm_iv,
                   LAG(atm_iv) OVER win AS prev_iv,
                   LAG(expiry)  OVER win AS prev_exp,
                   AVG(atm_iv) OVER (PARTITION BY symbol, expiry_rank ORDER BY date
                                     ROWS BETWEEN 2 PRECEDING AND CURRENT ROW) AS sm
            FROM {T}
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE {T} e
        SET atm_iv_smoothed = w.sm,
            iv_change = CASE WHEN w.prev_iv IS NOT NULL
                              AND w.prev_exp IS NOT DISTINCT FROM w.expiry
                             THEN w.atm_iv - w.prev_iv END
        FROM w WHERE e.symbol=w.symbol AND e.date=w.date AND e.expiry_rank=w.expiry_rank
    """)

    print("  2b. flip_velocity, basis_chg")
    con.execute(f"""
        WITH w AS (
            SELECT symbol, date, expiry_rank, expiry, gamma_flip, basis_annualized,
                   LAG(gamma_flip) OVER win AS prev_flip,
                   LAG(basis_annualized) OVER win AS prev_basis,
                   LAG(date) OVER win AS prev_date,
                   LAG(expiry) OVER win AS prev_exp
            FROM {T}
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE {T} e
        SET flip_velocity = CASE
                WHEN w.prev_flip IS NOT NULL AND w.gamma_flip IS NOT NULL
                     AND w.prev_exp IS NOT DISTINCT FROM w.expiry
                     AND DATE_DIFF('day', w.prev_date, w.date) > 0
                THEN (w.gamma_flip - w.prev_flip)
                     / DATE_DIFF('day', w.prev_date, w.date) END,
            basis_chg = CASE
                WHEN w.prev_basis IS NOT NULL AND w.basis_annualized IS NOT NULL
                     AND w.prev_exp IS NOT DISTINCT FROM w.expiry
                THEN ROUND(w.basis_annualized - w.prev_basis, 3) END
        FROM w WHERE e.symbol=w.symbol AND e.date=w.date AND e.expiry_rank=w.expiry_rank
    """)

    print("  2c. regime_compression (with expiry-roll guard)")
    con.execute(f"""
        WITH w AS (
            SELECT symbol, date, expiry_rank, expiry,
                   LAG(expiry, {COMPRESS_LOOKBACK}) OVER win AS exp_lb,
                   transition_width_norm AS twn,
                   LAG(transition_width_norm, {COMPRESS_LOOKBACK}) OVER win AS twn_lb,
                   ABS(flip_norm_distance) AS afnd,
                   LAG(ABS(flip_norm_distance), {COMPRESS_LOOKBACK}) OVER win AS afnd_lb,
                   gex_regime, atm_iv_smoothed AS ivs,
                   LAG(atm_iv_smoothed, 1) OVER win AS ivs_prev
            FROM {T}
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE {T} e
        SET regime_compression = (
            w.exp_lb IS NOT NULL AND w.exp_lb IS NOT DISTINCT FROM w.expiry
            AND w.gex_regime IN ('positive','all_positive')
            AND w.twn  IS NOT NULL AND w.twn_lb  IS NOT NULL AND w.twn  < w.twn_lb
            AND w.afnd IS NOT NULL AND w.afnd_lb IS NOT NULL AND w.afnd < w.afnd_lb
            AND (w.ivs_prev IS NULL OR w.ivs <= w.ivs_prev))
        FROM w WHERE e.symbol=w.symbol AND e.date=w.date AND e.expiry_rank=w.expiry_rank
    """)
    con.execute(f"UPDATE {T} SET regime_compression = FALSE "
                f"WHERE regime_compression IS NULL")

    print("  2d. compression_release")
    con.execute(f"""
        WITH w AS (
            SELECT symbol, date, expiry_rank, regime_compression,
                   LAG(regime_compression,1) OVER win AS prev_comp,
                   iv_change, gex_regime, transition_width,
                   LAG(transition_width,1) OVER win AS prev_tw
            FROM {T}
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE {T} e
        SET compression_release = (
            w.prev_comp = TRUE AND w.regime_compression = FALSE
            AND COALESCE(w.iv_change,0) > 0
            AND w.gex_regime IN ('negative','all_negative')
            AND w.prev_tw IS NOT NULL AND w.transition_width > w.prev_tw)
        FROM w WHERE e.symbol=w.symbol AND e.date=w.date AND e.expiry_rank=w.expiry_rank
    """)
    con.execute(f"UPDATE {T} SET compression_release = FALSE "
                f"WHERE compression_release IS NULL")

    print("  2e. day counters")
    con.execute(f"""
        WITH marked AS (
            SELECT symbol, date, expiry_rank, gex_regime,
                   CASE WHEN gex_regime IS DISTINCT FROM LAG(gex_regime) OVER win
                        THEN 1 ELSE 0 END AS chg
            FROM {T}
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        ),
        grp AS (SELECT symbol, date, expiry_rank,
                   SUM(chg) OVER (PARTITION BY symbol, expiry_rank ORDER BY date) AS gid
                FROM marked),
        cnt AS (SELECT symbol, date, expiry_rank,
                   ROW_NUMBER() OVER (PARTITION BY symbol, expiry_rank, gid ORDER BY date) AS dir
                FROM grp)
        UPDATE {T} e SET days_in_regime = c.dir, days_since_flip = c.dir - 1
        FROM cnt c WHERE e.symbol=c.symbol AND e.date=c.date AND e.expiry_rank=c.expiry_rank
    """)
    con.execute(f"""
        WITH marked AS (
            SELECT symbol, date, expiry_rank, regime_compression,
                   CASE WHEN regime_compression IS DISTINCT FROM
                        LAG(regime_compression) OVER win THEN 1 ELSE 0 END AS chg
            FROM {T}
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        ),
        grp AS (SELECT symbol, date, expiry_rank, regime_compression,
                   SUM(chg) OVER (PARTITION BY symbol, expiry_rank ORDER BY date) AS gid
                FROM marked),
        cnt AS (SELECT symbol, date, expiry_rank, regime_compression,
                   ROW_NUMBER() OVER (PARTITION BY symbol, expiry_rank, gid ORDER BY date) AS run
                FROM grp)
        UPDATE {T} e
        SET compression_days = CASE WHEN c.regime_compression THEN c.run ELSE 0 END
        FROM cnt c WHERE e.symbol=c.symbol AND e.date=c.date AND e.expiry_rank=c.expiry_rank
    """)
    con.execute(f"""
        WITH rel AS (
            SELECT symbol, date, expiry_rank,
                   MAX(CASE WHEN compression_release THEN date END)
                     OVER (PARTITION BY symbol, expiry_rank ORDER BY date
                           ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_rel
            FROM {T})
        UPDATE {T} e
        SET days_since_release = CASE WHEN r.last_rel IS NULL THEN NULL
                                      ELSE DATE_DIFF('day', r.last_rel, e.date) END
        FROM rel r WHERE e.symbol=r.symbol AND e.date=r.date AND e.expiry_rank=r.expiry_rank
    """)

    print("  2f. iv_cm30 / iv_rank / iv_pctile (constant maturity)")
    Tg = core.IVRANK_CM_TARGET_DTE
    lo_b, hi_b = core.IVRANK_DTE_BAND
    lb = core.IVRANK_LOOKBACK_DAYS
    con.execute(f"""
        WITH pair AS (
            SELECT a.symbol, a.date, a.dte AS d0, a.atm_iv AS iv0,
                   b.dte AS d1, b.atm_iv AS iv1
            FROM {T} a
            LEFT JOIN {T} b ON b.symbol=a.symbol AND b.date=a.date
                 AND b.expiry_rank=1 AND b.atm_iv>0 AND b.dte>a.dte
            WHERE a.expiry_rank=0 AND a.atm_iv>0
        ),
        s AS (
            SELECT symbol, date,
              CASE WHEN iv1 IS NOT NULL AND d0<={Tg} AND d1>={Tg}
                   THEN SQRT(GREATEST((iv0*iv0*d0 + (iv1*iv1*d1 - iv0*iv0*d0)
                        *({Tg}.0-d0)/(d1-d0))/{Tg}.0, 0.0))
                   WHEN d0 BETWEEN {lo_b} AND {hi_b} THEN iv0 END AS iv_cm,
              CASE WHEN iv1 IS NOT NULL AND d0<={Tg} AND d1>={Tg} THEN 'cm30'
                   WHEN d0 BETWEEN {lo_b} AND {hi_b} THEN 'band' END AS meth
            FROM pair
        ),
        w AS (
            SELECT symbol, date, iv_cm, meth,
                   MIN(iv_cm) OVER q AS mn, MAX(iv_cm) OVER q AS mx,
                   COUNT(*) OVER q AS nobs,
                   PERCENT_RANK() OVER (PARTITION BY symbol, meth ORDER BY iv_cm) AS pr
            FROM s WHERE iv_cm IS NOT NULL
            WINDOW q AS (PARTITION BY symbol, meth ORDER BY date
                         RANGE BETWEEN INTERVAL {lb} DAY PRECEDING AND CURRENT ROW)
        )
        UPDATE {T} e
        SET iv_cm30 = w.iv_cm, iv_rank_method = w.meth, iv_rank_n = w.nobs,
            iv_rank = CASE WHEN w.nobs >= {core.IVRANK_MIN_SAMPLES} AND w.mx > w.mn
                           THEN 100.0*(w.iv_cm-w.mn)/(w.mx-w.mn) END,
            iv_pctile = CASE WHEN w.nobs >= {core.IVRANK_MIN_SAMPLES}
                             THEN 100.0*w.pr END
        FROM w WHERE w.symbol=e.symbol AND w.date=e.date AND e.expiry_rank=0
    """)


def report(con) -> None:
    cols = [c for c, _ in STAGE1_COLS + STAGE2_COLS]
    have = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name='exposure_hist'").fetchall()}
    sel = ", ".join(f"round(100.0*count({c})/count(*),1) AS {c}"
                    for c in cols if c in have)
    df = con.execute(f"SELECT count(*) AS rows, {sel} FROM exposure_hist").df()
    print("\npopulated % by column:")
    print(df.T.to_string(header=False))


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    ap.add_argument("--bhav", default=r"C:\Users\aniru\od\bhavcopy.duckdb")
    ap.add_argument("--stage", choices=["1", "2", "both"], default="both")
    ap.add_argument("--from", dest="from_date", default="2020-06-15",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="to_date", default="2026-12-31",
                    type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    con.execute(f"ATTACH IF NOT EXISTS '{args.bhav}' AS bhav (READ_ONLY)")
    try:
        if args.stage in ("1", "both"):
            run_stage1(con, args.from_date, args.to_date)
        if args.stage in ("2", "both"):
            print("stage 2: temporal state")
            run_stage2(con)
        report(con)
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
