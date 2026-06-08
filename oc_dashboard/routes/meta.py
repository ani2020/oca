"""Meta endpoints — symbols, timestamps, expiries, lots, cache."""
from __future__ import annotations
from datetime import date, datetime
from typing import Optional
from fastapi import APIRouter, Query
from ..db import qdf, tbl, to_records, _qraw, safe_response, _safe, _ts_lo_hi
from ..cache import cache_get, cache_set, cache_clear_all
from .. import config

router = APIRouter()

@router.get("/api/symbols")
def list_symbols():
    cached = cache_get("symbols")
    if cached is not None:
        return cached
    df = qdf(f"SELECT DISTINCT symbol FROM {tbl()} ORDER BY symbol")
    result = df["symbol"].tolist()
    cache_set("symbols", result)
    return result


@router.get("/api/timestamps")
def list_timestamps(
    symbol: str           = Query(...),
    expiry: Optional[str] = Query(None),
):
    """List distinct timestamps (minute-truncated). Cached per (symbol, expiry)."""
    cache_key = f"ts:{symbol}:{expiry[:10] if expiry else 'all'}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached
    if expiry:
        df = qdf(
            f"SELECT DISTINCT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts "
            f"FROM {tbl()} WHERE symbol = ? "
            f"AND expiry = ? ORDER BY ts DESC",
            [symbol, expiry[:10]],
        )
    else:
        df = qdf(
            f"SELECT DISTINCT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts "
            f"FROM {tbl()} WHERE symbol = ? ORDER BY ts DESC",
            [symbol],
        )
    result = df["ts"].tolist()
    cache_set(cache_key, result)
    return result


@router.get("/api/expiries")
def list_expiries(
    symbol:      str           = Query(...),
    timestamp:   Optional[str] = Query(None),
    future_only: bool          = Query(True),
):
    """List expiries for a symbol. Cached per (symbol, future_only) when no timestamp filter."""
    cache_key = f"exp:{symbol}:{future_only}" if not timestamp else None
    if cache_key:
        cached = cache_get(cache_key)
        if cached is not None:
            return cached
    today = date.today().isoformat()
    base = f"SELECT DISTINCT CAST(expiry AS VARCHAR) AS exp FROM {tbl()} WHERE symbol = ?"
    params = [symbol]
    if timestamp:
        base += " AND timestamp BETWEEN ? AND ? "
        params.extend(_ts_lo_hi(timestamp))
    if future_only:
        base += " AND expiry >= ? "
        params.append(today)
    df = qdf(base + " ORDER BY exp", params)
    result = df["exp"].tolist()
    if cache_key:
        cache_set(cache_key, result)
    return result


@router.get("/api/lot_sizes")
def lot_sizes():
    """Return lot_size for each symbol (used by frontend for strike step)."""
    cached = cache_get("lot_sizes")
    if cached is not None:
        return cached
    df = qdf(
        f"SELECT symbol, MAX(COALESCE(lotsize, 1)) AS lot_size "
        f"FROM {tbl()} GROUP BY symbol ORDER BY symbol"
    )
    result = {} if df.empty else {row["symbol"]: int(row["lot_size"]) for row in df.to_dict(orient="records")}
    cache_set("lot_sizes", result)
    return result


@router.post("/api/cache/refresh")
def cache_refresh():
    """
    Clear the in-memory DB cache (symbols, expiries, timestamps, lot_sizes, overview).
    Next requests will re-query the DB and repopulate. Use after a new OC import.
    Does NOT clear the margin cache — use /api/icici/margin/refresh for that.
    """
    cache_clear_all()
    return {"status": "ok", "message": "DB cache cleared — next requests will refresh from DB"}


@router.get("/api/cache/status")
def cache_status():
    """Show what is currently cached."""
    with _cache_lock:
        keys = list(_cache_store.keys())
    with _margin_cache_lock:
        margin_count = len(_margin_cache_store)
        today = date.today().isoformat()
        margin_today = sum(1 for v in _margin_cache_store.values() if v.get("date") == today)
    return {
        "db_cache_keys":     keys,
        "db_cache_count":    len(keys),
        "margin_cache_total": margin_count,
        "margin_cache_today": margin_today,
        "margin_cache_file":  str(_margin_cache_path()),
        "margin_cache_file_exists": _margin_cache_path().exists(),
    }


