"""
wheel.py -- WHEEL page: wheel / short-premium backtest runs and paper books.

Reads wheel_bt.duckdb (written by oc/wheel_bt.py runs and the 17:45
oc/wheel_paper.py job), which lives alongside oc.duckdb. Same separate-DB
pattern as rv_forecast.py / vrp.py: nothing here touches oc.duckdb's
market-hours write lock. The trade drill-down additionally reads the
contract's daily SPAN price / delta and the underlying from bhavcopy.duckdb
(config.SPAN_DB), both read-only.

Per-stock tables and persistence stats come from wheel_bt itself
(stock_table / persistence), so this page and `wheel_bt.py report` can never
disagree about the same run.

EXPERIMENTAL: backtest numbers are in-sample unless the run is a `split OOS`
run, and paper books are a 1-2 month mechanics / slippage check, not proof
of edge. The frontend badges say so.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Optional

import duckdb
import json
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..db import to_records, safe_response
from .. import config

import wheel_bt as wb      # oc/ is on sys.path (see rv_forecast.py)

router = APIRouter()

SUMMARY_KEYS = ["trades", "pnl", "cagr_on_cash", "cagr_total", "max_dd_rs", "max_dd_pct_total",
                "max_dd_pct_cash", "worst_month_rs", "pct_months_negative", "win_rate", "calmar",
                "avg_util", "max_util_of_capacity", "breach_days", "cash_rule_breach_days",
                "costs_share_of_gross", "exit_mix", "pnl_by_side"]


def _wheel_db() -> str:
    return str(Path(config.DB_FILE).with_name("wheel_bt.duckdb"))


def _q(sql: str, params: list = (), db: Optional[str] = None) -> pd.DataFrame:
    path = db or _wheel_db()
    if not Path(path).exists():
        raise HTTPException(404, f"{Path(path).name} not found -- run oc/wheel_bt.py first")
    try:
        con = duckdb.connect(path, read_only=True)
    except Exception as exc:  # Windows: the paper job / a backtest save holds the write lock
        raise HTTPException(503, f"{Path(path).name} is busy (being written); retry in a minute: {exc}")
    try:
        return con.execute(sql, list(params)).df()
    except duckdb.CatalogException:
        return pd.DataFrame()            # table not created yet (e.g. no paper books)
    except Exception as exc:
        raise HTTPException(500, str(exc))
    finally:
        con.close()


def _params_changed(params: dict) -> dict:
    """Params that differ from wheel_bt.Params defaults. JSON turns tuples into lists
    and dates into strings, so normalise both sides before comparing."""
    def norm(v):
        return list(v) if isinstance(v, (list, tuple)) else (None if v is None else str(v))
    base = wb.Params()
    return {k: v for k, v in params.items()
            if k in base.__dataclass_fields__ and norm(v) != norm(getattr(base, k))}


def _metrics_summary(raw: str) -> dict:
    try:
        m = json.loads(raw)
    except Exception:
        return {}
    return {k: m.get(k) for k in SUMMARY_KEYS}


# ---------------------------------------------------------------------------
# backtest runs
# ---------------------------------------------------------------------------

@router.get("/api/wheel/runs")
def wheel_runs(limit: int = Query(300)):
    df = _q("SELECT run_id, created, label, params, metrics FROM runs ORDER BY created DESC LIMIT ?", [limit])
    rows = []
    for r in df.itertuples(index=False):
        m = _metrics_summary(r.metrics)
        try:
            p = json.loads(r.params)
        except Exception:
            p = {}
        rows.append({"run_id": r.run_id, "created": str(r.created)[:16], "label": r.label,
                     "start": p.get("start"), "end": p.get("end"),
                     "trades": m.get("trades"), "pnl": m.get("pnl"), "cagr_on_cash": m.get("cagr_on_cash"),
                     "max_dd_pct_total": m.get("max_dd_pct_total"), "calmar": m.get("calmar")})
    return safe_response({"rows": rows})


@router.get("/api/wheel/run/{run_id}")
def wheel_run(run_id: str):
    """Everything the backtest tab needs for one run, in one call."""
    head = _q("SELECT run_id, created, label, params, metrics FROM runs WHERE run_id = ?", [run_id])
    if head.empty:
        raise HTTPException(404, f"run {run_id} not found")
    h = head.iloc[0]
    params = json.loads(h["params"])
    metrics = _metrics_summary(h["metrics"])
    diff = _params_changed(params)

    eq = _q("SELECT date, equity, used_margin, capacity, n_open FROM equity WHERE run_id = ? ORDER BY date", [run_id])
    total0 = float(params.get("cash", 0)) + float(params.get("collateral", 0))
    monthly, curve = [], []
    if not eq.empty:
        eq["date"] = pd.to_datetime(eq["date"])
        eq["dd"] = eq["equity"] - eq["equity"].cummax()
        curve = to_records(eq.assign(date=eq["date"].dt.strftime("%Y-%m-%d"),
                                     pnl=eq["equity"] - total0)[["date", "pnl", "dd", "used_margin", "n_open"]])
        mth = eq.set_index("date")["equity"].resample("ME").last()
        first = pd.Series([total0], index=[mth.index[0] - pd.offsets.MonthEnd(1)])
        mp = pd.concat([first, mth]).diff().dropna()
        monthly = [{"year": int(i.year), "month": int(i.month), "pnl": round(float(v))} for i, v in mp.items()]

    tr = _q("SELECT * FROM trades WHERE run_id = ?", [run_id])
    by_year_side, stocks, persist = [], [], {}
    if not tr.empty:
        tr["yr"] = pd.to_datetime(tr["exit_date"]).dt.year
        ys = tr.pivot_table(index="yr", columns="side", values="pnl", aggfunc="sum").fillna(0).round()
        ys["total"] = ys.sum(axis=1)
        by_year_side = [{"year": int(y), **{c: float(ys.loc[y, c]) for c in ys.columns}} for y in ys.index]
        st = wb.stock_table(tr).reset_index()
        stocks = to_records(st)
        dts = pd.to_datetime(tr["exit_date"])
        split = (dts.min() + (dts.max() - dts.min()) / 2).date()
        persist = {"split": str(split), **wb.persistence(tr, split)}
    return safe_response({
        "run_id": run_id, "label": h["label"], "created": str(h["created"])[:16],
        "params_changed": diff, "params": params, "start": params.get("start"), "end": params.get("end"),
        "capital": total0, "cash": params.get("cash"), "metrics": metrics,
        "curve": curve, "monthly": monthly, "by_year_side": by_year_side,
        "stocks": stocks, "persistence": persist,
    })


@router.get("/api/wheel/trades")
def wheel_trades(run_id: str, symbol: Optional[str] = None, side: Optional[str] = None,
                 reason: Optional[str] = None, limit: int = Query(5000)):
    sql = """SELECT pid, symbol, side, sector, strike, expiry, lots, entry_date, exit_date, days,
                    entry_px, credit, exit_px, exit_fill, costs, pnl, margin, rom, reason
             FROM trades WHERE run_id = ?"""
    prm: list = [run_id]
    if symbol:
        sql += " AND symbol = ?"
        prm.append(symbol.upper())
    if side in ("PE", "CE"):
        sql += " AND side = ?"
        prm.append(side)
    if reason:
        sql += " AND reason = ?"
        prm.append(reason)
    sql += " ORDER BY entry_date, pid LIMIT ?"
    prm.append(limit)
    return safe_response({"rows": to_records(_q(sql, prm))})


@router.get("/api/wheel/trade_path")
def wheel_trade_path(run_id: Optional[str] = None, pid: int = Query(...), book: Optional[str] = None):
    """Daily option settle (SPAN price), delta and underlying across one trade's life.

    Works for a backtest trade (run_id) or a paper trade (book). Pre-rename
    symbols are tried too (TATAMOTORS for TMPV, etc.), since SPAN rows carry
    the name the contract traded under.
    """
    if book:
        t = _q("SELECT * FROM paper_trades WHERE book = ? AND pid = ?", [book, pid])
        if t.empty:
            t = _q("SELECT *, NULL AS exit_date, NULL AS exit_fill, NULL AS pnl, 'open' AS reason "
                   "FROM paper_positions WHERE book = ? AND pid = ?", [book, pid])
    else:
        t = _q("SELECT * FROM trades WHERE run_id = ? AND pid = ?", [run_id, pid])
    if t.empty:
        raise HTTPException(404, "trade not found")
    tr = t.iloc[0]
    sym = str(tr["symbol"])
    raws = [sym] + [old for old, new in wb.RENAMES.items() if new == sym]
    entry = pd.Timestamp(tr["entry_date"]).date()
    expiry = pd.Timestamp(tr["expiry"]).date()
    last = pd.Timestamp(tr["exit_date"]).date() if pd.notna(tr.get("exit_date")) else expiry
    lo, hi = entry - timedelta(days=7), min(expiry, last + timedelta(days=7))
    ph = ", ".join("?" * len(raws))
    path = _q(f"""
        WITH bt AS (SELECT trade_date, arg_max(tag, CASE WHEN tag = 's' THEN 9
                           ELSE TRY_CAST(right(tag, 1) AS INT) END) tag
                    FROM span_ingest_log WHERE trade_date BETWEEN ? AND ? GROUP BY 1)
        SELECT s.trade_date AS date, s.price AS opt, abs(s.delta) AS delta, u.price AS und
        FROM span_contract s JOIN bt ON bt.trade_date = s.trade_date AND bt.tag = s.tag
        LEFT JOIN span_und u ON u.trade_date = s.trade_date AND u.tag = s.tag AND u.symbol = s.symbol
        WHERE s.symbol IN ({ph}) AND s.instr = ? AND s.expiry = ? AND s.strike = ?
        ORDER BY 1""", [lo, hi] + raws + [tr["side"], expiry, float(tr["strike"])], db=str(config.SPAN_DB))
    if not path.empty:
        path["date"] = pd.to_datetime(path["date"]).dt.strftime("%Y-%m-%d")
    trade = {k: (None if (isinstance(v, float) and v != v) else v) for k, v in tr.to_dict().items()}
    return safe_response({"trade": trade, "path": to_records(path)})


# ---------------------------------------------------------------------------
# paper books
# ---------------------------------------------------------------------------

@router.get("/api/wheel/paper/books")
def wheel_paper_books():
    df = _q("""
        SELECT b.book, b.created, b.note, b.params, s.last_run, s.cash,
               (SELECT equity FROM paper_daily d WHERE d.book = b.book ORDER BY date DESC LIMIT 1) equity
        FROM paper_books b LEFT JOIN paper_state s USING (book) ORDER BY b.book""")
    rows = []
    for r in df.itertuples(index=False):
        p = json.loads(r.params)
        diff = _params_changed(p)
        rows.append({"book": r.book, "created": str(r.created)[:10], "note": r.note,
                     "last_run": None if pd.isna(r.last_run) else str(r.last_run)[:10],
                     "cash": r.cash, "equity": r.equity,
                     "capital": float(p.get("cash", 0)) + float(p.get("collateral", 0)),
                     "params_changed": diff, "params": p})
    return safe_response({"rows": rows})


@router.get("/api/wheel/paper/book/{book}")
def wheel_paper_book(book: str):
    daily = _q("""SELECT date, equity, used_margin, capacity, n_open, n_new, n_closed, span_file
                  FROM paper_daily WHERE book = ? ORDER BY date""", [book])
    pos = _q("""SELECT pid, symbol, side, strike, expiry, lots, entry_date, credit, mark,
                       (credit - mark) * lot AS unreal, delta, margin
                FROM paper_positions WHERE book = ? ORDER BY expiry, symbol""", [book])
    trades = _q("""SELECT pid, symbol, side, strike, expiry, lots, entry_date, exit_date, days, credit,
                          exit_fill, pnl, reason FROM paper_trades WHERE book = ? ORDER BY exit_date DESC""", [book])
    fills = _q("""SELECT date, pid, action, symbol, side, strike, expiry, lots, ltp, bid, ask, mid, fill,
                         settle, slip_vs_settle, reason
                  FROM paper_fills WHERE book = ? ORDER BY date DESC, pid""", [book])
    slip = _q("""SELECT action, count(*) n, count(settle) settled, avg(slip_vs_settle) avg_slip,
                        median(slip_vs_settle) med_slip, avg((ask - bid) / nullif(mid, 0)) avg_spread
                 FROM paper_fills WHERE book = ? GROUP BY 1""", [book])
    for df in (daily, pos, trades, fills):
        for c in ("date", "expiry", "entry_date", "exit_date"):
            if c in df.columns:
                df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d")
    return safe_response({"book": book, "daily": to_records(daily), "positions": to_records(pos),
                          "trades": to_records(trades), "fills": to_records(fills),
                          "slippage": to_records(slip)})
