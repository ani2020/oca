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
from fastapi.responses import HTMLResponse, JSONResponse
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
# DuckDB — single read-only connection
# ---------------------------------------------------------------------------
_con: Optional[duckdb.DuckDBPyConnection] = None


def get_con() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect(_DB_FILE, read_only=True)
    return _con


def qdf(sql: str, params: list = []) -> pd.DataFrame:
    try:
        return get_con().execute(sql, params).df()
    except Exception as exc:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(exc))


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


def latest_ts(symbol: str) -> str:
    row = get_con().execute(
        f"SELECT MAX(timestamp) FROM {tbl()} WHERE symbol = ?", [symbol]
    ).fetchone()
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

@asynccontextmanager
async def lifespan(app: FastAPI):
    print(f"✓ Connecting to {_DB_FILE} …")
    get_con()
    _load_icici_ticker_map()
    _try_init_icici()
    print("✓ Dashboard ready — open http://localhost:8000")
    yield
    if _con:
        _con.close()


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
    df = qdf(f"SELECT DISTINCT symbol FROM {tbl()} ORDER BY symbol")
    return df["symbol"].tolist()


@app.get("/api/timestamps")
def list_timestamps(
    symbol: str           = Query(...),
    expiry: Optional[str] = Query(None),
):
    """
    List distinct timestamps for a symbol.
    Optional expiry filter ensures timestamps shown actually have data
    for the selected expiry (timestamps can differ slightly per ticker).
    """
    if expiry:
        df = qdf(
            f"SELECT DISTINCT CAST(timestamp AS VARCHAR) AS ts "
            f"FROM {tbl()} WHERE symbol = ? "
            f"AND CAST(expiry AS VARCHAR) LIKE ? ORDER BY ts DESC",
            [symbol, f"%{expiry[:10]}%"],
        )
    else:
        df = qdf(
            f"SELECT DISTINCT CAST(timestamp AS VARCHAR) AS ts "
            f"FROM {tbl()} WHERE symbol = ? ORDER BY ts DESC",
            [symbol],
        )
    return df["ts"].tolist()


@app.get("/api/expiries")
def list_expiries(
    symbol:    str           = Query(...),
    timestamp: Optional[str] = Query(None),
):
    if timestamp:
        df = qdf(
            f"SELECT DISTINCT CAST(expiry AS VARCHAR) AS exp FROM {tbl()} "
            f"WHERE symbol = ? AND CAST(timestamp AS VARCHAR) LIKE ? ORDER BY exp",
            [symbol, f"%{timestamp[:16]}%"],
        )
    else:
        df = qdf(
            f"SELECT DISTINCT CAST(expiry AS VARCHAR) AS exp FROM {tbl()} "
            f"WHERE symbol = ? ORDER BY exp",
            [symbol],
        )
    return df["exp"].tolist()


@app.get("/api/lot_sizes")
def lot_sizes():
    """Return lot_size for each symbol (used by frontend for strike step)."""
    df = qdf(
        f"SELECT symbol, MAX(COALESCE(lotsize, 1)) AS lot_size "
        f"FROM {tbl()} GROUP BY symbol ORDER BY symbol"
    )
    if df.empty:
        return {}
    return {row["symbol"]: int(row["lot_size"]) for row in df.to_dict(orient="records")}


# ===========================================================================
# Overview / snapshot
# ===========================================================================

@app.get("/api/overview")
def overview():
    sql = f"""
    WITH latest AS (
        SELECT symbol, CAST(MAX(timestamp) AS VARCHAR) AS ts
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
        MAX(COALESCE(t.lotsize,  1))      AS lot_size
    FROM {tbl()} t
    JOIN latest l
      ON  t.symbol = l.symbol
      AND CAST(t.timestamp AS VARCHAR) = l.ts
    GROUP BY t.symbol, l.ts
    ORDER BY t.symbol
    """
    df = qdf(sql)
    df["pcr"] = df.apply(
        lambda r: round(r["total_pe_oi"] / r["total_ce_oi"], 3)
        if r["total_ce_oi"] else None, axis=1,
    )
    return to_records(df)


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
          AND CAST(expiry    AS VARCHAR) LIKE ?
          AND CAST(timestamp AS VARCHAR) LIKE ?
        ORDER BY strike_price
        """,
        [symbol, f"%{expiry[:10]}%", f"%{timestamp[:16]}%"],
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
          AND CAST(expiry    AS VARCHAR) LIKE ?
          AND CAST(timestamp AS VARCHAR) LIKE ?
        ORDER BY strike_price
        """,
        [symbol, f"%{expiry[:10]}%", f"%{ts_filter[:16]}%"],
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
          AND CAST(expiry    AS VARCHAR) LIKE ?
          AND CAST(timestamp AS VARCHAR) LIKE ?
        ORDER BY strike_price
        """,
        [symbol, f"%{expiry[:10]}%", f"%{ts_filter[:16]}%"],
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
            f"WHERE symbol = ? AND CAST(timestamp AS VARCHAR) LIKE ?",
            [symbol, f"%{ts_filter[:16]}%"],
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
          AND CAST(n.timestamp AS VARCHAR) LIKE ?
          AND CAST(o.timestamp AS VARCHAR) LIKE ?
        ORDER BY n.expiry, n.strike_price
        """,
        [symbol, f"%{ts_new[:16]}%", f"%{ts_old[:16]}%"],
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
              AND CAST(n.timestamp AS VARCHAR) LIKE ?
              AND CAST(o.timestamp AS VARCHAR) LIKE ?
            """,
            [sym, f"%{ts_new[:16]}%", f"%{ts_old[:16]}%"],
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
def volume_shockers(top_n: int = Query(30)):
    sql = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               COALESCE(ce_volume,0)+COALESCE(pe_volume,0) AS total_vol,
               DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
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
    df = qdf(sql, [top_n])
    return [] if df.empty else to_records(df)


@app.get("/api/iv_shockers")
def iv_shockers(top_n: int = Query(30)):
    sql = f"""
    WITH ts_ranked AS (
        SELECT symbol, strike_price, expiry, timestamp,
               (COALESCE(ce_iv,0)+COALESCE(pe_iv,0))/2.0 AS avg_iv,
               DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
        FROM {tbl()}
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
    df = qdf(sql, [top_n])
    return [] if df.empty else to_records(df)


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
          AND CAST(expiry    AS VARCHAR) LIKE ?
          AND CAST(timestamp AS VARCHAR) LIKE ?
          AND (COALESCE(ce_iv,0)>0 OR COALESCE(pe_iv,0)>0)
        ORDER BY strike_price
        """,
        [symbol, f"%{expiry[:10]}%", f"%{ts_filter[:16]}%"],
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
    WHERE p.ltp>0 {sym_filter}
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
        WHERE symbol=? AND strike_price=? AND CAST(expiry AS VARCHAR) LIKE ?
        ORDER BY timestamp
        """,
        [symbol, strike_price, f"%{expiry[:10]}%"],
    )
    return to_records(df)


# ===========================================================================
# Delta screener
# ===========================================================================

@app.get("/api/delta_screener")
def delta_screener(
    symbol:       str           = Query(...),
    expiry:       str           = Query(...),
    timestamp:    Optional[str] = Query(None),
    target_delta: float         = Query(30.0),
):
    ts_filter = timestamp or latest_ts(symbol)
    tgt = abs(target_delta) / 100.0

    df = qdf(
        f"""
        SELECT strike_price,
               COALESCE(ce_ltp,    0) AS ce_ltp,
               COALESCE(pe_ltp,    0) AS pe_ltp,
               COALESCE(ce_delta,  0) AS ce_delta,
               COALESCE(pe_delta,  0) AS pe_delta,
               COALESCE(ce_iv,     0) AS ce_iv,
               COALESCE(pe_iv,     0) AS pe_iv,
               COALESCE(ce_oi,     0) AS ce_oi,
               COALESCE(pe_oi,     0) AS pe_oi,
               COALESCE(ce_volume, 0) AS ce_volume,
               COALESCE(pe_volume, 0) AS pe_volume,
               lotsize                   AS raw_lot,
               COALESCE(underlying_price,0) AS spot,
               COALESCE(days_to_expiry,  0) AS dte
        FROM {tbl()}
        WHERE symbol=?
          AND CAST(expiry    AS VARCHAR) LIKE ?
          AND CAST(timestamp AS VARCHAR) LIKE ?
        ORDER BY strike_price
        """,
        [symbol, f"%{expiry[:10]}%", f"%{ts_filter[:16]}%"],
    )
    if df.empty:
        raise HTTPException(404, "No data for delta screener")

    lot = max(int(df["raw_lot"].iloc[0]) if df["raw_lot"].iloc[0] is not None else 1, 1)

    ce = df[df["ce_delta"] > 0].copy()
    ce["option_type"]     = "CE"
    ce["delta"]           = ce["ce_delta"]
    ce["ltp"]             = ce["ce_ltp"]
    ce["iv"]              = ce["ce_iv"]
    ce["oi"]              = ce["ce_oi"]
    ce["volume"]          = ce["ce_volume"]
    ce["delta_dist"]      = (ce["delta"] - tgt).abs()
    ce["premium_per_lot"] = ce["ltp"] * lot
    ce["risk_indicator"]  = ce["strike_price"] * lot

    pe = df[df["pe_delta"] < 0].copy()
    pe["option_type"]     = "PE"
    pe["delta"]           = pe["pe_delta"].abs()
    pe["ltp"]             = pe["pe_ltp"]
    pe["iv"]              = pe["pe_iv"]
    pe["oi"]              = pe["pe_oi"]
    pe["volume"]          = pe["pe_volume"]
    pe["delta_dist"]      = (pe["delta"] - tgt).abs()
    pe["premium_per_lot"] = pe["ltp"] * lot
    pe["risk_indicator"]  = pe["strike_price"] * lot

    keep = ["option_type","strike_price","delta","delta_dist",
            "ltp","iv","oi","volume","premium_per_lot","risk_indicator","spot","dte"]
    combined = pd.concat([
        ce[keep].sort_values("delta_dist").head(20),
        pe[keep].sort_values("delta_dist").head(20),
    ], ignore_index=True)
    combined["margin"]           = None
    combined["return_on_margin"] = None

    return safe_response({"lot_size": lot, "target_delta": target_delta, "rows": to_records(combined)})


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
):
    if _icici is None:
        raise HTTPException(503, "ICICI not configured")
    try:
        exp = datetime.strptime(expiry[:10], "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(400, f"Bad expiry '{expiry}'")
    ic_symbol = _to_icici_ticker(symbol)
    result = _icici.get_margin_for_option(
        symbol=ic_symbol, strike=strike, expiry=exp,
        option_type=option_type, ltp=ltp, qty=qty, action=action,
    )
    if result is None:
        raise HTTPException(502, "Margin API call failed")
    return result


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
