"""End-to-end test for wheel_paper on throwaway DuckDB files (never touches real DBs).

Run standalone:  python tests/test_wheel_paper.py
Run under pytest: pytest tests/test_wheel_paper.py

Three sessions on two synthetic stocks:
  D1  entries on the 15:40 snapshot (an older 14:00 snapshot with absurd prices must be ignored),
      filled at the BID; BBB blocked by results within 5 days; whitelist book only sees BBB
  D2  nothing fires; then a forced re-run of D2 must restore and reproduce the same state
  D3  crash: the short put passes 3x entry and is bought back at the ASK
plus: next-day reconciliation of a fill against NSE settle, one daily row per book per day,
and equity = start + realised + unrealised.
"""
import os
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

import duckdb

T = Path(tempfile.mkdtemp(prefix="wheel_paper_test_"))
os.environ.update(OC_DB=str(T / 'oc.duckdb'), BHAV_DB=str(T / 'bhav.duckdb'), WHEEL_DB=str(T / 'wheel.duckdb'),
                  WHEEL_HOLIDAYS=str(T / 'hol.json'))
(T / 'hol.json').write_text('[]')
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
D1, D2, D3 = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7)
EXP = date(2026, 10, 27)          # 22 DTE from D1 -> inside 21..35

# ---- oc.duckdb: universe + chain snapshots
oc = duckdb.connect(os.environ['OC_DB'])
oc.execute("CREATE TABLE index_constituents (index_name VARCHAR, symbol VARCHAR)")
oc.execute("INSERT INTO index_constituents VALUES ('NIFTY','AAA'),('NIFTY','BBB')")
oc.execute("""CREATE TABLE ocdata (timestamp TIMESTAMP, symbol VARCHAR, expiry DATE, strike_price DOUBLE,
  underlying_price DOUBLE, lotsize INTEGER, ce_ltp DOUBLE, ce_bid DOUBLE, ce_ask DOUBLE, ce_volume BIGINT, ce_oi DOUBLE,
  ce_delta DOUBLE, pe_ltp DOUBLE, pe_bid DOUBLE, pe_ask DOUBLE, pe_volume BIGINT, pe_oi DOUBLE, pe_delta DOUBLE)""")


def chain(day, put_px, und=100.0, hhmm=(15, 40)):
    ts = datetime.combine(day, datetime.min.time()).replace(hour=hhmm[0], minute=hhmm[1])
    for sym in ('AAA', 'BBB'):
        for k in (80, 85, 90, 95, 105, 110, 115, 120):
            pp = put_px.get(k, 0.5) if k < 100 else 0.1
            cp = 0.1 if k < 100 else {105: 3.0, 110: 2.0, 115: 1.0, 120: 0.4}[k]
            oc.execute("INSERT INTO ocdata VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [ts, sym, EXP, k, und, 100, cp, cp * 0.95, cp * 1.05, 50, 1000, 0.1,
                        pp, pp * 0.95, pp * 1.05, 50, 1000, -0.1])


chain(D1, {80: 0.6, 85: 1.2, 90: 2.0, 95: 3.5})
chain(D1, {80: 9.9, 85: 9.9, 90: 9.9, 95: 9.9}, hhmm=(14, 0))      # older snapshot must be ignored
chain(D2, {80: 0.6, 85: 1.0, 90: 1.8, 95: 3.0})
chain(D3, {80: 2.5, 85: 4.0, 90: 7.0, 95: 9.0}, und=88.0)           # crash: 80 PE 0.6 -> 2.5 (>3x)
oc.close()

# ---- bhavcopy.duckdb
bh = duckdb.connect(os.environ['BHAV_DB'])
bh.execute("CREATE TABLE span_ingest_log (trade_date DATE, tag VARCHAR, file VARCHAR, created TIMESTAMP, "
           "rows_und INT, rows_contract INT, arrays BOOLEAN, ingested_at TIMESTAMP)")
bh.execute("CREATE TABLE span_und (trade_date DATE, tag VARCHAR, symbol VARCHAR, price DOUBLE, price_scan DOUBLE, "
           "vol_scan DOUBLE, psr_pct DOUBLE)")
bh.execute("CREATE TABLE span_contract (trade_date DATE, tag VARCHAR, symbol VARCHAR, instr VARCHAR, expiry DATE, "
           "strike DOUBLE, price DOUBLE, delta DOUBLE, vol DOUBLE, long_scan DOUBLE, short_scan DOUBLE, ra FLOAT[])")
for day in (date(2026, 10, 1), D1, D2, D3):
    bh.execute("INSERT INTO span_ingest_log VALUES (?, 'i5', 'f', now(), 2, 16, false, now())", [day])
    for sym in ('AAA', 'BBB'):
        bh.execute("INSERT INTO span_und VALUES (?, 'i5', ?, 100, 14, 0.1, 0.14)", [day, sym])
        for k in (80, 85, 90, 95, 105, 110, 115, 120):
            ins = 'PE' if k < 100 else 'CE'
            dl = {80: 0.04, 85: 0.08, 90: 0.15, 95: 0.30, 105: 0.30, 110: 0.15, 115: 0.08, 120: 0.04}[k]
            bh.execute("INSERT INTO span_contract VALUES (?, 'i5', ?, ?, ?, ?, 1, ?, 0.3, 1, 6, NULL)",
                       [day, sym, ins, EXP, k, -dl if ins == 'PE' else dl])
bh.execute("CREATE TABLE cash_eod (trade_date DATE, symbol VARCHAR, series VARCHAR, close DOUBLE)")
for day, c in ((date(2026, 10, 1), 101.0), (D1, 100.0), (D2, 100.0)):
    for sym in ('AAA', 'BBB'):
        bh.execute("INSERT INTO cash_eod VALUES (?, ?, 'EQ', ?)", [day, sym, c])
bh.execute("CREATE TABLE corporate_events (symbol VARCHAR, event_date DATE, is_results BOOLEAN, "
           "event_type VARCHAR, announced_at TIMESTAMP)")
bh.execute("INSERT INTO corporate_events VALUES ('BBB', ?, true, 'RESULTS', ?)",
           [D1 + timedelta(days=3), datetime(2026, 9, 25, 16, 0)])                         # BBB blocked
# both stocks need results history, or the coverage gate treats them as unknown and skips them
bh.execute("INSERT INTO corporate_events VALUES ('AAA', '2026-07-20', true, 'RESULTS', NULL), "
           "('BBB', '2026-07-22', true, 'RESULTS', NULL)")
bh.execute("CREATE TABLE corporate_actions (symbol VARCHAR, ex_date DATE, ca_type VARCHAR)")
bh.execute("CREATE TABLE cash_returns_adj (symbol VARCHAR, trade_date DATE, is_ca BOOLEAN)")
bh.execute("CREATE TABLE options_eod (trade_date DATE, symbol VARCHAR, expiry DATE, strike DOUBLE, "
           "opt_type VARCHAR, settle DOUBLE, close DOUBLE)")
bh.execute("INSERT INTO options_eod VALUES (?, 'AAA', ?, 80, 'PE', 0.62, 0.60)", [D1, EXP])    # settle, close for D1 entry
bh.close()

import wheel_paper as wp  # noqa: E402

assert wp.main(['init', '--book', 'main', '--entry-mode', 'all', '--stop-mult', '3', '--dte-lo', '21',
                '--rom', '0.015', '--delta-cap', '0.20', '--max-new', '4']) == 0
assert wp.main(['init', '--book', 'picks', '--entry-mode', 'all', '--stop-mult', '3', '--dte-lo', '21',
                '--rom', '0.015', '--delta-cap', '0.20', '--symbols', 'BBB']) == 0
for d in (D1, D2):
    assert wp.main(['run', '--date', d.isoformat(), '--no-span']) == 0
con = duckdb.connect(os.environ['WHEEL_DB'])
POS_Q = "SELECT book, symbol, side, strike, round(credit,4), lots, entry_date FROM paper_positions ORDER BY 1,2,3"
pos1 = con.execute(POS_Q).fetchall()
con.close()
assert wp.main(['run', '--date', D2.isoformat(), '--no-span']) == 0           # no --force: skipped
assert wp.main(['run', '--date', D2.isoformat(), '--no-span', '--force']) == 0
con = duckdb.connect(os.environ['WHEEL_DB'])
pos2 = con.execute(POS_Q).fetchall()
con.close()
assert wp.main(['run', '--date', D3.isoformat(), '--no-span']) == 0

con = duckdb.connect(os.environ['WHEEL_DB'])
checks = {
    "PE entered at the BID (0.95 x mid) on the 15:40 snapshot, not 14:00":
        con.execute("SELECT strike, round(credit,4) FROM paper_trades WHERE book='main' AND side='PE'").fetchall()
        == [(80.0, round(0.6 * 0.95, 4))],
    "BBB blocked by results within 5 days (main has no BBB)":
        con.execute("SELECT count(*) FROM paper_trades WHERE book='main' AND symbol='BBB'").fetchone()[0] == 0
        and con.execute("SELECT count(*) FROM paper_positions WHERE book='main' AND symbol='BBB'").fetchone()[0] == 0,
    "whitelist book 'picks' only ever considers BBB -> nothing traded":
        con.execute("SELECT count(*) FROM paper_fills WHERE book='picks'").fetchone()[0] == 0,
    "D3 crash: PE closed by 3x stop at the ASK":
        con.execute("SELECT reason, round(exit_fill,4) FROM paper_trades WHERE book='main' AND side='PE'").fetchall()
        == [('stop_mult', round(2.5 * 1.05, 4))],
    "forced re-run of D2 left state identical": pos1 == pos2,
    "reconcile: D1 sell fill matched to settle 0.62, slip = (0.62-0.57)/0.62":
        abs(con.execute("SELECT slip_vs_settle FROM paper_fills WHERE book='main' AND action='S' AND side='PE'")
            .fetchone()[0] - (0.62 - 0.57) / 0.62) < 1e-9,
    "reconcile: D1 sell fill matched to the bhavcopy close 0.60, slip = (0.60-0.57)/0.60":
        abs(con.execute("SELECT slip_vs_close FROM paper_fills WHERE book='main' AND action='S' AND side='PE'")
            .fetchone()[0] - (0.60 - 0.57) / 0.60) < 1e-9,
    "one daily row per book per day": con.execute("SELECT count(*) FROM paper_daily").fetchone()[0] == 6,
    "accounting: equity = start + realised + unrealised":
        abs(con.execute("SELECT equity FROM paper_daily WHERE book='main' AND date=?", [D3]).fetchone()[0]
            - (5_000_000
               + con.execute("SELECT coalesce(sum(pnl),0) FROM paper_trades WHERE book='main'").fetchone()[0]
               + con.execute("SELECT coalesce(sum((credit-mark)*lot) - sum(entry_cost),0) FROM paper_positions "
                             "WHERE book='main'").fetchone()[0])) < 1e-6,
    "risk layer recorded: stress and its cap on every daily row":
        con.execute("SELECT count(*) FROM paper_daily WHERE stress IS NOT NULL AND stress_cap > 0").fetchone()[0] == 6,
}
con.close()

# ---- liquidity gate on live quotes (plan item 0), straight through build_candidates
import pandas as pd  # noqa: E402
import wheel_bt as wb  # noqa: E402
p = wb.Params(dte_lo=21, dte_hi=35, rom_target=0.01, delta_cap=0.20)
base = dict(symbol="AAA", ts=datetime(2026, 10, 5, 15, 40), expiry=EXP, und=100.0, lot=100, opt_type="PE",
            ltp=1.0, delta=-0.1)
ch = pd.DataFrame([
    dict(base, strike=80.0, bid=0.95, ask=1.05, mid=1.0, volume=50, oi=1000),   # ok: 10% spread
    dict(base, strike=85.0, bid=0.70, ask=1.30, mid=1.0, volume=50, oi=1000),   # 60% spread
    dict(base, strike=90.0, bid=0.95, ask=1.05, mid=1.0, volume=50, oi=20),     # OI 20 < 50
    dict(base, strike=95.0, bid=0.95, ask=1.05, mid=1.0, volume=10, oi=1000)])  # 10 traded < 25
span = {("AAA", "PE", EXP, float(k)): (6.0, -0.04) for k in (80, 85, 90, 95)}
cd = wp.build_candidates(p, D1, ch, span, {"AAA": 100.0})
checks2 = {
    "live gate keeps only the liquid strike (spread, OI and volume filters)":
        len(cd) == 1 and float(cd.iloc[0]["strike"]) == 80.0,
    "ROM measured on the bid, not the mid":
        len(cd) == 1 and abs(cd.iloc[0]["rom"] - 0.95 * 100 / (6 * 100 + 0.035 * 100 * 100)) < 1e-12,
}
checks.update(checks2)

# ---- per-book snapshot rank: a 'pre' book stored with rank 1 trades the 14:00 snapshot in the same run
assert wp.main(['init', '--book', 'pre', '--snap-rank', '1', '--entry-mode', 'all', '--stop-mult', '3', '--dte-lo', '21',
                '--rom', '0.015', '--delta-cap', '0.20', '--max-new', '4']) == 0
assert wp.main(['run', '--date', D1.isoformat(), '--no-span', '--snap-after', '13:00']) == 0
_c = duckdb.connect(os.environ['WHEEL_DB'])
checks.update({
    "book stored with snapshot rank 1": _c.execute("SELECT snap_rank FROM paper_books WHERE book='pre'").fetchone()[0] == 1,
    "rank-1 book filled on the earlier (14:00) snapshot, same run as rank-0 books":
        _c.execute("SELECT hour(snapshot_ts) FROM paper_fills WHERE book='pre' AND action='S' LIMIT 1").fetchone()[0] == 14,
    "rank-0 books untouched by the D1 re-run": _c.execute("SELECT count(*) FROM paper_daily WHERE book='main'").fetchone()[0] == 3,
})
_c.close()
assert wp.main(['init', '--book', 'pre', '--replace', '--entry-mode', 'all']) == 0
_c = duckdb.connect(os.environ['WHEEL_DB'])
checks["--replace without --snap-rank keeps the book's rank"] = \
    _c.execute("SELECT snap_rank FROM paper_books WHERE book='pre'").fetchone()[0] == 1
_c.close()

# ---- 28 Oct review: persisted state, interest across runs, unsupported settings, delta floor
_c = duckdb.connect(os.environ['WHEEL_DB'])
pr = _c.execute("SELECT entry_vol, iv, exit_pending FROM paper_positions WHERE book='pre'").fetchall()
_c.close()
checks["every Pos field step() needs is persisted (entry_vol, stress IV, pending exit)"] = \
    len(pr) > 0 and all(r[0] == 50 and r[1] is not None and r[2] == "" for r in pr)
assert wp.main(['init', '--book', 'yield', '--cash-yield', '0.10', '--entry-mode', 'all', '--dte-lo', '21',
                '--rom', '0.015', '--delta-cap', '0.20']) == 0
assert wp.main(['run', '--book', 'yield', '--date', D2.isoformat(), '--no-span']) == 0
assert wp.main(['run', '--book', 'yield', '--date', D3.isoformat(), '--no-span']) == 0
_c = duckdb.connect(os.environ['WHEEL_DB'])
iy = _c.execute("SELECT interest, last_run FROM paper_state WHERE book='yield'").fetchone()
_c.close()
checks["cash interest accrues across separate runs (last_day restored, interest persisted)"] = \
    iy[0] is not None and iy[0] > 0 and str(iy[1]) == D3.isoformat()
checks["init refuses settings paper cannot apply (IV/HV gate)"] = \
    wp.main(['init', '--book', 'ivhv', '--max-iv-hv', '1.0']) == 1
cd2 = wp.build_candidates(wb.replace(p, min_delta=0.05), D1, ch, span, {"AAA": 100.0})
checks["delta floor applied in paper candidates"] = len(cd2) == 0

# ---- snapshot choice by rank per symbol (--snap-rank)
from datetime import time as _t  # noqa: E402
_c = duckdb.connect(os.environ['OC_DB'], read_only=True)
c0, s0 = wp.load_chain(_c, D1, _t(15, 30))
c1, s1 = wp.load_chain(_c, D1, _t(13, 0), snap_rank=1)
c2, s2 = wp.load_chain(_c, D1, _t(13, 0), snap_rank=2)
_c.close()
px = lambda ch: float(ch[(ch.symbol == "AAA") & (ch.opt_type == "PE") & (ch.strike == 80)]["ltp"].iloc[0])
checks.update({
    "snap rank 0: latest (15:40) snapshot": s0.hour == 15 and px(c0) == 0.6,
    "snap rank 1: the snapshot before it, per symbol (14:00)": s1.hour == 14 and px(c1) == 9.9,
    "snap rank past the last snapshot: nothing": s2 is None,
})
fails = [k for k, v in checks.items() if not v]
for k, v in checks.items():
    print(("  PASS  " if v else "  FAIL  ") + k)
wp.main(['status'])
print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)


def test_all_properties():
    """pytest entry point."""
    assert not fails, f"failed: {fails}"
