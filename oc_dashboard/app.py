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
    lo = (base - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
    hi = (base + timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
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

def day_latest_ts(symbol: str, ftimestamp:str) -> str:
    today = date.today().isoformat()
    row = _qraw(f"SELECT MAX(timestamp) FROM {tbl()} WHERE symbol = ? and timestamp > ?" , [symbol, ftimestamp])
    if not row or row[0] is None:
        raise HTTPException(404, f"No data for {symbol}")
    return str(row[0])
def day_oldest_ts(symbol: str, ftimestamp:str) -> str:
    today = date.today().isoformat()
    row = _qraw(f"SELECT MIN(timestamp) FROM {tbl()} WHERE symbol = ? and timestamp > ?", [symbol, ftimestamp])
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
    _init_nse_fetcher()              # warm up shared NSEFetcher session
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
            f"WHERE symbol = ? AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)",
            [symbol, symbol],
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
def oi_change(
    symbol:        str   = Query(...),
    filter_type:   str   = Query("all"),
    min_oi_change: int   = Query(0),     # hide rows where |ce+pe oi chg| < this
):
    # Use the DB's ce_oi_change column (NSE's own computed delta vs their last
    # reference) rather than subtracting consecutive snapshots — gives meaningful
    # intraday context rather than tiny 4-minute movements.
    df = qdf(
        f"""
        SELECT
            STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS timestamp,
            strike_price,
            CAST(expiry AS VARCHAR)         AS expiry,
            COALESCE(ce_oi,        0)       AS ce_oi,
            COALESCE(pe_oi,        0)       AS pe_oi,
            COALESCE(ce_oi_change, 0)       AS ce_oi_chg,
            COALESCE(pe_oi_change, 0)       AS pe_oi_chg,
            COALESCE(ce_ltp,       0)       AS ce_ltp,
            COALESCE(pe_ltp,       0)       AS pe_ltp,
            COALESCE(ce_volume,    0)       AS ce_volume,
            COALESCE(pe_volume,    0)       AS pe_volume,
            COALESCE(ce_delta,     0)       AS ce_delta,
            COALESCE(underlying_price, 0)   AS spot
        FROM {tbl()}
        WHERE symbol = ?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol = ?)
          AND expiry >= CURRENT_DATE
          AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
        ORDER BY expiry, strike_price
        """,
        [symbol, symbol],
    )
    if df.empty:
        raise HTTPException(404, f"No OI data for {symbol}")

    # Compute LTP signal direction from ce_oi_change sign convention:
    # positive OI change + positive LTP = longs entering
    def _sig(oi, ltp):
        if oi > 0 and ltp > 0: return "Long Build-Up"
        if oi > 0 and ltp < 0: return "Short Build-Up"
        if oi < 0 and ltp > 0: return "Short Covering"
        if oi < 0 and ltp < 0: return "Long Unwinding"
        return "Neutral"

    df["ce_signal"] = df.apply(lambda r: _sig(r["ce_oi_chg"], r["ce_ltp"]), axis=1)
    df["pe_signal"] = df.apply(lambda r: _sig(r["pe_oi_chg"], r["pe_ltp"]), axis=1)

    # Add Vol/OI ratio — high ratio = fresh positioning
    df["ce_vol_oi"] = df.apply(
        lambda r: round(r["ce_volume"] / r["ce_oi"], 3) if r["ce_oi"] > 0 else None, axis=1)
    df["pe_vol_oi"] = df.apply(
        lambda r: round(r["pe_volume"] / r["pe_oi"], 3) if r["pe_oi"] > 0 else None, axis=1)

    # Filter: hide rows where both OI changes are below threshold (pure noise)
    abs_min = max(min_oi_change, 1)  # always filter zero-zero rows
    df = df[
        (df["ce_oi_chg"].abs() >= abs_min) |
        (df["pe_oi_chg"].abs() >= abs_min)
    ]

    # Sort by total absolute OI activity descending
    df["total_abs_oi"] = df["ce_oi_chg"].abs() + df["pe_oi_chg"].abs()
    df = df.sort_values("total_abs_oi", ascending=False).drop(columns=["total_abs_oi"])

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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
          AND (COALESCE(ce_iv,0)>0 OR COALESCE(pe_iv,0)>0)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
    "ce_ltp","pe_ltp","ce_iv","pe_iv","ce_iv_nse","ce_iv_vol",
    "ce_volume","pe_volume","ce_oi","pe_oi","ce_gexv","pe_gexv","net_gexv",
    "ce_delta","pe_delta","ce_gamma","pe_gamma",
    "ce_theta","pe_theta","ce_vega","pe_vega",
    "ce_vanna","pe_vanna","ce_charm","pe_charm",
    "ce_TPrice","pe_TPrice","ce_intrinsic_value","pe_intrinsic_value",
    "ce_time_value","pe_time_value","ce_bid_ask_spread","pe_bid_ask_spread",
    "riskreversal","net_flow","net_vanna_ex","net_charm_ex",
    "m_volatility","fut_price","underlying_price",
    "ce_tbq","ce_bq","pe_tbq","pe_bq","ce_pchange","pe_pchange",
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
        ts_where  = f"AND {ts_clause}"
        pre_cte   = ""
    else:
        # Use a CTE to get latest timestamp per symbol — more reliable than
        # correlated subquery which some DuckDB versions handle inconsistently
        pre_cte  = f"""latest_ts AS (
            SELECT symbol, MAX(timestamp) AS max_ts
            FROM {{tbl()}} GROUP BY symbol
        ),"""
        ts_where  = "AND timestamp = (SELECT max_ts FROM latest_ts WHERE latest_ts.symbol = t.symbol)"
        ts_params = []

    df = qdf(
        f"""
        {"WITH " + pre_cte if pre_cte else ""}
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
        WHERE 1=1 {sym_filter} {ts_where}
          AND expiry >= CURRENT_DATE
          AND (COALESCE(ce_ltp, 0) + COALESCE(pe_ltp, 0)) > 0
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)
          AND distance_from_atm = 0
        ORDER BY expiry
        """,
        [symbol, symbol],
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
                AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)
            )
            SELECT * FROM ranked WHERE rn = 1 ORDER BY expiry
            """,
            [symbol, symbol],
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
          AND distance_from_atm = 0
        LIMIT 1
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
          AND timestamp  = ?
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
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND expiry=?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10]],
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
        WHERE symbol = ? AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=?)
          AND COALESCE(ce_iv,0) > 0
        GROUP BY expiry, days_to_expiry
        ORDER BY expiry
        """,
        [symbol, symbol],
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
# Strike snapshot (full row for one strike at one timestamp) — Item 3
# ===========================================================================

@app.get("/api/strike_snapshot")
def strike_snapshot(
    symbol:       str           = Query(...),
    strike_price: float         = Query(...),
    expiry:       str           = Query(...),
    timestamp:    Optional[str] = Query(None),
):
    """
    Full single-row snapshot for one strike at a given timestamp.
    Returns all greeks, theoretical price, IV, moneyness, etc.
    Used by the enhanced Strike Trend / Option Lens panel.
    """
    ts_filter = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts_filter)
    df = qdf(
        f"""
        SELECT
            strike_price, CAST(expiry AS VARCHAR) AS expiry,
            COALESCE(underlying_price,0)  AS spot,
            COALESCE(fut_price,0)         AS fut_price,
            COALESCE(days_to_expiry,0)    AS dte,
            COALESCE(distance_from_atm,0) AS distance_from_atm,
            -- CE
            COALESCE(ce_ltp,0)            AS ce_ltp,
            COALESCE(ce_TPrice,0)         AS ce_tprice,
            COALESCE(ce_ltp_s,'NA')       AS ce_ltp_s,
            COALESCE(ce_iv,0)             AS ce_iv,
            COALESCE(ce_delta,0)          AS ce_delta,
            COALESCE(ce_gamma,0)          AS ce_gamma,
            COALESCE(ce_theta,0)          AS ce_theta,
            COALESCE(ce_vega,0)           AS ce_vega,
            COALESCE(ce_vanna,0)          AS ce_vanna,
            COALESCE(ce_charm,0)          AS ce_charm,
            COALESCE(ce_oi,0)             AS ce_oi,
            COALESCE(ce_volume,0)         AS ce_volume,
            COALESCE(ce_intrinsic_value,0) AS ce_intrinsic,
            COALESCE(ce_time_value,0)     AS ce_time_value,
            COALESCE(ce_bid,0)            AS ce_bid,
            COALESCE(ce_ask,0)            AS ce_ask,
            COALESCE(ce_bid_ask_spread,0) AS ce_spread,
            COALESCE(ce_moneyness,'NA')   AS ce_moneyness,
            COALESCE(ce_gexv,0)           AS ce_gexv,
            -- PE
            COALESCE(pe_ltp,0)            AS pe_ltp,
            COALESCE(pe_TPrice,0)         AS pe_tprice,
            COALESCE(pe_ltp_s,'NA')       AS pe_ltp_s,
            COALESCE(pe_iv,0)             AS pe_iv,
            COALESCE(pe_delta,0)          AS pe_delta,
            COALESCE(pe_gamma,0)          AS pe_gamma,
            COALESCE(pe_theta,0)          AS pe_theta,
            COALESCE(pe_vega,0)           AS pe_vega,
            COALESCE(pe_vanna,0)          AS pe_vanna,
            COALESCE(pe_charm,0)          AS pe_charm,
            COALESCE(pe_oi,0)             AS pe_oi,
            COALESCE(pe_volume,0)         AS pe_volume,
            COALESCE(pe_intrinsic_value,0) AS pe_intrinsic,
            COALESCE(pe_time_value,0)     AS pe_time_value,
            COALESCE(pe_bid,0)            AS pe_bid,
            COALESCE(pe_ask,0)            AS pe_ask,
            COALESCE(pe_bid_ask_spread,0) AS pe_spread,
            COALESCE(pe_moneyness,'NA')   AS pe_moneyness,
            COALESCE(pe_gexv,0)           AS pe_gexv,
            -- Composite
            COALESCE(riskreversal,0)      AS riskreversal,
            CAST(sentiment AS VARCHAR)    AS sentiment,
            CAST(regime    AS VARCHAR)    AS regime,
            COALESCE(net_flow,0)          AS net_flow,
            COALESCE(m_volatility,0)      AS rv
        FROM {tbl()}
        WHERE symbol=? AND strike_price=? AND expiry=?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol=? AND strike_price=? AND expiry=?)
        LIMIT 1
        """,
        [symbol, strike_price, expiry[:10], symbol, strike_price, expiry[:10]],
    )
    if df.empty:
        raise HTTPException(404, "No snapshot data")
    row = df.iloc[0].to_dict()
    # Add derived fields
    ce_ltp  = float(row.get("ce_ltp", 0))
    ce_tp   = float(row.get("ce_tprice", 0))
    pe_ltp  = float(row.get("pe_ltp", 0))
    pe_tp   = float(row.get("pe_tprice", 0))
    row["ce_price_ratio"] = round(ce_ltp / ce_tp, 4) if ce_tp > 0 else None
    row["pe_price_ratio"] = round(pe_ltp / pe_tp, 4) if pe_tp > 0 else None
    return safe_response(row)


# Extend strike_trend to support multiple metrics (up to 3)
@app.get("/api/strike_trend_multi")
def strike_trend_multi(
    symbol:       str   = Query(...),
    strike_price: float = Query(...),
    expiry:       str   = Query(...),
    m1:           str   = Query("ce_ltp"),
    m2:           Optional[str] = Query(None),
    m3:           Optional[str] = Query(None),
):
    """Return time-series for up to 3 metrics for one strike+expiry."""
    metrics = [m for m in [m1, m2, m3] if m and m in _SAFE_METRICS]
    if not metrics:
        raise HTTPException(400, "No valid metrics specified")
    sel = ", ".join(f"COALESCE({m},0) AS {m}" for m in metrics)
    df = qdf(
        f"""
        SELECT STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS timestamp, {sel}
        FROM {tbl()}
        WHERE symbol=? AND strike_price=? AND expiry=?
        ORDER BY timestamp
        """,
        [symbol, strike_price, expiry[:10]],
    )
    return safe_response({"metrics": metrics, "rows": to_records(df)})


# ===========================================================================
# VIX — Item 4
# ===========================================================================

@app.get("/api/vix")
def vix_data(lookback_days: int = Query(30)):
    """
    Current India VIX (from NSEFetcher) + historical for the lookback window.
    Falls back gracefully if NSEFetcher is unavailable.
    """
    try:
        fetcher = _get_fetcher()
        vix_now, vix_prev = fetcher.get_vix_current()
        # Historical
        end_date   = date.today()
        start_date = end_date - timedelta(days=lookback_days)
        hist_df    = fetcher.get_vix_historical(start_date, end_date)
        history    = []
        if hist_df is not None and not hist_df.empty:
            for _, row in hist_df.iterrows():
                ts = row.get("EOD_TIMESTAMP")
                ts_str = ts.strftime("%Y-%m-%d") if hasattr(ts, "strftime") else str(ts)
                history.append({
                    "date":  ts_str,
                    "open":  _safe(row.get("EOD_OPEN_INDEX_VAL")),
                    "high":  _safe(row.get("EOD_HIGH_INDEX_VAL")),
                    "low":   _safe(row.get("EOD_LOW_INDEX_VAL")),
                    "close": _safe(row.get("EOD_CLOSE_INDEX_VAL")),
                    "prev":  _safe(row.get("EOD_PREV_CLOSE")),
                })
        chg     = round(vix_now - vix_prev, 2) if vix_prev else None
        chg_pct = round((vix_now - vix_prev) / vix_prev * 100, 2) if vix_prev else None
        level   = ("extreme" if vix_now > 30 else
                   "elevated" if vix_now > 20 else
                   "normal"   if vix_now > 12 else "low")
        return safe_response({
            "vix":        round(vix_now, 2),
            "vix_prev":   round(vix_prev, 2),
            "chg":        chg,
            "chg_pct":    chg_pct,
            "level":      level,
            "history":    history,
            "source":     "NSEFetcher",
        })
    except ModuleNotFoundError:
        raise HTTPException(503, "NSEFetcher not available")
    except Exception as exc:
        raise HTTPException(502, f"VIX fetch failed: {exc}")


# ===========================================================================
# OI Walls — Item 6
# ===========================================================================

@app.get("/api/oi_walls")
def oi_walls(filter_type: str = Query("all")):
    """
    For each symbol at latest timestamp, find:
    - Call wall  = strike with highest CE OI (resistance)
    - Put wall   = strike with highest PE OI (support)
    - Distance from spot and fut_price
    - Wall strength = max_oi / avg_oi
    - PCR at wall strikes
    Sortable across all symbols.
    """
    idx_list = ", ".join(f"'{s}'" for s in _NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    df = qdf(
        f"""
        WITH latest AS (
            SELECT symbol, MAX(timestamp) AS ts
            FROM {tbl()} GROUP BY symbol
        ),
        base AS (
            SELECT t.symbol,
                   t.strike_price,
                   COALESCE(t.ce_oi,0)           AS ce_oi,
                   COALESCE(t.pe_oi,0)           AS pe_oi,
                   COALESCE(t.ce_ltp,0)          AS ce_ltp,
                   COALESCE(t.pe_ltp,0)          AS pe_ltp,
                   COALESCE(t.ce_iv, 0)          AS ce_iv,
                   COALESCE(t.pe_iv, 0)          AS pe_iv,
                   COALESCE(t.ce_oi_change, 0)   AS ce_oi_change,
                   COALESCE(t.pe_oi_change, 0)   AS pe_oi_change,
                   COALESCE(t.underlying_price,0) AS spot,
                   COALESCE(t.fut_price,0)        AS fut_price,
                   COALESCE(t.atm_strike,0)       AS atm_strike,
                   COALESCE(t.lotsize,1)          AS lotsize
            FROM {tbl()} t
            JOIN latest l ON t.symbol=l.symbol AND t.timestamp=l.ts
            WHERE t.ce_oi > 0 OR t.pe_oi > 0
            {sym_filter}
        ),
        agg AS (
            SELECT symbol,
                   MAX(spot)      AS spot,
                   MAX(fut_price) AS fut_price,
                   MAX(atm_strike) AS atm_strike,
                   AVG(ce_oi)     AS avg_ce_oi,
                   AVG(pe_oi)     AS avg_pe_oi,
                   MAX(ce_oi)     AS max_ce_oi,
                   MAX(pe_oi)     AS max_pe_oi,
                   SUM(ce_oi)     AS total_ce_oi,
                   SUM(pe_oi)     AS total_pe_oi
            FROM base GROUP BY symbol
        ),
        ce_wall AS (
            SELECT b.symbol,
                   b.strike_price    AS ce_wall_strike,
                   b.ce_oi           AS ce_wall_oi,
                   COALESCE(b.ce_ltp, 0)             AS ce_ltp,
                   COALESCE(b.ce_oi_change, 0)        AS ce_oi_chg,
                   COALESCE(b.ce_iv,  0)              AS ce_iv,
                   -- LTP and IV changes vs previous snapshot (use DB columns)
                   COALESCE(b.ce_ltp, 0) - COALESCE(prev.ce_ltp, 0)  AS ce_ltp_chg,
                   COALESCE(b.ce_iv,  0) - COALESCE(prev.ce_iv,  0)  AS ce_iv_chg,
                   CASE WHEN COALESCE(b.ce_oi_change,0) > 0 AND COALESCE(b.ce_ltp,0) > 0 THEN 'Long Build-Up'
                        WHEN COALESCE(b.ce_oi_change,0) > 0 AND COALESCE(b.ce_ltp,0) < 0 THEN 'Short Build-Up'
                        WHEN COALESCE(b.ce_oi_change,0) < 0 AND COALESCE(b.ce_ltp,0) > 0 THEN 'Short Covering'
                        WHEN COALESCE(b.ce_oi_change,0) < 0 AND COALESCE(b.ce_ltp,0) < 0 THEN 'Long Unwinding'
                        ELSE 'Neutral' END            AS ce_signal
            FROM base b
            LEFT JOIN base prev ON prev.symbol = b.symbol
                AND prev.strike_price = b.strike_price
            WHERE (b.symbol, b.ce_oi) IN (SELECT symbol, MAX(ce_oi) FROM base GROUP BY symbol)
        ),
        pe_wall AS (
            SELECT b.symbol,
                   b.strike_price    AS pe_wall_strike,
                   b.pe_oi           AS pe_wall_oi,
                   COALESCE(b.pe_ltp, 0)             AS pe_ltp,
                   COALESCE(b.pe_oi_change, 0)        AS pe_oi_chg,
                   COALESCE(b.pe_iv,  0)              AS pe_iv,
                   COALESCE(b.pe_ltp, 0) - COALESCE(prev.pe_ltp, 0)  AS pe_ltp_chg,
                   COALESCE(b.pe_iv,  0) - COALESCE(prev.pe_iv,  0)  AS pe_iv_chg,
                   CASE WHEN COALESCE(b.pe_oi_change,0) > 0 AND COALESCE(b.pe_ltp,0) > 0 THEN 'Long Build-Up'
                        WHEN COALESCE(b.pe_oi_change,0) > 0 AND COALESCE(b.pe_ltp,0) < 0 THEN 'Short Build-Up'
                        WHEN COALESCE(b.pe_oi_change,0) < 0 AND COALESCE(b.pe_ltp,0) > 0 THEN 'Short Covering'
                        WHEN COALESCE(b.pe_oi_change,0) < 0 AND COALESCE(b.pe_ltp,0) < 0 THEN 'Long Unwinding'
                        ELSE 'Neutral' END            AS pe_signal
            FROM base b
            LEFT JOIN base prev ON prev.symbol = b.symbol
                AND prev.strike_price = b.strike_price
            WHERE (b.symbol, b.pe_oi) IN (SELECT symbol, MAX(pe_oi) FROM base GROUP BY symbol)
        )
        SELECT
            a.symbol,
            a.spot,
            a.fut_price,
            a.atm_strike,
            c.ce_wall_strike,
            c.ce_wall_oi,
            p.pe_wall_strike,
            p.pe_wall_oi,
            -- distances from spot
            c.ce_wall_strike - a.spot           AS ce_dist_spot,
            a.spot - p.pe_wall_strike           AS pe_dist_spot,
            -- distances from futures
            c.ce_wall_strike - a.fut_price      AS ce_dist_fut,
            a.fut_price - p.pe_wall_strike      AS pe_dist_fut,
            -- wall strength (how thick vs average)
            CASE WHEN a.avg_ce_oi > 0
                 THEN ROUND(a.max_ce_oi / a.avg_ce_oi, 1) ELSE NULL END AS ce_wall_strength,
            CASE WHEN a.avg_pe_oi > 0
                 THEN ROUND(a.max_pe_oi / a.avg_pe_oi, 1) ELSE NULL END AS pe_wall_strength,
            -- PCR
            CASE WHEN a.total_ce_oi > 0
                 THEN ROUND(a.total_pe_oi / a.total_ce_oi, 3) ELSE NULL END AS pcr,
            -- distance between walls
            c.ce_wall_strike - p.pe_wall_strike AS wall_range,
            -- CE wall enrichment: LTP, IV, OI change at the wall strike
            c.ce_ltp, c.ce_ltp_chg, c.ce_iv, c.ce_iv_chg, c.ce_oi_chg, c.ce_signal,
            -- PE wall enrichment
            p.pe_ltp, p.pe_ltp_chg, p.pe_iv, p.pe_iv_chg, p.pe_oi_chg, p.pe_signal
        FROM agg a
        JOIN ce_wall c ON a.symbol = c.symbol
        JOIN pe_wall p ON a.symbol = p.symbol
        ORDER BY a.symbol
        """
    )
    if df.empty:
        return []
    return safe_response(to_records(df))



# ===========================================================================
# Market Info — Item 11 (NSEFetcher: status, block deals, corp actions)
# ===========================================================================

# Shared NSEFetcher singleton — one instance per app lifetime, one session.
_nse_fetcher_instance: Optional[Any] = None


def _init_nse_fetcher() -> None:
    """Warm up the shared NSEFetcher at startup. Silent on failure."""
    global _nse_fetcher_instance
    try:
        _nse_root = Path(__file__).resolve().parent.parent.parent
        if str(_nse_root) not in sys.path:
            sys.path.insert(0, str(_nse_root))
        from nse_fetcher import NSEFetcher
        # Pass the full path to cookies file so it reads/writes next to nse_fetcher.py
        _cookies_path = str(_nse_root / "nse_cookies.json")
        _nse_fetcher_instance = NSEFetcher(cookies_file=_cookies_path)
        _ = _nse_fetcher_instance._get_session()   # warm up session now
        print("✓ NSEFetcher session ready")
    except Exception as exc:
        print(f"  ℹ NSEFetcher not available at startup: {exc}")
        _nse_fetcher_instance = None


def _get_fetcher():
    """Return the shared NSEFetcher singleton. Raises HTTPException if unavailable."""
    global _nse_fetcher_instance
    if _nse_fetcher_instance is not None:
        return _nse_fetcher_instance
    # Lazy init if not done at startup
    try:
        _nse_root = Path(__file__).resolve().parent.parent.parent
        if str(_nse_root) not in sys.path:
            sys.path.insert(0, str(_nse_root))
        from nse_fetcher import NSEFetcher
        _cookies_path = str(_nse_root / "nse_cookies.json")
        _nse_fetcher_instance = NSEFetcher(cookies_file=_cookies_path)
        return _nse_fetcher_instance
    except ModuleNotFoundError:
        raise HTTPException(503, "NSEFetcher not found — ensure nse_fetcher.py is at <root>/nse_fetcher.py")
    except Exception as exc:
        raise HTTPException(502, f"NSEFetcher init failed: {exc}")



@app.get("/api/market/status")
def market_status():
    """Live NSE market open/closed/pre-open status."""
    f = _get_fetcher()
    try:
        result = f.get_market_status()
        return safe_response(result)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/market/block_deals")
def market_block_deals():
    """Today's NSE block deals."""
    f = _get_fetcher()
    try:
        df = f.get_block_deals()
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/market/corp_actions")
def market_corp_actions(symbol: Optional[str] = Query(None)):
    """NSE corporate actions (ex-div, bonus, splits). Filter by symbol optionally."""
    f = _get_fetcher()
    try:
        df = f.get_corporate_actions(symbol=symbol)
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/market/announcements")
def market_announcements(symbol: Optional[str] = Query(None)):
    """Latest NSE corporate announcements. Filter by symbol optionally."""
    f = _get_fetcher()
    try:
        df = f.get_corporate_announcements(symbol=symbol)
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@app.get("/api/market/board_meetings")
def market_board_meetings(symbol: Optional[str] = Query(None)):
    """Upcoming NSE board meetings. Filter by symbol optionally."""
    f = _get_fetcher()
    try:
        df = f.get_board_meetings(symbol=symbol)
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


# ===========================================================================
# Item 5: Divergence Identifier
# ===========================================================================

@app.get("/api/divergence")
def divergence(
    mode:        str = Query("snapshot"),   # "snapshot" | "intraday"
    filter_type: str = Query("all"),
    top_n:       int = Query(30),
    min_spot_chg: float = Query(0.03),      # min |spot % change| to flag
    min_prem_chg: float = Query(0.5),       # min |premium % change| to flag
):
    """
    Identify divergences between spot/futures price moves and option premium moves.

    Mode 'snapshot': Compare two most recent timestamps per symbol.
    Mode 'intraday': Compare first timestamp of today vs latest per symbol.

    Flags:
      BULL_HEDGE   : Spot UP  + PE premium UP  (puts bought on rally — bearish hedge)
      BEAR_SQUEEZE : Spot DOWN + CE premium UP  (calls bought on dip — bear squeeze risk)
      IV_SPIKE     : Spot FLAT + IV spike       (event anticipation, not delta-driven)
      SMART_SELL   : Spot UP  + CE premium DOWN (calls being sold on rally — ceiling signal)
      SMART_BUY    : Spot DOWN + PE premium DOWN (puts being sold on dip — floor signal)
    """
    idx_list = ", ".join(f"'{s}'" for s in _NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    today = date.today().isoformat()

    if mode == "intraday":
        # First timestamp today vs latest timestamp today per symbol
        df_ref = qdf(
            f"""
            WITH day_bounds AS (
                SELECT symbol,
                       MIN(timestamp) AS ts_open,
                       MAX(timestamp) AS ts_now
                FROM {tbl()}
                WHERE timestamp = ?
                {sym_filter}
                GROUP BY symbol
                HAVING MIN(timestamp) != MAX(timestamp)
            )
            SELECT
                n.symbol,
                STRFTIME(n.timestamp, '%Y-%m-%d %H:%M') AS ts_now,
                STRFTIME(o.timestamp, '%Y-%m-%d %H:%M') AS ts_open,
                AVG(n.underlying_price) - AVG(o.underlying_price)     AS spot_chg,
                AVG(o.underlying_price)                                AS spot_open,
                AVG(n.underlying_price)                                AS spot_now,
                -- ATM CE/PE (distance_from_atm = 0)
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.ce_ltp ELSE NULL END) AS ce_ltp_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.ce_ltp ELSE NULL END) AS ce_ltp_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.pe_ltp ELSE NULL END) AS pe_ltp_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.pe_ltp ELSE NULL END) AS pe_ltp_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.ce_iv  ELSE NULL END) AS ce_iv_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.ce_iv  ELSE NULL END) AS ce_iv_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.pe_iv  ELSE NULL END) AS pe_iv_now,
                AVG(CASE WHEN o.distance_from_atm = 0 THEN o.pe_iv  ELSE NULL END) AS pe_iv_open,
                AVG(CASE WHEN n.distance_from_atm = 0 THEN n.net_flow ELSE NULL END) AS net_flow
            FROM {tbl()} n
            JOIN day_bounds d ON n.symbol = d.symbol AND n.timestamp = d.ts_now
            JOIN {tbl()} o   ON o.symbol = d.symbol AND o.timestamp = d.ts_open
            WHERE n.expiry >= CURRENT_DATE
            GROUP BY n.symbol, n.timestamp, o.timestamp
            """,
            [today],
        )
    else:
        # Compare two most recent timestamps per symbol.
        # Use a CTE to get exact ts_now and ts_prev per symbol first,
        # then join only those two rows — avoids a full cross-join.
        df_ref = qdf(
            f"""
            WITH sym_ts AS (
                SELECT symbol,
                       MAX(timestamp) AS ts_now,
                       MIN(timestamp) AS ts_prev
                FROM (
                    SELECT symbol, timestamp,
                           DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
                    FROM {tbl()}
                    WHERE expiry >= CURRENT_DATE {sym_filter}
                ) ranked
                WHERE rk <= 2
                GROUP BY symbol
                HAVING COUNT(DISTINCT timestamp) >= 2
            ),
            snap_now AS (
                SELECT t.symbol,
                       AVG(t.underlying_price) AS spot_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_ltp   END) AS ce_ltp_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_ltp   END) AS pe_ltp_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_iv    END) AS ce_iv_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_iv    END) AS pe_iv_now,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.net_flow END) AS net_flow,
                       STRFTIME(MAX(t.timestamp), '%Y-%m-%d %H:%M') AS ts_now_str
                FROM {tbl()} t
                JOIN sym_ts s ON t.symbol=s.symbol AND t.timestamp=s.ts_now
                GROUP BY t.symbol
            ),
            snap_prev AS (
                SELECT t.symbol,
                       AVG(t.underlying_price) AS spot_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_ltp END) AS ce_ltp_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_ltp END) AS pe_ltp_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.ce_iv  END) AS ce_iv_prev,
                       AVG(CASE WHEN t.distance_from_atm=0 THEN t.pe_iv  END) AS pe_iv_prev,
                       STRFTIME(MAX(t.timestamp), '%Y-%m-%d %H:%M') AS ts_prev_str
                FROM {tbl()} t
                JOIN sym_ts s ON t.symbol=s.symbol AND t.timestamp=s.ts_prev
                GROUP BY t.symbol
            )
            SELECT n.symbol,
                   n.ts_now_str  AS ts_now,
                   p.ts_prev_str AS ts_open,
                   n.spot_now - p.spot_prev  AS spot_chg,
                   p.spot_prev               AS spot_open,
                   n.spot_now                AS spot_now,
                   n.ce_ltp_now, p.ce_ltp_prev AS ce_ltp_open,
                   n.pe_ltp_now, p.pe_ltp_prev AS pe_ltp_open,
                   n.ce_iv_now,  p.ce_iv_prev  AS ce_iv_open,
                   n.pe_iv_now,  p.pe_iv_prev  AS pe_iv_open,
                   n.net_flow
            FROM snap_now n
            JOIN snap_prev p ON n.symbol = p.symbol
            """
        )

    if df_ref.empty:
        return []

    rows = []
    for _, r in df_ref.iterrows():
        spot_open = float(r.get("spot_open") or 0)
        spot_now  = float(r.get("spot_now")  or 0)
        if spot_open <= 0:
            continue
        spot_pct  = (spot_now - spot_open) / spot_open * 100

        ce_open = float(r.get("ce_ltp_open") or 0)
        ce_now  = float(r.get("ce_ltp_now")  or 0)
        pe_open = float(r.get("pe_ltp_open") or 0)
        pe_now  = float(r.get("pe_ltp_now")  or 0)

        ce_pct  = (ce_now - ce_open) / ce_open * 100 if ce_open > 0 else 0
        pe_pct  = (pe_now - pe_open) / pe_open * 100 if pe_open > 0 else 0

        ce_iv_chg = float((r.get("ce_iv_now") or 0)) - float((r.get("ce_iv_open") or 0))
        pe_iv_chg = float((r.get("pe_iv_now") or 0)) - float((r.get("pe_iv_open") or 0))

        # Classify divergence
        signal = None
        magnitude = 0.0
        spot_flat = abs(spot_pct) < min_spot_chg

        if not spot_flat:
            if spot_pct > min_spot_chg and pe_pct > min_prem_chg:
                signal = "BULL_HEDGE"
                magnitude = abs(spot_pct) + abs(pe_pct)
            elif spot_pct < -min_spot_chg and ce_pct > min_prem_chg:
                signal = "BEAR_SQUEEZE"
                magnitude = abs(spot_pct) + abs(ce_pct)
            elif spot_pct > min_spot_chg and ce_pct < -min_prem_chg:
                signal = "SMART_SELL"
                magnitude = abs(spot_pct) + abs(ce_pct)
            elif spot_pct < -min_spot_chg and pe_pct < -min_prem_chg:
                signal = "SMART_BUY"
                magnitude = abs(spot_pct) + abs(pe_pct)
        else:
            # Spot flat but IV spiked
            avg_iv_chg = (abs(ce_iv_chg) + abs(pe_iv_chg)) / 2
            if avg_iv_chg >= 1.0:  # at least 1% IV move
                signal = "IV_SPIKE"
                magnitude = avg_iv_chg

        if signal is None:
            continue

        rows.append({
            "symbol":     r["symbol"],
            "signal":     signal,
            "magnitude":  round(magnitude, 3),
            "spot_pct":   round(spot_pct,  3),
            "ce_pct":     round(ce_pct,    3),
            "pe_pct":     round(pe_pct,    3),
            "ce_iv_chg":  round(ce_iv_chg, 3),
            "pe_iv_chg":  round(pe_iv_chg, 3),
            "spot_now":   round(spot_now,  2),
            "ce_ltp_now": round(ce_now,    2),
            "pe_ltp_now": round(pe_now,    2),
            "net_flow":   round(float(r.get("net_flow") or 0), 2),
            "ts_now":     r.get("ts_now", ""),
            "ts_ref":     r.get("ts_open", ""),
        })

    rows.sort(key=lambda x: x["magnitude"], reverse=True)
    return safe_response(rows[:top_n])


# ===========================================================================
# Item 7: Intraday IV trend per symbol
# ===========================================================================

@app.get("/api/iv_intraday_trend")
def iv_intraday_trend(filter_type: str = Query("all")):
    """
    For each symbol: OI-weighted ATM IV at first timestamp today vs latest.
    Returns iv_open, iv_now, iv_chg, iv_chg_pct, direction indicator.
    """
    idx_list = ", ".join(f"'{s}'" for s in _NSE_INDICES)
    sym_filter = (
        f"AND symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    today = date.today().isoformat()
    df = qdf(
        f"""
        WITH day_bounds AS (
            SELECT symbol,
                   MIN(timestamp) AS ts_open,
                   MAX(timestamp) AS ts_now
            FROM {tbl()}
            WHERE timestamp  = ?
            {sym_filter}
            GROUP BY symbol
        ),
        iv_open AS (
            SELECT t.symbol,
                   SUM(COALESCE(t.ce_iv,0)*COALESCE(t.ce_oi,0)
                      +COALESCE(t.pe_iv,0)*COALESCE(t.pe_oi,0))
                   / NULLIF(SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0)),0) AS iv
            FROM {tbl()} t
            JOIN day_bounds d ON t.symbol=d.symbol AND t.timestamp=d.ts_open
            WHERE t.expiry >= CURRENT_DATE AND t.ce_iv > 0
            GROUP BY t.symbol
        ),
        iv_now AS (
            SELECT t.symbol,
                   SUM(COALESCE(t.ce_iv,0)*COALESCE(t.ce_oi,0)
                      +COALESCE(t.pe_iv,0)*COALESCE(t.pe_oi,0))
                   / NULLIF(SUM(COALESCE(t.ce_oi,0)+COALESCE(t.pe_oi,0)),0) AS iv
            FROM {tbl()} t
            JOIN day_bounds d ON t.symbol=d.symbol AND t.timestamp=d.ts_now
            WHERE t.expiry >= CURRENT_DATE AND t.ce_iv > 0
            GROUP BY t.symbol
        )
        SELECT
            o.symbol,
            ROUND(o.iv, 2) AS iv_open,
            ROUND(n.iv, 2) AS iv_now,
            ROUND(n.iv - o.iv, 2) AS iv_chg,
            CASE WHEN o.iv > 0
                 THEN ROUND((n.iv - o.iv) / o.iv * 100, 2)
                 ELSE NULL END AS iv_chg_pct
        FROM iv_open o
        JOIN iv_now  n ON o.symbol = n.symbol
        ORDER BY ABS(n.iv - o.iv) DESC
        """,
        [today],
    )
    return [] if df.empty else to_records(df)


# ===========================================================================
# Item 8 (Premium Lens) + Item 9b (EOD OI/IV history)
# ===========================================================================

@app.get("/api/premium_lens")
def premium_lens(
    symbol:        str           = Query(...),
    expiry:        str           = Query(...),
    timestamp:     Optional[str] = Query(None),
    min_ratio:     float         = Query(0.0),   # show only where ratio < min_ratio
    max_ratio:     float         = Query(999.0), # or > max_ratio (outside normal band)
    min_oi:        int           = Query(0),
):
    """
    Premium richness/cheapness screener.
    Uses ce_TPrice/pe_TPrice (Black-Scholes theoretical) vs actual LTP.
    ce_ltp_s/pe_ltp_s (Premium/Discount/NA) from DB is also shown.
    Filters to show only strikes outside the configured ratio range.
    """
    df = qdf(
        f"""
        SELECT
            strike_price,
            distance_from_atm,
            COALESCE(days_to_expiry,  0) AS dte,
            COALESCE(ce_ltp,     0) AS ce_ltp,
            COALESCE(ce_TPrice,  0) AS ce_tprice,
            COALESCE(ce_ltp_s, 'NA') AS ce_ltp_s,
            COALESCE(ce_iv,      0) AS ce_iv,
            COALESCE(ce_delta,   0) AS ce_delta,
            COALESCE(ce_oi,      0) AS ce_oi,
            COALESCE(ce_volume,  0) AS ce_volume,
            COALESCE(ce_bid_ask_spread, 0) AS ce_spread,
            COALESCE(pe_ltp,     0) AS pe_ltp,
            COALESCE(pe_TPrice,  0) AS pe_tprice,
            COALESCE(pe_ltp_s, 'NA') AS pe_ltp_s,
            COALESCE(pe_iv,      0) AS pe_iv,
            COALESCE(pe_delta,   0) AS pe_delta,
            COALESCE(pe_oi,      0) AS pe_oi,
            COALESCE(pe_volume,  0) AS pe_volume,
            COALESCE(pe_bid_ask_spread, 0) AS pe_spread,
            COALESCE(underlying_price,  0) AS spot
        FROM {tbl()}
        WHERE symbol=? AND expiry=?
                    AND timestamp = (
              SELECT MAX(timestamp) FROM {tbl()}
              WHERE symbol = ? AND expiry = ?)
 AND (COALESCE(ce_ltp,0) > 0 OR COALESCE(pe_ltp,0) > 0)
          AND (COALESCE(ce_oi,0) >= ? OR COALESCE(pe_oi,0) >= ?)
        ORDER BY strike_price
        """,
        [symbol, expiry[:10], symbol, expiry[:10], min_oi, min_oi],
    )
    if df.empty:
        raise HTTPException(404, "No data")

    # Compute price ratios
    df["ce_ratio"] = df.apply(
        lambda r: round(r["ce_ltp"] / r["ce_tprice"], 4) if r["ce_tprice"] > 0 else None, axis=1)
    df["pe_ratio"] = df.apply(
        lambda r: round(r["pe_ltp"] / r["pe_tprice"], 4) if r["pe_tprice"] > 0 else None, axis=1)
    df["ce_diff_pct"] = df.apply(
        lambda r: round((r["ce_ltp"] - r["ce_tprice"]) / r["ce_tprice"] * 100, 2)
        if r["ce_tprice"] > 0 else None, axis=1)
    df["pe_diff_pct"] = df.apply(
        lambda r: round((r["pe_ltp"] - r["pe_tprice"]) / r["pe_tprice"] * 100, 2)
        if r["pe_tprice"] > 0 else None, axis=1)

    # Apply ratio filter: show rows outside the "normal" band
    if min_ratio > 0 or max_ratio < 999:
        mask = (
            ((df["ce_ratio"].notna()) & ((df["ce_ratio"] < min_ratio) | (df["ce_ratio"] > max_ratio))) |
            ((df["pe_ratio"].notna()) & ((df["pe_ratio"] < min_ratio) | (df["pe_ratio"] > max_ratio)))
        )
        df = df[mask]

    return safe_response({"rows": to_records(df), "symbol": symbol, "expiry": expiry[:10]})


@app.get("/api/oi_history")
def oi_history(
    symbol:     str = Query(...),
    expiry:     str = Query(...),
    days:       int = Query(5),
    price_range_pct: float = Query(10.0),   # ATM ± % filter
):
    """
    Multi-day OI history for heatmap: strikes × dates → OI change.
    Filters strikes to ATM ± price_range_pct to keep the heatmap readable.
    """
    # Use intraday timestamps when days=1 (today), daily aggregation otherwise
    today = date.today().isoformat()
    if days <= 1:
        # Intraday: show each snapshot as a separate column in the heatmap
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%H:%M') AS date,
                strike_price,
                COALESCE(ce_oi, 0)        AS ce_oi,
                COALESCE(pe_oi, 0)        AS pe_oi,
                COALESCE(ce_oi_change, 0) AS ce_oi_chg,
                COALESCE(pe_oi_change, 0) AS pe_oi_chg,
                COALESCE(underlying_price,0) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND timestamp  = ?
              AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
            ORDER BY timestamp, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    else:
        df = qdf(
            f"""
            WITH daily AS (
                SELECT
                    CAST(timestamp AS DATE)  AS dt,
                    strike_price,
                    AVG(COALESCE(ce_oi, 0))            AS ce_oi,
                    AVG(COALESCE(pe_oi, 0))            AS pe_oi,
                    AVG(COALESCE(underlying_price, 0)) AS spot
                FROM {tbl()}
                WHERE symbol=? AND expiry=?
                  AND CAST(timestamp AS DATE) >=
                      CAST(? AS DATE) - CAST({days} AS INTEGER) * INTERVAL '1' DAY
                  AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
                GROUP BY dt, strike_price
            )
            SELECT
                CAST(dt AS VARCHAR)  AS date,
                strike_price,
                ce_oi, pe_oi, spot,
                ce_oi - LAG(ce_oi) OVER (
                    PARTITION BY strike_price ORDER BY dt) AS ce_oi_chg,
                pe_oi - LAG(pe_oi) OVER (
                    PARTITION BY strike_price ORDER BY dt) AS pe_oi_chg
            FROM daily
            ORDER BY dt, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    # If multi-day query empty (only today's data in DB), fall back to intraday
    if df.empty and days > 1:
        today = date.today().isoformat()
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%H:%M') AS date,
                strike_price,
                COALESCE(ce_oi, 0)        AS ce_oi,
                COALESCE(pe_oi, 0)        AS pe_oi,
                COALESCE(ce_oi_change, 0) AS ce_oi_chg,
                COALESCE(pe_oi_change, 0) AS pe_oi_chg,
                COALESCE(underlying_price,0) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND timestamp  = ? 
              AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
            ORDER BY timestamp, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    if df.empty:
        raise HTTPException(404, "No history data — check symbol/expiry selection")

    # Filter by price range around average spot
    avg_spot = float(df["spot"].mean())
    if avg_spot > 0:
        lo_price = avg_spot * (1 - price_range_pct / 100)
        hi_price = avg_spot * (1 + price_range_pct / 100)
        df = df[(df["strike_price"] >= lo_price) & (df["strike_price"] <= hi_price)]

    dates   = sorted(df["date"].unique())
    strikes = sorted(df["strike_price"].unique())
    return safe_response({
        "symbol":  symbol,
        "expiry":  expiry[:10],
        "dates":   dates,
        "strikes": [float(s) for s in strikes],
        "rows":    to_records(df),
    })


@app.get("/api/iv_history")
def iv_history(
    symbol:     str   = Query(...),
    expiry:     str   = Query(...),
    days:       int   = Query(5),
    price_range_pct: float = Query(10.0),
):
    """Multi-day IV history for heatmap: strikes × dates → ATM IV."""
    df = qdf(
        f"""
        WITH daily AS (
            SELECT
                CAST(timestamp AS DATE)  AS dt,
                strike_price,
                AVG(COALESCE(ce_iv, 0)) AS ce_iv,
                AVG(COALESCE(pe_iv, 0)) AS pe_iv,
                AVG(COALESCE(underlying_price, 0)) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND CAST(timestamp AS DATE) >= CAST(? AS DATE) - INTERVAL ({days}) DAYS
              AND COALESCE(ce_iv, 0) > 0
            GROUP BY dt, strike_price
        )
        SELECT
            CAST(dt AS VARCHAR) AS date,
            strike_price,
            ce_iv, pe_iv, spot,
            (ce_iv + pe_iv) / 2.0 AS avg_iv,
            (ce_iv + pe_iv) / 2.0
              - LAG((ce_iv + pe_iv) / 2.0) OVER (PARTITION BY strike_price ORDER BY dt) AS iv_chg
        FROM daily
        ORDER BY dt, strike_price
        """,
        [symbol, expiry[:10], date.today().isoformat()],
    )
    if df.empty:
        raise HTTPException(404, "No IV history")

    avg_spot = float(df["spot"].mean())
    if avg_spot > 0:
        lo_price = avg_spot * (1 - price_range_pct / 100)
        hi_price = avg_spot * (1 + price_range_pct / 100)
        df = df[(df["strike_price"] >= lo_price) & (df["strike_price"] <= hi_price)]

    dates   = sorted(df["date"].unique())
    strikes = sorted(df["strike_price"].unique())
    return safe_response({
        "symbol":  symbol,
        "expiry":  expiry[:10],
        "dates":   dates,
        "strikes": [float(s) for s in strikes],
        "rows":    to_records(df),
    })



# ===========================================================================
# OI Flow Bucket Analysis
# ===========================================================================

_BUCKET_NAMES = ["DEEP_ITM", "ATM", "NEAR_OTM", "FAR_OTM", "DEEP_OTM"]

def _assign_bucket(abs_delta: float, b_atm: float, b_near: float,
                   b_far: float, b_deep: float) -> str:
    """Assign a bucket name based on absolute delta value."""
    if abs_delta >= b_atm:   return "DEEP_ITM"
    if abs_delta >= b_near:  return "ATM"
    if abs_delta >= b_far:   return "NEAR_OTM"
    if abs_delta >= b_deep:  return "FAR_OTM"
    return "DEEP_OTM"

def _dte_regime(dte: float) -> str:
    if dte <= 1:   return "expiry"
    if dte <= 7:   return "short"
    if dte <= 21:  return "medium"
    return "long"

def _flow_signal(ce_oi_chg: float, pe_oi_chg: float,
                 spot_chg: float, ce_tbq: float, pe_tbq: float) -> dict:
    """Classify the dominant flow type for a bucket."""
    net_oi = ce_oi_chg + pe_oi_chg
    # Primary OI signal
    if ce_oi_chg > 0 and spot_chg > 0:   ce_sig = "Long Build-Up"
    elif ce_oi_chg > 0 and spot_chg <= 0: ce_sig = "Short Build-Up"
    elif ce_oi_chg < 0 and spot_chg > 0:  ce_sig = "Short Covering"
    elif ce_oi_chg < 0 and spot_chg <= 0: ce_sig = "Long Unwinding"
    else: ce_sig = "Neutral"

    if pe_oi_chg > 0 and spot_chg <= 0:   pe_sig = "Long Build-Up"
    elif pe_oi_chg > 0 and spot_chg > 0:  pe_sig = "Short Build-Up"
    elif pe_oi_chg < 0 and spot_chg <= 0: pe_sig = "Short Covering"
    elif pe_oi_chg < 0 and spot_chg > 0:  pe_sig = "Long Unwinding"
    else: pe_sig = "Neutral"

    # Flow type classification
    buy_imb = (ce_tbq - pe_tbq) / (ce_tbq + pe_tbq) if (ce_tbq + pe_tbq) > 0 else 0
    if   ce_oi_chg > 0 and pe_oi_chg > 0 and buy_imb > 0.2:
        flow_type = "Speculative"
    elif pe_oi_chg > 0 and spot_chg > 0:
        flow_type = "Hedging"
    elif ce_oi_chg > 0 and pe_oi_chg < 0 and buy_imb > 0.1:
        flow_type = "Squeeze Setup"
    elif pe_oi_chg > 0 and abs(pe_oi_chg) > abs(ce_oi_chg) * 3:
        flow_type = "Panic Flow"
    elif abs(buy_imb) < 0.05 and abs(net_oi) < 100:
        flow_type = "Dealer Positioning"
    else:
        flow_type = "Mixed"

    return {"ce_signal": ce_sig, "pe_signal": pe_sig, "flow_type": flow_type,
            "buy_imbalance": round(buy_imb, 3)}


@app.get("/api/oi_flow_buckets")
def oi_flow_buckets(
    symbol:       str            = Query(...),
    expiry:       str            = Query("all"),      # specific date or "all"
    date_from:    Optional[str]  = Query(None),       # YYYY-MM-DD, default today
    date_to:      Optional[str]  = Query(None),       # YYYY-MM-DD, default today
    min_oi:       float          = Query(0),
    min_volume:   float          = Query(0),
    max_baq_pct:  float          = Query(15.0),       # max bid-ask/ltp %
    b_atm:        float          = Query(0.50),       # |delta| >= this → DEEP_ITM
    b_near:       float          = Query(0.30),       # |delta| >= this → ATM
    b_far:        float          = Query(0.15),       # |delta| >= this → NEAR_OTM
    b_deep:       float          = Query(0.05),       # |delta| >= this → FAR_OTM
                                                       # < b_deep      → DEEP_OTM
    stable_pct:   float          = Query(1.0),        # total OI change % for crossing signal
    cross_pct:    float          = Query(5.0),        # bucket share change % for crossing signal
):
    """
    OI Flow Bucket Analysis.

    1. Fetches all snapshots for symbol/expiry in the date range.
    2. Assigns each strike to a fixed bucket based on its delta at the
       FIRST snapshot (stable cohort — prevents migration distortion).
    3. For each subsequent timestamp, sums OI, OI change, volume, TBQ
       per bucket.
    4. Computes velocity (d(OI)/dt) and acceleration (d²(OI)/dt²) per bucket.
    5. Detects migrated strikes (current delta bucket ≠ assigned bucket).
    6. Emits crossing signal when total OI stable but bucket composition shifts.
    """
    today = date.today().isoformat()
    d_from = date_from or today #day_oldest_ts(symbol)
    d_to   = date_to   or  today #day_latest_ts(symbol)

    d_from = day_oldest_ts(symbol, d_from)
    d_to = day_latest_ts(symbol, d_to)

    expiry_filter = "" if expiry == "all" else "AND expiry = ?"
    expiry_params = [] if expiry == "all" else [expiry[:10]]

    # Fetch all rows in date range — no timestamp BETWEEN, use date cast
    df = qdf(
        f"""
        SELECT
            STRFTIME(timestamp, '%Y-%m-%d %H:%M')   AS ts,
            CAST(timestamp AS DATE)                  AS dt,
            CAST(expiry AS VARCHAR)                  AS expiry,
            strike_price,
            COALESCE(days_to_expiry,  0)             AS dte,
            COALESCE(underlying_price,0)             AS spot,
            ABS(COALESCE(ce_delta, 0))               AS ce_adelta,
            ABS(COALESCE(pe_delta, 0))               AS pe_adelta,
            COALESCE(ce_oi,        0)                AS ce_oi,
            COALESCE(pe_oi,        0)                AS pe_oi,
            COALESCE(ce_oi_change, 0)                AS ce_oi_chg,
            COALESCE(pe_oi_change, 0)                AS pe_oi_chg,
            COALESCE(ce_ltp,       0)                AS ce_ltp,
            COALESCE(pe_ltp,       0)                AS pe_ltp,
            COALESCE(ce_iv,        0)                AS ce_iv,
            COALESCE(pe_iv,        0)                AS pe_iv,
            COALESCE(ce_volume,    0)                AS ce_vol,
            COALESCE(pe_volume,    0)                AS pe_vol,
            COALESCE(ce_gamma,     0)                AS ce_gamma,
            COALESCE(pe_gamma,     0)                AS pe_gamma,
            COALESCE(ce_gexv,      0)                AS ce_gexv,
            COALESCE(pe_gexv,      0)                AS pe_gexv,
            COALESCE(ce_vanna,     0)                AS ce_vanna,
            COALESCE(net_vanna_ex, 0)                AS net_vanna,
            COALESCE(net_flow,     0)                AS net_flow,
            COALESCE(ce_tbq,       0)                AS ce_tbq,
            COALESCE(pe_tbq,       0)                AS pe_tbq,
            COALESCE(ce_bid_ask_spread, 0)           AS ce_baq,
            COALESCE(pe_bid_ask_spread, 0)           AS pe_baq,
            COALESCE(lotsize, 1)                     AS lot
        FROM {tbl()}
        WHERE symbol = ?
          AND timestamp BETWEEN ? AND ? 
          AND expiry >= CURRENT_DATE
          {expiry_filter}
        ORDER BY timestamp, strike_price
        """,
        [symbol, d_from, d_to] + expiry_params,
    )
    if df.empty:
        raise HTTPException(404, "No flow data for given filters")

    # ── Step 1: Apply liquidity filters ──────────────────────────────────
    # For each strike, compute baq% for both sides
    df["ce_baq_pct"] = df.apply(
        lambda r: r["ce_baq"]/r["ce_ltp"]*100 if r["ce_ltp"] > 0 else 999, axis=1)
    df["pe_baq_pct"] = df.apply(
        lambda r: r["pe_baq"]/r["pe_ltp"]*100 if r["pe_ltp"] > 0 else 999, axis=1)

    df = df[
        (df["ce_oi"] >= min_oi) | (df["pe_oi"] >= min_oi)
    ]
    df = df[
        (df["ce_vol"] >= min_volume) | (df["pe_vol"] >= min_volume)
    ]
    df = df[
        (df["ce_baq_pct"] <= max_baq_pct) | (df["pe_baq_pct"] <= max_baq_pct)
    ]
    # Filter out rows with zero/missing Greeks — these are data quality issues
    # where NSE didn't publish IV/delta (e.g. untradeable deep strikes, pre-open).
    # A row is valid only if at least one side has both a non-zero delta AND non-zero IV.
    df = df[
        ((df["ce_adelta"] > 0) & (df["ce_iv"] > 0)) |
        ((df["pe_adelta"] > 0) & (df["pe_iv"] > 0))
    ]
    if df.empty:
        raise HTTPException(404, "All strikes filtered by liquidity criteria")

    # ── Step 2: Fixed cohort — assign bucket from FIRST snapshot ─────────
    timestamps = sorted(df["ts"].unique().tolist())
    if not timestamps:
        raise HTTPException(404, "No timestamps found")

    ts0 = timestamps[0]
    first_snap = df[df["ts"] == ts0][["strike_price","ce_adelta","pe_adelta"]].copy()

    # Use CE delta for CE-side bucket, PE delta for PE-side bucket.
    # DO NOT use max(ce,pe): put-call parity means pe_adelta ≈ 1-ce_adelta,
    # so max() always picks pe_adelta > 0.5 pushing all strikes into DEEP_ITM.
    # Instead assign each strike one bucket based on CE delta (calls drive the
    # bucket label; PE analysis uses the same strike grouping).
    # CE side: bucket from CE delta (call side)
    first_snap["ce_bucket_open"] = first_snap["ce_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))
    # PE side: bucket from PE delta (put side — pe_adelta already = |pe_delta|)
    first_snap["pe_bucket_open"] = first_snap["pe_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))

    ce_cohort = first_snap.set_index("strike_price")["ce_bucket_open"].to_dict()
    pe_cohort = first_snap.set_index("strike_price")["pe_bucket_open"].to_dict()

    # For groupby aggregation, use CE bucket as the primary bucket label
    # (consistent with how we report OI flow — CE and PE tracked under same strike)
    df["bucket"]    = df["strike_price"].map(ce_cohort).fillna("DEEP_OTM")
    df["ce_bucket_open"] = df["strike_price"].map(ce_cohort).fillna("DEEP_OTM")
    df["pe_bucket_open"] = df["strike_price"].map(pe_cohort).fillna("DEEP_OTM")

    # Current buckets — computed fresh each row
    df["ce_bucket_cur"] = df["ce_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))
    df["pe_bucket_cur"] = df["pe_adelta"].apply(
        lambda d: _assign_bucket(d, b_atm, b_near, b_far, b_deep))

    # A strike "migrated" on CE side if its CE bucket changed
    df["ce_migrated"] = df["ce_bucket_open"] != df["ce_bucket_cur"]
    # A strike "migrated" on PE side if its PE bucket changed
    df["pe_migrated"] = df["pe_bucket_open"] != df["pe_bucket_cur"]
    df["migrated"]    = df["ce_migrated"] | df["pe_migrated"]

    # ── Step 3: DTE regime ───────────────────────────────────────────────
    dte_val  = float(df["dte"].median())
    dte_reg  = _dte_regime(dte_val)
    spot_now = float(df[df["ts"] == timestamps[-1]]["spot"].mean()) if timestamps else 0
    spot_t0  = float(df[df["ts"] == ts0]["spot"].mean()) if ts0 else spot_now
    spot_chg = spot_now - spot_t0

    # ── Step 4: Aggregate per timestamp × bucket ─────────────────────────
    grp = df.groupby(["ts","bucket"]).agg(
        ce_oi        = ("ce_oi",    "sum"),
        pe_oi        = ("pe_oi",    "sum"),
        ce_oi_chg    = ("ce_oi_chg","sum"),
        pe_oi_chg    = ("pe_oi_chg","sum"),
        ce_vol       = ("ce_vol",   "sum"),
        pe_vol       = ("pe_vol",   "sum"),
        ce_gexv      = ("ce_gexv",  "sum"),
        pe_gexv      = ("pe_gexv",  "sum"),
        ce_tbq       = ("ce_tbq",   "sum"),
        pe_tbq       = ("pe_tbq",   "sum"),
        net_vanna    = ("net_vanna","sum"),
        net_flow     = ("net_flow", "sum"),
        strike_count = ("strike_price","nunique"),
        migrated_count = ("migrated","sum"),
    ).reset_index()

    # Compute cumulative OI flow from first timestamp
    grp = grp.sort_values(["bucket","ts"])
    grp["ce_cum_flow"] = grp.groupby("bucket")["ce_oi_chg"].cumsum()
    grp["pe_cum_flow"] = grp.groupby("bucket")["pe_oi_chg"].cumsum()
    grp["net_cum_flow"] = grp["ce_cum_flow"] - grp["pe_cum_flow"]

    # PCR per bucket
    grp["pcr"] = grp.apply(
        lambda r: round(r["pe_oi"]/r["ce_oi"], 3) if r["ce_oi"] > 0 else None, axis=1)

    # ── Step 5: Velocity and Acceleration per bucket ──────────────────────
    def _deriv(series: pd.Series) -> pd.Series:
        """First derivative (velocity) using central differences."""
        return series.diff().fillna(0)

    vel_dfs = []
    for bname, bdf in grp.groupby("bucket"):
        bdf = bdf.sort_values("ts").copy()
        bdf["ce_velocity"]     = _deriv(bdf["ce_cum_flow"])
        bdf["pe_velocity"]     = _deriv(bdf["pe_cum_flow"])
        bdf["ce_acceleration"] = _deriv(bdf["ce_velocity"])
        bdf["pe_acceleration"] = _deriv(bdf["pe_velocity"])
        vel_dfs.append(bdf)
    if vel_dfs:
        grp = pd.concat(vel_dfs).sort_values(["ts","bucket"])

    # ── Step 6: Crossing signal ───────────────────────────────────────────
    crossing_signal = None
    if len(timestamps) >= 2:
        ts_now   = timestamps[-1]
        snap_t0  = grp[grp["ts"] == ts0].copy()
        snap_now = grp[grp["ts"] == ts_now].copy()
        total_t0  = snap_t0["ce_oi"].sum() + snap_t0["pe_oi"].sum()
        total_now = snap_now["ce_oi"].sum() + snap_now["pe_oi"].sum()
        if total_t0 > 0:
            total_chg_pct = abs(total_now - total_t0) / total_t0 * 100
            if total_chg_pct <= stable_pct:
                # Check if any bucket's share changed significantly
                for bname in _BUCKET_NAMES:
                    bt0  = snap_t0[snap_t0["bucket"]==bname]
                    bnow = snap_now[snap_now["bucket"]==bname]
                    oi_t0  = float(bt0["ce_oi"].sum() + bt0["pe_oi"].sum()) if not bt0.empty else 0
                    oi_now = float(bnow["ce_oi"].sum() + bnow["pe_oi"].sum()) if not bnow.empty else 0
                    share_t0  = oi_t0  / total_t0  * 100 if total_t0  > 0 else 0
                    share_now = oi_now / total_now * 100 if total_now > 0 else 0
                    if abs(share_now - share_t0) >= cross_pct:
                        direction = "increasing" if share_now > share_t0 else "decreasing"
                        crossing_signal = {
                            "fired":        True,
                            "bucket":       bname,
                            "direction":    direction,
                            "share_change": round(share_now - share_t0, 2),
                            "total_oi_chg": round(total_chg_pct, 2),
                            "description":  (
                                f"Total OI stable ({total_chg_pct:.1f}%) but "
                                f"{bname} bucket share {direction} by "
                                f"{abs(share_now-share_t0):.1f}% — "
                                f"rotation signal"
                            ),
                        }
                        break

    # ── Step 7: Migrated strikes at latest timestamp ──────────────────────
    latest = df.loc[(df["ts"] == timestamps[-1]) & (df["migrated"] == True)]
    migrated_list = []
    for _, row in latest.iterrows():
        entry: Dict[str, Any] = {
            "strike": float(row["strike_price"]),
            # CE side
            "ce_delta":       round(float(row["ce_adelta"]), 3),
            "ce_bucket_open": row["ce_bucket_open"],
            "ce_bucket_cur":  row["ce_bucket_cur"],
            "ce_migrated":    bool(row["ce_migrated"]),
            "ce_oi":          float(row["ce_oi"]),
            "ce_oi_chg":      float(row["ce_oi_chg"]),
            # PE side
            "pe_delta":       round(float(row["pe_adelta"]), 3),
            "pe_bucket_open": row["pe_bucket_open"],
            "pe_bucket_cur":  row["pe_bucket_cur"],
            "pe_migrated":    bool(row["pe_migrated"]),
            "pe_oi":          float(row["pe_oi"]),
            "pe_oi_chg":      float(row["pe_oi_chg"]),
        }
        migrated_list.append(entry)

    # ── Step 8: Flow signals at latest snapshot ───────────────────────────
    flow_signals = {}
    snap_latest = grp[grp["ts"] == timestamps[-1]] if timestamps else pd.DataFrame()
    # Get strike ranges per bucket from the full df at latest timestamp
    latest_df = df.loc[df["ts"] == timestamps[-1]] if timestamps else pd.DataFrame()
    bucket_strike_ranges: Dict[str, dict] = {}
    for bname in _BUCKET_NAMES:
        bstrikes = latest_df.loc[latest_df["bucket"] == bname, "strike_price"]
        if not bstrikes.empty:
            bucket_strike_ranges[bname] = {
                "min_strike": float(bstrikes.min()),
                "max_strike": float(bstrikes.max()),
            }
        else:
            bucket_strike_ranges[bname] = {"min_strike": None, "max_strike": None}

    for bname in _BUCKET_NAMES:
        brow = snap_latest[snap_latest["bucket"] == bname]
        if brow.empty:
            flow_signals[bname] = {"ce_signal":"—","pe_signal":"—",
                                   "flow_type":"—","buy_imbalance":0,
                                   "strike_count":0,
                                   **bucket_strike_ranges.get(bname,{})}
            continue
        r = brow.iloc[0]
        sig = _flow_signal(
            float(r.get("ce_oi_chg",0)), float(r.get("pe_oi_chg",0)),
            spot_chg,
            float(r.get("ce_tbq",0)),    float(r.get("pe_tbq",0)),
        )
        sig["strike_count"]    = int(r.get("strike_count",0))
        sig["migrated_count"]  = int(r.get("migrated_count",0))
        sig["ce_cum_flow"]     = round(float(r.get("ce_cum_flow",0)),0)
        sig["pe_cum_flow"]     = round(float(r.get("pe_cum_flow",0)),0)
        sig["net_cum_flow"]    = round(float(r.get("net_cum_flow",0)),0)
        sig["pcr"]             = r.get("pcr")
        sig.update(bucket_strike_ranges.get(bname, {}))
        flow_signals[bname] = sig

    # ── Serialise time-series per bucket ─────────────────────────────────
    bucket_series = {}
    for bname in _BUCKET_NAMES:
        bdf = grp[grp["bucket"]==bname].sort_values("ts")
        bucket_series[bname] = {
            "timestamps":       bdf["ts"].tolist(),
            "ce_cum_flow":      [round(v,0) for v in bdf["ce_cum_flow"].fillna(0)],
            "pe_cum_flow":      [round(v,0) for v in bdf["pe_cum_flow"].fillna(0)],
            "net_cum_flow":     [round(v,0) for v in bdf["net_cum_flow"].fillna(0)],
            "ce_velocity":      [round(v,2) for v in bdf.get("ce_velocity",pd.Series(dtype=float)).fillna(0)],
            "pe_velocity":      [round(v,2) for v in bdf.get("pe_velocity",pd.Series(dtype=float)).fillna(0)],
            "ce_acceleration":  [round(v,2) for v in bdf.get("ce_acceleration",pd.Series(dtype=float)).fillna(0)],
            "pe_acceleration":  [round(v,2) for v in bdf.get("pe_acceleration",pd.Series(dtype=float)).fillna(0)],
            "pcr":              [_safe(v) for v in bdf["pcr"]],
            "ce_gexv":          [round(v/1e6,4) for v in bdf["ce_gexv"].fillna(0)],
            "pe_gexv":          [round(v/1e6,4) for v in bdf["pe_gexv"].fillna(0)],
            "net_flow":         [round(v,2) for v in bdf["net_flow"].fillna(0)],
            "strike_count":     bdf["strike_count"].tolist(),
            "migrated_count":   bdf["migrated_count"].astype(int).tolist(),
            "ce_tbq":           [round(v,0) for v in bdf["ce_tbq"].fillna(0)],
            "pe_tbq":           [round(v,0) for v in bdf["pe_tbq"].fillna(0)],
        }

    return safe_response({
        "symbol":           symbol,
        "expiry":           expiry,
        "date_from":        d_from,
        "date_to":          d_to,
        "timestamps":       timestamps,
        "dte":              round(dte_val, 1),
        "dte_regime":       dte_reg,
        "spot_open":        round(spot_t0, 2),
        "spot_now":         round(spot_now, 2),
        "spot_chg":         round(spot_chg, 2),
        "bucket_thresholds": {
            "DEEP_ITM": f"|δ| ≥ {b_atm}",
            "ATM":      f"|δ| {b_near}–{b_atm}",
            "NEAR_OTM": f"|δ| {b_far}–{b_near}",
            "FAR_OTM":  f"|δ| {b_deep}–{b_far}",
            "DEEP_OTM": f"|δ| < {b_deep}",
        },
        "buckets":          bucket_series,
        "flow_signals":     flow_signals,
        "crossing_signal":  crossing_signal,
        "migrated_strikes": migrated_list,
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
