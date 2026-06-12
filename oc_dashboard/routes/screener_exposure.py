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
except Exception:
    SIGNAL_INFO = {}


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


@router.get("/api/exposure_screener")
def exposure_screener(
    screen_date:  Optional[str] = Query(None, description="EOD date; default latest"),
    view:         str = Query("changed", description="changed | all"),
    signal:       Optional[str] = Query(None, description="filter by one signal key"),
    regime:       Optional[str] = Query(None, description="positive|negative|all_positive|all_negative"),
    index_name:   Optional[str] = Query(None, description="filter to index constituents"),
    min_confidence: str = Query("low", description="low|medium|high minimum"),
    sort_by:      str = Query("net_gex_norm", description="ranking column"),
    limit:        int = Query(200),
):
    if not _exposure_table_exists():
        raise HTTPException(404, "exposure_eod table not found — run the batch compute")

    # Resolve date
    if screen_date:
        d = screen_date
    else:
        row = qdf("SELECT MAX(date) AS d FROM exposure_eod")
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

    # Base query for the screen date
    df = qdf(f"""
        SELECT e.*
        FROM exposure_eod e
        {idx_join}
        WHERE e.date = CAST(? AS DATE)
        ORDER BY e.symbol
    """, idx_params + [d])

    if df.empty:
        return safe_response({"date": d, "view": view, "rows": [], "counts": {}})

    # Confidence filter
    df = df[df["confidence"].map(lambda c: conf_rank.get(c, 0) >= min_rank)]

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
        WHERE date = CAST(? AS DATE) AND signals IS NOT NULL AND signals != ''
    """, [d])
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
        FROM exposure_eod WHERE date = CAST(? AS DATE)
    """, [d])
    indicator_counts = {
        "regime_compression": int(ind["compressing"].iloc[0] or 0),
        "compression_release": int(ind["releasing"].iloc[0] or 0),
    } if not ind.empty else {}

    return safe_response({
        "date":   d,
        "view":   view,
        "rows":   to_records(df),
        "counts": counts,
        "indicator_counts": indicator_counts,
        "total":  int(len(df)),
    })
