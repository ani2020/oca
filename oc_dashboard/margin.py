"""Short-option margin approximation.

Replaces the per-contract ICICI margin fetch for screening purposes. The point
is RELATIVE ordering across contracts and symbols, not an exact rupee figure:
ICICI and Zerodha disagree on RMS add-ons anyway, so tuning to either would be
fitting noise. Everything in a class is computed identically.

Shape follows SPAN rather than the Reg-T retail formula:

    scan_pct   = max(min_scan, K_SIGMA * sigma * sqrt(HORIZON / 243))
    base_pct   = scan_pct + exposure_pct
    floor_pct  = FLOOR_FRAC * base_pct

    otm        = max(0, K - F)   for a short call
                 max(0, F - K)   for a short put
    per_share  = premium + max(base_pct * F - otm, floor_pct * F)
    margin     = per_share * lot_size

Three deliberate differences from the Reg-T version this replaces:

1. base_pct is IV-SCALED. A flat 20% ranked an 11-vol index alongside a 60-vol
   single stock at equal notional, which defeats the purpose of a relative
   measure. SPAN's scan range is volatility-driven; this follows it.
2. Class-based constants (index vs stock) rather than one pair for everything.
3. All constants live in config, not as literals here.

Sanity check against real NSE levels:
    NIFTY   @ 11 vol  ->  ~8.0% of notional
    stock   @ 30 vol  ->  ~14.5%
    stock   @ 60 vol  ->  ~24.1%

CAVEAT: on far-OTM shorts the variable term goes negative and the floor takes
over, so margin ~ premium + floor_pct * F. Real SPAN is lower there, so RoM on
deep-OTM strikes reads pessimistic. Fine for ranking; do not read as absolute.
"""
from __future__ import annotations

from math import sqrt
from typing import Iterable, Optional

from . import config


def _class_of(symbol: str) -> str:
    return "index" if str(symbol).upper() in config.NSE_INDICES else "stock"


def base_pct(symbol: str, iv_pct: float) -> float:
    """Volatility-scaled base risk percentage for one instrument."""
    min_scan, exposure = config.MARGIN_PARAMS[_class_of(symbol)]
    sigma = max(float(iv_pct or 0.0), 0.0) / 100.0
    scan = config.MARGIN_K_SIGMA * sigma * sqrt(config.MARGIN_HORIZON_DAYS / 243.0)
    return max(min_scan, scan) + exposure


def short_margin(
    symbol: str,
    option_type: str,        # "CE" / "call"  |  "PE" / "put"
    ref_price: float,        # futures price preferred; spot acceptable
    strike: float,
    premium: float,
    lot_size: int,
    iv_pct: float,
) -> Optional[float]:
    """Approximate cash blocked for ONE naked short leg. None if inputs invalid."""
    try:
        ref = float(ref_price or 0.0)
        k = float(strike or 0.0)
        lot = int(lot_size or 0)
    except (TypeError, ValueError):
        return None
    if ref <= 0 or k <= 0 or lot <= 0:
        return None

    is_call = str(option_type).upper().startswith("C")
    otm = max(0.0, k - ref) if is_call else max(0.0, ref - k)

    bp = base_pct(symbol, iv_pct)
    variable = bp * ref - otm
    floor = config.MARGIN_FLOOR_FRAC * bp * ref

    # Safety multiplier applies to the RISK term only. The premium is exact —
    # you receive it and it is blocked — so all the approximation error lives
    # in max(variable, floor). See config.MARGIN_SAFETY_MULT.
    risk = max(variable, floor) * getattr(config, "MARGIN_SAFETY_MULT", 1.0)

    per_share = max(float(premium or 0.0), 0.0) + risk
    return round(per_share * lot, 2)


def combined_margin(legs: Iterable[Optional[float]]) -> Optional[float]:
    """Netted margin for a multi-leg position.

    margin = max(leg) + MARGIN_NET_K * sum(remaining legs)

    SPAN nets opposing legs heavily. Costing a short straddle at 2x a single leg
    would understate RoM by roughly 40%, pushing straddles down any RoM-sorted
    list purely as an artifact of the margin model.
    """
    vals = sorted((float(v) for v in legs if v is not None), reverse=True)
    if not vals:
        return None
    return round(vals[0] + config.MARGIN_NET_K * sum(vals[1:]), 2)


def return_on_margin(premium_collected: float, margin: Optional[float]) -> Optional[float]:
    """Premium as a percentage of cash blocked. None when margin is unusable."""
    if not margin or float(margin) <= 0:
        return None
    return round(float(premium_collected) / float(margin) * 100.0, 2)


# ═══════════════════════════════════════════════════════════════════════════
# Exact exchange SPAN (preferred when available)
# ═══════════════════════════════════════════════════════════════════════════
#
# nse_span_eod.py stores NSE Clearing's daily SPAN risk arrays in
# bhavcopy.duckdb; span_core turns them into exchange margin. When a contract
# is in the stored file this is the real number (SPAN + ELM), not a model:
# verified within 0-3% of Zerodha on RELIANCE; brokers may add security-
# specific margin on top (config.SPAN_BROKER_MULT).
#
# Every span_* function returns None when it cannot price -- tables absent,
# DB locked by the nightly writer, arrays pruned for an old date, strike not
# in the file -- and callers fall back to the approximation above. Nothing
# here may raise into a route.
#
# The two models block different things: the approximation is
# premium + risk (Reg-T-like shape); exchange SPAN prices the option value
# inside its scenarios and is what the broker actually blocks. On a far-OTM
# RELIANCE put (2026-10-01) the approximation read ~19% below exchange.

import threading
import time
from datetime import date, datetime

try:                                     # launch from od\  -> oc.span_core
    import oc.span_core as _sp
except ImportError:                      # launch from od\oc -> span_core
    try:
        import span_core as _sp
    except ImportError:                  # pragma: no cover
        _sp = None

_BOOK_LOCK = threading.Lock()
# Resolution cache: request ('latest' or a date) -> (checked_at, file key or None).
# File cache: file key (trade_date, tag) -> _SpanData. BOTH cache misses too:
# without that, a day with no stored arrays re-scanned span_contract once per
# screener ROW (thousands of rows -> minutes of CPU; seen 2026-10-02).
_REQ_CACHE: dict = {}
_FILE_CACHE: dict = {}


def _as_date(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:                                 # pandas Timestamp, numpy datetime64, str
        return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


class _SpanData:
    """One stored SPAN file, loaded once.

    book : full SpanBook with risk arrays (needed to NET multi-leg structures);
           None when the arrays were not kept (backfilled history, or pruned).
    scan : {(symbol, instr, expiry, strike): short_scan} -- always available,
           enough for an exact single-leg short margin on any stored day.
    und  : {symbol: underlying price} for the ELM leg.
    vol  : {(symbol, expiry): SPAN vol (fraction)} -- NSE's one risk vol per
           underlying x expiry (identical across strikes and CE/PE). Used by the
           Playbook's ATM IV / SPAN vol richness flag (added 2026-10-26).
    key  : (trade_date, tag) of the stored file.
    """
    __slots__ = ("book", "scan", "und", "vol", "key")

    def __init__(self, book, scan, und, vol=None, key=None):
        self.book, self.scan, self.und = book, scan, und
        self.vol = vol or {}
        self.key = key


def _span_con():
    import duckdb
    return duckdb.connect(str(config.SPAN_DB), read_only=True)


def _load_file(con, key) -> "_SpanData":
    fd, tag = key
    book = _sp.book_from_db(con, fd, tag)          # None when arrays absent
    if book is not None:
        scan = {k: c.short_scan for k, c in book._con.items() if k[1] in ("CE", "PE")}
        und = {s: u.price for s, u in book._und.items()}
        vol = {}
        for k, c in book._con.items():
            if k[1] in ("CE", "PE") and c.vol == c.vol and c.vol > 0:
                vol.setdefault((k[0], k[2]), c.vol)
    else:
        scan = {}
        for s, ins, exp, k, sc in con.execute(
                "SELECT symbol, instr, expiry, strike, short_scan FROM span_contract "
                "WHERE trade_date = ? AND tag = ? AND instr IN ('CE', 'PE')", [fd, tag]).fetchall():
            scan[_sp.contract_key(s, ins, _as_date(exp), k)] = sc
        und = dict(con.execute("SELECT symbol, price FROM span_und WHERE trade_date = ? AND tag = ?",
                               [fd, tag]).fetchall())
        vol = {(s, _as_date(exp)): v for s, exp, v in con.execute(
                "SELECT symbol, expiry, median(vol) FROM span_contract "
                "WHERE trade_date = ? AND tag = ? AND instr IN ('CE', 'PE') AND vol > 0 "
                "GROUP BY 1, 2", [fd, tag]).fetchall()}
    return _SpanData(book, scan, und, vol, key)


def span_data(as_of=None) -> Optional["_SpanData"]:
    """The stored SPAN file to price against, or None. One DB visit per request
    key per SPAN_RELOAD_SEC; every later call is a dict lookup.

    as_of=None  -> the latest stored file.
    as_of=date  -> the latest file on or before that date (historical views).
    """
    if _sp is None or not getattr(config, "SPAN_ENABLED", True):
        return None
    d = _as_date(as_of)
    req = d or "latest"
    now = time.time()
    try:
        with _BOOK_LOCK:
            hit = _REQ_CACHE.get(req)
            if hit is not None and now - hit[0] < config.SPAN_RELOAD_SEC:
                key = hit[1]
            else:
                con = _span_con()
                try:
                    key = _sp.latest_file(con, d)
                    if key is not None and key not in _FILE_CACHE:
                        while len(_FILE_CACHE) >= config.SPAN_CACHE_FILES:
                            _FILE_CACHE.pop(next(iter(_FILE_CACHE)))
                        _FILE_CACHE[key] = _load_file(con, key)
                finally:
                    con.close()
                if len(_REQ_CACHE) > 256:
                    _REQ_CACHE.clear()
                _REQ_CACHE[req] = (now, key)
            return _FILE_CACHE.get(key) if key is not None else None
    except Exception:                    # locked / tables missing / anything
        if req not in _REQ_CACHE:        # negative-cache failures too, so a locked DB
            _REQ_CACHE[req] = (now, None)  # doesn't cost a connect attempt per row
        return None


def span_book(as_of=None):
    """Full SpanBook (with risk arrays) or None. Kept for callers that net legs."""
    sd = span_data(as_of)
    return sd.book if sd is not None else None


def _elm_kw() -> dict:
    return {"broker_mult": getattr(config, "SPAN_BROKER_MULT", 1.0),
            "elm_overrides": getattr(config, "SPAN_ELM_OVERRIDES", None)}


def span_short_margin(symbol: str, option_type: str, expiry, strike: float,
                      lot_size: int, as_of=None) -> Optional[float]:
    """Exact SPAN + ELM for ONE naked short lot, or None. Needs only short_scan,
    so it works on every stored day, arrays or not."""
    sd = span_data(as_of)
    exp = _as_date(expiry)
    if sd is None or exp is None:
        return None
    try:
        k = _sp.contract_key(symbol, option_type, exp, float(strike))
        sc = sd.scan.get(k)
        u = sd.und.get(k[0])
        if sc is None or u is None:
            return None
        lot = int(lot_size)
        mult = getattr(config, "SPAN_BROKER_MULT", 1.0)
        elm = _sp.elm_pct(k[0], getattr(config, "SPAN_ELM_OVERRIDES", None))
        return round(max(0.0, sc) * lot * mult + elm * u * lot, 2)
    except Exception:
        return None


def span_combined_margin(symbol: str, legs, lot_size: int, as_of=None) -> Optional[float]:
    """Exact netted SPAN + ELM for short legs [(option_type, expiry, strike), ...].

    All-or-nothing: if any leg is missing returns None, so a partly exact
    figure is never mixed with an approximated one.
    """
    book = span_book(as_of)
    if book is None:
        return None
    try:
        span_legs = []
        for otype, expiry, strike in legs:
            exp = _as_date(expiry)
            if exp is None:
                return None
            span_legs.append(_sp.Leg(symbol, otype, exp, float(strike), -int(lot_size)))
        r = _sp.portfolio_margin(span_legs, book, **_elm_kw())
        return None if r.missing else r.total
    except Exception:
        return None


def best_short_margin(symbol, option_type, ref_price, strike, premium, lot_size, iv_pct,
                      expiry=None, as_of=None) -> tuple:
    """(margin, source): exchange SPAN when available, else the approximation."""
    if expiry is not None:
        m = span_short_margin(symbol, option_type, expiry, strike, lot_size, as_of)
        if m is not None:
            return m, "span"
    return short_margin(symbol, option_type, ref_price, strike, premium, lot_size, iv_pct), "approx"


def best_combined_margin(symbol, expiry, lot_size, legs_full, as_of=None) -> tuple:
    """(margin, source) for short multi-leg structures.

    legs_full: [(option_type, ref_price, strike, premium, iv_pct), ...]
    """
    m = span_combined_margin(symbol, [(o, expiry, k) for o, _, k, _, _ in legs_full],
                             lot_size, as_of)
    if m is not None:
        return m, "span"
    approx = [short_margin(symbol, o, f, k, p, lot_size, iv) for o, f, k, p, iv in legs_full]
    return combined_margin(approx), "approx"
