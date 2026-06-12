"""Stage-2 derived-metrics smoke test: full Stage1→Stage2 pipeline on synthetic data."""
import sys, pathlib, tempfile, os, datetime, re
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import duckdb
import pytest


@pytest.fixture(scope="module")
def staged_db():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "stage.duckdb")
    con = duckdb.connect(db)
    cols = """symbol VARCHAR, expiry DATE, timestamp TIMESTAMP, strike_price DOUBLE,
    fut_price DOUBLE, underlying_price DOUBLE, atm_strike DOUBLE, distance_from_atm DOUBLE,
    days_to_expiry INTEGER, ce_iv DOUBLE, ce_oi DOUBLE, pe_oi DOUBLE, ce_volume DOUBLE,
    pe_volume DOUBLE, ce_ltp DOUBLE, pe_ltp DOUBLE, ce_bid_ask_spread DOUBLE,
    pe_bid_ask_spread DOUBLE, ce_gamma DOUBLE, pe_gamma DOUBLE, lotsize INTEGER,
    ce_oi_change DOUBLE, pe_oi_change DOUBLE, net_gexv DOUBLE, ce_vanna_ex DOUBLE,
    pe_vanna_ex DOUBLE, net_vanna_ex DOUBLE, net_charm_ex DOUBLE"""
    con.execute(f"CREATE TABLE ocdata ({cols})")
    base = datetime.date(2026, 6, 1)
    days = [base + datetime.timedelta(days=i) for i in range(8)
            if (base + datetime.timedelta(days=i)).weekday() < 5]
    for di, day in enumerate(days):
        spot = 23400 + di * 30
        for k in range(int(spot * 0.92), int(spot * 1.08), 50):
            ng = (k - (spot + 50)) * 1000.0
            con.execute(
                "INSERT INTO ocdata VALUES (" + ",".join(["?"] * 28) + ")",
                ['NIFTY', '2026-06-25', f'{day} 15:29:00', float(k),
                 float(spot + 50), float(spot), float(spot), float(k - spot),
                 20 - di, 15.0 - di * 0.2, 10000.0, 12000.0, 500.0, 600.0,
                 100.0, 90.0, 1.0, 1.0, 0.0005, 0.0005, 50, 500.0, -600.0,
                 ng, 100.0, -120.0, -20.0, -5.0])
    sys.path.append(r"c:\users\aniru\od")
    import exposure_eod as m
    schema = open(pathlib.Path(__file__).resolve().parent.parent /
                  'create_exposure_eod.sql').read()
    con.execute(re.search(r'CREATE TABLE IF NOT EXISTS exposure_eod \(.*?\n\);',
                          schema, re.DOTALL).group(0))
    m.TABLE = 'ocdata'
    iv_hist, prev_rows = {}, {}
    for d in [str(x) for x in days]:
        m._store(con, m.process_date(con, ['NIFTY'], d, iv_hist, prev_rows))
    m.fill_forward_outcomes(con)
    m.run_stage2(con)
    return con


def test_days_in_regime_counts_up(staged_db):
    rows = staged_db.execute(
        "SELECT days_in_regime FROM exposure_eod ORDER BY date").fetchall()
    counts = [r[0] for r in rows]
    # Same regime throughout → strictly increasing 1,2,3...
    assert counts == sorted(counts)
    assert counts[0] == 1

def test_days_since_flip_is_regime_minus_one(staged_db):
    rows = staged_db.execute(
        "SELECT days_in_regime, days_since_flip FROM exposure_eod ORDER BY date"
    ).fetchall()
    for dir_, dsf in rows:
        assert dsf == dir_ - 1

def test_oi_turnover_populated(staged_db):
    r = staged_db.execute(
        "SELECT COUNT(*) FROM exposure_eod WHERE oi_turnover_ratio IS NOT NULL"
    ).fetchone()[0]
    assert r > 0

def test_compression_flags_not_null(staged_db):
    # Stage 2 sets FALSE not NULL for insufficient-history rows
    n = staged_db.execute(
        "SELECT COUNT(*) FROM exposure_eod WHERE regime_compression IS NULL"
    ).fetchone()[0]
    assert n == 0

def test_signals_recomputed(staged_db):
    # at least some rows have signals after Stage 2
    n = staged_db.execute(
        "SELECT COUNT(*) FROM exposure_eod WHERE signals != ''").fetchone()[0]
    assert n > 0
