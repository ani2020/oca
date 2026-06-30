"""
oc_exposure_eod.py
==================
Batch-compute EOD exposure metrics for all configured tickers and store in
exposure_eod. Runs after the final EOD pull (or as a backfill over history).

Modes:
    --backfill        compute for all historical EOD dates (chronological)
    --date YYYY-MM-DD compute for one specific date
    (default)         compute for the latest available EOD date

Usage:
    python oc_exposure_eod.py --db oc.duckdb --backfill
    python oc_exposure_eod.py --db oc.duckdb --date 2026-06-05
    python oc_exposure_eod.py --db oc.duckdb               # latest EOD

Design ref: exposure_screener_design_spec.md
"""
from __future__ import annotations
import argparse
import math
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import duckdb
import numpy as np
import pandas as pd

import oc.exposure_core as core

TABLE = "ocdata"

# ── Configurable parameters (mirror settings) ──────────────────────────────
DTE_EXCLUDE      = 3      # skip monthly contracts with DTE <= this
IV_SMOOTH_SPAN   = 5      # EWMA span for ATM IV (trading days)
STRIKE_BUFFER_K  = 1.0    # range buffer = K × strike interval
MAX_RANGE_PCT    = 15.0   # absolute range cap (% of fut_price)
RANGE_CHG_CAP    = 0.25   # max day-over-day range change (fraction)
BAQ_MAX_PCT      = 10.0   # max bid-ask spread as % of premium
MIN_OI           = 0
MIN_VOL          = 0
EARNINGS_WINDOW  = 2      # ± days around earnings to flag


# ═══════════════════════════════════════════════════════════════════
# Core single-snapshot computation
# ═══════════════════════════════════════════════════════════════════

def compute_snapshot(snap: pd.DataFrame, fut: float, exp_move: float,
                     prev_range_half: Optional[float] = None,
                     prev_gamma_com: Optional[float] = None) -> Dict[str, Any]:
    """Compute all exposure metrics for one ticker's EOD snapshot (one expiry).
    Pure math delegated to exposure_core. Futures-based throughout (Black-76)."""
    snap = snap.sort_values("strike_price")
    strikes = snap["strike_price"].values
    strike_int = core.strike_interval(strikes)

    lo, hi, half = core.analysis_range(fut, exp_move, strike_int, prev_range_half)
    mask = (strikes >= lo) & (strikes <= hi)
    rng = snap[mask]
    if rng.empty or len(rng) < 2:
        return {"_skip": "empty_range"}

    r_strikes = rng["strike_price"].values
    # Compute GEX using gamma×OI×lotsize — same formula as the GEX page.
    # This greek-GEX is the SINGLE gamma definition for flip, regime, shelf,
    # lopsidedness and neg-fraction (v2: no more dual net_gexv/greek-GEX split).
    lot = float(rng["lotsize"].median()) if "lotsize" in rng.columns else 1
    call_gex = rng["ce_gamma"].values * rng["ce_oi"].values * lot
    put_gex  = rng["pe_gamma"].values * rng["pe_oi"].values * lot
    net_gex  = call_gex - put_gex

    flip, nearest, regime = core.gamma_flip(r_strikes, net_gex, fut)
    tw = core.transition_width(r_strikes, net_gex, fut)

    # Gamma shelf (dominant |net gamma| band) — Q1/Q1b: largest dealer inventory
    # + hedging center of mass, with width / peak / single-strike + migration.
    shelf = core.gamma_shelf(r_strikes, net_gex, prev_com=prev_gamma_com)

    # Peaks and regime fractions from greek-GEX net_gex
    pp_idx = int(np.argmax(net_gex))
    pn_idx = int(np.argmin(net_gex))
    peak_pos = float(r_strikes[pp_idx]) if net_gex[pp_idx] > 0 else None
    peak_neg = float(r_strikes[pn_idx]) if net_gex[pn_idx] < 0 else None

    neg_frac = float((net_gex < 0).sum() / len(net_gex)) if len(net_gex) else None
    # net_gexv-based aggregate (spot²-scaled) kept ONLY for the stored net_gex
    # magnitude + AGG sign column (display scale); structural metrics use net_gex.
    net_gexv_sum = float(rng["net_gexv"].values.sum())

    out = {
        "fut_price":       round(fut, 2),
        "expected_move":   round(exp_move, 2),
        "strike_interval": strike_int,
        "range_lo":        round(lo, 2),
        "range_hi":        round(hi, 2),
        "_range_half":     half,
        "gamma_flip":      flip,
        "flip_nearest":    nearest,
        "flip_norm_distance": round((flip - fut) / exp_move, 4)
                              if flip and exp_move > 0 else None,
        "net_gex":         round(net_gexv_sum / 1e6, 4),
        "net_gex_sign":    "positive" if net_gexv_sum >= 0 else "negative",
        # Dimensionless lopsidedness ratio: net / gross gamma, bounded [-1, +1].
        # v2: computed on greek-GEX (the SAME gamma as flip/regime/shelf) so the
        # regime word, lopsidedness and shelf are mutually consistent.
        "net_gex_norm":    core.lopsidedness(net_gex),
        "gex_regime":      regime,
        "transition_width": tw,
        "transition_width_norm": round(tw / exp_move, 4)
                                 if tw and exp_move > 0 else None,
        "neg_gamma_fraction": round(neg_frac, 4) if neg_frac is not None else None,
        "peak_pos_gamma_strike": peak_pos,
        "peak_neg_gamma_strike": peak_neg,
        # Gamma shelf (Q1/Q1b/Q3 raw facts). None-safe unpack.
        "gamma_shelf_center":       (shelf or {}).get("center"),
        "gamma_shelf_width":        (shelf or {}).get("width"),
        "gamma_shelf_peak_strike":  (shelf or {}).get("peak_strike"),
        "gamma_shelf_peak_value":   (shelf or {}).get("peak_value"),
        "gamma_shelf_single_strike":(shelf or {}).get("single_strike"),
        "concentration":            (shelf or {}).get("concentration"),
        "gamma_com_migration":      (shelf or {}).get("migration"),
        "_gamma_com":               (shelf or {}).get("center"),  # carried for prev-day seeding
        "ce_vanna":        round(float(rng["ce_vanna_ex"].sum()), 2),
        "pe_vanna":        round(float(rng["pe_vanna_ex"].sum()), 2),
        "net_vanna":       round(float(rng["net_vanna_ex"].sum()), 2),
        "net_charm":       round(float(rng["net_charm_ex"].sum()), 2),
        "total_oi_in_range": float((rng["ce_oi"] + rng["pe_oi"]).sum()),
        # OI turnover ratio (INDICATOR): Σ|oi_change| / Σoi across CE+PE in range.
        # "What fraction of gamma-relevant OI turned over this session."
        "oi_turnover_ratio": round(
            float(rng["ce_oi_change"].abs().sum() + rng["pe_oi_change"].abs().sum()) /
            max(float((rng["ce_oi"] + rng["pe_oi"]).sum()), 1.0), 4),
        "n_strikes":       int(len(rng)),
    }
    return out


# ═══════════════════════════════════════════════════════════════════
# Signal derivation (day-over-day)
# ═══════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════
# Per-ticker / per-date orchestration
# ═══════════════════════════════════════════════════════════════════

def _monthly_expiries(con, symbol: str, d: str) -> List[Tuple[str, int]]:
    """Pick the NEAR (rank 0) and NEXT (rank 1) monthly expiries for a date.

    Monthly = last expiry in each calendar month. NEAR rolls at DTE <= threshold
    (DTE_EXCLUDE): the front monthly with DTE > threshold. NEXT is the monthly
    immediately after NEAR (no DTE gate — it is always ~25-35 DTE).

    Returns an ordered list of (expiry_iso, expiry_rank) — length 0, 1, or 2.
    Empty when no monthly with DTE > threshold exists (e.g. only the expiring
    contract is present that day → skip the symbol, matching the prior fallback
    intent of avoiding sub-threshold gamma)."""
    rows = con.execute(f"""
        WITH exps AS (
            SELECT DISTINCT CAST(expiry AS DATE) AS e
            FROM {TABLE}
            WHERE symbol = ? AND CAST(timestamp AS DATE) = CAST(? AS DATE)
              AND CAST(expiry AS DATE) >= CAST(? AS DATE)
        ),
        monthly AS (
            SELECT e, ROW_NUMBER() OVER (
                PARTITION BY DATE_TRUNC('month', e) ORDER BY e DESC) AS rn
            FROM exps
        )
        SELECT e FROM monthly WHERE rn = 1 ORDER BY e
    """, [symbol, d, d]).fetchall()
    if not rows:
        return []
    months = [r[0] for r in rows]
    dd = datetime.fromisoformat(d).date()
    # NEAR = first monthly with DTE > threshold (the tradeable front). The
    # expiring (<= threshold) contract is intentionally dropped, not used as a
    # fallback — its sub-threshold gamma is exactly what the threshold excludes.
    near_idx = None
    for i, e in enumerate(months):
        if (e - dd).days > DTE_EXCLUDE:
            near_idx = i
            break
    if near_idx is None:
        return []
    out: List[Tuple[str, int]] = [(months[near_idx].isoformat(), 0)]
    # NEXT = the monthly immediately after NEAR (if present).
    if near_idx + 1 < len(months):
        out.append((months[near_idx + 1].isoformat(), 1))
    return out


def _fetch_snapshot(con, symbol: str, expiry: str, d: str) -> pd.DataFrame:
    """Latest snapshot of the day for symbol+monthly expiry, quality-filtered."""
    df = con.execute(f"""
        WITH latest AS (
            SELECT MAX(timestamp) AS ts FROM {TABLE}
            WHERE symbol = ? AND CAST(expiry AS DATE) = CAST(? AS DATE)
              AND CAST(timestamp AS DATE) = CAST(? AS DATE)
        )
        SELECT
            strike_price,
            COALESCE(fut_price, underlying_price, 0) AS fut_price,
            COALESCE(underlying_price, 0)  AS spot,
            COALESCE(atm_strike, 0)        AS atm_strike,
            COALESCE(distance_from_atm, 999) AS dist_atm,
            COALESCE(days_to_expiry, 0)    AS dte,
            COALESCE(ce_iv, 0)             AS ce_iv,
            COALESCE(ce_oi_change, 0)      AS ce_oi_change,
            COALESCE(pe_oi_change, 0)      AS pe_oi_change,
            COALESCE(ce_oi, 0)             AS ce_oi,
            COALESCE(pe_oi, 0)             AS pe_oi,
            COALESCE(ce_volume, 0)         AS ce_vol,
            COALESCE(pe_volume, 0)         AS pe_vol,
            COALESCE(ce_ltp, 0)            AS ce_ltp,
            COALESCE(pe_ltp, 0)            AS pe_ltp,
            COALESCE(ce_bid_ask_spread, 0) AS ce_baq,
            COALESCE(pe_bid_ask_spread, 0) AS pe_baq,
            COALESCE(ce_gamma, 0)          AS ce_gamma,
            COALESCE(pe_gamma, 0)          AS pe_gamma,
            COALESCE(lotsize, 1)            AS lotsize,
            COALESCE(net_gexv, 0)          AS net_gexv,
            COALESCE(ce_vanna_ex, 0)       AS ce_vanna_ex,
            COALESCE(pe_vanna_ex, 0)       AS pe_vanna_ex,
            COALESCE(net_vanna_ex, 0)      AS net_vanna_ex,
            COALESCE(net_charm_ex, 0)      AS net_charm_ex
        FROM {TABLE}, latest
        WHERE symbol = ? AND CAST(expiry AS DATE) = CAST(? AS DATE)
          AND timestamp = latest.ts
        ORDER BY strike_price
    """, [symbol, expiry, d, symbol, expiry]).df()
    if df.empty:
        return df
    # Quality: bid-ask spread as % of premium
    df["ce_baq_pct"] = np.where(df["ce_ltp"] > 0, df["ce_baq"] / df["ce_ltp"] * 100, 999)
    df["pe_baq_pct"] = np.where(df["pe_ltp"] > 0, df["pe_baq"] / df["pe_ltp"] * 100, 999)
    df = df[
        ((df["ce_baq_pct"] <= BAQ_MAX_PCT) | (df["pe_baq_pct"] <= BAQ_MAX_PCT)) &
        ((df["ce_oi"] >= MIN_OI) | (df["pe_oi"] >= MIN_OI)) &
        ((df["ce_vol"] >= MIN_VOL) | (df["pe_vol"] >= MIN_VOL))
    ]
    return df


def _atm_iv(snap: pd.DataFrame) -> float:
    """ATM IV from the row nearest distance_from_atm = 0."""
    if snap.empty:
        return 0.0
    near = snap.iloc[(snap["dist_atm"].abs()).argmin()]
    return float(near["ce_iv"]) if near["ce_iv"] > 0 else 0.0


# ═══════════════════════════════════════════════════════════════════
# Main driver
# ═══════════════════════════════════════════════════════════════════

def process_date(con, symbols: List[str], d: str,
                 iv_hist: Dict[Tuple[str, int], List[float]],
                 prev_rows: Dict[Tuple[str, int], Dict]) -> List[Dict]:
    """Compute exposure_eod rows for all symbols on date d.

    Now emits up to two rows per symbol — NEAR (expiry_rank 0) and NEXT
    (expiry_rank 1). iv_hist and prev_rows are keyed by (symbol, expiry_rank)
    so day-over-day continuity (EWMA IV, range/COM seeding, signals) never
    crosses the near/next boundary."""
    out = []
    for sym in symbols:
        for expiry, rank in _monthly_expiries(con, sym, d):
            key = (sym, rank)
            try:
                snap = _fetch_snapshot(con, sym, expiry, d)
                if snap.empty or len(snap) < 3:
                    continue

                fut = float(snap["fut_price"].median())
                dte = float(snap["dte"].median())
                atm_iv = _atm_iv(snap)

                # Smoothed IV (EWMA over history) — per (symbol, rank)
                hist = iv_hist.setdefault(key, [])
                if atm_iv > 0:
                    hist.append(atm_iv)
                    if len(hist) > IV_SMOOTH_SPAN * 3:
                        hist[:] = hist[-IV_SMOOTH_SPAN * 3:]
                iv_sm = core.ewma_last(hist, IV_SMOOTH_SPAN) if hist else atm_iv

                em = core.expected_move(fut, iv_sm, dte)

                prev = prev_rows.get(key)
                prev_half = prev.get("_range_half") if prev else None
                prev_gcom = prev.get("_gamma_com") if prev else None
                m = compute_snapshot(snap, fut, em, prev_half, prev_gcom)
                if "_skip" in m:
                    continue

                # IV change vs prev day (same rank)
                iv_chg = (atm_iv - prev.get("atm_iv", atm_iv)) if prev else 0.0

                # flip_velocity is computed in the Stage-2 SQL pass (window
                # functions), not here — Stage 1 is per-snapshot only.

                spot_val = round(float(snap["spot"].median()), 2)
                # Basis (same-expiry future vs spot) — fut==spot fallback → None,
                # annualized clamped for dividend/data distortions (esp. stocks).
                basis, basis_pct, basis_ann = core.basis_metrics(fut, spot_val, int(dte))

                m.update({
                    "symbol":          sym,
                    "date":            d,   # also stored in prev_rows for velocity normalisation
                    "expiry":          expiry,
                    "expiry_rank":     rank,
                    "dte":             int(dte),
                    "spot":            spot_val,
                    "basis":           basis,
                    "basis_pct":       basis_pct,
                    "basis_annualized": basis_ann,
                    "atm_iv":          round(atm_iv, 2),
                    "atm_iv_smoothed": round(float(iv_sm), 2),
                    "iv_change":       round(iv_chg, 2),
                    "confidence":      core.confidence(m["total_oi_in_range"],
                                                   m["n_strikes"],
                                                   m.get("transition_width_norm")),
                    "near_earnings":   False,  # filled by events join (Phase A.2)
                })
                sigs, active = core.derive_signals(m, prev)
                m["signals"]       = sigs
                m["active_regime"] = active

                prev_rows[key] = m
                out.append(m)
            except Exception as exc:
                print(f"  ! {sym} r{rank} @ {d}: {exc}")
                continue
    return out


def _store(con, rows: List[Dict]):
    if not rows:
        return
    cols = [
        "symbol","date","expiry","expiry_rank","dte","fut_price","spot","expected_move",
        "strike_interval","range_lo","range_hi","atm_iv","atm_iv_smoothed",
        "iv_change","basis","basis_pct","basis_annualized","gamma_flip","flip_nearest",
        "flip_norm_distance","net_gex","gex_regime","transition_width",
        "net_gex_sign","net_gex_norm",
        "transition_width_norm","neg_gamma_fraction","peak_pos_gamma_strike",
        "peak_neg_gamma_strike","ce_vanna","pe_vanna","net_vanna","net_charm",
        "total_oi_in_range","oi_turnover_ratio","n_strikes","confidence",
        "near_earnings","active_regime",
        # v2 gamma-shelf raw facts (spec §3.1) + concentration + COM migration
        "gamma_shelf_center","gamma_shelf_width","gamma_shelf_peak_strike",
        "gamma_shelf_peak_value","gamma_shelf_single_strike","concentration",
        "gamma_com_migration",
        # NOTE: flip_velocity + signals + compression/day-counters + dealer_divergence
        # + migration_effectiveness + concentration_stability are populated by the
        # Stage-2 SQL pass (run_stage2), not Stage 1.
    ]
    df = pd.DataFrame([{c: r.get(c) for c in cols} for r in rows])
    con.register("_exp_batch", df)
    con.execute("DELETE FROM exposure_eod WHERE (symbol, date, expiry_rank) IN "
                "(SELECT symbol, CAST(date AS DATE), expiry_rank FROM _exp_batch)")
    con.execute(f"INSERT INTO exposure_eod ({','.join(cols)}) "
                f"SELECT {','.join(cols)} FROM _exp_batch")
    con.unregister("_exp_batch")


def fill_forward_outcomes(con):
    """Fill next_day_realized_move/range for rows missing it (one-day lag).
    Partitioned by (symbol, expiry_rank) so a NEAR row's next-day spot comes from
    the NEXT session's NEAR row, never the same date's NEXT-expiry row."""
    con.execute("""
        WITH nxt AS (
            SELECT e.symbol, e.date, e.expiry_rank,
                   LEAD(e.spot) OVER w AS next_spot,
                   LEAD(e.date) OVER w AS next_date
            FROM exposure_eod e
            WINDOW w AS (PARTITION BY e.symbol, e.expiry_rank ORDER BY e.date)
        )
        UPDATE exposure_eod ex
        SET next_day_realized_move =
            ROUND((n.next_spot - ex.spot) / NULLIF(ex.spot,0) * 100, 3),
            next_day_abs_move =
            ABS(ROUND((n.next_spot - ex.spot) / NULLIF(ex.spot,0) * 100, 3))
        FROM nxt n
        WHERE ex.symbol = n.symbol AND ex.date = n.date
          AND ex.expiry_rank = n.expiry_rank
          AND n.next_spot IS NOT NULL
          AND ex.next_day_realized_move IS NULL
    """)


def _seed_state_from_db(con, symbols, before_date, iv_hist, prev_rows):
    """For incremental daily runs: load prior stored row + recent IV history
    per (symbol, expiry_rank) from exposure_eod so day-over-day comparisons work.
    State is keyed by (symbol, expiry_rank) — NEAR and NEXT carry independent
    prev rows and IV histories."""
    # Most recent stored row per (symbol, expiry_rank) strictly before the run date
    rows = con.execute("""
        WITH ranked AS (
            SELECT *, ROW_NUMBER() OVER (
                PARTITION BY symbol, expiry_rank ORDER BY date DESC) AS rn
            FROM exposure_eod
            WHERE date < CAST(? AS DATE)
        )
        SELECT * FROM ranked WHERE rn = 1
    """, [before_date]).df()
    for _, r in rows.iterrows():
        rank = int(r["expiry_rank"]) if r.get("expiry_rank") is not None else 0
        prev_rows[(r["symbol"], rank)] = {
            "gamma_flip":            r.get("gamma_flip"),
            "gex_regime":            r.get("gex_regime"),
            "atm_iv":                r.get("atm_iv"),
            "transition_width_norm": r.get("transition_width_norm"),
            "neg_gamma_fraction":    r.get("neg_gamma_fraction"),
            "_gamma_com":            r.get("gamma_shelf_center"),  # for COM migration
            "date":                  str(r["date"]) if r.get("date") is not None else None,
            "_range_half":           (r.get("range_hi", 0) - r.get("range_lo", 0)) / 2
                                     if r.get("range_hi") is not None else None,
        }
    # Recent ATM IV history per (symbol, rank) (last span×3 days) for EWMA continuity
    hist = con.execute("""
        SELECT symbol, expiry_rank, date, atm_iv FROM exposure_eod
        WHERE date < CAST(? AS DATE) AND atm_iv > 0
        ORDER BY symbol, expiry_rank, date
    """, [before_date]).df()
    span3 = IV_SMOOTH_SPAN * 3
    for (sym, rank), grp in hist.groupby(["symbol", "expiry_rank"]):
        iv_hist[(sym, int(rank))] = grp["atm_iv"].tolist()[-span3:]
    print(f"  Seeded state for {len(prev_rows)} (symbol, rank) keys from prior EOD")


# ═══════════════════════════════════════════════════════════════════
# STAGE 2 — derived temporal metrics (stateless window-function pass)
# ═══════════════════════════════════════════════════════════════════
# Computes, over the full exposure_eod table (no in-memory state):
#   - flip_velocity            (Δflip / Δcalendar-days)
#   - regime_compression       (sustained tightening conjunction)
#   - compression_days         (consecutive sessions compression held)
#   - compression_release      (break day with expansion confirmation)
#   - days_in_regime           (consecutive sessions in current gex_regime)
#   - days_since_flip          (sessions since last regime change)
#   - days_since_release       (sessions since last compression_release)
#   - signals                  (via exposure_core.derive_signals — single source)
# Idempotent and re-runnable; just re-execute over the whole table.
#
# v3: every window/gaps-and-islands pass is partitioned by (symbol, expiry_rank)
# so NEAR and NEXT temporal metrics never bleed across the expiry boundary.

# Tunable Stage-2 params (mirror exposure_core where shared)
COMPRESS_LOOKBACK = 2     # transition_width_norm contraction window (days)

def run_stage2(con):
    """Recompute all derived temporal columns over exposure_eod. Stateless."""
    print("Stage 2: derived temporal metrics …")

    # ── 2a. flip_velocity via SQL window function (calendar-day normalised) ──
    con.execute("""
        WITH ordered AS (
            SELECT symbol, date, expiry_rank, gamma_flip,
                   LAG(gamma_flip) OVER w  AS prev_flip,
                   LAG(date)       OVER w  AS prev_date
            FROM exposure_eod
            WINDOW w AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE exposure_eod e
        SET flip_velocity = CASE
            WHEN o.prev_flip IS NOT NULL AND e.gamma_flip IS NOT NULL
                 AND o.prev_date IS NOT NULL
            THEN ROUND((e.gamma_flip - o.prev_flip) /
                       GREATEST(DATE_DIFF('day', o.prev_date, e.date), 1), 2)
            ELSE NULL END
        FROM ordered o
        WHERE e.symbol = o.symbol AND e.date = o.date AND e.expiry_rank = o.expiry_rank
    """)

    # ── 2a-bis. basis_chg: Δ annualized basis vs previous session ──
    con.execute("""
        WITH ordered AS (
            SELECT symbol, date, expiry_rank, basis_annualized,
                   LAG(basis_annualized) OVER w AS prev_basis
            FROM exposure_eod
            WINDOW w AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE exposure_eod e
        SET basis_chg = CASE
            WHEN o.prev_basis IS NOT NULL AND e.basis_annualized IS NOT NULL
            THEN ROUND(e.basis_annualized - o.prev_basis, 3)
            ELSE NULL END
        FROM ordered o
        WHERE e.symbol = o.symbol AND e.date = o.date AND e.expiry_rank = o.expiry_rank
    """)

    # ── 2b. regime_compression (sustained tightening conjunction) ──
    # Tightening over COMPRESS_LOOKBACK days, tolerant of single-day blips:
    #   transition_width_norm now < value LOOKBACK days ago
    #   AND |flip_norm_distance| now < value LOOKBACK days ago (converging)
    #   AND gex_regime positive AND atm_iv_smoothed not rising vs prev day.
    con.execute(f"""
        WITH w AS (
            SELECT symbol, date, expiry_rank,
                   transition_width_norm AS twn,
                   LAG(transition_width_norm, {COMPRESS_LOOKBACK}) OVER win AS twn_lb,
                   ABS(flip_norm_distance) AS afnd,
                   LAG(ABS(flip_norm_distance), {COMPRESS_LOOKBACK}) OVER win AS afnd_lb,
                   gex_regime,
                   atm_iv_smoothed AS ivs,
                   LAG(atm_iv_smoothed, 1) OVER win AS ivs_prev
            FROM exposure_eod
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE exposure_eod e
        SET regime_compression = (
            w.gex_regime IN ('positive','all_positive')
            AND w.twn  IS NOT NULL AND w.twn_lb  IS NOT NULL AND w.twn  < w.twn_lb
            AND w.afnd IS NOT NULL AND w.afnd_lb IS NOT NULL AND w.afnd < w.afnd_lb
            AND (w.ivs_prev IS NULL OR w.ivs <= w.ivs_prev)
        )
        FROM w
        WHERE e.symbol = w.symbol AND e.date = w.date AND e.expiry_rank = w.expiry_rank
    """)
    # Rows with insufficient history → FALSE not NULL
    con.execute("UPDATE exposure_eod SET regime_compression = FALSE "
                "WHERE regime_compression IS NULL")

    # ── 2c. compression_release (break day + expansion confirmation) ──
    con.execute("""
        WITH w AS (
            SELECT symbol, date, expiry_rank, regime_compression,
                   LAG(regime_compression,1) OVER win AS prev_comp,
                   iv_change, gex_regime, transition_width,
                   LAG(transition_width,1)   OVER win AS prev_tw
            FROM exposure_eod
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE exposure_eod e
        SET compression_release = (
            w.prev_comp = TRUE AND w.regime_compression = FALSE
            AND COALESCE(w.iv_change,0) > 0
            AND w.gex_regime IN ('negative','all_negative')
            AND w.prev_tw IS NOT NULL AND w.transition_width > w.prev_tw
        )
        FROM w
        WHERE e.symbol = w.symbol AND e.date = w.date AND e.expiry_rank = w.expiry_rank
    """)
    con.execute("UPDATE exposure_eod SET compression_release = FALSE "
                "WHERE compression_release IS NULL")

    # ── 2d. day-counters via gaps-and-islands ──
    # days_in_regime: consecutive sessions in current gex_regime
    con.execute("""
        WITH marked AS (
            SELECT symbol, date, expiry_rank, gex_regime,
                   CASE WHEN gex_regime IS DISTINCT FROM
                        LAG(gex_regime) OVER win THEN 1 ELSE 0 END AS chg
            FROM exposure_eod
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        ),
        grp AS (
            SELECT symbol, date, expiry_rank,
                   SUM(chg) OVER (PARTITION BY symbol, expiry_rank ORDER BY date) AS grp_id
            FROM marked
        ),
        cnt AS (
            SELECT symbol, date, expiry_rank,
                   ROW_NUMBER() OVER (PARTITION BY symbol, expiry_rank, grp_id ORDER BY date) AS dir
            FROM grp
        )
        UPDATE exposure_eod e
        SET days_in_regime = c.dir,
            days_since_flip = c.dir - 1
        FROM cnt c
        WHERE e.symbol = c.symbol AND e.date = c.date AND e.expiry_rank = c.expiry_rank
    """)

    # days_since_release: sessions since last compression_release=TRUE
    con.execute("""
        WITH rel AS (
            SELECT symbol, date, expiry_rank, compression_release,
                   MAX(CASE WHEN compression_release THEN date END)
                       OVER (PARTITION BY symbol, expiry_rank ORDER BY date
                             ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS last_rel
            FROM exposure_eod
        )
        UPDATE exposure_eod e
        SET days_since_release = CASE
            WHEN r.last_rel IS NULL THEN NULL
            ELSE DATE_DIFF('day', r.last_rel, e.date) END
        FROM rel r
        WHERE e.symbol = r.symbol AND e.date = r.date AND e.expiry_rank = r.expiry_rank
    """)

    # compression_days: consecutive sessions regime_compression held
    con.execute("""
        WITH marked AS (
            SELECT symbol, date, expiry_rank, regime_compression,
                   CASE WHEN regime_compression IS DISTINCT FROM
                        LAG(regime_compression) OVER win THEN 1 ELSE 0 END AS chg
            FROM exposure_eod
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        ),
        grp AS (
            SELECT symbol, date, expiry_rank, regime_compression,
                   SUM(chg) OVER (PARTITION BY symbol, expiry_rank ORDER BY date) AS grp_id
            FROM marked
        ),
        cnt AS (
            SELECT symbol, date, expiry_rank, regime_compression,
                   ROW_NUMBER() OVER (PARTITION BY symbol, expiry_rank, grp_id ORDER BY date) AS run
            FROM grp
        )
        UPDATE exposure_eod e
        SET compression_days = CASE WHEN c.regime_compression THEN c.run ELSE 0 END
        FROM cnt c
        WHERE e.symbol = c.symbol AND e.date = c.date AND e.expiry_rank = c.expiry_rank
    """)

    # ── 2e. migration_effectiveness = |spot move %| / |gamma-COM move %|  ──
    # Healthy ≈1 (COM tracks spot, orderly repositioning); ≫1 price running from
    # a static structure; ≈0 COM moves without spot (anticipatory). Small-denominator
    # guard: NULL when COM barely moves (avoids blow-ups), same as basis_annualized.
    con.execute("""
        WITH w AS (
            SELECT symbol, date, expiry_rank, spot, gamma_shelf_center,
                   LAG(spot)              OVER win AS prev_spot,
                   LAG(gamma_shelf_center) OVER win AS prev_com
            FROM exposure_eod
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date)
        )
        UPDATE exposure_eod e
        SET migration_effectiveness = CASE
            WHEN w.prev_spot IS NOT NULL AND w.prev_com IS NOT NULL
                 AND w.prev_spot <> 0 AND w.prev_com <> 0
                 AND ABS((w.gamma_shelf_center - w.prev_com) / w.prev_com) >= 0.0005
            THEN ROUND(
                ABS((w.spot - w.prev_spot) / w.prev_spot) /
                NULLIF(ABS((w.gamma_shelf_center - w.prev_com) / w.prev_com), 0), 3)
            ELSE NULL END
        FROM w
        WHERE e.symbol = w.symbol AND e.date = w.date AND e.expiry_rank = w.expiry_rank
    """)

    # ── 2f. concentration_stability = 1 - std(concentration, last 5 days)  ──
    # Independent defense-strength axis: consistency of the gamma-shelf concentration.
    # concentration is a bounded ratio [0,1] so its std is small/bounded. Requires
    # >=3 of the trailing 5 sessions present (else NULL).
    con.execute("""
        WITH w AS (
            SELECT symbol, date, expiry_rank,
                   STDDEV_SAMP(concentration) OVER win AS sd,
                   COUNT(concentration)       OVER win AS n
            FROM exposure_eod
            WINDOW win AS (PARTITION BY symbol, expiry_rank ORDER BY date
                           ROWS BETWEEN 4 PRECEDING AND CURRENT ROW)
        )
        UPDATE exposure_eod e
        SET concentration_stability = CASE
            WHEN w.n >= 3 AND w.sd IS NOT NULL
            THEN ROUND(1.0 - w.sd, 4)
            ELSE NULL END
        FROM w
        WHERE e.symbol = w.symbol AND e.date = w.date AND e.expiry_rank = w.expiry_rank
    """)

    # ── 2g. signals via exposure_core.derive_signals (single-source, stateless) ──
    _recompute_signals(con)
    print("Stage 2: done.")


def _recompute_signals(con):
    """Stateless signal recompute: read table, build curr/prev per consecutive
    row pair, call exposure_core.derive_signals. Single-source signal logic."""
    df = con.execute("""
        SELECT * FROM exposure_eod ORDER BY symbol, expiry_rank, date
    """).df()
    if df.empty:
        return
    out = []
    for (sym, rank), grp in df.groupby(["symbol", "expiry_rank"]):
        grp = grp.sort_values("date").reset_index(drop=True)
        prev = None
        for _, row in grp.iterrows():
            curr = row.to_dict()
            sigs, active = core.derive_signals(curr, prev)
            out.append((sigs, active, sym, int(rank), str(row["date"])))
            prev = curr
    con.executemany(
        "UPDATE exposure_eod SET signals = ?, active_regime = ? "
        "WHERE symbol = ? AND expiry_rank = ? AND date = ?", out)


def _ensure_schema(con):
    """Idempotently add v2 columns to exposure_eod (Stage-1 raw + Stage-2 derived).
    Safe to run on every invocation; ADD COLUMN IF NOT EXISTS is a no-op when present.
    The base table is created elsewhere; this only guarantees the new columns exist
    before _store INSERT and the Stage-2 UPDATEs reference them."""
    add = [
        # Expiry rank (0 = NEAR/front monthly, 1 = NEXT monthly). Existing rows
        # predate the two-expiry change and are all NEAR → default 0 so legacy
        # history is correctly tagged without a separate migration step.
        ("expiry_rank",               "TINYINT DEFAULT 0"),
        # Stage-1 gamma-shelf raw facts
        ("gamma_shelf_center",        "DOUBLE"),
        ("gamma_shelf_width",         "INTEGER"),
        ("gamma_shelf_peak_strike",   "DOUBLE"),
        ("gamma_shelf_peak_value",    "DOUBLE"),
        ("gamma_shelf_single_strike", "BOOLEAN"),
        ("concentration",             "DOUBLE"),
        ("gamma_com_migration",       "DOUBLE"),
        # Stage-2 derived
        ("migration_effectiveness",   "DOUBLE"),
        ("concentration_stability",   "DOUBLE"),
        # dealer_divergence (spec §3.3) reserved — populated once OI-shelf COM is
        # available to the batch (shared exposure_core shelf refactor). Columns
        # created now so the schema is stable across the backfill.
        ("dealer_divergence",         "DOUBLE"),
        ("dealer_divergence_label",   "VARCHAR"),
    ]
    for col, typ in add:
        try:
            con.execute(f"ALTER TABLE exposure_eod ADD COLUMN IF NOT EXISTS {col} {typ}")
        except Exception as exc:
            print(f"  ! schema add {col}: {exc}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="oc.duckdb")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--date", default=None)
    ap.add_argument("--symbols", default=None, help="comma list; default all")
    args = ap.parse_args()

    con = duckdb.connect(args.db)
    _ensure_schema(con)   # v2: guarantee gamma-shelf + derived columns exist

    # Symbol universe
    if args.symbols:
        symbols = [s.strip() for s in args.symbols.split(",")]
    else:
        symbols = [r[0] for r in con.execute(
            f"SELECT DISTINCT symbol FROM {TABLE} ORDER BY symbol").fetchall()]
    print(f"Symbols: {len(symbols)}")

    # Dates to process
    if args.backfill:
        dates = [r[0].isoformat() for r in con.execute(
            f"SELECT DISTINCT CAST(timestamp AS DATE) AS d FROM {TABLE} "
            f"WHERE timestamp IS NOT NULL ORDER BY d"
        ).fetchall() if r[0] is not None]
    elif args.date:
        dates = [args.date]
    else:
        _maxd = con.execute(
            f"SELECT CAST(MAX(timestamp) AS DATE) FROM {TABLE} WHERE timestamp IS NOT NULL").fetchone()[0]
        if _maxd is None:
            print("No data in table"); con.close(); return
        dates = [_maxd.isoformat()]
    print(f"Dates: {len(dates)} ({dates[0]} … {dates[-1]})")

    iv_hist: Dict[Tuple[str, int], List[float]] = {}
    prev_rows: Dict[Tuple[str, int], Dict] = {}
    # For single-date (incremental/daily) runs, seed prior state from the DB so
    # day-over-day signals (velocity, transitions, IV change) work correctly.
    if not args.backfill and dates:
        _seed_state_from_db(con, symbols, dates[0], iv_hist, prev_rows)
    total = 0
    for d in dates:
        rows = process_date(con, symbols, d, iv_hist, prev_rows)
        _store(con, rows)
        total += len(rows)
        print(f"  {d}: {len(rows)} rows")

    fill_forward_outcomes(con)
    run_stage2(con)
    con.close()
    print(f"\nDone — {total} rows written.")


if __name__ == "__main__":
    main()
