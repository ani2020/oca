"""
build_vol_predictions.py
========================
Nightly prediction log for the straddle/strangle screener, plus the resolver.

    python build_vol_predictions.py predict                  # nightly, after EOD
    python build_vol_predictions.py resolve                  # after each expiry
    python build_vol_predictions.py status
    python build_vol_predictions.py compare                  # model vs model

WHY THIS EXISTS, AND WHY IT COMES BEFORE THE PAGES
---------------------------------------------------
The two-term model (ev_per_margin + trend_eff) has 71 expiries of BACKTEST and
zero days of forward evidence. Every number behind it -- IC 0.1307, the
trend_eff sign, the eleven cut features -- came from data the model was
selected on. Some of it will not survive live markets.

So this logs BOTH models on the SAME candidates every night:

    new     score      the 2-term composite from straddle_core
    legacy  return_on_margin, be_vs_hv, vrp, iv_pctile
            -- the existing tab has no single score, it returns metrics and the
               UI sorts, so each sortable column is logged as its own candidate
               ranking. That answers "does the new model beat what you would
               actually have picked", not "does it beat a model I invented for
               it to beat".

Paired on identical trades, so a bad month is attributable to the model rather
than to the month. Without the control there is no way to tell the two apart.

THE IMMUTABILITY RULE
---------------------
Everything needed to reconstruct a prediction is frozen at write time and never
recomputed. The most common way a log like this fails is that the score gets
re-derived from current data at evaluation time, so you are forever grading the
newest model against history it never saw. `model_version` records the code
that produced each row; a row is never updated after the fact.

LIVE DATA, NOT THE BACKTEST'S DATA
----------------------------------
This reads exposure_eod and ocdata -- the live tables. The backtest read
exposure_hist, which is EOD-reconstructed. They will not agree exactly:
measured on the Feb-2026 overlap, atm_iv correlates 0.979 and gex_regime signs
agree 92.7%, but net_gex is on a completely different scale and
flip_norm_distance correlates only 0.48. So forward IC is NOT directly
comparable to backtest IC, and `data_source` records which was used.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import straddle_core as sc  # noqa: E402

SCHEMA = """
CREATE TABLE IF NOT EXISTS vol_prediction (
  pred_id        VARCHAR PRIMARY KEY,
  asof_date      DATE,
  symbol         VARCHAR,
  expiry         DATE,
  expiry_rank    INTEGER,
  dte            INTEGER,
  structure      VARCHAR,          -- straddle | strangle_<delta>
  -- frozen inputs -------------------------------------------------------
  spot           DOUBLE, fut_price DOUBLE, atm_strike DOUBLE, lot INTEGER,
  ce_ltp DOUBLE, pe_ltp DOUBLE,
  gross_credit   DOUBLE, net_credit DOUBLE, margin DOUBLE,
  atm_iv DOUBLE, hv20 DOUBLE, iv_pctile DOUBLE, iv_rank DOUBLE,
  em_1d DOUBLE, expected_move DOUBLE,
  total_vol_in_range DOUBLE, total_oi_in_range DOUBLE,
  spread_frac    DOUBLE, spread_cost DOUBLE,
  -- NEW model -----------------------------------------------------------
  ev_per_margin  DOUBLE, trend_eff DOUBLE,
  score          DOUBLE, score_rank INTEGER,
  sim_cvar05_per_margin DOUBLE, sim_pop DOUBLE, sim_tfr DOUBLE,
  rv_fcst_pct    DOUBLE,
  -- LEGACY sortable columns, each a candidate ranking --------------------
  legacy_rom     DOUBLE, legacy_rom_rank INTEGER,
  legacy_be_vs_hv DOUBLE, legacy_be_vs_hv_rank INTEGER,
  legacy_vrp     DOUBLE, legacy_vrp_rank INTEGER,
  legacy_iv_pctile_rank INTEGER,
  -- decision + provenance ------------------------------------------------
  gate_pass      BOOLEAN, gate_reasons VARCHAR,
  decision       VARCHAR,          -- TAKE | SHADOW | REJECT
  event_in_window BOOLEAN,
  data_source    VARCHAR,          -- 'live' here, 'hist' in the backtest
  model_version  VARCHAR,
  created_at     TIMESTAMP
);
CREATE TABLE IF NOT EXISTS vol_outcome (
  pred_id        VARCHAR PRIMARY KEY,
  resolved_at    TIMESTAMP,
  exit_date      DATE, exit_spot DOUBLE, exit_value DOUBLE,
  realised_pnl   DOUBLE, realised_ret DOUBLE, realised_pnl_per_margin DOUBLE,
  realised_move_pct DOUBLE, max_adverse_em DOUBLE,
  had_event      BOOLEAN, ca_in_window BOOLEAN
);
"""

#: every rankable column, new and legacy, so `compare` can score them alike
RANKERS = ["score", "ev_per_margin", "trend_eff",
           "legacy_rom", "legacy_be_vs_hv", "legacy_vrp"]


def git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=HERE,
            stderr=subprocess.DEVNULL, text=True).strip()
    except Exception:  # noqa: BLE001
        return "nogit"


def connect(db: str, bhav: str, read_only: bool = False):
    con = duckdb.connect(db, read_only=read_only)
    con.execute(f"ATTACH IF NOT EXISTS '{bhav}' AS bhav (READ_ONLY)")
    if not read_only:
        con.execute(SCHEMA)
    return con


# --------------------------------------------------------------------------
# predict
# --------------------------------------------------------------------------

def load_candidates(con, asof: date, rank: int) -> pd.DataFrame:
    """ATM chain joined to the live exposure metrics.

    VOLUME COMES FROM ocdata, NOT exposure_eod. exposure_eod has no
    total_vol_in_range (that column exists only in exposure_hist), and its
    total_oi_in_range is on a different scale entirely -- median 14,218 live
    against ~15M in exposure_hist, so the two are not interchangeable and the
    spread lookup's volume breakpoints (308 / 3,213 / 10,534 / 28,989) would be
    meaningless against the live figure.

    Summing ce_volume + pe_volume over strikes within +/-10% of the ATM strike
    reproduces the hist measure closely: median 22,237 live against 22,118 in
    exposure_hist. That is the number spread_fraction() was calibrated on, so
    it is the number that must be fed to it.

    LEFT JOIN on exposure_eod deliberately: 161 symbols quote in ocdata against
    149 in exposure_eod on a typical day, so ~7.5% have no hv20. Those rows are
    kept and gated out rather than silently dropped -- a symbol missing from the
    exposure table is a DATA gap, not a rejected candidate, and the two should
    not look alike in the log.
    """
    return con.execute("""
        WITH last_ts AS (
            SELECT symbol, max(timestamp) mx FROM ocdata
            WHERE CAST(timestamp AS DATE) = ? GROUP BY symbol
        ),
        vol AS (
            SELECT o.symbol, o.expiry,
                   sum(coalesce(o.ce_volume,0) + coalesce(o.pe_volume,0)) AS tvol,
                   sum(coalesce(o.ce_oi,0) + coalesce(o.pe_oi,0)) AS toi,
                   count(*) AS n_strikes_in_range
            FROM ocdata o JOIN last_ts l
              ON o.symbol = l.symbol AND o.timestamp = l.mx
            WHERE o.strike_price BETWEEN o.atm_strike*0.9 AND o.atm_strike*1.1
            GROUP BY 1, 2
        ),
        atm AS (
            SELECT o.symbol, o.expiry, o.atm_strike, o.underlying_price AS spot,
                   o.fut_price, o.lotsize AS lot, o.ce_ltp, o.pe_ltp,
                   o.ce_bid, o.ce_ask, o.pe_bid, o.pe_ask, o.ce_iv, o.pe_iv,
                   o.ce_oi, o.pe_oi,
                   datediff('day', CAST(o.timestamp AS DATE), o.expiry) AS dte,
                   dense_rank() OVER (PARTITION BY o.symbol ORDER BY o.expiry) - 1 AS erank
            FROM ocdata o JOIN last_ts l
              ON o.symbol = l.symbol AND o.timestamp = l.mx
            WHERE o.strike_price = o.atm_strike
              AND coalesce(o.ce_ltp,0) > 0 AND coalesce(o.pe_ltp,0) > 0
        ),
        ex_sym AS (
            -- SYMBOL-level measures. hv20 is the 20-day realised vol of the
            -- UNDERLYING and iv_rank / iv_pctile are constant-maturity series:
            -- none of them belong to a particular expiry, so they join on
            -- symbol alone. Taking them from the expiry_rank=0 row is just
            -- picking a representative row, which is what screener.py does.
            --
            -- Joining these on (symbol, expiry) instead left 91 of 92 rank-1
            -- candidates with a NULL hv20, which silently emptied
            -- legacy_be_vs_hv and legacy_vrp -- i.e. destroyed the paired
            -- comparison this whole log exists for.
            SELECT symbol, hv20, iv_pctile, iv_rank, em_1d
            FROM exposure_eod
            WHERE date = ? AND expiry_rank = 0
        ),
        ex_exp AS (
            -- EXPIRY-level measures: these really are per contract.
            SELECT symbol, expiry, atm_iv, expected_move,
                   gex_regime, gamma_flip, near_earnings
            FROM exposure_eod WHERE date = ?
        )
        SELECT a.*, v.tvol AS total_vol_in_range, v.toi AS total_oi_in_range,
               v.n_strikes_in_range,
               s.hv20, s.iv_pctile, s.iv_rank, s.em_1d,
               x.atm_iv AS ex_iv, x.expected_move,
               x.gex_regime, x.gamma_flip, x.near_earnings
        FROM atm a
        LEFT JOIN vol    v ON v.symbol = a.symbol AND v.expiry = a.expiry
        LEFT JOIN ex_sym s ON s.symbol = a.symbol
        LEFT JOIN ex_exp x ON x.symbol = a.symbol AND x.expiry = a.expiry
        WHERE a.erank = ?
    """, [asof, asof, asof, rank]).df()


def load_returns(con, symbols: list, upto: date) -> dict:
    ph = ",".join("?" * len(symbols))
    try:
        df = con.execute(f"""
            SELECT symbol, log_ret FROM bhav.cash_returns_adj
            WHERE trade_date <= ? AND symbol IN ({ph}) AND log_ret IS NOT NULL
            ORDER BY symbol, trade_date
        """, [upto] + list(symbols)).df()
    except Exception:
        # CA-adjusted table absent: fall back, but say so -- unadjusted returns
        # put ln(1/10) into the GARCH innovation pool on any split
        print("  !! cash_returns_adj missing; using RAW returns (splits uncorrected)")
        df = con.execute(f"""
            SELECT symbol, ln(close/prevclose) AS log_ret FROM bhav.cash_eod
            WHERE series='EQ' AND trade_date <= ? AND symbol IN ({ph})
              AND close > 0 AND prevclose > 0 ORDER BY symbol, trade_date
        """, [upto] + list(symbols)).df()
    return {s: g["log_ret"].to_numpy(float)[-600:] for s, g in df.groupby("symbol")}


def cmd_predict(args, con) -> int:
    asof = args.asof or con.execute(
        "SELECT max(CAST(timestamp AS DATE)) FROM ocdata").fetchone()[0]
    df = load_candidates(con, asof, args.expiry_rank)
    if df.empty:
        print(f"no candidates for {asof}")
        return 1
    print(f"{asof}: {len(df)} candidates, expiry_rank={args.expiry_rank}")

    rets = load_returns(con, df["symbol"].tolist(), asof)
    ver = f"{git_sha()}|2term"
    cfg = sc.GateConfig(min_dte=args.min_dte, max_dte=args.max_dte)

    feats, meta = [], []
    for _, r in df.iterrows():
        sym = str(r["symbol"])
        rr = rets.get(sym)
        iv = r.get("ex_iv")
        if iv is None or not np.isfinite(iv) or iv <= 0:
            iv = (float(r["ce_iv"]) + float(r["pe_iv"])) / 2.0
        spot, fut = float(r["spot"]), float(r["fut_price"] or r["spot"])
        dte, lot = int(r["dte"]), max(int(r["lot"] or 1), 1)
        ce, pe = float(r["ce_ltp"]), float(r["pe_ltp"])
        tvol = float(r["total_vol_in_range"]) if pd.notna(r.get("total_vol_in_range")) else np.nan

        st = sc.Structure(
            symbol=sym, lot_size=lot, ref_price=fut, iv_pct=float(iv),
            kind="straddle", spread_frac=sc.spread_fraction(tvol, dte),
            legs=[sc.Leg("CE", float(r["atm_strike"]), ce, -1,
                         r.get("ce_bid"), r.get("ce_ask")),
                  sc.Leg("PE", float(r["atm_strike"]), pe, -1,
                         r.get("pe_bid"), r.get("pe_ask"))])

        ev = tf = cv = pop = tfr = rvf = np.nan
        if rr is not None and rr.size >= 60:
            try:
                h = max(int(round(dte * sc.TRADING_DAYS / sc.CALENDAR_DAYS)), 2)
                frv = sc.forward_rv(rr, h, model="ewma", min_obs=min(120, rr.size))
                paths = sc.fhs_paths(frv, h, n_paths=args.paths, drift=0.0, seed=42)
                ms = sc.empirical_move_stats(paths)
                sim = sc.simulate_structure(st, paths, spot, stop_k_em=None,
                                            em_ref=spot * ms["mean_abs"])
                ev, cv, pop, tfr = (sim.ev_per_margin, sim.cvar05_per_margin,
                                    sim.pop, sim.tfr)
                rvf = frv.rv_annual_pct
                tf = sc.path_efficiency(rr[-20:])
            except Exception:
                pass

        passed, why = sc.apply_gates(
            st, dte, cfg, event_in_window=bool(r["near_earnings"])
            if pd.notna(r.get("near_earnings")) else None)
        if np.isfinite(tvol) and tvol < args.min_volume:
            passed = False
            why = why + [f"volume {tvol:.0f} < {args.min_volume}"]
        if np.isfinite(cv) and cv < -args.max_cvar:
            passed = False
            why = why + [f"cvar/margin {cv:.2f}"]

        hv20 = float(r["hv20"]) if pd.notna(r.get("hv20")) else np.nan
        mgn = sc.combo_margin(st)
        prem = ce + pe
        be_hv = np.nan
        if np.isfinite(hv20) and hv20 > 0 and dte > 0 and fut > 0:
            mv = fut * (hv20 / 100.0) * np.sqrt(dte / sc.TRADING_DAYS)
            if mv > 0:
                be_hv = prem / mv

        feats.append(sc.Features(symbol=sym, ev_per_margin=ev, trend_eff=tf,
                                 cvar05_per_margin=cv, pop=pop, tfr=tfr,
                                 iv_pct=float(iv), rv_fcst_pct=rvf,
                                 iv_rank=float(r["iv_rank"]) if pd.notna(r.get("iv_rank")) else np.nan,
                                 spread_frac=st.spread_frac))
        meta.append({
            "pred_id": f"{asof:%Y%m%d}|{sym}|{r['expiry']:%Y%m%d}|straddle",
            "asof_date": asof, "symbol": sym, "expiry": r["expiry"],
            "expiry_rank": args.expiry_rank, "dte": dte, "structure": "straddle",
            "spot": spot, "fut_price": fut, "atm_strike": float(r["atm_strike"]),
            "lot": lot, "ce_ltp": ce, "pe_ltp": pe,
            "gross_credit": st.gross_credit, "net_credit": sc.net_credit(st),
            "margin": mgn, "atm_iv": float(iv), "hv20": hv20,
            "iv_pctile": float(r["iv_pctile"]) if pd.notna(r.get("iv_pctile")) else np.nan,
            "iv_rank": float(r["iv_rank"]) if pd.notna(r.get("iv_rank")) else np.nan,
            "em_1d": float(r["em_1d"]) if pd.notna(r.get("em_1d")) else np.nan,
            "expected_move": float(r["expected_move"]) if pd.notna(r.get("expected_move")) else np.nan,
            "total_vol_in_range": tvol,
            "total_oi_in_range": float(r["total_oi_in_range"]) if pd.notna(r.get("total_oi_in_range")) else np.nan,
            "spread_frac": st.spread_frac, "spread_cost": st.spread_cost,
            "sim_cvar05_per_margin": cv, "sim_pop": pop, "sim_tfr": tfr,
            "rv_fcst_pct": rvf,
            "legacy_rom": (prem * lot) / (mgn * lot) if mgn > 0 else np.nan,
            "legacy_be_vs_hv": be_hv,
            "legacy_vrp": (float(iv) - hv20) if np.isfinite(hv20) else np.nan,
            "gate_pass": passed, "gate_reasons": "|".join(why),
            "decision": "SHADOW",   # log everything; trading is a separate call
            "event_in_window": bool(r["near_earnings"]) if pd.notna(r.get("near_earnings")) else None,
            "data_source": "live", "model_version": ver,
            "created_at": datetime.now(),
        })

    scored = sc.cross_sectional_score(feats)
    out = pd.concat([pd.DataFrame(meta),
                     pd.DataFrame(scored)[["score", "score_rank",
                                           "ev_per_margin", "trend_eff"]]], axis=1)
    # rank the legacy columns the same way, so `compare` scores like with like
    for col, dst in [("legacy_rom", "legacy_rom_rank"),
                     ("legacy_be_vs_hv", "legacy_be_vs_hv_rank"),
                     ("legacy_vrp", "legacy_vrp_rank"),
                     ("iv_pctile", "legacy_iv_pctile_rank")]:
        out[dst] = out[col].rank(ascending=False, method="min").astype("Int64")

    # Rewriting the same asof/structure/rank is idempotent: a re-run replaces
    # that day's rows rather than duplicating them. It does NOT touch other
    # days -- a prediction, once written, is never revised, because grading a
    # newer model against history it did not see is the failure mode this log
    # exists to prevent.
    con.execute(
        "DELETE FROM vol_prediction WHERE asof_date = ? AND structure = ? "
        "AND expiry_rank = ?", [asof, "straddle", args.expiry_rank])
    con.register("out_df", out)
    con.execute("INSERT INTO vol_prediction BY NAME SELECT * FROM out_df")
    con.unregister("out_df")
    print(f"  wrote {len(out)} predictions  "
          f"({int(out.gate_pass.sum())} gate-pass)  version={ver}")
    return 0


# --------------------------------------------------------------------------
# resolve
# --------------------------------------------------------------------------

def cmd_resolve(args, con) -> int:
    """Settle every prediction whose expiry has passed and has no outcome yet."""
    todo = con.execute("""
        SELECT p.* FROM vol_prediction p
        LEFT JOIN vol_outcome o USING (pred_id)
        WHERE o.pred_id IS NULL AND p.expiry < CURRENT_DATE
        ORDER BY p.expiry, p.symbol
    """).df()
    if todo.empty:
        print("nothing to resolve")
        return 0
    print(f"resolving {len(todo)} prediction(s)")

    rows = []
    for _, p in todo.iterrows():
        px = con.execute("""
            SELECT trade_date, close FROM bhav.cash_eod
            WHERE series='EQ' AND symbol=? AND trade_date > ? AND trade_date <= ?
            ORDER BY trade_date
        """, [p["symbol"], p["asof_date"], p["expiry"]]).df()
        if px.empty:
            continue
        # A corporate action re-strikes the option, so the contract at exit is
        # not the one entered. Flag it and record the P&L as NULL rather than
        # booking a number that compares two different instruments.
        ca = con.execute("""
            SELECT count(*) FROM bhav.corporate_actions
            WHERE symbol=? AND ex_date > ? AND ex_date <= ?
              AND ca_type IN ('SPLIT','BONUS','DEMERGER','RIGHTS')
        """, [p["symbol"], p["asof_date"], p["expiry"]]).fetchone()[0] > 0

        s_exit = float(px["close"].iloc[-1])
        k = float(p["atm_strike"])
        exit_val = abs(s_exit - k)
        st = sc.Structure(symbol=p["symbol"], lot_size=int(p["lot"]),
                          ref_price=float(p["fut_price"]), iv_pct=float(p["atm_iv"]),
                          legs=[sc.Leg("CE", k, float(p["ce_ltp"]), -1),
                                sc.Leg("PE", k, float(p["pe_ltp"]), -1)],
                          spread_frac=float(p["spread_frac"])
                          if pd.notna(p["spread_frac"]) else None)
        pnl = sc.net_credit(st) - exit_val - sc.exit_costs(st, exit_val, True)
        mae = (np.abs(px["close"].to_numpy(float) - k).max()
               / float(p["expected_move"])) if pd.notna(p.get("expected_move")) \
            and float(p.get("expected_move") or 0) > 0 else np.nan
        rows.append({
            "pred_id": p["pred_id"], "resolved_at": datetime.now(),
            "exit_date": px["trade_date"].iloc[-1], "exit_spot": s_exit,
            "exit_value": exit_val,
            "realised_pnl": None if ca else pnl,
            "realised_ret": None if ca else pnl / st.gross_credit,
            "realised_pnl_per_margin": None if ca else pnl / float(p["margin"]),
            "realised_move_pct": abs(s_exit - k) / k * 100.0,
            "max_adverse_em": mae,
            "had_event": bool(p["event_in_window"]) if pd.notna(p["event_in_window"]) else None,
            "ca_in_window": ca,
        })
    if not rows:
        print("no resolvable rows")
        return 0
    con.register("res", pd.DataFrame(rows))
    con.execute("INSERT OR REPLACE INTO vol_outcome BY NAME SELECT * FROM res")
    con.unregister("res")
    n_ca = sum(r["ca_in_window"] for r in rows)
    print(f"  resolved {len(rows)}  ({n_ca} nulled for a corporate action)")
    return 0


# --------------------------------------------------------------------------
# status / compare
# --------------------------------------------------------------------------

def cmd_status(args, con) -> int:
    r = con.execute("""
        SELECT count(*) n, count(DISTINCT asof_date) n_days,
               min(asof_date) d0, max(asof_date) d1,
               count(*) FILTER (WHERE gate_pass) gated
        FROM vol_prediction""").fetchone()
    if not r[0]:
        print("vol_prediction is empty")
        return 0
    o = con.execute("SELECT count(*), count(realised_pnl) FROM vol_outcome").fetchone()
    print(f"  predictions {r[0]:,} over {r[1]} day(s)  {r[2]} .. {r[3]}  "
          f"gate-pass {r[4]:,}")
    print(f"  outcomes    {o[0]:,} resolved, {o[1]:,} with P&L "
          f"({o[0]-o[1]} nulled for corporate actions)")
    print(con.execute("""
        SELECT model_version, count(*) n, min(asof_date) d0, max(asof_date) d1
        FROM vol_prediction GROUP BY 1 ORDER BY 3""").df().to_string(index=False))
    return 0


def cmd_compare(args, con) -> int:
    """Per-expiry rank IC for the new score AND every legacy sortable column.

    Paired: same candidates, same expiries, so the only thing that differs is
    the ranking rule. Backtest IC for the 2-term model was 0.1307 (t 8.24) over
    71 expiries of exposure_hist -- forward IC here is on live data and is NOT
    directly comparable, but the RELATIVE ordering of the models is.
    """
    df = con.execute("""
        SELECT p.*, o.realised_pnl_per_margin AS y
        FROM vol_prediction p JOIN vol_outcome o USING (pred_id)
        WHERE p.gate_pass AND o.realised_pnl_per_margin IS NOT NULL
    """).df()
    if len(df) < 50:
        print(f"only {len(df)} resolved gate-pass rows -- too few to compare")
        return 0
    from scipy.stats import spearmanr
    print(f"\n{len(df):,} resolved rows over {df.expiry.nunique()} expiries\n")
    print(f"  {'ranker':22s} {'IC':>8s} {'t':>7s} {'pos':>8s}")
    for c in RANKERS:
        if c not in df.columns:
            continue
        per = []
        for _, g in df.groupby("expiry"):
            a, b = g[c].to_numpy(float), g["y"].to_numpy(float)
            m = np.isfinite(a) & np.isfinite(b)
            if m.sum() >= 8 and a[m].min() < a[m].max():
                per.append(spearmanr(a[m], b[m]).statistic)
        per = np.array([x for x in per if np.isfinite(x)])
        if per.size < 2:
            continue
        t = per.mean() / (per.std(ddof=1) / np.sqrt(per.size))
        print(f"  {c:22s} {per.mean():+8.4f} {t:+7.2f} "
              f"{int((per>0).sum()):4d}/{per.size}")
    print("\n  Same candidates, same expiries -- only the ranking rule differs.")
    print("  Retire the legacy view only if the new score leads consistently")
    print("  over a full year, not on one good quarter.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["predict", "resolve", "status", "compare"])
    p.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    p.add_argument("--bhav", default=r"C:\Users\aniru\od\bhavcopy.duckdb")
    p.add_argument("--asof", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    p.add_argument("--expiry-rank", type=int, default=0, dest="expiry_rank")
    p.add_argument("--paths", type=int, default=8000)
    p.add_argument("--min-dte", type=int, default=10, dest="min_dte")
    p.add_argument("--max-dte", type=int, default=45, dest="max_dte")
    p.add_argument("--min-volume", type=float, default=1000.0, dest="min_volume")
    p.add_argument("--max-cvar", type=float, default=5.0, dest="max_cvar")
    args = p.parse_args()

    con = connect(args.db, args.bhav, read_only=(args.mode in ("status", "compare")))
    try:
        return {"predict": cmd_predict, "resolve": cmd_resolve,
                "status": cmd_status, "compare": cmd_compare}[args.mode](args, con)
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
