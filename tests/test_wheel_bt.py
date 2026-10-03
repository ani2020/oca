"""Property tests for wheel_bt (synthetic data, no DB).

Run standalone:  python tests/test_wheel_bt.py
Run under pytest: pytest tests/test_wheel_bt.py

  1  calm decay: entered, held, closed at final_exit with a profit
  2  accounting identity: final equity = start + sum(trade pnl)
  3  stop_mult fires on settle >= mult x entry; stop_requires_trade ignores untraded days
  4  spot-cross and delta stops
  5  results: no entry within earn_entry_days; held position closes the session before
  6  corporate action inside the life blocks entry
  7  margin cap and sector cap block entries
  8  rank_move: put needs a red day, call a green day; 'all' ignores direction
  9  costs: STT schedule by date; costs reduce pnl
 10  time_exit closes near-the-money positions at E-5, leaves far-OTM ones
 11  pct sizing: lots from capacity, capped by volume and caps; skip if 1 lot too big
"""
import sys
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import wheel_bt as wb  # noqa: E402

fails = []


def check(name, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + (f"   [{extra}]" if extra else ""))
    if not cond:
        fails.append(name)


CAL = [d.date() for d in pd.bdate_range("2024-01-01", "2024-04-30")]
EXP = date(2024, 2, 29)
ENTRY = date(2024, 1, 25)


def path(start=ENTRY, end=EXP, px=lambda i, n: 2.0 * (1 - i / n) + 0.05, vol=lambda i: 10,
         und=lambda i: 100.0, delta=lambda i: 0.08, scan=8.0):
    days = [d for d in CAL if start <= d <= end]
    n = len(days)
    # close deliberately != settle: a stale last-trade print must never drive marks or stops
    return {d: (px(i, n) * 3.0, float(vol(i)), px(i, n), delta(i), scan, und(i)) for i, d in enumerate(days)}


def cand(sym="XYZ", side="PE", strike=90.0, move=-0.01, day=ENTRY, margin=10_000.0, lot=100):
    return dict(trade_date=day, symbol=sym, opt_type=side, raw=sym, expiry=EXP, strike=strike,
                premium=2.0, lot=lot, margin=margin, rom=2.0 * lot / margin, delta=0.08, und=100.0,
                oi=1000, volume=10, move=move)


def data(cands, paths, results=None, ca=None):
    return wb.Data(CAL, pd.DataFrame(cands), paths, results or {}, ca or {}, {"XYZ": "IT", "ABC": "IT"})


BASE = wb.Params(start=CAL[0], end=CAL[-1], stop_mult=None, earn_entry_days=5, slippage=0.0,
                 brokerage=0.0, max_new_per_day=4)
KEY = ("XYZ", "PE", EXP, 90.0)

print("\n=== 1. calm decay ===")
r = wb.simulate(BASE, data([cand()], {KEY: path()}))
t = r.trades
check("one trade", len(t) == 1, str(len(t)))
check("closed by final_exit", t.iloc[0]["reason"] == "final_exit", t.iloc[0]["reason"])
check("profitable", t.iloc[0]["pnl"] > 0, f"{t.iloc[0]['pnl']:.1f}")
left = sum(1 for d in CAL if t.iloc[0]["exit_date"] < d <= EXP)
check("exit at <= 2 trading days left", left <= 2, str(left))

print("\n=== 2. accounting identity ===")
total0 = BASE.cash + BASE.collateral
check("final equity = start + sum(pnl)",
      abs(r.equity["equity"].iloc[-1] - (total0 + t["pnl"].sum())) < 1e-6,
      f"{r.equity['equity'].iloc[-1] - total0:.2f} vs {t['pnl'].sum():.2f}")
check("no open positions at end", r.equity["n_open"].iloc[-1] == 0)

print("\n=== 3. stop_mult on settle price ===")
spike = lambda i, n: 3.5 if i == 5 else 2.0 * (1 - i / n) + 0.05
p15 = replace(BASE, stop_mult=1.5)
r = wb.simulate(p15, data([cand()], {KEY: path(px=spike)}))
check("traded spike fires 1.5x stop", r.trades.iloc[0]["reason"] == "stop_mult", r.trades.iloc[0]["reason"])
check("stop exits at the spike price", abs(r.trades.iloc[0]["exit_px"] - 3.5) < 1e-9)
r = wb.simulate(replace(p15, stop_requires_trade=True),
                data([cand()], {KEY: path(px=spike, vol=lambda i: 0 if i == 5 else 10)}))
check("stop_requires_trade: untraded spike does NOT fire", r.trades.iloc[0]["reason"] != "stop_mult",
      r.trades.iloc[0]["reason"])
r = wb.simulate(p15, data([cand()], {KEY: path(px=spike, vol=lambda i: 0 if i == 5 else 10)}))
check("default: settle spike fires even untraded", r.trades.iloc[0]["reason"] == "stop_mult")
r = wb.simulate(replace(BASE, stop_mult=3.0), data([cand()], {KEY: path(px=spike)}))
check("3x stop ignores a 1.75x spike", r.trades.iloc[0]["reason"] != "stop_mult")

print("\n=== 4. spot-cross and delta stops ===")
crash = lambda i: 85.0 if i >= 6 else 100.0
r = wb.simulate(replace(BASE, stop_spot_cross=True), data([cand()], {KEY: path(und=crash)}))
check("spot through strike fires", r.trades.iloc[0]["reason"] == "stop_spot")
r = wb.simulate(replace(BASE, stop_delta=0.4), data([cand()], {KEY: path(delta=lambda i: 0.5 if i >= 4 else 0.1)}))
check("delta > 0.4 fires", r.trades.iloc[0]["reason"] == "stop_delta")

print("\n=== 5. results rules ===")
soon = {"XYZ": [ENTRY + timedelta(days=4)]}            # within 5 trading days
r = wb.simulate(BASE, data([cand()], {KEY: path()}, results=soon))
check("no entry with results within 5 trading days", len(r.trades) == 0)
later = date(2024, 2, 14)
r = wb.simulate(BASE, data([cand()], {KEY: path()}, results={"XYZ": [later]}))
check("entered when results are later", len(r.trades) == 1)
check("closed for results", r.trades.iloc[0]["reason"] == "results", r.trades.iloc[0]["reason"])
check("closed the session before results", r.trades.iloc[0]["exit_date"] == date(2024, 2, 13),
      str(r.trades.iloc[0]["exit_date"]))

print("\n=== 6. corporate action blackout ===")
r = wb.simulate(BASE, data([cand()], {KEY: path()}, ca={"XYZ": [date(2024, 2, 20)]}))
check("CA ex-date inside life blocks entry", len(r.trades) == 0)
r = wb.simulate(BASE, data([cand()], {KEY: path()}, ca={"XYZ": [date(2024, 3, 20)]}))
check("CA after expiry does not block", len(r.trades) == 1)

print("\n=== 7. margin and sector caps ===")
r = wb.simulate(replace(BASE, util_cap=0.0001), data([cand()], {KEY: path()}))
check("util cap blocks entry", len(r.trades) == 0)
k2 = ("ABC", "PE", EXP, 90.0)
big = 0.5 * 0.30 * 50_00_000 * 0.6          # 60% of the sector allowance each
r = wb.simulate(BASE, data([cand(margin=big), cand(sym="ABC", margin=big)],
                           {KEY: path(), k2: path()}))
check("sector cap admits only one of two same-sector names", len(r.trades) == 1, str(len(r.trades)))

print("\n=== 8. entry direction ===")
r = wb.simulate(BASE, data([cand(move=+0.01)], {KEY: path()}))
check("rank_move: no put on a green day", len(r.trades) == 0)
r = wb.simulate(replace(BASE, entry_mode="all"), data([cand(move=+0.01)], {KEY: path()}))
check("'all': put entered regardless of direction", len(r.trades) == 1)
r = wb.simulate(BASE, data([cand(move=-0.06)], {KEY: path()}))
check("move cap 4% skips a -6% day", len(r.trades) == 0)

print("\n=== 9. costs ===")
check("STT 0.05% pre-Apr-2023", wb.stt_sell(date(2022, 6, 1)) == 0.0005)
check("STT 0.0625% Apr-2023..Sep-2024", wb.stt_sell(date(2024, 1, 1)) == 0.000625)
check("STT 0.1% from Oct-2024", wb.stt_sell(date(2025, 1, 1)) == 0.001)
r0 = wb.simulate(BASE, data([cand()], {KEY: path()}))
r1 = wb.simulate(replace(BASE, slippage=0.10, brokerage=20.0), data([cand()], {KEY: path()}))
check("costs + slippage reduce pnl", r1.trades.iloc[0]["pnl"] < r0.trades.iloc[0]["pnl"],
      f"{r1.trades.iloc[0]['pnl']:.1f} < {r0.trades.iloc[0]['pnl']:.1f}")
check("identity holds with costs",
      abs(r1.equity["equity"].iloc[-1] - (BASE.cash + BASE.collateral + r1.trades["pnl"].sum())) < 1e-6)

print("\n=== 10. time exit ===")
r = wb.simulate(BASE, data([cand()], {KEY: path(delta=lambda i: 0.3)}))
lt = sum(1 for d in CAL if r.trades.iloc[0]["exit_date"] < d <= EXP)
check("near-money closes at E-5 window", r.trades.iloc[0]["reason"] == "time_exit" and lt <= 5, f"{r.trades.iloc[0]['reason']} {lt}")
r = wb.simulate(BASE, data([cand()], {KEY: path(delta=lambda i: 0.05)}))
check("far-OTM rides to final_exit", r.trades.iloc[0]["reason"] == "final_exit")

print("\n=== 11. pct sizing ===")
cap = BASE.cash + BASE.collateral
ps5 = replace(BASE, sizing="pct", trade_margin_pct=0.05, max_vol_frac=1.0)
r = wb.simulate(ps5, data([cand(margin=50_000.0)], {KEY: path()}))
want = int(0.05 * cap // 50_000)
check("lots = floor(5% x capacity / margin per lot)", r.trades.iloc[0]["lots"] == want,
      f"{r.trades.iloc[0]['lots']} vs {want}")
check("quantity = lot size x lots", r.trades.iloc[0]["lot"] == 100 * want)
r = wb.simulate(replace(ps5, max_vol_frac=0.2), data([cand(margin=50_000.0)], {KEY: path()}))
check("volume cap: 20% of 10 contracts -> 2 lots", r.trades.iloc[0]["lots"] == 2, str(r.trades.iloc[0]["lots"]))
r = wb.simulate(ps5, data([cand(margin=0.06 * cap)], {KEY: path()}))
check("one lot over the 5% cap -> skipped", len(r.trades) == 0)
r10 = wb.simulate(replace(ps5, trade_margin_pct=0.10), data([cand(margin=50_000.0)], {KEY: path()}))
r5 = wb.simulate(ps5, data([cand(margin=50_000.0)], {KEY: path()}))
check("10% sizing scales pnl ~2x of 5%", abs(r10.trades.iloc[0]["pnl"] / r5.trades.iloc[0]["pnl"] - 2) < 0.05,
      f"{r10.trades.iloc[0]['pnl'] / r5.trades.iloc[0]['pnl']:.3f}")
r = wb.simulate(replace(ps5, trade_margin_pct=0.10, util_cap=0.06, sector_cap=1.0), data([cand(margin=50_000.0)], {KEY: path()}))
check("util cap shrinks lots to fit", r.trades.iloc[0]["lots"] == int(0.06 * cap // 50_000), str(r.trades.iloc[0]["lots"]))
check("identity holds with sizing",
      abs(r10.equity["equity"].iloc[-1] - (cap + r10.trades["pnl"].sum())) < 1e-6)

print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)


def test_all_properties():
    """pytest entry point."""
    assert not fails, f"failed: {fails}"
