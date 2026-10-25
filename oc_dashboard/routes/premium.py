"""
premium.py -- PREMIUM page: strike richness flags, richness heatmap, participant positioning.

Reads
  richness.duckdb   richness_ref / richness_meta, built by oc/build_richness_ref.py
                    (weekly, post-close). Separate-DB pattern as vrp.py / rv_forecast.py.
  ocdata (tbl())    the live chain for the selected symbol/expiry/snapshot.
  participant.duckdb  participant_oi mirror written by nse_participant_oi.py (EOD), next
                    to config.SPAN_DB. Never bhavcopy.duckdb directly (15 GB; see _part_db).
All read-only.

STRIKE FLAGS -- what they mean
  Each live strike is mapped to the reference cell it would have been sold in:
    kz   = ln(K/F) / (sigma*sqrt(T)), F from ATM put-call parity, sigma*sqrt(T) from
           the ATM straddle (same construction as the reference), rounded to 0.25
    tdte = the reference entry-DTE bucket containing today's calendar DTE
  and gets that cell's HISTORICAL hold-to-expiry, delta-hedged, net-of-cost result:
    RICH   t >= 3, >= 20%/yr on SPAN margin, positive in both halves of the sample
    GOOD   t >= 2, >= 10%/yr, positive in both halves
    FAIR   positive, but weaker evidence
    WEAK   negative in one half, or t < 1
    AVOID  the cell lost money net of costs
  These describe the CELL's history, not a forecast for this trade. On top, the live
  premium is scored against the cell's usual premium (pn percentile): RICHER / CHEAPER
  than usual. History starts after the March-2020 crash, so the left tail is
  under-sampled; every flag is for a naked short leg and its tail risk is real
  (worst trades lost 20-65% of margin).

POSITIONING
  Aggregate index-options and index-futures OI by participant (Client/DII/FII/Pro).
  Market-level only: NSE does not split it by underlying, expiry or strike, so it is
  NOT used to re-sign per-strike GEX -- with a uniform-mix assumption a per-strike
  participant GEX is just the existing call/put gamma profiles reweighted by two
  daily scalars, i.e. no strike information (decided 2026-10-12).

Tested 2026-10-12 with FastAPI TestClient on synthetic fixtures (all endpoints,
missing-reference 404, bad-metric 400) and the PREMIUM view in jsdom.
"""
from __future__ import annotations

import math
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import duckdb
import numpy as np
import pandas as pd
from fastapi import APIRouter, HTTPException, Query

from ..db import qdf, tbl, safe_response, ts_filter_clause, latest_ts, to_records
from .. import config

router = APIRouter()

REF_DTES = [1, 2, 3, 4, 5, 7, 10, 14, 21, 28]
# Single stocks (2026-10-14): monthly expiries only, so their own DTE grid. Flags
# come from the POOLED reference over all F&O stocks ('_STOCKS'), because ~75
# monthly expiries per stock rarely reach t >= 3; each stock's own cell (n >= 24)
# is shown beside it. Cells are split by ev: results inside [entry, expiry] (ev=1)
# is a different trade (ATM legs lost ~9 bps on large caps with results vs +9
# without), so such strikes show RESULTS and the ev=1 history, never a flag.
STOCK_DTES = [3, 7, 10, 14, 21, 28]
INDEX_SYMBOLS = {"NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY", "NIFTYNXT50", "SENSEX", "BANKEX"}
POOLED = "_STOCKS"
HEAT_METRICS = {"rom_annual_pct", "hedged_bps", "hedged_t", "capture", "payoff_ratio", "pn_p50"}
PARTICIPANTS = ["Client", "DII", "FII", "Pro"]
Z_WINDOW = 60


# ----------------------------------------------------------------- helpers
def _rich_db() -> Path:
    return Path(config.DB_FILE).with_name("richness.duckdb")


def _part_db() -> Path:
    # Small mirror of bhavcopy.participant_oi written by nse_participant_oi.py.
    # NOT bhavcopy.duckdb itself: that file is ~15 GB with primary-key indexes on
    # ~170M rows, and opening it per request pegged the CPU (2026-10-12) to read a
    # 15k-row table.
    return Path(config.SPAN_DB).with_name("participant.duckdb")


_POS_CACHE: dict = {}   # kind -> (participant.duckdb mtime, derived frame)


def _read(path: Path, sql: str, params: list = (), attach_as: Optional[str] = None) -> pd.DataFrame:
    if not Path(path).exists():
        raise HTTPException(404, f"{Path(path).name} not found")
    try:
        con = duckdb.connect(str(path), read_only=True)
    except Exception as exc:  # Windows exclusive lock while a batch step writes it
        raise HTTPException(503, f"{Path(path).name} is busy (being written); retry shortly: {exc}")
    try:
        return con.execute(sql, list(params)).df()
    except duckdb.CatalogException as exc:
        raise HTTPException(404, f"table missing in {Path(path).name}: {exc}")
    except Exception as exc:
        raise HTTPException(500, str(exc))
    finally:
        con.close()


def flag(r) -> str:
    """Cell flag from its historical stats (see module docstring)."""
    hb, t, rom = r.get("hedged_bps"), r.get("hedged_t"), r.get("rom_annual_pct")
    h1, h2 = r.get("hedged_h1"), r.get("hedged_h2")
    if hb is None or (isinstance(hb, float) and math.isnan(hb)):
        return "NONE"
    if hb <= 0:
        return "AVOID"
    stable = (h1 is not None and h2 is not None and not math.isnan(h1) and not math.isnan(h2)
              and h1 > 0 and h2 > 0)
    t = t if t is not None and not math.isnan(t) else 0.0
    rom = rom if rom is not None and not math.isnan(rom) else 0.0
    if stable and t >= 3 and rom >= 20:
        return "RICH"
    if stable and t >= 2 and rom >= 10:
        return "GOOD"
    if not stable or t < 1:
        return "WEAK"
    return "FAIR"


def dte_bucket(dte: int, dtes: list = REF_DTES) -> tuple[Optional[int], bool]:
    """Reference entry bucket for a calendar DTE: (bucket, approximate?).
    Reference trades enter at DTE in [tdte, tdte+3]; outside every window the nearest
    bucket is used and flagged approximate. DTE 0 or > 31 -> no bucket."""
    if dte is None or dte < 1 or dte > 31:
        return None, False
    exact = [b for b in dtes if b <= dte <= b + 3]
    if exact:
        return max(exact), False
    return min(dtes, key=lambda b: abs(b - dte)), True


def pn_percentile(pn: float, q: dict) -> Optional[float]:
    """Interpolated percentile of pn within the cell's stored quantiles (5..95)."""
    xs = [q.get("pn_p10"), q.get("pn_p25"), q.get("pn_p50"), q.get("pn_p75"), q.get("pn_p90")]
    if pn is None or any(x is None or (isinstance(x, float) and math.isnan(x)) for x in xs):
        return None
    ps = [10, 25, 50, 75, 90]
    if pn <= xs[0]:
        return 5.0
    if pn >= xs[-1]:
        return 95.0
    return float(np.interp(pn, xs, ps))


def _ref(symbol: str) -> tuple[pd.DataFrame, dict]:
    ref = _read(_rich_db(), "SELECT * FROM richness_ref WHERE symbol = ?", [symbol])
    meta = _read(_rich_db(), "SELECT * FROM richness_meta WHERE symbol = ?", [symbol])
    if "ev" not in ref.columns:
        ref["ev"] = 0
    ref["ev"] = ref["ev"].fillna(0).astype(int)
    return ref, (meta.iloc[0].to_dict() if not meta.empty else {})


def _is_stock(symbol: str) -> bool:
    return symbol not in INDEX_SYMBOLS


def _results_in_window(symbol: str, expiry: str) -> Optional[bool]:
    """exposure_eod.near_earnings for this symbol/expiry on its latest date (a
    results date falls before expiry). From oc.duckdb -- never bhavcopy per request."""
    try:
        r = qdf("SELECT near_earnings FROM exposure_eod WHERE symbol = ? AND expiry = ? "
                "ORDER BY date DESC LIMIT 1", [symbol, expiry[:10]])
    except Exception:  # noqa: BLE001
        return None
    if r.empty or pd.isna(r.iloc[0, 0]):
        return None
    return bool(r.iloc[0, 0])


# ----------------------------------------------------------------- endpoints
@router.get("/api/premium/meta")
def premium_meta():
    out = {"richness": [], "stocks": [], "positioning": None}
    try:
        m = _read(_rich_db(), "SELECT symbol, built_at, d0, d1, n_cells, n_trades FROM richness_meta ORDER BY symbol")
        out["richness"] = to_records(m)
    except HTTPException:
        pass
    # Every stock with a live chain is flaggable from the pooled reference, even when
    # it has too little history for cells of its own (richness_meta lists only those:
    # ~80 names added to F&O since late 2024 have < 30 expiries). (2026-10-14)
    if any(r.get("symbol") == POOLED for r in out["richness"]):
        try:
            s = qdf("SELECT DISTINCT symbol FROM exposure_eod "
                    "WHERE date = (SELECT max(date) FROM exposure_eod) ORDER BY symbol")
            out["stocks"] = [x for x in s["symbol"].tolist() if x not in INDEX_SYMBOLS]
        except Exception:  # noqa: BLE001
            pass
    try:
        p = _read(_part_db(), "SELECT min(trade_date) AS d0, max(trade_date) AS d1, count(DISTINCT trade_date) AS n "
                              "FROM participant_oi WHERE kind = 'oi'")
        out["positioning"] = to_records(p)[0]
    except HTTPException:
        pass
    return safe_response(out)


@router.get("/api/premium/heatmap")
def premium_heatmap(symbol: str = Query("NIFTY"), metric: str = Query("rom_annual_pct")):
    if metric not in HEAT_METRICS:
        raise HTTPException(400, f"metric must be one of {sorted(HEAT_METRICS)}")
    ref, meta = _ref(symbol)
    if ref.empty:
        raise HTTPException(404, f"no richness reference for {symbol}; run oc/build_richness_ref.py")
    ref = ref[ref.ev == 0].copy()          # the map shows ordinary windows; results cells are warnings
    ref["flag"] = [flag(r) for r in ref.to_dict("records")]
    # puts on the left (negative kz), calls on the right; at ATM the frontend
    # averages the two legs and lists both in the hover
    cols = ["t", "tdte", "kz", "n", "hedged_bps", "hedged_t", "rom_annual_pct", "capture", "payoff_ratio",
            "hedged_h1", "hedged_h2", "worst_rom_pct", "hold_days", "pn_p50", "flag"]
    dtes = STOCK_DTES if (symbol == POOLED or _is_stock(symbol)) else REF_DTES
    return safe_response({"symbol": symbol, "metric": metric, "meta": meta,
                          "dtes": dtes, "cells": to_records(ref[cols])})


@router.get("/api/premium/chain")
def premium_chain(symbol: str = Query(...), expiry: str = Query(...),
                  timestamp: Optional[str] = Query(None), max_k: float = Query(3.0, ge=0.5, le=3.0)):
    stock = _is_stock(symbol)
    if stock:
        ref, meta = _ref(POOLED)
        own, _ = _ref(symbol)
        ev = 1 if _results_in_window(symbol, expiry) else 0
    else:
        ref, meta = _ref(symbol)
        own, ev = None, 0
    if ref.empty:
        raise HTTPException(404, f"no richness reference for {POOLED if stock else symbol}; "
                                 "run oc/build_richness_ref.py")
    ts = timestamp or latest_ts(symbol)
    ts_clause, ts_params = ts_filter_clause(ts)
    df = qdf(f"""
        SELECT strike_price AS k, ce_ltp, pe_ltp, ce_iv, pe_iv, ce_oi, pe_oi,
               ce_volume, pe_volume, ce_bid, ce_ask, pe_bid, pe_ask,
               underlying_price AS spot, lotsize
        FROM {tbl()}
        WHERE symbol = ? AND expiry = ? {ts_clause}
        ORDER BY strike_price
    """, [symbol, expiry[:10]] + ts_params)
    if df.empty:
        raise HTTPException(404, "no chain for these filters")
    df = df.groupby("k", as_index=False).last()        # one row per strike if the minute repeats
    spot = float(df["spot"].median())
    lot = int(df["lotsize"].dropna().iloc[0]) if df["lotsize"].notna().any() else None
    both = df[(df.ce_ltp > 0) & (df.pe_ltp > 0)]
    if both.empty:
        raise HTTPException(404, "no strike with both legs priced")
    atm = both.iloc[(both.k - spot).abs().argmin()]
    fwd = float(atm.k + atm.ce_ltp - atm.pe_ltp)
    straddle = float(atm.ce_ltp + atm.pe_ltp)
    snap_day = datetime.strptime(ts[:10], "%Y-%m-%d").date() if ts else date.today()
    dte = (datetime.strptime(expiry[:10], "%Y-%m-%d").date() - snap_day).days
    bucket, approx = dte_bucket(dte, STOCK_DTES if stock else REF_DTES)
    sT = straddle / (fwd * math.sqrt(2 / math.pi))
    sigma = sT / math.sqrt(max(dte, 1) / 365.0)

    refi = ref[ref.ev == ev].set_index(["t", "tdte", "kz"]) if bucket is not None else None
    owni = (own[own.ev == ev].set_index(["t", "tdte", "kz"])
            if stock and own is not None and not own.empty and bucket is not None else None)
    rows = []
    for r in df.itertuples():
        kz = math.log(r.k / fwd) / sT
        if abs(kz) > max_k + 0.125:
            continue
        for t, ltp, iv, oi, vol, bid, ask in (
                ("CE", r.ce_ltp, r.ce_iv, r.ce_oi, r.ce_volume, r.ce_bid, r.ce_ask),
                ("PE", r.pe_ltp, r.pe_iv, r.pe_oi, r.pe_volume, r.pe_bid, r.pe_ask)):
            if (t == "CE" and kz < -0.125) or (t == "PE" and kz > 0.125):
                continue                                 # OTM side only (both at ATM)
            if ltp is None or not ltp > 0:
                continue
            kc = max(-3.0, min(3.0, round(kz * 4) / 4))
            if t == "CE" and kc < 0 or t == "PE" and kc > 0:
                kc = 0.0
            rec = {"strike": r.k, "type": t, "kz": kz, "kz_cell": kc, "ltp": ltp, "iv": iv,
                   "oi": oi, "volume": vol,
                   "spread_pct": (ask - bid) / ltp * 100 if bid and ask and ask > bid else None}
            cell = None
            if refi is not None and (t, bucket, kc) in refi.index:
                cell = refi.loc[(t, bucket, kc)]
                cell = cell.iloc[0] if isinstance(cell, pd.DataFrame) else cell
            if cell is not None:
                c = cell.to_dict()
                pn = ltp / (fwd * sT)
                pct = pn_percentile(pn, c)
                rec.update({k: c.get(k) for k in ("n", "hedged_bps", "hedged_t", "rom_annual_pct",
                                                   "payoff_ratio", "capture", "hedged_h1", "hedged_h2",
                                                   "worst_rom_pct")})
                rec["flag"] = "RESULTS" if ev else flag(c)
                rec["pn"] = pn
                rec["pn_pct"] = pct
                rec["vs_usual"] = ("RICHER" if pct is not None and pct >= 75 else
                                   "CHEAPER" if pct is not None and pct <= 25 else "USUAL")
                rec["edge_per_lot"] = (c["hedged_bps"] / 1e4 * spot * lot) if lot and c.get("hedged_bps") is not None else None
            else:
                rec["flag"] = "NONE"
            if owni is not None and (t, bucket, kc) in owni.index:
                oc_ = owni.loc[(t, bucket, kc)]
                oc_ = (oc_.iloc[0] if isinstance(oc_, pd.DataFrame) else oc_).to_dict()
                rec.update({"own_n": oc_.get("n"), "own_hedged_bps": oc_.get("hedged_bps"),
                            "own_t": oc_.get("hedged_t"), "own_rom": oc_.get("rom_annual_pct"),
                            "own_flag": flag(oc_)})
            rows.append(rec)

    rank = {"RICH": 0, "GOOD": 1, "FAIR": 2, "WEAK": 3, "AVOID": 4, "RESULTS": 5, "NONE": 6}
    # best zones: one strike per (type, cell) -- the one closest to the cell centre --
    # so the list shows distinct opportunities rather than neighbouring strikes
    by_cell = {}
    for r in rows:
        if r["flag"] not in ("RICH", "GOOD"):
            continue
        key = (r["type"], r["kz_cell"])
        if key not in by_cell or abs(r["kz"] - r["kz_cell"]) < abs(by_cell[key]["kz"] - by_cell[key]["kz_cell"]):
            by_cell[key] = r
    best = sorted(by_cell.values(),
                  key=lambda r: (rank[r["flag"]], -(r.get("rom_annual_pct") or 0)))[:6]
    return safe_response({
        "symbol": symbol, "expiry": expiry[:10], "timestamp": ts, "spot": spot, "forward": fwd,
        "atm_strike": float(atm.k), "atm_straddle": straddle, "sigma_ann_pct": sigma * 100,
        "dte": dte, "bucket": bucket, "bucket_approx": approx, "lot": lot,
        "ref_built": meta.get("built_at"), "ref_from": meta.get("d0"), "ref_to": meta.get("d1"),
        "is_stock": stock, "results_in_window": bool(ev), "reference": POOLED if stock else symbol,
        "best": best, "rows": rows,
    })


def _positioning_frame(kind: str) -> pd.DataFrame:
    """Per-participant derived series for one kind, cached until participant.duckdb changes."""
    path = _part_db()
    if not path.exists():
        raise HTTPException(404, "participant.duckdb not found -- run: python nse_participant_oi.py mirror")
    mtime = path.stat().st_mtime
    hit = _POS_CACHE.get(kind)
    if hit and hit[0] == mtime:
        return hit[1]
    raw = _read(path, "SELECT * FROM participant_oi WHERE kind = ? ORDER BY trade_date", [kind])
    if raw.empty:
        raise HTTPException(404, "participant_oi is empty; run nse_participant_oi.py backfill")
    allm = _derive_positioning(raw)
    _POS_CACHE[kind] = (mtime, allm)
    return allm


@router.get("/api/premium/positioning")
def premium_positioning(history: int = Query(250, ge=5, le=2000), kind: str = Query("oi")):
    if kind not in ("oi", "vol"):
        raise HTTPException(400, "kind must be oi or vol")
    allm = _positioning_frame(kind)
    return _positioning_response(allm, kind, history)


def _derive_positioning(raw: pd.DataFrame) -> pd.DataFrame:
    tot = raw[raw.participant == "TOTAL"].set_index("trade_date")
    out = []
    for p in PARTICIPANTS:
        d = raw[raw.participant == p].set_index("trade_date")
        m = pd.DataFrame(index=d.index)
        m["participant"] = p
        m["call_net_pct"] = (d.opt_idx_call_long - d.opt_idx_call_short) / tot.opt_idx_call_long * 100
        m["put_net_pct"] = (d.opt_idx_put_long - d.opt_idx_put_short) / tot.opt_idx_put_long * 100
        m["fut_net_pct"] = (d.fut_idx_long - d.fut_idx_short) / tot.fut_idx_long * 100
        m["call_short_share"] = d.opt_idx_call_short / tot.opt_idx_call_short * 100
        m["put_short_share"] = d.opt_idx_put_short / tot.opt_idx_put_short * 100
        m["fut_net"] = d.fut_idx_long - d.fut_idx_short
        m["call_net"] = d.opt_idx_call_long - d.opt_idx_call_short
        m["put_net"] = d.opt_idx_put_long - d.opt_idx_put_short
        for c in ("call_net_pct", "put_net_pct", "fut_net_pct"):
            m[c + "_chg"] = m[c].diff()
            roll = m[c].rolling(Z_WINDOW, min_periods=20)
            m[c + "_z"] = (m[c] - roll.mean().shift(1)) / roll.std().shift(1)
        out.append(m.reset_index())
    return pd.concat(out, ignore_index=True)


def _positioning_response(allm: pd.DataFrame, kind: str, history: int):
    last_day = allm.trade_date.max()
    days = sorted(allm.trade_date.unique())
    prev_day = days[-2] if len(days) > 1 else None
    today = allm[allm.trade_date == last_day]
    fii = today[today.participant == "FII"].iloc[0]
    hedge_ratio = (fii.put_net / -fii.fut_net) if fii.fut_net < 0 and fii.put_net > 0 else None
    series = allm[allm.trade_date >= days[max(0, len(days) - history)]]
    return safe_response({
        "kind": kind, "date": last_day, "prev_date": prev_day, "n_days": len(days),
        "today": to_records(today),
        "prev": to_records(allm[allm.trade_date == prev_day]) if prev_day is not None else [],
        "fii_put_cover": hedge_ratio,
        "series": to_records(series[["trade_date", "participant", "call_net_pct", "put_net_pct",
                                     "fut_net_pct", "put_short_share", "call_short_share"]]),
    })
