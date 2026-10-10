"""
oi_reconcile.py -- OI reconciliation gate: 15:40 option-chain snapshot vs the
official bhavcopy EOD OI, per (symbol, date, expiry). (2026-10-04)

Why: every exposure_eod measure is built from the snapshot's OI. On the chains
exposure_eod uses (Aug-Oct 2026, ranks 0-1) the snapshot matches bhavcopy to a
median 0.5% OI-weighted error, but a tail does not: next-month chains for BPCL
and INDIANB were missing ~60% of their OI at 15:40. Those rows should not drive
a trade.

Rule (per chain, starting from bhavcopy strikes with OI > 0, so strikes absent
from the snapshot count as zero rather than dropping out):
    err      = sum|snap - bhav| / sum(bhav)            (OI-weighted)
    zero_oi  = bhav OI on strikes the snapshot shows as 0/missing / sum(bhav)
    FLAG     = (err > 3% AND sum|snap - bhav| >= 100 contracts) OR zero_oi > 2%
The 100-contract floor matters: 3% of a <2k-contract chain is a few dozen
contracts and moves no gamma measure; without it 19% of thin chains flagged.
With it: 1.0% of rank-0 and 2.6% of rank-1 chains (Aug-Oct 2026).

Writes
    oi_recon_eod       one row per (symbol, date, expiry) with the diagnostics
                       and same-expiry futures OI (contracts) and its change
    exposure_eod       oi_recon_err, oi_recon_ok, fut_oi, fut_oi_chg on matching
                       rows (futures OI is positioning context only: futures have
                       no gamma, it never enters GEX / vanna / charm)

Consumers: build_vol_predictions gates on oi_recon_ok = FALSE.

Run (nightly, after exposure_eod, before the straddle builder):
    python oc\\oi_reconcile.py --db oc.duckdb --bhav bhavcopy.duckdb --from D0 --to D1
    python oc\\oi_reconcile.py --all            # backfill every snapshot date
"""
from __future__ import annotations

import argparse
import os

import duckdb

ERR_MAX = 0.03          # OI-weighted relative error
ABS_MIN = 100.0         # contracts: below this an err breach is immaterial
ZERO_OI_MAX = 0.02      # share of official OI on strikes the snapshot shows as 0

DDL = """
CREATE TABLE IF NOT EXISTS oi_recon_eod (
    symbol VARCHAR, date DATE, expiry DATE,
    n_strikes INTEGER,           -- bhavcopy (strike, CE/PE) legs with OI > 0
    bhav_oi DOUBLE,              -- contracts
    snap_oi DOUBLE,              -- contracts, on the same legs
    abs_diff DOUBLE,             -- sum |snap - bhav|, contracts
    err DOUBLE,                  -- abs_diff / bhav_oi
    bias DOUBLE,                 -- sum(snap - bhav) / bhav_oi (negative = snapshot short)
    zero_oi_share DOUBLE,        -- bhav OI on legs the snapshot shows 0 or lacks
    vol_match_share DOUBLE,      -- legs whose volume matches exactly
    fut_oi DOUBLE, fut_oi_chg DOUBLE,  -- same-expiry futures, contracts
    recon_ok BOOLEAN,
    computed_at TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (symbol, date, expiry)
)"""

SQL = f"""
WITH last AS (
    SELECT symbol, CAST(timestamp AS DATE) AS d, max(timestamp) AS ts
    FROM ocdata
    WHERE CAST(timestamp AS DATE) BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
    GROUP BY 1, 2
),
o AS (
    SELECT o.symbol, l.d, CAST(o.expiry AS DATE) AS expiry, o.strike_price AS k,
           o.ce_oi, o.pe_oi, o.ce_volume, o.pe_volume
    FROM ocdata o JOIN last l ON o.symbol = l.symbol AND o.timestamp = l.ts
),
u AS (
    SELECT symbol, d, expiry, k, 'CE' AS t, ce_oi AS oi, ce_volume AS v FROM o
    UNION ALL
    SELECT symbol, d, expiry, k, 'PE', pe_oi, pe_volume FROM o
),
b AS (
    -- bhavcopy OI is in SHARES; contracts = oi / lot (verified 2020-2026)
    SELECT trade_date AS d, symbol, expiry, strike AS k, opt_type AS t,
           oi / lot AS boi, volume AS bv
    FROM bh.options_eod
    WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
      AND lot > 0 AND oi > 0
      AND symbol IN (SELECT DISTINCT symbol FROM last)
),
j AS (SELECT b.*, u.oi AS soi, u.v AS sv FROM b LEFT JOIN u USING (symbol, d, expiry, k, t)),
g AS (
    SELECT symbol, d AS date, expiry, count(*) AS n_strikes,
           sum(boi) AS bhav_oi, sum(coalesce(soi, 0)) AS snap_oi,
           sum(abs(coalesce(soi, 0) - boi)) AS abs_diff,
           sum(abs(coalesce(soi, 0) - boi)) / sum(boi) AS err,
           sum(coalesce(soi, 0) - boi) / sum(boi) AS bias,
           coalesce(sum(boi) FILTER (WHERE coalesce(soi, 0) = 0), 0) / sum(boi) AS zero_oi_share,
           avg((sv = bv)::INT) AS vol_match_share
    FROM j GROUP BY 1, 2, 3
),
f AS (
    SELECT trade_date AS date, symbol, expiry,
           oi / NULLIF(lot, 0) AS fut_oi, chgoi / NULLIF(lot, 0) AS fut_oi_chg
    FROM bh.futures_eod
    WHERE trade_date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
)
SELECT g.*, f.fut_oi, f.fut_oi_chg,
       NOT ((g.err > {ERR_MAX} AND g.abs_diff >= {ABS_MIN}) OR g.zero_oi_share > {ZERO_OI_MAX}) AS recon_ok
FROM g LEFT JOIN f USING (date, symbol, expiry)
"""


def run(con, bhav: str, d0: str, d1: str) -> int:
    con.execute(f"ATTACH IF NOT EXISTS '{bhav}' AS bh (READ_ONLY)")
    con.execute(DDL)
    for col, typ in (("oi_recon_err", "DOUBLE"), ("oi_recon_ok", "BOOLEAN"),
                     ("fut_oi", "DOUBLE"), ("fut_oi_chg", "DOUBLE")):
        con.execute(f"ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS {col} {typ}")
    df = con.execute(SQL, [d0, d1, d0, d1, d0, d1]).df()
    if df.empty:
        print(f"  oi_recon: no snapshot/bhavcopy overlap in {d0}..{d1}")
        return 0
    con.execute("DELETE FROM oi_recon_eod WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)",
                [d0, d1])
    con.register("_rec", df)
    cols = ",".join(c for c in df.columns)
    con.execute(f"INSERT INTO oi_recon_eod ({cols}) SELECT {cols} FROM _rec")
    con.execute("""
        UPDATE exposure_eod AS e
        SET oi_recon_err = r.err, oi_recon_ok = r.recon_ok,
            fut_oi = r.fut_oi, fut_oi_chg = r.fut_oi_chg
        FROM _rec r
        WHERE e.symbol = r.symbol AND e.date = r.date AND e.expiry = r.expiry
    """)
    con.unregister("_rec")
    s = con.execute("""
        SELECT count(*), count(*) FILTER (WHERE NOT oi_recon_ok),
               count(*) FILTER (WHERE NOT oi_recon_ok AND expiry_rank = 0)
        FROM exposure_eod WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
          AND oi_recon_ok IS NOT NULL
    """, [d0, d1]).fetchone()
    print(f"  oi_recon: {len(df):,} chains reconciled; exposure_eod rows flagged "
          f"{s[1]}/{s[0]} ({s[2]} rank 0)")
    return len(df)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    ap.add_argument("--bhav", default=os.environ.get("BHAV_DB", r"C:\Users\aniru\od\bhavcopy.duckdb"))
    ap.add_argument("--from", dest="d0")
    ap.add_argument("--to", dest="d1")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    con = duckdb.connect(a.db)
    if a.all or not (a.d0 and a.d1):
        lo, hi = con.execute("SELECT min(CAST(timestamp AS DATE)), max(CAST(timestamp AS DATE)) "
                             "FROM ocdata").fetchone()
        d0, d1 = (str(lo), str(hi)) if a.all else (str(hi), str(hi))
    else:
        d0, d1 = a.d0, a.d1
    run(con, a.bhav, d0, d1)
    con.close()


if __name__ == "__main__":
    main()
