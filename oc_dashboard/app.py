"""
oc_dashboard/app.py
-------------------
FastAPI backend for the NSE Options Chain Analytics Dashboard.

Run:
    python run.py                        # oc.duckdb in CWD, port 8000
    python run.py --db /path/to/oc.duckdb --port 8080
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import math

import duckdb
import numpy as np
import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.encoders import jsonable_encoder

# ---------------------------------------------------------------------------
# .env support
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Configuration — overridden by CLI args
# ---------------------------------------------------------------------------
_DB_FILE  = "oc.duckdb"
_DB_TABLE = "ocdata"

_NSE_INDICES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "SENSEX", "NIFTYIT",
}

# ---------------------------------------------------------------------------
# ICICI Breeze ticker map  NSE ticker → 6-char Breeze stock_code
# Loaded from icici_tickers.csv placed next to run.py / oc.duckdb
# ---------------------------------------------------------------------------
_ICICI_TICKER_MAP: Dict[str, str] = {}


def _load_icici_ticker_map() -> None:
    """Load NSE→ICICI ticker mapping from icici_tickers.csv if present."""
    global _ICICI_TICKER_MAP
    # Look next to the DB file, then next to this source file
    candidates = [
        Path(_DB_FILE).parent / "icici_tickers.csv",
        Path(__file__).parent.parent / "icici_tickers.csv",
        Path(__file__).parent / "icici_tickers.csv",
    ]
    for p in candidates:
        if p.exists():
            try:
                df = pd.read_csv(p, dtype=str).dropna()
                _ICICI_TICKER_MAP = {
                    row["NSETicker"].strip().upper(): row["ICTicker"].strip().upper()
                    for _, row in df.iterrows()
                }
                print(f"  Loaded {len(_ICICI_TICKER_MAP)} ICICI ticker mappings from {p}")
                return
            except Exception as exc:
                print(f"  ⚠ Could not load icici_tickers.csv: {exc}")
    print("  ℹ icici_tickers.csv not found — ICICI margin will use NSE tickers as-is")


def _to_icici_ticker(nse_symbol: str) -> str:
    """Return the ICICI Breeze stock_code for a given NSE symbol."""
    return _ICICI_TICKER_MAP.get(nse_symbol.upper(), nse_symbol.upper())


# ---------------------------------------------------------------------------
# DuckDB — JIT connection: open per-query, close immediately after.
# This ensures the file lock is released between requests so the import
# script can acquire a write lock without stopping the dashboard.
# ---------------------------------------------------------------------------

def qdf(sql: str, params: list = []) -> pd.DataFrame:
    """Open a short-lived read-only DuckDB connection, run the query, close."""
    con = None
    try:
        con = duckdb.connect(_DB_FILE, read_only=True)
        return con.execute(sql, params).df()
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if con:
            try:
                con.close()
            except Exception:
                pass


def _qraw(sql: str, params: list = []):
    """Like qdf but returns raw fetchone() result — used for scalar queries."""
    con = None
    try:
        con = duckdb.connect(_DB_FILE, read_only=True)
        return con.execute(sql, params).fetchone()
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if con:
            try:
                con.close()
            except Exception:
                pass


def _safe(v: Any) -> Any:
    """
    Recursively sanitise any value to be JSON-safe.
    Converts numpy scalars → Python natives; nan/inf → None.
    Works on arbitrarily nested dicts and lists.
    """
    # numpy scalar types first (before float check, since np.float64 IS a float subclass)
    if isinstance(v, np.floating):
        f = float(v)
        return None if (f != f or f == float('inf') or f == float('-inf')) else f
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.ndarray):
        return [_safe(x) for x in v.tolist()]
    # plain Python float  (np.float64 already handled above)
    if isinstance(v, float):
        return None if (v != v or v == float('inf') or v == float('-inf')) else v
    # containers
    if isinstance(v, dict):
        return {k: _safe(vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_safe(i) for i in v]
    return v


def to_records(df: pd.DataFrame) -> List[Dict]:
    """Convert DataFrame to JSON-safe list of dicts."""
    # First pass: pandas replace to turn numpy nan/inf into None
    clean = df.replace({np.nan: None, np.inf: None, -np.inf: None})
    # Second pass: _safe() handles any remaining numpy scalars
    return [
        {k: _safe(v) for k, v in row.items()}
        for row in clean.to_dict(orient="records")
    ]


def clean_dict(d: Any) -> Any:
    """Recursively sanitise a dict/list/scalar for JSON serialisation."""
    return _safe(d)


def safe_response(data: Any) -> JSONResponse:
    """
    Return a JSONResponse with all non-serialisable values sanitised.
    Bypasses FastAPI's default encoder which rejects nan/inf.
    """
    return JSONResponse(content=_safe(data))


def tbl() -> str:
    return _DB_TABLE


def ts_filter_clause(ts_str: str, col: str = "timestamp") -> tuple[str, list]:
    """
    Build a WHERE clause matching ±10 minutes of ts_str on a TIMESTAMP column.
    Uses typed TIMESTAMP comparison (not CAST+LIKE) so it works reliably on all
    platforms regardless of how DuckDB formats CAST(TIMESTAMP AS VARCHAR).
    ts_str expected as "YYYY-MM-DD HH:MM" (minute-truncated from the dropdown).
    """
    try:
        base = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            base = datetime.strptime(ts_str[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Last resort: cast-and-like
            return f"{col}  = ?", [f"{ts_str[:16]}"]
    lo = (base - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    # Use TIMESTAMP literals — works with DuckDB DATE/TIMESTAMP types directly
    return (
        f" {col} BETWEEN ?  AND ? ",
        [lo, hi]
    )


def _ts_lo_hi(ts_str: str) -> list:
    """Return [lo, hi] strings for a ±10-min BETWEEN clause from a minute-truncated ts."""
    try:
        base = datetime.strptime(ts_str[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        base = datetime.now()
    lo = (base - timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (base + timedelta(minutes=10)).strftime("%Y-%m-%d %H:%M:%S")
    return [lo, hi]


def expiry_clause(exp_str: str, col: str = "expiry") -> tuple[str, list]:
    """
    Build a WHERE clause for a DATE column expiry.
    exp_str expected as "YYYY-MM-DD" or "YYYY-MM-DD ...".
    Uses typed DATE comparison, not CAST+LIKE.
    """
    date_part = exp_str[:10]
    return f" {col}  = ? ", [date_part]


# ===========================================================================
# In-memory cache — keyed lookups for data that changes only on new imports
# ===========================================================================
import asyncio
import threading
import pickle
import time

_cache_lock   = threading.Lock()
_cache_store: Dict[str, Any] = {}   # key → cached value (no expiry — manual refresh)
_cache_populated: set = set()        # keys that have been set at least once


def _cache_get(key: str) -> Optional[Any]:
    with _cache_lock:
        return _cache_store.get(key)


def _cache_set(key: str, value: Any) -> None:
    with _cache_lock:
        _cache_store[key] = value
        _cache_populated.add(key)


def cache_clear_all() -> None:
    """Clear the in-memory DB cache (symbols, expiries, timestamps, overview)."""
    with _cache_lock:
        _cache_store.clear()
        _cache_populated.clear()
    print("  ✓ DB cache cleared")


# ===========================================================================
# Margin cache — file-persisted, same-day TTL, separate refresh
# File: margin_cache.pkl next to oc.duckdb
# Key: (symbol, strike, expiry_date, option_type)
# Value: {"result": {...}, "date": "YYYY-MM-DD"}
# ===========================================================================

_margin_cache_lock  = threading.Lock()
_margin_cache_store: Dict[tuple, Dict] = {}
_MARGIN_CACHE_FILE  = "margin_cache.pkl"


def _margin_cache_path() -> Path:
    return Path(_DB_FILE).parent / _MARGIN_CACHE_FILE


def _margin_cache_load() -> None:
    """Load margin cache from disk on startup."""
    global _margin_cache_store
    p = _margin_cache_path()
    if not p.exists():
        return
    try:
        # Only load if file was created today
        file_date = datetime.fromtimestamp(p.stat().st_mtime).date()
        if file_date != date.today():
            print(f"  ℹ Margin cache is from {file_date}, will be rebuilt today")
            p.unlink(missing_ok=True)
            return
        with open(p, "rb") as f:
            data = pickle.load(f)
        # Filter to today's entries only
        today = date.today().isoformat()
        valid = {k: v for k, v in data.items() if v.get("date") == today}
        with _margin_cache_lock:
            _margin_cache_store = valid
        if valid:
            print(f"  ✓ Margin cache: loaded {len(valid)} entries from prior session ({p.name})")
    except Exception as exc:
        print(f"  ⚠ Could not load margin cache: {exc}")


def _margin_cache_save(verbose: bool = False) -> None:
    """
    Persist current margin cache to disk.
    verbose=True prints confirmation (used at shutdown and after batch fetch).
    Background per-entry saves are silent to avoid console spam.
    """
    p = _margin_cache_path()
    try:
        with _margin_cache_lock:
            data = dict(_margin_cache_store)
        with open(p, "wb") as f:
            pickle.dump(data, f)
        if verbose:
            print(f"  ✓ Margin cache saved ({len(data)} entries) to {p}")
    except Exception as exc:
        print(f"  ⚠ Could not save margin cache: {exc}")


def _margin_cache_get(symbol: str, strike: str, expiry: str, option_type: str) -> Optional[Dict]:
    """Return cached margin result if present and from today, else None."""
    key = (symbol.upper(), str(strike), expiry[:10], option_type.lower())
    with _margin_cache_lock:
        entry = _margin_cache_store.get(key)
    if entry and entry.get("date") == date.today().isoformat():
        return entry["result"]
    return None


def _margin_cache_put(symbol: str, strike: str, expiry: str, option_type: str,
                      result: Dict, save: bool = False) -> None:
    """Store a margin result in cache (keyed to today's date).
    Pass save=False when doing bulk inserts to avoid repeated disk writes;
    call _margin_cache_save() once after the batch completes.
    """
    key = (symbol.upper(), str(strike), expiry[:10], option_type.lower())
    entry = {"result": result, "date": date.today().isoformat()}
    with _margin_cache_lock:
        _margin_cache_store[key] = entry
    if save:
        # Silent background save — verbose=False to avoid per-entry console spam
        threading.Thread(target=lambda: _margin_cache_save(verbose=False),
                         daemon=True).start()


def _margin_cache_clear() -> int:
    """Clear all margin cache entries. Returns count cleared."""
    with _margin_cache_lock:
        count = len(_margin_cache_store)
        _margin_cache_store.clear()
    p = _margin_cache_path()
    p.unlink(missing_ok=True)
    print(f"  ✓ Margin cache cleared ({count} entries)")
    return count


def latest_ts(symbol: str) -> str:
    row = _qraw(f"SELECT MAX(timestamp) FROM {tbl()} WHERE symbol = ?", [symbol])
    if not row or row[0] is None:
        raise HTTPException(404, f"No data for {symbol}")
    return str(row[0])


# ---------------------------------------------------------------------------
# ICICI Breeze — runtime state
# ---------------------------------------------------------------------------
_icici: Optional[Any] = None
_icici_status: str = "not_configured"


def _try_init_icici(session_token: Optional[str] = None) -> bool:
    global _icici, _icici_status
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from icici_b import ICB
        _icici = ICB(session_token=session_token)
        _icici_status = "connected"
        return True
    except ImportError:
        _icici_status = "breeze_connect_missing"
        return False
    except Exception as exc:
        _icici_status = f"error: {exc}"
        return False


# ---------------------------------------------------------------------------
# Gamma analysis — inlined from gamma_study modules
# ---------------------------------------------------------------------------

def _gamma_analysis_inline(
    gdf: pd.DataFrame,
    spot: float,
    dte: float,
    atm_iv: float,
    atr: Optional[float],
    lambda_gamma: float = 0.1,
    magnet_threshold: float = 0.75,
    decay_threshold: float = 0.40,
) -> Dict:
    import math

    g = gdf["total_gamma_billions"].values
    x = gdf["level"].values

    # Gamma flip
    gamma_flip = None
    idx = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
    if len(idx):
        i = idx[0]
        d = g[i + 1] - g[i]
        if d:
            gamma_flip = float(x[i] - g[i] * (x[i + 1] - x[i]) / d)

    # Gamma at spot
    ns_col = gdf["near_strike"].values
    nearest = int(np.argmin(np.abs(ns_col - spot)))
    gamma_at_spot = float(g[nearest])

    regime = "Positive Gamma" if (gamma_flip is None or spot >= gamma_flip) else "Negative Gamma"

    # Magnet zone
    max_g = float(gdf["total_gamma_billions"].max())
    magnet = None
    if max_g > 0:
        zone = gdf[gdf["total_gamma_billions"] >= magnet_threshold * max_g]
        if not zone.empty:
            magnet = {
                "lower":    float(zone["near_strike"].min()),
                "upper":    float(zone["near_strike"].max()),
                "center":   float(zone.loc[zone["total_gamma_billions"].idxmax(), "near_strike"]),
                "strength": round(max_g, 4),
            }

    # ATR / gamma-adjusted ATR
    gamma_adj_atr = None
    if atr is not None:
        scale = 1 / math.sqrt(1 + lambda_gamma * abs(gamma_at_spot))
        gamma_adj_atr = round(atr * scale, 2)

    # Upper boundary
    df_s = gdf.sort_values("near_strike").reset_index(drop=True)
    peak_idx = int(df_s["total_gamma_billions"].idxmax())
    peak_g   = float(df_s.loc[peak_idx, "total_gamma_billions"])
    window   = 3
    upper_boundary = None
    for i in range(peak_idx + window, len(df_s)):
        wg = df_s.loc[i - window:i, "total_gamma_billions"].mean()
        if wg <= (1 - decay_threshold) * peak_g:
            upper_boundary = float(df_s.loc[i, "near_strike"])
            break

    # Lower boundary
    lower_boundary = None
    if gamma_flip is not None:
        below = df_s[df_s["near_strike"] < gamma_flip].copy()
        if len(below) >= window + 1:
            below["gamma_change"] = below["total_gamma_billions"].diff()
            below["rolling_drop"] = below["gamma_change"].rolling(window).sum()
            min_idx = below["rolling_drop"].idxmin()
            if not pd.isna(min_idx):
                lower_boundary = float(below.loc[min_idx, "near_strike"])

    ga = round(gamma_adj_atr or 0)
    bullish_break = round(upper_boundary + ga, 0) if upper_boundary and ga else None
    bearish_break = round(lower_boundary - ga, 0) if lower_boundary and ga else None

    # Expected range
    exp_range = None
    if atm_iv and atm_iv > 0:
        raw_move = spot * atm_iv * np.sqrt(1 / 252)
        scale    = 1 / math.sqrt(1 + lambda_gamma * abs(gamma_at_spot))
        exp_range = round(raw_move * scale, 2)

    # Trend / behavior
    trend = "Directional / Trending"
    if regime == "Positive Gamma":
        if magnet:
            trend = "Range Bound with Upward Drift" if spot < magnet["center"] \
               else "Range Bound with Downward Drift"
        else:
            trend = "Range Bound"

    behavior = "Directional moves with volatility expansion"
    if regime == "Positive Gamma":
        if magnet and magnet["lower"] <= spot <= magnet["upper"]:
            behavior = "Pinning behavior with volatility compression"
        else:
            behavior = "Choppy mean-reverting price action"

    pin_zone = None
    if regime == "Positive Gamma" and dte is not None and dte <= 10:
        pin_zone = magnet

    # Structures
    structures: List[str] = []
    if regime == "Positive Gamma" and magnet and ga:
        center = magnet["center"]
        if abs(spot - center) <= ga:
            structures.append(f"Short Straddle @ {center:.0f}")
            structures.append(
                f"Iron Condor: sell {center-ga:.0f}P/{center+ga:.0f}C  "
                f"buy {center-2*ga:.0f}P/{center+2*ga:.0f}C"
            )
        elif spot < magnet["lower"]:
            structures.append(f"Put Credit Spread: sell {magnet['lower']:.0f}P / buy {magnet['lower']-ga:.0f}P")
        elif spot > magnet["upper"]:
            structures.append(f"Call Credit Spread: sell {magnet['upper']:.0f}C / buy {magnet['upper']+ga:.0f}C")
    elif regime == "Negative Gamma":
        if bullish_break and spot > bullish_break:
            structures.append(f"Call Ratio Spread above {bullish_break:.0f}")
        if bearish_break and spot < bearish_break:
            structures.append(f"Put Backspread below {bearish_break:.0f}")

    # Warnings
    warnings: List[str] = []
    if regime == "Positive Gamma":
        warnings.append("Avoid long straddles (volatility compression likely)")
    if gamma_flip and gamma_adj_atr and abs(spot - gamma_flip) <= gamma_adj_atr:
        warnings.append("Avoid short ATM options near gamma flip")
    if bullish_break and gamma_adj_atr and spot >= bullish_break - gamma_adj_atr:
        warnings.append("Avoid naked short calls (upside acceleration risk)")
    if bearish_break and gamma_adj_atr and spot <= bearish_break + gamma_adj_atr:
        warnings.append("Avoid naked short puts (downside acceleration risk)")
    if regime == "Negative Gamma":
        warnings.append("Avoid iron condors and range-bound strategies")

    return {
        "regime": regime,
        "gamma_flip": gamma_flip,
        "gamma_at_spot": round(gamma_at_spot, 4),
        "magnet": magnet,
        "upper_boundary": upper_boundary,
        "lower_boundary": lower_boundary,
        "bullish_break": bullish_break,
        "bearish_break": bearish_break,
        "atr": round(atr, 2) if atr else None,
        "gamma_adj_atr": gamma_adj_atr,
        "expected_range": exp_range,
        "trend": trend,
        "behavior": behavior,
        "pin_zone": pin_zone,
        "structures": structures,
        "warnings": warnings,
    }


# ===========================================================================
# App lifecycle
# ===========================================================================

_autosave_stop = threading.Event()


def _start_margin_cache_autosave(interval_secs: int = 300) -> None:
    """
    Start a daemon thread that saves the margin cache to disk every interval_secs.
    Silent — no console output. Stops when _autosave_stop is set (on shutdown).
    This ensures single-row /api/icici/margin calls eventually persist even if
    the batch endpoint is not used.
    """
    def _loop():
        while not _autosave_stop.wait(timeout=interval_secs):
            _margin_cache_save(verbose=False)
    t = threading.Thread(target=_loop, daemon=True, name="margin-cache-autosave")
    t.start()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Verify DB exists and is readable (quick smoke-test, no persistent lock held)
    try:
        con = duckdb.connect(_DB_FILE, read_only=True)
        con.close()
        print(f"✓ DB verified: {_DB_FILE}")
    except Exception as exc:
        print(f"✗ DB open failed: {exc}")
    _load_icici_ticker_map()
    _try_init_icici()
    _margin_cache_load()          # load persisted margin cache from disk
    _start_margin_cache_autosave()   # periodic silent background save
    print("✓ Dashboard ready — open http://localhost:8000")
    yield
    _margin_cache_save(verbose=True)   # persist margin cache on clean shutdown


class _SafeJSONEncoder(json.JSONEncoder):
    """
    JSON encoder that converts nan/inf → null and numpy scalars → Python natives.
    Wired as FastAPI's default so ALL responses are safe, no per-endpoint wrapping needed.
    """
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            f = float(obj)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)

    def iterencode(self, o, _one_shot=False):
        # Pre-sanitise the whole structure so nan/inf in plain Python floats
        # are replaced before the encoder sees them.
        return super().iterencode(_sanitise_for_json(o), _one_shot)


def _sanitise_for_json(v):
    """Recursively replace nan/inf/numpy scalars with JSON-safe equivalents."""
    if isinstance(v, np.integer):
        return int(v)
    if isinstance(v, np.floating):
        f = float(v)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(v, np.bool_):
        return bool(v)
    if isinstance(v, np.ndarray):
        return [_sanitise_for_json(x) for x in v.tolist()]
    if isinstance(v, float):
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(v, dict):
        return {k: _sanitise_for_json(vv) for k, vv in v.items()}
    if isinstance(v, list):
        return [_sanitise_for_json(i) for i in v]
    return v


class _SafeJSONResponse(JSONResponse):
    """JSONResponse that uses _SafeJSONEncoder for all serialisation."""
    def render(self, content) -> bytes:
        return json.dumps(
            _sanitise_for_json(content),
            cls=_SafeJSONEncoder,
            allow_nan=False,          # belt-and-suspenders: fail loudly if anything slips
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


app = FastAPI(
    title="OC Dashboard",
    lifespan=lifespan,
    default_response_class=_SafeJSONResponse,   # ← every endpoint uses this
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ===========================================================================
# Meta endpoints
# ===========================================================================

@app.get("/api/symbols")
def list_symbols():
    cached = _cache_get("symbols")
    if cached is not None:
        return cached
    df = qdf(f"SELECT DISTINCT symbol FROM {tbl()} ORDER BY symbol")
    result = df["symbol"].tolist()
    _cache_set("symbols", result)
    return result


@app.get("/api/timestamps")
def list_timestamps(
    symbol: str           = Query(...),
    expiry: Optional[str] = Query(None),
):
    """List distinct timestamps (minute-truncated). Cached per (symbol, expiry)."""
    cache_key = f"ts:{symbol}:{expiry[:10] if expiry else 'all'}"
    cached = _cache_get(cache_key)
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
    _cache_set(cache_key, result)
    return result


@app.get("/api/expiries")
def list_expiries(
    symbol:      str           = Query(...),
    timestamp:   Optional[str] = Query(None),
    future_only: bool          = Query(True),
):
    """List expiries for a symbol. Cached per (symbol, future_only) when no timestamp filter."""
    cache_key = f"exp:{symbol}:{future_only}" if not timestamp else None
    if cache_key:
        cached = _cache_get(cache_key)
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
        _cache_set(cache_key, result)
    return result


@app.get("/api/lot_sizes")
def lot_sizes():
    """Return lot_size for each symbol (used by frontend for strike step)."""
    cached = _cache_get("lot_sizes")
    if cached is not None:
        return cached
    df = qdf(
        f"SELECT symbol, MAX(COALESCE(lotsize, 1)) AS lot_size "
        f"FROM {tbl()} GROUP BY symbol ORDER BY symbol"
    )
    result = {} if df.empty else {row["symbol"]: int(row["lot_size"]) for row in df.to_dict(orient="records")}
    _cache_set("lot_sizes", result)
    return result


# ===========================================================================
# Overview / snapshot
# ===========================================================================

@app.get("/api/overview")
def overview():
    cached = _cache_get("overview")
    if cached is not None:
        return cached
    sql = f"""
    WITH latest AS (
        SELECT symbol, MAX(timestamp) AS ts
        FROM {tbl()} GROUP BY symbol
    )
    SELECT
        t.symbol,
        l.ts                               AS timestamp,
        AVG(t.underlying_price)            AS spot,
        AVG(t.atm_strike)                  AS atm_strike,
        SUM(COALESCE(t.ce_oi,     0))      AS total_ce_oi,
        SUM(COALESCE(t.pe_oi,     0))      AS total_pe_oi,
        SUM(COALESCE(t.ce_volume, 0))      AS total_ce_vol,
        SUM(COALESCE(t.pe_volume, 0))      AS total_pe_vol,
        SUM(COALESCE(t.net_gexv,  0))      AS net_gex,
        AVG(COALESCE(t.ce_iv,     0))      AS avg_ce_iv,
        AVG(COALESCE(t.pe_iv,     0))      AS avg_pe_iv,
        MAX(COALESCE(t.lotsize,  1))       AS lot_size,
        CASE WHEN SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0)) > 0
             THEN SUM(COALESCE(t.ce_iv,0)*COALESCE(t.ce_oi,0)
                    + COALESCE(t.pe_iv,0)*COALESCE(t.pe_oi,0))
                / SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0))
             ELSE NULL END                 AS oi_wtd_iv,
        AVG(COALESCE(t.m_volatility,0)) - AVG(COALESCE(t.ce_iv,0)) AS rv_iv_spread
    FROM {tbl()} t
    JOIN latest l
      ON  t.symbol = l.symbol
      AND t.timestamp = l.ts
    GROUP BY t.symbol, l.ts
    ORDER BY t.symbol
    """
    df = qdf(sql)
    df["pcr"] = df.apply(
        lambda r: round(r["total_pe_oi"] / r["total_ce_oi"], 3)
        if r["total_ce_oi"] else None, axis=1,
    )
    result = to_records(df)
    _cache_set("overview", result)
    return result


@app.get("/api/snapshot")
def snapshot(symbol: str = Query(...)):
    ts = latest_ts(symbol)
    df = qdf(
        f"""
        SELECT underlying_price, atm_strike,
            SUM(COALESCE(ce_oi,     0)) AS total_ce_oi,
            SUM(COALESCE(pe_oi,     0)) AS total_pe_oi,
            SUM(COALESCE(ce_volume, 0)) AS total_ce_vol,
            SUM(COALESCE(pe_volume, 0)) AS total_pe_vol,
            SUM(COALESCE(net_gexv,  0)) AS net_gex,
            AVG(COALESCE(ce_iv,     0)) AS avg_ce_iv,
            AVG(COALESCE(pe_iv,     0)) AS avg_pe_iv,
            COUNT(DISTINCT expiry)       AS num_expiries
        FROM {tbl()} WHERE symbol = ? AND timestamp = ?
        GROUP BY underlying_price, atm_strike LIMIT 1
        """,
        [symbol, ts],
    )
    if df.empty:
        raise HTTPException(404, f"No snapshot for {symbol}")
    r = df.iloc[0].to_dict()
    r.update(symbol=symbol, timestamp=ts,
             pcr=round(r["total_pe_oi"]/r["total_ce_oi"],3) if r["total_ce_oi"] else None)
    return {k: (None if isinstance(v, float) and (np.isnan(v) or np.isinf(v)) else v)
            for k, v in r.items()}


# ===========================================================================
# GEX
# ===========================================================================

@app.get("/api/gex")
def gex_chart(
    symbol:    str = Query(...),
    expiry:    str = Query(...),
    timestamp: str = Query(...),
):
    """
    Fetch raw GEX columns; scale to ₹M here to avoid alias-referencing
    alias bug in DuckDB Binder.
    """
    df = qdf(
        f"""
        SELECT
            strike_price,
            COALESCE(ce_gexv,          0) AS raw_ce_gex,
            COALESCE(pe_gexv,          0) AS raw_pe_gex,
            COALESCE(net_gexv,        0) AS raw_net_gexv,
            COALESCE(ce_oi,           0) AS ce_oi,
            COALESCE(pe_oi,           0) AS pe_oi,
            COALESCE(atm_strike,      0) AS atm_strike,
            COALESCE(underlying_price,0) AS spot
        FROM {tbl()}
        WHERE symbol    = ?
          AND expiry = ?
          AND timestamp BETWEEN ? AND ? 
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + _ts_lo_hi(timestamp),
    )
    if df.empty:
        raise HTTPException(404, "No GEX data for given filters")
    df["ce_gexv"]   = df["raw_ce_gex"]   / 1e6
    df["pe_gexv"]   = df["raw_pe_gex"]   / 1e6
    df["net_gexv"] = df["raw_net_gexv"] / 1e6
    df.drop(columns=["raw_ce_gex","raw_pe_gex","raw_net_gexv"], inplace=True)
    return to_records(df)


# ===========================================================================
# Gamma profile
# ===========================================================================

def _build_gamma_profile(
    symbol: str, expiry: str, ts_filter: str,
    num_levels: int = 200, price_range_pct: float = 5.0,
) -> tuple[pd.DataFrame, float, float, int]:
    """Returns (profile_df, spot, dte, lot_size)."""
    df = qdf(
        f"""
        SELECT
            strike_price,
            COALESCE(ce_gamma,        0) AS ce_gamma,
            COALESCE(pe_gamma,        0) AS pe_gamma,
            COALESCE(ce_oi,           0) AS ce_oi,
            COALESCE(pe_oi,           0) AS pe_oi,
            COALESCE(underlying_price,0) AS spot,
            COALESCE(days_to_expiry,  0) AS dte,
            COALESCE(atm_strike,      0) AS atm_strike,
            lotsize                       AS raw_lot,
            COALESCE(ce_iv,           0) AS ce_iv
        FROM {tbl()}
        WHERE symbol = ?
          AND expiry  = ? 
          AND timestamp BETWEEN ?  AND ? 
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + _ts_lo_hi(ts_filter),
    )
    if df.empty:
        return pd.DataFrame(), 0, 0, 1

    spot     = float(df["spot"].iloc[0])
    dte      = float(df["dte"].iloc[0])
    lot_size = max(int(df["raw_lot"].iloc[0]) if df["raw_lot"].iloc[0] is not None else 1, 1)
    strikes  = df["strike_price"].values
    call_gex = df["ce_gamma"].values * df["ce_oi"].values * lot_size
    put_gex  = df["pe_gamma"].values * df["pe_oi"].values * lot_size

    lo     = spot * (1 - price_range_pct / 100)
    hi     = spot * (1 + price_range_pct / 100)
    levels = np.linspace(lo, hi, num_levels)

    rows = []
    for lvl in levels:
        ni = int(np.argmin(np.abs(strikes - lvl)))
        cg =  float(call_gex[ni]) / 1e9
        pg = -float(put_gex[ni])  / 1e9
        rows.append({
            "level":                float(lvl),
            "near_strike":          float(strikes[ni]),
            "call_gamma_billions":  cg,
            "put_gamma_billions":   pg,
            "total_gamma_billions": cg + pg,
        })
    return pd.DataFrame(rows), spot, dte, lot_size


def _flip_and_magnet(gdf: pd.DataFrame):
    g, x = gdf["total_gamma_billions"].values, gdf["level"].values
    idx  = np.where(np.sign(g[:-1]) != np.sign(g[1:]))[0]
    gamma_flip = None
    if len(idx):
        i = idx[0]; d = g[i+1]-g[i]
        if d: gamma_flip = float(x[i] - g[i]*(x[i+1]-x[i])/d)
    max_g = float(gdf["total_gamma_billions"].max())
    magnet = None
    if max_g > 0:
        zone = gdf[gdf["total_gamma_billions"] >= 0.75 * max_g]
        if not zone.empty:
            magnet = {
                "lower":    float(zone["near_strike"].min()),
                "upper":    float(zone["near_strike"].max()),
                "center":   float(zone.loc[zone["total_gamma_billions"].idxmax(), "near_strike"]),
                "strength": round(max_g, 4),
            }
    return gamma_flip, magnet


@app.get("/api/gamma_profile")
def gamma_profile(
    symbol:          str           = Query(...),
    expiry:          str           = Query(...),
    timestamp:       Optional[str] = Query(None),
    num_levels:      int           = Query(200),
    price_range_pct: float         = Query(5.0),
):
    ts_filter = timestamp or latest_ts(symbol)
    gdf, spot, dte, lot = _build_gamma_profile(
        symbol, expiry, ts_filter, num_levels, price_range_pct
    )
    if gdf.empty:
        raise HTTPException(404, "No data for gamma profile")
    gamma_flip, magnet = _flip_and_magnet(gdf)
    return safe_response({
        "spot": spot, "gamma_flip": gamma_flip,
        "magnet": magnet, "profile": to_records(gdf),
    })


@app.get("/api/gamma_analysis")
def gamma_analysis(
    symbol:          str           = Query(...),
    expiry:          str           = Query(...),
    timestamp:       Optional[str] = Query(None),
    price_range_pct: float         = Query(5.0),
    num_levels:      int           = Query(200),
):
    ts_filter = timestamp or latest_ts(symbol)
    gdf, spot, dte, lot = _build_gamma_profile(
        symbol, expiry, ts_filter, num_levels, price_range_pct
    )
    if gdf.empty:
        raise HTTPException(404, "No data")

    # ATM IV
    raw = qdf(
        f"""
        SELECT strike_price, COALESCE(atm_strike,0) AS atm_strike,
               COALESCE(ce_iv, 0) AS ce_iv
        FROM {tbl()}
        WHERE symbol = ?
          AND expiry  = ? 
          AND timestamp  BETWEEN ?  AND ? 
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + _ts_lo_hi(ts_filter),
    )
    atm_str = float(raw["atm_strike"].iloc[0]) if not raw.empty else spot
    near    = raw.iloc[(raw["strike_price"] - atm_str).abs().argsort()[:2]]
    atm_iv_pct = float(near["ce_iv"].mean()) if not near.empty else 0.0
    atm_iv     = atm_iv_pct / 100.0

    # ATR — try three sources in order of preference:
    # 1. m_volatility column in oc.duckdb (annualised vol → daily ATR proxy)
    # 2. nse_fetcher historical OHLC (if module present)
    # 3. None (gamma analysis still runs, ATR-dependent fields show —)
    atr = None
    try:
        vol_df = qdf(
            f"SELECT AVG(COALESCE(m_volatility, 0)) AS avg_vol, "
            f"AVG(COALESCE(underlying_price, 0)) AS avg_spot "
            f"FROM {tbl()} "
            f"WHERE symbol = ? AND timestamp  BETWEEN ?  AND ? ",
            [symbol] + _ts_lo_hi(ts_filter),
        )
        if not vol_df.empty:
            avg_vol  = float(vol_df["avg_vol"].iloc[0] or 0)
            avg_spot = float(vol_df["avg_spot"].iloc[0] or spot)
            if avg_vol > 0 and avg_spot > 0:
                # Convert annualised % vol to daily point ATR
                atr = round(avg_spot * (avg_vol / 100.0) / (252 ** 0.5), 2)
    except Exception:
        pass

    if atr is None:
        try:
            from nse_fetcher import NSEFetcher
            fetcher    = NSEFetcher()
            end_date   = date.today()
            start_date = end_date - timedelta(days=30)
            ohlc_df = (
                fetcher.get_index_historical(symbol, start_date, end_date)
                if NSEFetcher.is_index(symbol)
                else fetcher.get_equity_historical(symbol, start_date, end_date)
            )
            if ohlc_df is not None and not ohlc_df.empty and \
                    all(c in ohlc_df.columns for c in ("high","low","close")):
                high, low, close = ohlc_df["high"], ohlc_df["low"], ohlc_df["close"]
                tr = pd.concat([
                    high - low,
                    (high - close.shift()).abs(),
                    (low  - close.shift()).abs(),
                ], axis=1).max(axis=1)
                val = float(tr.rolling(14).mean().iloc[-1])
                if not np.isnan(val):
                    atr = val
        except ModuleNotFoundError:
            pass   # nse_fetcher not installed — silent, m_volatility path is preferred
        except Exception as exc:
            print(f"  ATR via NSEFetcher skipped: {exc}")

    result = _gamma_analysis_inline(
        gdf=gdf, spot=spot, dte=dte, atm_iv=atm_iv, atr=atr,
    )
    result.update(symbol=symbol, expiry=expiry, spot=spot, dte=dte,
                  atm_iv_pct=round(atm_iv_pct, 2) if atm_iv_pct else None)
    return safe_response(result)


# ===========================================================================
# OI change / signals
# ===========================================================================

@app.get("/api/oi_change")
def oi_change(symbol: str = Query(...), filter_type: str = Query("all")):
    ts_df = qdf(
        f"SELECT DISTINCT CAST(timestamp AS VARCHAR) AS ts FROM {tbl()} "
        f"WHERE symbol = ? ORDER BY ts DESC LIMIT 2",
        [symbol],
    )
    if len(ts_df) < 2:
        raise HTTPException(422, f"Need ≥2 timestamps for {symbol}; found {len(ts_df)}")
    ts_new, ts_old = ts_df["ts"].iloc[0], ts_df["ts"].iloc[1]

    df = qdf(
        f"""
        SELECT
            n.strike_price,
            CAST(n.expiry AS VARCHAR)  AS expiry,
            COALESCE(n.ce_oi,  0)      AS ce_oi_new,
            COALESCE(o.ce_oi,  0)      AS ce_oi_old,
            COALESCE(n.pe_oi,  0)      AS pe_oi_new,
            COALESCE(o.pe_oi,  0)      AS pe_oi_old,
            COALESCE(n.ce_ltp, 0)      AS ce_ltp_new,
            COALESCE(o.ce_ltp, 0)      AS ce_ltp_old,
            COALESCE(n.pe_ltp, 0)      AS pe_ltp_new,
            COALESCE(o.pe_ltp, 0)      AS pe_ltp_old
        FROM {tbl()} n
        JOIN {tbl()} o
          ON  n.symbol       = o.symbol
          AND n.strike_price = o.strike_price
          AND n.expiry       = o.expiry
        WHERE n.symbol = ?
          AND n.timestamp  BETWEEN ?  AND ? 
          AND o.timestamp  BETWEEN ?  AND ? 
          AND n.expiry >= CURRENT_DATE
          AND (COALESCE(n.ce_ltp,0) + COALESCE(n.pe_ltp,0)) > 0
        ORDER BY n.expiry, n.strike_price
        """,
        [symbol] + _ts_lo_hi(ts_new) + _ts_lo_hi(ts_old),
    )
    if df.empty:
        raise HTTPException(404, f"No overlapping data between timestamps for {symbol}")

    df["ce_oi_chg"]  = df["ce_oi_new"]  - df["ce_oi_old"]
    df["pe_oi_chg"]  = df["pe_oi_new"]  - df["pe_oi_old"]
    df["ce_ltp_chg"] = df["ce_ltp_new"] - df["ce_ltp_old"]
    df["pe_ltp_chg"] = df["pe_ltp_new"] - df["pe_ltp_old"]

    def _sig(oi, ltp):
        if oi > 0 and ltp > 0: return "Long Build-Up"
        if oi > 0 and ltp < 0: return "Short Build-Up"
        if oi < 0 and ltp > 0: return "Short Covering"
        if oi < 0 and ltp < 0: return "Long Unwinding"
        return "Neutral"

    df["ce_signal"] = df.apply(lambda r: _sig(r["ce_oi_chg"], r["ce_ltp_chg"]), axis=1)
    df["pe_signal"] = df.apply(lambda r: _sig(r["pe_oi_chg"], r["pe_ltp_chg"]), axis=1)
    df["ts_new"]    = ts_new
    df["ts_old"]    = ts_old
    return to_records(df)


@app.get("/api/oi_signals_all")
def oi_signals_all():
    symbols = qdf(f"SELECT DISTINCT symbol FROM {tbl()} ORDER BY symbol")["symbol"].tolist()
    rows = []
    for sym in symbols:
        ts_df = qdf(
            f"SELECT DISTINCT CAST(timestamp AS VARCHAR) AS ts FROM {tbl()} "
            f"WHERE symbol=? ORDER BY ts DESC LIMIT 2", [sym],
        )
        if len(ts_df) < 2: continue
        ts_new, ts_old = ts_df["ts"].iloc[0], ts_df["ts"].iloc[1]
        agg = qdf(
            f"""
            SELECT
                SUM(COALESCE(n.ce_oi,  0))-SUM(COALESCE(o.ce_oi,  0)) AS ce_oi_chg,
                SUM(COALESCE(n.pe_oi,  0))-SUM(COALESCE(o.pe_oi,  0)) AS pe_oi_chg,
                AVG(COALESCE(n.ce_ltp, 0))-AVG(COALESCE(o.ce_ltp, 0)) AS avg_ce_ltp_chg,
                AVG(COALESCE(n.pe_ltp, 0))-AVG(COALESCE(o.pe_ltp, 0)) AS avg_pe_ltp_chg
            FROM {tbl()} n
            JOIN {tbl()} o
              ON  n.symbol=o.symbol AND n.strike_price=o.strike_price AND n.expiry=o.expiry
            WHERE n.symbol=?
              AND n.timestamp  BETWEEN ?  AND ? 
              AND o.timestamp  BETWEEN ?  AND ?
            """,
            [sym] + _ts_lo_hi(ts_new) + _ts_lo_hi(ts_old),
        )
        if agg.empty: continue
        r = agg.iloc[0].to_dict()
        r["symbol"] = sym; r["ts_new"] = ts_new
        r["total_oi_chg"] = abs(r.get("ce_oi_chg") or 0) + abs(r.get("pe_oi_chg") or 0)
        rows.append(r)
    if not rows: return []
    return to_records(pd.DataFrame(rows).sort_values("total_oi_chg", ascending=False))


# ===========================================================================
# Shockers
# ===========================================================================

@app.get("/api/volume_shockers")
def volume_shockers(top_n: int = Query(30), filter_type: str = Query("all")):
    sql = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               COALESCE(ce_volume,0)+COALESCE(pe_volume,0) AS total_vol,
               DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE           -- active expiries only
          AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0   -- has live price
    ),
    latest AS (SELECT * FROM ts_ranked WHERE rk=1),
    prev   AS (SELECT * FROM ts_ranked WHERE rk=2)
    SELECT
        l.symbol, l.strike_price,
        CAST(l.expiry AS VARCHAR) AS expiry,
        l.total_vol               AS vol_now,
        p.total_vol               AS vol_prev,
        l.total_vol-p.total_vol   AS vol_delta,
        CASE WHEN p.total_vol>0
             THEN ROUND((l.total_vol-p.total_vol)*100.0/p.total_vol,1)
             ELSE NULL END        AS vol_pct_chg
    FROM latest l
    JOIN prev p ON l.symbol=p.symbol AND l.strike_price=p.strike_price AND l.expiry=p.expiry
    WHERE l.total_vol > p.total_vol AND l.total_vol > 0
    ORDER BY vol_delta DESC LIMIT ?
    """
    df = qdf(sql, [top_n * 3])  # fetch extra then filter
    if df.empty: return []
    idx_set = _NSE_INDICES
    if filter_type == "index":  df = df[df["symbol"].isin(idx_set)]
    elif filter_type == "stock": df = df[~df["symbol"].isin(idx_set)]
    return to_records(df.head(top_n))


@app.get("/api/iv_shockers")
def iv_shockers(top_n: int = Query(30), filter_type: str = Query("all")):
    sql = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               (COALESCE(ce_iv,0)+COALESCE(pe_iv,0))/2.0 AS avg_iv,
               DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE           -- active expiries only
          AND (COALESCE(ce_iv,0) + COALESCE(pe_iv,0)) > 0     -- has live IV
    ),
    latest AS (SELECT * FROM ts_ranked WHERE rk=1),
    prev   AS (SELECT * FROM ts_ranked WHERE rk=2)
    SELECT
        l.symbol, l.strike_price,
        CAST(l.expiry AS VARCHAR) AS expiry,
        l.avg_iv                  AS iv_now,
        p.avg_iv                  AS iv_prev,
        l.avg_iv-p.avg_iv         AS iv_delta,
        ABS(l.avg_iv-p.avg_iv)    AS abs_iv_delta
    FROM latest l
    JOIN prev p ON l.symbol=p.symbol AND l.strike_price=p.strike_price AND l.expiry=p.expiry
    WHERE l.avg_iv>0 AND p.avg_iv>0
    ORDER BY abs_iv_delta DESC LIMIT ?
    """
    df = qdf(sql, [top_n * 3])
    if df.empty: return []
    if filter_type == "index":  df = df[df["symbol"].isin(_NSE_INDICES)]
    elif filter_type == "stock": df = df[~df["symbol"].isin(_NSE_INDICES)]
    return to_records(df.head(top_n))


# ===========================================================================
# IV smile
# ===========================================================================

@app.get("/api/iv_smile")
def iv_smile(
    symbol:      str           = Query(...),
    expiry:      str           = Query(...),
    timestamp:   Optional[str] = Query(None),
    filter_type: str           = Query("all"),
):
    ts_filter = timestamp or latest_ts(symbol)
    df = qdf(
        f"""
        SELECT strike_price,
               COALESCE(ce_iv,     0) AS ce_iv,
               COALESCE(pe_iv,     0) AS pe_iv,
               COALESCE(ce_volume, 0) AS ce_volume,
               COALESCE(pe_volume, 0) AS pe_volume,
               COALESCE(ce_oi,     0) AS ce_oi,
               COALESCE(pe_oi,     0) AS pe_oi,
               COALESCE(underlying_price,0) AS spot
        FROM {tbl()}
        WHERE symbol = ?
          AND expiry = ? 
          AND timestamp  BETWEEN ?  AND ? 
          AND (COALESCE(ce_iv,0)>0 OR COALESCE(pe_iv,0)>0)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10]] + _ts_lo_hi(ts_filter),
    )
    if df.empty:
        raise HTTPException(404, "No IV data found")

    from scipy.interpolate import UnivariateSpline

    def _fit(x_all: np.ndarray, y_all: np.ndarray):
        fit_mask = (y_all > 0.5) & (y_all < 150)
        xf, yf   = x_all[fit_mask], y_all[fit_mask]
        fitted   = np.full(len(x_all), np.nan)
        zscore   = np.full(len(x_all), np.nan)
        anomaly  = np.zeros(len(x_all), dtype=int)
        if len(xf) >= 5:
            try:
                s   = max(len(xf) * 0.5, 3.0)
                spl = UnivariateSpline(xf, yf, s=s, k=min(3, len(xf) - 1))
                fv  = spl(x_all)
                res = y_all - fv
                std = float(np.std(res[fit_mask])) or 1.0
                z   = res / std
                for i in range(len(x_all)):
                    if y_all[i] > 0:
                        fitted[i]  = float(fv[i])
                        zscore[i]  = float(z[i])
                        anomaly[i] = int(abs(z[i]) > 2.0 and fit_mask[i])
            except Exception as exc:
                print(f"  ⚠ Spline: {exc}")
        return fitted, zscore, anomaly

    strikes = df["strike_price"].values
    df["ce_iv_fit"], df["ce_zscore"], df["ce_anomaly"] = _fit(strikes, df["ce_iv"].values)
    df["pe_iv_fit"], df["pe_zscore"], df["pe_anomaly"] = _fit(strikes, df["pe_iv"].values)
    return to_records(df)


# ===========================================================================
# Top movers
# ===========================================================================

@app.get("/api/top_movers")
def top_movers(
    side:        str = Query("CE"),
    top_n:       int = Query(20),
    filter_type: str = Query("all"),
):
    col = "ce_ltp" if side.upper() == "CE" else "pe_ltp"
    idx_list = ", ".join(f"'{s}'" for s in _NSE_INDICES)
    sym_filter = (
        f"AND n.symbol IN ({idx_list})"      if filter_type == "index"
        else f"AND n.symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    base = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               COALESCE({col},0) AS ltp,
               DENSE_RANK() OVER (
                   PARTITION BY symbol, strike_price, expiry
                   ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE          -- exclude expired options
    ),
    latest AS (SELECT * FROM ts_ranked WHERE rk=1),
    prev   AS (SELECT * FROM ts_ranked WHERE rk=2)
    SELECT
        n.symbol, n.strike_price,
        CAST(n.expiry AS VARCHAR) AS expiry,
        n.ltp                     AS ltp_now,
        p.ltp                     AS ltp_prev,
        n.ltp-p.ltp               AS ltp_chg,
        CASE WHEN p.ltp>0
             THEN ROUND((n.ltp-p.ltp)*100.0/p.ltp,1)
             ELSE NULL END        AS ltp_pct_chg
    FROM latest n
    JOIN prev p ON n.symbol=p.symbol AND n.strike_price=p.strike_price AND n.expiry=p.expiry
    WHERE p.ltp>0                             -- at least one trade both snapshots
      AND n.ltp>0                             -- actively traded now
      {sym_filter}
    """
    return safe_response({
        "gainers": to_records(qdf(base + f" ORDER BY ltp_chg DESC LIMIT {top_n}")),
        "losers":  to_records(qdf(base + f" ORDER BY ltp_chg ASC  LIMIT {top_n}")),
    })


# ===========================================================================
# Strike trend
# ===========================================================================

_SAFE_METRICS = {
    "ce_ltp","pe_ltp","ce_iv","pe_iv","ce_volume","pe_volume",
    "ce_oi","pe_oi","ce_gexv","pe_gexv","net_gexv",
    "ce_delta","pe_delta","ce_gamma","pe_gamma",
    "ce_theta","pe_theta","ce_vega","pe_vega",
}


@app.get("/api/atm_strikes")
def atm_strikes(
    symbol: str           = Query(...),
    expiry: str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """Return distinct distance_from_atm values and corresponding strikes."""
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)
    df = qdf(
        f"SELECT DISTINCT distance_from_atm, strike_price, atm_strike "
        f"FROM {tbl()} WHERE symbol=? AND expiry  = ?  "
        f"AND {ts_clause} ORDER BY distance_from_atm",
        [symbol, expiry[:10]] + ts_params,
    )
    return to_records(df) if not df.empty else []


@app.get("/api/strike_trend")
def strike_trend(
    symbol:       str   = Query(...),
    strike_price: float = Query(...),
    expiry:       str   = Query(...),
    metric:       str   = Query("ce_ltp"),
):
    if metric not in _SAFE_METRICS:
        raise HTTPException(400, f"Unknown metric '{metric}'")
    df = qdf(
        f"""
        SELECT CAST(timestamp AS VARCHAR) AS timestamp,
               COALESCE({metric},0)       AS value
        FROM {tbl()}
        WHERE symbol=? AND strike_price=? AND expiry  = ? 
        ORDER BY timestamp
        """,
        [symbol, strike_price, expiry[:10]],
    )
    return to_records(df)


# ===========================================================================
# Delta screener
# ===========================================================================

@app.get("/api/delta_screener")
def delta_screener(
    timestamp:    Optional[str] = Query(None),
    target_delta: float         = Query(30.0),   # upper bound (absolute)
    min_delta:    float         = Query(5.0),    # lower bound (absolute)
    filter_type:  str           = Query("all"),  # all | index | stock
):
    tgt_hi = abs(target_delta) / 100.0
    tgt_lo = abs(min_delta)    / 100.0

    idx_list = ", ".join(f"\'{s}\'" for s in _NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )

    if timestamp:
        ts_clause, ts_params = ts_filter_clause(timestamp)
        ts_sql = f"AND {ts_clause}"
    else:
        ts_sql    = ("AND timestamp = ("
                     f"  SELECT MAX(timestamp) "
                     f"  FROM {tbl()} t2 WHERE t2.symbol = {tbl()}.symbol)")
        ts_params = []

    df = qdf(
        f"""
        SELECT
            symbol, strike_price,
            CAST(expiry AS VARCHAR)       AS expiry,
            COALESCE(underlying_price, 0) AS spot,
            COALESCE(days_to_expiry,   0) AS dte,
            lotsize                       AS raw_lot,
            COALESCE(ce_ltp,    0) AS ce_ltp,    COALESCE(pe_ltp,    0) AS pe_ltp,
            COALESCE(ce_delta,  0) AS ce_delta,  COALESCE(pe_delta,  0) AS pe_delta,
            COALESCE(ce_iv,     0) AS ce_iv,     COALESCE(pe_iv,     0) AS pe_iv,
            COALESCE(ce_oi,     0) AS ce_oi,     COALESCE(pe_oi,     0) AS pe_oi,
            COALESCE(ce_volume, 0) AS ce_volume, COALESCE(pe_volume, 0) AS pe_volume,
            COALESCE(ce_gamma,  0) AS ce_gamma,  COALESCE(pe_gamma,  0) AS pe_gamma,
            COALESCE(ce_theta,  0) AS ce_theta,  COALESCE(pe_theta,  0) AS pe_theta,
            COALESCE(ce_gexv,   0) AS ce_gexv,   COALESCE(pe_gexv,   0) AS pe_gexv,
            COALESCE(net_gexv,  0) AS net_gexv
        FROM {tbl()}
        WHERE 1=1 {sym_filter} {ts_sql}
          AND (
            ABS(COALESCE(ce_delta, 0)) BETWEEN {tgt_lo} AND {tgt_hi}
            OR
            ABS(COALESCE(pe_delta, 0)) BETWEEN {tgt_lo} AND {tgt_hi}
          )
        ORDER BY symbol, expiry, strike_price
        """,
        ts_params,
    )
    if df.empty:
        return safe_response({"target_delta": target_delta, "min_delta": min_delta, "rows": []})

    rows = []
    for _, r in df.iterrows():
        lot  = max(int(r["raw_lot"]) if r["raw_lot"] else 1, 1)
        spot = float(r["spot"])

        def make_row(otype, delta_raw, ltp, iv, oi, vol, gamma, theta, gexv):
            d = abs(float(delta_raw))
            return {
                "option_type": otype, "symbol": r["symbol"],
                "expiry": r["expiry"], "strike_price": float(r["strike_price"]),
                "delta": round(d, 4), "ltp": float(ltp),
                "iv": float(iv), "oi": float(oi), "volume": float(vol),
                "gamma": round(float(gamma), 6),
                "theta": round(float(theta), 4),
                "gexv":  round(float(gexv),  2),
                "net_gexv": round(float(r["net_gexv"]), 2),
                "lot_size": lot,
                "premium_per_lot": round(float(ltp) * lot, 2),
                "risk_indicator":  round(float(r["strike_price"]) * lot, 2),
                "spot": spot, "dte": float(r["dte"]),
                "margin": None, "return_on_margin": None,
            }

        ce_d = abs(float(r["ce_delta"]))
        if tgt_lo <= ce_d <= tgt_hi:
            rows.append(make_row("CE", r["ce_delta"], r["ce_ltp"], r["ce_iv"],
                                 r["ce_oi"], r["ce_volume"], r["ce_gamma"],
                                 r["ce_theta"], r["ce_gexv"]))

        pe_d = abs(float(r["pe_delta"]))
        if tgt_lo <= pe_d <= tgt_hi:
            rows.append(make_row("PE", r["pe_delta"], r["pe_ltp"], r["pe_iv"],
                                 r["pe_oi"], r["pe_volume"], r["pe_gamma"],
                                 r["pe_theta"], r["pe_gexv"]))

    return safe_response({"target_delta": target_delta, "min_delta": min_delta, "rows": rows})




# ===========================================================================
# Max Pain
# ===========================================================================

@app.get("/api/max_pain")
def max_pain(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Compute max pain strike for one expiry at a given timestamp.
    Max pain = strike where total intrinsic-value loss for option buyers is maximised,
    i.e. where option writers (MMs) lose least if price expires there.
    Returns: pain curve (intrinsic loss per potential expiry price) + max_pain_strike.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi = _ts_lo_hi(ts_filter)
    df = qdf(
        f"""
        SELECT strike_price,
               COALESCE(ce_oi, 0) AS ce_oi,
               COALESCE(pe_oi, 0) AS pe_oi,
               COALESCE(lotsize, 1) AS lotsize,
               COALESCE(underlying_price, 0) AS spot
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp BETWEEN ? AND ?
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], lo, hi],
    )
    if df.empty:
        raise HTTPException(404, "No data for max pain")

    strikes  = df["strike_price"].values
    ce_oi    = df["ce_oi"].values
    pe_oi    = df["pe_oi"].values
    lot      = max(int(df["lotsize"].iloc[0]), 1)
    spot     = float(df["spot"].iloc[0])

    # For each candidate expiry price, compute total intrinsic value (buyer loss = writer gain)
    pain = []
    for price in strikes:
        call_loss = float(np.sum(np.maximum(price - strikes, 0) * ce_oi * lot))
        put_loss  = float(np.sum(np.maximum(strikes - price, 0) * pe_oi * lot))
        pain.append({
            "price":     float(price),
            "call_pain": call_loss / 1e6,
            "put_pain":  put_loss  / 1e6,
            "total_pain":(call_loss + put_loss) / 1e6,
        })

    pain_df      = pd.DataFrame(pain)
    max_pain_idx = int(pain_df["total_pain"].idxmin())
    max_pain_strike = float(pain_df.loc[max_pain_idx, "price"])

    return safe_response({
        "symbol":          symbol,
        "expiry":          expiry[:10],
        "spot":            spot,
        "max_pain_strike": max_pain_strike,
        "distance_pts":    round(max_pain_strike - spot, 2),
        "distance_pct":    round((max_pain_strike - spot) / spot * 100, 3) if spot else None,
        "pain_curve":      to_records(pain_df),
    })


@app.get("/api/max_pain_series")
def max_pain_series(
    symbol: str = Query(...),
    expiry: str = Query(...),
):
    """
    Time series of max pain strike for one symbol+expiry across all timestamps in DB.
    Shows how max pain drifts intraday as OI changes.
    """
    # Get all distinct timestamps for this symbol+expiry
    ts_df = qdf(
        f"SELECT DISTINCT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts "
        f"FROM {tbl()} WHERE symbol = ? AND expiry = ? ORDER BY ts",
        [symbol, expiry[:10]],
    )
    if ts_df.empty:
        raise HTTPException(404, "No timestamps found")

    series = []
    for ts in ts_df["ts"].tolist():
        if ts is None or (isinstance(ts, float)):
            continue          # skip NaN rows produced by STRFTIME on NULL timestamps
        ts = str(ts)
        lo, hi = _ts_lo_hi(ts)
        df = qdf(
            f"""
            SELECT strike_price,
                   COALESCE(ce_oi, 0) AS ce_oi,
                   COALESCE(pe_oi, 0) AS pe_oi,
                   COALESCE(lotsize, 1) AS lotsize,
                   COALESCE(underlying_price, 0) AS spot
            FROM {tbl()}
            WHERE symbol = ? AND expiry = ?
              AND timestamp BETWEEN ? AND ?
            ORDER BY strike_price
            """,
            [symbol, expiry[:10], lo, hi],
        )
        if df.empty:
            continue
        strikes = df["strike_price"].values
        ce_oi   = df["ce_oi"].values
        pe_oi   = df["pe_oi"].values
        lot     = max(int(df["lotsize"].iloc[0]), 1)
        spot    = float(df["spot"].iloc[0])
        pain_vals = []
        for price in strikes:
            total = float(np.sum(np.maximum(price - strikes, 0) * ce_oi * lot) +
                          np.sum(np.maximum(strikes - price, 0) * pe_oi * lot))
            pain_vals.append(total)
        mp_idx = int(np.argmin(pain_vals))
        series.append({
            "timestamp":       ts,
            "max_pain_strike": float(strikes[mp_idx]),
            "spot":            spot,
            "distance_pts":    round(float(strikes[mp_idx]) - spot, 2),
        })

    return safe_response({"symbol": symbol, "expiry": expiry[:10], "series": series})


# ===========================================================================
# IV Term Structure
# ===========================================================================

@app.get("/api/iv_term_structure")
def iv_term_structure(
    symbol:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    ATM CE and PE IV across all expiries at a given timestamp.
    Uses the row where distance_from_atm = 0 (ATM strike) per expiry.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)
    df = qdf(
        f"""
        SELECT
            CAST(expiry AS VARCHAR)         AS expiry,
            COALESCE(days_to_expiry, 0)     AS dte,
            COALESCE(ce_iv, 0)              AS ce_iv,
            COALESCE(pe_iv, 0)              AS pe_iv,
            COALESCE(ce_iv_nse, 0)          AS ce_iv_nse,
            COALESCE(underlying_price, 0)   AS spot,
            strike_price
        FROM {tbl()}
        WHERE symbol = ?
          AND timestamp BETWEEN ? AND ?
          AND distance_from_atm = 0
        ORDER BY expiry
        """,
        [symbol, lo, hi],
    )
    if df.empty:
        # Fallback: nearest strike to spot per expiry
        df = qdf(
            f"""
            WITH ranked AS (
                SELECT
                    CAST(expiry AS VARCHAR)       AS expiry,
                    COALESCE(days_to_expiry, 0)   AS dte,
                    COALESCE(ce_iv, 0)            AS ce_iv,
                    COALESCE(pe_iv, 0)            AS pe_iv,
                    COALESCE(ce_iv_nse, 0)        AS ce_iv_nse,
                    COALESCE(underlying_price, 0) AS spot,
                    strike_price,
                    ABS(distance_from_atm)        AS atm_dist,
                    ROW_NUMBER() OVER (
                        PARTITION BY expiry
                        ORDER BY ABS(distance_from_atm), strike_price
                    ) AS rn
                FROM {tbl()}
                WHERE symbol = ? AND timestamp BETWEEN ? AND ?
            )
            SELECT * FROM ranked WHERE rn = 1 ORDER BY expiry
            """,
            [symbol, lo, hi],
        )
    if df.empty:
        raise HTTPException(404, "No IV term structure data")

    # Compute mid-IV and skew per row
    df["mid_iv"]  = (df["ce_iv"] + df["pe_iv"]) / 2.0
    df["skew"]    = df["pe_iv"] - df["ce_iv"]     # positive = put premium (normal)

    return safe_response({
        "symbol":    symbol,
        "timestamp": ts_filter[:16],
        "rows":      to_records(df[["expiry","dte","ce_iv","pe_iv","ce_iv_nse","mid_iv","skew","spot","strike_price"]]),
    })


# ===========================================================================
# IV Rank / Percentile / Realised-Implied spread
# ===========================================================================

@app.get("/api/iv_rank")
def iv_rank(
    symbol:        str           = Query(...),
    expiry:        str           = Query(...),
    timestamp:     Optional[str] = Query(None),
    lookback_days: int           = Query(90),
):
    """
    IV Rank and IV Percentile for ATM options of a given symbol+expiry.
    Lookback window is configurable (default 90 days from the selected timestamp).

    IV Rank     = (current_iv - min_iv) / (max_iv - min_iv)  × 100
    IV Pctile   = fraction of historical ATM IVs below current IV × 100
    RV-IV spread = m_volatility (realised annualised) - ATM IV
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)

    # Current ATM IV
    cur = qdf(
        f"""
        SELECT COALESCE(ce_iv, 0) AS ce_iv,
               COALESCE(pe_iv, 0) AS pe_iv,
               COALESCE(m_volatility, 0) AS rv
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp BETWEEN ? AND ?
          AND distance_from_atm = 0
        LIMIT 1
        """,
        [symbol, expiry[:10], lo, hi],
    )
    if cur.empty:
        raise HTTPException(404, "No current ATM IV data")

    ce_iv_now = float(cur["ce_iv"].iloc[0])
    pe_iv_now = float(cur["pe_iv"].iloc[0])
    atm_iv    = (ce_iv_now + pe_iv_now) / 2.0
    rv        = float(cur["rv"].iloc[0])

    # Historical ATM IVs within lookback window
    cutoff = (datetime.strptime(ts_filter[:16], "%Y-%m-%d %H:%M")
              - timedelta(days=lookback_days)).strftime("%Y-%m-%d %H:%M:%S")
    hist = qdf(
        f"""
        SELECT (COALESCE(ce_iv,0) + COALESCE(pe_iv,0)) / 2.0 AS atm_iv
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp >= ?
          AND distance_from_atm = 0
          AND (COALESCE(ce_iv,0) + COALESCE(pe_iv,0)) > 0
        """,
        [symbol, expiry[:10], cutoff],
    )

    iv_rank = iv_pctile = None
    hist_count = 0
    if not hist.empty:
        vals = hist["atm_iv"].dropna().values
        hist_count = len(vals)
        if len(vals) >= 2:
            iv_min, iv_max = float(vals.min()), float(vals.max())
            iv_rank   = round((atm_iv - iv_min) / (iv_max - iv_min) * 100, 1)                         if iv_max > iv_min else None
            iv_pctile = round(float(np.mean(vals < atm_iv)) * 100, 1)

    return safe_response({
        "symbol":       symbol,
        "expiry":       expiry[:10],
        "ce_iv":        round(ce_iv_now, 2),
        "pe_iv":        round(pe_iv_now, 2),
        "atm_iv":       round(atm_iv, 2),
        "rv":           round(rv, 2),
        "rv_iv_spread": round(rv - atm_iv, 2),
        "iv_rank":      iv_rank,
        "iv_pctile":    iv_pctile,
        "lookback_days":lookback_days,
        "hist_count":   hist_count,
    })


# ===========================================================================
# Put-Call Skew (25-delta Risk Reversal + sentiment/regime from DB)
# ===========================================================================

@app.get("/api/pc_skew")
def pc_skew(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Put-Call skew analysis:
    - 25-delta risk reversal: IV of ~25-delta put minus ~25-delta call
    - OTM skew: left-wing slope vs right-wing slope of IV smile
    - riskreversal, sentiment, regime columns from the ATM row
    - Time series of riskreversal across timestamps for this expiry
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)

    # All strikes for this expiry/timestamp
    df = qdf(
        f"""
        SELECT
            strike_price,
            distance_from_atm,
            COALESCE(ce_iv,    0) AS ce_iv,
            COALESCE(pe_iv,    0) AS pe_iv,
            COALESCE(ce_delta, 0) AS ce_delta,
            COALESCE(pe_delta, 0) AS pe_delta,
            COALESCE(riskreversal, 0) AS riskreversal,
            CAST(sentiment AS VARCHAR)  AS sentiment,
            CAST(regime    AS VARCHAR)  AS regime,
            COALESCE(underlying_price, 0) AS spot
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp BETWEEN ? AND ?
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], lo, hi],
    )
    if df.empty:
        raise HTTPException(404, "No skew data")

    spot = float(df["spot"].iloc[0])

    # ATM row (distance_from_atm == 0)
    atm_rows = df[df["distance_from_atm"] == 0]
    atm_row  = atm_rows.iloc[0].to_dict() if not atm_rows.empty else {}

    # 25-delta RR: find CE strike with delta nearest 0.25 and PE with |delta| nearest 0.25
    ce_candidates = df[(df["ce_delta"] > 0) & (df["ce_iv"] > 0)].copy()
    pe_candidates = df[(df["pe_delta"] < 0) & (df["pe_iv"] > 0)].copy()

    rr_25d = None
    ce_25d_iv = pe_25d_iv = None
    ce_25d_strike = pe_25d_strike = None

    if not ce_candidates.empty and not pe_candidates.empty:
        ce_candidates["d_dist"] = (ce_candidates["ce_delta"] - 0.25).abs()
        pe_candidates["d_dist"] = (pe_candidates["pe_delta"].abs() - 0.25).abs()
        ce_25 = ce_candidates.loc[ce_candidates["d_dist"].idxmin()]
        pe_25 = pe_candidates.loc[pe_candidates["d_dist"].idxmin()]
        ce_25d_iv     = float(ce_25["ce_iv"])
        pe_25d_iv     = float(pe_25["pe_iv"])
        ce_25d_strike = float(ce_25["strike_price"])
        pe_25d_strike = float(pe_25["strike_price"])
        rr_25d        = round(pe_25d_iv - ce_25d_iv, 2)

    # OTM wing slopes (from smile spline — simple linear fit each side)
    atm_strike = float(atm_row.get("strike_price", spot))
    left  = df[(df["strike_price"] < atm_strike) & (df["pe_iv"] > 1)].copy()
    right = df[(df["strike_price"] > atm_strike) & (df["ce_iv"] > 1)].copy()

    left_slope = right_slope = skew_asymmetry = None
    if len(left) >= 3:
        c = np.polyfit(left["strike_price"].values, left["pe_iv"].values, 1)
        left_slope = round(float(c[0]) * 100, 4)   # IV change per 1% of spot
    if len(right) >= 3:
        c = np.polyfit(right["strike_price"].values, right["ce_iv"].values, 1)
        right_slope = round(float(c[0]) * 100, 4)
    if left_slope is not None and right_slope is not None:
        skew_asymmetry = round(abs(left_slope) - abs(right_slope), 4)

    # riskreversal time series for this symbol+expiry (ATM row only)
    # Risk reversal series: limit to the same date as the selected timestamp
    # to avoid pulling in years of history and compressing the x-axis.
    ts_date = ts_filter[:10]   # "YYYY-MM-DD"
    rr_ts_df = qdf(
        f"""
        SELECT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS ts,
               COALESCE(riskreversal, 0)             AS riskreversal,
               CAST(sentiment AS VARCHAR)             AS sentiment,
               CAST(regime    AS VARCHAR)             AS regime
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND distance_from_atm = 0
          AND CAST(timestamp AS DATE) = CAST(? AS DATE)
        ORDER BY timestamp
        """,
        [symbol, expiry[:10], ts_date],
    )

    return safe_response({
        "symbol":         symbol,
        "expiry":         expiry[:10],
        "spot":           spot,
        "atm_strike":     atm_strike,
        # 25-delta RR
        "rr_25d":         rr_25d,
        "ce_25d_iv":      ce_25d_iv,
        "pe_25d_iv":      pe_25d_iv,
        "ce_25d_strike":  ce_25d_strike,
        "pe_25d_strike":  pe_25d_strike,
        # Wing slopes
        "left_wing_slope":  left_slope,
        "right_wing_slope": right_slope,
        "skew_asymmetry":   skew_asymmetry,
        # ATM row signals from DB
        "riskreversal": _safe(atm_row.get("riskreversal")),
        "sentiment":    atm_row.get("sentiment", ""),
        "regime":       atm_row.get("regime", ""),
        # Time series
        "rr_series": to_records(rr_ts_df),
    })


# ===========================================================================
# Delta-weighted OI (Net MM Delta Exposure)
# ===========================================================================

@app.get("/api/delta_oi")
def delta_oi(
    symbol:    str           = Query(...),
    expiry:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    Delta-weighted OI = SUM(delta × OI × lot) across all strikes.
    Represents the net delta position carried by market makers (they are on the
    opposite side of retail/hedger flow). A strongly positive value means MMs
    are net short delta — they buy underlying on rallies (amplifying moves).
    Returns per-strike breakdown + aggregate per expiry + grand total.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)

    df = qdf(
        f"""
        SELECT
            CAST(expiry AS VARCHAR)       AS expiry,
            strike_price,
            COALESCE(ce_delta, 0)         AS ce_delta,
            COALESCE(pe_delta, 0)         AS pe_delta,
            COALESCE(ce_oi,    0)         AS ce_oi,
            COALESCE(pe_oi,    0)         AS pe_oi,
            COALESCE(lotsize,  1)         AS lotsize,
            COALESCE(underlying_price, 0) AS spot,
            COALESCE(ce_vanna, 0)         AS ce_vanna,
            COALESCE(pe_vanna, 0)         AS pe_vanna,
            COALESCE(net_vanna_ex, 0)     AS net_vanna_ex,
            COALESCE(net_charm_ex, 0)     AS net_charm_ex,
            COALESCE(net_flow,  0)        AS net_flow
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ?
          AND timestamp BETWEEN ? AND ?
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], lo, hi],
    )
    if df.empty:
        raise HTTPException(404, "No delta OI data")

    lot  = max(int(df["lotsize"].iloc[0]), 1)
    spot = float(df["spot"].iloc[0])

    # Per-strike delta exposure (MM is short what retail is long)
    df["ce_delta_oi"]  = df["ce_delta"]        * df["ce_oi"] * lot
    df["pe_delta_oi"]  = df["pe_delta"].abs()  * df["pe_oi"] * lot
    df["net_delta_oi"] = df["ce_delta_oi"] - df["pe_delta_oi"]  # net MM delta

    # Scale to millions
    for c in ["ce_delta_oi", "pe_delta_oi", "net_delta_oi"]:
        df[c] = df[c] / 1e6

    net_total   = float(df["net_delta_oi"].sum())
    ce_total    = float(df["ce_delta_oi"].sum())
    pe_total    = float(df["pe_delta_oi"].sum())
    net_flow    = float(df["net_flow"].sum())
    net_vanna   = float(df["net_vanna_ex"].sum())
    net_charm   = float(df["net_charm_ex"].sum())

    return safe_response({
        "symbol":        symbol,
        "expiry":        expiry[:10],
        "spot":          spot,
        "net_delta_oi":  round(net_total, 4),
        "ce_delta_oi":   round(ce_total,  4),
        "pe_delta_oi":   round(pe_total,  4),
        "net_flow":      round(net_flow,  2),
        "net_vanna_ex":  round(net_vanna, 4),
        "net_charm_ex":  round(net_charm, 4),
        "interpretation": (
            "MMs net SHORT delta — buying pressure on rallies (trend amplifier)"
            if net_total < -0.5 else
            "MMs net LONG delta — selling pressure on rallies (mean-reverting)"
            if net_total > 0.5 else
            "MMs near delta-neutral — balanced book"
        ),
        "rows": to_records(df[[
            "strike_price", "ce_delta", "pe_delta",
            "ce_oi", "pe_oi", "ce_delta_oi", "pe_delta_oi", "net_delta_oi",
        ]]),
    })


# ===========================================================================
# OI-weighted IV (for overview enrichment)
# ===========================================================================

@app.get("/api/oi_weighted_iv")
def oi_weighted_iv(
    symbol:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    """
    OI-weighted average IV per expiry + overall.
    More accurate measure of the market's effective implied vol than simple average.
    """
    ts_filter = timestamp or latest_ts(symbol)
    lo, hi    = _ts_lo_hi(ts_filter)
    df = qdf(
        f"""
        SELECT
            CAST(expiry AS VARCHAR)           AS expiry,
            COALESCE(days_to_expiry, 0)       AS dte,
            SUM(COALESCE(ce_iv,0) * COALESCE(ce_oi,0)) AS ce_iv_x_oi,
            SUM(COALESCE(pe_iv,0) * COALESCE(pe_oi,0)) AS pe_iv_x_oi,
            SUM(COALESCE(ce_oi,0))            AS total_ce_oi,
            SUM(COALESCE(pe_oi,0))            AS total_pe_oi
        FROM {tbl()}
        WHERE symbol = ? AND timestamp BETWEEN ? AND ?
          AND COALESCE(ce_iv,0) > 0
        GROUP BY expiry, days_to_expiry
        ORDER BY expiry
        """,
        [symbol, lo, hi],
    )
    if df.empty:
        raise HTTPException(404, "No IV data")

    df["oi_wtd_ce_iv"] = df.apply(
        lambda r: r["ce_iv_x_oi"] / r["total_ce_oi"] if r["total_ce_oi"] > 0 else None, axis=1)
    df["oi_wtd_pe_iv"] = df.apply(
        lambda r: r["pe_iv_x_oi"] / r["total_pe_oi"] if r["total_pe_oi"] > 0 else None, axis=1)
    df["oi_wtd_avg_iv"] = df.apply(
        lambda r: (r["ce_iv_x_oi"] + r["pe_iv_x_oi"]) / (r["total_ce_oi"] + r["total_pe_oi"])
        if (r["total_ce_oi"] + r["total_pe_oi"]) > 0 else None, axis=1)

    total_ce_x = float(df["ce_iv_x_oi"].sum())
    total_pe_x = float(df["pe_iv_x_oi"].sum())
    total_ce   = float(df["total_ce_oi"].sum())
    total_pe   = float(df["total_pe_oi"].sum())
    overall    = (total_ce_x + total_pe_x) / (total_ce + total_pe) if (total_ce + total_pe) > 0 else None

    return safe_response({
        "symbol":         symbol,
        "overall_oi_iv":  round(overall, 2) if overall else None,
        "by_expiry":      to_records(df[["expiry","dte","oi_wtd_ce_iv","oi_wtd_pe_iv","oi_wtd_avg_iv"]]),
    })


# ===========================================================================
# Cache management endpoints
# ===========================================================================

@app.post("/api/cache/refresh")
def cache_refresh():
    """
    Clear the in-memory DB cache (symbols, expiries, timestamps, lot_sizes, overview).
    Next requests will re-query the DB and repopulate. Use after a new OC import.
    Does NOT clear the margin cache — use /api/icici/margin/refresh for that.
    """
    cache_clear_all()
    return {"status": "ok", "message": "DB cache cleared — next requests will refresh from DB"}


@app.get("/api/cache/status")
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


# ===========================================================================
# ICICI
# ===========================================================================

@app.post("/api/icici/configure")
def icici_configure(body: Dict[str, str]):
    token = (body.get("session_token") or "").strip()
    if not token:
        raise HTTPException(400, "session_token is required")
    if _icici is not None:
        try:
            _icici.refresh_session(token)
            return {"status": "refreshed"}
        except Exception:
            pass
    ok = _try_init_icici(session_token=token)
    if ok:
        return {"status": "connected"}
    raise HTTPException(500, f"ICICI init failed: {_icici_status}")


@app.get("/api/icici/status")
def icici_status():
    return {
        "status":          _icici_status,
        "configured":      _icici is not None,
        "env_key":         bool(os.environ.get("IC_API_KEY")),
        "ticker_map_size": len(_ICICI_TICKER_MAP),
    }


@app.get("/api/icici/margin")
def icici_margin(
    symbol:      str   = Query(...),
    strike:      str   = Query(...),
    expiry:      str   = Query(...),
    option_type: str   = Query(...),
    ltp:         float = Query(...),
    qty:         int   = Query(...),
    action:      str   = Query("sell"),
    force:       bool  = Query(False),   # force=true bypasses cache
):
    """
    Fetch margin for one option leg.
    Results are cached by (symbol, strike, expiry, option_type) for the trading day.
    Cached value is returned instantly on repeat calls — no Breeze API hit.
    Use force=true to bypass cache and re-fetch from Breeze.
    """
    if _icici is None:
        raise HTTPException(503, "ICICI not configured")
    try:
        exp = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"Bad expiry '{expiry}'")

    # Check cache first (skip if force=true)
    if not force:
        cached = _margin_cache_get(symbol, strike, expiry, option_type)
        if cached is not None:
            return {**cached, "cached": True}

    ic_symbol = _to_icici_ticker(symbol)
    result = _icici.get_margin_for_option(
        symbol=ic_symbol, strike=strike, expiry=exp,
        option_type=option_type, ltp=ltp, qty=qty, action=action,
    )
    if result is None:
        raise HTTPException(502, "Margin API call failed")

    # Store in memory cache (no disk write — batch endpoint and shutdown handle persistence)
    cache_result = {k: v for k, v in result.items() if k != "raw"}
    _margin_cache_put(symbol, strike, expiry, option_type, cache_result, save=False)
    return {**cache_result, "cached": False}


@app.post("/api/icici/margin/batch")
async def icici_margin_batch(body: Dict):
    """
    Fetch margin for a list of option rows server-side with rate-limiting.
    Accepts: {"rows": [{"symbol","strike","expiry","option_type","ltp","qty"}, ...]}
    Returns: Server-Sent Events stream with progress updates.

    Each event is JSON:
      {"type":"progress","done":N,"total":M,"symbol":...,"cached":bool}
      {"type":"result","idx":N,"margin":float,"span_margin":float,"cached":bool}
      {"type":"done","total":M,"from_cache":K,"fetched":J}
      {"type":"error","idx":N,"message":"..."}

    Rate limit: 750 ms between live API calls (~80 calls/min, under Breeze 100/min limit).
    Cache hits are returned immediately with no delay.
    """
    if _icici is None:
        raise HTTPException(503, "ICICI not configured")

    rows = body.get("rows", [])
    if not rows:
        raise HTTPException(400, "rows list is empty")

    DELAY_MS = 750   # ms between live Breeze API calls

    async def event_stream():
        total      = len(rows)
        from_cache = 0
        fetched    = 0

        def ev(d: dict) -> str:
            return "data: " + json.dumps(d) + "\n\n"

        for idx, row in enumerate(rows):
            symbol      = str(row.get("symbol", ""))
            strike      = str(row.get("strike", ""))
            expiry      = str(row.get("expiry", ""))[:10]
            option_type = str(row.get("option_type", "")).lower()
            ltp         = float(row.get("ltp", 0))
            qty         = int(row.get("qty", 1))

            if not symbol or not strike or not expiry or ltp <= 0:
                yield ev({"type": "error", "idx": idx, "message": "invalid row"})
                continue

            # Progress heartbeat (non-blocking, sent before each row)
            yield ev({"type": "progress", "done": idx, "total": total,
                      "symbol": symbol, "strike": strike})

            # Cache hit — return instantly, no delay
            hit = _margin_cache_get(symbol, strike, expiry, option_type)
            if hit is not None:
                from_cache += 1
                payload = {k: v for k, v in hit.items() if k != "raw"}
                payload.update({"type": "result", "idx": idx, "cached": True})
                yield ev(payload)
                continue

            # Live fetch — throttle between calls
            try:
                await asyncio.sleep(DELAY_MS / 1000.0)
                exp_date  = datetime.strptime(expiry, "%Y-%m-%d").date()
                ic_symbol = _to_icici_ticker(symbol)

                # Run blocking Breeze call in thread pool so event loop stays free
                loop   = asyncio.get_event_loop()
                result = await loop.run_in_executor(
                    None,
                    lambda s=ic_symbol, st=strike, e=exp_date, ot=option_type, l=ltp, q=qty:
                        _icici.get_margin_for_option(
                            symbol=s, strike=st, expiry=e,
                            option_type=ot, ltp=l, qty=q, action="sell",
                        )
                )
                if result:
                    cache_result = {k: v for k, v in result.items() if k != "raw"}
                    # save=False — single save after full batch
                    _margin_cache_put(symbol, strike, expiry, option_type,
                                      cache_result, save=False)
                    fetched += 1
                    payload = dict(cache_result)
                    payload.update({"type": "result", "idx": idx, "cached": False})
                    yield ev(payload)
                else:
                    yield ev({"type": "error", "idx": idx, "message": "API returned None"})
            except Exception as exc:
                yield ev({"type": "error", "idx": idx, "message": str(exc)})

        # One disk write for the whole batch
        threading.Thread(target=lambda: _margin_cache_save(verbose=True),
                         daemon=True).start()
        yield ev({"type": "done", "total": total,
                  "from_cache": from_cache, "fetched": fetched})

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/icici/margin/refresh")
def icici_margin_refresh():
    """
    Clear the margin cache (both in-memory and the .pkl file on disk).
    Use when you want to re-fetch fresh margin values from ICICI Breeze,
    e.g. after a significant market move or at the start of a new trading day.
    """
    count = _margin_cache_clear()
    return {
        "status": "ok",
        "cleared": count,
        "message": f"Margin cache cleared ({count} entries). Next margin requests will re-fetch from Breeze.",
    }


# ===========================================================================
# Serve SPA
# ===========================================================================

_STATIC_DIR = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
def serve_index():
    html = _STATIC_DIR / "index.html"
    if html.exists():
        return html.read_text(encoding="utf-8")
    return "<h1>Static files missing</h1>"


if _STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")


# ===========================================================================
# CLI
# ===========================================================================

def main() -> None:
    global _DB_FILE, _DB_TABLE
    parser = argparse.ArgumentParser(description="NSE OC Analytics Dashboard")
    parser.add_argument("--db",    default="oc.duckdb")
    parser.add_argument("--table", default="ocdata")
    parser.add_argument("--host",  default="127.0.0.1")
    parser.add_argument("--port",  type=int, default=8000)
    args = parser.parse_args()
    _DB_FILE  = args.db
    _DB_TABLE = args.table
    _load_icici_ticker_map()   # reload with correct DB path now known
    if not Path(_DB_FILE).exists():
        print(f"✗ DB file not found: {_DB_FILE}")
        sys.exit(1)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
