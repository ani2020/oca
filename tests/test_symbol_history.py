"""Smoke tests for the Symbol History (Trend) endpoints against a synthetic
exposure_eod fixture. Mirrors the stored schema (Stage-1 + Stage-2 columns)."""
import sys, pathlib, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import duckdb
import pytest
from datetime import date, timedelta


@pytest.fixture(scope="module")
def hist_db():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "hist_oc.duckdb")
    con = duckdb.connect(db)
    con.execute("""
        CREATE TABLE exposure_eod (
            symbol VARCHAR, date DATE, expiry DATE, dte INTEGER,
            fut_price DOUBLE, spot DOUBLE, expected_move DOUBLE,
            atm_iv DOUBLE, atm_iv_smoothed DOUBLE, iv_change DOUBLE,
            basis DOUBLE, basis_pct DOUBLE, basis_annualized DOUBLE, basis_chg DOUBLE,
            gamma_flip DOUBLE, flip_norm_distance DOUBLE,
            net_gex DOUBLE, gex_regime VARCHAR,
            net_gex_sign VARCHAR, net_gex_norm DOUBLE,
            transition_width_norm DOUBLE, neg_gamma_fraction DOUBLE,
            ce_vanna DOUBLE, pe_vanna DOUBLE, net_vanna DOUBLE,
            oi_turnover_ratio DOUBLE, confidence VARCHAR,
            flip_velocity DOUBLE, signals VARCHAR,
            regime_compression BOOLEAN, compression_release BOOLEAN, compression_days INTEGER,
            days_in_regime INTEGER, days_since_flip INTEGER,
            next_day_realized_move DOUBLE
        )
    """)
    base = date(2026, 6, 11)
    for i in range(9):
        d = base + timedelta(days=i)
        bpct = [0.30, 0.22, 0.15, 0.08, 0.02, -0.03, 0.18, 0.25, 0.30][i]
        reg = "positive" if i < 4 else "all_positive"
        con.execute(
            "INSERT INTO exposure_eod (symbol,date,expiry,dte,fut_price,spot,"
            "expected_move,atm_iv,atm_iv_smoothed,iv_change,basis,basis_pct,"
            "basis_annualized,basis_chg,gamma_flip,flip_norm_distance,net_gex,"
            "gex_regime,net_gex_sign,net_gex_norm,transition_width_norm,"
            "neg_gamma_fraction,ce_vanna,pe_vanna,net_vanna,oi_turnover_ratio,"
            "confidence,flip_velocity,signals,regime_compression,"
            "compression_release,compression_days,days_in_regime,days_since_flip,"
            "next_day_realized_move) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ["BANKINDIA", d, date(2026, 6, 25), (date(2026,6,25)-d).days,
             600+i*4+bpct, 600+i*4, 5.0, 28-i, 28-i, (-0.9 if i else 0.0),
             round((600+i*4+bpct)-(600+i*4),4), bpct,
             round(bpct*365/max((date(2026,6,25)-d).days,1),3), (0.1 if i else None),
             612+i*2, 0.2+i*0.1, 0.1+i*0.09, reg, "positive", 0.1+i*0.09,
             0.7-i*0.04, 0.45-i*0.04, 120+i*5, -80+i*3, 40.0, 0.3, "high",
             0.0, ("pin_strengthening" if reg=="all_positive" else ""),
             False, False, 0, i+1, i, (-6.2 if i==8 else 0.5)])
    con.close()
    return db


@pytest.fixture(scope="module")
def client(hist_db):
    from oc_dashboard import config
    config.DB_FILE = hist_db
    from fastapi.testclient import TestClient
    from oc_dashboard.app import app
    return TestClient(app)


def test_symbols(client):
    r = client.get("/api/symbol_history/symbols")
    assert r.status_code == 200
    assert "BANKINDIA" in r.json()["symbols"]

def test_dates_per_symbol(client):
    r = client.get("/api/symbol_history/dates?symbol=BANKINDIA")
    assert r.status_code == 200
    dates = r.json()["dates"]
    assert len(dates) == 9
    assert dates[0] == "2026-06-19"          # most-recent first, clean YYYY-MM-DD

def test_metrics_meta_includes_strength(client):
    r = client.get("/api/symbol_history/metrics_meta")
    assert r.status_code == 200
    keys = {m["key"] for m in r.json()["metrics"]}
    assert "strength_score" in keys

def test_history_symbol_keyed_dict(client):
    r = client.get("/api/symbol_history?symbol=BANKINDIA"
                   "&date_from=2026-06-11&date_to=2026-06-19")
    assert r.status_code == 200
    body = r.json()
    # symbol-keyed dict, NOT a bare array (multi-symbol ready)
    assert body["symbols"] == ["BANKINDIA"]
    assert "BANKINDIA" in body["series"]
    rows = body["series"]["BANKINDIA"]
    assert len(rows) == 9

def test_history_strength_cumulative_climbs(client):
    r = client.get("/api/symbol_history?symbol=BANKINDIA"
                   "&date_from=2026-06-11&date_to=2026-06-19")
    rows = r.json()["series"]["BANKINDIA"]
    # first row has no prior → 0; cumulative should be monotonic-ish & positive
    assert rows[0]["strength_score"] == 0
    assert rows[-1]["strength_cumulative"] > rows[0]["strength_cumulative"]
    # each row carries the axis breakdown
    assert "strength_axes" in rows[-1]

def test_history_basis_deadzone_flag(client):
    r = client.get("/api/symbol_history?symbol=BANKINDIA"
                   "&date_from=2026-06-11&date_to=2026-06-19")
    rows = r.json()["series"]["BANKINDIA"]
    # the -0.03 print (index 5) must be flagged in-zone
    jun16 = next(x for x in rows if x["date"] == "2026-06-16")
    assert jun16["basis_in_deadzone"] is True
    # a real basis (0.30) must be out of zone
    jun11 = next(x for x in rows if x["date"] == "2026-06-11")
    assert jun11["basis_in_deadzone"] is False

def test_history_regime_ramp_present(client):
    r = client.get("/api/symbol_history?symbol=BANKINDIA"
                   "&date_from=2026-06-11&date_to=2026-06-19")
    ramp = r.json()["regime_ramp"]
    assert "positive" in ramp and "all_positive" in ramp
    assert ramp["all_positive"]["order"] < ramp["positive"]["order"]

def test_history_default_window(client):
    # no dates → anchor TO=latest data date, FROM=TO-7d (clean YYYY-MM-DD)
    r = client.get("/api/symbol_history?symbol=BANKINDIA")
    body = r.json()
    assert body["date_to"] == "2026-06-19"
    assert body["date_from"] == "2026-06-12"

def test_history_empty_symbol_400(client):
    assert client.get("/api/symbol_history?symbol=").status_code == 400

def test_history_unknown_symbol_empty_series(client):
    r = client.get("/api/symbol_history?symbol=NOPE"
                   "&date_from=2026-06-01&date_to=2026-06-19")
    assert r.status_code == 200
    assert r.json()["series"]["NOPE"] == []

def test_history_404_without_table(tmp_path):
    # graceful 404 when exposure_eod absent (not 500)
    from oc_dashboard import config
    empty = str(tmp_path / "empty.duckdb")
    duckdb.connect(empty).close()
    config.DB_FILE = empty
    from fastapi.testclient import TestClient
    from oc_dashboard.app import app
    c = TestClient(app)
    assert c.get("/api/symbol_history?symbol=X").status_code == 404
