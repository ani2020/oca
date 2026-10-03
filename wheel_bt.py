"""
wheel_bt -- short-premium (Option A) / wheel backtest on index-constituent stocks.

Spec: "Wheel / Premium-Selling Backtest Spec" (Claude Doc). Phase 1 = Option A:
short OTM puts and naked calls, always closed before expiry, sized by exact
exchange SPAN margin.

Data (read-only)
----------------
  oc.duckdb        index_constituents (universe)
  bhavcopy.duckdb  options_eod (entry price, traded close, lot)
                   span_contract / span_und / span_ingest_log (margin, delta,
                       exchange price for every contract every day)
                   cash_returns_adj (CA-adjusted daily move, is_ca)
                   corporate_actions, corporate_events (CA / results blackouts)

Two SQL passes, then a pure-python day loop:
  1. candidates: per (day, symbol, side) the furthest-OTM TRADED strike with
     ROM >= rom_target and |delta| <= delta_cap, for the monthly contract with
     dte_lo..dte_hi calendar days left. ROM = premium*lot / margin,
     margin = SPAN short_scan*lot*broker_mult + ELM*underlying*lot.
  2. paths: daily rows for only the chosen contracts, built on span_contract
     (an exchange price for every contract every day, traded or not), with
     the bhavcopy close/volume joined where it traded.

Prices: NSE's official SETTLE price everywhere (= the SPAN file price;
last-30-minute VWAP when traded, theoretical otherwise), never bhavcopy
`close`. `close` is the last trade, and on thin strikes it is routinely
off-market: RELIANCE Feb-25 1060 PE printed 1.95 on a 1-contract trade
against a settle of 0.40. Using it inflated entry ROM and fired spurious
stops. Settle also matches the spec's entry window (last 30 minutes).
Entry additionally needs min_entry_volume contracts traded that day.

Corporate actions: blackout. No entry if a CA ex-date falls inside the
contract's life; a held position closes at the close before the ex-date.
NSE's own adjustments are inconsistent across CA types (see spec).
Renames are bridged: TATAMOTORS->TMPV, ZOMATO->ETERNAL, SRTRANSFIN->SHRIRAMFIN.

The day loop is step(), shared verbatim with the paper trader (wheel_paper.py):
the backtest feeds it settle prices with an assumed slippage, the paper trader
feeds it live closing quotes and fills at bid/ask.

Usage
-----
    python wheel_bt.py run                                   # spec defaults
    python wheel_bt.py run --stop-mult 3 --entry-mode rank_move
    python wheel_bt.py grid --grid stops                     # stop sweep, one data load
    python wheel_bt.py grid --grid stops --from 2023-01-01   # complete-data years only
    python wheel_bt.py grid --grid rom --entry-mode all --max-new 4
    python wheel_bt.py grid --grid sizing --entry-mode all --max-new 4
    python wheel_bt.py show                                  # last runs, side by side
    python wheel_bt.py report --run latest                   # per-stock / per-year / persistence
    python wheel_bt.py split --split 2023-01-01 --entry-mode all --stop-mult 3   # IS vs OOS

  Grid variants are applied ON TOP of the CLI base, so set the base with
  --entry-mode / --max-new / --stop-* etc. and the grid sweeps one dimension.

Sizing
------
  fixed : 1 lot per trade (default).
  pct   : lots = floor(trade_margin_pct x capacity / margin per lot), capped at
          max_lots_per_trade and at max_vol_frac x contracts traded that day.
          A trade whose single lot exceeds the per-trade cap is skipped, not
          forced to 1 lot. util_cap and sector_cap still bound the total.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd

BHAV_DB = Path(os.environ.get("BHAV_DB", r"C:\Users\aniru\od\bhavcopy.duckdb"))
OC_DB = Path(os.environ.get("OC_DB", r"C:\Users\aniru\od\oc.duckdb"))
WHEEL_DB = Path(os.environ.get("WHEEL_DB", r"C:\Users\aniru\od\wheel_bt.duckdb"))

RENAMES = {"TATAMOTORS": "TMPV", "ZOMATO": "ETERNAL", "SRTRANSFIN": "SHRIRAMFIN"}
CA_TYPES = ("SPLIT", "BONUS", "DEMERGER", "RIGHTS")

# Static sector map for the sector cap (no sector data in either DB). Edit freely;
# unmapped symbols fall into OTHER and are listed at load time.
SECTOR = {
    **dict.fromkeys(["HDFCBANK", "ICICIBANK", "SBIN", "KOTAKBANK", "AXISBANK", "INDUSINDBK",
                     "BANKBARODA", "CANBK", "PNB", "AUBANK", "IDFCFIRSTB", "FEDERALBNK",
                     "UNIONBANK", "YESBANK"], "BANK"),
    **dict.fromkeys(["BAJFINANCE", "BAJAJFINSV", "SHRIRAMFIN", "CHOLAFIN", "MUTHOOTFIN",
                     "LICHSGFIN", "SBICARD", "PFC", "RECLTD", "JIOFIN"], "NBFC"),
    **dict.fromkeys(["HDFCLIFE", "SBILIFE", "ICICIGI", "MFSL", "BSE"], "INS_CAPMKT"),
    **dict.fromkeys(["INFY", "TCS", "HCLTECH", "WIPRO", "TECHM"], "IT"),
    **dict.fromkeys(["MARUTI", "M&M", "BAJAJ-AUTO", "EICHERMOT", "TMPV"], "AUTO"),
    **dict.fromkeys(["SUNPHARMA", "DRREDDY", "CIPLA", "APOLLOHOSP", "MAXHEALTH"], "PHARMA_HEALTH"),
    **dict.fromkeys(["HINDUNILVR", "ITC", "NESTLEIND", "TATACONSUM", "ASIANPAINT", "TITAN",
                     "TRENT", "ETERNAL"], "CONSUMER"),
    **dict.fromkeys(["TATASTEEL", "JSWSTEEL", "HINDALCO", "COALINDIA", "ULTRACEMCO", "GRASIM"],
                    "METALS_MATERIALS"),
    **dict.fromkeys(["RELIANCE", "ONGC", "NTPC", "POWERGRID"], "ENERGY_UTIL"),
    **dict.fromkeys(["LT", "BEL", "ADANIENT", "ADANIPORTS", "INDIGO", "BHARTIARTL"], "INDUS_INFRA_TEL"),
}


# ---------------------------------------------------------------------------
# parameters
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Params:
    # window
    start: date = date(2020, 7, 1)
    end: date = date(2026, 9, 30)
    # entry
    dte_lo: int = 30                    # calendar days
    dte_hi: int = 35
    rom_target: float = 0.02            # cap: furthest-OTM strike with ROM >= this
    delta_cap: float = 0.25             # skip if that strike's |delta| exceeds this
    sides: tuple = ("PE", "CE")
    symbols: Optional[tuple] = None     # whitelist; None = whole universe
    entry_mode: str = "rank_move"       # 'rank_move' (put red / call green, by |move|) | 'all'
    move_cap: Optional[float] = 0.04    # skip names that moved more than this today
    max_new_per_day: int = 2
    max_lots_per_symbol_side: int = 1
    min_entry_volume: int = 5           # contracts traded on the entry day (liquidity gate)
    earn_entry_days: int = 5            # no entry if results within N trading days
    # exits (first to fire wins, checked in this order)
    earn_exit_lead: int = 1             # close N trading days before results date
    stop_mult: Optional[float] = 1.5    # settle >= mult x entry premium
    stop_requires_trade: bool = False   # only fire stop_mult on days the contract traded
    stop_spot_cross: bool = False       # spot through strike
    stop_delta: Optional[float] = None  # |delta| above this
    profit_residual: Optional[float] = None   # close when mark <= this x entry premium
    time_exit_td: int = 5               # at <= N trading days left, close if |delta| > near_money
    near_money_delta: float = 0.15
    final_exit_td: int = 2              # at <= N trading days left, close everything
    # portfolio
    cash: float = 25_00_000
    collateral: float = 25_00_000       # margin value of pledged securities
    util_cap: float = 0.50              # used margin <= util_cap x capacity
    sector_cap: float = 0.30            # sector margin <= sector_cap x (util_cap x capacity)
    compound: bool = True
    sizing: str = "fixed"               # 'fixed' (1 lot) | 'pct' (see module doc)
    trade_margin_pct: float = 0.05      # pct sizing: margin per trade <= this x capacity
    max_lots_per_trade: int = 20
    max_vol_frac: float = 0.20          # pct sizing: lots <= this x contracts traded on entry day
    # margin + costs
    elm: float = 0.035
    broker_mult: float = 1.0
    slippage: float = 0.10              # fraction of premium, each side
    untraded_slip: float = 0.10         # extra when exiting on a day the contract did not trade
    brokerage: float = 20.0             # Rs per order

    def key(self) -> str:
        d = {k: (str(v) if isinstance(v, date) else v) for k, v in asdict(self).items()}
        return hashlib.sha1(json.dumps(d, sort_keys=True, default=str).encode()).hexdigest()[:10]


def params_to_json(p: Params) -> str:
    return json.dumps(asdict(p), default=str)


def params_from_json(text: str) -> Params:
    d = json.loads(text)
    for k in ("start", "end"):
        if isinstance(d.get(k), str):
            d[k] = datetime.strptime(d[k], "%Y-%m-%d").date()
    for k in ("sides", "symbols"):
        if isinstance(d.get(k), list):
            d[k] = tuple(d[k])
    known = set(Params.__dataclass_fields__)
    return Params(**{k: v for k, v in d.items() if k in known})


def data_key(p: Params) -> tuple:
    """Fields that change the SQL pull; everything else is replayed on one load."""
    return (p.start, p.end, p.dte_lo, p.dte_hi, p.rom_target, p.delta_cap, p.elm, p.broker_mult,
            p.min_entry_volume)


# ---------------------------------------------------------------------------
# costs (date-aware)
# ---------------------------------------------------------------------------

def stt_sell(d: date) -> float:            # options, on premium, sell side
    if d < date(2023, 4, 1):
        return 0.0005
    if d < date(2024, 10, 1):
        return 0.000625
    return 0.001


def txn_rate(d: date) -> float:            # NSE options transaction charge, on premium
    return 0.00053 if d < date(2024, 10, 1) else 0.0003503


def order_costs(d: date, premium_value: float, side: str, brokerage: float) -> float:
    """All statutory + broker costs for one option order (Rs). side 'S' or 'B'."""
    txn = txn_rate(d) * premium_value
    sebi = premium_value * 10 / 1e7
    gst = 0.18 * (brokerage + txn + sebi)
    stt = stt_sell(d) * premium_value if side == "S" else 0.0
    stamp = 0.00003 * premium_value if side == "B" else 0.0
    return brokerage + txn + sebi + gst + stt + stamp


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------

@dataclass
class Data:
    calendar: list                       # trading days, sorted
    cands: pd.DataFrame                  # one row per (day, symbol, side) candidate
    paths: dict                          # contract key -> {date: row tuple}
    results: dict                        # canon symbol -> sorted list of results dates
    ca: dict                             # canon symbol -> sorted list of CA ex-dates
    sector: dict


def _alias_sql() -> str:
    return "(VALUES " + ", ".join(f"('{a}', '{b}')" for a, b in RENAMES.items()) + ") AS a(raw, canon)"


BEST_TAG = """
    SELECT trade_date, arg_max(tag, CASE WHEN tag = 's' THEN 9 ELSE TRY_CAST(right(tag, 1) AS INT) END) AS tag
    FROM bc.span_ingest_log GROUP BY 1
"""


def connect(oc_db: Path = OC_DB, bhav_db: Path = BHAV_DB):
    import duckdb
    con = duckdb.connect(str(oc_db), read_only=True)
    con.execute(f"ATTACH '{bhav_db}' AS bc (READ_ONLY)")
    return con


def load_events(con, start: date, end: date) -> tuple[dict, dict]:
    """Results dates and corporate-action ex-dates per CANONICAL symbol, sorted."""
    results: dict = {}
    for sym, d in con.execute("""
            SELECT DISTINCT symbol, event_date FROM bc.corporate_events
            WHERE is_results AND event_date BETWEEN ? AND ?""", [start, end]).fetchall():
        results.setdefault(RENAMES.get(sym, sym), []).append(_d(d))
    ca: dict = {}
    for sym, d in con.execute(f"""
            SELECT symbol, ex_date FROM bc.corporate_actions
            WHERE ca_type IN {CA_TYPES} AND ex_date BETWEEN ? AND ?
            UNION SELECT symbol, trade_date FROM bc.cash_returns_adj
            WHERE is_ca AND trade_date BETWEEN ? AND ?""", [start, end, start, end]).fetchall():
        ca.setdefault(RENAMES.get(sym, sym), []).append(_d(d))
    for dct in (results, ca):
        for k in dct:
            dct[k] = sorted(set(dct[k]))
    return results, ca


def load_data(p: Params, con=None) -> Data:
    own = con is None
    con = con or connect()
    try:
        cal = [r[0] for r in con.execute(
            "SELECT DISTINCT trade_date FROM bc.span_ingest_log WHERE trade_date BETWEEN ? AND ? ORDER BY 1",
            [p.start, p.end]).fetchall()]

        cands = con.execute(f"""
            WITH u AS (SELECT DISTINCT symbol AS raw, symbol AS canon FROM index_constituents
                       UNION ALL SELECT raw, canon FROM {_alias_sql()}),
            bt AS ({BEST_TAG}),
            o AS (SELECT o.trade_date, u.canon AS symbol, o.symbol AS raw, o.expiry, o.opt_type,
                         o.strike, coalesce(nullif(o.settle, 0), o.close) AS close, o.volume, o.oi, o.lot
                  FROM bc.options_eod o JOIN u ON o.symbol = u.raw
                  WHERE o.trade_date BETWEEN ? AND ?
                    AND o.expiry - o.trade_date BETWEEN ? AND ?
                    AND o.volume >= ? AND coalesce(nullif(o.settle, 0), o.close) > 0 AND o.lot > 0),
            j AS (SELECT o.*, s.short_scan, s.delta, su.price AS und
                  FROM o JOIN bt ON bt.trade_date = o.trade_date
                  JOIN bc.span_contract s ON s.trade_date = o.trade_date AND s.tag = bt.tag
                       AND s.symbol = o.raw AND s.instr = o.opt_type AND s.expiry = o.expiry AND s.strike = o.strike
                  JOIN bc.span_und su ON su.trade_date = o.trade_date AND su.tag = bt.tag AND su.symbol = o.raw
                  WHERE (o.opt_type = 'PE' AND o.strike < su.price) OR (o.opt_type = 'CE' AND o.strike > su.price)),
            m AS (SELECT *, short_scan * lot * ? + ? * und * lot AS margin FROM j),
            r AS (SELECT *, close * lot / margin AS rom FROM m WHERE margin > 0),
            pk AS (SELECT trade_date, symbol, opt_type,
                          arg_min(raw, abs(delta)) raw, arg_min(expiry, abs(delta)) expiry,
                          arg_min(strike, abs(delta)) strike, arg_min(close, abs(delta)) premium,
                          arg_min(lot, abs(delta)) lot, arg_min(margin, abs(delta)) margin,
                          arg_min(rom, abs(delta)) rom, min(abs(delta)) delta, arg_min(und, abs(delta)) und,
                          arg_min(oi, abs(delta)) oi, arg_min(volume, abs(delta)) volume
                   FROM r WHERE rom >= ? GROUP BY ALL)
            SELECT pk.*, exp(ra.log_ret) - 1 AS move
            FROM pk LEFT JOIN bc.cash_returns_adj ra ON ra.symbol = pk.raw AND ra.trade_date = pk.trade_date
            WHERE pk.delta <= ?
            ORDER BY pk.trade_date, pk.symbol, pk.opt_type
        """, [p.start, p.end, p.dte_lo, p.dte_hi, max(p.min_entry_volume, 1), p.broker_mult, p.elm,
              p.rom_target, p.delta_cap]).df()
        for c in ("trade_date", "expiry"):
            cands[c] = pd.to_datetime(cands[c]).dt.date

        keys = cands[["raw", "opt_type", "expiry", "strike"]].drop_duplicates()
        first = cands.groupby(["raw", "opt_type", "expiry", "strike"], as_index=False)["trade_date"].min()
        keys = keys.merge(first, on=["raw", "opt_type", "expiry", "strike"])
        con.register("_k", keys)
        pth = con.execute(f"""
            WITH bt AS ({BEST_TAG})
            SELECT s.symbol AS raw, s.instr AS opt_type, s.expiry, s.strike, s.trade_date,
                   o.close, coalesce(o.volume, 0) AS volume, s.price AS sprice, s.delta AS sdelta,
                   s.short_scan, su.price AS und
            FROM _k k
            JOIN bc.span_contract s ON s.symbol = k.raw AND s.instr = k.opt_type AND s.expiry = k.expiry
                 AND s.strike = k.strike AND s.trade_date >= k.trade_date AND s.trade_date <= k.expiry
            JOIN bt ON bt.trade_date = s.trade_date AND bt.tag = s.tag
            LEFT JOIN bc.span_und su ON su.trade_date = s.trade_date AND su.tag = s.tag AND su.symbol = s.symbol
            LEFT JOIN bc.options_eod o ON o.trade_date = s.trade_date AND o.symbol = s.symbol
                 AND o.expiry = s.expiry AND o.strike = s.strike AND o.opt_type = s.instr
        """).df()
        con.unregister("_k")
        paths: dict = {}
        for r in pth.itertuples(index=False):
            k = (r.raw, r.opt_type, _d(r.expiry), float(r.strike))
            paths.setdefault(k, {})[_d(r.trade_date)] = (
                _f(r.close), float(r.volume), _f(r.sprice), _f(r.sdelta), _f(r.short_scan), _f(r.und))

        results, ca = load_events(con, p.start, p.end + timedelta(days=60))
    finally:
        if own:
            con.close()

    unmapped = sorted(set(cands["symbol"]) - set(SECTOR))
    if unmapped:
        print(f"  note: no sector for {unmapped} -> OTHER")
    print(f"  loaded: {len(cal)} days, {len(cands):,} candidates, {len(paths):,} contract paths, "
          f"{sum(map(len, paths.values())):,} path rows")
    return Data(cal, cands, paths, results, ca, dict(SECTOR))


def _d(v) -> date:
    return v if isinstance(v, date) and not isinstance(v, datetime) else pd.Timestamp(v).date()


def _f(v) -> float:
    try:
        x = float(v)
        return x if x == x else float("nan")
    except (TypeError, ValueError):
        return float("nan")


# ---------------------------------------------------------------------------
# simulation
# ---------------------------------------------------------------------------

@dataclass
class Pos:
    pid: int
    symbol: str
    raw: str
    side: str
    expiry: date
    strike: float
    lot: int                 # quantity in UNITS (lot size x lots)
    entry_date: date
    entry_px: float          # settle price at entry (before slippage)
    credit: float            # per share, net of slippage
    entry_cost: float        # Rs
    margin: float
    sector: str
    mark: float = float("nan")
    delta: float = float("nan")
    und: float = float("nan")
    lots: int = 1

    @property
    def key(self) -> tuple:
        return (self.raw, self.side, self.expiry, float(self.strike))


@dataclass
class Result:
    params: Params
    trades: pd.DataFrame
    equity: pd.DataFrame
    metrics: dict


def _next_on_or_after(dates: list, d: date) -> Optional[date]:
    """First event dated d or later (an event today must still be acted on)."""
    i = bisect_left(dates, d)
    return dates[i] if i < len(dates) else None


@dataclass
class State:
    """Everything that carries from one session to the next. Persisted by the paper trader."""
    cash: float
    book: list = field(default_factory=list)      # open Pos
    pid: int = 0


@dataclass
class Ctx:
    """Static per-run context the step needs besides today's prices."""
    calendar: list                       # trading days, sorted; must extend past every open expiry
    results: dict
    ca: dict
    sector: dict


# Row tuple fed to step() for an open contract on day d:
#   (close, volume, mark, delta, short_scan, und, ask)
#   backtest: mark = settle (SPAN price), ask = NaN
#   paper   : mark = live mid,          ask = live ask (fills at the quote)
NAN = float("nan")


def step(p: Params, st: State, d: date, ctx: Ctx, row_of, cands: Optional[pd.DataFrame],
         fill_mode: str = "slippage") -> tuple[list, list, dict]:
    """Advance the book by one session: mark + exits, then entries, then record.

    row_of(key) -> row tuple or None. cands: today's candidates (one row per
    symbol/side, as load_data builds them; an optional 'bid' column is used
    for quote fills). Returns (closed trade dicts, newly opened Pos, equity row).
    Shared verbatim by the backtest (simulate) and the paper trader.
    """
    cal = ctx.calendar

    def td_between(a: date, b: date) -> int:
        """Trading days strictly after a up to and including b."""
        return bisect_right(cal, b) - bisect_right(cal, a)

    closed: list = []
    opened: list = []

    def close_pos(ps: Pos, row, reason: str):
        traded = row is not None and row[1] > 0
        px = ps.mark
        if reason == "expiry":
            intrinsic = max(ps.strike - ps.und, 0.0) if ps.side == "PE" else max(ps.und - ps.strike, 0.0)
            fill, cost = intrinsic, 0.0
        else:
            ask = row[6] if row is not None and len(row) > 6 else NAN
            if fill_mode == "quote" and ask == ask and ask > 0:
                fill = ask
            else:
                fill = px * (1 + p.slippage + (0.0 if traded else p.untraded_slip))
            cost = order_costs(d, fill * ps.lot, "B", p.brokerage)
        pnl = (ps.credit - fill) * ps.lot - ps.entry_cost - cost
        st.cash -= fill * ps.lot + cost          # credit and entry cost were booked at entry
        closed.append(dict(
            pid=ps.pid, symbol=ps.symbol, side=ps.side, sector=ps.sector, expiry=ps.expiry,
            strike=ps.strike, lot=ps.lot, lots=ps.lots, entry_date=ps.entry_date, exit_date=d,
            entry_px=ps.entry_px, credit=ps.credit, exit_px=px, exit_fill=fill,
            costs=ps.entry_cost + cost, pnl=pnl, margin=ps.margin,
            rom=pnl / ps.margin if ps.margin else NAN,
            days=(d - ps.entry_date).days, reason=reason, traded_exit=traded))

    # ---- 1. mark + exits --------------------------------------------------
    keep = []
    for ps in st.book:
        row = row_of(ps.key)
        if row is not None:
            close, vol, mark, sdelta, sscan, und = row[:6]
            ps.mark = mark if mark == mark else (close if close == close else ps.mark)
            ps.delta = abs(sdelta) if sdelta == sdelta else ps.delta
            ps.und = und if und == und else ps.und
            if sscan == sscan and ps.und == ps.und:
                ps.margin = sscan * ps.lot * p.broker_mult + p.elm * ps.und * ps.lot
        reason = None
        left = td_between(d, ps.expiry)
        res = _next_on_or_after(ctx.results.get(ps.symbol, []), d)
        cax = _next_on_or_after(ctx.ca.get(ps.symbol, []), d)
        if d >= ps.expiry:
            reason = "expiry"
        elif res is not None and res <= ps.expiry and td_between(d, res) <= p.earn_exit_lead:
            reason = "results"          # close the session before results (or today if missed)
        elif cax is not None and cax <= ps.expiry and td_between(d, cax) <= 1:
            reason = "corp_action"      # close the session before the ex-date
        elif (p.stop_mult and row is not None and ps.mark == ps.mark
              and (row[1] > 0 or not p.stop_requires_trade)
              and ps.mark >= p.stop_mult * ps.entry_px):
            reason = "stop_mult"
        elif p.stop_spot_cross and ps.und == ps.und and (
                (ps.side == "PE" and ps.und < ps.strike) or (ps.side == "CE" and ps.und > ps.strike)):
            reason = "stop_spot"
        elif p.stop_delta and ps.delta == ps.delta and ps.delta > p.stop_delta:
            reason = "stop_delta"
        elif p.profit_residual is not None and ps.mark == ps.mark and ps.mark <= p.profit_residual * ps.entry_px:
            reason = "profit"
        elif left <= p.final_exit_td:
            reason = "final_exit"
        elif left <= p.time_exit_td and ps.delta == ps.delta and ps.delta > p.near_money_delta:
            reason = "time_exit"
        if reason:
            close_pos(ps, row, reason)
        else:
            keep.append(ps)
    st.book = keep

    # ---- 2. capacity ------------------------------------------------------
    # Broker ledger view: cash already holds credited premium, so subtract
    # the current buy-back liability (MTM). Without compounding, gains do
    # not add capacity but losses still remove it.
    liab = sum(ps.mark * ps.lot for ps in st.book if ps.mark == ps.mark)
    cash_eff = st.cash - liab
    capacity = p.collateral + (cash_eff if p.compound else min(cash_eff, p.cash))
    used = sum(ps.margin for ps in st.book)

    # ---- 3. entries -------------------------------------------------------
    n_new = 0
    if cands is not None and len(cands):
        rows = cands[cands["opt_type"].isin(p.sides)].copy()
        if p.symbols:
            rows = rows[rows["symbol"].isin(p.symbols)]
        if p.entry_mode == "rank_move":
            mv = rows["move"]
            rows = rows[((rows["opt_type"] == "PE") & (mv < 0)) | ((rows["opt_type"] == "CE") & (mv > 0))]
            if p.move_cap is not None:
                rows = rows[rows["move"].abs() <= p.move_cap]
            rows = rows.assign(_r=rows["move"].abs()).sort_values("_r", ascending=False)
        else:
            if p.move_cap is not None:
                rows = rows[rows["move"].isna() | (rows["move"].abs() <= p.move_cap)]
            rows = rows.sort_values("rom", ascending=False)
        has_bid = "bid" in rows.columns
        for c in rows.itertuples(index=False):
            if n_new >= p.max_new_per_day:
                break
            sym, side, exp = c.symbol, c.opt_type, _d(c.expiry)
            if sum(1 for ps in st.book if ps.symbol == sym and ps.side == side) >= p.max_lots_per_symbol_side:
                continue
            nxt = _next_on_or_after(ctx.results.get(sym, []), d)
            if nxt is not None and td_between(d, nxt) <= p.earn_entry_days:
                continue
            cax = _next_on_or_after(ctx.ca.get(sym, []), d)
            if cax is not None and cax <= exp:
                continue
            m1 = float(c.margin)                         # margin for ONE lot
            if p.sizing == "pct":
                lots = int(p.trade_margin_pct * capacity // m1) if m1 > 0 else 0
                vol_lim = int(p.max_vol_frac * float(c.volume))
                lots = min(lots, p.max_lots_per_trade, max(vol_lim, 1))
                if lots < 1:
                    continue                             # one lot already over the per-trade cap
            else:
                lots = 1
            sec = ctx.sector.get(sym, "OTHER")
            sec_used = sum(ps.margin for ps in st.book if ps.sector == sec)
            room = min(p.util_cap * capacity - used, p.sector_cap * p.util_cap * capacity - sec_used)
            if p.sizing == "pct":
                lots = min(lots, int(room // m1) if m1 > 0 else 0)  # shrink to fit caps
            if lots < 1 or lots * m1 > room:
                continue
            m = m1 * lots
            lot = int(c.lot) * lots
            bid = float(c.bid) if has_bid else NAN
            if fill_mode == "quote" and bid == bid and bid > 0:
                credit = bid
            else:
                credit = float(c.premium) * (1 - p.slippage)
            ecost = order_costs(d, credit * lot, "S", p.brokerage)
            st.cash += credit * lot - ecost
            st.pid += 1
            ps = Pos(st.pid, sym, c.raw, side, exp, float(c.strike), lot, d, float(c.premium),
                     credit, ecost, m, sec, mark=float(c.premium), delta=float(c.delta), und=float(c.und),
                     lots=lots)
            st.book.append(ps)
            opened.append(ps)
            used += m
            n_new += 1

    # ---- 4. record --------------------------------------------------------
    liab = sum(ps.mark * ps.lot for ps in st.book if ps.mark == ps.mark)
    cash_eff = st.cash - liab
    eq = dict(date=d, equity=st.cash + p.collateral - liab, cash=st.cash, liability=liab, used_margin=used,
              capacity=p.collateral + cash_eff, n_open=len(st.book), n_new=n_new,
              breach=used > p.collateral + cash_eff, cash_rule_breach=cash_eff < 0.5 * used)
    return closed, opened, eq


def simulate(p: Params, data: Data) -> Result:
    cal = [d for d in data.calendar if p.start <= d <= p.end]
    ctx = Ctx(data.calendar, data.results, data.ca, data.sector)
    cand_by_day = {d: g for d, g in data.cands.groupby("trade_date")}
    st = State(cash=p.cash)
    trades, eq_rows = [], []
    for d in cal:
        def row_of(k, _d=d):
            return data.paths.get(k, {}).get(_d)
        closed, _, eq = step(p, st, d, ctx, row_of, cand_by_day.get(d))
        trades.extend(closed)
        eq_rows.append(eq)

    # force-close anything left at the end of the window (mark-to-market)
    if st.book and cal:
        last = cal[-1]
        for ps in st.book:
            row = data.paths.get(ps.key, {}).get(last)
            traded = row is not None and row[1] > 0
            fill = ps.mark * (1 + p.slippage + (0.0 if traded else p.untraded_slip))
            cost = order_costs(last, fill * ps.lot, "B", p.brokerage)
            pnl = (ps.credit - fill) * ps.lot - ps.entry_cost - cost
            st.cash -= fill * ps.lot + cost
            trades.append(dict(
                pid=ps.pid, symbol=ps.symbol, side=ps.side, sector=ps.sector, expiry=ps.expiry,
                strike=ps.strike, lot=ps.lot, lots=ps.lots, entry_date=ps.entry_date, exit_date=last,
                entry_px=ps.entry_px, credit=ps.credit, exit_px=ps.mark, exit_fill=fill,
                costs=ps.entry_cost + cost, pnl=pnl, margin=ps.margin,
                rom=pnl / ps.margin if ps.margin else NAN, days=(last - ps.entry_date).days,
                reason="window_end", traded_exit=traded))
        st.book = []
        eq_rows[-1]["equity"] = st.cash + p.collateral

    tr = pd.DataFrame(trades)
    eq = pd.DataFrame(eq_rows)
    return Result(p, tr, eq, compute_metrics(p, tr, eq))


# ---------------------------------------------------------------------------
# metrics
# ---------------------------------------------------------------------------

def compute_metrics(p: Params, tr: pd.DataFrame, eq: pd.DataFrame) -> dict:
    m: dict = {"trades": int(len(tr))}
    if eq.empty:
        return m
    e = eq.set_index(pd.to_datetime(eq["date"]))["equity"]
    years = max((e.index[-1] - e.index[0]).days / 365.25, 1e-9)
    total0 = p.cash + p.collateral
    pnl = e.iloc[-1] - total0
    m["pnl"] = round(pnl)
    m["cagr_total"] = round((e.iloc[-1] / total0) ** (1 / years) - 1, 4)
    m["cagr_on_cash"] = round(((p.cash + pnl) / p.cash) ** (1 / years) - 1, 4) if p.cash + pnl > 0 else -1.0
    dd = e - e.cummax()
    m["max_dd_rs"] = round(dd.min())
    m["max_dd_pct_total"] = round(dd.min() / total0, 4)
    m["max_dd_pct_cash"] = round(dd.min() / p.cash, 4)
    daily = e.diff().dropna()
    if len(daily):
        q = daily.quantile(0.05)
        m["cvar95_daily_rs"] = round(daily[daily <= q].mean())
        m["worst_day_rs"] = round(daily.min())
    monthly = e.resample("ME").last().diff().dropna()
    if len(monthly):
        m["worst_month_rs"] = round(monthly.min())
        m["pct_months_negative"] = round(float((monthly < 0).mean()), 3)
    m["calmar"] = round((pnl / years) / abs(dd.min()), 2) if dd.min() < 0 else None
    m["avg_util"] = round(float((eq["used_margin"] / (p.cash + p.collateral)).mean()), 3)
    m["max_util_of_capacity"] = round(float((eq["used_margin"] / eq["capacity"].clip(lower=1)).max()), 3)
    m["breach_days"] = int(eq["breach"].sum())
    m["cash_rule_breach_days"] = int(eq["cash_rule_breach"].sum())
    if len(tr):
        m["win_rate"] = round(float((tr["pnl"] > 0).mean()), 3)
        m["avg_pnl"] = round(float(tr["pnl"].mean()))
        m["avg_rom_per_trade"] = round(float(tr["rom"].mean()), 4)
        m["p1_trade_rs"] = round(float(tr["pnl"].quantile(0.01)))
        m["worst_trade_rs"] = round(float(tr["pnl"].min()))
        m["avg_days"] = round(float(tr["days"].mean()), 1)
        m["costs_share_of_gross"] = round(float(tr["costs"].sum() / max(tr["credit"].mul(tr["lot"]).sum(), 1)), 3)
        m["exit_mix"] = tr["reason"].value_counts(normalize=True).round(3).to_dict()
        m["pnl_by_side"] = tr.groupby("side")["pnl"].sum().round().to_dict()
        m["pnl_by_year"] = tr.groupby(pd.to_datetime(tr["exit_date"]).dt.year)["pnl"].sum().round().to_dict()
    return m


# ---------------------------------------------------------------------------
# reports: per stock, per period, persistence
# ---------------------------------------------------------------------------

def stock_table(tr: pd.DataFrame) -> pd.DataFrame:
    """One row per stock. P&L is per LOT (pnl / lots) so pct-sized runs compare fairly."""
    if tr.empty:
        return pd.DataFrame()
    t = tr.copy()
    t["lots"] = t["lots"].fillna(1).clip(lower=1) if "lots" in t else 1
    t["pnl1"] = t["pnl"] / t["lots"]
    t["m1"] = t["margin"] / t["lots"]
    t["yr"] = pd.to_datetime(t["exit_date"]).dt.year
    g = t.groupby("symbol")
    out = pd.DataFrame({
        "sector": g["sector"].first(),
        "trades": g.size(),
        "pnl_per_lot": g["pnl1"].sum().round(),
        "win": g["pnl"].apply(lambda x: (x > 0).mean()).round(2),
        "rom_pct": (100 * g["pnl1"].sum() / g["m1"].sum()).round(2),
        "stop_rate": g["reason"].apply(lambda x: x.str.startswith("stop").mean()).round(2),
        "worst": g["pnl1"].min().round(),
        "puts": t[t.side == "PE"].groupby("symbol")["pnl1"].sum().round(),
        "calls": t[t.side == "CE"].groupby("symbol")["pnl1"].sum().round(),
    })
    yearly = t.pivot_table(index="symbol", columns="yr", values="pnl1", aggfunc="sum")
    out["yrs_positive"] = (yearly > 0).sum(axis=1).astype(int).astype(str) + "/" + yearly.notna().sum(axis=1).astype(str)
    out = out.join(yearly.round().add_prefix("y"))
    return out.sort_values("pnl_per_lot", ascending=False)


def persistence(tr: pd.DataFrame, split: date) -> dict:
    """Does a stock's P&L before `split` predict its P&L after? (per lot)"""
    if tr.empty:
        return {}
    t = tr.copy()
    t["pnl1"] = t["pnl"] / (t["lots"].fillna(1).clip(lower=1) if "lots" in t else 1)
    t["half"] = (pd.to_datetime(t["exit_date"]).dt.date >= split).map({False: "before", True: "after"})
    pv = t.pivot_table(index="symbol", columns="half", values="pnl1", aggfunc="sum").dropna()
    if len(pv) < 5 or not {"before", "after"} <= set(pv.columns):
        return {"stocks": len(pv)}
    top = pv["before"].rank(ascending=False) <= max(len(pv) // 4, 1)
    return {
        "stocks": int(len(pv)),
        "pnl_corr": round(float(pv["before"].corr(pv["after"])), 2),
        "rank_corr": round(float(pv["before"].rank().corr(pv["after"].rank())), 2),
        "sign_agreement": round(float(((pv["before"] > 0) == (pv["after"] > 0)).mean()), 2),
        "top_quartile_before_avg_after": round(float(pv.loc[top, "after"].mean())),
        "rest_avg_after": round(float(pv.loc[~top, "after"].mean())),
    }


def print_report(label: str, metrics: dict, tr: pd.DataFrame, top: int = 12, split: Optional[date] = None):
    keys = ["trades", "pnl", "cagr_on_cash", "cagr_total", "max_dd_rs", "max_dd_pct_total", "worst_month_rs",
            "pct_months_negative", "win_rate", "calmar", "avg_util", "breach_days", "cash_rule_breach_days"]
    print(f"\n=== {label} ===")
    print("  " + "  ".join(f"{k}={metrics.get(k)}" for k in keys))
    if tr.empty:
        return
    yr = tr.assign(yr=pd.to_datetime(tr["exit_date"]).dt.year).pivot_table(
        index="yr", columns="side", values="pnl", aggfunc="sum").round()
    yr["total"] = yr.sum(axis=1)
    print("\n  P&L by year and side (Rs):\n  " + yr.to_string().replace("\n", "\n  "))
    st = stock_table(tr)
    cols = ["sector", "trades", "pnl_per_lot", "win", "rom_pct", "stop_rate", "puts", "calls", "yrs_positive", "worst"]
    with pd.option_context("display.width", 200, "display.max_columns", 20):
        print(f"\n  Best {top} stocks (P&L per lot):\n  " + st[cols].head(top).to_string().replace("\n", "\n  "))
        print(f"\n  Worst {top} stocks:\n  " + st[cols].tail(top).to_string().replace("\n", "\n  "))
    if split is None:
        dts = pd.to_datetime(tr["exit_date"])
        split = (dts.min() + (dts.max() - dts.min()) / 2).date()
    print(f"\n  Persistence (before vs after {split}): {persistence(tr, split)}")
    print("  (pnl_corr / rank_corr near 0 and sign_agreement near 0.5 = past stock P&L does not predict future)")


def load_run(run: str = "latest", db: Path = WHEEL_DB) -> tuple[str, str, dict, pd.DataFrame]:
    import duckdb
    con = duckdb.connect(str(db), read_only=True)
    try:
        if run == "latest":
            row = con.execute("SELECT run_id, label, metrics FROM runs ORDER BY created DESC LIMIT 1").fetchone()
        else:
            row = con.execute("SELECT run_id, label, metrics FROM runs WHERE run_id = ? OR label = ? "
                              "ORDER BY created DESC LIMIT 1", [run, run]).fetchone()
        if row is None:
            raise SystemExit(f"no run matching {run!r}")
        tr = con.execute("SELECT * FROM trades WHERE run_id = ?", [row[0]]).df()
    finally:
        con.close()
    return row[0], row[1], json.loads(row[2]), tr


def run_split(base: Params, split: date, label: str = "split") -> dict:
    """Same config, two windows, fresh capital each: settings chosen on one, checked on the other."""
    full = load_data(base)
    out = {}
    for tag, (a, b) in {"IS": (base.start, split - timedelta(days=1)), "OOS": (split, base.end)}.items():
        r = simulate(replace(base, start=a, end=b), full)
        rid = save(r, label=f"{label} {tag} {a}..{b}")
        out[tag] = (rid, r)
        print_report(f"{label} {tag}  {a} .. {b}  ({rid})", r.metrics, r.trades, top=8)
    both = pd.concat([out["IS"][1].trades, out["OOS"][1].trades], ignore_index=True)
    print(f"\n=== {label}: stock persistence IS -> OOS ===\n  {persistence(both, split)}")
    return out


# ---------------------------------------------------------------------------
# persistence + grids
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (run_id VARCHAR PRIMARY KEY, created TIMESTAMP, label VARCHAR,
    params JSON, metrics JSON);
CREATE TABLE IF NOT EXISTS trades (run_id VARCHAR, pid INTEGER, symbol VARCHAR, side VARCHAR, sector VARCHAR,
    expiry DATE, strike DOUBLE, lot INTEGER, lots INTEGER, entry_date DATE, exit_date DATE, entry_px DOUBLE,
    credit DOUBLE, exit_px DOUBLE, exit_fill DOUBLE, costs DOUBLE, pnl DOUBLE, margin DOUBLE,
    rom DOUBLE, days INTEGER, reason VARCHAR, traded_exit BOOLEAN);
CREATE TABLE IF NOT EXISTS equity (run_id VARCHAR, date DATE, equity DOUBLE, cash DOUBLE,
    liability DOUBLE, used_margin DOUBLE, capacity DOUBLE, n_open INTEGER, n_new INTEGER,
    breach BOOLEAN, cash_rule_breach BOOLEAN);
"""


def save(res: Result, label: str = "", db: Path = WHEEL_DB) -> str:
    import duckdb
    run_id = f"{datetime.now():%Y%m%d%H%M%S}-{res.params.key()}"
    con = duckdb.connect(str(db))
    try:
        con.execute(SCHEMA)
        con.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS lots INTEGER")   # pre-sizing DBs
        con.execute("INSERT INTO runs VALUES (?, now(), ?, ?, ?)",
                    [run_id, label, json.dumps(asdict(res.params), default=str),
                     json.dumps(res.metrics, default=str)])
        for name, df in (("trades", res.trades), ("equity", res.equity)):
            if len(df):
                con.register("_x", df.assign(run_id=run_id))
                cols = [r[0] for r in con.execute(f"DESCRIBE {name}").fetchall()]
                con.execute(f"INSERT INTO {name} SELECT {', '.join(cols)} FROM _x")
                con.unregister("_x")
    finally:
        con.close()
    return run_id


GRIDS = {
    # each grid sweeps one dimension on top of the CLI base
    "stops": [dict(stop_mult=None), dict(stop_mult=1.5), dict(stop_mult=2.0), dict(stop_mult=3.0),
              dict(stop_mult=None, stop_spot_cross=True), dict(stop_mult=None, stop_delta=0.30),
              dict(stop_mult=None, stop_delta=0.40), dict(stop_mult=3.0, stop_spot_cross=True)],
    "rom": [dict(rom_target=r) for r in (0.015, 0.02, 0.025, 0.03)],        # reloads data per value
    "delta": [dict(delta_cap=x) for x in (0.15, 0.20, 0.25, 0.30)],           # reloads data per value
    "sides": [dict(sides=("PE", "CE")), dict(sides=("PE",)), dict(sides=("CE",))],
    "dte": [dict(dte_lo=30, dte_hi=35), dict(dte_lo=25, dte_hi=35), dict(dte_lo=21, dte_hi=35)],
    "pace": [dict(max_new_per_day=n) for n in (2, 4, 6, 8)],
    "sizing": [dict(sizing="fixed"), dict(sizing="pct", trade_margin_pct=0.05),
               dict(sizing="pct", trade_margin_pct=0.075), dict(sizing="pct", trade_margin_pct=0.10)],
    "profit": [dict(profit_residual=None), dict(profit_residual=0.10), dict(profit_residual=0.20),
               dict(profit_residual=0.35)],
    "entry": [dict(entry_mode="rank_move"), dict(entry_mode="all"),
              dict(entry_mode="rank_move", move_cap=None), dict(entry_mode="all", max_new_per_day=4)],
    "util": [dict(util_cap=u) for u in (0.3, 0.4, 0.5, 0.6)],
}


def run_grid(base: Params, variants: list, label: str, data: Optional[Data] = None) -> list:
    out, cache = [], {}
    for v in variants:
        p = replace(base, **v)
        k = data_key(p)
        if k not in cache:
            cache[k] = data if (data is not None and k == data_key(base)) else load_data(p)
        r = simulate(p, cache[k])
        rid = save(r, label=f"{label} {v}")
        out.append((v, rid, r.metrics))
        _print_row(v, r.metrics)
    return out


def _print_row(v, m):
    print(f"  {str(v):<52} trades={m.get('trades', 0):5d}  pnl={m.get('pnl', 0):>11,}  "
          f"cagr_cash={m.get('cagr_on_cash', 0):7.2%}  maxDD={m.get('max_dd_pct_cash', 0):7.2%}  "
          f"worstM={m.get('worst_month_rs', 0):>10,}  win={m.get('win_rate', 0):.3f}  "
          f"calmar={m.get('calmar')}")


def add_param_args(ap) -> None:
    """Strategy flags shared by wheel_bt and wheel_paper (all optional; unset = Params default)."""
    ap.add_argument("--from", dest="start", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--to", dest="end", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--rom", type=float)
    ap.add_argument("--delta-cap", type=float)
    ap.add_argument("--entry-mode", choices=["rank_move", "all"])
    ap.add_argument("--max-new", type=int)
    ap.add_argument("--sides", choices=["both", "PE", "CE"])
    ap.add_argument("--symbols", help="comma whitelist, e.g. LICHSGFIN,CIPLA")
    ap.add_argument("--dte-lo", type=int)
    ap.add_argument("--dte-hi", type=int)
    ap.add_argument("--sizing", choices=["fixed", "pct"])
    ap.add_argument("--size-pct", type=float, help="pct sizing: margin per trade as fraction of capacity")
    ap.add_argument("--stop-mult", type=float)
    ap.add_argument("--no-stop-mult", action="store_true")
    ap.add_argument("--spot-cross", action="store_true")
    ap.add_argument("--stop-delta", type=float)
    ap.add_argument("--profit", type=float)
    ap.add_argument("--util", type=float)
    ap.add_argument("--slippage", type=float)
    ap.add_argument("--cash", type=float)
    ap.add_argument("--collateral", type=float)


def params_from_args(a, base: Optional[Params] = None) -> Params:
    kw = {}
    for f, k in (("start", "start"), ("end", "end"), ("rom", "rom_target"), ("delta_cap", "delta_cap"),
                 ("entry_mode", "entry_mode"), ("stop_mult", "stop_mult"), ("stop_delta", "stop_delta"),
                 ("profit", "profit_residual"), ("util", "util_cap"), ("slippage", "slippage"),
                 ("max_new", "max_new_per_day"), ("dte_lo", "dte_lo"), ("dte_hi", "dte_hi"),
                 ("sizing", "sizing"), ("size_pct", "trade_margin_pct"), ("cash", "cash"),
                 ("collateral", "collateral")):
        v = getattr(a, f, None)
        if v is not None:
            kw[k] = v
    if getattr(a, "no_stop_mult", False):
        kw["stop_mult"] = None
    if getattr(a, "spot_cross", False):
        kw["stop_spot_cross"] = True
    if getattr(a, "sides", None):
        kw["sides"] = ("PE", "CE") if a.sides == "both" else (a.sides,)
    if getattr(a, "symbols", None):
        kw["symbols"] = tuple(x.strip().upper() for x in a.symbols.split(",") if x.strip())
    return replace(base or Params(), **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["run", "grid", "show", "report", "split"])
    ap.add_argument("--run", default="latest", help="report: run_id or label (default latest)")
    ap.add_argument("--split", dest="split_date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date(),
                    default=date(2023, 1, 1), help="split: first out-of-sample day")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--grid", choices=sorted(GRIDS), default="stops")
    add_param_args(ap)
    ap.add_argument("--label", default="")
    a = ap.parse_args(argv)

    if a.mode == "report":
        rid, label, metrics, tr = load_run(a.run)
        print_report(f"{label}  ({rid})", metrics, tr, top=a.top)
        return 0

    if a.mode == "show":
        import duckdb
        con = duckdb.connect(str(WHEEL_DB), read_only=True)
        print(con.execute("""
            SELECT run_id, label,
                   metrics->>'trades' trades, metrics->>'pnl' pnl, metrics->>'cagr_on_cash' cagr_cash,
                   metrics->>'max_dd_pct_cash' dd_cash, metrics->>'worst_month_rs' worst_month,
                   metrics->>'win_rate' win, metrics->>'calmar' calmar
            FROM runs ORDER BY created DESC LIMIT 20""").df().to_string(index=False))
        return 0

    base = params_from_args(a)

    if a.mode == "run":
        r = simulate(base, load_data(base))
        rid = save(r, label=a.label or "run")
        print_report(f"{a.label or 'run'}  ({rid})", r.metrics, r.trades, top=a.top)
        return 0
    if a.mode == "split":
        run_split(base, a.split_date, label=a.label or "split")
        return 0
    print(f"  grid '{a.grid}' on base {base.key()}")
    run_grid(base, GRIDS[a.grid], label=a.label or a.grid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
