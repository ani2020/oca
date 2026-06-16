"""Database helpers — connection, query, serialisation."""
from __future__ import annotations
import json
import math
from datetime import date, datetime
from typing import Any, Dict, List, Optional

import duckdb
import numpy as np
import pandas as pd
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from fastapi.encoders import jsonable_encoder

from . import config


def tbl() -> str:
    """Return the configured table name."""
    return config.DB_TABLE


def qdf(sql: str, params: list = []) -> pd.DataFrame:
    """Execute query and return a DataFrame. Opens a read-only connection per call."""
    try:
        con = duckdb.connect(config.DB_FILE, read_only=True)
        df = con.execute(sql, params).df()
        con.close()
        return df
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _qraw(sql: str, params: list = []):
    """Execute and return raw fetchall result."""
    try:
        con = duckdb.connect(config.DB_FILE, read_only=True)
        result = con.execute(sql, params).fetchall()
        con.close()
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


def _safe(v: Any) -> Any:
    """Make a value JSON-safe (NaN/Inf → None)."""
    if v is None:
        return None
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    if isinstance(v, (np.floating, np.integer)):
        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return f
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, pd.Timestamp):
        return v.isoformat()
    return v


def to_records(df: pd.DataFrame) -> List[Dict]:
    """Convert DataFrame to list of dicts with JSON-safe values."""
    if df.empty:
        return []
    records = df.to_dict(orient="records")
    return [{k: _safe(v) for k, v in row.items()} for row in records]


def clean_dict(d: Any) -> Any:
    """Recursively clean a dict/list for JSON serialisation."""
    if isinstance(d, dict):
        return {k: clean_dict(v) for k, v in d.items()}
    if isinstance(d, list):
        return [clean_dict(i) for i in d]
    return _safe(d)


class _SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles NaN, Inf, numpy types, dates."""
    def default(self, o):
        if isinstance(o, (np.floating,)):
            v = float(o)
            if math.isnan(v) or math.isinf(v):
                return None
            return v
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.bool_,)):
            return bool(o)
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if isinstance(o, pd.Timestamp):
            return o.isoformat()
        return super().default(o)

    def encode(self, o):
        return super().encode(self._sanitise(o))

    @staticmethod
    def _sanitise(obj):
        if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
            return None
        if isinstance(obj, dict):
            return {k: _SafeJSONEncoder._sanitise(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_SafeJSONEncoder._sanitise(i) for i in obj]
        return obj


class _SafeJSONResponse(JSONResponse):
    """JSONResponse that uses our safe encoder."""
    def render(self, content: Any) -> bytes:
        return json.dumps(
            content,
            cls=_SafeJSONEncoder,
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")


def safe_response(data: Any) -> JSONResponse:
    """Return a _SafeJSONResponse with cleaned data."""
    return _SafeJSONResponse(content=clean_dict(data))


def ts_filter_clause(ts_str: str, col: str = "timestamp") -> tuple:
    """
    Build a timestamp filter clause.
    ts_str can be 'YYYY-MM-DD HH:MM' or 'YYYY-MM-DD HH:MM:SS' or 'YYYY-MM-DD'.
    Returns (sql_fragment, params_list).
    """
    if not ts_str:
        return "", []
    ts_str = ts_str.strip()
    if len(ts_str) == 10:  # date only → match the whole day
        return f"AND CAST({col} AS DATE) = CAST(? AS DATE)", [ts_str]
    if len(ts_str) == 16:  # YYYY-MM-DD HH:MM (no seconds) → match by minute
        return f"AND STRFTIME({col}, '%Y-%m-%d %H:%M') = ?", [ts_str]
    return f"AND {col} = ?", [ts_str]


def _ts_lo_hi(ts_str: str) -> list:
    """Return [lo, hi] datetime strings for a timestamp filter."""
    ts_str = ts_str.strip()
    if len(ts_str) == 10:
        return [ts_str + " 00:00:00", ts_str + " 23:59:59"]
    return [ts_str, ts_str]


def expiry_clause(exp_str: str, col: str = "expiry") -> tuple:
    """Build an expiry filter clause. Returns (sql_fragment, params_list)."""
    if not exp_str:
        return "", []
    return f"AND {col} = ?", [exp_str[:10]]


def latest_ts(symbol: str) -> str:
    """Get the latest timestamp string for a symbol."""
    rows = _qraw(
        f"SELECT STRFTIME(MAX(timestamp), '%Y-%m-%d %H:%M') FROM {tbl()} WHERE symbol=?",
        [symbol],
    )
    if rows and rows[0][0]:
        return rows[0][0]
    return ""


def latest_data_date(symbol: str) -> str:
    """Latest DATA date (YYYY-MM-DD) for a symbol — NOT wall-clock today.
    Use as the anchor for history windows / 'today's data' so that weekends,
    holidays, missed scrapes, or pre-market review (calendar ahead of data) don't
    truncate windows or 404. Per-symbol because the scraped NSE source has random
    symbol-level pull gaps, so a global MAX(date) can be ahead of a given symbol.
    """
    rows = _qraw(
        f"SELECT CAST(MAX(CAST(timestamp AS DATE)) AS VARCHAR) FROM {tbl()} WHERE symbol=?",
        [symbol],
    )
    if rows and rows[0][0]:
        return rows[0][0]
    return ""
