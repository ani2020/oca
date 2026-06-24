"""OI endpoints — signals, walls, history."""
from __future__ import annotations
import numpy as np
import pandas as pd
from datetime import date, datetime, timedelta
from typing import Optional
from fastapi import APIRouter, HTTPException, Query
from ..db import qdf, tbl, to_records, safe_response, _ts_lo_hi, latest_ts, latest_data_date
from ..cache import cache_get
from .. import config

router = APIRouter()

@router.get("/api/oi_change")
def oi_change(
    symbol:        str   = Query(...),
    filter_type:   str   = Query("all"),
    min_oi_change: int   = Query(0),     # hide rows where |ce+pe oi chg| < this
):
    # Use the DB's ce_oi_change column (NSE's own computed delta vs their last
    # reference) rather than subtracting consecutive snapshots — gives meaningful
    # intraday context rather than tiny 4-minute movements.
    df = qdf(
        f"""
        SELECT
            STRFTIME(timestamp, '%Y-%m-%d %H:%M') AS timestamp,
            strike_price,
            CAST(expiry AS VARCHAR)         AS expiry,
            COALESCE(ce_oi,        0)       AS ce_oi,
            COALESCE(pe_oi,        0)       AS pe_oi,
            COALESCE(ce_oi_change, 0)       AS ce_oi_chg,
            COALESCE(pe_oi_change, 0)       AS pe_oi_chg,
            COALESCE(ce_ltp,       0)       AS ce_ltp,
            COALESCE(pe_ltp,       0)       AS pe_ltp,
            COALESCE(ce_volume,    0)       AS ce_volume,
            COALESCE(pe_volume,    0)       AS pe_volume,
            COALESCE(ce_delta,     0)       AS ce_delta,
            COALESCE(ce_prem_oi_chg, 0)     AS ce_prem_oi_chg,
            COALESCE(pe_prem_oi_chg, 0)     AS pe_prem_oi_chg,
            COALESCE(underlying_price, 0)   AS spot
        FROM {tbl()}
        WHERE symbol = ?
          AND timestamp = (SELECT MAX(timestamp) FROM {tbl()} WHERE symbol = ?)
          AND expiry >= CURRENT_DATE
          AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
        ORDER BY expiry, strike_price
        """,
        [symbol, symbol],
    )
    if df.empty:
        raise HTTPException(404, f"No OI data for {symbol}")

    # Compute LTP signal direction from ce_oi_change sign convention:
    # positive OI change + positive LTP = longs entering
    def _sig(oi, ltp):
        if oi > 0 and ltp > 0: return "Long Build-Up"
        if oi > 0 and ltp < 0: return "Short Build-Up"
        if oi < 0 and ltp > 0: return "Short Covering"
        if oi < 0 and ltp < 0: return "Long Unwinding"
        return "Neutral"

    df["ce_signal"] = df.apply(lambda r: _sig(r["ce_oi_chg"], r["ce_ltp"]), axis=1)
    df["pe_signal"] = df.apply(lambda r: _sig(r["pe_oi_chg"], r["pe_ltp"]), axis=1)

    # Add Vol/OI ratio — high ratio = fresh positioning
    df["ce_vol_oi"] = df.apply(
        lambda r: round(r["ce_volume"] / r["ce_oi"], 3) if r["ce_oi"] > 0 else None, axis=1)
    df["pe_vol_oi"] = df.apply(
        lambda r: round(r["pe_volume"] / r["pe_oi"], 3) if r["pe_oi"] > 0 else None, axis=1)

    # Filter: hide rows where both OI changes are below threshold (pure noise)
    abs_min = max(min_oi_change, 1)  # always filter zero-zero rows
    df = df[
        (df["ce_oi_chg"].abs() >= abs_min) |
        (df["pe_oi_chg"].abs() >= abs_min)
    ]

    # Sort by total absolute OI activity descending
    df["total_abs_oi"] = df["ce_oi_chg"].abs() + df["pe_oi_chg"].abs()
    df = df.sort_values("total_abs_oi", ascending=False).drop(columns=["total_abs_oi"])

    return to_records(df)


@router.get("/api/oi_signals_all")
def oi_signals_all():
    # Single set-based query (was a 2-query-per-symbol loop = ~262 round-trips).
    # DENSE_RANK tags each symbol's two most recent snapshots; aggregate the
    # latest-vs-previous diff for ALL symbols in one pass.
    df = qdf(
        f"""
        WITH ranked AS (
            SELECT symbol, timestamp, strike_price, expiry,
                   COALESCE(ce_oi, 0)  AS ce_oi,
                   COALESCE(pe_oi, 0)  AS pe_oi,
                   COALESCE(ce_ltp, 0) AS ce_ltp,
                   COALESCE(pe_ltp, 0) AS pe_ltp,
                   DENSE_RANK() OVER (PARTITION BY symbol ORDER BY timestamp DESC) AS rk
            FROM {tbl()}
        ),
        two AS (
            SELECT * FROM ranked WHERE rk <= 2
        ),
        -- ensure a symbol actually has two distinct snapshots
        valid AS (
            SELECT symbol FROM two GROUP BY symbol
            HAVING COUNT(DISTINCT timestamp) >= 2
        ),
        agg AS (
            SELECT t.symbol,
                   SUM(CASE WHEN rk=1 THEN ce_oi ELSE 0 END)
                     - SUM(CASE WHEN rk=2 THEN ce_oi ELSE 0 END) AS ce_oi_chg,
                   SUM(CASE WHEN rk=1 THEN pe_oi ELSE 0 END)
                     - SUM(CASE WHEN rk=2 THEN pe_oi ELSE 0 END) AS pe_oi_chg,
                   AVG(CASE WHEN rk=1 THEN ce_ltp END)
                     - AVG(CASE WHEN rk=2 THEN ce_ltp END) AS avg_ce_ltp_chg,
                   AVG(CASE WHEN rk=1 THEN pe_ltp END)
                     - AVG(CASE WHEN rk=2 THEN pe_ltp END) AS avg_pe_ltp_chg,
                   MAX(CASE WHEN rk=1 THEN CAST(timestamp AS VARCHAR) END) AS ts_new
            FROM two t
            JOIN valid v ON t.symbol = v.symbol
            GROUP BY t.symbol
        )
        SELECT symbol, ce_oi_chg, pe_oi_chg, avg_ce_ltp_chg, avg_pe_ltp_chg, ts_new,
               ABS(COALESCE(ce_oi_chg,0)) + ABS(COALESCE(pe_oi_chg,0)) AS total_oi_chg
        FROM agg
        ORDER BY total_oi_chg DESC
        """
    )
    if df.empty:
        return []
    return to_records(df)


@router.get("/api/oi_walls_expiries")
def oi_walls_expiries():
    """Return distinct live expiry dates available in ocdata, for the OI walls expiry selector."""
    rows = qdf(
        f"""
        SELECT DISTINCT CAST(expiry AS VARCHAR) AS expiry
        FROM {tbl()}
        WHERE expiry >= CURRENT_DATE
          AND (ce_oi > 0 OR pe_oi > 0)
        ORDER BY expiry
        """
    )
    if rows.empty:
        return []
    return rows["expiry"].tolist()


@router.get("/api/oi_walls")
def oi_walls(
    filter_type: str = Query("all"),
    shelf_frac: float = Query(0.40, ge=0.05, le=0.95,
                              description="shelf member OI >= this x wall OI (runtime-tunable)"),
    expiry: str = Query(None, description="Fixed expiry date (YYYY-MM-DD). Omit for nearest."),
):
    """
    For each symbol at its latest snapshot (NEAREST live expiry only), find:
    - Call / Put WALL  = single highest CE/PE OI strike (classic resistance/support)
    - Call / Put SHELF = band of adjacent high-OI PREFERRED strikes around the wall
                         (exposure_core.oi_shelf — handles the round-number effect
                         and NSE's irregular strike lattice). Carries lo/hi/CoM,
                         aggregate build-up signal, and day-over-day CoM migration.
    - GAMMA WALL       = strike with highest |net gamma| (net_gexv). Often DIFFERENT
                         from the OI wall (e.g. KOTAKBANK OI wall 400 vs gamma wall
                         405) — that divergence is itself the signal: the gamma
                         wall is where dealer hedging actually pins/resists.
    - Distances from spot/fut, wall strength, symbol-wide PCR, wall range.

    Expiry is pinned to each symbol's NEAREST live expiry (MIN(expiry) >= today),
    computed per symbol — indices keep their weekly, monthly stocks their monthly.
    This differs from the legacy all-expiry blend (more correct: the pinning
    happens where the hedging grip actually is).

    shelf_frac is runtime-tunable for live observation; bake the settled value
    into exposure_core.OI_SHELF_DEFAULTS later.
    """
    import exposure_core as core

    idx_set = set(config.NSE_INDICES)
    idx_list = ", ".join(f"'{s}'" for s in config.NSE_INDICES)
    sym_filter = (
        f"AND t.symbol IN ({idx_list})"       if filter_type == "index"
        else f"AND t.symbol NOT IN ({idx_list})" if filter_type == "stock"
        else ""
    )
    # When a fixed expiry is requested, pin all symbols to that date.
    # Otherwise fall back to each symbol's nearest live expiry (original behaviour).
    expiry_clause = f"AND t.expiry = '{expiry[:10]}'" if expiry else "AND t.expiry >= CURRENT_DATE"
    nearest_cte = (
        f"SELECT symbol, CAST('{expiry[:10]}' AS DATE) AS exp FROM ranked WHERE rk = 1 GROUP BY symbol"
        if expiry
        else "SELECT symbol, MIN(expiry) AS exp FROM ranked WHERE rk = 1 GROUP BY symbol"
    )
    # Pull the full per-strike ladder for each symbol's latest snapshot, pinned to
    # the chosen expiry, plus the previous snapshot's LTP for the day-over-day
    # build-up signal. Shelf/gamma-wall maths happen in Python.
    df = qdf(
        f"""
        WITH ranked AS (
            SELECT t.*,
                   DENSE_RANK() OVER (PARTITION BY t.symbol ORDER BY t.timestamp DESC) AS rk
            FROM {tbl()} t
            WHERE (t.ce_oi > 0 OR t.pe_oi > 0)
              {expiry_clause}
            {sym_filter}
        ),
        nearest AS (
            -- each symbol's pinned expiry (fixed or nearest live)
            {nearest_cte}
        ),
        cur AS (
            SELECT r.symbol, r.strike_price,
                   COALESCE(r.ce_oi,0)            AS ce_oi,
                   COALESCE(r.pe_oi,0)            AS pe_oi,
                   COALESCE(r.ce_ltp,0)           AS ce_ltp,
                   COALESCE(r.pe_ltp,0)           AS pe_ltp,
                   COALESCE(r.ce_iv,0)            AS ce_iv,
                   COALESCE(r.pe_iv,0)            AS pe_iv,
                   COALESCE(r.ce_oi_change,0)     AS ce_oi_change,
                   COALESCE(r.pe_oi_change,0)     AS pe_oi_change,
                   COALESCE(r.net_gexv,0)         AS net_gexv,
                   COALESCE(r.ce_gexv,0)          AS ce_gexv,
                   COALESCE(r.pe_gexv,0)          AS pe_gexv,
                   COALESCE(r.underlying_price,0) AS spot,
                   COALESCE(r.fut_price,0)        AS fut_price,
                   COALESCE(r.atm_strike,0)       AS atm_strike
            FROM ranked r
            JOIN nearest n ON r.symbol = n.symbol AND r.expiry = n.exp
            WHERE r.rk = 1
        ),
        prev AS (
            SELECT r.symbol, r.strike_price,
                   COALESCE(r.ce_ltp,0) AS p_ce_ltp,
                   COALESCE(r.pe_ltp,0) AS p_pe_ltp
            FROM ranked r
            JOIN nearest n ON r.symbol = n.symbol AND r.expiry = n.exp
            WHERE r.rk = 2
        )
        SELECT c.*,
               c.ce_ltp - COALESCE(p.p_ce_ltp, c.ce_ltp) AS ce_ltp_chg,
               c.pe_ltp - COALESCE(p.p_pe_ltp, c.pe_ltp) AS pe_ltp_chg
        FROM cur c
        LEFT JOIN prev p ON p.symbol = c.symbol AND p.strike_price = c.strike_price
        ORDER BY c.symbol, c.strike_price
        """
    )
    if df.empty:
        return []

    fp = {"FRAC": float(shelf_frac)}
    rows = []
    for symbol, g in df.groupby("symbol", sort=True):
        g = g.sort_values("strike_price")
        strikes = g["strike_price"].to_numpy(float)
        ce_oi = g["ce_oi"].to_numpy(float)
        pe_oi = g["pe_oi"].to_numpy(float)
        ce_oi_chg = g["ce_oi_change"].to_numpy(float)
        pe_oi_chg = g["pe_oi_change"].to_numpy(float)
        ce_ltp_chg = g["ce_ltp_chg"].to_numpy(float)
        pe_ltp_chg = g["pe_ltp_chg"].to_numpy(float)
        net_gexv = g["net_gexv"].to_numpy(float)
        spot = float(g["spot"].max())
        fut = float(g["fut_price"].max())
        atm = float(g["atm_strike"].max())
        is_index = symbol in idx_set

        # ---- shelves (CE = resistance band, PE = support band) ----
        ce_shelf = core.oi_shelf(strikes, ce_oi, oi_change=ce_oi_chg,
                                 ltp_change=ce_ltp_chg, symbol=symbol,
                                 is_index=is_index, params=fp)
        pe_shelf = core.oi_shelf(strikes, pe_oi, oi_change=pe_oi_chg,
                                 ltp_change=pe_ltp_chg, symbol=symbol,
                                 is_index=is_index, params=fp)
        if ce_shelf is None or pe_shelf is None:
            continue

        # ---- gamma wall: strike with the largest |net gamma| ----
        gamma_wall_strike = None
        gamma_wall_val = None
        if np.isfinite(net_gexv).any() and np.abs(net_gexv).max() > 0:
            gi = int(np.argmax(np.abs(net_gexv)))
            gamma_wall_strike = float(strikes[gi])
            gamma_wall_val = float(net_gexv[gi])

        # ---- symbol-wide stats (PCR, wall strength) ----
        total_ce = float(ce_oi.sum())
        total_pe = float(pe_oi.sum())
        avg_ce = float(ce_oi[ce_oi > 0].mean()) if (ce_oi > 0).any() else 0.0
        avg_pe = float(pe_oi[pe_oi > 0].mean()) if (pe_oi > 0).any() else 0.0
        pcr = round(total_pe / total_ce, 3) if total_ce > 0 else None

        ce_wall = ce_shelf["wall_strike"]
        pe_wall = pe_shelf["wall_strike"]
        ce_wall_oi = ce_shelf["wall_oi"]
        pe_wall_oi = pe_shelf["wall_oi"]

        # gamma-wall vs OI-wall divergence (the signal): gamma wall sits at a
        # different strike than the CE OI wall → that strike does the pinning.
        gamma_oi_divergence = (
            gamma_wall_strike is not None
            and abs(gamma_wall_strike - ce_wall) > 1e-6
        )

        rows.append({
            "symbol":           symbol,
            "spot":             spot,
            "fut_price":        fut,
            "atm_strike":       atm,
            # ---- classic walls (back-compat with existing frontend fields) ----
            "ce_wall_strike":   ce_wall,
            "ce_wall_oi":       ce_wall_oi,
            "pe_wall_strike":   pe_wall,
            "pe_wall_oi":       pe_wall_oi,
            "ce_dist_spot":     ce_wall - spot,
            "pe_dist_spot":     spot - pe_wall,
            "ce_dist_fut":      ce_wall - fut,
            "pe_dist_fut":      fut - pe_wall,
            "ce_wall_strength": round(ce_wall_oi / avg_ce, 1) if avg_ce > 0 else None,
            "pe_wall_strength": round(pe_wall_oi / avg_pe, 1) if avg_pe > 0 else None,
            "pcr":              pcr,
            "wall_range":       ce_wall - pe_wall,
            # ---- wall enrichment (signal at the wall strike) ----
            "ce_signal":        ce_shelf.get("signal"),
            "pe_signal":        pe_shelf.get("signal"),
            # ---- CE shelf ----
            "ce_shelf_lo":      ce_shelf["lo"],
            "ce_shelf_hi":      ce_shelf["hi"],
            "ce_shelf_oi":      ce_shelf["oi"],
            "ce_shelf_com":     ce_shelf["com"],
            "ce_shelf_n":       ce_shelf["n_strikes"],
            "ce_is_shelf":      ce_shelf["is_shelf"],
            "ce_shelf_members": ce_shelf["members"],
            "ce_shelf_oi_chg":  ce_shelf.get("oi_change"),
            "ce_shelf_signal":  ce_shelf.get("signal"),
            # ---- PE shelf ----
            "pe_shelf_lo":      pe_shelf["lo"],
            "pe_shelf_hi":      pe_shelf["hi"],
            "pe_shelf_oi":      pe_shelf["oi"],
            "pe_shelf_com":     pe_shelf["com"],
            "pe_shelf_n":       pe_shelf["n_strikes"],
            "pe_is_shelf":      pe_shelf["is_shelf"],
            "pe_shelf_members": pe_shelf["members"],
            "pe_shelf_oi_chg":  pe_shelf.get("oi_change"),
            "pe_shelf_signal":  pe_shelf.get("signal"),
            # ---- gamma wall ----
            "gamma_wall_strike":   gamma_wall_strike,
            "gamma_wall_net_gexv": gamma_wall_val,
            "gamma_oi_divergence": gamma_oi_divergence,
            "shelf_frac":          float(shelf_frac),
        })

    if not rows:
        return []
    return safe_response(rows)



@router.get("/api/oi_history")
def oi_history(
    symbol:     str = Query(...),
    expiry:     str = Query(...),
    days:       int = Query(5),
    price_range_pct: float = Query(10.0),   # ATM ± % filter
):
    """
    Multi-day OI history for heatmap: strikes × dates → OI change.
    Filters strikes to ATM ± price_range_pct to keep the heatmap readable.
    """
    # Anchor to the symbol's latest DATA date (not wall-clock) so stale-data
    # days (weekends/holidays/missed scrapes) don't return empty.
    today = latest_data_date(symbol) or date.today().isoformat()
    if days <= 1:
        # Intraday: show each snapshot as a separate column in the heatmap
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%H:%M') AS date,
                strike_price,
                COALESCE(ce_oi, 0)        AS ce_oi,
                COALESCE(pe_oi, 0)        AS pe_oi,
                COALESCE(ce_oi_change, 0) AS ce_oi_chg,
                COALESCE(pe_oi_change, 0) AS pe_oi_chg,
                COALESCE(underlying_price,0) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
            ORDER BY timestamp, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    else:
        df = qdf(
            f"""
            WITH daily AS (
                SELECT
                    CAST(timestamp AS DATE)  AS dt,
                    strike_price,
                    AVG(COALESCE(ce_oi, 0))            AS ce_oi,
                    AVG(COALESCE(pe_oi, 0))            AS pe_oi,
                    AVG(COALESCE(underlying_price, 0)) AS spot
                FROM {tbl()}
                WHERE symbol=? AND expiry=?
                  AND CAST(timestamp AS DATE) >=
                      CAST(? AS DATE) - CAST({days} AS INTEGER) * INTERVAL '1' DAY
                  AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
                GROUP BY dt, strike_price
            )
            SELECT
                CAST(dt AS VARCHAR)  AS date,
                strike_price,
                ce_oi, pe_oi, spot,
                ce_oi - LAG(ce_oi) OVER (
                    PARTITION BY strike_price ORDER BY dt) AS ce_oi_chg,
                pe_oi - LAG(pe_oi) OVER (
                    PARTITION BY strike_price ORDER BY dt) AS pe_oi_chg
            FROM daily
            ORDER BY dt, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    # If multi-day query empty (only today's data in DB), fall back to intraday
    if df.empty and days > 1:
        today = date.today().isoformat()
        df = qdf(
            f"""
            SELECT
                STRFTIME(timestamp, '%H:%M') AS date,
                strike_price,
                COALESCE(ce_oi, 0)        AS ce_oi,
                COALESCE(pe_oi, 0)        AS pe_oi,
                COALESCE(ce_oi_change, 0) AS ce_oi_chg,
                COALESCE(pe_oi_change, 0) AS pe_oi_chg,
                COALESCE(underlying_price,0) AS spot
            FROM {tbl()}
            WHERE symbol=? AND expiry=?
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND (COALESCE(ce_ltp,0) + COALESCE(pe_ltp,0)) > 0
            ORDER BY timestamp, strike_price
            """,
            [symbol, expiry[:10], today],
        )
    if df.empty:
        raise HTTPException(404, "No history data — check symbol/expiry selection")

    # Filter by price range around average spot
    avg_spot = float(df["spot"].mean())
    if avg_spot > 0:
        lo_price = avg_spot * (1 - price_range_pct / 100)
        hi_price = avg_spot * (1 + price_range_pct / 100)
        df = df[(df["strike_price"] >= lo_price) & (df["strike_price"] <= hi_price)]

    dates   = sorted(df["date"].unique())
    strikes = sorted(df["strike_price"].unique())
    return safe_response({
        "symbol":  symbol,
        "expiry":  expiry[:10],
        "dates":   dates,
        "strikes": [float(s) for s in strikes],
        "rows":    to_records(df),
    })


