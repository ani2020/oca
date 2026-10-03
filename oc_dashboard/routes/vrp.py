"""vrp.py — VRP (variance risk premium) dashboard panel.

EXPERIMENTAL. Reads vrp.duckdb, built offline by vrp_build.py / vrp_signals.py
in C:\\Users\\aniru\\od\\claude -- see areas/vrp-dashboard.md and the VRP
Dashboard technical spec. Separate DB file from oc.duckdb for the same reason
rv_forecast.py's rv.duckdb is: written only by an offline EOD batch, so it is
safe to read here at any time without touching oc.duckdb's market-hours write
lock.

Canonical series for every endpoint here: anchor='fwd' (the forward-anchor ATM
strike, not spot-anchor -- see vrp_build.py's docstring for why), rv_kind='hv'
with estimator='hv20_cc' for anything presented as a LIVE reading (rv_kind=
'ifrv' rows compare against realized variance to expiry, which has not
happened yet for any live date -- they exist for backtesting only and this
route never serves them as a current value). A caller can still ask for a
different anchor or estimator explicitly; 'fwd' / 'hv20_cc' are just the
defaults these endpoints fall back to.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..db import to_records, safe_response
from .. import config

router = APIRouter()

INDEXES = ("NIFTY", "BANKNIFTY", "FINNIFTY")
VALID_HORIZONS = ("7d", "30d")
VALID_ANCHORS = ("fwd", "spot")
DEFAULT_ESTIMATOR = "hv20_cc"
REGIME_WARN_Z = 2.0   # matches vrp_signals.py's own threshold -- see that script's Phase 3 backtest


def _vrp_db_path() -> str:
    return str(Path(config.DB_FILE).with_name("vrp.duckdb"))


def vrp_qdf(sql: str, params: list = []):
    path = _vrp_db_path()
    if not Path(path).exists():
        raise HTTPException(404, "vrp.duckdb not found -- run vrp_build.py / "
                                  "vrp_signals.py first (see areas/vrp-dashboard.md)")
    try:
        con = duckdb.connect(path, read_only=True)
        try:
            return con.execute(sql, params).df()
        finally:
            con.close()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _check_horizon(horizon: str) -> None:
    if horizon not in VALID_HORIZONS:
        raise HTTPException(400, f"horizon must be one of {VALID_HORIZONS}, got {horizon!r}")


def _check_anchor(anchor: str) -> None:
    if anchor not in VALID_ANCHORS:
        raise HTTPException(400, f"anchor must be one of {VALID_ANCHORS}, got {anchor!r}")


def _datestr(df: pd.DataFrame, *cols: str) -> pd.DataFrame:
    """DuckDB DATE columns come back from .df() as pandas Timestamps, and both
    Timestamp.isoformat() (used by db.py's to_records/_safe) and str(Timestamp)
    include a spurious 'T00:00:00' / ' 00:00:00' time part a plain datetime.date
    would not have. Normalise every date column to a bare YYYY-MM-DD string
    right after the query, so every endpoint -- whether it builds rows by hand
    (vrp_latest) or hands a DataFrame straight to to_records (the others) --
    serialises the same way, and so the column sorts correctly as a plain ISO
    string in the frontend's generic sortTbl.
    """
    for c in cols:
        if c in df.columns and not df.empty:
            df[c] = pd.to_datetime(df[c]).dt.strftime("%Y-%m-%d")
    return df


@router.get("/api/vrp/latest")
def vrp_latest(horizon: str = Query("30d"), anchor: str = Query("fwd"),
               estimator: str = Query(DEFAULT_ESTIMATOR)):
    """One row per index: the most recent VRP reading (percentile, z-score, log
    ratio, vol-point spread), the current same-index regime warning state, and
    the current Phase 4 signal states. This is the panel's main table.

    rv_kind is always 'hv' here (trailing HV20 comparator) -- see module
    docstring for why 'ifrv' rows are never served as a live value.
    """
    _check_horizon(horizon)
    _check_anchor(anchor)

    m = _datestr(vrp_qdf("""
        SELECT symbol, d, dte_cal, dte_trd, w_iv, w_rv, ratio, log_ratio, spread_vol_pts
        FROM vrp_metrics
        WHERE horizon = ? AND anchor = ? AND estimator = ? AND rv_kind = 'hv'
          AND symbol IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY')
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY d DESC) = 1
    """, [horizon, anchor, estimator]), "d")

    rank = _datestr(vrp_qdf("""
        SELECT symbol, d, pct_rank, z_score, lr_skew, n_window
        FROM vrp_rank
        WHERE horizon = ? AND anchor = ? AND estimator = ? AND rv_kind = 'hv'
          AND symbol IN ('NIFTY', 'BANKNIFTY', 'FINNIFTY')
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY d DESC) = 1
    """, [horizon, anchor, estimator]), "d")

    regime = _datestr(vrp_qdf("""
        SELECT symbol, d, z_shock, hv20_pct
        FROM vrp_regime WHERE leg = 'same_index'
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY d DESC) = 1
    """), "d")

    sig = vrp_qdf("""
        SELECT symbol, signal, state
        FROM vrp_signals
        WHERE horizon = ? AND signal IN ('harvest_candidate', 'premium_cheap',
                                         'premium_compressing', 'term_structure_inversion')
        QUALIFY row_number() OVER (PARTITION BY symbol, signal ORDER BY d DESC) = 1
    """, [horizon])
    sig_piv = sig.pivot(index="symbol", columns="signal", values="state") if not sig.empty else pd.DataFrame()

    rows = []
    for sym in INDEXES:
        row = {"symbol": sym, "horizon": horizon, "anchor": anchor, "estimator": estimator}
        mr = m[m["symbol"] == sym]
        if not mr.empty:
            r = mr.iloc[0]
            row.update({"d": str(r["d"]), "dte_cal": int(r["dte_cal"]), "iv_ann": None, "rv_ann": None,
                       "log_ratio": round(float(r["log_ratio"]), 4),
                       "spread_vol_pts": round(float(r["spread_vol_pts"]), 2)})
        rr = rank[rank["symbol"] == sym]
        if not rr.empty:
            r = rr.iloc[0]
            row.update({"pct_rank": round(float(r["pct_rank"]), 1) if pd.notna(r["pct_rank"]) else None,
                       "z_score": round(float(r["z_score"]), 2) if pd.notna(r["z_score"]) else None,
                       "n_window": int(r["n_window"])})
        rg = regime[regime["symbol"] == sym]
        if not rg.empty:
            r = rg.iloc[0]
            z = r["z_shock"]
            row.update({"regime_z": round(float(z), 2) if pd.notna(z) else None,
                       "regime_warning": bool(pd.notna(z) and z >= REGIME_WARN_Z)})
        if sym in sig_piv.index:
            s = sig_piv.loc[sym]
            for k in ("harvest_candidate", "premium_cheap", "premium_compressing", "term_structure_inversion"):
                row[k] = (s.get(k) == "True") if k in s.index and pd.notna(s.get(k)) else None
        rows.append(row)

    return safe_response({"horizon": horizon, "anchor": anchor, "estimator": estimator, "rows": rows})


@router.get("/api/vrp/timeseries")
def vrp_timeseries(symbol: str = Query("NIFTY"), horizon: str = Query("30d"),
                   anchor: str = Query("fwd"), estimator: str = Query(DEFAULT_ESTIMATOR),
                   limit: int = Query(750)):
    """Full history for one index: log ratio, percentile, z-score, vol-point
    spread -- the panel's main chart. rv_kind='hv' only, same reasoning as
    /api/vrp/latest.
    """
    _check_horizon(horizon)
    _check_anchor(anchor)
    symbol = symbol.upper()
    if symbol not in INDEXES:
        raise HTTPException(400, f"symbol must be one of {INDEXES}, got {symbol!r}")

    m = _datestr(vrp_qdf("""
        SELECT d, log_ratio, spread_vol_pts, ratio FROM vrp_metrics
        WHERE symbol = ? AND horizon = ? AND anchor = ? AND estimator = ? AND rv_kind = 'hv'
        ORDER BY d
    """, [symbol, horizon, anchor, estimator]), "d")
    r = _datestr(vrp_qdf("""
        SELECT d, pct_rank, z_score FROM vrp_rank
        WHERE symbol = ? AND horizon = ? AND anchor = ? AND estimator = ? AND rv_kind = 'hv'
        ORDER BY d
    """, [symbol, horizon, anchor, estimator]), "d")
    if m.empty:
        return safe_response({"symbol": symbol, "horizon": horizon, "anchor": anchor, "rows": []})
    df = m.merge(r, on="d", how="left").tail(limit)
    return safe_response({"symbol": symbol, "horizon": horizon, "anchor": anchor, "estimator": estimator,
                          "rows": to_records(df)})


@router.get("/api/vrp/regime")
def vrp_regime(limit: int = Query(750)):
    """Same-index and market-wide shock-trigger history for every index, plus
    the ex-post backtest summary (precision/recall/lead per threshold, and the
    combo-rule comparison) already computed by vrp_signals.py's backtest/combo
    steps -- read from vrp_audit rather than recomputed here.
    """
    reg = _datestr(vrp_qdf("""
        SELECT symbol, d, leg, z_shock FROM vrp_regime WHERE z_shock IS NOT NULL ORDER BY d
    """), "d")
    series = {}
    for (sym, leg), g in (reg.groupby(["symbol", "leg"]) if not reg.empty else []):
        series.setdefault(sym, {})[leg] = to_records(g[["d", "z_shock"]].tail(limit))

    audit = vrp_qdf("""
        SELECT check_name, symbol, tenor, metric, value FROM vrp_audit
        WHERE check_name IN ('regime_backtest', 'regime_combo')
    """)
    backtest = to_records(audit[audit["check_name"] == "regime_backtest"]) if not audit.empty else []
    combo = to_records(audit[audit["check_name"] == "regime_combo"]) if not audit.empty else []

    return safe_response({"series": series, "backtest": backtest, "combo": combo})


@router.get("/api/vrp/signals")
def vrp_signals_table(horizon: str = Query("30d"), limit: int = Query(60)):
    """Recent harvest_candidate / premium_cheap / premium_compressing /
    term_structure_inversion / relative_richness states for all three indexes,
    most recent day first -- for a compact signal-history table.
    """
    _check_horizon(horizon)
    df = _datestr(vrp_qdf("""
        SELECT symbol, d, signal, state, value FROM vrp_signals
        WHERE horizon = ?
        QUALIFY row_number() OVER (PARTITION BY symbol ORDER BY d DESC) <= ?
        ORDER BY d DESC, symbol
    """, [horizon, limit]), "d")
    return safe_response({"horizon": horizon, "rows": to_records(df)})
