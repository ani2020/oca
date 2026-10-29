"""
wheel_paper -- daily paper trading of a wheel_bt configuration on live closing data.

Same decision code as the backtest (wheel_bt.step), so a paper book behaves
exactly like the tested configuration. What differs is the data each day
is fed and how fills are priced:

                 backtest                      paper
  marks/stops    NSE settle (SPAN price)       live mid from the closing chain
  entry fill     bhavcopy CLOSE x (1 - spread) live BID
  exit fill      CLOSE x (1 + spread) if the   live ASK
                 contract traded, else settle
                 + untraded slippage
  margin         stored EOD SPAN file          latest SPAN file for the day (i5, 15:32)
  delta          SPAN file                     SPAN file, else live chain delta

Timing
------
F&O trading ends 15:30; the oc_scheduler 17:00 chain pull stores NSE's
closing chain stamped ~15:40. The paper run uses the latest snapshot at or
after --snap-after (15:30) for the day, so decisions are made on the 15:30-
15:40 state -- the window the strategy is meant to trade in -- even though
the job itself runs ~17:45. It refuses to trade if fewer than --min-coverage
(90%) of the universe has a closing snapshot (pull still running / failed).
Each book stores a snapshot rank (init --snap-rank N, default 0): N = each
symbol's (N+1)-th latest snapshot, so 1 = the one before the closing pull, i.e.
the last pre-close quotes that could actually have been traded. Counted per
symbol because NSE response times vary. One run serves every book, loading the
chain once per rank; run --snap-rank overrides all books. The live SPAN pull is
skipped when the final file for the day is already stored, and every step
prints its elapsed time.

Validation the backtest cannot give
-----------------------------------
Every fill is stored with the live LTP / bid / ask / mid. The next run
reconciles each fill against that day's bhavcopy (loaded in the 19:30 batch):
slip_vs_close against CLOSE -- the price the backtest fills at since 23 Oct
2026 -- and slip_vs_settle against SETTLE (kept for history; for options that
did not trade late, SETTLE is NSE's theoretical price, not a traded one).
Compare slip_vs_close with the backtest's half-spread for that volume bucket.

Books
-----
A book is a named, frozen Params set plus its own capital, positions and
history. Run several side by side, e.g. the full universe and a whitelist,
so a stock selection is tested rather than assumed.

Usage
-----
    python wheel_paper.py init --book main --entry-mode all --stop-mult 3 --rom 0.025 \\
        --delta-cap 0.20 --dte-lo 21 --max-new 4 --sizing pct --size-pct 0.05
    python wheel_paper.py init --book picks --entry-mode all --stop-mult 3 --symbols LICHSGFIN,CIPLA,POWERGRID
    python wheel_paper.py run                      # every book, today (scheduler: ~17:45)
    python wheel_paper.py run --book main --date 2026-10-05 --force
    python wheel_paper.py status [--book main]     # positions, P&L, fill slippage vs bhavcopy close
    python wheel_paper.py reconcile                # match past fills to the bhavcopy close (also done by run)
    python wheel_paper.py books

Tables (wheel_bt.duckdb): paper_books, paper_state, paper_positions,
paper_trades, paper_fills, paper_daily, paper_snapshots (pre-run state per day,
so `run --force` on a day already run restores it first -- re-runs are idempotent).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time as _clock
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import span_core as sp  # noqa: E402
import wheel_bt as wb  # noqa: E402

ROOT = HERE.parent                                   # repo root: nse_span_eod.py lives here
HOLIDAY_CACHE = Path(os.environ.get("WHEEL_HOLIDAYS", str(ROOT / "wheel_holidays.json")))
NAN = float("nan")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_books (book VARCHAR PRIMARY KEY, params JSON, created TIMESTAMP, note VARCHAR);
CREATE TABLE IF NOT EXISTS paper_state (book VARCHAR PRIMARY KEY, cash DOUBLE, pid INTEGER,
    last_run DATE, updated TIMESTAMP);
CREATE TABLE IF NOT EXISTS paper_positions (book VARCHAR, pid INTEGER, symbol VARCHAR, raw VARCHAR,
    side VARCHAR, expiry DATE, strike DOUBLE, lot INTEGER, lots INTEGER, entry_date DATE, entry_px DOUBLE,
    credit DOUBLE, entry_cost DOUBLE, margin DOUBLE, sector VARCHAR, mark DOUBLE, delta DOUBLE, und DOUBLE,
    PRIMARY KEY (book, pid));
CREATE TABLE IF NOT EXISTS paper_trades (book VARCHAR, pid INTEGER, symbol VARCHAR, side VARCHAR,
    sector VARCHAR, expiry DATE, strike DOUBLE, lot INTEGER, lots INTEGER, entry_date DATE, exit_date DATE,
    entry_px DOUBLE, credit DOUBLE, exit_px DOUBLE, exit_fill DOUBLE, costs DOUBLE, pnl DOUBLE,
    margin DOUBLE, rom DOUBLE, days INTEGER, reason VARCHAR, traded_exit BOOLEAN,
    PRIMARY KEY (book, pid));
CREATE TABLE IF NOT EXISTS paper_fills (book VARCHAR, date DATE, snapshot_ts TIMESTAMP, pid INTEGER,
    action VARCHAR, symbol VARCHAR, raw VARCHAR, side VARCHAR, expiry DATE, strike DOUBLE, lots INTEGER,
    qty INTEGER, ltp DOUBLE, bid DOUBLE, ask DOUBLE, mid DOUBLE, fill DOUBLE, reason VARCHAR,
    settle DOUBLE, slip_vs_settle DOUBLE, PRIMARY KEY (book, date, pid, action));
CREATE TABLE IF NOT EXISTS paper_snapshots (book VARCHAR, date DATE, cash DOUBLE, pid INTEGER,
    last_run DATE, positions JSON, taken TIMESTAMP, PRIMARY KEY (book, date));
CREATE TABLE IF NOT EXISTS paper_daily (book VARCHAR, date DATE, snapshot_ts TIMESTAMP, equity DOUBLE,
    cash DOUBLE, liability DOUBLE, used_margin DOUBLE, capacity DOUBLE, n_open INTEGER, n_new INTEGER,
    n_closed INTEGER, breach BOOLEAN, cash_rule_breach BOOLEAN, span_file VARCHAR,
    PRIMARY KEY (book, date));
"""


# ---------------------------------------------------------------------------
# connections
# ---------------------------------------------------------------------------

def out_con(db: Path = wb.WHEEL_DB):
    import duckdb
    con = duckdb.connect(str(db))
    con.execute(SCHEMA)
    for col, typ in (("stress", "DOUBLE"), ("stress_cap", "DOUBLE"), ("brake", "BOOLEAN")):   # risk layer
        con.execute(f"ALTER TABLE paper_daily ADD COLUMN IF NOT EXISTS {col} {typ}")
    for col in ("close_px", "slip_vs_close"):                                                   # 23 Oct: close basis
        con.execute(f"ALTER TABLE paper_fills ADD COLUMN IF NOT EXISTS {col} DOUBLE")
    con.execute("ALTER TABLE paper_books ADD COLUMN IF NOT EXISTS snap_rank INTEGER")           # 28 Oct
    for col, typ in POS_EXTRA:                                                                  # 28 Oct review
        con.execute(f"ALTER TABLE paper_positions ADD COLUMN IF NOT EXISTS {col} {typ}")
    con.execute("ALTER TABLE paper_state ADD COLUMN IF NOT EXISTS interest DOUBLE")
    return con


def _num_or(v):
    """NaN -> NULL for DuckDB; anything else unchanged."""
    return None if isinstance(v, float) and v != v else v


def _num(v) -> Optional[float]:
    """NaN/inf -> NULL for DuckDB."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def in_con():
    """oc.duckdb + bhavcopy.duckdb, read-only (wheel_bt.connect)."""
    return wb.connect()


# ---------------------------------------------------------------------------
# calendar (history from SPAN log + future weekdays minus NSE holidays)
# ---------------------------------------------------------------------------

def holidays(refresh: bool = False) -> set:
    if HOLIDAY_CACHE.exists() and not refresh:
        try:
            return {date.fromisoformat(x) for x in json.loads(HOLIDAY_CACHE.read_text())}
        except Exception:  # noqa: BLE001
            pass
    days: set = set()
    try:
        sys.path.insert(0, str(ROOT))
        from nse_fetcher import NSEFetcher
        rows = NSEFetcher(cookies_file=str(ROOT / "nse_cookies.json")).get_holidays("FO") or []
        for r in rows:
            try:
                days.add(datetime.strptime(r["tradingDate"], "%d-%b-%Y").date())
            except (KeyError, ValueError):
                continue
        if days:
            HOLIDAY_CACHE.write_text(json.dumps(sorted(x.isoformat() for x in days)))
    except Exception as exc:  # noqa: BLE001
        print(f"  warn: holiday master unavailable ({exc}); future calendar = weekdays")
    return days


def build_calendar(con, d: date, hol: set, ahead_days: int = 120) -> list:
    hist = [r[0] for r in con.execute(
        "SELECT DISTINCT trade_date FROM bc.span_ingest_log WHERE trade_date < ? ORDER BY 1", [d]).fetchall()]
    fut, x = [], d
    while x <= d + timedelta(days=ahead_days):
        if x.weekday() < 5 and x not in hol:
            fut.append(x)
        x += timedelta(days=1)
    return sorted(set(hist) | set(fut))


# ---------------------------------------------------------------------------
# live data
# ---------------------------------------------------------------------------

def load_chain(con, d: date, snap_after: time, max_stale_min: int = 30,
               snap_rank: int = 0) -> tuple[pd.DataFrame, Optional[datetime]]:
    """One chain snapshot per universe symbol for day d, both sides stacked long.

    snap_rank 0 = each symbol's latest snapshot at/after snap_after (the ~15:40 closing
    pull); N = its (N+1)-th latest, counted per symbol, so 1 = the one before the closing
    pull (the last pre-close quotes) whatever time NSE answered."""
    lo = datetime.combine(d, snap_after)
    hi = datetime.combine(d, time(23, 59))
    df = con.execute("""
        WITH u AS (SELECT DISTINCT symbol FROM index_constituents),
        s AS (SELECT DISTINCT symbol, timestamp ts FROM ocdata
              WHERE timestamp BETWEEN ? AND ? AND symbol IN (SELECT symbol FROM u)),
        last AS (SELECT symbol, ts FROM s
                 QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY ts DESC) = ? + 1)
        SELECT o.symbol, o.timestamp ts, o.expiry, o.strike_price strike, o.underlying_price und,
               o.lotsize lot, o.ce_ltp, o.ce_bid, o.ce_ask, o.ce_volume, o.ce_oi, o.ce_delta,
               o.pe_ltp, o.pe_bid, o.pe_ask, o.pe_volume, o.pe_oi, o.pe_delta
        FROM ocdata o JOIN last l ON o.symbol = l.symbol AND o.timestamp = l.ts
    """, [lo, hi, max(int(snap_rank), 0)]).df()
    if df.empty:
        return df, None
    parts = []
    for side, pfx in (("CE", "ce_"), ("PE", "pe_")):
        x = df[["symbol", "ts", "expiry", "strike", "und", "lot"]].copy()
        x["opt_type"] = side
        for c in ("ltp", "bid", "ask", "volume", "oi", "delta"):
            x[c] = pd.to_numeric(df[pfx + c], errors="coerce")
        parts.append(x)
    ch = pd.concat(parts, ignore_index=True)
    ch["expiry"] = pd.to_datetime(ch["expiry"]).dt.date
    ch["mid"] = ch["ltp"]
    q = (ch["bid"] > 0) & (ch["ask"] > 0) & (ch["ask"] >= ch["bid"])
    ch.loc[q, "mid"] = (ch.loc[q, "bid"] + ch.loc[q, "ask"]) / 2
    snap = pd.to_datetime(ch["ts"]).min().to_pydatetime()
    newest = pd.to_datetime(ch["ts"]).max().to_pydatetime()
    if (newest - snap) > timedelta(minutes=max_stale_min):
        print(f"  warn: snapshot spread {snap:%H:%M}..{newest:%H:%M} across symbols")
    return ch, snap


def refresh_span(d: date) -> None:
    """Pull the latest intraday SPAN file for d into bhavcopy.duckdb (nse_span_eod live)."""
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "nse_span_eod.py"), "live", "--date", d.isoformat(), "--universe"]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=600)
        tail = (r.stdout or "").strip().splitlines()[-3:]
        print("  span live: " + (" | ".join(tail) if tail else f"rc={r.returncode}"))
    except Exception as exc:  # noqa: BLE001
        print(f"  warn: span refresh failed ({exc}); using latest stored file")


def span_lookup(con, d: date, symbols: set):
    """Latest stored SPAN file on or before d -> {(raw, instr, expiry, strike): (short_scan, delta, vol)}, und, tag."""
    lf = sp.latest_file(con, d, prefix="bc.")
    if lf is None:
        return {}, {}, None
    fd, tag = lf
    syms = sorted(symbols)
    ph = ", ".join("?" * len(syms))
    con_rows = con.execute(f"""
        SELECT symbol, instr, expiry, strike, short_scan, delta, vol FROM bc.span_contract
        WHERE trade_date = ? AND tag = ? AND instr IN ('CE', 'PE') AND symbol IN ({ph})""",
        [fd, tag] + syms).fetchall()
    und = dict(con.execute(f"SELECT symbol, price FROM bc.span_und WHERE trade_date = ? AND tag = ? "
                           f"AND symbol IN ({ph})", [fd, tag] + syms).fetchall())
    lk = {(s, i, wb._d(e), round(float(k), 2)): (sc, dl, wb._f(v)) for s, i, e, k, sc, dl, v in con_rows}
    return lk, und, f"{fd}:{tag}"


def prev_close(con, d: date, symbols: set) -> dict:
    syms = sorted(symbols)
    ph = ", ".join("?" * len(syms))
    return dict(con.execute(f"""
        SELECT symbol, arg_max(close, trade_date) FROM bc.cash_eod
        WHERE trade_date < ? AND series = 'EQ' AND symbol IN ({ph}) GROUP BY 1""", [d] + syms).fetchall())


def build_candidates(p: wb.Params, d: date, ch: pd.DataFrame, span: dict, pclose: dict) -> pd.DataFrame:
    """Same selection rule as wheel_bt.load_data, on live prices: furthest-OTM strike with
    ROM >= target and |delta| <= cap, per (symbol, side), for expiries dte_lo..dte_hi."""
    x = ch.copy()
    x["dte"] = [(e - d).days for e in x["expiry"]]
    x = x[(x["dte"] >= p.dte_lo) & (x["dte"] <= p.dte_hi)]
    x = x[((x["opt_type"] == "PE") & (x["strike"] < x["und"])) | ((x["opt_type"] == "CE") & (x["strike"] > x["und"]))]
    # liquidity gate (plan item 0): traded volume and OI (live chain OI is in contracts),
    # plus a quoted-spread cap the backtest cannot see
    x = x[(x["volume"] >= max(p.min_entry_volume, 1)) & (x["oi"].fillna(0) >= p.min_entry_oi)
          & (x["bid"] > 0) & (x["mid"] > 0) & (x["lot"] > 0)]
    x = x[(x["ask"] - x["bid"]) / x["mid"] <= p.max_quote_spread]
    if x.empty:
        return x
    sc, dl, sv = [], [], []
    for r in x.itertuples(index=False):
        v = span.get((r.symbol, r.opt_type, r.expiry, round(float(r.strike), 2)))
        sc.append(v[0] if v else NAN)
        dl.append(v[1] if v and v[1] == v[1] else NAN)
        sv.append(v[2] if v and len(v) > 2 else NAN)
    x["short_scan"] = sc
    x["svol"] = sv
    x["delta_span"] = dl
    x = x[x["short_scan"].notna()]
    x["delta"] = x["delta_span"].abs().fillna(x["delta"].abs())
    x["margin"] = x["short_scan"] * x["lot"] * p.broker_mult + p.elm * x["und"] * x["lot"]
    x["rom"] = x["bid"] * x["lot"] / x["margin"]          # ROM on what a sell actually gets
    x = x[(x["rom"] >= p.rom_target) & (x["delta"] <= p.delta_cap)]
    if p.min_delta is not None:
        x = x[x["delta"] >= p.min_delta]                   # delta band floor, as wheel_bt's SQL
    if x.empty:
        return x
    if p.pick_by == "delta":
        # same pick as wheel_bt.PICK_SQL: lowest |delta|, ties -> furthest OTM, then nearest expiry
        x = x.assign(_otm=np.where(x["opt_type"] == "PE", x["strike"], -x["strike"]))
        x = x.sort_values(["delta", "_otm", "expiry"], kind="mergesort").groupby(["symbol", "opt_type"],
                                                                                 as_index=False).first()
    else:
        # rich / volrich / rom: wheel_bt.pick_strikes on the same columns the backtest uses
        y = x.assign(trade_date=d, raw=x["symbol"], premium=x["mid"])
        picked = wb.pick_strikes(y, p)[["symbol", "opt_type", "expiry", "strike"]]
        x = x.merge(picked, on=["symbol", "opt_type", "expiry", "strike"])
    x["move"] = [(u / pclose[s] - 1) if pclose.get(s) else NAN for s, u in zip(x["symbol"], x["und"])]
    return pd.DataFrame({
        "trade_date": d, "symbol": x["symbol"], "opt_type": x["opt_type"], "raw": x["symbol"],
        "expiry": x["expiry"], "strike": x["strike"].astype(float), "premium": x["mid"], "lot": x["lot"],
        "margin": x["margin"], "rom": x["rom"], "delta": x["delta"], "und": x["und"], "oi": x["oi"],
        "volume": x["volume"], "move": x["move"], "bid": x["bid"], "ask": x["ask"], "ltp": x["ltp"],
        "svol": x["svol"]})


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------

# every Pos field step() reads is persisted: the script exits after each run
POS_EXTRA = [("entry_vol", "DOUBLE"), ("svol", "DOUBLE"), ("iv", "DOUBLE"), ("rich", "DOUBLE"),
             ("iv_hv", "DOUBLE"), ("exit_pending", "VARCHAR")]
POS_COLS = ["pid", "symbol", "raw", "side", "expiry", "strike", "lot", "lots", "entry_date", "entry_px",
            "credit", "entry_cost", "margin", "sector", "mark", "delta", "und"] + [c for c, _ in POS_EXTRA]
_POS_DEFAULT = {"entry_vol": 0.0, "svol": NAN, "iv": NAN, "rich": NAN, "iv_hv": NAN, "exit_pending": ""}


def _pos_value(v, col):
    """NULL / NaN from the DB -> the Pos default for that field."""
    if v is None or (isinstance(v, float) and v != v):
        return _POS_DEFAULT.get(col, v)
    return v


def load_book(oc, book: str) -> tuple[wb.Params, wb.State, Optional[date]]:
    row = oc.execute("SELECT params FROM paper_books WHERE book = ?", [book]).fetchone()
    if row is None:
        raise SystemExit(f"no paper book {book!r}; create it with: wheel_paper.py init --book {book} ...")
    p = wb.params_from_json(row[0])
    srow = oc.execute("SELECT cash, pid, last_run, coalesce(interest, 0) FROM paper_state WHERE book = ?",
                      [book]).fetchone()
    st = wb.State(cash=p.cash) if srow is None else wb.State(cash=srow[0], pid=srow[1])
    if srow is not None:
        st.last_day = wb._d(srow[2]) if srow[2] is not None else None   # interest accrues from here
        st.interest = float(srow[3])
    st.peak = oc.execute("SELECT coalesce(max(equity), 0) FROM paper_daily WHERE book = ?", [book]).fetchone()[0]
    for r in oc.execute(f"SELECT {', '.join(POS_COLS)} FROM paper_positions WHERE book = ? ORDER BY pid",
                        [book]).fetchall():
        d = {c: _pos_value(v, c) for c, v in zip(POS_COLS, r)}
        st.book.append(wb.Pos(d["pid"], d["symbol"], d["raw"], d["side"], wb._d(d["expiry"]), d["strike"],
                              d["lot"], wb._d(d["entry_date"]), d["entry_px"], d["credit"], d["entry_cost"],
                              d["margin"], d["sector"], mark=d["mark"], delta=d["delta"], und=d["und"],
                              lots=d["lots"], entry_vol=float(d["entry_vol"]), svol=d["svol"], iv=d["iv"],
                              rich=d["rich"], iv_hv=d["iv_hv"], exit_pending=d["exit_pending"] or ""))
    return p, st, (srow[2] if srow else None)


def snapshot_state(oc, book: str, d: date) -> None:
    """Store the book's state as it was BEFORE day d's run (once per day)."""
    if oc.execute("SELECT 1 FROM paper_snapshots WHERE book = ? AND date = ?", [book, d]).fetchone():
        return
    srow = oc.execute("SELECT cash, pid, last_run, interest FROM paper_state WHERE book = ?", [book]).fetchone()
    pos = oc.execute(f"SELECT {', '.join(POS_COLS)} FROM paper_positions WHERE book = ? ORDER BY pid", [book]).df()
    oc.execute("INSERT INTO paper_snapshots VALUES (?, ?, ?, ?, ?, ?, now())",
               [book, d, srow[0] if srow else None, srow[1] if srow else None, srow[2] if srow else None,
                json.dumps({"positions": json.loads(pos.to_json(orient="records", date_format="iso")),
                            "interest": srow[3] if srow else None})])


def restore_state(oc, book: str, d: date) -> bool:
    """Undo day d for a book: restore its pre-run state, drop that day's trades/fills/daily row."""
    row = oc.execute("SELECT cash, pid, last_run, positions FROM paper_snapshots WHERE book = ? AND date = ?",
                     [book, d]).fetchone()
    if row is None:
        return False
    cash, pid, last_run, positions = row
    blob = json.loads(positions or "[]")
    if isinstance(blob, dict):                    # 28 Oct: positions + interest
        plist, interest = blob.get("positions", []), blob.get("interest")
    else:                                         # older snapshots: a bare positions list
        plist, interest = blob, None
    oc.execute("BEGIN")
    try:
        oc.execute("DELETE FROM paper_positions WHERE book = ?", [book])
        for r in plist:
            r["expiry"] = r["expiry"][:10]
            r["entry_date"] = r["entry_date"][:10]
            oc.execute(f"INSERT INTO paper_positions (book, {', '.join(POS_COLS)}) "
                       f"VALUES ({', '.join('?' * (len(POS_COLS) + 1))})",
                       [book] + [_num_or(r.get(c)) for c in POS_COLS])
        oc.execute("DELETE FROM paper_trades WHERE book = ? AND exit_date = ?", [book, d])
        oc.execute("DELETE FROM paper_fills WHERE book = ? AND date = ?", [book, d])
        oc.execute("DELETE FROM paper_daily WHERE book = ? AND date = ?", [book, d])
        oc.execute("DELETE FROM paper_state WHERE book = ?", [book])
        if cash is not None:
            oc.execute("INSERT INTO paper_state (book, cash, pid, last_run, updated, interest) "
                       "VALUES (?, ?, ?, ?, now(), ?)", [book, cash, pid, last_run, interest])
        oc.execute("COMMIT")
    except Exception:
        oc.execute("ROLLBACK")
        raise
    return True


def save_book(oc, book: str, d: date, snap: datetime, st: wb.State, closed: list, opened: list,
              eq: dict, quotes: dict, span_file: Optional[str], raw_by_pid: dict) -> None:
    oc.execute("BEGIN")
    try:
        oc.execute("DELETE FROM paper_positions WHERE book = ?", [book])
        for ps in st.book:
            oc.execute(f"INSERT INTO paper_positions (book, {', '.join(POS_COLS)}) "
                       f"VALUES ({', '.join('?' * (len(POS_COLS) + 1))})",
                       [book] + [_num_or(getattr(ps, c)) for c in POS_COLS])
        for t in closed:
            oc.execute("DELETE FROM paper_trades WHERE book = ? AND pid = ?", [book, t["pid"]])
            cols = ["pid", "symbol", "side", "sector", "expiry", "strike", "lot", "lots", "entry_date",
                    "exit_date", "entry_px", "credit", "exit_px", "exit_fill", "costs", "pnl", "margin", "rom",
                    "days", "reason", "traded_exit"]
            oc.execute(f"INSERT INTO paper_trades (book, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                       [book] + [t[c] for c in cols])
        fills = []
        for ps in opened:
            q = quotes.get(ps.key, {})
            fills.append((ps.pid, "S", ps.symbol, ps.raw, ps.side, ps.expiry, ps.strike, ps.lots, ps.lot,
                          q.get("ltp"), q.get("bid"), q.get("ask"), q.get("mid"), ps.credit, "entry"))
        for t in closed:
            raw = raw_by_pid.get(t["pid"], t["symbol"])
            q = quotes.get((raw, t["side"], t["expiry"], float(t["strike"])), {})
            fills.append((t["pid"], "B", t["symbol"], raw, t["side"], t["expiry"], t["strike"],
                          t["lots"], t["lot"], q.get("ltp"), q.get("bid"), q.get("ask"), q.get("mid"),
                          t["exit_fill"], t["reason"]))
        for f in fills:
            oc.execute("DELETE FROM paper_fills WHERE book = ? AND date = ? AND pid = ? AND action = ?",
                       [book, d, f[0], f[1]])
            oc.execute("INSERT INTO paper_fills (book, date, snapshot_ts, pid, action, symbol, raw, side, expiry, "
                       "strike, lots, qty, ltp, bid, ask, mid, fill, reason) "
                       "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [book, d, snap] + list(f))
        oc.execute("DELETE FROM paper_daily WHERE book = ? AND date = ?", [book, d])
        oc.execute("INSERT INTO paper_daily (book, date, snapshot_ts, equity, cash, liability, used_margin, "
                   "capacity, n_open, n_new, n_closed, breach, cash_rule_breach, span_file, stress, stress_cap, "
                   "brake) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [book, d, snap, eq["equity"], eq["cash"], eq["liability"], eq["used_margin"], eq["capacity"],
                    eq["n_open"], eq["n_new"], len(closed), eq["breach"], eq["cash_rule_breach"], span_file,
                    _num(eq.get("stress")), _num(eq.get("stress_cap")), bool(eq.get("brake"))])
        oc.execute("DELETE FROM paper_state WHERE book = ?", [book])
        oc.execute("INSERT INTO paper_state (book, cash, pid, last_run, updated, interest) "
                   "VALUES (?, ?, ?, ?, now(), ?)", [book, st.cash, st.pid, d, st.interest])
        oc.execute("COMMIT")
    except Exception:
        oc.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# reconcile fills against NSE settle
# ---------------------------------------------------------------------------

def reconcile(oc, bhav_db: Path = wb.BHAV_DB) -> int:
    """Fill close_px / slip_vs_close (and settle / slip_vs_settle) for past fills once
    that day's bhavcopy is loaded.

    slip_vs_close > 0 means the quote fill was WORSE than the bhavcopy CLOSE (the
    backtest's reference price): sold below it, or bought back above it.
    Returns the number of fills that got their CLOSE this call.
    """
    oc.execute(f"ATTACH IF NOT EXISTS '{bhav_db}' AS bcr (READ_ONLY)")
    try:
        n0 = oc.execute("SELECT count(*) FROM paper_fills WHERE close_px IS NULL").fetchone()[0]
        oc.execute("""
            UPDATE paper_fills AS f SET close_px = o.close,
                slip_vs_close = CASE f.action WHEN 'S' THEN (o.close - f.fill) / o.close
                                              ELSE (f.fill - o.close) / o.close END
            FROM bcr.options_eod AS o
            WHERE f.close_px IS NULL AND o.trade_date = f.date AND o.symbol = f.raw AND o.expiry = f.expiry
              AND o.strike = f.strike AND o.opt_type = f.side AND o.close > 0""")
        oc.execute("""
            UPDATE paper_fills AS f SET settle = o.settle,
                slip_vs_settle = CASE WHEN o.settle > 0 THEN
                    CASE f.action WHEN 'S' THEN (o.settle - f.fill) / o.settle
                                  ELSE (f.fill - o.settle) / o.settle END END
            FROM bcr.options_eod AS o
            WHERE f.settle IS NULL AND o.trade_date = f.date AND o.symbol = f.raw AND o.expiry = f.expiry
              AND o.strike = f.strike AND o.opt_type = f.side AND o.settle IS NOT NULL""")
        n1 = oc.execute("SELECT count(*) FROM paper_fills WHERE close_px IS NULL").fetchone()[0]
    finally:
        oc.execute("DETACH bcr")
    return n0 - n1


# ---------------------------------------------------------------------------
# one book, one day
# ---------------------------------------------------------------------------

def run_book(oc, book: str, d: date, *, force: bool, ch: pd.DataFrame, snap: datetime, span: dict,
             span_file: Optional[str], pclose: dict, ctx: wb.Ctx) -> None:
    _, _, last = load_book(oc, book)
    if last is not None and last > d:
        print(f"  [{book}] already advanced to {last}; cannot run an earlier day {d}")
        return
    if last == d:
        if not force:
            print(f"  [{book}] already run for {d}; use --force to redo it")
            return
        if not restore_state(oc, book, d):
            print(f"  [{book}] no pre-run snapshot for {d}; refusing to re-run")
            return
        print(f"  [{book}] restored pre-run state for {d}")
    p0, _, _ = load_book(oc, book)
    bad = unsupported(p0)
    if bad:
        print(f"  [{book}] skipped: settings the paper trader cannot apply live: {'; '.join(bad)}")
        return
    snapshot_state(oc, book, d)
    p, st, last = load_book(oc, book)
    raw_by_pid = {ps.pid: ps.raw for ps in st.book}
    quotes, rows = {}, {}
    for r in ch.itertuples(index=False):
        k = (r.symbol, r.opt_type, r.expiry, float(r.strike))
        v = span.get((r.symbol, r.opt_type, r.expiry, round(float(r.strike), 2)))
        sscan = v[0] if v else NAN
        dl = v[1] if v and v[1] == v[1] else r.delta
        quotes[k] = dict(symbol=r.symbol, ltp=r.ltp, bid=r.bid, ask=r.ask, mid=r.mid)
        rows[k] = (r.ltp, float(r.volume or 0), r.mid, dl, sscan, r.und, r.ask,
                   v[2] if v and len(v) > 2 else NAN)

    def row_of(k):
        return rows.get(k)

    missing = [ps.key for ps in st.book if ps.key not in rows]
    if missing:
        print(f"  [{book}] warn: {len(missing)} open contract(s) not in today's chain; carrying last mark: {missing[:4]}")
    if p.hedge:
        print(f"  [{book}] note: the index hedge is simulated in the backtest only; paper trades without it")
        p = wb.replace(p, hedge=False)
    cands = build_candidates(p, d, ch, span, pclose)
    closed, opened, eq = wb.step(p, st, d, ctx, row_of, cands, fill_mode="quote")
    save_book(oc, book, d, snap, st, closed, opened, eq, quotes, span_file, raw_by_pid)

    stress = eq.get("stress")
    risk = (f"  stress {stress:,.0f} / cap {eq.get('stress_cap'):,.0f}" if stress == stress and stress is not None
            else "") + ("  BRAKE ON (no new puts)" if eq.get("brake") else "")
    print(f"\n  [{book}] {d}  snapshot {snap:%H:%M}  span {span_file}  candidates={len(cands)}{risk}")
    for t in closed:
        print(f"    CLOSE {t['symbol']:<11} {t['side']} {t['strike']:>9} {t['expiry']}  x{t['lots']}  "
              f"fill {t['exit_fill']:.2f}  pnl {t['pnl']:>10,.0f}  ({t['reason']})")
    for ps in opened:
        print(f"    OPEN  {ps.symbol:<11} {ps.side} {ps.strike:>9} {ps.expiry}  x{ps.lots}  "
              f"credit {ps.credit:.2f}  margin {ps.margin:>10,.0f}  delta {ps.delta:.3f}")
    print(f"    equity {eq['equity']:,.0f}  open {eq['n_open']}  used margin {eq['used_margin']:,.0f} "
          f"/ capacity {eq['capacity']:,.0f}")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _step(msg: str, t0: list) -> None:
    """Progress line with elapsed seconds since the previous one (never a silent wait)."""
    now = _clock.perf_counter()
    print(f"  [{now - t0[0]:6.1f}s] {msg}", flush=True)
    t0[0] = now


def span_final_stored(d: date) -> bool:
    """True when the final ('s') SPAN file for d is already stored: no live pull needed."""
    try:
        con = in_con()
        try:
            lf = sp.latest_file(con, d, prefix="bc.")
        finally:
            con.close()
        return lf is not None and lf[0] == d and lf[1] == "s"
    except Exception:  # noqa: BLE001
        return False


def cmd_run(a) -> int:
    d = a.date or date.today()
    t0 = [_clock.perf_counter()]
    hol = holidays()
    if d.weekday() >= 5 or d in hol:
        print(f"  {d} is not a trading day; nothing to do")
        return 0
    oc = out_con()
    try:
        rows = oc.execute("SELECT book, coalesce(snap_rank, 0) FROM paper_books ORDER BY 1").fetchall()
        ranks = {b: int(r) for b, r in rows}
        books = [a.book] if a.book else list(ranks)
        if not books:
            print("  no paper books; create one with: wheel_paper.py init --book NAME ...")
            return 1
        if a.snap_rank is not None:                       # CLI overrides every book's stored rank
            ranks.update({b: int(a.snap_rank) for b in books})
        _step("reconciling earlier fills", t0)
        n = reconcile(oc)
        if n:
            print(f"  reconciled {n} fill(s) against the bhavcopy close")
    finally:
        oc.close()

    if a.no_span:
        pass
    elif span_final_stored(d):
        _step(f"final SPAN file for {d} already stored; skipping the live pull", t0)
    else:
        _step(f"pulling the latest intraday SPAN file for {d} (nse_span_eod live, up to 10 min)", t0)
        refresh_span(d)
    by_rank: dict = {}
    for b in books:
        by_rank.setdefault(ranks.get(b, 0), []).append(b)
    ic = in_con()
    try:
        chains = {}
        for rk in sorted(by_rank):
            snap_after = a.snap_after or (time(15, 30) if not rk else time(14, 30))
            _step(f"loading chain snapshots, rank {rk} (at/after {snap_after:%H:%M}) for {', '.join(by_rank[rk])}", t0)
            ch, snap = load_chain(ic, d, snap_after, snap_rank=rk)
            if snap is None:
                print(f"  no chain snapshot (rank {rk}) at/after {snap_after} on {d}; "
                      f"has the closing pull run? Not trading {', '.join(by_rank[rk])}.")
                continue
            syms = set(ch["symbol"])
            n_univ = ic.execute("SELECT count(DISTINCT symbol) FROM index_constituents").fetchone()[0]
            if n_univ and len(syms) / n_univ < a.min_coverage:
                print(f"  only {len(syms)}/{n_univ} universe symbols have a snapshot at rank {rk} "
                      f"(< {a.min_coverage:.0%}); chain pull still running or failed. Not trading {', '.join(by_rank[rk])}.")
                continue
            chains[rk] = (ch, snap)
        if not chains:
            return 2
        syms = set().union(*(set(ch["symbol"]) for ch, _ in chains.values()))
        _step("SPAN margins, previous closes, events, market series", t0)
        span, _, span_file = span_lookup(ic, d, syms)
        if span_file is None or not span_file.startswith(str(d)):
            print(f"  warn: margin from SPAN file {span_file} (none stored for {d} yet)")
        pclose = prev_close(ic, d, syms)
        ev = wb.load_events_full(ic, d - timedelta(days=10), d + timedelta(days=120))
        # VIX / NIFTY / betas / trend as of the latest stored close (today's index and cash
        # files land in the 19:30 batch, after this 17:45 run, so the brake uses yesterday)
        mkt = wb.load_market(ic, d - timedelta(days=30), d)
        # CA blackout: contract-adjusting set as in the backtest; no knowability filter here --
        # anything already in the DB has been announced (ca_known left empty)
        ctx = wb.Ctx(build_calendar(ic, d, hol), ev["results"], ev["ca"], dict(wb.SECTOR),
                     ev["board"], ev["known"], ev["coverage"], mkt, ca_adj=ev.get("ca_adj"))
    finally:
        ic.close()

    oc = out_con()
    try:
        for rk, (ch, snap) in chains.items():
            for b in by_rank[rk]:
                _step(f"running book {b} on snapshot rank {rk}", t0)
                run_book(oc, b, d, force=a.force, ch=ch, snap=snap, span=span, span_file=span_file,
                         pclose=pclose, ctx=ctx)
    finally:
        oc.close()
    _step("done", t0)
    return 0


PAPER_UNSUPPORTED = (
    ("max_iv_hv", lambda p: p.max_iv_hv is not None, "the IV/HV gate needs ATM IV per candidate (backtest only; dropped)"),
    ("rich_confirm_days", lambda p: p.rich_confirm_days > 0, "richness confirmation needs earlier sessions' "
                                                              "candidates (backtest only; dropped)"),
)


def unsupported(p: wb.Params) -> list:
    """Settings the paper trader cannot apply live: a book using them would silently never trade."""
    return [f"{k}: {why}" for k, test, why in PAPER_UNSUPPORTED if test(p)]


def cmd_init(a) -> int:
    p = wb.params_from_args(a)
    bad = unsupported(p)
    if bad:
        print("  refusing: this book uses settings the paper trader cannot apply live:\n    " + "\n    ".join(bad))
        return 1
    oc = out_con()
    try:
        old = oc.execute("SELECT coalesce(snap_rank, 0) FROM paper_books WHERE book = ?", [a.book]).fetchone()
        if old and not a.replace:
            print(f"  book {a.book!r} exists; --replace to overwrite its params (positions/history are kept)")
            return 1
        if a.snap_rank is None and old:
            a.snap_rank = old[0]                          # --replace without --snap-rank keeps the book's rank
        oc.execute("DELETE FROM paper_books WHERE book = ?", [a.book])
        oc.execute("INSERT INTO paper_books (book, params, created, note, snap_rank) VALUES (?, ?, now(), ?, ?)",
                   [a.book, wb.params_to_json(p), a.note or "", int(a.snap_rank or 0)])
    finally:
        oc.close()
    print(f"  book {a.book!r} saved (snapshot rank {int(a.snap_rank or 0)}):")
    for k, v in asdict(p).items():
        if v != getattr(wb.Params(), k):
            print(f"    {k} = {v}")
    return 0


def cmd_status(a) -> int:
    oc = out_con()
    try:
        n = reconcile(oc)
        if n:
            print(f"  reconciled {n} fill(s)")
        where, prm = ("WHERE book = ?", [a.book]) if a.book else ("", [])
        with pd.option_context("display.width", 200, "display.max_columns", 30):
            print("\n  Books:\n  " + oc.execute(f"""
                SELECT b.book, s.last_run, round(s.cash) cash,
                       (SELECT round(equity) FROM paper_daily d WHERE d.book = b.book ORDER BY date DESC LIMIT 1) equity,
                       (SELECT count(*) FROM paper_positions x WHERE x.book = b.book) open_pos,
                       (SELECT count(*) FROM paper_trades t WHERE t.book = b.book) closed,
                       (SELECT round(sum(pnl)) FROM paper_trades t WHERE t.book = b.book) realised
                FROM paper_books b LEFT JOIN paper_state s USING (book) {where.replace('book', 'b.book')} ORDER BY 1
            """, prm).df().to_string(index=False).replace("\n", "\n  "))
            pos = oc.execute(f"""SELECT book, symbol, side, strike, expiry, lots, entry_date, round(credit, 2) credit,
                round(mark, 2) mark, round((credit - mark) * lot) unreal, round(delta, 3) delta, round(margin) margin
                FROM paper_positions {where} ORDER BY book, expiry, symbol""", prm).df()
            print("\n  Open positions:\n  " + (pos.to_string(index=False).replace("\n", "\n  ") if len(pos) else "(none)"))
            tr = oc.execute(f"""SELECT book, symbol, side, strike, entry_date, exit_date, round(credit, 2) credit,
                round(exit_fill, 2) exit_fill, round(pnl) pnl, reason FROM paper_trades {where}
                ORDER BY exit_date DESC LIMIT 20""", prm).df()
            print("\n  Last closed trades:\n  " + (tr.to_string(index=False).replace("\n", "\n  ") if len(tr) else "(none)"))
            sl = oc.execute(f"""SELECT book, action, count(*) fills, count(close_px) reconciled,
                round(100 * avg(slip_vs_close), 2) avg_slip_close_pct, round(100 * median(slip_vs_close), 2) med_slip_close_pct,
                round(100 * avg(slip_vs_settle), 2) avg_slip_settle_pct,
                round(100 * avg((ask - bid) / nullif(mid, 0)), 2) avg_spread_pct
                FROM paper_fills {where} GROUP BY 1, 2 ORDER BY 1, 2""", prm).df()
            print("\n  Fill quality vs the bhavcopy CLOSE (the backtest's fill price; it charges the half-spread"
                  "\n  of the contract's volume bucket, ~2-10% of premium):\n  "
                  + (sl.to_string(index=False).replace("\n", "\n  ") if len(sl) else "(no fills yet)"))
    finally:
        oc.close()
    return 0


def cmd_reconcile(a) -> int:
    oc = out_con()
    try:
        print(f"  reconciled {reconcile(oc)} fill(s)")
    finally:
        oc.close()
    return 0


def cmd_books(a) -> int:
    oc = out_con()
    try:
        for book, params, created, note, rk in oc.execute(
                "SELECT book, params, created, note, coalesce(snap_rank, 0) FROM paper_books ORDER BY 1").fetchall():
            p = wb.params_from_json(params)
            diff = {k: v for k, v in asdict(p).items() if v != getattr(wb.Params(), k)}
            print(f"  {book}  (created {created:%Y-%m-%d}, snapshot rank {rk})  {note or ''}\n    {diff}")
    finally:
        oc.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["init", "run", "status", "reconcile", "books"])
    ap.add_argument("--book")
    ap.add_argument("--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--snap-after", type=lambda s: datetime.strptime(s, "%H:%M").time(), default=None,
                    help="only consider chain snapshots at/after this time (default 15:30; 14:30 with --snap-rank)")
    ap.add_argument("--snap-rank", type=int, default=None,
                    help="init: the book's snapshot rank (stored; default 0). run: override every book's rank. "
                         "0 = each symbol's latest snapshot (closing pull, ~15:40); 1 = the one before it "
                         "(last pre-close quotes); 2 = two before; counted per symbol, not by clock time")
    ap.add_argument("--no-span", action="store_true", help="skip pulling the latest intraday SPAN file")
    ap.add_argument("--min-coverage", type=float, default=0.90,
                    help="refuse to trade if fewer than this share of the universe has a closing snapshot")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--replace", action="store_true", help="init: overwrite an existing book's params")
    ap.add_argument("--note")
    wb.add_param_args(ap)
    a = ap.parse_args(argv)
    if a.mode == "init" and not a.book:
        ap.error("init requires --book")
    return {"init": cmd_init, "run": cmd_run, "status": cmd_status,
            "reconcile": cmd_reconcile, "books": cmd_books}[a.mode](a)


if __name__ == "__main__":
    sys.exit(main())
