"""wheel_signals: pricing helpers, and the outcome walker against wheel_bt.simulate on the same trade."""
import math, os, sys
from dataclasses import replace
from datetime import date
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np, pandas as pd
import wheel_bt as wb
import wheel_signals as ws

fails = []
def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)

print("\n=== 1. vectorised pricing ===")
xs = np.linspace(-4, 4, 81)
check("normal CDF within 2e-7 of math.erf", max(abs(ws._ncdf(xs) - np.array([wb._ncdf(x) for x in xs]))) < 2e-7)
t = 35 / 365
for side, k in (("PE", 90.0), ("CE", 110.0), ("PE", 100.0)):
    px = wb.bs_price_r(side, 100.0, k, t, 0.32, ws.RATE)
    check(f"bs_vec = wheel_bt.bs_price_r ({side} {k})", abs(ws.bs_vec(side == "CE", 100.0, k, t, 0.32) - px) < 1e-5)
    check(f"iv_vec recovers 32% ({side} {k})", abs(ws.iv_vec([side == "CE"], [px], [100.0], [k], [t])[0] - 0.32) < 1e-4)
check("price below intrinsic -> NaN", np.isnan(ws.iv_vec([False], [5.0], [90.0], [100.0], [t])[0]))

print("\n=== 2. walker matches the backtest's exits ===")
CAL = [d.date() for d in pd.bdate_range("2024-01-01", "2024-04-30")]
EXP, ENTRY = date(2024, 2, 29), date(2024, 1, 25)
def path(px, und=lambda i: 100.0, vol=lambda i: 10):
    days = [d for d in CAL if ENTRY <= d <= EXP]
    n = len(days)
    return {d: (px(i, n) * 1.1, float(vol(i)), px(i, n), 0.08, 8.0, und(i)) for i, d in enumerate(days)}
cand = dict(trade_date=ENTRY, symbol="XYZ", opt_type="PE", raw="XYZ", expiry=EXP, strike=90.0, premium=2.0, lot=100,
            margin=10_000.0, rom=0.02, delta=0.08, und=100.0, oi=1000, volume=50, move=-0.01, svol=0.3)
KEY = ("XYZ", "PE", EXP, 90.0)
P = replace(wb.Params(), start=CAL[0], end=CAL[-1], stop_mult=3.0, stress_cap=None, require_event_coverage=False,
            entry_mode="all", sizing="fixed", min_rich=None)
cases = {
    "decay to final exit": lambda i, n: 2.0 * (1 - i / n) + 0.05,
    "spike -> 3x stop": lambda i, n: 6.5 if i == 6 else 2.0 * (1 - i / n) + 0.05,
}
for name, px in cases.items():
    data = wb.Data(CAL, pd.DataFrame([cand]), {KEY: path(px)}, {}, {}, {"XYZ": "IT"}, {}, {}, {}, {}, {})
    r = wb.simulate(P, data)
    tr = r.trades.iloc[0]
    entry_ok, walk = ws.make_walker(P, data)
    c = pd.DataFrame([cand]).iloc[0]
    kept, pm, reason, days = walk(c)
    credit = 2.0 * (1 - wb.half_spread(P, 50))
    check(f"{name}: same exit reason and day as simulate", reason == tr["reason"] and days == tr["days"],
          f"{reason}/{days} vs {tr['reason']}/{tr['days']}")
    check(f"{name}: same exit fill", abs((credit - kept * 2.0) - tr["exit_fill"]) < 1e-9,
          f"{credit - kept * 2.0:.4f} vs {tr['exit_fill']:.4f}")
data = wb.Data(CAL, pd.DataFrame([cand]), {KEY: path(cases['decay to final exit'])}, {"XYZ": [date(2024, 1, 29)]},
               {}, {"XYZ": "IT"}, {}, {}, {}, {}, {})
entry_ok, walk = ws.make_walker(replace(P, event_scope="results"), data)
check("results within 5 sessions -> entry blocked", not entry_ok("XYZ", ENTRY, EXP))
check("no results -> entry allowed", ws.make_walker(P, wb.Data(CAL, pd.DataFrame([cand]), {}, {}, {}, {}, {}, {}, {}, {}, {}))[0]("XYZ", ENTRY, EXP))

print("\n=== 3. scoring ===")
rng = np.random.default_rng(1)
n = 3000
df = pd.DataFrame({"month": pd.period_range("2020-01", periods=30, freq="M").repeat(100), "sig": rng.normal(size=n)})
df["y"] = 0.5 * df["sig"] + rng.normal(size=n)
s = ws.score(df, "sig", "y")
check("informative signal: IC ~0.45, top quintile beats bottom", 0.35 < s["ic"] < 0.55 and s["q5_q1"] > 0 and s["ic_pos"] == 1.0, str(s))
df["noise"] = rng.normal(size=n)
s = ws.score(df, "noise", "y")
check("noise signal: IC ~0", abs(s["ic"]) < 0.06, str(s))

print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)

def test_all():
    assert not fails, f"failed: {fails}"
