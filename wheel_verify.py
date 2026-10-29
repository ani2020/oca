"""
wheel_verify -- independent re-derivation of a wheel_bt run's trades from the raw bhavcopy / SPAN data.

Written separately from wheel_bt (it imports nothing from it): it reads the run's stored
params and trades, then for every option trade re-checks, from raw tables,

  entry   E1 the contract traded on the entry day and entry_px == the bhavcopy CLOSE
          E2 volume >= min_entry_volume and OI (contracts) >= min_entry_oi
          E3 days to expiry within dte_lo..dte_hi
          E4 out of the money against the SPAN underlying price
          E5 ROM = close x lot / (short_scan x lot x broker_mult + elm x und x lot) >= rom_target
          E6 |SPAN delta| <= delta_cap, and it is THE pick: the lowest-|delta| qualifying strike
             (ties: furthest OTM, then nearest expiry)
          E7 the symbol was an index member that day (latest month-end list before the day)
          E8 no board meeting (bar AGM/EGM) already announced within earn_entry_days sessions
          E9 richness (premium / Black-Scholes at SPAN vol, rate rich_rate) >= min_rich, if set
          E10 lots <= max_lots_per_trade and quantity = lots x the bhavcopy lot size
          E11 no corporate action (run's scope; announced by entry if pit_ca) inside the contract's life
  exit    X1 the exit day and reason = the FIRST day a rule fires, walking the SPAN marks:
             board meeting within earn_exit_lead sessions, stop (mark >= stop_mult x entry_px),
             final exit (<= final_exit_td sessions left), time exit (<= time_exit_td sessions
             left and |delta| > near_money_delta)
          X2 exit_px = that day's CLOSE if the contract traded, else the SPAN mark
  money   M1 credit = CLOSE x (1 - half-spread of the entry-day volume bucket)
          M2 exit fill = exit_px x (1 + half-spread(max(exit vol, entry vol)) + untraded slippage)
          M3 pnl = (credit - fill) x quantity - costs, and costs / premium within 0-3%

Checks that depend on wheel_bt features this script does not re-implement (stress cap,
sizing beyond the lot cap, sector cap, ranking among candidates) are not checked.

    python wheel_verify.py --run "cash yield2 {'cash_yield': 0.0}"
    python wheel_verify.py --run <run_id> --limit 200
writes <out>/wheel_verify_<run_id>.csv (one row per trade, a column per check)
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import os
import sys
from pathlib import Path

import pandas as pd

OC_DB = Path(os.environ.get("OC_DB", r"C:\Users\aniru\od\oc.duckdb"))
BHAV_DB = Path(os.environ.get("BHAV_DB", r"C:\Users\aniru\od\bhavcopy.duckdb"))
WHEEL_DB = Path(os.environ.get("WHEEL_DB", r"C:\Users\aniru\od\wheel_bt.duckdb"))
RENAMES = {"TATAMOTORS": "TMPV", "ZOMATO": "ETERNAL", "SRTRANSFIN": "SHRIRAMFIN", "INFRATEL": "INDUSTOWER"}
SPREAD = ((10, 0.535), (25, 0.17), (100, 0.095), (500, 0.045), (None, 0.02))
TOL = 0.006          # price tolerance (Rs): bhavcopy prices have 2 decimals


def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs(side, s, k, t, v, r):
    if t <= 0 or v <= 0 or s <= 0 or k <= 0:
        return max(k - s, 0.0) if side == "PE" else max(s - k, 0.0)
    sd = v * math.sqrt(t)
    d1 = (math.log(s / k) + (r + 0.5 * v * v) * t) / sd
    d2 = d1 - sd
    dk = k * math.exp(-r * t)
    return dk * ncdf(-d2) - s * ncdf(-d1) if side == "PE" else s * ncdf(d1) - dk * ncdf(d2)


def hs(p, vol):
    if p.get("cost_model", "spread") != "spread":
        return p.get("slippage", 0.10)
    buckets = p.get("spread_buckets") or SPREAD
    for hi, h in buckets:
        if hi is None or vol < hi:
            return h
    return buckets[-1][1]


def connect():
    import duckdb
    con = duckdb.connect(str(OC_DB), read_only=True)
    con.execute(f"ATTACH '{BHAV_DB}' AS bc (READ_ONLY)")
    return con


def load_run(run: str):
    import duckdb
    w = duckdb.connect(str(WHEEL_DB), read_only=True)
    try:
        row = w.execute("SELECT run_id, label, params FROM runs WHERE run_id = ? OR label = ? "
                        "ORDER BY created DESC LIMIT 1", [run, run]).fetchone()
        if row is None:
            raise SystemExit(f"no run {run!r}")
        tr = w.execute("SELECT * FROM trades WHERE run_id = ? AND side IN ('CE','PE') ORDER BY entry_date, pid",
                       [row[0]]).df()
    finally:
        w.close()
    return row[0], row[1], json.loads(row[2]), tr


def verify(run: str, out: Path, limit: int | None = None, con=None) -> pd.DataFrame:
    rid, label, p, tr = load_run(run)
    if limit:
        tr = tr.head(limit)
    for c in ("entry_date", "exit_date", "expiry"):
        tr[c] = pd.to_datetime(tr[c]).dt.date
    print(f"  verifying {len(tr)} option trades of {label}  ({rid})")
    own = con is None
    con = con or connect()
    try:
        cal = [r[0] for r in con.execute("SELECT DISTINCT trade_date FROM bc.span_ingest_log ORDER BY 1").fetchall()]
        pos = {d: i for i, d in enumerate(cal)}
        tag = dict(con.execute("""SELECT trade_date, arg_max(tag, CASE WHEN tag = 's' THEN 9
                                  ELSE TRY_CAST(right(tag, 1) AS INT) END) FROM bc.span_ingest_log GROUP BY 1""").fetchall())
        raw_of = {}                                       # canonical -> raw names used in the bhavcopy
        for a, b in RENAMES.items():
            raw_of.setdefault(b, set()).add(a)
        tr["raws"] = [sorted({s} | raw_of.get(s, set())) for s in tr["symbol"]]
        # (a) each trade's own contract on every day it was held  (b) the whole chain on entry days only
        pk = pd.DataFrame([(d, r, t.side, t.expiry, float(t.strike), tag.get(d))
                           for t in tr.itertuples(index=False) for r in t.raws
                           for d in cal[pos[t.entry_date]:pos[t.exit_date] + 1]],
                          columns=["trade_date", "symbol", "opt_type", "expiry", "strike", "tag"]).drop_duplicates()
        ek = pd.DataFrame(sorted({(t.entry_date, r, tag.get(t.entry_date)) for t in tr.itertuples(index=False)
                                  for r in t.raws}), columns=["trade_date", "symbol", "tag"])
        d0, d1 = min(tr["entry_date"]), max(tr["exit_date"])
        con.register("_pk", pk)
        con.register("_ek", ek)
        oc = "o.trade_date, o.symbol, o.expiry, o.opt_type, o.strike, o.close, o.volume, o.oi, o.lot"
        sc = "s.trade_date, s.symbol, s.instr AS opt_type, s.expiry, s.strike, s.price, s.delta, s.short_scan, s.vol"
        opt = con.execute(f"""
            SELECT {oc} FROM bc.options_eod o JOIN _pk k ON k.trade_date = o.trade_date AND k.symbol = o.symbol
                 AND k.opt_type = o.opt_type AND k.expiry = o.expiry AND k.strike = o.strike
            WHERE o.trade_date BETWEEN ? AND ?
            UNION
            SELECT {oc} FROM bc.options_eod o JOIN _ek k ON k.trade_date = o.trade_date AND k.symbol = o.symbol
            WHERE o.trade_date BETWEEN ? AND ?""", [d0, d1, d0, d1]).df()
        spn = con.execute(f"""
            SELECT {sc} FROM bc.span_contract s JOIN _pk k ON k.trade_date = s.trade_date AND k.symbol = s.symbol
                 AND k.opt_type = s.instr AND k.expiry = s.expiry AND k.strike = s.strike AND k.tag = s.tag
            WHERE s.trade_date BETWEEN ? AND ?
            UNION
            SELECT {sc} FROM bc.span_contract s JOIN _ek k ON k.trade_date = s.trade_date AND k.symbol = s.symbol
                 AND k.tag = s.tag
            WHERE s.trade_date BETWEEN ? AND ?""", [d0, d1, d0, d1]).df()
        und = con.execute("""SELECT u.trade_date, u.symbol, u.price AS und FROM bc.span_und u
                             JOIN (SELECT DISTINCT trade_date, symbol, tag FROM _pk) k
                             ON k.symbol = u.symbol AND k.trade_date = u.trade_date AND k.tag = u.tag""").df()
        con.unregister("_pk")
        con.unregister("_ek")
        ev = con.execute("""SELECT symbol, event_date, announced_at FROM bc.corporate_events
                            WHERE event_type <> 'AGM_EGM'""").df()
        # corporate actions, rebuilt independently: every ex-date (legacy scope) and the
        # contract-adjusting subset, each with the board meeting that announced it
        cax = con.execute("""SELECT symbol, ex_date, upper(coalesce(ca_type, '')) t, div_amount
                             FROM bc.corporate_actions""").df()
        cax_old = con.execute(f"""SELECT symbol, ex_date FROM bc.corporate_actions WHERE ca_type IN
                                  ('SPLIT','BONUS','DEMERGER','RIGHTS')
                                  UNION SELECT symbol, trade_date FROM bc.cash_returns_adj WHERE is_ca""").df()
        cax_isca = con.execute("""SELECT symbol, trade_date AS ex_date, upper(coalesce(ca_type, 'OTHER')) t
                                  FROM bc.cash_returns_adj WHERE is_ca""").df()
        meets = con.execute("SELECT DISTINCT symbol, event_date FROM bc.corporate_events").df()
        upx = con.execute("SELECT symbol, trade_date, avg(price) px FROM bc.span_und WHERE price > 0 GROUP BY 1, 2").df()
        hist = con.execute("SELECT index_name, as_of, symbol FROM index_constituents_hist").df() \
            if con.execute("SELECT count(*) FROM information_schema.tables WHERE table_name = 'index_constituents_hist'"
                           ).fetchone()[0] else pd.DataFrame(columns=["index_name", "as_of", "symbol"])
    finally:
        if own:
            con.close()
    for df in (opt, spn, und):
        df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.date
    for df in (opt, spn):
        df["expiry"] = pd.to_datetime(df["expiry"]).dt.date
        df["strike"] = df["strike"].astype(float)
    kc = ["trade_date", "symbol", "opt_type", "expiry", "strike"]
    O = {k: v for k, v in zip(map(tuple, opt[kc].values), opt[["close", "volume", "oi", "lot"]].values.tolist())}
    S = {k: v for k, v in zip(map(tuple, spn[kc].values), spn[["price", "delta", "short_scan", "vol"]].values.tolist())}
    U = dict(zip(zip(und["trade_date"], und["symbol"]), und["und"]))
    OC = {"close": 0, "volume": 1, "oi": 2, "lot": 3}
    SC = {"price": 0, "delta": 1, "short_scan": 2, "vol": 3}
    ev["event_date"] = pd.to_datetime(ev["event_date"]).dt.date
    ev["known"] = pd.to_datetime(ev["announced_at"]).dt.date
    ev["sym"] = ev["symbol"].map(lambda s: RENAMES.get(s, s))
    evs = {s: g[["event_date", "known"]].values.tolist() for s, g in ev.groupby("sym")}
    # ---- CA ex-dates per canonical symbol: legacy set, adjusting set, knowable date
    for df, c in ((cax, "ex_date"), (cax_old, "ex_date"), (cax_isca, "ex_date"), (meets, "event_date"), (upx, "trade_date")):
        df[c] = pd.to_datetime(df[c]).dt.date
    mt = {s: sorted(g["event_date"]) for s, g in meets.groupby("symbol")}
    px = {s: (list(g.sort_values("trade_date")["trade_date"]), list(g.sort_values("trade_date")["px"]))
          for s, g in upx.groupby("symbol")}

    def meeting(sym, ex):
        m = [x for x in mt.get(sym, []) if ex - pd.Timedelta(days=120).to_pytimedelta() <= x <= ex]
        return m[-1] if m else None

    def price_on(sym, dd):
        ds, vs = px.get(sym, ([], []))
        i = bisect.bisect_right(ds, dd) - 1
        return vs[i] if i >= 0 and (dd - ds[i]).days <= 30 else None

    ca_old, ca_adj, ca_kn = {}, {}, {}
    for sym, exd in cax_old[["symbol", "ex_date"]].values:
        ca_old.setdefault(RENAMES.get(sym, sym), set()).add(exd)
    allrows = [(r.symbol, r.ex_date, r.t, r.div_amount) for r in cax.itertuples(index=False)] + \
              [(r.symbol, r.ex_date, r.t, None) for r in cax_isca.itertuples(index=False)]
    for sym, exd, t, amt in allrows:
        cs = RENAMES.get(sym, sym)
        m = meeting(sym, exd)
        kn = (m + pd.Timedelta(days=1).to_pytimedelta()) if m else exd - pd.Timedelta(days=7).to_pytimedelta()
        ca_kn[(cs, exd)] = min(ca_kn.get((cs, exd), kn), kn)
        adj = any(x in t for x in ("SPLIT", "BONUS", "RIGHTS", "DEMERGER"))
        if not adj and "DIVIDEND" in t and amt is not None and amt == amt:
            pr = price_on(sym, m if m else exd - pd.Timedelta(days=7).to_pytimedelta())
            thr = 0.05 if exd < pd.Timestamp("2022-06-29").date() else 0.02
            adj = (pr is None) or (amt / pr >= thr)
        if adj:
            ca_adj.setdefault(cs, set()).add(exd)
    scope_adj = p.get("ca_scope", "all") == "adjusting"      # runs before 28 Oct: legacy rule
    pit_ca = p.get("pit_ca", False)

    def cas_known(sym, dd):
        src = (ca_adj if scope_adj else ca_old).get(sym, set())
        return sorted(e for e in src if (not pit_ca) or ca_kn.get((sym, e), e - pd.Timedelta(days=7).to_pytimedelta()) <= dd)

    hist["as_of"] = pd.to_datetime(hist["as_of"]).dt.date
    lists = {d: set(g["symbol"]) for d, g in hist.groupby("as_of")}
    list_days = sorted(lists)

    def td(a, b):
        """Sessions from a to b; a date that is not a session counts from the next session."""
        return bisect.bisect_left(cal, b) - bisect.bisect_left(cal, a)

    def td_exp(a, e):
        """Sessions from a to expiry e; an expiry on a holiday moves to the PREVIOUS session (NSE rule)."""
        return (bisect.bisect_right(cal, e) - 1) - bisect.bisect_left(cal, a)

    def get(df, key, col):
        row = df.get(key)
        if row is None:
            return float("nan")
        v = row[(OC if df is O else SC)[col]]
        return float(v) if v is not None else float("nan")

    def events_known(sym, d):
        return [e for e, k in evs.get(sym, []) if (k != k or k is None or k <= d)]

    opt_by = {k: g for k, g in opt.groupby(["trade_date", "symbol", "opt_type"])}
    spn_by = {k: g for k, g in spn.groupby(["trade_date", "symbol"])}
    rows = []
    for t in tr.itertuples(index=False):
        raw = next((r for r in t.raws if (t.entry_date, r, t.side, t.expiry, float(t.strike)) in O), t.raws[0])
        k0 = (t.entry_date, raw, t.side, t.expiry, float(t.strike))
        close0, vol0, oi0, lot0 = (get(O, k0, c) for c in ("close", "volume", "oi", "lot"))
        sc0, dl0, sv0 = (get(S, k0, c) for c in ("short_scan", "delta", "vol"))
        u0 = U.get((t.entry_date, raw), float("nan"))
        r = dict(pid=t.pid, symbol=t.symbol, side=t.side, strike=t.strike, expiry=t.expiry, entry_date=t.entry_date,
                 exit_date=t.exit_date, reason=t.reason)
        r["E1_close"] = abs(close0 - t.entry_px) <= TOL
        r["E2_liquidity"] = vol0 >= max(p.get("min_entry_volume", 25), 1) and oi0 / lot0 >= p.get("min_entry_oi", 50)
        dte = (t.expiry - t.entry_date).days
        r["E3_dte"] = p["dte_lo"] <= dte <= p["dte_hi"]
        r["E4_otm"] = (t.strike < u0) if t.side == "PE" else (t.strike > u0)
        margin1 = sc0 * lot0 * p.get("broker_mult", 1.0) + p.get("elm", 0.035) * u0 * lot0
        rom = close0 * lot0 / margin1 if margin1 > 0 else float("nan")
        r["E5_rom"] = rom >= p["rom_target"] - 1e-12
        r["E6_delta_cap"] = abs(dl0) <= p["delta_cap"] + 1e-12
        # E6b: the pick -- lowest |delta| qualifying strike across expiries
        day = opt_by.get((t.entry_date, raw, t.side), opt.iloc[:0])
        day = day.merge(spn_by.get((t.entry_date, raw), spn.iloc[:0]), on=["trade_date", "symbol", "opt_type",
                                                                          "expiry", "strike"])
        day["dte"] = [(e - t.entry_date).days for e in day["expiry"]]
        day = day[(day.dte >= p["dte_lo"]) & (day.dte <= p["dte_hi"]) & (day.close > 0)
                  & (day.volume >= max(p.get("min_entry_volume", 25), 1)) & (day.oi / day.lot >= p.get("min_entry_oi", 50))
                  & ((day.strike < u0) if t.side == "PE" else (day.strike > u0))]
        m = day.short_scan * day.lot * p.get("broker_mult", 1.0) + p.get("elm", 0.035) * u0 * day.lot
        day = day[(m > 0) & (day.close * day.lot / m >= p["rom_target"])]
        if len(day):
            day = day.assign(ad=day.delta.abs(), otm=day.strike if t.side == "PE" else -day.strike)
            best = day.sort_values(["ad", "otm", "expiry"]).iloc[0]
            r["E6_is_pick"] = (best.strike == t.strike) and (best.expiry == t.expiry)
        else:
            r["E6_is_pick"] = False
        i = bisect.bisect_right(list_days, t.entry_date - pd.Timedelta(days=1).to_pytimedelta()) - 1
        r["E7_member"] = (raw in lists[list_days[i]]) if (i >= 0 and lists) else None
        nxt = [e for e in events_known(t.symbol, t.entry_date) if e >= t.entry_date]
        r["E8_no_event"] = not (nxt and td(t.entry_date, min(nxt)) <= p.get("earn_entry_days", 5))
        r["E11_no_ca"] = not any(t.entry_date <= e <= t.expiry for e in cas_known(t.symbol, t.entry_date))
        if p.get("min_rich") is not None:
            mdl = bs(t.side, u0, t.strike, max(dte, 1) / 365.0, sv0, p.get("rich_rate", 0.065))
            r["E9_rich"] = (close0 / mdl >= p["min_rich"] - 1e-9) if mdl > 1e-6 else False
        r["E10_lots"] = (t.lots <= p.get("max_lots_per_trade", 20)) and abs(t.lot - t.lots * lot0) < 0.5
        # exit walk
        stop = p.get("stop_mult")
        defer = p.get("untraded_exit", "slip") == "defer"           # runs before 28 Oct: 'slip'
        pending = None
        exp_reason, exp_day = None, None
        if t.reason == "window_end" and t.exit_date <= t.entry_date:
            exp_reason, exp_day = "window_end", t.exit_date      # entered on the last day of the run
        for d in ([] if exp_day else cal[pos[t.entry_date] + 1:]):
            k = (d, raw, t.side, t.expiry, float(t.strike))
            mark = get(S, k, "price")
            dl = abs(get(S, k, "delta"))
            vol = get(O, k, "volume")
            traded = vol == vol and vol > 0
            if mark != mark and traded:                             # no SPAN row that day: traded close
                mark = get(O, k, "close")
            left = td_exp(d, t.expiry)
            ne = [e for e in events_known(t.symbol, d) if e >= d]
            if left <= 0:
                exp_reason = "expiry"
            elif ne and min(ne) <= t.expiry and td(d, min(ne)) <= p.get("earn_exit_lead", 1):
                exp_reason = "results"
            elif (lambda xs: bool(xs) and xs[0] <= t.expiry and td(d, xs[0]) <= 1)(
                    [e for e in cas_known(t.symbol, d) if e >= d]):
                exp_reason = "corp_action"
            elif stop and mark == mark and (traded or not p.get("stop_requires_trade")) and mark >= stop * t.entry_px:
                exp_reason = "stop_mult"
            elif p.get("profit_residual") is not None and mark == mark and mark <= p["profit_residual"] * t.entry_px:
                exp_reason = "profit"
            elif left <= p.get("final_exit_td", 2):
                exp_reason = "final_exit"
            elif left <= p.get("time_exit_td", 5) and dl == dl and dl > p.get("near_money_delta", 0.15):
                exp_reason = "time_exit"
            if exp_reason is None and pending:
                exp_reason = pending
            if exp_reason and exp_reason != "expiry" and defer and not traded:
                pending = pending or exp_reason                      # cannot buy back today: wait
                exp_reason = None
            elif exp_reason:
                if exp_reason != "expiry" and pending:
                    exp_reason = pending
                exp_day = d
                break
            if d >= t.exit_date and t.reason == "window_end":
                exp_reason, exp_day = "window_end", d
                break
        r["X1_exit"] = (exp_day == t.exit_date) and (exp_reason == t.reason or
                                                     (t.reason, exp_reason) in {("corp_action", "results")})
        r["exp_reason"], r["exp_day"] = exp_reason, exp_day
        kx = (t.exit_date, raw, t.side, t.expiry, float(t.strike))
        cx, vx, mx = get(O, kx, "close"), get(O, kx, "volume"), get(S, kx, "price")
        tradedx = vx == vx and vx > 0 and cx == cx and cx > 0
        ref = cx if (p.get("price_basis") == "close" and tradedx) else mx
        r["X2_exit_px"] = abs(ref - t.exit_px) <= TOL if t.reason != "expiry" else True
        r["M1_credit"] = abs(close0 * (1 - hs(p, vol0)) - t.credit) <= 1e-6
        vx0 = vx if vx == vx else 0.0
        vcost = vx0 if p.get("exit_cost_vol", "max") == "exit" else max(vx0, vol0)   # runs before 28 Oct: max
        fill = t.exit_px * (1 + hs(p, vcost) + (0.0 if tradedx else p.get("untraded_slip", 0.1)))
        r["M2_fill"] = abs(fill - t.exit_fill) <= 1e-6 if t.reason not in ("expiry",) else True
        prem = t.credit * t.lot
        deliv = p.get("delivery_cost", 0.0) * t.strike * t.lot if (t.reason == "expiry" and t.exit_fill > 0) else 0.0
        r["M3_pnl"] = abs((t.credit - t.exit_fill) * t.lot - t.costs - t.pnl) <= 0.5 and \
            0 <= t.costs <= 0.03 * prem + 100 + deliv
        rows.append(r)
    v = pd.DataFrame(rows)
    checks = [c for c in v.columns if c[:2] in ("E1", "E2", "E3", "E4", "E5", "E6", "E7", "E8", "E9", "X1", "X2", "M1", "M2", "M3")
              or c.startswith("E10")]
    out.mkdir(parents=True, exist_ok=True)
    v.to_csv(out / f"wheel_verify_{rid}.csv", index=False)
    print(f"\n  {'check':<14} {'pass':>6} {'fail':>6}")
    for c in checks:
        col = v[c].dropna()
        print(f"  {c:<14} {int(col.sum()):>6} {int((~col.astype(bool)).sum()):>6}")
    bad = v[~v[checks].fillna(True).astype(bool).all(axis=1)]
    print(f"\n  trades failing any check: {len(bad)} of {len(v)}")
    if len(bad):
        with pd.option_context("display.width", 220, "display.max_columns", 40):
            print(bad.head(15)[["pid", "symbol", "side", "entry_date", "exit_date", "reason", "exp_reason", "exp_day"]
                               + [c for c in checks if not bad[c].fillna(True).all()]].to_string(index=False))
    print(f"  wrote {out / f'wheel_verify_{rid}.csv'}")
    return v


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--run", required=True, help="run_id or label")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--out", type=Path, default=WHEEL_DB.parent)
    a = ap.parse_args(argv)
    verify(a.run, a.out, a.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
