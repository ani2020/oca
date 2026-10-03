"""span_core -- NSE Clearing SPAN risk-parameter files: parse + margin math.

Pure library: no DB, no network. Used by
  - nse_span_eod.py           (daily loader / backfill into bhavcopy.duckdb)
  - the wheel backtest        (exact historical margin per leg)
  - oc_dashboard              (exact current margin, multi-leg netting)

Source files
------------
  https://nsearchives.nseindia.com/archives/nsccl/span/nsccl.YYYYMMDD.<tag>.zip
    tag 's'          end-of-day (final)            -- use for history
    tag 'i1'..'i5'   intraday refreshes            -- 'i4' ~14:00, 'i5' ~15:30
  Archive verified back to 2020-01-02 (Oct 2026). One zip ~9-11 MB, holds a
  single SPAN 4.00 XML (.spn), ~40 MB uncompressed, ~117k option + ~1.3k
  future contracts.

What NSE's file contains (and does not)
---------------------------------------
  phyPf  per underlying: <p> price, <scanRate><priceScan>  (absolute, Rs)
  futPf  per future:     <pe> expiry, <p>, <d>, <ra> 16 scenario values
  oopPf  per option:     <series><pe> expiry, <opt><o>C|P <k> <p> <d> <v> <ra>
  NO ccDef / short-option-minimum / inter- or intra-commodity spread tiers.
  So exchange SPAN = scan risk only, netted per underlying across expiries.

Risk-array convention (verified against Zerodha on 2026-10-01 i4 file)
  a_i > 0  is a LOSS for a LONG of one unit in scenario i.
  For a signed position q (units, +long / -short): loss_i = a_i * q.
  Scan risk for an underlying = max(0, max_i sum_legs a_i * q_leg).
  Scenario order: 1-2 unchanged (vol up/down), 3-4 up 1/3, 5-6 down 1/3,
  7-8 up 2/3, 9-10 down 2/3, 11-12 up 3/3, 13-14 down 3/3, 15 extreme up,
  16 extreme down (extremes already weighted in the file values).
  RELIANCE Nov-26 1060 PE short 500: file 75.92 x 500 = 37,960; Zerodha
  SPAN 36,945 (later i5 file gives 36,815). HDFCBANK Oct-26 660 PE: file
  32,560 vs Zerodha 36,537 -- brokers/NSE can add security-specific margin
  on top, hence `broker_mult`.

Exposure margin (ELM) is NOT in the SPAN file. Default: 3.5% of underlying
value for stock F&O (SEBI: max(3.5%, 1.5 sigma)), 2% for index options.
Charged on futures and SHORT options only. Override per symbol if needed.
Measured on the two Zerodha quotes: 3.44-3.57% of notional.
"""
from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Iterator, Mapping, Optional, Union
from xml.etree.ElementTree import iterparse

ARCHIVE = "https://nsearchives.nseindia.com"
TAG_RANK = {"s": 9, "i5": 5, "i4": 4, "i3": 3, "i2": 2, "i1": 1}   # higher = later/more final

INDEX_SYMBOLS = frozenset({
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "NIFTYIT",
})
ELM_PCT = {"stock": 0.035, "index": 0.020}

N_SCEN = 16


def span_url(d: date, tag: str = "s") -> str:
    return f"{ARCHIVE}/archives/nsccl/span/nsccl.{d:%Y%m%d}.{tag}.zip"


# ---------------------------------------------------------------------------
# records
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Underlying:
    symbol: str
    price: float
    price_scan: float            # Rs, absolute
    vol_scan: float

    @property
    def psr_pct(self) -> float:
        """Price scan range as a fraction of price (e.g. 0.142)."""
        return self.price_scan / self.price if self.price else float("nan")


@dataclass(frozen=True)
class Contract:
    symbol: str
    instr: str                   # 'FUT' | 'CE' | 'PE'
    expiry: date
    strike: float                # 0.0 for futures
    price: float
    delta: float
    vol: float                   # NaN for futures
    ra: tuple                    # 16 floats, loss for LONG 1 unit

    @property
    def key(self) -> tuple:
        return contract_key(self.symbol, self.instr, self.expiry, self.strike)

    @property
    def long_scan(self) -> float:
        return max(0.0, max(self.ra))

    @property
    def short_scan(self) -> float:
        return max(0.0, -min(self.ra))


@dataclass
class SpanFile:
    trade_date: Optional[date]
    created: Optional[datetime]
    underlyings: dict = field(default_factory=dict)     # symbol -> Underlying
    contracts: dict = field(default_factory=dict)       # key -> Contract


def contract_key(symbol: str, instr: str, expiry: date, strike: float) -> tuple:
    instr = instr.upper()
    if instr in ("C", "CALL"):
        instr = "CE"
    elif instr in ("P", "PUT"):
        instr = "PE"
    elif instr.startswith("FUT"):
        instr = "FUT"
    return (symbol.upper(), instr, expiry, 0.0 if instr == "FUT" else round(float(strike), 2))


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

Source = Union[str, Path, bytes, BinaryIO]


def _open_spn(source: Source) -> BinaryIO:
    """Return a binary stream of the .spn XML from a zip/.spn path, bytes, or stream."""
    if isinstance(source, (str, Path)):
        raw = Path(source).read_bytes()
    elif isinstance(source, (bytes, bytearray)):
        raw = bytes(source)
    else:
        raw = source.read()
    if raw[:2] == b"PK":
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = [n for n in z.namelist() if n.lower().endswith(".spn")] or z.namelist()
        return io.BytesIO(z.read(names[0]))
    return io.BytesIO(raw)


def _f(text: Optional[str], default: float = float("nan")) -> float:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _d8(text: Optional[str]) -> Optional[date]:
    if not text or len(text) < 8 or text.startswith("0000"):
        return None
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def _ra(el) -> Optional[tuple]:
    ra = el.find("ra")
    if ra is None:
        return None
    vals = tuple(_f(a.text, 0.0) for a in ra.findall("a"))
    return vals if len(vals) == N_SCEN else None


def iter_span(source: Source, symbols: Optional[set] = None) -> Iterator[Union[tuple, Underlying, Contract]]:
    """Stream records out of a SPAN file without holding the whole tree.

    Yields, in file order:
      ('meta', trade_date, created)   once, before the first portfolio
      Underlying                      one per phyPf
      Contract                        one per future / option

    `symbols` restricts output to those underlyings (upper-case set).
    NSE test portfolios (pfCode ending 'NSETEST') are skipped.
    ~5 s for a full day file (~120k contracts).
    """
    stream = _open_spn(source)
    stack: list[str] = []
    pf_kind: Optional[str] = None          # 'phyPf' | 'futPf' | 'oopPf'
    pf_code: Optional[str] = None
    series_pe: Optional[date] = None
    trade_date = created = None
    meta_sent = False

    def keep() -> bool:
        if not pf_code or pf_code.endswith("NSETEST"):
            return False
        return symbols is None or pf_code in symbols

    for event, el in iterparse(stream, events=("start", "end")):
        tag = el.tag
        if event == "start":
            stack.append(tag)
            if tag in ("phyPf", "futPf", "oopPf"):
                pf_kind, pf_code, series_pe = tag, None, None
                if not meta_sent:
                    yield ("meta", trade_date, created)
                    meta_sent = True
            continue

        # ---- end events ---------------------------------------------------
        stack.pop()
        parent = stack[-1] if stack else None

        if tag == "created" and created is None and el.text:
            t = el.text.strip()
            try:
                created = datetime.strptime(t[:12], "%Y%m%d%H%M")
            except ValueError:
                created = None
        elif tag == "date" and parent == "pointInTime" and trade_date is None:
            trade_date = _d8((el.text or "").strip())
        elif tag == "pfCode" and parent in ("phyPf", "futPf", "oopPf"):
            # parent check matters: <undPf><pfCode> inside an oopPf must not
            # overwrite the option portfolio's own code
            pf_code = (el.text or "").strip().upper()
        elif tag == "pe" and parent == "series":
            series_pe = _d8((el.text or "").strip())

        elif tag == "phy" and pf_kind == "phyPf":
            if keep():
                sr = el.find("scanRate")
                yield Underlying(
                    symbol=pf_code,
                    price=_f(el.findtext("p")),
                    price_scan=_f(sr.findtext("priceScan")) if sr is not None else float("nan"),
                    vol_scan=_f(sr.findtext("volScan")) if sr is not None else float("nan"),
                )
            el.clear()

        elif tag == "fut" and pf_kind == "futPf":
            ra = _ra(el)
            exp = _d8((el.findtext("pe") or "").strip())
            if keep() and ra is not None and exp is not None:
                yield Contract(pf_code, "FUT", exp, 0.0, _f(el.findtext("p")),
                               _f(el.findtext("d")), float("nan"), ra)
            el.clear()

        elif tag == "opt" and pf_kind == "oopPf":
            ra = _ra(el)
            if keep() and ra is not None and series_pe is not None:
                o = (el.findtext("o") or "").strip().upper()
                yield Contract(pf_code, "CE" if o == "C" else "PE", series_pe,
                               round(_f(el.findtext("k")), 2), _f(el.findtext("p")),
                               _f(el.findtext("d")), _f(el.findtext("v")), ra)
            el.clear()

        elif tag in ("phyPf", "futPf", "oopPf"):
            pf_kind = pf_code = series_pe = None
            el.clear()
        elif tag == "series":
            el.clear()

    if not meta_sent:
        yield ("meta", trade_date, created)


def parse_span(source: Source, symbols: Optional[set] = None) -> SpanFile:
    """Load a whole SPAN file into memory (one day ~120k contracts)."""
    out = SpanFile(None, None)
    for rec in iter_span(source, symbols):
        if isinstance(rec, tuple):
            _, out.trade_date, out.created = rec
        elif isinstance(rec, Underlying):
            out.underlyings[rec.symbol] = rec
        else:
            out.contracts[rec.key] = rec
    return out


# ---------------------------------------------------------------------------
# margin
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Leg:
    """One position. qty in UNITS (shares), signed: +long, -short.

    For a lot-based caller: Leg(sym, 'PE', exp, 1060, -1 * lot_size).
    """
    symbol: str
    instr: str
    expiry: date
    strike: float
    qty: float

    @property
    def key(self) -> tuple:
        return contract_key(self.symbol, self.instr, self.expiry, self.strike)


class SpanBook:
    """Lookup interface over one SPAN file (one trade_date + tag).

    Any object with .contract(key) and .underlying(symbol) works with
    portfolio_margin(); book_from_db() builds one of these in memory.
    """

    def __init__(self, underlyings: Mapping[str, Underlying], contracts: Mapping[tuple, Contract],
                 trade_date: Optional[date] = None, tag: Optional[str] = None,
                 created: Optional[datetime] = None):
        self._und = dict(underlyings)
        self._con = dict(contracts)
        self.trade_date, self.tag, self.created = trade_date, tag, created

    @classmethod
    def from_file(cls, source: Source, symbols: Optional[set] = None, tag: Optional[str] = None) -> "SpanBook":
        sf = parse_span(source, symbols)
        return cls(sf.underlyings, sf.contracts, sf.trade_date, tag, sf.created)

    def contract(self, key: tuple) -> Optional[Contract]:
        return self._con.get(key)

    def underlying(self, symbol: str) -> Optional[Underlying]:
        return self._und.get(symbol.upper())

    def expiries(self, symbol: str, instr: Optional[str] = None) -> list:
        s = symbol.upper()
        return sorted({k[2] for k in self._con if k[0] == s and (instr is None or k[1] == instr)})

    def strikes(self, symbol: str, instr: str, expiry: date) -> list:
        s, i = symbol.upper(), instr.upper()
        return sorted(k[3] for k in self._con if k[0] == s and k[1] == i and k[2] == expiry)

    def __len__(self) -> int:
        return len(self._con)


def elm_pct(symbol: str, overrides: Optional[Mapping[str, float]] = None) -> float:
    s = symbol.upper()
    if overrides and s in overrides:
        return float(overrides[s])
    return ELM_PCT["index" if s in INDEX_SYMBOLS else "stock"]


@dataclass
class MarginResult:
    span: float                 # exchange scan risk, netted per underlying
    exposure: float             # ELM on futures + short options
    total: float                # span * broker_mult + exposure
    premium_received: float     # credit from short options (not netted in)
    by_underlying: dict
    missing: list               # legs not found in the book

    @property
    def ok(self) -> bool:
        return not self.missing


def portfolio_margin(
    legs: Iterable[Leg],
    book,
    elm_overrides: Optional[Mapping[str, float]] = None,
    broker_mult: float = 1.0,
    elm_fn: Optional[Callable[[str], float]] = None,
) -> MarginResult:
    """SPAN + exposure for a set of legs, netting risk arrays per underlying.

    Netting is real SPAN behaviour: a short strangle can even price slightly
    BELOW its worst single leg, because in the put's worst scenario the short
    call decays and its gain offsets. broker_mult scales the SPAN component
    only (broker / ad-hoc add-ons).
    """
    per_und: dict[str, list] = {}
    expo = 0.0
    prem = 0.0
    missing = []
    for leg in legs:
        c = book.contract(leg.key)
        if c is None:
            missing.append(leg)
            continue
        vec = per_und.setdefault(c.symbol, [0.0] * N_SCEN)
        for i, a in enumerate(c.ra):
            vec[i] += a * leg.qty
        short = leg.qty < 0
        if c.instr == "FUT" or short:
            u = book.underlying(c.symbol)
            ref = u.price if u is not None and u.price == u.price else c.price
            pct = elm_fn(c.symbol) if elm_fn else elm_pct(c.symbol, elm_overrides)
            expo += pct * ref * abs(leg.qty)
        if c.instr != "FUT" and short:
            prem += c.price * -leg.qty

    by_und = {s: max(0.0, max(v)) for s, v in per_und.items()}
    span = sum(by_und.values())
    total = span * broker_mult + expo
    return MarginResult(round(span, 2), round(expo, 2), round(total, 2), round(prem, 2),
                        {k: round(v, 2) for k, v in by_und.items()}, missing)


def short_option_margin(book, symbol: str, instr: str, expiry: date, strike: float,
                        lot: int, lots: int = 1, **kw) -> Optional[MarginResult]:
    """Convenience: one naked short option, `lots` lots."""
    r = portfolio_margin([Leg(symbol, instr, expiry, strike, -lot * lots)], book, **kw)
    return None if r.missing else r


def return_on_margin(premium: float, margin: float) -> Optional[float]:
    return premium / margin if margin and margin > 0 else None


# ---------------------------------------------------------------------------
# DB-backed books (tables written by nse_span_eod.py into bhavcopy.duckdb)
# ---------------------------------------------------------------------------

def latest_file(con, on_or_before: Optional[date] = None, prefix: str = "") -> Optional[tuple]:
    """(trade_date, tag) of the most final SPAN file stored, optionally capped by date.

    `con` is any DB-API-ish connection with .execute(sql, params).fetchall();
    `prefix` lets callers point at an ATTACHed db, e.g. 'bc.'.
    """
    where, params = "", []
    if on_or_before is not None:
        where, params = "WHERE trade_date <= ?", [on_or_before]
    rows = con.execute(
        f"SELECT trade_date, tag FROM {prefix}span_ingest_log {where} "
        f"ORDER BY trade_date DESC", params).fetchall()
    if not rows:
        return None
    top = rows[0][0]
    tags = [t for d, t in rows if d == top]
    return top, max(tags, key=lambda t: TAG_RANK.get(t, 0))


def book_from_db(con, trade_date: Optional[date] = None, tag: Optional[str] = None,
                 symbols: Optional[Iterable[str]] = None, prefix: str = "") -> Optional[SpanBook]:
    """Load one stored SPAN file into an in-memory SpanBook.

    Needs risk arrays (column `ra`), which the loader keeps for 'daily'/'live'
    runs and prunes after N days. For history without arrays, single-leg
    margins are still available in SQL via span_contract.short_scan /
    long_scan -- see nse_span_eod.py.

    Loads into memory and returns, so a reader (the dashboard) never holds a
    DuckDB handle across the writer's ingest window.
    """
    if trade_date is None or tag is None:
        lf = latest_file(con, trade_date, prefix)
        if lf is None:
            return None
        trade_date, tag = lf
    sym_sql, params = "", [trade_date, tag]
    if symbols:
        syms = sorted({s.upper() for s in symbols})
        sym_sql = f" AND symbol IN ({', '.join('?' * len(syms))})"
        params += syms
    und = {}
    for s, p, ps, vs in con.execute(
            f"SELECT symbol, price, price_scan, vol_scan FROM {prefix}span_und "
            f"WHERE trade_date = ? AND tag = ?{sym_sql}", params).fetchall():
        und[s] = Underlying(s, p, ps, vs)
    cons = {}
    for s, ins, exp, k, p, d, v, ra in con.execute(
            f"SELECT symbol, instr, expiry, strike, price, delta, vol, ra FROM {prefix}span_contract "
            f"WHERE trade_date = ? AND tag = ? AND ra IS NOT NULL{sym_sql}", params).fetchall():
        c = Contract(s, ins, exp, k, p, d, v if v is not None else float("nan"), tuple(ra))
        cons[c.key] = c
    if not cons:
        return None
    return SpanBook(und, cons, trade_date, tag)
