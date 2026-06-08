"""Market info and VIX endpoints."""
from __future__ import annotations
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _safe
from ..nse import get_fetcher
from .. import config

router = APIRouter()

@router.get("/api/vix")
def vix_data(lookback_days: int = Query(30)):
    """
    Current India VIX (from NSEFetcher) + historical for the lookback window.
    Falls back gracefully if NSEFetcher is unavailable.
    """
    try:
        fetcher = get_fetcher()
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


@router.get("/api/market/status")
def market_status():
    """Live NSE market open/closed/pre-open status."""
    f = get_fetcher()
    try:
        result = f.get_market_status()
        return safe_response(result)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/api/market/block_deals")
def market_block_deals():
    """Today's NSE block deals."""
    f = get_fetcher()
    try:
        df = f.get_block_deals()
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/api/market/corp_actions")
def market_corp_actions(symbol: Optional[str] = Query(None)):
    """NSE corporate actions (ex-div, bonus, splits). Filter by symbol optionally."""
    f = get_fetcher()
    try:
        df = f.get_corporate_actions(symbol=symbol)
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/api/market/announcements")
def market_announcements(symbol: Optional[str] = Query(None)):
    """Latest NSE corporate announcements. Filter by symbol optionally."""
    f = get_fetcher()
    try:
        df = f.get_corporate_announcements(symbol=symbol)
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


@router.get("/api/market/board_meetings")
def market_board_meetings(symbol: Optional[str] = Query(None)):
    """Upcoming NSE board meetings. Filter by symbol optionally."""
    f = get_fetcher()
    try:
        df = f.get_board_meetings(symbol=symbol)
        return [] if df is None or df.empty else to_records(df)
    except Exception as exc:
        raise HTTPException(502, str(exc))


