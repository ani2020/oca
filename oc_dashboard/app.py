"""
oc_dashboard/app.py
-------------------
FastAPI backend for the NSE Options Chain Analytics Dashboard.

Refactored: routes in routes/, helpers in helpers.py, DB in db.py,
cache in cache.py, NSEFetcher in nse.py, config in config.py.

Run:
    python run.py
    python run.py --db /path/to/oc.duckdb --port 8080
"""
from __future__ import annotations

import argparse
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from .db import _SafeJSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .cache import margin_cache_load, margin_cache_save, start_margin_cache_autosave
from .nse import init_nse_fetcher

# Route modules
from .routes import (
    meta, overview, gex, oi, iv, screener, shockers,
    divergence, flow, smartmoney, market, icici,
)


# ---------------------------------------------------------------------------
# .env support
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ---------------------------------------------------------------------------
# ICICI ticker map (loaded at startup)
# ---------------------------------------------------------------------------
def _load_icici_ticker_map() -> None:
    """Load ICICI Breeze NSE→6-char ticker map from icici_tickers.csv."""
    import csv
    p = Path(config.DB_FILE).with_name("icici_tickers.csv")
    if not p.exists():
        return
    try:
        with open(p, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                nse = row.get("nse_ticker", "").strip()
                breeze = row.get("breeze_code", "").strip()
                if nse and breeze:
                    config.ICICI_MAP[nse] = breeze
        print(f"  ✓ ICICI ticker map loaded ({len(config.ICICI_MAP)} entries)")
    except Exception as exc:
        print(f"  ℹ ICICI ticker map not loaded: {exc}")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    _load_icici_ticker_map()
    margin_cache_load()
    start_margin_cache_autosave()
    init_nse_fetcher()
    print("✓ Dashboard ready — open http://localhost:8000")
    yield
    margin_cache_save(verbose=True)


# ---------------------------------------------------------------------------
# App creation
# ---------------------------------------------------------------------------
app = FastAPI(
    title="OC Dashboard",
    default_response_class=_SafeJSONResponse,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Register route modules
# ---------------------------------------------------------------------------
app.include_router(meta.router, tags=["meta"])
app.include_router(overview.router, tags=["overview"])
app.include_router(gex.router, tags=["gex"])
app.include_router(oi.router, tags=["oi"])
app.include_router(iv.router, tags=["iv"])
app.include_router(screener.router, tags=["screener"])
app.include_router(shockers.router, tags=["shockers"])
app.include_router(divergence.router, tags=["divergence"])
app.include_router(flow.router, tags=["flow"])
app.include_router(smartmoney.router, tags=["smartmoney"])
app.include_router(market.router, tags=["market"])
app.include_router(icici.router, tags=["icici"])


# ---------------------------------------------------------------------------
# Static files + index.html
# ---------------------------------------------------------------------------
_static_dir = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
def serve_index():
    index = _static_dir / "index.html"
    if not index.exists():
        return HTMLResponse("<h1>index.html not found</h1>", status_code=404)
    return HTMLResponse(index.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="OC Dashboard")
    parser.add_argument("--db", default=os.getenv("OC_DB", "oc.duckdb"),
                        help="Path to DuckDB file")
    parser.add_argument("--port", type=int, default=int(os.getenv("OC_PORT", "8000")))
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    config.DB_FILE = str(Path(args.db).resolve())
    print(f"DB: {config.DB_FILE}")
    print(f"Port: {args.port}")

    uvicorn.run(
        "oc_dashboard.app:app",
        host=args.host,
        port=args.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
