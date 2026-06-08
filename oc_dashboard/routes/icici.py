"""ICICI Breeze margin endpoints."""
from __future__ import annotations
import os
import sys
import threading
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records
from ..cache import margin_cache_get, margin_cache_put, margin_cache_clear
from .. import config

router = APIRouter()

# ICICI Breeze session (module-level, lazy init)
_icici: Any = None
_icici_status: str = "not_configured"
_icici_error: Optional[str] = None


def _to_icici_ticker(nse_symbol: str) -> str:
    """Convert NSE ticker to ICICI Breeze 6-char stock_code."""
    return config.ICICI_MAP.get(nse_symbol, nse_symbol[:6].upper())


def _try_init_icici(session_token: Optional[str] = None) -> bool:
    """Attempt to initialise ICICI Breeze session."""
    global _icici, _icici_status, _icici_error
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
        from icici_b import ICB
        _icici = ICB(session_token=session_token)
        _icici_status = "connected"
        _icici_error = None
        return True
    except ImportError:
        _icici_status = "breeze_connect_missing"
        _icici_error = "icici_b module not found"
        return False
    except Exception as exc:
        _icici_status = f"error: {exc}"
        _icici_error = str(exc)
        return False


@router.post("/api/icici/configure")
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


@router.get("/api/icici/status")
def icici_status():
    return {
        "status":          _icici_status,
        "configured":      _icici is not None,
        "env_key":         bool(os.environ.get("IC_API_KEY")),
        "ticker_map_size": len(config.ICICI_MAP),
    }


@router.get("/api/icici/margin")
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
    margin_cache_put(symbol, strike, expiry, option_type, cache_result, save=False)
    return {**cache_result, "cached": False}


@router.post("/api/icici/margin/batch")
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
                    margin_cache_put(symbol, strike, expiry, option_type,
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


@router.post("/api/icici/margin/refresh")
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


