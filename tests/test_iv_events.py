"""iv_events: constant-maturity IV, straddle P&L, and event-window alignment."""
import os, sys
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np, pandas as pd
import iv_events as ie

fails = []
def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)

print("\n=== 1. 30-day constant maturity ===")
atm = pd.DataFrame([dict(d=date(2024, 1, 2), sym="X", dte=20, iv=0.30), dict(d=date(2024, 1, 2), sym="X", dte=50, iv=0.20),
                    dict(d=date(2024, 1, 2), sym="Y", dte=40, iv=0.25)])
c = ie.cm30(atm).set_index("sym")["iv30"]
w = (30 - 20) / (50 - 20)
exp = np.sqrt(((1 - w) * 0.30 ** 2 * 20 + w * 0.20 ** 2 * 50) / 30)
check("two expiries -> total-variance interpolation at 30 days", abs(c["X"] - exp) < 1e-12, f"{c['X']:.5f} vs {exp:.5f}")
check("one expiry -> its IV", abs(c["Y"] - 0.25) < 1e-12)
check("an expiry exactly at 30 days is used as is",
      abs(ie.cm30(pd.DataFrame([dict(d=date(2024, 1, 2), sym="Z", dte=30, iv=0.22)]))["iv30"].iloc[0] - 0.22) < 1e-12)

print("\n=== 2. straddle P&L ===")
check("no move, no IV change -> small gain (2 days of decay)", 0 < ie.straddle_pnl(100.0, 100.0, 0.3, 0.3) < 0.05)
check("IV crush with no move -> bigger gain", ie.straddle_pnl(100.0, 100.0, 0.4, 0.25) > ie.straddle_pnl(100.0, 100.0, 0.3, 0.3))
check("8% move -> loss", ie.straddle_pnl(100.0, 108.0, 0.3, 0.3) < 0)

print("\n=== 3. event windows ===")
cal = [d.date() for d in pd.bdate_range("2024-01-01", "2024-03-29")]
ser = pd.DataFrame([dict(d=d, sym="X", iv30=0.30 + (0.10 if d == cal[24] else 0.0), s=100.0) for d in cal])
ev = pd.DataFrame([dict(sym="X", event_date=date(2024, 2, 3), event_type="BUDGET")])      # a Saturday
st, pa = ie.event_rows(ser, ev, cal)
check("non-trading event day -> day 0 is the next session", st["day0"].iloc[0] == date(2024, 2, 5), str(st["day0"].iloc[0]))
check("IV spike on day -1 (Fri 2 Feb): run-up +33%, crush -25%",
      abs(st["runup"].iloc[0] - 0.40 / 0.30 + 1) < 1e-12 and abs(st["crush"].iloc[0] - (0.30 / 0.40 - 1)) < 1e-12,
      f"{st['runup'].iloc[0]:.3f} / {st['crush'].iloc[0]:.3f}")
check("path has days -10..+10", sorted(pa["k"].unique()) == list(range(-10, 11)))
ev2 = pd.DataFrame([dict(sym="X", event_date=cal[3], event_type="MPC")])
check("event too close to the start is skipped", ie.event_rows(ser, ev2, cal)[0].empty)

print("\n=== 4. corporate actions, review dates, index changes ===")
sk = []
st_ca, _ = ie.event_rows(ser, ev, cal, {"X": [date(2024, 2, 9)]}, sk)
check("event with a corporate action in its window is skipped and recorded", st_ca.empty and sk == [("X", "BUDGET", date(2024, 2, 3))], str(sk))
st_ok, _ = ie.event_rows(ser, ev, cal, {"X": [date(2024, 3, 28)]}, [])
check("corporate action outside the window does not skip", len(st_ok) == 1)
re_ = ie.rule_events(cal)
check("MSCI review = last business day of Feb; NSE review = last trading day of Mar",
      set(map(tuple, re_[["event_date", "event_type"]].values)) == {(date(2024, 2, 29), "MSCI_REVIEW"), (date(2024, 3, 29), "NSE_REVIEW")},
      str(re_[["event_date", "event_type"]].values.tolist()))
import duckdb
db = duckdb.connect()
db.execute("CREATE TABLE index_constituents_hist (index_name VARCHAR, as_of DATE, symbol VARCHAR)")
db.execute("""INSERT INTO index_constituents_hist VALUES
  ('NIFTY','2024-02-29','AAA'), ('NIFTY','2024-02-29','BBB'), ('NIFTY','2024-02-29','ZOMATO'),
  ('NIFTY','2024-03-28','AAA'), ('NIFTY','2024-03-28','CCC'), ('NIFTY','2024-03-28','ETERNAL'), ('NIFTY','2024-03-28','DUMMYX')""")
ic = ie.index_change_events(db, date(2024, 1, 1), date(2024, 12, 31))
got = sorted(map(tuple, ic[["sym", "event_type"]].values))
check("adds / deletes found; rename (ZOMATO->ETERNAL) and DUMMY placeholder ignored",
      got == [("BBB", "INDEX_DEL"), ("CCC", "INDEX_ADD")] and set(ic["event_date"]) == {date(2024, 3, 28)}, str(got))

print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)

def test_all():
    assert not fails, f"failed: {fails}"
