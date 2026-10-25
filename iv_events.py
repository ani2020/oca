"""
iv_events -- how implied volatility behaves around results, RBI MPC decisions, the Union Budget
and elections, for the wheel universe (point-in-time index members, stock options) and NIFTY.

1. Builds a daily 30-day constant-maturity ATM IV per symbol from the bhavcopy CLOSE:
   ATM IV of an expiry = mean IV of the nearest-strike call and put that traded that day;
   the two expiries around 30 days are interpolated in total variance (one expiry if only
   one is available; 5-75 DTE). NIFTY likewise from its index options; India VIX as given.
2. For each event (day 0 = the event date, or the next session if it was not a trading day)
   records, relative to the session before (day -1):
     runup      IV(-1) / IV(-10) - 1        the build-up into the event
     crush      IV(+1) / IV(-1) - 1         what happens to IV after it
     move_ratio |ln S(+1)/S(-1)| / (IV(-1) x sqrt(2/252))
                the 2-session move against the move the day -1 IV implied for 2 sessions
                (> 1: moved more than priced)
     straddle   P&L / premium of SELLING a 30-day ATM straddle at day -1 and buying it back
                at day +1 (Black-Scholes at that day's 30-day IV; no costs) -- does selling
                the event pay? Compared with the same 2-session trade on non-event days.
3. Summaries: the average IV path from day -10 to +10, then run-up / crush / move ratio /
   straddle P&L by event type, by sector and by symbol (with the number of events), and
   whether a symbol's pattern persists from the early to the late half of the sample.

    python iv_events.py                                # 2019-10 .. latest
    python iv_events.py --macro data/macro_events.csv  # the shipped MPC / Budget calendar
writes <out>/iv_events_events.csv, iv_events_paths.csv, iv_events_summary.txt
"""
from __future__ import annotations

import argparse
import bisect
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import wheel_bt as wb
from wheel_signals import bs_vec, iv_vec

WIN = 10
BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# 30-day constant-maturity ATM IV
# ---------------------------------------------------------------------------

def cm30(atm: pd.DataFrame) -> pd.DataFrame:
    """atm: rows (d, sym, dte, iv) per expiry -> one 30-day IV per (d, sym), total-variance interpolated."""
    a = atm[(atm["dte"] >= 5) & (atm["dte"] <= 75) & atm["iv"].notna()].sort_values("dte")
    lo = a[a["dte"] <= 30].groupby(["d", "sym"]).last()[["dte", "iv"]]
    hi = a[a["dte"] >= 30].groupby(["d", "sym"]).first()[["dte", "iv"]]
    j = lo.join(hi, lsuffix="1", rsuffix="2", how="outer")
    both = j["dte1"].notna() & j["dte2"].notna() & (j["dte2"] > j["dte1"])
    w = ((30 - j["dte1"]) / (j["dte2"] - j["dte1"])).where(both)
    var = ((1 - w) * j["iv1"] ** 2 * j["dte1"] + w * j["iv2"] ** 2 * j["dte2"]) / 30
    iv = np.sqrt(var).where(both)
    iv = iv.fillna(j["iv1"].where(j["dte1"].notna() & ~both)).fillna(j["iv2"])
    return iv.rename("iv30").reset_index()


def load_iv(con, start: date, end: date) -> pd.DataFrame:
    u_ctes, _ = wb.universe_ctes(con, "pit")
    q = f"""
        WITH {u_ctes}
        bt AS ({wb.BEST_TAG}),
        und AS (SELECT su.trade_date, su.symbol, su.price FROM bc.span_und su JOIN bt
                ON bt.trade_date = su.trade_date AND bt.tag = su.tag),
        o AS (SELECT o.trade_date AS d, coalesce(u.canon, o.symbol) AS sym, o.expiry, o.opt_type, o.strike,
                     o.close, coalesce(nullif(o.spot, 0), und.price) AS s
              FROM bc.options_eod o
              LEFT JOIN (SELECT DISTINCT raw, canon FROM u) u ON u.raw = o.symbol
              LEFT JOIN und ON und.trade_date = o.trade_date AND und.symbol = o.symbol
              WHERE o.trade_date BETWEEN ? AND ? AND o.close > 0 AND o.volume > 0
                AND (u.raw IS NOT NULL OR o.symbol = 'NIFTY')
                AND o.expiry - o.trade_date BETWEEN 5 AND 75)
        SELECT * FROM o WHERE s > 0 AND abs(strike / s - 1) <= 0.05
        QUALIFY row_number() OVER (PARTITION BY d, sym, expiry, opt_type ORDER BY abs(strike / s - 1), strike) = 1"""
    a = con.execute(q, [start, end]).df()
    for c in ("d", "expiry"):
        a[c] = pd.to_datetime(a[c]).dt.date
    a["dte"] = [(e - d).days for d, e in zip(a["d"], a["expiry"])]
    a["iv"] = iv_vec(a["opt_type"] == "CE", a["close"], a["s"], a["strike"], a["dte"] / 365.0)
    atm = a.groupby(["d", "sym", "expiry", "dte"], as_index=False)["iv"].mean()
    out = cm30(atm)
    spot = a.groupby(["d", "sym"])["s"].first().rename("s").reset_index()
    return out.merge(spot, on=["d", "sym"], how="left")


def load_vix(con, start, end) -> pd.DataFrame:
    v = con.execute("""SELECT trade_date AS d, close / 100.0 AS iv30 FROM bc.index_eod
                       WHERE index_name = 'India VIX' AND trade_date BETWEEN ? AND ? AND close > 0""",
                    [start, end]).df()
    n = con.execute("""SELECT trade_date AS d, close AS s FROM bc.index_eod
                       WHERE index_name = 'Nifty 50' AND trade_date BETWEEN ? AND ? AND close > 0""",
                    [start, end]).df()
    v = v.merge(n, on="d", how="left")
    v["d"] = pd.to_datetime(v["d"]).dt.date
    v["sym"] = "INDIAVIX"
    return v


# ---------------------------------------------------------------------------
# event windows
# ---------------------------------------------------------------------------

def rule_events(cal: list) -> pd.DataFrame:
    """Market-wide index-review dates by rule (no history file needed): MSCI quarterly reviews are
    implemented at the close of the last business day of Feb / May / Aug / Nov; NSE's semi-annual
    reviews take effect after the last trading day of Mar / Sep (passive flows at that close)."""
    s = pd.Series(cal, index=pd.to_datetime(cal))
    last = s.groupby([s.index.year, s.index.month]).max()
    rows = []
    for (y, m), d in last.items():
        if m in (2, 5, 8, 11):
            rows.append((d, "MSCI_REVIEW", "MSCI quarterly review (rule: last business day)"))
        if m in (3, 9):
            rows.append((d, "NSE_REVIEW", "NSE semi-annual index review (rule: last trading day)"))
    return pd.DataFrame(rows, columns=["event_date", "event_type", "description"])


def index_change_events(con, start, end) -> pd.DataFrame:
    """Stock-level NIFTY / BANKNIFTY / FINNIFTY adds and deletes from index_constituents_hist:
    day 0 = the first month-end list that shows the change (about the effective date for the
    March / September reviews; approximate for mid-month changes). Renames and demerger
    placeholders (DUMMY...) are not counted."""
    if not wb._has_main_table(con, "index_constituents_hist"):
        return pd.DataFrame(columns=["sym", "event_date", "event_type"])
    h = con.execute("SELECT index_name, as_of, symbol FROM index_constituents_hist").df()
    h["as_of"] = pd.to_datetime(h["as_of"]).dt.date
    canon = lambda s: wb.RENAMES.get(s, s)
    rows = []
    for ix, g in h.groupby("index_name"):
        lists = {d: {canon(s) for s in x["symbol"] if not s.startswith("DUMMY")} for d, x in g.groupby("as_of")}
        ds = sorted(lists)
        for a, b in zip(ds, ds[1:]):
            for s in lists[b] - lists[a]:
                rows.append((s, b, "INDEX_ADD", ix))
            for s in lists[a] - lists[b]:
                rows.append((s, b, "INDEX_DEL", ix))
    e = pd.DataFrame(rows, columns=["sym", "event_date", "event_type", "index_name"])
    e = e[(e["event_date"] >= start) & (e["event_date"] <= end)]
    # a stock leaving one index and joining another on the same list counts once per type
    return e.drop_duplicates(["sym", "event_date", "event_type"])[["sym", "event_date", "event_type"]]


def load_price_actions(con, start, end) -> dict:
    """Ex-dates that re-base the stock price: splits, bonuses, demergers, rights -- NOT ordinary
    dividends (cash_returns_adj.is_ca also flags every dividend: 826 of 954 flagged days for
    universe stocks since 2019 were plain dividends, which would wrongly drop a quarter of
    results events)."""
    types = "('SPLIT', 'BONUS', 'DEMERGER', 'RIGHTS')"
    rows = con.execute(f"""
        SELECT symbol, ex_date FROM bc.corporate_actions WHERE ca_type IN {types} AND ex_date BETWEEN ? AND ?
        UNION SELECT symbol, trade_date FROM bc.cash_returns_adj
        WHERE is_ca AND trade_date BETWEEN ? AND ?
          AND regexp_matches(coalesce(ca_type, ''), 'SPLIT|BONUS|DEMERGER|RIGHTS')""",
                       [start, end, start, end]).fetchall()
    ca: dict = {}
    for s, d in rows:
        ca.setdefault(wb.RENAMES.get(s, s), []).append(pd.Timestamp(d).date())
    return {k: sorted(set(v)) for k, v in ca.items()}


def straddle_pnl(s0, s1, iv0, iv1, days_cal=2):
    """Short 30-day ATM straddle at (s0, iv0), bought back at (s1, iv1) days_cal calendar days later."""
    t0, t1 = 30 / 365.0, (30 - days_cal) / 365.0
    k = s0
    prem = bs_vec(True, s0, k, t0, iv0, 0.0) + bs_vec(False, s0, k, t0, iv0, 0.0)
    back = bs_vec(True, s1, k, t1, iv1, 0.0) + bs_vec(False, s1, k, t1, iv1, 0.0)
    return (prem - back) / prem


def event_rows(series: pd.DataFrame, events: pd.DataFrame, cal: list, ca: Optional[dict] = None,
               skipped: Optional[list] = None) -> tuple[pd.DataFrame, pd.DataFrame]:
    """series: (d, sym, iv30, s); events: (sym, event_date, event_type) -> per-event stats and paths.

    ca: symbol -> corporate-action ex-dates (splits, bonus, demergers, rights). An event with an
    ex-date inside its -10..+10 window is skipped (the unadjusted spot jumps: VEDL's Apr-2026
    demerger showed up as a 100% 'results move'); skipped events are appended to `skipped`."""
    by_sym = {s: g.set_index("d").sort_index() for s, g in series.groupby("sym")}
    ca = ca or {}
    stats, paths = [], []
    for e in events.itertuples(index=False):
        g = by_sym.get(e.sym)
        if g is None:
            continue
        i0 = bisect.bisect_left(cal, e.event_date)                  # day 0: the event, or next session
        if i0 >= len(cal) or i0 - WIN < 0 or i0 + WIN >= len(cal):
            continue
        days = {k: cal[i0 + k] for k in range(-WIN, WIN + 1)}
        if any(days[-WIN] <= x <= days[WIN] for x in ca.get(e.sym, [])):
            if skipped is not None:
                skipped.append((e.sym, e.event_type, e.event_date))
            continue
        iv = {k: g["iv30"].get(d) for k, d in days.items()}
        if any(iv.get(k) is None or not iv[k] == iv[k] for k in (-WIN, -1, 1)):
            continue
        s_m1, s_p1 = g["s"].get(days[-1]), g["s"].get(days[1])
        mv = abs(np.log(s_p1 / s_m1)) if (s_m1 and s_p1 and s_m1 > 0 and s_p1 > 0) else np.nan
        cal_days = (days[1] - days[-1]).days
        stats.append(dict(sym=e.sym, event_type=e.event_type, event_date=e.event_date, day0=days[0],
                          iv_m10=iv[-WIN], iv_m1=iv[-1], iv_p1=iv[1], iv_p5=iv.get(5),
                          runup=iv[-1] / iv[-WIN] - 1, crush=iv[1] / iv[-1] - 1,
                          move=mv, move_ratio=mv / (iv[-1] * np.sqrt(2 / 252)),
                          straddle=float(straddle_pnl(s_m1, s_p1, iv[-1], iv[1], cal_days))
                          if mv == mv else np.nan))
        for k, v in iv.items():
            if v is not None and v == v:
                paths.append(dict(sym=e.sym, event_type=e.event_type, event_date=e.event_date, k=k,
                                  rel=v / iv[-WIN]))
    return pd.DataFrame(stats), pd.DataFrame(paths)


def baseline(series: pd.DataFrame, cal: list, exclude: set) -> pd.DataFrame:
    """The same 2-session short straddle on every non-event day: (sym, d, straddle)."""
    calset = set(cal)
    parts = []
    for sym, g in series.groupby("sym"):
        g = g[g["d"].isin(calset)].sort_values("d")
        nx = g.shift(-2)                                   # the session two rows later (both traded)
        b = pd.DataFrame({"sym": sym, "d": g["d"].values, "s0": g["s"].values, "s1": nx["s"].values,
                          "iv0": g["iv30"].values, "iv1": nx["iv30"].values,
                          "cd": [(y - x).days if isinstance(y, date) else np.nan for x, y in zip(g["d"], nx["d"])]})
        b = b[[(sym, x) not in exclude for x in b["d"]]]
        parts.append(b.dropna())
    b = pd.concat(parts, ignore_index=True)
    b = b[(b["s0"] > 0) & (b["s1"] > 0)]
    b["straddle"] = _straddle_vec(b["s0"].to_numpy(), b["s1"].to_numpy(), b["iv0"].to_numpy(),
                                  b["iv1"].to_numpy(), b["cd"].to_numpy())
    return b[["sym", "d", "straddle"]]


def _straddle_vec(s0, s1, iv0, iv1, cd):
    t0, t1 = 30 / 365.0, (30 - cd) / 365.0
    prem = bs_vec(True, s0, s0, t0, iv0, 0.0) + bs_vec(False, s0, s0, t0, iv0, 0.0)
    back = bs_vec(True, s1, s0, t1, iv1, 0.0) + bs_vec(False, s1, s0, t1, iv1, 0.0)
    return (prem - back) / prem


def summarize(ev: pd.DataFrame, by: list, min_n: int = 1) -> pd.DataFrame:
    g = ev.groupby(by)
    out = pd.DataFrame({
        "events": g.size(),
        "runup_med%": (g["runup"].median() * 100).round(1),
        "crush_med%": (g["crush"].median() * 100).round(1),
        "crush<0 share": g["crush"].apply(lambda z: (z < 0).mean()).round(2),
        "move_ratio_med": g["move_ratio"].median().round(2),
        "moved>implied share": g["move_ratio"].apply(lambda z: (z > 1).mean()).round(2),
        "straddle_mean%": (g["straddle"].mean() * 100).round(1),
        "straddle_med%": (g["straddle"].median() * 100).round(1),
        "straddle_win": g["straddle"].apply(lambda z: (z > 0).mean()).round(2),
    })
    return out[out["events"] >= min_n]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    default=date(2019, 10, 1))
    ap.add_argument("--to", dest="end", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=date.today())
    ap.add_argument("--macro", type=Path, default=BASE_DIR / "data" / "macro_events.csv")
    ap.add_argument("--split", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(), default=date(2023, 4, 1),
                    help="early / late halves for the persistence check")
    ap.add_argument("--out", type=Path, default=wb.WHEEL_DB.parent)
    a = ap.parse_args(argv)
    return run(a.start, a.end, a.macro, a.split, a.out)


def run(start, end, macro_path, split, out, con=None) -> int:
    own = con is None
    con = con or wb.connect()
    try:
        cal = [r[0] for r in con.execute("SELECT DISTINCT trade_date FROM bc.span_ingest_log "
                                         "WHERE trade_date BETWEEN ? AND ? ORDER BY 1", [start, end]).fetchall()]
        print(f"  {len(cal)} sessions; building 30-day ATM IV ...")
        t0 = time.time()
        iv = load_iv(con, start, end)
        print(f"  ... IV built ({time.time() - t0:.0f}s)")
        vix = load_vix(con, start, end)
        ev_all = wb.load_events_full(con, start, end + pd.Timedelta(days=30).to_pytimedelta())
        idx_ev = index_change_events(con, start, end)
        ca = load_price_actions(con, start - pd.Timedelta(days=30).to_pytimedelta(),
                                end + pd.Timedelta(days=30).to_pytimedelta())
    finally:
        if own:
            con.close()
    stocks = iv[iv["sym"] != "NIFTY"]
    print(f"  IV series: {stocks['sym'].nunique()} stocks, {len(stocks):,} stock-days; NIFTY {int((iv.sym=='NIFTY').sum())} days")

    # results events: every universe stock's results dates
    syms = set(stocks["sym"])                          # (was rebuilt per element: ~10 min on the full history)
    res = [(s, d, "RESULTS") for s, ds in ev_all["results"].items() if s in syms for d in ds]
    res = pd.DataFrame(res, columns=["sym", "event_date", "event_type"])
    res = res[(res["event_date"] >= start) & (res["event_date"] <= end)]
    t0 = time.time()
    skipped: list = []
    st_res, pa_res = event_rows(stocks, res, cal, ca, skipped)
    print(f"  results events: {len(st_res):,} of {len(res):,} usable; {len(skipped)} skipped for a corporate "
          f"action in the window ({time.time() - t0:.0f}s)")
    st_res["sector"] = st_res["sym"].map(wb.SECTOR).fillna("OTHER")

    macro = pd.read_csv(macro_path, comment="#", parse_dates=["event_date"])
    macro["event_date"] = macro["event_date"].dt.date
    macro = macro[(macro["event_date"] >= start) & (macro["event_date"] <= end)]
    macro = pd.concat([macro, rule_events(cal)], ignore_index=True)
    mrows = []
    for sym in ["INDIAVIX", "NIFTY"] + sorted(stocks["sym"].unique()):
        mrows.append(macro.assign(sym=sym)[["sym", "event_date", "event_type"]])
    mac = pd.concat(mrows, ignore_index=True)
    t0 = time.time()
    st_mac, pa_mac = event_rows(pd.concat([vix, iv], ignore_index=True), mac, cal, ca, skipped)
    print(f"  macro events x symbols: {len(st_mac):,} ({time.time() - t0:.0f}s)")
    st_idx, pa_idx = event_rows(stocks, idx_ev, cal, ca, skipped)
    st_idx["sector"] = st_idx["sym"].map(wb.SECTOR).fillna("OTHER")
    print(f"  index adds / deletes: {len(st_idx)} of {len(idx_ev)} usable")
    st_mac["sector"] = st_mac["sym"].map(wb.SECTOR).fillna(st_mac["sym"])

    # baseline: the same trade on days that are not within 2 sessions of any event for that symbol
    excl = set()
    for e in pd.concat([res, mac, idx_ev]).itertuples(index=False):
        i0 = bisect.bisect_left(cal, e.event_date)
        for k in range(-3, 3):
            if 0 <= i0 + k < len(cal):
                excl.add((e.sym, cal[i0 + k]))
    for sym, ds in ca.items():                         # corporate-action jumps are not 'normal days' either
        for x in ds:
            i0 = bisect.bisect_left(cal, x)
            for k in range(-3, 2):
                if 0 <= i0 + k < len(cal):
                    excl.add((sym, cal[i0 + k]))
    t0 = time.time()
    base = baseline(stocks, cal, excl)
    print(f"  baseline non-event trades: {len(base):,} ({time.time() - t0:.0f}s)")

    out.mkdir(parents=True, exist_ok=True)
    allev = pd.concat([st_res, st_mac, st_idx], ignore_index=True)
    allev.to_csv(out / "iv_events_events.csv", index=False)
    pd.concat([pa_res, pa_mac, pa_idx], ignore_index=True).to_csv(out / "iv_events_paths.csv", index=False)
    pd.DataFrame(skipped, columns=["sym", "event_type", "event_date"]).to_csv(out / "iv_events_skipped_ca.csv", index=False)

    lines = []
    def emit(title, df):
        lines.append(f"\n== {title}\n" + (df.to_string() if len(df) else "(none)"))
    with pd.option_context("display.width", 220, "display.max_rows", 400):
        lines.append(f"baseline (non-event days, stocks): 2-session short ATM straddle mean "
                     f"{base['straddle'].mean()*100:.1f}% of premium, win {(base['straddle']>0).mean():.2f}, n={len(base):,}")
        path = pd.concat([pa_res.assign(grp="RESULTS (stocks)"), pa_idx.assign(grp=lambda z: z.event_type + " (stocks)"),
                          pa_mac[pa_mac.sym.isin(["INDIAVIX", "NIFTY"])].assign(grp=lambda z: z.event_type + " " + z.sym),
                          pa_mac[~pa_mac.sym.isin(["INDIAVIX", "NIFTY"])].assign(grp=lambda z: z.event_type + " (stocks)")])
        emit("average 30-day IV path, relative to day -10 (median)",
             (path.groupby(["grp", "k"])["rel"].median().unstack("k")[[-10, -5, -3, -1, 0, 1, 2, 5, 10]] * 100 - 100).round(1))
        emit("results: all stocks", summarize(st_res, ["event_type"]))
        emit("results by sector", summarize(st_res, ["sector"]))
        emit("results by symbol (>= 12 events), sorted by straddle P&L",
             summarize(st_res, ["sym"], 12).sort_values("straddle_mean%"))
        emit("index adds / deletes (stocks)", summarize(st_idx, ["event_type"]))
        emit("macro events: NIFTY options and India VIX", summarize(st_mac[st_mac.sym.isin(["INDIAVIX", "NIFTY"])],
                                                                  ["event_type", "sym"]))
        emit("macro events: stocks, by sector", summarize(st_mac[~st_mac.sym.isin(["INDIAVIX", "NIFTY"])],
                                                          ["event_type", "sector"]))
        emit("each Budget / election: NIFTY", st_mac[(st_mac.sym == "NIFTY") & st_mac.event_type.isin(["BUDGET", "ELECTION"])]
             [["event_type", "event_date", "runup", "crush", "move", "move_ratio", "straddle"]].round(3).set_index("event_date"))
        # persistence of symbol-level results behaviour
        st_res["half"] = np.where(st_res["event_date"] < split, "early", "late")
        per = st_res.groupby(["sym", "half"])[["crush", "move_ratio", "straddle"]].median().unstack("half")
        per = per[per.notna().all(axis=1)]
        if len(per) > 5:
            lines.append("\n== does a symbol's results behaviour persist? rank corr early vs late halves "
                         f"(split {split}, n={len(per)} symbols)")
            for m in ("crush", "move_ratio", "straddle"):
                lines.append(f"  {m:11s} {per[(m, 'early')].rank().corr(per[(m, 'late')].rank()):.2f}")
    txt = "\n".join(lines)
    print(txt)
    (out / "iv_events_summary.txt").write_text(txt, encoding="utf-8")
    print(f"\n  wrote {out / 'iv_events_events.csv'}, iv_events_paths.csv, iv_events_summary.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
