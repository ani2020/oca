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
 12  event gates: board scope, point-in-time announcement, event-history coverage
 13  liquidity: half-spread cost by volume bucket
 14  liquidity: multi-lot volume floor
 15  stress cap: skip (fixed) or shrink (pct) to keep the scenario loss within the cap
 16  market brake: VIX / NIFTY drawdown stops new puts, not calls
 17  index hedge: sizing, roll, P&L, accounting identity
 18  entry ladder
 19  hurdle exits: hard, and soft swap only when an entry is waiting
 20  call trend filter
 21  drawdown sizing, beta in the stress test, asof lookups
 22  defaults (brake off, cap 15%); rank by premium per crash rupee
 23  stress trim: close the worst contributor once stress passes N x cap
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


def data(cands, paths, results=None, ca=None, board=None, known=None, coverage=None, market=None, hedge=None):
    return wb.Data(CAL, pd.DataFrame(cands), paths, results or {}, ca or {}, {"XYZ": "IT", "ABC": "IT"},
                   board or {}, known or {}, coverage or {}, market or {}, hedge or {})


# Tests 1-12 pin the pre-Oct-2026 cost and risk settings (flat slippage, no
# multi-lot volume floor, no stress cap or brake) so they keep testing what
# they were written for; 13+ switch the new layers on one at a time.
BASE = wb.Params(start=CAL[0], end=CAL[-1], stop_mult=None, earn_entry_days=5, slippage=0.0,
                 brokerage=0.0, max_new_per_day=4, cost_model="flat", multi_lot_min_volume=0,
                 stress_cap=None, brake_vix_level=None, brake_vix_jump=None, brake_nifty_dd=None,
                 price_basis="settle", iv_source="implied")
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

print("\n=== 12. event gates ===")
later = date(2024, 2, 14)
brd = {"XYZ": [later]}                                # a 'Dividend' board meeting, not labelled results
r = wb.simulate(BASE, data([cand()], {KEY: path()}, board=brd, results={"ABC": [later]}))
check("board scope: unlabelled board meeting closes the position", r.trades.iloc[0]["reason"] == "results",
      r.trades.iloc[0]["reason"])
r = wb.simulate(replace(BASE, event_scope="results"), data([cand()], {KEY: path()}, board=brd,
                                                           results={"ABC": [later]}))
check("results scope ignores it", r.trades.iloc[0]["reason"] != "results", r.trades.iloc[0]["reason"])
soon = ENTRY + timedelta(days=4)
kn = {("XYZ", soon): ENTRY + timedelta(days=1)}       # announced the day after entry
r = wb.simulate(BASE, data([cand()], {KEY: path()}, results={"XYZ": [soon]}, known=kn))
check("pit: unannounced results do not block entry", len(r.trades) == 1, str(len(r.trades)))
check("pit: still closed the session before results once announced",
      len(r.trades) == 1 and r.trades.iloc[0]["reason"] == "results"
      and r.trades.iloc[0]["exit_date"] == date(2024, 1, 26), str(r.trades.iloc[0]["exit_date"]) if len(r.trades) else "")
r = wb.simulate(replace(BASE, pit_events=False), data([cand()], {KEY: path()}, results={"XYZ": [soon]}, known=kn))
check("no-pit: the same results block entry (look-ahead)", len(r.trades) == 0)
r = wb.simulate(BASE, data([cand()], {KEY: path()}, coverage={"XYZ": ENTRY + timedelta(days=200)}))
check("coverage: no entry before the symbol's event history", len(r.trades) == 0)
r = wb.simulate(BASE, data([cand()], {KEY: path()}, coverage={"XYZ": ENTRY - timedelta(days=10)}))
check("coverage: entry once history exists", len(r.trades) == 1)
r = wb.simulate(BASE, data([cand()], {KEY: path()}, coverage={"ABC": ENTRY}))
check("coverage: symbol with no history at all is skipped", len(r.trades) == 0)

print("\n=== 13. liquidity: spread costs by volume bucket ===")
sp = replace(BASE, cost_model="spread")
check("half-spread buckets", [wb.half_spread(sp, v) for v in (5, 10, 30, 200, 900)] == [0.535, 0.17, 0.095, 0.045, 0.02])
check("flat model ignores volume", wb.half_spread(BASE, 5) == BASE.slippage)
r = wb.simulate(sp, data([cand()], {KEY: path()}))                 # 10 contracts -> 17% each side
t = r.trades.iloc[0]
check("entry credit = premium x (1 - 17%)", abs(t["credit"] - 2.0 * 0.83) < 1e-9, f"{t['credit']:.4f}")
check("exit fill = mark x (1 + 17%)", abs(t["exit_fill"] - t["exit_px"] * 1.17) < 1e-9)
r5 = wb.simulate(sp, data([dict(cand(), volume=5)], {KEY: path()}))
check("thinner strike pays more (5 contracts -> 53.5%)", r5.trades.iloc[0]["pnl"] < t["pnl"])
check("identity holds with spread costs",
      abs(r.equity["equity"].iloc[-1] - (BASE.cash + BASE.collateral + r.trades["pnl"].sum())) < 1e-6)

print("\n=== 14. liquidity: multi-lot floor ===")
pl = replace(BASE, sizing="pct", trade_margin_pct=0.05, max_vol_frac=1.0, multi_lot_min_volume=100)
r = wb.simulate(pl, data([cand(margin=50_000.0)], {KEY: path()}))
check("below 100 contracts traded -> 1 lot", r.trades.iloc[0]["lots"] == 1, str(r.trades.iloc[0]["lots"]))
r = wb.simulate(pl, data([dict(cand(margin=50_000.0), volume=150)], {KEY: path()}))
check("150 contracts traded -> sized normally (5 lots)", r.trades.iloc[0]["lots"] == 5, str(r.trades.iloc[0]["lots"]))

print("\n=== 15. stress cap ===")
t35 = (EXP - ENTRY).days / 365.0
w1 = max(wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 1.0))   # worst scenario loss, 1 lot
check("a -20% / IV x2 scenario loses far more than the credit", w1 > 5 * 2.0 * 100, f"{w1:.0f}")
r = wb.simulate(replace(BASE, stress_cap=0.5 * w1 / BASE.cash), data([cand()], {KEY: path()}))
check("fixed sizing: a lot over the stress cap is skipped", len(r.trades) == 0)
ps_ = replace(BASE, sizing="pct", trade_margin_pct=0.05, max_vol_frac=1.0, stress_cap=3.5 * w1 / BASE.cash)
r = wb.simulate(ps_, data([cand(margin=50_000.0)], {KEY: path()}))
check("pct sizing: lots shrink to fit the stress cap (5 -> 3)", r.trades.iloc[0]["lots"] == 3, str(r.trades.iloc[0]["lots"]))
check("stress recorded on the equity row", r.equity["stress"].max() > 0)

print("\n=== 16. market brake ===")
KC = ("XYZ", "CE", EXP, 110.0)
call = dict(cand(side="CE", strike=110.0, move=0.01), delta=0.08)
mk = {"daily": {ENTRY: {"vix": 31.0, "vix_jump": 0.0, "nifty_dd": 0.0}}}
pb = replace(BASE, entry_mode="all", brake_vix_level=25.0)
r = wb.simulate(pb, data([cand(), call], {KEY: path(), KC: path()}, market=mk))
check("VIX 31 > 25: no new put, call still entered", list(r.trades["side"]) == ["CE"], str(list(r.trades["side"])))
mk2 = {"daily": {ENTRY: {"vix": 18.0, "vix_jump": 0.0, "nifty_dd": -0.07}}}
r = wb.simulate(replace(pb, brake_nifty_dd=0.05), data([cand()], {KEY: path()}, market=mk2))
check("NIFTY 7% off its 10-day high: no new put", len(r.trades) == 0)
r = wb.simulate(pb, data([cand()], {KEY: path()}, market={"daily": {ENTRY: {"vix": 18.0}}}))
check("calm market: put entered", len(r.trades) == 1)

print("\n=== 17. index hedge ===")
HEXP = date(2024, 3, 28)
hdays = [d for d in CAL if d <= HEXP]
hc = {"px": {(HEXP, 9000.0): {d: (50.0 if d < date(2024, 2, 15) else 200.0) for d in hdays}},
      "und": {d: 10000.0 for d in hdays}}
ph = replace(BASE, hedge=True, hedge_ratio=0.5, hedge_slip=0.03)
r = wb.simulate(ph, data([cand()], {KEY: path()}, hedge=hc))
hed = r.trades[r.trades["side"] == "HEDGE"]
check("hedge bought once the put is on, sized 0.5 x notional / NIFTY",
      len(hed) == 1 and abs(hed.iloc[0]["lot"] - 0.5 * 100 * 100 / 10000) < 1e-9, str(hed[["lot"]].values.tolist()))
check("hedge rolled out before its expiry and not rebought (no short puts left)",
      len(hed) == 1 and hed.iloc[0]["reason"] == "hedge_roll")
check("hedge P&L = (200 x 0.97 - 50 x 1.03) x qty - charges",
      abs(hed.iloc[0]["pnl"] - ((194.0 - 51.5) * 0.5 - hed.iloc[0]["costs"])) < 1e-6
      and 0 < hed.iloc[0]["costs"] < 1, f"{hed.iloc[0]['pnl']:.4f} costs {hed.iloc[0]['costs']:.4f}")
check("identity holds with the hedge",
      abs(r.equity["equity"].iloc[-1] - (BASE.cash + BASE.collateral + r.trades["pnl"].sum())) < 1e-6)
check("hedge value carried in equity while held", r.equity["hedge_value"].max() > 0)

print("\n=== 18. entry ladder ===")
J26, J29 = date(2024, 1, 26), date(2024, 1, 29)
lad = [cand(), cand(day=J26), cand(day=J29)]
r = wb.simulate(replace(BASE, ladder_entries=2, ladder_min_gap_days=2), data(lad, {KEY: path()}))
check("two entries, the second >= 2 trading days after the first",
      sorted(r.trades["entry_date"]) == [ENTRY, J29], str(sorted(r.trades["entry_date"])))
r = wb.simulate(BASE, data(lad, {KEY: path()}))
check("no ladder: one entry per stock-side", len(r.trades) == 1)
r = wb.simulate(replace(BASE, sizing="pct", trade_margin_pct=0.05, max_vol_frac=1.0, ladder_entries=2),
                data([cand(margin=50_000.0)], {KEY: path()}))
check("ladder splits the per-trade budget (5 lots -> 2 per entry)", r.trades.iloc[0]["lots"] == 2,
      str(r.trades.iloc[0]["lots"]))

print("\n=== 19. hurdle exits ===")
# margin ~Rs 20k (entry yield ~10% a year); a favourable move leaves Rs 0.10 with ~3 weeks to go
hp = path(scan=200.0, px=lambda i, n: 2.0 if i < 5 else 0.10)
r = wb.simulate(replace(BASE, exit_hurdle=0.08), data([cand()], {KEY: hp}))
t = r.trades.iloc[0]
check("hard hurdle closes a decayed position mid-life", t["reason"] == "hurdle", t["reason"])
ry = t["exit_px"] * 100 / t["margin"] * 365 / max((EXP - t["exit_date"]).days, 1)
check("... once its remaining yield is below 8%", ry < 0.08, f"{ry:.3f}")
r0 = wb.simulate(BASE, data([cand()], {KEY: hp}))
check("... earlier than the final exit would have", t["exit_date"] < r0.trades.iloc[0]["exit_date"])
F12 = date(2024, 2, 12)
KB = ("ABC", "PE", EXP, 90.0)
big = 600_000.0                                    # sector room (Rs 7.5L) fits exactly one
sw = replace(BASE, hurdle_swap=0.5)
r = wb.simulate(sw, data([cand(margin=big), cand(sym="ABC", day=F12, margin=big)],
                         {KEY: path(scan=6000.0), KB: path(start=F12, scan=6000.0)}))
x = r.trades.set_index("symbol")
check("soft hurdle: tired XYZ swapped out on the day ABC was waiting",
      x.loc["XYZ", "reason"] == "swap" and x.loc["XYZ", "exit_date"] == F12, str(x.loc["XYZ", ["reason", "exit_date"]].tolist()))
check("... and ABC entered that day", x.loc["ABC", "entry_date"] == F12)
r = wb.simulate(BASE, data([cand(margin=big), cand(sym="ABC", day=F12, margin=big)],
                           {KEY: path(scan=6000.0), KB: path(start=F12, scan=6000.0)}))
check("without the soft hurdle ABC is simply refused", list(r.trades["symbol"]) == ["XYZ"])
check("identity holds with swaps",
      abs(r.equity["equity"].iloc[-1] - (BASE.cash + BASE.collateral + r.trades["pnl"].sum())) < 1e-6)

print("\n=== 20. call trend filter ===")
tr = {"trend": {"XYZ": ([ENTRY], [(0.12, 0.02)])}}         # 12% above the 50-day average
pa = replace(BASE, entry_mode="all")
r = wb.simulate(replace(pa, call_trend_dist=0.10), data([cand(), call], {KEY: path(), KC: path()}, market=tr))
check("call 12% above its 50-day average blocked at 10%; put unaffected", list(r.trades["side"]) == ["PE"],
      str(list(r.trades["side"])))
r = wb.simulate(replace(pa, call_trend_dist=0.15), data([call], {KC: path()}, market=tr))
check("... allowed at 15%", len(r.trades) == 1)
r = wb.simulate(replace(pa, call_trend_ret=0.01), data([call], {KC: path()}, market=tr))
check("20-session return 2% > 1% blocks the call", len(r.trades) == 0)

print("\n=== 21. drawdown sizing + beta ===")
pd_ = replace(BASE, sizing="pct", trade_margin_pct=0.05, max_vol_frac=1.0, dd_halve_at=0.10)
ctx = wb.Ctx(CAL, {}, {}, {"XYZ": "IT"})
c1 = pd.DataFrame([cand(margin=50_000.0)])
st = wb.State(cash=pd_.cash, peak=pd_.cash + pd_.collateral + 0.2 * pd_.cash)    # 20% of cash under peak
_, op, _ = wb.step(pd_, st, ENTRY, ctx, lambda k: None, c1)
check("in a 20% drawdown, sizing halves (5 -> 2 lots)", op and op[0].lots == 2, str(op[0].lots if op else None))
st = wb.State(cash=pd_.cash)
_, op, _ = wb.step(pd_, st, ENTRY, ctx, lambda k: None, c1)
check("no drawdown: full size (5 lots)", op and op[0].lots == 5)
lo = max(wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 0.5))
hi = max(wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 2.0))
check("higher beta -> larger stress loss", hi > w1 > lo, f"{lo:.0f} < {w1:.0f} < {hi:.0f}")
check("asof: latest value on or before the day", wb.asof(([ENTRY, J29], [1.1, 1.3]), J26) == 1.1
      and wb.asof(([ENTRY], [1.1]), ENTRY - timedelta(days=1)) is None)

print("\n=== 22. defaults and stress ranking ===")
check("brake off by default; stress cap 15% on", wb.Params().brake_vix_level is None
      and wb.Params().brake_nifty_dd is None and wb.Params().stress_cap == 0.15)
# A: richer ROM on a high-beta stock.  B: lower ROM on a low-beta stock, so less crash loss per rupee.
KA, KB2 = ("XYZ", "PE", EXP, 97.0), ("ABC", "PE", EXP, 90.0)
ca = dict(cand(strike=97.0), premium=4.0, rom=0.04)
cb = dict(cand(sym="ABC", strike=90.0), premium=2.0, rom=0.02)
betas = {"beta": {"XYZ": ([ENTRY], [2.0]), "ABC": ([ENTRY], [0.5])}}
def worst(c, b):
    return max(wb.scenario_losses(BASE, "PE", c["premium"], 100.0, c["strike"], t35, 100, b))
ra, rb = ca["premium"] * 100 / worst(ca, 2.0), cb["premium"] * 100 / worst(cb, 0.5)
check("fixture: the low-beta put buys more premium per crash rupee", rb > ra, f"{rb:.3f} vs {ra:.3f}")
one = replace(BASE, entry_mode="all", max_new_per_day=1)
r = wb.simulate(one, data([ca, cb], {KA: path(), KB2: path()}, market=betas))
check("rank by ROM takes the richer ROM (XYZ)", list(r.trades["symbol"]) == ["XYZ"])
r = wb.simulate(replace(one, rank_by="stress"), data([ca, cb], {KA: path(), KB2: path()}, market=betas))
check("rank by stress takes the low-beta put (ABC)", list(r.trades["symbol"]) == ["ABC"], str(list(r.trades["symbol"])))

print("\n=== 23. stress trim ===")
w_e = max(wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 1.0))
cap = 1.02 * w_e / BASE.cash                     # entry fits just inside the cap
slide = path(und=lambda i: 100.0 if i < 3 else 88.0, px=lambda i, n: 2.0 if i < 3 else 9.5)
pt = replace(BASE, stress_cap=cap, stress_trim_at=1.15)
r = wb.simulate(pt, data([cand()], {KEY: slide}))
check("stock falls 12%: stress past 1.15x cap -> trimmed", r.trades.iloc[0]["reason"] == "stress_trim",
      r.trades.iloc[0]["reason"])
check("... on the first day it crossed", r.trades.iloc[0]["exit_date"] == [d for d in CAL if d >= ENTRY][3],
      str(r.trades.iloc[0]["exit_date"]))
r = wb.simulate(replace(pt, stress_trim_at=None), data([cand()], {KEY: slide}))
check("without trim the position rides on", r.trades.iloc[0]["reason"] != "stress_trim")
r = wb.simulate(replace(pt, stress_trim_at=10.0), data([cand()], {KEY: slide}))
check("trim threshold not reached -> no trim", r.trades.iloc[0]["reason"] != "stress_trim")
check("identity holds with trims",
      abs(wb.simulate(pt, data([cand()], {KEY: slide})).equity["equity"].iloc[-1]
          - (BASE.cash + BASE.collateral + wb.simulate(pt, data([cand()], {KEY: slide})).trades["pnl"].sum())) < 1e-6)

print("\n=== 24. single-stock shock scenarios ===")
I20 = ((0.20, 1.3),)
cv_sys = max(wb.scenario_losses(BASE, "CE", 2.0, 100.0, 105.0, t35, 100, 0.5))
cv_idio = max(wb.scenario_losses(BASE, "CE", 2.0, 100.0, 105.0, t35, 100, 1.0, shocks=I20))
pv_idio = max(wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 1.0, shocks=I20))
check("low-beta call: one-name +20% gap costs far more than the index set", cv_idio > 2 * cv_sys,
      f"{cv_idio:.0f} vs {cv_sys:.0f}")
check("a +20% gap does not hurt a short put", pv_idio <= 0, f"{pv_idio:.0f}")
KC, KP = ("XYZ", "CE", EXP, 105.0), ("ABC", "PE", EXP, 90.0)
cc = dict(cand(side="CE", strike=105.0, move=0.01), premium=2.0)
cp = dict(cand(sym="ABC", strike=90.0), premium=2.0)
lowb = {"beta": {"XYZ": ([ENTRY], [0.5]), "ABC": ([ENTRY], [0.5])}}
rs = replace(BASE, entry_mode="all", max_new_per_day=1, rank_by="stress", stress_cap=1.0)
r = wb.simulate(rs, data([cc, cp], {KC: path(), KP: path()}, market=lowb))
check("index scenarios only: stress ranking prefers the low-beta call", list(r.trades["symbol"]) == ["XYZ"],
      str(list(r.trades["symbol"])))
r = wb.simulate(replace(rs, stress_idio=I20), data([cc, cp], {KC: path(), KP: path()}, market=lowb))
check("with the one-name gap: it takes the put instead", list(r.trades["symbol"]) == ["ABC"],
      str(list(r.trades["symbol"])))
check("a lone short put has no single-name gap loss", r.equity["stress_idio"].max() == 0)
fx = replace(BASE, stress_cap=1.0, stress_idio=I20)
r = wb.simulate(replace(fx, stress_idio_cap=0.9 * cv_idio / BASE.cash), data([cc], {KC: path()}, market=lowb))
check("name cap below one lot's gap loss -> call not entered", r.trades.empty)
r = wb.simulate(replace(fx, stress_idio_cap=1.1 * cv_idio / BASE.cash), data([cc], {KC: path()}, market=lowb))
check("name cap above it -> entered", len(r.trades) == 1)
e_in = r.equity.set_index("date")["stress_idio"].get(ENTRY)
check("equity records the call's gap loss on entry day", e_in is not None and abs(e_in - cv_idio) < 1
      and r.metrics.get("max_idio_pct_cash", 0) > 0, f"{e_in}")
pj = wb.params_from_json(wb.params_to_json(replace(BASE, stress_idio=((0.2, 1.3), (-0.2, 1.3)))))
check("stress_idio survives the paper-book JSON round trip", pj.stress_idio == ((0.2, 1.3), (-0.2, 1.3)))
check("off by default", wb.Params().stress_idio == ())

print("\n=== 25. point-in-time universe ===")
import duckdb
ux = duckdb.connect()
ux.execute("CREATE TABLE index_constituents (index_name VARCHAR, symbol VARCHAR)")
ux.execute("INSERT INTO index_constituents VALUES ('NIFTY','AAA'), ('NIFTY','ETERNAL')")
ux.execute("CREATE TABLE opts (symbol VARCHAR, trade_date DATE)")
ux.execute("""INSERT INTO opts VALUES ('AAA','2022-03-31'), ('AAA','2022-04-01'), ('OLD','2022-03-31'),
              ('OLD','2022-04-01'), ('ZOMATO','2022-04-29'), ('ZOMATO','2022-05-02'), ('XXX','2022-04-01')""")
def members(universe):
    ctes, pred = wb.universe_ctes(ux, universe)
    return sorted(ux.execute(f"WITH {ctes} x AS (SELECT 1) SELECT u.canon, o.trade_date FROM opts o "
                             f"JOIN u ON o.symbol = u.raw WHERE {pred}").fetchall())
cur = members("pit")                                  # no hist table yet -> falls back to today's list
check("no history table: falls back to today's list (+ old names via RENAMES)",
      [c for c, _ in cur] == ["AAA", "AAA", "ETERNAL", "ETERNAL"], str(cur))
ux.execute("CREATE TABLE index_constituents_hist (index_name VARCHAR, as_of DATE, symbol VARCHAR)")
ux.execute("""INSERT INTO index_constituents_hist VALUES
    ('NIFTY','2022-02-28','OLD'), ('NIFTY','2022-02-28','AAA'),
    ('NIFTY','2022-03-31','AAA'), ('NIFTY','2022-03-31','ZOMATO'),
    ('BANKNIFTY','2022-03-31','AAA'),
    ('NIFTY','2022-04-29','AAA')""")
m = members("pit")
check("OLD trades on 31 Mar (Feb list still applies) but not from 1 Apr",
      (("OLD", date(2022, 3, 31)) in [(c, d) for c, d in m]) and ("OLD", date(2022, 4, 1)) not in m, str(m))
check("a symbol in two indices is not duplicated", m.count(("AAA", date(2022, 4, 1))) == 1, str(m))
check("ZOMATO (Mar list) trades 29 Apr as ETERNAL, not after the Apr list drops it",
      ("ETERNAL", date(2022, 4, 29)) in m and ("ETERNAL", date(2022, 5, 2)) not in m, str(m))
check("never-member XXX excluded", all(c != "XXX" for c, _ in m))
check("current universe ignores history", [c for c, _ in members("current")] == ["AAA", "AAA", "ETERNAL", "ETERNAL"])
check("beta/trend symbol list covers past members by today's name",
      {r[0] for r in ux.execute(wb.universe_symbols_sql(ux)).fetchall()} == {"AAA", "ETERNAL", "OLD"})
check("universe is part of the data key", wb.data_key(replace(BASE, universe="current")) != wb.data_key(BASE))

print("\n=== 26. stress IV source and fail-closed losses ===")
P = wb.Params()
iv_impl = wb.implied_vol("PE", 2.0, 100.0, 90.0, t35)
check("SPAN vol used when sane", wb.stress_iv(P, "PE", 2.0, 100.0, 90.0, t35, 0.33) == 0.33)
check("no SPAN vol -> price-implied", abs(wb.stress_iv(P, "PE", 2.0, 100.0, 90.0, t35) - iv_impl) < 1e-9)
check("absurd SPAN vol (5.0) -> price-implied", abs(wb.stress_iv(P, "PE", 2.0, 100.0, 90.0, t35, 5.0) - iv_impl) < 1e-9)
check("price at/below intrinsic -> last IV, not 40%",
      wb.stress_iv(P, "PE", 9.0, 90.0, 100.0, t35, NAN_ := float("nan"), 0.27) == 0.27)
check("... and 40% when there is no last IV", wb.stress_iv(P, "PE", 9.0, 90.0, 100.0, t35) == 0.40)
check("implied price at the 500% ceiling -> last IV",
      wb.stress_iv(P, "PE", 60.0, 100.0, 90.0, t35, NAN_, 0.31) == 0.31)
check("iv_source='implied' keeps the old method", abs(wb.stress_iv(replace(P, iv_source="implied"),
      "PE", 2.0, 100.0, 90.0, t35, 0.33) - iv_impl) < 1e-9)
a = wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 1.0)
b = wb.scenario_losses(BASE, "PE", 2.0, 100.0, 90.0, t35, 100, 1.0, iv=iv_impl)
check("passing the IV gives the same losses as backing it out", max(abs(x - y) for x, y in zip(a, b)) < 1e-6)
n0 = wb.STRESS_NONFINITE[0]
bad = wb.scenario_losses(BASE, "PE", float("nan"), 100.0, 90.0, t35, 100, 1.0)
check("NaN mark -> every loss = full notional (fails closed), counted",
      all(x == 100 * 90.0 for x in bad) and wb.STRESS_NONFINITE[0] == n0 + len(bad), str(bad))
# a position whose mark/und are missing still counts toward book stress
pst = replace(BASE, stress_cap=0.15)
st = wb.State(cash=pst.cash)
st.book.append(wb.Pos(1, "XYZ", "XYZ", "PE", EXP, 90.0, 100, ENTRY, 2.0, 2.0, 0.0, 10_000.0, "IT"))
n0 = wb.STRESS_NONFINITE[0]
_, _, eq = wb.step(pst, st, ENTRY, wb.Ctx(CAL, {}, {}, {"XYZ": "IT"}), lambda k: None, None)
check("position with no mark/und priced from entry, not dropped", eq["stress"] > 0 and wb.STRESS_NONFINITE[0] > n0,
      f"{eq['stress']:.0f}")
# SPAN vol travels from the path row to the position and drives its stress
hi_vol = path()
hi_vol = {d: r[:6] + (float("nan"), 0.90) for d, r in hi_vol.items()}
lo_vol = {d: r[:6] + (float("nan"), 0.20) for d, r in path().items()}
ps_ = replace(BASE, stress_cap=1.0, iv_source="span")
rh = wb.simulate(ps_, data([dict(cand(), svol=0.90)], {KEY: hi_vol}))
rl = wb.simulate(ps_, data([dict(cand(), svol=0.20)], {KEY: lo_vol}))
e_h = rh.equity.set_index("date")["stress"].get(ENTRY); e_l = rl.equity.set_index("date")["stress"].get(ENTRY)
check("higher SPAN vol -> higher stress", e_h > e_l, f"{e_h:.0f} vs {e_l:.0f}")

print("\n=== 27. fills at the traded close ===")
check("close basis is the default", wb.Params().price_basis == "close")
pc = replace(BASE, stop_mult=1.5, price_basis="close")
r = wb.simulate(pc, data([cand()], {KEY: path(px=spike)}))          # fixture: close = 3 x settle
tr0 = r.trades.iloc[0]
check("stop still fires on the settle/SPAN mark", tr0["reason"] == "stop_mult")
check("... but fills at that day's traded close", abs(tr0["exit_px"] - 3.5 * 3.0) < 1e-9, str(tr0["exit_px"]))
untr = path(px=spike, vol=lambda i: 0 if i == 5 else 10)
r = wb.simulate(pc, data([cand()], {KEY: untr}))
tr0 = r.trades.iloc[0]
check("untraded stop day: mark + untraded slippage (no close to use)",
      abs(tr0["exit_px"] - 3.5) < 1e-9 and abs(tr0["exit_fill"] - 3.5 * (1 + pc.slippage + pc.untraded_slip)) < 1e-9,
      f"{tr0['exit_px']} {tr0['exit_fill']}")
short = replace(BASE, price_basis="close", end=CAL[CAL.index(ENTRY) + 5])
r = wb.simulate(short, data([cand()], {KEY: path()}))
tr0 = r.trades.iloc[0]
d_end = short.end
check("window-end close uses the traded close too", tr0["reason"] == "window_end"
      and abs(tr0["exit_px"] - path()[d_end][0]) < 1e-9, f"{tr0['exit_px']} vs {path()[d_end][0]}")
check("identity holds on the close basis",
      abs(r.equity["equity"].iloc[-1] - (BASE.cash + BASE.collateral + r.trades["pnl"].sum())) < 1e-6)
check("price basis is part of the data key", wb.data_key(replace(BASE, price_basis="close")) != wb.data_key(BASE))

print("\n=== 28. defective-run flags ===")
import tempfile, os as _os, json as _json
check("legacy params (no basis / universe / pit_ca keys) -> all three flags",
      wb.run_flags({}) == "settle_fills,survivorship,ca_lookahead")
check("current defaults -> clean", wb.run_flags(_json.loads(_json.dumps(wb.asdict(wb.Params()), default=str))) is None)
check("explicit settle basis is flagged", wb.run_flags({"price_basis": "settle", "universe": "pit", "pit_ca": True}) == "settle_fills")
check("pre-28-Oct CA look-ahead is flagged", wb.run_flags({"price_basis": "close", "universe": "pit"}) == "ca_lookahead")
tmpdb = Path(tempfile.mkdtemp()) / "w.duckdb"
r_ok = wb.simulate(replace(BASE, price_basis="close", universe="pit"), data([cand()], {KEY: path()}))
r_bad = wb.simulate(BASE, data([cand()], {KEY: path()}))
id_ok, id_bad = wb.save(r_ok, "ok", tmpdb), wb.save(r_bad, "bad", tmpdb)
fx = duckdb.connect(str(tmpdb))
fx.execute("UPDATE runs SET flags = NULL")                         # as if saved before flags existed
fx.close()
wb.flag_runs(tmpdb)
fx = duckdb.connect(str(tmpdb), read_only=True)
got = dict(fx.execute("SELECT run_id, flags FROM runs").fetchall()); fx.close()
check("flag mode marks the old-basis run and leaves the clean one", got[id_ok] is None and got[id_bad] == "settle_fills",
      str(got))
check("report labels a flagged run", "FLAGGED" in wb.load_run(id_bad, tmpdb)[1] and "FLAGGED" not in wb.load_run(id_ok, tmpdb)[1])

print("\n=== 29. IV-rich entry gate ===")
m_pe = wb.bs_price_r("PE", 100.0, 90.0, t35, 0.30, 0.065)
check("premium = model price -> rich 1.0", abs(wb.richness("PE", m_pe, 100.0, 90.0, t35, 0.30) - 1.0) < 1e-9)
check("rate 0 reduces to the plain model", abs(wb.bs_price_r("CE", 100, 110, t35, 0.3, 0.0) - wb.bs_price("CE", 100, 110, t35, 0.3)) < 1e-12)
check("with a rate an OTM call's model price rises (forward), so it looks less rich",
      wb.richness("CE", 1.0, 100, 110, t35, 0.3, 0.065) < wb.richness("CE", 1.0, 100, 110, t35, 0.3, 0.0))
check("no SPAN vol -> NaN", wb.richness("PE", 2.0, 100, 90, t35, float("nan")) != wb.richness("PE", 2.0, 100, 90, t35, float("nan")))
m90 = wb.bs_price_r("PE", 100.0, 90.0, t35, 0.30, 0.065)
rich_c = dict(cand(), premium=2.0 * m90, svol=0.30)        # rich 2.0
cheap_c = dict(cand(), premium=0.7 * m90, svol=0.30)       # rich 0.7
none_c = dict(cand(), premium=2.0 * m90)                   # no SPAN vol
pg = replace(BASE, min_rich=1.25)
r = wb.simulate(pg, data([rich_c], {KEY: path()}))
check("rich option (2.0) entered, richness recorded", len(r.trades) == 1 and abs(r.trades.iloc[0]["rich"] - 2.0) < 1e-6,
      str(r.trades["rich"].tolist() if len(r.trades) else None))
check("cheap option (0.7) skipped", wb.simulate(pg, data([cheap_c], {KEY: path()})).trades.empty)
check("no SPAN vol -> skipped while the gate is on", wb.simulate(pg, data([none_c], {KEY: path()})).trades.empty)
check("gate off: cheap option entered, rich still recorded",
      abs(wb.simulate(BASE, data([cheap_c], {KEY: path()})).trades.iloc[0]["rich"] - 0.7) < 1e-6)

print("\n=== 30. interest on cash ===")
check("off by default", wb.Params().cash_yield == 0.0 and wb.Params().cash_haircut == 0.0)
r0 = wb.simulate(BASE, data([], {}))
check("no yield -> no interest", r0.metrics.get("interest_rs") == 0 and r0.metrics["pnl"] == 0)
py = replace(BASE, cash_yield=0.065)
ri = wb.simulate(py, data([], {}))
days = (CAL[-1] - CAL[0]).days
exp_i = BASE.cash * ((1 + 0.065 / 365) ** days - 1)           # daily accrual on a growing balance
check("idle cash earns ~6.5% p.a. over the window (compounded by session)",
      abs(ri.metrics["interest_rs"] - exp_i) / exp_i < 0.01, f"{ri.metrics['interest_rs']} vs {exp_i:.0f}")
check("weekend days accrue too (calendar days)", ri.equity["interest"].iloc[1] > 0
      and abs(ri.equity["interest"].iloc[1] - BASE.cash * 0.065 * (CAL[1] - CAL[0]).days / 365) < 1)
rt = wb.simulate(py, data([cand()], {KEY: path()}))
check("identity: final equity = start + trade P&L + interest",
      abs(rt.equity["equity"].iloc[-1] - (BASE.cash + BASE.collateral + rt.trades["pnl"].sum()
                                         + rt.equity["interest"].iloc[-1])) < 1e-6)
check("trading_pnl = pnl - interest", rt.metrics["trading_pnl"] == rt.metrics["pnl"] - rt.metrics["interest_rs"])
ph = replace(BASE, sizing="pct", trade_margin_pct=0.05, max_vol_frac=1.0)
c_big = pd.DataFrame([cand(margin=50_000.0)])
st = wb.State(cash=ph.cash)
_, op0, _ = wb.step(ph, st, ENTRY, wb.Ctx(CAL, {}, {}, {"XYZ": "IT"}), lambda k: None, c_big)
st = wb.State(cash=ph.cash)
_, op1, _ = wb.step(replace(ph, cash_haircut=0.5), st, ENTRY, wb.Ctx(CAL, {}, {}, {"XYZ": "IT"}), lambda k: None, c_big)
check("a cash haircut shrinks capacity -> fewer lots", op0[0].lots > op1[0].lots, f"{op0[0].lots} vs {op1[0].lots}")

print("\n=== 31. dated cash rates and deterministic picks ===")
rf = Path(tempfile.mkdtemp()) / "rates.csv"
rf.write_text("# test\ndate,rate,note\n2024-02-01,0.04,a\n2024-03-01,6.0,percent\n")
pr = replace(BASE, cash_yield=0.10, cash_yield_file=str(rf))
check("rate before the first row = cash_yield", wb.cash_rate(pr, date(2024, 1, 15)) == 0.10)
check("row applies from its date", wb.cash_rate(pr, date(2024, 2, 1)) == 0.04 and wb.cash_rate(pr, date(2024, 2, 29)) == 0.04)
check("percent values are read as percent", abs(wb.cash_rate(pr, date(2024, 3, 5)) - 0.06) < 1e-12)
rr = wb.simulate(pr, data([], {}))
ie = rr.equity.set_index("date")["interest"]
acc_feb = ie[max(x for x in ie.index if x <= date(2024, 2, 29))] - ie[max(x for x in ie.index if x <= date(2024, 1, 31))]
check("February accrues at ~4%, not 10%", 0.035 < acc_feb / (BASE.cash * 29 / 365) < 0.05, f"{acc_feb / (BASE.cash * 29 / 365):.4f}")
check("the shipped rate table loads", len(wb.load_rate_table(str(wb.DEFAULT_RATE_FILE))[0]) > 5
      if wb.DEFAULT_RATE_FILE.exists() else True)
px = duckdb.connect()
px.execute("""CREATE TABLE r AS SELECT * FROM (VALUES
  (DATE '2019-12-05','YESBANK','PE','YESBANK',DATE '2019-12-26',30.0,0.20,1,1.0,0.03,0.0,50.0,1,1,0.5),
  (DATE '2019-12-05','YESBANK','PE','YESBANK',DATE '2019-12-26',25.0,0.10,1,1.0,0.03,0.0,50.0,1,1,0.5),
  (DATE '2019-12-05','YESBANK','PE','YESBANK',DATE '2020-01-30',25.0,0.30,1,1.0,0.03,0.0,50.0,1,1,0.5),
  (DATE '2019-12-05','YESBANK','CE','YESBANK',DATE '2019-12-26',70.0,0.10,1,1.0,0.03,0.0,50.0,1,1,0.5),
  (DATE '2019-12-05','YESBANK','CE','YESBANK',DATE '2019-12-26',80.0,0.05,1,1.0,0.03,0.0,50.0,1,1,0.5))
  AS v(trade_date, symbol, opt_type, raw, expiry, strike, close, lot, margin, rom, delta, und, oi, volume, svol)""")
picks = [[(a, float(b), c, float(e)) for a, b, c, e in
          px.execute(f"WITH {wb.PICK_SQL} SELECT opt_type, strike, expiry, premium FROM pk ORDER BY 1", [0.02]).fetchall()]
         for _ in range(5)]
check("delta ties: furthest-OTM strike, nearest expiry, fields from one row, every time",
      all(x == picks[0] for x in picks) and picks[0] == [("CE", 80.0, date(2019, 12, 26), 0.05),
                                                       ("PE", 25.0, date(2019, 12, 26), 0.10)], str(picks[0]))

print("\n=== 32. put regime gate ===")
pg = replace(BASE, entry_mode="all", put_block_dma=50, put_block_vix=20.0)
def mkt(vs50, vix): return {"daily": {ENTRY: {"vix": vix, "nifty_vs50": vs50}}}
r = wb.simulate(pg, data([cand(), call], {KEY: path(), KC: path()}, market=mkt(-0.01, 15.0)))
check("NIFTY below 50-DMA, VIX 15: no new put, call still entered", list(r.trades["side"]) == ["CE"],
      str(list(r.trades["side"])))
r = wb.simulate(pg, data([cand()], {KEY: path()}, market=mkt(-0.01, 26.0)))
check("below 50-DMA but VIX 26 (fear priced): put entered", len(r.trades) == 1)
r = wb.simulate(pg, data([cand()], {KEY: path()}, market=mkt(0.01, 15.0)))
check("above 50-DMA: put entered", len(r.trades) == 1)
r = wb.simulate(replace(pg, put_block_vix=None), data([cand()], {KEY: path()}, market=mkt(-0.01, 26.0)))
check("no VIX limit: any close below the average blocks puts", len(r.trades) == 0)
r = wb.simulate(pg, data([cand()], {KEY: path()}, market={"daily": {ENTRY: {"vix": 15.0}}}))
check("no average yet (short history): gate stays open", len(r.trades) == 1)
check("gate off by default", wb.Params().put_block_dma is None)
con32 = duckdb.connect()
con32.execute("ATTACH ':memory:' AS bc")
con32.execute("""CREATE TABLE bc.index_eod AS SELECT DATE '2024-01-01' + i::INTEGER AS trade_date, n AS index_name,
                 CASE WHEN n = 'Nifty 50' THEN 100.0 + i::INTEGER ELSE 15.0 END AS close
                 FROM range(0, 60) t(i), (VALUES ('Nifty 50'), ('India VIX')) v(n)""")
mm = wb.load_market(con32, date(2024, 1, 1), date(2024, 3, 1))["daily"]
d59 = date(2024, 1, 1) + timedelta(days=59)
check("load_market: 50-session average of 110..159 = 134.5", abs(mm[d59]["nifty_vs50"] - (159 / 134.5 - 1)) < 1e-12)
check("load_market: no 50-DMA before 50 sessions", "nifty_vs50" not in mm[date(2024, 1, 1) + timedelta(days=48)])

print("\n=== 33. IV / HV20 gate ===")
hvm = {"hv20": {"XYZ": ([ENTRY - timedelta(days=3)], [0.30])}}
pv = replace(BASE, max_iv_hv=1.0)
r = wb.simulate(pv, data([dict(cand(), atm_iv=0.27)], {KEY: path()}, market=hvm))
check("IV/HV 0.9 <= 1.0: entered, ratio recorded", len(r.trades) == 1 and abs(r.trades.iloc[0]["iv_hv"] - 0.9) < 1e-9,
      str(list(r.trades.get("iv_hv", []))))
r = wb.simulate(pv, data([dict(cand(), atm_iv=0.36)], {KEY: path()}, market=hvm))
check("IV/HV 1.2 > 1.0: skipped", len(r.trades) == 0)
r = wb.simulate(pv, data([cand()], {KEY: path()}, market=hvm))
check("no ATM IV: skipped (fail closed)", len(r.trades) == 0)
r = wb.simulate(pv, data([dict(cand(), atm_iv=0.27)], {KEY: path()}))
check("no HV20: skipped (fail closed)", len(r.trades) == 0)
r = wb.simulate(BASE, data([dict(cand(), atm_iv=0.36)], {KEY: path()}, market=hvm))
check("gate off: entered", len(r.trades) == 1 and wb.Params().max_iv_hv is None)
cq = duckdb.connect()
cq.execute("ATTACH ':memory:' AS bc")
d0, e0, u0 = date(2024, 1, 25), date(2024, 2, 29), 100.0
tt = (e0 - d0).days / 365.0
rows = [(d0, "XYZ", e0, side, k, round(wb.bs_price(side, u0, k, tt, 0.25), 2), v)
        for side in ("CE", "PE") for k, v in ((95.0, 10), (100.0, 10), (101.0, 0), (105.0, 10))]
cq.execute("CREATE TABLE bc.options_eod (trade_date DATE, symbol VARCHAR, expiry DATE, opt_type VARCHAR, "
           "strike DOUBLE, close DOUBLE, volume DOUBLE)")
cq.executemany("INSERT INTO bc.options_eod VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
ci = pd.DataFrame([dict(trade_date=d0, raw="XYZ", expiry=e0, und=100.4), dict(trade_date=d0, raw="XYZ", expiry=e0, und=100.4),
                   dict(trade_date=d0, raw="ABC", expiry=e0, und=50.0)])
aiv = wb.load_atm_iv(cq, ci)
check("load_atm_iv: nearest TRADED strike (100, not untraded 101), ~25% vol", abs(aiv.iloc[0] - 0.25) < 0.01
      and aiv.iloc[0] == aiv.iloc[1], f"{aiv.iloc[0]:.4f}")
check("load_atm_iv: no options -> NaN", aiv.iloc[2] != aiv.iloc[2])

print("\n=== 34. early profit-taking ===")
pp = replace(BASE, profit_residual=0.5)
pt = path()
r = wb.simulate(pp, data([cand()], {KEY: pt}))
t0 = r.trades.iloc[0]
prev = max(d for d in pt if d < t0["exit_date"])
check("closes on the first day mark <= 50% of entry premium", t0["reason"] == "profit"
      and pt[t0["exit_date"]][2] <= 1.0 < pt[prev][2], f"{t0['reason']} {t0['exit_date']}")
day2 = t0["exit_date"]
r = wb.simulate(pp, data([cand(), dict(cand(day=day2), strike=85.0)], {KEY: pt, ("XYZ", "PE", EXP, 85.0): path(start=day2)}))
check("freed slot is re-used the same session", len(r.trades) == 2 and r.trades.iloc[1]["entry_date"] == day2,
      str(list(r.trades["entry_date"])))

print("\n=== 35. strike choice ===")
d0, e0 = date(2024, 1, 25), date(2024, 2, 29)
def strk(k, prem, dl, rom, svol=0.25, side="PE", sym="XYZ", e=e0):
    return dict(trade_date=d0, symbol=sym, opt_type=side, raw=sym, expiry=e, strike=k, premium=prem, lot=100,
                margin=1e4, rom=rom, delta=dl, und=100.0, oi=1000, volume=50, svol=svol)
tt = (e0 - d0).days / 365.0
fair = lambda k: wb.bs_price_r("PE", 100.0, k, tt, 0.25, 0.065)
chain = pd.DataFrame([strk(90.0, fair(90.0) * 1.3, 0.10, 0.025), strk(95.0, fair(95.0) * 1.1, 0.20, 0.040),
                      strk(88.0, fair(88.0) * 1.3, 0.08, 0.021, svol=float("nan")),
                      strk(85.0, 0.30, 0.05, 0.02, side="CE", e=date(2024, 3, 28)),
                      strk(110.0, 0.30, 0.05, 0.02, side="CE")])
pr = wb.pick_strikes(chain, replace(BASE, pick_by="rich")).set_index("opt_type")
check("rich: highest premium / model price (90 at 1.3x; no-SPAN-vol strike ranks last)", pr.loc["PE", "strike"] == 90.0,
      str(pr.loc["PE", "strike"]))
po = wb.pick_strikes(chain, replace(BASE, pick_by="rom")).set_index("opt_type")
check("ties: equal rom and delta -> furthest OTM call (110)", po.loc["CE", "strike"] == 110.0,
      str(po.loc["CE", ["strike", "expiry"]].tolist()))
check("rom: highest premium / margin (95)", po.loc["PE", "strike"] == 95.0)
check("one row per day / symbol / side", len(pr) == 2 and len(po) == 2)
check("pick_by changes the data key", wb.data_key(replace(BASE, pick_by="rich")) != wb.data_key(BASE))

print("\n=== 36. richness measure, delta floor, confirmation ===")
tt36 = (EXP - ENTRY).days / 365.0
iv36 = wb.implied_vol_r("PE", 2.0, 100.0, 90.0, tt36, 0.065)
check("vol ratio = strike IV / SPAN vol", abs(wb.vol_ratio("PE", 2.0, 100.0, 90.0, tt36, iv36 / 1.2) - 1.2) < 1e-6)
pv36 = replace(BASE, rich_measure="vol", min_rich=1.1)
r = wb.simulate(pv36, data([dict(cand(), svol=iv36 / 1.2)], {KEY: path()}))
check("vol gate: IV 1.2x SPAN vol passes 1.1 (computed live)", len(r.trades) == 1 and abs(r.trades.iloc[0]["rich"] - 1.2) < 1e-6)
r = wb.simulate(pv36, data([dict(cand(), svol=iv36 / 1.05)], {KEY: path()}))
check("vol gate: 1.05x fails 1.1", len(r.trades) == 0)
pc36 = replace(BASE, min_rich=1.1, rich_confirm_days=1)
r = wb.simulate(pc36, data([dict(cand(), rich_px=1.3, rich_px_p1=1.0)], {KEY: path()}))
check("confirm 1: rich today but not yesterday -> skipped", len(r.trades) == 0)
r = wb.simulate(pc36, data([dict(cand(), rich_px=1.3, rich_px_p1=1.15)], {KEY: path()}))
check("confirm 1: rich today and yesterday -> entered", len(r.trades) == 1)
r = wb.simulate(pc36, data([dict(cand(), rich_px=1.3)], {KEY: path()}))
check("confirm 1: no history -> skipped (fail closed)", len(r.trades) == 0)
d1, d2, d4 = CAL[10], CAL[11], CAL[13]
h = pd.DataFrame([dict(cand(day=x), svol=0.3, expiry=EXP) for x in (d1, d2, d4)])
hh = wb.add_rich_history(h, CAL, BASE).set_index("trade_date")
check("history: previous session's value attached", abs(hh.loc[d2, "rich_px_p1"] - hh.loc[d1, "rich_px"]) < 1e-12
      and hh.loc[d2, "rich_px_p2"] != hh.loc[d2, "rich_px_p2"])
check("history: a session gap gives NaN", hh.loc[d4, "rich_px_p1"] != hh.loc[d4, "rich_px_p1"]
      and abs(hh.loc[d4, "rich_px_p2"] - hh.loc[d2, "rich_px"]) < 1e-12)
ch36 = pd.DataFrame([dict(trade_date=ENTRY, symbol="XYZ", opt_type="PE", raw="XYZ", expiry=EXP, strike=k, premium=pr,
                          lot=100, margin=1e4, rom=0.03, delta=dl, und=100.0, oi=1000, volume=50, svol=0.25)
                     for k, pr, dl in ((90.0, wb.bs_price_r("PE", 100, 90, tt36, 0.30, 0.065), 0.10),
                                       (85.0, wb.bs_price_r("PE", 100, 85, tt36, 0.28, 0.065), 0.06))])
pk36 = wb.pick_strikes(ch36, replace(BASE, pick_by="volrich"))
check("volrich: picks the higher IV / SPAN vol strike (90 at 1.2x over 85 at 1.12x)", float(pk36.iloc[0]["strike"]) == 90.0)
check("min_delta changes the data key", wb.data_key(replace(BASE, min_delta=0.05)) != wb.data_key(BASE))

print("\n=== 37. notional caps ===")
n37 = 90.0 * 100                                          # one lot: strike 90 x 100 shares
r = wb.simulate(replace(BASE, max_symbol_notional=0.003), data([cand()], {KEY: path()}))
check("per-stock cap below one lot's notional: skipped", len(r.trades) == 0)
r = wb.simulate(replace(BASE, max_symbol_notional=0.004), data([cand()], {KEY: path()}))
check("per-stock cap above one lot: entered", len(r.trades) == 1)
pp37 = replace(BASE, sizing="pct", trade_margin_pct=0.05, max_lots_per_trade=20, max_vol_frac=1.0)
c37 = dict(cand(), volume=1000)
r0 = wb.simulate(pp37, data([c37], {KEY: path()}))
r1 = wb.simulate(replace(pp37, max_symbol_notional=10.5 * n37 / BASE.cash), data([c37], {KEY: path()}))
check("pct sizing shrinks lots to the per-stock notional cap", int(r0.trades.iloc[0]["lots"]) > 10
      and int(r1.trades.iloc[0]["lots"]) == 10, f"{int(r0.trades.iloc[0]['lots'])} -> {int(r1.trades.iloc[0]['lots'])}")
r = wb.simulate(replace(pp37, max_put_notional=3.5 * n37 / (BASE.cash + BASE.collateral)), data([c37], {KEY: path()}))
check("put notional cap: 3 lots", int(r.trades.iloc[0]["lots"]) == 3, str(int(r.trades.iloc[0]["lots"])))
r = wb.simulate(replace(BASE, max_put_notional=0.0001), data([cand(), call], {KEY: path(), KC: path()}))
check("put notional cap does not touch calls", list(r.trades["side"]) == ["CE"], str(list(r.trades["side"])))
check("caps off by default", wb.Params().max_symbol_notional is None and wb.Params().max_put_notional is None)

print("\n=== 38. CA blackout scope + knowability, weekend results, window end, SPAN gaps ===")
ex38 = ENTRY + timedelta(days=14)                          # ex-date inside the contract's life
ann38 = ENTRY + timedelta(days=7)                          # announced a week after entry
def d38(adj, known, ca_all=None):
    dd = data([cand()], {KEY: path()}, ca=ca_all if ca_all is not None else {"XYZ": [ex38]})
    dd.ca_adj, dd.ca_known = adj, known
    return dd
r = wb.simulate(BASE, d38({"XYZ": [ex38]}, {("XYZ", ex38): ann38}))
t38 = r.trades.iloc[0] if len(r.trades) else None
check("adjusting CA not yet announced at entry: entered", t38 is not None)
prev38 = max(x for x in CAL if x < ex38)
check("once announced, closed the session before the ex-date", t38 is not None and t38["reason"] == "corp_action"
      and t38["exit_date"] == prev38, f"{None if t38 is None else (t38['reason'], t38['exit_date'])}")
r = wb.simulate(BASE, d38({"XYZ": [ex38]}, {("XYZ", ex38): ENTRY - timedelta(days=3)}))
check("adjusting CA already announced: entry skipped", len(r.trades) == 0)
r = wb.simulate(BASE, d38({}, {("XYZ", ex38): ENTRY - timedelta(days=3)}))
check("ordinary dividend (not in the adjusting set): ignored by default", len(r.trades) == 1
      and r.trades.iloc[0]["reason"] != "corp_action")
r = wb.simulate(replace(BASE, ca_scope="all"), d38({}, {("XYZ", ex38): ENTRY - timedelta(days=3)}))
check("ca_scope 'all': the same dividend blocks entry", len(r.trades) == 0)
r = wb.simulate(replace(BASE, pit_ca=False), d38({"XYZ": [ex38]}, {("XYZ", ex38): ann38}))
check("pit_ca off: unannounced ex-date blocks entry (old look-ahead)", len(r.trades) == 0)
check("defaults: adjusting scope, point-in-time", wb.Params().ca_scope == "adjusting" and wb.Params().pit_ca)
sat = date(2024, 2, 10)                                    # Saturday results
r = wb.simulate(BASE, data([cand()], {KEY: path()}, results={"XYZ": [sat]}))
check("Saturday results: exit Friday (last session before), not Thursday",
      r.trades.iloc[0]["reason"] == "results" and r.trades.iloc[0]["exit_date"] == date(2024, 2, 9),
      str(r.trades.iloc[0]["exit_date"]))
r = wb.simulate(replace(BASE, end=EXP - timedelta(days=12)), data([cand()], {KEY: path()}))
check("window ends before expiry: no false final exit, closed as window_end",
      r.trades.iloc[0]["reason"] == "window_end", r.trades.iloc[0]["reason"])
pg = path()
dg = sorted(pg)[5]
pg[dg] = (7.0, 10.0, wb.NAN, wb.NAN, wb.NAN, wb.NAN, wb.NAN, wb.NAN)         # traded at 7.0, no SPAN row
r = wb.simulate(replace(BASE, stop_mult=3.0, price_basis="close"), data([cand()], {KEY: pg}))
check("SPAN gap day: marked at the traded close, 3x stop fires at 7.0",
      r.trades.iloc[0]["reason"] == "stop_mult" and abs(r.trades.iloc[0]["exit_px"] - 7.0) < 1e-9
      and r.trades.iloc[0]["exit_date"] == dg, f"{r.trades.iloc[0]['reason']} {r.trades.iloc[0]['exit_px']}")

print("\n" + "=" * 62)
print(f"  {'ALL PASSED' if not fails else str(len(fails)) + ' FAILED: ' + ', '.join(fails)}")
print("=" * 62)


def test_all_properties():
    """pytest entry point."""
    assert not fails, f"failed: {fails}"
