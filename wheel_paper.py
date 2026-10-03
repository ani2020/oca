"""
wheel_paper -- daily paper trading of a wheel_bt configuration on live closing data.

Same decision code as the backtest (wheel_bt.step), so a paper book behaves
exactly like the tested configuration. What differs is the data each day
is fed and how fills are priced:

                 backtest                      paper
  prices         NSE settle (SPAN price)       live mid from the closing chain
  entry fill     settle x (1 - slippage)       live BID
  exit fill      settle x (1 + slippage)       live ASK
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

Validation the backtest cannot give
-----------------------------------
Every fill is stored with the live LTP / bid / ask / mid. The next run
reconciles each fill against that day's NSE settle (bhavcopy, loaded in the
19:30 batch) and stores slip_vs_settle, the real cost of trading at the
quote relative to the price the backtest assumes. Compare it with the
backtest's slippage parameter (default 10% of premium).

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
    python wheel_paper.py status [--book main]     # positions, P&L, slippage vs settle
    python wheel_paper.py reconcile                # settle-match past fills (also done by run)
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
from dataclasses import asdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Optional

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
    return con


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

def load_chain(con, d: date, snap_after: time, max_stale_min: int = 30) -> tuple[pd.DataFrame, Optional[datetime]]:
    """Latest closing snapshot per universe symbol for day d, both sides stacked long."""
    lo = datetime.combine(d, snap_after)
    hi = datetime.combine(d, time(23, 59))
    df = con.execute("""
        WITH u AS (SELECT DISTINCT symbol FROM index_constituents),
        last AS (SELECT symbol, max(timestamp) ts FROM ocdata
                 WHERE timestamp BETWEEN ? AND ? AND symbol IN (SELECT symbol FROM u) GROUP BY 1)
        SELECT o.symbol, o.timestamp ts, o.expiry, o.strike_price strike, o.underlying_price und,
               o.lotsize lot, o.ce_ltp, o.ce_bid, o.ce_ask, o.ce_volume, o.ce_oi, o.ce_delta,
               o.pe_ltp, o.pe_bid, o.pe_ask, o.pe_volume, o.pe_oi, o.pe_delta
        FROM ocdata o JOIN last l ON o.symbol = l.symbol AND o.timestamp = l.ts
    """, [lo, hi]).df()
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
    """Latest stored SPAN file on or before d -> {(raw, instr, expiry, strike): (short_scan, delta)}, und, tag."""
    lf = sp.latest_file(con, d, prefix="bc.")
    if lf is None:
        return {}, {}, None
    fd, tag = lf
    syms = sorted(symbols)
    ph = ", ".join("?" * len(syms))
    con_rows = con.execute(f"""
        SELECT symbol, instr, expiry, strike, short_scan, delta FROM bc.span_contract
        WHERE trade_date = ? AND tag = ? AND instr IN ('CE', 'PE') AND symbol IN ({ph})""",
        [fd, tag] + syms).fetchall()
    und = dict(con.execute(f"SELECT symbol, price FROM bc.span_und WHERE trade_date = ? AND tag = ? "
                           f"AND symbol IN ({ph})", [fd, tag] + syms).fetchall())
    lk = {(s, i, wb._d(e), round(float(k), 2)): (sc, dl) for s, i, e, k, sc, dl in con_rows}
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
    x = x[(x["volume"] >= max(p.min_entry_volume, 1)) & (x["bid"] > 0) & (x["mid"] > 0) & (x["lot"] > 0)]
    if x.empty:
        return x
    sc, dl = [], []
    for r in x.itertuples(index=False):
        v = span.get((r.symbol, r.opt_type, r.expiry, round(float(r.strike), 2)))
        sc.append(v[0] if v else NAN)
        dl.append(v[1] if v and v[1] == v[1] else NAN)
    x["short_scan"] = sc
    x["delta_span"] = dl
    x = x[x["short_scan"].notna()]
    x["delta"] = x["delta_span"].abs().fillna(x["delta"].abs())
    x["margin"] = x["short_scan"] * x["lot"] * p.broker_mult + p.elm * x["und"] * x["lot"]
    x["rom"] = x["mid"] * x["lot"] / x["margin"]
    x = x[(x["rom"] >= p.rom_target) & (x["delta"] <= p.delta_cap)]
    if x.empty:
        return x
    x = x.sort_values("delta").groupby(["symbol", "opt_type"], as_index=False).first()
    x["move"] = [(u / pclose[s] - 1) if pclose.get(s) else NAN for s, u in zip(x["symbol"], x["und"])]
    return pd.DataFrame({
        "trade_date": d, "symbol": x["symbol"], "opt_type": x["opt_type"], "raw": x["symbol"],
        "expiry": x["expiry"], "strike": x["strike"].astype(float), "premium": x["mid"], "lot": x["lot"],
        "margin": x["margin"], "rom": x["rom"], "delta": x["delta"], "und": x["und"], "oi": x["oi"],
        "volume": x["volume"], "move": x["move"], "bid": x["bid"], "ask": x["ask"], "ltp": x["ltp"]})


# ---------------------------------------------------------------------------
# state persistence
# ---------------------------------------------------------------------------

POS_COLS = ["pid", "symbol", "raw", "side", "expiry", "strike", "lot", "lots", "entry_date", "entry_px",
            "credit", "entry_cost", "margin", "sector", "mark", "delta", "und"]


def load_book(oc, book: str) -> tuple[wb.Params, wb.State, Optional[date]]:
    row = oc.execute("SELECT params FROM paper_books WHERE book = ?", [book]).fetchone()
    if row is None:
        raise SystemExit(f"no paper book {book!r}; create it with: wheel_paper.py init --book {book} ...")
    p = wb.params_from_json(row[0])
    srow = oc.execute("SELECT cash, pid, last_run FROM paper_state WHERE book = ?", [book]).fetchone()
    st = wb.State(cash=p.cash) if srow is None else wb.State(cash=srow[0], pid=srow[1])
    for r in oc.execute(f"SELECT {', '.join(POS_COLS)} FROM paper_positions WHERE book = ? ORDER BY pid",
                        [book]).fetchall():
        d = dict(zip(POS_COLS, r))
        st.book.append(wb.Pos(d["pid"], d["symbol"], d["raw"], d["side"], wb._d(d["expiry"]), d["strike"],
                              d["lot"], wb._d(d["entry_date"]), d["entry_px"], d["credit"], d["entry_cost"],
                              d["margin"], d["sector"], mark=d["mark"], delta=d["delta"], und=d["und"],
                              lots=d["lots"]))
    return p, st, (srow[2] if srow else None)


def snapshot_state(oc, book: str, d: date) -> None:
    """Store the book's state as it was BEFORE day d's run (once per day)."""
    if oc.execute("SELECT 1 FROM paper_snapshots WHERE book = ? AND date = ?", [book, d]).fetchone():
        return
    srow = oc.execute("SELECT cash, pid, last_run FROM paper_state WHERE book = ?", [book]).fetchone()
    pos = oc.execute(f"SELECT {', '.join(POS_COLS)} FROM paper_positions WHERE book = ? ORDER BY pid", [book]).df()
    oc.execute("INSERT INTO paper_snapshots VALUES (?, ?, ?, ?, ?, ?, now())",
               [book, d, srow[0] if srow else None, srow[1] if srow else None, srow[2] if srow else None,
                pos.to_json(orient="records", date_format="iso")])


def restore_state(oc, book: str, d: date) -> bool:
    """Undo day d for a book: restore its pre-run state, drop that day's trades/fills/daily row."""
    row = oc.execute("SELECT cash, pid, last_run, positions FROM paper_snapshots WHERE book = ? AND date = ?",
                     [book, d]).fetchone()
    if row is None:
        return False
    cash, pid, last_run, positions = row
    oc.execute("BEGIN")
    try:
        oc.execute("DELETE FROM paper_positions WHERE book = ?", [book])
        for r in json.loads(positions or "[]"):
            r["expiry"] = r["expiry"][:10]
            r["entry_date"] = r["entry_date"][:10]
            oc.execute(f"INSERT INTO paper_positions VALUES ({', '.join('?' * (len(POS_COLS) + 1))})",
                       [book] + [r[c] for c in POS_COLS])
        oc.execute("DELETE FROM paper_trades WHERE book = ? AND exit_date = ?", [book, d])
        oc.execute("DELETE FROM paper_fills WHERE book = ? AND date = ?", [book, d])
        oc.execute("DELETE FROM paper_daily WHERE book = ? AND date = ?", [book, d])
        oc.execute("DELETE FROM paper_state WHERE book = ?", [book])
        if cash is not None:
            oc.execute("INSERT INTO paper_state VALUES (?, ?, ?, ?, now())", [book, cash, pid, last_run])
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
            oc.execute(f"INSERT INTO paper_positions VALUES ({', '.join('?' * (len(POS_COLS) + 1))})",
                       [book] + [getattr(ps, c) for c in POS_COLS])
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
        oc.execute("INSERT INTO paper_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   [book, d, snap, eq["equity"], eq["cash"], eq["liability"], eq["used_margin"], eq["capacity"],
                    eq["n_open"], eq["n_new"], len(closed), eq["breach"], eq["cash_rule_breach"], span_file])
        oc.execute("DELETE FROM paper_state WHERE book = ?", [book])
        oc.execute("INSERT INTO paper_state VALUES (?, ?, ?, ?, now())", [book, st.cash, st.pid, d])
        oc.execute("COMMIT")
    except Exception:
        oc.execute("ROLLBACK")
        raise


# ---------------------------------------------------------------------------
# reconcile fills against NSE settle
# ---------------------------------------------------------------------------

def reconcile(oc, bhav_db: Path = wb.BHAV_DB) -> int:
    """Fill settle + slip_vs_settle for past fills once that day's bhavcopy is loaded.

    slip_vs_settle > 0 means the quote fill was WORSE than settle (the
    backtest's reference price): sold below it, or bought back above it.
    """
    oc.execute(f"ATTACH IF NOT EXISTS '{bhav_db}' AS bcr (READ_ONLY)")
    try:
        n0 = oc.execute("SELECT count(*) FROM paper_fills WHERE settle IS NULL").fetchone()[0]
        oc.execute("""
            UPDATE paper_fills AS f SET settle = o.settle,
                slip_vs_settle = CASE WHEN o.settle > 0 THEN
                    CASE f.action WHEN 'S' THEN (o.settle - f.fill) / o.settle
                                  ELSE (f.fill - o.settle) / o.settle END END
            FROM bcr.options_eod AS o
            WHERE f.settle IS NULL AND o.trade_date = f.date AND o.symbol = f.raw AND o.expiry = f.expiry
              AND o.strike = f.strike AND o.opt_type = f.side AND o.settle IS NOT NULL""")
        n1 = oc.execute("SELECT count(*) FROM paper_fills WHERE settle IS NULL").fetchone()[0]
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
        rows[k] = (r.ltp, float(r.volume or 0), r.mid, dl, sscan, r.und, r.ask)

    def row_of(k):
        return rows.get(k)

    missing = [ps.key for ps in st.book if ps.key not in rows]
    if missing:
        print(f"  [{book}] warn: {len(missing)} open contract(s) not in today's chain; carrying last mark: {missing[:4]}")
    cands = build_candidates(p, d, ch, span, pclose)
    closed, opened, eq = wb.step(p, st, d, ctx, row_of, cands, fill_mode="quote")
    save_book(oc, book, d, snap, st, closed, opened, eq, quotes, span_file, raw_by_pid)

    print(f"\n  [{book}] {d}  snapshot {snap:%H:%M}  span {span_file}  candidates={len(cands)}")
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

def cmd_run(a) -> int:
    d = a.date or date.today()
    hol = holidays()
    if d.weekday() >= 5 or d in hol:
        print(f"  {d} is not a trading day; nothing to do")
        return 0
    oc = out_con()
    try:
        books = [a.book] if a.book else [r[0] for r in oc.execute("SELECT book FROM paper_books ORDER BY 1").fetchall()]
        if not books:
            print("  no paper books; create one with: wheel_paper.py init --book NAME ...")
            return 1
        n = reconcile(oc)
        if n:
            print(f"  reconciled {n} fill(s) against NSE settle")
    finally:
        oc.close()

    if not a.no_span:
        refresh_span(d)
    ic = in_con()
    try:
        ch, snap = load_chain(ic, d, a.snap_after)
        if snap is None:
            print(f"  no chain snapshot at/after {a.snap_after} on {d}; has the closing pull run? Not trading.")
            return 2
        syms = set(ch["symbol"])
        n_univ = ic.execute("SELECT count(DISTINCT symbol) FROM index_constituents").fetchone()[0]
        if n_univ and len(syms) / n_univ < a.min_coverage:
            print(f"  only {len(syms)}/{n_univ} universe symbols have a closing snapshot "
                  f"(< {a.min_coverage:.0%}); chain pull still running or failed. Not trading.")
            return 3
        span, _, span_file = span_lookup(ic, d, syms)
        if span_file is None or not span_file.startswith(str(d)):
            print(f"  warn: margin from SPAN file {span_file} (none stored for {d} yet)")
        pclose = prev_close(ic, d, syms)
        res, ca = wb.load_events(ic, d - timedelta(days=10), d + timedelta(days=120))
        ctx = wb.Ctx(build_calendar(ic, d, hol), res, ca, dict(wb.SECTOR))
    finally:
        ic.close()

    oc = out_con()
    try:
        for b in books:
            run_book(oc, b, d, force=a.force, ch=ch, snap=snap, span=span, span_file=span_file,
                     pclose=pclose, ctx=ctx)
    finally:
        oc.close()
    return 0


def cmd_init(a) -> int:
    p = wb.params_from_args(a)
    oc = out_con()
    try:
        if oc.execute("SELECT 1 FROM paper_books WHERE book = ?", [a.book]).fetchone() and not a.replace:
            print(f"  book {a.book!r} exists; --replace to overwrite its params (positions/history are kept)")
            return 1
        oc.execute("DELETE FROM paper_books WHERE book = ?", [a.book])
        oc.execute("INSERT INTO paper_books VALUES (?, ?, now(), ?)", [a.book, wb.params_to_json(p), a.note or ""])
    finally:
        oc.close()
    print(f"  book {a.book!r} saved:")
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
            sl = oc.execute(f"""SELECT book, action, count(*) fills, count(settle) settled,
                round(100 * avg(slip_vs_settle), 2) avg_slip_pct, round(100 * median(slip_vs_settle), 2) med_slip_pct,
                round(100 * avg((ask - bid) / nullif(mid, 0)), 2) avg_spread_pct
                FROM paper_fills {where} GROUP BY 1, 2 ORDER BY 1, 2""", prm).df()
            print("\n  Fill quality vs NSE settle (backtest assumes 10% of premium each side):\n  "
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
        for book, params, created, note in oc.execute("SELECT * FROM paper_books ORDER BY 1").fetchall():
            p = wb.params_from_json(params)
            diff = {k: v for k, v in asdict(p).items() if v != getattr(wb.Params(), k)}
            print(f"  {book}  (created {created:%Y-%m-%d})  {note or ''}\n    {diff}")
    finally:
        oc.close()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["init", "run", "status", "reconcile", "books"])
    ap.add_argument("--book")
    ap.add_argument("--date", type=lambda s: datetime.strptime(s, "%Y-%m-%d").date())
    ap.add_argument("--snap-after", type=lambda s: datetime.strptime(s, "%H:%M").time(), default=time(15, 30),
                    help="use the latest chain snapshot at/after this time (default 15:30)")
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
