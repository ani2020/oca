"""Exposure Screener — market-wide EOD regime/signal scan from exposure_eod."""
from __future__ import annotations
from datetime import date
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query

from ..db import qdf, to_records, safe_response
from .. import config

router = APIRouter()

# Signal display metadata — single source from exposure_core
try:
    import exposure_core as _core
    SIGNAL_INFO = _core.SIGNAL_INFO
    METRIC_INFO = getattr(_core, "METRIC_INFO", {})
except Exception:
    SIGNAL_INFO = {}
    METRIC_INFO = {}


def _exposure_table_exists() -> bool:
    try:
        df = qdf("SELECT 1 FROM exposure_eod LIMIT 1")
        return True
    except Exception:
        return False


@router.get("/api/exposure_screener/dates")
def exposure_screener_dates():
    """Available EOD dates in exposure_eod (most recent first)."""
    if not _exposure_table_exists():
        raise HTTPException(404, "exposure_eod table not found — run the batch compute")
    df = qdf("SELECT DISTINCT date FROM exposure_eod ORDER BY date DESC")
    return safe_response({"dates": [str(d) for d in df["date"].tolist()]})


@router.get("/api/exposure_screener/signals_meta")
def exposure_screener_signals_meta():
    """Signal labels + descriptions for the UI help/guide."""
    return safe_response({
        "signals": [
            {"key": k, "label": v[0], "description": v[1]}
            for k, v in SIGNAL_INFO.items()
        ]
    })


@router.get("/api/exposure_screener/metrics_meta")
def exposure_screener_metrics_meta():
    """Column/metric help text (label, meaning, interpret) for th tooltips + GUIDE."""
    return safe_response({
        "metrics": [
            {"key": k, "label": v[0], "meaning": v[1], "interpret": v[2]}
            for k, v in METRIC_INFO.items()
        ]
    })


@router.get("/api/exposure_screener")
def exposure_screener(
    screen_date:  Optional[str] = Query(None, description="EOD date; default latest"),
    view:         str = Query("changed", description="changed | all | dropped"),
    signal:       Optional[str] = Query(None, description="filter by one signal key"),
    regime:       Optional[str] = Query(None, description="positive|negative|all_positive|all_negative"),
    index_name:   Optional[str] = Query(None, description="filter to index constituents"),
    min_confidence: str = Query("low", description="low|medium|high minimum"),
    sort_by:      str = Query("net_gex_norm", description="ranking column"),
    expiry_rank:  int = Query(0, description="0 = NEAR (front monthly), 1 = NEXT monthly"),
    limit:        int = Query(200),
):
    if not _exposure_table_exists():
        raise HTTPException(404, "exposure_eod table not found — run the batch compute")

    # Resolve date
    if screen_date:
        d = screen_date
    else:
        row = qdf("SELECT MAX(date) AS d FROM exposure_eod WHERE expiry_rank = ?",
                  [expiry_rank])
        if row.empty or row["d"].iloc[0] is None:
            raise HTTPException(404, "No data in exposure_eod")
        d = str(row["d"].iloc[0])

    # Confidence ordering
    conf_rank = {"low": 0, "medium": 1, "high": 2}
    min_rank = conf_rank.get(min_confidence, 0)

    # Optional index-constituent filter via join
    idx_join = ""
    idx_params: List[Any] = []
    if index_name:
        idx_join = ("JOIN index_constituents ic "
                    "ON ic.symbol = e.symbol AND ic.index_name = ?")
        idx_params = [index_name]

    # Base query for the screen date — one row per symbol at the chosen expiry rank
    df = qdf(f"""
        SELECT e.*
        FROM exposure_eod e
        {idx_join}
        WHERE e.date = CAST(? AS DATE) AND e.expiry_rank = ?
        ORDER BY e.symbol
    """, idx_params + [d, expiry_rank])

    if df.empty:
        return safe_response({"date": d, "view": view, "rows": [], "counts": {}})

    # Confidence filter
    df = df[df["confidence"].map(lambda c: conf_rank.get(c, 0) >= min_rank)]

    # View: dropped = tickers that HAD a signal on the previous trading day but
    # have NONE today (their signal reset / fell off the radar). Self-join vs the
    # prior available date. Returns today's row enriched with yesterday's signals.
    if view == "dropped":
        prev = qdf("""
            SELECT MAX(date) AS d FROM exposure_eod
            WHERE date < CAST(? AS DATE) AND expiry_rank = ?
        """, [d, expiry_rank])
        if prev.empty or prev["d"].iloc[0] is None:
            return safe_response({"date": d, "view": view, "rows": [], "counts": {},
                                  "indicator_counts": {}, "prev_date": None, "total": 0})
        pdate = str(prev["d"].iloc[0])
        # Symbols with a signal yesterday
        had = qdf("""
            SELECT symbol, signals AS prev_signals FROM exposure_eod
            WHERE date = CAST(? AS DATE) AND expiry_rank = ? AND signals != ''
        """, [pdate, expiry_rank])
        # Symbols with a signal today
        now = qdf("""
            SELECT symbol FROM exposure_eod
            WHERE date = CAST(? AS DATE) AND expiry_rank = ? AND signals != ''
        """, [d, expiry_rank])
        now_set = set(now["symbol"].tolist())
        dropped_map = {r.symbol: r.prev_signals
                       for r in had.itertuples() if r.symbol not in now_set}
        # Keep today's rows for the dropped symbols; attach yesterday's signals
        df = df[df["symbol"].isin(dropped_map.keys())].copy()
        df["prev_signals"] = df["symbol"].map(dropped_map)
        # (skip the normal signal/changed filtering for this view)
        if regime:
            df = df[df["gex_regime"] == regime]
        if sort_by in df.columns and not df.empty:
            df = df.reindex(df[sort_by].abs().sort_values(ascending=False).index)
        df = df.head(limit)
        return safe_response({
            "date": d, "prev_date": pdate, "view": view,
            "rows": to_records(df),
            "counts": {}, "indicator_counts": {},
            "total": int(len(df)),
        })

    # View: changed = rows with a fired signal OR an active compression/release
    # indicator today (anything noteworthy, not just signal-bar items)
    if view == "changed":
        has_signal = df["signals"].fillna("") != ""
        has_compress = df["regime_compression"].fillna(False).astype(bool)
        has_release = df["compression_release"].fillna(False).astype(bool)
        df = df[has_signal | has_compress | has_release]

    # Signal filter
    if signal:
        df = df[df["signals"].fillna("").str.contains(signal, regex=False)]

    # Regime filter
    if regime:
        df = df[df["gex_regime"] == regime]

    # Sort — by magnitude of the chosen column (most extreme first)
    if sort_by in df.columns:
        df = df.reindex(df[sort_by].abs().sort_values(ascending=False).index)

    df = df.head(limit)

    # Signal counts across the (pre-limit) screen date for the summary bar
    full = qdf(f"""
        SELECT signals FROM exposure_eod
        WHERE date = CAST(? AS DATE) AND expiry_rank = ?
          AND signals IS NOT NULL AND signals != ''
    """, [d, expiry_rank])
    counts: Dict[str, int] = {}
    for s in full["signals"].tolist():
        for sig in str(s).split(","):
            sig = sig.strip()
            if sig:
                counts[sig] = counts.get(sig, 0) + 1

    # Indicator counts (compression state + release events) for the screen date
    ind = qdf(f"""
        SELECT
            SUM(CASE WHEN regime_compression THEN 1 ELSE 0 END) AS compressing,
            SUM(CASE WHEN compression_release THEN 1 ELSE 0 END) AS releasing
        FROM exposure_eod WHERE date = CAST(? AS DATE) AND expiry_rank = ?
    """, [d, expiry_rank])
    indicator_counts = {
        "regime_compression": int(ind["compressing"].iloc[0] or 0),
        "compression_release": int(ind["releasing"].iloc[0] or 0),
    } if not ind.empty else {}

    return safe_response({
        "date":   d,
        "view":   view,
        "expiry_rank": expiry_rank,
        "rows":   to_records(df),
        "counts": counts,
        "indicator_counts": indicator_counts,
        "total":  int(len(df)),
    })
