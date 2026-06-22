"""Symbol History (Trend) view — per-symbol EOD time series from exposure_eod.

Read-only over the same exposure_eod table the screener scans, but sliced the
OTHER way: one symbol, many dates (the screener is one date, many symbols). Adds
the missing time axis so you can see how a symbol's gamma structure EVOLVED and
answer "stabilising or noisy/destabilising?".

Multi-symbol READY (length-1 today): the endpoint returns a SYMBOL-KEYED dict
({"BANKINDIA": [...rows]}) even for a single symbol, so a 2nd ticker is additive
(another key) rather than a refactor. The comparison math / selector is v2.

No new compute, no backfill. The per-day structural strength score + cumulative
and the basis dead-zone are applied here (server-side) from exposure_core, so the
frontend just renders.
"""
from __future__ import annotations
from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..db import qdf, to_records, safe_response

router = APIRouter()

# Pure helpers — single-sourced from exposure_core (same module the screener +
# batch script use, so the math lives in exactly one place).
try:
    import exposure_core as _core
    METRIC_INFO = getattr(_core, "METRIC_INFO", {})
    _basis_deadzone = _core.basis_deadzone
    _strength_series = _core.strength_series
    _regime_color = _core.regime_color
except Exception:  # pragma: no cover - exposure_core should always import
    METRIC_INFO = {}
    def _basis_deadzone(_v):  # type: ignore
        return _v is None
    def _strength_series(rows):  # type: ignore
        return [dict(r, strength_score=0, strength_axes={}, strength_cumulative=0)
                for r in rows]
    def _regime_color(_r):  # type: ignore
        return {"color": "#3d5270", "order": 99, "label": ""}


def _exposure_table_exists() -> bool:
    try:
        qdf("SELECT 1 FROM exposure_eod LIMIT 1")
        return True
    except Exception:
        return False


def _apply_basis_deadzone(rows: List[Dict]) -> None:
    """Neutralise tick-size basis noise IN PLACE (display/interpretation side).
    When |basis_pct| sits inside the dead-zone we treat the basis as flat:
      - flag basis_in_deadzone = True (UI renders neutral, not contango/backwardn)
      - suppress basis_chg sign-flip emphasis when BOTH this and the prior row are
        in-zone (a flip between two noise prints is not a real event).
    Does NOT null the stored numbers — the values stay visible, just de-emphasised.
    rows must be ascending by date."""
    prev_in_zone = True  # nothing before the first row → treat as in-zone
    for r in rows:
        in_zone = bool(_basis_deadzone(r.get("basis_pct")))
        r["basis_in_deadzone"] = in_zone
        # only emphasise a basis_chg sign-flip when at least one side is OUTSIDE
        # the dead-zone (i.e. a real move, not noise→noise)
        r["basis_chg_emphasis"] = not (in_zone and prev_in_zone)
        prev_in_zone = in_zone


@router.get("/api/symbol_history/metrics_meta")
def symbol_history_metrics_meta():
    """Column/metric help text (label, meaning, interpret) — shared single source
    with the screener (same METRIC_INFO), plus the strength_score entry."""
    return safe_response({
        "metrics": [
            {"key": k, "label": v[0], "meaning": v[1], "interpret": v[2]}
            for k, v in METRIC_INFO.items()
        ]
    })


@router.get("/api/symbol_history/dates")
def symbol_history_dates(symbol: Optional[str] = Query(None)):
    """Available EOD dates (most recent first). If symbol given, restrict to dates
    that symbol actually has data on (per-symbol scrape gaps mean a global date
    list can be ahead of a given ticker)."""
    if not _exposure_table_exists():
        raise HTTPException(404, "exposure_eod table not found — run the batch compute")
    if symbol:
        df = qdf("SELECT DISTINCT CAST(date AS VARCHAR) AS date FROM exposure_eod "
                 "WHERE symbol = ? ORDER BY date DESC", [symbol])
    else:
        df = qdf("SELECT DISTINCT CAST(date AS VARCHAR) AS date FROM exposure_eod "
                 "ORDER BY date DESC")
    return safe_response({"dates": [str(d) for d in df["date"].tolist()]})


@router.get("/api/symbol_history/symbols")
def symbol_history_symbols():
    """Symbols present in exposure_eod (for the History symbol picker)."""
    if not _exposure_table_exists():
        raise HTTPException(404, "exposure_eod table not found — run the batch compute")
    df = qdf("SELECT DISTINCT symbol FROM exposure_eod ORDER BY symbol")
    return safe_response({"symbols": df["symbol"].tolist()})


def _load_symbol_series(symbol: str, date_from: Optional[str],
                        date_to: Optional[str]) -> List[Dict]:
    """One query for one symbol → date-ordered, dead-zoned, strength-annotated rows."""
    where = ["symbol = ?"]
    params: List[Any] = [symbol]
    if date_from:
        where.append("date >= CAST(? AS DATE)")
        params.append(date_from)
    if date_to:
        where.append("date <= CAST(? AS DATE)")
        params.append(date_to)
    sql = (f"SELECT e.* FROM exposure_eod e "
           f"WHERE {' AND '.join(where)} ORDER BY date")
    df = qdf(sql, params)
    if df.empty:
        return []
    rows = to_records(df)                  # JSON-safe (NaN→None, dates→iso)
    for r in rows:                         # normalise date → clean YYYY-MM-DD
        dv = r.get("date")
        if isinstance(dv, str) and len(dv) >= 10:
            r["date"] = dv[:10]
    _apply_basis_deadzone(rows)            # in-place, ascending order
    rows = _strength_series(rows)          # adds strength_score/_axes/_cumulative
    return rows


@router.get("/api/symbol_history")
def symbol_history(
    symbol:    str = Query(..., description="ticker (single symbol today; N-ready)"),
    date_from: Optional[str] = Query(None, description="YYYY-MM-DD inclusive"),
    date_to:   Optional[str] = Query(None, description="YYYY-MM-DD inclusive; default latest"),
):
    """Per-symbol EOD trend series.

    Returns a SYMBOL-KEYED dict (multi-symbol ready — length 1 today):
        {
          "symbols": ["BANKINDIA"],
          "series":  { "BANKINDIA": [ {row...}, ... ] },
          "date_from": "...", "date_to": "...",
          "regime_ramp": { regime: {color, order, label} }   # shared mapping
        }
    Each row carries the stored exposure_eod columns PLUS basis_in_deadzone,
    basis_chg_emphasis, strength_score, strength_axes, strength_cumulative.
    """
    if not _exposure_table_exists():
        raise HTTPException(404, "exposure_eod table not found — run the batch compute")

    # Resolve the symbol list (comma-separated tolerated now so v2 is a no-op).
    symbols = [s.strip() for s in str(symbol).split(",") if s.strip()]
    if not symbols:
        raise HTTPException(400, "symbol is required")

    # Default date_to = symbol's latest available DATA date (not wall-clock today
    # — weekends/holidays/missed scrapes must not 404 or truncate the window).
    if not date_to:
        row = qdf("SELECT CAST(MAX(date) AS VARCHAR) AS d FROM exposure_eod WHERE symbol = ?",
                  [symbols[0]])
        if row.empty or row["d"].iloc[0] is None:
            raise HTTPException(404, f"No data for {symbols[0]} in exposure_eod")
        date_to = str(row["d"].iloc[0])[:10]
    else:
        date_to = str(date_to)[:10]
    # Default window = to − 7 days (matches the jump-from-screener default).
    if not date_from:
        try:
            d = date.fromisoformat(date_to)
            date_from = str(d - timedelta(days=7))
        except ValueError:
            date_from = None

    series: Dict[str, List[Dict]] = {}
    for sym in symbols:
        series[sym] = _load_symbol_series(sym, date_from, date_to)

    # Build the regime ramp only for regimes actually present (small, shared map).
    regimes_seen = set()
    for rows in series.values():
        for r in rows:
            if r.get("gex_regime"):
                regimes_seen.add(r["gex_regime"])
    regime_ramp = {rg: _regime_color(rg) for rg in regimes_seen}

    return safe_response({
        "symbols":    symbols,
        "series":     series,
        "date_from":  date_from,
        "date_to":    date_to,
        "regime_ramp": regime_ramp,
    })
