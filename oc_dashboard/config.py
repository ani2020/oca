"""Shared configuration for the OC Dashboard."""
from __future__ import annotations
from pathlib import Path

DB_FILE  = "oc.duckdb"
DB_TABLE = "ocdata"

# Front-month (NEAR) selection threshold for snapshot-level aggregates.
# Mirrors DTE_EXCLUDE in oc_exposure_eod.py: monthly contracts with DTE <= this
# are skipped so NEAR is always the tradeable front month, not the expiring one.
# Keep these two in sync — the overview and the exposure screen must agree on
# which expiry constitutes "the market" for a symbol.
DTE_EXCLUDE_OVERVIEW = 3

# ── IV Rank / Percentile ──────────────────────────────────────────────────
# IV Rank is built on a CONSTANT-MATURITY series, not on raw front-month ATM
# IV. Ranking the spliced rank-0 series makes the number a function of where
# you sit in the roll cycle: measured over exposure_eod, mean IV Rank ran 16.8
# at DTE 0-4 vs 47.5 at DTE 15-21 — a 31pt swing with no change in vol regime,
# so every name in expiry week looked like cheap vol. (A linear CORR test misses
# this: it returns ~0.03 because the shape is a hump, not a line. Check
# bucketed means, not correlation.)
#
# Interpolating rank-0 and rank-1 to a fixed 30-day point in TOTAL VARIANCE —
# the same thing India VIX does — cuts that spread to 9.8pt and lifts expiry
# week to 26.3. Where rank-1 is unavailable (~45% of symbol-days) we fall back
# to ranking rank-0 restricted to a DTE band, which gets the spread to 12.7pt.
# The response reports which method produced the number.
IVRANK_CM_TARGET_DTE = 30          # constant-maturity point, in calendar days
IVRANK_DTE_BAND      = (15, 35)    # fallback band when rank-1 is missing
IVRANK_MIN_SAMPLES   = 20          # below this, return None rather than a number
IVRANK_LOOKBACK_DAYS = 90          # default trailing window

# ── Short-option margin approximation ───────────────────────────────────
# Deliberately NOT tuned to any broker. ICICI and Zerodha differ on RMS add-ons,
# so an exact match is neither achievable nor useful — what matters is that the
# ORDERING across contracts and symbols is right, computed identically for every
# instrument in a class.
#
# Shape follows SPAN: a volatility-driven scan range plus a flat exposure
# component, with a floor so deep-OTM shorts never approach zero.
#   scan_pct  = max(min_scan, K_SIGMA * iv * sqrt(HORIZON/243))
#   base_pct  = scan_pct + exposure_pct
#   floor_pct = FLOOR_FRAC * base_pct
# Sanity: NIFTY at 11 vol -> ~8% of notional; a 30-vol stock -> ~14.5%; a 60-vol
# stock -> ~24%. A flat 20% (the Reg-T style constant) would have ranked the
# 11-vol index alongside the 60-vol stock.
MARGIN_K_SIGMA      = 3.5     # scan range in sigmas
MARGIN_HORIZON_DAYS = 2.0     # SPAN looks 2 sessions ahead
MARGIN_PARAMS = {
    # class:   (min_scan_pct, exposure_pct)
    "index":   (0.050, 0.030),
    "stock":   (0.075, 0.050),
}
MARGIN_FLOOR_FRAC   = 0.50    # floor as a fraction of base_pct
# Global scale on the RISK component only (not the premium, which is exact).
# One knob for "the approximation reads low against what my broker actually
# blocks". 1.15 = +15% on the risk term; total margin rises by slightly less
# than 15% because the premium leg is unscaled - most on far-OTM strikes where
# premium is a small share, least at the money.
#
# Scaling the risk term rather than the total is deliberate: multiplying the
# whole figure would divide every RoM by the same constant and change nothing
# about the ordering, which is the only thing this model is for.
MARGIN_SAFETY_MULT  = 1.15
# Multi-leg netting: margin = max(leg) + MARGIN_NET_K * min(leg). SPAN nets
# opposing legs heavily, so a short straddle is NOT 2x a single leg - costing it
# at 2x understates RoM by roughly 40%.
MARGIN_NET_K        = 0.25

# ── Exact exchange SPAN (nse_span_eod.py -> bhavcopy.duckdb) ────────────
# When the stored SPAN file covers a contract, screeners use exchange
# SPAN + ELM instead of the approximation above and report
# margin_source="span". Anything missing falls back to "approx".
import os as _os
SPAN_ENABLED       = True
SPAN_DB            = Path(_os.environ.get("BHAV_DB", r"C:\Users\aniru\od\bhavcopy.duckdb"))
SPAN_RELOAD_SEC    = 600      # re-probe for a newer stored file this often
SPAN_CACHE_FILES   = 3        # SpanBooks held in memory (~60 MB each, full universe)
# Broker / security-specific add-on on the SPAN component only. 1.0 = pure
# exchange. Zerodha matched RELIANCE at ~1.0 but HDFCBANK at ~1.12 (2026-10-01).
SPAN_BROKER_MULT   = 1.0
SPAN_ELM_OVERRIDES: dict = {}  # symbol -> ELM fraction; default 3.5% stock / 2% index

NSE_INDICES = {
    "NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY",
    "NIFTYNXT50", "SENSEX", "NIFTYIT",
}

# ICICI Breeze ticker map — loaded once at startup
ICICI_MAP: dict = {}

# Safe metrics whitelist for strike_trend
SAFE_METRICS = {
    "ce_ltp", "pe_ltp", "ce_oi", "pe_oi", "ce_oi_change", "pe_oi_change",
    "ce_iv", "pe_iv", "ce_delta", "pe_delta", "ce_gamma", "pe_gamma",
    "ce_theta", "pe_theta", "ce_vega", "pe_vega", "ce_volume", "pe_volume",
    "ce_rho", "pe_rho", "ce_vanna", "pe_vanna", "ce_charm", "pe_charm",
    "underlying_price", "ce_TPrice", "pe_TPrice", "ce_ltp_s", "pe_ltp_s",
    "ce_intrinsic_value", "pe_intrinsic_value", "ce_time_value", "pe_time_value",
    "ce_gexv", "pe_gexv", "net_gexv",
    "ce_nd1", "ce_nd2", "pe_nd1", "pe_nd2",
    "ce_prem_oi", "pe_prem_oi", "ce_prem_oi_chg", "pe_prem_oi_chg",
    "ce_delta_oi", "pe_delta_oi", "ce_delta_oi_chg", "pe_delta_oi_chg",
    "expected_move_theoretical", "expected_move_straddle",
}
