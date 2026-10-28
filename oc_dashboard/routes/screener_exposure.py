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


# ── Playbook inputs (2026-10-26) ─────────────────────────────────────────────
# Backtested 2020-26 on short ATM straddles (exit 50% of credit or DTE-5); see
# the Playbook guide in app.js. Thresholds are provisional until a few months of
# full-universe live SPAN data exist (history only covers ~70 wheel-universe names).
#
#   span_rich = ATM IV / NSE SPAN vol for the row's expiry. Same-day ranking among
#     passing names: top third beat bottom third by +8.0 pts of credit (t 5.8),
#     +8.4 out of sample 2024-26 (t 3.6); not explained by IV level or IV/HV20.
#     < 0.65 -> -5 pts vs same day (THIN); >= 0.85 -> +3.5..4 pts (RICH).
#   weather: market-level, NOT per stock.
#     univ_score = mean legacy Playbook SELL score over single stocks. Bottom
#       tercile weeks (< 63) earned ~0.7% of credit vs ~12% otherwise.
#     univ_ivhv  = median ATM IV / HV20 (CA-adjusted) over single stocks.
#       < 1.07 earned ~2.1% vs ~11%. Both low together: -7.1% (47 weeks).
PB_SPAN_THIN, PB_SPAN_RICH = 0.65, 0.85
PB_W_SCORE_LOW, PB_W_IVHV_LOW = 63.0, 1.07


def _legacy_sell_score(r) -> Optional[float]:
    """The pre-2026-10-26 Playbook SELL score -- kept ONLY as a market-level input."""
    import math
    def ok(v):
        return v is not None and not (isinstance(v, float) and math.isnan(v))
    g, lop, conc, days = (r.get("gamma_position"), r.get("net_gex_norm"),
                          r.get("concentration"), r.get("days_in_regime"))
    if not all(ok(v) for v in (g, lop, conc, days)):
        return None
    p = 69 if g <= -0.25 else 71 if g <= 0 else 72 if g <= 0.4 else 74 if g <= 0.7 else 76
    clip = lambda x: max(0.0, min(1.0, x))
    return 100 * (30 * clip((p - 69) / 7) + 18 * clip(lop) + 14 * (1 - clip((conc - 0.2) / 0.3))
                  + 4 * clip(days / 8)) / 66


def _attach_playbook_inputs(df, d):
    """Adds span_vol / span_rich per row; returns (df, weather dict)."""
    import numpy as np
    from .. import margin as _margin
    sd = None
    try:
        sd = _margin.span_data(as_of=d)
    except Exception:
        sd = None
    vol = getattr(sd, "vol", {}) if sd is not None else {}
    def _sv(r):
        e = r["expiry"]
        if e is None or e != e:
            return None
        import pandas as pd
        return vol.get((str(r["symbol"]).upper(), pd.Timestamp(e).date()))
    df = df.copy()
    df["span_vol"] = df.apply(_sv, axis=1).astype(float) if len(df) else []
    df["span_rich"] = (df["atm_iv"] / 100.0) / df["span_vol"]
    df.loc[~np.isfinite(df["span_rich"]), "span_rich"] = np.nan

    stocks = df[~df["symbol"].isin(config.NSE_INDICES)]
    sc = [x for x in (_legacy_sell_score(r) for r in stocks.to_dict("records")) if x is not None]
    ivhv = (stocks["atm_iv"] / stocks["hv20"]).replace([np.inf, -np.inf], np.nan).dropna()
    us = float(np.mean(sc)) if sc else None
    ui = float(ivhv.median()) if len(ivhv) else None
    key = getattr(sd, "key", None) if sd is not None else None
    weather = {
        "univ_score": us, "univ_score_low": (us is not None and us < PB_W_SCORE_LOW),
        "univ_ivhv": ui, "univ_ivhv_low": (ui is not None and ui < PB_W_IVHV_LOW),
        "n_stocks": int(len(stocks)),
        "thresholds": {"score": PB_W_SCORE_LOW, "ivhv": PB_W_IVHV_LOW,
                       "span_thin": PB_SPAN_THIN, "span_rich": PB_SPAN_RICH},
        "span_file": [str(key[0]), key[1]] if key else None,
        "span_coverage": int(df["span_vol"].notna().sum()),
    }
    return df, weather


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

    # Playbook inputs: SPAN richness per row + market weather over the full universe
    # (computed before any confidence/view filtering so weather is universe-wide).
    df, weather = _attach_playbook_inputs(df, d)

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
        "weather": weather,
        "total":  int(len(df)),
    })
