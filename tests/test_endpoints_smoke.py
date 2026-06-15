"""
Endpoint smoke tests against a small synthetic DuckDB fixture.
Catches import errors, NaN-serialisation, SQL bugs (double-AND, etc.) —
the regression class that broke during the refactor.
"""
import sys, pathlib, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import duckdb
import numpy as np
import pytest


@pytest.fixture(scope="module")
def synth_db():
    """Build a tiny oc.duckdb-like fixture with a couple of symbols/dates."""
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "test_oc.duckdb")
    con = duckdb.connect(db)
    # Minimal ocdata with the columns the endpoints read
    con.execute("""
        CREATE TABLE ocdata (
            Id VARCHAR, timestamp TIMESTAMP, symbol VARCHAR, expiry DATE,
            strike_price DOUBLE, underlying_price DOUBLE, fut_price DOUBLE,
            days_to_expiry INTEGER, atm_strike DOUBLE, distance_from_atm DOUBLE,
            lotsize INTEGER, ce_oi DOUBLE, pe_oi DOUBLE, ce_volume DOUBLE,
            pe_volume DOUBLE, ce_ltp DOUBLE, pe_ltp DOUBLE, ce_iv DOUBLE, pe_iv DOUBLE,
            ce_delta DOUBLE, pe_delta DOUBLE, ce_gamma DOUBLE, pe_gamma DOUBLE,
            ce_bid_ask_spread DOUBLE, pe_bid_ask_spread DOUBLE,
            net_gexv DOUBLE, ce_gexv DOUBLE, pe_gexv DOUBLE,
            ce_vanna_ex DOUBLE, pe_vanna_ex DOUBLE, net_vanna_ex DOUBLE, net_charm_ex DOUBLE,
            ce_nd2 DOUBLE, pe_nd2 DOUBLE, m_volatility DOUBLE,
            expected_move_straddle DOUBLE, expected_move_theoretical DOUBLE,
            ce_delta_oi DOUBLE, pe_delta_oi DOUBLE,
            ce_delta_oi_chg DOUBLE, pe_delta_oi_chg DOUBLE,
            ce_prem_oi DOUBLE, pe_prem_oi DOUBLE,
            ce_prem_oi_chg DOUBLE, pe_prem_oi_chg DOUBLE,
            ce_time_value DOUBLE, pe_time_value DOUBLE,
            ce_oi_change DOUBLE, pe_oi_change DOUBLE,
            ce_theta DOUBLE, pe_theta DOUBLE, ce_vega DOUBLE, pe_vega DOUBLE,
            ce_rho DOUBLE, pe_rho DOUBLE, ce_vanna DOUBLE, pe_vanna DOUBLE,
            ce_charm DOUBLE, pe_charm DOUBLE
        )
    """)
    rows = []
    for sym in ["NIFTY", "RELIANCE"]:
        spot = 23400 if sym == "NIFTY" else 2800
        for k in range(int(spot*0.9), int(spot*1.1), 50):
            net_gex = (k - spot) * 1000.0
            rows.append((
                f"{sym}-{k}", "2026-06-05 15:29:00", sym, "2026-06-25",
                float(k), float(spot), float(spot+50), 20, float(spot), float(k-spot),
                50, 10000.0, 12000.0, 500.0, 600.0, 100.0, 90.0, 15.0, 16.0,
                0.5, -0.5, 0.0005, 0.0005, 1.0, 1.0,
                net_gex, abs(net_gex)*0.6, -abs(net_gex)*0.4,
                100.0, -120.0, -20.0, -5.0, 0.5, 0.5, 14.0,
                50.0, 48.0,            # expected_move straddle/theoretical
                5000.0, -6000.0,       # ce/pe_delta_oi
                500.0, -600.0,         # ce/pe_delta_oi_chg
                1.0e6, 1.08e6,         # ce/pe_prem_oi
                1.0e5, 1.1e5,          # ce/pe_prem_oi_chg
                30.0, 28.0,            # ce/pe_time_value
                100.0, 120.0,          # ce/pe_oi_change
                -5.0, -4.0, 10.0, 9.0, 1.0, 1.0,   # theta/vega/rho
                0.01, -0.01, -2.0, 2.0,            # vanna/charm
            ))
    con.executemany(
        "INSERT INTO ocdata VALUES (" + ",".join(["?"]*59) + ")", rows)
    con.close()
    return db


@pytest.fixture(scope="module")
def client(synth_db):
    """FastAPI TestClient pointed at the synthetic DB."""
    from oc_dashboard import config
    config.DB_FILE = synth_db
    from fastapi.testclient import TestClient
    from oc_dashboard.app import app
    return TestClient(app)


# ── Smoke tests: each endpoint returns 200 + valid JSON ────────────
def test_symbols(client):
    r = client.get("/api/symbols")
    assert r.status_code == 200
    assert "NIFTY" in str(r.json())

def test_expiries(client):
    r = client.get("/api/expiries?symbol=NIFTY&future_only=false")
    assert r.status_code == 200

def test_timestamps_no_nan_crash(client):
    # This was the NaN-serialisation regression
    r = client.get("/api/timestamps?symbol=NIFTY")
    assert r.status_code == 200

def test_gex_chart(client):
    r = client.get("/api/gex?symbol=NIFTY&expiry=2026-06-25&timestamp=2026-06-05 15:29")
    assert r.status_code in (200, 404)   # 404 ok if filter excludes all

def test_overview(client):
    r = client.get("/api/overview")
    assert r.status_code == 200

def test_delta_screener_no_double_and(client):
    # The double-AND SQL bug would 500 here
    r = client.get("/api/delta_screener?target_delta=30&min_delta=5&filter_type=all")
    assert r.status_code in (200, 404)


# ── Exposure screener (needs exposure_eod) ─────────────────────────
def test_exposure_screener_graceful_without_table(client):
    # Table doesn't exist in fixture → should 404 gracefully, not 500
    r = client.get("/api/exposure_screener")
    assert r.status_code == 404

def test_signals_meta(client):
    r = client.get("/api/exposure_screener/signals_meta")
    assert r.status_code == 200
    body = r.json()
    assert any(s["key"] == "crash_risk" for s in body["signals"])


def test_overview_meta(client):
    r = client.get("/api/overview_meta")
    assert r.status_code == 200
    body = r.json()
    assert "snapshot_ts" in body and "signal_summary" in body

def test_oi_walls_all(client):
    r = client.get("/api/oi_walls?filter_type=all")
    assert r.status_code in (200, 404)

def test_oi_walls_index_no_ambiguous_symbol(client):
    # The ambiguous-symbol bug 500'd specifically on index/stock filter
    r = client.get("/api/oi_walls?filter_type=index")
    assert r.status_code in (200, 404)   # must NOT be 500

def test_oi_walls_stock_no_ambiguous_symbol(client):
    r = client.get("/api/oi_walls?filter_type=stock")
    assert r.status_code in (200, 404)
