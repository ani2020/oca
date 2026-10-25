"""
build_richness_ref.py -- reference table for strike premium-richness flags.

For NIFTY / BANKNIFTY (any index with options_eod + span_contract history), sell
each option at a grid of moneyness x entry-DTE cells and hold to expiry, 2020-06
onward, and record per cell what the seller actually kept:

  hedged_bps      mean P&L, bps of spot, net of costs, futures delta-hedged at entry
  hedged_t        t-stat of that mean
  rom_annual_pct  return on SPAN+ELM margin, annualised as
                  per-trade ROM * min(243 / holding days, trades available per year)
  hedged_h1/h2    the mean in each half of the sample (split 2023-07-01)
  payoff_ratio    payoff paid / premium received
  pn_p10..p90     the cell's usual premium in sigma units, pn = px / (F*sigma*sqrt(T)),
                  so a live strike can be scored "richer / cheaper than usual"
                  (sanity: ATM pn ~0.399 = 1/sqrt(2*pi); measured 0.39-0.40)

Cell definitions (identical to od/claude/voltiming/richness.py, the research version):
  entry day   latest trading day with calendar DTE in [tdte, tdte+3]
  forward F   K_atm + C_atm - P_atm;  sigma from the ATM straddle
  kz          ln(K/F) / (sigma*sqrt(T)) on a 0.25 grid, OTM side only (both at 0);
              nearest listed strike with volume > 0, dropped if > 0.15 sigma off grid
  cost (Rs)   0.05 tick + 0.25% premium + 0.125% intrinsic
  margin      span_contract.short_scan (tag 's') + 2% of spot ELM

Reads bhavcopy.duckdb only (read-only); writes richness.duckdb next to oc.duckdb.
Slow (~1-2 min per symbol) and changes slowly, so it rebuilds at most weekly
unless --force. Run post-close.

    python oc/build_richness_ref.py --bhav bhavcopy.duckdb --out richness.duckdb
    python oc/build_richness_ref.py --symbols NIFTY,BANKNIFTY --force

Validated 2026-10-12: the aggregation SQL reproduces the study numbers exactly
(e.g. NIFTY PE 21-DTE -1.5 sigma: 12.13 bps hedged, t 5.55, 29.3%/yr on margin).

Caveats carried to the UI: history starts after the March-2020 crash (left tail
under-sampled); flat-vol delta under-hedges puts; margin is naked single-leg.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta

import duckdb

INDEX_NAME = {"NIFTY": "nifty 50", "BANKNIFTY": "nifty bank",
              "FINNIFTY": "nifty financial services", "MIDCPNIFTY": "nifty midcap select"}
DTES = [1, 2, 3, 4, 5, 7, 10, 14, 21, 28]
SPLIT = "2023-07-01"
ELM = 0.02
MIN_N = 60

TRADES_SQL = """
CREATE OR REPLACE TEMP TABLE rich AS
WITH sp AS (SELECT trade_date d, close s FROM bc.index_eod WHERE index_name ILIKE '{index_name}'),
o AS (SELECT trade_date d, expiry e, strike k, opt_type t, coalesce(nullif(close,0),settle) px, volume v
      FROM bc.options_eod WHERE symbol='{symbol}'),
atm AS (SELECT o.d, o.e, arg_min(o.k, abs(o.k-sp.s)) k_atm, any_value(sp.s) s FROM o JOIN sp USING (d) GROUP BY 1,2),
sig AS (SELECT a.d, a.e, a.s, a.k_atm + c.px - p.px f,
               (c.px+p.px)/((a.k_atm + c.px - p.px)*sqrt(2/pi())*sqrt((a.e-a.d)/365.0)) sigma
        FROM atm a JOIN o c ON c.d=a.d AND c.e=a.e AND c.k=a.k_atm AND c.t='CE'
        JOIN o p ON p.d=a.d AND p.e=a.e AND p.k=a.k_atm AND p.t='PE'
        WHERE a.e > a.d AND c.px>0 AND p.px>0 AND c.v>0 AND p.v>0),
tg AS (SELECT unnest({dtes}) tdte),
ent AS (SELECT tg.tdte, sig.e, max(sig.d) d FROM sig, tg
        WHERE sig.e - sig.d >= tg.tdte AND sig.e - sig.d <= tg.tdte + 3 GROUP BY 1,2),
ex AS (SELECT DISTINCT e FROM o WHERE e <= (SELECT max(d) FROM sp)),
st AS (SELECT ex.e, (SELECT s FROM sp WHERE sp.d <= ex.e ORDER BY sp.d DESC LIMIT 1) s_e,
              (SELECT count(*) FROM sp WHERE sp.d <= ex.e) rn_e FROM ex),
kg AS (SELECT unnest(range(-12,13))/4.0 kz),
tgt AS (SELECT ent.tdte, ent.e, ent.d, sig.s, sig.f, sig.sigma, (ent.e-ent.d) dte, kg.kz,
               CASE WHEN kg.kz>0 THEN 'CE' WHEN kg.kz<0 THEN 'PE' END t0,
               sig.f*exp(kg.kz*sig.sigma*sqrt((ent.e-ent.d)/365.0)) kstar
        FROM ent JOIN sig ON sig.d=ent.d AND sig.e=ent.e, kg),
tt AS (SELECT tgt.* EXCLUDE (t0), t FROM tgt, (SELECT unnest(['CE','PE']) t) x
       WHERE tgt.t0 = x.t OR (tgt.kz=0)),
pick AS (SELECT tt.tdte, tt.e, tt.d, tt.s, tt.f, tt.sigma, tt.dte, tt.kz, tt.t,
                arg_min(o.k, abs(o.k - tt.kstar)) k, arg_min(o.px, abs(o.k - tt.kstar)) px
         FROM tt JOIN o ON o.d=tt.d AND o.e=tt.e AND o.t=tt.t AND o.v>0 AND o.px>=0.05 GROUP BY ALL)
SELECT pick.*, st.s_e,
       st.rn_e - (SELECT count(*) FROM sp WHERE sp.d <= pick.d) tdays,
       CASE WHEN t='CE' THEN greatest(st.s_e-k,0) ELSE greatest(k-st.s_e,0) END payoff
FROM pick JOIN st USING (e) WHERE st.s_e IS NOT NULL
"""

AGG_SQL = """
WITH r0 AS (SELECT *, sigma*sqrt(dte/365.0) sT, ln(k/f)/(sigma*sqrt(dte/365.0)) kr FROM rich),
r1 AS (SELECT *, (ln(f/k) + 0.5*sT*sT)/sT d1 FROM r0 WHERE abs(kr-kz) < 0.15),
r2 AS (SELECT *, 0.5*(1+tanh(sqrt(2/pi())*(d1+0.044715*d1*d1*d1))) nd1 FROM r1),
r AS (SELECT *, CASE WHEN t='CE' THEN nd1 ELSE nd1-1 END bsd,
             (px - payoff - (0.05 + 0.0025*px + 0.00125*payoff))/s*1e4 net,
             px/(f*sT) pn FROM r2),
h AS (SELECT r.*, r.net + r.bsd*(r.s_e - r.f)/r.s*1e4 neth,
             (sc.short_scan + {elm}*r.s)/r.s*1e4 margin_bps
      FROM r LEFT JOIN bc.span_contract sc
        ON sc.trade_date=r.d AND sc.symbol='{symbol}' AND sc.instr=r.t AND sc.expiry=r.e
       AND sc.strike=r.k AND sc.tag='s'),
yrs AS (SELECT greatest((max(d) - min(d)) / 365.25, 0.5) y FROM h),
g AS (SELECT t, tdte, kz, count(*) AS n, avg(tdays) AS hold_days,
             avg(px/s)*1e4 AS prem_bps, sum(payoff)/sum(px) AS payoff_ratio,
             avg(net) AS net_bps, avg(neth) AS hedged_bps,
             avg(neth)/(stddev(neth)/sqrt(count(*))) AS hedged_t,
             avg(neth)/avg(px/s*1e4) AS capture,
             avg(margin_bps) AS margin_bps,
             avg(neth/margin_bps)*100 AS rom_trade_pct,
             min(neth/margin_bps)*100 AS worst_rom_pct,
             min(neth) AS worst_bps,
             avg(CASE WHEN d <  DATE '{split}' THEN neth END) AS hedged_h1,
             avg(CASE WHEN d >= DATE '{split}' THEN neth END) AS hedged_h2,
             count(CASE WHEN d <  DATE '{split}' THEN 1 END) AS n_h1,
             count(CASE WHEN d >= DATE '{split}' THEN 1 END) AS n_h2,
             quantile_cont(pn, [0.1, 0.25, 0.5, 0.75, 0.9]) AS pnq,
             min(d) AS d0, max(d) AS d1
      FROM h GROUP BY 1,2,3 HAVING count(*) >= {min_n})
SELECT '{symbol}' AS symbol, g.* EXCLUDE (pnq),
       g.rom_trade_pct * least(243.0 / greatest(g.hold_days, 1), g.n / yrs.y) AS rom_annual_pct,
       pnq[1] AS pn_p10, pnq[2] AS pn_p25, pnq[3] AS pn_p50, pnq[4] AS pn_p75, pnq[5] AS pn_p90
FROM g, yrs
ORDER BY t, tdte, kz
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS richness_meta (
    symbol VARCHAR PRIMARY KEY, built_at TIMESTAMP, d0 DATE, d1 DATE,
    n_cells INTEGER, n_trades INTEGER, split DATE, notes VARCHAR);
"""

# ── single stocks (added 2026-10-14) ───────────────────────────────────────
# Same cell construction as the index reference, with what differs for stocks:
#   spot          cash_eod EQ close (expiry settles on the stock's close)
#   corp actions  trades whose window spans a split/bonus/large CA (|adj_factor-1|
#                 > 2%) are dropped: NSE adjusts the strikes, the payoff breaks
#   results       ev = 1 when a results date falls in [entry, expiry]. Kept as
#                 SEPARATE cells: on 10 large caps (2020-26, 7-14 DTE) ATM legs
#                 made +9.4 bps (t 3.1) without results but -9 bps with, and
#                 puts paid back 1.2-1.3x premium. The screen flags ev=0 cells
#                 only; ev=1 is shown as a results warning.
#   costs         half-spread 1.75% of premium (stock OTM full spreads measured
#                 3.2-3.6% of premium on 2026-10-12..14, vs 1.6% for NIFTY)
#   margin        SPAN short_scan + 3.5% ELM (stock ELM), same span_contract join
#   DTE grid      monthly expiries only: 3, 7, 10, 14, 21, 28
#   pooling       ~75 monthly expiries per stock is too few for t >= 3 in most
#                 cells, so flags come from the POOLED reference over all F&O
#                 stocks (symbol '_STOCKS'); per-stock cells (n >= 24) are stored
#                 beside it and shown as that stock's own record.
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
STOCK_DTES = [3, 7, 10, 14, 21, 28]
STOCK_ELM = 0.035
STOCK_HALF_SPREAD = 0.0175
STOCK_MIN_N_POOLED = 60
STOCK_MIN_N_OWN = 24
POOLED = "_STOCKS"

STOCK_TRADES_SQL = """
CREATE OR REPLACE TEMP TABLE srich AS
WITH syms AS (SELECT DISTINCT symbol FROM bc.options_eod
              WHERE trade_date >= (SELECT max(trade_date) FROM bc.options_eod) - INTERVAL 45 DAY
                AND symbol NOT IN ({indices}) {only}),
sp AS (SELECT symbol, trade_date d, close s, row_number() OVER (PARTITION BY symbol ORDER BY trade_date) rn
       FROM bc.cash_eod WHERE series = 'EQ' AND close > 0 AND symbol IN (SELECT symbol FROM syms)),
o AS (SELECT symbol, trade_date d, expiry e, strike k, opt_type t, coalesce(nullif(close,0), settle) px, volume v
      FROM bc.options_eod WHERE symbol IN (SELECT symbol FROM syms) AND trade_date >= DATE '2020-06-15'),
atm AS (SELECT o.symbol, o.d, o.e, arg_min(o.k, abs(o.k - sp.s)) k_atm, any_value(sp.s) s, any_value(sp.rn) rn_d
        FROM o JOIN sp USING (symbol, d) GROUP BY 1, 2, 3),
sig AS (SELECT a.symbol, a.d, a.e, a.s, a.rn_d, a.k_atm + c.px - p.px f,
               (c.px + p.px) / ((a.k_atm + c.px - p.px) * sqrt(2 / pi()) * sqrt((a.e - a.d) / 365.0)) sigma
        FROM atm a
        JOIN o c ON c.symbol = a.symbol AND c.d = a.d AND c.e = a.e AND c.k = a.k_atm AND c.t = 'CE'
        JOIN o p ON p.symbol = a.symbol AND p.d = a.d AND p.e = a.e AND p.k = a.k_atm AND p.t = 'PE'
        WHERE a.e > a.d AND c.px > 0 AND p.px > 0 AND c.v > 0 AND p.v > 0),
tg AS (SELECT unnest({dtes}) tdte),
ent AS (SELECT sig.symbol, tg.tdte, sig.e, max(sig.d) d FROM sig, tg
        WHERE sig.e - sig.d >= tg.tdte AND sig.e - sig.d <= tg.tdte + 3 GROUP BY 1, 2, 3),
lastd AS (SELECT symbol, max(d) dmax FROM sp GROUP BY 1),
ex AS (SELECT DISTINCT ent.symbol, ent.e FROM ent JOIN lastd USING (symbol) WHERE ent.e <= lastd.dmax),
st AS (SELECT ex.symbol, ex.e, sp.s s_e, sp.rn rn_e, sp.d d_e
       FROM ex ASOF JOIN sp ON ex.symbol = sp.symbol AND ex.e >= sp.d),
kg AS (SELECT unnest(range(-12, 13)) / 4.0 kz),
tgt AS (SELECT ent.symbol, ent.tdte, ent.e, ent.d, sig.s, sig.f, sig.sigma, sig.rn_d, (ent.e - ent.d) dte, kg.kz,
               CASE WHEN kg.kz > 0 THEN 'CE' WHEN kg.kz < 0 THEN 'PE' END t0,
               sig.f * exp(kg.kz * sig.sigma * sqrt((ent.e - ent.d) / 365.0)) kstar
        FROM ent JOIN sig ON sig.symbol = ent.symbol AND sig.d = ent.d AND sig.e = ent.e, kg),
tt AS (SELECT tgt.* EXCLUDE (t0), x.t FROM tgt, (SELECT unnest(['CE', 'PE']) t) x
       WHERE tgt.t0 = x.t OR tgt.kz = 0),
pick AS (SELECT tt.symbol, tt.tdte, tt.e, tt.d, tt.s, tt.f, tt.sigma, tt.rn_d, tt.dte, tt.kz, tt.t,
                arg_min(o.k, abs(o.k - tt.kstar)) k, arg_min(o.px, abs(o.k - tt.kstar)) px
         FROM tt JOIN o ON o.symbol = tt.symbol AND o.d = tt.d AND o.e = tt.e AND o.t = tt.t
                       AND o.v > 0 AND o.px >= 0.05
         GROUP BY ALL),
ca AS (SELECT symbol, trade_date FROM bc.cash_returns_adj
       WHERE is_ca AND abs(coalesce(adj_factor, 1) - 1) > 0.02),
res AS (SELECT DISTINCT symbol, event_date FROM bc.corporate_events WHERE is_results)
SELECT pick.* EXCLUDE (rn_d), st.s_e, st.rn_e - pick.rn_d tdays,
       CASE WHEN t = 'CE' THEN greatest(st.s_e - k, 0) ELSE greatest(k - st.s_e, 0) END payoff,
       EXISTS (SELECT 1 FROM res WHERE res.symbol = pick.symbol
               AND res.event_date BETWEEN pick.d AND pick.e)::INT ev
FROM pick JOIN st USING (symbol, e)
WHERE st.d_e >= pick.e - 5
  AND NOT EXISTS (SELECT 1 FROM ca WHERE ca.symbol = pick.symbol
                  AND ca.trade_date > pick.d AND ca.trade_date <= pick.e)
"""

STOCK_AGG_SQL = """
WITH r0 AS (SELECT *, sigma*sqrt(dte/365.0) sT, ln(k/f)/(sigma*sqrt(dte/365.0)) kr FROM srich),
r1 AS (SELECT *, (ln(f/k) + 0.5*sT*sT)/sT d1 FROM r0 WHERE abs(kr-kz) < 0.15),
r2 AS (SELECT *, 0.5*(1+tanh(sqrt(2/pi())*(d1+0.044715*d1*d1*d1))) nd1 FROM r1),
r AS (SELECT *, CASE WHEN t='CE' THEN nd1 ELSE nd1-1 END bsd,
             (px - payoff - (0.05 + {half_spread}*px + 0.00125*payoff))/s*1e4 net,
             px/(f*sT) pn FROM r2),
h AS (SELECT r.*, r.net + r.bsd*(r.s_e - r.f)/r.s*1e4 neth,
             (sc.short_scan + {elm}*r.s)/r.s*1e4 margin_bps
      FROM r LEFT JOIN bc.span_contract sc
        ON sc.trade_date=r.d AND sc.symbol=r.symbol AND sc.instr=r.t AND sc.expiry=r.e
       AND sc.strike=r.k AND sc.tag='s'),
g AS (SELECT {sym_expr} AS symbol, t, tdte, kz, ev, count(*) AS n, avg(tdays) AS hold_days,
             avg(px/s)*1e4 AS prem_bps, sum(payoff)/sum(px) AS payoff_ratio,
             avg(net) AS net_bps, avg(neth) AS hedged_bps,
             avg(neth)/(stddev(neth)/sqrt(count(*))) AS hedged_t,
             avg(neth)/avg(px/s*1e4) AS capture,
             avg(margin_bps) AS margin_bps,
             avg(neth/margin_bps)*100 AS rom_trade_pct,
             min(neth/margin_bps)*100 AS worst_rom_pct,
             min(neth) AS worst_bps,
             avg(CASE WHEN d <  DATE '{split}' THEN neth END) AS hedged_h1,
             avg(CASE WHEN d >= DATE '{split}' THEN neth END) AS hedged_h2,
             count(CASE WHEN d <  DATE '{split}' THEN 1 END) AS n_h1,
             count(CASE WHEN d >= DATE '{split}' THEN 1 END) AS n_h2,
             quantile_cont(pn, [0.1, 0.25, 0.5, 0.75, 0.9]) AS pnq,
             min(d) AS d0, max(d) AS d1,
             greatest((max(d) - min(d)) / 365.25, 0.5) AS yrs
      FROM h GROUP BY {group} HAVING count(*) >= {min_n})
SELECT g.* EXCLUDE (pnq, yrs),
       g.rom_trade_pct * least(243.0 / greatest(g.hold_days, 1), g.n / g.yrs) AS rom_annual_pct,
       pnq[1] AS pn_p10, pnq[2] AS pn_p25, pnq[3] AS pn_p50, pnq[4] AS pn_p75, pnq[5] AS pn_p90
FROM g
ORDER BY symbol, t, tdte, kz, ev
"""


def build_stocks(bcon, only: list[str] | None = None):
    """(pooled frame, per-stock frame) for every F&O stock with options in the
    last 45 days. One extraction pass, two aggregations."""
    idx = ", ".join(f"'{s}'" for s in sorted(INDEX_SYMBOLS))
    flt = ("AND symbol IN (" + ", ".join(f"'{s}'" for s in only) + ")") if only else ""
    bcon.execute(STOCK_TRADES_SQL.format(indices=idx, only=flt, dtes=STOCK_DTES))
    common = dict(half_spread=STOCK_HALF_SPREAD, elm=STOCK_ELM, split=SPLIT)
    pooled = bcon.execute(STOCK_AGG_SQL.format(sym_expr=f"'{POOLED}'", group="t, tdte, kz, ev",
                                               min_n=STOCK_MIN_N_POOLED, **common)).df()
    own = bcon.execute(STOCK_AGG_SQL.format(sym_expr="symbol", group="symbol, t, tdte, kz, ev",
                                            min_n=STOCK_MIN_N_OWN, **common)).df()
    return pooled, own


def _write(out, sym_list, df, notes: str) -> None:
    """Replace the reference rows (and meta) for sym_list with df, atomically."""
    out.execute("BEGIN")
    out.execute("CREATE TABLE IF NOT EXISTS richness_ref AS SELECT * FROM df WHERE false")
    out.execute("ALTER TABLE richness_ref ADD COLUMN IF NOT EXISTS ev INTEGER DEFAULT 0")
    ph = ", ".join("?" * len(sym_list))
    out.execute(f"DELETE FROM richness_ref WHERE symbol IN ({ph})", list(sym_list))
    out.execute("INSERT INTO richness_ref BY NAME SELECT * FROM df")
    out.execute(f"DELETE FROM richness_meta WHERE symbol IN ({ph})", list(sym_list))
    now = datetime.now()
    for s, g in df.groupby("symbol"):
        out.execute("INSERT INTO richness_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [s, now, g.d0.min(), g.d1.max(), len(g), int(g.n.sum()), SPLIT, notes])
    out.execute("COMMIT")


def ref_sql(symbol: str, index_name: str) -> tuple[str, str]:
    return (TRADES_SQL.format(symbol=symbol, index_name=index_name, dtes=DTES),
            AGG_SQL.format(symbol=symbol, elm=ELM, split=SPLIT, min_n=MIN_N))


def build_symbol(bcon, symbol: str):
    t_sql, a_sql = ref_sql(symbol, INDEX_NAME[symbol])
    bcon.execute(t_sql)
    return bcon.execute(a_sql).df()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bhav", default=r"C:\Users\aniru\od\bhavcopy.duckdb")
    ap.add_argument("--out", default=r"C:\Users\aniru\od\richness.duckdb")
    ap.add_argument("--symbols", default="NIFTY,BANKNIFTY,STOCKS",
                    help="indices by name; STOCKS = every F&O stock (pooled + per-stock)")
    ap.add_argument("--only", default=None,
                    help="with STOCKS: restrict to these stocks (testing), e.g. RELIANCE,INFY")
    ap.add_argument("--min-age-days", type=int, default=7, help="skip a symbol built more recently than this")
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)

    out = duckdb.connect(a.out)
    out.execute(SCHEMA)
    bcon = duckdb.connect(":memory:")
    bcon.execute(f"ATTACH '{a.bhav}' AS bc (READ_ONLY)")
    rc = 0
    for sym in [s.strip().upper() for s in a.symbols.split(",") if s.strip()]:
        if sym == "STOCKS":
            last = out.execute("SELECT built_at FROM richness_meta WHERE symbol=?", [POOLED]).fetchone()
            if last and not a.force and last[0] > datetime.now() - timedelta(days=a.min_age_days):
                print(f"  STOCKS: built {last[0]:%Y-%m-%d}, < {a.min_age_days}d old, skipped (use --force)")
                continue
            t0 = datetime.now()
            only = [s.strip().upper() for s in a.only.split(",")] if a.only else None
            try:
                pooled, own = build_stocks(bcon, only)
            except Exception as exc:  # noqa: BLE001
                print(f"  STOCKS: FAILED {exc}"); rc = 1; continue
            if pooled.empty:
                print("  STOCKS: no cells"); rc = 1; continue
            # Replace every stock symbol previously built, plus the pooled set.
            prev = [r[0] for r in out.execute(
                "SELECT symbol FROM richness_meta WHERE notes LIKE 'stock%'").fetchall()]
            allsyms = sorted(set(prev) | set(own.symbol) | {POOLED})
            import pandas as pd
            _write(out, allsyms, pd.concat([pooled, own], ignore_index=True),
                   "stock: hold-to-expiry, futures delta-hedged, SPAN+3.5% ELM, 1.75% half-spread; "
                   "ev=1 cells = results inside the window; flags from the pooled _STOCKS rows")
            print(f"  STOCKS: {own.symbol.nunique()} stocks, pooled {len(pooled)} cells, "
                  f"own {len(own)} cells, {int(pooled.n.sum()):,} trades, "
                  f"{(datetime.now()-t0).seconds}s")
            continue
        if sym not in INDEX_NAME:
            print(f"  {sym}: no index_eod mapping, skipped"); continue
        last = out.execute("SELECT built_at FROM richness_meta WHERE symbol=?", [sym]).fetchone()
        if last and not a.force and last[0] > datetime.now() - timedelta(days=a.min_age_days):
            print(f"  {sym}: built {last[0]:%Y-%m-%d}, < {a.min_age_days}d old, skipped (use --force)")
            continue
        t0 = datetime.now()
        try:
            df = build_symbol(bcon, sym)
        except Exception as exc:  # noqa: BLE001
            print(f"  {sym}: FAILED {exc}"); rc = 1; continue
        if df.empty:
            print(f"  {sym}: no cells"); rc = 1; continue
        df["ev"] = 0                    # index reference: no results split
        out.execute("BEGIN")
        out.execute("CREATE TABLE IF NOT EXISTS richness_ref AS SELECT * FROM df WHERE false")
        out.execute("ALTER TABLE richness_ref ADD COLUMN IF NOT EXISTS ev INTEGER DEFAULT 0")
        out.execute("DELETE FROM richness_ref WHERE symbol=?", [sym])
        out.execute("INSERT INTO richness_ref BY NAME SELECT * FROM df")
        out.execute("DELETE FROM richness_meta WHERE symbol=?", [sym])
        out.execute("INSERT INTO richness_meta VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [sym, datetime.now(), df.d0.min(), df.d1.max(), len(df), int(df.n.sum()), SPLIT,
                     "hold-to-expiry, futures delta-hedged, SPAN+ELM margin"])
        out.execute("COMMIT")
        print(f"  {sym}: {len(df)} cells, {int(df.n.sum()):,} trades, {(datetime.now()-t0).seconds}s")
    out.close()
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
