# NSE Options Chain Analytics Dashboard

A fast, browser-based analytics dashboard that reads directly from `oc.duckdb`
— no Excel, no ODBC driver, no intermediate files.

## Structure

```
oc_dashboard/
├── __init__.py
├── __main__.py
└── app.py          ← FastAPI backend (all API endpoints)
    static/
    └── index.html  ← Single-page frontend (Plotly, no build step)
run.py              ← Convenience launcher
requirements.txt
```

## Quick Start

```bash
# 1. Install dependencies (once)
pip install fastapi uvicorn duckdb pandas numpy scipy

# 2. Run from the directory that contains oc.duckdb
python run.py

# Or if oc.duckdb is elsewhere:
python run.py --db /path/to/oc.duckdb

# 3. Open browser
#    http://localhost:8000
```

## Dashboard Views

### OVERVIEW
- Ticker cards for every symbol in the DB showing Spot, PCR, Avg IV, Net GEX
- Aggregate OI change signals table across all tickers
- Click any ticker card to jump to the GEX view for that symbol

### GEX & GAMMA *(replaces Excel pivot → bar chart)*
- **GEX Bar Chart**: CE GEX + PE GEX as stacked bars per strike, Net GEX as line
  ATM and SPOT marked with annotation lines
- **Gamma Profile**: Dealer gamma exposure across a price grid (±N% range).
  Gamma Flip level marked; Magnet Zone shaded
- **KPI strip**: Spot, Gamma Flip, Magnet Center, Magnet Strength
- Filters: Symbol → Expiry → Timestamp → Range %

### OI SIGNALS *(Long Build-Up, Short Covering, etc.)*
- Compares two most-recent timestamps for the selected symbol
- CE OI Change bar chart + PE OI Change bar chart
- Signal table with colour-coded pills: ⬆ LBU / ⬇ SBU / ↩ SC / ↪ LU

### SHOCKERS
- **Volume Shockers**: Strikes with highest volume spike (absolute Δ) vs prior snapshot
- **IV Shockers**: Strikes with largest absolute IV change vs prior snapshot
- Both tabs show a horizontal bar chart + sortable table

### IV SMILE *(reuses oc_analysis.py spline logic)*
- CE and PE IV smile plots with spline fit overlay
- Anomalous strikes (|z-score| > 2) marked with ✕ in accent colour
- SPOT line overlaid for context
- Anomaly table below charts

### MOVERS
- Top gainers and losers by LTP change (CE or PE selectable)
- Bar charts + tables with % change

### STRIKE TREND *(time-series per strike)*
- Any metric over all available timestamps for a given symbol/strike/expiry
- Metrics: LTP, IV, Volume, OI, GEX, Net GEXv, Delta, Gamma, Theta, Vega (CE/PE)

## API Endpoints

All endpoints are at `http://localhost:8000/api/…`

| Endpoint | Description |
|---|---|
| `GET /api/symbols` | All distinct symbols |
| `GET /api/timestamps?symbol=X` | All timestamps for X |
| `GET /api/expiries?symbol=X&timestamp=T` | Expiries for X at T |
| `GET /api/overview` | Latest snapshot summary for all symbols |
| `GET /api/gex?symbol=X&expiry=E&timestamp=T` | GEX per strike |
| `GET /api/gamma_profile?symbol=X&expiry=E&timestamp=T&price_range_pct=5` | Gamma profile |
| `GET /api/oi_change?symbol=X` | OI build-up/covering for X |
| `GET /api/oi_signals_all` | Aggregate OI signals, all tickers |
| `GET /api/volume_shockers?top_n=30` | Top volume spikes |
| `GET /api/iv_shockers?top_n=30` | Top IV spikes |
| `GET /api/iv_smile?symbol=X&expiry=E&timestamp=T` | IV smile + anomaly flags |
| `GET /api/top_movers?side=CE&top_n=20` | Top LTP gainers/losers |
| `GET /api/strike_trend?symbol=X&expiry=E&strike_price=S&metric=ce_ltp` | Time-series |
| `GET /api/snapshot?symbol=X` | Key metrics from latest snapshot |

## CLI Options

```
python run.py --help

  --db PATH       Path to oc.duckdb (default: oc.duckdb in CWD)
  --table NAME    Table name (default: ocdata)
  --host HOST     Bind host (default: 127.0.0.1)
  --port PORT     Port (default: 8000)
```

## Notes

- The DB is opened **read-only** — safe to run while `option_chain_ivm.py` is writing.
- All filtering in the UI cascades: symbol → expiry → timestamp selects are
  populated dynamically from what's actually in the DB.
- Tables in every view are client-side sortable — click any column header.
- The Gamma Profile replicates the `GPC.Process_oc` logic internally so you
  don't need the `GammaProfile` module installed in the same directory.
  If you want the full `gamma_study` analysis (structures, warnings, pin zones),
  run `gamma_study.py` separately and store results — a future tab can display them.
