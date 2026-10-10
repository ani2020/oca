"""
em1d.py -- one-session expected move (em_1d), shared by exposure_eod and
exposure_hist. Single source for the features, the rolling calibration scale
and the table updates (recalibrated 2026-10-04; see exposure_core EM1D_*).

    em_1d^2 = k_t * ( w_iv*IV_d^2 + w_r1*r_t^2 + w_rv5*RV5 + w_rv22*RV22 )
    IV_d    = calendar-quoted ATM IV / 100 / sqrt(243)      (per trading day)
    r_t     = TODAY's CA-adjusted log return (known at t's close)
    RV5/22  = mean squared CA-adjusted log return, last 5 / 22 sessions incl. t
    k_t     = trailing 250-session pooled mean of r_{s+1}^2 / sigma_s^2 over
              exposure_hist rank-0 rows, sessions strictly before t, results
              windows excluded (variance scale; em_1d scales by sqrt(k_t))

Returns: stocks from bhavcopy cash_returns_adj (nse_ca.py apply) with the
no-factor corporate-action days dropped; indices from index_eod. No |r| clip:
the old 15% clip removed 8,502 genuine moves for every 507 CA artefacts.

Units: exposure_eod.em_1d in PRICE (x fut_price), exposure_hist.em_1d as a
FRACTION of spot -- each table keeps its existing convention.

CLI (full backfill of both tables, then nightly via the pipelines):
    python oc/em1d.py --db oc.duckdb --bhav bhavcopy.duckdb --all
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import exposure_core as core          # run from oc/ or as a script
except ImportError:                       # pragma: no cover
    from oc import exposure_core as core

DEFAULT_BHAV = os.environ.get("BHAV_DB", r"C:\Users\aniru\od\bhavcopy.duckdb")
INDEX_MAP = {
    "Nifty 50": "NIFTY", "Nifty Bank": "BANKNIFTY",
    "Nifty Financial Services": "FINNIFTY", "Nifty Midcap Select": "MIDCPNIFTY",
    "Nifty Next 50": "NIFTYNXT50",
}


def _attach(con, bhav: str) -> str:
    """Return an alias for bhavcopy.duckdb, reusing an existing attachment (the
    hist builder already attaches it as 'bhav'; some DuckDB versions refuse a
    second attachment of the same file)."""
    want = os.path.normcase(os.path.abspath(bhav))
    for name, path in con.execute(
            "SELECT database_name, path FROM duckdb_databases() WHERE path IS NOT NULL").fetchall():
        if os.path.normcase(os.path.abspath(path)) == want:
            return name
    con.execute(f"ATTACH '{bhav}' AS bh (READ_ONLY)")
    return "bh"


def features_sql(a: str = "bh") -> str:
    """CTE body: (symbol, dt, lr, nlr, ndt, rv5, rv22, n22, hv20) per session.
    lr is the session's own return; nlr the NEXT session's (calibration only)."""
    cases = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in INDEX_MAP.items())
    names = ",".join(f"'{k}'" for k in INDEX_MAP)
    w = core.HV_WINDOW_DAYS
    return f"""
    ra AS (
        SELECT symbol, trade_date AS dt,
               CASE WHEN flagged IS NOT NULL AND flagged <> 'unexplained_extreme'
                    THEN NULL ELSE log_ret END AS lr
        FROM {a}.cash_returns_adj
        UNION ALL
        SELECT CASE index_name {cases} END, trade_date,
               LN(close / LAG(close) OVER (PARTITION BY index_name ORDER BY trade_date))
        FROM {a}.index_eod WHERE close > 0 AND index_name IN ({names})
    ),
    em_f AS (
        SELECT symbol, dt, lr,
               LEAD(lr) OVER w AS nlr, LEAD(dt) OVER w AS ndt,
               AVG(lr*lr)  OVER (w ROWS BETWEEN 4  PRECEDING AND CURRENT ROW) AS rv5,
               AVG(lr*lr)  OVER (w ROWS BETWEEN 21 PRECEDING AND CURRENT ROW) AS rv22,
               COUNT(lr)   OVER (w ROWS BETWEEN 21 PRECEDING AND CURRENT ROW) AS n22,
               100 * SQRT({core.TRADING_DAYS_YEAR})
                   * STDDEV_SAMP(lr) OVER (w ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW) AS hv20,
               COUNT(lr)   OVER (w ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW) AS n20
        FROM ra WHERE symbol IS NOT NULL
        WINDOW w AS (PARTITION BY symbol ORDER BY dt)
    )"""


def _raw_var_sql(iv_col: str) -> str:
    """sigma^2 before the k_t scale, from an IV column in calendar-quoted percent."""
    W = core.EM1D_HAR_W
    ivd2 = f"POWER({iv_col} / 100.0, 2) / {core.TRADING_DAYS_YEAR}"
    return (f"({W['iv2']}*{ivd2} + {W['r1']}*f.lr*f.lr "
            f"+ {W['rv5']}*f.rv5 + {W['rv22']}*f.rv22)")


def refresh_scale(con, a: str = "bh") -> int:
    """Rebuild em1d_scale(date, k_var, n_days) from exposure_hist. Full rebuild:
    a few seconds over six years, and it keeps k_t consistent after any rebuild."""
    con.execute(f"""
        CREATE OR REPLACE TABLE em1d_scale AS
        WITH {features_sql(a).strip()},
        ev AS (SELECT DISTINCT symbol, event_date FROM {a}.corporate_events WHERE is_results),
        rows AS (
            SELECT h.date, f.nlr * f.nlr / {_raw_var_sql('h.atm_iv_cal')} AS q
            FROM exposure_hist h JOIN em_f f ON f.symbol = h.symbol AND f.dt = h.date
            WHERE h.expiry_rank = 0 AND h.confidence <> 'low' AND h.atm_iv_cal > 0
              AND f.lr IS NOT NULL AND f.nlr IS NOT NULL AND f.n22 >= {core.EM1D_MIN_OBS}
              AND NOT EXISTS (SELECT 1 FROM ev WHERE ev.symbol = h.symbol
                              AND ev.event_date BETWEEN h.date - 1 AND f.ndt)
        ),
        daily AS (SELECT date, AVG(q) AS q, COUNT(*) AS n FROM rows GROUP BY date)
        -- k on row s covers sessions up to AND INCLUDING s (q_s uses r_(s+1), known
        -- at s+1's close). It is applied to dates STRICTLY AFTER s (see _k_join),
        -- so session t uses q up to t-1 -- everything known at t's close, no more.
        SELECT date,
               AVG(q)   OVER s AS k_var,
               COUNT(q) OVER s AS n_days
        FROM daily
        WINDOW s AS (ORDER BY date ROWS BETWEEN {core.EM1D_SCALE_WINDOW - 1} PRECEDING AND CURRENT ROW)
    """)
    con.execute(f"DELETE FROM em1d_scale WHERE n_days < {core.EM1D_SCALE_MIN_DAYS}")
    n = con.execute("SELECT count(*) FROM em1d_scale").fetchone()[0]
    last = con.execute("SELECT date, k_var FROM em1d_scale ORDER BY date DESC LIMIT 1").fetchone()
    if last:
        print(f"  em1d_scale: {n} dates, latest {last[0]} k_var={last[1]:.3f} "
              f"(em_1d x {last[1] ** 0.5:.3f})")
    return n


def _k_join(alias: str) -> str:
    """k for the row's date: the latest scale row STRICTLY BEFORE it. Dates beyond
    exposure_hist's last session carry the last known k forward."""
    return (f"ASOF LEFT JOIN em1d_scale k ON k.date < {alias}.date")


def update_eod(con, a: str, date_from: str, date_to: str) -> None:
    """hv20 (CA-adjusted, incl. today) and em_1d (price units) for exposure_eod."""
    con.execute(f"""
        WITH {features_sql(a).strip()},
        x AS (
            SELECT e.symbol, e.date, e.expiry_rank,
                   CASE WHEN f.n20 >= {core.HV_MIN_OBS} THEN f.hv20 END AS hv20,
                   CASE WHEN f.n22 >= {core.EM1D_MIN_OBS} AND e.atm_iv > 0 AND e.fut_price > 0
                             AND f.lr IS NOT NULL AND k.k_var IS NOT NULL
                        THEN e.fut_price * SQRT(k.k_var * {_raw_var_sql('e.atm_iv')})
                   END AS em_1d
            FROM exposure_eod e
            JOIN em_f f ON f.symbol = e.symbol AND f.dt = e.date
            {_k_join('e')}
            WHERE e.date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        )
        UPDATE exposure_eod AS e SET hv20 = x.hv20, em_1d = x.em_1d
        FROM x WHERE x.symbol = e.symbol AND x.date = e.date AND x.expiry_rank = e.expiry_rank
    """, [date_from, date_to])
    got, tot = con.execute("""
        SELECT COUNT(em_1d), COUNT(*) FROM exposure_eod
        WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE) AND atm_iv > 0
    """, [date_from, date_to]).fetchone()
    print(f"  em_1d (eod): {got}/{tot} rows populated")


def update_hist(con, a: str, date_from: str, date_to: str) -> None:
    """em_1d (FRACTION of spot) for exposure_hist, same model. hv20 is left as
    the builder writes it (its own settlement-close definition)."""
    con.execute(f"""
        WITH {features_sql(a).strip()},
        x AS (
            SELECT h.symbol, h.date, h.expiry,
                   CASE WHEN f.n22 >= {core.EM1D_MIN_OBS} AND h.atm_iv_cal > 0
                             AND f.lr IS NOT NULL AND k.k_var IS NOT NULL
                        THEN SQRT(k.k_var * {_raw_var_sql('h.atm_iv_cal')})
                   END AS em_1d
            FROM exposure_hist h
            JOIN em_f f ON f.symbol = h.symbol AND f.dt = h.date
            {_k_join('h')}
            WHERE h.date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
        )
        UPDATE exposure_hist AS h SET em_1d = x.em_1d
        FROM x WHERE x.symbol = h.symbol AND x.date = h.date AND x.expiry = h.expiry
    """, [date_from, date_to])
    got, tot = con.execute("""
        SELECT COUNT(em_1d), COUNT(*) FROM exposure_hist
        WHERE date BETWEEN CAST(? AS DATE) AND CAST(? AS DATE)
    """, [date_from, date_to]).fetchone()
    print(f"  em_1d (hist): {got}/{tot} rows populated")


def run(con, bhav: str, eod_range=None, hist_range=None) -> None:
    """Attach, refresh the scale, update the requested ranges. Never raises:
    a missing sibling DB must not cost the night's exposure build."""
    if not Path(bhav).exists():
        print(f"  em_1d: skipped -- missing {bhav}")
        return
    try:
        a = _attach(con, bhav)
        for t in ("exposure_eod", "exposure_hist"):
            for col in ("hv20", "em_1d"):
                try:
                    con.execute(f"ALTER TABLE {t} ADD COLUMN IF NOT EXISTS {col} DOUBLE")
                except duckdb.Error:
                    pass
        refresh_scale(con, a)
        if eod_range:
            update_eod(con, a, *eod_range)
        if hist_range:
            update_hist(con, a, *hist_range)
    except Exception as exc:  # noqa: BLE001
        print(f"  em_1d: skipped -- {type(exc).__name__}: {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=r"C:\Users\aniru\od\oc.duckdb")
    ap.add_argument("--bhav", default=DEFAULT_BHAV)
    ap.add_argument("--all", action="store_true", help="backfill both tables, full range")
    ap.add_argument("--from", dest="d0", default=None)
    ap.add_argument("--to", dest="d1", default=None)
    args = ap.parse_args()
    con = duckdb.connect(args.db)
    if args.all:
        rng = ("2000-01-01", "2100-01-01")
        run(con, args.bhav, eod_range=rng, hist_range=rng)
    else:
        if not (args.d0 and args.d1):
            ap.error("give --all or both --from and --to")
        run(con, args.bhav, eod_range=(args.d0, args.d1), hist_range=(args.d0, args.d1))
    con.close()


if __name__ == "__main__":
    main()
